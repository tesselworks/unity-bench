"""Per-variant project copies: where Library, edits and builds live, away from your working tree."""
import json
from pathlib import Path
import shutil
import subprocess
import time

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


def create(campaign, variant, work_root):
    """Make `<work_root>/<variant id>` and return (project directory, worktree root or None, provenance).

    Without `revision` the project directory is copied (minus Library/Temp/...). With one, a detached
    git worktree of that commit is used, and the project is the same relative path inside it.
    The provenance says which commit that really is (`HEAD` is resolved), whether the copy was dirty and what happened to the package.
    """
    source = Path(campaign['project']['path'])
    target = Path(work_root) / variant['id']
    if target.exists():
        if any(p.is_file() for p in target.rglob('*')):
            raise FileExistsError(f'{target} already exists and has files in it (left behind by an earlier run?); delete it or choose another --work directory')
        shutil.rmtree(target, ignore_errors=True)   # only empty directories: what a surviving process kept from a finished run
    worktree = None
    info = {'revision': variant.get('revision'), 'warnings': []}
    if variant.get('revision'):
        repo = Path(campaign['project']['repository'] or git(source, 'rev-parse', '--show-toplevel'))
        relative = source.relative_to(Path(git(source, 'rev-parse', '--show-toplevel')).resolve())
        git(repo, 'worktree', 'add', '--detach', str(target), variant['revision'])
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
    for _ in range(5):
        shutil.rmtree(Path(project) / 'Library', ignore_errors=True)
        if not (Path(project) / 'Library').exists():
            return
        time.sleep(2)
    raise OSError('could not delete Library')


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

