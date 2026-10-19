// Memory readings of the Editor workloads (docs/methodology.md#memory-counters): start and end of the measured part, the peak over the run, one
// reading per reload (editor.reload_leak) or Play Mode cycle (editor.play_leak), and an optional Memory Profiler snapshot at the end.
// Readings are samples with a unit (bytes, count, ms): `mem/<start|end|peak>/<name>` once per process, `leak/<name>` once per reload or cycle (round = its index).
// The leak kinds also count unloaded code contexts that are still alive (CoreCLR; see CountSurvivors).
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using UnityEditor;
using MemoryProbe = UnityBench.Player.MemoryProbe;
using MemoryValue = UnityBench.Player.MemoryValue;

namespace UnityBench.Editor
{
    public static partial class BatchEntry
    {
        const string SnapshotStep = "snapshot";
        const double PollMilliseconds = 250;
        // Counters that only grow have no peak distinct from their end value.
        static readonly string[] NoPeak = { ".NET GC Count gen", ".NET Total Allocated", ".NET GC Pause Total" };

        static bool IsEdit(string kind) => kind == "editor.script_edit" || kind == "editor.reload_leak";
        static bool IsPlay(string kind) => kind == "editor.play" || kind == "editor.play_leak";
        static bool IsLeak(string kind) => kind == "editor.reload_leak" || kind == "editor.play_leak";
        // editor.scene_play reads its memory inside Play Mode (SceneWorkload), like a player does.
        static bool Measures => s.req.memory && s.req.kind != "editor.scene_play";

        static void AddMemorySample(string name, MemoryValue v, int round)
        {
            s.samples.Add(new SampleRecord { name = name, ms = v.Value, unit = v.Unit, round = round, phase = "", hash = "", workGroup = "" });
        }

        static void Track(List<MemoryValue> values)
        {
            foreach (var v in values)
            {
                if (Array.Exists(NoPeak, p => v.Name.StartsWith(p))) continue;
                var key = v.Name + "\t" + v.Unit;   // the unit travels in the key: Number has no room for it
                var i = s.memPeak.FindIndex(n => n.k == key);
                if (i < 0) s.memPeak.Add(new Number { k = key, v = v.Value });
                else if (v.Value > s.memPeak[i].v) s.memPeak[i].v = v.Value;
            }
        }

        static List<MemoryValue> Read(bool full)
        {
            var missing = new List<string>();
            var values = MemoryProbe.Read(full, missing);
            foreach (var m in missing) if (!s.memMissing.Contains(m)) s.memMissing.Add(m);
            Track(values);
            return values;
        }

        static void MemoryStart()
        {
            if (!Measures) return;
            if (IsLeak(s.req.kind)) TrackGeneration();
            foreach (var v in Read(true)) AddMemorySample("mem/start/" + v.Name, v, 0);
            s.lastPoll = Now;
        }

        // A cheap reading (Profiler API and .NET GC, no recorders) a few times a second, only to find the peak.
        static void MemoryPoll()
        {
            if (s.phase != "run" || !Measures || Ms(s.lastPoll, Now) < PollMilliseconds) return;
            s.lastPoll = Now;
            Read(false);
        }

        static void MemoryStep()
        {
            if (s.phase == "run" && Measures) Read(true);
        }

        static void MemoryEnd()
        {
            if (!Measures) return;
            try
            {
                foreach (var v in Read(true)) AddMemorySample("mem/end/" + v.Name, v, 0);
                foreach (var p in s.memPeak)
                {
                    var parts = p.k.Split('\t');
                    AddMemorySample("mem/peak/" + parts[0], new MemoryValue { Name = parts[0], Unit = parts[1], Value = p.v }, 0);
                }
                s.notes.Add(new Pair { k = "memory.offered", v = string.Join(", ", MemoryProbe.Offered()) });
                s.notes.Add(new Pair { k = "memory.missing", v = string.Join(", ", s.memMissing) });
                if (IsLeak(s.req.kind)) s.notes.Add(new Pair { k = "memory.forced_gc", v = s.req.forceGc ? "GC.Collect, WaitForPendingFinalizers, GC.Collect, EditorUtility.UnloadUnusedAssetsImmediate" : "none" });
                if (IsLeak(s.req.kind)) s.notes.Add(new Pair { k = "memory.unloaded_contexts", v = Collectible
                    ? "one weak GCHandle per generation of UnityBench.Editor.BatchEntry; counted after up to " + SurvivorCollections + " x (GC.Collect, WaitForPendingFinalizers)"
                    : "not counted: this Editor's script code is not in a collectible AssemblyLoadContext (Mono)" });
            }
            catch (Exception e) { s.notes.Add(new Pair { k = "memory.error", v = e.GetType().Name + ": " + e.Message }); }
        }

        // One reading after a reload (or a Play Mode cycle), after the collection the notes document. The runner fits the series.
        static void LeakPoint()
        {
            if (!IsLeak(s.req.kind) || !s.req.memory) return;
            if (s.req.forceGc)
            {
                MemoryProbe.CollectGarbage();
                EditorUtility.UnloadUnusedAssetsImmediate();
            }
            foreach (var v in Read(true)) AddMemorySample("leak/" + v.Name, v, s.leakIndex);
            CountSurvivors(s.leakIndex);
            s.leakIndex++;
        }

        // ---- unloaded code that stays alive (CoreCLR) ------------------------------------------------------------------
        //
        // AppDomain.GetAssemblies() and AssemblyLoadContext.All stop listing a context as soon as Unload() is called, even when it is never collected, so
        // `Loaded Assemblies` and `AssemblyLoadContexts` stay flat while every reload leaks the previous context (tesselworks/unity-bench#18). Instead this keeps
        // one weak GCHandle per generation of this package's code (to the Type BatchEntry, which lives in the same collectible context as the project's
        // scripts) across reloads; the handle table survives a code reload, the handle does not keep its target alive. After the forced collections, a handle
        // to an earlier generation whose target is still alive is a context that was unloaded but not collected. A healthy Editor reads 0 (sometimes 1 while
        // the last unload completes); a leaking one reads the number of reloads so far. Mono reloads the whole domain, has no collectible contexts, and reports
        // the counter as not available.

        const string SurvivorName = "Unloaded contexts still alive", TrackedName = "Unloaded contexts";
        const int SurvivorCollections = 5;
        static readonly PropertyInfo IsCollectibleProperty = typeof(Assembly).GetProperty("IsCollectible");   // not in the API surface the Editor compiles against

        static bool Collectible
        {
            get
            {
                try { return Type.GetType("Mono.Runtime") == null && IsCollectibleProperty != null && (bool)IsCollectibleProperty.GetValue(typeof(BatchEntry).Assembly); }
                catch (Exception) { return false; }
            }
        }

        // Called on every load of the package's code while a leak workload runs (Recover) and at its start: a new generation gets a handle.
        static void TrackGeneration()
        {
            if (s == null || !Collectible || Known(typeof(BatchEntry))) return;
            s.generations.Add(GCHandle.ToIntPtr(GCHandle.Alloc(typeof(BatchEntry), GCHandleType.Weak)).ToInt64());
        }

        static void CountSurvivors(int round)
        {
            if (!Collectible) { if (!s.memMissing.Contains(SurvivorName)) s.memMissing.Add(SurvivorName); return; }
            TrackGeneration();
            int alive = 0, old = 0;
            // An unload completes over more than one collection (the loader allocator is finalized first); stop as soon as nothing old is alive.
            for (var i = 0; i <= SurvivorCollections; i++)
            {
                Survivors(out alive, out old);
                if (alive == 0 || i == SurvivorCollections) break;
                GC.Collect();
                GC.WaitForPendingFinalizers();
            }
            AddMemorySample("leak/" + SurvivorName, new MemoryValue { Name = SurvivorName, Unit = "count", Value = alive }, round);
            AddMemorySample("leak/" + TrackedName, new MemoryValue { Name = TrackedName, Unit = "count", Value = old }, round);
        }

        // Kept out of line so that no target stays on the caller's stack (and alive) through its collections.
        [MethodImpl(MethodImplOptions.NoInlining)]
        static void Survivors(out int alive, out int old)
        {
            alive = old = 0;
            foreach (var h in s.generations)
            {
                var target = Target(h);
                if (ReferenceEquals(target, typeof(BatchEntry))) continue;
                old++;
                if (target != null) alive++;
            }
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        static bool Known(Type current)
        {
            foreach (var h in s.generations) if (ReferenceEquals(Target(h), current)) return true;
            return false;
        }

        static object Target(long handle)
        {
            try { return GCHandle.FromIntPtr(new IntPtr(handle)).Target; }
            catch (Exception) { return null; }
        }

        static void FreeGenerations()
        {
            foreach (var h in s.generations)
            {
                try { GCHandle.FromIntPtr(new IntPtr(h)).Free(); } catch (Exception) { }
            }
            s.generations.Clear();
        }

        // ---- Memory Profiler snapshot ----------------------------------------------------------------------------------

        static void SnapshotStepRun(Step step)
        {
            if (!s.started)
            {
                if (!Idle) return;
                Begin(step);
                var why = TakeSnapshot(s.req.snapshotPath);
                if (why != null) { s.notes.Add(new Pair { k = "memory.snapshot", v = "skipped: " + why }); s.snapshotDone = true; Save(); }
                return;
            }
            if (s.snapshotDone) End(step);
        }

        // The snapshot is taken with the engine's API (found by reflection: its namespace differs between Editors) and only when the project has
        // the Memory Profiler package, which is what opens it. Returns why nothing was started, or null when a snapshot is on its way.
        static string TakeSnapshot(string path)
        {
            if (UnityEditor.PackageManager.PackageInfo.FindForAssetPath("Packages/com.unity.memoryprofiler") == null) return "com.unity.memoryprofiler is not in the project";
            if (string.IsNullOrEmpty(path)) return "no snapshot path in the request";
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            MethodInfo take = null;
            foreach (var name in new[] { "Unity.Profiling.Memory.MemoryProfiler, UnityEngine.CoreModule", "UnityEngine.Profiling.Memory.Experimental.MemoryProfiler, UnityEngine.CoreModule" })
            {
                var methods = Type.GetType(name, false)?.GetMethods(BindingFlags.Public | BindingFlags.Static) ?? new MethodInfo[0];
                foreach (var m in methods)
                    if (m.Name == "TakeSnapshot" && m.GetParameters().Length >= 2 && m.GetParameters()[0].ParameterType == typeof(string) && m.GetParameters()[1].ParameterType == typeof(Action<string, bool>)) take = m;
                if (take != null) break;
            }
            if (take == null) return "this Editor has no MemoryProfiler.TakeSnapshot";
            Action<string, bool> done = (file, ok) =>
            {
                if (s == null) return;
                s.notes.Add(new Pair { k = "memory.snapshot", v = ok ? file : "failed: " + file });
                s.snapshotDone = true; Save();
            };
            var parameters = take.GetParameters();
            var args = new object[parameters.Length];
            args[0] = path; args[1] = done;
            for (var i = 2; i < parameters.Length; i++) args[i] = parameters[i].HasDefaultValue ? parameters[i].DefaultValue : parameters[i].ParameterType.IsValueType ? Activator.CreateInstance(parameters[i].ParameterType) : null;
            take.Invoke(null, args);
            return null;
        }
    }
}
