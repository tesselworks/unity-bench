"""End-to-end runner tests with a fake Editor (tests/fake_unity.py): no Unity needed."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from unity_bench import runner, summary, workspace  # noqa: E402

FAKE = str(Path(__file__).with_name('fake_unity.py'))


def editor(*options):
    return [sys.executable, FAKE, *options]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / 'proj/Assets').mkdir(parents=True)
        (self.tmp / 'proj/Assets/A.cs').write_text('class A {}\n')
        (self.tmp / 'proj/ProjectSettings').mkdir()
        (self.tmp / 'proj/ProjectSettings/EditorSettings.asset').write_text('original\n')

    def campaign(self, variants, workloads, **defaults):
        cfg = {'name': 'fake', 'project': {'path': 'proj'}, 'defaults': {'repeats': 2, 'rounds': 3, **defaults}, 'variants': variants, 'workloads': workloads}
        path = self.tmp / 'c.json'
        path.write_text(json.dumps(cfg))
        return path

    def run_campaign(self, path, name='out', **kwargs):
        out = runner.run_campaign(path, self.tmp / name, log=lambda *_: None, **kwargs)
        return out, summary.read_rows(out / 'results.jsonl')

    def test_full_flow_ratio_identity_first_use_and_reload_attribution(self):
        path = self.campaign(
            [{'id': 'a', 'editor': editor()}, {'id': 'b', 'editor': editor('--scale', '0.5', '--salt', 'x', '--salt-item', 'beta')}],
            [{'id': 'cold', 'kind': 'editor.cold_open', 'repeats': 3},
             {'id': 'adapter', 'kind': 'editor.adapter', 'method': 'Some.Type.Run'},
             {'id': 'edit', 'kind': 'editor.script_edit', 'targets': [{'id': 'leaf', 'path': 'Assets/A.cs'}]}])
        out, rows = self.run_campaign(path)
        self.assertTrue(all(r['status'] == 'ok' for r in rows if r['type'] == 'process'))
        self.assertEqual(sum(1 for r in rows if r['type'] == 'process'), 6 + 4 + 4)
        # Raw data keeps first-use samples; the summary excludes them by default.
        self.assertTrue(any(r['type'] == 'sample' and r['phase'] == 'first-use' and r['name'] == 'alpha' for r in rows))
        s = json.loads((out / 'summary.json').read_text())
        by = {w['workload']: w for w in s['workloads']}
        alpha = next(i for i in by['adapter']['items'] if i['name'] == 'alpha')
        self.assertAlmostEqual(alpha['ratios']['b']['ratio'], 0.5)
        self.assertEqual(alpha['variants']['a']['median'], 10)               # the 30 ms first-use sample is not in it
        self.assertEqual([i['name'] for i in by['adapter']['differing']], ['beta'])    # different hash in b: shown apart, not blocking
        self.assertEqual(by['cold']['items'][0]['variants']['a']['n'], 2)    # three processes, first one is first-use
        leaf = by['edit']['reload'][0]
        self.assertEqual(leaf['step'], 'leaf')
        self.assertEqual(leaf['cells']['a']['total']['median'], 1000)
        md = (out / 'summary.md').read_text()
        self.assertIn('did different work', md)
        self.assertIn('no overlap', md)
        self.assertFalse((out / 'work').exists() and any((out / 'work').iterdir()))   # project copies removed
        self.assertEqual((self.tmp / 'proj/Assets/A.cs').read_text(), 'class A {}\n')  # the original project is never touched

    def test_report_can_be_regenerated_with_other_options(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}, {'id': 'b', 'editor': editor('--scale', '2')}],
                             [{'id': 'adapter', 'kind': 'editor.adapter', 'method': 'Some.Type.Run'}])
        out, _ = self.run_campaign(path)
        from unity_bench.report import write_reports
        s = write_reports(out, exclude_first_use=False)
        alpha = s['workloads'][0]['items'][0]
        self.assertEqual(alpha['variants']['a']['n'], 2)
        self.assertEqual(alpha['variants']['a']['median'], 10)    # per process: median(30, 10, 10)

    def test_failures_and_timeouts_are_recorded_and_the_schedule_continues(self):
        path = self.campaign([{'id': 'ok', 'editor': editor()}, {'id': 'bad', 'editor': editor('--fail')}, {'id': 'slow', 'editor': editor('--hang')}],
                             [{'id': 'adapter', 'kind': 'editor.adapter', 'method': 'Some.Type.Run', 'repeats': 1, 'timeout_seconds': 2}])
        out, rows = self.run_campaign(path)
        status = {r['variant']: (r['status'], r['error']) for r in rows if r['type'] == 'process'}
        self.assertEqual(status['ok'][0], 'ok')
        self.assertEqual(status['bad'][0], 'failed')
        self.assertIn('exit code 1', status['bad'][1])
        self.assertEqual(status['slow'][0], 'timeout')
        self.assertIn('Failed processes', (out / 'summary.md').read_text())

    def test_edited_files_are_restored_even_if_the_editor_leaves_them_changed(self):
        # Reaching into the copy: a fake edit left behind by a process would be put back by the runner.
        from unity_bench.workspace import restore, snapshot
        project = self.tmp / 'proj'
        saved = snapshot(project, ['Assets/A.cs', 'Missing.cs'])
        self.assertEqual(list(saved), ['Assets/A.cs'])
        (project / 'Assets/A.cs').write_text('changed')
        self.assertEqual(restore(project, saved), ['Assets/A.cs'])
        self.assertEqual((project / 'Assets/A.cs').read_text(), 'class A {}\n')
        self.assertEqual(restore(project, saved), [])

    def test_existing_output_directory_is_never_overwritten(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 1}])
        self.run_campaign(path)
        with self.assertRaises(FileExistsError):
            self.run_campaign(path)

    def test_package_is_injected_into_the_copy_not_the_source(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 1}])
        out, _ = self.run_campaign(path, keep_work=True)
        self.assertTrue((out / 'work/a/Packages' / workspace.PACKAGE_NAME / 'package.json').is_file())
        self.assertFalse((self.tmp / 'proj/Packages').exists())

    def test_read_result_rejects_bad_files(self):
        p = self.tmp / 'r.json'
        for bad in ('', '{}', '{"schema":1,"completed":false,"error":"boom\\nmore"}', '{"schema":1,"completed":true,"samples":[]}',
                    '{"schema":1,"completed":true,"samples":[{"name":"x","phase":"hot","ms":1}]}',
                    '{"schema":1,"completed":true,"samples":[{"name":"x","phase":"warm","ms":-1}]}'):
            p.write_text(bad)
            with self.assertRaises(runner.ResultError):
                runner.read_result(p)
        p.write_text('{"schema":1,"completed":true,"samples":[{"name":"x","phase":"warm","ms":1.5}]}')
        self.assertEqual(runner.read_result(p)['samples'][0]['ms'], 1.5)


if __name__ == '__main__':
    unittest.main()
