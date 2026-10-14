"""Summary: per-process medians, median across processes, range, ratio, identity flags, headline aggregates.

Everything is computed from the raw rows in results.jsonl, so a report can be regenerated
at any time and with different options. The raw data is never filtered on disk.
"""
from collections import Counter
from fnmatch import fnmatchcase
import json
import re
import statistics

from .config import SCENE_KINDS
from .identity import identity, pair_identity
from .stats import RESOLUTION_MS, bootstrap_ratio, compare, describe, geomean, linear_fit, process_medians

RELOAD_PHASES = ['total', 'BeginReloadAssembly', 'LoadAllAssembliesAndSetupDomain', 'TypeCache.ScanAssembly',
                 'FinalizeReload', 'ProcessInitializeOnLoadAttributes']
PLAYER_KINDS = ('player.startup', 'player.adapter', 'player.scene')
QUANTILE = re.compile(r' (\S+ (?:p50|p95|p99|max))$')   # "frame steady p95": the statistic of a distribution, as the runner names its items
MEMORY_PREFIX, LEAK_PREFIX = 'mem/', 'leak/'   # item names of the memory readings (once per process: mem/<start|end|peak>/<name>) and of a series (leak/<name>, one per reload, round = its index)
ENVIRONMENT_KEYS = ('unityVersion', 'unityVersionFull', 'runtime', 'runtimeKind', 'codeOptimization', 'backend', 'development', 'platform', 'graphicsDevice', 'batchMode')


def read_rows(path):
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def is_headline(name, declared):
    """`declared` is True (every item) or a list of fnmatch patterns on the item name."""
    return declared is True or any(fnmatchcase(name, p) for p in declared or ())


def summarise(rows, exclude_first_use=True, include_differing=False, baseline=None, variant_order=None, baselines=None,
              headline=None, bootstrap=0, resolution_ms=RESOLUTION_MS):
    """Build the summary dict that report.py renders and summary.json stores.

    `headline` maps a workload id to True or a list of item-name patterns (the campaign's choice); patterns the adapters declared
    in their results are added. Only headline items enter an aggregate.
    """
    processes = [r for r in rows if r['type'] == 'process']
    samples = [r for r in rows if r['type'] == 'sample']
    reloads = [r for r in rows if r['type'] == 'reload']
    variants = variant_order or list(dict.fromkeys(r['variant'] for r in processes))
    baseline = baseline if baseline in variants else (variants[0] if variants else None)
    # Each variant's ratio is taken against its own baseline (a campaign with Debug and Release pairs compares within each pair).
    base_of = {v: (baselines or {}).get(v) if (baselines or {}).get(v) in variants else baseline for v in variants}
    options = {'exclude_first_use': exclude_first_use, 'include_differing': include_differing, 'bootstrap': bootstrap, 'resolution_ms': resolution_ms}
    out = {'baseline': baseline, 'baselines': base_of, 'variants': variants, 'exclude_first_use': exclude_first_use, 'options': options, 'workloads': [],
           'failures': [{k: p.get(k) for k in ('index', 'workload', 'variant', 'repeat', 'status', 'error')} for p in processes if p['status'] != 'ok']}
    for workload in dict.fromkeys(p['workload'] for p in processes):
        mine = [s for s in samples if s['workload'] == workload]
        mine_processes = [p for p in processes if p['workload'] == workload]
        entry = {'workload': workload, 'kind': mine_processes[0]['kind'], 'variants': [v for v in variants if any(p['variant'] == v for p in mine_processes)],
                 'items': [], 'differing': [], 'first_use': [], 'first_use_ratios': [], 'reload': [], 'memory': [], 'leak': []}
        chosen = (headline or {}).get(workload)
        declared = True if chosen is True else list(chosen or []) + [x for p in mine_processes for x in p.get('headline') or []]
        compared = [v for v in entry['variants'] if base_of[v] != v]
        by_name = {}
        for s in mine:
            if not s['name'].startswith(LEAK_PREFIX):   # a leak series is fitted per process below, not summarised as one item
                by_name.setdefault(s['name'], []).append(s)
        for name, item_samples in by_name.items():
            steady = [s for s in item_samples if not (exclude_first_use and s['phase'] == 'first-use')]
            first = [s for s in item_samples if s['phase'] == 'first-use']
            per_variant = {}
            for v in entry['variants']:
                stats = describe(process_medians([s for s in steady if s['variant'] == v]))
                if stats:
                    per_variant[v] = stats
            ident = identity(item_samples, entry['variants'])
            # Work is judged per compared pair: Unity 7 IL2CPP and CoreCLR can do the same work as each other but not as 6.7.
            judged = {v: pair_identity(item_samples, base_of[v], v) for v in compared}
            pair_work = {v: j[0] for v, j in judged.items()}
            unit = next((s['unit'] for s in item_samples if s.get('unit')), 'ms')   # bytes and counts have no timer resolution
            diagnostic = {v: compare(per_variant.get(base_of[v]), per_variant.get(v), resolution_ms if unit == 'ms' else 0.0) for v in compared}
            same_somewhere = ident['status'] != 'differs' or any(w != 'differs' for w in pair_work.values())
            ratios = diagnostic if include_differing or ident['status'] != 'differs' else {v: (r if pair_work[v] != 'differs' else None) for v, r in diagnostic.items()}
            quantile = QUANTILE.search(name)
            row = {'name': name, 'unit': unit, 'variants': per_variant, 'identity': ident, 'pair_work': pair_work, 'pair_coverage': {v: j[1] for v, j in judged.items()},
                   'phases': sorted({s['phase'] for s in steady}), 'info': info_summary(steady, entry['variants']), 'ratios': ratios, 'diagnostic_ratios': diagnostic,
                   'family': quantile.group(1) if quantile else None, 'headline': is_headline(name, declared),
                   'work_groups': sorted({s['work_group'] for s in item_samples if s.get('work_group')})}
            if bootstrap:
                row['bootstrap'] = {v: bootstrap_ratio(per_variant[base_of[v]]['values'], per_variant[v]['values'], bootstrap)
                                    for v, r in ratios.items() if r and r['ratio'] is not None}
            memory = name.startswith(MEMORY_PREFIX) and not row['headline']   # a memory reading is an annotation unless the campaign marks it as headline
            if memory:
                row['ratios'] = diagnostic
            target = entry['memory'] if memory else entry['differing'] if not same_somewhere and not include_differing else entry['items']
            if per_variant or ident['status'] == 'differs':
                target.append(row)
            for v in entry['variants']:
                fs = [s['ms'] for s in first if s['variant'] == v]
                if fs:
                    entry['first_use'].append({'name': name, 'variant': v, 'ms': fs})
            first_stats = {v: describe(process_medians([s for s in first if s['variant'] == v])) for v in entry['variants']}
            if any(first_stats.values()):
                entry['first_use_ratios'].append({'name': name, 'variants': {v: x for v, x in first_stats.items() if x},
                                                  'ratios': {v: compare(first_stats[base_of[v]], first_stats[v], resolution_ms) for v in compared}})
        entry['aggregate'] = aggregate(entry['items'], compared, base_of)
        entry['headline_items'] = sum(1 for i in entry['items'] if i['headline'])
        entry['observed'] = observed(mine, mine_processes, entry['variants'])
        if entry['kind'] in SCENE_KINDS:
            entry['work_verified'] = any(i['identity']['status'] != 'unreported' for i in entry['items'] + entry['differing'])
            entry['markers'] = markers(mine_processes, entry['variants'])
        entry['reload'] = reload_table(workload, reloads, entry['variants'], exclude_first_use)
        entry['leak'] = leak_table(mine, entry['variants'])
        entry['memory_counters'] = memory_counters(mine_processes, entry['variants'])
        entry['snapshots'] = [{'variant': p['variant'], 'index': p['index'], 'result': (p.get('notes') or {})['memory.snapshot']} for p in mine_processes
                              if p['status'] == 'ok' and (p.get('notes') or {}).get('memory.snapshot')]   # a .snap path in the run directory, or why none was taken
        out['workloads'].append(entry)
    out['host'] = host_summary(processes)
    out['environments'] = environments(processes)
    return out


def observed(samples, processes, variants):
    """What was actually measured, not what the campaign asked for: finished processes per variant, samples per item and process, phases."""
    samples = [s for s in samples if not s['name'].startswith((MEMORY_PREFIX, LEAK_PREFIX))]   # readings, not rounds of the workload
    plain = Counter((s['index'], s['name']) for s in samples if 'frames' not in s['info'])   # distributions are many executions, not rounds
    return {'processes': {v: sum(1 for p in processes if p['variant'] == v and p['status'] == 'ok' and not p.get('role')) for v in variants},
            'samples_per_item': [min(plain.values()), max(plain.values())] if plain else None,
            'phases': dict(Counter(s['phase'] for s in samples))}


def memory_counters(processes, variants):
    """Per variant, which memory counters the Editor or player offered and which of the ones the workload asks for it did not (the process notes `memory.offered`, `memory.missing`)."""
    out = {}
    for v in variants:
        mine = [p.get('notes') or {} for p in processes if p['variant'] == v and p['status'] == 'ok' and not p.get('role')]   # not the Editor that built the player
        found = {key: sorted({x for n in mine for x in str(n.get(key, '')).split(', ') if x}) for key in ('memory.offered', 'memory.missing')}
        if found['memory.offered'] or found['memory.missing']:
            out[v] = {'offered': found['memory.offered'], 'missing': found['memory.missing'], 'processes': len(mine)}
    return out


def leak_table(samples, variants):
    """Per counter and variant: first and last reading of a series, and its least-squares slope per reload (or Play Mode cycle) with the residual range.

    Each process is fitted on its own (the process is the unit of repeat); the table takes the median of the processes' values and the range of their slopes.
    `tail_slope` is the slope of the second half of the series, which leaves out start-up growth that has not settled yet.
    """
    series = {}
    for s in samples:
        if s['name'].startswith(LEAK_PREFIX):
            series.setdefault((s['name'][len(LEAK_PREFIX):], s['variant'], s['index']), []).append((s['round'], s['ms'], s.get('unit') or 'bytes'))
    rows = []
    for name in dict.fromkeys(k[0] for k in series):
        cells, unit = {}, 'bytes'
        for v in variants:
            fits = []
            for (n, variant, _), points in series.items():
                if n != name or variant != v:
                    continue
                points.sort()
                xs, ys, unit = [p[0] for p in points], [p[1] for p in points], points[0][2]
                fit, tail = linear_fit(xs, ys), linear_fit(xs[len(xs) // 2:], ys[len(xs) // 2:])
                if fit:
                    fits.append({'first': ys[0], 'last': ys[-1], 'slope': fit['slope'], 'tail_slope': tail['slope'] if tail else None,
                                 'residual': [fit['residual_min'], fit['residual_max']], 'points': len(points)})
            if fits:
                tails = [f['tail_slope'] for f in fits if f['tail_slope'] is not None]
                cells[v] = {'processes': len(fits), 'points': fits[0]['points'], 'first': statistics.median(f['first'] for f in fits), 'last': statistics.median(f['last'] for f in fits),
                            'slope': statistics.median(f['slope'] for f in fits), 'slope_range': [min(f['slope'] for f in fits), max(f['slope'] for f in fits)],
                            'tail_slope': statistics.median(tails) if tails else None,
                            'residual': [min(f['residual'][0] for f in fits), max(f['residual'][1] for f in fits)]}
        rows.append({'name': name, 'unit': unit, 'cells': cells})
    return rows


def markers(processes, variants):
    """Per variant, which profiler markers the scene workload recorded and which this player or Editor did not have (the process notes `markers.recorded` and `markers.missing`)."""
    out = {}
    for v in variants:
        mine = [p.get('notes') or {} for p in processes if p['variant'] == v and p['status'] == 'ok']
        found = {key: sorted({x for n in mine for x in n.get(key, '').split(', ') if x}) for key in ('markers.recorded', 'markers.missing')}
        if mine:
            out[v] = {'recorded': found['markers.recorded'], 'missing': found['markers.missing'], 'processes': len(mine)}
    return out


def environments(processes):
    """Per variant and role ('editor' or 'player'), the distinct environments seen, most common first.

    A player process reports the player's runtime, backend, build kind and graphics API; the Editor that built it (the setup build and
    every other Editor workload) reports the Editor's. They are different things and must not stand in for each other.
    """
    found = {}
    for p in processes:
        if not p.get('environment'):
            continue
        role = 'player' if p['kind'] in PLAYER_KINDS and p.get('role') != 'setup-build' else 'editor'
        env = {k: p['environment'][k] for k in ENVIRONMENT_KEYS if p['environment'].get(k) is not None}
        key = json.dumps([env, p.get('build')], sort_keys=True)
        slot = found.setdefault(p['variant'], {}).setdefault(role, {})
        slot.setdefault(key, {'environment': env, 'build': p.get('build'), 'processes': 0})['processes'] += 1
    return {v: {role: sorted(slot.values(), key=lambda e: -e['processes']) for role, slot in roles.items()} for v, roles in found.items()}


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


def aggregate(items, others, base_of):
    """Geometric mean of the ratios of the headline items only, one line per family of statistic.

    Without headline items there is no aggregate: an unweighted mean over everything lets duplicated series, sums and their parts,
    and sub-resolution timings vote. Within the headline items a family (`steady p95`, `warmup p50`, or plain items) is never mixed
    with another, an identical series counts once, and items below the timer resolution are left out with a count.
    """
    out = {}
    for v in others:
        families = {}
        for item in items:
            if item['headline'] and item['ratios'].get(v):
                families.setdefault(item['family'], []).append(item)
        lines = []
        for family, members in families.items():
            seen, kept, below, duplicates = set(), [], 0, 0
            for item in members:
                if item['ratios'][v]['ratio'] is None:
                    below += 1
                    continue
                series = (tuple(item['variants'][base_of[v]]['values']), tuple(item['variants'][v]['values']))
                if series in seen:
                    duplicates += 1
                    continue
                seen.add(series)
                kept.append(item)
            if len(kept) > 1:
                ratios = [i['ratios'][v] for i in kept]
                lines.append({'family': family, 'items': len(kept), 'geomean': geomean(r['ratio'] for r in ratios), 'below_resolution': below, 'duplicates': duplicates,
                              'faster_separated': sum(1 for r in ratios if not r['ranges_overlap'] and r['ratio'] < 1),
                              'slower_separated': sum(1 for r in ratios if not r['ranges_overlap'] and r['ratio'] > 1),
                              'work': dict(Counter(i['pair_work'][v] for i in kept))})
        if lines:
            out[v] = lines
    return out


def reload_table(workload, reloads, variants, exclude_first_use):
    """Per step and variant: median across processes of each process's median per phase.

    One round can contain several reload blocks (a compile that reloads twice, a Play Mode transition plus a refresh). They are
    summed per round first, so the medians describe what the round cost; the number of blocks per round is a row of its own.
    """
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
                round_sum = {}
                for block in r['reloads']:
                    round_sum['total'] = round_sum.get('total', 0) + block['total_ms']
                    for phase in RELOAD_PHASES[1:]:
                        if phase in block['phases']:
                            round_sum[phase] = round_sum.get(phase, 0) + block['phases'][phase]
                for block in r['refreshes']:
                    round_sum['compile (refresh)'] = round_sum.get('compile (refresh)', 0) + block['compile_ms']
                    round_sum['domain reload (refresh)'] = round_sum.get('domain reload (refresh)', 0) + block['domain_reload_ms']
                round_sum['reload blocks per round'] = len(r['reloads'])
                for name, value in round_sum.items():
                    bucket.setdefault(name, []).append(value)
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
                      'memory_percent_max': max(h['memory_percent_max'] for h in hosts),
                      'tree_rss_max_mb': max((h.get('tree_rss_max_mb') or 0 for h in hosts), default=0)}
    return out
