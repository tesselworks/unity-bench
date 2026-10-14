"""Per-variant project copies: where Library, edits and builds live, away from your working tree."""
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import time

from . import cache, fastcopy

PACKAGE_NAME = 'com.tesselworks.unity-bench'
SKIP_DIRS = {'Library', 'Temp', 'Logs', 'obj', 'UserSettings', '.git', 'Builds', '.vs', '.idea'}


def framework_package():
    """The Unity package to inject: the copy shipped inside an installed wheel, else the one in a source checkout."""
    here = Path(__file__).resolve().parent
    for path in (here / 'unity_package', here.parents[1] / 'packages' / PACKAGE_NAME):
        if (path / 'package.json').is_file():
            return path
    raise FileNotFoundError('Unity package not found (looked inside the installed unity_bench and in a source checkout). '
                            'Reinstall with `pip install .` or `pip install -e .`, or set project.inject_package = false and add the package to your project yourself.')


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def git_state(directory):
    """HEAD commit and the number of uncommitted paths under `directory`, or None outside a git repository."""
    try:
        return {'commit': git(directory, 'rev-parse', 'HEAD'), 'dirty_files': len(git(directory, 'status', '--porcelain', '--', '.').splitlines())}
    except (OSError, subprocess.CalledProcessError):
        return None


def ignore(directory, names):
    return [n for n in names if n in SKIP_DIRS]


def outside_references(source):
    """`file:` dependencies of the project's manifest that point outside the project: a plain copy leaves them behind."""
    try:
        deps = json.loads((Path(source) / 'Packages' / 'manifest.json').read_text(encoding='utf-8')).get('dependencies', {})
    except (OSError, ValueError, AttributeError):
        return []
    root = Path(source).resolve()
    return [f'{name} -> {value}' for name, value in deps.items()
            if isinstance(value, str) and value.startswith('file:') and not (root / 'Packages' / value[5:]).resolve().is_relative_to(root)]


def inject_package(project):
    """Put the framework's Unity package into the copy. An embedded copy the project already has is replaced, so the C# that runs
    is the version that belongs to this runner. Returns what happened, for the provenance record."""
    target = Path(project) / 'Packages' / PACKAGE_NAME
    replaced = target.is_symlink() or target.exists()
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    shutil.copytree(framework_package(), target, ignore=ignore)
    return 'replaced the project\'s embedded copy' if replaced else 'injected'


def target_path(work_root, variant):
    return Path(work_root) / variant['id']


def clear_target(target, managed=False):
    """Make `target` free. A directory with files in it is somebody's (an earlier run's or the user's) and stops the run, unless the run owns the
    directory (`managed`: the default work directory inside the cache, where leftovers of a killed run are ours to delete)."""
    target = Path(target)
    if not target.exists():
        return
    if managed:
        fastcopy.remove_tree(target)
    elif any(p.is_file() for p in target.rglob('*')):
        raise FileExistsError(f'{target} already exists and has files in it (left behind by an earlier run?); delete it or choose another --work directory')
    else:
        shutil.rmtree(target, ignore_errors=True)   # only empty directories: what a surviving process kept from a finished run


def git_blob(repo, commit, path):
    """The bytes of `path` at `commit`, or None when the commit has no such file."""
    done = subprocess.run(['git', '-C', str(repo), 'show', f'{commit}:{path}'], capture_output=True)
    return done.stdout if done.returncode == 0 else None


UNITY_TOP_LEVEL = ('Assets', 'Packages', 'ProjectSettings')   # what Unity reads of a project that is the root of its repository (see `content_identity`)


def git_object(repo, commit, path):
    """The object id of `path` at `commit` (a tree for a directory, a blob for a file), or None when the commit has no such path."""
    done = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', f'{commit}:{path}'], capture_output=True, text=True)
    return done.stdout.strip() or None if done.returncode == 0 else None


def local_package_paths(manifest_bytes):
    """{package name: value} of the `file:` dependencies in the bytes of a `Packages/manifest.json` (none when it is missing or not JSON)."""
    try:
        deps = json.loads((manifest_bytes or b'{}').decode('utf-8-sig')).get('dependencies', {})
    except (ValueError, AttributeError):
        return {}
    return {name: value for name, value in deps.items() if isinstance(value, str) and value.startswith('file:')}


def content_identity(repo, commit, top, source, relative):
    """What of the repository at `commit` the prepared copy is made from, as object ids instead of the commit's own id, so a commit that changes
    nothing Unity reads leaves it, and the cache key, as it was:

    - `project`: the git tree of the project directory (`<commit>:<relative>`); when the project is the repository root, the trees of `Assets`, `Packages` and
      `ProjectSettings` only (the rest of the root, `.github` and the like, is not read by Unity; `UserSettings` and `Library` are not versioned).
    - `local_packages`: for every `file:` dependency of `Packages/manifest.json`, resolved from the project's `Packages` directory, the tree (or blob, for a
      tarball) at `commit` when it lies inside the repository, else a stat digest of what is on disk (a path outside the repository has no commit)."""
    prefix = '' if relative == '.' else relative + '/'
    if relative == '.':
        project = {name: git_object(repo, commit, name) for name in UNITY_TOP_LEVEL}
    else:
        project = git_object(repo, commit, relative)
    packages = {}
    for name, value in sorted(local_package_paths(git_blob(repo, commit, prefix + 'Packages/manifest.json')).items()):
        text = value[5:]
        outside = None
        if os.path.isabs(text) or re.match(r'^[A-Za-z]:', text):   # absolute: in the repository only if it points into this checkout
            outside = Path(text).resolve()
            try:
                inside = outside.relative_to(top).as_posix()
            except ValueError:
                inside = None
        else:
            inside = posixpath.normpath(posixpath.join(prefix + 'Packages', text))
            if inside == '..' or inside.startswith('../'):
                inside = None
        if inside is not None:
            oid = git_object(repo, commit, inside) if inside != '.' else git(repo, 'rev-parse', f'{commit}^{{tree}}')
            packages[name] = {'git': inside, 'id': oid}
        else:
            outside = outside or (source / 'Packages' / text).resolve()
            if outside.is_dir():
                digest, count = cache.stat_digest(outside, SKIP_DIRS)
                packages[name] = {'outside': str(outside), 'stat': digest, 'files': count}
            elif outside.is_file():
                info = outside.stat()
                packages[name] = {'outside': str(outside), 'stat': cache.sha(f'{info.st_size}\0{info.st_mtime_ns // 1_000_000}'), 'files': 1}
            else:
                packages[name] = {'outside': str(outside), 'stat': None}
    return {'project': project, 'local_packages': packages}


def source_identity(campaign, variant):
    """What the variant's project is before anything is prepared, for the cache key.

    A worktree variant is identified by content, not by commit: the git objects of the project and of its local packages at the resolved commit (see
    `content_identity`), the digest of the package lock, and the project's place inside the repository. The resolved `commit` is part of the record, for
    provenance and for checking the worktree out, but `cache.components` leaves it out of the key. A plain copy is a digest of the files (path, size, time),
    the package lock, and the commit its directory is at."""
    source = Path(campaign['project']['path'])
    if variant.get('revision'):
        top = Path(git(source, 'rev-parse', '--show-toplevel')).resolve()
        relative = source.resolve().relative_to(top).as_posix()
        repo = Path(campaign['project']['repository'] or top)
        commit = git(repo, 'rev-parse', '--verify', variant['revision'] + '^{commit}')
        lock = git_blob(repo, commit, f"{relative + '/' if relative != '.' else ''}Packages/packages-lock.json")
        return {'mode': 'worktree', 'commit': commit, 'relative': relative, 'package_lock': cache.sha(lock) if lock is not None else None,
                **content_identity(repo, commit, top, source.resolve(), relative)}
    tree, count = cache.stat_digest(source, SKIP_DIRS)
    lock = source / 'Packages' / 'packages-lock.json'
    return {'mode': 'copy', 'tree': tree, 'files': count, 'package_lock': cache.file_digest(lock) if lock.is_file() else None,
            'commit': (git_state(source) or {}).get('commit')}


def injected_digest(campaign):
    return cache.tree_digest(framework_package()) if campaign['project']['inject_package'] else None


def create(campaign, variant, work_root, commit=None, managed=False):
    """Make `<work_root>/<variant id>` and return (project directory, worktree root or None, provenance).

    Without `revision` the project directory is copied (minus Library/Temp/...). With one, a detached
    git worktree of that commit is used, and the project is the same relative path inside it.
    The provenance says which commit that really is (`HEAD` is resolved), whether the copy was dirty and what happened to the package.
    `commit` is the already resolved commit to check out, so that the cache key and the checkout cannot disagree.
    """
    source = Path(campaign['project']['path'])
    target = target_path(work_root, variant)
    clear_target(target, managed)
    worktree = None
    info = {'revision': variant.get('revision'), 'warnings': []}
    if variant.get('revision'):
        repo = Path(campaign['project']['repository'] or git(source, 'rev-parse', '--show-toplevel'))
        relative = source.relative_to(Path(git(source, 'rev-parse', '--show-toplevel')).resolve())
        subprocess.run(['git', '-C', str(repo), 'worktree', 'prune'], capture_output=True)   # a killed earlier run can leave its registration behind
        git(repo, 'worktree', 'add', '--detach', str(target), commit or variant['revision'])
        worktree = (repo, target)
        project = target / relative
        info.update(mode='worktree', **(git_state(target) or {}))
    else:
        shutil.copytree(source, target, ignore=ignore)
        project = target
        info.update(mode='copy', **(git_state(source) or {'commit': None, 'dirty_files': None}))
        info['warnings'] += [f'the manifest depends on {ref}, outside the project; a plain copy does not contain it' for ref in outside_references(source)]
    info['package'] = inject_package(project) if campaign['project']['inject_package'] else 'not injected'
    return project, worktree, info


def materialize(campaign, variant, work_root, entry, managed=False):
    """Make `<work_root>/<variant id>` from a cache entry instead of preparing it. Returns (project, worktree or None, provenance, copy result).

    A worktree is registered with git again (`--no-checkout`, so git does not write the files a second time) at the commit the entry was made from
    (`meta['source']['commit']`, which can be an older commit than the one this run asked for: the key is the content, see `cache.components`) and the
    snapshot's files are copied over it; a plain copy is the snapshot. The provenance is the one recorded when the entry was made; the caller says which
    commit this run resolved.
    """
    target = target_path(work_root, variant)
    clear_target(target, managed)
    meta = entry.meta
    info = dict(meta.get('info') or {})
    info['warnings'] = list(info.get('warnings') or [])
    worktree = None
    source = Path(campaign['project']['path'])
    if meta.get('mode') == 'worktree':
        repo = Path(campaign['project']['repository'] or git(source, 'rev-parse', '--show-toplevel'))
        subprocess.run(['git', '-C', str(repo), 'worktree', 'prune'], capture_output=True)
        git(repo, 'worktree', 'add', '--detach', '--no-checkout', str(target), meta['source']['commit'])
        worktree = (repo, target)
    result = restore_root(entry.tree, target)
    if worktree:
        subprocess.run(['git', '-C', str(target), 'reset', '-q'], capture_output=True)   # index = HEAD, so `git status` shows what prepare changed
    relative = meta.get('project_relative', '.')
    return (target / relative if relative != '.' else target), worktree, info, result


def snapshot_root(root, destination):
    """Copy the root of a prepared copy (the project, or the checkout of a worktree) to the empty directory `destination`, without `.git`, Temp and Logs.
    Retried: a just-exited Editor can still be releasing files on Windows."""
    last = None
    for attempt in range(4):
        try:
            if Path(destination).exists():
                fastcopy.remove_tree(destination)
            return fastcopy.sync_root(root, destination, skip=cache.SNAPSHOT_SKIP)
        except OSError as error:
            last = error
            fastcopy.sleep(2)
    raise OSError(f'could not snapshot {root}: {last}')


def restore_root(snapshot, root, skip=('.git',)):
    """Put the root of a prepared copy back to a snapshot. Returns the `sync_root` result."""
    last = None
    for attempt in range(3):
        try:
            return fastcopy.sync_root(snapshot, root, skip=skip)
        except OSError as error:
            last = error
            fastcopy.sleep(2)
    raise OSError(f'could not restore {root}: {last}')


def remove(project_dir, worktree):
    """Delete a variant's copy and return None, or return its path when something still holds it.

    A shared Unity daemon that survived cleanup (see process.py) can have the copy as its working directory, which on Windows
    keeps that directory from being deleted; the contents go, the empty directory may stay until the daemon exits.
    """
    target = Path(worktree[1] if worktree else project_dir)
    if worktree:
        subprocess.run(['git', '-C', str(worktree[0]), 'worktree', 'remove', '--force', str(target)], capture_output=True)
    for attempt in range(3):
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            break
        time.sleep(1.5)
    if worktree:
        subprocess.run(['git', '-C', str(worktree[0]), 'worktree', 'prune'], capture_output=True)
    return str(target) if target.exists() else None


def remove_library(project):
    """Delete Library, retrying briefly: a just-exited Editor can still be releasing files."""
    try:
        fastcopy.remove_tree(Path(project) / 'Library', attempts=5, delay=2)
    except OSError:
        raise OSError('could not delete Library') from None


def snapshot(project, relative_paths):
    """Bytes of the existing files among `relative_paths`, so an Editor process's edits can be undone."""
    return {rel: (Path(project) / rel).read_bytes() for rel in relative_paths if (Path(project) / rel).is_file()}


def restore(project, saved):
    """Put back any snapshotted file that changed. Returns the relative paths that had to be restored."""
    restored = []
    for rel, data in saved.items():
        path = Path(project) / rel
        if not path.is_file() or path.read_bytes() != data:
            restored.append(rel)
            path.write_bytes(data)
    return restored

