"""Parse reload and compile breakdowns out of an Editor log.

Two block types are read:

    Domain Reload Profiling: 3065ms
        BeginReloadAssembly (250ms)
            DisableScriptedObjects (20ms)
        FinalizeReload (1713ms)
        ...

    Asset Pipeline Refresh (id=...): Total: 6.615 seconds - Initiated by ...
        Scripting: domain reloads=1, domain reload time=1437 ms, compile time=2790 ms, other=0 ms

The Editor workloads also print `[UnityBench] begin <step> <round>` / `[UnityBench] end <step> <round>`
markers, so each block can be attributed to the step that was running when it was written.
Blocks outside any marker pair (project open, setup) are reported with step None.
"""
import re

RELOAD = re.compile(r'^Domain Reload Profiling: (\d+)ms')
PHASE = re.compile(r'^\s+(\S.*?) \((\d+)ms\)\s*$')
REFRESH = re.compile(r'^Asset Pipeline Refresh \(id=[0-9a-f]+\): Total: ([\d.]+) seconds - Initiated by (.*)$')
SCRIPTING = re.compile(r'Scripting: domain reloads=(\d+), domain reload time=(\d+) ms, compile time=(\d+) ms, other=(\d+) ms')
MARKER = re.compile(r'^\[UnityBench\] (begin|end) (\S+) (\d+)\s*$')


def parse(lines):
    """Return the ordered events: reload blocks, refresh blocks that ran scripts, and step markers."""
    events = []
    lines = list(lines)
    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\r\n')
        marker = MARKER.match(line)
        reload = RELOAD.match(line)
        refresh = REFRESH.match(line)
        if marker:
            events.append({'event': marker.group(1), 'step': marker.group(2), 'round': int(marker.group(3))})
        elif reload:
            phases = {}
            j = i + 1
            while j < len(lines) and lines[j][:1] in ('\t', ' '):
                m = PHASE.match(lines[j].rstrip('\r\n'))
                if m:
                    phases[m.group(1)] = phases.get(m.group(1), 0) + int(m.group(2))
                j += 1
            events.append({'event': 'reload', 'total_ms': int(reload.group(1)), 'phases': phases})
            i = j - 1
        elif refresh:
            block = {'event': 'refresh', 'total_ms': round(float(refresh.group(1)) * 1000), 'initiated_by': refresh.group(2)}
            j = i + 1
            while j < len(lines) and lines[j][:1] in ('\t', ' '):
                m = SCRIPTING.search(lines[j])
                if m:
                    block.update(domain_reloads=int(m.group(1)), domain_reload_ms=int(m.group(2)), compile_ms=int(m.group(3)))
                j += 1
            if 'compile_ms' in block:
                events.append(block)
            i = j - 1
        i += 1
    return events


def attribute(events):
    """Group blocks by the marker pair they fall inside: [{'step', 'round', 'reloads': [...], 'refreshes': [...]}]."""
    groups, current = [], {'step': None, 'round': None, 'reloads': [], 'refreshes': []}
    for e in events:
        if e['event'] == 'begin':
            groups.append(current)
            current = {'step': e['step'], 'round': e['round'], 'reloads': [], 'refreshes': []}
        elif e['event'] == 'end':
            groups.append(current)
            current = {'step': None, 'round': None, 'reloads': [], 'refreshes': []}
        elif e['event'] == 'reload':
            current['reloads'].append({k: e[k] for k in ('total_ms', 'phases')})
        elif e['event'] == 'refresh':
            current['refreshes'].append({k: v for k, v in e.items() if k != 'event'})
    groups.append(current)
    return [g for g in groups if g['reloads'] or g['refreshes'] or g['step'] is not None]


def parse_file(path):
    with open(path, encoding='utf-8', errors='replace') as handle:
        return attribute(parse(handle))
