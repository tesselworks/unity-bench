# Unity Bench package

Editor-only batch entry for [Unity Bench](../../README.md). The runner copies it into each variant's project copy; you can also add it to a project yourself (Package Manager, "Add package from disk") when writing an adapter.

- `UnityBench.Editor.BatchEntry.Run` is the `-executeMethod` entry. It reads a request file (`-unityBenchRequest`), runs one workload (`editor.script_edit`, `editor.play`, `editor.adapter` or `player.build_time`), and writes a result file (schema in [docs/adapters.md](../../docs/adapters.md)). It exits the Editor itself.
- `UnityBench.Editor.AdapterContext` and `WorkResult` are the API for project adapters.
- Session state is kept in `SessionState`, so a workload survives the script reloads it causes. The source compiles unchanged on Unity 6.7 (Mono) and Unity 7 (CoreCLR).
- `PlayerProbe~/` is not compiled with the package (the `~` hides it from Unity). The runner copies it to `Assets/` for `player.startup`. It does nothing unless the player is launched with `-unityBenchSignal`.

Nothing here runs unless the Editor is started with `-executeMethod UnityBench.Editor.BatchEntry.Run`.

MIT licensed.
