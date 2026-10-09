#!/usr/bin/env python3
"""Run from a source checkout without installing dependencies."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from unity_bench.cli import main

if __name__ == '__main__':
    sys.exit(main())
