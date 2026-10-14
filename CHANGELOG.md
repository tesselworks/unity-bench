# Changelog

## Unreleased

### Fixed

- **A prepared copy of a `revision` variant is now keyed by content, not by commit.** The cache key used to contain the resolved commit, so any commit, even one that only touched a workflow file or a README, made the next run prepare every copy again (minutes of copy, prepare and re-import per variant); on a CI branch that moves all the time the cache was almost never reused. The key now holds the git tree of the project directory at that commit (for a project at the repository root: the trees of `Assets`, `Packages` and `ProjectSettings` only) and, for every `file:` dependency in `Packages/manifest.json`, the tree (or blob, for a tarball) of the path it points to at that commit; a `file:` path outside the repository is keyed by a digest of its files' paths, sizes and times, as a plain copy is. The package-lock digest and the project's place in the repository stay in the key. A plain copy is keyed as before.
- The commit is still recorded, in the provenance and the report. When a run reuses a copy that was made at an older commit with the same content, the provenance names this run's `commit` and adds `prepared_from_commit`; the report's "Project commit" cell says so. `cache list` shows the commit the entry was made at.
- The cache format is 2: entries written by 0.6.0 and earlier are not read (`entry was written by another cache format`) and are removed by `cache prune`; the first run prepares each copy once more.

## 0.6.0

Editor memory: a breakdown on every Editor process, and workloads that ask whether an Editor keeps growing when the same code is reloaded again and again (tesselworks/unity-bench#15). Philosophy unchanged: annotate, do not refuse; a counter an Editor does not offer is listed, not an error.

### New

- **Memory breakdown (managed heap / Unity native / graphics / process)** on every Editor process that runs a built-in workload through the package, and on `player.scene` / `editor.scene_play`. Sampled at the start, at the end and at the peak of the measured part, from the `ProfilerRecorder` Memory counters, the `Profiler` API (as a cross-check), the .NET GC (`GC.GetGCMemoryInfo`, `GetTotalAllocatedBytes`, `GetTotalPauseDuration` by reflection, because Mono's class library lacks them) and the loaded assemblies and `AssemblyLoadContext`s; and, from the runner for every process including opens and player launches, the peak RSS of the Editor and of its process tree (psutil). The report shows it as a table with variant ratios and lists, per variant, the counters the Editor did not offer. Annotations only: a campaign can promote items with `headline = ["mem/end/*"]`. `memory = false` on a workload turns the in-process readings off. See [methodology](docs/methodology.md#memory-counters) for what each counter is, forced-collection and used/reserved caveats, and which counters Mono and CoreCLR Editors offer (verified).
- **`editor.reload_leak`** (`targets`, `rounds` default 30, `force_gc`, `memory_snapshot`): N script edit + reload cycles in one Editor with the `editor.script_edit` machinery (including the restore step after an `api` edit), memory read after each reload after a documented forced collection (`GC.Collect`, `WaitForPendingFinalizers`, `GC.Collect`, `EditorUtility.UnloadUnusedAssetsImmediate`). **`editor.play_leak`** (`rounds` default 10, `domain_reload`, `scene`): the same for Play Mode enter/exit cycles. The report gives per counter the first and last reading, the least-squares slope per cycle (MB), the slope of the second half, the residual range and the points behind it; every counter is in `summary.json` under `leak`. Their reload breakdown from the Editor log is reported as for `editor.script_edit`.
- **Memory Profiler snapshot** (`memory_snapshot = true`): a `.snap` at the end of the process in `<run dir>/snapshots/`, if the project has `com.unity.memoryprofiler`; otherwise the process records why none was taken and carries on.
- `stats.linear_fit`, `host_sampling` records the Editor process alone (`root_rss_max_mb`) and the tree's private memory (`tree_private_max_mb`, where the OS gives it).
- Example: `examples/minimal.toml` has `reload-leak` and `play-leak` entries.

### Behaviour that changes

- Editor workloads now read memory by default (`memory = true`): a few readings per process, about four a second of cheap calls (Profiler API, .NET GC) plus a full reading at every step boundary. This adds `mem/...` samples to those workloads' results and a memory section to the report; nothing already reported changes. `memory = false` removes it.
- `package.json` and the framework are 0.6.0. The Editor assembly gains `MemoryProbe` (in the runtime assembly) and `BatchMemory.cs`.
- Verified on macOS, Apple silicon, example project: both Editors offer the same 33 Memory counters and the Profiler API; Mono lacks `GC.GetGCMemoryInfo`, `GetTotalAllocatedBytes`, `GetTotalPauseDuration` and `AssemblyLoadContext`.

## 0.5.0

Scene workloads: measure how a project's scene runs in a built player or in Play Mode without writing an adapter, and grow into verified work with a few hook calls (tesselworks/unity-bench#7). Philosophy unchanged: annotate, do not refuse; a profiler marker a player does not offer is listed, not an error.

### New

- **`player.scene`** (`scene`, `warmup_seconds`/`warmup_frames`, `duration_seconds`/`duration_frames`, `time = { capture_delta, fixed_timestep }`, `vsync`, `list_markers`, plus the player keys `development`, `backend`, `headless`, `window`, `player_args`). The player is built once per variant and scene and launched `repeats` times. Inside it the package's runtime records the frame time series (warm-up and steady, p50/p95/p99/max), `ProfilerRecorder` markers and counters (main thread, PlayerLoop, physics, scripts, rendering, GC.Alloc/GC.Collect, allocation bytes and count per frame, GC and total memory, resident memory), managed GC collection counts, the process's peak working set, and custom segments.
- **`editor.scene_play`**: the same collection in the Editor in Play Mode (batchmode). It takes `state` like the other Editor workloads.
- **Which markers exist is recorded**: recorders are looked up in `ProfilerRecorderHandle.GetAvailable`; the report lists per variant what was recorded and what this Editor, player or backend did not offer ("Not available here"). `list_markers = true` stores the full list. See [methodology](docs/methodology.md#profiler-markers-per-backend) for what Unity 6.7 and 7 offer on macOS.
- **Hooks for game code** (`UnityBench.Player.Bench`): `Mark(counter, value)`, `Hash(name, text | bytes)`, `Begin(segment)` / `End(segment)`. Marks and hashes become the scene's work identity, inherited by every item, so the Work column reads `same` / `differs` / `partial`; segments are reported per call as `segment <name> steady p50...`. All calls are no-ops outside Unity Bench. Without them the report says **work not verified** (it said "not reported" for adapters, which is unchanged). The class is `Bench`, not `UnityBench`, because the namespace `UnityBench` already exists.
- **Units**: samples and distributions may carry `unit` (`ms` default, `bytes`, `count`). The report formats bytes as KB/MB/GB and counts plainly; a zero baseline has no ratio (`baseline 0`) instead of failing, and `<res` only applies to milliseconds. Result files stay `schema: 1` (additive, see [adapters.md](docs/adapters.md#the-result-file)).
- **Resident memory of the process tree** is sampled with the host CPU (`tree_rss_max_mb` and a one-second series, a lower bound of the peak) and shown in the host table.
- Example: the example project now lists `com.unity.modules.physics` in its manifest, and `examples/minimal-unity/Assets/Scenes/Bodies.unity` (rigidbodies and per-frame garbage; no Unity Bench call) and `BodiesHooked.unity` (the same with `Bench.Mark` / `Begin` / `End`), with four campaign entries in `examples/minimal.toml`.

### Behaviour that changes

- `package.json` is now versioned with the framework (0.5.0). The Editor assembly references the runtime assembly (`UnityBench.Player`) for `editor.scene_play`.

## 0.4.0

Cache control: named start states, snapshots, and prepared project copies that survive between campaigns and runs (tesselworks/unity-bench#6). Philosophy unchanged: annotate, do not refuse; a state that cannot be set up is recorded and the process runs anyway.

### Behaviour that changes **(changes numbers or where files are)**

- **Prepared copies are cached and primed by default.** After `project.prepare`, an untimed Editor open imports (and on a newer Editor migrates) the copy, and the result is snapshotted under the cache directory (`--cache-dir`, `UNITY_BENCH_CACHE_DIR`, else the OS user cache directory). The next run or campaign with the same key restores the snapshot instead of preparing: no copy, no `project.prepare`, no import. The first `warm_open` of a copy therefore no longer pays the import. `--no-cache` is the v0.3 behaviour exactly.
- **`editor.cold_open` isolates the global caches** it can: the script compilation cache (`BEE_CACHE_DIRECTORY`), the Package Manager cache (`UPM_CACHE_ROOT`) and the Accelerator (`-cacheServerEnableDownload/Upload false`), each pointed at an empty directory made for that process. With the Package Manager cache isolated, a project with registry packages downloads them in every cold open; `[cache] isolate = []` goes back to "Library deleted". The user's real caches are never read or written for the isolated parts.
- **Where the project copies live.** With the cache on, the default work directory is `<cache dir>/w` instead of `<run dir>/work`, the same path on every run: a primed project opened from another path recompiled all its scripts (31 s instead of 0.5 s in the example project). `--work` still overrides it; use the same directory every time. A leftover copy in the default directory (a killed run) is deleted; a leftover in a directory you named still stops the run.

### New

- **`state = "cold" | "warm" | "as-is"` per workload** (`editor.*`, `player.build_time`). `warm` restores the prepared copy's snapshot (everything, `Library` included) before every process. Defaults keep v0.3 behaviour: `cold` for `editor.cold_open`, `as-is` for the rest.
- **Snapshots and restore**: APFS `clonefile` on macOS, `cp --reflink=auto` on Linux, a parallel copy through the OS copy call on Windows (block clones on ReFS where Windows does them); never hard links. Deleting is retried while a just-exited Editor holds files (Windows), long paths use the `\\?\` prefix. Every restore is checked against the snapshot's manifest.
- **Keyed reuse**: key = resolved project commit (or a digest of a plain copy's files), full Editor version with revision, backend and build settings, package lock digest, injected package digest, prepare step digest (commands, the files they name, `project.prepare_inputs`) and the live copy's path. A matching entry whose files were modified is discarded and prepared again.
- `unity-bench cache list` and `cache prune [--older-than 30d] [--keep N] [--dry-run]`.
- `project.prepare_inputs` (files and directories the prepare commands depend on) and the `[cache]` table (`isolate`).
- **Report and results**: `prepared: reused <key>` or `prepared: built (N s)` per variant; the start state of every workload (`warm (snapshot <key>)`, `cold (Library removed; isolated: ...)`), restore times, and the Editor's own count of assets imported at open. `results.jsonl` gets `state` on process rows and `prepare` rows; `campaign.json` gets `cache` and `provenance.variants.<id>.prepared`; `summary.json` gets `states`. All additive, schema 1 (see [docs/adapters.md](docs/adapters.md#what-the-runner-adds-to-resultsjsonl-and-summaryjson-v04-additive)).
- Per process, the settings files the Editor rewrote (`settings_changed`) are listed.

Documentation: [methodology: Cache states](docs/methodology.md#cache-states) (what each state is, which caches `cold` isolates per OS, with evidence, and what is not controlled).

## 0.3.0

Fixes from two independent reviews of v0.2 and its first Windows campaign. Philosophy unchanged: annotate, do not refuse. Results rendered by 0.3.0 can differ from 0.2.0 renderings of the same `results.jsonl` for the reasons marked **(changes numbers or text)**.

### Results that were wrong or misleading

- **Environment per role.** A player's row in the Variants/Players tables is what the player reported (runtime, backend, Release/Development, platform, graphics API, build settings), not the Editor that built it. Several environments for one variant and role are listed. **(changes text)**
- **Identity coverage.** A pair where one side, or only some rounds, reports a hash or counters is `partial` with the coverage (`a 6/6, b 0/6`), no longer `same`. Adapters can declare a **work group** (name, hash, counters) that samples and distributions inherit, so derived metrics of one run share its work identity.
- **Aggregates.** No geometric mean over everything. An aggregate exists only for `headline` items (campaign `headline` per workload, or `Headline(...)` in an adapter), one line per family (`steady p95`, `warmup p50`, plain), identical series counted once, sub-resolution items left out with a count. Zero medians show `<res` instead of silently dropping out or raising. **(changes numbers: the old per-workload geomeans are gone)**
- **Reload summary.** The reload blocks of one round are summed before the median; block counts are reported. **(changes numbers where a round had several blocks)**
- **`--include-differing`.** The table shows the ratio marked `[work differs]`, the aggregate counts it and says so, and the option is recorded in `summary.json`.
- **Rounds.** The header states the processes and samples per item per process that actually ran, and says when that is not the campaign's `rounds` (a player adapter that runs once no longer shows "5 rounds").
- **Phases.** Adapters may tag samples `first-use`, `warm`, `warmup` or `steady`. An untagged sample is first-use/warm only when the name is measured more than once in the process, otherwise `single`. Distribution items carry their phase. First-use samples can be shown as a ratio table (`report --first-use-ratios`).
- **Startup metrics renamed** for what they measure: `first_frame` -> `launch_to_probe`, `first_frame_in_player` -> `engine_to_probe`, `process_total` -> `launch_to_exit`. The probe frame is not a presented frame.
- **Host sampling.** The CPU/memory series stay aligned when a lookup fails; a child first seen mid-run is charged its CPU so far instead of 0; a one-second series per process is stored in `results.jsonl`; "other CPU" is labelled approximate.
- **Wall time** of a process no longer includes cleanup of its process tree; a timeout records the timeout as the wall time. **(changes numbers by the cleanup time, up to ~2 s per process on Windows)**

### Statistics

- Ratio cells state n on both sides and say "ranges separate" / "ranges overlap" instead of "no overlap"; each table is followed by the chance of separated ranges with no real difference, `2 / C(a+b, a)`.
- `report --bootstrap N`: opt-in percentile-bootstrap 95% interval of each ratio of medians over processes. Descriptive.

### Schedule

- More than two variants run each rotation followed by its reverse, so every compared pair runs in both orders equally often (v0.2 rotated only, which kept 7 IL2CPP ahead of 7 CoreCLR in both repeats). `repeats` is raised to the next even number for more than two variants and the schedule says so. Two variants still alternate A,B / B,A.
- `--only` runs the `player.build_time` workloads the selected ones use, once. A `build` reference must name a workload defined earlier.

### Provenance

- `campaign.json` records the framework version and `git describe`, and per variant the resolved project commit (`HEAD` resolved), worktree or copy, uncommitted file count of a copy, package handling and warnings. `summary.*` record the rendering framework commit and the report options. Editors report `unityVersionFull` (with revision). Player builds record scripting backend, API compatibility level, IL2CPP code generation and compiler, stripping level, development flag, target and graphics APIs; a campaign can set `api_compatibility`, `il2cpp_code_generation` and `stripping` with a `[build]` table or per variant.

### Robustness

- A malformed adapter result (a `null` or string sample, non-finite counters, an unknown phase, ...) is a recorded process failure (`ResultError`); any exception in a scheduled process is recorded and the schedule continues.
- `unity-bench run` exits 3 when a process failed, never ran, or left a (workload, variant) without samples (after running everything it could).
- Windows: cleanup no longer terminates `Unity.Licensing.Client` (and any process whose image name starts with `unity.licensing`); POSIX does the same by signalling the group member by member. What was terminated and what was kept is recorded and shown. Win32 error codes appear in `windows_process` errors.
- A project that already embeds the Unity package gets it replaced in the copy, instead of an error. A copy that cannot be deleted because a surviving shared daemon holds it is reported (`left_behind`) and a later run reuses it once it is empty.

- The Unity package declares its dependency on `com.unity.modules.jsonserialize` (the player runtime uses `JsonUtility`); a project whose manifest lists no built-in modules, such as the bundled example, did not compile before.
- When the Editor stores a different value than the one a `[build]` key asked for, the build settings show it as `(requested ...)`.

### Packaging

- `pip install .` works: the wheel carries the Unity package (`unity_bench/unity_package`); `pip install -e .` and running from a checkout use `packages/`.

### Consumers (adapters, other tools)

- Result files stay `schema: 1`; new optional fields: `samples[].phase` may be null or `warmup`/`steady`, `samples[].workGroup`, `distributions[].workGroup`, top-level `workGroups` and `headline`, `environment.unityVersionFull`. Validation is stricter, see [docs/adapters.md](docs/adapters.md#the-result-file).
- C# API additions on both `AdapterContext` and `PlayerContext`: `DefineWorkGroup`, `Headline`, a trailing `workGroup` parameter on `Measure`/`Add`/`AddDistribution` (and `RecordFrames` in the player). Untagged samples are no longer pre-tagged first-use/warm on the Unity side.
- `summary.json`: `workloads[].aggregate[variant]` is now a list (one entry per family); ratio entries carry `n`, `chance_separated` and `below_resolution`; items carry `pair_coverage`, `family`, `headline`, `phases`; new `options`, `environments`, `report`, `observed`, `first_use_ratios`.

## 0.2.0

Player adapter (`player.adapter`, frame distributions, shared builds), paired baselines for Debug/Release comparisons, work identity judged per compared pair.

## 0.1.0

First release: annotate-don't-refuse runner with built-in Editor workloads (open, script edit, Play Mode, player build and startup), Editor adapters, reload-log parsing.
