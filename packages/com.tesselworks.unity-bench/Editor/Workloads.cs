using System;
using System.Collections.Generic;

namespace UnityBench.Editor
{
    // Prepare and proof computation are outside the framework Execute boundary.
    // Optional consumer metrics may define narrower operation clocks within Execute.
    // Each Execute must perform the same frozen work, without progressively reducing it.
    public interface IWorkload
    {
        void Prepare();
        void Execute();
        WorkProof CaptureProof();
    }

    // Optional consumer preparation per iteration, before any Execute stopwatch.
    // Prepare() remains the once-per-process setup boundary for all existing workloads.
    public interface IPreparedIterationWorkload : IWorkload
    {
        void PrepareIteration(int iteration);
    }

    // Consumer clocks must finish before metrics and proof are captured.
    public interface IWorkloadMetrics : IWorkload
    {
        string AggregateMetric { get; }
        IReadOnlyDictionary<string, string> MetricScopes { get; }
        IReadOnlyDictionary<string, double> CaptureMetrics();
    }

    // Complete diagnostic work remains observable, but cannot authorize a ratio.
    public interface IWorkloadValidity : IWorkload
    {
        bool IsValid { get; }
    }

    public sealed class WorkProof
    {
        public string InputFingerprint;
        public string OutputFingerprint;
        public Dictionary<string, double> WorkCounters = new Dictionary<string, double>();
    }

    public static class Workloads
    {
        private static readonly Dictionary<string, Func<IWorkload>> Factories =
            new Dictionary<string, Func<IWorkload>>(StringComparer.Ordinal);

        // Register from an InitializeOnLoad constructor in the consuming project.
        public static void Register(string id, Func<IWorkload> factory)
        {
            if (string.IsNullOrWhiteSpace(id) || factory == null)
                throw new ArgumentException("A workload requires an ID and factory.");
            if (Factories.ContainsKey(id))
                throw new InvalidOperationException("Duplicate workload registration: " + id);
            Factories.Add(id, factory);
        }

        internal static IWorkload Create(string id)
        {
            if (!Factories.TryGetValue(id, out var factory))
                throw new InvalidOperationException("The requested workload is not registered.");
            return factory() ?? throw new InvalidOperationException("The workload factory returned null.");
        }
    }
}
