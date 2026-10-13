"""Work and output identity: do two variants do the same work for one item? Reported, never enforced."""
import json


def fingerprint(sample):
    if sample.get('hash') is None and not sample.get('counters'):
        return None
    return json.dumps({'hash': sample.get('hash'), 'counters': sample.get('counters')}, sort_keys=True)


def identity(samples, variants):
    """Classify one item from every sample of every variant, first-use included.

    'identical'  every sample reported a hash/counters and all are equal
    'partial'    the ones reported are equal, but some samples (another variant, or some rounds) reported nothing
    'differs'    some differ; `detail` says whether the variants differ or one variant is unstable
    'unreported' no sample carried a hash or counters
    `coverage` is [samples that reported, samples] per variant: a pair where only one side reports is not "same".
    """
    seen = {v: {} for v in variants}
    coverage = {v: [0, 0] for v in variants}
    for s in samples:
        f = fingerprint(s)
        coverage[s['variant']][1] += 1
        if f is not None:
            coverage[s['variant']][0] += 1
            seen[s['variant']].setdefault(f, s)
    coverage = {v: c for v, c in coverage.items() if c[1]}
    prints = {f for per in seen.values() for f in per}
    if not prints:
        return {'status': 'unreported', 'coverage': coverage}
    if len(prints) == 1:
        return {'status': 'identical' if all(r == t for r, t in coverage.values()) else 'partial', 'coverage': coverage}
    unstable = [v for v, per in seen.items() if len(per) > 1]
    shown = {v: [{'hash': s.get('hash'), 'counters': s.get('counters')} for s in per.values()] for v, per in seen.items() if per}
    return {'status': 'differs', 'coverage': coverage, 'detail': 'unstable within ' + ', '.join(unstable) if unstable else 'variants differ', 'values': shown}


PAIR_WORK = {'identical': 'same', 'partial': 'partial', 'unreported': 'not reported', 'differs': 'differs'}


def pair_identity(samples, base, other):
    """('same' | 'partial' | 'not reported' | 'differs', coverage) for one compared pair; a variant unstable within itself counts as differs."""
    result = identity([s for s in samples if s['variant'] in (base, other)], [base, other])
    return PAIR_WORK[result['status']], result['coverage']
