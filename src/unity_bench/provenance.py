"""Which code produced a result: the framework checkout, so a published number can be re-aggregated with the same code."""
from pathlib import Path
import subprocess

from . import __version__

ROOT = Path(__file__).resolve().parents[2]   # the repository root when running from a checkout (also for `pip install -e .`)


def framework():
    """`version` always; `commit` is `git describe --always --dirty --tags` of the checkout, or None for an installed copy."""
    commit = None
    if (ROOT / '.git').exists() and (ROOT / 'src' / 'unity_bench').is_dir():
        try:
            commit = subprocess.run(['git', '-C', str(ROOT), 'describe', '--always', '--dirty', '--tags'], capture_output=True, text=True,
                                    check=True, timeout=10).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
    return {'version': __version__, 'commit': commit}
