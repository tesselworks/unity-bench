"""`pip install .` must carry the Unity package; the runner finds it in an installed copy and in a source checkout."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from unity_bench import workspace  # noqa: E402


class PackagingTests(unittest.TestCase):
    def test_a_source_checkout_finds_the_package_in_packages(self):
        path = workspace.framework_package()
        self.assertEqual(path, ROOT / 'packages' / workspace.PACKAGE_NAME)
        self.assertTrue((path / 'Editor' / 'BatchEntry.cs').is_file())

    def test_an_installed_copy_is_preferred(self):
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / 'unity_bench'
            (installed / 'unity_package').mkdir(parents=True)
            (installed / 'unity_package' / 'package.json').write_text('{}')
            with mock.patch.object(workspace, '__file__', str(installed / 'workspace.py')):
                self.assertEqual(workspace.framework_package(), installed.resolve() / 'unity_package')

    def test_the_wheel_build_ships_the_unity_package(self):
        try:
            import setuptools  # noqa: F401
        except ImportError:
            self.skipTest('setuptools is not installed')
        with tempfile.TemporaryDirectory() as lib:
            subprocess.run([sys.executable, 'setup.py', '-q', 'build_py', '--build-lib', lib], cwd=ROOT, check=True, capture_output=True)
            shipped = Path(lib) / 'unity_bench' / 'unity_package'
            self.assertTrue((shipped / 'package.json').is_file())
            self.assertTrue((shipped / 'Editor' / 'BatchEntry.cs').is_file())
            self.assertTrue((shipped / 'Runtime' / 'UnityBench.Player.asmdef.meta').is_file())   # .meta files travel too
            self.assertTrue((Path(lib) / 'unity_bench' / 'cli.py').is_file())


if __name__ == '__main__':
    unittest.main()
