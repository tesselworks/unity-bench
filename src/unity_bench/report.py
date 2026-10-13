"""Render summary.md and summary.json from a run directory's raw results.jsonl."""
from collections import Counter
import json
from pathlib import Path

from . import provenance
from .stats import RESOLUTION_MS, chance_separation
from .summary import read_rows, summarise

ROUNDS_KINDS = ('editor.script_edit', 'editor.play', 'editor.adapter', 'player.build_time', 'player.adapter')   # kinds where `rounds` means something


def ms(value):
    if value < 1:
        return f'{value:.3f}'
    if value < 10:
        return f'{value:.2f}'
    if value < 100:
        return f'{value:.1f}'
    return f'{value:,.0f}'


def cell(stats, count=False, resolution=0.0):
    if not stats:
        return 'no data'
    if count:
        return f"{stats['median']:g} [{stats['min']:g}-{stats['max']:g}] n={stats['n']}"
    median = '<res' if stats['median'] < resolution else ms(stats['median'])
    return f"{median} [{ms(stats['min'])}-{ms(stats['max'])}] n={stats['n']}"


def ratio_cell(r):
    """The ratio, whether the process ranges separate, and n on each side (a separation among two or three processes happens by chance)."""
    if not r:
        return '-'
    if r['ratio'] is None:
        return '<res'
    return f"{r['ratio']:.2f} (ranges {'overlap' if r['ranges_overlap'] else 'separate'}, n={r['n'][0]} vs {r['n'][1]})"


def table(header, rows):
    lines = ['| ' + ' | '.join(header) + ' |', '| ' + ' | '.join('---' for _ in header) + ' |']
    lines += ['| ' + ' | '.join(str(c) for c in row) + ' |' for row in rows]
    return lines


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


def coverage_text(coverage):
    return ', '.join(f'{v} {r}/{t}' for v, (r, t) in coverage.items())


def work_cell(item, compared, bases):
    status = item['identity']['status']
    if status == 'identical':
        return 'same'
    if status == 'unreported':
        return 'not reported'
    if status == 'partial':
        return f"partial ({coverage_text(item['identity']['coverage'])})"
    parts = []   # differs somewhere, but not in every compared pair
    for v in compared:
        work = item['pair_work'].get(v)
        if work in ('same', 'partial'):
            parts.append(f'{v}/{bases[v]} {work}' + (f" ({coverage_text(item['pair_coverage'][v])})" if work == 'partial' else ''))
    return 'within ' + '; '.join(parts) if parts else 'differs'


def env_text(env):
    return ' / '.join(str(env[k]) for k in ('runtimeKind', 'runtime', 'codeOptimization', 'platform', 'graphicsDevice') if env.get(k) is not None)


def build_text(build):
    names = (('scriptingBackend', 'backend'), ('apiCompatibility', 'API level'), ('il2cppCodeGeneration', 'IL2CPP code generation'), ('il2cppCompiler', 'IL2CPP compiler'),
             ('stripping', 'stripping'))
    build = build or {}
    # `<key>Requested` is present only when the Editor stored a different value than the campaign asked for.
    return ', '.join(f'{label} {build[key]}' + (f" (requested {build[key + 'Requested']})" if build.get(key + 'Requested') else '')
                     for key, label in names if build.get(key) is not None) or 'not recorded'


def commit_text(info):
    if info is None:
        return 'not recorded'
    if not info.get('commit'):
        return 'not in git'
    text = info['commit'][:12]
    if info.get('revision') and not info['commit'].startswith(info['revision']):
        text += f" ({info['revision']})"
    return text + (f" +{info['dirty_files']} uncommitted" if info.get('dirty_files') else '')


def options_text(options):
    """The `report` flags that reproduce these options."""
    flags = [] if options.get('exclude_first_use', True) else ['--include-first-use']
    flags += ['--include-differing'] * bool(options.get('include_differing')) + ['--first-use-ratios'] * bool(options.get('first_use_ratios'))
    flags += [f"--bootstrap {options['bootstrap']}"] * bool(options.get('bootstrap'))
    if options.get('resolution_ms', RESOLUTION_MS) != RESOLUTION_MS:
        flags.append(f"--resolution-ms {options['resolution_ms']:g}")
    return ' '.join(flags)


def render_header(summary, campaign):
    variants, base = summary['variants'], summary['baseline']
    bases = summary.get('baselines') or {v: base for v in variants}
    others = [v for v in variants if bases[v] != v]
    pairs = len({bases[v] for v in others}) > 1
    run = campaign.get('provenance', {}).get('framework') or ({'version': campaign['version']} if campaign.get('version') else {})
    report = summary.get('report', {}).get('framework') or {}
    lines = [f"# Unity Bench: {campaign.get('campaign', {}).get('name', 'run')}", '',
             (f"Baseline (A): `{base}`. " if not pairs else 'Each variant is compared with its own baseline: ' + ', '.join(f'`{v}` with `{bases[v]}`' for v in others) + '. ')
             + 'Ratios are other / baseline, so below 1.00 means faster than the baseline. '
             'Each cell is the median across processes of each process\'s median, then [min-max] across processes, n = processes. '
             + ('First-use samples are excluded from these tables and listed separately.' if summary['exclude_first_use'] else 'First-use samples are included.'), '']
    if run or report:
        ran = f"unity-bench {run.get('version', '?')} (`{run.get('commit') or 'commit not recorded'}`)" if run else 'an unrecorded unity-bench'
        flags = options_text(summary['options'])
        lines += [f"Measured with {ran}. Rendered by unity-bench {report.get('version', '?')} (`{report.get('commit') or 'no git checkout'}`) with "
                  f"{'report options `' + flags + '`' if flags else 'the default report options'}. To reproduce this report, check out that commit and run "
                  f"`unity-bench report <run dir>{' ' + flags if flags else ''}` on the raw `results.jsonl`.", '']
    for note in campaign.get('schedule_notes') or []:
        lines.append(f'- Schedule: {note}.')
    for vid, info in (campaign.get('provenance', {}).get('variants') or {}).items():
        for warning in info.get('warnings') or []:
            lines.append(f'- `{vid}`: {warning}.')
    if campaign.get('schedule_notes') or any(i.get('warnings') for i in (campaign.get('provenance', {}).get('variants') or {}).values()):
        lines.append('')
    return lines, variants, bases


def render_variants(summary, campaign, variants):
    configured = {v['id']: v for v in campaign.get('campaign', {}).get('variants', [])}
    sources = campaign.get('provenance', {}).get('variants') or {}
    envs = summary.get('environments') or {}
    editor = {v: (envs.get(v, {}).get('editor') or [{'environment': {}}]) for v in variants}
    lines = ['## Variants', '']
    lines += table(['Variant', 'Unity', 'Project commit', 'Editor runtime', 'Editor code optimization', 'Editor'],
                   [[v, editor[v][0]['environment'].get('unityVersionFull') or editor[v][0]['environment'].get('unityVersion') or configured.get(v, {}).get('unity_version') or '?',
                     commit_text(sources.get(v)), editor[v][0]['environment'].get('runtime', '?'), editor[v][0]['environment'].get('codeOptimization', '?'),
                     f"`{configured.get(v, {}).get('editor', '?')}`"] for v in variants])
    for v in variants:
        if len(editor[v]) > 1:
            lines += ['', f'`{v}`: {len(editor[v])} Editor environments were seen; the row above shows the most common.']
            lines += [f"- {e['processes']} processes: {env_text(e['environment'])}" for e in editor[v]]
    players = {v: envs[v]['player'] for v in variants if envs.get(v, {}).get('player')}
    if players:
        lines += ['', 'Players (what the built player reported, not the Editor that built it):', '']
        lines += table(['Variant', 'Player backend', 'Runtime', 'Build', 'Platform / graphics API', 'Build settings'],
                       [[v, e[0]['environment'].get('backend', '?'), e[0]['environment'].get('runtime', '?'), e[0]['environment'].get('codeOptimization', '?'),
                         f"{e[0]['environment'].get('platform', '?')} / {e[0]['environment'].get('graphicsDevice', '?')}", build_text(e[0]['build'])] for v, e in players.items()])
        for v, e in players.items():
            if len(e) > 1:
                lines += ['', f'`{v}`: {len(e)} player environments were seen; the row above shows the most common.']
                lines += [f"- {x['processes']} processes: {env_text(x['environment'])}; {build_text(x['build'])}" for x in e]
    return lines


def render_workload(w, summary, campaign, bases):
    options = summary['options']
    resolution = options.get('resolution_ms', 0.0)
    configured = [x for x in campaign.get('campaign', {}).get('workloads', []) if x['id'] == w['workload']]
    seen = w['observed']
    counts = ', '.join(f'{v} {n}' for v, n in seen['processes'].items())
    per_process = ''
    if seen['samples_per_item']:
        low, high = seen['samples_per_item']
        per_process = f"; {low if low == high else f'{low}-{high}'} sample(s) per item per process"
        if configured and w['kind'] in ROUNDS_KINDS and (low, high) != (configured[0].get('rounds'),) * 2:
            per_process += f" (the campaign's rounds = {configured[0]['rounds']} is not what ran)"
    lines = ['', f"## {w['workload']} (`{w['kind']}`) - processes per variant: {counts}{per_process}", '']
    if seen['phases']:
        lines += ['Samples by phase: ' + ', '.join(f'{p} {n}' for p, n in sorted(seen['phases'].items())) +
                  ('. `single`: measured once per process, so neither first-use nor claimed to be warm.' if 'single' in seen['phases'] else '.'), '']
    compared = [v for v in w['variants'] if bases[v] != v]
    if w['items']:
        boot = bool(options.get('bootstrap'))
        header = ['Item', *[v + (' (A)' if v == summary['baseline'] else '') for v in w['variants']], *[f'{v}/{bases[v]}' for v in compared],
                  *[f'{v}/{bases[v]} 95% CI (bootstrap)' for v in compared if boot], 'Work']
        body = []
        for item in w['items']:
            pair_cells = []
            for v in compared:
                differs = item.get('pair_work', {}).get(v) == 'differs'
                pair_cells.append('work differs' if differs and not options.get('include_differing')
                                  else ratio_cell(item['ratios'].get(v)) + (' [work differs]' if differs else ''))
            intervals = [(lambda ci: f'{ci[0]:.2f}-{ci[1]:.2f}' if ci else '-')((item.get('bootstrap') or {}).get(v)) for v in compared] if boot else []
            body.append([item['name'], *[cell(item['variants'].get(v), resolution=resolution) for v in w['variants']], *pair_cells, *intervals, work_cell(item, compared, bases)])
        lines += table(header, body)
        sizes = sorted({tuple(r['n']) for item in w['items'] for r in item['ratios'].values() if r})
        if sizes:
            lines += ['', 'Ranges "separate" when every process of one side is faster than every process of the other. With no real difference that happens by chance '
                      + '; '.join(f'{100 * chance_separation(a, b):.1f}% of the time for n={a} vs {b}' for a, b in sizes)
                      + ' (2 / C(a+b, a)). Descriptive, not a significance test.']
        if boot:
            lines += ['', f"95% CI: percentile bootstrap over processes ({options['bootstrap']} resamples) of the ratio of medians. Descriptive; with small n it is coarse."]
        if options.get('include_differing') and any(r == 'differs' for i in w['items'] for r in i['pair_work'].values()):
            lines += ['', 'Ratios marked [work differs] compare variants that did different work or produced different output: diagnostic only, not a speed comparison. '
                      '`--include-differing` put them in this table and in the aggregate below.']
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
    lines += render_aggregate(w, bases, compared)
    if w['differing']:
        lines += ['', f"**{len(w['differing'])} item(s) did different work or produced different output in every compared pair, or varied within a variant. They are excluded from the table and from any aggregate.** "
                  'Their timings are shown here for diagnosis only; a ratio between different work is not a speed comparison.', '']
        lines += table(['Item', 'Why', 'Values', *[f'{v}/{bases[v]}' for v in compared]],
                       [[i['name'], i['identity']['detail'], describe_fingerprints(i['identity']['values']), *[ratio_cell(i['diagnostic_ratios'].get(v)) for v in compared]] for i in w['differing']])
    if options.get('first_use_ratios') and w['first_use_ratios']:
        lines += ['', 'First-use samples as ratios (one per process; the first time something runs in a process: JIT, caches, first scene, first import):', '']
        lines += table(['Item', *w['variants'], *[f'{v}/{bases[v]}' for v in compared]],
                       [[f['name'], *[cell(f['variants'].get(v), resolution=resolution) for v in w['variants']], *[ratio_cell(f['ratios'].get(v)) for v in compared]]
                        for f in w['first_use_ratios']])
    elif w['first_use'] and summary['exclude_first_use']:
        lines += ['', 'First-use samples (kept in results.jsonl; one per process; ms):', '']
        lines += table(['Item', 'Variant', 'Samples'], [[f['name'], f['variant'], ', '.join(ms(x) for x in f['ms'])] for f in w['first_use']])
    for step in w['reload']:
        lines += ['', f"Reload breakdown for `{step['step']}` (ms, blocks of one round summed, then median across processes; log blocks attributed by step markers):", '']
        body = []
        for phase in step['phases']:
            cells = [c.get(phase) for c in step['cells'].values()]
            if phase == 'compile (refresh)' and all(not c or c['median'] < 10 for c in cells):
                continue   # a refresh that only reloads reports ~1 ms of compile; the compile happens before it
            row = [phase, *[cell(step['cells'].get(v, {}).get(phase), count=phase.endswith('per round')) for v in w['variants']]]
            for v in compared:
                a, b = step['cells'].get(bases[v], {}).get(phase), step['cells'].get(v, {}).get(phase)
                row.append(f"{b['median'] / a['median']:.2f}" if a and b and a['median'] else '-')
            body.append(row)
        lines += table(['Phase', *w['variants'], *[f'{v}/{bases[v]}' for v in compared]], body)
    return lines


def render_aggregate(w, bases, compared):
    """Aggregates exist for headline items only; say so when a workload has none."""
    lines = []
    for v, aggregates in w['aggregate'].items():
        for a in aggregates:
            work = ', '.join(f'{n} {status}' for status, n in sorted(a['work'].items()))
            left_out = [f"{a[key]} {text}" for key, text in (('below_resolution', 'below the timer resolution'), ('duplicates', 'identical series')) if a[key]]
            lines += ['', f"Headline{' (' + a['family'] + ')' if a['family'] else ''}, `{v}` vs `{bases[v]}`: geometric mean of {a['items']} ratios {a['geomean']:.2f}; "
                          f"{a['faster_separated']} faster and {a['slower_separated']} slower with separated ranges. Work: {work}."
                          + (f" Left out: {', '.join(left_out)}." if left_out else '')]
    if compared and len(w['items']) > 1 and not w['headline_items']:
        lines += ['', 'No aggregate: no headline items are declared for this workload (`headline` in the campaign, or `Headline(...)` in the adapter).']
    return lines


def render_host(summary, campaign, rows):
    lines = ['', '## Host (annotation only)', '']
    if summary['host']:
        lines += ['Machine CPU is whole-machine utilisation while the process ran. "Other" CPU is approximate: machine CPU minus the CPU time of the launched process tree, '
                  'sampled once a second, so a process that starts and ends between two samples is missed and "other" can be over-stated. The one-second series of every process '
                  'is in `results.jsonl`. Nothing was refused or discarded because of any of it.', '']
        lines += table(['Variant', 'Processes', 'Machine CPU median / max %', 'Other CPU (approx.) median / max %', 'Memory max %'],
                       [[v, h['processes'], f"{h['machine_cpu_median']:.0f} / {h['machine_cpu_max']:.0f}", f"{h['other_cpu_median']:.0f} / {h['other_cpu_max']:.0f}", f"{h['memory_percent_max']:.0f}"] for v, h in summary['host'].items()])
    else:
        lines.append('Not recorded (install psutil to record CPU and memory).')
    host = campaign.get('host') or {}
    if host:
        lines += ['', f"{host.get('platform')} {host.get('machine')}, {host.get('cpus')} CPUs, Python {host.get('python')}."]
    terminated, kept = Counter(), Counter()
    for r in rows:
        if r['type'] == 'process' and r.get('cleanup'):
            terminated.update(r['cleanup'].get('terminated') or [])
            kept.update(r['cleanup'].get('kept') or [])
    if terminated or kept:
        lines += ['', 'Process cleanup after each run: ' + (f"terminated {', '.join(f'{n} x{c}' for n, c in sorted(terminated.items()))}" if terminated else 'nothing left to terminate')
                  + (f"; left running (shared Unity daemons): {', '.join(f'{n} x{c}' for n, c in sorted(kept.items()))}" if kept else '') + '.']
    return lines


def render(summary, campaign, rows):
    lines, variants, bases = render_header(summary, campaign)
    lines += render_variants(summary, campaign, variants)
    for w in summary['workloads']:
        lines += render_workload(w, summary, campaign, bases)
    lines += render_host(summary, campaign, rows)
    if summary['failures']:
        lines += ['', '## Failed processes (not in the numbers)', '']
        lines += [f"- #{f['index']} {f['workload']} {f['variant']} repeat {f['repeat']}: {f['status']}: {f['error']}" for f in summary['failures']]
    return '\n'.join(lines) + '\n'


def write_reports(run_dir, exclude_first_use=None, include_differing=False, bootstrap=0, first_use_ratios=False, resolution_ms=RESOLUTION_MS):
    """(Re)generate summary.json and summary.md in `run_dir` from results.jsonl."""
    run_dir = Path(run_dir)
    campaign = json.loads((run_dir / 'campaign.json').read_text(encoding='utf-8')) if (run_dir / 'campaign.json').is_file() else {}
    cfg = campaign.get('campaign', {})
    defaults = cfg.get('defaults', {})
    exclude = defaults.get('exclude_first_use', True) if exclude_first_use is None else exclude_first_use
    rows = read_rows(run_dir / 'results.jsonl')
    order = [v['id'] for v in cfg.get('variants', [])]
    configured = {v['id']: v.get('baseline') for v in cfg.get('variants', [])}
    headline = {w['id']: w['headline'] for w in cfg.get('workloads', []) if w.get('headline')}
    summary = summarise(rows, exclude_first_use=exclude, include_differing=include_differing, baseline=defaults.get('baseline'), baselines=configured,
                        variant_order=[v for v in order if any(r['variant'] == v for r in rows)] or None, headline=headline, bootstrap=bootstrap, resolution_ms=resolution_ms)
    summary['options']['first_use_ratios'] = first_use_ratios
    summary['report'] = {'framework': provenance.framework(), 'options': summary['options']}
    (run_dir / 'summary.json').write_text(json.dumps(summary, indent=1, allow_nan=False), encoding='utf-8')
    (run_dir / 'summary.md').write_text(render(summary, campaign, rows), encoding='utf-8')
    return summary
