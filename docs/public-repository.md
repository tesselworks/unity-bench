# Pre-release checklist

Short list to walk before tagging a release or announcing the repository.

- [ ] `python3 -I -m unittest discover -s tests` passes in CI on Linux and Windows.
- [ ] Run `examples/minimal.toml` (small `repeats`/`rounds`) on every Editor version named in the README. Every built-in Editor workload and the example adapter produce numbers; `player.build_time` and `player.startup` ran at least once per Editor.
- [ ] The sample report in the README is from a real run, with n, machine and date stated.
- [ ] No private project names beyond the allowed ones (MatterKit, Spill, described generically), no private repository links, no personal paths or hostnames, no credentials, in files, test fixtures or git history. The log fixtures in `tests/fixtures/` are short excerpts with project paths replaced.
- [ ] `LICENSE` (MIT) present; `pyproject.toml` and README agree.
- [ ] The Unity package compiles with no warnings on each supported Editor and the `.meta` files are committed.
- [ ] No binaries, Library folders or run directories are tracked.
- [ ] Known limits are stated in the README (batchmode/no-graphics, small n, no significance test, Player adapter not yet implemented).
