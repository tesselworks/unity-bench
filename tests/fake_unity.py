"""A stand-in for the Unity Editor so the runner can be tested without Unity.

It understands just the arguments the runner passes: -projectPath, -logFile, -quit, -executeMethod and
-unityBenchRequest. A player.build_time request makes it write a "player": a small launcher that runs this same script
with --as-player, which understands -unityBenchAdapter / -unityBenchOut / -unityBenchArgs / -unityBenchSignal.
Behaviour is chosen by its own options, given before the Unity arguments:
  --scale F        multiply every reported time by F (a "faster" or "slower" Editor)
  --salt S         mix S into the hash of the item named in --salt-item (different output between variants)
  --salt-item N    item name that gets the salt (default: none)
  --fail           exit 1 without writing a result
  --hang           sleep for a minute (to exercise the timeout)
  --player-fail    the built player exits 1 without writing a result
  --player-hang    the built player sleeps for a minute
  --player-error   the built player writes completed=false with an error
  --player-bad-signal  the built player writes NaN into its first-frame signal
  --player-once    the built player ignores `rounds` and reports one sample per name (like a player adapter that runs once)
  --bad-result K   write a malformed result: null-sample, str-sample, nan-counter, bad-phase
  --scene-bodies N the scene workloads (player.scene, editor.scene_play) report the work counter bodies=N through a work group (a scene with hooks);
                   without it they report none ("work not verified")
  --scene-no-render  the scene workloads cannot record the rendering markers (headless-like)
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time

import fake_launcher

parser = argparse.ArgumentParser()
parser.add_argument('--scale', type=float, default=1.0)
parser.add_argument('--salt', default='')
parser.add_argument('--salt-item', default='')
parser.add_argument('--fail', action='store_true')
parser.add_argument('--hang', action='store_true')
parser.add_argument('--player-fail', action='store_true')
parser.add_argument('--player-hang', action='store_true')
parser.add_argument('--player-error', action='store_true')
parser.add_argument('--player-once', action='store_true')
parser.add_argument('--player-bad-signal', action='store_true')
parser.add_argument('--bad-result', default='')
parser.add_argument('--scene-bodies', type=int, default=0)
parser.add_argument('--scene-no-render', action='store_true')
parser.add_argument('--as-player', action='store_true')
ours, unity = parser.parse_known_args()


def arg(name):
    return unity[unity.index(name) + 1] if name in unity else None


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


SCENE_METHOD = 'UnityBench.Player.SceneWorkload.Run'


def scene_result(args, env):
    """What the package's scene workload reports: frame and marker distributions with units, totals, notes, and a work group when the scene made hook calls."""
    group = 'scene' if ours.scene_bodies else None
    steady = [float(i) * ours.scale for i in range(1, 101)]
    markers = ['Main Thread', 'PlayerLoop', 'GC Allocated In Frame'] + ([] if ours.scene_no_render else ['Camera.Render'])
    values = {'Main Thread': steady, 'PlayerLoop': [v / 2 for v in steady], 'Camera.Render': [v / 4 for v in steady],
              'GC Allocated In Frame': [0.0] * 90 + [1024.0 * ours.scale] * 10}
    dists = [{'name': 'frame', 'phase': 'warmup', 'values': [30.0 * ours.scale] * 3, 'unit': 'ms', 'workGroup': group},
             {'name': 'frame', 'phase': 'steady', 'values': steady, 'unit': 'ms', 'workGroup': group}]
    dists += [{'name': m, 'phase': 'steady', 'values': values[m], 'unit': 'bytes' if m.startswith('GC') else 'ms', 'workGroup': group} for m in markers]
    result = {'schema': 1, 'completed': True, 'error': None, 'environment': env, 'distributions': dists,
              'samples': [{'name': 'GC Allocated In Frame steady total', 'ms': 10240.0 * ours.scale, 'unit': 'bytes', 'workGroup': group},
                          {'name': 'GC collections gen0 steady', 'ms': 2.0, 'unit': 'count', 'workGroup': group}],
              'notes': {'markers.recorded': ', '.join(markers), 'markers.missing': '' if not ours.scene_no_render else 'Camera.Render', 'scene': args.get('scene', '')}}
    if group:
        result['workGroups'] = {group: {'hash': None, 'counters': {'bodies': ours.scene_bodies}}}
    return result


def run_player():
    """What a built player does for the runner's player.adapter and player.startup workloads."""
    if ours.fail:
        sys.exit(1)
    if ours.hang:
        time.sleep(60)
    Path(arg('-logFile')).write_text('fake player\n')
    env = {'unityVersion': 'fake-1', 'runtime': 'fake player runtime', 'runtimeKind': 'fake-player', 'backend': 'fake', 'development': False,
           'codeOptimization': 'Release build', 'platform': 'FakePlayer', 'graphicsDevice': 'FakeGL'}
    if arg('-unityBenchSignal'):
        Path(arg('-unityBenchSignal')).write_text(json.dumps({'schema': 1, 'environment': env, 'frame': 2,
                                                              'realtimeSinceStartupMs': float('nan') if ours.player_bad_signal else 100.0 * ours.scale}))
        return
    request = json.loads(Path(arg('-unityBenchArgs')).read_text())
    if ours.player_error:
        Path(arg('-unityBenchOut')).write_text(json.dumps({'schema': 1, 'completed': False, 'error': 'adapter blew up\nsecond line', 'samples': []}))
        sys.exit(1)
    if arg('-unityBenchAdapter') == SCENE_METHOD:
        Path(arg('-unityBenchOut')).write_text(json.dumps(scene_result({pair['k']: pair['v'] for pair in request['args']}, env)))
        return
    salt = ours.salt if ours.salt_item == 'frame' else ''
    steady = [float(i) * ours.scale for i in range(1, 101)]
    result = {'schema': 1, 'completed': True, 'error': None, 'environment': env, 'notes': {'method': arg('-unityBenchAdapter')},
              'samples': [{'name': 'alpha', 'phase': 'first-use' if r == 0 else 'warm', 'round': r, 'ms': 10.0 * ours.scale, 'hash': digest('alpha'),
                           'counters': {'rounds': request['rounds']}, 'info': {}} for r in range(1 if ours.player_once else request['rounds'])],
              'distributions': [{'name': 'frame', 'phase': 'warmup', 'values': [30.0 * ours.scale] * 3, 'hash': digest('frame' + salt), 'counters': {'frames': 3}},
                                {'name': 'frame', 'phase': 'steady', 'values': steady, 'hash': digest('frame' + salt), 'counters': {'frames': 100}}]}
    args = {pair['k']: pair['v'] for pair in request['args']}
    if args.get('echo'):
        result['notes']['echo'] = args['echo']
    Path(arg('-unityBenchOut')).write_text(json.dumps(result))


def write_player(output_dir):
    """A launcher that behaves like the built player: the same script, in player mode, with this process's own options."""
    options = [sys.executable, str(Path(__file__).resolve()), '--as-player', '--scale', str(ours.scale), '--salt', ours.salt, '--salt-item', ours.salt_item]
    options += ['--scene-bodies', str(ours.scene_bodies)] + ['--scene-no-render'] * ours.scene_no_render
    options += ['--fail'] * ours.player_fail + ['--hang'] * ours.player_hang + ['--player-error'] * ours.player_error + ['--player-once'] * ours.player_once + ['--player-bad-signal'] * ours.player_bad_signal
    output_dir.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        launcher = fake_launcher.write(output_dir, options)
    else:
        launcher = output_dir / 'UnityBenchPlayer'
        launcher.write_text('#!/bin/sh\nexec ' + ' '.join(shlex.quote(x) for x in options) + ' "$@"\n')
        launcher.chmod(0o755)
    return launcher


if ours.as_player:
    run_player()
    sys.exit(0)
project = Path(arg('-projectPath'))
log = Path(arg('-logFile'))
if ours.fail:
    sys.exit(1)
if ours.hang:
    time.sleep(60)
(project / 'Library').mkdir(exist_ok=True)
opens = project / 'Library' / 'opens.txt'   # how often this Library has been opened: a restored snapshot goes back to the count it was taken with
before = len(opens.read_text().split()) if opens.exists() else 0
with opens.open('a') as handle:
    handle.write('x\n')
lines = ['Unity fake editor', f'Library opens before this launch: {before}', f'Imports: total={5 if before == 0 else 0} (actual={5 if before == 0 else 0}, local cache=0, cache server=0)']
lines += [f'ENV {name}={os.environ.get(name, "")}' for name in ('BEE_CACHE_DIRECTORY', 'UPM_CACHE_ROOT')] + ['ARGS ' + ' '.join(unity)]
env = {'unityVersion': 'fake-1', 'unityVersionFull': 'fake-1 (abc123)', 'runtime': 'fake runtime', 'runtimeKind': 'mono', 'codeOptimization': 'Debug', 'backend': 'Mono2x'}


def block(total):
    return [f'Domain Reload Profiling: {total}ms', f'\tBeginReloadAssembly ({total // 4}ms)', f'\tLoadAllAssembliesAndSetupDomain ({total // 4}ms)',
            f'\tFinalizeReload ({total // 2}ms)', '']


request_path = arg('-unityBenchRequest')
if request_path:
    req = json.loads(Path(request_path).read_text())
    samples = []
    if req['kind'] == 'editor.adapter':
        for r in range(req['rounds']):
            for name, ms in (('alpha', 10.0), ('beta', 20.0)):
                salt = ours.salt if name == ours.salt_item else ''
                samples.append({'name': name, 'phase': 'first-use' if r == 0 else 'warm', 'round': r, 'ms': ms * ours.scale * (3 if r == 0 else 1),
                                'hash': digest(name + salt), 'counters': {'items': 100}, 'info': {}})
    elif req['kind'] == 'editor.script_edit':
        for r in range(req['rounds']):
            for t in req['targets']:
                lines += [f"[UnityBench] begin {t['id']} {r}", *block(1000), 'Asset Pipeline Refresh (id=0a0b): Total: 1.2 seconds - Initiated by X',
                          '\tSummary:', '\t\tScripting: domain reloads=1, domain reload time=400 ms, compile time=300 ms, other=0 ms', f"[UnityBench] end {t['id']} {r}"]
                for suffix, ms in (('compile', 300.0), ('compile+reload', 1000.0)):
                    samples.append({'name': f"{t['id']}/{suffix}", 'phase': 'first-use' if r == 0 else 'warm', 'round': r, 'ms': ms * ours.scale,
                                    'hash': None, 'counters': {}, 'info': {'assemblies': 1, 'reloaded': True}})
    notes = {}
    if req['kind'] == 'editor.scene_play':
        Path(req['outPath']).write_text(json.dumps(scene_result({pair['k']: pair['v'] for pair in req['args']}, env)))
        log.write_text('\n'.join(lines) + '\n')
        sys.exit(0)
    if req['kind'] == 'player.build_time':
        notes['playerExecutable'] = str(write_player(Path(req['outputDir'])))
        for r in range(req['rounds']):
            samples.append({'name': 'build', 'phase': 'first-use' if r == 0 else 'warm', 'round': r, 'ms': 5000.0 * ours.scale, 'hash': None, 'counters': {}, 'info': {}})
        notes['scenes'] = ','.join(req['scenes'])
        notes['prebuild'] = req['prebuild']
        notes['backend'] = req['backend']
        notes['build.scriptingBackend'] = req['backend']
        notes['build.apiCompatibility'] = req.get('apiCompatibility') or 'Default'
        notes['build.stripping'] = req.get('stripping') or 'Default'
    result = {'schema': 1, 'completed': True, 'error': None, 'environment': env, 'samples': samples, 'notes': notes}
    if ours.bad_result:
        bad = {'null-sample': [None], 'str-sample': ['alpha'], 'nan-counter': [{**samples[0], 'counters': {'items': float('nan')}}],
               'bad-phase': [{**samples[0], 'phase': 'hot'}]}[ours.bad_result] if samples else [None]
        result['samples'] = bad
    Path(req['outPath']).write_text(json.dumps(result))
else:
    time.sleep(0.05 * ours.scale)
log.write_text('\n'.join(lines) + '\n')
