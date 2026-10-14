"""Memory breakdown and leak workloads (v0.6): config, slope math, summaries, report text, missing counters and runner-side memory, with a fake Editor."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from unity_bench import config, host_sampling, report, runner, stats, summary  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import testenv  # noqa: E402,F401  (first: keeps the cache directory out of the user's real one)

FAKE = str(Path(__file__).with_name('fake_unity.py'))
MB = 2**20


def editor(*options):
    return [sys.executable, FAKE, *options]


def raw(*workloads):
    return {'name': 'c', 'project': {'path': 'p'}, 'variants': [{'id': 'a', 'editor': '/u/a'}], 'workloads': list(workloads)}


LEAF = {'id': 'leaf', 'path': 'Assets/A.cs'}


class ConfigTests(unittest.TestCase):
    def test_leak_workloads_default_to_30_and_10_cycles_whatever_the_campaign_default_is(self):
        raw_cfg = raw({'id': 'r', 'kind': 'editor.reload_leak', 'targets': [LEAF]}, {'id': 'p', 'kind': 'editor.play_leak'})
        raw_cfg['defaults'] = {'rounds': 4}
        w = {x['id']: x for x in config.validate(raw_cfg, '/base')['workloads']}
        self.assertEqual((w['r']['rounds'], w['p']['rounds']), (30, 10))
        self.assertEqual((w['r']['memory'], w['r']['force_gc'], w['r']['memory_snapshot']), (True, True, False))
        self.assertEqual((w['p']['domain_reload'], w['p']['state']), ('default', 'as-is'))
        explicit = config.validate(raw({'id': 'r', 'kind': 'editor.reload_leak', 'rounds': 12, 'targets': [LEAF], 'force_gc': False, 'memory_snapshot': True}), '/base')['workloads'][0]
        self.assertEqual((explicit['rounds'], explicit['force_gc'], explicit['memory_snapshot']), (12, False, True))

    def test_leak_workloads_are_validated(self):
        for workload, message in (({'id': 'r', 'kind': 'editor.reload_leak'}, 'targets must be a non-empty list'),
                                  ({'id': 'r', 'kind': 'editor.reload_leak', 'targets': [LEAF], 'memory': False}, 'memory = false'),
                                  ({'id': 'r', 'kind': 'editor.reload_leak', 'targets': [LEAF], 'rounds': 2}, 'at least 3'),
                                  ({'id': 'r', 'kind': 'editor.reload_leak', 'targets': [LEAF], 'force_gc': 'yes'}, 'force_gc must be a boolean'),
                                  ({'id': 'p', 'kind': 'editor.play_leak', 'domain_reload': 'sometimes'}, 'domain_reload')):
            with self.assertRaisesRegex(config.ConfigError, message):
                config.validate(raw(workload), '/base')

    def test_memory_key_is_for_the_kinds_that_read_memory(self):
        self.assertFalse(config.validate(raw({'id': 'e', 'kind': 'editor.script_edit', 'targets': [LEAF], 'memory': False}), '/base')['workloads'][0]['memory'])
        self.assertTrue(config.validate(raw({'id': 'e', 'kind': 'editor.play'}), '/base')['workloads'][0]['memory'])
        for kind in ('editor.cold_open', 'editor.warm_open', 'player.startup'):
            with self.assertRaisesRegex(config.ConfigError, 'unknown key'):
                config.validate(raw({'id': 'e', 'kind': kind, 'memory': False}), '/base')
        with self.assertRaisesRegex(config.ConfigError, 'memory must be a boolean'):
            config.validate(raw({'id': 'e', 'kind': 'editor.play', 'memory': 'no'}), '/base')


class FitTests(unittest.TestCase):
    def test_exact_line_has_no_residual(self):
        fit = stats.linear_fit([0, 1, 2, 3], [10.0, 12.0, 14.0, 16.0])
        self.assertAlmostEqual(fit['slope'], 2.0)
        self.assertAlmostEqual(fit['intercept'], 10.0)
        self.assertAlmostEqual(fit['residual_min'], 0.0)
        self.assertAlmostEqual(fit['residual_max'], 0.0)

    def test_residual_range_and_flat_series(self):
        fit = stats.linear_fit([0, 1, 2], [0.0, 3.0, 0.0])   # symmetric hump: slope 0, residuals -1, +2, -1
        self.assertAlmostEqual(fit['slope'], 0.0)
        self.assertAlmostEqual((fit['residual_min'], fit['residual_max']), (-1.0, 2.0))
        flat = stats.linear_fit([0, 1, 2, 3], [5, 5, 5, 5])
        self.assertEqual((flat['slope'], flat['residual_min'], flat['residual_max']), (0.0, 0.0, 0.0))

    def test_least_squares_not_endpoints(self):
        # Endpoints say (9 - 0) / 3 = 3 per step; the least-squares line through all four points is 2.8.
        fit = stats.linear_fit([0, 1, 2, 3], [0.0, 4.0, 5.0, 9.0])
        self.assertAlmostEqual(fit['slope'], 2.8)

    def test_too_few_or_degenerate_points(self):
        self.assertIsNone(stats.linear_fit([0], [1]))
        self.assertIsNone(stats.linear_fit([2, 2, 2], [1, 2, 3]))
        self.assertIsNone(stats.linear_fit([], []))


def process(index, variant, workload='w', kind='editor.reload_leak', notes=None):
    return {'type': 'process', 'index': index, 'workload': workload, 'kind': kind, 'variant': variant, 'repeat': 0, 'status': 'ok', 'error': None,
            'host': {'available': False}, 'notes': notes or {}}


def reading(index, variant, name, value, unit='bytes', round_=0, workload='w'):
    return {'type': 'sample', 'index': index, 'workload': workload, 'variant': variant, 'name': name, 'ms': value, 'phase': 'single', 'round': round_,
            'info': {}, 'counters': None, 'hash': None, 'unit': unit}


def leak_rows(variant_slopes, cycles=10, processes=2):
    """Processes per variant whose 'GC Used Memory' grows by the variant's slope (MB per cycle) from 10 MB, plus a flat object count."""
    rows, index = [], 0
    for variant, slope in variant_slopes.items():
        for _ in range(processes):
            rows.append(process(index, variant))
            for c in range(cycles):
                rows.append(reading(index, variant, 'leak/GC Used Memory', 10 * MB + slope * MB * c, round_=c))
                rows.append(reading(index, variant, 'leak/Object Count', 900, unit='count', round_=c))
            rows.append(reading(index, variant, 'compile', 100.0, unit='ms'))
            index += 1
    return rows


class SummaryTests(unittest.TestCase):
    def test_leak_series_are_fitted_per_process_and_kept_out_of_the_items(self):
        w = summary.summarise(leak_rows({'mono': 0.5, 'core': 0.0}))['workloads'][0]
        self.assertEqual([i['name'] for i in w['items']], ['compile'])   # a series is not an item with a median over its rounds
        rows = {r['name']: r for r in w['leak']}
        mono, core = rows['GC Used Memory']['cells']['mono'], rows['GC Used Memory']['cells']['core']
        self.assertAlmostEqual(mono['slope'] / MB, 0.5)
        self.assertAlmostEqual(mono['tail_slope'] / MB, 0.5)
        self.assertAlmostEqual(mono['first'] / MB, 10.0)
        self.assertAlmostEqual(mono['last'] / MB, 14.5)
        self.assertEqual((mono['points'], mono['processes']), (10, 2))
        self.assertAlmostEqual(core['slope'], 0.0)
        self.assertAlmostEqual(mono['residual'][0], 0.0, places=3)
        self.assertEqual(rows['Object Count']['unit'], 'count')
        self.assertEqual(rows['Object Count']['cells']['mono']['slope'], 0.0)

    def test_second_half_slope_leaves_out_start_up_growth(self):
        rows = [process(0, 'a')]
        values = [100, 160, 200, 200, 200, 200, 200, 200]   # grows early, then settles
        rows += [reading(0, 'a', 'leak/GC Used Memory', v * MB, round_=c) for c, v in enumerate(values)]
        cell = summary.summarise(rows)['workloads'][0]['leak'][0]['cells']['a']
        self.assertGreater(cell['slope'] / MB, 5)
        self.assertAlmostEqual(cell['tail_slope'], 0.0)

    def test_memory_readings_are_annotations_unless_the_campaign_marks_them_headline(self):
        rows = []
        for i, v in enumerate(['a', 'a', 'b', 'b']):
            rows += [process(i, v, kind='editor.script_edit'), reading(i, v, 'mem/end/GC Used Memory', (10 if v == 'a' else 20) * MB), reading(i, v, 'compile', 100.0, unit='ms')]
        plain = summary.summarise(rows, baseline='a')['workloads'][0]
        self.assertEqual([i['name'] for i in plain['items']], ['compile'])
        self.assertEqual([i['name'] for i in plain['memory']], ['mem/end/GC Used Memory'])
        self.assertAlmostEqual(plain['memory'][0]['ratios']['b']['ratio'], 2.0)
        self.assertEqual(plain['observed']['samples_per_item'], [1, 1])   # a reading is not a round of the workload
        self.assertEqual(plain['aggregate'], {})
        marked = summary.summarise(rows, baseline='a', headline={'w': ['mem/end/*']})['workloads'][0]
        self.assertEqual(sorted(i['name'] for i in marked['items']), ['compile', 'mem/end/GC Used Memory'])
        self.assertEqual(marked['memory'], [])

    def test_memory_ratio_ignores_work_identity(self):
        rows = []
        for i, v in enumerate(['a', 'b']):
            rows += [process(i, v, kind='editor.scene_play'), {**reading(i, v, 'mem/end/GC Used Memory', (10 if v == 'a' else 30) * MB), 'counters': {'bodies': 5 if v == 'a' else 6}}]
        item = summary.summarise(rows, baseline='a')['workloads'][0]['memory'][0]
        self.assertAlmostEqual(item['ratios']['b']['ratio'], 3.0)

    def test_missing_counters_are_listed_per_variant(self):
        rows = [process(0, 'mono', notes={'memory.offered': 'GC Used Memory (bytes), Object Count (count)', 'memory.missing': '.NET Heap Size, .NET GC Pause Total'}),
                process(1, 'mono', notes={'memory.offered': 'GC Used Memory (bytes), Object Count (count)', 'memory.missing': '.NET Heap Size'}),
                process(2, 'core', notes={'memory.offered': 'GC Used Memory (bytes), Object Count (count)', 'memory.missing': ''}),
                reading(0, 'mono', 'compile', 1.0, unit='ms'), reading(1, 'mono', 'compile', 1.0, unit='ms'), reading(2, 'core', 'compile', 1.0, unit='ms')]
        counters = summary.summarise(rows)['workloads'][0]['memory_counters']
        self.assertEqual(counters['mono']['missing'], ['.NET GC Pause Total', '.NET Heap Size'])
        self.assertEqual(counters['core']['missing'], [])
        self.assertEqual(counters['core']['offered'], ['GC Used Memory (bytes)', 'Object Count (count)'])

    def test_snapshots_and_the_reason_for_none_are_listed(self):
        rows = [process(0, 'a', notes={'memory.snapshot': '/run/snapshots/0000-w-a.snap'}), process(1, 'b', notes={'memory.snapshot': 'skipped: com.unity.memoryprofiler is not in the project'}),
                reading(0, 'a', 'leak/GC Used Memory', 1.0, round_=0), reading(0, 'a', 'leak/GC Used Memory', 2.0, round_=1)]
        w = summary.summarise(rows)['workloads'][0]
        self.assertEqual([(s['variant'], s['result']) for s in w['snapshots']], [('a', '/run/snapshots/0000-w-a.snap'), ('b', 'skipped: com.unity.memoryprofiler is not in the project')])
        text = '\n'.join(report.render_leak(w))
        self.assertTrue('`a` process #0: `snapshots/0000-w-a.snap`' in text and '`b` process #1: skipped: com.unity.memoryprofiler is not in the project' in text, text)


class HostMemoryTests(unittest.TestCase):
    def test_runner_side_peaks_become_byte_samples_only_when_they_were_sampled(self):
        written = []
        campaign = runner.Campaign.__new__(runner.Campaign)
        campaign.write = written.append
        campaign.write_process_memory({'index': 3, 'workload': 'w'}, {'available': True, 'root_rss_max_mb': 400.0, 'tree_rss_max_mb': 2000.5, 'tree_private_max_mb': 1500.0})
        self.assertEqual([(r['name'], r['ms'] / MB, r['unit']) for r in written],
                         [('mem/peak/Process RSS (runner)', 400.0, 'bytes'), ('mem/peak/Process tree RSS (runner)', 2000.5, 'bytes'), ('mem/peak/Process tree Private (runner)', 1500.0, 'bytes')])
        written.clear()
        campaign.write_process_memory({'index': 3, 'workload': 'w'}, {'available': True, 'root_rss_max_mb': 400.0})   # private bytes the OS would not give: no row
        campaign.write_process_memory({'index': 3, 'workload': 'w'}, {'available': False})
        self.assertEqual([r['name'] for r in written], ['mem/peak/Process RSS (runner)'])

    def test_sampler_keeps_the_editor_apart_from_its_children_and_asks_for_private_memory_once(self):
        class Memory:
            def __init__(self, rss): self.rss = rss

        class Process:
            def __init__(self, pid, rss, children=(), uss=None):
                self.pid, self.rss, self._children, self.uss = pid, rss, children, uss
            def children(self, recursive=True): return list(self._children)
            def cpu_times(self): return mock.Mock(user=1.0, system=0.0)
            def memory_info(self): return Memory(self.rss)
            def memory_full_info(self):
                if self.uss is None:
                    raise PermissionError('denied')
                return mock.Mock(uss=self.uss)

        class Error(Exception):
            pass

        root = Process(10, 400 * MB, [Process(11, 600 * MB, uss=100 * MB)], uss=300 * MB)
        fake = mock.Mock(Error=Error, NoSuchProcess=type('NoSuchProcess', (Error,), {}), __version__='x')
        fake.Process.return_value = root
        fake.cpu_percent.return_value = 10.0
        fake.virtual_memory.return_value = mock.Mock(percent=50.0)
        with mock.patch.object(host_sampling, 'psutil', fake), mock.patch.object(host_sampling, 'WINDOWS', False):
            sampler = host_sampling.Sampler()
            sampler._last -= 5
            sampler.sample(10)
            summary_ = sampler.summary()
        self.assertEqual((summary_['root_rss_max_mb'], summary_['tree_rss_max_mb']), (400.0, 1000.0))
        self.assertEqual(summary_['tree_private_max_mb'], 400.0)   # 300 + 100 MB of unique set size

        denied = Process(20, 100 * MB, [Process(21, 50 * MB)])   # the child's unique set size is denied: the tree total is not reported, and not asked for again
        fake.Process.return_value = denied
        with mock.patch.object(host_sampling, 'psutil', fake), mock.patch.object(host_sampling, 'WINDOWS', False):
            sampler = host_sampling.Sampler()
            sampler._last -= 5
            sampler.sample(20)
            self.assertFalse(sampler._private_ok)
            self.assertNotIn('tree_private_max_mb', sampler.summary())


class FakeEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {'UNITY_BENCH_CACHE_DIR': str(self.tmp / 'cache')})
        patch.start()
        self.addCleanup(patch.stop)
        (self.tmp / 'proj/Assets').mkdir(parents=True)
        (self.tmp / 'proj/Assets/A.cs').write_text('class A {}\n')
        (self.tmp / 'proj/ProjectSettings').mkdir()
        (self.tmp / 'proj/ProjectSettings/EditorSettings.asset').write_text('original\n')

    def run_campaign(self, variants, workloads):
        cfg = {'name': 'fake', 'project': {'path': 'proj'}, 'defaults': {'repeats': 2, 'rounds': 3, 'baseline': variants[0]['id']}, 'variants': variants, 'workloads': workloads}
        path = self.tmp / 'c.json'
        path.write_text(json.dumps(cfg))
        out = runner.run_campaign(path, self.tmp / 'out', log=lambda *_: None)
        return out, summary.read_rows(out / 'results.jsonl')

    def test_leak_workloads_report_the_slope_per_variant_and_list_what_an_editor_did_not_offer(self):
        out, rows = self.run_campaign(
            [{'id': 'mono', 'editor': editor('--leak-mb', '0.5', '--mem-missing', '.NET Heap Size,.NET Committed')}, {'id': 'core', 'editor': editor('--leak-mb', '0')}],
            [{'id': 'reload', 'kind': 'editor.reload_leak', 'rounds': 8, 'targets': [LEAF]}, {'id': 'play', 'kind': 'editor.play_leak', 'rounds': 5}, {'id': 'edit', 'kind': 'editor.script_edit', 'targets': [LEAF]}])
        self.assertEqual([r['error'] for r in rows if r['type'] == 'process' and r['status'] != 'ok'], [])
        request = json.loads(next((out / 'requests').glob('*reload-mono.json')).read_text())
        self.assertEqual((request['kind'], request['rounds'], request['memory'], request['forceGc'], request['memorySnapshot'], request['snapshotPath']), ('editor.reload_leak', 8, True, True, False, ''))
        self.assertEqual(json.loads(next((out / 'requests').glob('*play-core.json')).read_text())['rounds'], 5)
        s = json.loads((out / 'summary.json').read_text())
        by = {w['workload']: w for w in s['workloads']}
        series = {r['name']: r for r in by['reload']['leak']}
        self.assertAlmostEqual(series['GC Used Memory']['cells']['mono']['slope'] / MB, 0.5)
        self.assertAlmostEqual(series['Unity Native (Total Used - GC Used)']['cells']['mono']['slope'] / MB, 1.0)
        self.assertAlmostEqual(series['GC Used Memory']['cells']['core']['slope'], 0.0)
        self.assertEqual(series['GC Used Memory']['cells']['mono']['points'], 8)
        self.assertEqual({r['name']: r for r in by['play']['leak']}['GC Used Memory']['cells']['mono']['points'], 5)
        self.assertEqual(by['reload']['memory_counters']['mono']['missing'], ['.NET Committed', '.NET Heap Size'])
        self.assertEqual(by['reload']['memory_counters']['core']['missing'], [])
        # the ordinary workload also has the breakdown, as annotations
        self.assertIn('mem/end/GC Used Memory', [i['name'] for i in by['edit']['memory']])
        self.assertNotIn('mem/end/GC Used Memory', [i['name'] for i in by['edit']['items']])
        md = (out / 'summary.md').read_text()
        for text in ('Memory breakdown of `edit`', '| managed heap | GC Used Memory | end |', 'Memory after each reload or Play Mode cycle of `reload`',
                     '| managed heap | GC Used Memory | mono | 12.5 MB | 16.0 MB | +0.500 MB [+0.500 MB to +0.500 MB] |', '| mono | .NET Committed, .NET Heap Size |'):
            self.assertTrue(text in md, f'not in summary.md: {text}')   # not assertIn: a failure would print the whole report
        self.assertEqual(sorted(w['workload'] for w in s['workloads'] if w['reload']), ['edit', 'play', 'reload'])   # reload blocks are attributed for the leak kinds too

    def test_memory_false_and_an_editor_without_the_package_code_leave_no_readings(self):
        out, rows = self.run_campaign([{'id': 'a', 'editor': editor()}, {'id': 'b', 'editor': editor('--no-memory')}],
                                      [{'id': 'edit', 'kind': 'editor.script_edit', 'memory': False, 'targets': [LEAF]}])
        self.assertFalse([r for r in rows if r['type'] == 'sample' and r['name'].startswith('mem/')])
        self.assertFalse(json.loads(next((out / 'requests').glob('*edit-a.json')).read_text())['memory'])
        md = (out / 'summary.md').read_text()
        self.assertFalse('Memory breakdown' in md)


if __name__ == '__main__':
    unittest.main()
