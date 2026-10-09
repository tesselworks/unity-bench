#!/usr/bin/env python3
"""Executable protocol fixture, NOT a Unity workload or performance baseline."""
import hashlib
import json
from pathlib import Path
import sys
import time

request = json.loads(Path(sys.argv[1]).read_text())
samples = []
answer = None
for iteration in range(request['warmIterations'] + 1):
    start = time.perf_counter()
    answer = sum(index * index for index in range(4096))
    elapsed = (time.perf_counter() - start) * 1000
    samples.append({'iteration': iteration, 'phase': 'first' if iteration == 0 else 'warm', 'metric': 'fixedIntegerWork', 'value': elapsed, 'unit': 'ms'})
result = {'schemaVersion': 1, 'runId': request['runId'], 'variant': request['variant'], 'workload': request['workload'],
          'status': 'completed', 'environment': {'unityVersion': 'not-unity', 'backend': 'python-contract-fixture',
          'settings': {'integerCount': 4096}}, 'inputFingerprint': hashlib.sha256(b'integers:0..4095').hexdigest(),
          'outputFingerprint': hashlib.sha256(str(answer).encode()).hexdigest(), 'workCounters': {'integers': 4096}, 'samples': samples}
with Path(request['outputPath']).open('x', encoding='utf-8') as stream:
    json.dump(result, stream, allow_nan=False)
