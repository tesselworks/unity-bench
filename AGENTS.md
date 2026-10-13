# Agent notes

Unity Bench compares Unity Editors and player backends on one project with a fixed A/B schedule. MIT licensed.

## Principles (do not reintroduce the old refusal machinery)

- **Annotate, never refuse.** Host CPU and memory are recorded next to each process. No quiet gates, no settling handshakes, no discarded campaigns. A failed or timed-out process is recorded and the schedule continues.
- **Flag, never block.** Work/output identity (hash, counters) is compared in the report. Differing items are shown separately and left out of the ratio summary by default; they never stop a run.
- **Raw data is never filtered on disk.** First-use samples are labelled and excluded from the summary by default; `results.jsonl` keeps everything. `unity-bench report` regenerates the summary with other options.
- Fixed schedule (rotating A/B order), fresh process per repeat, explicit timeouts, whole process tree killed on timeout.
- Summaries are per-process median, then median across processes, min-max range, ratio, "ranges do not overlap". Describe, do not claim significance.

## Layout

- `src/unity_bench/` Python runner: `config.py` (campaign + schedule), `runner.py`, `process.py` / `windows_process.py` (owned process trees: POSIX process groups, Windows Job Objects), `host_sampling.py`, `reload_log.py`, `summary.py` (math), `distributions.py` (per-frame distributions to p50/p95/p99/max samples), `report.py`, `workspace.py` (per-variant project copies), `cli.py`.
- `packages/com.tesselworks.unity-bench/` the Unity package. `Editor/` (batch entry, adapter API) is Editor-only; `Runtime/` (assembly `UnityBench.Player`: first-frame probe and the player adapter runner) is compiled into player builds and inert unless the player is started with `-unityBenchAdapter` or `-unityBenchSignal`. Must compile unchanged on Unity 6.7 (Mono) and 7 (CoreCLR): lifecycle attributes `[OnCodeLoaded]`/`[OnCodeUnloading]` behind `UNITY_6000_7_OR_NEWER`, `partial` classes (UAC0031), no reliance on `JsonUtility` for nulls.
- `examples/minimal-unity/` tiny public-safe project with a custom adapter; `examples/minimal.toml` exercises every built-in workload.
- `docs/` methodology, adapters (result schema), workloads, pre-release checklist.

## Working here

- Tests: `python3 -I -m unittest discover -s tests`. They use `tests/fake_unity.py` and need no Unity. CI runs Linux and Windows. Windows process ownership is only verified by that CI; do not edit `windows_process.py` without a Windows run.
- Real Unity behaviour needs a real Editor run: run the example campaign with small `repeats`/`rounds` on every Editor you touch. Unity Editors and licenses are prerequisites; never install them or Unity modules, and keep runs short on shared machines.
- Python 3.11+. `psutil` is optional.
- Project-specific adapters belong in the consumer repository. Do not put private project code, paths or repository links in this repository. MatterKit (physics/fracture library) and Spill (physics incremental game) may be named as consumers with context; their source stays private.
- Commit only when asked. Binaries and run directories (`bench-runs/`) stay out of git.
