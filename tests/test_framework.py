"""Exercise the public extraction's real checkout/process/receipt boundaries."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

FRAMEWORK = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('unity_bench', FRAMEWORK / 'src/unity_bench/__init__.py', submodule_search_locations=[str(FRAMEWORK / 'src/unity_bench')])
package = importlib.util.module_from_spec(spec); sys.modules[spec.name] = package; spec.loader.exec_module(package)
from unity_bench.config import schedule, validate
from unity_bench.runner import plan, run
from unity_bench import runner
from unity_bench.report import summarize, validate_result


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.repo = self.base / 'independent-repo'; self.repo.mkdir()
        self.git('init', '-q'); self.git('config', 'user.name', 'Protocol Test'); self.git('config', 'user.email', 'test@example.invalid')
        project = self.repo / 'Project'; project.mkdir()
        shutil.copyfile(FRAMEWORK / 'examples/portable-contract/adapter.py', project / 'adapter.py')
        self.git('add', '.'); self.git('commit', '-qm', 'Standalone protocol fixture')
        self.sha = self.git('rev-parse', 'HEAD')
        self.config = {'schemaVersion': 1, 'name': 'portable-contract', 'repository': str(self.repo), 'repetitions': 3, 'calibrationRepetitions': 1,
                       'variants': [{'id': side, 'revision': self.sha, 'unityVersion': 'not-unity', 'backend': 'python-contract-fixture'} for side in ['a', 'b']],
                       'axes': [{'id': 'version', 'left': 'a', 'right': 'b'}],
                       'workloads': [{'id': 'fixedIntegerWork', 'projectPath': 'Project', 'mode': 'samples', 'warmIterations': 2,
                                      'timeoutSeconds': 30, 'command': [sys.executable, '{project}/adapter.py', '{request}']}]}
        self.path = self.base / 'config.json'; self.save()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def save(self):
        self.path.write_text(json.dumps(self.config))

    def test_real_independent_checkout_campaign_and_readonly_replay(self):
        destination = self.base / 'campaign'; result = run(self.path, destination)
        self.assertEqual(8, result['completedProcesses']); self.assertEqual([], result['failures'])
        self.assertEqual(['a', 'b', 'a', 'b', 'b', 'a', 'a', 'b'], [row['variant'] for row in schedule(self.config)])
        self.assertEqual(result, summarize(destination))
        self.assertFalse(result['comparisons'][0]['comparable']); self.assertEqual([], result['comparisons'][0]['ratios'])
        self.assertIn('independent-host-observer-disabled', result['comparisons'][0]['reasons'])
        self.assertNotIn(str(self.repo), (destination / 'public-report.json').read_text())
        self.assertEqual(self.sha, self.git('rev-parse', 'HEAD'))
        with self.assertRaises(ValueError): run(self.path, destination)

    def test_nonzero_exit_cannot_adopt_a_valid_receipt_and_no_replacement(self):
        with (self.repo / 'Project/adapter.py').open('a') as f: f.write('\nsys.exit(7)\n')
        self.git('add', '.'); self.git('commit', '-qm', 'Nonzero exit fixture')
        for variant in self.config['variants']: variant['revision'] = self.git('rev-parse', 'HEAD')
        self.save(); result = run(self.path, self.base / 'failed')
        self.assertEqual(0, result['completedProcesses']); self.assertEqual(8, len(result['failures']))

    def test_candidate_preparation_failure_never_times_a_baseline_subset(self):
        (self.repo/'Project/prepare.py').write_text('import sys\nsys.exit(7 if sys.argv[1] == "b" else 0)\n')
        self.git('add', '.'); self.git('commit', '-qm', 'Candidate preparation failure fixture')
        for variant in self.config['variants']: variant['revision'] = self.git('rev-parse', 'HEAD')
        self.config['workloads'][0]['prepare'] = [[sys.executable, '{project}/prepare.py', '{variant}']]
        self.save(); folder = self.base/'preparation-failure'
        report = run(self.path, folder)
        self.assertEqual(0, report['completedProcesses']); self.assertEqual([], list((folder/'runs').glob('*/result.json')))
        self.assertEqual(8, len(report['failures']))
        self.assertEqual(8, len(list((folder/'runs').glob('*/request.json'))))

    def test_external_clock_is_reported_as_whole_invocation(self):
        self.config['workloads'][0]['mode'] = 'external'; self.save()
        report = run(self.path, self.base / 'external')
        metrics = report['comparisons'][0]['descriptiveProcessDistributions']['a']
        self.assertEqual(['whole-process-wall/process'], list(metrics))
        self.assertGreater(metrics['whole-process-wall/process']['minMs'], 0)

    def test_changed_plan_cannot_relabel_evidence(self):
        folder = self.base / 'changed'; run(self.path, folder)
        path = folder / 'plan.json'; value = json.loads(path.read_text()); value['config']['name'] = 'changed'; path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): summarize(folder)

    def test_retained_success_file_does_not_override_nonzero_exit(self):
        folder = self.base/'changed-exit'; run(self.path, folder)
        path = folder/'runs/0000/execution.json'; execution = json.loads(path.read_text())
        execution['exitCode'] = 7; path.write_text(json.dumps(execution))
        replay = summarize(folder)
        self.assertEqual(7, replay['completedProcesses'])
        self.assertEqual('rejected', replay['failures'][0]['status'])

    def test_changed_request_and_missing_request_are_rejected_in_report(self):
        folder = self.base / 'request-change'; run(self.path, folder)
        path = folder / 'runs/0000/request.json'; original = path.read_text()
        for field, replacement in [('warmIterations', 1), ('clockMode', 'external'), ('sourceRevision', '0'*40), ('planDigest', '0'*64)]:
            value = json.loads(original); value[field] = replacement; path.write_text(json.dumps(value))
            self.assertEqual(7, summarize(folder)['completedProcesses'])
        path.unlink(); self.assertEqual(7, summarize(folder)['completedProcesses'])

    def test_cli_refuses_unknown_commit_without_disclosing_command(self):
        self.config['variants'][0]['revision'] = '0'*40; self.save()
        result = subprocess.run([sys.executable, str(FRAMEWORK/'unity-bench.py'), 'plan', str(self.path)], capture_output=True, text=True)
        self.assertEqual(2, result.returncode); self.assertNotIn(str(self.repo), result.stderr); self.assertNotIn('Traceback', result.stderr)

    def test_installed_source_fingerprint_never_scans_python_library(self):
        package = self.base/'site-packages/unity_bench'; package.mkdir(parents=True)
        (package/'runner.py').write_text('# installed package source\n')
        (package.parent/'private.py').write_bytes(b'\xffnot-utf8')
        with mock.patch.object(runner, '__file__', str(package/'runner.py')):
            evidence = plan(self.path)
        self.assertEqual(['python/runner.py'], list(evidence['frameworkSourceFiles']))
        self.assertEqual('python-package-only', evidence['frameworkSourceScope'])

    def test_unknown_placeholders_and_escaped_project_refused(self):
        for mutate in [lambda c: c['workloads'][0].update(projectPath='../outside'),
                       lambda c: c['workloads'][0].update(command=['{checkout.__class__}']),
                       lambda c: c['workloads'][0].update(command='echo hello'),
                       lambda c: c.update(repetitions=True),
                       lambda c: c['variants'][0].update(revision='main')]:
            candidate = copy.deepcopy(self.config); mutate(candidate)
            with self.assertRaises(ValueError): validate(candidate)

    def test_wrong_identity_nan_missing_round_and_changed_runtime_refused(self):
        folder = self.base / 'receipt'; run(self.path, folder)
        request = json.loads((folder/'runs/0000/request.json').read_text()); original = json.loads((folder/'runs/0000/result.json').read_text())
        for mutate in [lambda r: r.update(runId='stale'), lambda r: r['samples'][0].update(value=float('nan')),
                       lambda r: r['samples'].pop(), lambda r: r['workCounters'].clear(), lambda r: r['environment'].update(backend='il2cpp')]:
            value = copy.deepcopy(original); mutate(value)
            with self.assertRaises(ValueError): validate_result(value, request, self.config['variants'][0])


if __name__ == '__main__': unittest.main()
