#!/usr/bin/env python3
"""Write a new contract-fixture campaign for a committed adapter in a local repository."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repository', required=True, type=Path)
parser.add_argument('--revision', required=True)
parser.add_argument('--project-path', default='examples/portable-contract')
parser.add_argument('--out', required=True, type=Path)
args = parser.parse_args()
if not re.fullmatch('[0-9a-f]{40}', args.revision):
    parser.error('revision must be an exact 40-character commit')
repository = args.repository.resolve()
project = Path(args.project_path)
if project.is_absolute() or '..' in project.parts:
    parser.error('project-path must stay inside the checkout')
subprocess.run(['git', '-C', str(repository), 'cat-file', '-e', args.revision + ':' + project.as_posix() + '/adapter.py'], check=True)
config = {'schemaVersion': 1, 'name': 'portable-contract', 'repository': str(repository),
          'variants': [{'id': side, 'revision': args.revision, 'unityVersion': 'not-unity', 'backend': 'python-contract-fixture'} for side in ('a', 'b')],
          'axes': [{'id': 'contract', 'left': 'a', 'right': 'b'}], 'repetitions': 3, 'calibrationRepetitions': 1,
          'workloads': [{'id': 'fixedIntegerWork', 'projectPath': project.as_posix(), 'mode': 'samples', 'warmIterations': 3,
                         'timeoutSeconds': 30, 'command': [sys.executable, '{project}/adapter.py', '{request}']}]}
with args.out.open('x', encoding='utf-8') as stream:
    json.dump(config, stream, indent=2)
    stream.write('\n')
