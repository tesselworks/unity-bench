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

## Schedule

The schedule is fixed before anything runs (`unity-bench plan <campaign>` prints it):

- For each workload, for each repeat, the variants run in rotated order: A, B then B, A then A, B, and so on. Rotating the order spreads slow drift (thermal state, caches, background work) over both variants instead of loading it onto one.
- Every repeat is a **fresh process**. The process is the unit of repeat; a process may run several rounds internally.
- Every process has an explicit timeout. On timeout the whole process tree is killed (POSIX process groups, Windows Job Objects), the process is recorded as `timeout`, and the schedule continues.
- A failed process is recorded with its exit code or error and is left out of the numbers. It does not stop the campaign and it is not retried.

## What is measured

Built-in workloads measure wall time with the clock described in [workloads.md](workloads.md): the whole Editor process for opens, in-Editor stopwatches for edits, Play Mode and builds, a file timestamp for player startup. Project adapters bring their own stopwatch around exactly the work they care about ([adapters.md](adapters.md)).

Each measurement is a **sample** with a name, a `phase` and milliseconds. Phase is `first-use` for the first time something runs in a process (first round, first open of a project copy, first launch of a build) and `warm` after that. First-use samples pay one-off costs such as JIT, caches, project import and migration on a newer Editor, and code signing checks. They are labelled, kept in `results.jsonl`, listed in the report, and **excluded from the steady-state summary by default**. `report --include-first-use` (or `defaults.exclude_first_use = false`) includes them.

## Summary

For each item (a sample name inside a workload) and variant:

1. Take the samples of each process and compute that process's **median**.
2. Report the **median across processes** of those medians, with the **min-max range** across processes and the process count `n`.
3. For each variant that has a baseline other than itself, report the **ratio** variant / baseline of those two medians. Below 1.00 means faster than the baseline.
4. Mark the pair **"no overlap"** when the ranges are separated (the slowest process of the faster variant is faster than the fastest process of the slower one), otherwise "overlap".

By default every variant is compared with `defaults.baseline`. A campaign with pairs, for example Debug and Release of two Editors, gives each variant its own `baseline`: `6.7-debug` and `6.7-release` (its own baseline, so it has no ratio column) with `7-debug` compared with `6.7-debug` and `7-release` with `6.7-release`.

Medians of per-process medians stop one long process from dominating, and the range shows how much repeats disagree. For workloads with many items the report also gives the geometric mean of the ratios and how many items are separated in each direction.

This describes what happened in n processes. It is not a significance test. With n = 2 or 3 two ranges often fail to overlap by chance alone; read "no overlap" together with n and the range widths.

## Distributions

Adapters that run over many frames report them per phase. The runner reduces each phase, per process, to p50, p95, p99 and max (nearest-rank), and these become ordinary items named `<name> <phase> <stat>`, so the summary treats them like any other: median across processes, range, ratio. A tail percentile of one process is itself a noisy number; the range across processes shows it. Label the start of a run `warmup` and the rest `steady` (the adapter decides where the line is) and the first seconds, which pay JIT, caches and first-use work, are reported next to the steady state instead of inside it. See [adapters.md](adapters.md#distributions).

## Host load is an annotation

While each process runs, the runner samples whole-machine CPU, the CPU of the launched process tree, and memory (this needs `psutil`; without it nothing is recorded). The report shows them per variant under "Host (annotation only)": machine CPU median/max, and "other" CPU, which is machine CPU minus the launched tree and so approximates what everything else was doing.

Nothing depends on these numbers. No run is refused, delayed, retried or discarded because the machine was busy, and there are no quiet gates or settling handshakes. If a campaign must be quiet, make the machine quiet before you start, then read the annotations to confirm it was.

## Work and output identity is flagged, not enforced

A workload can report, per sample, a **hash** of its output and **counters** of the work it did. The report compares them across all processes and variants for each item:

| Status | Meaning |
| --- | --- |
| same | every reported hash/counter set is equal |
| not reported | no sample carried a hash or counters (the built-in workloads) |
| differs | something differs between variants, or one variant disagreed with itself between processes |

An item that differs is shown in a separate table with the differing values and "variants differ" or "unstable within <variant>", and is **excluded from the ratio table and from the geometric mean**, with a note saying so. `report --include-differing` puts it back in the main table. A different result can mean a different amount of work was done, in which case its ratio is not a speed comparison; it can also be harmless (a floating point sign bit). The tool tells you and you decide. Nothing blocks a run.

## Limits

- The Editor runs in `-batchmode -nographics`. There is no GPU rendering, no window and no Inspector refresh. "A frame" in Play Mode is an update, not a presented image. Player startup is headless by default for the same reason.
- n is small by design (minutes, not days). The report gives ranges, not p-values.
- Built-in Editor workloads measure the Editor on your project, including its asset-pipeline and package state. They do not separate Unity's own cost from your project's.
- A newer Editor migrates an older project on its first open. That cost is real but one-off, which is why it is first-use.
- Absolute numbers belong to one machine at one moment. Ratios between variants measured in the same campaign travel better than the milliseconds do.
- Player builds are slow, so their `rounds` and `repeats` are small by default and their ranges are wide.
- Player runs write a `PlayerPrefs` file (macOS: `~/Library/Preferences/com.DefaultCompany.<variant>.plist`) because the player is built with the project's own player settings.
