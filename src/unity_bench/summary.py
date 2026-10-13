"""Summary math: per-process medians, median across processes, range, ratio, identity flags.

Everything is computed from the raw rows in results.jsonl, so a report can be regenerated
at any time and with different options. The raw data is never filtered on disk.
"""
import json
import math
import statistics

RELOAD_PHASES = ['total', 'BeginReloadAssembly', 'LoadAllAssembliesAndSetupDomain', 'TypeCache.ScanAssembly',
                 'FinalizeReload', 'ProcessInitializeOnLoadAttributes']


def read_rows(path):
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def describe(values):
    """n, median, min, max of a non-empty list, else None."""
    if not values:
        return None
    return {'n': len(values), 'median': statistics.median(values), 'min': min(values), 'max': max(values)}


def process_medians(samples):
    """Median of each process's samples, in process order. One process is the unit of repeat."""
    by_process = {}
    for s in samples:
        by_process.setdefault(s['index'], []).append(s['ms'])
    return [statistics.median(v) for _, v in sorted(by_process.items())]


def compare(base, other):
    """Ratio other/base of the two medians-of-process-medians, and whether the process ranges overlap."""
    if not base or not other or not base['median']:
        return None
    separated = other['max'] < base['min'] or base['max'] < other['min']
    return {'ratio': other['median'] / base['median'], 'ranges_overlap': not separated}


def fingerprint(sample):
    if sample.get('hash') is None and not sample.get('counters'):
        return None
    return json.dumps({'hash': sample.get('hash'), 'counters': sample.get('counters')}, sort_keys=True)


def identity(samples, variants):
    """Classify one item from every sample of every variant, first-use included.

    'identical'  all reported hashes/counters equal across processes and variants
    'differs'    some differ; `detail` says whether the variants differ or one variant is unstable
    'unreported' no sample carried a hash or counters
    """
    seen = {v: {} for v in variants}
    for s in samples:
        f = fingerprint(s)
        if f is not None:
            seen[s['variant']].setdefault(f, s)
    prints = {f for per in seen.values() for f in per}
    if not prints:
        return {'status': 'unreported'}
    if len(prints) == 1:
        return {'status': 'identical'}
    unstable = [v for v, per in seen.items() if len(per) > 1]
    shown = {v: [{'hash': s.get('hash'), 'counters': s.get('counters')} for s in per.values()] for v, per in seen.items() if per}
    return {'status': 'differs', 'detail': 'unstable within ' + ', '.join(unstable) if unstable else 'variants differ', 'values': shown}


def summarise(rows, exclude_first_use=True, include_differing=False, baseline=None, variant_order=None):
    """Build the summary dict that report.py renders and summary.json stores."""
    processes = [r for r in rows if r['type'] == 'process']
    samples = [r for r in rows if r['type'] == 'sample']
    reloads = [r for r in rows if r['type'] == 'reload']
    variants = variant_order or list(dict.fromkeys(r['variant'] for r in processes))
    baseline = baseline if baseline in variants else (variants[0] if variants else None)
    out = {'baseline': baseline, 'variants': variants, 'exclude_first_use': exclude_first_use, 'workloads': [],
           'failures': [{k: p.get(k) for k in ('index', 'workload', 'variant', 'repeat', 'status', 'error')} for p in processes if p['status'] != 'ok']}
    for workload in dict.fromkeys(p['workload'] for p in processes):
        mine = [s for s in samples if s['workload'] == workload]
        entry = {'workload': workload, 'kind': next(p['kind'] for p in processes if p['workload'] == workload),
                 'variants': [v for v in variants if any(p['variant'] == v and p['workload'] == workload for p in processes)],
                 'items': [], 'differing': [], 'first_use': [], 'reload': []}
        for name in dict.fromkeys(s['name'] for s in mine):
            item_samples = [s for s in mine if s['name'] == name]
            steady = [s for s in item_samples if not (exclude_first_use and s['phase'] == 'first-use')]
            first = [s for s in item_samples if s['phase'] == 'first-use']
            per_variant = {}
            for v in entry['variants']:
                vs = [s for s in steady if s['variant'] == v]
                stats = describe(process_medians(vs))
                if stats:
                    per_variant[v] = stats
            ident = identity(item_samples, entry['variants'])
            row = {'name': name, 'variants': per_variant, 'identity': ident, 'info': info_summary(steady, entry['variants']),
                   'ratios': {v: compare(per_variant.get(baseline), per_variant.get(v)) for v in entry['variants'] if v != baseline}}
            target = entry['differing'] if ident['status'] == 'differs' and not include_differing else entry['items']
            if per_variant or ident['status'] == 'differs':
                target.append(row)
            for v in entry['variants']:
                fs = [s['ms'] for s in first if s['variant'] == v]
                if fs:
                    entry['first_use'].append({'name': name, 'variant': v, 'ms': fs})
        entry['aggregate'] = aggregate(entry['items'], [v for v in entry['variants'] if v != baseline])
        entry['reload'] = reload_table(workload, reloads, entry['variants'], exclude_first_use)
        out['workloads'].append(entry)
    out['host'] = host_summary(processes)
    return out


def info_summary(samples, variants):
    """Per variant: which assembly counts were seen and in how many samples the Editor reloaded. Annotations, never compared."""
    out = {}
    for v in variants:
        mine = [s['info'] for s in samples if s['variant'] == v and s.get('info')]
        entry = {}
        counts = sorted({i['assemblies'] for i in mine if 'assemblies' in i})
        if counts:
            entry['assemblies'] = counts
        reloaded = [i['reloaded'] for i in mine if 'reloaded' in i]
        if reloaded:
            entry['reloaded'] = [sum(1 for x in reloaded if x), len(reloaded)]
        if entry:
            out[v] = entry
    return out


def aggregate(items, others):
    """Across the items of a workload: geometric mean of ratios and how many are separated (ranges do not overlap)."""
    out = {}
    for v in others:
        ratios = [i['ratios'][v] for i in items if i['ratios'].get(v)]
        if len(ratios) > 1:
            out[v] = {'items': len(ratios), 'geomean': math.exp(statistics.fmean(math.log(r['ratio']) for r in ratios if r['ratio'] > 0)),
                      'faster_separated': sum(1 for r in ratios if not r['ranges_overlap'] and r['ratio'] < 1),
                      'slower_separated': sum(1 for r in ratios if not r['ranges_overlap'] and r['ratio'] > 1)}
    return out


def reload_table(workload, reloads, variants, exclude_first_use):
    """Per step and variant: median across processes of each process's median per phase."""
    rows = [r for r in reloads if r['workload'] == workload and r['step'] is not None and not r['step'].startswith('restore:')
            and not (exclude_first_use and r['round'] == 0)]
    tables = []
    for step in dict.fromkeys(r['step'] for r in rows):
        cells = {}
        for v in variants:
            per_process = {}
            for r in rows:
                if r['step'] != step or r['variant'] != v:
                    continue
                bucket = per_process.setdefault(r['index'], {})
                for block in r['reloads']:
                    bucket.setdefault('total', []).append(block['total_ms'])
                    for phase in RELOAD_PHASES[1:]:
                        if phase in block['phases']:
                            bucket.setdefault(phase, []).append(block['phases'][phase])
                for block in r['refreshes']:
                    bucket.setdefault('compile (refresh)', []).append(block['compile_ms'])
                    bucket.setdefault('domain reload (refresh)', []).append(block['domain_reload_ms'])
                bucket.setdefault('reload blocks per round', []).append(len(r['reloads']))
            names = {n for b in per_process.values() for n in b}
            cells[v] = {n: describe([statistics.median(b[n]) for b in per_process.values() if n in b]) for n in names}
        order = [p for p in RELOAD_PHASES + ['compile (refresh)', 'domain reload (refresh)', 'reload blocks per round'] if any(p in c for c in cells.values())]
        if 'total' in order:   # steps without any reload (Play Mode exit, Play with domain reload off) have nothing to break down
            tables.append({'step': step, 'phases': order, 'cells': cells})
    return tables


def host_summary(processes):
    """Per variant: median of per-process machine/other CPU medians, and the worst single-sample maxima."""
    out = {}
    for v in dict.fromkeys(p['variant'] for p in processes):
        hosts = [p['host'] for p in processes if p['variant'] == v and p.get('host', {}).get('available') and p['host'].get('samples')]
        if hosts:
            out[v] = {'processes': len(hosts),
                      'machine_cpu_median': statistics.median(h['machine_cpu_median'] for h in hosts),
                      'machine_cpu_max': max(h['machine_cpu_max'] for h in hosts),
                      'other_cpu_median': statistics.median(h['other_cpu_median'] for h in hosts),
                      'other_cpu_max': max(h['other_cpu_max'] for h in hosts),
                      'memory_percent_max': max(h['memory_percent_max'] for h in hosts)}
    return out
