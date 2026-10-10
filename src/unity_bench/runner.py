"""Execute fixed campaign plans in owned detached worktrees without a shell."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from .config import digest, read, read_json, require, schedule
from .evidence import machine_lock, write
from .report import summarize, validate_result
from .process import launch
from .host import HostSession


def git(repository, *args):
    return subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True, timeout=60).stdout.decode('utf-8').strip()


def command(argv, fields):
    return [token.format_map(fields) for token in argv]


def plan(path):
    config = read(path)
    for variant in config['variants']:
        require(git(config['repository'], 'rev-parse', variant['revision'] + '^{commit}') == variant['revision'], 'Revision is not an available exact commit')
    package = Path(__file__).resolve().parent
    files = {'python/' + str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(package.rglob('*.py')) if '__pycache__' not in p.parts}
    # Installed Python wheels contain only this package: never scan site-packages or the host's Python library.
    unity_source = package.parent.parent / 'packages/com.tesselworks.unity-bench' if package.parent.name == 'src' else None
    if unity_source is not None and unity_source.is_dir():
        files.update({'unity/' + str(p.relative_to(unity_source)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in sorted(unity_source.rglob('*')) if p.is_file() and p.suffix in ('.cs', '.json', '.asmdef')})
    return {'schemaVersion': 1, 'config': config, 'planDigest': digest(config), 'schedule': schedule(config),
            'frameworkSourceDigest': digest(files), 'frameworkSourceFiles': files,
            'frameworkSourceScope': 'python-and-unity-package' if unity_source is not None and unity_source.is_dir() else 'python-package-only'}


def run(config_path, destination):
    require(os.name in ('posix', 'nt'), 'Owned process launch is unavailable on this platform')
    # Coordinates participating campaigns only, never a declaration that the host is quiet.
    with machine_lock(Path(tempfile.gettempdir()) / 'tesselworks-unity-bench.lock'):
        return _run(config_path, destination)


def _run(config_path, destination):
    frozen = plan(config_path)
    destination = Path(destination).resolve()
    require(not destination.exists(), 'Output directory already exists; evidence cannot be overwritten')
    destination.mkdir(parents=True)
    write(destination / 'plan.json', frozen)
    config = frozen['config']
    policy = config.get('host', {'mode': 'none'})
    controlled = policy['mode'] == 'windows-shared-host'
    require(not controlled or os.name == 'nt', 'Windows host observation requires Windows')
    variants = {v['id']: v for v in config['variants']}
    workloads = {w['id']: w for w in config['workloads']}
    checkouts = {}
    # Checkouts/Library are retained for audit. Removal is an explicit operator action after evidence preservation.
    for variant in config['variants']:
        checkout = destination / 'checkouts' / variant['id']
        checkout.parent.mkdir(exist_ok=True)
        git(config['repository'], 'worktree', 'add', '--detach', str(checkout), variant['revision'])
        checkouts[variant['id']] = checkout
    contexts = []
    for row in frozen['schedule']:
        variant, workload = variants[row['variant']], workloads[row['workload']]
        checkout = checkouts[row['variant']]
        project = (checkout / workload['projectPath']).resolve()
        directory = destination / 'runs' / ('%04d' % row['index']); directory.mkdir(parents=True)
        request = {'schemaVersion': 1, 'runId': str(uuid.uuid4()), 'variant': row['variant'], 'workload': row['workload'],
                   'outputPath': str(directory / 'result.json'), 'warmIterations': workload.get('warmIterations', 3),
                   'clockMode': workload['mode'], 'sourceRevision': variant['revision'], 'planDigest': frozen['planDigest']}
        if 'measuredWorkerRoles' in workload:
            request['measuredWorkerRoles'] = list(workload['measuredWorkerRoles'])
        if controlled:
            request.update({key+'Path': str(directory/(key+'.json')) for key in ('ready', 'start', 'finished', 'release')})
            request['handshakeTimeoutSeconds'] = 120
        write(directory / 'request.json', request)
        fields = {'checkout': str(checkout), 'project': str(project), 'unity': variant.get('unityExecutable', ''),
                  'request': str(directory / 'request.json'), 'result': request['outputPath'], 'variant': row['variant'], 'workload': row['workload'],
                  'unityVersion': variant['unityVersion'], 'backend': variant['backend'], 'revision': variant['revision']}
        contexts.append((row, variant, workload, checkout, project, directory, request, fields))
    # Prepare every variant before any calibration/comparison process. A later
    # candidate compile failure must not leave an already timed baseline subset.
    prepared = set()
    for row, variant, workload, checkout, project, directory, request, fields in contexts:
        identity = row['variant'], row['workload']
        if identity in prepared: continue
        try:
            require(project.is_relative_to(checkout.resolve()) and project.is_dir(), 'Project missing or escaped owned checkout')
            for argv in workload.get('prepare', []):
                code, _, _ = launch(command(argv, fields), checkout, workload.get('timeoutSeconds', 300))
                require(code == 0, 'Preparation returned nonzero exit')
            write(destination/'prepared'/row['variant']/(row['workload']+'.json'),
                  {'variant': row['variant'], 'workload': row['workload'], 'completed': True})
            prepared.add(identity)
        except (OSError, ValueError, KeyError, RuntimeError, TimeoutError) as error:
            write(directory/'execution.json', {'status': 'failed', 'kind': row['kind'], 'stage': 'preparation',
                  'hostObservation': 'unavailable', 'requestDigest': digest(request), 'error': type(error).__name__})
            return complete_report(destination)
    for row, variant, workload, checkout, project, directory, request, fields in contexts:
        execution = {'status': 'failed', 'kind': row['kind'], 'hostObservation': 'unavailable', 'requestDigest': digest(request)}
        host = None
        try:
            require(project.is_relative_to(checkout.resolve()) and project.is_dir(), 'Project missing or escaped owned checkout')
            if controlled:
                host = HostSession(request, variant, directory, policy)
                host.before()
            code, elapsed, pid = launch(command(workload['command'], fields), checkout, workload.get('timeoutSeconds', 300),
                                        on_started=host.on_started if host else None,
                                        on_poll=host.on_poll if host else None,
                                        on_finished=host.on_finished if host else None)
            execution.update(exitCode=code, processId=pid, wrapperProcessId=pid, wholeProcessWallMs=elapsed)
            require(code == 0, 'Adapter returned nonzero exit')
            result = validate_result(read_json(request['outputPath']), request, variant)
            if controlled:
                require(result.get('requestSha256') == hashlib.sha256((directory/'request.json').read_bytes()).hexdigest(), 'Result request bytes differ')
                require(result.get('processId') == host.primary, 'Result actual Editor PID differs')
            execution['resultDigest'] = digest(result)
            if workload['mode'] == 'external':
                # Explicitly whole invocation: includes startup, adapter output and shutdown, not an import-only clock.
                execution['externalMetric'] = {'metric': 'whole-process-wall', 'value': elapsed, 'unit': 'ms'}
            execution['status'] = 'completed'
        except (OSError, ValueError, KeyError, RuntimeError, TimeoutError, json.JSONDecodeError) as error:
            # Error types are safe; arbitrary adapter/Unity strings never enter public reports.
            execution['error'] = type(error).__name__
        finally:
            if host is not None:
                summary = host.finish()
                execution.update(hostObservation=summary['status'], hostSummaryDigest=digest(summary),
                                 wrapperProcessId=host.wrapper, primaryProcessId=host.primary)
        write(directory / 'execution.json', execution)
        if execution.get('error') == 'RuntimeError' or (controlled and (execution.get('error') or execution.get('hostObservation') != 'eligible')):
            # Lifecycle/cleanup uncertainty must not launch another process on this host.
            break
    return complete_report(destination)


def complete_report(destination):
    report = summarize(destination)
    write(destination / 'report.json', report)
    write(destination / 'public-report.json', report)
    return report
