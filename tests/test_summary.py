from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from unity_bench import summary  # noqa: E402


def process(index, variant, workload='w', status='ok', **extra):
    return {'type': 'process', 'index': index, 'workload': workload, 'kind': 'editor.adapter', 'variant': variant, 'repeat': 0, 'status': status, 'error': None, 'host': {'available': False}, **extra}


def sample(index, variant, name, ms, phase='warm', hash=None, counters=None, workload='w', **extra):
    return {'type': 'sample', 'index': index, 'workload': workload, 'variant': variant, 'name': name, 'ms': ms, 'phase': phase, 'round': 0,
            'info': {}, 'counters': counters, 'hash': hash, **extra}


class MathTests(unittest.TestCase):
    def test_describe_and_empty(self):
        self.assertEqual(summary.describe([3, 1, 2]), {'n': 3, 'median': 2, 'min': 1, 'max': 3})
        self.assertIsNone(summary.describe([]))

    def test_process_median_comes_before_the_median_across_processes(self):
        # Process 0 has many samples, process 1 has one. The pooled median would be 1; per process it is median(1, 10) = 5.5.
        samples = [sample(0, 'a', 'x', v) for v in (1, 1, 1, 1)] + [sample(1, 'a', 'x', 10)]
        medians = summary.process_medians(samples)
        self.assertEqual(medians, [1, 10])
        self.assertEqual(summary.describe(medians)['median'], 5.5)

    def test_ratio_and_range_overlap(self):
        a = summary.describe([10, 11, 12])
        b = summary.describe([5, 6, 7])
        r = summary.compare(a, b)
        self.assertAlmostEqual(r['ratio'], 6 / 11)
        self.assertFalse(r['ranges_overlap'])          # max(b)=7 < min(a)=10
        c = summary.describe([9, 11, 13])
        self.assertTrue(summary.compare(a, c)['ranges_overlap'])
        self.assertIsNone(summary.compare(a, None))
        self.assertIsNone(summary.compare(summary.describe([0, 0]), b))


class IdentityTests(unittest.TestCase):
    V = ['a', 'b']

    def test_identical_unreported_and_differs(self):
        same = [sample(0, 'a', 'x', 1, hash='h', counters={'n': 1}), sample(1, 'b', 'x', 2, hash='h', counters={'n': 1})]
        self.assertEqual(summary.identity(same, self.V)['status'], 'identical')
        self.assertEqual(summary.identity([sample(0, 'a', 'x', 1), sample(1, 'b', 'x', 2)], self.V)['status'], 'unreported')
        diff = [sample(0, 'a', 'x', 1, hash='h1'), sample(1, 'b', 'x', 2, hash='h2')]
        result = summary.identity(diff, self.V)
        self.assertEqual((result['status'], result['detail']), ('differs', 'variants differ'))

    def test_counters_alone_are_compared(self):
        diff = [sample(0, 'a', 'x', 1, counters={'cells': 189280}), sample(1, 'b', 'x', 2, counters={'cells': 183872})]
        self.assertEqual(summary.identity(diff, self.V)['status'], 'differs')

    def test_instability_inside_one_variant_is_named(self):
        diff = [sample(0, 'a', 'x', 1, hash='h1'), sample(1, 'a', 'x', 1, hash='h2'), sample(2, 'b', 'x', 1, hash='h1')]
        self.assertEqual(summary.identity(diff, self.V)['detail'], 'unstable within a')


class SummariseTests(unittest.TestCase):
    def rows(self):
        rows = []
        for i, (v, scale) in enumerate([('a', 1.0), ('b', 0.5), ('b', 0.5), ('a', 1.0)]):
            rows.append(process(i, v))
            rows += [sample(i, v, 'fast', 10 * scale, hash='same'), sample(i, v, 'fast', 12 * scale, hash='same'),
                     sample(i, v, 'fast', 100, phase='first-use', hash='same'),
                     sample(i, v, 'odd', 5 * scale, hash='x' + v)]
        return rows

    def test_first_use_is_excluded_by_default_but_kept(self):
        out = summary.summarise(self.rows())
        item = out['workloads'][0]['items'][0]
        self.assertEqual(item['name'], 'fast')
        self.assertEqual(item['variants']['a']['median'], 11)       # first-use 100 not in it
        self.assertEqual(out['workloads'][0]['first_use'][0]['ms'], [100, 100])
        included = summary.summarise(self.rows(), exclude_first_use=False)['workloads'][0]['items'][0]
        self.assertEqual(included['variants']['a']['median'], 12)    # median(10, 12, 100)

    def test_ratio_baseline_and_no_overlap_flag(self):
        item = summary.summarise(self.rows())['workloads'][0]['items'][0]
        self.assertAlmostEqual(item['ratios']['b']['ratio'], 0.5)
        self.assertFalse(item['ratios']['b']['ranges_overlap'])
        self.assertEqual(item['identity']['status'], 'identical')

    def test_differing_items_are_separated_and_left_out_of_the_aggregate(self):
        w = summary.summarise(self.rows())['workloads'][0]
        self.assertEqual([i['name'] for i in w['items']], ['fast'])
        self.assertEqual([i['name'] for i in w['differing']], ['odd'])
        self.assertNotIn('b', w['aggregate'])                        # one item left: no aggregate to report
        kept = summary.summarise(self.rows(), include_differing=True)['workloads'][0]
        self.assertEqual([i['name'] for i in kept['items']], ['fast', 'odd'])
        self.assertEqual(kept['differing'], [])

    def test_work_is_judged_per_compared_pair(self):
        # 6.7 does different work from both 7 variants, which match each other: the 7/7 ratio stays, the 7/6.7 one is withheld.
        rows = [process(i, v) for i, v in enumerate(('6.7', '7i', '7c'))]
        rows += [sample(0, '6.7', 'frame', 10, counters={'fractures': 100}),
                 sample(1, '7i', 'frame', 11, counters={'fractures': 102}),
                 sample(2, '7c', 'frame', 8, counters={'fractures': 102})]
        w = summary.summarise(rows, baselines={'7i': '6.7', '7c': '7i'})['workloads'][0]
        self.assertEqual([i['name'] for i in w['items']], ['frame'])
        item = w['items'][0]
        self.assertEqual(item['pair_work'], {'7i': 'differs', '7c': 'same'})
        self.assertIsNone(item['ratios']['7i'])
        self.assertAlmostEqual(item['ratios']['7c']['ratio'], 8 / 11)
        self.assertAlmostEqual(item['diagnostic_ratios']['7i']['ratio'], 11 / 10)
        # When every compared pair differs, the item is set apart as before.
        rows[-1] = sample(2, '7c', 'frame', 8, counters={'fractures': 101})
        w = summary.summarise(rows, baselines={'7i': '6.7', '7c': '7i'})['workloads'][0]
        self.assertEqual(w['items'], [])
        self.assertEqual([i['name'] for i in w['differing']], ['frame'])

    def test_aggregate_geomean(self):
        rows = []
        for i, v in enumerate(['a', 'b']):
            rows.append(process(i, v))
            rows += [sample(i, v, 'x', 10 if v == 'a' else 5), sample(i, v, 'y', 10 if v == 'a' else 20)]
        agg = summary.summarise(rows)['workloads'][0]['aggregate']['b']
        self.assertAlmostEqual(agg['geomean'], 1.0)                  # 0.5 and 2.0
        self.assertEqual((agg['faster_separated'], agg['slower_separated']), (1, 1))

    def test_failed_processes_are_listed_and_contribute_nothing(self):
        rows = self.rows() + [process(9, 'b', status='timeout', error='timeout; process tree killed')]
        out = summary.summarise(rows)
        self.assertEqual(out['failures'][0]['status'], 'timeout')
        self.assertEqual(out['workloads'][0]['items'][0]['variants']['b']['n'], 2)

    def test_reload_table_uses_per_process_medians_and_skips_first_use_and_restore_steps(self):
        rows = [process(0, 'a'), process(1, 'b'), sample(0, 'a', 'x', 1), sample(1, 'b', 'x', 1)]
        for i, v, total in ((0, 'a', 100), (0, 'a', 300), (1, 'b', 50)):
            rows.append({'type': 'reload', 'index': i, 'workload': 'w', 'variant': v, 'step': 'leaf', 'round': 1,
                         'reloads': [{'total_ms': total, 'phases': {'FinalizeReload': total // 2}}], 'refreshes': []})
        rows.append({'type': 'reload', 'index': 0, 'workload': 'w', 'variant': 'a', 'step': 'leaf', 'round': 0, 'reloads': [{'total_ms': 9999, 'phases': {}}], 'refreshes': []})
        rows.append({'type': 'reload', 'index': 0, 'workload': 'w', 'variant': 'a', 'step': 'restore:leaf', 'round': 1, 'reloads': [{'total_ms': 1, 'phases': {}}], 'refreshes': []})
        tables = summary.summarise(rows)['workloads'][0]['reload']
        self.assertEqual([t['step'] for t in tables], ['leaf'])
        self.assertEqual(tables[0]['cells']['a']['total']['median'], 200)    # median(100, 300) in one process
        self.assertEqual(tables[0]['cells']['b']['total']['median'], 50)

    def test_host_summary_is_annotation_only(self):
        host = {'available': True, 'samples': 3, 'machine_cpu_median': 80, 'machine_cpu_max': 99, 'other_cpu_median': 70, 'other_cpu_max': 90, 'memory_percent_max': 50}
        out = summary.summarise([process(0, 'a', host=host), sample(0, 'a', 'x', 1)])
        self.assertEqual(out['host']['a']['machine_cpu_max'], 99)
        self.assertEqual(out['workloads'][0]['items'][0]['variants']['a']['n'], 1)   # a 99% busy machine discards nothing


if __name__ == '__main__':
    unittest.main()
