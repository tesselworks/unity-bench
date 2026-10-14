"""End-to-end runner tests with a fake Editor (tests/fake_unity.py): no Unity needed."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from unity_bench import cli, runner, summary, workspace  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import testenv  # noqa: E402,F401  (first: keeps the cache directory out of the user's real one)

FAKE = str(Path(__file__).with_name('fake_unity.py'))


def editor(*options):
    return [sys.executable, FAKE, *options]


def work_dir(out):
    """Where the run kept its project copies: the cache's work directory, recorded in campaign.json."""
    return Path(json.loads((Path(out) / 'campaign.json').read_text())['cache']['work'])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.dict(os.environ, {'UNITY_BENCH_CACHE_DIR': str(self.tmp / 'cache')})   # a test never touches the user's real cache
        patch.start()
        self.addCleanup(patch.stop)
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
        self.assertIn('ranges separate, n=2 vs 2', md)
        self.assertNotIn('no overlap', md)
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
                             [{'id': 'adapter', 'kind': 'editor.adapter', 'method': 'Some.Type.Run', 'repeats': 1, 'timeout_seconds': 2}], build_timeout_seconds=2)
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
        self.assertTrue((work_dir(out) / 'a/Packages' / workspace.PACKAGE_NAME / 'package.json').is_file())
        self.assertFalse((self.tmp / 'proj/Packages').exists())

    def test_read_result_rejects_bad_files(self):
        p = self.tmp / 'r.json'
        ok = {'name': 'x', 'phase': 'warm', 'ms': 1.5}
        bad_samples = ['', '{}', '{"schema":1,"completed":false,"error":"boom\\nmore"}', '{"schema":1,"completed":true,"samples":[]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","phase":"hot","ms":1}]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","phase":"warm","ms":-1}]}',
                       '{"schema":1,"completed":true,"samples":[null]}', '{"schema":1,"completed":true,"samples":["x"]}', '{"schema":1,"completed":true,"samples":{"a":1}}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":NaN}]}', '{"schema":1,"completed":true,"samples":[{"name":"x","ms":Infinity}]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1,"counters":{"n":null}}]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1,"counters":{"n":NaN}}]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1,"counters":[1]}]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1,"hash":5}]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1,"workGroup":"nope"}]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1}],"workGroups":[1]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1}],"headline":"x"}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1}],"environment":"x"}',
                       '{"schema":1,"completed":true,"samples":[],"distributions":[{"name":"f","phase":"p","values":[1],"counters":{"n":NaN}}]}',
                       '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1,"info":{"v":NaN}}]}', '{"schema":1,"completed":true,"samples":[{"name":"x","ms":1}],"notes":{"v":Infinity}}']
        for bad in bad_samples:
            p.write_text(bad)
            with self.assertRaises(runner.ResultError, msg=bad):
                runner.read_result(p)
        p.write_text(json.dumps({'schema': 1, 'completed': True, 'samples': [ok]}))
        self.assertEqual(runner.read_result(p)['samples'][0]['ms'], 1.5)

    def test_phases_are_tagged_by_the_adapter_or_derived_from_how_often_a_name_is_measured(self):
        p = self.tmp / 'r.json'
        def sample(name, phase=None):
            return {'name': name, 'ms': 1, **({'phase': phase} if phase else {})}
        p.write_text(json.dumps({'schema': 1, 'completed': True, 'samples': [
            sample('many'), sample('once'), sample('many'), sample('many'), sample('tagged', 'warm'), sample('s', 'steady'), sample('w', 'warmup'), sample('f', 'first-use')]}))
        phases = [(s['name'], s['phase']) for s in runner.read_result(p)['samples']]
        self.assertEqual(phases, [('many', 'first-use'), ('once', 'single'), ('many', 'warm'), ('many', 'warm'), ('tagged', 'warm'), ('s', 'steady'), ('w', 'warmup'), ('f', 'first-use')])

    def test_work_groups_are_inherited_by_samples_and_distributions_that_report_nothing(self):
        p = self.tmp / 'r.json'
        p.write_text(json.dumps({'schema': 1, 'completed': True, 'workGroups': {'run': {'hash': 'h', 'counters': {'fractures': 100}}},
                                 'samples': [{'name': 'plan', 'ms': 1, 'workGroup': 'run'}, {'name': 'own', 'ms': 1, 'workGroup': 'run', 'hash': 'mine'}, {'name': 'none', 'ms': 1}],
                                 'distributions': [{'name': 'frame', 'phase': 'steady', 'values': [1, 2, 3], 'workGroup': 'run'}]}))
        by = {s['name']: s for s in runner.read_result(p)['samples']}
        self.assertEqual((by['plan']['hash'], by['plan']['counters'], by['plan']['work_group']), ('h', {'fractures': 100}, 'run'))
        self.assertEqual((by['own']['hash'], by['own']['counters']), ('mine', {}))               # an explicit identity wins
        self.assertIsNone(by['none']['hash'])
        self.assertEqual((by['frame steady p95']['hash'], by['frame steady p95']['phase']), ('h', 'steady'))

    def test_malformed_results_fail_that_process_and_the_schedule_continues(self):
        for kind in ('null-sample', 'str-sample', 'nan-counter', 'bad-phase'):
            with self.subTest(kind=kind):
                path = self.campaign([{'id': 'ok', 'editor': editor()}, {'id': 'bad', 'editor': editor('--bad-result', kind)}, {'id': 'after', 'editor': editor()}],
                                     [{'id': 'adapter', 'kind': 'editor.adapter', 'method': 'Some.Type.Run', 'repeats': 2}])
                _, rows = self.run_campaign(path, name='out-' + kind)
                status = [(r['variant'], r['status']) for r in rows if r['type'] == 'process']
                self.assertEqual(status.count(('bad', 'failed')), 2)
                self.assertEqual(status.count(('after', 'ok')), 2)                                # processes after the bad one still ran
                bad = next(r for r in rows if r['type'] == 'process' and r['variant'] == 'bad')
                self.assertIn('ResultError', bad['error'])

    def test_run_exits_with_a_distinct_code_when_something_failed_but_still_runs_everything(self):
        def main(*args):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return cli.main(list(args))
        good = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'w', 'kind': 'editor.adapter', 'method': 'Some.Type.Run', 'repeats': 1}])
        out = self.tmp / 'good'
        self.assertEqual(main('run', str(good), '--out', str(out)), 0)
        self.assertEqual(runner.problems(out), [])
        bad = self.campaign([{'id': 'a', 'editor': editor()}, {'id': 'b', 'editor': editor('--fail')}],
                            [{'id': 'w', 'kind': 'editor.adapter', 'method': 'Some.Type.Run', 'repeats': 2}])
        out = self.tmp / 'bad'
        self.assertEqual(main('run', str(bad), '--out', str(out)), cli.INCOMPLETE)
        self.assertEqual(sum(1 for r in summary.read_rows(out / 'results.jsonl') if r['type'] == 'process'), 4)   # everything was attempted
        lines = runner.problems(out)
        self.assertEqual(len(lines), 3)                                                                            # two failed processes, and b has no samples at all
        self.assertTrue(any('b: no samples' in x or 'b no samples' in x for x in lines))
        self.assertEqual(main('run', str(self.tmp / 'missing.json')), 2)

    def test_provenance_records_commits_framework_and_package_handling(self):
        repo = self.tmp / 'repo'
        (repo / 'unity/Assets').mkdir(parents=True)
        (repo / 'unity/Packages' / workspace.PACKAGE_NAME).mkdir(parents=True)
        (repo / 'unity/Packages' / workspace.PACKAGE_NAME / 'stale.txt').write_text('an older embedded copy\n')
        (repo / 'unity/Assets/A.cs').write_text('class A {}\n')
        git = lambda *a: subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', *a], check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
        git('init', '-q')
        git('add', '-A')
        git('commit', '-q', '-m', 'x')
        head = git('rev-parse', 'HEAD')
        cfg = {'name': 'fake', 'project': {'path': 'repo/unity'}, 'defaults': {'repeats': 1, 'rounds': 1},
               'variants': [{'id': 'wt', 'editor': editor(), 'revision': 'HEAD'}, {'id': 'copy', 'editor': editor()}],
               'workloads': [{'id': 'w', 'kind': 'editor.warm_open'}]}
        path = self.tmp / 'c.json'
        path.write_text(json.dumps(cfg))
        (repo / 'unity/Assets/Dirty.cs').write_text('class Dirty {}\n')                 # uncommitted: only the plain copy sees it
        out, rows = self.run_campaign(path, keep_work=True)
        record = json.loads((out / 'campaign.json').read_text())
        wt, copy = record['provenance']['variants']['wt'], record['provenance']['variants']['copy']
        self.assertEqual((wt['revision'], wt['commit'], wt['mode'], wt['dirty_files']), ('HEAD', head, 'worktree', 0))     # HEAD resolved to a sha
        self.assertEqual((copy['commit'], copy['mode'], copy['dirty_files']), (head, 'copy', 1))
        self.assertEqual(wt['package'], "replaced the project's embedded copy")
        framework = record['provenance']['framework']
        self.assertEqual(framework['version'], '0.4.0')
        self.assertTrue(framework['commit'] is None or isinstance(framework['commit'], str))
        for variant in ('wt', 'copy'):
            package = work_dir(out) / variant / ('unity/' if variant == 'wt' else '') / 'Packages' / workspace.PACKAGE_NAME
            self.assertTrue((package / 'package.json').is_file())
            self.assertFalse((package / 'stale.txt').exists())                         # the embedded copy was replaced, not merged or an error
        md = (out / 'summary.md').read_text()
        self.assertIn(head[:12], md)
        self.assertIn('uncommitted', md)
        subprocess.run(['git', '-C', str(repo), 'worktree', 'prune'], capture_output=True)

    def test_copies_are_removed_and_a_directory_kept_by_a_survivor_does_not_break_the_next_run(self):
        campaign = {'project': {'path': str(self.tmp / 'proj'), 'repository': None, 'inject_package': False}}
        work = self.tmp / 'w'
        project, worktree, _ = workspace.create(campaign, {'id': 'a'}, work)
        self.assertIsNone(workspace.remove(project, worktree))
        self.assertFalse((work / 'a').exists())
        (work / 'a/Deep/Er').mkdir(parents=True)                                      # an empty tree left by a process that held it open
        project, worktree, _ = workspace.create(campaign, {'id': 'a'}, work)
        self.assertTrue((project / 'Assets/A.cs').is_file())
        with self.assertRaisesRegex(FileExistsError, 'left behind by an earlier run'):
            workspace.create(campaign, {'id': 'a'}, work)                              # real files are never deleted for the user
        self.assertIsNone(workspace.remove(project, worktree))

    def test_a_plain_copy_warns_about_file_dependencies_outside_the_project(self):
        (self.tmp / 'proj/Packages').mkdir()
        (self.tmp / 'proj/Packages/manifest.json').write_text(json.dumps({'dependencies': {'com.x.inside': 'file:Local', 'com.x.outside': 'file:../../shared/pkg', 'com.x.reg': '1.0.0'}}))
        self.assertEqual(workspace.outside_references(self.tmp / 'proj'), ['com.x.outside -> file:../../shared/pkg'])

    def test_report_names_the_actual_framework_commit_and_options(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}, {'id': 'b', 'editor': editor('--scale', '2')}], [{'id': 'adapter', 'kind': 'editor.adapter', 'method': 'Some.Type.Run'}])
        out, _ = self.run_campaign(path)
        from unity_bench.report import write_reports
        s = write_reports(out, include_differing=True, bootstrap=50, first_use_ratios=True)
        self.assertEqual(s['options'], {'exclude_first_use': True, 'include_differing': True, 'bootstrap': 50, 'resolution_ms': 0.001, 'first_use_ratios': True})
        self.assertEqual(s['report']['framework']['version'], '0.4.0')
        md = (out / 'summary.md').read_text()
        self.assertIn('--include-differing --first-use-ratios --bootstrap 50', md)
        self.assertIn('95% CI (bootstrap)', md)
        self.assertIn('First-use samples as ratios', md)



if __name__ == '__main__':
    unittest.main()
