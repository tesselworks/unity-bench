// The built-in entry point of player.scene and editor.scene_play: plays one scene for a warm-up and a recording period and reports
//   frame time (warm-up and steady), ProfilerRecorder markers and counters (main thread, PlayerLoop, physics, rendering, scripts, GC, memory),
//   and whatever the game says through Bench.Mark / Bench.Hash / Bench.Begin / Bench.End.
// It runs as an adapter (AdapterRunner pumps the coroutine and writes the result file), so a player or an Editor that was not started with
// -unityBenchAdapter never reaches it. Which markers exist differs between Editors, scripting backends and Release/Development players;
// a marker that is not there is listed in the result notes (markers.missing) and is not an error.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using Unity.Profiling;
using Unity.Profiling.LowLevel;
using Unity.Profiling.LowLevel.Unsafe;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.Scripting;

namespace UnityBench.Player
{
    [Preserve]
    public static class SceneWorkload
    {
        public const string Method = "UnityBench.Player.SceneWorkload.Run";

        // What to record, in report order, by the name the player lists it under (ProfilerRecorderHandle.GetAvailable). A name the player does not list
        // is reported as missing: StartNew on an unknown name would answer Valid and then never record anything, so it is not used.
        //   Gauge: a level (memory in use), not an amount per frame.
        //   Total: also report the sum over the steady phase (allocation per run is easier to read than per frame).
        //   Alias: another name the same thing has in players (the script-update marker is BehaviourUpdate there).
        struct Wanted
        {
            public string Name, Alias;
            public bool Gauge, Total;
            public Wanted(string name, string alias = null, bool gauge = false, bool total = false) { Name = name; Alias = alias; Gauge = gauge; Total = total; }
        }

        static Wanted[] WantedProbes() => new[]
        {
            new Wanted("Main Thread"), new Wanted("PlayerLoop"), new Wanted("Update.ScriptRunBehaviourUpdate", "BehaviourUpdate"), new Wanted("FixedUpdate.PhysicsFixedUpdate", "FixedBehaviourUpdate"),
            new Wanted("Physics.Simulate"), new Wanted("Physics2D.Simulate"),
            new Wanted("Culling"), new Wanted("Camera.Render"), new Wanted("RenderLoop"), new Wanted("Gfx.WaitForPresentOnGfxThread"), new Wanted("Gfx.WaitForRenderThread"),
            new Wanted("SetPass Calls Count"), new Wanted("Triangles Count"), new Wanted("Vertices Count"),
            new Wanted("GC.Alloc"), new Wanted("GC.Collect"), new Wanted("GC Allocated In Frame", total: true), new Wanted("GC Allocation In Frame Count", total: true),
            new Wanted("GC Used Memory", gauge: true), new Wanted("GC Reserved Memory", gauge: true), new Wanted("Total Used Memory", gauge: true),
            new Wanted("App Resident Memory", gauge: true),
        };

        sealed class Probe
        {
            public string Name, Unit;
            public double Scale = 1;
            public bool Gauge, Total;
            public ProfilerRecorder Recorder;
            public double Previous;
            public readonly List<double> Values = new List<double>(4096);

            // The value of the frame that just ended. When the profiler closes frames the recorder holds one sample per frame (LastValue).
            // When it does not (a batchmode Editor), samples never complete and CurrentValue keeps accumulating, so a frame's value is the
            // change since the last read. A gauge is just its current level.
            public double Read()
            {
                var current = (double)Recorder.CurrentValue;
                var value = Gauge ? (Recorder.Count >= 2 ? Recorder.LastValue : current)
                          : Recorder.Count >= 2 ? Recorder.LastValue : current >= Previous ? current - Previous : current;
                Previous = current;
                return Math.Max(0, value * Scale);
            }
        }

        sealed class Options
        {
            public string Scene;
            public int WarmupFrames, DurationFrames;          // 0 = not used
            public double WarmupSeconds, DurationSeconds;
            public float CaptureDelta, FixedTimestep;         // 0 = leave the project's
            public bool VSync, ListMarkers, Memory;

            public static Options From(PlayerContext ctx)
            {
                var o = new Options { Scene = ctx.Arg("scene") };
                if (string.IsNullOrEmpty(o.Scene)) throw new ArgumentException("scene workload: the scene argument is missing");
                o.WarmupFrames = Int(ctx, "warmup_frames");
                o.WarmupSeconds = o.WarmupFrames > 0 ? 0 : Num(ctx, "warmup_seconds", 0);
                o.DurationFrames = Int(ctx, "duration_frames");
                o.DurationSeconds = o.DurationFrames > 0 ? 0 : Num(ctx, "duration_seconds", 10);
                o.CaptureDelta = (float)Num(ctx, "capture_delta", 0);
                o.FixedTimestep = (float)Num(ctx, "fixed_timestep", 0);
                o.VSync = ctx.Arg("vsync", "false") == "true";
                o.ListMarkers = ctx.Arg("list_markers", "false") == "true";
                o.Memory = ctx.Arg("memory", "true") == "true";
                return o;
            }

            static int Int(PlayerContext ctx, string key) => int.Parse(ctx.Arg(key, "0"), CultureInfo.InvariantCulture);
            static double Num(PlayerContext ctx, string key, double fallback) => double.Parse(ctx.Arg(key, fallback.ToString(CultureInfo.InvariantCulture)), CultureInfo.InvariantCulture);

            // A phase ends after `frames` frames, or after `seconds` of real time when no frame count was given.
            public static bool Over(int frames, double seconds, int counted, long ticks) =>
                frames > 0 ? counted >= frames : ticks * 1.0 / Stopwatch.Frequency >= seconds;
        }

        /// <summary>Entry point named by the runner as <c>-unityBenchAdapter</c> (player.scene).</summary>
        [Preserve]
        public static IEnumerator Run(PlayerContext ctx) => Play(ctx, Options.From(ctx));

        /// <summary>Starts the same workload in Play Mode of the Editor (editor.scene_play); <paramref name="finished"/> gets the exit code.</summary>
        public static void RunInEditor(string outPath, string requestPath, Action<int> finished)
        {
            AdapterRunner.Finished = finished;
            AdapterRunner.Attach(Method, outPath, requestPath);
        }

        static IEnumerator Play(PlayerContext ctx, Options o)
        {
            var savedDelta = Time.captureDeltaTime;
            var savedFixed = Time.fixedDeltaTime;
            var savedVSync = QualitySettings.vSyncCount;
            var savedTarget = Application.targetFrameRate;
            var probes = new List<Probe>();
            var warmup = new List<double>(1024);
            var steady = new List<double>(4096);
            var gc0 = new int[3];
            var gc1 = new int[3];
            long steadyTicks = 0;
            var memory = new MemoryBreakdown();
            try
            {
                if (!o.VSync) { QualitySettings.vSyncCount = 0; Application.targetFrameRate = -1; }
                if (o.CaptureDelta > 0) Time.captureDeltaTime = o.CaptureDelta;
                if (o.FixedTimestep > 0) Time.fixedDeltaTime = o.FixedTimestep;

                if (SceneManager.GetActiveScene().path != o.Scene)
                {
                    var loading = Stopwatch.StartNew();
                    var load = SceneManager.LoadSceneAsync(o.Scene);
                    if (load == null) throw new InvalidOperationException("the scene cannot be loaded (is it in the build?): " + o.Scene);
                    while (!load.isDone) yield return null;
                    yield return null;
                    ctx.AddValue("scene_load", loading.Elapsed.TotalMilliseconds, "ms", null);
                }

                StartProbes(ctx, o, probes);
                Bench.StartRecording();
                var isSteady = o.WarmupFrames <= 0 && o.WarmupSeconds <= 0;
                if (isSteady) { Bench.SetSteady(true); for (var g = 0; g < 3; g++) gc0[g] = GC.CollectionCount(g); memory.ReadStart(o, probes); }
                var last = Stopwatch.GetTimestamp();
                var phaseStart = last;
                var counted = 0;
                while (true)
                {
                    yield return null;
                    var now = Stopwatch.GetTimestamp();
                    var ms = (now - last) * 1000.0 / Stopwatch.Frequency;
                    last = now;
                    counted++;
                    if (!isSteady)
                    {
                        foreach (var p in probes) p.Read();   // keeps the change-since-last-read baseline current
                        warmup.Add(ms);
                        if (!Options.Over(o.WarmupFrames, o.WarmupSeconds, counted, now - phaseStart)) continue;
                        isSteady = true; counted = 0; phaseStart = now;
                        Bench.SetSteady(true);
                        for (var g = 0; g < 3; g++) gc0[g] = GC.CollectionCount(g);
                        if (memory.ReadStart(o, probes)) last = phaseStart = Stopwatch.GetTimestamp();   // the reading is not part of the first steady frame
                        continue;
                    }
                    steady.Add(ms);
                    foreach (var p in probes) p.Values.Add(p.Read());
                    if (Options.Over(o.DurationFrames, o.DurationSeconds, counted, now - phaseStart)) { steadyTicks = now - phaseStart; break; }
                }
                for (var g = 0; g < 3; g++) gc1[g] = GC.CollectionCount(g);
                memory.ReadEnd(o);
            }
            finally
            {
                Bench.StopRecording();
                foreach (var p in probes) if (p.Recorder.Valid) p.Recorder.Dispose();
                Time.captureDeltaTime = savedDelta;
                Time.fixedDeltaTime = savedFixed;
                QualitySettings.vSyncCount = savedVSync;
                Application.targetFrameRate = savedTarget;
            }
            Report(ctx, o, probes, warmup, steady, gc0, gc1, steadyTicks, memory);
        }

        // The memory breakdown of the steady phase: a reading as the phase starts and one as it ends (MemoryProbe), and as the peak the largest level a gauge
        // recorder saw in any steady frame (reading in the frame loop would allocate and disturb the per-frame numbers the workload exists to record).
        sealed class MemoryBreakdown
        {
            List<MemoryValue> start, end;
            readonly List<string> missing = new List<string>();

            // Returns whether a reading was taken. The recorders' change-since-last-read baselines are reset afterwards, so the reading's own garbage is not charged to a frame.
            public bool ReadStart(Options o, List<Probe> probes)
            {
                if (!o.Memory) return false;
                start = MemoryProbe.Read(true, missing);
                foreach (var p in probes) p.Read();
                return true;
            }

            public void ReadEnd(Options o) { if (o.Memory) end = MemoryProbe.Read(true, missing); }

            public void Report(PlayerContext ctx, List<Probe> probes, string group)
            {
                if (start == null || end == null) return;
                foreach (var v in start) ctx.AddValue("mem/start/" + v.Name, v.Value, v.Unit, group);
                foreach (var v in end) ctx.AddValue("mem/end/" + v.Name, v.Value, v.Unit, group);
                foreach (var p in probes)
                {
                    if (!p.Gauge || p.Values.Count == 0) continue;
                    var peak = 0.0;
                    foreach (var x in p.Values) peak = Math.Max(peak, x);
                    foreach (var v in start) if (v.Name == p.Name) peak = Math.Max(peak, v.Value);
                    foreach (var v in end) if (v.Name == p.Name) peak = Math.Max(peak, v.Value);
                    ctx.AddValue("mem/peak/" + p.Name, peak, p.Unit, group);
                }
                ctx.Note("memory.offered", string.Join(", ", MemoryProbe.Offered()));
                ctx.Note("memory.missing", string.Join(", ", new HashSet<string>(missing)));
            }
        }

        // ---- profiler recorders ------------------------------------------------------------------------------------

        static void StartProbes(PlayerContext ctx, Options o, List<Probe> probes)
        {
            var offered = new Dictionary<string, ProfilerRecorderHandle>();
            var catalog = new List<string>();
            var handles = new List<ProfilerRecorderHandle>();
            try
            {
                ProfilerRecorderHandle.GetAvailable(handles);
                foreach (var h in handles)
                {
                    var d = ProfilerRecorderHandle.GetDescription(h);
                    if (!offered.ContainsKey(d.Name)) offered[d.Name] = h;
                    if (o.ListMarkers) catalog.Add(d.Category.Name + "\t" + d.Name + "\t" + d.UnitType);
                }
            }
            catch (Exception e) { ctx.Note("markers.catalog.error", e.GetType().Name + ": " + e.Message); }
            ctx.Note("markers.offered", offered.Count.ToString(CultureInfo.InvariantCulture));
            if (o.ListMarkers) { catalog.Sort(StringComparer.Ordinal); ctx.Note("markers.catalog", string.Join("\n", catalog)); }

            const ProfilerRecorderOptions options = ProfilerRecorderOptions.WrapAroundWhenCapacityReached | ProfilerRecorderOptions.StartImmediately | ProfilerRecorderOptions.SumAllSamplesInFrame;
            const int capacity = 8;
            var recorded = new List<string>();
            var missing = new List<string>();
            foreach (var w in WantedProbes())
            {
                var recorder = default(ProfilerRecorder);
                var name = w.Name;
                try
                {
                    if (!offered.ContainsKey(name) && w.Alias != null && offered.ContainsKey(w.Alias)) name = w.Alias;
                    if (offered.TryGetValue(name, out var handle)) recorder = new ProfilerRecorder(handle, capacity, options);
                }
                catch (Exception) { }
                var probe = new Probe { Name = name, Gauge = w.Gauge, Total = w.Total, Recorder = recorder, Previous = recorder.Valid ? recorder.CurrentValue : 0 };
                if (recorder.Valid && UnitOf(recorder.UnitType, probe)) { probes.Add(probe); recorded.Add(name); }
                else { if (recorder.Valid) recorder.Dispose(); missing.Add(w.Name); }
            }
            ctx.Note("markers.recorded", string.Join(", ", recorded));
            ctx.Note("markers.missing", string.Join(", ", missing));
        }

        // Time markers are in ns, counters in bytes or plain counts. Anything else (percent, Hz) is not reported.
        static bool UnitOf(ProfilerMarkerDataUnit unit, Probe probe)
        {
            switch (unit)
            {
                case ProfilerMarkerDataUnit.TimeNanoseconds: probe.Unit = "ms"; probe.Scale = 1e-6; return true;
                case ProfilerMarkerDataUnit.Bytes: probe.Unit = "bytes"; return true;
                case ProfilerMarkerDataUnit.Count: probe.Unit = "count"; return true;
                default: return false;
            }
        }

        // ---- the result --------------------------------------------------------------------------------------------

        static void Report(PlayerContext ctx, Options o, List<Probe> probes, List<double> warmup, List<double> steady, int[] gc0, int[] gc1, long steadyTicks, MemoryBreakdown memory)
        {
            // Work identity comes only from what the game reported; frames and markers are never claimed as work.
            var snapshot = Bench.Take();
            string group = null;
            if (snapshot.Marks.Count > 0 || snapshot.Hash != null)
            {
                var work = new WorkResult(snapshot.Hash);
                foreach (var m in snapshot.Marks) work.Count(m.Key, m.Value);
                ctx.DefineWorkGroup("scene", work);
                group = "scene";
            }
            if (warmup.Count > 0) ctx.AddValues("frame", "warmup", warmup, "ms", group);
            ctx.AddValues("frame", "steady", steady, "ms", group);
            foreach (var p in probes)
            {
                ctx.AddValues(p.Name, "steady", p.Values, p.Unit, group);
                if (p.Total)
                {
                    double sum = 0;
                    foreach (var v in p.Values) sum += v;
                    ctx.AddValue(p.Name + " steady total", sum, p.Unit, group);
                }
            }
            foreach (var s in snapshot.Segments) ctx.AddValues("segment " + s.Key, "steady", s.Value, "ms", group);
            for (var g = 0; g < 3; g++) ctx.AddValue("GC collections gen" + g + " steady", gc1[g] - gc0[g], "count", group);
            try
            {
                using (var self = Process.GetCurrentProcess())
                {
                    var peak = self.PeakWorkingSet64;
                    if (peak > 0) ctx.AddValue("process peak working set", peak, "bytes", group);
                }
            }
            catch (Exception) { /* not available on every platform and backend */ }
            memory.Report(ctx, probes, group);

            ctx.Note("scene", o.Scene);
            ctx.Note("frames.warmup", warmup.Count.ToString(CultureInfo.InvariantCulture));
            ctx.Note("frames.steady", steady.Count.ToString(CultureInfo.InvariantCulture));
            ctx.Note("seconds.steady", (steadyTicks * 1.0 / Stopwatch.Frequency).ToString("0.###", CultureInfo.InvariantCulture));
            ctx.Note("clock.captureDeltaTime", o.CaptureDelta.ToString("R", CultureInfo.InvariantCulture));
            ctx.Note("clock.fixedDeltaTime", o.FixedTimestep.ToString("R", CultureInfo.InvariantCulture));
            ctx.Note("vsync", o.VSync ? "project setting" : "forced off");
            ctx.Note("hooks", group == null ? "none" : snapshot.Marks.Count + " marks" + (snapshot.Hash != null ? ", hash" : ""));
        }
    }
}
