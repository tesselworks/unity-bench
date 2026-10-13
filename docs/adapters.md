# Project adapters and the result schema

A **workload** is something a campaign measures. Built-in workloads ([workloads.md](workloads.md)) work on any project. A project adds its own with an **adapter**: a C# entry point that does the project's real work and reports samples, work counters and an output hash. The runner invokes it, collects the result file and does the statistics.

## Editor adapter (`editor.adapter`)

Add the package `packages/com.tesselworks.unity-bench` to the project (the runner injects it into its project copies by default, `project.inject_package = true`), reference the assembly `UnityBench.Editor` from an Editor assembly definition, and write:

```csharp
using UnityBench.Editor;

public static class MyAdapter
{
    public static void Run(AdapterContext ctx)
    {
        for (var round = 0; round < ctx.Rounds; round++)
            ctx.Measure("hash/sha256", () =>
            {
                var output = DoTheWork();                       // only this call is inside the stopwatch
                return new WorkResult(WorkResult.Sha256Hex(output)).Count("bytes", output.Length);
            });
    }
}
```

and in the campaign:

```toml
[[workloads]]
id = "my-work"
kind = "editor.adapter"
method = "MyNamespace.MyAdapter.Run"   # public or non-public static, one AdapterContext parameter
args = { size = 4096 }                   # optional; read with ctx.Arg("size")
rounds = 5
```

`AdapterContext`:

| Member | Meaning |
| --- | --- |
| `Rounds` | the `rounds` value; the adapter decides what a round is |
| `Arg(key, fallback)` | a value from the workload's `args` table, as a string |
| `Measure(name, Func<WorkResult>, workGroup)` | times the call, records one sample. Untagged: the runner derives the phase ([Phases](#phases)) |
| `Add(name, ms, work, phase, workGroup)` | records a sample you timed yourself; `phase` tags it explicitly |
| `AddDistribution(name, phase, values, work, workGroup)` | many per-frame times for one phase; see [Distributions](#distributions) |
| `DefineWorkGroup(name, work)` | a work identity several samples share; see [Work groups](#work-groups) |
| `Headline(patterns)` | declares which items are this workload's headline items; see [Headline items](#headline-items) |
| `Note(key, value)` | free-form annotation for the whole process (raw results only) |
| `WorkResult(hash)`, `.Count(name, value)` | output hash and work counters of one sample. `WorkResult.Sha256Hex(...)` is a helper |

Rules for adapters:

- The adapter runs once per Editor process, after the Editor has been set up (optional code optimization applied, Editor idle). It must be synchronous. A script reload during the adapter loses the process state, so adapters do not edit scripts.
- Use one **item name per unit of work** (a scenario, an operation) and keep names stable between variants. The report lines items up by name.
- Report a hash or counters when two variants *should* do identical work. The report flags differences. When you do not, the item is labelled "not reported". When only some samples report (one variant, some rounds) the item is `partial`, not `same`.
- Throwing an exception fails the process. The exception text is stored in the process record. So does a malformed result (see [The result file](#the-result-file)): the schedule continues.
- The source must compile unchanged on every Editor in the campaign. Unity 7 (CoreCLR) needs classes that carry lifecycle attributes to be `partial`, and `JsonUtility` turns null strings into `""`; the package already handles both.

The example in `examples/minimal-unity/Assets/Editor/HashAdapter.cs` is a complete adapter.

## Player adapter (`player.adapter`)

A player adapter is a C# entry point inside a built player. The package's `Runtime/` folder (assembly `UnityBench.Player`) is compiled into the player by the project that has the package; it is inert unless the player is started with the arguments below, so a normal build of your game does not change behaviour. Reference `UnityBench.Player` from your adapter's assembly definition (a runtime assembly, not Editor-only) and write:

```csharp
using System.Collections;
using UnityBench.Player;
using UnityEngine.Scripting;

[Preserve]                                   // keeps the type and method when IL2CPP strips the player
public static class MyPlayerAdapter
{
    [Preserve]
    public static IEnumerator Run(PlayerContext ctx)     // or: public static void Run(PlayerContext ctx)
    {
        var recorder = ctx.RecordFrames("frame", "warmup");
        for (var i = 0; i < 600; i++)
        {
            if (i == 60) { recorder.Stop(); recorder = ctx.RecordFrames("frame", "steady"); }
            DoOneFrameOfWork();
            yield return null;
        }
        recorder.Stop();
        ctx.Measure("hash/sha256", () => new WorkResult(Hash()).Count("bytes", 1024));
    }
}
```

and in the campaign:

```toml
[[workloads]]
id = "my-frames"
kind = "player.adapter"
method = "MyNamespace.MyPlayerAdapter.Run"
args = { frames = 600 }
scenes = ["Assets/Scenes/Main.unity"]   # optional; default is a generated empty scene
backend = "il2cpp"                       # optional; default is the variant's backend
prebuild = "MyNamespace.MyEditor.PrepareBuild"   # optional static void(); runs in the Editor before each build, untimed
headless = false                         # default for adapters: a window (window = "1280x800"); true = -batchmode -nographics
player_args = ["-force-d3d11"]           # optional extra player arguments
rounds = 3
repeats = 5                              # fresh player processes per variant
timeout_seconds = 1800
```

A player adapter that runs its work once per process (the usual shape for a long scenario) simply reports one sample per name; `rounds` is only what the campaign passes in, and the report states how many samples per item per process ran. Do not tag such samples `warm`: nothing was repeated. Leave them untagged and they are `single`, or tag them `first-use` if you want them kept out of the steady-state table. For code paths that JIT, a spawn or first call measured once is a first-use result; run the scenario twice in the process if you want a warm one next to it.

Per variant the runner builds the player once and launches `repeats` fresh processes with `-unityBenchAdapter <Type.Method> -unityBenchOut <result.json> -unityBenchArgs <request.json>`. The request file holds `workload`, `variant`, `rounds` and `args` (also accepted: a JSON object given directly after `-unityBenchArgs`). The adapter may run over many frames: an `IEnumerator` is pumped like a coroutine (yield `null`, `WaitForSeconds`, or another `IEnumerator`). When it returns, the runtime writes the result file and quits the player. An exception makes the result `completed: false` and the process a failure; a player that dies writes no result and is recorded with its exit code.

Builds are shared. A build is identified, per variant, by backend, scene list, `development`, `build_target` and `prebuild`. The first `player.adapter` or `player.startup` workload that needs one builds it (recorded as a `setup-build` process, not part of any summary); later workloads with the same settings run the same build, and so does a `player.build_time` workload with those settings that ran before them. `build = "<player.build_time workload>"` picks that workload's player explicitly.

`PlayerContext`:

| Member | Meaning |
| --- | --- |
| `Rounds`, `Variant`, `Workload` | from the campaign |
| `Arg(key, fallback)` | a value from the workload's `args` table, as a string |
| `Measure(name, Func<WorkResult>, workGroup)` | times the call, records one sample; untagged, so the runner derives the phase |
| `Add(name, ms, work, phase, workGroup)` | records a sample you timed yourself |
| `AddDistribution(name, phase, values, work, workGroup)` | many per-frame times for one phase, see below |
| `RecordFrames(name, phase, workGroup)` | records the real time between frames until `.Stop()`, then reports it as a distribution |
| `DefineWorkGroup(name, work)`, `Headline(patterns)` | as for Editor adapters |
| `Note(key, value)` | free-form annotation for the whole process (raw results only) |
| `WorkResult(hash)`, `.Count(name, value)` | output hash and work counters, as for Editor adapters |

An adapter that wraps a tool which runs by itself (a bench that already exists in the project) can start it with `yield return tool.Run()`, read the tool's own report afterwards and turn it into samples and distributions. The runtime keeps pumping until the adapter's enumerator ends, so a `Application.Quit` the tool calls at its end does not lose the result.

### Phases

A sample's `phase` is `first-use`, `warm`, `warmup` or `steady`, or absent. Absent (an untagged `Measure`/`Add`) is classified by the runner from how often the name occurs in that process: the first of several is `first-use`, the later ones `warm`, and a name that occurs once is `single`. First-use samples are excluded from the steady-state tables by default and shown in the first-use table; every other phase is summarised. A tagged sample is taken at its word, so tag `warm` only when something really ran before it.

### Work groups

Metrics that come from one run (a table run's plan time, commit time, latencies, spawn time and frame times) should be judged on that run's work, not individually. Define the run's identity once and attach it to every sample and distribution that came out of it:

```csharp
ctx.DefineWorkGroup("table", new WorkResult(runHash).Count("fractures", fractures).Count("piecesRemoved", removed));
ctx.Add("plan", planMs, workGroup: "table");
ctx.AddDistribution("frame", "steady", frames, workGroup: "table");
```

A sample or distribution with a `workGroup` and no hash or counters of its own **inherits the group's hash and counters**; one with its own keeps its own. The inherited identity is then compared like any other, so a run that did different work in two variants withholds all of its metrics' ratios together instead of only the ones that happened to carry a hash. The group name is stored with each sample.

### Headline items

`Headline("frame steady p50", "frame steady p95", "core/*")` declares, as item-name patterns (`*` and `?`), which items are the headline of this workload: the only items an aggregate may include. The campaign can name more with `headline` on the workload (see [methodology](methodology.md#aggregates-are-for-headline-items-only)). Patterns are matched against final item names, so a distribution's items are `<name> <phase> <stat>`.

### Distributions

A soak or a long Play Mode run has thousands of frame times, and a median hides the stalls. An adapter reports them as one **distribution** per phase instead of one sample each (`AddDistribution` and `RecordFrames` in the player, `AddDistribution` in `AdapterContext` for Editor adapters). The runner reduces each distribution, per process, to four samples named `<name> <phase> p50`, `p95`, `p99` and `max` (nearest-rank percentiles), and the summary does the usual: median across processes of those values, range, ratio. Use different phase names for different periods, `warmup` and `steady` for example, and they appear as separate items, so a slow start is visible instead of averaged away. The raw frame times stay in the process's file under `results/`. A distribution's `hash` and `counters` (or its work group's) are attached to all four of its items, so work that differs excludes all four. The phase label is the samples' phase when it is `first-use`, `warmup` or `steady`, and `warm` for any other label. The summary never mixes statistics: `steady p95` and `steady p50` are different families, and so are `warmup` and `steady`.

## The result file

The runner passes a request file (`-unityBenchRequest`, written by the runner, read by the package) and the package writes a result file. You normally do not write this by hand; this is the schema, for anyone implementing an adapter in another language or runtime.

```json
{
  "schema": 1,
  "completed": true,
  "error": null,
  "environment": {
    "unityVersion": "7000.0.0a7",
    "unityVersionFull": "7000.0.0a7 (abcdef123456)",
    "runtime": ".NET 10.0.10-dev (CLR 10.0.10)",
    "runtimeKind": "coreclr",
    "codeOptimization": "Debug",
    "backend": "Mono2x",
    "platform": "OSXEditor",
    "batchMode": true
  },
  "samples": [
    {
      "name": "hash/sha256",
      "phase": "warm",
      "round": 1,
      "ms": 21.4,
      "hash": "06b7bbf...",
      "counters": { "bytes": 33554432 },
      "workGroup": null,
      "info": { "reloaded": false }
    }
  ],
  "distributions": [
    { "name": "frame", "phase": "steady", "values": [16.4, 16.9, 17.1], "hash": null, "counters": { "frames": 3 }, "workGroup": "run" }
  ],
  "workGroups": { "run": { "hash": "5d41402...", "counters": { "fractures": 100 } } },
  "headline": [ "frame steady p50" ],
  "notes": { }
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `schema` | `1` | required. Additions are optional fields; the version stays 1 |
| `completed` | bool | required; `false` makes the process a failure and `error` is shown |
| `error` | string or null | first line is kept in the process record |
| `environment` | object or null | free-form strings. The report shows `unityVersion`/`unityVersionFull`, `runtime`, `runtimeKind`, `codeOptimization`, `backend`, `development`, `platform`, `graphicsDevice` |
| `samples[].name` | string | the item identity inside the workload |
| `samples[].phase` | `"first-use"`, `"warm"`, `"warmup"`, `"steady"`, or null/absent | absent: the runner derives it ([Phases](#phases)). First-use samples are excluded from the steady-state summary by default |
| `samples[].ms` | number >= 0, finite | the metric |
| `samples[].round` | int | informational; reload log blocks are matched to steps by the log markers below |
| `samples[].hash` | string or null | output hash; compared across variants and repeats |
| `samples[].counters` | object of finite numbers | work counters; compared like the hash |
| `samples[].workGroup` | string or null | a key of `workGroups`; the sample inherits its hash and counters when it has none of its own |
| `samples[].info` | object | annotations (assemblies compiled, reloaded...). Never compared |
| `distributions` | list, optional | `name`, `phase` (any label), `values` (list of numbers >= 0, in ms), optional `hash`, `counters`, `workGroup`. Expanded to four samples as described above |
| `workGroups` | object, optional | name -> `{ "hash": string or null, "counters": { name: finite number } }` |
| `headline` | list of strings, optional | item-name patterns (`*`, `?`) of the headline items |
| `notes` | object | process-level annotations (`player.build_time` adds `build.*`: the build settings it used) |

`hash` and `counters` mean *identity*: the same work should produce the same values. `info` is for anything that is allowed to differ.

A result with an unknown schema, `completed: false`, no samples (and no distributions), or anything malformed fails the process with a `ResultError` in its record: a sample that is not an object, a name that is empty, a non-finite or negative `ms`, a `hash` that is not a string, `counters` that are not a table of finite numbers (`null` and `NaN` count as malformed), an unknown `phase`, a `workGroup` that is not defined, `environment`/`notes` that are not objects. Nothing is guessed, and the rest of the schedule runs.

## Log markers (optional)

Built-in script-edit and Play Mode workloads print `[UnityBench] begin <step> <round>` and `[UnityBench] end <step> <round>` lines to the Editor log. The runner parses `Domain Reload Profiling` and `Asset Pipeline Refresh` blocks from that log ([workloads.md](workloads.md)) and attributes each block to the step whose markers enclose it. An adapter that triggers reloads in a way the package cannot see can print the same two lines itself to get the same attribution.
