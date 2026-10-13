"""player.adapter, build sharing, distributions, project.prepare, baselines per pair and worktree targets, with a fake Editor and player."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from unity_bench import config, distributions, runner, summary  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import fake_launcher  # noqa: E402

FAKE = str(Path(__file__).with_name('fake_unity.py'))


def failures(rows):
    """One line per process that did not succeed, with its error, so a failed assertion says why."""
    return [f"{r['workload']} {r['variant']} #{r['repeat']}: {r['status']}: {r['error']}" for r in rows if r['type'] == 'process' and r['status'] != 'ok']


def editor(*options):
    return [sys.executable, FAKE, *options]


def setting(rows, role=None):
    return [r for r in rows if r['type'] == 'process' and r.get('role') == role]


class DistributionTests(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        values = list(range(1, 101))
        self.assertEqual([distributions.percentile(values, f) for _, f in distributions.STATS], [50, 95, 99, 100])
        self.assertEqual(distributions.percentile([7], .99), 7)
        self.assertEqual(distributions.percentile([1, 2], .50), 1)

    def test_a_distribution_becomes_four_samples_that_carry_its_identity(self):
        out = distributions.to_samples([{'name': 'frame', 'phase': 'steady', 'values': [3, 1, 2], 'hash': 'h', 'counters': {'n': 3}}])
        self.assertEqual([(s['name'], s['ms']) for s in out], [('frame steady p50', 2), ('frame steady p95', 3), ('frame steady p99', 3), ('frame steady max', 3)])
        self.assertTrue(all(s['hash'] == 'h' and s['counters'] == {'n': 3} and s['info']['frames'] == 3 for s in out))

    def test_check_rejects_unusable_entries(self):
        good = {'name': 'f', 'phase': 'p', 'values': [1.5]}
        self.assertTrue(distributions.check(good))
        for bad in ({**good, 'values': []}, {**good, 'values': [-1]}, {**good, 'values': [float('nan')]}, {**good, 'values': [True]}, {**good, 'phase': ''}, {**good, 'name': 3}, 'x'):
            self.assertFalse(distributions.check(bad))

    def test_read_result_expands_and_validates_distributions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'r.json'
            p.write_text(json.dumps({'schema': 1, 'completed': True, 'samples': [], 'distributions': [{'name': 'f', 'phase': 'p', 'values': [1, 2, 3]}]}))
            self.assertEqual([s['name'] for s in runner.read_result(p)['samples']], ['f p p50', 'f p p95', 'f p p99', 'f p max'])
            p.write_text(json.dumps({'schema': 1, 'completed': True, 'samples': [], 'distributions': [{'name': 'f', 'phase': 'p', 'values': []}]}))
            with self.assertRaises(runner.ResultError):
                runner.read_result(p)


class PlayerRunnerTests(unittest.TestCase):
    def setUp(self):
        if os.name == 'nt' and fake_launcher.launcher() is None:
            self.skipTest('no .NET Framework compiler to build a fake player .exe (the runner refuses batch files)')
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / 'proj/Assets').mkdir(parents=True)
        (self.tmp / 'proj/Assets/A.cs').write_text('class A {}\n')

    def campaign(self, variants, workloads, project=None, build=None, **defaults):
        cfg = {'name': 'fake', 'project': {'path': 'proj', **(project or {})}, 'defaults': {'repeats': 2, 'rounds': 2, **defaults}, 'variants': variants, 'workloads': workloads,
               **({'build': build} if build else {})}
        path = self.tmp / 'c.json'
        path.write_text(json.dumps(cfg))
        return path

    def run_campaign(self, path, name='out', **kwargs):
        out = runner.run_campaign(path, self.tmp / name, log=lambda *_: None, **kwargs)
        return out, summary.read_rows(out / 'results.jsonl')

    def test_adapter_reports_samples_and_distributions_per_process(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}, {'id': 'b', 'editor': editor('--scale', '2'), 'baseline': 'a'}],
                             [{'id': 'pa', 'kind': 'player.adapter', 'method': 'My.Adapters.Run', 'args': {'echo': 'hi'}, 'rounds': 3, 'repeats': 3}])
        out, rows = self.run_campaign(path)
        self.assertEqual(failures(rows), [])
        by = {i['name']: i for i in json.loads((out / 'summary.json').read_text())['workloads'][0]['items']}
        self.assertEqual(sorted(by), ['alpha', 'frame steady max', 'frame steady p50', 'frame steady p95', 'frame steady p99', 'frame warmup max', 'frame warmup p50', 'frame warmup p95', 'frame warmup p99'])
        # steady frames are 1..100 ms scaled by the variant: nearest-rank p50 50, p95 95, p99 99, max 100; the warm-up is shown apart
        self.assertEqual([by[f'frame steady {s}']['variants']['a']['median'] for s in ('p50', 'p95', 'p99', 'max')], [50, 95, 99, 100])
        self.assertAlmostEqual(by['frame steady p99']['ratios']['b']['ratio'], 2.0)
        self.assertEqual(by['frame warmup p50']['variants']['a']['median'], 30)
        self.assertEqual(by['alpha']['variants']['a']['n'], 3)
        self.assertEqual(by['alpha']['identity']['status'], 'identical')
        samples = [r for r in rows if r['type'] == 'sample' and r['name'] == 'alpha']
        self.assertEqual(sum(1 for s in samples if s['phase'] == 'first-use'), 6)   # round 0 of each of the 6 processes
        self.assertEqual(samples[0]['counters'], {'rounds': 3})
        raw = json.loads(next((out / 'results').glob('*pa-a.json')).read_text())     # the raw per-frame values stay on disk
        self.assertEqual(len(raw['distributions'][1]['values']), 100)
        self.assertEqual(raw['notes'], {'method': 'My.Adapters.Run', 'echo': 'hi'})
        self.assertIn('frame steady p95', (out / 'summary.md').read_text())

    def test_one_build_serves_several_player_workloads_but_not_other_settings(self):
        shared = {'kind': 'player.adapter', 'method': 'My.Adapters.Run', 'repeats': 2}
        path = self.campaign([{'id': 'a', 'editor': editor(), 'backend': 'il2cpp'}, {'id': 'b', 'editor': editor(), 'backend': 'coreclr'}],
                             [{'id': 'one', **shared}, {'id': 'two', **shared}, {'id': 'startup', 'kind': 'player.startup', 'repeats': 2},
                              {'id': 'other-scene', **shared, 'scenes': ['Assets/Other.unity']},
                              {'id': 'other-backend', **shared, 'backend': 'mono'},
                              {'id': 'hooked', **shared, 'prebuild': 'My.Prebuild.Run'}])
        out, rows = self.run_campaign(path, keep_work=True)
        self.assertEqual(failures(rows), [])
        builds = setting(rows, 'setup-build')
        # per variant: default settings (one, two, startup), another scene list, another backend, another prebuild hook
        self.assertEqual(len(builds), 2 * 4)
        self.assertEqual(sorted({(b['workload'], b['variant']) for b in builds if b['variant'] == 'a'}), [('hooked', 'a'), ('one', 'a'), ('other-backend', 'a'), ('other-scene', 'a')])
        notes = {b['workload']: b['notes'] for b in builds if b['variant'] == 'a'}
        self.assertEqual(notes['other-scene']['scenes'], 'Assets/Other.unity')
        self.assertEqual((notes['one']['backend'], notes['other-backend']['backend']), ('il2cpp', 'mono'))
        self.assertEqual(notes['hooked']['prebuild'], 'My.Prebuild.Run')

    def test_explicit_build_workload_is_shared_and_a_missing_one_fails_the_process(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}],
                             [{'id': 'build', 'kind': 'player.build_time', 'repeats': 1, 'rounds': 1},
                              {'id': 'pa', 'kind': 'player.adapter', 'method': 'My.A.Run', 'build': 'build', 'repeats': 2}])
        out, rows = self.run_campaign(path)
        self.assertEqual(setting(rows, 'setup-build'), [])
        self.assertEqual(failures(rows), [])
        bad = self.campaign([{'id': 'a', 'editor': editor('--fail')}],
                            [{'id': 'build', 'kind': 'player.build_time', 'repeats': 1, 'rounds': 1},
                             {'id': 'pa', 'kind': 'player.adapter', 'method': 'My.A.Run', 'build': 'build', 'repeats': 1}])
        _, rows = self.run_campaign(bad, name='out2')
        pa = next(r for r in rows if r['type'] == 'process' and r['workload'] == 'pa')
        self.assertEqual(pa['status'], 'failed')
        self.assertIn('did not produce one', pa['error'])

    def test_failed_hung_and_erroring_players_are_recorded_and_the_schedule_continues(self):
        path = self.campaign([{'id': 'ok', 'editor': editor()}, {'id': 'crash', 'editor': editor('--player-fail')},
                              {'id': 'hang', 'editor': editor('--player-hang')}, {'id': 'error', 'editor': editor('--player-error')}],
                             [{'id': 'pa', 'kind': 'player.adapter', 'method': 'My.A.Run', 'repeats': 1, 'timeout_seconds': 2}])
        out, rows = self.run_campaign(path)
        status = {r['variant']: (r['status'], r['error']) for r in rows if r['type'] == 'process' and r.get('role') is None}
        self.assertEqual(status['ok'], ('ok', None))
        self.assertEqual(status['crash'][0], 'failed')
        self.assertEqual(status['hang'][0], 'timeout')
        self.assertEqual(status['error'][0], 'failed')
        self.assertIn('adapter blew up', status['error'][1])
        self.assertNotIn('second line', status['error'][1])
        self.assertEqual(sum(1 for r in rows if r['type'] == 'sample' and r['variant'] != 'ok'), 0)

    def test_player_startup_still_works_with_the_shared_build_path(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'startup', 'kind': 'player.startup', 'repeats': 3}])
        _, rows = self.run_campaign(path)
        self.assertEqual(failures(rows), [])
        names = [r['name'] for r in rows if r['type'] == 'sample']
        for name in ('launch_to_probe', 'engine_to_probe', 'launch_to_exit'):          # named for what they measure (docs/workloads.md#playerstartup)
            self.assertEqual(names.count(name), 3)
        self.assertNotIn('first_frame', names)
        self.assertEqual(len(setting(rows, 'setup-build')), 1)
        phases = [r['phase'] for r in rows if r['type'] == 'sample' and r['name'] == 'launch_to_probe']
        self.assertEqual(phases, ['first-use', 'warm', 'warm'])                         # tagged by the workload, which knows what a repeat is

    def test_players_show_their_own_environment_build_settings_and_observed_rounds(self):
        path = self.campaign([{'id': 'a', 'editor': editor(), 'backend': 'il2cpp'}, {'id': 'b', 'editor': editor('--player-once'), 'backend': 'il2cpp', 'build': {'api_compatibility': 'NET'}}],
                             [{'id': 'pa', 'kind': 'player.adapter', 'method': 'My.A.Run', 'rounds': 3, 'repeats': 2}], build={'api_compatibility': 'NET_Standard', 'stripping': 'Minimal'})
        out, rows = self.run_campaign(path)
        self.assertEqual(failures(rows), [])
        md = (out / 'summary.md').read_text()
        self.assertIn('Players (what the built player reported, not the Editor that built it)', md)
        players = md.split('Players (what the built player reported')[1].splitlines()
        player_row = next(line for line in players if line.startswith('| a |'))
        self.assertIn('Release build', player_row)                                      # the player's build kind, not the Editor's "Debug"
        self.assertIn('FakePlayer / FakeGL', player_row)
        self.assertIn('API level NET_Standard', player_row)                             # the campaign's setting reached the Editor request...
        self.assertIn('stripping Minimal', player_row)
        self.assertIn('API level NET,', next(line for line in players if line.startswith('| b |')))   # ...per variant
        request = json.loads(next((out / 'requests').glob('*-build.json')).read_text())
        self.assertEqual((request['apiCompatibility'], request['stripping'], request['il2cppCodeGeneration']), ('NET_Standard', 'Minimal', ''))
        s = json.loads((out / 'summary.json').read_text())
        w = s['workloads'][0]
        self.assertEqual(w['observed']['samples_per_item'], [1, 3])                       # a ran 3 rounds, b's player ignores rounds
        self.assertIn("the campaign's rounds = 3 is not what ran", md)
        self.assertEqual(s['environments']['a']['editor'][0]['environment']['codeOptimization'], 'Debug')
        self.assertEqual(s['environments']['a']['player'][0]['environment']['codeOptimization'], 'Release build')

    def test_only_runs_the_build_workload_a_launch_needs(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}],
                             [{'id': 'build', 'kind': 'player.build_time', 'repeats': 2, 'rounds': 1}, {'id': 'start', 'kind': 'player.startup', 'build': 'build', 'repeats': 2}])
        out, rows = self.run_campaign(path, only=['start'])
        self.assertEqual(failures(rows), [])
        self.assertEqual(sum(1 for r in rows if r['type'] == 'process' and r['workload'] == 'build'), 1)   # pulled in, once
        self.assertEqual(sum(1 for r in rows if r['type'] == 'process' and r['workload'] == 'start'), 2)
        self.assertEqual(setting(rows, 'setup-build'), [])
        self.assertIn('pulled in by --only', json.loads((out / 'campaign.json').read_text())['schedule_notes'][0])

    def test_debug_and_release_variants_are_compared_within_each_pair(self):
        variants = [{'id': '6.7-debug', 'editor': editor(), 'code_optimization': 'debug'},
                    {'id': '6.7-release', 'editor': editor('--scale', '0.5'), 'code_optimization': 'release', 'baseline': '6.7-release'},
                    {'id': '7-debug', 'editor': editor('--scale', '0.5'), 'code_optimization': 'debug'},
                    {'id': '7-release', 'editor': editor('--scale', '0.25'), 'code_optimization': 'release', 'baseline': '6.7-release'}]
        path = self.campaign(variants, [{'id': 'core', 'kind': 'editor.adapter', 'method': 'My.Core.Run', 'repeats': 4}], baseline='6.7-debug')
        out, rows = self.run_campaign(path)
        s = json.loads((out / 'summary.json').read_text())
        self.assertEqual(s['baselines'], {'6.7-debug': '6.7-debug', '6.7-release': '6.7-release', '7-debug': '6.7-debug', '7-release': '6.7-release'})
        alpha = next(i for i in s['workloads'][0]['items'] if i['name'] == 'alpha')
        self.assertEqual(sorted(alpha['ratios']), ['7-debug', '7-release'])           # the baselines have no ratio of their own
        self.assertAlmostEqual(alpha['ratios']['7-debug']['ratio'], 0.5)
        self.assertAlmostEqual(alpha['ratios']['7-release']['ratio'], 0.5)            # 0.25 against 0.5, not against the debug baseline
        md = (out / 'summary.md').read_text()
        self.assertIn('7-debug/6.7-debug', md)
        self.assertIn('7-release/6.7-release', md)
        self.assertNotIn('6.7-release/6.7-debug', md)
        self.assertIn('Each variant is compared with its own baseline', md)

    def test_prepare_commands_run_per_variant_in_the_copy_with_placeholders(self):
        script = self.tmp / 'prep.py'
        script.write_text('import sys, pathlib\npathlib.Path(sys.argv[1]).joinpath("prepared.txt").write_text(" ".join(sys.argv[2:]))\n')
        path = self.campaign([{'id': 'a', 'editor': editor()}, {'id': 'b', 'editor': editor()}], [{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 1}],
                             project={'prepare': [['{python}', '{campaign}/prep.py', '{project}', '{variant}', '{root}']]})
        out, _ = self.run_campaign(path, keep_work=True)
        text = (out / 'work/a/prepared.txt').read_text().split()
        self.assertEqual(text[0], 'a')
        self.assertTrue((out / 'work/b/prepared.txt').is_file())
        failing = self.campaign([{'id': 'a', 'editor': editor()}], [{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 1}], project={'prepare': [[sys.executable, '-c', 'raise SystemExit(3)']]})
        with self.assertRaisesRegex(RuntimeError, 'exited 3'):
            self.run_campaign(failing, name='out2')
        self.assertFalse((self.tmp / 'out2/work/a').exists())

    def test_only_variants_that_run_get_a_copy_and_work_dir_can_be_elsewhere(self):
        path = self.campaign([{'id': 'a', 'editor': editor()}, {'id': 'unused', 'editor': editor()}],
                             [{'id': 'w', 'kind': 'editor.warm_open', 'repeats': 1, 'variants': ['a']}])
        work = self.tmp / 'short'
        out, _ = self.run_campaign(path, keep_work=True, work=work)
        self.assertTrue((work / 'a').is_dir())
        self.assertFalse((work / 'unused').exists())
        self.assertFalse((out / 'work').exists())

    def test_script_edit_target_outside_the_project_uses_the_variants_worktree(self):
        repo = self.tmp / 'repo'
        (repo / 'unity/Assets').mkdir(parents=True)
        (repo / 'packages/core').mkdir(parents=True)
        (repo / 'unity/Assets/A.cs').write_text('class A {}\n')
        (repo / 'packages/core/Core.cs').write_text('class Core {}\n')
        for args in (['init', '-q'], ['add', '-A'], ['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'x']):
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
        cfg = {'name': 'fake', 'project': {'path': 'repo/unity'}, 'defaults': {'repeats': 1, 'rounds': 1},
               'variants': [{'id': 'a', 'editor': editor(), 'revision': 'HEAD'}, {'id': 'b', 'editor': editor(), 'revision': 'HEAD'}],
               'workloads': [{'id': 'edit', 'kind': 'editor.script_edit', 'targets': [{'id': 'leaf', 'path': 'Assets/A.cs'},
                                                                                         {'id': 'core', 'path': 'packages/core/Core.cs', 'root': 'worktree'}]}]}
        path = self.tmp / 'c.json'
        path.write_text(json.dumps(cfg))
        out, rows = self.run_campaign(path, keep_work=True)
        self.assertEqual(failures(rows), [])
        for variant in ('a', 'b'):
            request = json.loads(next((out / 'requests').glob(f'*edit-{variant}.json')).read_text())
            targets = {t['id']: t['path'] for t in request['targets']}
            self.assertEqual(targets['leaf'], 'Assets/A.cs')
            self.assertEqual(Path(targets['core']), out / 'work' / variant / 'packages/core/Core.cs')
        self.assertEqual((repo / 'packages/core/Core.cs').read_text(), 'class Core {}\n')
        subprocess.run(['git', '-C', str(repo), 'worktree', 'prune'], capture_output=True)


class PlayerConfigTests(unittest.TestCase):
    @staticmethod
    def base(**changes):
        raw = {'name': 'c', 'project': {'path': 'p'}, 'variants': [{'id': 'a', 'editor': '/u/a'}, {'id': 'b', 'editor': '/u/b'}],
               'workloads': [{'id': 'w', 'kind': 'player.adapter', 'method': 'A.B.Run'}]}
        raw.update(changes)
        return raw

    def check(self, raw, message):
        with self.assertRaisesRegex(config.ConfigError, message):
            config.validate(raw, '/base')

    def test_player_adapter_defaults_and_validation(self):
        w = config.validate(self.base(), '/base')['workloads'][0]
        self.assertEqual((w['headless'], w['window'], w['scenes'], w['player_args'], w['timeout_seconds'], w['prebuild']), (False, '1280x800', [], [], 1800, None))
        scene = config.validate(self.base(workloads=[{'id': 'w', 'kind': 'player.build_time', 'scene': 'Assets/M.unity'}]), '/base')['workloads'][0]
        self.assertEqual(scene['scenes'], ['Assets/M.unity'])
        self.check(self.base(workloads=[{'id': 'w', 'kind': 'player.adapter'}]), 'method')
        self.check(self.base(workloads=[{'id': 'w', 'kind': 'player.adapter', 'method': 'A.B.Run', 'scenes': ['../x.unity']}]), 'scenes')
        self.check(self.base(workloads=[{'id': 'w', 'kind': 'player.adapter', 'method': 'A.B.Run', 'window': 'big'}]), 'window')
        self.check(self.base(workloads=[{'id': 'w', 'kind': 'player.adapter', 'method': 'A.B.Run', 'prebuild': 'nope'}]), 'prebuild')
        self.check(self.base(workloads=[{'id': 'w', 'kind': 'player.adapter', 'method': 'A.B.Run', 'build': 'zzz'}]), 'build must name')
        self.check(self.base(workloads=[{'id': 'w', 'kind': 'player.adapter', 'method': 'A.B.Run', 'typo': 1}]), 'unknown key')

    def test_baselines_prepare_and_worktree_targets(self):
        cfg = config.validate(self.base(defaults={'baseline': 'b'}, variants=[{'id': 'a', 'editor': '/u/a', 'baseline': 'a'}, {'id': 'b', 'editor': '/u/b'}, {'id': 'c', 'editor': '/u/c'}]), '/base')
        self.assertEqual({v['id']: v['baseline'] for v in cfg['variants']}, {'a': 'a', 'b': 'b', 'c': 'b'})
        self.check(self.base(variants=[{'id': 'a', 'editor': '/u/a', 'baseline': 'zzz'}]), 'baseline must name')
        self.check(self.base(project={'path': 'p', 'prepare': ['not-a-list']}), 'prepare')
        edit = {'id': 'e', 'kind': 'editor.script_edit', 'targets': [{'id': 't', 'path': 'packages/x/Y.cs', 'root': 'worktree'}]}
        self.check(self.base(workloads=[edit]), 'need every selected variant to set a revision')
        ok = self.base(workloads=[edit], variants=[{'id': 'a', 'editor': '/u/a', 'revision': 'HEAD'}])
        self.assertEqual(config.validate(ok, '/base')['workloads'][0]['targets'][0]['root'], 'worktree')
        self.check(self.base(workloads=[{**edit, 'targets': [{'id': 't', 'path': '../x.cs', 'root': 'worktree'}]}]), 'project-relative')
        self.check(self.base(workloads=[{**edit, 'targets': [{'id': 't', 'path': 'x.cs', 'root': 'moon'}]}]), 'root must be')


if __name__ == '__main__':
    unittest.main()
