# minimal-unity

A tiny project for trying Unity Bench. It has four assembly definitions so script-edit workloads have something to show:

- `UnityBench.Example.Core` (`Assets/Core/Work.cs`): referenced by everything else. Editing it shows the cost of changing something widely used.
- `UnityBench.Example.FeatureA` (`Assets/FeatureA/FeatureA.cs`): a leaf; nothing references it.
- `UnityBench.Example.FeatureB`: another dependent of Core.
- `UnityBench.Example.Editor` (`Assets/Editor/HashAdapter.cs`): an `editor.adapter` workload. It times a deterministic integer computation and a loop of SHA-256 hashes and reports an output hash and work counters for each.

Run it through the campaign in `../minimal.toml`, which copies this project per Editor and adds the Unity Bench package to the copy, so this directory is never modified. To open it yourself in the Editor, add `packages/com.tesselworks.unity-bench` from disk in the Package Manager first.
