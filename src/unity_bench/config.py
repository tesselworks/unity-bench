"""A versioned, shell-free campaign contract."""
import hashlib
import json
import ntpath
from pathlib import Path
import re
import string

PLACEHOLDERS = {'checkout', 'project', 'unity', 'request', 'result', 'variant', 'workload', 'unityVersion', 'backend', 'revision'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def positive(value, label, maximum):
    require(type(value) is int and 0 < value <= maximum, label + ' must be a bounded positive integer')


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}', value), 'Invalid identifier')


def absolute_identity(value):
    # Stored Windows evidence must be replayable on a different OS without rewriting its paths/hashes.
    return isinstance(value, str) and (Path(value).is_absolute() or ntpath.isabs(value))


def read_json(path):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'Duplicate JSON field')
            value[key] = item
        return value
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs)


def argv(value):
    require(isinstance(value, list) and value and all(isinstance(v, str) and v and '\0' not in v for v in value), 'Command must be nonempty argv, never shell text')
    for token in value:
        for _, field, spec, conversion in string.Formatter().parse(token):
            if field is not None:
                require(field in PLACEHOLDERS and not spec and not conversion, 'Unknown command placeholder')


def validate(value):
    require(isinstance(value, dict) and type(value.get('schemaVersion')) is int and value['schemaVersion'] == 1, 'Unsupported campaign schema')
    identifier(value.get('name'))
    require(isinstance(value.get('repository'), str) and value['repository'], 'Repository path required')
    positive(value.get('repetitions', 3), 'repetitions', 100)
    calibration = value.get('calibrationRepetitions', 3)
    require(type(calibration) is int and 0 <= calibration <= 100, 'Invalid calibration count')
    host = value.get('host', {'mode': 'none'})
    require(isinstance(host, dict) and host.get('mode') in ('none', 'windows-shared-host'), 'Unknown host policy')
    if host['mode'] == 'windows-shared-host':
        threshold = host.get('foreignCpuReferencePercent', 3)
        require(type(threshold) in (int, float) and threshold == 3, 'Shared-host foreign CPU reference is fixed at 3%')
        require(host.get('backgroundCpuLimitPercent', .5) == .5, 'Owned background limit is fixed at 0.5%')
    for key in ('variants', 'axes', 'workloads'):
        require(isinstance(value.get(key), list) and value[key], key + ' cannot be empty')
    variants = set()
    for variant in value['variants']:
        require(isinstance(variant, dict), 'Variant must be an object')
        identifier(variant.get('id'))
        require(variant['id'] not in variants, 'Duplicate variant')
        variants.add(variant['id'])
        require(isinstance(variant.get('revision'), str) and re.fullmatch('[0-9a-f]{40}', variant['revision']), 'Exact 40-character commit required')
        for field in ('unityVersion', 'backend'):
            require(isinstance(variant.get(field), str) and variant[field], 'Variant requires ' + field)
        if 'unityExecutable' in variant:
            require(absolute_identity(variant['unityExecutable']), 'Unity executable must be absolute')
        if host['mode'] == 'windows-shared-host':
            require(absolute_identity(variant.get('editorExecutable')), 'Host control requires exact installed Editor executable')
    axes = set()
    for axis in value['axes']:
        require(isinstance(axis, dict), 'Axis must be an object')
        identifier(axis.get('id'))
        require(axis['id'] not in axes, 'Duplicate axis')
        axes.add(axis['id'])
        require(axis.get('left') in variants and axis.get('right') in variants and axis['left'] != axis['right'], 'Axis requires two distinct variants')
    workloads = set()
    for workload in value['workloads']:
        require(isinstance(workload, dict), 'Workload must be an object')
        identifier(workload.get('id'))
        require(workload['id'] not in workloads, 'Duplicate workload')
        workloads.add(workload['id'])
        require(isinstance(workload.get('projectPath'), str), 'Project path must be a string')
        path = Path(workload['projectPath'])
        require(str(path) != '.' and not path.is_absolute() and '..' not in path.parts, 'Project path must stay inside checkout')
        require(workload.get('mode') in ('samples', 'external'), 'Unknown clock mode')
        argv(workload.get('command'))
        positive(workload.get('timeoutSeconds', 300), 'timeoutSeconds', 86400)
        positive(workload.get('warmIterations', 3), 'warmIterations', 10000)
        require(isinstance(workload.get('prepare', []), list), 'prepare must contain argv arrays')
        for command in workload.get('prepare', []):
            argv(command)
    return value


def read(path):
    path = Path(path).resolve()
    value = validate(read_json(path))
    repository = Path(value['repository'])
    value['repository'] = str((path.parent / repository).resolve()) if not repository.is_absolute() else str(repository.resolve())
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def schedule(config):
    """Freeze all processes before execution; no replacement or quiet selection."""
    rows = []
    for workload in config['workloads']:
        for variant in config['variants']:
            for repeat in range(config.get('calibrationRepetitions', 3)):
                rows.append({'kind': 'calibration', 'axis': None, 'pair': repeat, 'variant': variant['id'], 'workload': workload['id']})
        for axis in config['axes']:
            for pair in range(config.get('repetitions', 3)):
                order = (axis['left'], axis['right']) if pair % 2 == 0 else (axis['right'], axis['left'])
                for variant in order:
                    rows.append({'kind': 'comparison', 'axis': axis['id'], 'pair': pair, 'variant': variant, 'workload': workload['id']})
    for index, row in enumerate(rows):
        row['index'] = index
    return rows
