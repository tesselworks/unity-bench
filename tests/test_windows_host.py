"""Native host protocol checks using WinAPI-shaped fakes; no live Unity needed."""
import ctypes
import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / 'src/unity_bench/windows_host.py'
SPEC = importlib.util.spec_from_file_location('unity_bench_windows_host_test', PATH)
HOST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOST)


class Entry(ctypes.Structure):
    _fields_ = [('dwSize', ctypes.c_uint32), ('th32ProcessID', ctypes.c_uint32),
                ('th32ParentProcessID', ctypes.c_uint32), ('szExeFile', ctypes.c_wchar * 260)]


class FileTime(ctypes.Structure):
    _fields_ = [('dwLowDateTime', ctypes.c_uint32), ('dwHighDateTime', ctypes.c_uint32)]


class WindowsHostTests(unittest.TestCase):
    def probe(self):
        probe = HOST.WindowsProbe.__new__(HOST.WindowsProbe)
        probe.Entry = Entry
        probe.w = types.SimpleNamespace(DWORD=ctypes.c_uint32, FILETIME=FileTime)
        probe.handles, probe.known = {}, {}
        probe.handle_access = {}; probe.probe_failures = []; probe.image_queries = {}
        probe.owned_root_pid = None; probe.owned_sync_handles = {}; probe.owned_sync_identities = {}
        probe.primary_sync_handle = None; probe.primary_sync_pid = None; probe.primary_sync_identity = None
        probe.primary_editor_pid = None
        probe.memory_api = None
        probe.memory_api_error = 'Unavailable'
        probe.cpu_count = 8
        def enumerate_first(snapshot, pointer):
            row = pointer._obj
            row.th32ProcessID, row.th32ParentProcessID, row.szExeFile = 123, 100, 'worker.exe'
            return 1
        def active(handle, pointer):
            pointer._obj.value = 259
            return 1
        def image(handle, flags, buffer, length):
            buffer.value = 'C:\\bench\\worker.exe'
            length._obj.value = len(buffer.value)
            return 1
        def system(idle, kernel, user):
            for pointer, ticks in zip((idle, kernel, user), (10_000_000, 30_000_000, 20_000_000)):
                pointer._obj.dwLowDateTime = ticks
            return 1
        probe.api = types.SimpleNamespace(
            CreateToolhelp32Snapshot=Mock(return_value=77), CloseHandle=Mock(),
            Process32FirstW=Mock(side_effect=enumerate_first), Process32NextW=Mock(return_value=0),
            OpenProcess=Mock(return_value=88), K32GetProcessImageFileNameW=Mock(return_value=0), GetLogicalDrives=Mock(return_value=4), QueryDosDeviceW=Mock(return_value=0), WaitForSingleObject=Mock(return_value=258), GetProcessId=Mock(return_value=999), GetExitCodeProcess=Mock(side_effect=active),
            QueryFullProcessImageNameW=Mock(side_effect=image), GetSystemTimes=Mock(side_effect=system),
            GetProcessTimes=Mock(return_value=0))
        return probe

    def test_import_is_portable_but_nonwindows_creation_refuses(self):
        with patch.object(HOST.os, 'name', 'posix'):
            with self.assertRaisesRegex(RuntimeError, 'Windows only'):
                HOST.WindowsProbe()

    def test_snapshot_preserves_live_identity_and_unknown_cpu_memory(self):
        probe = self.probe()
        result = probe.snapshot()
        self.assertEqual(set(result), {'utc', 'monotonic', 'memory', 'processes', 'known',
                                      'cpuCounters', 'processExits', 'processHandleStates', 'processHandleStateHistory', 'systemBusySeconds', 'processorCount', 'probeMs', 'censusStartUtc', 'censusStartMonotonic'})
        self.assertTrue(result['utc'].endswith('+00:00'))
        self.assertTrue(result['censusStartUtc'].endswith('+00:00'))
        self.assertLessEqual(result['censusStartUtc'], result['utc'])
        self.assertLessEqual(result['censusStartMonotonic'], result['monotonic'])
        self.assertEqual(result['processes'], [{'pid': 123, 'parentPid': 100, 'name': 'worker.exe',
                                               'executable': 'C:\\bench\\worker.exe', 'startedUnix': None}])
        self.assertEqual(result['known'], result['processes'])
        self.assertEqual(result['cpuCounters'], {123: None})
        self.assertEqual(result['systemBusySeconds'], 4)
        self.assertEqual(result['processorCount'], 8)
        self.assertFalse(result['memory']['available'])
        self.assertEqual(result['memory']['error'], 'Unavailable')
        probe.api.CloseHandle.assert_called_once_with(77)
        probe.close()
        self.assertEqual(probe.api.CloseHandle.call_args.args, (88,))

    def test_exited_handle_still_contributes_counter_without_claiming_live_process(self):
        probe = self.probe()
        probe.handles[999] = 9999
        probe.known[999] = {'pid': 999, 'parentPid': 123, 'name': 'old.exe'}
        probe.cpu = lambda handle: 12 if handle == 9999 else 1
        result = probe.snapshot()
        self.assertEqual(result['cpuCounters'], {999: 12, 123: 1})
        self.assertEqual({r['pid'] for r in result['processes']}, {123})
        self.assertEqual({r['pid'] for r in result['known']}, {999, 123})

    def test_retained_native_exit_times_prove_exact_process_identity(self):
        probe = self.probe()
        pid = 999; probe.handles[pid] = 9999
        probe.known[pid] = {'pid': pid, 'parentPid': 123, 'name': 'Unity.exe', 'executable': r'C:\Unity\Editor\Unity.exe'}
        started, exited = 1700000000, 1700000002
        def code(handle, pointer):
            pointer._obj.value = 0 if handle == 9999 else 259
            return 1
        def times(handle, creation, exit_time, kernel, user):
            if handle != 9999: return 0
            for pointer, seconds in ((creation, started+11644473600), (exit_time, exited+11644473600), (kernel, 2), (user, 3)):
                ticks = int(seconds*10000000)
                pointer._obj.dwLowDateTime = ticks & 0xffffffff
                pointer._obj.dwHighDateTime = ticks >> 32
            return 1
        probe.api.GetExitCodeProcess.side_effect = code; probe.api.GetProcessTimes.side_effect = times
        snap = probe.snapshot()
        self.assertEqual(snap['processExits'][pid], {'pid': pid, 'startedUnix': started, 'exitedUnix': exited,
                                                  'exitCode': 0, 'executable': r'C:\Unity\Editor\Unity.exe'})
        self.assertEqual(5, snap['cpuCounters'][pid])
        self.assertNotIn(pid, {row['pid'] for row in snap['processes']})
        probe.api.GetProcessTimes.side_effect = None; probe.api.GetProcessTimes.return_value = 0
        self.assertIsNone(probe.exit_proof(pid, 9999))
        probe.api.GetExitCodeProcess.side_effect = lambda handle, pointer: 0
        self.assertIsNone(probe.exit_proof(pid, 9999))

    def pending_probe(self):
        probe = self.probe(); pid = 999; probe.primary_editor_pid = pid
        probe.handles[pid] = 9999
        probe.known[pid] = {'pid': pid, 'parentPid': 123, 'name': 'Unity.exe',
                            'executable': r'C:\bench\worker.exe', 'startedUnix': 1700000000}
        def times(handle, creation, exit_time, kernel, user):
            if handle not in (9999, 9998, 8888): return 0
            # Nonzero exit bits are deliberately meaningless while active.
            for pointer, seconds in ((creation, 1700000000+11644473600),
                                     (exit_time, 1700000100+11644473600), (kernel, 2), (user, 3)):
                ticks = int(seconds*10000000)
                pointer._obj.dwLowDateTime = ticks & 0xffffffff
                pointer._obj.dwHighDateTime = ticks >> 32
            return 1
        probe.api.GetProcessTimes.side_effect = times
        probe.primary_sync_handle = 9998; probe.primary_sync_pid = pid
        probe.primary_sync_identity = dict(probe.known[pid])
        probe.handles[pid] = 9998; probe.handle_access[pid] = 0x101000
        probe.owned_root_pid = pid; probe.owned_sync_handles[pid] = 9998
        probe.owned_sync_identities[pid] = dict(probe.known[pid])
        return probe

    def test_omitted_census_primary_has_fresh_pending_handle_identity_and_shared_cpu(self):
        probe = self.pending_probe(); result = probe.snapshot(); state = result['processHandleStates'][999]
        self.assertNotIn(999, {r['pid'] for r in result['processes']})
        self.assertTrue(state['querySucceeded']); self.assertEqual(999, state['pid'])
        self.assertEqual(1700000000, state['startedUnix']); self.assertEqual(259, state['exitCode'])
        self.assertFalse(state['exitTimeDefined']); self.assertIsNone(state['exitedUnix'])
        self.assertEqual(5, state['cpuSeconds']); self.assertEqual(state['cpuSeconds'], result['cpuCounters'][999])
        self.assertNotIn(999, result['processExits'])
        selected_reads = [c for c in probe.api.GetProcessTimes.call_args_list if c.args[0] == 9998]
        self.assertEqual(1, len(selected_reads))
        probe.api.GetProcessId.assert_called_once_with(9998)

    def test_optional_handle_state_failure_preserves_independent_cpu_but_missing_cpu_stays_unknown(self):
        for change in ('zero-pid', 'wrong-pid', 'exit-query', 'image-query', 'times-query'):
            probe = self.pending_probe()
            if change == 'zero-pid': probe.api.GetProcessId.return_value = 0
            if change == 'wrong-pid': probe.api.GetProcessId.return_value = 998
            if change == 'exit-query': probe.api.GetExitCodeProcess.side_effect = lambda h,p: 0
            if change == 'image-query': probe.api.QueryFullProcessImageNameW.side_effect = lambda *a: 0
            if change == 'times-query': probe.api.GetProcessTimes.side_effect = lambda *a: 0
            if change == 'wrong-pid':
                with self.assertRaisesRegex(OSError, 'Retained handle PID differs'): probe.snapshot()
                continue
            result = probe.snapshot()
            with self.subTest(change=change):
                self.assertFalse(result['processHandleStates'][999]['querySucceeded'])
                if change == 'times-query': self.assertIsNone(result['cpuCounters'][999])
                else: self.assertEqual(5, result['cpuCounters'][999])

    def test_owned_sync_cannot_reopen_an_existing_query_only_handle(self):
        probe = self.pending_probe(); probe.owned_sync_handles.clear()
        with self.assertRaisesRegex(OSError, 'first native open'): probe.owned_sync(999, {999})
        probe.api.OpenProcess.assert_not_called()

    def test_process_object_polls_define_pending_even_for_assigned_zero_exit_code(self):
        for code in (0, 259):
            probe = self.pending_probe()
            def assigned(handle, pointer): pointer._obj.value = code; return 1
            probe.api.GetExitCodeProcess.side_effect = assigned
            state = probe.handle_state(999, 9998)
            self.assertEqual((258, 258), (state['waitBefore'], state['waitAfter']))
            self.assertFalse(state['exitTimeDefined']); self.assertIsNone(state['exitedUnix'])
            self.assertEqual(code, state['exitCode']); self.assertEqual(5, state['cpuSeconds'])
            self.assertEqual([(9998, 0), (9998, 0)], [c.args for c in probe.api.WaitForSingleObject.call_args_list])
            self.assertIsNone(probe.exit_proof(999, 9999))

    def test_object_transition_requires_fresh_terminal_times_and_failed_or_reversed_wait_is_hard(self):
        probe = self.pending_probe(); probe.api.WaitForSingleObject.side_effect = [258, 0, 0]
        def exited(handle, pointer): pointer._obj.value = 0; return 1
        probe.api.GetExitCodeProcess.side_effect = exited
        state = probe.handle_state(999, 9998)
        self.assertFalse(state['exitTimeDefined']); self.assertIsNone(state['exitedUnix'])
        proof = probe.exit_proof(999, 9999)
        self.assertEqual(0, proof['objectWaitCode']); self.assertEqual(1700000100, proof['exitedUnix'])
        calls = [c for c in probe.api.GetProcessTimes.call_args_list if c.args[0] == 9998]
        self.assertEqual(2, len(calls))
        probe = self.pending_probe(); probe.api.WaitForSingleObject.return_value = 0
        probe.api.GetExitCodeProcess.side_effect = exited
        state = probe.handle_state(999, 9998)
        self.assertTrue(state['exitTimeDefined']); self.assertEqual(1700000100, state['exitedUnix'])
        for outcomes in ([0, 258], [0xffffffff], [128], [258, 0xffffffff]):
            probe = self.pending_probe(); probe.api.WaitForSingleObject.side_effect = outcomes
            with self.subTest(outcomes=outcomes), self.assertRaises(OSError): probe.handle_state(999, 9998)

    def test_owned_terminal_counter_uses_same_postsignal_native_times_as_exit_proof(self):
        probe = self.pending_probe(); probe.primary_editor_pid = None
        probe.api.WaitForSingleObject.return_value = 0
        def code(handle, pointer): pointer._obj.value = 0 if handle == 9998 else 259; return 1
        read_count = [0]
        def times(handle, creation, exit_time, kernel, user):
            if handle != 9998: return 0
            read_count[0] += 1
            cpu = 1.140625 if read_count[0] % 2 else 1.15625
            for pointer, seconds in ((creation, 1700000000+11644473600),
                                     (exit_time, 1700000100+11644473600), (kernel, cpu), (user, 0)):
                ticks = int(seconds*10000000)
                pointer._obj.dwLowDateTime = ticks & 0xffffffff; pointer._obj.dwHighDateTime = ticks >> 32
            return 1
        probe.api.GetExitCodeProcess.side_effect = code; probe.api.GetProcessTimes.side_effect = times
        result = probe.snapshot(); proof = result['processExits'][999]
        self.assertEqual(1.15625, result['cpuCounters'][999]); self.assertEqual(result['cpuCounters'][999], proof['cpuSeconds'])
        self.assertEqual(0, proof['objectWaitCode'])
        self.assertEqual(2, sum(c.args[0] == 9998 for c in probe.api.GetProcessTimes.call_args_list))
        probe.api.WaitForSingleObject.return_value = 258
        result = probe.snapshot()
        self.assertNotIn(999, result['processExits']); self.assertEqual(1.140625, result['cpuCounters'][999])

    def test_selected_earlier_cpu_is_retained_then_metadata_reread_after_terminal_proof(self):
        probe = self.pending_probe(); reads = [0]
        probe.api.WaitForSingleObject.side_effect = [258, 0, 0, 0, 0]
        def code(handle, pointer): pointer._obj.value = 0 if handle == 9998 else 259; return 1
        def times(handle, creation, exit_time, kernel, user):
            if handle != 9998: return 0
            reads[0] += 1; cpu = 5 if reads[0] == 1 else 5.25
            for pointer, seconds in ((creation, 1700000000+11644473600),
                                     (exit_time, 1700000100+11644473600), (kernel, cpu), (user, 0)):
                ticks = int(seconds*10000000)
                pointer._obj.dwLowDateTime = ticks & 0xffffffff; pointer._obj.dwHighDateTime = ticks >> 32
            return 1
        probe.api.GetExitCodeProcess.side_effect = code; probe.api.GetProcessTimes.side_effect = times
        result = probe.snapshot()
        self.assertEqual(5, result['processHandleStateHistory'][999][0]['cpuSeconds'])
        self.assertEqual(5.25, result['processHandleStates'][999]['cpuSeconds'])
        self.assertEqual(5.25, result['cpuCounters'][999]); self.assertEqual(5.25, result['processExits'][999]['cpuSeconds'])
        self.assertEqual((0, 0), (result['processHandleStates'][999]['waitBefore'], result['processHandleStates'][999]['waitAfter']))
        self.assertEqual(3, reads[0])

    def test_default_probe_does_not_open_owned_sync_handles_even_if_primary_memory_selected(self):
        probe = self.pending_probe(); probe.owned_root_pid = None
        result = probe.snapshot()
        self.assertEqual({}, result['processHandleStates']); self.assertEqual({}, result['processHandleStateHistory'])
        self.assertFalse(any(c.args[0] == (0x1000 | 0x100000) for c in probe.api.OpenProcess.call_args_list))
        probe.api.WaitForSingleObject.assert_not_called()

    def test_owned_join_records_current_object_identity_wait_and_failure_reasons(self):
        probe = self.pending_probe(); pending = probe.owned_join_observation()
        row = pending['processes'][0]
        self.assertTrue(row['querySucceeded']); self.assertEqual(258, row['waitCode'])
        self.assertEqual(999, row['actualPid']); self.assertEqual(probe.known[999], row['identity'])
        probe.api.WaitForSingleObject.return_value = 0
        self.assertEqual(0, probe.owned_join_observation()['processes'][0]['waitCode'])
        for change in ('missing-anchor', 'wrong-pid', 'creation-query', 'wait-failed', 'wait-unknown'):
            probe = self.pending_probe()
            if change == 'missing-anchor': probe.owned_sync_handles.clear()
            if change == 'wrong-pid': probe.api.GetProcessId.return_value = 998
            if change == 'creation-query': probe.api.GetProcessTimes.side_effect = lambda *a: 0
            if change == 'wait-failed': probe.api.WaitForSingleObject.return_value = 0xffffffff
            if change == 'wait-unknown': probe.api.WaitForSingleObject.return_value = 128
            row = probe.owned_join_observation()['processes'][0]
            with self.subTest(change=change):
                self.assertFalse(row['querySucceeded']); self.assertTrue(row['error'])
                if change == 'wait-failed': self.assertEqual(0xffffffff, row['waitCode'])

    def controlled_tree_probe(self):
        probe = self.probe(); probe.owned_root_pid = 123; probe.primary_editor_pid = 123
        descriptors = [(999, 0, 'foreign.exe'), (124, 123, 'child.exe'), (123, 0, 'root.exe')]
        def populate(pointer, item):
            row = pointer._obj; row.th32ProcessID, row.th32ParentProcessID, row.szExeFile = item
            return 1
        cursor = [0]
        probe.api.Process32FirstW.side_effect = lambda snap,p: populate(p, descriptors[0])
        def next_row(snap, pointer):
            cursor[0] += 1
            return populate(pointer, descriptors[cursor[0]]) if cursor[0] < len(descriptors) else 0
        probe.api.Process32NextW.side_effect = next_row
        probe.api.OpenProcess.side_effect = lambda access,inherit,pid: pid+1000
        probe.api.GetProcessId.side_effect = lambda handle: handle-1000
        def image(handle, flags, buffer, length):
            buffer.value = r'C:\bench\%s.exe' % (handle-1000); length._obj.value = len(buffer.value); return 1
        def times(handle, creation, exited, kernel, user):
            for pointer, seconds in ((creation, 1700000000+(handle-1000)+11644473600),
                                     (exited, 1700001000+11644473600), (kernel, 2), (user, 3)):
                ticks = int(seconds*10000000); pointer._obj.dwLowDateTime = ticks & 0xffffffff; pointer._obj.dwHighDateTime = ticks >> 32
            return 1
        probe.api.QueryFullProcessImageNameW.side_effect = image; probe.api.GetProcessTimes.side_effect = times
        return probe

    def test_frozen_ancestry_opens_owned_objects_first_once_and_aliases_query_sync_handles(self):
        probe = self.controlled_tree_probe(); result = probe.snapshot()
        self.assertEqual([(0x101000, False, 124), (0x101000, False, 123), (0x1000, False, 999)],
                         [c.args for c in probe.api.OpenProcess.call_args_list])
        self.assertEqual(probe.handles[124], probe.owned_sync_handles[124])
        self.assertEqual(probe.handles[123], probe.owned_sync_handles[123])
        self.assertEqual({123, 124, 999}, {r['pid'] for r in result['processes']})
        probe.close(); probe.close()
        for handle in (1123, 1124, 1999):
            self.assertEqual(1, sum(c.args == (handle,) for c in probe.api.CloseHandle.call_args_list))

    def test_failed_first_anchor_preserves_original_pid_stage_and_partial_identity(self):
        for change, stage in (('open', 'open-process'), ('pid', 'query-owned-pid'), ('image', 'query-image'), ('creation', 'query-creation')):
            probe = self.controlled_tree_probe()
            if change == 'open': probe.api.OpenProcess.side_effect = lambda *a: 0
            if change == 'pid': probe.api.GetProcessId.side_effect = lambda handle: 5
            if change == 'image': probe.api.QueryFullProcessImageNameW.side_effect = lambda *a: 0
            if change == 'creation': probe.api.GetProcessTimes.side_effect = lambda *a: 0
            with self.subTest(change=change), self.assertRaises(OSError): probe.snapshot()
            failure = probe.probe_failures[-1]
            self.assertEqual(124, failure['pid']); self.assertEqual(stage, failure['stage'])
            self.assertEqual(123, failure['descriptor']['parentPid']); self.assertTrue(failure['error'])
            self.assertNotIn(124, probe.owned_sync_handles)

    def test_anchored_object_can_signal_during_query_without_becoming_live_or_needing_fresh_image(self):
        probe = self.controlled_tree_probe(); probe.snapshot()
        probe.api.Process32NextW.side_effect = lambda *a: 0
        probe.api.Process32FirstW.side_effect = lambda snap,p: (setattr(p._obj,'th32ProcessID',124),
            setattr(p._obj,'th32ParentProcessID',123),setattr(p._obj,'szExeFile','child.exe'),1)[-1]
        probe.api.WaitForSingleObject.return_value = 0
        probe.api.GetExitCodeProcess.side_effect = lambda h,p: (setattr(p._obj,'value',0),1)[-1]
        probe.api.QueryFullProcessImageNameW.side_effect = lambda *a: 0
        result = probe.snapshot()
        self.assertNotIn(124, {r['pid'] for r in result['processes']})
        self.assertEqual(r'C:\bench\124.exe', next(r['executable'] for r in result['known'] if r['pid']==124))
        self.assertEqual(0, result['processExits'][124]['objectWaitCode'])
        self.assertEqual(3, probe.api.OpenProcess.call_count)
        probe.api.QueryFullProcessImageNameW.side_effect = lambda h,f,b,l: (setattr(b,'value',r'C:\Other\wrong.exe'),1)[-1]
        with self.assertRaisesRegex(OSError, 'identity differs'): probe.snapshot()

    def fallback_probe(self, k32=False):
        probe = self.probe(); native = r'\Device\HarddiskVolume3\bench\worker.exe'
        def query(handle, flags, buffer, length):
            if flags == 0 or k32: return 0
            buffer.value = native; length._obj.value = len(native); return 1
        def image(handle, buffer, size): buffer.value = native; return len(native)
        def dos(name, buffer, size):
            payload = 'C:\0\0' if name is None else '\\Device\\HarddiskVolume3\0\\Device\\OldVolume\0\0'
            buffer.value = payload; return len(payload)
        probe.api.QueryFullProcessImageNameW.side_effect = query
        probe.api.K32GetProcessImageFileNameW.side_effect = image
        probe.api.QueryDosDeviceW.side_effect = dos
        return probe, native

    def test_native_image_fallback_uses_same_handle_and_current_mapping_with_offline_proof(self):
        for k32 in (False, True):
            probe, native = self.fallback_probe(k32)
            executable, proof = probe.native_image_fallback(9, 999, 5)
            self.assertEqual(r'C:\bench\worker.exe', executable)
            self.assertEqual(executable, HOST.validate_image_proof(proof, 9, executable))
            self.assertEqual(native, proof['nativeImagePath']); self.assertEqual(5, proof['attempts'][0]['errorCode'])
            self.assertEqual(999, probe.api.QueryFullProcessImageNameW.call_args.args[0])
            if k32: self.assertEqual(999, probe.api.K32GetProcessImageFileNameW.call_args.args[0])
            self.assertEqual(['C:'], [m['dosDevice'] for m in proof['mappings']])

    def test_native_mapping_refuses_old_prefix_aliases_bad_length_and_contradictory_attempts(self):
        import copy
        probe, native = self.fallback_probe(); executable, proof = probe.native_image_fallback(9, 999, 5)
        for mappings in ([{'dosDevice':'C:', 'targets':[r'\Device\Other',r'\Device\HarddiskVolume3']}],
                         [{'dosDevice':'C:', 'targets':[r'\Device\HarddiskVolume30']}],
                         [{'dosDevice':'C:', 'targets':[r'\Device\HarddiskVolume3']},
                          {'dosDevice':'D:', 'targets':[r'\Device\HarddiskVolume3']}], [None]):
            with self.subTest(mappings=mappings), self.assertRaises(ValueError): HOST.native_to_dos(native, mappings)
        for mutate in (lambda p:p.update(nativeLength=True), lambda p:p.update(nativeLength=1),
                       lambda p:p.update(attempts=[]), lambda p:p['attempts'][0].update(success=True),
                       lambda p:p['attempts'][1].update(success=False), lambda p:p['attempts'][1].update(flags=True),
                       lambda p:p['attempts'][0].update(errorCode=-1),
                       lambda p:p.pop('driveInventory'),
                       lambda p:p['driveInventory']['queries'][0].update(returnedCount=1),
                       lambda p:p['driveInventory'].update(mask=12)):
            bad = copy.deepcopy(proof); mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): HOST.validate_image_proof(bad, 9, executable)
        probe, _ = self.fallback_probe(True); probe.api.K32GetProcessImageFileNameW.side_effect = None
        probe.api.K32GetProcessImageFileNameW.return_value = 0
        with self.assertRaises(OSError): probe.native_image_fallback(9, 999, 5)
        self.assertEqual(3, len(probe.image_queries[9]['attempts']))

    def test_logical_drive_inventory_is_bounded_and_per_drive_failures_record_count_error(self):
        for mask in (0, 1 << 26):
            probe, _ = self.fallback_probe(); probe.api.GetLogicalDrives.return_value = mask
            with self.assertRaisesRegex(OSError, 'Logical drive inventory'): probe.native_image_fallback(9, 999, 5)
            self.assertEqual(mask, probe.image_queries[9]['driveInventory']['mask'])
        probe, _ = self.fallback_probe(); probe.api.QueryDosDeviceW.side_effect = lambda *a: 0
        with self.assertRaisesRegex(OSError, 'device=C: count=0 error='): probe.native_image_fallback(9, 999, 5)
        self.assertEqual(0, probe.image_queries[9]['driveInventory']['queries'][0]['returnedCount'])
        probe, _ = self.fallback_probe(); probe.native_image_fallback(9, 999, 5)
        self.assertEqual(['C:'], [c.args[0] for c in probe.api.QueryDosDeviceW.call_args_list])

    @unittest.skipUnless(HOST.os.name == 'nt', 'Actual Windows API image proof')
    def test_actual_windows_native_image_proof_for_owned_python_child(self):
        import subprocess, sys
        probe = HOST.WindowsProbe(); child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'])
        try:
            full_query = probe.api.QueryFullProcessImageNameW
            def force_win32_failure(handle, flags, buffer, length):
                return 0 if flags == 0 else full_query(handle, flags, buffer, length)
            probe.api.QueryFullProcessImageNameW = force_win32_failure
            probe.owned_root_pid = child.pid
            snapshot = probe.snapshot()
            identity = next(row for row in snapshot['known'] if row['pid'] == child.pid)
            proof = identity['imageProof']; executable = identity['executable']
            self.assertEqual(HOST.ntpath.normcase(HOST.ntpath.normpath(sys.executable)), HOST.ntpath.normcase(HOST.ntpath.normpath(executable)))
            self.assertEqual(executable, HOST.validate_image_proof(proof, child.pid, executable))
            buffer = ctypes.create_unicode_buffer(32768)
            count = probe.api.K32GetProcessImageFileNameW(probe.handles[child.pid], buffer, len(buffer))
            self.assertGreater(count, 0); self.assertEqual(proof['nativeImagePath'].casefold(), buffer.value.casefold())
        finally:
            probe.close(); child.wait(timeout=10)

    def test_census_start_timestamp_precedes_native_freeze_call(self):
        import datetime
        probe = self.probe(); events = []
        original = probe.api.CreateToolhelp32Snapshot
        def census(*args):
            events.append('census'); return original(*args)
        probe.api.CreateToolhelp32Snapshot = census
        class Clock(datetime.datetime):
            @classmethod
            def now(cls, timezone=None):
                events.append('utc')
                offset = 0 if len(events) == 1 else .03
                return datetime.datetime.fromtimestamp(1700000000+offset, datetime.timezone.utc)
        with patch.object(HOST.datetime, 'datetime', Clock), patch.object(HOST.time, 'monotonic', side_effect=[100, 100.03]):
            result = probe.snapshot()
        self.assertEqual(['utc', 'census', 'utc'], events)
        self.assertEqual(100, result['censusStartMonotonic'])
        self.assertEqual(100.03, result['monotonic'])
        self.assertLess(result['censusStartUtc'], result['utc'])

    def test_pid_reuse_raises_and_closes_candidate_and_snapshot(self):
        probe = self.probe()
        probe.handles[123] = 99
        def exit_code(handle, pointer):
            pointer._obj.value = 259 if handle == 88 else 0
            return 1
        probe.api.GetExitCodeProcess.side_effect = exit_code
        with self.assertRaisesRegex(OSError, 'PID identity reused'):
            probe.snapshot()
        self.assertEqual([call.args[0] for call in probe.api.CloseHandle.call_args_list], [77, 88])

    def test_native_snapshot_and_system_failures_are_hard_errors(self):
        probe = self.probe()
        probe.api.CreateToolhelp32Snapshot.return_value = ctypes.c_void_p(-1).value
        with self.assertRaisesRegex(OSError, 'snapshot unavailable'):
            probe.snapshot()
        probe.api.CloseHandle.assert_not_called()
        probe = self.probe()
        probe.api.Process32FirstW.side_effect = None
        probe.api.Process32FirstW.return_value = 0
        with self.assertRaisesRegex(OSError, 'Empty process snapshot'):
            probe.snapshot()
        probe.api.CloseHandle.assert_called_once_with(77)
        probe = self.probe()
        probe.api.GetSystemTimes.side_effect = None
        probe.api.GetSystemTimes.return_value = 0
        with self.assertRaisesRegex(OSError, 'Host CPU counters unavailable'):
            probe.snapshot()
        probe.api.CloseHandle.assert_called_once_with(77)


if __name__ == '__main__':
    unittest.main()
