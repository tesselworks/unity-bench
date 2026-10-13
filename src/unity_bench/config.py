"""Campaign file loading, validation and the fixed schedule.

A campaign is one TOML (or JSON) file: variants (Unity Editor + optional project
revision + player scripting backend), workloads, and how often to repeat them.
"""
import json
from pathlib import Path
import re

import tomllib

KINDS = {
    'editor.cold_open': {'description': 'Delete Library, open the project in batchmode, quit.'},
    'editor.warm_open': {'description': 'Open the project in batchmode with its Library, quit.'},
    'editor.script_edit': {'description': 'Edit a script, time compile and compile+reload+idle.', 'keys': {'targets'}},
    'editor.play': {'description': 'Enter and exit Play Mode.', 'keys': {'domain_reload', 'scene'}},
    'editor.adapter': {'description': 'Run a project C# adapter inside the Editor.', 'keys': {'method', 'args'}},
    'player.build_time': {'description': 'BuildPipeline.BuildPlayer wall time.', 'keys': {'mode', 'development', 'scene', 'build_target', 'backend'}},
    'player.startup': {'description': 'Launch a built player until its first frame.', 'keys': {'development', 'scene', 'build_target', 'backend', 'build', 'headless', 'startup_timeout_seconds'}},
}
COMMON_KEYS = {'id', 'kind', 'repeats', 'rounds', 'timeout_seconds', 'variants'}
VARIANT_KEYS = {'id', 'editor', 'unity_version', 'backend', 'code_optimization', 'revision'}
DEFAULT_KEYS = {'repeats', 'rounds', 'timeout_seconds', 'exclude_first_use', 'baseline', 'build_timeout_seconds'}
TARGET_CHANGES = ('comment', 'api')
BACKENDS = ('mono', 'il2cpp', 'coreclr')


class ConfigError(ValueError):
    pass


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
    unknown(raw, {'name', 'project', 'defaults', 'variants', 'workloads'}, 'campaign')
    identifier(raw.get('name'), 'name')
    project = raw.get('project') or {}
    unknown(project, {'path', 'repository', 'inject_package'}, 'project')
    require(isinstance(project.get('path'), str) and project['path'], 'project.path is required')
    base = Path(base)
    root = (base / project['path']).resolve()
    inject = project.get('inject_package', True)
    require(isinstance(inject, bool), 'project.inject_package must be a boolean')
    repository = (base / project['repository']).resolve() if project.get('repository') else None

    defaults = {'repeats': 4, 'rounds': 5, 'timeout_seconds': 1800, 'build_timeout_seconds': 3600,
                'exclude_first_use': True, 'baseline': None, **(raw.get('defaults') or {})}
    unknown(defaults, DEFAULT_KEYS, 'defaults')
    for key in ('repeats', 'rounds', 'timeout_seconds', 'build_timeout_seconds'):
        positive_int(defaults[key], 'defaults.' + key)
    require(isinstance(defaults['exclude_first_use'], bool), 'defaults.exclude_first_use must be a boolean')

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
        variants.append({'backend': 'mono', 'code_optimization': 'default', 'unity_version': None, 'revision': None, **v})
    if defaults['baseline'] is not None:
        require(defaults['baseline'] in ids, 'defaults.baseline must name a variant')
    else:
        defaults['baseline'] = variants[0]['id']

    workloads, wids = [], set()
    require(isinstance(raw.get('workloads'), list) and raw['workloads'], 'At least one [[workloads]] entry is required')
    for w in raw['workloads']:
        require(w.get('kind') in KINDS, f'workload kind must be one of {", ".join(KINDS)}')
        unknown(w, COMMON_KEYS | KINDS[w['kind']].get('keys', set()), 'workload ' + str(w.get('id')))
        identifier(w.get('id'), 'workload.id')
        require(w['id'] not in wids, f'Duplicate workload {w["id"]}')
        wids.add(w['id'])
        merged = {k: defaults[k] for k in ('repeats', 'rounds', 'timeout_seconds')}
        if w['kind'].startswith('player.build') or w['kind'] == 'player.startup':
            merged['timeout_seconds'] = defaults['build_timeout_seconds']
        merged.update(w)
        for key in ('repeats', 'rounds', 'timeout_seconds'):
            positive_int(merged[key], f'workload {w["id"]}.{key}')
        selected = merged.setdefault('variants', [v['id'] for v in variants])
        require(isinstance(selected, list) and selected and set(selected) <= ids, f'workload {w["id"]}: variants must be a list of known variant ids')
        check_kind(merged)
        workloads.append(merged)
    for w in workloads:
        if w['kind'] == 'player.startup' and w.get('build'):
            ref = [x for x in workloads if x['id'] == w['build']]
            require(ref and ref[0]['kind'] == 'player.build_time', f'workload {w["id"]}: build must name a player.build_time workload')
            require(set(w['variants']) <= set(ref[0]['variants']), f'workload {w["id"]}: build workload must cover the same variants')
    return {'name': raw['name'], 'project': {'path': str(root), 'repository': str(repository) if repository else None, 'inject_package': inject},
            'defaults': defaults, 'variants': variants, 'workloads': workloads}


def check_kind(w):
    kind, wid = w['kind'], w['id']
    if kind == 'editor.script_edit':
        targets = w.get('targets')
        require(isinstance(targets, list) and targets, f'workload {wid}: targets must be a non-empty list')
        seen = set()
        for t in targets:
            unknown(t, {'id', 'path', 'change'}, f'workload {wid} target')
            identifier(t.get('id'), 'target.id')
            require(t['id'] not in seen, f'workload {wid}: duplicate target {t["id"]}')
            seen.add(t['id'])
            require(isinstance(t.get('path'), str) and t['path'].endswith('.cs') and not Path(t['path']).is_absolute() and '..' not in Path(t['path']).parts,
                    f'workload {wid}: target path must be a project-relative .cs file')
            t.setdefault('change', 'comment')
            require(t['change'] in TARGET_CHANGES, f'workload {wid}: target change must be one of {TARGET_CHANGES}')
    elif kind == 'editor.play':
        w.setdefault('domain_reload', 'default')
        require(w['domain_reload'] in ('default', 'disabled'), f'workload {wid}: domain_reload must be default or disabled')
        w.setdefault('scene', None)
    elif kind == 'editor.adapter':
        require(isinstance(w.get('method'), str) and re.fullmatch(r'[\w.]+\.\w+', w['method']), f'workload {wid}: method must be Namespace.Type.Method')
        args = w.setdefault('args', {})
        require(isinstance(args, dict) and all(isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in args.items()), f'workload {wid}: args must be a flat table')
    elif kind in ('player.build_time', 'player.startup'):
        w.setdefault('development', False)
        w.setdefault('scene', None)
        w.setdefault('build_target', None)
        require(w.get('backend') is None or w['backend'] in BACKENDS, f'workload {wid}: backend must be one of {BACKENDS}')
        if kind == 'player.build_time':
            w.setdefault('mode', 'clean')
            require(w['mode'] in ('clean', 'incremental'), f'workload {wid}: mode must be clean or incremental')
        else:
            w.setdefault('build', None)
            w.setdefault('headless', True)
            positive_int(w.setdefault('startup_timeout_seconds', 120), f'workload {wid}.startup_timeout_seconds')


def schedule(campaign, only=None):
    """Freeze every process before anything runs. Order rotates per repeat: A,B then B,A (then A,B ...)."""
    rows = []
    for w in campaign['workloads']:
        if only and w['id'] not in only:
            continue
        order = [v['id'] for v in campaign['variants'] if v['id'] in w['variants']]
        for repeat in range(w['repeats']):
            shift = repeat % len(order)
            for variant in order[shift:] + order[:shift]:
                rows.append({'index': len(rows), 'workload': w['id'], 'variant': variant, 'repeat': repeat})
    return rows
