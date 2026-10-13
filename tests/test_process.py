import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'src/unity_bench/process.py'
SPEC = importlib.util.spec_from_file_location('bench_process_tests', PATH)
PROCESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROCESS)


def running(pid):
    # Zombies have stopped executing but some hosts reap orphan children later.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, timeout=5)
    return result.returncode == 0 and result.stdout.strip() and not result.stdout.strip().startswith('Z')


class ProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_normal_nonzero_exit_discards_output(self):
        code, elapsed, pid = PROCESS.launch([sys.executable, '-c', "print('sensitive output'); raise SystemExit(7)"], '.', 5)
        self.assertEqual(code, 7)
        self.assertGreater(elapsed, 0)
        self.assertGreater(pid, 0)

    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_timeout_kills_owned_wrapper_child_and_preserves_unrelated_process(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        child_pid = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                receipt = Path(directory) / 'child.pid'
                script = '''import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(30)
'''
                start = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, 'Owned adapter exceeded'):
                    PROCESS.launch([sys.executable, '-c', script, str(receipt)], directory, 1)
                self.assertLess(time.monotonic() - start, 4)
                child_pid = int(receipt.read_text())
                self.assertFalse(running(child_pid))
                self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate(); unrelated.wait(timeout=5)
            if child_pid and running(child_pid):
                os.kill(child_pid, signal.SIGKILL)

    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_normal_wrapper_exit_cleans_up_owned_background_child(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'child.pid'
            script = '''import pathlib, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
'''
            code, _, _ = PROCESS.launch([sys.executable, '-c', script, str(receipt)], directory, 5)
            self.assertEqual(code, 0)
            self.assertFalse(running(int(receipt.read_text())))

    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_permission_denied_requires_census_proof_of_no_live_owned_members(self):
        census = types.SimpleNamespace(returncode=0, stdout='123 Z+\n999 S\n')
        with patch.object(PROCESS.os, 'killpg', side_effect=PermissionError('denied')), \
                patch.object(PROCESS.subprocess, 'run', return_value=census) as read:
            self.assertFalse(PROCESS._signal_group(123, 0))
            self.assertFalse(PROCESS._signal_group(123, signal.SIGKILL))
            self.assertEqual(read.call_args.args[0], ['ps', '-A', '-o', 'pgid=', '-o', 'stat='])
            census.stdout = '123 Z\n123 S+\n999 Z\n'
            with self.assertRaisesRegex(RuntimeError, 'live members'):
                PROCESS._signal_group(123, 0)
            census.stdout = '999 S\n'
            self.assertFalse(PROCESS._signal_group(123, 0))

    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_permission_denied_with_unavailable_or_malformed_census_refuses(self):
        with patch.object(PROCESS.os, 'killpg', side_effect=PermissionError('denied')), \
                patch.object(PROCESS.subprocess, 'run') as read:
            for code, output in ((1, '123 Z'), (0, ''), (0, 'unparseable'), (0, '123 Z extra')):
                read.return_value = types.SimpleNamespace(returncode=code, stdout=output)
                with self.assertRaises(RuntimeError):
                    PROCESS._signal_group(123, 0)
            read.side_effect = subprocess.TimeoutExpired('ps', 2)
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                PROCESS._signal_group(123, 0)

    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_shared_unity_daemons_survive_cleanup_and_everything_else_in_the_group_does_not(self):
        client_pid = None
        with tempfile.TemporaryDirectory() as directory:
            client = Path(directory) / 'Unity.Licensing.Client'
            os.symlink(sys.executable, client)                # a process whose image name is the licensing client's
            receipt = Path(directory) / 'pids'
            script = '''import pathlib, subprocess, sys
client = subprocess.Popen([sys.argv[2], '-c', 'import time; time.sleep(30)'])
other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
pathlib.Path(sys.argv[1]).write_text(f'{client.pid} {other.pid}')
'''
            report = {}
            try:
                code, _, _ = PROCESS.launch([sys.executable, '-c', script, str(receipt), str(client)], directory, 10, report=report)
                client_pid, other_pid = map(int, receipt.read_text().split())
                self.assertEqual(code, 0)
                self.assertTrue(running(client_pid), 'the licensing client must outlive the Editor tree')
                self.assertFalse(running(other_pid))
                self.assertEqual(len(report['kept']), 1)
                self.assertTrue(report['kept'][0].lower().startswith('unity.licensing'))     # `ps` truncates long names on Linux
                self.assertEqual(len(report['terminated']), 1)                               # the other child, recorded by name
            finally:
                if client_pid and running(client_pid):
                    os.kill(client_pid, signal.SIGKILL)

    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_cleanup_report_names_what_was_terminated_and_wall_time_excludes_cleanup(self):
        script = '''import pathlib, subprocess, sys
subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
'''
        report = {}
        started = time.perf_counter()
        code, elapsed_ms, _ = PROCESS.launch([sys.executable, '-c', script], '.', 10, report=report)
        total_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(code, 0)
        self.assertEqual((len(report['terminated']), report['kept']), (1, []))
        self.assertGreater(total_ms - elapsed_ms, 300)       # killing a child that ignores SIGTERM takes the grace period; the measurement stopped before it
        slow = PROCESS.run([sys.executable, '-c', 'import time; time.sleep(30)'], '.', 1)
        self.assertTrue(slow.timed_out)
        self.assertEqual(slow.wall_ms, 1000.0)
        self.assertEqual(len(slow.cleanup['terminated']), 1)

    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_callbacks_observe_start_poll_and_finished_after_cleanup(self):
        calls = []
        def started(pid):
            calls.append(('started', pid))
        def finished():
            self.assertFalse(running(calls[0][1]))
            calls.append(('finished', None))
        code, _, pid = PROCESS.launch([sys.executable, '-c', 'import time; time.sleep(.4)'], '.', 5,
                                     started, lambda: calls.append(('poll', None)), finished)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ('started', pid))
        self.assertIn(('poll', None), calls)
        self.assertEqual(calls[-1], ('finished', None))

    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_callback_failure_cleans_up_before_propagating(self):
        pids = []
        def started(pid):
            pids.append(pid)
            raise ValueError('observer refused')
        with self.assertRaisesRegex(ValueError, 'observer refused'):
            PROCESS.launch([sys.executable, '-c', 'import time; time.sleep(30)'], '.', 5, started)
        self.assertFalse(running(pids[0]))

    @unittest.skipUnless(os.name == 'posix', 'POSIX process ownership')
    def test_invalid_timeout_and_launch_errors_are_safe(self):
        with patch.object(PROCESS.subprocess, 'Popen') as popen:
            for timeout in (0, -1, float('nan'), float('inf'), True):
                with self.assertRaises(ValueError):
                    PROCESS.launch(['private'], '.', timeout)
            popen.assert_not_called()
            popen.side_effect = OSError('private payload')
            with self.assertRaisesRegex(RuntimeError, '^Owned adapter could not be launched$'):
                PROCESS.launch(['private'], '.', 1)


if __name__ == '__main__':
    unittest.main()
