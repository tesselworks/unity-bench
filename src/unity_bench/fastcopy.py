"""Copying and deleting project trees: clones where the file system has them, retries where Windows holds files, long paths.

Unity writes into `Library/`, so a snapshot must be a real copy: never a hard link. A file-system clone (APFS `clonefile`,
Linux reflink) is a copy for every purpose that matters here (writes to either side diverge) and costs almost nothing.
"""
import concurrent.futures
import ctypes
import ctypes.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

sleep = time.sleep   # tests replace this
WORKERS = 8


def extended(path):
    """`path` as an absolute path the Windows file APIs accept beyond 260 characters (`\\\\?\\` prefix); unchanged elsewhere."""
    text = os.path.abspath(str(path))
    if os.name != 'nt' or text.startswith('\\\\?\\'):
        return text
    return '\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text


def _make_writable(function, path, *_):
    """rmtree error handler: a read-only file (Unity marks some) is made writable and the operation tried once more."""
    try:
        os.chmod(os.path.dirname(path), stat.S_IRWXU)   # POSIX: a file cannot be unlinked from a directory that is not writable
        os.chmod(path, stat.S_IRWXU if os.path.isdir(path) else stat.S_IWRITE | stat.S_IREAD)
        function(path)
    except FileNotFoundError:
        pass


def remove_path(path):
    """Delete a file, a link or a directory tree; a path that is already gone is fine. Raises OSError when something resists."""
    path = extended(path)
    if os.path.islink(path) or os.path.isfile(path):
        try:
            os.unlink(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.unlink(path)
        except FileNotFoundError:
            pass
    elif os.path.isdir(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_make_writable)
        else:
            shutil.rmtree(path, onerror=_make_writable)
    if os.path.lexists(path):
        raise PermissionError(f'could not delete {path}')


def remove_tree(path, attempts=6, delay=1.5):
    """`remove_path`, retried: a just-exited Editor (its asset import workers, Bee, an antivirus scan) can keep files open on Windows for a moment.

    Returns the number of retries that were needed. Raises OSError if the path is still there after `attempts` tries.
    """
    for attempt in range(attempts):
        try:
            remove_path(path)
            return attempt
        except OSError as error:
            if attempt == attempts - 1:
                raise OSError(f'could not delete {path} after {attempts} attempts: {error}') from None
            sleep(delay)
    return attempts


def _clonefile(source, target):
    """APFS clone of a whole directory tree in one call. False when the volume cannot (another volume, not APFS)."""
    library = ctypes.CDLL(ctypes.util.find_library('c') or None, use_errno=True)
    function = getattr(library, 'clonefile', None)
    if function is None:
        return False
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    return function(os.fsencode(source), os.fsencode(target), 0) == 0


def _cp_reflink(source, target):
    """`cp -a --reflink=auto`: a reflink copy where the file system has them (btrfs, XFS, ...), a plain copy elsewhere. False if `cp` rejects the flags."""
    try:
        return subprocess.run(['cp', '-a', '--reflink=auto', source, target], capture_output=True).returncode == 0
    except OSError:
        return False


def _threaded_copy(source, target, workers=WORKERS):
    """A plain copy keeping modification times (Unity's change detection looks at them) and links, files in parallel."""
    files = []

    def walk(src, dst):
        os.makedirs(extended(dst), exist_ok=True)
        with os.scandir(extended(src)) as entries:
            listing = list(entries)
        for entry in listing:
            s, d = os.path.join(src, entry.name), os.path.join(dst, entry.name)
            if entry.is_symlink():
                try:
                    os.symlink(os.readlink(extended(s)), extended(d))
                except OSError:   # Windows without the privilege to make links: copy what the link points at
                    shutil.copy2(extended(s), extended(d))
            elif entry.is_dir(follow_symlinks=False):
                walk(s, d)
            else:
                files.append((s, d))

    walk(str(source), str(target))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(lambda pair: shutil.copy2(extended(pair[0]), extended(pair[1])), files):
            pass
    return 'copy' if workers > 1 else 'copy (serial)'


def copy_tree(source, target):
    """Copy the directory `source` to the new path `target`. Returns how it was done: `clonefile`, `reflink-auto` or `copy`.

    macOS tries an APFS clone first and Linux `cp --reflink=auto`. Windows goes through the OS copy call per file (which clones blocks on
    ReFS and Dev Drives where Windows does that, and is a plain copy on NTFS). Anything else, or a failure of the fast route, is the plain copy.
    """
    source, target = str(source), str(target)
    if os.path.lexists(extended(target)):
        raise FileExistsError(target)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    if sys.platform == 'darwin' and _clonefile(source, target):
        return 'clonefile'
    if sys.platform.startswith('linux'):
        if _cp_reflink(source, target):
            return 'reflink-auto'
        remove_path(target)   # a half-made copy from a `cp` that failed part-way
    return _threaded_copy(source, target)


def copy_entry(source, target):
    """`copy_tree` for a directory, `copy2` for a file or a link. Returns the method."""
    source = Path(source)
    if source.is_symlink() or source.is_file():
        os.makedirs(os.path.dirname(os.path.abspath(str(target))), exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), target)
        else:
            shutil.copy2(extended(source), extended(target))
        return 'file'
    return copy_tree(source, target)


def sync_root(source, target, skip=()):
    """Make the top level of `target` equal to that of `source`: every entry of `target` not named in `skip` is deleted, then every entry of
    `source` not named in `skip` is copied in. `.git` of a worktree is kept by listing it in `skip`.

    Returns {'method': the copy methods used, 'retries': delete retries, 'removed_ms': ..., 'copied_ms': ...}.
    """
    source, target = Path(source), Path(target)
    start = time.perf_counter()
    retries = 0
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(target.iterdir()):
        if entry.name not in skip:
            retries += remove_tree(entry)
    removed = time.perf_counter()
    methods = set()
    for entry in sorted(source.iterdir()):
        if entry.name not in skip:
            methods.add(copy_entry(entry, target / entry.name))
    methods.discard('file')
    done = time.perf_counter()
    return {'method': '+'.join(sorted(methods)) or 'copy', 'retries': retries, 'removed_ms': (removed - start) * 1000, 'copied_ms': (done - removed) * 1000}
