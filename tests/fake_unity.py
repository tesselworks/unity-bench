"""A stand-in for the Unity Editor so the runner can be tested without Unity.

It understands just the arguments the runner passes: -projectPath, -logFile, -quit, -executeMethod and
-unityBenchRequest. Behaviour is chosen by its own options, given before the Unity arguments:
  --scale F        multiply every reported time by F (a "faster" or "slower" Editor)
  --salt S         mix S into the hash of the item named in --salt-item (different output between variants)
  --salt-item N    item name that gets the salt (default: none)
  --fail           exit 1 without writing a result
  --hang           sleep for a minute (to exercise the timeout)
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--scale', type=float, default=1.0)
parser.add_argument('--salt', default='')
parser.add_argument('--salt-item', default='')
parser.add_argument('--fail', action='store_true')
parser.add_argument('--hang', action='store_true')
ours, unity = parser.parse_known_args()


def arg(name):
    return unity[unity.index(name) + 1] if name in unity else None


project = Path(arg('-projectPath'))
log = Path(arg('-logFile'))
if ours.fail:
    sys.exit(1)
if ours.hang:
    time.sleep(60)
(project / 'Library').mkdir(exist_ok=True)
lines = ['Unity fake editor']
env = {'unityVersion': 'fake-1', 'runtime': 'fake runtime', 'runtimeKind': 'mono', 'codeOptimization': 'Debug', 'backend': 'Mono2x'}


def block(total):
    return [f'Domain Reload Profiling: {total}ms', f'\tBeginReloadAssembly ({total // 4}ms)', f'\tLoadAllAssembliesAndSetupDomain ({total // 4}ms)',
            f'\tFinalizeReload ({total // 2}ms)', '']


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


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
    Path(req['outPath']).write_text(json.dumps({'schema': 1, 'completed': True, 'error': None, 'environment': env, 'samples': samples, 'notes': {}}))
else:
    time.sleep(0.05 * ours.scale)
log.write_text('\n'.join(lines) + '\n')
