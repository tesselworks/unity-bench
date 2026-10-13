"""The small statistics the report uses: per-process medians, ratio, range separation, an optional bootstrap. Descriptive only."""
import math
import random
import statistics

RESOLUTION_MS = 0.001   # a median below this prints as "<res": the stopwatch tick (or the report's finest digit) is all there is


def describe(values):
    """n, median, min, max and the values themselves (one per process) of a non-empty list, else None."""
    if not values:
        return None
    return {'n': len(values), 'median': statistics.median(values), 'min': min(values), 'max': max(values), 'values': list(values)}


def process_medians(samples):
    """Median of each process's samples, in process order. One process is the unit of repeat."""
    by_process = {}
    for s in samples:
        by_process.setdefault(s['index'], []).append(s['ms'])
    return [statistics.median(v) for _, v in sorted(by_process.items())]


def chance_separation(a, b):
    """Chance that groups of a and b values drawn from the same distribution have completely separated ranges, in either direction.

    Of the C(a+b, a) equally likely ways to label a+b ordered values as group A or B, exactly two separate the groups: 2 / C(a+b, a).
    For equal n this is 2 / C(2n, n): 33% for n = 2, 10% for n = 3, 2.9% for n = 4.
    """
    return 2 / math.comb(a + b, a)


def compare(base, other, resolution=RESOLUTION_MS):
    """Ratio other/base of the two medians-of-process-medians, the n of each side, and whether the process ranges overlap.

    A median below `resolution` on either side has no meaningful ratio: the result says `below_resolution` and has `ratio` None.
    """
    if not base or not other:
        return None
    n = [base['n'], other['n']]
    if base['median'] < resolution or other['median'] < resolution:
        return {'ratio': None, 'below_resolution': True, 'n': n}
    separated = other['max'] < base['min'] or base['max'] < other['min']
    return {'ratio': other['median'] / base['median'], 'ranges_overlap': not separated, 'n': n, 'chance_separated': chance_separation(*n)}


def geomean(ratios):
    return math.exp(statistics.fmean(math.log(r) for r in ratios))


def bootstrap_ratio(base, other, resamples, confidence=0.95, seed=12345):
    """Percentile bootstrap interval of the ratio of medians: resample the processes of each side with replacement.

    A description of how much the few processes you have disagree. With n = 2 per side only three medians exist per side, so the
    interval is coarse; it does not turn a small experiment into a large one. The seed is fixed so a report regenerates identically.
    """
    rng = random.Random(seed)
    ratios = sorted(statistics.median(rng.choices(other, k=len(other))) / m for _ in range(resamples)
                    if (m := statistics.median(rng.choices(base, k=len(base)))) > 0)
    if not ratios:
        return None
    tail = (1 - confidence) / 2
    return [ratios[int(tail * len(ratios))], ratios[min(len(ratios) - 1, math.ceil((1 - tail) * len(ratios)) - 1)]]
