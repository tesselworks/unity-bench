using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using UnityEditor;
using UnityEngine;

namespace UnityBench.Editor
{
    public static class BatchEntry
    {
        private const int IdleHoldPollingDelayMs = 50;
        [Serializable]
        private sealed class Request
        {
            public int schemaVersion;
            public string runId;
            public string variant;
            public string workload;
            public string outputPath;
            public int warmIterations;
            public bool preparationOnly;
            public string sourceRevision;
            public string planDigest;
            public string readyPath;
            public string startPath;
            public string finishedPath;
            public string releasePath;
            public int handshakeTimeoutSeconds;
            [NonSerialized] public string RequestSha256;
        }

        [Serializable]
        private sealed class Signal
        {
            public string runId;
            public int processId;
            public string utc;
            public string requestSha256;
        }

        private sealed class Sample
        {
            public int Iteration;
            public double Milliseconds;
        }

        private static Coordinator active;

        private sealed class Coordinator
        {
            private enum Phase { WaitingForInitialEditorIdle, WaitingForPreparedEditorIdle, WaitingForStart, WaitingForRelease }
            private readonly Request request;
            private readonly IWorkload workload;
            private readonly Stopwatch wait = new Stopwatch();
            private readonly List<Sample> samples = new List<Sample>();
            private WorkProof proof;
            private string startedUtc = "", completedUtc = "";
            private Phase phase;
            private bool exited;

            internal Coordinator(Request request, IWorkload workload)
            {
                this.request = request;
                this.workload = workload;
            }

            internal void Begin()
            {
                phase = Phase.WaitingForInitialEditorIdle;
                wait.Start();
                // Return from executeMethod so import/compilation services can advance.
                EditorApplication.update += Tick;
            }

            private static bool EditorIdle => !EditorApplication.isCompiling && !EditorApplication.isUpdating;

            internal void Tick()
            {
                if (exited) return;
                try
                {
                    if (phase == Phase.WaitingForInitialEditorIdle || phase == Phase.WaitingForPreparedEditorIdle)
                    {
                        if (wait.Elapsed.TotalSeconds >= 120)
                            throw new TimeoutException("Editor preparation did not become idle before its bound.");
                        if (!EditorIdle) return;
                        if (phase == Phase.WaitingForInitialEditorIdle)
                        {
                            workload.Prepare();
                            phase = Phase.WaitingForPreparedEditorIdle;
                            // Yield one Editor update after preparation, which may request imports.
                            // Both idle phases share the original 120-second preparation bound.
                            return;
                        }
                        if (request.preparationOnly)
                        {
                            startedUtc = UtcNow();
                            workload.Execute();
                            proof = workload.CaptureProof();
                            ValidateProof(proof);
                            completedUtc = UtcNow();
                            // A full-work compatibility/cache prime, with no operation stopwatch.
                            WriteResult(request, "prepared", proof, samples, startedUtc, completedUtc);
                            Exit(0);
                            return;
                        }
                        WriteSignal(request.readyPath, request);
                        phase = Phase.WaitingForStart;
                        wait.Restart();
                        return;
                    }
                    if (wait.Elapsed.TotalSeconds >= request.handshakeTimeoutSeconds)
                        throw new TimeoutException("Lifecycle signal did not arrive before its bound.");
                    if (phase == Phase.WaitingForStart)
                    {
                        if (!ReadSignalIfPresent(request.startPath, request))
                        {
                            YieldIdleWait();
                            return;
                        }
                        if (!EditorIdle)
                            throw new InvalidOperationException("Editor became busy before the measurement start.");
                        Measure();
                        phase = Phase.WaitingForRelease;
                        wait.Restart();
                        return;
                    }
                    if (ReadSignalIfPresent(request.releasePath, request)) Exit(0);
                    else YieldIdleWait();
                }
                catch (Exception) { Fail(); }
            }

            private static void YieldIdleWait()
            {
                // Batchmode can spin the update loop at one full core. Throttle only
                // idle start/release holds, outside operation clocks, then return to
                // the Editor so compilation/import services can continue advancing.
                if (EditorIdle) Thread.Sleep(IdleHoldPollingDelayMs);
            }

            private void Measure()
            {
                startedUtc = UtcNow();
                for (var iteration = 0; iteration <= request.warmIterations; iteration++)
                {
                    var watch = Stopwatch.StartNew();
                    workload.Execute();
                    watch.Stop();
                    var elapsed = watch.Elapsed.TotalMilliseconds;
                    if (!Finite(elapsed) || elapsed < 0)
                        throw new InvalidOperationException("Invalid measured time.");
                    var next = workload.CaptureProof();
                    ValidateProof(next);
                    if (proof != null && !Equal(proof, next))
                        throw new InvalidOperationException("Work or output changed between iterations.");
                    proof = new WorkProof { InputFingerprint = next.InputFingerprint,
                        OutputFingerprint = next.OutputFingerprint,
                        WorkCounters = new Dictionary<string, double>(next.WorkCounters) };
                    samples.Add(new Sample { Iteration = iteration, Milliseconds = elapsed });
                }
                completedUtc = UtcNow();
                WriteResult(request, "completed", proof, samples, startedUtc, completedUtc);
                WriteSignal(request.finishedPath, request, completedUtc);
            }

            internal void Fail()
            {
                WriteFailure(request, startedUtc, completedUtc);
                Exit(1);
            }

            private void Exit(int code)
            {
                if (exited) return;
                exited = true;
                EditorApplication.update -= Tick;
                active = null;
                EditorApplication.Exit(code);
            }
        }

        // Call with -batchmode -executeMethod UnityBench.Editor.BatchEntry.Run
        // -unityBenchRequest /absolute/request.json. Do not add -quit: this owns exit.
        public static void Run()
        {
            if (!Application.isBatchMode)
                throw new InvalidOperationException("Unity Bench requires an explicit batch invocation.");
            if (active != null)
                throw new InvalidOperationException("A Unity Bench invocation is already active.");
            Request request = null;
            try
            {
                request = ReadRequest();
                active = new Coordinator(request, Workloads.Create(request.workload));
                active.Begin();
            }
            catch (Exception)
            {
                if (active != null) active.Fail();
                else { WriteFailure(request, "", ""); EditorApplication.Exit(1); }
            }
        }

        private static void WriteFailure(Request request, string startedUtc, string completedUtc)
        {
            // No request content, paths, source payloads or exception messages in stdout.
            UnityEngine.Debug.LogError("Unity Bench failed; no performance result may be adopted.");
            if (request == null || File.Exists(request.outputPath)) return;
            try { WriteResult(request, "failed", null, new List<Sample>(), startedUtc, completedUtc); }
            catch (Exception) { UnityEngine.Debug.LogError("Unity Bench could not write its failure receipt."); }
        }

        private static Request ReadRequest()
        {
            var args = Environment.GetCommandLineArgs();
            string path = null;
            for (var i = 0; i < args.Length; i++)
                if (args[i] == "-unityBenchRequest")
                {
                    if (path != null || i + 1 >= args.Length)
                        throw new ArgumentException("Invalid request argument.");
                    path = args[++i];
                }
            if (path == null || !Path.IsPathRooted(path))
                throw new ArgumentException("An absolute request path is required.");
            var requestBytes = File.ReadAllBytes(path);
            var request = JsonUtility.FromJson<Request>(new UTF8Encoding(false, true).GetString(requestBytes));
            if (request == null || request.schemaVersion != 1 ||
                string.IsNullOrWhiteSpace(request.runId) || string.IsNullOrWhiteSpace(request.variant) ||
                string.IsNullOrWhiteSpace(request.workload) || string.IsNullOrWhiteSpace(request.outputPath) ||
                !Path.IsPathRooted(request.outputPath) || request.warmIterations < 0 || request.warmIterations > 10000)
                throw new ArgumentException("Invalid v1 request.");
            request.outputPath = Path.GetFullPath(request.outputPath);
            if (File.Exists(request.outputPath))
                throw new IOException("A result must never overwrite an existing receipt.");
            using (var sha = SHA256.Create())
                request.RequestSha256 = BitConverter.ToString(sha.ComputeHash(requestBytes)).Replace("-", "").ToLowerInvariant();
            var paths = new[] { request.readyPath, request.startPath, request.finishedPath, request.releasePath };
            var count = 0;
            foreach (var signalPath in paths) if (!string.IsNullOrWhiteSpace(signalPath)) count++;
            if (request.preparationOnly)
            {
                if (count != 0 || request.warmIterations != 0 ||
                    string.IsNullOrWhiteSpace(request.sourceRevision) || string.IsNullOrWhiteSpace(request.planDigest))
                    throw new ArgumentException("Preparation-only requires no lifecycle paths, zero warm iterations and provenance.");
                return request;
            }
            if (count != 4 || request.handshakeTimeoutSeconds < 1 || request.handshakeTimeoutSeconds > 120 ||
                string.IsNullOrWhiteSpace(request.sourceRevision) || string.IsNullOrWhiteSpace(request.planDigest))
                throw new ArgumentException("Lifecycle requires all four paths, bounded wait and provenance.");
            {
                var unique = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { request.outputPath, Path.GetFullPath(path) };
                foreach (var signalPath in paths)
                    if (!Path.IsPathRooted(signalPath) || !unique.Add(Path.GetFullPath(signalPath)) || File.Exists(signalPath))
                        throw new ArgumentException("Lifecycle paths must be distinct absolute new files.");
            }
            return request;
        }

        private static int ProcessId() { using (var process = Process.GetCurrentProcess()) return process.Id; }
        private static string UtcNow() => DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);

        private static void WriteSignal(string path, Request request, string completedUtc = null)
        {
            var json = "{\"runId\":" + Quote(request.runId) + ",\"processId\":" + ProcessId().ToString(CultureInfo.InvariantCulture) +
                ",\"requestSha256\":" + Quote(request.RequestSha256) +
                ",\"utc\":" + Quote(completedUtc ?? UtcNow()) +
                ",\"isCompiling\":" + (EditorApplication.isCompiling ? "true" : "false") +
                ",\"isUpdating\":" + (EditorApplication.isUpdating ? "true" : "false") +
                (completedUtc == null ? "" : ",\"completedUtc\":" + Quote(completedUtc)) + "}\n";
            var temporary = path + "." + ProcessId().ToString(CultureInfo.InvariantCulture) + ".tmp";
            WriteNew(temporary, json);
            File.Move(temporary, path);
        }

        private static bool ReadSignalIfPresent(string path, Request request)
        {
            if (!File.Exists(path)) return false;
            // The controller writes signals atomically; partial/malformed signals fail closed.
            var signal = JsonUtility.FromJson<Signal>(File.ReadAllText(path, new UTF8Encoding(false, true)));
            if (signal == null || signal.runId != request.runId || signal.processId != ProcessId() ||
                signal.requestSha256 != request.RequestSha256 ||
                string.IsNullOrWhiteSpace(signal.utc) ||
                !(signal.utc.EndsWith("Z", StringComparison.Ordinal) || signal.utc.EndsWith("+00:00", StringComparison.Ordinal)) ||
                !DateTimeOffset.TryParse(signal.utc, CultureInfo.InvariantCulture, DateTimeStyles.None, out var timestamp) ||
                timestamp.Offset != TimeSpan.Zero)
                throw new InvalidOperationException("Lifecycle signal identity or UTC is invalid.");
            return true;
        }

        private static void WriteNew(string path, string json)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            using (var writer = new StreamWriter(stream, new UTF8Encoding(false))) writer.Write(json);
        }

        private static bool Finite(double value) => !double.IsNaN(value) && !double.IsInfinity(value);
        private static void ValidateProof(WorkProof proof)
        {
            if (proof == null || string.IsNullOrWhiteSpace(proof.InputFingerprint) ||
                string.IsNullOrWhiteSpace(proof.OutputFingerprint) || proof.WorkCounters == null || proof.WorkCounters.Count == 0)
                throw new InvalidOperationException("A workload requires input, output and work proof.");
            foreach (var counter in proof.WorkCounters)
                if (string.IsNullOrWhiteSpace(counter.Key) || !Finite(counter.Value) || counter.Value < 0)
                    throw new InvalidOperationException("Invalid work counter.");
        }

        private static bool Equal(WorkProof left, WorkProof right)
        {
            if (left.InputFingerprint != right.InputFingerprint || left.OutputFingerprint != right.OutputFingerprint ||
                left.WorkCounters.Count != right.WorkCounters.Count) return false;
            foreach (var counter in left.WorkCounters)
                if (!right.WorkCounters.TryGetValue(counter.Key, out var value) || value != counter.Value) return false;
            return true;
        }

        private static string Backend()
        {
            if (Type.GetType("Mono.Runtime") != null) return "mono";
            if (Type.GetType("System.Runtime.Loader.AssemblyLoadContext, System.Private.CoreLib") != null ||
                Type.GetType("System.Runtime.Loader.AssemblyLoadContext, System.Runtime.Loader") != null) return "coreclr";
            return "unknown";
        }

        private static object PublicSetting(string typeName, string member)
        {
            var type = Type.GetType(typeName);
            if (type == null) return null;
            var property = type.GetProperty(member, BindingFlags.Public | BindingFlags.Static);
            if (property != null) return property.GetValue(null, null);
            var field = type.GetField(member, BindingFlags.Public | BindingFlags.Static);
            return field?.GetValue(null);
        }

        private static string Settings()
        {
            // Public runtime settings only. Missing APIs are marked unavailable, never guessed.
            var workers = PublicSetting("Unity.Jobs.LowLevel.Unsafe.JobsUtility, UnityEngine.CoreModule", "JobWorkerCount");
            var optimization = PublicSetting("UnityEditor.Compilation.CompilationPipeline, UnityEditor.CoreModule", "codeOptimization");
            return "{\"batchMode\":" + (Application.isBatchMode ? "true" : "false") +
                ",\"idleHoldPollingDelayMs\":" + IdleHoldPollingDelayMs.ToString(CultureInfo.InvariantCulture) +
                ",\"jobWorkerCountAvailable\":" + (workers == null ? "false" : "true") +
                ",\"jobWorkerCount\":" + (workers == null ? "null" : Convert.ToInt32(workers, CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture)) +
                ",\"codeOptimizationAvailable\":" + (optimization == null ? "false" : "true") +
                ",\"codeOptimization\":" + (optimization == null ? "null" : Quote(optimization.ToString())) +
                ",\"vSyncCount\":" + QualitySettings.vSyncCount.ToString(CultureInfo.InvariantCulture) +
                ",\"targetFrameRate\":" + Application.targetFrameRate.ToString(CultureInfo.InvariantCulture) +
                ",\"qualityLevel\":" + QualitySettings.GetQualityLevel().ToString(CultureInfo.InvariantCulture) + "}";
        }

        private static void WriteResult(Request request, string status, WorkProof proof, List<Sample> samples,
            string startedUtc, string completedUtc)
        {
            var json = new StringBuilder("{\"schemaVersion\":1,\"runId\":").Append(Quote(request.runId))
                .Append(",\"variant\":").Append(Quote(request.variant)).Append(",\"workload\":").Append(Quote(request.workload))
                .Append(",\"processId\":").Append(ProcessId().ToString(CultureInfo.InvariantCulture))
                .Append(",\"requestSha256\":").Append(Quote(request.RequestSha256))
                .Append(",\"sourceRevision\":").Append(Quote(request.sourceRevision ?? ""))
                .Append(",\"planDigest\":").Append(Quote(request.planDigest ?? ""))
                .Append(",\"startedUtc\":").Append(Quote(startedUtc))
                .Append(",\"completedUtc\":").Append(Quote(completedUtc))
                .Append(",\"status\":").Append(Quote(status)).Append(",\"environment\":{\"unityVersion\":")
                .Append(Quote(Application.unityVersion)).Append(",\"backend\":").Append(Quote(Backend()))
                .Append(",\"executionKind\":\"editor-batch\",\"platform\":").Append(Quote(Application.platform.ToString()))
                .Append(",\"settings\":").Append(Settings())
                .Append("},\"inputFingerprint\":").Append(Quote(proof?.InputFingerprint ?? ""))
                .Append(",\"outputFingerprint\":").Append(Quote(proof?.OutputFingerprint ?? ""))
                .Append(",\"workCounters\":{");
            if (proof != null)
            {
                var keys = new List<string>(proof.WorkCounters.Keys);
                keys.Sort(StringComparer.Ordinal);
                for (var i = 0; i < keys.Count; i++)
                {
                    if (i > 0) json.Append(',');
                    json.Append(Quote(keys[i])).Append(':').Append(proof.WorkCounters[keys[i]].ToString("R", CultureInfo.InvariantCulture));
                }
            }
            json.Append("},\"samples\":[");
            for (var i = 0; i < samples.Count; i++)
            {
                if (i > 0) json.Append(',');
                json.Append("{\"iteration\":").Append(samples[i].Iteration).Append(",\"phase\":")
                    .Append(Quote(samples[i].Iteration == 0 ? "first" : "warm"))
                    .Append(",\"metric\":\"executeWallTime\",\"value\":")
                    .Append(samples[i].Milliseconds.ToString("R", CultureInfo.InvariantCulture)).Append(",\"unit\":\"ms\"}");
            }
            json.Append("]}\n");
            WriteNew(request.outputPath, json.ToString());
        }

        private static string Quote(string value)
        {
            var text = new StringBuilder("\"");
            foreach (var character in value)
            {
                if (character == '\\' || character == '"') text.Append('\\').Append(character);
                else if (character < 32 || char.IsSurrogate(character))
                    text.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                else text.Append(character);
            }
            return text.Append('"').ToString();
        }
    }
}
