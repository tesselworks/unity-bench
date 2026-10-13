# Changelog

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
