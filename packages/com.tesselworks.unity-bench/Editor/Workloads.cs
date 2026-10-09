using System;
using System.Collections.Generic;

namespace UnityBench.Editor
{
    // Preparation and proof computation are outside the measured Execute boundary.
    // Each Execute must perform the same frozen work, without progressively reducing it.
    public interface IWorkload
    {
        void Prepare();
        void Execute();
        WorkProof CaptureProof();
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
