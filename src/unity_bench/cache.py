"""Prepared project copies kept between campaigns and runs, and the cheap checks that say whether one can still be trusted.

An entry is a snapshot of a variant's project copy after it was prepared (copied or checked out, package injected, `project.prepare`
commands run) and imported once by an untimed Editor open, `Library/` included. It is stored under a key made of everything that
decides what that state is, and restored instead of preparing again. Layout of a cache directory:

    <cache>/e/<key>/meta.json   what the entry is, how it was made, its manifest
    <cache>/e/<key>/t/          the tree (the project copy, or the checkout root for a git worktree, without `.git`)
    <cache>/tmp/                entries being written (renamed into place when complete)
    <cache>/w/                  the default place for the live project copies (see `default_work`)

The live copy has to sit at the same absolute path every time: Unity's Library holds absolute paths, and a primed project opened from
another directory recompiles all its scripts (31 s instead of 0.5 s on the example project). The path is therefore part of the key.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sys
import time

from . import fastcopy

FORMAT = 1
SNAPSHOT_SKIP = ('.git', 'Temp', 'Logs')   # never part of a snapshot: git metadata of a worktree, and what a running Editor leaves
MAX_AUTO_HASHED_FILE = 8 << 20             # a file named on a prepare command line is hashed into the key up to this size


# ---- where -----------------------------------------------------------------------------------------------------------------

def default_cache_dir():
    """UNITY_BENCH_CACHE_DIR, else the user's cache directory of the OS."""
    if os.environ.get('UNITY_BENCH_CACHE_DIR'):
        return Path(os.environ['UNITY_BENCH_CACHE_DIR']).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Caches' / 'unity-bench'
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'unity-bench' / 'cache'
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'unity-bench'


def now():
    return datetime.datetime.now().isoformat(timespec='seconds')


# ---- hashing ---------------------------------------------------------------------------------------------------------------

def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode('utf-8')).hexdigest()


def file_digest(path):
    digest = hashlib.sha256()
    with open(fastcopy.extended(path), 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(path, skip=('__pycache__', 'bin', 'obj', '.DS_Store')):
    """Content digest of a (small) directory: relative paths and file bytes. For scripts and packages, not for projects."""
    digest = hashlib.sha256()
    base = Path(path)
    for current, dirs, files in os.walk(fastcopy.extended(base)):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        for name in sorted(files):
            if name in skip:
                continue
            full = os.path.join(current, name)
            digest.update(os.path.relpath(full, fastcopy.extended(base)).replace(os.sep, '/').encode() + b'\0')
            digest.update(file_digest(full).encode())
    return digest.hexdigest()


def stat_digest(path, skip):
    """Digest of (relative path, size, modification time) of every file under `path` outside the `skip` names: the identity of a project copy
    that has no commit to name it. A touched file changes it, which only costs a re-preparation; a changed file always does."""
    digest = hashlib.sha256()
    base = fastcopy.extended(path)
    count = 0
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        for name in sorted(files):
            if name in skip:
                continue
            full = os.path.join(current, name)
            info = os.lstat(full)
            digest.update(f'{os.path.relpath(full, base).replace(os.sep, "/")}\0{info.st_size}\0{info.st_mtime_ns // 1_000_000}\n'.encode())
            count += 1
    return digest.hexdigest(), count


# ---- the Editor ------------------------------------------------------------------------------------------------------------

VERSION_IN_PATH = re.compile(r'(?<![\d.])(\d{4}\.\d+\.\d+[abfp]\d+)(?![\w.])')


def editor_identity(editor):
    """Who the Editor is, before it runs: full version with revision where the install says it, else the version folder and a file fingerprint.

    macOS reads `Unity.app/Contents/Info.plist` (`7000.0.0a7 (581996e1a8f7)`), Windows the product version of `Unity.exe` (`7000.0.0a7_581996e1a8f7`
    on Hub installs). Everything else, or a failure, falls back to the version in the install path plus size and modification time of the executable,
    which changes when the Editor is reinstalled in place.
    """
    command = [editor] if isinstance(editor, str) else list(editor)
    executable = Path(command[-1])
    info = {'source': 'path', 'full': None, 'executable': str(executable)}
    try:
        for parent in executable.parents:
            plist = parent / 'Info.plist'
            if parent.name == 'Contents' and plist.is_file():
                data = plistlib.loads(plist.read_bytes())
                match = re.search(r'Unity version (\S+) \((\w+)\)', str(data.get('CFBundleGetInfoString', '')))
                if match:
                    info.update(source='Info.plist', full=f'{match.group(1)} ({match.group(2)})')
                elif data.get('CFBundleVersion'):
                    info.update(source='Info.plist', full=str(data['CFBundleVersion']))
                break
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    if info['full'] is None and os.name == 'nt':
        info.update(_windows_product_version(executable))
    if info['full'] is None:
        found = VERSION_IN_PATH.search(str(executable).replace('\\', '/'))
        try:
            stat = executable.stat()
            fingerprint = f'{stat.st_size}-{stat.st_mtime_ns // 1_000_000_000}'
        except OSError:
            fingerprint = 'unreadable'
        info.update(source='path', version=found.group(1) if found else None, fingerprint=fingerprint)
        info['full'] = f"{found.group(1) if found else executable.name} [{fingerprint}]"
    return info


def _windows_product_version(executable):
    """ProductVersion of the executable's version resource through version.dll (ctypes), or {} on any failure."""
    try:
        import ctypes
        from ctypes import wintypes
        version = ctypes.WinDLL('version', use_last_error=True)
        path = str(executable)
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return {}
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buffer):
            return {}
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(buffer, '\\VarFileInfo\\Translation', ctypes.byref(pointer), ctypes.byref(length)) or not length.value:
            return {}
        language, codepage = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort * 2)).contents
        if not version.VerQueryValueW(buffer, f'\\StringFileInfo\\{language:04x}{codepage:04x}\\ProductVersion', ctypes.byref(pointer), ctypes.byref(length)):
            return {}
        text = ctypes.wstring_at(pointer.value, length.value).rstrip('\0').strip()
        return {'source': 'version resource', 'full': text} if text else {}
    except Exception:   # ctypes can raise several things; the identity then falls back to the version in the path
        return {}


# ---- the key ---------------------------------------------------------------------------------------------------------------

def expand_probe(command):
    """A prepare command with its placeholders replaced by marker strings, to find the parts that are files on disk."""
    return [part.replace('{python}', '\0python').replace('{root}', '\0root').replace('{project}', '\0project').replace('{variant}', '\0variant') for part in command]


def prepare_digest(project, variant_id):
    """What the prepare step is: the commands, the files they name (up to 8 MB each, so a script's edits change the key) and the paths in
    `project.prepare_inputs` (files or directories, relative to the campaign file). Returns (digest, inputs hashed)."""
    campaign_dir = Path(project.get('campaign_dir') or '.')
    digest = hashlib.sha256()
    hashed = []
    uses_variant = False
    for command in project.get('prepare') or []:
        digest.update(json.dumps(command).encode() + b'\n')
        uses_variant = uses_variant or any('{variant}' in part for part in command)
        for part in command:
            expanded = part.replace('{campaign}', str(campaign_dir))
            candidate = Path(expanded)
            if '{' in expanded or not candidate.is_absolute():
                continue   # still has a placeholder (the copy's own paths), or is a plain argument and not a path
            try:
                if candidate.is_file() and candidate.stat().st_size <= MAX_AUTO_HASHED_FILE:
                    digest.update(f'file {candidate.name} {file_digest(candidate)}\n'.encode())
                    hashed.append(str(candidate))
            except OSError:
                pass
    for name in project.get('prepare_inputs') or []:
        path = (campaign_dir / name).resolve()
        if path.is_dir():
            digest.update(f'dir {name} {tree_digest(path)}\n'.encode())
        elif path.is_file():
            digest.update(f'file {name} {file_digest(path)}\n'.encode())
        else:
            digest.update(f'missing {name}\n'.encode())
        hashed.append(str(path))
    if uses_variant:
        digest.update(f'variant {variant_id}\n'.encode())
    return digest.hexdigest(), hashed


def components(cfg, variant, target, identity, editor, injected):
    """Everything that decides what a prepared copy is. `identity` comes from `workspace.source_identity`, `injected` is the digest of the Unity package
    put into the copy (None when the campaign does not inject it)."""
    prepare, hashed = prepare_digest(cfg['project'], variant['id'])
    return {'format': FORMAT, 'source': identity, 'editor': editor['full'], 'backend': variant['backend'], 'build': variant['build'],
            'injected_package': injected, 'prepare': prepare, 'path': os.path.normcase(os.path.abspath(str(target)))}, hashed


def key_of(parts):
    return sha(json.dumps(parts, sort_keys=True, separators=(',', ':')))[:16]


# ---- manifest --------------------------------------------------------------------------------------------------------------

def _walk_stats(path):
    """(files, bytes, newest modification in ms) under `path`, links not followed."""
    files = size = newest = 0
    for current, _, names in os.walk(fastcopy.extended(path)):
        for name in names:
            info = os.lstat(os.path.join(current, name))
            files += 1
            size += info.st_size
            newest = max(newest, info.st_mtime_ns // 1_000_000)
    return [files, size, newest]


def settings_digests(project):
    """{relative path: sha256} of every ProjectSettings file and of the package manifest and lock: what the Editor writes about the project itself."""
    out = {}
    project = Path(project)
    settings = project / 'ProjectSettings'
    if settings.is_dir():
        for current, _, names in os.walk(fastcopy.extended(settings)):
            for name in names:
                full = os.path.join(current, name)
                out['ProjectSettings/' + os.path.relpath(full, fastcopy.extended(settings)).replace(os.sep, '/')] = file_digest(full)
    for name in ('manifest.json', 'packages-lock.json'):
        if (project / 'Packages' / name).is_file():
            out['Packages/' + name] = file_digest(project / 'Packages' / name)
    return out


def settings_changed(before, after):
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def manifest(root, project_relative='.'):
    """A cheap fingerprint of a prepared tree: the top level, the bytes of every settings file, the package manifest and lock, the size and time of the
    asset databases in Library, and (files, bytes, newest time) of Assets and Packages. Hashes a few hundred KB and stats the project's own files.

    It is what makes "the key matches but the files were modified" detectable: the entry's manifest is stored when it is made and compared again
    before it is trusted, and after every restore. Library's inside is not walked; its top level and the asset databases stand for it.
    """
    root = Path(root)
    project = root / project_relative
    out = {'root': sorted(p.name for p in root.iterdir() if p.name not in SNAPSHOT_SKIP), 'settings': settings_digests(project), 'top_files': {}, 'trees': {}, 'library': {}}
    if project.is_dir():
        out['top_files'] = {p.name: p.stat().st_size for p in sorted(project.iterdir()) if p.is_file()}
    for name in ('Assets', 'Packages'):
        if (project / name).is_dir():
            out['trees'][name] = _walk_stats(project / name)
    library = project / 'Library'
    if library.is_dir():
        out['library']['entries'] = sorted(p.name for p in library.iterdir())
        for name in ('ArtifactDB', 'SourceAssetDB'):
            if (library / name).is_file():
                info = (library / name).stat()
                out['library'][name] = [info.st_size, info.st_mtime_ns // 1_000_000]
    return out


def manifest_digest(data):
    return sha(json.dumps(data, sort_keys=True, separators=(',', ':')))


def manifest_diff(expected, actual):
    """What differs between two manifests, as short phrases (at most a dozen)."""
    out = settings_changed(expected.get('settings', {}), actual.get('settings', {}))
    for section in ('root', 'top_files', 'trees', 'library'):
        if expected.get(section) != actual.get(section):
            if isinstance(expected.get(section), dict) and isinstance(actual.get(section), dict):
                out += [f'{section}/{n}' for n in sorted(k for k in set(expected[section]) | set(actual[section]) if expected[section].get(k) != actual[section].get(k))]
            else:
                out.append(section)
    return out[:12]


# ---- the store -------------------------------------------------------------------------------------------------------------

class Entry:
    def __init__(self, path, meta):
        self.path, self.meta = Path(path), meta

    @property
    def key(self):
        return self.meta['key']

    @property
    def tree(self):
        return self.path / 't'


class Store:
    """A cache directory. `reuse=False` makes a store that is written (so `warm` has a snapshot to restore) but never answers a lookup."""

    def __init__(self, root, reuse=True, persistent=True):
        self.root, self.reuse, self.persistent = Path(root), reuse, persistent

    def entry_path(self, key):
        return self.root / 'e' / key

    @property
    def default_work(self):
        return self.root / 'w'

    def lookup(self, key):
        """(Entry, None) for an entry that is there and still matches its manifest, else (None, why)."""
        path = self.entry_path(key)
        if not self.reuse:
            return None, 'reuse is off'
        if not (path / 'meta.json').is_file():
            return None, 'no entry'
        try:
            meta = json.loads((path / 'meta.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None, 'meta.json unreadable'
        if meta.get('format') != FORMAT or meta.get('key') != key:
            return None, 'entry was written by another cache format'
        if not (path / 't').is_dir():
            return None, 'tree missing'
        actual = manifest(path / 't', meta.get('project_relative', '.'))
        if manifest_digest(actual) != meta.get('manifest_digest'):
            return None, 'files changed since the snapshot: ' + (', '.join(manifest_diff(meta.get('manifest') or {}, actual)) or 'manifest mismatch')
        return Entry(path, meta), None

    def staging(self, key):
        """A fresh directory for an entry being made. Nothing is visible to other runs until `commit`."""
        path = self.root / 'tmp' / f'{key}.{os.getpid()}.{int(time.time())}'
        fastcopy.remove_tree(path)
        path.mkdir(parents=True)
        return path

    def commit(self, staged, key, meta):
        """Write meta.json and move the staged entry into place (replacing a stale or damaged one)."""
        meta = {'format': FORMAT, 'key': key, 'created_at': now(), 'last_used': now(), **meta}
        (Path(staged) / 'meta.json').write_text(json.dumps(meta, indent=1), encoding='utf-8')
        final = self.entry_path(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            fastcopy.remove_tree(final)
        for attempt in range(4):
            try:
                os.replace(fastcopy.extended(staged), fastcopy.extended(final))
                break
            except PermissionError:   # a virus scanner still looking at the new files (Windows)
                if attempt == 3:
                    raise
                fastcopy.sleep(1.5)
        return Entry(final, meta)

    def touch(self, entry):
        entry.meta['last_used'] = now()
        entry.meta['uses'] = entry.meta.get('uses', 0) + 1
        try:
            (entry.path / 'meta.json').write_text(json.dumps(entry.meta, indent=1), encoding='utf-8')
        except OSError:
            pass

    def discard(self, key):
        fastcopy.remove_tree(self.entry_path(key))

    def entries(self):
        out = []
        base = self.root / 'e'
        for path in sorted(base.iterdir()) if base.is_dir() else []:
            try:
                out.append(Entry(path, json.loads((path / 'meta.json').read_text(encoding='utf-8'))))
            except (OSError, ValueError):
                out.append(Entry(path, {'key': path.name, 'broken': True, 'last_used': '', 'created_at': ''}))
        return out

    def prune(self, older_than=None, keep=None, dry_run=False, current=None):
        """Remove entries last used more than `older_than` seconds ago, then all but the `keep` most recently used. Broken entries and stale staging
        directories always go. Returns the removed entries (with 'size_bytes')."""
        current = current or time.time()
        entries = self.entries()
        removed = [e for e in entries if e.meta.get('broken')]
        alive = sorted((e for e in entries if not e.meta.get('broken')), key=lambda e: e.meta.get('last_used', ''), reverse=True)
        if older_than is not None:
            cutoff = datetime.datetime.fromtimestamp(current - older_than).isoformat(timespec='seconds')
            removed += [e for e in alive if e.meta.get('last_used', '') < cutoff]
            alive = [e for e in alive if e not in removed]
        if keep is not None:
            removed += alive[keep:]
        if not dry_run:
            for e in removed:
                fastcopy.remove_tree(e.path)
            for stale in (self.root / 'tmp').iterdir() if (self.root / 'tmp').is_dir() else []:
                if current - stale.stat().st_mtime > 3600:
                    fastcopy.remove_tree(stale)
        return removed


DURATION = re.compile(r'^(\d+(?:\.\d+)?)([smhdw])$')


def parse_age(text):
    """'12h', '30d', '2w' (s, m, h, d, w) to seconds."""
    match = DURATION.match(text.strip())
    if not match:
        raise ValueError(f'{text!r} is not a duration such as 12h, 30d or 2w')
    return float(match.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[match.group(2)]


def human_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def listing(store):
    """Lines for `cache list`."""
    entries = store.entries()
    if not entries:
        return [f'{store.root}: no prepared copies']
    lines = [f'{store.root}: {len(entries)} prepared cop{"y" if len(entries) == 1 else "ies"}',
             f"{'key':<17}{'editor':<26}{'variant':<14}{'source':<22}{'size':>10}  {'last used':<20}{'reused':>7}  built in"]
    for e in entries:
        m = e.meta
        if m.get('broken'):
            lines.append(f'{e.key:<17}(unreadable: remove with `cache prune`)')
            continue
        c = m.get('components', {})
        source = c.get('source', {})
        what = (source.get('commit') or source.get('tree') or '?')[:12] + (' wt' if source.get('mode') == 'worktree' else ' copy')
        lines.append(f"{e.key:<17}{str(c.get('editor', '?'))[:25]:<26}{str(m.get('variant', '?'))[:13]:<14}{what:<22}{human_size(m.get('size_bytes', 0)):>10}  "
                     f"{m.get('last_used', ''):<20}{m.get('uses', 0):>7}  {m.get('built_seconds', 0):.0f} s")
    return lines


def tree_size(path):
    files, size, _ = _walk_stats(path)
    return files, size
