"""Command line: unity-bench run <campaign> | report <run-dir> | plan <campaign>."""
import argparse
import datetime
from pathlib import Path
import sys

from . import config
from .report import write_reports
from .runner import run_campaign


def main(argv=None):
    parser = argparse.ArgumentParser(prog='unity-bench', description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('run', help='run a campaign and write a run directory')
    p.add_argument('campaign')
    p.add_argument('--out', help='new run directory (default: bench-runs/<name>-<timestamp>)')
    p.add_argument('--only', action='append', metavar='WORKLOAD', help='run only this workload id (repeatable)')
    p.add_argument('--keep-work', action='store_true', help='keep the per-variant project copies (large: each has a Library)')
    p = sub.add_parser('plan', help='print the fixed schedule without running anything')
    p.add_argument('campaign')
    p = sub.add_parser('report', help='regenerate summary.md and summary.json from results.jsonl')
    p.add_argument('run_dir')
    p.add_argument('--include-first-use', action='store_true', help='include first-use samples in the summary')
    p.add_argument('--include-differing', action='store_true', help='keep items with different work/output in the main tables')
    args = parser.parse_args(argv)
    try:
        if args.action == 'plan':
            cfg = config.load(args.campaign)
            for row in config.schedule(cfg):
                print(f"{row['index']:4d}  {row['workload']:<24} {row['variant']:<20} repeat {row['repeat']}")
            return 0
        if args.action == 'run':
            cfg_name = config.load(args.campaign)['name']
            out = args.out or Path('bench-runs') / f"{cfg_name}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
            out = run_campaign(args.campaign, out, keep_work=args.keep_work, only=args.only)
            print((out / 'summary.md').read_text(encoding='utf-8'))
            print(f'results: {out}')
            return 0
        write_reports(args.run_dir, exclude_first_use=False if args.include_first_use else None, include_differing=args.include_differing)
        print((Path(args.run_dir) / 'summary.md').read_text(encoding='utf-8'))
        return 0
    except (OSError, config.ConfigError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
