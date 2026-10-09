---
type: guide
date: 2026-10-09
tags: [unity, benchmark, quickstart]
---

# Quickstart

This is a development framework, not a published package. The commands below run from the repository root with Python 3.10 or newer. Unity execution additionally requires an already installed exact Editor version and a valid license. The runner does not install or license Unity for you.

`run` uses process groups on macOS/Linux and owned Job Objects on Windows 10/Server 2016 or newer. Native cleanup and actual Unity integration require Windows verification; portable tests alone cannot establish that. On POSIX, wrappers and children must remain in the owned session; adapters must not detach services into new sessions.

## Try the portable contract example

The `examples/portable-contract` adapter exercises request identity, result validation and reporting without Unity. It performs a small portable workload; its timings do not establish anything about Unity versions. Follow [that example's instructions](../examples/portable-contract/README.md) to generate a configuration for your checkout, then inspect it before execution:

```sh
python3 unity-bench.py plan /absolute/path/to/config.json
python3 unity-bench.py run /absolute/path/to/config.json --out /absolute/path/to/new-campaign
python3 unity-bench.py report /absolute/path/to/new-campaign
```

Choose a new output directory for each campaign. Retained requests, results and reports are evidence, and overwrite refusal is intentional. Keep campaign directories outside tracked source. The run command creates `report.json` and `public-report.json`. The report command prints a fresh summary from retained evidence; use `--out /absolute/path/to/new-report.json` to save it without overwriting an earlier report. Inspect even the public report before sharing it.

## Connect your project

Create a JSON configuration with an exact source commit and installed Editors. Replace the example paths, commit and versions with verified values. The adapter executable must exist in the target commit and implement the [request/result contract](architecture.md).

```json
{
  "schemaVersion": 1,
  "name": "my-project-engine-comparison",
  "repository": "/absolute/path/to/my-project",
  "variants": [
    {
      "id": "base",
      "revision": "0123456789abcdef0123456789abcdef01234567",
      "unityVersion": "6000.7.0b2",
      "backend": "mono",
      "unityExecutable": "/absolute/path/to/base/Unity"
    },
    {
      "id": "candidate",
      "revision": "0123456789abcdef0123456789abcdef01234567",
      "unityVersion": "7000.0.0a7",
      "backend": "coreclr",
      "unityExecutable": "/absolute/path/to/candidate/Unity"
    }
  ],
  "axes": [{"id": "editor-version", "left": "base", "right": "candidate"}],
  "workloads": [
    {
      "id": "project-managed-operation",
      "projectPath": "unity/MyProject",
      "mode": "samples",
      "command": [
        "python3", "{checkout}/tools/my-benchmark-adapter.py",
        "--unity", "{unity}", "--project", "{project}",
        "--request", "{request}", "--result", "{result}"
      ],
      "timeoutSeconds": 300,
      "warmIterations": 3
    }
  ],
  "repetitions": 3,
  "calibrationRepetitions": 3
}
```

This is an integration template, not a bundled `my-benchmark-adapter.py`. Editor runtime names and player backend names must match the actual process; do not label an Editor as IL2CPP simply because a player will later be built with that backend.

`prepare`, when needed, is a list of argument arrays on the workload. It prepares the checkout before measured execution, for example through a project-owned importer/build wrapper. Neither `prepare` nor `command` is a shell string. Supported substitutions are `{checkout}`, `{project}`, `{unity}`, `{request}`, `{result}`, `{variant}`, `{workload}`, `{unityVersion}`, `{backend}` and `{revision}`. Use explicit argument tokens; shell redirection, pipelines and command substitution are not executed.

Run `plan` first. Review identities, workload boundaries and the entire schedule, then run into a new campaign directory. Review the full report's refusal reasons before reading timing summaries. Without an independent host observer, reports remain diagnostic and produce no accepted speed ratios.

On Windows, opt in using `"host": {"mode": "windows-shared-host", "foreignCpuReferencePercent": 3, "backgroundCpuLimitPercent": 0.5}` and provide each variant's absolute `editorExecutable` for the actual installed Editor. `unityExecutable` may identify the official Unity CLI used by your wrapper. Controlled runs require the adapter to implement the four lifecycle signals documented in [architecture](architecture.md); the bundled Unity entry already implements them. Prepare/import each variant before the fixed measured schedule. Preserve all snapshots and signals for offline replay. This is an exploratory shared-host profile, with strict observation/ownership/background refusals and retained foreign CPU excursions.

## Unity package and independent sample

The generic Unity package exposes `UnityBench.Editor.BatchEntry.Run` for opt-in batch execution and a versioned result stream. Use the package's own documentation for its exact command line and sample entry point. The independent Unity sample is a small CPU workload for validating integration; it is not a representative game, physics or rendering comparison.

For that bundled sample, use the Windows controlled host mode described above, this repository as `repository`, an exact commit of this repository, `projectPath: "examples/minimal-unity"`, workload ID `example.synthetic-fixed-work`, and this command array:

```json
[
  "{unity}", "-batchmode", "-projectPath", "{project}",
  "-executeMethod", "UnityBench.Editor.BatchEntry.Run",
  "-unityBenchRequest", "{request}"
]
```

Do not pass `-quit`; the batch entry owns exit after writing the result and completing its mandatory measurement lifecycle. The package is referenced locally by the sample. Preserve that relative package layout if extracting the framework into its own repository. Merely changing its ProjectVersion pin is not a compatibility test: select an actual installed Editor and inspect its detected version/backend result. A separately flagged `preparationOnly` invocation primes the Library and validates work without emitting timed samples; its receipt cannot count as a measured process.

Set `unityExecutable` to the verified Editor binary for this async entry. The official Unity CLI remains useful for installed-version and license prerequisites, but its `unity run` command manages `-quit` itself and is unsuitable for this update-driven lifecycle. Use the Editor's official batch command line shown above inside the runner's owned process lifetime. See [the official CLI's batch behavior](https://github.com/Unity-Technologies/skills/blob/main/skills/unity-cli/references/build-run-test.md).

An adapter must distinguish compilation/preparation from the measured process. Before using results in an article, verify actual Unity compilation and execution, expected sample counts, request matching, real detected runtime/backend, performed work and input/output parity. A successful portable contract test does not verify those Unity behaviors.

## Development checks

Run `python3 -I -m unittest discover -s tests` from this repository root. These tests create a real separate Git repository and execute fresh adapter processes. Native WinAPI observations are mocked outside Windows; Unity compilation still needs actual Editor verification.
