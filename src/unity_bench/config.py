"""Campaign file loading, validation and the fixed schedule.

A campaign is one TOML (or JSON) file: variants (Unity Editor + optional project
revision + player scripting backend), workloads, and how often to repeat them.
"""
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

import tomllib

from . import states

BUILD_KEYS = {'development', 'scene', 'scenes', 'build_target', 'backend', 'prebuild'}   # what decides which player a workload gets
SCENE_KEYS = {'duration_seconds', 'duration_frames', 'warmup_seconds', 'warmup_frames', 'time', 'vsync', 'list_markers'}   # the scene workloads' run: what and for how long
TIME_KEYS = {'capture_delta', 'fixed_timestep'}
KINDS = {
    'editor.cold_open': {'description': 'Delete Library, open the project in batchmode, quit.'},
    'editor.warm_open': {'description': 'Open the project in batchmode with its Library, quit.'},
    'editor.script_edit': {'description': 'Edit a script, time compile and compile+reload+idle.', 'keys': {'targets'}},
    'editor.play': {'description': 'Enter and exit Play Mode.', 'keys': {'domain_reload', 'scene'}},
    'editor.reload_leak': {'description': 'Edit a script and reload N times in one Editor, read memory after each reload.', 'keys': {'targets', 'force_gc', 'memory_snapshot', 'alc_leak_detection'}},
    'editor.play_leak': {'description': 'Enter and exit Play Mode N times in one Editor, read memory after each cycle.', 'keys': {'domain_reload', 'scene', 'force_gc', 'memory_snapshot', 'alc_leak_detection'}},
    'editor.adapter': {'description': 'Run a project C# adapter inside the Editor.', 'keys': {'method', 'args'}},
    'player.build_time': {'description': 'BuildPipeline.BuildPlayer wall time.', 'keys': BUILD_KEYS | {'mode'}},
    'player.startup': {'description': 'Launch a built player until its first frame.', 'keys': BUILD_KEYS | {'build', 'headless', 'window', 'player_args', 'startup_timeout_seconds'}},
    'player.adapter': {'description': 'Run a project C# adapter inside a built player.', 'keys': BUILD_KEYS | {'build', 'method', 'args', 'headless', 'window', 'player_args'}},
    'editor.scene_play': {'description': 'Play a scene in the Editor and record frames, markers, GC and memory.', 'keys': {'scene'} | SCENE_KEYS},
    'player.scene': {'description': 'Play a scene in a built player and record frames, markers, GC and memory.',
                     'keys': (BUILD_KEYS - {'scenes'}) | SCENE_KEYS | {'headless', 'window', 'player_args'}},
}
SCENE_KINDS = ('editor.scene_play', 'player.scene')
LEAK_KINDS = ('editor.reload_leak', 'editor.play_leak')   # N reloads or Play Mode cycles in one Editor, a memory reading after each
LEAK_ROUNDS = {'editor.reload_leak': 30, 'editor.play_leak': 10}   # their default `rounds`: the campaign's generic default (a handful) would be too few for a slope
MEMORY_KINDS = ('editor.script_edit', 'editor.play', 'editor.adapter', 'editor.scene_play', 'player.scene', 'player.build_time', *LEAK_KINDS)   # kinds that read memory inside the process
COMMON_KEYS = {'id', 'kind', 'repeats', 'rounds', 'timeout_seconds', 'variants', 'headline'}
VARIANT_KEYS = {'id', 'editor', 'unity_version', 'backend', 'code_optimization', 'revision', 'baseline', 'build'}
BUILD_SETTING_KEYS = ('api_compatibility', 'il2cpp_code_generation', 'stripping')   # player build settings a campaign may hold equal across Editors
DEFAULT_KEYS = {'repeats', 'rounds', 'timeout_seconds', 'exclude_first_use', 'baseline', 'build_timeout_seconds'}
TARGET_CHANGES = ('comment', 'api')
TARGET_ROOTS = ('project', 'worktree')
BACKENDS = ('mono', 'il2cpp', 'coreclr')


class ConfigError(ValueError):
    pass



def project_relative(path):
    """True for a relative path inside the project on every OS: no root, no drive, no '..' (checked as POSIX and Windows)."""
    posix, windows = PurePosixPath(path), PureWindowsPath(path)
    return not (posix.is_absolute() or windows.root or windows.drive) and '..' not in posix.parts and '..' not in windows.parts

def require(condition, message):
    if not condition:
        raise ConfigError(message)


def identifier(value, what):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value), f'{what} must match [A-Za-z0-9][A-Za-z0-9_.-]* (got {value!r})')


def positive_int(value, what):
    require(type(value) is int and value > 0, f'{what} must be a positive integer')


def unknown(table, allowed, what):
    extra = sorted(set(table) - set(allowed))
    require(not extra, f'{what}: unknown key(s) {", ".join(extra)}')


def load(path):
    path = Path(path).resolve()
    if path.suffix == '.json':
        raw = json.loads(path.read_text(encoding='utf-8'))
    else:
        raw = tomllib.loads(path.read_text(encoding='utf-8'))
    return validate(raw, path.parent)


def validate(raw, base):
    """Return the campaign with defaults applied and paths resolved against `base`."""
    require(isinstance(raw, dict), 'Campaign must be a table')
    unknown(raw, {'name', 'project', 'defaults', 'build', 'cache', 'variants', 'workloads'}, 'campaign')
    identifier(raw.get('name'), 'name')
    project = raw.get('project') or {}
    unknown(project, {'path', 'repository', 'inject_package', 'prepare', 'prepare_inputs'}, 'project')
    require(isinstance(project.get('path'), str) and project['path'], 'project.path is required')
    base = Path(base)
    root = (base / project['path']).resolve()
    inject = project.get('inject_package', True)
    require(isinstance(inject, bool), 'project.inject_package must be a boolean')
    repository = (base / project['repository']).resolve() if project.get('repository') else None
    prepare = project.get('prepare', [])
    require(isinstance(prepare, list) and all(isinstance(c, list) and c and all(isinstance(x, str) for x in c) for c in prepare),
            'project.prepare must be a list of commands, each a non-empty list of strings')
    prepare_inputs = project.get('prepare_inputs', [])
    require(isinstance(prepare_inputs, list) and all(isinstance(x, str) and x for x in prepare_inputs),
            'project.prepare_inputs must be a list of paths (files or directories, relative to the campaign file) the prepare commands depend on')
    cache_table = raw.get('cache') or {}
    require(isinstance(cache_table, dict), 'cache must be a table')
    unknown(cache_table, {'isolate'}, 'cache')
    isolate = cache_table.get('isolate', list(states.DEFAULT_ISOLATE))
    require(isinstance(isolate, list) and all(x in states.ISOLATE for x in isolate) and len(set(isolate)) == len(isolate),
            f'cache.isolate must be a list of unique names from {", ".join(states.ISOLATE)} (empty: a cold start only deletes Library)')

    defaults = {'repeats': 4, 'rounds': 5, 'timeout_seconds': 1800, 'build_timeout_seconds': 3600,
                'exclude_first_use': True, 'baseline': None, **(raw.get('defaults') or {})}
    unknown(defaults, DEFAULT_KEYS, 'defaults')
    for key in ('repeats', 'rounds', 'timeout_seconds', 'build_timeout_seconds'):
        positive_int(defaults[key], 'defaults.' + key)
    require(isinstance(defaults['exclude_first_use'], bool), 'defaults.exclude_first_use must be a boolean')

    shared_build = build_settings(raw.get('build') or {}, 'build')
    variants, ids = [], set()
    require(isinstance(raw.get('variants'), list) and raw['variants'], 'At least one [[variants]] entry is required')
    for v in raw['variants']:
        unknown(v, VARIANT_KEYS, 'variant')
        identifier(v.get('id'), 'variant.id')
        require(v['id'] not in ids, f'Duplicate variant {v["id"]}')
        ids.add(v['id'])
        editor = v.get('editor')
        require(isinstance(editor, str) and editor or isinstance(editor, list) and editor and all(isinstance(x, str) for x in editor),
                f'variant {v["id"]}: editor must be the Unity executable path (or a command list that ends up running one)')
        require(v.get('backend', 'mono') in BACKENDS, f'variant {v["id"]}: backend must be one of {BACKENDS}')
        require(v.get('code_optimization', 'default') in ('default', 'debug', 'release'), f'variant {v["id"]}: code_optimization must be default, debug or release')
        require(v.get('revision') is None or isinstance(v['revision'], str), f'variant {v["id"]}: revision must be a string')
        variants.append({'backend': 'mono', 'code_optimization': 'default', 'unity_version': None, 'revision': None, **v,
                         'build': {**shared_build, **build_settings(v.get('build') or {}, f'variant {v["id"]}.build')}})
    if defaults['baseline'] is not None:
        require(defaults['baseline'] in ids, 'defaults.baseline must name a variant')
    else:
        defaults['baseline'] = variants[0]['id']
    for v in variants:
        # The variant a ratio is taken against. A variant that is its own baseline gets no ratio column.
        v['baseline'] = v.get('baseline') or defaults['baseline']
        require(v['baseline'] in ids, f'variant {v["id"]}: baseline must name a variant')

    workloads, wids = [], set()
    require(isinstance(raw.get('workloads'), list) and raw['workloads'], 'At least one [[workloads]] entry is required')
    for w in raw['workloads']:
        require(w.get('kind') in KINDS, f'workload kind must be one of {", ".join(KINDS)}')
        require('state' not in w or w['kind'] in states.STATE_KINDS,
                f"workload {w.get('id')}: state applies to workloads that start an Editor on the project; {w['kind']} runs a built player (set it on the build workload)")
        unknown(w, COMMON_KEYS | KINDS[w['kind']].get('keys', set()) | ({'state'} if w['kind'] in states.STATE_KINDS else set()) | ({'memory'} if w['kind'] in MEMORY_KINDS else set()),
                'workload ' + str(w.get('id')))
        identifier(w.get('id'), 'workload.id')
        require(w['id'] not in wids, f'Duplicate workload {w["id"]}')
        wids.add(w['id'])
        merged = {k: defaults[k] for k in ('repeats', 'rounds', 'timeout_seconds')}
        if w['kind'] in ('player.build_time', 'player.startup'):
            merged['timeout_seconds'] = defaults['build_timeout_seconds']
        if w['kind'] in LEAK_KINDS:
            merged['rounds'] = LEAK_ROUNDS[w['kind']]
        merged.update(w)
        for key in ('repeats', 'rounds', 'timeout_seconds'):
            positive_int(merged[key], f'workload {w["id"]}.{key}')
        headline = merged.get('headline')
        require(headline is None or headline is True or isinstance(headline, list) and headline and all(isinstance(x, str) and x for x in headline),
                f'workload {w["id"]}: headline must be true (every item) or a list of item-name patterns')
        selected = merged.setdefault('variants', [v['id'] for v in variants])
        require(isinstance(selected, list) and selected and set(selected) <= ids, f'workload {w["id"]}: variants must be a list of known variant ids')
        check_kind(merged)
        workloads.append(merged)
    revisions = {v['id'] for v in variants if v['revision']}
    for w in workloads:
        if w['kind'] in ('player.startup', 'player.adapter') and w.get('build'):
            ref = [x for x in workloads if x['id'] == w['build']]
            require(ref and ref[0]['kind'] == 'player.build_time' and workloads.index(ref[0]) < workloads.index(w),
                    f'workload {w["id"]}: build must name a player.build_time workload defined before it')
            require(set(w['variants']) <= set(ref[0]['variants']), f'workload {w["id"]}: build workload must cover the same variants')
        if w['kind'] in ('editor.script_edit', 'editor.reload_leak') and any(t['root'] == 'worktree' for t in w['targets']):
            require(set(w['variants']) <= revisions, f'workload {w["id"]}: targets with root = "worktree" need every selected variant to set a revision')
    return {'name': raw['name'], 'project': {'path': str(root), 'repository': str(repository) if repository else None, 'inject_package': inject,
                                             'prepare': prepare, 'prepare_inputs': prepare_inputs, 'campaign_dir': str(base.resolve())},
            'cache': {'isolate': isolate}, 'defaults': defaults, 'variants': variants, 'workloads': workloads}


def build_settings(table, what):
    """The optional player build settings of a campaign or variant: enum names as the Editor spells them, validated by the Editor itself."""
    require(isinstance(table, dict), f'{what} must be a table')
    unknown(table, BUILD_SETTING_KEYS, what)
    for key, value in table.items():
        require(isinstance(value, str) and re.fullmatch(r'\w+', value), f'{what}.{key} must be an enum name such as NET_Standard')
    return dict(table)


def check_kind(w):
    kind, wid = w['kind'], w['id']
    if kind in states.STATE_KINDS:
        w.setdefault('state', states.default_state(kind))
        require(w['state'] in states.STATES, f'workload {wid}: state must be one of {", ".join(states.STATES)}')
    if kind in MEMORY_KINDS:
        w.setdefault('memory', True)
        require(isinstance(w['memory'], bool), f'workload {wid}: memory must be a boolean')
    if kind in LEAK_KINDS:
        require(w['memory'], f'workload {wid}: {kind} reads memory after every cycle; memory = false leaves nothing to report')
        require(w['rounds'] >= 3, f'workload {wid}: rounds must be at least 3 to fit a slope')
        for key in ('force_gc', 'memory_snapshot', 'alc_leak_detection'):
            w.setdefault(key, key == 'force_gc')
            require(isinstance(w[key], bool), f'workload {wid}: {key} must be a boolean')
    if kind in ('editor.script_edit', 'editor.reload_leak'):
        targets = w.get('targets')
        require(isinstance(targets, list) and targets, f'workload {wid}: targets must be a non-empty list')
        seen = set()
        for t in targets:
            unknown(t, {'id', 'path', 'change', 'root'}, f'workload {wid} target')
            identifier(t.get('id'), 'target.id')
            require(t['id'] not in seen, f'workload {wid}: duplicate target {t["id"]}')
            seen.add(t['id'])
            require(isinstance(t.get('path'), str) and t['path'].endswith('.cs') and project_relative(t['path']),
                    f'workload {wid}: target path must be a project-relative .cs file')
            t.setdefault('change', 'comment')
            require(t['change'] in TARGET_CHANGES, f'workload {wid}: target change must be one of {TARGET_CHANGES}')
            t.setdefault('root', 'project')   # 'worktree': path is relative to the variant's git worktree (for files outside the Unity project)
            require(t['root'] in TARGET_ROOTS, f'workload {wid}: target root must be one of {TARGET_ROOTS}')
    elif kind in ('editor.play', 'editor.play_leak'):
        w.setdefault('domain_reload', 'default')
        require(w['domain_reload'] in ('default', 'disabled'), f'workload {wid}: domain_reload must be default or disabled')
        w.setdefault('scene', None)
    elif kind in SCENE_KINDS:
        check_scene(w)
    elif kind in ('editor.adapter', 'player.adapter'):
        require(isinstance(w.get('method'), str) and re.fullmatch(r'[\w.]+\.\w+', w['method']), f'workload {wid}: method must be Namespace.Type.Method')
        args = w.setdefault('args', {})
        require(isinstance(args, dict) and all(isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in args.items()), f'workload {wid}: args must be a flat table')
    if kind.startswith('player.'):
        check_player(w)


def check_scene(w):
    """The scene, how long to warm up and record, and the optional clock settings of a scene workload."""
    wid = w['id']
    require(isinstance(w.get('scene'), str) and w['scene'].endswith('.unity') and project_relative(w['scene']), f'workload {wid}: scene must be a project-relative .unity file')
    for what, default in (('duration', 10), ('warmup', 2)):
        seconds, frames = f'{what}_seconds', f'{what}_frames'
        require(seconds not in w or frames not in w, f'workload {wid}: give {seconds} or {frames}, not both')
        if frames in w:
            require(type(w[frames]) is int and (w[frames] > 0 if what == 'duration' else w[frames] >= 0), f'workload {wid}.{frames} must be a {"positive" if what == "duration" else "non-negative"} integer')
        else:
            w.setdefault(seconds, default)
            require(isinstance(w[seconds], (int, float)) and not isinstance(w[seconds], bool) and (w[seconds] > 0 if what == 'duration' else w[seconds] >= 0),
                    f'workload {wid}.{seconds} must be a {"positive" if what == "duration" else "non-negative"} number')
    clock = w.setdefault('time', {})
    require(isinstance(clock, dict), f'workload {wid}: time must be a table')
    unknown(clock, TIME_KEYS, f'workload {wid} time')
    for key, value in clock.items():
        require(isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 1, f'workload {wid}.time.{key} must be a number of seconds in (0, 1]')
    w.setdefault('vsync', False)
    w.setdefault('list_markers', False)
    require(isinstance(w['vsync'], bool) and isinstance(w['list_markers'], bool), f'workload {wid}: vsync and list_markers must be booleans')
    if w['kind'] == 'player.scene':
        w['scenes'] = [w['scene']]   # the build holds exactly this scene


def check_player(w):
    kind, wid = w['kind'], w['id']
    scenes = w.get('scenes')
    if scenes is None and w.get('scene'):
        scenes = [w['scene']]
    w.pop('scene', None)
    w['scenes'] = scenes or []
    require(isinstance(w['scenes'], list) and all(isinstance(x, str) and x.endswith('.unity') and project_relative(x) for x in w['scenes']),
            f'workload {wid}: scenes must be a list of project-relative .unity files')
    w.setdefault('development', False)
    w.setdefault('build_target', None)
    require(w.get('backend') is None or w['backend'] in BACKENDS, f'workload {wid}: backend must be one of {BACKENDS}')
    w.setdefault('prebuild', None)
    require(w['prebuild'] is None or isinstance(w['prebuild'], str) and re.fullmatch(r'[\w.]+\.\w+', w['prebuild']), f'workload {wid}: prebuild must be Namespace.Type.Method')
    if kind == 'player.build_time':
        w.setdefault('mode', 'clean')
        require(w['mode'] in ('clean', 'incremental'), f'workload {wid}: mode must be clean or incremental')
        return
    w.setdefault('build', None)
    w.setdefault('headless', kind == 'player.startup')   # an adapter usually wants real frames; startup does not
    require(isinstance(w['headless'], bool), f'workload {wid}: headless must be a boolean')
    w.setdefault('window', '1280x800')
    require(isinstance(w['window'], str) and re.fullmatch(r'\d{2,5}x\d{2,5}', w['window']), f'workload {wid}: window must look like 1280x800')
    w.setdefault('player_args', [])
    require(isinstance(w['player_args'], list) and all(isinstance(x, str) for x in w['player_args']), f'workload {wid}: player_args must be a list of strings')
    if kind == 'player.startup':
        positive_int(w.setdefault('startup_timeout_seconds', 120), f'workload {wid}.startup_timeout_seconds')


def variant_orders(ids):
    """Variant orders for consecutive repeats, so every compared pair runs in both orders equally often.

    Two variants alternate (A,B then B,A). More variants use each rotation followed by its reverse; a pair of
    consecutive repeats then flips every pair, and 2n repeats also put each variant in each position equally often.
    """
    n = len(ids)
    if n < 3:
        return [ids, ids[::-1]] if n == 2 else [ids]
    rotations = [ids[i:] + ids[:i] for i in range(n)]
    return [order for r in rotations for order in (r, r[::-1])]


def balanced_repeats(requested, variant_count):
    """More than two variants need an even number of repeats to balance pair order; two alternate and keep the number asked for."""
    return requested + requested % 2 if variant_count > 2 else requested


def schedule(campaign, only=None, notes=None):
    """Freeze every process before anything runs. `notes` (a list) collects what the schedule changed on purpose."""
    workloads = {w['id']: w for w in campaign['workloads']}
    needed = {}   # --only: the build workloads of the selected ones are pulled in (one repeat), or their launches would have no player
    for wid in only or ():
        w = workloads.get(wid)
        if w and w.get('build') and w['build'] not in only:
            needed.setdefault(w['build'], set()).update(w['variants'])
    rows = []
    for w in campaign['workloads']:
        pulled = w['id'] in needed
        if only and w['id'] not in only and not pulled:
            continue
        order = [v['id'] for v in campaign['variants'] if v['id'] in w['variants'] and (not pulled or v['id'] in needed[w['id']])]
        repeats = 1 if pulled else balanced_repeats(w['repeats'], len(order))
        if notes is not None and pulled:
            notes.append(f"{w['id']}: pulled in by --only for its player builds, 1 repeat")
        elif notes is not None and repeats != w['repeats']:
            notes.append(f"{w['id']}: repeats {w['repeats']} -> {repeats} so that every pair of {len(order)} variants runs in both orders equally often")
        orders = variant_orders(order)
        for repeat in range(repeats):
            for variant in orders[repeat % len(orders)]:
                rows.append({'index': len(rows), 'workload': w['id'], 'variant': variant, 'repeat': repeat})
    return rows
