import os
from pathlib import Path
import sys
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


if __name__ == '__main__':
    unittest.main()
