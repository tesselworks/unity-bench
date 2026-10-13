# Built-in workloads

All built-in workloads work on any Unity project. Editor workloads run `Unity -batchmode -nographics`. Those that need to act inside the Editor use the batch entry in `packages/com.tesselworks.unity-bench`, which the runner copies into each variant's project copy (`project.inject_package`, default on; a copy of the package the project already embeds is replaced, so the C# that runs belongs to this runner; the framework's copy comes from the installed wheel or from `packages/` in a source checkout). The package compiles unchanged on Unity 6.7 (Mono) and Unity 7 (CoreCLR); its `Runtime/` part is inert unless a player is started with the arguments below.

Common workload keys: `id`, `kind`, `repeats` (fresh processes per variant), `rounds` (iterations inside one Editor or player process), `timeout_seconds`, `variants` (restrict to some variant ids), `headline` (`true`, or a list of item-name patterns such as `["leaf/*", "play_enter"]`: the items an aggregate may include; see [methodology](methodology.md#aggregates-are-for-headline-items-only)). Defaults come from `[defaults]`. `repeats` is raised to the next even number when a workload runs on more than two variants (balanced order, see [methodology](methodology.md#schedule)).

| Kind | Clock | Items reported | Notes |
| --- | --- | --- | --- |
| `editor.cold_open` | whole Editor process | `open` | deletes `Library`, opens, quits. Repeat 0 is first-use |
| `editor.warm_open` | whole Editor process | `open` | opens with the existing `Library`, quits. Repeat 0 is first-use |
| `editor.script_edit` | stopwatch in the Editor | `<target>/compile`, `<target>/compile+reload` | edits a script, times compile and compile+reload+idle |
| `editor.play` | stopwatch in the Editor | `play_enter`, `play_exit` | Play Mode transitions |
| `editor.adapter` | the adapter's own | the adapter's | see [adapters.md](adapters.md) |
| `player.build_time` | stopwatch around `BuildPipeline.BuildPlayer` | `build` | clean or incremental |
| `player.startup` | file timestamp from outside the player | `launch_to_probe`, `engine_to_probe`, `launch_to_exit` | launches a built player |
| `player.adapter` | the adapter's own | the adapter's | see [adapters.md](adapters.md#player-adapter-playeradapter) |

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

The values are looked up by name on the Editor (the enum names differ between Unity 6.7 and 7), so a value an Editor does not have fails that build with the list of valid names, instead of building with something else. Leave a key out to keep the project's own setting.

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

## Preparing the project copy

Some projects need a step before Unity can open them (stage a plugin, generate data, copy an adapter in). `[project] prepare` lists commands, each a list of strings, run once per variant in that variant's copy before the schedule starts. The working directory is the copy's root (the git worktree for variants with a `revision`, otherwise the copied project). Placeholders: `{python}` (the interpreter running Unity Bench), `{root}`, `{project}`, `{campaign}` (the campaign file's directory) and `{variant}`. A failing command stops the run before any measurement; its output is in `logs/prepare-<variant>-<n>.log`. A plain copy of the project (no `revision`) leaves behind `file:` package dependencies that point outside the project; the run records a warning for each in `campaign.json` and the report.

```toml
[project]
path = "../unity/MyProject"
prepare = [["{python}", "{campaign}/prepare.py", "--root", "{root}", "--project", "{project}"]]
```

Variants that no scheduled workload uses get no copy. `unity-bench run --work <dir>` puts the copies elsewhere than `<run dir>/work`; on Windows a short path keeps Library paths under the 260-character limit.

## Output files

A run directory contains `campaign.json` (resolved campaign, schedule and its notes, host, provenance: framework version and commit, and per variant the resolved project commit), `results.jsonl` (raw; one row per process, sample and reload group; a process row carries its environment, the player's `build` settings, adapter-declared `headline` patterns, the `cleanup` record of what was terminated and what was left running, and the host's one-second CPU/memory series), `summary.md` and `summary.json` (regenerated by `unity-bench report`), and `logs/`, `requests/`, `results/` for every process. `--keep-work` keeps the project copies and builds, which can be large.

`unity-bench run` exits 0 when every scheduled process succeeded and produced samples, 2 on a usage or configuration error, and 3 after a complete run in which a process failed, never ran or left a (workload, variant) without samples; the lines are printed to stderr.
