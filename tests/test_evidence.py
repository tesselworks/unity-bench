"""Exercise the public evidence module without Unity or project dependencies."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / 'src/unity_bench/evidence.py'
SPEC = importlib.util.spec_from_file_location('unity_bench_evidence_test', PATH)
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)


class EvidenceTests(unittest.TestCase):
    def test_identity_preserves_existing_order_and_unicode_serialization(self):
        left = {'z': ['한글', {'b': 2, 'a': 1}], 'a': 0}
        right = {'a': 0, 'z': ['한글', {'a': 1, 'b': 2}]}
        self.assertEqual(EVIDENCE.canonical(left), EVIDENCE.canonical(right))
        self.assertEqual(EVIDENCE.canonical(left), b'{"a":0,"z":["\\ud55c\\uae00",{"a":1,"b":2}]}')
        self.assertEqual(EVIDENCE.sha(b'abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
        self.assertNotEqual(EVIDENCE.sha(EVIDENCE.canonical(left)), EVIDENCE.sha(EVIDENCE.canonical({'a': 0, 'z': list(reversed(left['z']))})))

    def test_receipt_refuses_overwrite_and_nonfinite_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nested/receipt.json'
            EVIDENCE.write(path, {'label': '한글', 'count': 2})
            original = path.read_bytes()
            self.assertEqual(json.loads(original), {'label': '한글', 'count': 2})
            self.assertTrue(original.endswith(b'\n'))
            with self.assertRaises(FileExistsError):
                EVIDENCE.write(path, {'count': 3})
            self.assertEqual(path.read_bytes(), original)
            for index, value in enumerate((float('nan'), float('inf'), float('-inf'))):
                with self.assertRaises(ValueError):
                    EVIDENCE.write(Path(directory) / f'invalid-{index}.json', {'value': value})

    def test_process_graph_is_order_independent_bounded_and_does_not_claim_orphans(self):
        rows = [{'pid': 12, 'parentPid': 11}, {'pid': 11, 'parentPid': 10},
                {'pid': 10, 'parentPid': 9}, {'pid': 20, 'parentPid': 99},
                {'pid': 30, 'parentPid': 31}, {'pid': 31, 'parentPid': 30}]
        self.assertEqual(EVIDENCE.descendants(rows, {10}), {10, 11, 12})
        self.assertEqual(EVIDENCE.ancestry(rows, 12), {10, 11, 12})
        self.assertEqual(EVIDENCE.ancestry(rows, 99), set())
        self.assertEqual(EVIDENCE.ancestry(rows, 30), {30, 31})
        self.assertEqual(EVIDENCE.descendants(rows, {30}), {30, 31})

    def test_module_runs_outside_repository_and_lock_coordinates_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            isolated = folder / 'evidence.py'
            shutil.copyfile(PATH, isolated)
            lock = folder / 'shared/machine.lock'
            command = [sys.executable, '-I', '-c', '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('isolated_evidence', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    with module.machine_lock(pathlib.Path(sys.argv[2])):
        print('acquired')
except BlockingIOError:
    print('held')
''', str(isolated), str(lock)]
            with EVIDENCE.machine_lock(lock):
                first = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=15, check=True)
                self.assertEqual(first.stdout.strip(), 'held')
                self.assertEqual(first.stderr, '')
            second = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=15, check=True)
            self.assertEqual(second.stdout.strip(), 'acquired')
            self.assertEqual(second.stderr, '')
            self.assertTrue(lock.exists())


if __name__ == '__main__':
    unittest.main()
