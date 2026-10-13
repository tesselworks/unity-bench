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


def describe():
    """Static facts about the machine, stored once per campaign."""
    info = {'platform': platform.platform(), 'machine': platform.machine(), 'cpus': os.cpu_count(),
            'python': platform.python_version(), 'psutil': psutil.__version__ if psutil else None}
    if psutil:
        info['memory_total_mb'] = round(psutil.virtual_memory().total / 2**20)
    return info


class Sampler:
    """Call `sample(pid)` periodically while a process runs, then `summary()`.

    machine_cpu is whole-machine CPU percent since the previous sample. tree_cpu is the
    launched process and its children, normalised to the same 0-100 scale, so
    other_cpu = machine - tree is a rough measure of what everything else was doing.
    """

    def __init__(self):
        self.cpu, self.tree, self.memory = [], [], []
        self.load = os.getloadavg()[0] if hasattr(os, 'getloadavg') else None
        self._procs = {}
        self._last = time.monotonic()
        if psutil:
            psutil.cpu_percent(None)

    def sample(self, pid):
        if not psutil or time.monotonic() - self._last < 1.0:
            return
        self._last = time.monotonic()
        try:
            self.cpu.append(psutil.cpu_percent(None))
            self.memory.append(psutil.virtual_memory().percent)
            root = self._procs.get(pid) or self._procs.setdefault(pid, psutil.Process(pid))
            total = 0.0
            for proc in [root, *root.children(recursive=True)]:
                proc = self._procs.setdefault(proc.pid, proc)
                total += proc.cpu_percent(None)
            self.tree.append(min(100.0, total / (os.cpu_count() or 1)))
        except psutil.Error:
            pass  # the process may have just exited; a missing sample is not an error

    def summary(self):
        if not psutil:
            return {'available': False}
        other = [max(0.0, m - t) for m, t in zip(self.cpu, self.tree)]
        out = {'available': True, 'samples': len(self.cpu), 'load1_at_start': self.load}
        if self.cpu:
            out.update(machine_cpu_median=round(statistics.median(self.cpu), 1), machine_cpu_max=round(max(self.cpu), 1),
                       other_cpu_median=round(statistics.median(other), 1), other_cpu_max=round(max(other), 1),
                       memory_percent_max=round(max(self.memory), 1))
        return out
