"""Run a campaign: fixed schedule, fresh process per repeat, results appended to results.jsonl.

Nothing in here refuses to measure. Host load is sampled and stored next to each process; a
failed or timed-out process is recorded and the schedule continues.
"""
import datetime
import json
import math
from pathlib import Path
import shutil
import time

from . import __version__, config, host_sampling, process, reload_log, workspace
from .report import write_reports

BATCH_METHOD = 'UnityBench.Editor.BatchEntry.Run'
EDITOR_ARGS = ['-batchmode', '-nographics']
BATCH_KINDS = ('editor.script_edit', 'editor.play', 'editor.adapter', 'player.build_time')
LOGGED_KINDS = ('editor.script_edit', 'editor.play', 'editor.adapter')   # kinds whose Editor.log has reload blocks to parse


class ResultError(ValueError):
    pass


def read_result(path):
    """Load and check an adapter/built-in result file (docs/adapters.md)."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ResultError(f'no readable result file ({type(error).__name__})') from None
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ResultError('result schema must be 1')
    if data.get('completed') is not True:
        raise ResultError('adapter reported failure: ' + str(data.get('error') or 'no error text').splitlines()[0][:300])
    for s in data.get('samples') or []:
        ms = s.get('ms') if isinstance(s, dict) else None
        if not (isinstance(s.get('name'), str) and s['name'] and s.get('phase') in ('first-use', 'warm')
                and isinstance(ms, (int, float)) and not isinstance(ms, bool) and math.isfinite(ms) and ms >= 0):
            raise ResultError('invalid sample: ' + json.dumps(s)[:200])
    if not data.get('samples'):
        raise ResultError('result has no samples')
    return data


class Campaign:
    def __init__(self, cfg, out):
        self.cfg, self.out = cfg, Path(out)
        self.variants = {v['id']: v for v in cfg['variants']}
        self.workloads = {w['id']: w for w in cfg['workloads']}
        self.projects, self.worktrees = {}, {}
        self.builds = {}   # (build workload id, variant id) -> player executable
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
        saved = workspace.snapshot(project, [t['path'] for t in w.get('targets', [])] + ['ProjectSettings/EditorSettings.asset'])
        sampler = host_sampling.Sampler()
        started = time.time()
        outcome = {'samples': [], 'reloads': [], 'environment': None, 'notes': {}, 'error': None, 'exit_code': None, 'wall_ms': None, 'timed_out': False}
        try:
            if w['kind'] in ('editor.cold_open', 'editor.warm_open'):
                self.open_project(w, v, project, row, stem, sampler, outcome)
            elif w['kind'] == 'player.startup':
                self.player_startup(w, v, project, row, stem, sampler, outcome)
            else:
                self.batch(w, v, project, row, stem, sampler, outcome)
        except (OSError, ResultError, RuntimeError, ValueError) as error:
            outcome['error'] = outcome['error'] or f'{type(error).__name__}: {error}'
        restored = workspace.restore(project, saved)
        status = 'ok' if outcome['error'] is None else 'timeout' if outcome['timed_out'] else 'failed'
        self.write({'type': 'process', **base, 'status': status, 'error': outcome['error'], 'exit_code': outcome['exit_code'],
                    'wall_ms': outcome['wall_ms'], 'environment': outcome['environment'], 'notes': outcome['notes'],
                    'host': sampler.summary(), 'restored_files': restored, 'started_at': datetime.datetime.fromtimestamp(started).isoformat(timespec='seconds'),
                    'log': f'logs/{stem}.log' if log.exists() else None})
        if status == 'ok':
            for s in outcome['samples']:
                self.write({'type': 'sample', **base, 'name': s['name'], 'phase': s['phase'], 'ms': s['ms'], 'round': s.get('round', 0),
                            'info': s.get('info') or {}, 'counters': s.get('counters') or None, 'hash': s.get('hash')})
            for group in outcome['reloads']:
                self.write({'type': 'reload', **base, **group})
        return status

    def editor_command(self, v, project, log, extra):
        editor = [v['editor']] if isinstance(v['editor'], str) else list(v['editor'])
        return [*editor, *EDITOR_ARGS, '-projectPath', str(project), '-logFile', str(log), *extra]

    def finish_run(self, run, outcome):
        outcome.update(exit_code=run.exit_code, wall_ms=run.wall_ms, timed_out=run.timed_out)
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
            outcome['samples'].append({'name': 'open', 'ms': run.wall_ms, 'phase': 'first-use' if row['repeat'] == 0 else 'warm'})

    def request(self, w, v, row, stem):
        req = {'schema': 1, 'kind': w['kind'], 'workload': w['id'], 'variant': v['id'], 'outPath': str(self.out / 'results' / f'{stem}.json'),
               'codeOptimization': v['code_optimization'], 'rounds': w['rounds'], 'domainReload': w.get('domain_reload', 'default'),
               'scene': w.get('scene') or '', 'method': w.get('method', ''), 'args': [{'k': k, 'v': str(x).lower() if isinstance(x, bool) else str(x)} for k, x in w.get('args', {}).items()],
               'targets': [{k: t[k] for k in ('id', 'path', 'change')} for t in w.get('targets', [])],
               'buildMode': w.get('mode', 'clean'), 'development': bool(w.get('development')), 'buildTarget': w.get('build_target') or '',
               'backend': (w.get('backend') or v['backend']) if w['kind'].startswith('player.') else '',
               'outputDir': str(self.out / 'builds' / v['id'] / w['id'])}
        path = self.out / 'requests' / f'{stem}.json'
        path.write_text(json.dumps(req, indent=1), encoding='utf-8')
        return req, path

    def batch(self, w, v, project, row, stem, sampler, outcome):
        req, path = self.request(w, v, row, stem)
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
        outcome.update(samples=data['samples'], environment=data.get('environment'), notes=data.get('notes') or {})
        if w['kind'] in LOGGED_KINDS:
            outcome['reloads'] = [g for g in reload_log.parse_file(log) if g['step'] is not None]
        if w['kind'] == 'player.build_time' and outcome['notes'].get('playerExecutable'):
            self.builds[(w['id'], v['id'])] = Path(outcome['notes']['playerExecutable'])

    def player_startup(self, w, v, project, row, stem, sampler, outcome):
        key = next((k for k in ((w['build'], v['id']), (w['id'], v['id'])) if k[0] and k in self.builds), None)
        if key is None:
            # No build workload supplied a player: build once for this variant, untimed, and keep that process in the raw data.
            build = {'id': w['id'], 'kind': 'player.build_time', 'rounds': 1, 'mode': 'clean', 'timeout_seconds': self.cfg['defaults']['build_timeout_seconds'],
                     **{k: w.get(k) for k in ('development', 'scene', 'build_target', 'backend')}}
            setup = {'samples': [], 'reloads': [], 'environment': None, 'notes': {}, 'error': None, 'exit_code': None, 'wall_ms': None, 'timed_out': False}
            build_sampler = host_sampling.Sampler()
            self.batch(build, v, project, row, stem + '-build', build_sampler, setup)
            self.write({'type': 'process', 'index': row['index'], 'workload': w['id'], 'kind': 'player.startup', 'variant': v['id'], 'repeat': row['repeat'],
                        'role': 'setup-build', 'status': 'ok' if setup['error'] is None else 'failed', 'error': setup['error'], 'exit_code': setup['exit_code'],
                        'wall_ms': setup['wall_ms'], 'environment': setup['environment'], 'notes': setup['notes'], 'host': build_sampler.summary(), 'log': f'logs/{stem}-build.log'})
            if setup['error']:
                outcome['error'] = 'setup build failed: ' + setup['error']
                return
            key = (w['id'], v['id'])
        exe = self.builds[key]
        signal = self.out / 'signals' / f'{stem}.json'
        argv = [str(exe), *(EDITOR_ARGS if w['headless'] else ['-screen-fullscreen', '0', '-screen-width', '640', '-screen-height', '360']),
                '-logFile', str(self.out / 'logs' / f'{stem}-player.log'), '-unityBenchSignal', str(signal)]
        before = time.time()
        run = process.run(argv, exe.parent, w['startup_timeout_seconds'], sampler.sample)
        self.finish_run(run, outcome)
        if outcome['error']:
            return
        if not signal.is_file():
            outcome['error'] = 'player exited without writing the first-frame signal'
            return
        reported = json.loads(signal.read_text(encoding='utf-8'))
        phase = 'first-use' if row['repeat'] == 0 else 'warm'   # the first launch of a fresh build pays OS caches and code signing checks
        outcome['environment'] = reported.get('environment')
        outcome['samples'] = [
            {'name': 'first_frame', 'ms': (signal.stat().st_mtime - before) * 1000, 'phase': phase, 'info': {'frame': reported.get('frame')}},
            {'name': 'first_frame_in_player', 'ms': reported['realtimeSinceStartupMs'], 'phase': phase},
            {'name': 'process_total', 'ms': run.wall_ms, 'phase': phase}]


def run_campaign(config_path, out, keep_work=False, only=None, log=print):
    cfg = config.load(config_path)
    rows = config.schedule(cfg, only)
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f'{out} already exists and is not empty; results are never overwritten')
    for sub in ('logs', 'requests', 'results', 'signals', 'work'):
        (out / sub).mkdir(parents=True, exist_ok=True)
    campaign = Campaign(cfg, out)
    (out / 'campaign.json').write_text(json.dumps({'tool': 'unity-bench', 'version': __version__, 'campaign': cfg, 'schedule': rows,
                                                   'host': host_sampling.describe(), 'started_at': datetime.datetime.now().isoformat(timespec='seconds')}, indent=1), encoding='utf-8')
    campaign.results = (out / 'results.jsonl').open('a', encoding='utf-8')
    probe = any(w['kind'].startswith('player.') for w in cfg['workloads'])
    try:
        for v in cfg['variants']:
            log(f"preparing {v['id']}")
            campaign.projects[v['id']], campaign.worktrees[v['id']] = workspace.create(cfg, v, out / 'work', probe)
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
        write_reports(out)
    return out
