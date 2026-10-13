"""Windows 10+ adapter ownership with atomic Job Object association.

CreateProcessW receives an owned job via PROC_THREAD_ATTRIBUTE_JOB_LIST while
CREATE_SUSPENDED keeps user code stopped. Only owned NUL handles are inherited.
No fallback starts an uncontained process; unsupported job/attribute APIs refuse.

Shared Unity daemons survive cleanup. An Editor starts `Unity.Licensing.Client` for the whole machine; terminating it
with the job makes the next Editor start it again and takes it away from the user's own session. Letting it break
away from the job is not an option here, because the Editor creates it without CREATE_BREAKAWAY_FROM_JOB (and
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK would let every child escape). So cleanup lists the job's processes, terminates
all but the shared daemons one by one, and clears KILL_ON_JOB_CLOSE before the job handle closes so the daemons are
not killed with it. A job without a shared daemon, or one that cannot be listed, is still terminated atomically.
Both lists are reported to the caller.

Primary API contracts:
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""
import ctypes
import math
import os
from pathlib import Path
import shutil
import subprocess
import time

DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t

CREATE_SUSPENDED = 0x4
EXTENDED_STARTUPINFO_PRESENT = 0x80000
STARTF_USESTDHANDLES = 0x100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_TERMINATE = 0x1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_MORE_DATA = 234
SHARED_DAEMONS = ('unity.licensing',)   # lower-case image-name prefixes of processes an Editor starts for the whole machine
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x2000D
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class SecurityAttributes(ctypes.Structure):
    _fields_ = [('nLength', DWORD), ('lpSecurityDescriptor', HANDLE), ('bInheritHandle', BOOL)]


class StartupInfo(ctypes.Structure):
    _fields_ = [('cb', DWORD), ('lpReserved', ctypes.c_wchar_p), ('lpDesktop', ctypes.c_wchar_p),
                ('lpTitle', ctypes.c_wchar_p), ('dwX', DWORD), ('dwY', DWORD),
                ('dwXSize', DWORD), ('dwYSize', DWORD), ('dwXCountChars', DWORD),
                ('dwYCountChars', DWORD), ('dwFillAttribute', DWORD), ('dwFlags', DWORD),
                ('wShowWindow', ctypes.c_uint16), ('cbReserved2', ctypes.c_uint16),
                ('lpReserved2', HANDLE), ('hStdInput', HANDLE), ('hStdOutput', HANDLE), ('hStdError', HANDLE)]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [('StartupInfo', StartupInfo), ('lpAttributeList', HANDLE)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [('hProcess', HANDLE), ('hThread', HANDLE), ('dwProcessId', DWORD), ('dwThreadId', DWORD)]


class BasicLimitInformation(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', DWORD), ('MinimumWorkingSetSize', SIZE_T), ('MaximumWorkingSetSize', SIZE_T),
                ('ActiveProcessLimit', DWORD), ('Affinity', SIZE_T), ('PriorityClass', DWORD), ('SchedulingClass', DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount',
               'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimitInformation), ('IoInfo', IoCounters),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]


class BasicAccountingInformation(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in ('TotalUserTime', 'TotalKernelTime',
                'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                (name, DWORD) for name in ('TotalPageFaultCount', 'TotalProcesses',
                'ActiveProcesses', 'TotalTerminatedProcesses')]


class ProcessIdList(ctypes.Structure):
    _fields_ = [('NumberOfAssignedProcesses', DWORD), ('NumberOfProcessIdsInList', DWORD), ('ProcessIdList', SIZE_T * 512)]


def is_shared_daemon(name):
    return name.replace('\\', '/').rsplit('/', 1)[-1].lower().startswith(SHARED_DAEMONS)


def native_api():
    if os.name != 'nt':
        raise RuntimeError('Windows adapter ownership requires Windows')
    try:
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        declarations = {
            'CreateJobObjectW': ([HANDLE, ctypes.c_wchar_p], HANDLE),
            'SetInformationJobObject': ([HANDLE, ctypes.c_int32, HANDLE, DWORD], BOOL),
            'InitializeProcThreadAttributeList': ([HANDLE, DWORD, DWORD, ctypes.POINTER(SIZE_T)], BOOL),
            'UpdateProcThreadAttribute': ([HANDLE, DWORD, SIZE_T, HANDLE, SIZE_T, HANDLE, HANDLE], BOOL),
            'DeleteProcThreadAttributeList': ([HANDLE], None),
            'CreateFileW': ([ctypes.c_wchar_p, DWORD, DWORD, ctypes.POINTER(SecurityAttributes), DWORD, DWORD, HANDLE], HANDLE),
            'CreateProcessW': ([ctypes.c_wchar_p, ctypes.c_wchar_p, HANDLE, HANDLE, BOOL, DWORD,
                               HANDLE, ctypes.c_wchar_p, ctypes.POINTER(StartupInfoEx), ctypes.POINTER(ProcessInformation)], BOOL),
            'IsProcessInJob': ([HANDLE, HANDLE, ctypes.POINTER(BOOL)], BOOL),
            'ResumeThread': ([HANDLE], DWORD),
            'WaitForSingleObject': ([HANDLE, DWORD], DWORD),
            'GetExitCodeProcess': ([HANDLE, ctypes.POINTER(DWORD)], BOOL),
            'TerminateJobObject': ([HANDLE, ctypes.c_uint32], BOOL),
            'TerminateProcess': ([HANDLE, ctypes.c_uint32], BOOL),
            'QueryInformationJobObject': ([HANDLE, ctypes.c_int32, HANDLE, DWORD, HANDLE], BOOL),
            'OpenProcess': ([DWORD, BOOL, DWORD], HANDLE),
            'QueryFullProcessImageNameW': ([HANDLE, DWORD, ctypes.c_wchar_p, ctypes.POINTER(DWORD)], BOOL),
            'CloseHandle': ([HANDLE], BOOL),
        }
        for name, (args, result) in declarations.items():
            function = getattr(api, name)
            function.argtypes, function.restype = args, result
        if ctypes.sizeof(SIZE_T) == 8 and tuple(ctypes.sizeof(t) for t in
                (SecurityAttributes, StartupInfo, StartupInfoEx, ProcessInformation,
                 BasicLimitInformation, ExtendedLimitInformation, BasicAccountingInformation)) != (24, 104, 112, 24, 64, 144, 48):
            raise ValueError('Native structure layout unavailable')
        return api
    except (AttributeError, OSError, ValueError):
        raise RuntimeError('Windows Job Object ownership APIs are unavailable') from None


def win32_error():
    """The last Win32 error code of the calling thread as ' (Win32 error 5: Access is denied)', or '' when there is none."""
    code = getattr(ctypes, 'get_last_error', lambda: 0)()
    if not code:
        return ''
    text = getattr(ctypes, 'FormatError', lambda c: '')(code).strip()
    return f' (Win32 error {code}' + (f': {text}' if text else '') + ')'


def _check(success, message):
    if not success:
        raise RuntimeError(message + win32_error())


def _job_members(api, job):
    """[(pid, image name)] of the processes in the job, or None when it cannot be listed."""
    ids = ProcessIdList()
    listed = api.QueryInformationJobObject(job, 3, ctypes.byref(ids), ctypes.sizeof(ids), None)   # JobObjectBasicProcessIdList
    if not listed and getattr(ctypes, 'get_last_error', lambda: 0)() != ERROR_MORE_DATA:
        return None
    members = []
    for pid in ids.ProcessIdList[:ids.NumberOfProcessIdsInList]:
        handle = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        name = ''
        if handle:
            buffer, size = ctypes.create_unicode_buffer(1024), DWORD(1024)
            name = buffer.value if api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else ''
            api.CloseHandle(handle)
        members.append((int(pid), name))
    return members


def _terminate_all_but_shared(api, job, members, report):
    """Terminate the job's processes one by one, leaving shared daemons alone, until only those remain."""
    deadline = time.monotonic() + 2
    done = set()
    while True:
        victims = [(pid, name) for pid, name in members if not is_shared_daemon(name)]
        if not victims:
            return
        for pid, name in victims:
            handle = api.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                api.TerminateProcess(handle, 1)
                api.CloseHandle(handle)
            if pid not in done:
                done.add(pid)
                report.setdefault('terminated', []).append(os.path.basename(name.replace('\\', '/')) or f'pid {pid}')
        if time.monotonic() >= deadline:
            raise RuntimeError('Owned Windows job cleanup did not complete')
        time.sleep(0.02)
        members = _job_members(api, job)
        if members is None:
            raise RuntimeError('Owned Windows job cleanup could not be verified' + win32_error())


def _cleanup_job(api, job, process, report):
    members = _job_members(api, job)
    names = {pid: os.path.basename(name.replace('\\', '/')) or f'pid {pid}' for pid, name in members or []}
    kept = [pid for pid, name in members or [] if is_shared_daemon(name)]
    report['kept'] = [names[pid] for pid in kept]
    if kept:
        _terminate_all_but_shared(api, job, members, report)
        # Closing the job handle must not take the shared daemons with it.
        _check(api.SetInformationJobObject(job, 9, ctypes.byref(ExtendedLimitInformation()), ctypes.sizeof(ExtendedLimitInformation)),
               'Owned Windows job could not release the shared Unity daemons')
    else:
        _check(api.TerminateJobObject(job, 1), 'Owned Windows job could not be terminated')
        report['terminated'] = list(names.values())
        if members is None:
            report['census'] = 'unavailable'
    deadline = time.monotonic() + 2
    while True:
        accounting = BasicAccountingInformation()
        _check(api.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None),
               'Owned Windows job cleanup could not be verified')
        if accounting.ActiveProcesses <= len(kept):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Owned Windows job cleanup did not complete')
        time.sleep(0.02)
    if api.WaitForSingleObject(process, 2000) != WAIT_OBJECT_0:
        raise RuntimeError('Owned Windows primary process cleanup did not complete')


def launch_windows(argv, cwd, timeout, on_started=None, on_poll=None, on_finished=None, report=None, *, _api=None):
    """Start only a suspended, job-contained process; discard stdout/stderr.

    Returns exit code, wall milliseconds until the process exited (cleanup is not included) and PID. `report`, if
    given, is filled with the image names `terminated` and `kept` (shared daemons) by cleanup.
    """
    report = {} if report is None else report
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Adapter timeout must be finite and positive')
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError('Adapter argv must be a nonempty sequence')
    argv = [os.fspath(token) for token in argv]
    if any(not isinstance(token, str) or '\0' in token for token in argv):
        raise ValueError('Adapter argv must contain strings without null characters')
    api = native_api() if _api is None else _api
    # Resolve the executable explicitly; CreateProcess must never interpret an
    # ambiguous unquoted executable prefix or implicitly invoke a command shell.
    candidate = Path(argv[0])
    if candidate.is_absolute() or os.path.dirname(argv[0]):
        if not candidate.is_absolute():
            candidate = Path(cwd) / candidate
        executable = str(candidate.resolve()) if candidate.is_file() else None
    else:
        executable = shutil.which(argv[0])
    if executable is None or Path(executable).suffix.lower() in ('.bat', '.cmd'):
        raise RuntimeError('Windows adapter executable is unavailable or requires a shell')
    start = time.perf_counter()
    job = null_handle = None
    attributes = None
    initialized = False
    started = False
    info = ProcessInformation()
    try:
        job = api.CreateJobObjectW(None, None)
        _check(job, 'Owned Windows job could not be created')
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _check(api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)),
               'Owned Windows job containment could not be configured')
        size = SIZE_T()
        api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        _check(0 < size.value <= 1024 * 1024, 'Windows startup attributes are unavailable')
        attributes = ctypes.create_string_buffer(size.value)
        _check(api.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)),
               'Windows startup attributes could not be initialized')
        initialized = True
        security = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, True)
        null_handle = api.CreateFileW('NUL', 0xC0000000, 3, ctypes.byref(security), 3, 0, None)
        _check(null_handle and null_handle != INVALID_HANDLE_VALUE, 'Owned Windows output handles are unavailable')
        inherited_handles = (HANDLE * 1)(null_handle)
        owned_jobs = (HANDLE * 1)(job)
        _check(api.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                            inherited_handles, ctypes.sizeof(inherited_handles), None, None),
               'Windows handle inheritance could not be restricted')
        _check(api.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST,
                                            owned_jobs, ctypes.sizeof(owned_jobs), None, None),
               'Atomic Windows job association is unavailable')
        startup = StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = null_handle
        startup.lpAttributeList = ctypes.cast(attributes, HANDLE)
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        _check(api.CreateProcessW(executable, command_line, None, None, True,
                                  CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT, None,
                                  str(Path(cwd).resolve()), ctypes.byref(startup), ctypes.byref(info)),
               'Owned Windows adapter could not be created')
        associated = BOOL()
        _check(api.IsProcessInJob(info.hProcess, job, ctypes.byref(associated)) and associated.value,
               'Owned Windows adapter job association could not be verified')
        started = True
        if on_started is not None:
            on_started(int(info.dwProcessId))
        # CREATE_SUSPENDED begins with exactly one suspension. Any unexpected
        # suspension count is a hard error; cleanup still owns the whole job.
        _check(api.ResumeThread(info.hThread) == 1, 'Owned Windows adapter could not be resumed')
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Owned adapter exceeded its timeout')
            wait = api.WaitForSingleObject(info.hProcess, max(1, min(250, int(remaining * 1000))))
            if wait == WAIT_OBJECT_0:
                break
            if wait != WAIT_TIMEOUT:
                raise RuntimeError('Owned Windows adapter wait failed')
            if on_poll is not None:
                on_poll()
        elapsed = (time.perf_counter() - start) * 1000
        code = DWORD()
        _check(api.GetExitCodeProcess(info.hProcess, ctypes.byref(code)), 'Owned Windows adapter exit code is unavailable')
        return_code, pid = int(code.value), int(info.dwProcessId)
    finally:
        try:
            if info.hProcess:
                try:
                    _cleanup_job(api, job, info.hProcess, report)
                except BaseException:
                    # Retain the direct owned handle as a fallback even when
                    # job association/cleanup verification failed. Reap the
                    # suspended primary before propagating the hard failure.
                    api.TerminateProcess(info.hProcess, 1)
                    if api.WaitForSingleObject(info.hProcess, 2000) != WAIT_OBJECT_0:
                        raise RuntimeError('Owned Windows primary fallback cleanup did not complete') from None
                    raise
            if job:
                _check(api.CloseHandle(job), 'Owned Windows job handle could not be closed')
                job = None
            if started and on_finished is not None:
                on_finished()
        finally:
            if job:
                api.CloseHandle(job)  # KILL_ON_JOB_CLOSE remains the final safety net.
            if info.hThread:
                api.CloseHandle(info.hThread)
            if info.hProcess:
                api.CloseHandle(info.hProcess)
            if null_handle and null_handle != INVALID_HANDLE_VALUE:
                api.CloseHandle(null_handle)
            if initialized:
                api.DeleteProcThreadAttributeList(attributes)
    return return_code, elapsed, pid
