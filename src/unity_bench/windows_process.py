"""Windows 10+ adapter ownership with atomic Job Object association.

CreateProcessW receives an owned job via PROC_THREAD_ATTRIBUTE_JOB_LIST while
CREATE_SUSPENDED keeps user code stopped. Only owned NUL handles are inherited.
No fallback starts an uncontained process; unsupported job/attribute APIs refuse.

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


def _check(success, message):
    if not success:
        raise RuntimeError(message)


FAILURE_STAGES = frozenset({'launch', 'callback-started', 'callback-poll', 'cleanup-job',
                            'cleanup-fallback', 'close-job', 'callback-finished', 'cleanup-handles'})
FAILURE_TYPES = frozenset({'Exception', 'OSError', 'ValueError', 'RuntimeError', 'TimeoutError', 'KeyError',
                           'TypeError', 'OverflowError', 'AttributeError', 'MemoryError', 'KeyboardInterrupt',
                           'SystemExit', 'FileNotFoundError', 'FileExistsError', 'PermissionError',
                           'ProcessLookupError', 'InterruptedError', 'BrokenPipeError', 'ConnectionResetError',
                           'NotADirectoryError', 'IsADirectoryError'})


def failure_details(error):
    """Safe type/stage metadata only; never exception text, argv or paths."""
    rows = getattr(error, 'unity_bench_failures', None)
    if type(rows) is not list or len(rows) > 32:
        return []
    return [{'stage': row['stage'], 'type': row['type']} for row in rows
            if type(row) is dict and set(row) == {'stage', 'type'}
            and type(row.get('stage')) is str and row['stage'] in FAILURE_STAGES
            and type(row.get('type')) is str and row['type'] in FAILURE_TYPES]


def _record_failure(details, error, stage):
    label = type(error).__name__
    details.append({'stage': stage, 'type': label if label in FAILURE_TYPES else 'Exception'})
    error.unity_bench_failures = [dict(row) for row in details]


def _cleanup_job(api, job, process):
    _check(api.TerminateJobObject(job, 1), 'Owned Windows job could not be terminated')
    deadline = time.monotonic() + 2
    while True:
        accounting = BasicAccountingInformation()
        _check(api.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None),
               'Owned Windows job cleanup could not be verified')
        if accounting.ActiveProcesses == 0:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Owned Windows job cleanup did not complete')
        time.sleep(0.02)
    if api.WaitForSingleObject(process, 2000) != WAIT_OBJECT_0:
        raise RuntimeError('Owned Windows primary process cleanup did not complete')


def launch_windows(argv, cwd, timeout, on_started=None, on_poll=None, on_finished=None, *, _api=None):
    """Start only a suspended, job-contained process; discard stdout/stderr."""
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
    original_failure = None; details = []; stage = 'launch'
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
            stage = 'callback-started'
            on_started(int(info.dwProcessId))
            stage = 'launch'
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
                stage = 'callback-poll'
                on_poll()
                stage = 'launch'
        code = DWORD()
        _check(api.GetExitCodeProcess(info.hProcess, ctypes.byref(code)), 'Owned Windows adapter exit code is unavailable')
        return_code, pid = int(code.value), int(info.dwProcessId)
    except BaseException as error:
        original_failure = error
        _record_failure(details, error, stage)
        raise
    finally:
        cleanup_recorded = None
        try:
            if info.hProcess:
                try:
                    stage = 'cleanup-job'
                    _cleanup_job(api, job, info.hProcess)
                except BaseException as error:
                    _record_failure(details, error, stage); cleanup_recorded = error
                    # Preserve the same owned fallback and its hard refusal.
                    stage = 'cleanup-fallback'
                    api.TerminateProcess(info.hProcess, 1)
                    if api.WaitForSingleObject(info.hProcess, 2000) != WAIT_OBJECT_0:
                        raise RuntimeError('Owned Windows primary fallback cleanup did not complete') from None
                    raise
            if job:
                stage = 'close-job'
                _check(api.CloseHandle(job), 'Owned Windows job handle could not be closed')
                job = None
            if started and on_finished is not None:
                stage = 'callback-finished'
                try:
                    on_finished()
                except BaseException as error:
                    _record_failure(details, error, stage); cleanup_recorded = error
                    if original_failure is not None and isinstance(error, Exception):
                        # Verified job cleanup has already finished. Keep the
                        # original observation/launch failure as the headline,
                        # retaining this independent callback failure as metadata.
                        original_failure.unity_bench_failures = [dict(row) for row in details]
                        raise original_failure from error
                    raise
        except BaseException as error:
            if error is not cleanup_recorded and error is not original_failure:
                _record_failure(details, error, stage)
            else:
                error.unity_bench_failures = [dict(row) for row in details]
            raise
        finally:
            try:
                stage = 'cleanup-handles'
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
            except BaseException as error:
                _record_failure(details, error, stage)
                raise
    return return_code, (time.perf_counter() - start) * 1000, pid
