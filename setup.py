"""Ships the Unity package inside the wheel, so that `pip install .` can inject it into project copies.

The package stays in `packages/com.tesselworks.unity-bench/` for Unity users (Package Manager, "Add package from disk"). A built
wheel carries a copy as `unity_bench/unity_package`; a source checkout or `pip install -e .` finds `packages/` directly.
"""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

PACKAGE = Path(__file__).resolve().parent / 'packages' / 'com.tesselworks.unity-bench'


class BuildPy(build_py):
    def run(self):
        super().run()
        if not getattr(self, 'editable_mode', False):
            target = Path(self.build_lib) / 'unity_bench' / 'unity_package'
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(PACKAGE, target, ignore=shutil.ignore_patterns('__pycache__', '.DS_Store'))


setup(cmdclass={'build_py': BuildPy})
