"""Windows ownership mocks plus native self-tests selected automatically on Windows."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

PATH = Path(__file__).resolve().parents[1] / 'src/unity_bench/windows_process.py'
SPEC = importlib.util.spec_from_file_location('bench_windows_process_tests', PATH)
WINDOWS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WINDOWS)

PROCESS_SPEC = importlib.util.spec_from_file_location('bench_public_process_tests', PATH.with_name('process.py'))
PROCESS = importlib.util.module_from_spec(PROCESS_SPEC)
PROCESS_SPEC.loader.exec_module(PROCESS)


def fake_api():
    events = []
    api = types.SimpleNamespace(events=events)
    api.CreateJobObjectW = Mock(return_value=201)
    def limits(job, kind, pointer, size):
        assert job == 201 and kind == 9
        assert pointer._obj.BasicLimitInformation.LimitFlags == WINDOWS.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        events.append('limits')
        return 1
    api.SetInformationJobObject = Mock(side_effect=limits)
    def initialize(attributes, count, flags, size):
        assert count == 2
        size._obj.value = 64
        return int(attributes is not None)
    api.InitializeProcThreadAttributeList = Mock(side_effect=initialize)
    api.DeleteProcThreadAttributeList = Mock()
    api.CreateFileW = Mock(return_value=202)
    def attribute(attributes, flags, kind, values, size, previous, returned):
        if kind == WINDOWS.PROC_THREAD_ATTRIBUTE_HANDLE_LIST:
            assert list(values) == [202]
            events.append('handle-list')
        elif kind == WINDOWS.PROC_THREAD_ATTRIBUTE_JOB_LIST:
            assert list(values) == [201]
            events.append('job-list')
        else:
            raise AssertionError('Unexpected attribute')
        return 1
    api.UpdateProcThreadAttribute = Mock(side_effect=attribute)
    def create(executable, command_line, process_attributes, thread_attributes, inherit, flags, environment, cwd, startup, info):
        assert 'job-list' in events and 'handle-list' in events
        assert flags & WINDOWS.CREATE_SUSPENDED and flags & WINDOWS.EXTENDED_STARTUPINFO_PRESENT
        assert inherit and environment is None
        assert startup._obj.StartupInfo.cb == ctypes.sizeof(WINDOWS.StartupInfoEx)
        assert (startup._obj.StartupInfo.hStdInput, startup._obj.StartupInfo.hStdOutput, startup._obj.StartupInfo.hStdError) == (202, 202, 202)
        events.append('created-suspended')
        info._obj.hProcess, info._obj.hThread = 301, 302
        info._obj.dwProcessId, info._obj.dwThreadId = 123, 124
        return 1
    api.CreateProcessW = Mock(side_effect=create)
    def association(process, job, value):
        assert process == 301 and job == 201
        value._obj.value = 1
        events.append('association-verified')
        return 1
    api.IsProcessInJob = Mock(side_effect=association)
    def resume(thread):
        assert thread == 302
        events.append('resumed')
        return 1
    api.ResumeThread = Mock(side_effect=resume)
    api.WaitForSingleObject = Mock(side_effect=[WINDOWS.WAIT_TIMEOUT, WINDOWS.WAIT_OBJECT_0, WINDOWS.WAIT_OBJECT_0])
    def code(process, value):
        value._obj.value = 7
        return 1
    api.GetExitCodeProcess = Mock(side_effect=code)
    def terminate_job(job, code):
        assert job == 201
        events.append('terminated-job')
        return 1
    api.TerminateJobObject = Mock(side_effect=terminate_job)
    api.TerminateProcess = Mock(return_value=1)
    def accounting(job, kind, pointer, size, returned):
        assert job == 201 and kind == 1
        pointer._obj.ActiveProcesses = 0
        return 1
    api.QueryInformationJobObject = Mock(side_effect=accounting)
    def close(handle):
        events.append('closed-' + str(handle))
        return 1
    api.CloseHandle = Mock(side_effect=close)
    return api


class WindowsProcessMockTests(unittest.TestCase):
    def launch(self, api, **callbacks):
        return WINDOWS.launch_windows([sys.executable, '-c', 'pass'], '.', 5, _api=api, **callbacks)

    def test_containment_precedes_user_code_and_callbacks_finish_after_cleanup(self):
        api = fake_api()
        def started(pid):
            self.assertEqual(pid, 123)
            self.assertNotIn('resumed', api.events)
            self.assertIn('association-verified', api.events)
            api.events.append('started')
        def finished():
            self.assertIn('terminated-job', api.events)
            self.assertIn('closed-201', api.events)
            api.events.append('finished')
        code, elapsed, pid = self.launch(api, on_started=started, on_poll=lambda: api.events.append('poll'), on_finished=finished)
        self.assertEqual((code, pid), (7, 123))
        self.assertGreater(elapsed, 0)
        self.assertLess(api.events.index('started'), api.events.index('resumed'))
        self.assertIn('poll', api.events)
        self.assertLess(api.events.index('finished'), api.events.index('closed-301'))
        self.assertEqual({call.args[0] for call in api.CloseHandle.call_args_list}, {201, 202, 301, 302})
        api.DeleteProcThreadAttributeList.assert_called_once()

    def test_atomic_job_attribute_failure_refuses_before_process_creation(self):
        api = fake_api()
        old = api.UpdateProcThreadAttribute.side_effect
        api.UpdateProcThreadAttribute.side_effect = lambda *args: 0 if args[2] == WINDOWS.PROC_THREAD_ATTRIBUTE_JOB_LIST else old(*args)
        with self.assertRaisesRegex(RuntimeError, 'Atomic Windows job association'):
            self.launch(api)
        api.CreateProcessW.assert_not_called()
        api.ResumeThread.assert_not_called()
        self.assertEqual({call.args[0] for call in api.CloseHandle.call_args_list}, {201, 202})

    def test_configuration_failure_never_creates_or_resumes_a_process(self):
        for field in ('SetInformationJobObject', 'InitializeProcThreadAttributeList', 'CreateFileW'):
            with self.subTest(api=field):
                api = fake_api()
                function = getattr(api, field)
                function.side_effect = None
                function.return_value = 0
                with self.assertRaises(RuntimeError):
                    self.launch(api)
                api.CreateProcessW.assert_not_called()
                api.ResumeThread.assert_not_called()
                self.assertIn('closed-201', api.events)

    def test_failed_association_resume_and_observer_cleanup_suspended_process(self):
        for failure in ('association', 'resume', 'observer'):
            with self.subTest(failure=failure):
                api = fake_api()
                api.WaitForSingleObject.side_effect = None
                api.WaitForSingleObject.return_value = WINDOWS.WAIT_OBJECT_0
                callbacks = {}
                if failure == 'association':
                    api.IsProcessInJob.side_effect = None
                    api.IsProcessInJob.return_value = 0
                elif failure == 'resume':
                    api.ResumeThread.side_effect = None
                    api.ResumeThread.return_value = WINDOWS.WAIT_FAILED
                else:
                    def refuse(pid):
                        raise ValueError('observer refused')
                    callbacks['on_started'] = refuse
                with self.assertRaises((RuntimeError, ValueError)):
                    self.launch(api, **callbacks)
                self.assertIn('terminated-job', api.events)
                self.assertIn('closed-201', api.events)
                if failure != 'resume':
                    api.ResumeThread.assert_not_called()

    def test_wait_failure_stops_whole_job(self):
        api = fake_api()
        api.WaitForSingleObject.side_effect = [WINDOWS.WAIT_FAILED, WINDOWS.WAIT_OBJECT_0]
        with self.assertRaisesRegex(RuntimeError, 'wait failed'):
            self.launch(api)
        self.assertIn('terminated-job', api.events)
        self.assertIn('closed-201', api.events)

    def test_unverified_cleanup_refuses_finished_callback_but_closes_job(self):
        api = fake_api()
        api.QueryInformationJobObject.side_effect = None
        api.QueryInformationJobObject.return_value = 0
        api.WaitForSingleObject.side_effect = [WINDOWS.WAIT_TIMEOUT, WINDOWS.WAIT_OBJECT_0, WINDOWS.WAIT_OBJECT_0]
        finished = Mock()
        with self.assertRaisesRegex(RuntimeError, 'cleanup could not be verified'):
            self.launch(api, on_finished=finished)
        finished.assert_not_called()
        self.assertIn('closed-201', api.events)
        api.TerminateProcess.assert_called_with(301, 1)

    def test_poll_and_finished_failures_preserve_original_and_safe_details(self):
        api = fake_api()
        original = OSError('sensitive original path/credential')
        def poll(): raise original
        def finished():
            self.assertIn('terminated-job', api.events)
            self.assertIn('closed-201', api.events)
            raise ValueError('sensitive secondary path/credential')
        with self.assertRaises(OSError) as caught:
            self.launch(api, on_poll=poll, on_finished=finished)
        self.assertIs(original, caught.exception)
        details = WINDOWS.failure_details(caught.exception)
        self.assertEqual([{'stage': 'callback-poll', 'type': 'OSError'},
                          {'stage': 'callback-finished', 'type': 'ValueError'}], details)
        self.assertNotIn('sensitive', str(details))
        self.assertEqual({201, 202, 301, 302}, {call.args[0] for call in api.CloseHandle.call_args_list})
        api.DeleteProcThreadAttributeList.assert_called_once()

    def test_started_failure_and_finished_failure_never_resume_user_code(self):
        api = fake_api(); original = OSError('initial observer')
        api.WaitForSingleObject.side_effect = None
        api.WaitForSingleObject.return_value = WINDOWS.WAIT_OBJECT_0
        def started(pid): raise original
        def finished(): raise ValueError('missing ready on aborted startup')
        with self.assertRaises(OSError) as caught:
            self.launch(api, on_started=started, on_finished=finished)
        self.assertIs(caught.exception, original)
        api.ResumeThread.assert_not_called()
        self.assertEqual(['callback-started', 'callback-finished'],
                         [row['stage'] for row in WINDOWS.failure_details(caught.exception)])

    def test_actual_job_cleanup_failure_remains_hard_and_keeps_original(self):
        api = fake_api(); original = OSError('native observation failed')
        def poll(): raise original
        api.QueryInformationJobObject.side_effect = None
        api.QueryInformationJobObject.return_value = 0
        # poll timeout, then original owned fallback reaps the direct process.
        api.WaitForSingleObject.side_effect = [WINDOWS.WAIT_TIMEOUT, WINDOWS.WAIT_OBJECT_0]
        finished = Mock()
        with self.assertRaisesRegex(RuntimeError, 'cleanup could not be verified') as caught:
            self.launch(api, on_poll=poll, on_finished=finished)
        self.assertEqual([{'stage': 'callback-poll', 'type': 'OSError'},
                          {'stage': 'cleanup-job', 'type': 'RuntimeError'}], WINDOWS.failure_details(caught.exception))
        finished.assert_not_called()
        api.TerminateProcess.assert_called_with(301, 1)
        self.assertIn('closed-201', api.events)

    def test_finished_failure_on_success_still_refuses(self):
        api = fake_api()
        def finished(): raise ValueError('invalid observer completion')
        with self.assertRaises(ValueError) as caught:
            self.launch(api, on_finished=finished)
        self.assertEqual([{'stage': 'callback-finished', 'type': 'ValueError'}], WINDOWS.failure_details(caught.exception))

    def test_safe_metadata_filters_unknown_types_stages_extra_fields_and_bound(self):
        error = OSError('private')
        error.unity_bench_failures = [
            {'stage': 'callback-poll', 'type': 'OSError'},
            {'stage': 'password=secret', 'type': 'ValueError'},
            {'stage': 'launch', 'type': 'secret-type'},
            {'stage': 'launch', 'type': 'RuntimeError', 'message': 'secret'}]
        self.assertEqual([{'stage': 'callback-poll', 'type': 'OSError'}], WINDOWS.failure_details(error))
        error.unity_bench_failures = [{'stage': 'launch', 'type': 'RuntimeError'}] * 33
        self.assertEqual([], WINDOWS.failure_details(error))



@unittest.skipUnless(os.name == 'nt', 'Requires native Windows Job Object APIs')
class WindowsProcessNativeTests(unittest.TestCase):
    """Run in the ordinary source CI lane; tests own only generated child trees."""
    def setUp(self):
        self.api = WINDOWS.native_api()
        self.api.OpenProcess.argtypes = [WINDOWS.DWORD, WINDOWS.BOOL, WINDOWS.DWORD]
        self.api.OpenProcess.restype = WINDOWS.HANDLE

    def process_stopped(self, pid):
        handle = self.api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE, read-only
        if not handle:
            return True
        try:
            return self.api.WaitForSingleObject(handle, 2000) == WINDOWS.WAIT_OBJECT_0
        finally:
            self.api.CloseHandle(handle)

    def test_native_unicode_argv_exit_code_and_suspended_start(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'args.json'
            arguments = ['spaces and 한글', 'quote"value', 'trailing\\']
            script = 'import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding="utf-8"); print("discarded"); raise SystemExit(7)'
            def started(pid):
                self.assertFalse(receipt.exists(), 'User code executed before suspended callback')
            code, _, pid = PROCESS.launch([sys.executable, '-c', script, str(receipt), *arguments], directory, 10, on_started=started)
            self.assertEqual(code, 7)
            self.assertEqual(json.loads(receipt.read_text(encoding='utf-8')), arguments)
            self.assertTrue(self.process_stopped(pid))

    def test_native_timeout_kills_child_tree_and_preserves_unrelated_process(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with tempfile.TemporaryDirectory() as directory:
                receipt = Path(directory) / 'child.pid'
                script = 'import pathlib,subprocess,sys,time; child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)'
                with self.assertRaises(TimeoutError):
                    WINDOWS.launch_windows([sys.executable, '-c', script, str(receipt)], directory, 2)
                self.assertTrue(self.process_stopped(int(receipt.read_text())))
                self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate(); unrelated.wait(timeout=5)

    def test_native_normal_wrapper_exit_kills_remaining_owned_child(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'child.pid'
            script = 'import pathlib,subprocess,sys; child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); pathlib.Path(sys.argv[1]).write_text(str(child.pid))'
            code, _, _ = WINDOWS.launch_windows([sys.executable, '-c', script, str(receipt)], directory, 10)
            self.assertEqual(code, 0)
            self.assertTrue(self.process_stopped(int(receipt.read_text())))

    def test_native_observer_failure_prevents_user_code(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'unexpected.txt'
            pids = []
            def refuse(pid):
                pids.append(pid)
                raise ValueError('observer refused')
            with self.assertRaisesRegex(ValueError, 'observer refused'):
                WINDOWS.launch_windows([sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("unexpected")', str(receipt)], directory, 10, on_started=refuse)
            self.assertFalse(receipt.exists())
            self.assertTrue(self.process_stopped(pids[0]))


if __name__ == '__main__':
    unittest.main()
