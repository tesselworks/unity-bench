// Hooks for game code: tell a Unity Bench scene workload what work the scene did and time its own segments.
// Every call is a no-op unless the process was started by unity-bench (a player run with -unityBenchAdapter, or an Editor run with
// -unityBenchRequest), so the calls can stay in the game.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;

namespace UnityBench.Player
{
    /// <summary>
    /// Optional calls from game code that turn a scene workload (<c>player.scene</c>, <c>editor.scene_play</c>) from "frames and markers" into
    /// "frames and markers for verified work": <see cref="Mark"/> and <see cref="Hash"/> say what the scene did, so the report can tell
    /// whether two variants did the same work; <see cref="Begin"/> and <see cref="End"/> time a piece of the game's own code.
    /// </summary>
    public static class Bench
    {
        static readonly bool enabled = Detect();
        static readonly object gate = new object();
        static readonly SortedDictionary<string, double> marks = new SortedDictionary<string, double>(StringComparer.Ordinal);
        static readonly SortedDictionary<string, string> hashes = new SortedDictionary<string, string>(StringComparer.Ordinal);
        static readonly Dictionary<string, Stack<long>> open = new Dictionary<string, Stack<long>>();
        static readonly Dictionary<string, List<double>> segments = new Dictionary<string, List<double>>();
        static volatile bool recording, steady;   // the scene workload is running / is in its steady phase

        static bool Detect()
        {
            foreach (var a in Environment.GetCommandLineArgs())
                if (a == "-unityBenchAdapter" || a == "-unityBenchRequest") return true;
            return false;
        }

        /// <summary>
        /// Reports a work counter, for example the number of bodies simulated. The last value reported under a name is the one that counts.
        /// The report compares counters between variants: equal counters read "same", different ones "work differs". A counter that
        /// depends on how many frames ran is only comparable when the workload runs a fixed number of frames (<c>duration_frames</c>).
        /// </summary>
        public static void Mark(string counter, double value)
        {
            if (!enabled || string.IsNullOrEmpty(counter) || double.IsNaN(value) || double.IsInfinity(value)) return;
            lock (gate) marks[counter] = value;
        }

        /// <summary>Reports an output fingerprint under a name (any stable text). The last value under a name counts; all names together form the scene's hash.</summary>
        public static void Hash(string name, string value)
        {
            if (!enabled || string.IsNullOrEmpty(name) || value == null) return;
            lock (gate) hashes[name] = value;
        }

        /// <summary>Reports the SHA-256 of some output bytes under a name.</summary>
        public static void Hash(string name, byte[] data)
        {
            if (!enabled || data == null) return;
            Hash(name, WorkResult.Sha256Hex(data));
        }

        /// <summary>
        /// Starts timing a segment of the game's own code. Calls with the same name may nest or repeat; each Begin/End pair is one call.
        /// Pairs that end during the steady phase are reported as <c>segment NAME steady p50/p95/p99/max</c>, one value per call, in ms.
        /// </summary>
        public static void Begin(string segment)
        {
            if (!enabled || !recording || string.IsNullOrEmpty(segment)) return;
            var now = Stopwatch.GetTimestamp();
            lock (gate)
            {
                if (!open.TryGetValue(segment, out var stack)) open[segment] = stack = new Stack<long>();
                stack.Push(now);
            }
        }

        /// <summary>Ends the innermost open <see cref="Begin"/> of this name. An End without a Begin is ignored.</summary>
        public static void End(string segment)
        {
            if (!enabled || !recording || string.IsNullOrEmpty(segment)) return;
            var now = Stopwatch.GetTimestamp();
            lock (gate)
            {
                if (!open.TryGetValue(segment, out var stack) || stack.Count == 0) return;
                var ms = (now - stack.Pop()) * 1000.0 / Stopwatch.Frequency;
                if (!steady) return;
                if (!segments.TryGetValue(segment, out var list)) segments[segment] = list = new List<double>();
                list.Add(ms);
            }
        }

        // ---- the scene workload's side ---------------------------------------------------------------------------

        internal static void StartRecording() { lock (gate) { open.Clear(); segments.Clear(); } recording = true; }
        internal static void SetSteady(bool value) { steady = value; }
        internal static void StopRecording() { recording = false; }

        internal sealed class Snapshot
        {
            public Dictionary<string, double> Marks = new Dictionary<string, double>();
            public string Hash;
            public SortedDictionary<string, List<double>> Segments = new SortedDictionary<string, List<double>>(StringComparer.Ordinal);
        }

        internal static Snapshot Take()
        {
            var s = new Snapshot();
            lock (gate)
            {
                foreach (var m in marks) s.Marks[m.Key] = m.Value;
                if (hashes.Count > 0)
                {
                    var text = new StringBuilder();
                    foreach (var h in hashes) text.Append(h.Key).Append('=').Append(h.Value).Append('\n');
                    s.Hash = WorkResult.Sha256Hex(text.ToString());
                }
                foreach (var g in segments) s.Segments[g.Key] = new List<double>(g.Value);
            }
            return s;
        }
    }
}
