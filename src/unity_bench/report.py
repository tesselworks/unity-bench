"""Read immutable results; retain rejection reasons and process-level distributions."""
import json
import hashlib
import math
from pathlib import Path
import statistics
from .config import digest, read_json, require, schedule, validate
from .host import validate_host, utc


def validate_result(result, request, variant):
    require(isinstance(result, dict) and type(result.get('schemaVersion')) is int and result['schemaVersion'] == 1, 'Unsupported result schema')
    for key in ('runId', 'variant', 'workload'):
        require(result.get(key) == request[key], 'Stale or wrong result identity: ' + key)
    require(result.get('status') == 'completed', 'Adapter did not complete')
    if request.get('readyPath'):
        require(type(result.get('processId')) is int and result['processId'] > 0, 'Actual Editor PID missing')
        for key in ('sourceRevision', 'planDigest'):
            require(result.get(key) == request[key], 'Result provenance differs')
        require(utc(result['completedUtc']) >= utc(result['startedUtc']), 'Invalid actual work boundaries')
    environment = result.get('environment')
    require(isinstance(environment, dict), 'Actual environment missing')
    for key in ('unityVersion', 'backend'):
        require(environment.get(key) == variant[key], 'Actual environment differs: ' + key)
    if request.get('readyPath'):
        settings = environment.get('settings', {})
        require(environment.get('executionKind') == 'editor-batch' and settings.get('batchMode') is True,
                'Controlled lifecycle requires actual batch Editor')
        require(settings.get('jobWorkerCountAvailable') is True and type(settings.get('jobWorkerCount')) is int and settings['jobWorkerCount'] > 0,
                'Actual job worker count unavailable')
        require(settings.get('codeOptimizationAvailable') is True and settings.get('codeOptimization') in ('Debug', 'Release'),
                'Actual code optimization unavailable')
    for key in ('inputFingerprint', 'outputFingerprint'):
        require(isinstance(result.get(key), str) and len(result[key]) == 64 and all(c in '0123456789abcdef' for c in result[key]), 'Invalid ' + key)
    counters = result.get('workCounters')
    require(isinstance(counters, dict) and counters, 'Work counters missing')
    for key, value in counters.items():
        require(isinstance(key, str) and key and type(value) in (int, float) and math.isfinite(value) and value >= 0, 'Invalid work counter')
    samples = result.get('samples')
    require(isinstance(samples, list), 'Samples must be an array')
    if request.get('clockMode') == 'external' and not samples:
        return result
    require(samples, 'No measured samples')
    seen, metrics = set(), set()
    for sample in samples:
        require(isinstance(sample, dict), 'Malformed sample')
        iteration = sample.get('iteration')
        require(type(iteration) is int and 0 <= iteration <= request['warmIterations'], 'Invalid iteration')
        require(sample.get('phase') == ('first' if iteration == 0 else 'warm'), 'Wrong phase')
        require(isinstance(sample.get('metric'), str) and sample['metric'], 'Metric missing')
        require(sample.get('unit') == 'ms', 'Unsupported metric unit')
        value = sample.get('value')
        require(type(value) in (int, float) and math.isfinite(value) and value >= 0, 'Nonfinite or negative time')
        identity = sample['metric'], iteration
        require(identity not in seen, 'Duplicate sample')
        seen.add(identity); metrics.add(sample['metric'])
    require(seen == {(metric, iteration) for metric in metrics for iteration in range(request['warmIterations'] + 1)}, 'Incomplete metric/round coverage')
    return result


def process_metrics(result):
    values = {}
    for sample in result['samples']:
        values.setdefault((sample['metric'], sample['phase']), []).append(sample['value'])
    return {metric + '/' + phase: statistics.median(samples) for (metric, phase), samples in values.items()}


def distribution(values):
    return {'count': len(values), 'medianMs': statistics.median(values), 'minMs': min(values), 'maxMs': max(values)} if values else None


def measured_metrics(result, execution, workload):
    if workload['mode'] != 'external': return process_metrics(result)
    value = execution.get('externalMetric', {}).get('value')
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0, 'Invalid external whole-process clock')
    return {'whole-process-wall/process': value}


def summarize(folder):
    folder = Path(folder)
    plan = read_json(folder / 'plan.json')
    config, rows = plan['config'], plan['schedule']
    validate(config)
    require(plan.get('planDigest') == digest(config) and rows == schedule(config), 'Campaign plan was changed')
    source_files = plan.get('frameworkSourceFiles')
    require(isinstance(source_files, dict) and source_files and
            all(isinstance(name, str) and name and isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
                for name, value in source_files.items()) and plan.get('frameworkSourceDigest') == digest(source_files)
            and plan.get('frameworkSourceScope') in ('python-and-unity-package', 'python-package-only'), 'Framework source provenance is missing or changed')
    evidence = []
    for row in rows:
        path = folder / 'runs' / ('%04d' % row['index']) / 'execution.json'
        try:
            execution = read_json(path)
            require(isinstance(execution, dict), 'Malformed execution')
        except (OSError, ValueError):
            execution = {'status': 'missing', 'error': 'Missing or invalid execution record'}
        result = None
        if execution.get('status') == 'completed':
            directory = path.parent
            variant = next(v for v in config['variants'] if v['id'] == row['variant'])
            workload = next(w for w in config['workloads'] if w['id'] == row['workload'])
            try:
                request = read_json(directory / 'request.json')
                require(isinstance(request, dict), 'Request must be an object')
                require(request.get('variant') == row['variant'] and request.get('workload') == row['workload'], 'Request differs from frozen plan')
                require(request.get('planDigest') == plan['planDigest'] and request.get('sourceRevision') == variant['revision'] and
                        request.get('clockMode') == workload['mode'] and type(request.get('warmIterations')) is int and
                        request['warmIterations'] == workload.get('warmIterations', 3), 'Request measurement contract was changed')
                require(execution.get('requestDigest') == digest(request), 'Request bytes do not match retained execution')
                require(execution.get('exitCode') == 0, 'Nonzero adapter exit')
                candidate = read_json(directory / 'result.json')
                require(execution.get('resultDigest') == digest(candidate), 'Result differs from retained execution')
                result = validate_result(candidate, request, variant)
                if workload['mode'] == 'external':
                    measured_metrics(result, execution, workload)
                if config.get('host', {}).get('mode') == 'windows-shared-host':
                    require(candidate.get('requestSha256') == hashlib.sha256((directory/'request.json').read_bytes()).hexdigest(), 'Request bytes differ from actual Editor receipt')
                    host = validate_host(directory, request, variant, config['host'], execution)
                    execution = {**execution, 'validatedHost': host}
            except (ValueError, KeyError, OSError, json.JSONDecodeError):
                execution = {'status': 'rejected', 'error': 'Retained adapter evidence is invalid'}
                result = None
        evidence.append((row, execution, result))
    comparisons = []
    for axis in config['axes']:
        for workload in config['workloads']:
            selected = [(row, execution, result) for row, execution, result in evidence if row['axis'] == axis['id'] and row['workload'] == workload['id']]
            reasons = []
            if any(result is None for _, _, result in selected):
                reasons.append('incomplete-or-rejected-process')
            results = [result for _, _, result in selected if result is not None]
            if results:
                for key in ('inputFingerprint', 'outputFingerprint', 'workCounters'):
                    if any(result[key] != results[0][key] for result in results):
                        reasons.append('unequal-' + key)
                settings = [result['environment'].get('settings') for result in results]
                if not settings[0] or any(value != settings[0] for value in settings):
                    reasons.append('missing-or-unequal-settings')
                coverage = [{(sample['metric'], sample['phase'], sample['iteration']) for sample in result['samples']} for result in results]
                if any(value != coverage[0] for value in coverage):
                    reasons.append('unequal-metric-coverage')
            controlled = config.get('host', {}).get('mode') == 'windows-shared-host'
            if not controlled:
                reasons.append('independent-host-observer-disabled')
            elif any(execution.get('validatedHost', {}).get('status') != 'eligible' for _, execution, _ in selected):
                reasons.append('ineligible-independent-host-evidence')
            fingerprints = {execution.get('validatedHost', {}).get('hostFingerprint') for _, execution, _ in selected}
            if controlled and (None in fingerprints or len(fingerprints) != 1):
                reasons.append('missing-or-unequal-host')
            calibrations = [(row, execution, result) for row, execution, result in evidence
                            if row['kind'] == 'calibration' and row['workload'] == workload['id'] and row['variant'] in (axis['left'], axis['right'])]
            if controlled:
                if config.get('repetitions', 3) < 3: reasons.append('insufficient-comparison-processes')
                if config.get('calibrationRepetitions', 3) < 3 or any(result is None or execution.get('validatedHost', {}).get('status') != 'eligible' for _, execution, result in calibrations):
                    reasons.append('incomplete-noise-calibration')
                for _, calibrated_execution, calibrated in calibrations:
                    if calibrated is not None and results and any(calibrated[key] != results[0][key] for key in ('inputFingerprint', 'outputFingerprint', 'workCounters')):
                        reasons.append('unequal-calibration-work')
                    if calibrated is not None and results and calibrated['environment'].get('settings') != results[0]['environment'].get('settings'):
                        reasons.append('unequal-calibration-settings')
                    if calibrated_execution.get('validatedHost', {}).get('hostFingerprint') not in fingerprints:
                        reasons.append('unequal-calibration-host')
                    if calibrated is not None and results and {(s['metric'], s['phase'], s['iteration']) for s in calibrated['samples']} != coverage[0]:
                        reasons.append('unequal-calibration-metric-coverage')
            grouped = {side: {} for side in (axis['left'], axis['right'])}
            for row, execution, result in selected:
                if result is not None:
                    metrics = process_metrics(result)
                    if workload['mode'] == 'external':
                        external = execution.get('externalMetric', {})
                        value = external.get('value')
                        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                            reasons.append('invalid-external-clock')
                            continue
                        metrics = {'whole-process-wall/process': value}
                    for metric, value in metrics.items():
                        grouped[row['variant']].setdefault(metric, []).append(value)
            ratios = []
            if not reasons:
                metrics_by_pair = {(row['pair'], row['variant']): measured_metrics(result, execution, workload) for row, execution, result in selected}
                # First and warm are separate; each warm observation is one process median.
                for metric in sorted(grouped[axis['left']]):
                    left, right = grouped[axis['left']][metric], grouped[axis['right']].get(metric, [])
                    if len(left) != config.get('repetitions', 3) or len(right) != len(left) or min(left+right) <= 0:
                        reasons.append('incomplete-or-zero-comparison-metric'); continue
                    pairs = [{'pair': pair, 'leftMs': metrics_by_pair[(pair, axis['left'])][metric],
                              'rightMs': metrics_by_pair[(pair, axis['right'])][metric]} for pair in range(len(left))]
                    noise = {}
                    for side in (axis['left'], axis['right']):
                        values = [measured_metrics(result, execution, workload)[metric] for row, execution, result in calibrations if row['variant'] == side]
                        noise[side] = max(values)-min(values) if values else None
                    envelope = max(noise.values())
                    deltas = [pair['rightMs']-pair['leftMs'] for pair in pairs]
                    verdict = 'unresolved'
                    if min(right) > max(left) and all(delta > envelope for delta in deltas): verdict = 'right-slower-beyond-observed-spread'
                    elif max(right) < min(left) and all(-delta > envelope for delta in deltas): verdict = 'right-faster-beyond-observed-spread'
                    ratios.append({'metric': metric, 'rightOverLeft': statistics.median(right)/statistics.median(left),
                                   'pairedProcesses': pairs, 'calibrationRangeMs': noise, 'verdict': verdict})
                if reasons: ratios = []
            comparisons.append({'axis': axis['id'], 'workload': workload['id'], 'status': 'controlled-exploratory' if not reasons else 'diagnostic', 'comparable': not reasons,
                                'adoptedProcesses': len(selected) if not reasons else 0, 'reasons': sorted(set(reasons)), 'descriptiveProcessDistributions':
                                {side: {metric: distribution(values) for metric, values in metrics.items()} for side, metrics in grouped.items()},
                                'ratios': ratios, 'verdict': 'Exploratory paired direction against observed calibration spread' if not reasons else 'No controlled speed verdict; diagnostic process distributions only'})
    calibration = {}
    for row, execution, result in evidence:
        if row['kind'] == 'calibration' and result is not None:
            workload = next(w for w in config['workloads'] if w['id'] == row['workload'])
            metrics = process_metrics(result)
            if workload['mode'] == 'external':
                value = execution.get('externalMetric', {}).get('value')
                metrics = {'whole-process-wall/process': value} if type(value) in (int, float) and math.isfinite(value) and value >= 0 else {}
            for metric, value in metrics.items():
                calibration.setdefault(row['workload'] + '/' + row['variant'] + '/' + metric, []).append(value)
    hosts = [execution['validatedHost'] for _, execution, _ in evidence if execution.get('validatedHost', {}).get('status') == 'eligible']
    host_evidence = {'eligibleProcesses': len(hosts), 'observedIntervals': sum(host.get('intervalCount', 0) for host in hosts),
                     'foreignCpuExcursions': sum(host.get('foreignExcursions', 0) for host in hosts),
                     'foreignCpuReferencePercent': 3 if hosts else None, 'ownedBackgroundCpuLimitPercent': .5 if hosts else None}
    return {'schemaVersion': 1, 'campaign': config['name'], 'planDigest': plan['planDigest'], 'frameworkSourceDigest': plan.get('frameworkSourceDigest'), 'plannedProcesses': len(rows),
            'completedProcesses': sum(result is not None for _, _, result in evidence), 'hostObservation': 'windows-shared-host-exploratory' if config.get('host', {}).get('mode') == 'windows-shared-host' else 'unavailable',
            'hostEvidence': host_evidence,
            'calibration': {key: distribution(values) for key, values in calibration.items()}, 'comparisons': comparisons,
            'failures': [{'index': row['index'], 'status': execution['status'], 'error': execution.get('error', '')}
                         for row, execution, result in evidence if result is None],
            'limitations': ['Shared-host foreign CPU excursions are retained annotations, not evidence of an isolated machine.',
                            'No significance, pure runtime attribution or automatic upgrade recommendation.',
                            'Initial import, reload and Play transition adapters are not implemented.']}
