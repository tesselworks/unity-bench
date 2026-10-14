"""Start states of a measured process: `cold`, `warm` and `as-is`, and the global caches `cold` isolates.

  cold    Library is deleted and the global caches below are pointed at empty directories made for that one process
  warm    the project copy is put back to the snapshot taken after it was prepared and imported (Library included)
  as-is   nothing is touched: the process starts from whatever the previous one left

The isolation only uses switches this Unity honours (verified on 6000.7.0b2 and 7000.0.0a7 on macOS, see docs/methodology.md#cache-states):
environment variables set for the Editor process and nothing else. The user's real caches are never read for the isolated parts and never written.
"""
import contextlib
import os
from pathlib import Path
import re

STATES = ('cold', 'warm', 'as-is')
DEFAULT_STATE = {'editor.cold_open': 'cold'}   # every other kind starts as-is, as before v0.4
STATE_KINDS = ('editor.cold_open', 'editor.warm_open', 'editor.script_edit', 'editor.play', 'editor.adapter', 'editor.scene_play', 'player.build_time')   # kinds that start an Editor on the project

# What `cold` can isolate, by the name a campaign's [cache].isolate uses.
ISOLATE = {
    'compilation': {'env': 'BEE_CACHE_DIRECTORY', 'what': 'script compilation cache (Preferences > Compilation cache; macOS ~/Library/Unity/cache/bee)'},
    'upm': {'env': 'UPM_CACHE_ROOT', 'what': 'Package Manager global cache (registry tarballs, git checkouts, package database)'},
    'accelerator': {'args': ['-cacheServerEnableDownload', 'false', '-cacheServerEnableUpload', 'false'], 'what': 'Unity Accelerator / cache server (downloads and uploads off)'},
}
DEFAULT_ISOLATE = ('compilation', 'upm', 'accelerator')


def default_state(kind):
    return DEFAULT_STATE.get(kind, 'as-is')


def cold_isolation(scratch, isolate):
    """(environment, extra Editor arguments, description) for a cold process whose private cache directories live under `scratch`."""
    env, args, described = {}, [], {}
    for name in isolate:
        spec = ISOLATE[name]
        if 'env' in spec:
            directory = Path(scratch) / name
            directory.mkdir(parents=True, exist_ok=True)
            env[spec['env']] = str(directory)
            described[name] = f"{spec['env']}={directory}"
        else:
            args += spec['args']
            described[name] = ' '.join(spec['args'])
    return env, args, described


@contextlib.contextmanager
def environment(values):
    """Set environment variables for the Editor processes launched inside the block (they inherit this process's environment), then put them back."""
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


IMPORTS = re.compile(r'Imports: total=(\d+) \(actual=(\d+), local cache=(\d+), cache server=(\d+)\)')


def initial_import(log_path):
    """What the Editor's first asset refresh of a process did, from its log: {'total', 'actual', 'local_cache', 'cache_server'} or None.

    This is the evidence behind a start state: a cold open imports the whole project (`actual` is the number of assets), a warm one imports nothing
    (`total=0`), and a nonzero `cache_server` means an Accelerator served imports, which a cold start should not have.
    """
    try:
        text = Path(log_path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    match = IMPORTS.search(text)
    return dict(zip(('total', 'actual', 'local_cache', 'cache_server'), map(int, match.groups()))) if match else None
