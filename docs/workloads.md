# Built-in workloads

All built-in workloads work on any Unity project. Editor workloads run `Unity -batchmode -nographics`. Those that need to act inside the Editor use the batch entry in `packages/com.tesselworks.unity-bench`, which the runner copies into each variant's project copy (`project.inject_package`, default on; a copy of the package the project already embeds is replaced, so the C# that runs belongs to this runner; the framework's copy comes from the installed wheel or from `packages/` in a source checkout). The package compiles unchanged on Unity 6.7 (Mono) and Unity 7 (CoreCLR); its `Runtime/` part is inert unless a player is started with the arguments below.

Common workload keys: `id`, `kind`, `repeats` (fresh processes per variant), `rounds` (iterations inside one Editor or player process), `timeout_seconds`, `variants` (restrict to some variant ids), `memory` (`editor.script_edit`, `editor.play`, `editor.adapter`, the scene and leak workloads and `player.build_time`: read the process's memory counters at the start, at the end and at the peak, default `true`; see [Memory readings](#memory-readings)), `headline` (`true`, or a list of item-name patterns such as `["leaf/*", "play_enter"]`: the items an aggregate may include; see [methodology](methodology.md#aggregates-are-for-headline-items-only)). Defaults come from `[defaults]`. `repeats` is raised to the next even number when a workload runs on more than two variants (balanced order, see [methodology](methodology.md#schedule)).

| Kind | Clock | Items reported | Notes |
| --- | --- | --- | --- |
| `editor.cold_open` | whole Editor process | `open` | deletes `Library`, opens, quits. Repeat 0 is first-use |
| `editor.warm_open` | whole Editor process | `open` | opens with the existing `Library`, quits. Repeat 0 is first-use |
| `editor.script_edit` | stopwatch in the Editor | `<target>/compile`, `<target>/compile+reload` | edits a script, times compile and compile+reload+idle |
| `editor.play` | stopwatch in the Editor | `play_enter`, `play_exit` | Play Mode transitions |
| `editor.reload_leak` | readings after each reload | `<target>/compile`, `<target>/compile+reload`, memory slopes | N script edit + reload cycles in one Editor, memory read after each, see [below](#editorreload_leak-and-editorplay_leak) |
| `editor.play_leak` | readings after each cycle | `play_enter`, `play_exit`, memory slopes | N Play Mode enter/exit cycles in one Editor, memory read after each |
| `editor.adapter` | the adapter's own | the adapter's | see [adapters.md](adapters.md) |
| `player.build_time` | stopwatch around `BuildPipeline.BuildPlayer` | `build` | clean or incremental |
| `player.startup` | file timestamp from outside the player | `launch_to_probe`, `engine_to_probe`, `launch_to_exit` | launches a built player |
| `player.adapter` | the adapter's own | the adapter's | see [adapters.md](adapters.md#player-adapter-playeradapter) |
| `player.scene` | frames in the player | `frame`, profiler markers, GC and memory, custom segments | plays a scene with no project code, see [below](#playerscene-and-editorscene_play) |
| `editor.scene_play` | frames in Play Mode | the same | the same collection in the Editor |

## Start state (`state`)

Every workload that starts an Editor on the project (`editor.*` and `player.build_time`) takes `state = "cold" | "warm" | "as-is"`. The default keeps the behaviour of each kind: `editor.cold_open` is `cold`, everything else `as-is`. What the states are, what `cold` isolates and how snapshots work: [methodology](methodology.md#cache-states).

```toml
[[workloads]]
id = "warm-open"
kind = "editor.warm_open"
state = "warm"          # every process starts from the snapshot of the prepared copy, not from what the last one left
repeats = 4

[cache]                  # optional, campaign-wide
isolate = ["compilation", "upm", "accelerator"]   # what `cold` points at empty directories (this is the default); [] = `cold` only deletes Library
```

`state` is not accepted on `player.startup` and `player.adapter`: they run a built player, which has no project start state (the build workload they use has one).

## editor.cold_open and editor.warm_open

The time is the wall clock of the whole Editor process: launch, open, import whatever is out of date, quit. Repeat 0 is labelled first-use (the OS file cache, JIT and the first launch of that copy), so `repeats` should include it: ask for one more than the number of samples you want. Since v0.4 the import and migration of a variant's copy happen in an untimed priming open before the schedule (see [methodology](methodology.md#prepared-copies-snapshots-and-reuse)), so they no longer fall into the first `warm_open`; `cold_open` imports everything in every process. `--no-cache` restores the v0.3 behaviour, where the first open of a copy imports it.

Code optimization has no effect here because it is applied after the project has opened.

## editor.script_edit

```toml
[[workloads]]
id = "script-edit"
kind = "editor.script_edit"
rounds = 5
targets = [
  { id = "leaf",     path = "Assets/Feature/Leaf.cs",  change = "comment" },
  { id = "wide",     path = "Assets/Core/Util.cs",     change = "comment" },
  { id = "wide-api", path = "Assets/Core/Util.cs",     change = "api" },
]
```

A target's `path` is project-relative. Set `root = "worktree"` for a file outside the Unity project (a package that lives beside it in the repository): `path` is then relative to the variant's git worktree, so every selected variant needs a `revision`.

Each round, for each target: write the edit, `AssetDatabase.Refresh()`, then time
- `<id>/compile`: until script compilation finished,
- `<id>/compile+reload`: until compilation finished, the reload after it happened, and the Editor is idle.

Edits are appended to the end of the file, so it must be a normal C# file:
- `change = "comment"` appends a comment. The assembly recompiles; assemblies that reference it do not need to, so pick a leaf script and a widely-referenced script to see both ends.
- `change = "api"` appends a public class whose constant changes each round: a public API change, so every dependent assembly recompiles too. After each api edit an untimed restore step puts the file back (that is itself an API change, and would otherwise pollute the next measurement).

The report shows how many assemblies each edit compiled (annotation) and whether the Editor reloaded. Scripts are always restored; the runner also checks the files after each process and puts them back if a killed process left them modified.

### Reload breakdown

For this workload, `editor.play` and `editor.adapter`, the runner parses the Editor log of every process and attributes each block to the step whose `[UnityBench] begin/end` markers enclose it:

- `Domain Reload Profiling: Nms` blocks: the total and the phases `BeginReloadAssembly`, `LoadAllAssembliesAndSetupDomain`, `TypeCache.ScanAssembly`, `FinalizeReload` and `ProcessInitializeOnLoadAttributes` (all phases are stored in `results.jsonl`),
- `Asset Pipeline Refresh ... Scripting: domain reloads=N, domain reload time=X ms, compile time=Y ms`.

The report prints the per-phase medians across processes, with variant ratios. It is what shows *where* a reload got slower, for example unloading the old code versus loading assemblies versus running `InitializeOnLoad` code. Steps without any reload print nothing.

## editor.play

```toml
[[workloads]]
id = "play-no-reload"
kind = "editor.play"
domain_reload = "disabled"   # or "default"
scene = "Assets/Scenes/Main.unity"   # optional; default is a generated scene with a camera and light
```

Per round: `play_enter` is `EnterPlaymode()` until `EnteredPlayMode` plus one more update. `play_exit` is `ExitPlaymode()` until `EnteredEditMode`. The info columns show whether the Editor reloaded. `domain_reload = "default"` uses the project's own Enter Play Mode settings; `"disabled"` sets Enter Play Mode Options with Reload Domain off for that process and puts the project's setting back before exiting. A generated scene is created under `Assets/UnityBench/Generated` in the project copy. Note `play_enter` also includes whatever your project does on entering Play Mode, so it measures "Play in this project", not Unity's bare cost.

## Memory readings

Every Editor process that runs a built-in workload through the package (everything above except the two opens) and every scene workload reads the process's own memory counters and reports them next to its timings. They are **annotations**: the report shows them in a "Memory breakdown" table (managed heap / Unity native / graphics / process, with variant ratios) and they never enter an aggregate, unless the campaign marks them in `headline` (item names `mem/<start|end|peak>/<counter>`, for example `headline = ["mem/end/*"]`). Set `memory = false` on a workload to switch the readings off.

- **start**: when the measured part begins (the plan is built and the Editor is idle); **end**: when the last step finished; **peak**: the largest value any reading saw. The Editor workloads read the Profiler API and the .NET GC about four times a second and the full set (with the `ProfilerRecorder` counters) at every step boundary, so the peak is the largest of those; it can miss a spike between two readings. The scene workloads read at the start and the end of the steady phase, and their peak is the largest level a gauge recorder (GC Used, GC Reserved, Total Used, App Resident) saw in any steady frame; they do not read inside the frame loop, which would allocate and disturb the per-frame numbers they exist to record.
- **Process rows** (`Process RSS (runner)`, `Process tree RSS (runner)`, `Process tree Private (runner)`) come from the runner, via psutil, once a second, for every process including the opens and player launches: the launched process alone, the whole tree (an Editor starts a compiler server and import workers, so the tree is several times the Editor), and the tree's private memory where the OS lets us read it (Windows private bytes, Linux unique set size; macOS refuses it). A once-a-second sample is a lower bound of the peak.
- Which counters exist depends on the Editor or player: a counter that is not offered is listed per variant in the report ("Not available here"), never an error. What each counter means, where it comes from and which Editors offer it: [methodology](methodology.md#memory-counters).

## editor.reload_leak and editor.play_leak

Does an Editor keep growing when the same code is reloaded again and again? One Editor process runs N cycles and reads its memory after every one; the report fits a line through the readings.

```toml
[[workloads]]
id = "reload-leak"
kind = "editor.reload_leak"
targets = [{ id = "leaf", path = "Assets/FeatureA/FeatureA.cs", change = "comment" }]   # as editor.script_edit
rounds = 30                 # the default; cycles per process. `repeats` fresh processes per variant, as always
force_gc = true             # default: collect garbage before every reading (below)
memory_snapshot = false     # true: a Memory Profiler .snap at the end of the process, if the project has com.unity.memoryprofiler

[[workloads]]
id = "play-leak"
kind = "editor.play_leak"
rounds = 10                 # the default; Play Mode enter + exit cycles
domain_reload = "default"   # or "disabled", as editor.play
```

`editor.reload_leak` is `editor.script_edit` with a memory reading after each reload: the same edit, refresh, compile, reload and idle wait, the same restore step after an `api` edit, the same restoring of the files afterwards (the runner checks them too). It takes `targets` like `editor.script_edit`; with several targets a cycle edits each in turn and every reload is a point. Its time samples (`<target>/compile`, `<target>/compile+reload`) are reported as usual, so a reload that gets slower while memory grows shows up in the same table. `editor.play_leak` is `editor.play` with a reading after every exit. Both default to more rounds than the campaign's `rounds` (30 and 10); an explicit `rounds` on the workload overrides it, and at least 3 are needed for a fit.

**What a point is.** After each reload (or Play Mode exit) the Editor is idle again; then, with `force_gc = true`, it runs `GC.Collect`, `GC.WaitForPendingFinalizers`, `GC.Collect` and `EditorUtility.UnloadUnusedAssetsImmediate`, and reads every counter ([methodology](methodology.md#memory-counters) lists them). The collection is there to take garbage that is merely not collected yet out of the series; what is left is what stayed referenced. It is not what a user's Editor would hold a moment after a reload, and it means the managed-heap numbers are a floor, not the working size. `force_gc = false` reads without it.

**What the report says** per counter and variant (only the breakdown counters in `summary.md`, all of them in `summary.json` under `leak`): the first and the last reading, the least-squares slope per cycle (MB per cycle for bytes), the slope of the second half of the series (which leaves out start-up growth that has not settled), the range of the residuals around the fitted line, and the points and processes behind it. Each process is fitted on its own, the table shows the median over processes and the range of their slopes. A residual range that is large next to the slope says the series is not a line (steps, or a reserve that grows in chunks): read the first and last values and the second-half slope then.

**Memory Profiler snapshot.** With `memory_snapshot = true` the last step takes a `.snap` with the engine's snapshot API and keeps it in `<run dir>/snapshots/`. The project must have the `com.unity.memoryprofiler` package (it is what opens the file; `1.1.12` compiled on both Editors, `1.1.6` did not on 6000.7.0b2); without it the process records `memory.snapshot: skipped: com.unity.memoryprofiler is not in the project` and carries on. Snapshots are large (about 65 MB for a Mono Editor and 225 MB for a CoreCLR one after four reloads of the example project) and are not part of the results.

## player.build_time

```toml
[[workloads]]
id = "build"
kind = "player.build_time"
mode = "clean"         # or "incremental"
backend = "il2cpp"     # optional; default is the variant's backend (mono, il2cpp, coreclr)
rounds = 2
repeats = 2
development = false
scenes = ["Assets/Scenes/Main.unity"]   # optional; default is the generated empty scene
prebuild = "MyNamespace.MyEditor.PrepareBuild"   # optional static void(), run in the Editor before each build, untimed
```

Times `BuildPipeline.BuildPlayer` with a stopwatch, for the host platform's standalone target (`build_target` overrides). `clean` deletes the output and passes `BuildOptions.CleanBuildCache` every round. `incremental` does a full build in round 0 (first-use) and then rebuilds the same output with nothing changed. An unavailable backend fails the process with a clear error (CoreCLR exists only in Unity 7; a missing platform module also fails). Unity Bench never installs modules. The report also stores the build report's own total time and output size.

The build settings that were really used are read back from the Editor and stored with the build (`notes` of the build process, `build` of every player process that ran it): scripting backend, API compatibility level, IL2CPP code generation, IL2CPP compiler configuration, managed stripping level, development flag, target and graphics APIs. A campaign can set three of them so that they are equal across Editors where the Editors have them:

```toml
[build]                               # all variants; a variant can override with its own `build = { ... }`
api_compatibility = "NET_Standard"    # as the Editor names the ApiCompatibilityLevel value
il2cpp_code_generation = "OptimizeSpeed"
stripping = "Minimal"
```

The values are looked up by name on the Editor (the enum names differ between Unity 6.7 and 7), so a value an Editor does not have fails that build with the list of valid names, instead of building with something else. Leave a key out to keep the project's own setting. The Editor may also store a different value than the one it was given (Unity 7 stores `NET_Standard` as `NET` for IL2CPP); the build settings then read `API level NET (requested NET_Standard)`, and the raw notes carry `build.apiCompatibilityRequested`.

`clean` removes the output folder and the Bee build cache; it does not clear the shader cache, the Library or OS file caches.

## player.startup

```toml
[[workloads]]
id = "startup"
kind = "player.startup"
build = "build"        # use the player built by that player.build_time workload
repeats = 5
headless = true        # -batchmode -nographics; false opens a 640x360 window
```

Launches the built player with `-unityBenchSignal <file>`. What it measures, exactly: a small probe in the package's `Runtime/` folder is created after the first scene has loaded, waits for its `Start` and then until two more frames have completed, writes the file in that `Update`, and quits. It does not check that anything was presented to the screen or that your scene's content is ready; it is "the engine got to a running frame loop", not "the player was usable". The items:

- `launch_to_probe`: the runner's clock from just before the launch to the file's modification time, so it includes process spawn, engine start, the first scene load and the probe's frames, and does not depend on how often the runner polls;
- `engine_to_probe`: `Time.realtimeSinceStartup` inside the player when the file is written (it starts when the engine starts, so it excludes process spawn);
- `launch_to_exit`: the whole process including quitting.

These replace the v0.2 names `first_frame`, `first_frame_in_player` and `process_total`. For "usable" (first scene content, first interaction) write a `player.adapter` that measures it. Repeat 0 is first-use: the first launch of a fresh build pays OS caches and code signing checks; the later repeats are tagged `warm` by the workload.

With `unity-bench run --only`, a workload that names a `build` pulls that build workload in (once). A `build` workload must be defined before the workloads that use it.

Without `build`, the runner builds the player once per variant before the first launch (a later `player.startup` or `player.adapter` with the same backend, scenes, `development`, `build_target` and `prebuild` runs the same build); that build is stored as a process record with `role: setup-build` and is not part of any summary.

## player.scene and editor.scene_play

Play one scene of the project and record how it runs. Nothing in the project has to change: the package's runtime part (inert unless the process was started by the runner) loads the scene, waits out a warm-up, then records for the duration. `player.scene` builds the player once per variant (the build holds exactly this scene, `development` and `backend` as for the other player workloads, shared with any workload that has the same settings) and launches `repeats` fresh processes. `editor.scene_play` opens the scene in the batchmode Editor and records in Play Mode; it takes `state` like the other Editor workloads.

```toml
[[workloads]]
id = "main-scene"
kind = "player.scene"                       # or editor.scene_play
scene = "Assets/Scenes/Main.unity"          # required, project-relative
warmup_seconds = 2                          # or warmup_frames; default 2 s
duration_seconds = 10                       # or duration_frames; default 10 s
time = { capture_delta = 0.016667, fixed_timestep = 0.02 }   # optional
vsync = false                               # default false: vSyncCount 0 and no frame-rate cap while recording; true keeps the project's setting
headless = false                            # player only; default a 1280x800 window (window = "1280x800"); true = -batchmode -nographics
list_markers = false                        # true: store every profiler recorder the player offers in the process notes (markers.catalog), for diagnosis
repeats = 5
```

- Give each of warm-up and duration in seconds **or** frames, not both. Seconds are real time, so variants that run faster simulate more frames. For work you want to compare, use `duration_frames`.
- `time.capture_delta` sets `Time.captureDeltaTime`: game time then advances by this fixed step per frame whatever the real frame time is, so every variant simulates the same frames. `time.fixed_timestep` sets `Time.fixedDeltaTime`. Both are put back when the run ends. Frame times stay real wall-clock times either way.
- `headless = false` is the default for the player: a headless player has no rendering, so there are no rendering markers.

### What is recorded

Everything is a per-frame series of the steady phase (the warm-up is excluded), reduced per process to `p50`, `p95`, `p99` and `max` like any [distribution](adapters.md#distributions), so the items are named `<metric> steady p50` and so on. Values have a unit (`ms`, `bytes`, `count`), shown in the table.

| Item | Source | Unit |
| --- | --- | --- |
| `frame warmup ...`, `frame steady ...` | wall time between two frames | ms |
| `Main Thread`, `PlayerLoop`, `Update.ScriptRunBehaviourUpdate`, `FixedUpdate.PhysicsFixedUpdate`, `Physics.Simulate`, `Physics2D.Simulate`, `Culling`, `Camera.Render`, `RenderLoop`, `Gfx.WaitForPresentOnGfxThread`, `Gfx.WaitForRenderThread`, `GC.Alloc`, `GC.Collect` | `ProfilerRecorder` marker, summed over the frame | ms |
| `SetPass Calls Count`, `Triangles Count`, `Vertices Count`, `GC Allocation In Frame Count` | `ProfilerRecorder` counter of the frame | count |
| `GC Allocated In Frame` | `ProfilerRecorder` counter of the frame | bytes |
| `GC Used Memory`, `GC Reserved Memory`, `Total Used Memory`, `App Resident Memory` | `ProfilerRecorder` level at each frame | bytes |
| `GC Allocated In Frame steady total`, `GC Allocation In Frame Count steady total` | sum over the steady phase | bytes, count |
| `GC collections gen0 steady` (and gen1, gen2) | `GC.CollectionCount` change over the steady phase (managed, so it exists on every backend) | count |
| `process peak working set` | the player's own `PeakWorkingSet64` at the end, when the platform gives one | bytes |
| `segment <name> steady ...` | `Bench.Begin/End` calls in the game, one value per call | ms |

A marker or counter that this Editor, player or backend does not offer is **not an error**: the recorders are looked up in the list `ProfilerRecorderHandle.GetAvailable` returns, and the missing ones are listed per variant in the report ("Not available here") and in the process notes (`markers.recorded`, `markers.missing`, `markers.offered`). What a Release player lists is much shorter than the Editor's list: see the table in [methodology](methodology.md#profiler-markers-per-backend). A zero-time marker prints `<res`; a zero baseline has no ratio (`baseline 0`).

The Editor's batchmode Play Mode never closes profiler frames, so the recorders there keep accumulating; the runtime then takes the change since the previous frame, which is the same quantity. In a player the recorder's own per-frame samples are used.

Also stored: the process's host RSS (sampled once a second, a lower bound of the peak), and as notes the frames and seconds of the steady phase, the clock settings and which hooks the scene called.

### Work: verified or not

The frames and markers say how the scene ran, not that two variants ran the same scene. Without hook calls from the game the report labels every item **work not verified**. When the game calls `Bench.Mark`, `Bench.Hash` (see [Scene hooks](adapters.md#scene-hooks)), those counters and hash become the scene's work identity, attached to every item of the workload, and the Work column reads `same` / `differs` / `partial` like for adapters. Add the calls later and the same workload becomes verified; no workload has to be rewritten as an adapter.

## Preparing the project copy

Some projects need a step before Unity can open them (stage a plugin, generate data, copy an adapter in). `[project] prepare` lists commands, each a list of strings, run once per variant in that variant's copy before the schedule starts. The working directory is the copy's root (the git worktree for variants with a `revision`, otherwise the copied project). Placeholders: `{python}` (the interpreter running Unity Bench), `{root}`, `{project}`, `{campaign}` (the campaign file's directory) and `{variant}`. A failing command stops the run before any measurement; its output is in `logs/prepare-<variant>-<n>.log`. A plain copy of the project (no `revision`) leaves behind `file:` package dependencies that point outside the project; the run records a warning for each in `campaign.json` and the report.

```toml
[project]
path = "../unity/MyProject"
prepare = [["{python}", "{campaign}/prepare.py", "--root", "{root}", "--project", "{project}"]]
```

Variants that no scheduled workload uses get no copy.

**Where the copies go.** By default `<cache dir>/w/<variant id>`, the same place on every run, because Unity's `Library` holds absolute paths and a prepared copy only stays prepared at the path it was prepared at. `unity-bench run --work <dir>` puts them elsewhere; keep using the same directory (the path is part of the cache key), and on Windows prefer a short one, which keeps Library paths under the 260-character limit. With `--no-cache` the default is `<run dir>/work` as before. Two campaigns that use the same work directory and variant ids must not run at the same time.

**What the prepare step is, for the cache.** The prepared copy is reused when the key matches (see [methodology](methodology.md#prepared-copies-snapshots-and-reuse)); the key includes the prepare commands and the contents of any file a command names by an absolute path or under `{campaign}` (up to 8 MB). A script usually depends on more than itself, so list the rest:

```toml
[project]
prepare = [["{python}", "{campaign}/prepare.py", "--root", "{root}", "--project", "{project}", "--with", "core"]]
prepare_inputs = ["adapters", "helpers.py"]   # files or directories relative to this campaign file; their contents are part of the key
```

A campaign whose prepare commands differ from another's gets its own prepared copy: to share one across the campaigns of a session, give them the same `prepare` (stage the union of what they need).

`{variant}` in a prepare command also puts the variant's id into the key (otherwise the id is not: the live path, which contains it, is).

## Output files

A run directory contains `campaign.json` (resolved campaign, schedule and its notes, host, provenance: framework version and commit, and per variant the resolved project commit), `results.jsonl` (raw; one row per process, sample and reload group, plus a `prepare` row for each untimed priming open; a process row carries its start `state`, its environment, the player's `build` settings, adapter-declared `headline` patterns, the `cleanup` record of what was terminated and what was left running, and the host's one-second CPU/memory series), `summary.md` and `summary.json` (regenerated by `unity-bench report`), and `logs/`, `requests/`, `results/` for every process. `--keep-work` keeps the project copies and builds, which can be large.

`unity-bench run` exits 0 when every scheduled process succeeded and produced samples, 2 on a usage or configuration error, and 3 after a complete run in which a process failed, never ran or left a (workload, variant) without samples; the lines are printed to stderr.
