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
import tempfile
import time

from . import __version__, cache, config, fastcopy, host_sampling, process, provenance, reload_log, states, workspace
from .report import write_reports
from .distributions import finite
from .result import ResultError, read_result  # noqa: F401  (ResultError is part of this module's interface)
from .summary import read_rows

BATCH_METHOD = 'UnityBench.Editor.BatchEntry.Run'
EDITOR_ARGS = ['-batchmode', '-nographics']
PLAYER_KINDS = ('player.startup', 'player.adapter', 'player.scene')
SCENE_METHOD = 'UnityBench.Player.SceneWorkload.Run'   # the built-in player entry point of the scene workloads (a player.adapter in the package)
LOGGED_KINDS = ('editor.script_edit', 'editor.play', 'editor.adapter', 'editor.reload_leak', 'editor.play_leak')   # kinds whose Editor.log has reload blocks to parse


class Campaign:
    def __init__(self, cfg, out):
        self.cfg, self.out = cfg, Path(out)
        self.variants = {v['id']: v for v in cfg['variants']}
        self.workloads = {w['id']: w for w in cfg['workloads']}
        self.projects, self.worktrees = {}, {}
        self.store = None   # cache.Store when prepared copies are cached or snapshotted, else None (v0.3 behaviour)
        self.prepared = {}   # variant id -> what preparing it was: status (reused/built), key, snapshot entry, seconds
        self.launch_env, self.launch_args = {}, []   # the start state of the process being run: environment and Editor arguments for the cold isolation
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
        sampler = host_sampling.Sampler()
        started = time.time()
        outcome = new_outcome()
        state, scratch = None, None
        try:
            state, scratch = self.start_state(w, v, project, row)
        except Exception as error:   # a state that could not be set up is recorded; the process still runs from what is there
            state = {'requested': w.get('state') or states.default_state(w['kind']), 'actual': 'as-is', 'notes': [f'{type(error).__name__}: {error}']}
        saved = workspace.snapshot(project, [self.target_file(t, v) for t in w.get('targets', [])] + ['ProjectSettings/EditorSettings.asset'])
        settings_before = cache.settings_digests(project)
        try:
            if w['kind'] in ('editor.cold_open', 'editor.warm_open'):
                self.open_project(w, v, project, row, stem, sampler, outcome)
            elif w['kind'] in PLAYER_KINDS:
                self.player_run(w, v, project, row, stem, sampler, outcome)
            else:
                self.batch(w, v, project, row, stem, sampler, outcome)
        except Exception as error:   # one bad process, result file or adapter must never stop the schedule
            outcome['error'] = outcome['error'] or f'{type(error).__name__}: {error}'
        finally:
            self.launch_env, self.launch_args = {}, []
            if scratch:
                try:
                    fastcopy.remove_tree(scratch, attempts=3)
                except OSError:
                    pass
        restored = workspace.restore(project, saved)
        changed = cache.settings_changed(settings_before, cache.settings_digests(project))
        if state is not None and changed:
            state['settings_changed'] = changed   # the Editor rewrote its own settings files during this process
        if state is not None and log.exists():
            imports = states.initial_import(log)
            if imports:
                state['imports'] = imports
        status = 'ok' if outcome['error'] is None else 'timeout' if outcome['timed_out'] else 'failed'
        self.write({'type': 'process', **base, 'status': status, 'error': outcome['error'], 'exit_code': outcome['exit_code'], 'state': state,
                    'wall_ms': outcome['wall_ms'], 'environment': outcome['environment'], 'notes': outcome['notes'], 'build': outcome['build'],
                    'headline': outcome['headline'], 'cleanup': outcome['cleanup'], 'host': sampler.summary(), 'restored_files': restored,
                    'started_at': datetime.datetime.fromtimestamp(started).isoformat(timespec='seconds'), 'log': f'logs/{stem}.log' if log.exists() else None})
        if status == 'ok':
            for s in outcome['samples']:
                self.write({'type': 'sample', **base, 'name': s['name'], 'phase': s['phase'], 'ms': s['ms'], 'round': s.get('round', 0),
                            'info': s.get('info') or {}, 'counters': s.get('counters') or None, 'hash': s.get('hash'), 'work_group': s.get('work_group'),
                            **({'unit': s['unit']} if s.get('unit') else {})})
            for group in outcome['reloads']:
                self.write({'type': 'reload', **base, **group})
            self.write_process_memory(base, sampler.summary())
        return status

    def write_process_memory(self, base, host):
        """The process tree's peak memory as the runner saw it (psutil, once a second: a lower bound), as ordinary byte samples next to the ones the Unity side reports."""
        for name, key in (('Process RSS (runner)', 'root_rss_max_mb'), ('Process tree RSS (runner)', 'tree_rss_max_mb'), ('Process tree Private (runner)', 'tree_private_max_mb')):
            if host.get(key):
                self.write({'type': 'sample', **base, 'name': 'mem/peak/' + name, 'phase': 'single', 'ms': host[key] * 2**20, 'round': 0, 'info': {}, 'counters': None,
                            'hash': None, 'work_group': None, 'unit': 'bytes'})

    def editor_command(self, v, project, log, extra):
        editor = [v['editor']] if isinstance(v['editor'], str) else list(v['editor'])
        return [*editor, *EDITOR_ARGS, '-projectPath', str(project), '-logFile', str(log), *extra, *self.launch_args]

    def launch(self, argv, cwd, timeout, sampler=None):
        """Run one owned process with the start state's environment (the cold isolation of global caches) in force."""
        with states.environment(self.launch_env):
            return process.run(argv, cwd, timeout, sampler.sample if sampler else None)

    def start_state(self, w, v, project, row):
        """Put the project copy into the workload's start state. Returns (the record stored with the process, a scratch directory to delete afterwards).

        cold: Library is deleted and the cold isolation of global caches is set up for this one process.
        warm: the copy goes back to the snapshot taken after it was prepared and imported. Without a snapshot the process runs as-is and says so.
        as-is: nothing.
        """
        if w['kind'] not in states.STATE_KINDS:
            return None, None
        requested = w.get('state') or states.default_state(w['kind'])
        state = {'requested': requested, 'actual': requested, 'notes': []}
        if requested == 'cold':
            workspace.remove_library(project)
            isolate = self.cfg['cache']['isolate']
            scratch = tempfile.mkdtemp(prefix='ub-cold-')
            self.launch_env, self.launch_args, described = states.cold_isolation(scratch, isolate)
            state.update(library_removed=True, isolated=described)
            return state, scratch
        if requested == 'warm':
            prepared = self.prepared.get(v['id']) or {}
            entry = prepared.get('entry')
            if entry is None:
                state.update(actual='as-is')
                state['notes'].append('no snapshot of this copy exists' + (f": {prepared['note']}" if prepared.get('note') else ''))
                return state, None
            state.update(self.restore(entry, v, project))
            state['snapshot'] = entry.key
        return state, None

    def restore(self, entry, v, project):
        """Put the live copy back to `entry`'s snapshot and check it against the snapshot's manifest. Returns the state fields describing it."""
        worktree = self.worktrees[v['id']]
        root = Path(worktree[1] if worktree else project)
        relative = entry.meta.get('project_relative', '.')
        start = time.perf_counter()
        result = workspace.restore_root(entry.tree, root)
        verified, diff = False, []
        for attempt in range(2):
            diff = cache.manifest_diff(entry.meta['manifest'], cache.manifest(root, relative))
            verified = not diff
            if verified:
                break
            result = workspace.restore_root(entry.tree, root)   # something resisted (a file still open): once more
        out = {'restore_ms': round((time.perf_counter() - start) * 1000), 'restore_method': result['method'], 'verified': verified}
        if result['retries']:
            out['delete_retries'] = result['retries']
        if not verified:
            out['notes'] = ['the restored copy differs from the snapshot in: ' + ', '.join(diff)]
        return out

    def finish_run(self, run, outcome):
        outcome.update(exit_code=run.exit_code, wall_ms=run.wall_ms, timed_out=run.timed_out, cleanup=run.cleanup)
        if run.timed_out:
            outcome['error'] = 'timeout; process tree killed'
        elif run.exit_code != 0:
            outcome['error'] = f'exit code {run.exit_code}'

    def open_project(self, w, v, project, row, stem, sampler, outcome):
        run = self.launch(self.editor_command(v, project, self.out / 'logs' / f'{stem}.log', ['-quit']), project, w['timeout_seconds'], sampler)
        self.finish_run(run, outcome)
        if run.ok:
            # The first open of a variant's project copy also imports and, on a newer Editor, migrates it.
            outcome['samples'].append({'name': 'open', 'ms': run.wall_ms, 'phase': 'first-use' if row['repeat'] == 0 else 'warm'})   # tagged: the workload says what it measures

    def request(self, w, v, row, stem, output=None):
        req = {'schema': 1, 'kind': w['kind'], 'workload': w['id'], 'variant': v['id'], 'outPath': str(self.out / 'results' / f'{stem}.json'),
               'codeOptimization': v['code_optimization'], 'rounds': w['rounds'], 'domainReload': w.get('domain_reload', 'default'),
               'scene': w.get('scene') or '', 'method': w.get('method', ''),
               'args': text_args(scene_args(w) if w['kind'] in config.SCENE_KINDS else w.get('args', {})),
               'targets': [{'id': t['id'], 'change': t['change'], 'path': self.target_file(t, v)} for t in w.get('targets', [])],
               'memory': bool(w.get('memory', True)), 'memorySnapshot': bool(w.get('memory_snapshot')), 'forceGc': bool(w.get('force_gc', True)),
               'snapshotPath': str(self.out / 'snapshots' / f'{stem}.snap') if w.get('memory_snapshot') else '',
               'buildMode': w.get('mode', 'clean'), 'development': bool(w.get('development')), 'buildTarget': w.get('build_target') or '',
               'backend': (w.get('backend') or v['backend']) if w['kind'].startswith('player.') else '', 'scenes': w.get('scenes', []),
               'prebuild': w.get('prebuild') or '', **build_request(v), 'outputDir': str(self.out / 'builds' / v['id'] / (output or w['id']))}
        path = self.out / 'requests' / f'{stem}.json'
        path.write_text(json.dumps(req, indent=1), encoding='utf-8')
        return req, path

    def batch(self, w, v, project, row, stem, sampler, outcome, output=None):
        req, path = self.request(w, v, row, stem, output)
        log = self.out / 'logs' / f'{stem}.log'
        run = self.launch(self.editor_command(v, project, log, ['-executeMethod', BATCH_METHOD, '-unityBenchRequest', str(path)]),
                          project, w['timeout_seconds'], sampler)
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

    # ---- preparing a variant's copy -----------------------------------------------------------------------------

    def prepare_variant(self, v, work, managed, record, log):
        """Make the variant's project copy: from a cache entry when one matches, else copy or check out, inject the package, run `project.prepare`,
        and (when prepared copies are cached or any workload starts `warm`) import it once in an untimed Editor open and snapshot it.

        Sets self.projects / self.worktrees / self.prepared for the variant. The record of what happened is `self.prepared[id]`.
        """
        started = time.time()
        store = self.store
        prep = {'status': 'built', 'key': None, 'entry': None, 'note': None}
        self.prepared[v['id']] = prep
        target = workspace.target_path(work, v)
        identity = key = parts = hashed = None
        if store:
            identity = workspace.source_identity(self.cfg, v)
            editor = cache.editor_identity(v['editor'])
            parts, hashed = cache.components(self.cfg, v, target, identity, editor, workspace.injected_digest(self.cfg))
            key = cache.key_of(parts)
            prep.update(key=key, editor=editor['full'], cache_dir=str(store.root) if store.persistent else None)
            entry, why = store.lookup(key)
            if entry is not None:
                log(f"preparing {v['id']}: reusing prepared copy {key}")
                try:
                    project, worktree, info, result = workspace.materialize(self.cfg, v, work, entry, managed)
                except (OSError, subprocess.CalledProcessError) as error:   # a full disk or a locked file: prepare it the long way instead of failing the run
                    why = f'the cached copy could not be restored: {type(error).__name__}: {error}'
                    fastcopy.remove_tree(target, attempts=3)
                    subprocess.run(['git', '-C', str(self.cfg['project']['repository'] or self.cfg['project']['path']), 'worktree', 'prune'], capture_output=True)
                else:
                    self.projects[v['id']], self.worktrees[v['id']] = project, worktree
                    store.touch(entry)
                    prep.update(status='reused', entry=entry, seconds=round(time.time() - started, 2), method=result['method'], built_seconds=entry.meta.get('built_seconds'))
                    record['provenance']['variants'][v['id']] = {**info, 'prepared': public(prep)}
                    return
            if why not in ('no entry', 'reuse is off'):
                prep['rebuilt_because'] = why
                log(f"preparing {v['id']}: cached copy {key} is not usable ({why}); preparing again")
                if entry is None:
                    store.discard(key)
        log(f"preparing {v['id']}")
        project, worktree, info = workspace.create(self.cfg, v, work, commit=(identity or {}).get('commit') if v.get('revision') else None, managed=managed)
        self.projects[v['id']], self.worktrees[v['id']] = project, worktree
        record['provenance']['variants'][v['id']] = info
        write_campaign(self.out, record)   # the resolved commits are known as soon as the copies exist
        root = Path(worktree[1] if worktree else project)
        prepare_project(self.cfg, v, root, project, self.out)
        if store:
            self.prime_and_snapshot(v, project, root, key, parts, hashed, info, prep, started, log)
        prep['seconds'] = round(time.time() - started, 1)
        info['prepared'] = public(prep)

    def prime_and_snapshot(self, v, project, root, key, parts, hashed, info, prep, started, log):
        """The untimed first open (import and, on a newer Editor, migration), then a snapshot of the result as cache entry `key`.

        A failed or timed-out open leaves the copy as it is and no entry: a snapshot is only taken from a clean point (the Editor exited 0 and left no Temp).
        """
        stem = f"prime-{v['id']}"
        sampler = host_sampling.Sampler()
        before = cache.settings_digests(project)
        self.launch_env, self.launch_args = {}, []
        run = self.launch(self.editor_command(v, project, self.out / 'logs' / f'{stem}.log', ['-quit']), project, self.cfg['defaults']['build_timeout_seconds'], sampler)
        failure = None if run.ok else ('timeout; process tree killed' if run.timed_out else f'exit code {run.exit_code}')
        self.write({'type': 'prepare', 'role': 'prime', 'variant': v['id'], 'key': key, 'status': 'ok' if failure is None else 'failed', 'error': failure,
                    'exit_code': run.exit_code, 'wall_ms': run.wall_ms, 'cleanup': run.cleanup, 'host': sampler.summary(), 'log': f'logs/{stem}.log'})
        prep['prime_ms'] = run.wall_ms
        if failure:
            prep['note'] = f'the priming open failed ({failure}); the copy was not snapshotted or cached'
            log(f"{v['id']}: {prep['note']}")
            return
        changed = cache.settings_changed(before, cache.settings_digests(project))
        relative = Path(project).relative_to(root).as_posix() if Path(project) != root else '.'
        try:
            staged = self.store.staging(key)
            workspace.snapshot_root(root, staged / 't')
            data = cache.manifest(staged / 't', relative)
            files, size = cache.tree_size(staged / 't')
            meta = {'variant': v['id'], 'components': parts, 'project_relative': relative, 'mode': info.get('mode'), 'info': {k: x for k, x in info.items() if k != 'prepared'},
                    'manifest': data, 'manifest_digest': cache.manifest_digest(data), 'built_seconds': round(time.time() - started, 1), 'prime_ms': run.wall_ms,
                    'files': files, 'size_bytes': size, 'settings_changed_by_priming': changed[:30], 'prepare_inputs': hashed, 'framework': __version__}
            entry = self.store.commit(staged, key, meta)
        except OSError as error:
            prep['note'] = f'snapshot failed: {error}'
            log(f"{v['id']}: {prep['note']}")
            return
        prep['entry'] = entry
        if changed:
            prep['settings_changed_by_priming'] = changed[:30]

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
        if w['kind'] in ('player.adapter', 'player.scene'):
            self.player_adapter(w, v, exe, row, stem, log, sampler, outcome)
        else:
            self.player_startup(w, exe, row, stem, log, sampler, outcome)

    def player_adapter(self, w, v, exe, row, stem, log, sampler, outcome):
        result, request = self.out / 'results' / f'{stem}.json', self.out / 'requests' / f'{stem}.json'
        scene = w['kind'] == 'player.scene'   # a player.adapter whose entry point is the package's own
        request.write_text(json.dumps({'schema': 1, 'workload': w['id'], 'variant': v['id'], 'rounds': w['rounds'],
                                       'args': text_args(scene_args(w) if scene else w['args'])}, indent=1), encoding='utf-8')
        argv = [*self.player_argv(w, exe, log), '-unityBenchAdapter', SCENE_METHOD if scene else w['method'], '-unityBenchOut', str(result), '-unityBenchArgs', str(request), *w['player_args']]
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
        try:
            json.dumps(reported, allow_nan=False)
            if not finite(reported['realtimeSinceStartupMs']):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ResultError('invalid first-frame signal') from None
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


def scene_args(w):
    """The scene workloads' keys as the flat args the Unity side reads (the same names, strings; `time.*` flattened)."""
    keys = ('duration_seconds', 'duration_frames', 'warmup_seconds', 'warmup_frames', 'vsync', 'list_markers', 'memory')
    return {'scene': w['scenes'][0] if w['kind'] == 'player.scene' else w['scene'], **{k: w[k] for k in keys if k in w}, **w.get('time', {})}


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


def public(prep):
    """The part of a variant's preparation record that goes into campaign.json."""
    return {k: x for k, x in prep.items() if k != 'entry' and x is not None}


def run_campaign(config_path, out, keep_work=False, only=None, work=None, log=print, cache_dir=None, use_cache=True):
    cfg = config.load(config_path)
    notes = []
    rows = config.schedule(cfg, only, notes)
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f'{out} already exists and is not empty; results are never overwritten')
    used = {r['variant'] for r in rows}
    wants_warm = any(w.get('state') == 'warm' and set(w['variants']) & used for w in cfg['workloads'] if w['id'] in {r['workload'] for r in rows})
    store = None
    if use_cache:
        store = cache.Store(Path(cache_dir).expanduser().resolve() if cache_dir else cache.default_cache_dir().resolve())
    elif wants_warm:
        store = cache.Store(out / 'snapshots', reuse=False, persistent=False)   # warm needs a snapshot to restore; nothing is kept for later runs
    managed = bool(store and store.persistent and not work)
    # Where the live copies go: the project path is part of the cache key (Unity's Library holds absolute paths), so the default is a fixed directory inside
    # the cache, not the run directory; a short path helps on Windows, where Library paths get long.
    work = Path(work).resolve() if work else (store.default_work if managed else out / 'work')
    for sub in ('logs', 'requests', 'results', 'signals'):
        (out / sub).mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    campaign = Campaign(cfg, out)
    campaign.store = store
    record = {'tool': 'unity-bench', 'version': __version__, 'campaign': cfg, 'schedule': rows, 'schedule_notes': notes, 'host': host_sampling.describe(),
              'cache': {'enabled': bool(store and store.persistent), 'dir': str(store.root) if store and store.persistent else None, 'work': str(work),
                        'isolate': cfg['cache']['isolate']},
              'provenance': {'framework': provenance.framework(), 'variants': {}}, 'started_at': datetime.datetime.now().isoformat(timespec='seconds')}
    write_campaign(out, record)
    campaign.results = (out / 'results.jsonl').open('a', encoding='utf-8')
    for note in notes:
        log(note)
    try:
        for v in cfg['variants']:
            if v['id'] not in used:
                continue
            campaign.prepare_variant(v, work, managed, record, log)
            write_campaign(out, record)
        for row in rows:
            w = campaign.workloads[row['workload']]
            stem = f"{row['index']:04d}-{row['workload']}-{row['variant']}"
            status = campaign.execute(row, out / 'logs' / f'{stem}.log')
            log(f"[{row['index'] + 1}/{len(rows)}] {w['id']} {row['variant']} #{row['repeat']}: {status}")
    finally:
        campaign.results.close()
        if not keep_work:
            left = [path for vid, project in campaign.projects.items() if (path := workspace.remove(project, campaign.worktrees[vid]))]
            if left:
                record['left_behind'] = left
                write_campaign(out, record)
                log('could not remove (a process still has it open?): ' + ', '.join(left))
            shutil.rmtree(out / 'builds', ignore_errors=True)   # built players are binaries; --keep-work keeps them
            if store and not store.persistent:
                shutil.rmtree(store.root, ignore_errors=True)
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
