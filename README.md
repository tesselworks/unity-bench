# Unity Bench

Compare the same Unity project across Editor versions or player scripting backends, with a fixed A/B schedule, fresh processes, and per-process medians. It ships built-in workloads that work on any project (open, script edit, Play Mode, player build, player startup) and a small adapter API for your own.

It was built for a Unity 6.7 (Mono) vs Unity 7 (CoreCLR) comparison. MatterKit, a physics and fracture library, is the first consumer: its scenarios are meant to plug in as adapters (the MatterKit source is private, so that adapter lives in its own repository). License: MIT.

What it does differently from a strict benchmark harness: it **annotates instead of refusing**. Host CPU and memory are recorded next to each measurement; work and output hashes are compared and flagged; first-use samples are labelled. Nothing blocks a run and nothing is deleted. See [docs/methodology.md](docs/methodology.md).

## Quickstart (about 5 minutes)

You need Python 3.11+, one or more Unity Editors with a valid license, and a source checkout of this repository. `psutil` is optional and adds host CPU/memory annotations (`pip install psutil`).

1. Copy `examples/minimal.toml` and set the `editor` paths of your two Editors (and `baseline`).
2. Look at the schedule without running anything:

   ```sh
   python3 unity-bench.py plan examples/minimal.toml
   ```

3. Run it. Start small: set `repeats = 2` and `rounds = 3` in `[defaults]`.

   ```sh
   python3 unity-bench.py run examples/minimal.toml
   ```

   The run directory (default `bench-runs/<name>-<time>/`) gets `results.jsonl` (raw), `summary.md`, `summary.json`, and every Editor log. The project is copied per variant, so your working tree is never touched.

4. Regenerate the report from raw data at any time, with other options:

   ```sh
   python3 unity-bench.py report bench-runs/minimal-example-... --include-first-use
   ```

To measure your own project: point `[project] path` at it, list your Editors under `[[variants]]`, and keep the workloads you want. For project-specific work, write an [adapter](docs/adapters.md). `pip install .` also installs the `unity-bench` command; Unity Bench never installs Editors or Unity modules.

## Workloads

| Kind | Measures |
| --- | --- |
| `editor.cold_open`, `editor.warm_open` | open the project with and without `Library` |
| `editor.script_edit` | edit a leaf / widely-referenced / public-API script: compile time, compile+reload time, assemblies compiled, and the reload breakdown parsed from the Editor log |
| `editor.play` | Enter/Exit Play Mode, with the project's settings or with domain reload disabled |
| `editor.adapter` | your C# entry point: samples, work counters, output hash |
| `player.build_time` | `BuildPipeline.BuildPlayer`, clean or incremental, per scripting backend |
| `player.startup` | launch a built player until its first frame |

Details and keys: [docs/workloads.md](docs/workloads.md).

## Sample report

Output of `examples/minimal.toml` on an Apple M-series Mac while other work was running (machine CPU median 59%, max 100%), `repeats = 2`, `rounds = 3`. Cells are median [min-max] across processes in ms, n = processes; the ratio is Unity 7 / Unity 6.7. This only shows what the report looks like: n is tiny and the machine was busy.

| Item | unity-6.7 (A) | unity-7 | unity-7 / unity-6.7 | Work |
| --- | --- | --- | --- | --- |
| warm open | 4,612 [3,922-6,175] n=3 | 6,528 [5,156-6,585] n=3 | 1.42 (overlap) | not reported |
| core/compile+reload | 2,473 [1,882-3,063] n=2 | 3,531 [3,317-3,745] n=2 | 1.43 (no overlap) | not reported |
| play_enter, domain reload off | 129 [127-131] n=2 | 28.0 [26.9-29.1] n=2 | 0.22 (no overlap) | not reported |
| adapter: xorshift | 2.79 [2.43-3.14] n=2 | 3.47 [3.27-3.67] n=2 | 1.25 (no overlap) | same |
| adapter: sha256 | 1,324 [1,130-1,518] n=2 | 16.9 [16.8-16.9] n=2 | 0.01 (no overlap) | same |
| player first_frame | 345 [336-355] n=3 | 368 [365-380] n=3 | 1.07 (no overlap) | not reported |

The full report also lists first-use samples separately, per-phase reload tables, items whose work or output differed (shown apart, left out of the ratios), host annotations and failed processes.

## Honest limits

- The Editor runs in `-batchmode -nographics`: no window, no GPU, no Inspector. Play Mode "frames" are updates, and player startup is headless by default.
- n is small. The report gives medians, ranges and "ranges do not overlap"; it is not a significance test, and with n = 2 or 3 non-overlap happens easily.
- Built-in Editor workloads measure the Editor on your project, not Unity in isolation.
- A newer Editor migrates the project on its first open; that sample is labelled first-use and excluded from the summary by default.
- A Player-side adapter (a C# entry point inside a built player) is not implemented in v0.1. Editor adapters are.
- The Python runner is used from a source checkout (it injects the package from `packages/`). Windows process ownership is verified by CI mocks and native tests; the Unity workloads have been run on macOS (Apple silicon) with Unity 6000.7.0b2 and 7000.0.0a7.

## Repository layout

- `src/unity_bench/` runner, process ownership (POSIX process groups, Windows Job Objects), reload-log parser, report
- `packages/com.tesselworks.unity-bench/` Editor-only Unity package (batch entry, adapter API)
- `examples/` a tiny Unity project with one custom adapter and a campaign that exercises every built-in workload
- `docs/` [methodology](docs/methodology.md), [adapters and result schema](docs/adapters.md), [workloads](docs/workloads.md), [pre-release checklist](docs/public-repository.md)

Tests need no Unity: `python3 -I -m unittest discover -s tests`.
