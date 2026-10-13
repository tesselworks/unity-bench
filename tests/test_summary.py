from math import comb
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from unity_bench import identity, report, stats, summary  # noqa: E402


def process(index, variant, workload='w', status='ok', **extra):
    return {'type': 'process', 'index': index, 'workload': workload, 'kind': 'editor.adapter', 'variant': variant, 'repeat': 0, 'status': status, 'error': None, 'host': {'available': False}, **extra}


def sample(index, variant, name, ms, phase='warm', hash=None, counters=None, workload='w', **extra):
    return {'type': 'sample', 'index': index, 'workload': workload, 'variant': variant, 'name': name, 'ms': ms, 'phase': phase, 'round': 0,
            'info': {}, 'counters': counters, 'hash': hash, **extra}


def two_variants(items, headline=None, **options):
    """Each item is (name, ms in a, ms in b); two processes per variant, so n = 2."""
    rows = []
    for i, v in enumerate(['a', 'a', 'b', 'b']):
        rows.append(process(i, v))
        rows += [sample(i, v, name, a if v == 'a' else b * (1 + 0.01 * (i % 2))) for name, a, b in items]
    return summary.summarise(rows, headline=headline, **options)['workloads'][0]


class MathTests(unittest.TestCase):
    def test_describe_and_empty(self):
        self.assertEqual(stats.describe([3, 1, 2]), {'n': 3, 'median': 2, 'min': 1, 'max': 3, 'values': [3, 1, 2]})
        self.assertIsNone(stats.describe([]))

    def test_process_median_comes_before_the_median_across_processes(self):
        # Process 0 has many samples, process 1 has one. The pooled median would be 1; per process it is median(1, 10) = 5.5.
        samples = [sample(0, 'a', 'x', v) for v in (1, 1, 1, 1)] + [sample(1, 'a', 'x', 10)]
        medians = stats.process_medians(samples)
        self.assertEqual(medians, [1, 10])
        self.assertEqual(stats.describe(medians)['median'], 5.5)

    def test_ratio_range_overlap_and_n_per_side(self):
        a = stats.describe([10, 11, 12])
        b = stats.describe([5, 6, 7])
        r = stats.compare(a, b)
        self.assertAlmostEqual(r['ratio'], 6 / 11)
        self.assertFalse(r['ranges_overlap'])          # max(b)=7 < min(a)=10
        self.assertEqual(r['n'], [3, 3])
        self.assertTrue(stats.compare(a, stats.describe([9, 11, 13]))['ranges_overlap'])
        self.assertEqual(stats.compare(a, stats.describe([5, 6]))['n'], [3, 2])
        self.assertIsNone(stats.compare(a, None))

    def test_a_median_below_the_resolution_has_no_ratio_and_never_raises(self):
        b = stats.describe([5, 6, 7])
        for base in (stats.describe([0, 0]), stats.describe([0, 0.0002])):
            r = stats.compare(base, b)
            self.assertEqual((r['ratio'], r['below_resolution']), (None, True))
        self.assertTrue(stats.compare(b, stats.describe([0]))['below_resolution'])
        self.assertIsNotNone(stats.compare(b, stats.describe([0.002, 0.004]))['ratio'])

    def test_chance_of_separated_ranges_matches_the_published_numbers(self):
        self.assertAlmostEqual(stats.chance_separation(2, 2), 1 / 3)
        self.assertAlmostEqual(stats.chance_separation(3, 3), 0.1)
        self.assertAlmostEqual(stats.chance_separation(4, 4), 2 / 70)
        self.assertAlmostEqual(stats.chance_separation(2, 3), 2 / comb(5, 2))   # unequal sizes
        self.assertEqual(stats.chance_separation(1, 1), 1.0)
        # Brute force for n = 3 vs 2: of the 10 ways to split 5 ordered values into groups, 2 separate them.
        from itertools import combinations
        splits = [set(c) for c in combinations(range(5), 3)]
        self.assertEqual(sum(1 for c in splits if max(c) < 3 or min(c) >= 2), 2)

    def test_bootstrap_interval_is_deterministic_and_contains_the_ratio_for_clear_cases(self):
        low, high = stats.bootstrap_ratio([10, 11, 12, 10.5], [5, 5.5, 6, 5.2], 500)
        self.assertEqual((low, high), tuple(stats.bootstrap_ratio([10, 11, 12, 10.5], [5, 5.5, 6, 5.2], 500)))
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.45)
        self.assertLess(high, 0.7)
        self.assertIsNone(stats.bootstrap_ratio([0, 0], [1, 2], 50))   # a zero baseline median gives no ratio at all


class IdentityTests(unittest.TestCase):
    V = ['a', 'b']

    def test_identical_unreported_and_differs(self):
        same = [sample(0, 'a', 'x', 1, hash='h', counters={'n': 1}), sample(1, 'b', 'x', 2, hash='h', counters={'n': 1})]
        self.assertEqual(identity.identity(same, self.V)['status'], 'identical')
        self.assertEqual(identity.identity([sample(0, 'a', 'x', 1), sample(1, 'b', 'x', 2)], self.V)['status'], 'unreported')
        diff = [sample(0, 'a', 'x', 1, hash='h1'), sample(1, 'b', 'x', 2, hash='h2')]
        result = identity.identity(diff, self.V)
        self.assertEqual((result['status'], result['detail']), ('differs', 'variants differ'))

    def test_counters_alone_are_compared(self):
        diff = [sample(0, 'a', 'x', 1, counters={'cells': 189280}), sample(1, 'b', 'x', 2, counters={'cells': 183872})]
        self.assertEqual(identity.identity(diff, self.V)['status'], 'differs')

    def test_instability_inside_one_variant_is_named(self):
        diff = [sample(0, 'a', 'x', 1, hash='h1'), sample(1, 'a', 'x', 1, hash='h2'), sample(2, 'b', 'x', 1, hash='h1')]
        self.assertEqual(identity.identity(diff, self.V)['detail'], 'unstable within a')

    def test_one_side_or_some_rounds_reporting_is_partial_not_same(self):
        one_side = [sample(0, 'a', 'x', 1, hash='h'), sample(1, 'b', 'x', 2)]
        result = identity.identity(one_side, self.V)
        self.assertEqual((result['status'], result['coverage']), ('partial', {'a': [1, 1], 'b': [0, 1]}))
        some_rounds = [sample(0, 'a', 'x', 1, hash='h'), sample(0, 'a', 'x', 1), sample(1, 'b', 'x', 2, hash='h'), sample(1, 'b', 'x', 2, hash='h')]
        result = identity.identity(some_rounds, self.V)
        self.assertEqual((result['status'], result['coverage']), ('partial', {'a': [1, 2], 'b': [2, 2]}))
        self.assertEqual(identity.pair_identity(one_side, 'a', 'b')[0], 'partial')
        self.assertEqual(identity.pair_identity([sample(0, 'a', 'x', 1, hash='h'), sample(1, 'b', 'x', 2, hash='h')], 'a', 'b')[0], 'same')


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

    def test_first_use_can_be_a_table_of_ratios(self):
        rows = self.rows()
        for r in rows:
            if r['type'] == 'sample' and r['phase'] == 'first-use':
                r['ms'] = 200 if r['variant'] == 'a' else 50
        table = summary.summarise(rows)['workloads'][0]['first_use_ratios'][0]
        self.assertEqual(table['name'], 'fast')
        self.assertAlmostEqual(table['ratios']['b']['ratio'], 0.25)
        self.assertEqual(table['variants']['a']['n'], 2)

    def test_ratio_baseline_and_ranges_separate_flag(self):
        item = summary.summarise(self.rows())['workloads'][0]['items'][0]
        self.assertAlmostEqual(item['ratios']['b']['ratio'], 0.5)
        self.assertFalse(item['ratios']['b']['ranges_overlap'])
        self.assertEqual(item['ratios']['b']['n'], [2, 2])
        self.assertEqual(item['identity']['status'], 'identical')

    def test_differing_items_are_separated_and_left_out_of_the_aggregate(self):
        w = summary.summarise(self.rows(), headline={'w': True})['workloads'][0]
        self.assertEqual([i['name'] for i in w['items']], ['fast'])
        self.assertEqual([i['name'] for i in w['differing']], ['odd'])
        self.assertNotIn('b', w['aggregate'])                        # one item left: no aggregate to report
        kept = summary.summarise(self.rows(), include_differing=True)['workloads'][0]
        self.assertEqual([i['name'] for i in kept['items']], ['fast', 'odd'])
        self.assertEqual(kept['differing'], [])

    def test_include_differing_keeps_table_aggregate_and_metadata_consistent(self):
        out = summary.summarise(self.rows(), include_differing=True, headline={'w': True})
        w = out['workloads'][0]
        odd = next(i for i in w['items'] if i['name'] == 'odd')
        self.assertEqual(odd['pair_work'], {'b': 'differs'})
        self.assertAlmostEqual(odd['ratios']['b']['ratio'], 0.5)    # the ratio is in the table...
        self.assertEqual(w['aggregate']['b'][0]['items'], 2)         # ...and in the aggregate, which counts it
        self.assertEqual(w['aggregate']['b'][0]['work'], {'same': 1, 'differs': 1})
        self.assertTrue(out['options']['include_differing'])
        self.assertFalse(summary.summarise(self.rows())['options']['include_differing'])

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

    def test_a_pair_with_one_side_reporting_is_partial_and_shows_coverage(self):
        rows = [process(0, 'a'), process(1, 'b'), sample(0, 'a', 'x', 10, hash='h'), sample(1, 'b', 'x', 5)]
        item = summary.summarise(rows)['workloads'][0]['items'][0]
        self.assertEqual(item['pair_work'], {'b': 'partial'})
        self.assertEqual(item['pair_coverage'], {'b': {'a': [1, 1], 'b': [0, 1]}})
        self.assertIsNotNone(item['ratios']['b'])                    # annotated, not withheld

    def test_aggregate_is_empty_without_headline_items(self):
        w = two_variants([('x', 10, 5), ('y', 10, 20)])
        self.assertEqual(w['aggregate'], {})
        self.assertEqual(w['headline_items'], 0)

    def test_aggregate_geomean_of_headline_items_only(self):
        w = two_variants([('x', 10, 5), ('y', 10, 20), ('z', 10, 100)], headline={'w': ['x', 'y']})
        agg = w['aggregate']['b'][0]
        self.assertAlmostEqual(agg['geomean'], 1.0, places=1)         # 0.5 and 2.0; z is not a headline item
        self.assertEqual(agg['items'], 2)
        self.assertEqual((agg['faster_separated'], agg['slower_separated']), (1, 1))
        self.assertEqual(two_variants([('x', 10, 5), ('y', 10, 20)], headline={'w': 'nothing*'.split()})['aggregate'], {})
        self.assertEqual(two_variants([('x', 10, 5), ('y', 10, 20)], headline={'w': True})['aggregate']['b'][0]['items'], 2)

    def test_headline_patterns_and_the_adapter_declaration(self):
        rows = [process(0, 'a', headline=['core/*']), process(1, 'a'), process(2, 'b'), process(3, 'b')]
        for i, v in enumerate('aabb'):
            rows += [sample(i, v, 'core/one', 10 if v == 'a' else 5), sample(i, v, 'core/two', 10 if v == 'a' else 20), sample(i, v, 'other', 1)]
        w = summary.summarise(rows)['workloads'][0]
        self.assertEqual({i['name'] for i in w['items'] if i['headline']}, {'core/one', 'core/two'})   # declared by the adapter's result
        self.assertEqual(summary.summarise(rows, headline={'w': ['other']})['workloads'][0]['headline_items'], 3)   # campaign and adapter add up

    def test_distribution_families_and_identical_series_are_not_mixed(self):
        rows = []
        for i, v in enumerate('aabb'):
            rows.append(process(i, v))
            scale = 1.0 if v == 'a' else 0.5
            for label in ('warmup', 'steady'):
                for stat, base in (('p50', 10), ('p95', 20), ('p99', 20), ('max', 20)):   # p95, p99 and max are the same series
                    rows.append(sample(i, v, f'frame {label} {stat}', base * scale * (3 if label == 'warmup' else 1), info={'frames': 9}))
        w = summary.summarise(rows, headline={'w': True})['workloads'][0]
        # p50, p95, p99, max and warmup/steady are separate families with one item each: nothing is aggregated across statistics or phases
        self.assertEqual(w['aggregate'], {})
        # Two headline series inside one family do aggregate, and an identical one counts once.
        rows2 = []
        for i, v in enumerate('aabb'):
            rows2.append(process(i, v))
            for name, a, b in (('s1', 10, 5), ('s2', 10, 20), ('s2 twin', 10, 20)):
                rows2.append(sample(i, v, f'{name} steady p95', a if v == 'a' else b, info={'frames': 9}))
        agg = summary.summarise(rows2, headline={'w': True})['workloads'][0]['aggregate']['b'][0]
        self.assertEqual((agg['family'], agg['items'], agg['duplicates']), ('steady p95', 2, 1))

    def test_zero_and_sub_resolution_values_are_shown_not_aggregated_and_never_crash(self):
        w = two_variants([('zero', 0, 0), ('tiny', 0.0001, 0.0002), ('x', 10, 5), ('y', 10, 20)], headline={'w': True})
        by = {i['name']: i for i in w['items']}
        self.assertEqual(by['zero']['ratios']['b'], {'ratio': None, 'below_resolution': True, 'n': [2, 2]})
        agg = w['aggregate']['b'][0]
        self.assertEqual((agg['items'], agg['below_resolution']), (2, 2))
        self.assertAlmostEqual(agg['geomean'], 1.0, places=1)
        all_zero = two_variants([('zero', 0, 0), ('zero2', 0, 0)], headline={'w': True})
        self.assertEqual(all_zero['aggregate'], {})                    # [0, 0] used to raise StatisticsError
        text = report.cell(by['zero']['variants']['a'], resolution=0.001)
        self.assertTrue(text.startswith('<res'))
        self.assertEqual(report.ratio_cell(by['zero']['ratios']['b']), '<res')

    def test_bootstrap_is_opt_in_and_per_item(self):
        self.assertNotIn('bootstrap', summary.summarise(self.rows())['workloads'][0]['items'][0])
        item = summary.summarise(self.rows(), bootstrap=200)['workloads'][0]['items'][0]
        low, high = item['bootstrap']['b']
        self.assertAlmostEqual(low, 0.5, places=1)
        self.assertAlmostEqual(high, 0.5, places=1)

    def test_failed_processes_are_listed_and_contribute_nothing(self):
        rows = self.rows() + [process(9, 'b', status='timeout', error='timeout; process tree killed')]
        out = summary.summarise(rows)
        self.assertEqual(out['failures'][0]['status'], 'timeout')
        self.assertEqual(out['workloads'][0]['items'][0]['variants']['b']['n'], 2)
        self.assertEqual(out['workloads'][0]['observed']['processes'], {'a': 2, 'b': 2})

    def test_observed_samples_per_item_not_the_configured_rounds(self):
        rows = [process(0, 'a'), process(1, 'b'), sample(0, 'a', 'x', 1, phase='single'), sample(1, 'b', 'x', 1, phase='single')]
        w = summary.summarise(rows)['workloads'][0]
        self.assertEqual(w['observed']['samples_per_item'], [1, 1])
        self.assertEqual(w['observed']['phases'], {'single': 2})
        self.assertEqual(w['items'][0]['phases'], ['single'])        # single runs stay in the table, labelled as such

    def test_reload_blocks_of_one_round_are_summed_before_the_median(self):
        rows = [process(0, 'a'), process(1, 'b'), sample(0, 'a', 'x', 1), sample(1, 'b', 'x', 1)]
        def reload(index, variant, blocks, round_=1):
            return {'type': 'reload', 'index': index, 'workload': 'w', 'variant': variant, 'step': 'leaf', 'round': round_,
                    'reloads': [{'total_ms': t, 'phases': {'FinalizeReload': t // 2}} for t in blocks], 'refreshes': []}
        rows += [reload(0, 'a', [100, 100]), reload(1, 'b', [150])]
        cells = summary.summarise(rows)['workloads'][0]['reload'][0]['cells']
        self.assertEqual(cells['a']['total']['median'], 200)           # not 100: per-block medians would rank a faster than b
        self.assertEqual(cells['a']['FinalizeReload']['median'], 100)
        self.assertEqual(cells['b']['total']['median'], 150)
        self.assertEqual(cells['a']['reload blocks per round']['median'], 2)
        self.assertEqual(cells['b']['reload blocks per round']['median'], 1)

    def test_reload_table_uses_per_process_medians_and_skips_first_use_and_restore_steps(self):
        rows = [process(0, 'a'), process(1, 'b'), sample(0, 'a', 'x', 1), sample(1, 'b', 'x', 1)]
        for i, v, total, round_ in ((0, 'a', 100, 1), (0, 'a', 300, 2), (1, 'b', 50, 1)):
            rows.append({'type': 'reload', 'index': i, 'workload': 'w', 'variant': v, 'step': 'leaf', 'round': round_,
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

    def test_environments_are_kept_per_role_and_several_are_listed(self):
        editor = {'unityVersion': '7000.0.0a7', 'runtime': '.NET 10', 'runtimeKind': 'coreclr', 'codeOptimization': 'Debug', 'platform': 'WindowsEditor'}
        player = {'unityVersion': '7000.0.0a7', 'runtime': '.NET 10', 'runtimeKind': 'il2cpp', 'codeOptimization': 'Release build', 'platform': 'WindowsPlayer', 'graphicsDevice': 'Direct3D12'}
        build = {'scriptingBackend': 'IL2CPP', 'apiCompatibility': 'NET_Standard'}
        rows = [process(0, 'a', role='setup-build', kind='player.adapter', environment=editor), process(0, 'a', kind='player.adapter', environment=player, build=build),
                process(1, 'a', kind='player.adapter', environment=player, build=build), process(2, 'a', kind='player.adapter', environment={**player, 'graphicsDevice': 'Vulkan'}, build=build),
                process(3, 'a', kind='editor.play', environment=editor), sample(0, 'a', 'x', 1)]
        env = summary.summarise(rows)['environments']['a']
        self.assertEqual(env['editor'][0]['environment']['codeOptimization'], 'Debug')
        self.assertEqual(env['editor'][0]['processes'], 2)
        self.assertEqual([e['processes'] for e in env['player']], [2, 1])                       # two player environments, the common one first
        self.assertEqual(env['player'][0]['environment']['codeOptimization'], 'Release build')   # the player's, not the Editor's
        self.assertEqual(env['player'][0]['build'], build)


class ReportTextTests(unittest.TestCase):
    def test_build_text_shows_a_value_the_editor_stored_instead_of_the_requested_one(self):
        self.assertEqual(report.build_text({'apiCompatibility': 'NET', 'apiCompatibilityRequested': 'NET_Standard', 'stripping': 'Minimal'}),
                         'API level NET (requested NET_Standard), stripping Minimal')
        self.assertEqual(report.build_text({'apiCompatibility': 'NET_Standard'}), 'API level NET_Standard')
        self.assertEqual(report.build_text(None), 'not recorded')

    def test_ratio_cell_states_n_and_separation(self):
        r = stats.compare(stats.describe([10, 11]), stats.describe([5, 6, 7]))
        self.assertEqual(report.ratio_cell(r), '0.57 (ranges separate, n=2 vs 3)')
        self.assertEqual(report.ratio_cell(stats.compare(stats.describe([10, 11]), stats.describe([9, 12]))), '1.00 (ranges overlap, n=2 vs 2)')
        self.assertNotIn('no overlap', report.ratio_cell(r))

    def test_work_cell_shows_coverage_for_partial(self):
        item = {'identity': {'status': 'partial', 'coverage': {'a': [3, 3], 'b': [0, 3]}}}
        self.assertEqual(report.work_cell(item, ['b'], {'b': 'a'}), 'partial (a 3/3, b 0/3)')


if __name__ == '__main__':
    unittest.main()
