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
| `Measure(name, Func<WorkResult>)` | times the call, records one sample. The first call per `name` is `first-use`, later ones `warm` |
| `Add(name, ms, work, phase)` | records a sample you timed yourself |
| `AddDistribution(name, phase, values, work)` | many per-frame times for one phase; see [Distributions](#distributions) |
| `Note(key, value)` | free-form annotation for the whole process (raw results only) |
| `WorkResult(hash)`, `.Count(name, value)` | output hash and work counters of one sample. `WorkResult.Sha256Hex(...)` is a helper |

Rules for adapters:

- The adapter runs once per Editor process, after the Editor has been set up (optional code optimization applied, Editor idle). It must be synchronous. A script reload during the adapter loses the process state, so adapters do not edit scripts.
- Use one **item name per unit of work** (a scenario, an operation) and keep names stable between variants. The report lines items up by name.
- Report a hash or counters when two variants *should* do identical work. The report flags differences. When you do not, the item is labelled "not reported".
- Throwing an exception fails the process. The exception text is stored in the process record.
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

Per variant the runner builds the player once and launches `repeats` fresh processes with `-unityBenchAdapter <Type.Method> -unityBenchOut <result.json> -unityBenchArgs <request.json>`. The request file holds `workload`, `variant`, `rounds` and `args` (also accepted: a JSON object given directly after `-unityBenchArgs`). The adapter may run over many frames: an `IEnumerator` is pumped like a coroutine (yield `null`, `WaitForSeconds`, or another `IEnumerator`). When it returns, the runtime writes the result file and quits the player. An exception makes the result `completed: false` and the process a failure; a player that dies writes no result and is recorded with its exit code.

Builds are shared. A build is identified, per variant, by backend, scene list, `development`, `build_target` and `prebuild`. The first `player.adapter` or `player.startup` workload that needs one builds it (recorded as a `setup-build` process, not part of any summary); later workloads with the same settings run the same build, and so does a `player.build_time` workload with those settings that ran before them. `build = "<player.build_time workload>"` picks that workload's player explicitly.

`PlayerContext`:

| Member | Meaning |
| --- | --- |
| `Rounds`, `Variant`, `Workload` | from the campaign |
| `Arg(key, fallback)` | a value from the workload's `args` table, as a string |
| `Measure(name, Func<WorkResult>)` | times the call, records one sample. The first call per `name` is `first-use`, later ones `warm` |
| `Add(name, ms, work, phase)` | records a sample you timed yourself |
| `AddDistribution(name, phase, values, work)` | many per-frame times for one phase, see below |
| `RecordFrames(name, phase)` | records the real time between frames until `.Stop()`, then reports it as a distribution |
| `Note(key, value)` | free-form annotation for the whole process (raw results only) |
| `WorkResult(hash)`, `.Count(name, value)` | output hash and work counters, as for Editor adapters |

An adapter that wraps a tool which runs by itself (a bench that already exists in the project) can start it with `yield return tool.Run()`, read the tool's own report afterwards and turn it into samples and distributions. The runtime keeps pumping until the adapter's enumerator ends, so a `Application.Quit` the tool calls at its end does not lose the result.

### Distributions

A soak or a long Play Mode run has thousands of frame times, and a median hides the stalls. An adapter reports them as one **distribution** per phase instead of one sample each (`AddDistribution` and `RecordFrames` in the player, `AddDistribution` in `AdapterContext` for Editor adapters). The runner reduces each distribution, per process, to four samples named `<name> <phase> p50`, `p95`, `p99` and `max` (nearest-rank percentiles), and the summary does the usual: median across processes of those values, range, ratio. Use different phase names for different periods, `warmup` and `steady` for example, and they appear as separate items, so a slow start is visible instead of averaged away. The raw frame times stay in the process's file under `results/`. A distribution's `hash` and `counters` are attached to all four of its items, so work that differs excludes all four.

## The result file

The runner passes a request file (`-unityBenchRequest`, written by the runner, read by the package) and the package writes a result file. You normally do not write this by hand; this is the schema, for anyone implementing an adapter in another language or runtime.

```json
{
  "schema": 1,
  "completed": true,
  "error": null,
  "environment": {
    "unityVersion": "7000.0.0a7",
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
      "info": { "reloaded": false }
    }
  ],
  "distributions": [
    { "name": "frame", "phase": "steady", "values": [16.4, 16.9, 17.1], "hash": null, "counters": { "frames": 3 } }
  ],
  "notes": { }
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `schema` | `1` | required |
| `completed` | bool | required; `false` makes the process a failure and `error` is shown |
| `error` | string or null | first line is kept in the process record |
| `environment` | object | free-form strings; the report shows `unityVersion`, `runtime`, `codeOptimization` |
| `samples[].name` | string | the item identity inside the workload |
| `samples[].phase` | `"first-use"` or `"warm"` | first-use samples are excluded from the steady-state summary by default |
| `samples[].ms` | number >= 0, finite | the metric |
| `samples[].round` | int | informational; reload log blocks are matched to steps by the log markers below |
| `samples[].hash` | string or null | output hash; compared across variants and repeats |
| `samples[].counters` | object of numbers | work counters; compared like the hash |
| `samples[].info` | object | annotations (assemblies compiled, reloaded...). Never compared |
| `distributions` | list, optional | `name`, `phase` (any label), `values` (list of numbers >= 0, in ms), optional `hash` and `counters`. Expanded to four samples as described above |
| `notes` | object | process-level annotations |

`hash` and `counters` mean *identity*: the same work should produce the same values. `info` is for anything that is allowed to differ.

A result with an unknown schema, `completed: false`, no samples (and no distributions) or an invalid sample or distribution fails the process; nothing is guessed.

## Log markers (optional)

Built-in script-edit and Play Mode workloads print `[UnityBench] begin <step> <round>` and `[UnityBench] end <step> <round>` lines to the Editor log. The runner parses `Domain Reload Profiling` and `Asset Pipeline Refresh` blocks from that log ([workloads.md](workloads.md)) and attributes each block to the step whose markers enclose it. An adapter that triggers reloads in a way the package cannot see can print the same two lines itself to get the same attribution.
