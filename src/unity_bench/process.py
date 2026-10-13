"""Bounded adapter lifetime in an owned POSIX process session.

Adapters and their children must remain in the inherited process group: detached
services are outside this protocol. Windows uses suspended creation with atomic Job Object association. This module
does not change existing project-specific Windows campaign runners.

Shared Unity daemons are the one exception to "everything in the group dies". The Unity Licensing Client is started by
an Editor but serves every Editor on the machine; killing it with the Editor's tree makes the next Editor pay to start it
again (and a user's own session loses it). Cleanup therefore terminates every other member of the group and leaves
processes whose image name starts with `unity.licensing` alone. What was terminated and what was kept is recorded.
"""
import importlib.util
import math
from pathlib import Path
import os
import signal
import subprocess
import time


_GRACE_SECONDS = 0.5
SHARED_DAEMONS = ('unity.licensing',)   # lower-case image-name prefixes of processes an Editor starts for the whole machine


def is_shared_daemon(name):
    return os.path.basename(name).lower().startswith(SHARED_DAEMONS)


def _group_has_live_members(group):
    """Resolve EPERM using a bounded census of PGID/status, never command lines.

    Some macOS lifetimes leave only orphan zombies in a group and killpg reports
    EPERM. A zombie is not executing; any live member or unreadable census must
    still refuse successful cleanup rather than assume the process disappeared.
    """
    try:
        result = subprocess.run(['ps', '-A', '-o', 'pgid=', '-o', 'stat='],
                                capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('Owned adapter group status is unavailable') from None
    if result.returncode or not result.stdout.strip():
        raise RuntimeError('Owned adapter group status is unavailable')
    live = False
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            raise RuntimeError('Owned adapter group status is invalid')
        if int(fields[0]) == group and not fields[1].startswith('Z'):
            live = True
    return live


def _signal_group(group, sig):
    try:
        os.killpg(group, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        if _group_has_live_members(group):
            raise RuntimeError('Owned adapter group has live members that cannot be signalled') from None
        return False


def _members(group):
    """[(pid, image name, live)] of the processes in `group`, or None when `ps` cannot say."""
    try:
        result = subprocess.run(['ps', '-A', '-o', 'pid=', '-o', 'pgid=', '-o', 'stat=', '-o', 'comm='], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    members = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and fields[0].isdigit() and fields[1].isdigit() and int(fields[1]) == group:
            members.append((int(fields[0]), os.path.basename(fields[3]).strip('()'), not fields[2].startswith('Z')))   # ps shows a process that is between fork and exec as (name)
    return members


def _kill_all_but_shared(group, deadline):
    """Signal every live member except shared daemons, one by one, until only those are left (the group may keep forking meanwhile)."""
    signal_number = signal.SIGTERM
    grace = time.monotonic() + _GRACE_SECONDS
    while time.monotonic() < deadline:
        members = _members(group)
        if members is None:
            raise RuntimeError('Owned adapter group status is unavailable')
        victims = [pid for pid, name, live in members if live and not is_shared_daemon(name)]
        if not victims:
            return
        if time.monotonic() >= grace:
            signal_number = signal.SIGKILL
        for pid in victims:
            try:
                os.kill(pid, signal_number)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.02)
    raise RuntimeError('Owned adapter cleanup did not complete')


def _cleanup(process, report=None):
    # start_new_session fixes the owned group ID to this child PID. Never infer a
    # group from another process or signal the caller's inherited process group.
    group = process.pid
    members = _members(group)
    live = [(name, is_shared_daemon(name)) for _, name, alive in members or [] if alive]
    if report is not None:
        report.update(terminated=[n for n, shared in live if not shared], kept=[n for n, shared in live if shared])
    if any(shared for _, shared in live):
        _kill_all_but_shared(group, time.monotonic() + 3)
    elif _signal_group(group, signal.SIGTERM):
        deadline = time.monotonic() + _GRACE_SECONDS
        while time.monotonic() < deadline:
            process.poll()  # Reap the wrapper while its children finish.
            if not _signal_group(group, 0):
                break
            time.sleep(0.02)
        _signal_group(group, signal.SIGKILL)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Owned adapter cleanup did not complete') from None


def launch(argv, cwd, timeout, on_started=None, on_poll=None, on_finished=None, report=None):
    """Return exit code, wall milliseconds until the wrapper exited (cleanup is not included) and wrapper PID.

    `report`, if given, is filled with the image names `terminated` and `kept` (shared daemons) by cleanup.

    Output is discarded, and errors contain neither command arguments nor
    subprocess output. A completed wrapper cannot leave children running in the
    owned group; both normal and abnormal exits clean up that group's lifetime.
    """
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Adapter timeout must be finite and positive')
    if os.name == 'nt':
        spec = importlib.util.spec_from_file_location(
            'unity_bench_windows_process', Path(__file__).with_name('windows_process.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.launch_windows(argv, cwd, timeout, on_started, on_poll, on_finished, report)
    if os.name != 'posix':
        raise RuntimeError('Adapter ownership is unavailable on this platform')
    start = time.perf_counter()
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise RuntimeError('Owned adapter could not be launched') from None
    try:
        if on_started is not None:
            on_started(process.pid)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Owned adapter exceeded its timeout')
            try:
                code = process.wait(timeout=min(0.25, remaining))
                elapsed = (time.perf_counter() - start) * 1000
                break
            except subprocess.TimeoutExpired:
                if on_poll is not None:
                    on_poll()
    finally:
        _cleanup(process, report)
        if on_finished is not None:
            on_finished()
    return code, elapsed, process.pid


class Run:
    """Outcome of `run`: `exit_code` is None when the process tree was killed on timeout."""

    def __init__(self, exit_code, wall_ms, pid, timed_out, cleanup=None):
        self.exit_code, self.wall_ms, self.pid, self.timed_out, self.cleanup = exit_code, wall_ms, pid, timed_out, cleanup

    @property
    def ok(self):
        return self.exit_code == 0 and not self.timed_out


def run(argv, cwd, timeout, on_poll=None):
    """Launch `argv` in an owned process tree, kill the whole tree on timeout, never raise for a timeout.

    `on_poll(pid)` is called about every 250 ms while the process runs (used for host sampling).
    """
    pids, report = [], {}
    poll = (lambda: on_poll(pids[0])) if on_poll else None
    try:
        code, wall_ms, pid = launch(argv, cwd, timeout, on_started=pids.append, on_poll=poll, report=report)
    except TimeoutError:
        return Run(None, timeout * 1000.0, pids[0] if pids else None, True, report)
    return Run(code, wall_ms, pid, False, report)
