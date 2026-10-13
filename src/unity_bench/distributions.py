"""Per-frame distributions reported by adapters, turned into ordinary samples.

An adapter that has thousands of per-frame times for a phase (a soak, a long Play Mode run) reports them as one
`distribution` instead of thousands of samples. The runner reduces each to p50, p95, p99 and max for that process and
stores four samples named `<name> <phase> <stat>`. The summary then does what it does for every item: median across
processes, min-max range, ratio. The raw frame times stay in the process's result file under `results/`.
"""
import math

STATS = (('p50', .50), ('p95', .95), ('p99', .99), ('max', 1.0))


def percentile(sorted_values, fraction):
    """Nearest-rank percentile of a sorted, non-empty list: the smallest value with at least `fraction` of the values at or below it."""
    return sorted_values[max(0, math.ceil(fraction * len(sorted_values)) - 1)]


def check(d):
    """True when `d` is a usable distribution entry from a result file."""
    return (isinstance(d, dict) and isinstance(d.get('name'), str) and d['name'] and isinstance(d.get('phase'), str) and d['phase']
            and isinstance(d.get('values'), list) and d['values']
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0 for x in d['values']))


def to_samples(entries):
    """Four samples per distribution: p50, p95, p99 and max of its values, each carrying the distribution's hash and counters."""
    out = []
    for d in entries:
        values = sorted(d['values'])
        for stat, fraction in STATS:
            out.append({'name': f"{d['name']} {d['phase']} {stat}", 'phase': 'warm', 'round': 0, 'ms': percentile(values, fraction),
                        'hash': d.get('hash'), 'counters': d.get('counters') or {}, 'info': {**(d.get('info') or {}), 'frames': len(values)}})
    return out
