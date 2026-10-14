"""Command line: unity-bench run <campaign> | report <run-dir> | plan <campaign> | cache list|prune."""
import argparse
import datetime
from pathlib import Path
import sys

from . import cache, config
from .report import write_reports
from .runner import problems, run_campaign
from .stats import RESOLUTION_MS

INCOMPLETE = 3   # `run` finished everything it could, but a process failed or an item has no samples (2 is a usage or configuration error)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='unity-bench', description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('run', help='run a campaign and write a run directory; exit code 3 if anything failed or is missing')
    p.add_argument('campaign')
    p.add_argument('--out', help='new run directory (default: bench-runs/<name>-<timestamp>)')
    p.add_argument('--only', action='append', metavar='WORKLOAD', help='run only this workload id (repeatable); the build workloads it needs run once as well')
    p.add_argument('--keep-work', action='store_true', help='keep the per-variant project copies (large: each has a Library)')
    p.add_argument('--work', help='where the per-variant project copies go (default: <cache dir>/w, or <run dir>/work with --no-cache); a short path avoids Windows path-length trouble. '
                                  'Use the same directory every time: the path is part of the cache key')
    p.add_argument('--cache-dir', help='where prepared project copies are kept between runs (default: $UNITY_BENCH_CACHE_DIR, else the OS user cache directory); see `unity-bench cache`')
    p.add_argument('--no-cache', action='store_true', help='prepare every copy from scratch and keep nothing: the v0.3 behaviour (a workload that starts `warm` still gets a run-local snapshot)')
    p = sub.add_parser('cache', help='list or prune the prepared project copies kept between runs')
    csub = p.add_subparsers(dest='cache_action', required=True)
    c = csub.add_parser('list', help='prepared copies: key, Editor, variant, source, size, last use')
    c.add_argument('--cache-dir')
    c = csub.add_parser('prune', help='delete prepared copies by age and count (broken entries always go)')
    c.add_argument('--cache-dir')
    c.add_argument('--older-than', metavar='AGE', help='delete copies not used for this long: 12h, 30d, 2w')
    c.add_argument('--keep', type=int, metavar='N', help='keep only the N most recently used copies')
    c.add_argument('--dry-run', action='store_true', help='say what would be deleted')
    p = sub.add_parser('plan', help='print the fixed schedule without running anything')
    p.add_argument('campaign')
    p.add_argument('--only', action='append', metavar='WORKLOAD')
    p = sub.add_parser('report', help='regenerate summary.md and summary.json from results.jsonl')
    p.add_argument('run_dir')
    p.add_argument('--include-first-use', action='store_true', help='include first-use samples in the summary')
    p.add_argument('--include-differing', action='store_true', help='keep items with different work/output in the main tables (ratios are marked, and enter the aggregate)')
    p.add_argument('--first-use-ratios', action='store_true', help='show first-use samples as their own table with ratios')
    p.add_argument('--bootstrap', type=int, default=0, metavar='N', help='add a percentile-bootstrap 95%% interval (N resamples over processes) of each ratio of medians; descriptive')
    p.add_argument('--resolution-ms', type=float, default=RESOLUTION_MS, help='medians below this print as <res and are left out of ratios (default: %(default)s)')
    args = parser.parse_args(argv)
    try:
        if args.action == 'plan':
            notes = []
            cfg = config.load(args.campaign)
            rows = config.schedule(cfg, args.only, notes)
            for row in rows:
                print(f"{row['index']:4d}  {row['workload']:<24} {row['variant']:<20} repeat {row['repeat']}")
            for note in notes:
                print(f'note: {note}')
            scheduled = {r['workload'] for r in rows}
            for w in cfg['workloads']:
                if w['id'] in scheduled and w.get('state') in ('cold', 'warm'):
                    print(f"note: {w['id']}: every process starts {w['state']}" + (' (Library deleted, global caches isolated: ' + ', '.join(cfg['cache']['isolate']) + ')' if w['state'] == 'cold' and cfg['cache']['isolate']
                                                                                      else ' (from the snapshot of the prepared copy)' if w['state'] == 'warm' else ''))
            return 0
        if args.action == 'cache':
            store = cache.Store(Path(args.cache_dir).expanduser() if args.cache_dir else cache.default_cache_dir())
            if args.cache_action == 'list':
                print('\n'.join(cache.listing(store)))
                return 0
            if args.older_than is None and args.keep is None:
                raise ValueError('prune needs --older-than and/or --keep')
            removed = store.prune(older_than=cache.parse_age(args.older_than) if args.older_than else None, keep=args.keep, dry_run=args.dry_run)
            for e in removed:
                print(f"{'would delete' if args.dry_run else 'deleted'} {e.key}  {e.meta.get('last_used', '?')}  {cache.human_size(e.meta.get('size_bytes', 0))}")
            print(f"{len(removed)} prepared cop{'y' if len(removed) == 1 else 'ies'} {'would be ' if args.dry_run else ''}deleted from {store.root}")
            return 0
        if args.action == 'run':
            cfg_name = config.load(args.campaign)['name']
            out = args.out or Path('bench-runs') / f"{cfg_name}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
            out = run_campaign(args.campaign, out, keep_work=args.keep_work, only=args.only, work=args.work, cache_dir=args.cache_dir, use_cache=not args.no_cache)
            print((out / 'summary.md').read_text(encoding='utf-8'))
            print(f'results: {out}')
            missing = problems(out)
            for line in missing:
                print(f'incomplete: {line}', file=sys.stderr)
            return INCOMPLETE if missing else 0
        write_reports(args.run_dir, exclude_first_use=False if args.include_first_use else None, include_differing=args.include_differing,
                      bootstrap=args.bootstrap, first_use_ratios=args.first_use_ratios, resolution_ms=args.resolution_ms)
        print((Path(args.run_dir) / 'summary.md').read_text(encoding='utf-8'))
        return 0
    except (OSError, RuntimeError, config.ConfigError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
