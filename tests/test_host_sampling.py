import collections
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from unity_bench import host_sampling  # noqa: E402


class HostSamplingTests(unittest.TestCase):
    def test_without_psutil_everything_is_a_no_op(self):
        with mock.patch.object(host_sampling, 'psutil', None):
            sampler = host_sampling.Sampler()
            sampler.sample(os.getpid())
            self.assertEqual(sampler.summary(), {'available': False})
            self.assertIsNone(host_sampling.describe()['psutil'])

    @unittest.skipUnless(host_sampling.psutil, 'psutil not installed')
    def test_with_psutil_a_sample_is_recorded_and_never_raises(self):
        sampler = host_sampling.Sampler()
        sampler._last = 0   # allow an immediate sample
        sampler.sample(os.getpid())
        sampler._last = 0
        sampler.sample(2 ** 22 - 3)   # a pid that does not exist is ignored
        summary = sampler.summary()
        self.assertTrue(summary['available'])
        self.assertGreaterEqual(summary['samples'], 1)
        self.assertLessEqual(summary['other_cpu_max'], 100)


class FakeProcess:
    def __init__(self, pid, cpu_by_call, children=()):
        self.pid, self.cpu_by_call, self.kids, self.calls = pid, list(cpu_by_call), children, 0

    def cpu_times(self):
        value = self.cpu_by_call[min(self.calls, len(self.cpu_by_call) - 1)]
        self.calls += 1
        return collections.namedtuple('Times', 'user system')(value, 0.0)

    def children(self, recursive=True):
        kids = self.kids() if callable(self.kids) else self.kids
        return list(kids)


class SamplerWithFakePsutilTests(unittest.TestCase):
    def fake(self, root, machine=(50.0, 50.0, 50.0)):
        values = iter(machine)
        class Error(Exception):
            pass
        return types.SimpleNamespace(Error=Error, __version__='fake', Process=lambda pid: root, cpu_percent=lambda _=None: next(values, 50.0),
                                     virtual_memory=lambda: types.SimpleNamespace(percent=40.0, total=2 ** 30))

    def sampler(self, psutil):
        clock = iter(range(0, 100))
        patches = [mock.patch.object(host_sampling, 'psutil', psutil), mock.patch.object(host_sampling.time, 'monotonic', side_effect=lambda: next(clock)),
                   mock.patch.object(host_sampling.os, 'cpu_count', return_value=4)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return host_sampling.Sampler()

    def test_series_stay_aligned_when_a_lookup_fails_midway(self):
        class Flaky(FakeProcess):
            def children(self, recursive=True):
                self.fail = getattr(self, 'fail', 0) + 1
                if self.fail == 2:
                    raise self.error('exited')
                return []
        psutil = self.fake(None)
        root = Flaky(1, [0.5, 1.0, 1.5, 2.0])
        root.error = psutil.Error
        psutil.Process = lambda pid: root
        sampler = self.sampler(psutil)
        for _ in range(4):
            sampler.sample(1)
        out = sampler.summary()
        self.assertEqual(len(sampler.cpu), len(sampler.tree))
        self.assertEqual(len(sampler.cpu), len(sampler.memory))
        self.assertEqual(out['samples'], 3)                                  # the failed attempt recorded nothing, instead of one series
        self.assertEqual({len(v) for k, v in out['series'].items() if k != 'interval_s'}, {3})

    def test_a_child_first_seen_mid_run_is_charged_its_cpu_so_far_not_zero(self):
        late_child = FakeProcess(2, [0.8])
        kids = []
        root = FakeProcess(1, [0.4, 0.8, 1.2], children=lambda: kids)
        sampler = self.sampler(self.fake(root))
        sampler.sample(1)                       # root alone: 0.4 s of CPU in 1 s on 4 CPUs
        kids.append(late_child)
        sampler.sample(1)                       # the child has already used 0.8 s; root 0.4 s more
        self.assertAlmostEqual(sampler.tree[0], 10.0)
        self.assertAlmostEqual(sampler.tree[1], 30.0)                        # (0.4 + 0.8) / 1 s / 4 CPUs
        sampler.sample(1)
        self.assertAlmostEqual(sampler.tree[2], 10.0 + 0.0)                  # the child's CPU is not charged twice (its fake total stays 0.8)

    def test_summary_keeps_a_compact_series_and_other_is_machine_minus_tree(self):
        sampler = self.sampler(self.fake(FakeProcess(1, [0.4, 0.8])))
        sampler.sample(1)
        sampler.sample(1)
        out = sampler.summary()
        self.assertEqual(out['series']['interval_s'], 1.0)
        self.assertEqual(out['series']['machine_cpu'], [50.0, 50.0])
        self.assertEqual(out['series']['tree_cpu'], [10.0, 10.0])
        self.assertEqual(out['other_cpu_median'], 40.0)


if __name__ == '__main__':
    unittest.main()
