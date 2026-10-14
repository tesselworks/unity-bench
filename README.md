# Unity Bench

Compare the same Unity project across Editor versions or player scripting backends, with a fixed A/B schedule, fresh processes, and per-process medians. It ships built-in workloads that work on any project (open, script edit, Play Mode, player build, player startup) and a small adapter API, for the Editor and for built players, for your own.

It was built for a Unity 6.7 (Mono) vs Unity 7 (CoreCLR) comparison. MatterKit, a physics and fracture library, is the first consumer: its scenarios plug in as Editor and player adapters (the MatterKit source is private, so those adapters live in its own repository). License: MIT.

What it does differently from a strict benchmark harness: it **annotates instead of refusing**. Host CPU and memory are recorded next to each measurement; work and output hashes are compared and flagged, with the coverage that backs "same"; first-use samples are labelled; every run records the commits, Editors and player settings that produced it. Nothing blocks a run and nothing is deleted. Cache state is a core feature: every workload names its start state (`cold`, `warm`, `as-is`), prepared project copies are snapshotted and reused between campaigns and runs, and the state each process really had is recorded. See [docs/methodology.md](docs/methodology.md), including how to read a report and how to regenerate a published one from its raw data.

## Quickstart (about 5 minutes)

You need Python 3.11+, one or more Unity Editors with a valid license, and either a checkout of this repository or `pip install .` from one. `psutil` is optional and adds host CPU/memory annotations (`pip install psutil`).

1. Copy `examples/minimal.toml` and set the `editor` paths of your two Editors (and `baseline`).
2. Look at the schedule without running anything:

   ```sh
   python3 unity-bench.py plan examples/minimal.toml
   ```

3. Run it. Start small: set `repeats = 2` and `rounds = 3` in `[defaults]`. (With more than two variants, `repeats` is raised to an even number so that every pair runs in both orders equally often; `plan` says so.)

   ```sh
   python3 unity-bench.py run examples/minimal.toml
   ```

   The run directory (default `bench-runs/<name>-<time>/`) gets `results.jsonl` (raw), `campaign.json` (what was run and with which commits), `summary.md`, `summary.json`, and every Editor log. The project is copied per variant, so your working tree is never touched. The command runs everything it can and then exits 0, or 3 if a process failed or a variant ended up without samples (2 means a usage or configuration error).

4. Regenerate the report from raw data at any time, with other options:

   ```sh
   python3 unity-bench.py report bench-runs/minimal-example-... --first-use-ratios --bootstrap 1000
   ```

5. Run it again. The second run does not prepare the project copies: it restores the prepared copies from the cache (`prepared: reused <key>` in the report), and `unity-bench cache list` shows what is kept. A workload can say where each of its processes starts (`state = "cold" | "warm" | "as-is"`, see [methodology](docs/methodology.md#cache-states)); `warm` puts the prepared copy back before every process, `cold` also isolates the global caches Unity lets us isolate.

To measure your own project: point `[project] path` at it, list your Editors under `[[variants]]`, and keep the workloads you want. Say which items are the result with `headline` on a workload; there is no aggregate over everything. For project-specific work, write an [adapter](docs/adapters.md).

`pip install .` installs the `unity-bench` command and carries the Unity package inside the wheel; `pip install -e .` (or running `python3 unity-bench.py` from a checkout) uses `packages/` in place. Unity Bench never installs Editors or Unity modules.

## Workloads

| Kind | Measures |
| --- | --- |
| `editor.cold_open`, `editor.warm_open` | open the project with and without `Library` |
| `editor.script_edit` | edit a leaf / widely-referenced / public-API script: compile time, compile+reload time, assemblies compiled, and the reload breakdown parsed from the Editor log |
| `editor.play` | Enter/Exit Play Mode, with the project's settings or with domain reload disabled |
| `editor.adapter` | your C# entry point: samples, per-frame distributions, work counters, output hash |
| `player.build_time` | `BuildPipeline.BuildPlayer`, clean or incremental, per scripting backend |
| `player.startup` | launch a built player until its probe frame (`launch_to_probe`: after the first scene loaded and two frames ran; not a presented frame) |
| `player.adapter` | your C# entry point inside a built player, over many frames: samples, p50/p95/p99/max distributions with warm-up kept apart, work counters, output hash |
| `player.scene`, `editor.scene_play` | **no code needed**: play a scene in a built player or in Play Mode and record frame time, `ProfilerRecorder` markers (main thread, PlayerLoop, physics, rendering, scripts, GC) and memory. Optional hook calls in the game make the work verified |

Details and keys: [docs/workloads.md](docs/workloads.md).

**Zero-code start, hooks when you need verified work.** To see how your main scene runs on Editor or backend B against A, point a `player.scene` (or `editor.scene_play`) workload at it: no code in the project, and you get frame percentiles and the profiler markers each backend offers. Such a table says "work not verified", because nothing tells it the variants did the same work. When that matters, add `Bench.Mark("bodies", n)` (and `Bench.Hash`, `Bench.Begin/End` for your own segments) to the game: the same workload then compares your counters and reads "same" or "differs", with no rewrite as an adapter ([Scene hooks](docs/adapters.md#scene-hooks)).

## Sample report

Output of `examples/minimal.toml` on an Apple M-series Mac while other work was running (machine CPU median 59%, max 100%), `repeats = 2`, `rounds = 3`, taken with v0.2 and re-formatted for v0.3 (the numbers are unchanged; v0.3 prints n on both sides of each ratio and the player metric is named `launch_to_probe`). Cells are median [min-max] across processes in ms, n = processes; the ratio is Unity 7 / Unity 6.7. This only shows what the report looks like: n is tiny and the machine was busy.

| Item | unity-6.7 (A) | unity-7 | unity-7 / unity-6.7 | Work |
| --- | --- | --- | --- | --- |
| warm open | 4,612 [3,922-6,175] n=3 | 6,528 [5,156-6,585] n=3 | 1.42 (ranges overlap, n=3 vs 3) | not reported |
| core/compile+reload | 2,473 [1,882-3,063] n=2 | 3,531 [3,317-3,745] n=2 | 1.43 (ranges separate, n=2 vs 2) | not reported |
| play_enter, domain reload off | 129 [127-131] n=2 | 28.0 [26.9-29.1] n=2 | 0.22 (ranges separate, n=2 vs 2) | not reported |
| adapter: xorshift | 2.79 [2.43-3.14] n=2 | 3.47 [3.27-3.67] n=2 | 1.25 (ranges separate, n=2 vs 2) | same |
| adapter: sha256 | 1,324 [1,130-1,518] n=2 | 16.9 [16.8-16.9] n=2 | 0.01 (ranges separate, n=2 vs 2) | same |
| player launch_to_probe | 345 [336-355] n=3 | 368 [365-380] n=3 | 1.07 (ranges separate, n=3 vs 3) | not reported |

Under each table the report prints how often ranges separate by chance for the n involved (33% for 2 vs 2, 10% for 3 vs 3).

The full report also lists the variants with their resolved project commits and Editor versions, the players' own runtime, build kind and build settings, first-use samples (as a ratio table with `--first-use-ratios`), per-phase reload tables, items whose work or output differed (shown apart, left out of the ratios), a headline aggregate for the items you named, host annotations and failed processes.

## Honest limits

- The Editor runs in `-batchmode -nographics`: no window, no GPU, no Inspector. Play Mode "frames" are updates, and player startup is headless by default and measures a probe frame, not a presented one.
- n is small. The report gives medians, ranges, "ranges separate" with n on both sides and the chance of that with no real difference (33% for 2 vs 2); it is not a significance test, and `--bootstrap` intervals are descriptive.
- Built-in Editor workloads measure the Editor on your project, not Unity in isolation.
- A newer Editor migrates the project on its first open. Since v0.4 that happens in an untimed priming open, not in a measured sample; `--no-cache` brings back the old behaviour, where that sample is labelled first-use.
- `cold` isolates what Unity lets us isolate (compilation cache, Package Manager cache, Accelerator, `Library`); the OS file cache, the GPU driver's shader cache and the GI cache are not controlled ([methodology](docs/methodology.md#cache-states)).
- Player adapters run a built player per repeat and a build per variant, so they are slow to schedule; tail percentiles of a single process are noisy, and the report shows their range across processes rather than a single number.
- Windows process ownership is verified by CI mocks and native tests on `windows-latest`; Unity workloads have been run on macOS (Apple silicon) and Windows (x64) with Unity 6000.7.0b2 and 7000.0.0a7.

## Repository layout

- `src/unity_bench/` runner, process ownership (POSIX process groups, Windows Job Objects), result checking, statistics and identity, reload-log parser, report
- `packages/com.tesselworks.unity-bench/` Unity package: `Editor/` batch entry and adapter API, `Runtime/` the inert-unless-asked player side
- `examples/` a tiny Unity project with one custom adapter and a campaign that exercises every built-in workload
- `docs/` [methodology](docs/methodology.md) (including how to read results and how to reproduce a published one), [adapters and result schema](docs/adapters.md), [workloads](docs/workloads.md), [pre-release checklist](docs/public-repository.md); [CHANGELOG](CHANGELOG.md)

Tests need no Unity: `python3 -I -m unittest discover -s tests`.
