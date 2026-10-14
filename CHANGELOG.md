# Changelog

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
