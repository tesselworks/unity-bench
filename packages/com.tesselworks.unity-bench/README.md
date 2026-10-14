# Unity Bench package

Batch entry and adapter API for [Unity Bench](../../README.md). The runner copies it into each variant's project copy; you can also add it to a project yourself (Package Manager, "Add package from disk") when writing an adapter.

- `UnityBench.Editor.BatchEntry.Run` is the `-executeMethod` entry. It reads a request file (`-unityBenchRequest`), runs one workload (`editor.script_edit`, `editor.reload_leak`, `editor.play`, `editor.play_leak`, `editor.adapter` or `player.build_time`), and writes a result file (schema in [docs/adapters.md](../../docs/adapters.md)). It exits the Editor itself.
- `UnityBench.Editor.AdapterContext` and `WorkResult` are the API for project adapters.
- Session state is kept in `SessionState`, so a workload survives the script reloads it causes. The source compiles unchanged on Unity 6.7 (Mono) and Unity 7 (CoreCLR).
- `Runtime/` (assembly `UnityBench.Player`) is compiled into player builds. `PlayerContext` and `WorkResult` are the API for `player.adapter`; `AdapterRunner` runs the adapter (a method or a coroutine) and writes the result file; `UnityBenchPlayerProbe` backs `player.startup`. `SceneWorkload` is the built-in entry point of `player.scene` and `editor.scene_play` (frames, `ProfilerRecorder` markers, GC and memory of any scene, no project code), and `Bench` is the optional static API for game code (`Mark`, `Hash`, `Begin`, `End`; no-ops outside Unity Bench). `MemoryProbe` reads the process's memory counters (the `ProfilerRecorder` Memory category, the Profiler API, the .NET GC) for the Editor workloads and the scene workloads. It does nothing unless the player is launched with `-unityBenchAdapter` or `-unityBenchSignal`.

Nothing here runs unless the Editor is started with `-executeMethod UnityBench.Editor.BatchEntry.Run`.

MIT licensed.
