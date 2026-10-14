"""Import first in any test module that runs a campaign: keeps the cache directory out of the user's real one (UNITY_BENCH_CACHE_DIR)."""
import atexit
import os
import shutil
import tempfile

ROOT = tempfile.mkdtemp(prefix='ub-test-cache-')
os.environ['UNITY_BENCH_CACHE_DIR'] = ROOT
atexit.register(shutil.rmtree, ROOT, True)
