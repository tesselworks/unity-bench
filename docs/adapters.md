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
| `Note(key, value)` | free-form annotation for the whole process (raw results only) |
| `WorkResult(hash)`, `.Count(name, value)` | output hash and work counters of one sample. `WorkResult.Sha256Hex(...)` is a helper |

Rules for adapters:

- The adapter runs once per Editor process, after the Editor has been set up (optional code optimization applied, Editor idle). It must be synchronous. A script reload during the adapter loses the process state, so adapters do not edit scripts.
- Use one **item name per unit of work** (a scenario, an operation) and keep names stable between variants. The report lines items up by name.
- Report a hash or counters when two variants *should* do identical work. The report flags differences. When you do not, the item is labelled "not reported".
- Throwing an exception fails the process. The exception text is stored in the process record.
- The source must compile unchanged on every Editor in the campaign. Unity 7 (CoreCLR) needs classes that carry lifecycle attributes to be `partial`, and `JsonUtility` turns null strings into `""`; the package already handles both.

The example in `examples/minimal-unity/Assets/Editor/HashAdapter.cs` is a complete adapter.

## Player adapter

A player adapter is a C# entry point inside a built player that writes the same result file. The runner side for it is **not in v0.1**. The result file below is the contract it will use; the built-in `player.startup` already launches built players with `-unityBenchSignal <path>` and reads a small signal file.

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
| `notes` | object | process-level annotations |

`hash` and `counters` mean *identity*: the same work should produce the same values. `info` is for anything that is allowed to differ.

A result with an unknown schema, `completed: false`, no samples or an invalid sample fails the process; nothing is guessed.

## Log markers (optional)

Built-in script-edit and Play Mode workloads print `[UnityBench] begin <step> <round>` and `[UnityBench] end <step> <round>` lines to the Editor log. The runner parses `Domain Reload Profiling` and `Asset Pipeline Refresh` blocks from that log ([workloads.md](workloads.md)) and attributes each block to the step whose markers enclose it. An adapter that triggers reloads in a way the package cannot see can print the same two lines itself to get the same attribution.
