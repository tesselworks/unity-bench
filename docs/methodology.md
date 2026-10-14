# Methodology

Unity Bench answers one question: how does the same project behave on this Editor or player backend compared with that one? The method is deliberately plain. It records what happened and tells you where to be careful. It never refuses to run and never throws a measurement away.

## Variants

A **variant** is a project revision, a Unity Editor, and a player scripting backend:

| Part | Where it comes from |
| --- | --- |
| project revision | `revision` (a git commit; the runner makes a detached worktree) or, if omitted, a copy of the project directory |
| Unity Editor | `editor`, the path of the Unity executable |
| scripting backend | `backend`: `mono`, `il2cpp` or `coreclr`. It applies to player builds and player startup. The Editor's own runtime is whatever that Editor ships and is reported by the Editor, not configured |

Every variant gets its own project copy, so Library, edits and builds never mix between variants or touch your working tree. Editor workloads can also set Editor **code optimization** (`debug` or `release`) per variant. That setting only lasts for the Editor session, so each process applies it itself (after opening, before the workload starts) and nothing leaks into your normal Editor. It therefore has no effect on `editor.cold_open` and `editor.warm_open`, which measure the open itself.

Player builds can also hold three Unity player settings equal across Editors, with a `[build]` table (campaign-wide) or a `build` table on a variant: `api_compatibility`, `il2cpp_code_generation` and `stripping`, spelled as the Editor spells the enum values. Without them the project's own settings apply, and a comparison between Editors then also compares whatever each Editor migrated or defaulted. Whatever was actually used (scripting backend, API compatibility level, IL2CPP code generation and compiler configuration, managed stripping level, development flag, graphics APIs) is read back from the Editor after it applied the request and recorded with the build; the report shows it next to the player's own environment. A setting an Editor does not have fails that build instead of being skipped quietly.

## Schedule

The schedule is fixed before anything runs (`unity-bench plan <campaign>` prints it, with a note for anything it changed on purpose):

- For each workload, for each repeat, the variants run in a balanced order, so that slow drift (thermal state, caches, background work) and "who ran right after whom" are spread over the variants instead of loaded onto one.
  - **Two variants** alternate: A,B then B,A then A,B, and so on.
  - **More than two variants** run each rotation followed by its reverse: for A,B,C that is ABC, CBA, BCA, ACB, CAB, BAC. Any two consecutive repeats put every pair in both orders, so every compared pair runs in both orders equally often, and `2n` repeats also put each variant in each position equally often. A plain rotation does not do this (ABC then BCA always runs A before B and B before C in the first half). Because the balance needs pairs of repeats, **`repeats` is raised to the next even number for more than two variants** (`repeats = 3` runs 4); `plan` and the report say so. With two variants an odd `repeats` ends one repeat unbalanced; that is the price of keeping the number you asked for.
- Every repeat is a **fresh process**. The process is the unit of repeat; a process may run several rounds internally.
- Every process has an explicit timeout. On timeout the whole process tree is killed (POSIX process groups, Windows Job Objects), the process is recorded as `timeout`, and the schedule continues. The wall time of a process stops when it exits (or at the timeout), before the process tree is cleaned up.
- A failed process is recorded with its exit code or error and is left out of the numbers. It does not stop the campaign and it is not retried. When the campaign has finished, `unity-bench run` exits with code 3 if any process failed, never ran or left a variant without samples, so a CI job can tell; everything that could run has run by then.
- `--only <workload>` runs just that workload, plus (once) the `player.build_time` workload it uses through `build`, because its launches have no player without it.

### What cleanup does to the machine

When a process ends, every process still alive in its group (POSIX) or job (Windows) is terminated, and the report says which ones (for example Unity's asset import workers and build backend). One class is deliberately left alone: **shared Unity daemons**, today the Unity Licensing Client. An Editor starts it, but it serves every Editor on the machine; killing it with the Editor's tree would make the next Editor pay to start it again and would take it away from the Editor you have open yourself. On Windows the licensing client cannot be allowed to "break away" from the job (the Editor creates it without the breakaway flag, and silent breakaway would let every child escape), so cleanup lists the job's members, terminates all but the shared ones, and clears the job's kill-on-close limit before closing it. On POSIX the group is signalled member by member instead of with one `killpg`. A job or group without a shared daemon, or one that cannot be listed, is still killed atomically. A surviving daemon can have a project copy as its working directory, which on Windows keeps that directory from being deleted: the runner removes the contents, retries, reports what it could not remove (`left_behind` in `campaign.json`), and a later run reuses an emptied leftover directory (it refuses one that still has files, and never deletes them for you).

## Cache states

Unity caches in many places, and "cold" used to mean only "`Library` deleted". Each workload that starts an Editor on the project (`editor.*`, `player.build_time`) now says where it starts with `state`, and every process records the state it actually had.

| State | The process starts from | Default for |
| --- | --- | --- |
| `cold` | `Library` deleted, and the global caches below pointed at empty directories made for that one process | `editor.cold_open` |
| `warm` | the snapshot taken after the variant's copy was prepared and imported: the whole copy (Assets, ProjectSettings, Packages, `Library`) is put back before every process | nothing; ask for it |
| `as-is` | whatever the previous process left | every other workload, as before v0.4 |

`warm` is what makes repeats comparable: every process starts from the same state instead of from whatever the previous one left (a `warm_open` after a script edit workload, a Play Mode process after a build). `as-is` stays the default because some workloads are meant to build on each other (an incremental build starts from the cache the clean builds left).

The record is in `results.jsonl` (`state` on each process row: requested and actual state, snapshot key, restore time and method, whether the restored copy matched the snapshot, the isolated caches, and the Editor's own count of assets imported at open, read from its log) and in the report ("Start states and prepared copies"): `warm (snapshot <key>)`, `cold (Library removed; isolated: compilation, upm, accelerator)`. A state that could not be set up is not an error: the process runs from what is there and says so (`requested warm, actual as-is`, with the reason).

### Prepared copies, snapshots and reuse

Preparing a variant's copy used to repeat in every campaign: copy or check out, inject the package, run `project.prepare`, and import everything in the first measured open (about 3.5 minutes per variant and campaign in the first Windows session). Now:

1. After `project.prepare`, an untimed **priming open** (`-quit`, recorded as a `prepare` row, not a process) imports the project and, on a newer Editor, migrates it.
2. That state is **snapshotted** (the copy's root without `.git`, `Temp` and `Logs`, `Library` included) as a cache entry. Only a clean point is snapshotted: the Editor exited 0. A failed priming open is recorded, nothing is cached, and the schedule runs on the unprimed copy (as in v0.3).
3. The next run, or the next campaign of a session, finds the entry by its **key** and restores it instead of preparing: `prepared: reused <key>` instead of `prepared: built (N s)`.

The key is made of everything that decides what the prepared state is: the resolved project commit (for a plain copy, a digest of the files' paths, sizes and times), the full Editor version with its revision, the variant's backend and `build` settings, the digest of `Packages/packages-lock.json`, the digest of the injected Unity package, the digest of the prepare step (the commands, the files they name and `project.prepare_inputs`, see [workloads.md](workloads.md#preparing-the-project-copy)), and **the absolute path of the live copy**. The path is in the key because Unity's `Library` holds absolute paths: a primed project opened from another directory recompiled all its scripts (31 s instead of 0.5 s on the example project, 6000.7.0b2 on macOS). So the live copies sit in a fixed place, `<cache dir>/w/<variant id>` by default or whatever `--work` names, and a different `--work` is a different entry.

Guards against a prepared copy that is no longer what its key says:

- Each entry stores a **manifest**: the top level of the copy, the SHA-256 of every `ProjectSettings` file and of the package manifest and lock, the size of the top-level files, (files, bytes, newest time) of `Assets` and `Packages`, and the top level of `Library` with the size and time of `Library/ArtifactDB` and `SourceAssetDB`. It is compared again before the entry is used. If anything differs, the entry is discarded and the copy is prepared again (`prepared: built ... the cached copy was not used: files changed since the snapshot: ProjectSettings/EditorSettings.asset`).
- After every restore the live copy is compared with the same manifest (`verified` on the process row); a mismatch is restored once more and then recorded.
- Every process row lists the settings files the Editor rewrote during that process (`settings_changed`), and the entry records which ones the priming open changed.
- A key is exact, not approximate: touching a source file of a plain copy changes it (a re-preparation, never a stale reuse).

The manifest is cheap on purpose (a few hundred KB hashed, a stat of the project's own files); it does not hash `Library`. It detects edits, deletions and a half-made copy, not a bit flip inside a cache file.

Restoring replaces the whole top level of the live copy by a copy of the snapshot. On macOS that is an APFS clone (`clonefile`, no data is copied; writes of either side diverge, nothing is hard-linked) and takes well under a second for a small project. On Linux it is `cp --reflink=auto` (a reflink on btrfs and XFS, a plain copy elsewhere). On Windows it is a parallel copy through the OS copy call, which clones blocks on ReFS and Dev Drives where Windows does so and is a plain copy on NTFS; long paths use the `\\?\` prefix, and deleting is retried while the Editor's children still hold files. The method and time are recorded per process (`restore_method`, `restore_ms`).

`unity-bench cache list` shows the entries (key, Editor, variant, source, size, last use, how often reused); `unity-bench cache prune [--older-than 30d] [--keep N] [--dry-run]` deletes by age and count. An entry is the size of the project plus its `Library`; on a file system without clones, the live copy and the entry each take that much. `--cache-dir` (or `UNITY_BENCH_CACHE_DIR`) chooses the directory, `--no-cache` goes back to v0.3 (prepare everything, keep nothing; a `warm` workload still gets a snapshot for that run).

### What `cold` isolates

`cold` deletes `Library` and sets the following for that one Editor process. The directories are empty and private to the process (a fresh temporary directory, deleted afterwards), so the user's real caches are neither read nor written. A campaign chooses the set with `[cache] isolate = ["compilation", "upm", "accelerator"]` (the default; `[]` makes `cold` only delete `Library`).

| Cache | Where it lives | Isolated by | Evidence (macOS, 6000.7.0b2 and 7000.0.0a7) |
| --- | --- | --- | --- |
| Script compilation cache (Bee; "Compilation cache" in Preferences) | `~/Library/Unity/cache/bee` (macOS) | `BEE_CACHE_DIRECTORY` | A cold process's log ends with `Total cache size 143611` (6000.7.0b2) / `147627` (7000.0.0a7), the size of the empty directory; a normal process on the same project prints `267305024`, the user's. Three cold opens in a row printed the same size, and the user's directory kept its file count, size and newest modification time |
| Package Manager global cache (registry tarballs, git checkouts, package database) | `~/Library/Unity/cache/{npm,packages}` and `~/Library/Caches/Unity/upm` (macOS) | `UPM_CACHE_ROOT` | The empty directory receives `db`; in a project with `com.unity.postprocessing` (not shipped with the Editor) it grew to 48 MB, the first open took 21.5 s / 19.2 s against 8.4 s / 8.9 s for the second, and the user's caches were untouched. The Editor itself says "The Packages Cache location is set by environment variable UPM_CACHE_ROOT" |
| Accelerator / cache server (shared asset import results and shader cache) | a server, when the project or the user's preferences name one | `-cacheServerEnableDownload false -cacheServerEnableUpload false` | Off by default. Both Editors accept the arguments and their logs report `Imports: total=N (actual=N, local cache=0, cache server=0)`; not tried against a real Accelerator |
| Imported assets, shader cache (`Library/ShaderCache`), Burst cache (`Library/BurstCache`), Bee artifacts, `PackageCache`, IL2CPP and build caches | inside the project's `Library` | deleting `Library` | A cold open's log says `Imports: total=43 (actual=43, local cache=0, cache server=0)`, a warm one `total=0` |

The variables are not specific to an OS, and Unity's own defaults for the locations differ (the Windows and Linux ones were not looked up here). On Windows and Linux the isolation uses the same variables and arguments, but only macOS was verified.

Not isolated:

- **GI cache** (`GICacheFolder`): an Editor preference (`GICacheFolder`, shared by all projects) with no command-line switch or environment variable that we found in either Editor. It only matters for workloads that bake lighting. Its default location per OS was not looked up.
- **Asset Store cache** (`ASSETSTORE_CACHE_PATH` exists, but nothing in these workloads reads it) and the MSBuild project cache (`UNITY_MSBUILD_PROJECT_CACHE_DIR`): not touched, not verified to matter.
- The **OS file cache** and, with a window or a player, the **GPU driver's shader cache**: not controllable from here. A first open after a reboot pays for the first. This is why the first process of a workload is labelled first-use.
- Per-user settings (the Editor's preferences, licensing) are deliberately left alone. Redirecting `HOME` would be the blunt way to isolate everything, but it would also move the license and preferences that Unity finds there; that was not tried.
- With `upm` isolated, a project with registry packages downloads them for every cold process (the Editor ships some offline; the rest come from the network). That is the cost of a first open on a clean machine, and it makes cold opens depend on the network; drop `upm` from `isolate` for a campaign that must run offline.

## What is measured

Built-in workloads measure wall time with the clock described in [workloads.md](workloads.md): the whole Editor process for opens, in-Editor stopwatches for edits, Play Mode and builds, a file timestamp for player startup. Project adapters bring their own stopwatch around exactly the work they care about ([adapters.md](adapters.md)).

Each measurement is a **sample** with a name, a `phase` and milliseconds. The phase says what kind of execution it was:

| Phase | Meaning |
| --- | --- |
| `first-use` | the first time something runs in a process (first round, first open of a project copy, first launch of a build). It pays one-off costs such as JIT, caches, project import and migration on a newer Editor, and code signing checks |
| `warm` | it ran again after a first use in the same process (or the workload defines it as a repeat of an already-used thing) |
| `warmup`, `steady` | labels an adapter gives to the early and the settled part of a long series |
| `single` | a name measured once in a process. It is neither first-use nor warm: nothing was repeated, so nothing is claimed |

An adapter may tag a sample with any of the first four. An untagged sample is classified by the runner from how often its name occurs in the process (first-use for the first of several, warm for the rest, single for one), so a single execution is never called warm unless the adapter says so. First-use samples are labelled, kept in `results.jsonl`, listed in the report (as raw samples, or as a table of ratios with `report --first-use-ratios`), and **excluded from the steady-state summary by default**. `report --include-first-use` (or `defaults.exclude_first_use = false`) includes them. When the story is startup or JIT (the first Play Mode entry after an edit, the first spawn of a new object kind), the first-use table is the result, not a footnote.

## Summary

For each item (a sample name inside a workload) and variant:

1. Take the samples of each process and compute that process's **median**.
2. Report the **median across processes** of those medians, with the **min-max range** across processes and the process count `n`.
3. For each variant that has a baseline other than itself, report the **ratio** variant / baseline of those two medians. Below 1.00 means faster than the baseline.
4. Say whether the ranges **separate** (the slowest process of the faster variant is faster than the fastest process of the slower one) or overlap, with `n` on each side: `0.50 (ranges separate, n=2 vs 2)`. A median below the timer resolution (default 0.001 ms) prints as `<res`; it has no ratio and is left out of aggregates, with a count.

By default every variant is compared with `defaults.baseline`. A campaign with pairs, for example Debug and Release of two Editors, gives each variant its own `baseline`: `6.7-debug` and `6.7-release` (its own baseline, so it has no ratio column) with `7-debug` compared with `6.7-debug` and `7-release` with `6.7-release`.

Medians of per-process medians stop one long process from dominating, and the range shows how much repeats disagree. The report states how many processes and how many samples per item per process **actually ran**; if the adapter does not use the campaign's `rounds`, it says that too.

### Aggregates are for headline items only

A single number over a whole workload invites misreading: sums and their parts, p50/p95/p99/max of one series, warm-up and steady variants of one series and series that happen to be identical all vote separately, and a 0.003 ms item weighs as much as a 3 s one. So there is **no aggregate by default**. The campaign names the items that are the workload's result (`headline = ["leaf/compile+reload", "play_*"]`, or `headline = true` for all of them), and the adapter can declare its own (`Headline(...)`). For those items the report gives the geometric mean of the ratios, how many are separated in each direction and how many had the same work, with these rules:

- a family of statistics is never mixed with another: `steady p50`, `steady p95`, `warmup p50` and plain items each get their own line;
- items whose per-process values are identical in every variant (the same series reported twice) count once;
- items below the timer resolution are left out, with their count.

Pick headline items that answer the question you are asking, say why in your write-up, and quote the items, not just the mean.

### Statistics honesty

This describes what happened in n processes. It is not a significance test.

- **Ranges separate by chance.** With no real difference, two groups of a and b processes have completely separated ranges (in either direction) with probability `2 / C(a+b, a)`: 33% for 2 vs 2, 10% for 3 vs 3, 2.9% for 4 vs 4, 0.8% for 5 vs 5. The report prints the probability for the sizes in each table. With n = 2 or 3, one in three or one in ten items looks "separated" for nothing; read separated ranges across many items and with the sizes in view.
- **Samples are not repeats.** 1,000 frame times in one process are one process. The unit of repeat is the process, and a tail percentile of one process is itself a noisy number.
- **`report --bootstrap N`** adds a percentile-bootstrap 95% interval of each ratio of medians, resampling the processes of each side with replacement (N resamples, fixed seed so a report regenerates identically). It is a description of how much your few processes disagree, not a promise about the next run; with n = 2 it is coarse (three possible medians per side).
- A ratio between different work is not a speed comparison (next section).

## Distributions

Adapters that run over many frames report them per phase. The runner reduces each phase, per process, to p50, p95, p99 and max (nearest-rank), and these become ordinary items named `<name> <phase> <stat>`, so the summary treats them like any other: median across processes, range, ratio. Label the start of a run `warmup` and the rest `steady` (the adapter decides where the line is) and the first seconds, which pay JIT, caches and first-use work, are reported next to the steady state instead of inside it. See [adapters.md](adapters.md#distributions). A frame time is only comparable between variants that did the same work in the frame; for a loop that runs a fixed time rather than a fixed amount of work, report the work (a sum of step times, a counter), not only the frames.

## Host load is an annotation

While each process runs, the runner samples (once a second) whole-machine CPU, the CPU time of the launched process tree, and memory (this needs `psutil`; without it nothing is recorded). The report shows them per variant under "Host (annotation only)": machine CPU median/max, and "other" CPU, which is machine CPU minus the launched tree. "Other" is **approximate**: a process that starts and ends between two samples is missed, so it can over-state what else was running; a high maximum is a reason to look at the per-process series, not proof that something else used the machine. The one-second series of every process is stored in `results.jsonl`.

Nothing depends on these numbers. No run is refused, delayed, retried or discarded because the machine was busy, and there are no quiet gates or settling handshakes. If a campaign must be quiet, make the machine quiet before you start, then read the annotations to confirm it was.

## Work and output identity is flagged, not enforced

A workload can report, per sample, a **hash** of its output and **counters** of the work it did. A set of metrics that all come from one run can share one identity: the adapter defines a **work group** (a name, a hash, counters) and attaches it to the samples and distributions of that run, so a metric without a hash of its own inherits the run's. The report compares them across all processes and variants for each item and for each compared pair:

| Status | Meaning |
| --- | --- |
| same | every sample on both sides reported, and everything reported is equal |
| partial | what was reported is equal, but some samples did not report (one side only, or only some rounds). Shown with the coverage, `partial (a 6/6, b 0/6)`. Not "same": nothing was checked for the unreported part |
| not reported | no sample carried a hash or counters (the built-in workloads) |
| differs | something differs between variants, or one variant disagreed with itself between processes |

An item that differs is shown in a separate table with the differing values and "variants differ" or "unstable within <variant>", and is **excluded from the ratio table and from any aggregate**, with a note saying so. `report --include-differing` puts it back: its ratio is then marked `[work differs]` in the table and counted, as such, in the aggregate line, and the option is recorded in `summary.json`. A different result can mean a different amount of work was done, in which case its ratio is not a speed comparison; it can also be harmless (a floating point sign bit). The tool tells you and you decide. Nothing blocks a run.

## Provenance

`campaign.json` records what produced a run: the framework (`version` and `git describe` of its checkout), and per variant the **resolved** project commit (`HEAD` is resolved), whether it was a worktree or a copy, how many files were uncommitted in a copy, what happened to the Unity package (injected, or replaced the project's embedded copy), and warnings (for example a `file:` package dependency a plain copy leaves behind). Processes record the environment they reported: Unity version with revision, runtime, code optimization for the Editor; runtime, backend, build kind (Development/Release), platform and graphics API for a player, with the build settings listed above. `summary.json` and `summary.md` record the framework that rendered them and the report options.

Environments are reported per role. A player's table row shows what the **player** said (Release, WindowsPlayer, Direct3D12), not the Editor that built it. When more than one environment was seen for a variant and role, the report lists them all.

## How to read results

Short list of what a number in a report does and does not say.

- **Describe, then decide.** A ratio is the ratio of two medians of a few processes. "Ranges separate" with n = 2 happens by chance one time in three. Read it with the n and the chance printed under the table, and across many items, not as a verdict on one.
- **One mean does not summarize a workload.** Aggregates exist only for the headline items the campaign or adapter named, one line per family of statistic. Quote the items.
- **Work must match.** `same` means every sample reported and agreed. `partial`, `not reported` and `differs` mean the ratio compares things nobody verified to be the same.
- **First-use is its own result.** The steady-state table excludes it by default; JIT and startup stories live in the first-use table (`--first-use-ratios`).
- **`single` is not warm.** An item measured once per process had no warm-up; a player that ran once reports one sample, whatever `rounds` says. The header says how many samples per process ran.
- **Players are not the Editor.** Check the Players table for the runtime, build kind and settings that ran. The Editor's runtime and Debug/Release are about the Editor that built it.
- **Fixed time is not fixed work.** A frame-time distribution of a loop that runs for a fixed time shifts when the frame count shifts. Compare the work done, not only the frames.
- **Host columns are hints.** "Other CPU" is approximate and nothing was discarded because of it.
- **Different commits are different measurements.** The report names the framework commit and each variant's project commit. Results from different project commits are not one data set.

## Reproducing a published result

A published run directory contains the raw `results.jsonl`, `campaign.json` and, if you wish, the logs. The tables are a function of the raw rows and the report options, so they can be regenerated, and checked, without Unity:

```sh
git clone https://github.com/tesselworks/unity-bench && cd unity-bench
git checkout <framework commit named in summary.md or campaign.json>
python3 unity-bench.py report <run dir> <the report options named in summary.md>
```

`summary.md` and `summary.json` state the framework commit that measured the run, the one that rendered it and the options (`--include-first-use`, `--include-differing`, `--first-use-ratios`, `--bootstrap N`, `--resolution-ms`). A newer framework can re-render the same data (it never rewrites `results.jsonl`) and its output may differ from the published tables for exactly the reasons in the [changelog](../CHANGELOG.md); a result produced with a given commit regenerates from that commit. Re-running the measurement itself needs the same project at the commit in `campaign.json`, the same Editors, and the same machine class; a private project cannot be re-run by others, but its tables can still be re-aggregated.

## Limits

- The Editor runs in `-batchmode -nographics`. There is no GPU rendering, no window and no Inspector refresh. "A frame" in Play Mode is an update, not a presented image. Player startup is headless by default for the same reason.
- n is small by design (minutes, not days). The report gives ranges and an optional bootstrap interval, not p-values.
- `cold` deletes `Library` and isolates the caches listed under [Cache states](#cache-states); the OS file cache, the GPU driver's shader cache and the GI cache stay as they are.
- Built-in Editor workloads measure the Editor on your project, including its asset-pipeline and package state. They do not separate Unity's own cost from your project's.
- A newer Editor migrates an older project on its first open. That cost is real but one-off, which is why it is first-use. The migrated settings can change physics or rendering; compare the project settings per variant if it matters.
- Absolute numbers belong to one machine at one moment. Ratios between variants measured in the same campaign travel better than the milliseconds do.
- Player builds are slow, so their `rounds` and `repeats` are small by default and their ranges are wide.
- Player runs write a `PlayerPrefs` file (macOS: `~/Library/Preferences/com.DefaultCompany.<variant>.plist`) because the player is built with the project's own player settings.
