"""Read and check a result file (docs/adapters.md). A malformed result is a failed process, never an exception that stops the schedule."""
import json
from pathlib import Path

from . import distributions
from .distributions import counters_ok, finite

PHASES = ('first-use', 'warm', 'warmup', 'steady')


class ResultError(ValueError):
    pass


def check_sample(s):
    return (isinstance(s, dict) and isinstance(s.get('name'), str) and s['name'] and s.get('phase') in (None, '', *PHASES)
            and finite(s.get('ms')) and s['ms'] >= 0 and (s.get('hash') is None or isinstance(s['hash'], str)) and counters_ok(s.get('counters'))
            and isinstance(s.get('info') or {}, dict) and (s.get('workGroup') is None or isinstance(s['workGroup'], str))
            and s.get('unit') in (None, *distributions.UNITS))


def work_groups(data):
    groups = data.get('workGroups') or {}
    if not isinstance(groups, dict) or not all(isinstance(g, dict) and (g.get('hash') is None or isinstance(g['hash'], str))
                                               and counters_ok(g.get('counters')) for g in groups.values()):
        raise ResultError('invalid workGroups')
    return groups


def derive_phases(samples):
    """Fill in the phase of untagged samples from how often the name occurs in this process: the first of several is
    first-use, later ones warm, and a name measured once is `single`. Nothing is called warm without a repeat to back it up."""
    counts, earlier = {}, set()
    for s in samples:
        counts[s['name']] = counts.get(s['name'], 0) + 1
    for s in samples:
        if not s.get('phase'):
            s['phase'] = 'single' if counts[s['name']] == 1 else 'warm' if s['name'] in earlier else 'first-use'
        earlier.add(s['name'])


def read_result(path):
    """Load and check an adapter/built-in result file. Samples come back normalised: derived phase, inherited work identity, distributions expanded."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ResultError(f'no readable result file ({type(error).__name__})') from None
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ResultError('result schema must be 1')
    if data.get('completed') is not True:
        raise ResultError('adapter reported failure: ' + str(data.get('error') or 'no error text').splitlines()[0][:300])
    try:
        json.dumps(data, allow_nan=False)   # results.jsonl is strict JSON: a NaN hiding in notes or info would abort the run when written
    except ValueError:
        raise ResultError('result contains NaN or Infinity') from None
    samples, dists = data.get('samples') or [], data.get('distributions') or []
    if not isinstance(samples, list) or not isinstance(dists, list):
        raise ResultError('samples and distributions must be lists')
    for s in samples:
        if not check_sample(s):
            raise ResultError('invalid sample: ' + json.dumps(s, default=str)[:200])
    for d in dists:
        if not distributions.check(d):
            raise ResultError('invalid distribution: ' + json.dumps(d, default=str)[:200])
    groups = work_groups(data)
    headline = data.get('headline') or []
    if not isinstance(headline, list) or not all(isinstance(x, str) and x for x in headline):
        raise ResultError('headline must be a list of item-name patterns')
    for key in ('environment', 'notes'):
        if data.get(key) is not None and not isinstance(data[key], dict):
            raise ResultError(f'{key} must be an object')
    out = [{'name': s['name'], 'phase': s.get('phase') or None, 'round': s['round'] if isinstance(s.get('round'), int) else 0, 'ms': s['ms'],
            'hash': s.get('hash'), 'counters': s.get('counters') or {}, 'info': s.get('info') or {}, 'work_group': s.get('workGroup'), 'unit': s.get('unit')} for s in samples]
    derive_phases(out)
    out += distributions.to_samples(dists)
    for s in out:   # a sample that reports no identity of its own inherits its work group's
        group = s['work_group']
        if group is not None and group not in groups:
            raise ResultError(f'sample {s["name"]!r} names the undefined work group {group!r}')
        if group is not None and s['hash'] is None and not s['counters']:
            s['hash'], s['counters'] = groups[group].get('hash'), groups[group].get('counters') or {}
    if not out:
        raise ResultError('result has no samples')
    return {**data, 'samples': out, 'headline': headline}
