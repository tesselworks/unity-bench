# Unity Bench

Compare the same Unity project across Editor versions or player backends, using a fixed execution plan and project-defined workloads.

Unity Bench separates campaign orchestration from the code that performs and validates your project's work. A Python runner prepares isolated Git checkouts, launches adapters without a shell, retains per-process evidence and produces a descriptive report. A Unity package supplies an opt-in batch entry point and a result contract. Your adapter defines project preparation, actual workloads, and what makes two results equivalent.

This is the independent private `tesselworks/unity-bench` development repository. It contains the framework source, Unity package and independent examples, with a fresh Git history and no consumer project assets or measurement archives. It is not a public release; no public license has been selected. Do not assume an open-source license grant.

## Start here

- [Quickstart](docs/quickstart.md): run the portable example and connect a Unity project.
- [Architecture and adapter contract](docs/architecture.md): who prepares, measures and verifies each run.
- [Measurement methodology](docs/methodology.md): clock boundaries, repeated processes, parity and report limits.
- [Project adapter boundary](docs/adapters.md): what belongs in a consumer repository and how it connects to the framework.
- [Public release plan](docs/public-repository.md): packaging, licensing, privacy and release requirements.

## What the framework compares

A campaign identifies exact variants: revision, Unity version and backend. Each workload runs a predetermined schedule, such as A/B, B/A, A/B, in fresh processes. Optional same-variant calibration is separate from those comparisons. Outputs live in a fresh campaign directory; completed evidence is not overwritten.

The report uses each process's samples to form a process-level summary, then shows repeated observations and their spread. A result with missing evidence or different input, output, settings or work counters is retained for diagnosis. Windows campaigns can opt into an independent CPU/PID observer with a bounded prepare/start/finish/release protocol. Only complete, replayable eligible evidence permits exploratory ratios. Campaigns without that observer retain descriptive distributions and no adopted speed ratios. Successful process execution alone is not a performance verdict.

There is no universal Unity workload. The framework cannot infer equivalent gameplay from two matching scene names, force older Unity versions to compile newer project source, or turn a few repetitions into statistical significance. The exact preparation and measurement boundaries are part of the experiment.

## Development scope

The initial implementation focuses on a standalone runner, versioned request/result evidence, descriptive reporting, a Unity adapter boundary and a small independent sample. The portable sample exercises the orchestration and validation contract; it is not a Unity performance measurement. Read the quickstart and package documentation for the currently available commands.

Production adapters for cold import, script reload, Play entry and project-specific player scenes need actual Unity execution and evidence review before being presented as supported measurements. Public release, published packages and a blog-ready cross-version comparison are subsequent deliverables, not implied by this private repository existing.

Standalone campaign execution uses owned process groups on macOS/Linux and atomically assigned Job Objects on Windows 10/Server 2016 or newer. Native Windows ownership, cleanup and image-path evidence have been verified in Windows CI. The independent Unity sample has completed the full preparation, measurement, host-validation and report-replay pipeline with Unity 6000.7.0b2 (Mono) and 7000.0.0a7 (CoreCLR) on Windows. This supports the bundled synthetic Editor integration; it does not establish other Editor versions, actual macOS/Linux Unity execution, production Editor workloads or player measurements. Existing project-specific Windows campaigns retain their measurement policies.

## Local development

From the repository root, run `python3 -I -m unittest discover -s tests` and `python3 unity-bench.py --help`. To install the Python CLI from source, run `python3 -m pip install .` in your own virtual environment, then `unity-bench --help`. The Unity package and examples are source directories in this repository; installing the Python package does not install a Unity Editor or copy the Unity package into your project.

The contract CI runs on Linux and Windows without private project dependencies. It verifies the Python protocol and OS process ownership tests; it does not install or execute Unity. The previously verified Windows Unity integration used the identical extracted measurement implementation before repository separation. Running this repository revision through native Unity is a separate validation step; this extraction does not provide new engine performance measurements.
