#!/usr/bin/env python3
"""Pin only an isolated consumer project's Editor version; never install or launch Unity."""
from __future__ import annotations

import argparse
import pathlib
import re


def prepare(project: pathlib.Path, unity_version: str, backend: str) -> pathlib.Path:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[a-z][0-9]+", unity_version):
        raise ValueError("Expected an exact Unity release version, including release suffix.")
    if backend not in {"mono", "coreclr"}:
        raise ValueError("Expected managed Editor backend mono or coreclr.")
    if project.is_symlink():
        raise ValueError("The prepared project must be an owned directory, not a symlink.")
    project = project.resolve(strict=True)
    if not project.is_dir() or not (project / "Assets").is_dir() or not (project / "Packages/manifest.json").is_file():
        raise ValueError("Expected an existing Unity consumer project with Assets and a package manifest.")
    settings = project / "ProjectSettings"
    if settings.is_symlink() or not settings.is_dir():
        raise ValueError("Expected an owned ProjectSettings directory.")
    version_file = settings / "ProjectVersion.txt"
    if version_file.is_symlink():
        raise ValueError("Refusing to modify a linked ProjectVersion file.")
    # The Editor's managed runtime comes from the selected installed Editor.
    # Player scripting-backend settings do not choose an Editor runtime. Backend
    # is an explicit expected identity that the actual receipt must verify.
    version_file.write_bytes(("m_EditorVersion: " + unity_version + "\n").encode("utf-8"))
    return version_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=pathlib.Path)
    parser.add_argument("--unity-version", required=True)
    parser.add_argument("--backend", required=True, choices=("mono", "coreclr"))
    args = parser.parse_args()
    prepare(args.project, args.unity_version, args.backend)
    print("Prepared isolated project version; actual Editor backend requires receipt verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
