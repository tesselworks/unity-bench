"""Render summary.md and summary.json from a run directory's raw results.jsonl."""
import json
from pathlib import Path

from .summary import read_rows, summarise


def ms(value):
    if value < 1:
        return f'{value:.3f}'
    if value < 10:
        return f'{value:.2f}'
    if value < 100:
        return f'{value:.1f}'
    return f'{value:,.0f}'


def cell(stats, count=False):
    if not stats:
        return 'no data'
    if count:
        return f"{stats['median']:g} [{stats['min']:g}-{stats['max']:g}] n={stats['n']}"
    return f"{ms(stats['median'])} [{ms(stats['min'])}-{ms(stats['max'])}] n={stats['n']}"


def ratio_cell(r):
    if not r:
        return '-'
    return f"{r['ratio']:.2f} ({'overlap' if r['ranges_overlap'] else 'no overlap'})"


def table(header, rows):
    lines = ['| ' + ' | '.join(header) + ' |', '| ' + ' | '.join('---' for _ in header) + ' |']
    lines += ['| ' + ' | '.join(str(c) for c in row) + ' |' for row in rows]
    return lines


def environments(rows):
    """First reported environment per variant, from any process."""
    found = {}
    for r in rows:
        if r['type'] == 'process' and r.get('environment') and r['variant'] not in found:
            found[r['variant']] = r['environment']
    return found


def short(value):
    value = '' if value is None else str(value)
    return value[:12] + '...' if len(value) > 15 else value


def describe_fingerprints(values):
    out = []
    for v, prints in values.items():
        for p in prints:
            counters = ', '.join(f'{k}={x:g}' for k, x in (p.get('counters') or {}).items())
            out.append(f"{v}: hash {short(p.get('hash')) or '-'}" + (f"; {counters}" if counters else ''))
    return '<br>'.join(out)


def render(summary, campaign, rows):
    variants, base = summary['variants'], summary['baseline']
    others = [v for v in variants if v != base]
    envs = environments(rows)
    configured = {v['id']: v for v in campaign.get('campaign', {}).get('variants', [])}
    lines = [f"# Unity Bench: {campaign.get('campaign', {}).get('name', 'run')}", '',
             f"Baseline (A): `{base}`. Ratios are other / baseline, so below 1.00 means faster than the baseline. "
             'Each cell is the median across processes of each process\'s median, then [min-max] across processes, n = processes. '
             + ('First-use samples are excluded from these tables and listed separately.' if summary['exclude_first_use'] else 'First-use samples are included.'), '',
             '## Variants', '']
    lines += table(['Variant', 'Unity', 'Runtime', 'Player backend', 'Code optimization', 'Editor'],
                   [[v, (envs.get(v) or {}).get('unityVersion') or configured.get(v, {}).get('unity_version') or '?', (envs.get(v) or {}).get('runtime', '?'),
                     configured.get(v, {}).get('backend', '?'), (envs.get(v) or {}).get('codeOptimization', '?'), f"`{configured.get(v, {}).get('editor', '?')}`"] for v in variants])
    for w in summary['workloads']:
        wl = [x for x in campaign.get('campaign', {}).get('workloads', []) if x['id'] == w['workload']]
        extra = f" - {wl[0]['repeats']} processes x {wl[0]['rounds']} rounds" if wl and w['kind'] in ('editor.script_edit', 'editor.play', 'editor.adapter', 'player.build_time') else (f" - {wl[0]['repeats']} processes" if wl else '')
        lines += ['', f"## {w['workload']} (`{w['kind']}`){extra}", '']
        if w['items']:
            header = ['Item', *[v + (' (A)' if v == base else '') for v in w['variants']], *[f'{v}/{base}' for v in w['variants'] if v != base], 'Work']
            body = []
            for item in w['items']:
                work = {'identical': 'same', 'unreported': 'not reported'}.get(item['identity']['status'], '?')
                body.append([item['name'], *[cell(item['variants'].get(v)) for v in w['variants']],
                             *[ratio_cell(item['ratios'].get(v)) for v in w['variants'] if v != base], work])
            lines += table(header, body)
            notes = []
            for item in w['items']:
                if item.get('info'):
                    group = item['name'].rsplit('/', 1)[0] if '/' in item['name'] else item['name']
                    text = '; '.join(f"{v}: " + ', '.join(([f"assemblies compiled {'/'.join(map(str, i['assemblies']))}"] if 'assemblies' in i else [])
                                                         + ([f"reloaded {i['reloaded'][0]}/{i['reloaded'][1]}"] if 'reloaded' in i else []))
                                     for v, i in item['info'].items())
                    note = f'- `{group}`: {text}'
                    if note not in notes:
                        notes.append(note)
            if notes:
                lines += ['', 'Annotations from the Editor (not compared):', '', *notes]
        else:
            lines.append('No steady-state samples.')
        for v, agg in w['aggregate'].items():
            lines += ['', f"`{v}` vs `{base}`: geometric mean of {agg['items']} ratios {agg['geomean']:.2f}; {agg['faster_separated']} faster and {agg['slower_separated']} slower with non-overlapping ranges."]
        if w['differing']:
            lines += ['', f"**{len(w['differing'])} item(s) did different work or produced different output across variants or repeats. They are excluded from the table and the geometric mean above.** "
                      'Their timings are shown here for diagnosis only; a ratio between different work is not a speed comparison.', '']
            lines += table(['Item', 'Why', 'Values', *[f'{v}/{base}' for v in w['variants'] if v != base]],
                           [[i['name'], i['identity']['detail'], describe_fingerprints(i['identity']['values']), *[ratio_cell(i['ratios'].get(v)) for v in w['variants'] if v != base]] for i in w['differing']])
        if w['first_use'] and summary['exclude_first_use']:
            lines += ['', 'First-use samples (kept in results.jsonl; one per process; ms):', '']
            lines += table(['Item', 'Variant', 'Samples'], [[f['name'], f['variant'], ', '.join(ms(x) for x in f['ms'])] for f in w['first_use']])
        for step in w['reload']:
            lines += ['', f"Reload breakdown for `{step['step']}` (ms, median across processes; log blocks attributed by step markers):", '']
            body = []
            for phase in step['phases']:
                cells = [c.get(phase) for c in step['cells'].values()]
                if phase == 'compile (refresh)' and all(not c or c['median'] < 10 for c in cells):
                    continue   # a refresh that only reloads reports ~1 ms of compile; the compile happens before it
                row = [phase, *[cell(step['cells'].get(v, {}).get(phase), count=phase.endswith('per round')) for v in w['variants']]]
                for v in others:
                    a, b = step['cells'].get(base, {}).get(phase), step['cells'].get(v, {}).get(phase)
                    row.append(f"{b['median'] / a['median']:.2f}" if a and b and a['median'] else '-')
                body.append(row)
            lines += table(['Phase', *w['variants'], *[f'{v}/{base}' for v in others]], body)
    lines += ['', '## Host (annotation only)', '']
    if summary['host']:
        lines += ['Machine CPU is whole-machine utilisation while the process ran; "other" subtracts the launched process tree. Nothing was refused or discarded because of it.', '']
        lines += table(['Variant', 'Processes', 'Machine CPU median / max %', 'Other CPU median / max %', 'Memory max %'],
                       [[v, h['processes'], f"{h['machine_cpu_median']:.0f} / {h['machine_cpu_max']:.0f}", f"{h['other_cpu_median']:.0f} / {h['other_cpu_max']:.0f}", f"{h['memory_percent_max']:.0f}"] for v, h in summary['host'].items()])
    else:
        lines.append('Not recorded (install psutil to record CPU and memory).')
    host = campaign.get('host') or {}
    if host:
        lines += ['', f"{host.get('platform')} {host.get('machine')}, {host.get('cpus')} CPUs, Python {host.get('python')}."]
    if summary['failures']:
        lines += ['', '## Failed processes (not in the numbers)', '']
        lines += [f"- #{f['index']} {f['workload']} {f['variant']} repeat {f['repeat']}: {f['status']}: {f['error']}" for f in summary['failures']]
    return '\n'.join(lines) + '\n'


def write_reports(run_dir, exclude_first_use=None, include_differing=False):
    """(Re)generate summary.json and summary.md in `run_dir` from results.jsonl."""
    run_dir = Path(run_dir)
    campaign = json.loads((run_dir / 'campaign.json').read_text(encoding='utf-8')) if (run_dir / 'campaign.json').is_file() else {}
    defaults = campaign.get('campaign', {}).get('defaults', {})
    exclude = defaults.get('exclude_first_use', True) if exclude_first_use is None else exclude_first_use
    rows = read_rows(run_dir / 'results.jsonl')
    order = [v['id'] for v in campaign.get('campaign', {}).get('variants', [])]
    summary = summarise(rows, exclude_first_use=exclude, include_differing=include_differing, baseline=defaults.get('baseline'),
                        variant_order=[v for v in order if any(r['variant'] == v for r in rows)] or None)
    (run_dir / 'summary.json').write_text(json.dumps(summary, indent=1, allow_nan=False), encoding='utf-8')
    (run_dir / 'summary.md').write_text(render(summary, campaign, rows), encoding='utf-8')
    return summary
