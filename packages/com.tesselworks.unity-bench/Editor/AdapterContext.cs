using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;

namespace UnityBench.Editor
{
    /// <summary>What a piece of work did, so the report can tell whether two variants did the same work.</summary>
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
    /// Passed to an <c>editor.adapter</c> entry point: <c>public static void Run(AdapterContext ctx)</c>.
    /// The adapter measures its own work with <see cref="Measure"/> and the framework writes the result file.
    /// </summary>
    public sealed class AdapterContext
    {
        readonly Request request;
        readonly List<SampleRecord> samples;
        readonly List<DistributionRecord> distributions;
        readonly List<WorkGroupRecord> workGroups;
        readonly List<string> headline;
        readonly List<Pair> notes;
        readonly Dictionary<string, int> seen = new Dictionary<string, int>();

        internal AdapterContext(Request request, List<SampleRecord> samples, List<DistributionRecord> distributions,
            List<WorkGroupRecord> workGroups, List<string> headline, List<Pair> notes)
        {
            this.request = request; this.samples = samples; this.distributions = distributions;
            this.workGroups = workGroups; this.headline = headline; this.notes = notes;
        }

        /// <summary>The <c>rounds</c> value from the campaign. The adapter decides what a round is.</summary>
        public int Rounds => request.rounds;
        public string Variant => request.variant;

        /// <summary>A value from the workload's <c>args</c> table, or <paramref name="fallback"/>.</summary>
        public string Arg(string key, string fallback = null)
        {
            foreach (var p in request.args) if (p.k == key) return p.v;
            return fallback;
        }

        /// <summary>
        /// Times <paramref name="work"/> (the stopwatch covers only the call) and records one sample.
        /// The sample is untagged, so the runner derives its phase: first-use for the first of a name measured more than once,
        /// warm after that, single when the name is measured once.
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
            var sample = new SampleRecord { name = name, ms = ms, round = round, phase = phase ?? "", hash = work?.Hash ?? "", workGroup = workGroup ?? "" };
            if (work != null) foreach (var c in work.Counters) sample.counters.Add(new Number { k = c.Key, v = c.Value });
            samples.Add(sample);
        }

        /// <summary>
        /// Reports many per-frame (or per-operation) times for one phase, in milliseconds. The runner stores p50, p95, p99 and max of
        /// this process as the items "name phase p50" and so on. Use different phase names (warmup, steady) to see them apart.
        /// </summary>
        public void AddDistribution(string name, string phase, IEnumerable<double> values, WorkResult work = null, string workGroup = null)
        {
            var record = new DistributionRecord { name = name, phase = phase, hash = work?.Hash ?? "", workGroup = workGroup ?? "" };
            record.values.AddRange(values);
            if (work != null) foreach (var c in work.Counters) record.counters.Add(new Number { k = c.Key, v = c.Value });
            distributions.Add(record);
        }

        /// <summary>
        /// Registers (or replaces) a named work identity: a hash and counters. Samples and distributions added with this
        /// <c>workGroup</c> name share it, so metrics derived from one run inherit the run's work identity.
        /// </summary>
        public void DefineWorkGroup(string name, WorkResult work)
        {
            workGroups.RemoveAll(g => g.name == name);
            var group = new WorkGroupRecord { name = name, hash = work?.Hash ?? "" };
            if (work != null) foreach (var c in work.Counters) group.counters.Add(new Number { k = c.Key, v = c.Value });
            workGroups.Add(group);
        }

        /// <summary>
        /// Declares the headline items of this workload as item-name patterns (shell-style <c>*</c> and <c>?</c>, matched by the runner
        /// against final item names such as "frame steady p50").
        /// </summary>
        public void Headline(params string[] patterns)
        {
            foreach (var p in patterns) if (!string.IsNullOrEmpty(p)) headline.Add(p);
        }

        /// <summary>A free-form annotation for the whole process (shown in raw results, never compared).</summary>
        public void Note(string key, string value) => notes.Add(new Pair { k = key, v = value });
    }
}
