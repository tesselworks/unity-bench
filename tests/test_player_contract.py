"""Player protocol units: mocks establish contracts, never native execution evidence."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('unity_bench', ROOT/'src/unity_bench/__init__.py', submodule_search_locations=[str(ROOT/'src/unity_bench')])
package = importlib.util.module_from_spec(spec); sys.modules[spec.name] = package; spec.loader.exec_module(package)
from unity_bench.config import validate, primary_executable, digest
from unity_bench.host import validate_readiness, interval
from unity_bench.report import validate_result


class PlayerContractTests(unittest.TestCase):
    def test_player_runtime_metadata_rejects_wrong_types_and_contradictory_library(self):
        variant, request, result = self.fixture()
        for key in ('runtimeDetectionBasis', 'coreLibrary', 'runtimeDescription'):
            for value in (True, ['runtime'], {'name': 'runtime'}):
                bad = copy.deepcopy(result); bad['environment'][key] = value
                with self.assertRaises(ValueError): validate_result(bad, request, variant)
        result['environment']['coreLibrary'] = 'mscorlib'
        with self.assertRaisesRegex(ValueError, 'contradicts'): validate_result(result, request, variant)

    def test_optional_worker_cpu_requires_exact_owned_measurement_scope(self):
        from test_host import fixture, row, stamp, BASE, EDITOR
        snapshots, _, _, _, _ = fixture()
        previous, current = copy.deepcopy(snapshots[10]), copy.deepcopy(snapshots[11])
        worker = row(7, 3, 'dotnet.exe', r'C:\Unity\Editor\Data\NetCoreRuntime\dotnet.exe', 4.8)
        for snapshot, cpu in ((previous, .1), (current, .9)):
            snapshot['known'].append(copy.deepcopy(worker)); snapshot['processes'].append(copy.deepcopy(worker)); snapshot['cpuCounters'][7] = cpu
        region = {'roles': ['script-compiler'], 'startedUtc': stamp(4.6), 'completedUtc': stamp(5.5),
                  'intervals': [{'iteration': 0, 'startedUtc': stamp(4.6), 'completedUtc': stamp(5.5)}]}
        legacy = interval(previous, current, 1, 2, 3, EDITOR)
        measured = interval(previous, current, 1, 2, 3, EDITOR, measured_region=region)
        self.assertGreater(legacy['ownedBackgroundCpuPercent'], .5)
        self.assertEqual(measured['ownedBackgroundCpuPercent'], 0)
        self.assertEqual(measured['originalOwnedBackgroundCpuPercent'], legacy['ownedBackgroundCpuPercent'])
        self.assertEqual(measured['measuredWorkerAttribution'][0]['role'], 'script-compiler')
        self.assertNotIn('measuredWorkerAttribution', legacy)
        for mutation in ('early-birth', 'outside-installation', 'wrong-ancestry', 'boundary-straddle'):
            a, b, policy = copy.deepcopy(previous), copy.deepcopy(current), copy.deepcopy(region)
            if mutation == 'boundary-straddle': policy['intervals'][0]['startedUtc'] = stamp(5.1)
            else:
                for snapshot in (a, b):
                    for table in ('known', 'processes'):
                        item = next(r for r in snapshot[table] if r['pid'] == 7)
                        if mutation == 'early-birth': item['startedUnix'] = BASE+4.3
                        if mutation == 'outside-installation': item['executable'] = r'C:\Program Files\dotnet\dotnet.exe'
                        if mutation == 'wrong-ancestry': item['parentPid'] = 2
            refused = interval(a, b, 1, 2, 3, EDITOR, measured_region=policy)
            self.assertGreater(refused.get('ownedBackgroundCpuPercent', 1), .5)

    def test_complete_diagnostic_round_proof_is_preserved_without_stability_claim(self):
        variant, request, result = self.fixture()
        proofs = [{'iteration': i, 'inputFingerprint': result['inputFingerprint'], 'outputFingerprint': ('d' if i == 0 else 'f')*64,
                   'workCounters': result['workCounters']} for i in (0, 1)]
        result['semanticParity'] = {'stable': False, 'iterations': proofs, 'orderedProofsDigest': digest(proofs)}
        result['outputFingerprint'] = digest(proofs)
        validate_result(result, request, variant)
        result['semanticParity']['stable'] = True
        with self.assertRaisesRegex(ValueError, 'declaration'): validate_result(result, request, variant)

    def test_consumer_validity_false_preserves_completion_but_wrong_type_refuses(self):
        variant,request,result=self.fixture()
        result['workloadValidity']={'valid':False}
        validate_result(result,request,variant)
        result['workloadValidity']['valid']=1
        with self.assertRaisesRegex(ValueError,'validity'): validate_result(result,request,variant)

    def test_unstable_complete_execution_retained_but_ratios_refused(self):
        from test_report import ReportTests
        fixture=ReportTests(); fixture.setUp()
        try:
            def unstable(result):
                proofs=[{'iteration':i,'inputFingerprint':result['inputFingerprint'],'outputFingerprint':('e' if i==1 else 'd')*64,'workCounters':result['workCounters']} for i in range(3)]
                result['semanticParity']={'stable':False,'iterations':proofs,'orderedProofsDigest':digest(proofs)}
                result['outputFingerprint']=digest(proofs)
            fixture.change_result(6,unstable)
            from unity_bench.report import summarize
            report=summarize(fixture.folder)
            self.assertEqual(report['comparisons'][0]['adoptedProcesses'],0)
            self.assertEqual(report['comparisons'][0]['ratios'],[])
            self.assertIn('unstable-semantic-round-proofs',report['comparisons'][0]['reasons'])
            self.assertEqual(report['failures'],[])
        finally: fixture.doCleanups()

    def fixture(self):
        variant = {'id': 'player', 'revision': 'a'*40, 'unityVersion': '7000.0.0a7', 'backend': 'coreclr',
                   'executionKind': 'player', 'primaryExecutable': str(ROOT/'Player.exe')}
        request = {'runId': 'run', 'variant': 'player', 'workload': 'full-table', 'sourceRevision': 'a'*40,
                   'planDigest': 'b'*64, 'readyPath': str(ROOT/'ready.json'), 'warmIterations': 1}
        result = {'schemaVersion': 1, 'runId': 'run', 'variant': 'player', 'workload': 'full-table', 'status': 'completed',
                  'processId': 123, 'sourceRevision': 'a'*40, 'planDigest': 'b'*64,
                  'startedUtc': '2026-10-10T00:00:00Z', 'completedUtc': '2026-10-10T00:01:00Z',
                  'inputFingerprint': 'c'*64, 'outputFingerprint': 'd'*64, 'workCounters': {'steps': 1800},
                  'samples': [{'iteration': i, 'phase': 'first' if i == 0 else 'warm', 'metric': 'frame/p95', 'value': 2., 'unit': 'ms'} for i in (0, 1)],
                  'environment': {'unityVersion': variant['unityVersion'], 'backend': 'coreclr', 'executionKind': 'player',
                                  'isEditor': False, 'primaryExecutable': variant['primaryExecutable'], 'runtimeDetectionBasis': 'System.Private.CoreLib without Mono.Runtime',
                                  'coreLibrary': 'System.Private.CoreLib', 'runtimeDescription': '.NET10',
                                  'settings': {'debugBuild': False, 'jobWorkerCountAvailable': True, 'jobWorkerCount': 8},
                                  'buildIdentity': {'sourceRevision': 'a'*40, 'unityVersion': variant['unityVersion'], 'backend': 'coreclr',
                                                    'developmentBuild': False, 'binarySha256': 'e'*64}}}
        return variant, request, result

    def test_player_requires_actual_build_runtime_and_native_executable_identity(self):
        variant, request, result = self.fixture()
        self.assertIs(validate_result(result, request, variant), result)
        mutations = (lambda r: r['environment'].update(isEditor=True),
                     lambda r: r['environment'].update(executionKind='editor-batch'),
                     lambda r: r['environment'].update(primaryExecutable=str(ROOT/'Wrong.exe')),
                     lambda r: r['environment'].update(backend='mono'),
                     lambda r: r['environment'].update(runtimeDetectionBasis=''),
                     lambda r: r['environment']['buildIdentity'].update(sourceRevision='f'*40),
                     lambda r: r['environment']['buildIdentity'].update(binarySha256='bad'),
                     lambda r: r['environment']['settings'].update(debugBuild=True))
        for mutate in mutations:
            invalid = copy.deepcopy(result); mutate(invalid)
            with self.assertRaises(ValueError): validate_result(invalid, request, variant)

    def test_player_readiness_does_not_spoof_editor_idle_flags(self):
        variant, _, _ = self.fixture()
        validate_readiness({'executionKind': 'player', 'prepared': True, 'assetLoadComplete': True}, variant)
        with self.assertRaises(ValueError):
            validate_readiness({'isCompiling': False, 'isUpdating': False}, variant)
        validate_readiness({'isCompiling': False, 'isUpdating': False}, {'editorExecutable': 'legacy'})

    def test_legacy_editor_default_and_config_remain_unchanged(self):
        variant, _, _ = self.fixture()
        config = {'schemaVersion': 1, 'name': 'player-contract', 'repository': str(ROOT), 'host': {'mode': 'windows-shared-host'},
                  'variants': [variant, dict(variant, id='other')], 'axes': [{'id': 'backend', 'left': 'player', 'right': 'other'}],
                  'workloads': [{'id': 'full-table', 'projectPath': 'Project', 'mode': 'samples', 'warmIterations': 3, 'command': ['adapter']}]}
        self.assertEqual(validate(copy.deepcopy(config)), config)
        legacy = dict(variant); legacy.pop('executionKind'); legacy.pop('primaryExecutable'); legacy['editorExecutable'] = str(ROOT/'Unity.exe')
        self.assertEqual(primary_executable(legacy), legacy['editorExecutable'])
        config['variants'][0]['primaryExecutable'] = 'relative.exe'
        with self.assertRaises(ValueError): validate(config)


if __name__ == '__main__': unittest.main()
