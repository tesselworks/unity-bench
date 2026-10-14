// Memory readings of the running process, shared by the Editor workloads and the scene workloads (docs/methodology.md#memory-counters).
//
// One reading is a list of named values from four sources, and a source that does not exist in this Editor or runtime is listed as missing, never an error:
//   ProfilerRecorder  the Memory-category counters (bytes and counts), looked up in ProfilerRecorderHandle.GetAvailable by name
//   Profiler API      Profiler.GetMonoUsedSizeLong and friends: live, no recorder involved; the cross-check of the recorders
//   .NET GC           GC.GetGCMemoryInfo, GC.GetTotalAllocatedBytes, GC.GetTotalPauseDuration by reflection (Mono does not have them), GC.CollectionCount
//   derived           Unity native = Total Used Memory - GC Used Memory (the same number as Profiler.GetTotalAllocatedMemoryLong, which the API row shows)
// The same source compiles on Unity 6.7 (Mono) and 7 (CoreCLR); nothing here is conditional on the runtime.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using Unity.Profiling;
using Unity.Profiling.LowLevel;
using Unity.Profiling.LowLevel.Unsafe;
using UnityEngine.Profiling;

namespace UnityBench.Player
{
    public struct MemoryValue
    {
        public string Name, Unit;   // Unit: bytes, count or ms
        public double Value;
    }

    public static class MemoryProbe
    {
        /// <summary>Memory-category counters that make up the breakdown. Every other bytes/count counter of the category the Editor offers is recorded too.</summary>
        static readonly string[] Wanted =
        {
            "GC Used Memory", "GC Reserved Memory", "Total Used Memory", "Total Reserved Memory", "Gfx Used Memory", "Gfx Reserved Memory",
            "Audio Used Memory", "System Used Memory", "App Resident Memory", "Object Count", "Texture Count", "Mesh Count",
        };
        // Per-frame amounts and the machine-wide total are not levels of this process.
        static readonly string[] Skipped = { "GC Allocated In Frame", "GC Allocation In Frame Count", "System Total Used Memory" };

        static Dictionary<string, ProfilerRecorderHandle> counters;
        static Dictionary<string, string> units;

        /// <summary>The bytes/count counters of the Memory category this process offers, "name (unit)", sorted. Empty when the list cannot be read.</summary>
        public static List<string> Offered()
        {
            Index();
            var list = new List<string>();
            foreach (var c in counters) list.Add(c.Key + " (" + units[c.Key] + ")");
            list.Sort(StringComparer.Ordinal);
            return list;
        }

        static void Index()
        {
            if (counters != null) return;
            counters = new Dictionary<string, ProfilerRecorderHandle>();
            units = new Dictionary<string, string>();
            try
            {
                var handles = new List<ProfilerRecorderHandle>();
                ProfilerRecorderHandle.GetAvailable(handles);
                foreach (var h in handles)
                {
                    var d = ProfilerRecorderHandle.GetDescription(h);
                    if (d.Category.Name != "Memory" || Array.IndexOf(Skipped, d.Name) >= 0 || counters.ContainsKey(d.Name)) continue;
                    if (d.UnitType == ProfilerMarkerDataUnit.Bytes) { counters[d.Name] = h; units[d.Name] = "bytes"; }
                    else if (d.UnitType == ProfilerMarkerDataUnit.Count) { counters[d.Name] = h; units[d.Name] = "count"; }
                }
            }
            catch (Exception) { /* an Editor without the list reports every recorder as missing */ }
        }

        /// <summary>
        /// Reads the process's memory now. <paramref name="full"/> includes the ProfilerRecorder counters (a recorder is created and disposed per counter);
        /// without it only the API and .NET GC sources are read, which is cheap enough to poll. Names that are not available are added to <paramref name="missing"/>.
        /// </summary>
        public static List<MemoryValue> Read(bool full, List<string> missing = null)
        {
            var values = new List<MemoryValue>(64);
            double gcUsed = -1, totalUsed = -1;
            if (full)
            {
                Index();
                var names = new List<string>(Wanted);
                foreach (var n in counters.Keys) if (!names.Contains(n)) names.Add(n);
                foreach (var name in names)
                {
                    if (!counters.TryGetValue(name, out var handle)) { missing?.Add(name); continue; }
                    try
                    {
                        // SumAllSamplesInFrame is required for any recorder that could be a marker; a counter ignores it. CurrentValue of a counter is its live level.
                        using (var recorder = new RecorderScope(handle))
                        {
                            if (!recorder.Valid) { missing?.Add(name); continue; }
                            var v = (double)recorder.Value;
                            values.Add(new MemoryValue { Name = name, Unit = units[name], Value = v });
                            if (name == "GC Used Memory") gcUsed = v;
                            if (name == "Total Used Memory") totalUsed = v;
                        }
                    }
                    catch (Exception) { missing?.Add(name); }
                }
                if (gcUsed >= 0 && totalUsed >= 0) values.Add(new MemoryValue { Name = "Unity Native (Total Used - GC Used)", Unit = "bytes", Value = Math.Max(0, totalUsed - gcUsed) });
                else missing?.Add("Unity Native (Total Used - GC Used)");
            }
            Api(values, missing, "Mono Used (API)", () => Profiler.GetMonoUsedSizeLong());
            Api(values, missing, "Mono Heap (API)", () => Profiler.GetMonoHeapSizeLong());
            Api(values, missing, "Unity Allocated (API)", () => Profiler.GetTotalAllocatedMemoryLong());
            Api(values, missing, "Unity Reserved (API)", () => Profiler.GetTotalReservedMemoryLong());
            Api(values, missing, "Gfx Driver (API)", () => Profiler.GetAllocatedMemoryForGraphicsDriver());
            Api(values, missing, ".NET GetTotalMemory", () => GC.GetTotalMemory(false));
            DotNet(values, missing);
            return values;
        }

        sealed class RecorderScope : IDisposable
        {
            ProfilerRecorder recorder;
            public RecorderScope(ProfilerRecorderHandle handle)
            {
                recorder = new ProfilerRecorder(handle, 1, ProfilerRecorderOptions.SumAllSamplesInFrame | ProfilerRecorderOptions.StartImmediately | ProfilerRecorderOptions.WrapAroundWhenCapacityReached);
            }
            public bool Valid => recorder.Valid;
            public long Value => recorder.CurrentValue;
            public void Dispose() { if (recorder.Valid) recorder.Dispose(); }
        }

        static void Api(List<MemoryValue> values, List<string> missing, string name, Func<long> read)
        {
            try { values.Add(new MemoryValue { Name = name, Unit = "bytes", Value = read() }); }
            catch (Exception) { missing?.Add(name); }
        }

        // ---- .NET GC, by reflection: the members are in .NET (CoreCLR) but not in Mono's class library ------------------

        static readonly string[] GcInfoProperties = { "HeapSizeBytes", "FragmentedBytes", "TotalCommittedBytes", "PromotedBytes" };
        static readonly string[] GcInfoNames = { ".NET Heap Size", ".NET Fragmented", ".NET Committed", ".NET Promoted" };

        static void DotNet(List<MemoryValue> values, List<string> missing)
        {
            for (var g = 0; g < 3; g++) values.Add(new MemoryValue { Name = ".NET GC Count gen" + g, Unit = "count", Value = GC.CollectionCount(g) });
            var info = Call("GetGCMemoryInfo", Type.EmptyTypes);
            for (var i = 0; i < GcInfoProperties.Length; i++)
            {
                try
                {
                    var p = info?.GetType().GetProperty(GcInfoProperties[i]);
                    if (p == null) { missing?.Add(GcInfoNames[i]); continue; }
                    values.Add(new MemoryValue { Name = GcInfoNames[i], Unit = "bytes", Value = Convert.ToDouble(p.GetValue(info), CultureInfo.InvariantCulture) });
                }
                catch (Exception) { missing?.Add(GcInfoNames[i]); }
            }
            var allocated = Call("GetTotalAllocatedBytes", new[] { typeof(bool) }, false);
            if (allocated != null) values.Add(new MemoryValue { Name = ".NET Total Allocated", Unit = "bytes", Value = Convert.ToDouble(allocated, CultureInfo.InvariantCulture) });
            else missing?.Add(".NET Total Allocated");
            var pause = Call("GetTotalPauseDuration", Type.EmptyTypes);
            if (pause is TimeSpan span) values.Add(new MemoryValue { Name = ".NET GC Pause Total", Unit = "ms", Value = span.TotalMilliseconds });
            else missing?.Add(".NET GC Pause Total");
            // Code that stays loaded after a reload: assemblies in the process, and (CoreCLR) the AssemblyLoadContexts that hold them. A count that grows per reload means old code was not unloaded.
            values.Add(new MemoryValue { Name = "Loaded Assemblies", Unit = "count", Value = AppDomain.CurrentDomain.GetAssemblies().Length });
            var contexts = CountContexts();
            if (contexts >= 0) values.Add(new MemoryValue { Name = "AssemblyLoadContexts", Unit = "count", Value = contexts });
            else missing?.Add("AssemblyLoadContexts");
        }

        static int CountContexts()
        {
            try
            {
                var all = Type.GetType("System.Runtime.Loader.AssemblyLoadContext")?.GetProperty("All", BindingFlags.Public | BindingFlags.Static)?.GetValue(null) as System.Collections.IEnumerable;
                if (all == null) return -1;
                var n = 0;
                foreach (var _ in all) n++;
                return n;
            }
            catch (Exception) { return -1; }
        }

        static object Call(string method, Type[] parameters, params object[] args)
        {
            try { return typeof(GC).GetMethod(method, BindingFlags.Public | BindingFlags.Static, null, parameters, null)?.Invoke(null, args); }
            catch (Exception) { return null; }
        }

        /// <summary>The documented forced collection used before leak readings: two managed collections around the finalizers.</summary>
        public static void CollectGarbage()
        {
            GC.Collect();
            GC.WaitForPendingFinalizers();
            GC.Collect();
        }
    }
}
