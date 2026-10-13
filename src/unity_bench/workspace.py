"""Per-variant project copies: where Library, edits and builds live, away from your working tree."""
from pathlib import Path
import shutil
import subprocess
import time

PACKAGE_NAME = 'com.tesselworks.unity-bench'
SKIP_DIRS = {'Library', 'Temp', 'Logs', 'obj', 'UserSettings', '.git', 'Builds', '.vs', '.idea'}
PLAYER_PROBE = 'PlayerProbe~'


def framework_package():
    path = Path(__file__).resolve().parents[2] / 'packages' / PACKAGE_NAME
    if not path.is_dir():
        raise FileNotFoundError(f'Unity package not found at {path}. Run unity-bench from a source checkout, '
                                'or set project.inject_package = false and add the package to your project yourself.')
    return path


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def ignore(directory, names):
    return [n for n in names if n in SKIP_DIRS]


def create(campaign, variant, work_root, use_player_probe):
    """Make `<work_root>/<variant id>` and return (project directory, worktree root or None).

    Without `revision` the project directory is copied (minus Library/Temp/...). With one, a detached
    git worktree of that commit is used, and the project is the same relative path inside it.
    """
    source = Path(campaign['project']['path'])
    target = Path(work_root) / variant['id']
    worktree = None
    if variant.get('revision'):
        repo = Path(campaign['project']['repository'] or git(source, 'rev-parse', '--show-toplevel'))
        relative = source.relative_to(Path(git(source, 'rev-parse', '--show-toplevel')).resolve())
        git(repo, 'worktree', 'add', '--detach', str(target), variant['revision'])
        worktree = (repo, target)
        project = target / relative
    else:
        shutil.copytree(source, target, ignore=ignore)
        project = target
    if campaign['project']['inject_package']:
        shutil.copytree(framework_package(), project / 'Packages' / PACKAGE_NAME, ignore=ignore)
    if use_player_probe:
        shutil.copytree(framework_package() / PLAYER_PROBE, project / 'Assets' / 'UnityBenchPlayerProbe')
    return project, worktree


def remove(project_dir, worktree):
    if worktree:
        repo, target = worktree
        subprocess.run(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(target)], capture_output=True)
    else:
        shutil.rmtree(Path(project_dir), ignore_errors=True)


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

