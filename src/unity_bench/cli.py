"""Standalone command-line entry point."""
import argparse
import json
from pathlib import Path
import sys
import subprocess
from .runner import plan, run
from .report import summarize
from .evidence import write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('plan'); p.add_argument('config')
    p = sub.add_parser('run'); p.add_argument('config'); p.add_argument('--out', required=True)
    p = sub.add_parser('report'); p.add_argument('campaign'); p.add_argument('--out', help='New immutable report file; default prints sanitized report')
    args = parser.parse_args()
    try:
        result = plan(args.config) if args.action == 'plan' else run(args.config, args.out) if args.action == 'run' else summarize(args.campaign)
        if args.action == 'report' and args.out:
            write(Path(args.out), result)
        else:
            print(json.dumps(result, indent=2, allow_nan=False))
        return 1 if args.action == 'run' and result['failures'] else 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('Campaign refused: ' + type(error).__name__, file=sys.stderr)
        return 2
