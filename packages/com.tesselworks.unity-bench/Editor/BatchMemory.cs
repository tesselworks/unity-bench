// Memory readings of the Editor workloads (docs/methodology.md#memory-counters): start and end of the measured part, the peak over the run, one
// reading per reload (editor.reload_leak) or Play Mode cycle (editor.play_leak), and an optional Memory Profiler snapshot at the end.
// Readings are samples with a unit (bytes, count, ms): `mem/<start|end|peak>/<name>` once per process, `leak/<name>` once per reload or cycle (round = its index).
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
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
            s.leakIndex++;
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
