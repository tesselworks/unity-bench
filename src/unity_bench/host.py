"""Independent Windows CPU/PID evidence and bounded workload lifecycle holds."""
import datetime
import hashlib
import math
import ntpath
import os
from pathlib import Path
import platform
import time
from .config import read_json, require
from .evidence import ancestry, canonical, descendants, sha, write
from .windows_host import WindowsProbe, validate_image_proof

BLOCKERS = {'unity', 'unityshadercompiler', 'assetimportworker', 'bee_backend', 'dotnet', 'msbuild',
            'vbcscompiler', 'clang', 'clang++', 'clang-cl', 'lld-link', 'il2cpp', 'cl', 'runner.worker'}
BACKGROUND = BLOCKERS - {'unity', 'runner.worker'}


def utc(value):
    require(isinstance(value, str) and value, 'Host UTC must be an aware timestamp')
    parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None and parsed.utcoffset() == datetime.timedelta(0), 'Naive or non-UTC host timestamp')
    return parsed.timestamp()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def path_equal(left, right):
    return bool(left) and bool(right) and ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def infrastructure(row, table):
    parent = table.get(row.get('parentPid'), {})
    child_path = row.get('executable') or ''
    parent_path = parent.get('executable') or ''
    folder = ntpath.dirname(child_path)
    return (ntpath.basename(child_path).lower() == 'unity.exe' and ntpath.basename(folder).lower() == 'resources'
            and ntpath.basename(parent_path).lower() == 'unity hub.exe'
            and path_equal(parent_path, ntpath.join(ntpath.dirname(folder), 'Unity Hub.exe')))


def census_bounds(snapshot):
    """Native census-call start and probe end, not an inferred timing tolerance."""
    begin = utc(snapshot.get('censusStartUtc'))
    begin_mono = snapshot.get('censusStartMonotonic')
    require(finite(begin_mono) and finite(snapshot.get('monotonic')), 'Census monotonic provenance unavailable')
    wall = utc(snapshot['utc']) - begin
    elapsed = snapshot['monotonic'] - begin_mono
    require(finite(elapsed) and 0 <= wall <= 2 and 0 <= elapsed <= 2 and abs(wall-elapsed) <= .010,
            'Native census window is invalid')
    return begin


def interval(previous, current, controller_pid, wrapper_pid=None, primary_pid=None, editor=None, cli=None, classification_primary_pid=None):
    """Every observed interval is retained, including unknown/refused evidence."""
    classification = primary_pid if classification_primary_pid is None else classification_primary_pid
    basis = 'final-ready-native-identity-replay' if classification_primary_pid is not None else 'observed-primary-only' if primary_pid is not None else 'provisional-unassigned-primary'
    base = {'backgroundClassificationPrimaryPid': classification, 'backgroundClassificationBasis': basis, 'startUtc': previous.get('utc'), 'utc': current.get('utc'), 'status': 'unknown', 'blockers': [],
            'memory': current.get('memory'), 'processes': current.get('processes'), 'probeMs': current.get('probeMs')}
    try:
        for snapshot in (previous, current):
            if 'censusStartUtc' in snapshot or 'censusStartMonotonic' in snapshot: census_bounds(snapshot)
        if 'censusStartUtc' in previous and 'censusStartUtc' in current:
            require(utc(current['censusStartUtc']) >= utc(previous['utc'])
                    and current['censusStartMonotonic'] >= previous['monotonic'], 'Native census windows overlap')
        dt = current['monotonic'] - previous['monotonic']
        wall = utc(current['utc']) - utc(previous['utc'])
        require(finite(dt) and finite(wall) and 0 < dt <= 2 and 0 < wall <= 2 and abs(dt-wall) <= .010, 'Host gap or UTC/monotonic skew')
        cores = current['processorCount']
        require(type(cores) is int and cores > 0 and cores == previous['processorCount'], 'Processor count changed')
        known = current['known']; table = process_table(known)
        prior = process_table(previous['known'])
        process_table(current['processes'])
        require(controller_pid in table, 'Controller identity unavailable')
        current_counters = cpu_table(current['cpuCounters'])
        previous_counters = cpu_table(previous['cpuCounters'])
        control = {controller_pid}
        cursor = controller_pid; visited_ancestors = set()
        while cursor in table and cursor not in (0, 4):
            require(cursor not in visited_ancestors, 'Cyclic controller ancestry')
            visited_ancestors.add(cursor); cursor = table[cursor]['parentPid']
        branch = ancestry(known, controller_pid)
        workers = {pid for pid in branch if table[pid]['name'].lower().removesuffix('.exe') == 'runner.worker'}
        require(len(workers) <= 1, 'Ambiguous controlling worker')
        if workers:
            pid = controller_pid
            visited = set()
            while pid in table:
                require(pid not in visited, 'Cyclic controller ancestry')
                visited.add(pid); control.add(pid)
                if pid in workers: break
                pid = table[pid]['parentPid']
        owned = descendants(known, {wrapper_pid}) if wrapper_pid else set()
        owned -= {0, 4}
        control -= {0, 4}
        identity_required = owned | control | {r['pid'] for r in current['processes'] if r['name'].lower().removesuffix('.exe') in BLOCKERS}
        for pid in prior.keys() & table.keys() & identity_required:
            require(prior[pid].get('startedUnix') == table[pid].get('startedUnix')
                    and path_equal(prior[pid].get('executable'), table[pid].get('executable'))
                    and prior[pid].get('parentPid') == table[pid].get('parentPid')
                    and prior[pid].get('name') == table[pid].get('name'), 'PID identity reused')
        for owned_pid in owned:
            seen_chain = set(); cursor = owned_pid
            while cursor != wrapper_pid:
                require(cursor in table and cursor not in seen_chain, 'Invalid owned process ancestry')
                seen_chain.add(cursor); cursor = table[cursor]['parentPid']
        for row in current['processes']:
            name = row['name'].lower().removesuffix('.exe')
            if name not in BLOCKERS or infrastructure(row, table): continue
            require(row.get('executable') and finite(row.get('startedUnix')), 'Unknown competing process identity')
            permitted = row['pid'] in control | owned
            if permitted and name == 'unity':
                permitted = path_equal(row.get('executable'), editor) or path_equal(row.get('executable'), cli)
            if not permitted:
                base['blockers'].append({'pid': row['pid'], 'name': row['name'], 'role': 'nonowned-or-unknown-build-editor-worker'})
        require(not base['blockers'], 'Competing Editor/compiler/worker')
        work_cpu = background_cpu = control_cpu = 0.0
        uncertain_birth_cpu = 0.0; census_baselines = []
        for pid in owned | control:
            after = current_counters.get(pid)
            before = previous_counters.get(pid)
            require(finite(after) and after >= 0, 'Owned CPU counter unavailable')
            if before is None and pid not in previous_counters:
                started = table.get(pid, {}).get('startedUnix')
                require(pid not in prior and finite(started) and started <= utc(current['utc']), 'Missing process start identity/baseline')
                if started < utc(previous['utc']):
                    # Toolhelp freezes its census near the beginning of a native
                    # probe. A child born inside that independently timestamped
                    # prior probe can be first observed here, before prior endUTC.
                    census_start = census_bounds(previous)
                    require(census_start <= started, 'Missing process predates proved census window')
                    # Full cumulative CPU overestimates, rather than understates,
                    # this child's interval CPU. Do not subtract that uncertain
                    # contribution from the conservative foreign upper bound.
                    uncertain_birth_cpu += after
                    census_baselines.append({'pid': pid, 'startedUnix': started,
                        'priorCensusStartUtc': previous['censusStartUtc'], 'priorEndpointUtc': previous['utc'],
                        'fullCumulativeCpuSeconds': after,
                        'basis': 'native-birth-within-prior-census-window-conservative-full-cpu'})
                before = 0
            require(finite(before) and 0 <= before <= after, 'CPU baseline missing or counter decreased')
            delta = after-before
            row = table[pid]
            require(row.get('executable') and finite(row.get('startedUnix')), 'Owned process identity unavailable')
            if pid in control and pid not in owned: control_cpu += delta; continue
            name = row['name'].lower().removesuffix('.exe')
            if name in BACKGROUND or (name == 'unity' and path_equal(row['executable'], editor) and classification is not None and pid != classification):
                background_cpu += delta
            else: work_cpu += delta
        busy = current['systemBusySeconds'] - previous['systemBusySeconds']
        require(finite(busy) and busy >= 0, 'System CPU counter unavailable/decreased')
        scale = 100/(dt*cores)
        foreign_lower = max(0, busy-work_cpu-background_cpu-control_cpu)
        foreign_upper = max(0, busy-work_cpu-background_cpu-control_cpu+uncertain_birth_cpu)
        require(all(finite(value*scale) for value in (work_cpu, background_cpu, control_cpu, foreign_upper)), 'Nonfinite CPU percentage')
        base.update(status='eligible', intervalSeconds=dt, wallSeconds=wall, monotonicSkewSeconds=abs(dt-wall),
                    ownedPids=sorted(owned), primaryProcessId=primary_pid, ownedCpuPercent=work_cpu*scale,
                    ownedBackgroundCpuPercent=background_cpu*scale, controllerCpuPercent=control_cpu*scale,
                    foreignCpuPercent=foreign_upper*scale, foreignCpuLowerBoundPercent=foreign_lower*scale,
                    foreignCpuBasis='conservative-upper-bound-with-census-birth-uncertainty' if census_baselines else 'system-minus-observed-owned-and-controller',
                    newProcessCensusBaselines=census_baselines)
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        base['reason'] = type(error).__name__
    return base


def cpu_table(values):
    require(isinstance(values, dict), 'CPU counters must be an object')
    result = {}
    for key, value in values.items():
        require((type(key) is int and key >= 0) or (isinstance(key, str) and key.isascii() and key.isdigit() and str(int(key)) == key), 'Invalid counter PID')
        pid = int(key)
        require(pid not in result, 'Duplicate counter PID')
        result[pid] = value
    return result


def process_table(rows):
    require(isinstance(rows, list), 'Process census must be an array')
    table = {}
    for row in rows:
        require(isinstance(row, dict) and type(row.get('pid')) is int and row['pid'] >= 0
                and type(row.get('parentPid')) is int and row['parentPid'] >= 0
                and isinstance(row.get('name'), str) and row['name']
                and row['pid'] not in table, 'Invalid or duplicate process identity')
        table[row['pid']] = row
    return table


def signal(path, request, pid, request_sha):
    value = {'runId': request['runId'], 'processId': pid, 'requestSha256': request_sha, 'utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    target = Path(path); pending = target.with_name(target.name+'.pending')
    require(not target.exists(), 'Signal already exists')
    write(pending, value); pending.rename(target)
    return value



def settled_before(records, observed_ready_utc):
    """First three-second consecutive idle preparation run; never timed selection."""
    start = None; seconds = 0.0; last = None
    for row in records:
        if utc(row['startUtc']) < utc(observed_ready_utc): continue
        require(row['status'] == 'eligible', 'Unknown or competing preparation evidence')
        require(last is None or row['startUtc'] == last, 'Preparation timeline discontinuity')
        last = row['utc']
        idle = row['ownedCpuPercent'] + row['ownedBackgroundCpuPercent'] <= .5
        if not idle:
            start = None; seconds = 0.0
            continue
        if start is None: start = row['startUtc']
        seconds += row['intervalSeconds']
        if seconds >= 3.0: return start
    return None


def validated_primary_identity(snapshots, observed_utc, ready, wrapper_pid, primary_pid, editor):
    """Identify the main process at ready; this does not assert earlier readiness."""
    endpoints = [snap for snap in snapshots if snap.get('utc') == observed_utc]
    require(len(endpoints) == 1, 'Ready endpoint is missing or ambiguous')
    endpoint = endpoints[0]
    known = process_table(endpoint['known']); live = process_table(endpoint['processes'])
    require(primary_pid in live and primary_pid in known
            and primary_pid in descendants(endpoint['known'], {wrapper_pid}), 'Ready main is not the live owned process')
    identity = live[primary_pid]
    require(all(identity.get(key) == known[primary_pid].get(key) for key in ('pid', 'parentPid', 'name', 'executable', 'startedUnix'))
            and path_equal(identity.get('executable'), editor) and finite(identity.get('startedUnix'))
            and utc(snapshots[0]['utc']) <= identity['startedUnix'] <= utc(ready['utc']) <= utc(observed_utc),
            'Ready main native executable/creation identity differs')
    return identity


def retained_owned(snapshots, wrapper_pid):
    """Union retained native identities so a later-discovered child cannot escape proof."""
    table = {}; owned = set()
    for snapshot in snapshots:
        observed = process_table(snapshot['known'])
        owned.update(descendants(list(observed.values()), {wrapper_pid}))
        table.update(observed)
    # Retain historical ownership even if a later record changes its parent.
    # The merged chain also catches identities whose parent was observed later.
    owned.update(descendants(list(table.values()), {wrapper_pid}))
    owned -= {0, 4}
    require(wrapper_pid in table and owned, 'Owned exit identity union unavailable')
    for snapshot in snapshots:
        observed = process_table(snapshot['known'])
        for pid in owned & observed.keys():
            require(all(observed[pid].get(key) == table[pid].get(key) for key in ('pid', 'parentPid', 'name', 'executable', 'startedUnix')),
                    'Retained owned identity changed')
    return {pid: table[pid] for pid in owned}


def prove_owned_exit(snapshot, identities, primary_pid, editor):
    """First post-job census must prove every retained owned process is exited."""
    live = process_table(snapshot['processes']); known = process_table(snapshot['known'])
    exits = snapshot.get('processExits'); counters = cpu_table(snapshot['cpuCounters'])
    require(isinstance(exits, dict), 'Owned native exit proof unavailable')
    require(not (identities.keys() & live.keys()), 'Owned process is still live after job return')
    endpoint = utc(snapshot['utc'])
    proofs = {}
    for pid, identity in identities.items():
        require(pid in known and all(known[pid].get(key) == identity.get(key) for key in ('pid', 'parentPid', 'name', 'executable', 'startedUnix')),
                'Owned identity missing at first exit census')
        native = exits.get(pid, exits.get(str(pid)))
        require(identity.get('executable') and finite(identity.get('startedUnix')) and isinstance(native, dict)
                and native.get('pid') == pid and native.get('startedUnix') == identity['startedUnix']
                and path_equal(native.get('executable'), identity['executable'])
                and type(native.get('exitCode')) is int and 0 <= native['exitCode'] <= 0xffffffff
                and finite(native.get('exitedUnix')) and identity['startedUnix'] <= native['exitedUnix'] <= endpoint
                and finite(counters.get(pid)) and counters[pid] >= 0, 'Owned native exit identity/counter is invalid')
        require(type(native.get('objectWaitCode')) is int and native['objectWaitCode'] == 0
                and finite(native.get('cpuSeconds')) and native['cpuSeconds'] == counters[pid],
                'Owned native exit lacks signaled final CPU proof')
        if pid == primary_pid:
            require(type(native.get('objectWaitCode')) is int and native['objectWaitCode'] == 0, 'Primary native exit lacks signaled object proof')
            require(native['exitCode'] == 0 and path_equal(identity['executable'], editor), 'Primary native exit failed')
        proofs[pid] = {'exit': native, 'cpuSeconds': counters[pid]}
    require(primary_pid in proofs, 'Actual primary native exit not proven')
    return proofs


def validate_owned_join(receipt, identities, boundaries):
    require(isinstance(receipt, dict) and type(receipt.get('schemaVersion')) is int and receipt['schemaVersion'] == 1
            and type(receipt.get('timeoutSeconds')) is int and receipt['timeoutSeconds'] == 2
            and receipt.get('status') == 'completed', 'Owned join receipt missing or incomplete')
    entry = utc(receipt.get('apiReturnedUtc')); complete = utc(receipt.get('completedUtc'))
    entry_mono = receipt.get('apiReturnedMonotonic'); complete_mono = receipt.get('completedMonotonic')
    require(finite(entry_mono) and finite(complete_mono) and 0 <= complete_mono-entry_mono <= 2
            and 0 <= complete-entry <= 2 and abs((complete-entry)-(complete_mono-entry_mono)) <= .01
            and receipt['apiReturnedUtc'] == boundaries.get('jobApiReturnedUtc')
            and receipt['completedUtc'] == boundaries.get('jobReturnedUtc') == boundaries.get('ownedCleanupCompletedUtc'),
            'Owned join boundary or bounded clocks differ')
    observations = receipt.get('observations')
    require(isinstance(observations, list) and observations, 'Owned join has no native observations')
    previous_utc = entry; previous_mono = entry_mono; signaled = set()
    for index, observation in enumerate(observations):
        begin = utc(observation.get('beginUtc')); end = utc(observation.get('utc'))
        begin_mono = observation.get('beginMonotonic'); end_mono = observation.get('monotonic')
        require(finite(begin_mono) and finite(end_mono) and previous_utc <= begin <= end <= complete
                and previous_mono <= begin_mono <= end_mono <= complete_mono
                and abs((end-begin)-(end_mono-begin_mono)) <= .01, 'Owned join observation clock is invalid')
        rows = observation.get('processes')
        require(isinstance(rows, list) and len(rows) == len(identities), 'Owned join census does not cover identities')
        seen = set()
        for row in rows:
            pid = row.get('pid'); identity = row.get('identity')
            require(type(pid) is int and pid in identities and pid not in seen and identity == identities[pid]
                    and row.get('querySucceeded') is True and row.get('error') is None
                    and type(row.get('actualPid')) is int and row['actualPid'] == pid
                    and row.get('startedUnix') == identity['startedUnix']
                    and type(row.get('waitCode')) is int and row['waitCode'] in (0, 258)
                    and begin <= utc(row.get('utc')) <= end and finite(row.get('monotonic'))
                    and begin_mono <= row['monotonic'] <= end_mono, 'Owned join identity/wait observation is invalid')
            require(pid not in signaled or row['waitCode'] == 0, 'Owned join process signal reversed')
            if row['waitCode'] == 0: signaled.add(pid)
            seen.add(pid)
        all_signaled = all(row['waitCode'] == 0 for row in rows)
        require(all_signaled == (index == len(observations)-1), 'Owned join skipped first complete object observation')
        previous_utc = end; previous_mono = end_mono
    require(observations[-1]['utc'] == receipt['completedUtc']
            and observations[-1]['monotonic'] == complete_mono, 'Owned join completion differs from native observation')


def derive(snapshots, boundaries, request, variant, policy, controller_pid, wrapper_pid, primary_pid):
    """Recompute lifecycle eligibility solely from retained native snapshots/signals."""
    errors, records = [], []
    require(isinstance(snapshots, list) and len(snapshots) >= 2, 'Missing host snapshots')
    require(type(controller_pid) is int and controller_pid > 0, 'Missing controller PID')
    require(type(wrapper_pid) is int and wrapper_pid > 0, 'Missing wrapper PID')
    require(type(primary_pid) is int and primary_pid > 0, 'Missing primary PID')
    threshold = policy.get('foreignCpuReferencePercent', 3)
    require(finite(threshold) and threshold == 3, 'Unsupported foreign reference')
    ready, start, finished, release = [boundaries.get(key) for key in ('ready', 'start', 'finished', 'release')]
    after_exit = boundaries.get('afterExitUtc')
    job_returned = boundaries.get('jobReturnedUtc')
    ready_observed = boundaries.get('readyObservedUtc')
    finished_observed = boundaries.get('finishedObservedUtc')
    launch_observed = boundaries.get('launchObservedUtc')
    settled_start = boundaries.get('settledBeforeStartUtc')
    held_begin = boundaries.get('finishedHeldBeginUtc')
    held_end = boundaries.get('finishedHeldEndUtc')
    request_sha = boundaries.get('requestSha256')
    require(isinstance(request_sha, str) and len(request_sha) == 64, 'Missing request byte digest')
    for value in (ready, start, finished, release):
        require(isinstance(value, dict) and value.get('runId') == request['runId']
                and value.get('processId') == primary_pid and value.get('requestSha256') == request_sha, 'Wrong lifecycle identity')
    require(ready.get('isCompiling') is False and ready.get('isUpdating') is False, 'Editor readiness is not settled-capable')
    times = [utc(value['utc']) for value in (ready, start, finished, release)]
    require(times == sorted(times) and len(set(times)) == 4, 'Lifecycle order is invalid')
    require(utc(snapshots[0]['utc']) <= utc(launch_observed) <= times[0]
            <= utc(ready_observed) <= utc(settled_start) <= times[1] <= times[2]
            <= utc(finished_observed) <= times[3] <= utc(job_returned) <= utc(after_exit)
            <= utc(snapshots[-1]['utc']), 'Lifecycle lies outside observed lifetime')
    require(times[1] - times[0] < 120 and times[1] - utc(launch_observed) < 120, 'Preparation exceeded bounded wait')
    require(finished.get('completedUtc') is not None and utc(finished['completedUtc']) <= times[2]
            and utc(finished['completedUtc']) >= times[1], 'Finished precedes result completion')
    held_candidates = [(previous['utc'], current['utc']) for previous, current in zip(snapshots, snapshots[1:])
                       if utc(previous['utc']) >= utc(finished_observed) and utc(current['utc']) <= times[3]]
    require(held_candidates and held_candidates[-1] == (held_begin, held_end), 'Finished hold anchor differs from last complete raw interval')
    require(utc(finished['completedUtc']) <= utc(finished_observed) <= utc(held_begin) < utc(held_end) <= times[3], 'Quit bridge overlaps completed work or hold')
    observed_times = [snap.get('utc') for snap in snapshots]
    require(launch_observed in observed_times and ready_observed in observed_times and finished_observed in observed_times, 'Boundary observation has no raw snapshot')
    require(next((snap['utc'] for snap in snapshots if snap.get('observer', {}).get('wrapperPid') == wrapper_pid), None) == launch_observed
            and next((snap['utc'] for snap in snapshots if snap.get('observer', {}).get('primaryPid') == primary_pid), None) == ready_observed, 'Boundary observation precedes identity census')
    identities = retained_owned(snapshots, wrapper_pid)
    validate_owned_join(boundaries.get('ownedJoin'), identities, boundaries)
    require(times[3] <= utc(boundaries.get('jobApiReturnedUtc')) <= utc(job_returned), 'API cleanup entry precedes release')
    candidates = [(index, snapshot) for index, snapshot in enumerate(snapshots)
                  if utc(snapshot.get('censusStartUtc')) >= utc(job_returned)]
    require(candidates and candidates[0][1]['utc'] == after_exit, 'Owned exit boundary is not the first post-job census')
    exit_index, exit_snapshot = candidates[0]
    require(exit_index > 0 and utc(snapshots[exit_index-1]['utc']) <= utc(job_returned)
            <= utc(exit_snapshot['censusStartUtc']), 'Job return lies inside an earlier census')
    census_bounds(exit_snapshot)
    exit_proofs = prove_owned_exit(exit_snapshot, identities, primary_pid, variant['editorExecutable'])
    require(all(proof['exit']['exitedUnix'] <= utc(job_returned) for proof in exit_proofs.values()), 'Native exit follows claimed owned join completion')
    for snapshot in snapshots[exit_index:]:
        require(prove_owned_exit(snapshot, identities, primary_pid, variant['editorExecutable']) == exit_proofs,
                'Owned native exit identity or final CPU changed after exit observation')
    initial = process_table(snapshots[0]['known'])
    require(controller_pid in initial, 'Controller not observed before launch')
    machine_fingerprint = snapshots[0]['observer'].get('machineFingerprint')
    require(isinstance(machine_fingerprint, str) and len(machine_fingerprint) == 64
            and all(c in '0123456789abcdef' for c in machine_fingerprint), 'Host identity unavailable')
    previous_time = None
    primary_identity = validated_primary_identity(snapshots, ready_observed, ready, wrapper_pid, primary_pid, variant['editorExecutable'])
    for index, snap in enumerate(snapshots):
        timestamp = utc(snap['utc'])
        if previous_time is not None: require(timestamp > previous_time, 'Noncontiguous host timeline')
        previous_time = timestamp
        meta = snap.get('observer')
        require(isinstance(meta, dict) and meta.get('controllerPid') == controller_pid and meta.get('machineFingerprint') == machine_fingerprint, 'Missing or changed observer snapshot metadata')
        expected_wrapper = wrapper_pid if timestamp >= utc(launch_observed) else None
        expected_primary = primary_pid if timestamp >= utc(ready_observed) else None
        require(meta.get('wrapperPid') == expected_wrapper and meta.get('primaryPid') == expected_primary, 'Observer identity chronology differs')
        if expected_primary is not None:
            states = snap.get('processHandleStates', {})
            require(isinstance(states, dict), 'Native handle state malformed')
            selected_state = states.get(primary_pid, states.get(str(primary_pid)))
            if selected_state is not None:
                require(isinstance(selected_state, dict), 'Selected native handle state malformed')
                handle_pid = selected_state.get('pid')
                require(handle_pid is None or (type(handle_pid) is int and handle_pid in (0, primary_pid)),
                        'Retained handle PID contradicts selected primary')
                before_wait = selected_state.get('waitBefore'); after_wait = selected_state.get('waitAfter')
                require(type(before_wait) is int and type(after_wait) is int
                        and before_wait in (0, 258) and after_wait in (0, 258)
                        and not (before_wait == 0 and after_wait != 0), 'Selected process object wait proof is invalid')
                if selected_state.get('querySucceeded') is True:
                    counter = cpu_table(snap['cpuCounters']).get(primary_pid)
                    require(selected_state.get('error') is None and type(handle_pid) is int and handle_pid == primary_pid
                            and type(selected_state.get('requestedPid')) is int and selected_state['requestedPid'] == primary_pid
                            and selected_state.get('startedUnix') == primary_identity['startedUnix']
                            and path_equal(selected_state.get('executable'), primary_identity['executable'])
                            and finite(selected_state.get('cpuSeconds')) and selected_state['cpuSeconds'] >= 0
                            and selected_state['cpuSeconds'] == counter,
                            'Successful handle state contradicts anchored primary identity or CPU')
        history = cpu_table(snap.get('processHandleStateHistory', {}))
        for history_pid, earlier_states in history.items():
            require(expected_primary is not None and history_pid == primary_pid
                    and isinstance(earlier_states, list) and len(earlier_states) == 1,
                    'Selected handle state history is malformed')
            earlier = earlier_states[0]
            proof = snap.get('processExits', {}).get(primary_pid, snap.get('processExits', {}).get(str(primary_pid)))
            counter = cpu_table(snap['cpuCounters']).get(primary_pid)
            require(isinstance(earlier, dict) and earlier.get('querySucceeded') is True and earlier.get('error') is None
                    and type(earlier.get('pid')) is int and earlier['pid'] == primary_pid
                    and type(earlier.get('requestedPid')) is int and earlier['requestedPid'] == primary_pid
                    and earlier.get('startedUnix') == primary_identity['startedUnix']
                    and path_equal(earlier.get('executable'), primary_identity['executable'])
                    and type(earlier.get('waitBefore')) is int and type(earlier.get('waitAfter')) is int
                    and earlier['waitBefore'] in (0, 258) and earlier['waitAfter'] in (0, 258)
                    and not (earlier['waitBefore'] == 0 and earlier['waitAfter'] != 0)
                    and finite(earlier.get('cpuSeconds')) and earlier['cpuSeconds'] >= 0
                    and isinstance(proof, dict) and type(proof.get('objectWaitCode')) is int and proof['objectWaitCode'] == 0
                    and proof.get('startedUnix') == primary_identity['startedUnix']
                    and path_equal(proof.get('executable'), primary_identity['executable'])
                    and finite(proof.get('cpuSeconds')) and proof['cpuSeconds'] == counter
                    and earlier['cpuSeconds'] <= counter
                    and (earlier['waitBefore'] != 0 or earlier['cpuSeconds'] == counter),
                    'Earlier selected handle state contradicts terminal identity or CPU')
        known = process_table(snap['known']); live = process_table(snap['processes'])
        for image_pid, image_identity in known.items():
            if 'imageProof' in image_identity:
                validate_image_proof(image_identity['imageProof'], image_pid, image_identity.get('executable'))
        if expected_primary is not None and timestamp < utc(after_exit):
            require(primary_pid in known and primary_pid in descendants(snap['known'], {wrapper_pid}), 'Owned primary identity absent during lifetime')
            identity = known[primary_pid]
            if primary_pid in live:
                require(all(live[primary_pid].get(key) == identity.get(key) for key in ('pid', 'parentPid', 'name', 'executable', 'startedUnix')), 'Live primary identity differs from retained handle')
            require(path_equal(identity.get('executable'), variant['editorExecutable'])
                    and finite(identity.get('startedUnix'))
                    and utc(snapshots[0]['utc']) <= identity['startedUnix'] <= times[0], 'Actual primary executable/start identity differs')
            if primary_identity is None: primary_identity = identity
            require(identity['startedUnix'] == primary_identity['startedUnix'] and path_equal(identity['executable'], primary_identity['executable']), 'Primary PID reused')
            if primary_pid not in live:
                exits = snap.get('processExits', {})
                require(isinstance(exits, dict), 'Native exit evidence malformed')
                native = exits.get(primary_pid, exits.get(str(primary_pid)))
                exited = timestamp >= times[3] and isinstance(native, dict) \
                        and native.get('pid') == primary_pid \
                        and native.get('startedUnix') == primary_identity['startedUnix'] \
                        and path_equal(native.get('executable'), primary_identity['executable']) \
                        and type(native.get('exitCode')) is int and native['exitCode'] == 0 \
                        and finite(native.get('exitedUnix')) and times[3] <= native['exitedUnix'] <= timestamp
                if exited:
                    require(type(native.get('objectWaitCode')) is int and native['objectWaitCode'] == 0,
                            'Primary native exit lacks signaled object proof')
                if not exited:
                    states = snap.get('processHandleStates', {})
                    require(isinstance(states, dict), 'Native handle state malformed')
                    pending = states.get(primary_pid, states.get(str(primary_pid)))
                    counter = cpu_table(snap['cpuCounters']).get(primary_pid)
                    require(index > 0 and utc(snapshots[index-1]['utc']) >= utc(held_end)
                            and utc(snap.get('censusStartUtc')) >= times[3]
                            and timestamp < utc(after_exit) and native is None
                            and isinstance(pending, dict) and pending.get('querySucceeded') is True
                            and pending.get('error') is None and type(pending.get('pid')) is int
                            and pending['pid'] == primary_pid and pending.get('requestedPid') == primary_pid
                            and pending.get('startedUnix') == primary_identity['startedUnix']
                            and path_equal(pending.get('executable'), primary_identity['executable'])
                            and type(pending.get('exitCode')) is int and pending['exitCode'] in (0, 259)
                            and type(pending.get('waitBefore')) is int and pending['waitBefore'] == 258
                            and type(pending.get('waitAfter')) is int and pending['waitAfter'] == 258
                            and pending.get('exitTimeDefined') is False and pending.get('exitedUnix') is None
                            and finite(pending.get('cpuSeconds')) and pending['cpuSeconds'] >= 0
                            and pending['cpuSeconds'] == counter,
                            'Missing primary lacks validated post-release native exit or pending handle state')
        if index:
            prior = snapshots[index-1]
            # The previous endpoint determines which primary was independently observed.
            prior_primary = prior['observer']['primaryPid']
            current_wrapper = meta['wrapperPid']
            row = interval(prior, snap, controller_pid, current_wrapper, prior_primary,
                           variant['editorExecutable'], variant.get('unityExecutable'), classification_primary_pid=primary_pid)
            records.append(row)
            if row['status'] != 'eligible': errors.append('unknown-or-competing-host-interval'); continue
            begin, end = utc(row['startUtc']), utc(row['utc'])
            quit_transition = utc(held_end) <= begin < utc(after_exit) and end > times[3] and end <= utc(after_exit)
            measured = begin < times[2] and end > times[1]
            row['phase'] = 'quit-transition' if quit_transition else 'measurement' if measured else 'unmeasured-preparation' if end <= times[1] and begin < utc(settled_start) else 'outside-measurement'
            row['preparationCpuAnnotation'] = begin < utc(settled_start)
            row['backgroundAnnotation'] = quit_transition
            if begin >= utc(settled_start) and not quit_transition and row['ownedBackgroundCpuPercent'] > .5:
                errors.append('owned-background-load')
            row['foreignExcursion'] = row['foreignCpuPercent'] > threshold
    require(primary_identity is not None, 'Primary identity never observed')
    final_exits = snapshots[-1].get('processExits', {})
    require(isinstance(final_exits, dict), 'Final native exit evidence malformed')
    final_exit = final_exits.get(primary_pid, final_exits.get(str(primary_pid)))
    require(isinstance(final_exit, dict) and type(final_exit.get('objectWaitCode')) is int and final_exit['objectWaitCode'] == 0 and final_exit.get('pid') == primary_pid
            and final_exit.get('startedUnix') == primary_identity['startedUnix']
            and path_equal(final_exit.get('executable'), primary_identity['executable'])
            and type(final_exit.get('exitCode')) is int and final_exit['exitCode'] == 0
            and finite(final_exit.get('exitedUnix')) and times[3] <= final_exit['exitedUnix'] <= utc(after_exit),
            'Final primary native exit proof is missing or invalid')
    def hold(begin, end, quiet_primary=False):
        rows = [r for r in records if utc(r['startUtc']) >= begin and utc(r['utc']) <= end]
        require(rows and any(r['status'] == 'eligible' for r in rows), 'Missing complete lifecycle hold')
        require(all(r['status'] == 'eligible' and r['ownedBackgroundCpuPercent'] <= .5
                    and (not quiet_primary or r['ownedCpuPercent'] + r['ownedBackgroundCpuPercent'] <= .5) for r in rows), 'Ineligible lifecycle hold')
    wrapper_identity = process_table(snapshots[-1]['known']).get(wrapper_pid, {})
    require(finite(wrapper_identity.get('startedUnix')) and utc(snapshots[0]['utc']) <= wrapper_identity['startedUnix'] <= utc(launch_observed), 'Wrapper creation identity unavailable')
    hold(utc(snapshots[0]['utc']), wrapper_identity['startedUnix'])
    preparation = [r for r in records if utc(r['utc']) <= times[1]]
    require(settled_before(preparation, ready_observed) == settled_start, 'Settled before window differs from first qualifying preparation')
    hold(utc(settled_start), times[1], True)
    hold(utc(finished_observed), times[3])
    hold(utc(after_exit), utc(snapshots[-1]['utc']))
    return {'schemaVersion': 4, 'status': 'eligible' if not errors else 'ineligible', 'errors': sorted(set(errors)),
            'profile': 'windows-shared-host-exploratory', 'controllerPid': controller_pid,
            'wrapperPid': wrapper_pid, 'primaryProcessId': primary_pid, 'primaryIdentity': primary_identity,
            'hostFingerprint': machine_fingerprint,
            'intervalCount': len(records), 'foreignExcursions': sum(r.get('foreignExcursion', False) for r in records),
            'boundaries': boundaries, 'intervals': records}


def validate_host(directory, request, variant, policy, execution):
    """Replay native evidence; never trust the stored eligible boolean."""
    directory = Path(directory)
    summary = read_json(directory/'host-summary.json')
    require(type(summary.get('schemaVersion')) is int and summary['schemaVersion'] == 4, 'Unsupported host evidence schema')
    require(isinstance(summary.get('probeFailures', []), list) and not summary.get('probeFailures', []), 'Partial native census failed')
    from .config import digest
    require(execution.get('hostSummaryDigest') == digest(summary), 'Host summary digest differs')
    snapshots_bytes = (directory/'host-snapshots.json').read_bytes()
    intervals_bytes = (directory/'host-intervals.json').read_bytes()
    require(summary.get('snapshotsSha256') == sha(snapshots_bytes)
            and summary.get('intervalsSha256') == sha(intervals_bytes), 'Raw host evidence digest differs')
    require(read_json(directory/'request.json') == request, 'Host request object differs')
    require(summary.get('requestSha256') == sha((directory/'request.json').read_bytes()), 'Host request digest differs')
    require(summary['boundaries'].get('requestSha256') == summary['requestSha256'], 'Lifecycle request digest differs')
    for key, path_key in (('ready', 'readyPath'), ('start', 'startPath'), ('finished', 'finishedPath'), ('release', 'releasePath')):
        require(ntpath.basename(request[path_key]) == key+'.json', 'Unexpected retained signal filename')
        require(read_json(directory/(key+'.json')) == summary['boundaries'][key], 'Retained lifecycle signal differs')
    require(ntpath.basename(request['outputPath']) == 'result.json', 'Unexpected retained result filename')
    result = read_json(directory/'result.json')
    require(result.get('processId') == summary['primaryProcessId'] and result.get('requestSha256') == summary['requestSha256']
            and result.get('completedUtc') == summary['boundaries']['finished'].get('completedUtc'), 'Result lifecycle proof differs')
    require(utc(summary['boundaries']['start']['utc']) <= utc(result['startedUtc'])
            <= utc(result['completedUtc']) <= utc(summary['boundaries']['finished']['utc']), 'Result measurement lies outside lifecycle')
    require(summary.get('wrapperPid') == execution.get('wrapperProcessId', execution.get('processId')), 'Wrapper PID differs')
    snapshots = read_json(directory/'host-snapshots.json')
    replay = derive(snapshots, summary['boundaries'], request, variant, policy,
                    summary['controllerPid'], summary['wrapperPid'], summary['primaryProcessId'])
    require(replay['intervals'] == read_json(directory/'host-intervals.json'), 'Host interval replay differs')
    for key in ('status', 'errors', 'primaryIdentity', 'intervalCount', 'foreignExcursions', 'hostFingerprint'):
        require(replay[key] == summary.get(key), 'Host summary replay differs')
    require(replay['status'] == 'eligible', 'Independent host evidence is ineligible')
    return {key: value for key, value in replay.items() if key != 'intervals'}


class HostSession:
    """Native census with complete, independently replayable lifecycle holds."""
    def __init__(self, request, variant, directory, policy, probe=None):
        self.request, self.variant, self.directory, self.policy = request, variant, Path(directory), policy
        self.probe = probe or WindowsProbe()
        self.controller = os.getpid()
        self.machine_fingerprint = sha(canonical({'node': platform.node(), 'system': platform.system(), 'machine': platform.machine(), 'processorCount': self.probe.cpu_count}))
        self.request_sha = sha((self.directory/'request.json').read_bytes())
        self.wrapper = self.primary = None
        self.previous = None; self.records = []; self.snapshots = []
        self.ready = self.finished = self.start = self.release = None
        self.ready_observed = self.finished_observed = self.launch_observed = None
        self.errors = []; self.after_exit = None; self.job_returned = None; self.job_api_returned = None; self.owned_join = None; self.next_sample = 0
        self.settled_start = None; self.preparation_deadline = None
        self.finished_held_begin = self.finished_held_end = None

    def sample(self, force=False):
        if not force and time.monotonic() < self.next_sample: return
        self.probe.primary_editor_pid = self.primary
        self.probe.owned_root_pid = self.wrapper
        current = self.probe.snapshot()
        current['observer'] = {'controllerPid': self.controller, 'wrapperPid': self.wrapper, 'primaryPid': self.primary, 'machineFingerprint': self.machine_fingerprint}
        self.snapshots.append(current)
        if self.previous is not None:
            self.records.append(interval(self.previous, current, self.controller, self.wrapper,
                                         self.previous['observer']['primaryPid'], self.variant['editorExecutable'],
                                         self.variant.get('unityExecutable')))
        self.previous = current; self.next_sample = time.monotonic() + .5

    def before(self):
        self.sample(True); time.sleep(.5); self.sample(True)
        require(self.records[-1]['status'] == 'eligible' and self.records[-1]['ownedBackgroundCpuPercent'] <= .5,
                'Prelaunch host evidence is unknown or competing')

    def on_started(self, pid):
        self.wrapper = pid; self.preparation_deadline = time.monotonic() + 120
        self.sample(True); self.launch_observed = self.previous['utc']

    def read_signal(self, key):
        path = Path(self.request[key])
        if not path.exists(): return None
        value = read_json(path)
        require(value.get('runId') == self.request['runId'] and value.get('requestSha256') == self.request_sha
                and type(value.get('processId')) is int and value['processId'] > 0, 'Wrong lifecycle identity')
        require(utc(value['utc']) <= datetime.datetime.now(datetime.timezone.utc).timestamp(), 'Future lifecycle timestamp')
        # Ensure signal identity/time is backed by a census endpoint at or after it.
        if utc(value['utc']) > utc(self.previous['utc']): self.sample(True)
        require(utc(value['utc']) <= utc(self.previous['utc']), 'Lifecycle newer than host observation')
        return value

    def on_poll(self):
        self.sample()
        if self.start is None and self.preparation_deadline is not None and time.monotonic() >= self.preparation_deadline:
            raise TimeoutError('Editor preparation exceeded bounded wait')
        if self.ready is None:
            ready = self.read_signal('readyPath')
            if ready is not None:
                pid = ready['processId']; known = self.previous['known']; table = process_table(known)
                validated_primary_identity(self.snapshots, self.previous['utc'], ready, self.wrapper, pid, self.variant['editorExecutable'])
                require(ready.get('isCompiling') is False and ready.get('isUpdating') is False, 'Editor readiness flags are invalid')
                self.primary = pid; self.ready = ready; self.ready_observed = self.previous['utc']
                remaining = 120 - (datetime.datetime.now(datetime.timezone.utc).timestamp() - utc(ready['utc']))
                require(remaining > 0, 'Preparation readiness is stale')
                self.preparation_deadline = min(self.preparation_deadline, time.monotonic() + remaining)
                self.previous['observer']['primaryPid'] = pid
        if self.ready is not None and self.start is None:
            if time.monotonic() >= self.preparation_deadline:
                raise TimeoutError('Editor preparation exceeded bounded wait')
            historical = [interval(previous, current, self.controller, current['observer']['wrapperPid'],
                                   previous['observer']['primaryPid'], self.variant['editorExecutable'],
                                   self.variant.get('unityExecutable'), classification_primary_pid=self.primary)
                          for previous, current in zip(self.snapshots, self.snapshots[1:])]
            # Unknown ownership/census, competing work and clock gaps are hard for
            # the entire observed lifetime. Bootstrap compiler CPU is retained as
            # unmeasured preparation and cannot replace a timed observation.
            require(all(row['status'] == 'eligible' for row in historical), 'Unknown or competing startup host evidence')
            settled = settled_before(historical, self.ready_observed)
            if settled is not None:
                self.settled_start = settled
                self.start = signal(self.request['startPath'], self.request, self.primary, self.request_sha)
        if self.start is not None and self.finished is None:
            finished = self.read_signal('finishedPath')
            if finished is not None:
                require(finished['processId'] == self.primary and utc(finished['utc']) >= utc(self.start['utc']), 'Wrong finished PID/time')
                result = read_json(self.request['outputPath'])
                require(result.get('processId') == self.primary and result.get('status') == 'completed'
                        and result.get('requestSha256') == self.request_sha
                        and result.get('completedUtc') == finished.get('completedUtc'), 'Finished before matching complete result')
                self.finished = finished; self.finished_observed = self.previous['utc']
        if self.finished is not None and self.release is None:
            held = [r for r in self.records if utc(r['startUtc']) >= utc(self.finished_observed)]
            if held:
                require(all(r['status'] == 'eligible' and r['ownedBackgroundCpuPercent'] <= .5 for r in held), 'Completion hold is not eligible')
                self.finished_held_begin = held[-1]['startUtc']; self.finished_held_end = held[-1]['utc']
                self.release = signal(self.request['releasePath'], self.request, self.primary, self.request_sha)

    def on_finished(self):
        self.job_api_returned = datetime.datetime.now(datetime.timezone.utc).isoformat()
        started = time.monotonic()
        self.owned_join = {'schemaVersion': 1, 'timeoutSeconds': 2, 'apiReturnedUtc': self.job_api_returned,
                           'apiReturnedMonotonic': started, 'completedUtc': None, 'completedMonotonic': None,
                           'status': 'pending', 'observations': []}
        signaled = set()
        while True:
            observation = self.probe.owned_join_observation()
            self.owned_join['observations'].append(observation)
            if not observation['processes'] or not all(row['querySucceeded'] for row in observation['processes']):
                self.owned_join['status'] = 'invalid'
                raise ValueError('Owned object join identity/query failure')
            if any(row['pid'] in signaled and row['waitCode'] != 0 for row in observation['processes']):
                self.owned_join['status'] = 'invalid'
                raise ValueError('Owned object join signal reversed')
            signaled.update(row['pid'] for row in observation['processes'] if row['waitCode'] == 0)
            if observation['monotonic'] - started > 2:
                self.owned_join['status'] = 'timeout'
                raise TimeoutError('Owned object join exceeded two seconds')
            if all(row['waitCode'] == 0 for row in observation['processes']):
                self.job_returned = observation['utc']
                self.owned_join.update(status='completed', completedUtc=self.job_returned,
                                       completedMonotonic=observation['monotonic'])
                break
            # Observe all shutdown CPU while joining; this checks object
            # termination only, never CPU quietness or a favorable census.
            self.sample(); time.sleep(min(.05, max(0, 2-(time.monotonic()-started))))
        self.sample(True)
        require(utc(self.previous.get('censusStartUtc')) >= utc(self.job_returned), 'Exit census started before owned cleanup completed')
        census_bounds(self.previous)
        identities = retained_owned(self.snapshots, self.wrapper)
        prove_owned_exit(self.previous, identities, self.primary, self.variant['editorExecutable'])
        self.after_exit = self.previous['utc']
        time.sleep(.5); self.sample(True)

    def finish(self):
        try:
            boundaries = {'ready': self.ready, 'start': self.start, 'finished': self.finished,
                          'release': self.release, 'afterExitUtc': self.after_exit, 'jobReturnedUtc': self.job_returned,
                          'jobApiReturnedUtc': self.job_api_returned, 'ownedCleanupCompletedUtc': self.job_returned, 'ownedJoin': self.owned_join, 'readyObservedUtc': self.ready_observed,
                          'finishedObservedUtc': self.finished_observed, 'launchObservedUtc': self.launch_observed,
                          'requestSha256': self.request_sha, 'settledBeforeStartUtc': self.settled_start,
                          'finishedHeldBeginUtc': self.finished_held_begin, 'finishedHeldEndUtc': self.finished_held_end}
            try:
                summary = derive(self.snapshots, boundaries, self.request, self.variant, self.policy,
                                 self.controller, self.wrapper, self.primary)
                records = summary.pop('intervals')
            except (ValueError, KeyError, TypeError, OverflowError) as error:
                summary = {'schemaVersion': 4, 'status': 'ineligible', 'errors': ['invalid-host-lifecycle-' + type(error).__name__],
                           'controllerPid': self.controller, 'wrapperPid': self.wrapper, 'primaryProcessId': self.primary,
                           'boundaries': boundaries}
                records = self.records
            summary['probeFailures'] = list(getattr(self.probe, 'probe_failures', []))
            if summary['probeFailures']:
                summary['status'] = 'ineligible'
                summary['errors'] = sorted(set(summary['errors'] + ['partial-native-probe-failure']))
            summary['requestSha256'] = self.request_sha
            write(self.directory/'host-snapshots.json', self.snapshots)
            write(self.directory/'host-intervals.json', records)
            summary['snapshotsSha256'] = sha((self.directory/'host-snapshots.json').read_bytes())
            summary['intervalsSha256'] = sha((self.directory/'host-intervals.json').read_bytes())
            write(self.directory/'host-summary.json', summary)
            return summary
        finally:
            self.probe.close()
