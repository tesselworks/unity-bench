---
type: guide
date: 2026-10-09
tags: [unity, benchmark, framework]
---

# Independent minimal Unity consumer

This project references only the local `com.tesselworks.unity-bench` package. It has no MatterKit, Tessel Works kit, URP, native plugin, recipe or game dependency. The package reference resolves from the project's `Packages` directory; preserve this relative layout when extracting the framework repository.

The workload `example.synthetic-fixed-work` performs 16 passes over 65,536 fixed unsigned integers. Preparation and SHA-256 output proof happen outside the timed operation. Iteration zero is reported as `first`; subsequent iterations are `warm`. Every iteration performs the complete workload and must produce exactly matching fingerprints and work counters. This example exercises the protocol; its timings are not evidence of whole-Editor or game performance.

Input proof hashes the actual consumed array rather than a workload description: uint32 little-endian Count, uint32 little-endian Passes, then each actual uint32 array element in order (262,152 bytes total). Hashing occurs outside the operation stopwatch. An independently reconstructed input has SHA-256 `005083467aa8eba31f36f45c5336ad8629b692966a9ca1b99685e924f4b6d0e5`. Output proof and all three work counters retain the full workload and integer checksum golden.

Use the framework runner to prepare and invoke the project with the selected Editor. The direct entry point is `UnityBench.Editor.BatchEntry.Run`, supplied with `-batchmode` and `-unityBenchRequest /absolute/request.json`. The entry owns exit; do not supply `-quit`. A v1 request contains `schemaVersion`, `runId`, `variant`, `workload`, absolute `outputPath`, integer `warmIterations`, provenance and the mandatory lifecycle fields described below. The output path must not already exist. No request file or result belongs in this source project.

For controlled host observation, the runner supplies `sourceRevision`, `planDigest`, `readyPath`, `startPath`, `finishedPath`, `releasePath` and bounded `handshakeTimeoutSeconds`. Actual Editor compilation/import idle precedes workload preparation, then one update yield and a second idle check precede ready; measurement waits for the controller's matching start via Editor update callbacks with a single 50 ms cooperative idle sleep on each start/release wait callback; the completed receipt precedes finished; process shutdown waits for release. The framework verifies the actual process ID and request-byte digest and observes the entire process lifetime separately from operation timing.

The project version is an initial template pin. The framework must explicitly select and record the actual Editor version for each prepared variant. A version-setting change does not establish compatibility. Actual compatibility must be established by the selected revision’s native Unity CI; source and protocol checks cannot substitute for that verification.

The complete bundled sample pipeline has been verified on Windows with these exact Editors:

| Editor | Actual runtime | Verified scope |
|---|---|---|
| 6000.7.0b2 | Mono | Batch compilation, full-work preparation, synthetic operation timing and native host evidence |
| 7000.0.0a7 | CoreCLR | Batch compilation, full-work preparation, synthetic operation timing and native host evidence |

The fixed campaign and its comparison report were replayed from retained evidence. These versions establish compatibility for this independent synthetic example, rather than representative import, Play entry, physics, rendering or player performance. Other Editor versions and actual Unity execution on macOS/Linux remain unverified.

This is an independent private development repository. The package is not published, and a public repository license and release URL have not been selected.

Preparation uses `python prepare.py --project /absolute/isolated/project --unity-version 7000.0.0a7 --backend coreclr`. The helper changes only `ProjectVersion.txt`; it installs nothing, changes no global settings and does not launch Unity. The backend argument states the expected actual Editor runtime. It does not change a Player scripting-backend setting or force an incompatible Editor runtime. The actual result must independently match the requested identity.

Before controlled measurements, the runner may request an explicit unmeasured compatibility/cache prime with `preparationOnly: true`, zero warm iterations and no lifecycle paths. It waits for actual Editor compilation/import idle using a bounded update coordinator, executes the full synthetic work once and validates output, but records `prepared` status and no timing samples. The runner excludes these prime processes from measured process counts and performance claims.

The 50 ms cooperative idle hold applies only while awaiting start or release and only when actual Editor compilation/import flags are idle. It returns to the Editor between callbacks and does not change Execute, work counters, worker settings, operation timing, or host CPU acceptance thresholds. Its purpose is to avoid counting the framework’s own unmeasured batch-loop spin as workload-independent preparation load.
