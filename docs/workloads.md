# Built-in workloads

All built-in workloads work on any Unity project. Editor workloads run `Unity -batchmode -nographics`. Those that need to act inside the Editor use the batch entry in `packages/com.tesselworks.unity-bench`, which the runner copies into each variant's project copy (`project.inject_package`, default on). The package is Editor-only and compiles unchanged on Unity 6.7 (Mono) and Unity 7 (CoreCLR).

Common workload keys: `id`, `kind`, `repeats` (fresh processes per variant), `rounds` (iterations inside one Editor process), `timeout_seconds`, `variants` (restrict to some variant ids). Defaults come from `[defaults]`.

| Kind | Clock | Items reported | Notes |
| --- | --- | --- | --- |
| `editor.cold_open` | whole Editor process | `open` | deletes `Library`, opens, quits. Repeat 0 is first-use |
| `editor.warm_open` | whole Editor process | `open` | opens with the existing `Library`, quits. Repeat 0 is first-use |
| `editor.script_edit` | stopwatch in the Editor | `<target>/compile`, `<target>/compile+reload` | edits a script, times compile and compile+reload+idle |
| `editor.play` | stopwatch in the Editor | `play_enter`, `play_exit` | Play Mode transitions |
| `editor.adapter` | the adapter's own | the adapter's | see [adapters.md](adapters.md) |
| `player.build_time` | stopwatch around `BuildPipeline.BuildPlayer` | `build` | clean or incremental |
| `player.startup` | file timestamp from outside the player | `first_frame`, `first_frame_in_player`, `process_total` | launches a built player |

## editor.cold_open and editor.warm_open

The time is the wall clock of the whole Editor process: launch, open, import whatever is out of date, quit. The first open of a variant's project copy is not representative: it imports everything and, on a newer Editor, migrates the project. It is labelled first-use. `repeats` includes that first one, so ask for one more than the number of samples you want.

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
scene = "Assets/Scenes/Main.unity"   # optional; default is the generated empty scene
```

Times `BuildPipeline.BuildPlayer` with a stopwatch, for the host platform's standalone target (`build_target` overrides). `clean` deletes the output and passes `BuildOptions.CleanBuildCache` every round. `incremental` does a full build in round 0 (first-use) and then rebuilds the same output with nothing changed. An unavailable backend fails the process with a clear error (CoreCLR exists only in Unity 7; a missing platform module also fails). Unity Bench never installs modules. The report also stores the build report's own total time and output size.

## player.startup

```toml
[[workloads]]
id = "startup"
kind = "player.startup"
build = "build"        # use the player built by that player.build_time workload
repeats = 5
headless = true        # -batchmode -nographics; false opens a 640x360 window
```

Launches the built player with `-unityBenchSignal <file>`. A small probe script (`PlayerProbe~/` in the package, copied into the project copy's `Assets/` for campaigns that use `player.*`) writes the file after the first full frame and quits. The runner takes the file's modification time minus the time just before launch as `first_frame`, so the clock does not depend on how often the runner polls. `first_frame_in_player` is `Time.realtimeSinceStartup` inside the player at that moment (it starts when the engine starts, so it excludes process spawn). `process_total` includes quitting. Repeat 0 is first-use: the first launch of a fresh build pays OS caches and code signing checks.

Without `build`, the runner builds the player once per variant before the first launch; that build is stored as a process record with `role: setup-build` and is not part of any summary.

## Output files

A run directory contains `campaign.json` (resolved campaign, schedule, host), `results.jsonl` (raw; one row per process, sample and reload group), `summary.md` and `summary.json` (regenerated by `unity-bench report`), and `logs/`, `requests/`, `results/` for every process. `--keep-work` keeps the project copies and builds, which can be large.
