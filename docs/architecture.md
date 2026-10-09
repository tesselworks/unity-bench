---
type: reference
date: 2026-10-09
tags: [unity, benchmark, architecture]
---

# Architecture and adapter contract

## Components

```text
campaign configuration
  -> Python runner: exact checkout, preparation, fixed schedule, process launch
       -> project adapter: build/import or invoke the intended Unity workload
            -> Unity package/workload: actual environment and measured samples
       <- per-run result evidence
  -> Python report: validate identities/parity/host eligibility, summarize processes
```

The framework resides outside the target checkout. Each variant pins a full Git revision, exact Unity version and backend. Project-relative paths resolve inside the isolated checkout. Unity's Library and generated outputs are variant-local. The tool does not upgrade the user's working project in place.

The revision identifies requested source; actual inputs and generated content still require adapter evidence. Unity versions may import identical source differently. Likewise, a configured version/backend is a request, not proof of the environment that performed the work.

## Configuration v1

| Field | Meaning |
|---|---|
| `schemaVersion` | Configuration schema, currently `1` |
| `name`, `repository` | Campaign name and project repository |
| `variants` | Exact `id`, 40-character `revision`, `unityVersion`, `backend`, optional absolute `unityExecutable` |
| `axes` | Named comparisons referencing `left` and `right` variant IDs |
| `workloads` | Project workload ID, repository-relative `projectPath`, `mode`, argument-array `command`, optional argument-array lists `prepare`, timeout and warm iteration count |
| `repetitions` | Predetermined fresh-process comparison repetitions |
| `calibrationRepetitions` | Separate same-variant calibration repetitions |
| `host` | Optional `mode: windows-shared-host`, foreign CPU reference 3%, fixed owned background limit 0.5%; otherwise `mode: none` |

`mode: "samples"` uses the adapter's in-process measurements. `mode: "external"` reports whole adapter invocation wall time, including process launch, startup, output and shutdown. The report labels this `whole-process-wall/process`; it is not an import-only or startup-ready metric. An external adapter may return an empty sample array but must still provide completion, actual environment and parity proof. Both modes still require a complete adapter result. A future lifecycle adapter must provide a real workload and a defined completion condition. See [methodology](methodology.md).

The runner expands only documented argv placeholders and starts subprocesses without a shell. Adapters may perform project-specific preparation, but they must not install software, alter machine-wide settings or terminate unrelated sessions implicitly.

The POSIX launcher creates an owned session, bounds waiting, and cleans up inherited-session children after timeout or wrapper completion. Adapters must not create detached services/new sessions; escaped descendants are outside this protocol. Windows launches a suspended process with atomic Job Object assignment and a restricted inherited handle list, verifies ownership, then resumes it. Both normal completion and failure clean up the entire owned job; unrelated processes are never termination targets.

## Per-run protocol

Each fresh run gets a distinct immutable request with schema version, run ID, variant identity, workload identity, result output path, warm iteration count, clock mode, requested source revision and plan digest. The plan separately fingerprints framework source files; an exact project revision alone does not identify an uncommitted measurement tool. The adapter reads this request and emits the corresponding versioned result. The Python validator and Unity package source define the exact accepted fields; adapters must preserve request identity instead of generating unrelated success records.

Controlled Windows runs add absolute `readyPath`, `startPath`, `finishedPath`, `releasePath` and a bounded handshake timeout. The actual Editor prepares work, writes ready with its PID and exact request-byte SHA-256, waits for start, measures, writes its complete result, holds at finished, and exits only after release. The controller requires complete CPU intervals inside both holds and after exit. Start/release signals bind the same run, request digest and actual PID. Result `startedUtc`/`completedUtc` locate the workload inside these boundaries; individual durations use the monotonic workload clock.

The result's evidence has four responsibilities:

1. **Execution identity:** match the exact requested run, variant and workload; detect actual Unity/runtime/backend and measured process identity.
2. **Measured work:** provide finite valid samples from the actual operation and report performed work counters.
3. **Parity:** derive input and output fingerprints from actual consumed/produced data. Source labels or arbitrary configuration strings do not establish equality.
4. **Completion and eligibility:** report a complete successful operation and any host assessment. Nonzero process exit, timeout or malformed/incomplete output remains a failure even if a file exists.

Output serialization and metadata gathering are outside the workload's in-process sample clock unless the campaign explicitly measures them. Do not use them as an engine benchmark. A project may add more provenance, including compiler/package inventories, loaded content proof, physics settings, build flags and render settings.

## Reporting boundary

The report retains per-process observations and diagnostic refusal reasons. It checks matching identities, inputs, outputs, settings and work. Eligible Windows comparisons require replayed raw host evidence for every scheduled comparison and calibration process on the same host. First-use and warm process medians remain separate; paired direction is compared with observed same-variant ranges. This is exploratory evidence, not a significance test. Missing observation or failed parity keeps a comparison diagnostic with zero adopted processes and no ratios.

During observation, `primaryProcessId` becomes known only at the validated ready endpoint. Final raw replay separately records `backgroundClassificationPrimaryPid` and `backgroundClassificationBasis: final-ready-native-identity-replay`: once the main PID, executable, native creation time and ownership are proven, every other owned `Unity.exe` at that executable is background throughout the retained lifetime. This includes import workers that exited before ready. This retrospective CPU classification does not assert that the main was observed-ready earlier.

Preparation follows the bounded settling protocol: the ready signal must report `isCompiling: false` and `isUpdating: false`, then the controller waits up to the 120-second preparation deadline for the first consecutive three seconds of complete post-ready intervals with total owned CPU at or below 0.5%. Startup and earlier busy preparation intervals stay in raw evidence as `unmeasured-preparation` and `preparationCpuAnnotation`; they are not timed observations. `settledBeforeStartUtc` identifies the first qualifying run and is independently rederived. Background CPU above 0.5% remains a hard refusal from that settled-before window through measurement, completion hold and final after, except for the validated completed-hold-to-quit bridge described below. Unknown identities, competing blockers, clock gaps and malformed evidence remain hard across the entire observed lifetime. Waiting for readiness does not replace a process or select favorable timed samples.

The generic Windows CPU/memory census retains actual process ancestry, executable identities and CPU counters, including exited owned descendants. Unknown counters, competing Editors/compilers/workers, PID reuse, intervals over two seconds and UTC/monotonic skew over 10 ms refuse adoption across the observed lifetime. Owned background CPU over 0.5% refuses adoption within the settled-before-to-after window, except for a proved unmeasured quit bridge. `finishedHeldBeginUtc` and `finishedHeldEndUtc` must match the last complete raw interval after observed completion and before release; that whole interval remains subject to the background ceiling. Work completion must precede its beginning. A quit annotation is allowed only when the interval begins at or after that held end, crosses or follows the release signal, and ends no later than the independently observed whole-owned exit endpoint. The same actual main PID, request SHA and native zero-exit identity proof remain required. Measurement, the complete finished hold and final after are never exempted. The annotation does not claim an ancillary process role or causality. Foreign CPU above the preregistered 3% reference is retained as an excursion in this shared-host exploratory profile. The cooperative lock coordinates participating campaigns; it does not establish a quiet physical machine. Synthetic tests validate refusal logic and cannot prove actual host eligibility.

Each native snapshot records `censusStartUtc` and `censusStartMonotonic` before the Toolhelp census call, separately from the probe's endpoint. A child born inside that prior recorded window may be absent from its frozen census but appear at the next endpoint. Such a genuinely newly observed child uses its full cumulative CPU as a conservative owned upper bound. The interval retains `newProcessCensusBaselines`, including the native creation time and both source bounds. Its `foreignCpuPercent` is then a conservative upper bound that does not subtract the uncertain child's contribution; the lower residual is retained separately. This cannot make background load or foreign excursions look quieter. No window is inferred from `probeMs`, and no arbitrary tolerance is added to process birth. Existing missing/None/decreased counters, births before the recorded start, malformed windows, spans over two seconds or UTC/monotonic skew over 10 ms remain hard refusals. Recorded census windows must also be sequential: the next census starts no earlier than the preceding endpoint on both clocks. Older snapshots without start provenance cannot use this boundary case.

The whole-owned exit cutoff is independently observed. `jobApiReturnedUtc` records entry into the cleanup callback after the native job API returns. The callback then joins existing anchored owned process objects for at most two seconds, retaining their native PID, creation identity, zero-time wait results and UTC/monotonic observations in `ownedJoin`. Missing anchors, query errors, unexpected or reversing wait states and timeout are hard failures. Joining checks termination only; the controller continues normal CPU sampling while pending and does not wait for CPU quietness. The first observation with every object signaled defines `ownedCleanupCompletedUtc` and its alias `jobReturnedUtc`. `afterExitUtc` is the endpoint of exactly the first census begun after that joint completion. That first census must prove every retained owned PID absent from the live census with matching native executable, creation and exit identity, a finite final CPU counter, and an exit time no later than the endpoint. The actual main must exit with code zero. Replay uses the union of all retained owned identities and requires exit proofs and counters to remain unchanged during final after. A later census cannot replace a failed first proof. This allows the completed-hold-to-quit bridge to include native child finalization observed in that first census; the following complete after interval remains subject to the strict background ceiling.

A Toolhelp census can unlink the completed main before its process object is signaled. Host evidence schema 4 uses a metadata-free frozen Toolhelp ancestry pass before opening process handles. The declared root and retained ancestry identify owned rows, which are queried first. Their initial `OpenProcess` requests `PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE`; that same native object supplies both query and synchronization evidence, with strict PID, executable and creation identity checks and no later PID reopen to repair an anchor. Foreign and default-probe handles retain ordinary query rights. Shared query/synchronize aliases are closed once. An already anchored object may retain its original executable when a later image query fails during termination, provided its fresh native PID/creation and descriptor still match; successful contradictions remain hard. Signaled objects stay in retained evidence and are omitted from the live census.

`processHandleStates` brackets fresh identity and a single shared CPU read with two nonblocking `WaitForSingleObject(handle, 0)` polls. Two `WAIT_TIMEOUT` (258) observations permit pending completion with assigned exit code 0 or 259 only inside the completed-hold-to-quit bridge; the census begins after release and the interval begins after the complete finished hold. Exit FILETIME is undefined before termination, so pending records store `exitTimeDefined: false` and `exitedUnix: null`. If the first poll is signaled, the following times read may define exit time. A timeout-to-signaled transition cannot qualify through pending state; it needs fresh terminal proof collected afterward. Failed, unknown or reversed wait states are hard refusals. The [Windows wait contract](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject) and [process-times contract](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes) define these boundaries.

Every owned terminal proof includes `objectWaitCode: 0` observed on that same anchored synchronize handle before a fresh native exit-time read. That same post-signal `GetProcessTimes` read supplies `cpuSeconds`, which is used exactly in the snapshot counter. A prior partial CPU read cannot be paired with a later termination timestamp. Final whole-owned exit and the strict following after interval remain mandatory. Nonzero PID contradictions and successful-state creation/path/CPU contradictions are rejected throughout the selected lifetime. Failed optional metadata queries preserve an independently available CPU read but cannot authorize pending census absence. Schema 1, 2 and 3 host summaries are not admitted by the modern validator; original receipts and judgments stay immutable and are not mixed with schema 4 campaigns. Request, result and plan schema 1 and metric clocks remain unchanged.

If the selected main terminates after its first metadata read within a census, the observer retains that earlier state in `processHandleStateHistory` and performs one explicit metadata read after the successful terminal proof within the same census. The earlier identity must match the anchored main and its CPU must not exceed the signaled final CPU; a state already signaled before its read must have exactly that final CPU. Current successful state and snapshot CPU remain equal. This preserves both observations without changing the first exit endpoint, replacing an interval, freezing a counter or waiting for a later quiet census.

For a first image query that fails, the same held object may supply a supported native image query (`QueryFullProcessImageNameW` with native flag 1, then `K32GetProcessImageFileNameW`). Its UTF-16 length, API outcomes, actual NT image path and read-only DOS-device mapping are retained in `imageProof` schema 2. A bounded `GetLogicalDrives` bitmask enumerates at most 26 actual drive letters; `QueryDosDeviceW` queries only those letters. The receipt preserves each returned count, API error and reported MULTISZ data, and replay checks complete inventory coverage and the current first NUL-terminated target within that count. Conversion accepts exactly one current `QueryDosDeviceW` target with a full prefix boundary; obsolete targets, ambiguous aliases, missing or malformed queries remain refusals. Replay independently checks API-success evidence and conversion. These APIs do not guarantee success after termination; no executable basename is used to fill missing identity.

A failed initial anchor remains a hard refusal. `probeFailures` preserves the original PID, query stage, error and partial metadata even if cleanup subsequently fails. An incomplete census cannot be replaced with a later one or adopted through a successful-looking summary.

`public-report.json` contains the same summary as `report.json`, omitting raw configuration, paths and subprocess output by construction. It is not a credential scanner or a redistribution decision. Audit custom identifiers, metric labels and all included values before publication. Keep raw logs, private content and machine paths out of public examples.

## Extension rules

Keep the core independent of consumer project types, object catalogs and native build logic. New workloads implement the adapter contract and retain their own validity rules. Add lifecycle adapters separately when their clocks and readiness conditions differ from in-process operations. Version protocol changes explicitly; never change metric meaning while retaining the same metric name and silently comparing old results.
