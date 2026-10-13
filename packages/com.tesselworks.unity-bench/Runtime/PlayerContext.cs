using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;

namespace UnityBench.Player
{
    /// <summary>What a piece of work did, so the report can tell whether two variants did the same work. Same meaning as the Editor's WorkResult.</summary>
    public sealed class WorkResult
    {
        /// <summary>Hash of the output (any stable string). Compared across variants and repeats; a difference is flagged, never blocks.</summary>
        public string Hash;
        /// <summary>Work counters, for example items processed. Compared like the hash.</summary>
        public Dictionary<string, double> Counters = new Dictionary<string, double>();

        public WorkResult(string hash = null) { Hash = hash; }

        public WorkResult Count(string name, double value) { Counters[name] = value; return this; }

        public static string Sha256Hex(byte[] bytes)
        {
            using (var sha = SHA256.Create())
            {
                var b = new StringBuilder();
                foreach (var x in sha.ComputeHash(bytes)) b.Append(x.ToString("x2"));
                return b.ToString();
            }
        }

        public static string Sha256Hex(string text) => Sha256Hex(Encoding.UTF8.GetBytes(text));
    }

    /// <summary>
    /// Records the wall time between consecutive frames until <see cref="Stop"/>, then reports them as one distribution
    /// (the runner reduces it to p50, p95, p99 and max per process). Frame time here is the real time from one frame's Update to the
    /// next, whatever <c>Time.timeScale</c> or <c>Time.captureDeltaTime</c> say.
    /// </summary>
    public sealed class FrameRecorder
    {
        readonly PlayerContext context;
        readonly string name, phase, workGroup;
        readonly List<double> values = new List<double>(4096);
        long last = -1;
        bool stopped;

        internal FrameRecorder(PlayerContext context, string name, string phase, string workGroup)
        {
            this.context = context; this.name = name; this.phase = phase; this.workGroup = workGroup;
        }

        public int Count => values.Count;

        internal void Tick(long now)
        {
            if (last >= 0) values.Add((now - last) * 1000.0 / Stopwatch.Frequency);
            last = now;
        }

        /// <summary>Ends recording and reports what was recorded. Safe to call more than once.</summary>
        public void Stop()
        {
            if (stopped) return;
            stopped = true;
            context.Release(this);
            if (values.Count > 0) context.AddDistribution(name, phase, values, null, workGroup);
        }
    }

    /// <summary>
    /// Passed to a <c>player.adapter</c> entry point: <c>public static void Run(PlayerContext ctx)</c> or, to run over many frames,
    /// <c>public static IEnumerator Run(PlayerContext ctx)</c>. The adapter reports through the context; the runtime writes the result
    /// file and quits the player when the adapter returns or its coroutine ends. Mark the adapter type and method
    /// <c>[UnityEngine.Scripting.Preserve]</c> so IL2CPP stripping keeps them.
    /// </summary>
    public sealed class PlayerContext
    {
        internal sealed class Sample
        {
            public string Name, Phase, Hash, WorkGroup;
            public double Ms;
            public int Round;
            public Dictionary<string, double> Counters;
        }

        internal sealed class Distribution
        {
            public string Name, Phase, Hash, WorkGroup;
            public double[] Values;
            public Dictionary<string, double> Counters;
        }

        internal sealed class WorkGroup
        {
            public string Name, Hash;
            public Dictionary<string, double> Counters;
        }

        readonly Dictionary<string, string> args;
        readonly Dictionary<string, int> seen = new Dictionary<string, int>();
        readonly List<FrameRecorder> recorders = new List<FrameRecorder>();
        internal readonly List<Sample> Samples = new List<Sample>();
        internal readonly List<Distribution> Distributions = new List<Distribution>();
        internal readonly List<WorkGroup> WorkGroups = new List<WorkGroup>();
        internal readonly List<string> HeadlineItems = new List<string>();
        internal readonly List<KeyValuePair<string, string>> Notes = new List<KeyValuePair<string, string>>();

        internal PlayerContext(string workload, string variant, int rounds, Dictionary<string, string> args)
        {
            Workload = workload; Variant = variant; Rounds = rounds; this.args = args;
        }

        public string Workload { get; }
        public string Variant { get; }
        /// <summary>The <c>rounds</c> value from the campaign. The adapter decides what a round is.</summary>
        public int Rounds { get; }

        /// <summary>A value from the workload's <c>args</c> table, or <paramref name="fallback"/>.</summary>
        public string Arg(string key, string fallback = null) => args.TryGetValue(key, out var value) ? value : fallback;

        /// <summary>
        /// Times <paramref name="work"/> (the stopwatch covers only the call) and records one sample. The sample is untagged, so the runner
        /// derives its phase: first-use for the first of a name measured more than once, warm after that, single when measured once.
        /// </summary>
        public WorkResult Measure(string name, Func<WorkResult> work, string workGroup = null)
        {
            var timer = Stopwatch.StartNew();
            var result = work();
            timer.Stop();
            Add(name, timer.Elapsed.TotalMilliseconds, result, null, workGroup);
            return result;
        }

        /// <summary>
        /// Records a sample whose time you measured yourself. Without a <paramref name="phase"/> the runner derives it (first-use, warm
        /// or single, see <see cref="Measure"/>); an adapter may tag a sample "first-use", "warm", "warmup" or "steady" explicitly.
        /// <paramref name="workGroup"/> names a group from <see cref="DefineWorkGroup"/> that derived metrics of this run inherit.
        /// </summary>
        public void Add(string name, double ms, WorkResult work = null, string phase = null, string workGroup = null)
        {
            seen.TryGetValue(name, out var round);
            seen[name] = round + 1;
            Samples.Add(new Sample { Name = name, Ms = ms, Round = round, Phase = phase ?? "", Hash = work?.Hash, Counters = work?.Counters, WorkGroup = workGroup });
        }

        /// <summary>
        /// Reports many per-frame (or per-operation) times for one phase, in milliseconds. The runner stores p50, p95, p99 and max of this
        /// process as the items "name phase p50" and so on. Use different phase names (for example warmup and steady) to see them apart.
        /// </summary>
        public void AddDistribution(string name, string phase, IEnumerable<double> values, WorkResult work = null, string workGroup = null)
        {
            Distributions.Add(new Distribution { Name = name, Phase = phase, Values = new List<double>(values).ToArray(), Hash = work?.Hash, Counters = work?.Counters, WorkGroup = workGroup });
        }

        public void AddDistribution(string name, string phase, IEnumerable<float> values, WorkResult work = null, string workGroup = null)
        {
            var list = new List<double>();
            foreach (var v in values) list.Add(v);
            AddDistribution(name, phase, list, work, workGroup);
        }

        /// <summary>Starts recording frame times; call <see cref="FrameRecorder.Stop"/> to end the phase and report it.</summary>
        public FrameRecorder RecordFrames(string name, string phase, string workGroup = null)
        {
            var recorder = new FrameRecorder(this, name, phase, workGroup);
            recorders.Add(recorder);
            return recorder;
        }

        /// <summary>
        /// Registers (or replaces) a named work identity: a hash and counters. Samples and distributions added with this
        /// <c>workGroup</c> name share it, so metrics derived from one run inherit the run's work identity.
        /// </summary>
        public void DefineWorkGroup(string name, WorkResult work)
        {
            WorkGroups.RemoveAll(g => g.Name == name);
            WorkGroups.Add(new WorkGroup { Name = name, Hash = work?.Hash, Counters = work == null ? null : new Dictionary<string, double>(work.Counters) });
        }

        /// <summary>
        /// Declares the headline items of this workload as item-name patterns (shell-style <c>*</c> and <c>?</c>, matched by the runner
        /// against final item names such as "frame steady p50").
        /// </summary>
        public void Headline(params string[] patterns)
        {
            foreach (var p in patterns) if (!string.IsNullOrEmpty(p)) HeadlineItems.Add(p);
        }

        /// <summary>A free-form annotation for the whole process (shown in raw results, never compared).</summary>
        public void Note(string key, string value) => Notes.Add(new KeyValuePair<string, string>(key, value));

        internal void Release(FrameRecorder recorder) => recorders.Remove(recorder);

        internal void Tick()
        {
            if (recorders.Count == 0) return;
            var now = Stopwatch.GetTimestamp();
            for (var i = 0; i < recorders.Count; i++) recorders[i].Tick(now);
        }

        internal void StopRecorders()
        {
            while (recorders.Count > 0) recorders[recorders.Count - 1].Stop();
        }
    }
}
