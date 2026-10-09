"""Aggregation tests use explicit synthetic receipts, not actual host eligibility proof."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

FRAMEWORK = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('unity_bench', FRAMEWORK/'src/unity_bench/__init__.py', submodule_search_locations=[str(FRAMEWORK/'src/unity_bench')])
package = importlib.util.module_from_spec(spec); sys.modules[spec.name] = package; spec.loader.exec_module(package)
from unity_bench.config import digest, schedule
from unity_bench.evidence import write
from unity_bench.report import summarize


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = {'schemaVersion': 1, 'name': 'synthetic-report-unit-test', 'repository': str(self.folder),
                       'host': {'mode': 'windows-shared-host'}, 'repetitions': 3, 'calibrationRepetitions': 3,
                       'variants': [{'id': side, 'revision': 'a'*40, 'unityVersion': 'synthetic-version', 'backend': 'mono',
                                     'editorExecutable': str(self.folder/'Unity.exe')} for side in ('a', 'b')],
                       'axes': [{'id': 'version', 'left': 'a', 'right': 'b'}],
                       'workloads': [{'id': 'synthetic', 'projectPath': 'Project', 'mode': 'samples', 'warmIterations': 2, 'command': ['synthetic']} ]}
        self.rows = schedule(self.config)
        files = {'python/synthetic-unit-test.py': 'f'*64}
        write(self.folder/'plan.json', {'config': self.config, 'schedule': self.rows, 'planDigest': digest(self.config),
                                       'frameworkSourceFiles': files, 'frameworkSourceDigest': digest(files), 'frameworkSourceScope': 'python-package-only'})
        for row in self.rows:
            directory = self.folder/'runs'/('%04d'%row['index'])
            request = {'runId': str(row['index']), 'variant': row['variant'], 'workload': 'synthetic', 'warmIterations': 2,
                       'clockMode': 'samples', 'planDigest': digest(self.config), 'sourceRevision': 'a'*40,
                       'readyPath': str(directory/'ready.json')}
            write(directory/'request.json', request)
            value = (10 if row['variant'] == 'a' else 20) + (row['pair']*.1)
            result = {'schemaVersion': 1, 'runId': request['runId'], 'variant': row['variant'], 'workload': 'synthetic', 'status': 'completed',
                      'processId': row['index']+100, 'sourceRevision': 'a'*40, 'planDigest': digest(self.config),
                      'requestSha256': hashlib.sha256((directory/'request.json').read_bytes()).hexdigest(),
                      'startedUtc': '2026-10-09T00:00:01Z', 'completedUtc': '2026-10-09T00:00:02Z',
                      'environment': {'unityVersion': 'synthetic-version', 'backend': 'mono', 'executionKind': 'editor-batch',
                                      'settings': {'batchMode': True, 'jobWorkerCountAvailable': True, 'jobWorkerCount': 8,
                                                   'codeOptimizationAvailable': True, 'codeOptimization': 'Release'}},
                      'inputFingerprint': 'a'*64, 'outputFingerprint': 'b'*64, 'workCounters': {'items': 100},
                      'samples': [{'iteration': i, 'phase': 'first' if i == 0 else 'warm', 'metric': 'work', 'unit': 'ms', 'value': value} for i in range(3)]}
            write(directory/'result.json', result)
            write(directory/'execution.json', {'status': 'completed', 'exitCode': 0, 'requestDigest': digest(request), 'resultDigest': digest(result)})
        self.verifier = mock.patch('unity_bench.report.validate_host', return_value={'status': 'eligible', 'hostFingerprint': 'synthetic-unit-test-host'})
        self.verifier.start(); self.addCleanup(self.verifier.stop)

    def change_result(self, index, mutate):
        directory = self.folder/'runs'/('%04d'%index)
        path = directory/'result.json'; result = json.loads(path.read_text()); mutate(result)
        path.write_text(json.dumps(result))
        path = directory/'execution.json'; execution = json.loads(path.read_text()); execution['resultDigest'] = digest(result)
        path.write_text(json.dumps(execution))

    def test_process_pairs_and_first_warm_are_separate(self):
        comparison = summarize(self.folder)['comparisons'][0]
        self.assertTrue(comparison['comparable']); self.assertEqual(6, comparison['adoptedProcesses'])
        self.assertEqual(['work/first', 'work/warm'], [ratio['metric'] for ratio in comparison['ratios']])
        for ratio in comparison['ratios']:
            self.assertEqual(3, len(ratio['pairedProcesses']))
            self.assertEqual('right-slower-beyond-observed-spread', ratio['verdict'])

    def test_different_calibration_work_refuses_all_ratios(self):
        self.change_result(0, lambda r: r['workCounters'].update(items=101))
        comparison = summarize(self.folder)['comparisons'][0]
        self.assertFalse(comparison['comparable']); self.assertEqual([], comparison['ratios'])
        self.assertIn('unequal-calibration-work', comparison['reasons'])

    def test_different_calibration_metric_retains_a_diagnostic_report(self):
        self.change_result(0, lambda r: [s.update(metric='different-metric') for s in r['samples']])
        comparison = summarize(self.folder)['comparisons'][0]
        self.assertFalse(comparison['comparable']); self.assertEqual([], comparison['ratios'])
        self.assertIn('unequal-calibration-metric-coverage', comparison['reasons'])

    def test_removed_or_changed_framework_provenance_refuses_report(self):
        path = self.folder/'plan.json'; original = json.loads(path.read_text())
        value = dict(original); value.pop('frameworkSourceFiles'); path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): summarize(self.folder)
        value = dict(original); value['frameworkSourceDigest'] = '0'*64; path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): summarize(self.folder)

    def test_original_windows_identity_paths_replay_without_rewriting_hashes(self):
        for variant in self.config['variants']:
            variant['editorExecutable'] = 'C:\\Editors\\Unity.exe'
            variant['unityExecutable'] = 'C:\\Tools\\unity.exe'
        from unity_bench.config import validate
        validate(self.config)

    def test_missing_actual_worker_api_refuses_receipt(self):
        self.change_result(6, lambda r: r['environment']['settings'].update(jobWorkerCountAvailable=False))
        report = summarize(self.folder)
        self.assertEqual(11, report['completedProcesses']); self.assertFalse(report['comparisons'][0]['comparable'])

    def test_failed_host_verification_never_adopts_ratios(self):
        with mock.patch('unity_bench.report.validate_host', return_value={'status': 'ineligible', 'hostFingerprint': 'synthetic-unit-test-host'}):
            comparison = summarize(self.folder)['comparisons'][0]
        self.assertEqual(0, comparison['adoptedProcesses']); self.assertEqual([], comparison['ratios'])

    def test_large_same_variant_noise_makes_direction_unresolved(self):
        self.change_result(0, lambda r: [s.update(value=50) for s in r['samples']])
        comparison = summarize(self.folder)['comparisons'][0]
        self.assertTrue(comparison['comparable'])
        self.assertTrue(all(r['verdict'] == 'unresolved' for r in comparison['ratios']))


if __name__ == '__main__': unittest.main()
