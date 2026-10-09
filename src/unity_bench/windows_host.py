"""Read-only Windows host census and CPU/memory evidence.

Importing this module is portable. Creating a probe requires Windows. No command
lines are collected and no processes are terminated or reconfigured. Assessment
of the resulting evidence belongs to the campaign policy, not this module.
"""
import ctypes
import datetime
import os
import ntpath
import time


def native_to_dos(native_path, mappings):
    """Convert only one exact current DOS-device target, never prior targets."""
    if not isinstance(native_path, str) or not native_path.casefold().startswith('\\device\\') or '\0' in native_path or not isinstance(mappings, list):
        raise ValueError('Native image path or device mappings malformed')
    candidates = []; seen = set()
    for mapping in mappings:
        if not isinstance(mapping, dict): raise ValueError('DOS mapping entry malformed')
        drive = mapping.get('dosDevice'); targets = mapping.get('targets')
        if not isinstance(drive, str) or len(drive) != 2 or drive[1] != ':' or not drive[0].isascii() or not drive[0].isalpha():
            raise ValueError('DOS drive device malformed')
        drive = drive.upper()
        if drive in seen or not isinstance(targets, list) or not targets or not all(isinstance(t,str) and t and '\0' not in t for t in targets):
            raise ValueError('DOS current mapping missing or duplicated')
        seen.add(drive); current = targets[0]
        if native_path.casefold().startswith(current.casefold()) and native_path[len(current):].startswith('\\'):
            candidates.append(drive + native_path[len(current):])
    if len(candidates) != 1: raise ValueError('Native image has missing or ambiguous current mapping')
    return candidates[0]


def validate_image_proof(proof, pid, executable):
    if not isinstance(proof, dict) or type(proof.get('schemaVersion')) is not int or proof['schemaVersion'] != 2:
        raise ValueError('Native image proof schema malformed')
    if type(proof.get('pid')) is not int or proof['pid'] != pid or proof.get('api') not in ('QueryFullProcessImageNameW-native', 'K32GetProcessImageFileNameW'):
        raise ValueError('Native image proof identity/API differs')
    native = proof.get('nativeImagePath'); count = proof.get('nativeLength')
    if (not isinstance(native, str) or type(count) is not int or not 0 < count < 32768
            or count != len(native.encode('utf-16-le', 'surrogatepass')) // 2):
        raise ValueError('Native image proof length differs')
    attempts = proof.get('attempts')
    if not isinstance(attempts, list) or len(attempts) not in (2, 3): raise ValueError('Native image API attempts missing')
    for attempt in attempts:
        if (not isinstance(attempt, dict) or type(attempt.get('success')) is not bool
                or type(attempt.get('errorCode')) is not int or not 0 <= attempt['errorCode'] <= 0xffffffff):
            raise ValueError('Native image API outcome malformed')
    if (attempts[0].get('api') != 'QueryFullProcessImageNameW' or type(attempts[0].get('flags')) is not int
            or attempts[0]['flags'] != 0 or attempts[0]['success'] is not False
            or attempts[1].get('api') != 'QueryFullProcessImageNameW' or type(attempts[1].get('flags')) is not int
            or attempts[1]['flags'] != 1):
        raise ValueError('Native image API order differs')
    if proof['api'] == 'QueryFullProcessImageNameW-native':
        valid_selected = len(attempts) == 2 and attempts[1]['success'] is True and attempts[1]['errorCode'] == 0
    else:
        valid_selected = len(attempts) == 3 and attempts[1]['success'] is False and attempts[2].get('api') == 'K32GetProcessImageFileNameW' and attempts[2]['success'] is True and attempts[2]['errorCode'] == 0
    if not valid_selected: raise ValueError('Native image successful API evidence differs')
    inventory = proof.get('driveInventory')
    if inventory is None: raise ValueError('Logical drive inventory proof missing')
    if inventory is not None:
        if (not isinstance(inventory, dict) or inventory.get('api') != 'GetLogicalDrives'
                or type(inventory.get('mask')) is not int or not 0 < inventory['mask'] < (1 << 26)
                or type(inventory.get('errorCode')) is not int or inventory['errorCode'] != 0):
            raise ValueError('Logical drive inventory proof malformed')
        expected = [chr(ord('A')+bit)+':' for bit in range(26) if inventory['mask'] & (1 << bit)]
        queries = inventory.get('queries'); mappings = proof.get('mappings')
        if not isinstance(queries, list) or not isinstance(mappings, list) or len(queries) != len(expected) or len(mappings) != len(expected):
            raise ValueError('Logical drive mapping coverage differs')
        for drive, query, mapping in zip(expected, queries, mappings):
            if (not isinstance(query, dict) or not isinstance(mapping, dict) or query.get('dosDevice') != drive
                    or mapping.get('dosDevice') != drive or type(query.get('returnedCount')) is not int
                    or not 0 < query['returnedCount'] < 32768 or type(query.get('errorCode')) is not int or query['errorCode'] != 0):
                raise ValueError('Logical drive query evidence malformed')
            payload = query.get('returnedValue'); targets = mapping.get('targets')
            if (not isinstance(payload, str) or len(payload.encode('utf-16-le', 'surrogatepass')) // 2 != query['returnedCount']
                    or '\0' not in payload or not payload.split('\0', 1)[0]
                    or not isinstance(targets, list) or not targets or targets[0] != payload.split('\0', 1)[0]):
                raise ValueError('Logical drive returned MULTISZ/count differs')
    if proof.get('executable') != executable or native_to_dos(proof.get('nativeImagePath'), proof.get('mappings')) != executable:
        raise ValueError('Native image conversion differs')
    return executable


class MemoryStatus(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_uint32), ('dwMemoryLoad', ctypes.c_uint32)] + [(name, ctypes.c_uint64) for name in
        ('ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile', 'ullAvailPageFile', 'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual')]


class PerformanceMemory(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_uint32)] + [(name, ctypes.c_size_t) for name in
        ('CommitTotal', 'CommitLimit', 'CommitPeak', 'PhysicalTotal', 'PhysicalAvailable', 'SystemCache', 'KernelTotal', 'KernelPaged', 'KernelNonpaged', 'PageSize')] + [(name, ctypes.c_uint32) for name in ('HandleCount', 'ProcessCount', 'ThreadCount')]


class ProcessMemory(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_uint32), ('PageFaultCount', ctypes.c_uint32)] + [(name, ctypes.c_size_t) for name in
        ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]


class WindowsProbe:
    """Read PIDs, ancestry, executable paths and CPU times, never command lines.

    Keep query handles until the campaign ends so exited owned children still
    contribute final CPU counters; otherwise their CPU could appear as foreign.
    """
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('The actual campaign host probe currently supports Windows only')
        from ctypes import wintypes as w
        self.w = w
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        class Entry(ctypes.Structure):
            _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ProcessID', w.DWORD),
                        ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', w.DWORD),
                        ('cntThreads', w.DWORD), ('th32ParentProcessID', w.DWORD), ('pcPriClassBase', w.LONG),
                        ('dwFlags', w.DWORD), ('szExeFile', w.WCHAR * 260)]
        self.Entry = Entry
        self.api.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = w.HANDLE
        self.api.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        self.api.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        self.api.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
        self.api.GetLogicalDrives.argtypes = []
        self.api.GetLogicalDrives.restype = w.DWORD
        self.api.QueryDosDeviceW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
        self.api.QueryDosDeviceW.restype = w.DWORD
        self.api.K32GetProcessImageFileNameW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD]
        self.api.K32GetProcessImageFileNameW.restype = w.DWORD
        self.api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.api.WaitForSingleObject.restype = w.DWORD
        self.api.GetProcessId.argtypes = [w.HANDLE]
        self.api.GetProcessId.restype = w.DWORD
        self.api.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        self.api.GetSystemTimes.argtypes = [ctypes.POINTER(w.FILETIME)] * 3
        self.handles, self.known = {}, {}
        self.handle_access = {}; self.probe_failures = []; self.image_queries = {}
        self.owned_root_pid = None; self.owned_sync_handles = {}; self.owned_sync_identities = {}
        self.primary_sync_handle = None; self.primary_sync_pid = None; self.primary_sync_identity = None
        self.primary_editor_pid = None; self.memory_api_error = None; self.memory_api = None
        try:
            if ctypes.sizeof(ctypes.c_size_t) == 8 and tuple(ctypes.sizeof(t) for t in (MemoryStatus, PerformanceMemory, ProcessMemory)) != (64, 104, 80):
                raise ValueError('Unexpected Win64 memory API structure layout')
            self.memory_api = ctypes.WinDLL('psapi', use_last_error=True)
            self.api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
            self.api.GlobalMemoryStatusEx.restype = w.BOOL
            self.memory_api.GetPerformanceInfo.argtypes = [ctypes.POINTER(PerformanceMemory), w.DWORD]
            self.memory_api.GetPerformanceInfo.restype = w.BOOL
            self.memory_api.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(ProcessMemory), w.DWORD]
            self.memory_api.GetProcessMemoryInfo.restype = w.BOOL
        except Exception as error:
            self.memory_api_error = type(error).__name__; self.memory_api = None
        self.cpu_count = os.cpu_count() or 1

    @staticmethod
    def ticks(value):
        return ((value.dwHighDateTime << 32) | value.dwLowDateTime) / 10_000_000

    def cpu(self, handle):
        values = [self.w.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
            return None
        return self.ticks(values[2]) + self.ticks(values[3])

    def started(self, handle):
        values = [self.w.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(handle, *[ctypes.byref(v) for v in values]):
            return None
        return self.ticks(values[0]) - 11644473600

    @staticmethod
    def last_error():
        return int(getattr(ctypes, 'get_last_error', lambda: 0)())

    def dos_mappings(self, receipt=None):
        receipt = receipt if receipt is not None else {}
        mask = int(self.api.GetLogicalDrives()); error = 0 if mask else self.last_error()
        receipt.update(api='GetLogicalDrives', mask=mask, errorCode=error, queries=[])
        if not 0 < mask < (1 << 26):
            raise OSError('Logical drive inventory failed or invalid: mask=%s error=%s' % (mask, error))
        capacity = 32768; mappings = []
        for bit in range(26):
            if not mask & (1 << bit): continue
            drive = chr(ord('A')+bit) + ':'
            buffer = ctypes.create_unicode_buffer(capacity)
            count = int(self.api.QueryDosDeviceW(drive, buffer, capacity))
            error = 0 if count else self.last_error()
            receipt['queries'].append({'dosDevice': drive, 'returnedCount': count, 'errorCode': error})
            if not 0 < count < capacity:
                raise OSError('DOS device query failed or truncated: device=%s count=%s error=%s' % (drive, count, error))
            payload = buffer[:count]; receipt['queries'][-1]['returnedValue'] = payload
            parts = payload.split('\0')
            if len(parts) < 2 or not parts[0]: raise OSError('DOS current device target is not terminated within reported count')
            # The first NUL-terminated string is current. Only fully reported
            # remaining strings are retained; they never participate in mapping.
            targets = [parts[0]] + [target for target in parts[1:-1] if target]
            mappings.append({'dosDevice': drive, 'targets': targets})
        return mappings

    def native_image_fallback(self, pid, handle, first_error):
        capacity = 32768
        attempts = [{'api': 'QueryFullProcessImageNameW', 'flags': 0, 'errorCode': first_error, 'success': False}]
        proof = {'schemaVersion': 2, 'pid': pid, 'api': None, 'attempts': attempts,
                 'nativeImagePath': None, 'mappings': [], 'executable': None}
        self.image_queries[pid] = proof
        buffer = ctypes.create_unicode_buffer(capacity); length = self.w.DWORD(capacity)
        success = bool(self.api.QueryFullProcessImageNameW(handle, 1, buffer, ctypes.byref(length)))
        attempts.append({'api': 'QueryFullProcessImageNameW', 'flags': 1, 'errorCode': 0 if success else self.last_error(), 'success': success})
        if success:
            native = buffer.value; count = int(length.value); api = 'QueryFullProcessImageNameW-native'
        else:
            buffer = ctypes.create_unicode_buffer(capacity)
            count = int(self.api.K32GetProcessImageFileNameW(handle, buffer, capacity))
            attempts.append({'api': 'K32GetProcessImageFileNameW', 'errorCode': 0 if count else self.last_error(), 'success': bool(count)})
            native = buffer.value; api = 'K32GetProcessImageFileNameW'
        proof.update(api=api, nativeImagePath=native, nativeLength=count)
        if not native or not 0 < count < capacity or count != len(native.encode('utf-16-le', 'surrogatepass')) // 2:
            raise OSError('Native image query failed, malformed or truncated')
        proof['driveInventory'] = {}
        mappings = self.dos_mappings(proof['driveInventory']); proof['mappings'] = mappings
        executable = native_to_dos(native, mappings); proof['executable'] = executable
        validate_image_proof(proof, pid, executable)
        return executable, proof

    def wait_state(self, handle):
        value = int(self.api.WaitForSingleObject(handle, 0))
        if value not in (0, 258):
            raise OSError('Selected process object wait failed or is unknown')
        return value

    def owned_pids(self):
        if self.owned_root_pid is None: return set()
        owned = {self.owned_root_pid}
        while True:
            expanded = owned | {pid for pid, row in self.known.items() if row.get('parentPid') in owned}
            if expanded == owned: return owned & self.known.keys()
            owned = expanded

    def owned_sync(self, pid, live_pids):
        if pid not in self.owned_sync_handles or self.handles.get(pid) != self.owned_sync_handles[pid]:
            raise OSError('Owned synchronize handle was not anchored on its first native open')
        return self.owned_sync_handles[pid]

    def selected_sync(self, pid, live_pids):
        if self.primary_sync_handle is not None:
            if self.primary_sync_pid != pid: raise OSError('Selected synchronize handle PID changed')
            return self.primary_sync_handle
        handle = self.owned_sync(pid, live_pids)
        self.primary_sync_handle = handle; self.primary_sync_pid = pid
        self.primary_sync_identity = self.owned_sync_identities[pid]
        return handle

    def owned_join_observation(self):
        begin_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(); begin_mono = time.monotonic()
        records = []
        required = self.owned_pids() | self.owned_sync_handles.keys()
        for pid in sorted(required):
            identity = self.owned_sync_identities.get(pid)
            record = {'pid': pid, 'identity': identity, 'actualPid': None, 'startedUnix': None,
                      'waitCode': None, 'querySucceeded': False, 'error': None}
            try:
                handle = self.owned_sync_handles.get(pid)
                if handle is None or identity is None: raise ValueError('Owned join lacks anchored handle')
                record['actualPid'] = int(self.api.GetProcessId(handle))
                record['startedUnix'] = self.started(handle)
                if record['actualPid'] != pid or record['startedUnix'] != identity['startedUnix']:
                    raise ValueError('Owned join native identity differs')
                record['waitCode'] = int(self.api.WaitForSingleObject(handle, 0))
                if record['waitCode'] not in (0, 258): raise ValueError('Owned join wait failed or unknown')
                record['querySucceeded'] = True
            except (ValueError, OSError) as error:
                record['error'] = str(error)
            record['utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat(); record['monotonic'] = time.monotonic()
            records.append(record)
        return {'beginUtc': begin_utc, 'beginMonotonic': begin_mono,
                'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'monotonic': time.monotonic(),
                'processes': records}

    def exit_proof(self, pid, handle):
        """Retained native handle proves exit identity/time, not census absence alone."""
        object_wait = None
        controlled_owned = self.owned_root_pid is not None and pid in self.owned_pids()
        if controlled_owned:
            handle = self.owned_sync_handles.get(pid)
            if handle is None: return None
            object_wait = self.wait_state(handle)
            if object_wait != 0: return None
            if int(self.api.GetProcessId(handle)) != pid:
                raise OSError('Retained handle PID differs from owned process')
        code = self.w.DWORD()
        if not self.api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259:
            return None
        values = [self.w.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(handle, *[ctypes.byref(value) for value in values]):
            return None
        started = self.ticks(values[0]) - 11644473600
        exited = self.ticks(values[1]) - 11644473600
        if exited <= 0 or started <= 0 or exited < started:
            return None
        known = self.known.get(pid, {})
        proof = {'pid': pid, 'startedUnix': started, 'exitedUnix': exited,
                 'exitCode': int(code.value), 'executable': known.get('executable')}
        if object_wait is not None:
            proof['objectWaitCode'] = object_wait
            proof['cpuSeconds'] = self.ticks(values[2]) + self.ticks(values[3])
        return proof

    def handle_state(self, pid, handle):
        """Bracket fresh identity/one CPU read with nonblocking process-object polls."""
        result = {'requestedPid': pid, 'pid': None, 'querySucceeded': False,
                  'error': None, 'executable': None, 'startedUnix': None,
                  'exitCode': None, 'exitTimeDefined': False, 'exitedUnix': None,
                  'cpuSeconds': None, 'waitBefore': self.wait_state(handle), 'waitAfter': None}
        values = None
        try:
            actual_pid = int(self.api.GetProcessId(handle)); result['pid'] = actual_pid
            if actual_pid and actual_pid != pid:
                raise OSError('Retained handle PID differs from selected primary')
            if actual_pid != pid:
                result['error'] = 'Handle PID query failed or differs'; return result
            code = self.w.DWORD()
            if not self.api.GetExitCodeProcess(handle, ctypes.byref(code)):
                result['error'] = 'GetExitCodeProcess failed'; return result
            result['exitCode'] = int(code.value)
            buffer = ctypes.create_unicode_buffer(32768); length = self.w.DWORD(len(buffer))
            if not self.api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)) or not buffer.value:
                result['error'] = 'QueryFullProcessImageNameW failed'; return result
            values = [self.w.FILETIME() for _ in range(4)]
            if not self.api.GetProcessTimes(handle, *[ctypes.byref(value) for value in values]):
                result['error'] = 'GetProcessTimes failed'; return result
            result.update(querySucceeded=True, executable=buffer.value,
                          startedUnix=self.ticks(values[0]) - 11644473600,
                          cpuSeconds=self.ticks(values[2]) + self.ticks(values[3]))
            # Exit bits are defined only if the object was already signaled
            # BEFORE the times read. Assigned code 0 alone is insufficient.
            if result['waitBefore'] == 0:
                result['exitTimeDefined'] = True
                result['exitedUnix'] = self.ticks(values[1]) - 11644473600
            return result
        finally:
            result['waitAfter'] = self.wait_state(handle)
            if result['waitBefore'] == 0 and result['waitAfter'] != 0:
                raise OSError('Selected process object signal reversed')

    def memory(self, live_pids):
        """Three native reads: host physical/commit and one validated primary process only."""
        result = {'available': False, 'error': None, 'primaryProcess': {'available': False,
                  'pid': self.primary_editor_pid, 'error': 'Validated primary process PID is not selected or live'},
                  'selectionBasis': 'Orchestrator-validated primary PID, executable and ownership; may be Editor or player',
                  'scope': 'Read-only diagnostic telemetry; no memory eligibility threshold or causal attribution'}
        if self.memory_api is None:
            result['error'] = self.memory_api_error or 'Memory API unavailable'; return result
        try:
            status = MemoryStatus(); status.dwLength = ctypes.sizeof(status)
            if self.api.GlobalMemoryStatusEx(ctypes.byref(status)):
                result.update(available=True, physicalLoadPercent=int(status.dwMemoryLoad),
                              physicalTotalBytes=int(status.ullTotalPhys), physicalAvailableBytes=int(status.ullAvailPhys),
                              processLimitedCommitLimitBytes=int(status.ullTotalPageFile),
                              processLimitedCommitAvailableBytes=int(status.ullAvailPageFile))
            else:
                result['error'] = 'GlobalMemoryStatusEx failed'
            commit = PerformanceMemory(); commit.cb = ctypes.sizeof(commit)
            if self.memory_api.GetPerformanceInfo(ctypes.byref(commit), commit.cb):
                result['systemCommit'] = {'available': True, 'error': None,
                    'totalBytes': int(commit.CommitTotal * commit.PageSize),
                    'limitBytes': int(commit.CommitLimit * commit.PageSize),
                    'peakBytes': int(commit.CommitPeak * commit.PageSize)}
            else:
                result['systemCommit'] = {'available': False, 'error': 'GetPerformanceInfo failed'}
            pid = self.primary_editor_pid; handle = self.handles.get(pid) if pid in live_pids else None
            if handle:
                process = ProcessMemory(); process.cb = ctypes.sizeof(process)
                if self.memory_api.GetProcessMemoryInfo(handle, ctypes.byref(process), process.cb):
                    result['primaryProcess'] = {'available': True, 'error': None, 'pid': pid,
                        'workingSetBytes': int(process.WorkingSetSize), 'peakWorkingSetBytes': int(process.PeakWorkingSetSize),
                        'privateBytes': int(process.PrivateUsage), 'pageFaultCount': int(process.PageFaultCount)}
                else:
                    result['primaryProcess']['error'] = 'GetProcessMemoryInfo failed'
        except Exception as error:
            result['error'] = type(error).__name__
        return result

    def snapshot(self):
        begin = time.perf_counter()
        census_start_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        census_start_monotonic = time.monotonic()
        snapshot = self.api.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError('Process snapshot unavailable')
        frozen = []
        try:
            entry = self.Entry(); entry.dwSize = ctypes.sizeof(entry)
            more = self.api.Process32FirstW(snapshot, ctypes.byref(entry))
            if not more: raise OSError('Empty process snapshot')
            while more:
                frozen.append({'pid': int(entry.th32ProcessID), 'parentPid': int(entry.th32ParentProcessID),
                               'name': str(entry.szExeFile)})
                more = self.api.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        ancestry = dict(self.known)
        ancestry.update({row['pid']: row for row in frozen})
        owned = set(self.owned_sync_handles)
        if self.owned_root_pid is not None:
            owned.add(self.owned_root_pid)
            while True:
                expanded = owned | {pid for pid, row in ancestry.items() if row.get('parentPid') in owned}
                if expanded == owned: break
                owned = expanded
        rows = []
        # Metadata-free frozen ancestry is complete before any process query.
        # Owned objects are opened first, once, with query+synchronize rights.
        for descriptor in sorted(frozen, key=lambda row: row['pid'] not in owned):
            pid = descriptor['pid']; row = dict(descriptor); stage = 'open-process'
            try:
                controlled = self.owned_root_pid is not None and pid in owned
                wanted_access = 0x101000 if controlled else 0x1000
                if pid not in self.handles:
                    handle = self.api.OpenProcess(wanted_access, False, pid)
                    if handle: self.handles[pid] = handle; self.handle_access[pid] = wanted_access
                handle = self.handles.get(pid)
                existing_anchor = self.owned_sync_identities.get(pid)
                if controlled and (not handle or self.handle_access.get(pid) != wanted_access
                                   or (pid in self.owned_sync_handles and self.owned_sync_handles[pid] != handle)):
                    raise OSError('Owned initial query/synchronize handle unavailable or mismatched')
                if handle:
                    if controlled:
                        stage = 'query-owned-pid'
                        if int(self.api.GetProcessId(handle)) != pid: raise OSError('Owned initial handle PID differs')
                    stage = 'query-exit-code'; active = self.w.DWORD()
                    queried_active = self.api.GetExitCodeProcess(handle, ctypes.byref(active))
                    if controlled and not queried_active: raise OSError('Owned initial exit-code query failed')
                    if not controlled and (not queried_active or active.value != 259):
                        candidate = self.api.OpenProcess(0x1000, False, pid); live = self.w.DWORD()
                        if candidate:
                            try:
                                if self.api.GetExitCodeProcess(candidate, ctypes.byref(live)) and live.value == 259:
                                    raise OSError('PID identity reused; eligibility unknown')
                            finally: self.api.CloseHandle(candidate)
                        continue
                    stage = 'query-image'; buffer = ctypes.create_unicode_buffer(32768); length = self.w.DWORD(len(buffer))
                    queried_image = self.api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length))
                    first_image_error = 0 if queried_image else self.last_error()
                    row['executable'] = buffer.value if queried_image and buffer.value else None
                    if controlled and not row['executable']:
                        if existing_anchor: row['executable'] = existing_anchor['executable']
                        else: row['executable'], row['imageProof'] = self.native_image_fallback(pid, handle, first_image_error)
                    if existing_anchor and 'imageProof' in existing_anchor:
                        row['imageProof'] = existing_anchor['imageProof']
                else: row['executable'] = None
                stage = 'query-creation'; row['startedUnix'] = self.started(handle) if handle else None
                if controlled:
                    if row['startedUnix'] is None: raise OSError('Owned initial creation query failed')
                    previous = self.owned_sync_identities.get(pid) or self.known.get(pid)
                    if previous and any(previous.get(key) != row.get(key) for key in ('pid', 'parentPid', 'name', 'executable', 'startedUnix')):
                        raise OSError('Owned initial native identity differs from retained anchor')
                    self.owned_sync_handles[pid] = handle; self.owned_sync_identities[pid] = dict(row)
                self.known[pid] = row
                if not controlled or self.wait_state(handle) != 0: rows.append(row)
            except (OSError, ValueError) as error:
                self.probe_failures.append({'schemaVersion': 1, 'censusStartUtc': census_start_utc,
                    'censusStartMonotonic': census_start_monotonic, 'failedUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'failedMonotonic': time.monotonic(), 'pid': pid, 'stage': stage, 'errorType': type(error).__name__,
                    'error': str(error), 'descriptor': descriptor, 'known': self.known.get(pid), 'partialIdentity': row,
                    'ownedRootPid': self.owned_root_pid, 'imageQuery': self.image_queries.get(pid)})
                raise
        values = [self.w.FILETIME() for _ in range(3)]
        if not self.api.GetSystemTimes(*[ctypes.byref(v) for v in values]):
            raise OSError('Host CPU counters unavailable')
        live_pids = {row['pid'] for row in rows}
        for owned_pid in self.owned_pids(): self.owned_sync(owned_pid, live_pids)
        states = {}; state_history = {}
        if self.owned_root_pid is not None and self.primary_editor_pid in self.handles:
            selected = self.primary_editor_pid
            selected_handle = self.selected_sync(selected, {row['pid'] for row in rows})
            states[selected] = self.handle_state(selected, selected_handle)
        counters = {pid: states[pid]['cpuSeconds'] if pid in states and states[pid]['cpuSeconds'] is not None else self.cpu(handle)
                    for pid, handle in self.handles.items()}
        exits = {pid: proof for pid, handle in self.handles.items()
                 if (proof := self.exit_proof(pid, handle)) is not None}
        for pid, proof in exits.items():
            if pid not in self.owned_pids(): continue
            counters[pid] = proof['cpuSeconds']
            if pid in states and states[pid]['querySucceeded'] and states[pid]['cpuSeconds'] != counters[pid]:
                # Preserve the earlier read, then read selected metadata once
                # AFTER proven termination within this same census. Never edit
                # the earlier CPU or choose a different census endpoint.
                state_history[pid] = [states[pid]]
                states[pid] = self.handle_state(pid, self.owned_sync_handles[pid])
        memory = self.memory(live_pids)
        return {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'monotonic': time.monotonic(),
                'censusStartUtc': census_start_utc, 'censusStartMonotonic': census_start_monotonic,
                'memory': memory,
                'processes': rows, 'known': list(self.known.values()), 'cpuCounters': counters, 'processExits': exits, 'processHandleStates': states, 'processHandleStateHistory': state_history,
                'systemBusySeconds': self.ticks(values[1]) + self.ticks(values[2]) - self.ticks(values[0]),
                'processorCount': self.cpu_count, 'probeMs': (time.perf_counter() - begin) * 1000}

    def close(self):
        handles = set(self.handles.values()) | set(self.owned_sync_handles.values())
        if self.primary_sync_handle is not None: handles.add(self.primary_sync_handle)
        for handle in handles: self.api.CloseHandle(handle)
        self.handles.clear(); self.handle_access.clear(); self.owned_sync_handles.clear()
        self.owned_sync_identities.clear(); self.primary_sync_handle = None
