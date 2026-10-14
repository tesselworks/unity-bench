"""Host CPU and memory sampling, recorded as annotations only.

Nothing here can refuse, delay or discard a measurement. Without psutil every
call is a no-op and the report simply has no host columns.
"""
import os
import platform
import statistics
import time

try:
    import psutil
except ImportError:
    psutil = None

INTERVAL_SECONDS = 1.0
WINDOWS = os.name == 'nt'


def describe():
    """Static facts about the machine, stored once per campaign."""
    info = {'platform': platform.platform(), 'machine': platform.machine(), 'cpus': os.cpu_count(),
            'python': platform.python_version(), 'psutil': psutil.__version__ if psutil else None}
    if psutil:
        info['memory_total_mb'] = round(psutil.virtual_memory().total / 2**20)
    return info


class Sampler:
    """Call `sample(pid)` periodically while a process runs, then `summary()`.

    machine_cpu is whole-machine CPU percent since the previous sample. tree_cpu is the CPU time the launched process and
    its children used since the previous sample, on the same 0-100 scale, so other_cpu = machine - tree is only an
    approximation of what everything else was doing: a child that starts and ends between two samples is not seen at all.
    A child first seen mid-run is charged all of its CPU time so far, so a process that lives for one interval is not
    counted as idle (psutil's own cpu_percent returns 0.0 on its first call for a new process). The compact series, one
    value per interval, is kept with the summary.
    """

    def __init__(self):
        self.cpu, self.tree, self.memory, self.rss, self.private, self.root_rss = [], [], [], [], [], []
        self._private_ok = True   # off after the first process whose private memory this OS or user may not read
        self.load = os.getloadavg()[0] if hasattr(os, 'getloadavg') else None
        self._procs, self._times = {}, {}
        self._last = time.monotonic()
        if psutil:
            psutil.cpu_percent(None)

    def sample(self, pid):
        now = time.monotonic()
        if not psutil or now - self._last < INTERVAL_SECONDS:
            return
        elapsed, self._last = now - self._last, now
        try:
            # Everything is read before anything is stored, so the three series stay aligned even when a lookup fails.
            cpu, memory = psutil.cpu_percent(None), psutil.virtual_memory().percent
            root = self._procs.get(pid) or self._procs.setdefault(pid, psutil.Process(pid))
            used, resident, private, own = 0.0, 0, 0, 0
            for proc in [root, *root.children(recursive=True)]:
                try:
                    times = proc.cpu_times()
                except psutil.Error:
                    continue   # exited between listing and reading
                total = times.user + times.system
                used += total - self._times.get(proc.pid, 0.0)
                self._times[proc.pid] = total
                try:
                    resident += proc.memory_info().rss
                    if proc is root:
                        own = resident   # the launched process alone (the Editor), before its children are added
                except psutil.Error:
                    pass   # resident size is an extra; a process that denies it still counts for CPU
                if self._private_ok:
                    private += self.private_bytes(proc) or 0
            tree = min(100.0, 100.0 * used / elapsed / (os.cpu_count() or 1))
        except psutil.Error:
            return  # the process may have just exited; a missing sample is not an error
        self.cpu.append(cpu)
        self.memory.append(memory)
        self.tree.append(tree)
        self.rss.append(resident)
        self.root_rss.append(own)
        if self._private_ok:
            self.private.append(private)

    def private_bytes(self, proc):
        """Private (not shared) memory of one process: private bytes on Windows, unique set size elsewhere. None when the OS does not let us read it."""
        try:
            return proc.memory_info().private if WINDOWS else proc.memory_full_info().uss
        except Exception as error:   # psutil.Error, or an OS that has no such field
            if not isinstance(error, getattr(psutil, 'NoSuchProcess', ())):
                self._private_ok = False   # macOS answers AccessDenied for other processes' unique set size; do not ask again this process
            return None

    def summary(self):
        if not psutil:
            return {'available': False}
        other = [max(0.0, m - t) for m, t in zip(self.cpu, self.tree)]
        out = {'available': True, 'samples': len(self.cpu), 'load1_at_start': self.load}
        if self.cpu:
            out.update(machine_cpu_median=round(statistics.median(self.cpu), 1), machine_cpu_max=round(max(self.cpu), 1),
                       other_cpu_median=round(statistics.median(other), 1), other_cpu_max=round(max(other), 1),
                       memory_percent_max=round(max(self.memory), 1), tree_rss_max_mb=round(max(self.rss) / 2**20, 1),   # resident set of the process tree, sampled once a second
                       root_rss_max_mb=round(max(self.root_rss) / 2**20, 1),
                       **({'tree_private_max_mb': round(max(self.private) / 2**20, 1)} if self._private_ok and self.private and max(self.private) > 0 else {}),   # private bytes / unique set size of the tree
                       series={'interval_s': INTERVAL_SECONDS, 'machine_cpu': [round(x, 1) for x in self.cpu],
                               'tree_cpu': [round(x, 1) for x in self.tree], 'memory_percent': [round(x, 1) for x in self.memory],
                               'tree_rss_mb': [round(x / 2**20, 1) for x in self.rss]})
        return out
