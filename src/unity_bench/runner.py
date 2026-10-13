"""Run a campaign: fixed schedule, fresh process per repeat, results appended to results.jsonl.

Nothing in here refuses to measure. Host load is sampled and stored next to each process; a
failed or timed-out process is recorded and the schedule continues.
"""
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from . import __version__, config, host_sampling, process, provenance, reload_log, workspace
from .report import write_reports
from .result import ResultError, read_result  # noqa: F401  (ResultError is part of this module's interface)
from .summary import read_rows

BATCH_METHOD = 'UnityBench.Editor.BatchEntry.Run'
EDITOR_ARGS = ['-batchmode', '-nographics']
PLAYER_KINDS = ('player.startup', 'player.adapter')
LOGGED_KINDS = ('editor.script_edit', 'editor.play', 'editor.adapter')   # kinds whose Editor.log has reload blocks to parse


class Campaign:
    def __init__(self, cfg, out):
        self.cfg, self.out = cfg, Path(out)
        self.variants = {v['id']: v for v in cfg['variants']}
        self.workloads = {w['id']: w for w in cfg['workloads']}
        self.projects, self.worktrees = {}, {}
        self.builds = {}   # (build key, variant id) -> player executable; see build_key
        self.build_settings = {}   # same keys -> the player build settings the Editor reported
        self.results = None

    def write(self, row):
        self.results.write(json.dumps(row, allow_nan=False) + '\n')
        self.results.flush()

    # ---- one scheduled process ----------------------------------------------------------------------------------

    def execute(self, row, log):
        w, v = self.workloads[row['workload']], self.variants[row['variant']]
        project = self.projects[v['id']]
        stem = f"{row['index']:04d}-{w['id']}-{v['id']}"
        base = {'index': row['index'], 'workload': w['id'], 'kind': w['kind'], 'variant': v['id'], 'repeat': row['repeat']}
        saved = workspace.snapshot(project, [self.target_file(t, v) for t in w.get('targets', [])] + ['ProjectSettings/EditorSettings.asset'])
        sampler = host_sampling.Sampler()
        started = time.time()
        outcome = new_outcome()
        try:
            if w['kind'] in ('editor.cold_open', 'editor.warm_open'):
                self.open_project(w, v, project, row, stem, sampler, outcome)
            elif w['kind'] in PLAYER_KINDS:
                self.player_run(w, v, project, row, stem, sampler, outcome)
            else:
                self.batch(w, v, project, row, stem, sampler, outcome)
        except Exception as error:   # one bad process, result file or adapter must never stop the schedule
            outcome['error'] = outcome['error'] or f'{type(error).__name__}: {error}'
        restored = workspace.restore(project, saved)
        status = 'ok' if outcome['error'] is None else 'timeout' if outcome['timed_out'] else 'failed'
        self.write({'type': 'process', **base, 'status': status, 'error': outcome['error'], 'exit_code': outcome['exit_code'],
                    'wall_ms': outcome['wall_ms'], 'environment': outcome['environment'], 'notes': outcome['notes'], 'build': outcome['build'],
                    'headline': outcome['headline'], 'cleanup': outcome['cleanup'], 'host': sampler.summary(), 'restored_files': restored, 'started_at': datetime.datetime.fromtimestamp(started).isoformat(timespec='seconds'),
                    'log': f'logs/{stem}.log' if log.exists() else None})
        if status == 'ok':
            for s in outcome['samples']:
                self.write({'type': 'sample', **base, 'name': s['name'], 'phase': s['phase'], 'ms': s['ms'], 'round': s.get('round', 0),
                            'info': s.get('info') or {}, 'counters': s.get('counters') or None, 'hash': s.get('hash'), 'work_group': s.get('work_group')})
            for group in outcome['reloads']:
                self.write({'type': 'reload', **base, **group})
        return status

    def editor_command(self, v, project, log, extra):
        editor = [v['editor']] if isinstance(v['editor'], str) else list(v['editor'])
        return [*editor, *EDITOR_ARGS, '-projectPath', str(project), '-logFile', str(log), *extra]

    def finish_run(self, run, outcome):
        outcome.update(exit_code=run.exit_code, wall_ms=run.wall_ms, timed_out=run.timed_out, cleanup=run.cleanup)
        if run.timed_out:
            outcome['error'] = 'timeout; process tree killed'
        elif run.exit_code != 0:
            outcome['error'] = f'exit code {run.exit_code}'

    def open_project(self, w, v, project, row, stem, sampler, outcome):
        if w['kind'] == 'editor.cold_open':
            workspace.remove_library(project)
        run = process.run(self.editor_command(v, project, self.out / 'logs' / f'{stem}.log', ['-quit']), project, w['timeout_seconds'], sampler.sample)
        self.finish_run(run, outcome)
        if run.ok:
            # The first open of a variant's project copy also imports and, on a newer Editor, migrates it.
            outcome['samples'].append({'name': 'open', 'ms': run.wall_ms, 'phase': 'first-use' if row['repeat'] == 0 else 'warm'})   # tagged: the workload says what it measures

    def request(self, w, v, row, stem, output=None):
        req = {'schema': 1, 'kind': w['kind'], 'workload': w['id'], 'variant': v['id'], 'outPath': str(self.out / 'results' / f'{stem}.json'),
               'codeOptimization': v['code_optimization'], 'rounds': w['rounds'], 'domainReload': w.get('domain_reload', 'default'),
               'scene': w.get('scene') or '', 'method': w.get('method', ''), 'args': text_args(w.get('args', {})),
               'targets': [{'id': t['id'], 'change': t['change'], 'path': self.target_file(t, v)} for t in w.get('targets', [])],
               'buildMode': w.get('mode', 'clean'), 'development': bool(w.get('development')), 'buildTarget': w.get('build_target') or '',
               'backend': (w.get('backend') or v['backend']) if w['kind'].startswith('player.') else '', 'scenes': w.get('scenes', []),
               'prebuild': w.get('prebuild') or '', **build_request(v), 'outputDir': str(self.out / 'builds' / v['id'] / (output or w['id']))}
        path = self.out / 'requests' / f'{stem}.json'
        path.write_text(json.dumps(req, indent=1), encoding='utf-8')
        return req, path

    def batch(self, w, v, project, row, stem, sampler, outcome, output=None):
        req, path = self.request(w, v, row, stem, output)
        log = self.out / 'logs' / f'{stem}.log'
        run = process.run(self.editor_command(v, project, log, ['-executeMethod', BATCH_METHOD, '-unityBenchRequest', str(path)]),
                          project, w['timeout_seconds'], sampler.sample)
        self.finish_run(run, outcome)
        if outcome['error']:
            # A failed process often wrote a result with the reason.
            try:
                read_result(req['outPath'])
            except ResultError as error:
                outcome['error'] += f' ({error})'
            return
        data = read_result(req['outPath'])
        outcome.update(samples=data['samples'], environment=data.get('environment'), notes=data.get('notes') or {}, headline=data['headline'])
        if w['kind'] in LOGGED_KINDS:
            outcome['reloads'] = [g for g in reload_log.parse_file(log) if g['step'] is not None]
        if w['kind'] == 'player.build_time' and outcome['notes'].get('playerExecutable'):
            exe = Path(outcome['notes']['playerExecutable'])
            settings = {k[len('build.'):]: x for k, x in outcome['notes'].items() if k.startswith('build.')}
            for key in ((('workload', w['id']), v['id']), (build_key(w, v), v['id'])):   # a campaign's other player workloads with the same build settings reuse it
                self.builds[key], self.build_settings[key] = exe, settings

    def target_file(self, target, v):
        """The file a script-edit target changes: project-relative, or relative to the variant's git worktree."""
        if target['root'] == 'worktree':
            return str(Path(self.worktrees[v['id']][1]) / target['path'])
        return target['path']

    # ---- players ------------------------------------------------------------------------------------------------

    def player_exe(self, w, v, project, row, stem, outcome):
        """The built player this workload runs, or None (with outcome['error'] set). Builds once per distinct build key and variant."""
        key = (('workload', w['build']) if w.get('build') else build_key(w, v), v['id'])
        if key not in self.builds and not w.get('build'):
            # No build workload supplied a player: build once, untimed, and keep that process in the raw data. Later workloads with
            # the same backend, scenes and settings run the same build.
            spec = {'id': w['id'], 'kind': 'player.build_time', 'rounds': 1, 'mode': 'clean', 'timeout_seconds': self.cfg['defaults']['build_timeout_seconds'],
                    **{k: w.get(k) for k in ('development', 'scenes', 'build_target', 'backend', 'prebuild')}}
            setup = new_outcome()
            sampler = host_sampling.Sampler()
            self.batch(spec, v, project, row, stem + '-build', sampler, setup, output='auto-' + build_key_id(w, v))
            self.write({'type': 'process', 'index': row['index'], 'workload': w['id'], 'kind': w['kind'], 'variant': v['id'], 'repeat': row['repeat'],
                        'role': 'setup-build', 'status': 'ok' if setup['error'] is None else 'failed', 'error': setup['error'], 'exit_code': setup['exit_code'],
                        'wall_ms': setup['wall_ms'], 'environment': setup['environment'], 'notes': setup['notes'], 'cleanup': setup['cleanup'], 'host': sampler.summary(),
                        'log': f'logs/{stem}-build.log'})
            if setup['error']:
                outcome['error'] = 'setup build failed: ' + setup['error']
                return None
        if key not in self.builds:
            outcome['error'] = f"no player available for {v['id']}" + (f" (build workload {w['build']} did not produce one)" if w.get('build') else '')
            return None
        outcome['build'] = self.build_settings.get(key)
        return self.builds[key]

    def player_argv(self, w, exe, log):
        window = ['-batchmode', '-nographics'] if w['headless'] else ['-screen-fullscreen', '0', '-screen-width', w['window'].split('x')[0], '-screen-height', w['window'].split('x')[1]]
        return [str(exe), *window, '-logFile', str(log)]

    def player_run(self, w, v, project, row, stem, sampler, outcome):
        exe = self.player_exe(w, v, project, row, stem, outcome)
        if exe is None:
            return
        log = self.out / 'logs' / f'{stem}-player.log'
        if w['kind'] == 'player.adapter':
            self.player_adapter(w, v, exe, row, stem, log, sampler, outcome)
        else:
            self.player_startup(w, exe, row, stem, log, sampler, outcome)

    def player_adapter(self, w, v, exe, row, stem, log, sampler, outcome):
        result, request = self.out / 'results' / f'{stem}.json', self.out / 'requests' / f'{stem}.json'
        request.write_text(json.dumps({'schema': 1, 'workload': w['id'], 'variant': v['id'], 'rounds': w['rounds'],
                                       'args': text_args(w['args'])}, indent=1), encoding='utf-8')
        argv = [*self.player_argv(w, exe, log), '-unityBenchAdapter', w['method'], '-unityBenchOut', str(result), '-unityBenchArgs', str(request), *w['player_args']]
        run = process.run(argv, Path(exe).parent, w['timeout_seconds'], sampler.sample)
        self.finish_run(run, outcome)
        try:
            data = read_result(result)
        except ResultError as error:
            if not outcome['error']:
                raise
            outcome['error'] += f' ({error})'   # a failed player often wrote a result with the reason
            return
        if outcome['error']:
            return
        outcome.update(samples=data['samples'], environment=data.get('environment'), notes=data.get('notes') or {}, headline=data['headline'])

    def player_startup(self, w, exe, row, stem, log, sampler, outcome):
        signal = self.out / 'signals' / f'{stem}.json'
        argv = [*self.player_argv(w, exe, log), '-unityBenchSignal', str(signal), *w['player_args']]
        before = time.time()
        run = process.run(argv, Path(exe).parent, w['startup_timeout_seconds'], sampler.sample)
        self.finish_run(run, outcome)
        if outcome['error']:
            return
        if not signal.is_file():
            outcome['error'] = 'player exited without writing the first-frame signal'
            return
        reported = json.loads(signal.read_text(encoding='utf-8'))
        phase = 'first-use' if row['repeat'] == 0 else 'warm'   # tagged: the first launch of a fresh build pays OS caches and code signing checks
        outcome['environment'] = reported.get('environment')
        # What these measure is the probe, not a presented frame (docs/workloads.md#playerstartup).
        outcome['samples'] = [
            {'name': 'launch_to_probe', 'ms': (signal.stat().st_mtime - before) * 1000, 'phase': phase, 'info': {'frame': reported.get('frame')}},
            {'name': 'engine_to_probe', 'ms': reported['realtimeSinceStartupMs'], 'phase': phase},
            {'name': 'launch_to_exit', 'ms': run.wall_ms, 'phase': phase}]


def new_outcome():
    return {'samples': [], 'reloads': [], 'environment': None, 'notes': {}, 'build': None, 'headline': [], 'cleanup': None,
            'error': None, 'exit_code': None, 'wall_ms': None, 'timed_out': False}


def build_request(variant):
    """The player build settings a campaign holds equal: request keys for the Editor side (empty = leave the project's own)."""
    build = variant['build']
    return {'apiCompatibility': build.get('api_compatibility') or '', 'il2cppCodeGeneration': build.get('il2cpp_code_generation') or '',
            'stripping': build.get('stripping') or ''}


def text_args(args):
    """A workload's args table as the list of {k, v} string pairs the Unity side reads (JsonUtility has no dictionaries)."""
    return [{'k': k, 'v': str(x).lower() if isinstance(x, bool) else str(x)} for k, x in args.items()]


def build_key(w, v):
    """What makes two player workloads share a build, per variant: backend, scenes, settings and the prebuild hook."""
    return ('spec', w.get('backend') or v['backend'], tuple(w.get('scenes') or ()), bool(w.get('development')), w.get('build_target') or '', w.get('prebuild') or '')


def build_key_id(w, v):
    return hashlib.sha256(json.dumps(build_key(w, v)).encode()).hexdigest()[:8]


def expand(command, values):
    """Replace {python}, {root}, {project}, {campaign} and {variant} in one prepare command."""
    out = []
    for part in command:
        for name, value in values.items():
            part = part.replace('{' + name + '}', value)
        out.append(part)
    return out


def prepare_project(cfg, variant, root, project, out):
    """Run the campaign's `project.prepare` commands for one variant's copy, with the copy's root as the working directory."""
    values = {'python': sys.executable, 'root': str(root), 'project': str(project), 'campaign': cfg['project']['campaign_dir'], 'variant': variant['id']}
    for number, command in enumerate(cfg['project']['prepare']):
        log = out / 'logs' / f"prepare-{variant['id']}-{number}.log"
        with log.open('w', encoding='utf-8') as handle:
            try:
                done = subprocess.run(expand(command, values), cwd=root, stdout=handle, stderr=subprocess.STDOUT, timeout=cfg['defaults']['build_timeout_seconds'])
            except (OSError, subprocess.TimeoutExpired) as error:
                raise RuntimeError(f"prepare command {number} for {variant['id']} failed: {type(error).__name__}: {error} (see {log})") from None
        if done.returncode != 0:
            raise RuntimeError(f"prepare command {number} for {variant['id']} exited {done.returncode} (see {log})")


def run_campaign(config_path, out, keep_work=False, only=None, work=None, log=print):
    cfg = config.load(config_path)
    notes = []
    rows = config.schedule(cfg, only, notes)
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f'{out} already exists and is not empty; results are never overwritten')
    work = Path(work).resolve() if work else out / 'work'   # a short path helps on Windows, where Library paths get long
    for sub in ('logs', 'requests', 'results', 'signals'):
        (out / sub).mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    campaign = Campaign(cfg, out)
    record = {'tool': 'unity-bench', 'version': __version__, 'campaign': cfg, 'schedule': rows, 'schedule_notes': notes, 'host': host_sampling.describe(),
              'provenance': {'framework': provenance.framework(), 'variants': {}}, 'started_at': datetime.datetime.now().isoformat(timespec='seconds')}
    write_campaign(out, record)
    campaign.results = (out / 'results.jsonl').open('a', encoding='utf-8')
    used = {r['variant'] for r in rows}
    for note in notes:
        log(note)
    try:
        for v in cfg['variants']:
            if v['id'] not in used:
                continue
            log(f"preparing {v['id']}")
            campaign.projects[v['id']], campaign.worktrees[v['id']], info = workspace.create(cfg, v, work)
            record['provenance']['variants'][v['id']] = info
            write_campaign(out, record)   # the resolved commits are known as soon as the copies exist
            worktree = campaign.worktrees[v['id']]
            prepare_project(cfg, v, worktree[1] if worktree else campaign.projects[v['id']], campaign.projects[v['id']], out)
        for row in rows:
            w = campaign.workloads[row['workload']]
            stem = f"{row['index']:04d}-{row['workload']}-{row['variant']}"
            status = campaign.execute(row, out / 'logs' / f'{stem}.log')
            log(f"[{row['index'] + 1}/{len(rows)}] {w['id']} {row['variant']} #{row['repeat']}: {status}")
    finally:
        campaign.results.close()
        if not keep_work:
            for vid, project in campaign.projects.items():
                workspace.remove(project, campaign.worktrees[vid])
            shutil.rmtree(out / 'builds', ignore_errors=True)   # built players are binaries; --keep-work keeps them
            try:
                work.rmdir()
            except OSError:
                pass
        write_reports(out)
    return out


def write_campaign(out, record):
    (out / 'campaign.json').write_text(json.dumps(record, indent=1), encoding='utf-8')


def problems(out):
    """What a finished run lacks, one line each: processes that failed, scheduled processes that never ran, and
    (workload, variant) pairs that produced no samples. Empty when the campaign is complete."""
    out = Path(out)
    rows = read_rows(out / 'results.jsonl')
    schedule = json.loads((out / 'campaign.json').read_text(encoding='utf-8')).get('schedule', [])
    done = {r['index']: r for r in rows if r['type'] == 'process' and r.get('role') is None}
    sampled = {(r['workload'], r['variant']) for r in rows if r['type'] == 'sample'}
    lines = [f"#{r['index']} {r['workload']} {r['variant']} repeat {r['repeat']}: {r['status']}: {r['error']}" for r in done.values() if r['status'] != 'ok']
    lines += [f"#{p['index']} {p['workload']} {p['variant']} repeat {p['repeat']}: never ran" for p in schedule if p['index'] not in done]
    lines += [f'{w} {v}: no samples' for w, v in dict.fromkeys((p['workload'], p['variant']) for p in schedule) if (w, v) not in sampled]
    return lines
