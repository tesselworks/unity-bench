# Agent notes

Unity Bench compares Unity Editors and player backends on one project with a fixed A/B schedule. MIT licensed.

## Principles (do not reintroduce the old refusal machinery)

- **Annotate, never refuse.** Host CPU and memory are recorded next to each process. No quiet gates, no settling handshakes, no discarded campaigns. A failed or timed-out process is recorded and the schedule continues.
- **Flag, never block.** Work/output identity (hash, counters) is compared in the report, with the coverage behind "same" (a pair where one side does not report is `partial`). Differing items are shown separately and left out of the ratio summary by default; they never stop a run.
- **Raw data is never filtered on disk.** First-use samples are labelled and excluded from the summary by default; `results.jsonl` keeps everything. `unity-bench report` regenerates the summary with other options.
- Fixed schedule (A/B alternation; more variants use each rotation followed by its reverse, repeats raised to an even number), fresh process per repeat, explicit timeouts, whole process tree killed on timeout except shared Unity daemons (the licensing client).
- Summaries are per-process median, then median across processes, min-max range, ratio, "ranges separate" with n on both sides and the chance of that with no difference. Describe, do not claim significance.
- **No aggregate over everything.** Geometric means exist for `headline` items only, per family of statistic. Do not reintroduce a default mean.
- **Cache state is recorded, never assumed.** A process row says which state it asked for and which it had; a state that cannot be set up (no snapshot, a restore that did not verify) degrades to `as-is` with a note. A prepared copy is reused only while its key and manifest still match. Never touch the user's real Unity caches: `cold` redirects with environment variables for one Editor process, nothing else.
- Provenance is recorded, never enforced: resolved commits, framework commit, Editor revision, player build settings.

## Layout

- `src/unity_bench/` Python runner: `config.py` (campaign + schedule), `runner.py`, `result.py` (result-file checking, phase derivation, work groups), `process.py` / `windows_process.py` (owned process trees: POSIX process groups, Windows Job Objects; shared Unity daemons are kept), `host_sampling.py`, `reload_log.py`, `stats.py` (medians, ratio, chance of separation, bootstrap), `identity.py` (work identity and coverage), `summary.py` (items, aggregates, environments), `distributions.py` (per-frame distributions to p50/p95/p99/max samples), `report.py`, `provenance.py`, `workspace.py` (per-variant project copies, package injection, materialising and snapshotting a copy), `cache.py` (cache keys, Editor identity, manifests, the store, list/prune), `fastcopy.py` (clones, parallel copy, retried deletes, long paths), `states.py` (cold/warm/as-is, what `cold` isolates), `cli.py`. `setup.py` ships the Unity package inside the wheel.
- `packages/com.tesselworks.unity-bench/` the Unity package. `Editor/` (batch entry, adapter API) is Editor-only; `Runtime/` (assembly `UnityBench.Player`: first-frame probe, the player adapter runner, `SceneWorkload` for `player.scene`/`editor.scene_play`, and the `Bench` hook API for game code; the Editor assembly references it) is compiled into player builds and inert unless the player is started with `-unityBenchAdapter` or `-unityBenchSignal`. Must compile unchanged on Unity 6.7 (Mono) and 7 (CoreCLR): lifecycle attributes `[OnCodeLoaded]`/`[OnCodeUnloading]` behind `UNITY_6000_7_OR_NEWER`, `partial` classes (UAC0031), no reliance on `JsonUtility` for nulls.
- `examples/minimal-unity/` tiny public-safe project with a custom adapter and two scenes for the scene workloads; `examples/minimal.toml` exercises every built-in workload.
- `docs/` methodology, adapters (result schema), workloads, pre-release checklist.

## Working here

- Tests: `python3 -I -m unittest discover -s tests` (a single test: `cd tests && python3 -m unittest test_runner.RunnerTests.test_x`). They use `tests/fake_unity.py` and need no Unity. CI runs Linux and Windows. Windows process ownership is only verified by that CI; do not edit `windows_process.py` without a Windows run.
- Result-file and `summary.json` changes are consumer-visible (MatterKit adapters): keep result files `schema: 1` with additive optional fields, and note changes in `CHANGELOG.md` and `docs/adapters.md`.
- Real Unity behaviour needs a real Editor run: run the example campaign with small `repeats`/`rounds` on every Editor you touch. Unity Editors and licenses are prerequisites; never install them or Unity modules, and keep runs short on shared machines.
- Python 3.11+. `psutil` is optional.
- Project-specific adapters belong in the consumer repository. Do not put private project code, paths or repository links in this repository. MatterKit (physics/fracture library) and Spill (physics incremental game) may be named as consumers with context; their source stays private.
- Commit only when asked. Binaries and run directories (`bench-runs/`) stay out of git.
