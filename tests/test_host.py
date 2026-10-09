"""Replay lifecycle/CPU evidence independently of an eligibility assertion."""
import copy
import datetime
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if ROOT.name == '.github':
    ROOT = ROOT.parent / 'benchmark-framework'
sys.path.insert(0, str(ROOT/'src'))
from unity_bench.config import digest
from unity_bench.evidence import sha, write
from unity_bench.host import derive, interval, validate_host, cpu_table, HostSession, retained_owned

BASE = datetime.datetime(2026, 10, 9, tzinfo=datetime.timezone.utc).timestamp()
EDITOR = r'C:\Unity\Editor\Unity.exe'


def stamp(seconds):
    return datetime.datetime.fromtimestamp(BASE+seconds, datetime.timezone.utc).isoformat()


def row(pid, parent, name, path, started):
    return {'pid': pid, 'parentPid': parent, 'name': name, 'executable': path, 'startedUnix': BASE+started}


def fixture():
    controller = row(1, 0, 'python.exe', r'C:\Python\python.exe', -10)
    wrapper = row(2, 1, 'python.exe', r'C:\Python\python.exe', .75)
    primary = row(3, 2, 'Unity.exe', EDITOR, .8)
    snapshots = []
    for t in (0, .5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7):
        known = [controller] + ([wrapper, primary] if t >= 1 else [])
        live = [controller] + ([wrapper, primary] if 1 <= t < 6.5 else [])
        counters = {1: 1+t*.001}
        if t >= 1:
            counters.update({2: .001, 3: .2 if t >= 5 else 0})
        snapshots.append({'utc': stamp(t), 'monotonic': 100+t, 'censusStartUtc': stamp(t-.02), 'censusStartMonotonic': 100+t-.02, 'processorCount': 4,
                          'systemBusySeconds': 10+t, 'processes': copy.deepcopy(live), 'known': copy.deepcopy(known),
                          'cpuCounters': counters, 'memory': {}, 'probeMs': 1,
                          'processExits': {2: {'pid': 2, 'startedUnix': BASE+.75, 'exitedUnix': BASE+6.46, 'exitCode': 0, 'executable': wrapper['executable']}, 3: {'pid': 3, 'startedUnix': BASE+.8, 'exitedUnix': BASE+6.4, 'exitCode': 0, 'objectWaitCode': 0, 'executable': EDITOR}} if t >= 6.5 else {},
                          'observer': {'controllerPid': 1, 'wrapperPid': 2 if t >= 1 else None, 'primaryPid': 3 if t >= 1.5 else None, 'machineFingerprint': 'c'*64}})
    request = {'runId': 'campaign-run', 'warmIterations': 3, 'variant': 'a', 'workload': 'fixed'}
    byte_digest = 'a'*64
    def sig(t):
        return {'runId': request['runId'], 'processId': 3, 'requestSha256': byte_digest, 'utc': stamp(t)}
    boundaries = {'ready': sig(1.2), 'start': sig(4.6), 'finished': sig(5.5), 'release': sig(6.1),
                  'afterExitUtc': stamp(6.5), 'jobReturnedUtc': stamp(6.47), 'readyObservedUtc': stamp(1.5), 'finishedObservedUtc': stamp(5.5),
                  'launchObservedUtc': stamp(1), 'requestSha256': byte_digest, 'settledBeforeStartUtc': stamp(1.5), 'finishedHeldBeginUtc': stamp(5.5), 'finishedHeldEndUtc': stamp(6)}
    boundaries['ready'].update(isCompiling=False, isUpdating=False)
    boundaries['finished']['completedUtc'] = stamp(5.4)
    for snap in snapshots:
        for proof_pid, proof in snap['processExits'].items():
            proof.update(objectWaitCode=0, cpuSeconds=snap['cpuCounters'][proof_pid])
    return snapshots, boundaries, request, {'editorExecutable': EDITOR, 'unityExecutable': EDITOR}, {'foreignCpuReferencePercent': 3}


class HostReplayTests(unittest.TestCase):
    def replay(self, args):
        if 'ownedJoin' not in args[1]:
            complete = args[1].get('jobReturnedUtc', stamp(6.47)); end = datetime.datetime.fromisoformat(complete).timestamp()-BASE
            entry = min(6.45, end); identities = retained_owned(args[0], 2)
            args[1].update(jobApiReturnedUtc=stamp(entry), ownedCleanupCompletedUtc=complete)
            args[1]['ownedJoin'] = {'schemaVersion': 1, 'timeoutSeconds': 2, 'status': 'completed',
                'apiReturnedUtc': stamp(entry), 'apiReturnedMonotonic': 100+entry,
                'completedUtc': complete, 'completedMonotonic': 100+end,
                'observations': [{'beginUtc': stamp(entry), 'beginMonotonic': 100+entry,
                    'utc': complete, 'monotonic': 100+end,
                    'processes': [{'pid': pid, 'identity': identity, 'actualPid': pid,
                        'startedUnix': identity['startedUnix'], 'waitCode': 0, 'querySucceeded': True, 'error': None,
                        'utc': complete, 'monotonic': 100+end} for pid, identity in identities.items()]}]}
        return derive(*args, 1, 2, 3)

    def test_complete_lifecycle_retains_foreign_excursions_without_selecting(self):
        result = self.replay(fixture())
        self.assertEqual('eligible', result['status'])
        self.assertGreater(result['foreignExcursions'], 0)
        self.assertEqual(14, result['intervalCount'])
        self.assertEqual(None, result['intervals'][2]['primaryProcessId'])
        self.assertEqual(3, result['intervals'][3]['primaryProcessId'])

    def test_json_string_counter_keys_replay_identically(self):
        args = fixture(); expected = self.replay(args)
        raw = json.loads(json.dumps(args[0]))
        self.assertEqual(expected, self.replay((raw, *args[1:])))
        with self.assertRaises(ValueError): cpu_table({'01': 0})
        with self.assertRaises(ValueError): cpu_table({1: 0, '1': 0})

    def test_missing_pid_and_wrong_executable_and_pid_reuse_refused(self):
        for change in ('missing', 'wrong-exe', 'reuse'):
            args = fixture(); snap = args[0][10]
            if change == 'missing': snap['processes'] = snap['processes'][:2]
            if change == 'wrong-exe': snap['processes'][2]['executable'] = r'C:\Other\Unity.exe'
            if change == 'reuse': snap['processes'][2]['startedUnix'] += .1
            with self.subTest(change=change), self.assertRaises(ValueError): self.replay(args)

    def test_gap_skew_nan_and_missing_owned_counter_are_hard(self):
        for key, value in (('monotonic', 108), ('utc', stamp(5.02)), ('systemBusySeconds', float('nan'))):
            args = fixture(); args[0][10][key] = value
            with self.subTest(key=key):
                self.assertEqual('ineligible', self.replay(args)['status'])
        args = fixture(); del args[0][10]['cpuCounters'][3]
        self.assertEqual('ineligible', self.replay(args)['status'])

    def test_short_hold_future_signal_and_wrong_request_digest_refused(self):
        for mutate in (lambda a: a[1]['start'].update(utc=stamp(1.8)),
                       lambda a: a[1]['release'].update(utc=stamp(5.7)),
                       lambda a: a[1]['ready'].update(requestSha256='b'*64),
                       lambda a: a[1]['finished'].update(completedUtc=stamp(5.6)),
                       lambda a: a[1]['release'].update(utc=stamp(9))):
            args = fixture(); mutate(args)
            with self.assertRaises(ValueError): self.replay(args)

    def test_owned_background_hard_even_before_ready_and_boundary_straddle(self):
        args = fixture()
        for snap in args[0][2:]:
            child = row(5, 2, 'UnityShaderCompiler.exe', r'C:\Unity\Editor\UnityShaderCompiler.exe', .9)
            snap['known'].append(child)
            if snap['utc'] >= stamp(6.5):
                snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'], 'exitedUnix': BASE+6.46, 'exitCode': 1, 'objectWaitCode': 0, 'cpuSeconds': 0, 'executable': child['executable']}
            if stamp(6.5) > snap['utc']: snap['processes'].append(copy.deepcopy(child))
            snap['cpuCounters'][5] = .4 if snap['utc'] >= stamp(5) else .2
            if 5 in snap['processExits']: snap['processExits'][5]['cpuSeconds'] = snap['cpuCounters'][5]
        self.assertIn('owned-background-load', self.replay(args)['errors'])

    def test_unreadable_nonblocker_os_process_does_not_waive_unknown_blocker(self):
        args = fixture()
        for snap in args[0]:
            opaque = {'pid': 8, 'parentPid': 0, 'name': 'ProtectedOS.exe', 'executable': None, 'startedUnix': None}
            snap['processes'].append(opaque); snap['known'].append(copy.deepcopy(opaque)); snap['cpuCounters'][8] = None
        self.assertEqual('eligible', self.replay(args)['status'])
        for snap in args[0]: snap['processes'][-1]['name'] = 'Runner.Worker.exe'
        with self.assertRaises(ValueError): self.replay(args)

    def test_completed_hold_bridge_and_following_release_background_are_annotations(self):
        args = fixture()
        extra = copy.deepcopy(args[0][12]); extra.update(utc=stamp(6.2), monotonic=106.2, censusStartUtc=stamp(6.18), censusStartMonotonic=106.18, systemBusySeconds=16.2)
        extra['cpuCounters'][1] = 1.0062
        args[0].insert(13, extra)
        for snap in args[0][13:]:
            child = row(5, 2, 'UnityShaderCompiler.exe', r'C:\Unity\Editor\UnityShaderCompiler.exe', 6.15)
            snap['known'].append(child)
            if snap['utc'] >= stamp(6.5):
                snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'], 'exitedUnix': BASE+6.46, 'exitCode': 1, 'objectWaitCode': 0, 'cpuSeconds': 0, 'executable': child['executable']}
            if snap['utc'] < stamp(6.5): snap['processes'].append(copy.deepcopy(child))
            snap['cpuCounters'][5] = .1 if snap['utc'] >= stamp(6.5) else 0
            if 5 in snap['processExits']: snap['processExits'][5]['cpuSeconds'] = snap['cpuCounters'][5]
        result = self.replay(args)
        self.assertEqual('eligible', result['status'])
        self.assertEqual(2, sum(r['backgroundAnnotation'] for r in result['intervals']))
        self.assertTrue(any(r['backgroundAnnotation'] and r['ownedBackgroundCpuPercent'] > .5 for r in result['intervals']))
        args[1]['release']['utc'] = stamp(6.3)
        with self.assertRaises(ValueError): self.replay(args)

    def test_exact_controlling_worker_ancestry_allowed_other_worker_refused(self):
        args = fixture()
        for snap in args[0]:
            worker = row(7, 0, 'Runner.Worker.exe', r'C:\runner\Runner.Worker.exe', -20)
            snap['processes'].append(copy.deepcopy(worker)); snap['known'].append(worker); snap['cpuCounters'][7] = 1
            snap['processes'][0]['parentPid'] = 7; snap['known'][0]['parentPid'] = 7
        self.assertEqual('eligible', self.replay(args)['status'])
        args[0][10]['processes'].append(row(8, 0, 'Runner.Worker.exe', r'C:\other\Runner.Worker.exe', -10))
        args[0][10]['known'].append(copy.deepcopy(args[0][10]['processes'][-1])); args[0][10]['cpuCounters'][8] = 0
        self.assertEqual('ineligible', self.replay(args)['status'])

    def test_controller_cycle_and_non_utc_time_refused(self):
        args = fixture(); args[0][10]['known'][0]['parentPid'] = 1
        self.assertEqual('ineligible', self.replay(args)['status'])
        args = fixture(); args[0][10]['utc'] = '2026-10-09T09:00:02.500000+09:00'
        with self.assertRaises(ValueError): self.replay(args)

    def test_primary_disappears_after_release_only_with_exact_native_exit_proof(self):
        args = fixture()
        snap = copy.deepcopy(args[0][12]); snap.update(utc=stamp(6.45), monotonic=106.45, censusStartUtc=stamp(6.43), censusStartMonotonic=106.43, systemBusySeconds=16.45)
        snap['cpuCounters'][1] = 1.00645
        snap['processes'] = snap['processes'][:2]
        snap['processExits'] = {3: {'pid': 3, 'startedUnix': BASE+.8, 'exitedUnix': BASE+6.4, 'exitCode': 0, 'objectWaitCode': 0, 'executable': EDITOR}}
        args[0].insert(13, snap)
        self.assertEqual('eligible', self.replay(args)['status'])
        for mutate in (lambda s: s.update(processExits={}),
                       lambda s: s['processExits'][3].update(startedUnix=BASE+.7),
                       lambda s: s['processExits'][3].update(executable=r'C:\Other\Unity.exe'),
                       lambda s: s['processExits'][3].update(exitedUnix=BASE+6.0),
                       lambda s: s['processExits'][3].update(exitCode=1)):
            bad = copy.deepcopy(args); mutate(bad[0][13])
            with self.assertRaises(ValueError): self.replay(bad)
        bad = copy.deepcopy(args); bad[0][12]['processes'] = bad[0][12]['processes'][:2]
        bad[0][12]['processExits'] = copy.deepcopy(snap['processExits'])
        with self.assertRaises(ValueError): self.replay(bad)

    def test_final_wrapper_success_cannot_hide_wrong_primary_exit(self):
        for change in ({'exitCode': 1}, {'exitedUnix': BASE+6.7}, {'pid': 99}):
            args = fixture(); args[0][-1]['processExits'][3].update(change)
            with self.assertRaises(ValueError): self.replay(args)

    def test_same_executable_secondary_unity_before_ready_is_background(self):
        args = fixture()
        for snap in args[0][2:]:
            child = row(5, 3, 'Unity.exe', EDITOR, .9)
            snap['known'].append(child)
            if snap['utc'] >= stamp(6.5):
                snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'], 'exitedUnix': BASE+6.46, 'exitCode': 1, 'objectWaitCode': 0, 'cpuSeconds': 0, 'executable': child['executable']}
            if snap['utc'] == stamp(1): snap['processes'].append(copy.deepcopy(child))
            snap['cpuCounters'][5] = .2
            if 5 in snap['processExits']: snap['processExits'][5]['cpuSeconds'] = snap['cpuCounters'][5]
        result = self.replay(args)
        self.assertEqual('eligible', result['status'])
        startup = result['intervals'][1]
        self.assertTrue(startup['preparationCpuAnnotation'])
        self.assertIsNone(startup['primaryProcessId'])
        self.assertEqual(3, startup['backgroundClassificationPrimaryPid'])
        self.assertEqual('final-ready-native-identity-replay', startup['backgroundClassificationBasis'])
        self.assertGreater(startup['ownedBackgroundCpuPercent'], .5)

    def test_pre_ready_main_cpu_is_work_and_identity_mismatch_cannot_classify(self):
        args = fixture(); args[0][2]['cpuCounters'][3] = .2
        for snap in args[0][3:]:
            snap['cpuCounters'][3] += .2
            if 3 in snap['processExits']: snap['processExits'][3]['cpuSeconds'] = snap['cpuCounters'][3]
        result = self.replay(args)
        self.assertEqual('eligible', result['status'])
        startup = result['intervals'][1]
        self.assertIsNone(startup['primaryProcessId'])
        self.assertGreater(startup['ownedCpuPercent'], .5)
        self.assertEqual(0, startup['ownedBackgroundCpuPercent'])
        args[0][3]['known'][2]['executable'] = r'C:\Wrong\Unity.exe'
        with self.assertRaises(ValueError): self.replay(args)

    def test_bounded_settling_uses_first_consecutive_idle_before_any_timing(self):
        args = fixture()
        from unity_bench.host import settled_before
        result = self.replay(args)
        self.assertEqual(stamp(1.5), settled_before(result['intervals'], stamp(1.5)))
        args[1]['settledBeforeStartUtc'] = stamp(2)
        with self.assertRaises(ValueError): self.replay(args)
        args = fixture(); args[1]['ready']['isCompiling'] = True
        with self.assertRaises(ValueError): self.replay(args)
        args = fixture(); args[1]['ready']['isUpdating'] = True
        with self.assertRaises(ValueError): self.replay(args)

    def test_postready_busy_interval_resets_settling_without_dropping_raw(self):
        args = fixture()
        # A background child active after ready interrupts idle; no 3s run
        # exists before this fixture's start. Earlier bootstrap work is retained.
        for snap in args[0][4:]:
            child = row(5, 3, 'Unity.exe', EDITOR, 1.8)
            snap['known'].append(child)
            if snap['utc'] >= stamp(6.5):
                snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'], 'exitedUnix': BASE+6.46, 'exitCode': 1, 'objectWaitCode': 0, 'cpuSeconds': 0, 'executable': child['executable']}
            if snap['utc'] < stamp(2.5): snap['processes'].append(copy.deepcopy(child))
            snap['cpuCounters'][5] = .2
            if 5 in snap['processExits']: snap['processExits'][5]['cpuSeconds'] = snap['cpuCounters'][5]
        with self.assertRaises(ValueError): self.replay(args)

    def test_live_callback_waits_startup_then_releases_after_three_seconds_idle(self):
        args = fixture(); full = args[0]
        for snap in full[2:]:
            child = row(5, 3, 'Unity.exe', EDITOR, .9)
            snap['known'].append(child)
            if snap['utc'] >= stamp(6.5):
                snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'], 'exitedUnix': BASE+6.46, 'exitCode': 1, 'objectWaitCode': 0, 'cpuSeconds': 0, 'executable': child['executable']}
            if snap['utc'] == stamp(1): snap['processes'].append(copy.deepcopy(child))
            snap['cpuCounters'][5] = .2
            if 5 in snap['processExits']: snap['processExits'][5]['cpuSeconds'] = snap['cpuCounters'][5]
        host = HostSession.__new__(HostSession)
        host.controller = 1; host.wrapper = 2; host.primary = None
        host.variant = args[3]; host.snapshots = full[:4]; host.previous = host.snapshots[-1]
        host.previous['observer']['primaryPid'] = None
        host.ready = host.start = host.finished = host.release = None
        host.records = []; host.preparation_deadline = 100; host.settled_start = None
        host.sample = lambda: None
        host.read_signal = lambda key: args[1]['ready'] if key == 'readyPath' else None
        host.request = args[2]; host.request_sha = 'a'*64
        class Clock(datetime.datetime):
            current = 1.6
            @classmethod
            def now(cls, timezone=None):
                return datetime.datetime.fromtimestamp(BASE+cls.current, datetime.timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            host.request['startPath'] = str(Path(temp)/'start.json')
            with patch('unity_bench.host.datetime.datetime', Clock), patch('unity_bench.host.time.monotonic', return_value=1):
                host.on_poll()
            self.assertIsNone(host.start)
            host.snapshots = full[:10]; host.previous = host.snapshots[-1]; Clock.current = 4.6
            with patch('unity_bench.host.datetime.datetime', Clock), patch('unity_bench.host.time.monotonic', return_value=4):
                host.on_poll()
            self.assertEqual(stamp(1.5), host.settled_start)
            self.assertEqual(stamp(4.6), host.start['utc'])
            self.assertEqual(10, len(host.snapshots))

    def test_live_callback_preparation_timeout_does_not_start_work(self):
        host = HostSession.__new__(HostSession); host.start = None; host.preparation_deadline = 120
        host.sample = lambda: None
        with patch('unity_bench.host.time.monotonic', return_value=120):
            with self.assertRaises(TimeoutError): host.on_poll()
        self.assertIsNone(host.start)

    def test_cross_release_background_is_annotation_after_exact_completed_hold(self):
        args = fixture()
        # The bridge starts at the complete held end (6.0), just before the
        # release signal (6.1); all work completed before held begin (5.5).
        for snap in args[0][13:]:
            child = row(5, 3, 'Unity.exe', EDITOR, 6.2)
            snap['known'].append(child)
            if snap['utc'] >= stamp(6.5):
                snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'], 'exitedUnix': BASE+6.46, 'exitCode': 1, 'objectWaitCode': 0, 'cpuSeconds': 0, 'executable': child['executable']}
            snap['cpuCounters'][5] = .2
            if 5 in snap['processExits']: snap['processExits'][5]['cpuSeconds'] = snap['cpuCounters'][5]
        result = self.replay(args)
        bridge = next(row for row in result['intervals'] if row['startUtc'] == stamp(6))
        self.assertEqual('eligible', result['status'])
        self.assertTrue(bridge['backgroundAnnotation'])
        self.assertGreater(bridge['ownedBackgroundCpuPercent'], .5)
        self.assertLess(stamp(6), args[1]['release']['utc'])
        self.assertEqual(stamp(6), args[1]['finishedHeldEndUtc'])

    def test_bridge_cannot_overlap_work_or_bypass_ineligible_complete_hold(self):
        for mutate in (lambda a: a[1].update(finishedHeldEndUtc=stamp(5.5)),
                       lambda a: a[1]['finished'].update(completedUtc=stamp(5.6))):
            args = fixture(); mutate(args)
            with self.assertRaises(ValueError): self.replay(args)
        args = fixture()
        for snap in args[0][12:]:
            child = row(5, 3, 'Unity.exe', EDITOR, 5.7)
            snap['known'].append(child)
            if snap['utc'] >= stamp(6.5):
                snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'], 'exitedUnix': BASE+6.46, 'exitCode': 1, 'objectWaitCode': 0, 'cpuSeconds': 0, 'executable': child['executable']}
            if snap['utc'] < stamp(6.5): snap['processes'].append(copy.deepcopy(child))
            snap['cpuCounters'][5] = .2
            if 5 in snap['processExits']: snap['processExits'][5]['cpuSeconds'] = snap['cpuCounters'][5]
        with self.assertRaisesRegex(ValueError, 'Ineligible lifecycle hold'):
            self.replay(args)

    def born_within_prior_census(self, birth=1.48, name='UnityCrashHandler64.exe'):
        args = fixture(); previous, current = copy.deepcopy(args[0][3:5])
        previous.update(censusStartUtc=stamp(1.47), censusStartMonotonic=101.47)
        child = row(5, 3, name, EDITOR if name == 'Unity.exe' else r'C:\Unity\Editor\UnityCrashHandler64.exe', birth)
        current['known'].append(child); current['processes'].append(copy.deepcopy(child)); current['cpuCounters'][5] = .1
        current['systemBusySeconds'] = previous['systemBusySeconds'] + .2
        return previous, current

    def test_new_native_birth_inside_prior_probe_retains_conservative_cpu_bounds(self):
        previous, current = self.born_within_prior_census()
        result = interval(previous, current, 1, 2, 3, EDITOR, EDITOR, classification_primary_pid=3)
        self.assertEqual('eligible', result['status'])
        self.assertEqual(.1, result['newProcessCensusBaselines'][0]['fullCumulativeCpuSeconds'])
        self.assertGreater(result['ownedCpuPercent'], 4.9)
        self.assertGreater(result['foreignCpuPercent'], result['foreignCpuLowerBoundPercent'])
        self.assertGreater(result['foreignCpuPercent'], 9.9)
        self.assertEqual('conservative-upper-bound-with-census-birth-uncertainty', result['foreignCpuBasis'])
        for boundary in (1.47, 1.5):
            previous, current = self.born_within_prior_census(boundary)
            self.assertEqual('eligible', interval(previous, current, 1, 2, 3, EDITOR, EDITOR)['status'])

    def test_genuine_old_missing_baseline_or_missing_bad_census_proof_stays_unknown(self):
        for mutate in (lambda p,c: c['known'][-1].update(startedUnix=BASE+1.469999),
                       lambda p,c: p.pop('censusStartUtc'),
                       lambda p,c: p.pop('censusStartMonotonic'),
                       lambda p,c: p.update(censusStartUtc=stamp(1.51)),
                       lambda p,c: p.update(censusStartUtc=stamp(-1)),
                       lambda p,c: p.update(censusStartMonotonic=101.0),
                       lambda p,c: p['cpuCounters'].update({5: None}),
                       lambda p,c: p['cpuCounters'].update({5: .2}),
                       lambda p,c: p['known'].append(copy.deepcopy(c['known'][-1]))):
            previous, current = self.born_within_prior_census(); mutate(previous,current)
            self.assertEqual('unknown', interval(previous,current,1,2,3,EDITOR,EDITOR)['status'])

    def test_census_birth_uncertainty_never_hides_measured_background_load(self):
        args = fixture()
        args[0][10].update(censusStartUtc=stamp(4.97), censusStartMonotonic=104.97)
        for snap in args[0][11:]:
            child = row(5,3,'Unity.exe',EDITOR,4.98)
            snap['known'].append(child)
            if snap['utc'] >= stamp(6.5):
                snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'], 'exitedUnix': BASE+6.46, 'exitCode': 1, 'objectWaitCode': 0, 'cpuSeconds': 0, 'executable': child['executable']}
            if snap['utc'] < stamp(6.5): snap['processes'].append(copy.deepcopy(child))
            snap['cpuCounters'][5] = .1
            if 5 in snap['processExits']: snap['processExits'][5]['cpuSeconds'] = snap['cpuCounters'][5]
        result = self.replay(args)
        self.assertEqual('ineligible', result['status'])
        self.assertIn('owned-background-load', result['errors'])
        measured = result['intervals'][10]
        self.assertGreater(measured['ownedBackgroundCpuPercent'], .5)
        self.assertEqual(5, measured['newProcessCensusBaselines'][0]['pid'])

    def test_bad_recorded_census_window_refuses_even_without_new_process(self):
        for mutate in (lambda s: s.pop('censusStartMonotonic'),
                       lambda s: s.pop('censusStartUtc'),
                       lambda s: s.update(censusStartUtc=stamp(1.51), censusStartMonotonic=101.51),
                       lambda s: s.update(censusStartUtc=stamp(-1), censusStartMonotonic=99),
                       lambda s: s.update(censusStartUtc=stamp(1.47), censusStartMonotonic=101.0)):
            args = fixture(); previous, current = copy.deepcopy(args[0][3:5]); mutate(previous)
            self.assertEqual('unknown', interval(previous,current,1,2,3,EDITOR,EDITOR)['status'])
        args = fixture(); previous, current = copy.deepcopy(args[0][3:5])
        previous.update(censusStartUtc=stamp(1.47), censusStartMonotonic=101.47)
        self.assertEqual('eligible', interval(previous,current,1,2,3,EDITOR,EDITOR)['status'])

    def test_native_census_windows_are_sequential_without_overlap_tolerance(self):
        args = fixture(); previous, current = copy.deepcopy(args[0][3:5])
        previous.update(censusStartUtc=stamp(1.47), censusStartMonotonic=101.47)
        current.update(censusStartUtc=stamp(1.5), censusStartMonotonic=101.5)
        self.assertEqual('eligible', interval(previous,current,1,2,3,EDITOR,EDITOR)['status'])
        for start in (1.49, 1.499999):
            current.update(censusStartUtc=stamp(start), censusStartMonotonic=100+start)
            self.assertEqual('unknown', interval(previous,current,1,2,3,EDITOR,EDITOR)['status'])

    def test_first_native_census_endpoint_proves_whole_owned_exit_after_job_return(self):
        args = fixture()
        # Ancillary native finalization is after job return but before the first
        # census endpoint. That complete bridge is unmeasured; final after is hard.
        for snap in args[0][13:]:
            child = row(5, 3, 'Unity.exe', EDITOR, 6.2)
            snap['known'].append(child); snap['cpuCounters'][5] = .2
            snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'],
                                      'exitedUnix': BASE+6.465, 'exitCode': 1, 'objectWaitCode': 0, 'cpuSeconds': .2, 'executable': EDITOR}
        result = self.replay(args)
        self.assertEqual('eligible', result['status'])
        self.assertGreater(args[0][13]['processExits'][5]['exitedUnix'], BASE+6.45)
        bridge = result['intervals'][12]
        self.assertTrue(bridge['backgroundAnnotation'])
        self.assertGreater(bridge['ownedBackgroundCpuPercent'], .5)
        self.assertFalse(result['intervals'][-1]['backgroundAnnotation'])

    def test_first_exit_census_cannot_be_skipped_or_accept_missing_wrong_future_live_proof(self):
        for mutate in (
                lambda a: a[0][13]['processExits'].pop(2),
                lambda a: a[0][13]['processExits'][2].update(executable=r'C:\Other\python.exe'),
                lambda a: a[0][13]['processExits'][2].update(startedUnix=BASE+.7),
                lambda a: a[0][13]['processExits'][2].update(exitedUnix=BASE+6.51),
                lambda a: a[0][13]['processExits'][2].update(exitCode=None),
                lambda a: a[0][13]['processes'].append(copy.deepcopy(a[0][13]['known'][1])),
                lambda a: a[1].update(afterExitUtc=stamp(7)),
                lambda a: a[1].pop('jobReturnedUtc'),
                lambda a: a[0][13].pop('censusStartUtc')):
            args = fixture(); mutate(args)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): self.replay(args)

    def test_later_discovered_owned_child_or_changed_final_counter_cannot_escape_exit_proof(self):
        args = fixture(); child = row(5, 3, 'Unity.exe', EDITOR, 6.3)
        args[0][-1]['known'].append(child); args[0][-1]['cpuCounters'][5] = .1
        args[0][-1]['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'],
                                        'exitedUnix': BASE+6.4, 'exitCode': 0, 'objectWaitCode': 0, 'executable': EDITOR}
        with self.assertRaisesRegex(ValueError, 'Owned identity missing'): self.replay(args)
        for mutate in (lambda s: s['cpuCounters'].update({2: .002}),
                       lambda s: s['processExits'][2].update(exitedUnix=BASE+6.47)):
            args = fixture(); mutate(args[0][-1])
            with self.assertRaisesRegex(ValueError, 'Owned native exit .*CPU'): self.replay(args)

    def test_historical_owned_identity_cannot_escape_through_final_parent_change(self):
        for changed in ({'parentPid': 0}, {'executable': r'C:\Other\child.exe'},
                        {'name': 'Other.exe'}, {'startedUnix': BASE+.95}):
            args = fixture()
            child = row(5, 3, 'ordinary-child.exe', r'C:\Unity\ordinary-child.exe', .9)
            for snap in args[0][2:]:
                snap['known'].append(copy.deepcopy(child)); snap['cpuCounters'][5] = .001
                if snap['utc'] >= stamp(6.5):
                    snap['processExits'][5] = {'pid': 5, 'startedUnix': child['startedUnix'],
                                              'exitedUnix': BASE+6.4, 'exitCode': 0,
                                              'executable': child['executable'], 'objectWaitCode': 0, 'cpuSeconds': .001}
            self.assertEqual('eligible', self.replay(args)['status'])
            args[0][-1]['known'][-1].update(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'Retained owned identity changed'):
                self.replay(args)

    def test_job_return_inside_previous_census_cannot_skip_first_owned_exit_endpoint(self):
        args = fixture()
        after = copy.deepcopy(args[0][-1])
        after.update(utc=stamp(7.5), monotonic=107.5, censusStartUtc=stamp(7.48),
                     censusStartMonotonic=107.48, systemBusySeconds=17.5)
        after['cpuCounters'][1] = 1.0075
        args[0].append(after)
        self.assertEqual('eligible', self.replay(args)['status'])
        # A forged callback time inside the already-observed first exit census
        # would skip that census and extend the quit bridge through the next.
        args[1].update(jobReturnedUtc=stamp(6.49), ownedCleanupCompletedUtc=stamp(6.49), afterExitUtc=stamp(7))
        join = args[1]['ownedJoin']; join.update(completedUtc=stamp(6.49), completedMonotonic=106.49)
        join['observations'][-1].update(utc=stamp(6.49), monotonic=106.49)
        for row in join['observations'][-1]['processes']: row.update(utc=stamp(6.49), monotonic=106.49)
        with self.assertRaisesRegex(ValueError, 'Job return lies inside an earlier census'):
            self.replay(args)

    def pending_primary_fixture(self):
        args = fixture(); snap = copy.deepcopy(args[0][12])
        snap.update(utc=stamp(6.3), monotonic=106.3, censusStartUtc=stamp(6.28),
                    censusStartMonotonic=106.28, systemBusySeconds=16.3)
        snap['cpuCounters'][1] = 1.0063
        snap['processes'] = snap['processes'][:2]
        snap['processHandleStates'] = {3: {'requestedPid': 3, 'pid': 3, 'querySucceeded': True,
                                          'error': None, 'executable': EDITOR, 'startedUnix': BASE+.8,
                                          'exitCode': 259, 'waitBefore': 258, 'waitAfter': 258, 'exitTimeDefined': False, 'exitedUnix': None,
                                          'cpuSeconds': snap['cpuCounters'][3]}}
        args[0].insert(13, snap)
        return args

    def test_postrelease_census_absence_needs_current_pending_handle_and_eventual_native_zero_exit(self):
        args = self.pending_primary_fixture()
        self.assertEqual('eligible', self.replay(args)['status'])
        for mutate in (lambda a: a[0][13].pop('processHandleStates'),
                       lambda a: a[0][-1]['processExits'].pop(3),
                       lambda a: a[0][13]['processHandleStates'][3].update(querySucceeded=False),
                       lambda a: a[0][13]['processHandleStates'][3].update(pid=4),
                       lambda a: a[0][13]['processHandleStates'][3].update(startedUnix=BASE+.9),
                       lambda a: a[0][13]['processHandleStates'][3].update(executable=r'C:\Other\Unity.exe'),
                       lambda a: a[0][13]['processHandleStates'][3].update(cpuSeconds=.21),
                       lambda a: a[0][13]['processHandleStates'][3].update(exitCode=1),
                       lambda a: a[0][13]['processHandleStates'][3].update(error='failed')):
            bad = copy.deepcopy(args); mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): self.replay(bad)

    def test_assigned_zero_exit_is_pending_only_with_double_object_timeout_proof(self):
        args = self.pending_primary_fixture(); state = args[0][13]['processHandleStates'][3]
        state['exitCode'] = 0
        self.assertEqual('eligible', self.replay(args)['status'])
        for mutate in (lambda s: s.pop('waitBefore'), lambda s: s.pop('waitAfter'),
                       lambda s: (s.pop('waitBefore'), s.pop('waitAfter')),
                       lambda s: s.update(waitBefore=0, waitAfter=258),
                       lambda s: s.update(waitBefore=258, waitAfter=0),
                       lambda s: s.update(waitBefore=0xffffffff),
                       lambda s: s.update(waitAfter=128)):
            bad = copy.deepcopy(args); mutate(bad[0][13]['processHandleStates'][3])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): self.replay(bad)
        args[0][13]['processHandleStates'][3]['waitAfter'] = 0
        args[0][13]['processExits'][3] = {'pid': 3, 'startedUnix': BASE+.8, 'exitedUnix': BASE+6.29,
                                        'exitCode': 0, 'objectWaitCode': 0, 'cpuSeconds': .2, 'executable': EDITOR}
        for snap in args[0][14:]: snap['processExits'][3]['exitedUnix'] = BASE+6.29
        self.assertEqual('eligible', self.replay(args)['status'])

    def test_primary_terminal_exit_always_requires_signaled_object_proof(self):
        for index in (13, 14):
            args = fixture(); args[0][index]['processExits'][3].pop('objectWaitCode')
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'signaled final CPU proof'):
                self.replay(args)

    def test_optional_failed_state_does_not_replace_strong_native_exit_and_cpu_proof(self):
        args = fixture()
        for snap in args[0][13:]:
            snap['processHandleStates'] = {3: {'requestedPid': 3, 'pid': 3, 'querySucceeded': False, 'waitBefore': 0, 'waitAfter': 0,
                                              'error': 'QueryFullProcessImageNameW failed', 'cpuSeconds': None}}
        self.assertEqual('eligible', self.replay(args)['status'])
        args[0][-1]['cpuCounters'][3] = None
        with self.assertRaises(ValueError): self.replay(args)

    def test_current_nonzero_handle_pid_contradiction_is_hard_even_live_or_with_terminal_proof(self):
        for index in (10, 13, 14):
            args = fixture()
            args[0][index]['processHandleStates'] = {3: {'requestedPid': 3, 'pid': 4,
                                                        'querySucceeded': False, 'waitBefore': 0, 'waitAfter': 0, 'error': 'wrong handle'}}
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'Retained handle PID contradicts'):
                self.replay(args)
        args = fixture()
        for snap in args[0][13:]:
            snap['processHandleStates'] = {3: {'requestedPid': 3, 'pid': 0, 'querySucceeded': False, 'waitBefore': 0, 'waitAfter': 0,
                                              'error': 'GetProcessId failed', 'cpuSeconds': None}}
        self.assertEqual('eligible', self.replay(args)['status'])

    def test_successful_handle_state_must_match_anchor_and_cpu_even_live_or_with_terminal_proof(self):
        for index in (10, 13, 14):
            for changed in ({'executable': r'C:\Other\Unity.exe'}, {'startedUnix': BASE+.9}, {'cpuSeconds': .21}):
                args = fixture(); snap = args[0][index]
                state = {'requestedPid': 3, 'pid': 3, 'querySucceeded': True, 'waitBefore': 0, 'waitAfter': 0, 'error': None,
                         'executable': EDITOR, 'startedUnix': BASE+.8, 'cpuSeconds': snap['cpuCounters'][3]}
                snap['processHandleStates'] = {3: state}
                self.assertEqual('eligible', self.replay(args)['status'])
                state.update(changed)
                with self.subTest(index=index, changed=changed), self.assertRaisesRegex(ValueError, 'Successful handle state contradicts'):
                    self.replay(args)

    def test_pending_primary_absence_cannot_precede_release_census_or_complete_finished_hold(self):
        args = self.pending_primary_fixture(); args[0][13].update(censusStartUtc=stamp(6.09), censusStartMonotonic=106.09)
        with self.assertRaises(ValueError): self.replay(args)
        args = self.pending_primary_fixture(); snap = args[0].pop(13)
        snap.update(utc=stamp(5.75), monotonic=105.75, censusStartUtc=stamp(5.73),
                    censusStartMonotonic=105.73, systemBusySeconds=15.75)
        snap['cpuCounters'][1] = 1.00575
        args[0].insert(12, snap)
        with self.assertRaises(ValueError): self.replay(args)

    def test_owned_terminal_cpu_binding_and_selected_history_do_not_hide_contradictions(self):
        args = fixture(); snap = args[0][13]
        state = {'requestedPid': 3, 'pid': 3, 'querySucceeded': True, 'error': None,
                 'startedUnix': BASE+.8, 'executable': EDITOR, 'cpuSeconds': .2, 'exitCode': 0,
                 'waitBefore': 0, 'waitAfter': 0, 'exitTimeDefined': True, 'exitedUnix': BASE+6.4}
        snap['processHandleStates'] = {3: copy.deepcopy(state)}
        prior = copy.deepcopy(state); prior.update(cpuSeconds=.19, waitBefore=258, waitAfter=0,
                                                  exitTimeDefined=False, exitedUnix=None)
        snap['processHandleStateHistory'] = {3: [prior]}
        self.assertEqual('eligible', self.replay(args)['status'])
        for changed in ({'pid': 4}, {'requestedPid': 4}, {'startedUnix': BASE+.9},
                        {'executable': r'C:\Other\Unity.exe'}, {'cpuSeconds': .21},
                        {'waitBefore': 0}, {'querySucceeded': False}):
            bad = copy.deepcopy(args); bad[0][13]['processHandleStateHistory'][3][0].update(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'Earlier selected handle state contradicts'):
                self.replay(bad)
        for mutate in (lambda a: a[0][13]['processExits'][2].pop('objectWaitCode'),
                       lambda a: a[0][13]['processExits'][2].update(cpuSeconds=.002),
                       lambda a: a[0][14]['cpuCounters'].update({2: .002})):
            bad = copy.deepcopy(args); mutate(bad)
            with self.assertRaises(ValueError): self.replay(bad)

    def test_owned_join_replay_refuses_missing_identity_bad_wait_clock_and_late_complete(self):
        args = fixture(); self.replay(args)
        for mutate in (lambda j: j.update(schemaVersion=True), lambda j: j.update(schemaVersion=1.0),
                       lambda j: j.update(timeoutSeconds=2.0), lambda j: j.update(status='pending'),
                       lambda j: j.update(completedMonotonic=108.46),
                       lambda j: j['observations'][0]['processes'].pop(),
                       lambda j: j['observations'][0]['processes'][0].update(actualPid=99),
                       lambda j: j['observations'][0]['processes'][0].update(startedUnix=BASE),
                       lambda j: j['observations'][0]['processes'][0].update(waitCode=False),
                       lambda j: j['observations'][0]['processes'][0].update(waitCode=0xffffffff),
                       lambda j: j['observations'][0]['processes'][0].update(querySucceeded=False)):
            bad = copy.deepcopy(args); mutate(bad[1]['ownedJoin'])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError): self.replay(bad)
        bad = copy.deepcopy(args); join = bad[1]['ownedJoin']
        later = copy.deepcopy(join['observations'][0]); later.update(beginUtc=stamp(6.47), beginMonotonic=106.47,
                                                                    utc=stamp(6.475), monotonic=106.475)
        for row in later['processes']: row.update(utc=stamp(6.475), monotonic=106.475)
        join['observations'].append(later); join.update(completedUtc=stamp(6.475), completedMonotonic=106.475)
        bad[1].update(jobReturnedUtc=stamp(6.475), ownedCleanupCompletedUtc=stamp(6.475))
        with self.assertRaisesRegex(ValueError, 'skipped first complete'): self.replay(bad)

    def test_owned_join_cannot_reverse_an_already_signaled_process(self):
        args = fixture(); self.replay(args); join = args[1]['ownedJoin']
        template = join['observations'][0]; observations = []
        for begin, end, waits in ((6.45, 6.46, (0, 258)), (6.46, 6.465, (258, 258)), (6.465, 6.47, (0, 0))):
            observation = copy.deepcopy(template)
            observation.update(beginUtc=stamp(begin), beginMonotonic=100+begin, utc=stamp(end), monotonic=100+end)
            for row, wait in zip(observation['processes'], waits): row.update(waitCode=wait, utc=stamp(end), monotonic=100+end)
            observations.append(observation)
        join['observations'] = observations
        with self.assertRaisesRegex(ValueError, 'signal reversed'): self.replay(args)

    def join_callback_fixture(self, outcome='completed'):
        args = fixture(); self.replay(args)
        complete = copy.deepcopy(args[1]['ownedJoin']['observations'][0])
        pending = copy.deepcopy(complete); pending.update(beginUtc=stamp(6.45), beginMonotonic=106.45,
                                                         utc=stamp(6.46), monotonic=106.46)
        for row in pending['processes']: row.update(utc=stamp(6.46), monotonic=106.46)
        pending['processes'][0]['waitCode'] = 258
        complete.update(beginUtc=stamp(6.465), beginMonotonic=106.465)
        observations = [pending, complete]
        if outcome == 'invalid':
            pending['processes'][0].update(querySucceeded=False, error='wrong identity')
        if outcome == 'timeout':
            complete.update(beginUtc=stamp(8.45), beginMonotonic=108.45, utc=stamp(8.46), monotonic=108.46)
            for row in complete['processes']: row.update(utc=stamp(8.46), monotonic=108.46)
        host = HostSession.__new__(HostSession); host.wrapper = 2; host.primary = 3; host.variant = args[3]
        host.snapshots = args[0][:13]; host.previous = host.snapshots[-1]
        host.probe = type('Probe', (), {'owned_join_observation': lambda _: observations.pop(0)})()
        calls = []
        def sample(force=False):
            calls.append(force)
            if force:
                next_snapshot = args[0][13 if calls.count(True) == 1 else 14]
                host.snapshots.append(next_snapshot); host.previous = next_snapshot
        host.sample = sample
        return host, calls

    def test_callback_joins_objects_with_sampling_before_one_first_exit_census(self):
        host, calls = self.join_callback_fixture()
        class Clock(datetime.datetime):
            @classmethod
            def now(cls, timezone=None): return datetime.datetime.fromtimestamp(BASE+6.45, datetime.timezone.utc)
        with patch('unity_bench.host.datetime.datetime', Clock), patch('unity_bench.host.time.monotonic', return_value=106.45), patch('unity_bench.host.time.sleep'):
            host.on_finished()
        self.assertEqual([False, True, True], calls)
        self.assertEqual(stamp(6.45), host.job_api_returned); self.assertEqual(stamp(6.47), host.job_returned)
        self.assertEqual(stamp(6.5), host.after_exit); self.assertEqual(2, len(host.owned_join['observations']))
        for outcome, error in (('invalid', ValueError), ('timeout', TimeoutError)):
            host, calls = self.join_callback_fixture(outcome)
            with patch('unity_bench.host.datetime.datetime', Clock), patch('unity_bench.host.time.monotonic', return_value=106.45), patch('unity_bench.host.time.sleep'):
                with self.assertRaises(error): host.on_finished()
            self.assertNotIn(True, calls); self.assertEqual(outcome, host.owned_join['status'])

    def test_native_initial_image_conversion_is_validated_in_raw_identity_replay(self):
        args = fixture(); native = r'\Device\HarddiskVolume3\Unity\Editor\Unity.exe'
        proof = {'schemaVersion':2, 'pid':3, 'api':'QueryFullProcessImageNameW-native', 'nativeLength':len(native),
                 'nativeImagePath':native, 'executable':EDITOR, 'mappings':[{'dosDevice':'C:', 'targets':[r'\Device\HarddiskVolume3']}],
                 'attempts':[{'api':'QueryFullProcessImageNameW','flags':0,'success':False,'errorCode':5},
                             {'api':'QueryFullProcessImageNameW','flags':1,'success':True,'errorCode':0}]}
        payload = proof['mappings'][0]['targets'][0] + '\0\0'
        proof['driveInventory'] = {'api':'GetLogicalDrives','mask':4,'errorCode':0,
            'queries':[{'dosDevice':'C:','returnedCount':len(payload),'errorCode':0,'returnedValue':payload}]}
        for snap in args[0][2:]:
            next(row for row in snap['known'] if row['pid']==3)['imageProof'] = copy.deepcopy(proof)
        self.assertEqual('eligible', self.replay(args)['status'])
        bad = copy.deepcopy(args)
        next(row for row in bad[0][10]['known'] if row['pid']==3)['imageProof']['attempts'][1]['success'] = False
        with self.assertRaisesRegex(ValueError, 'successful API evidence'): self.replay(bad)

    def test_hash_bound_summary_is_rederived_not_trusted(self):
        args = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); request = args[2]
            for key, file in [('readyPath', 'ready.json'), ('startPath', 'start.json'), ('finishedPath', 'finished.json'),
                              ('releasePath', 'release.json'), ('outputPath', 'result.json')]: request[key] = str(directory/file)
            write(directory/'request.json', request)
            byte_digest = sha((directory/'request.json').read_bytes())
            args[1]['requestSha256'] = byte_digest
            for key in ('ready', 'start', 'finished', 'release'):
                args[1][key]['requestSha256'] = byte_digest; write(Path(request[key+'Path']), args[1][key])
            write(Path(request['outputPath']), {'processId': 3, 'requestSha256': byte_digest, 'startedUtc': stamp(4.7), 'completedUtc': stamp(5.4)})
            summary = self.replay(args); records = summary.pop('intervals'); summary['requestSha256'] = byte_digest
            write(directory/'host-snapshots.json', args[0]); write(directory/'host-intervals.json', records)
            summary['snapshotsSha256'] = sha((directory/'host-snapshots.json').read_bytes())
            summary['intervalsSha256'] = sha((directory/'host-intervals.json').read_bytes())
            write(directory/'host-summary.json', summary)
            execution = {'wrapperProcessId': 2, 'hostSummaryDigest': digest(summary)}
            self.assertEqual('eligible', validate_host(directory, request, args[3], args[4], execution)['status'])
            for old_schema in (1, 2, 3):
                summary['schemaVersion'] = old_schema
                (directory/'host-summary.json').write_text(json.dumps(summary)); execution['hostSummaryDigest'] = digest(summary)
                with self.subTest(old_schema=old_schema), self.assertRaisesRegex(ValueError, 'Unsupported host evidence schema'):
                    validate_host(directory, request, args[3], args[4], execution)
            summary['schemaVersion'] = 4
            summary['probeFailures'] = [{'pid': 124, 'stage': 'query-image', 'errorType': 'OSError', 'error': 'Initial image query failed'}]
            (directory/'host-summary.json').write_text(json.dumps(summary)); execution['hostSummaryDigest'] = digest(summary)
            with self.assertRaisesRegex(ValueError, 'Partial native census failed'):
                validate_host(directory, request, args[3], args[4], execution)
            summary['probeFailures'] = []
            summary['foreignExcursions'] = 0
            (directory/'host-summary.json').write_text(json.dumps(summary)); execution['hostSummaryDigest'] = digest(summary)
            with self.assertRaises(ValueError): validate_host(directory, request, args[3], args[4], execution)


if __name__ == '__main__': unittest.main()
