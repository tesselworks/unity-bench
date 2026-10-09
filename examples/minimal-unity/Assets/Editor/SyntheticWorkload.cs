using System;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using UnityEditor;
using UnityBench.Editor;

namespace UnityBench.Minimal
{
    [InitializeOnLoad]
    public static class Registration
    {
        static Registration() => Workloads.Register("example.synthetic-fixed-work", () => new SyntheticWorkload());
    }

    // Protocol integration fixture. This is not a representative game, physics,
    // import, reload, build or whole-Editor workload, and provides no speed claim.
    internal sealed class SyntheticWorkload : IWorkload
    {
        private const int Count = 65536;
        private const int Passes = 16;
        private uint[] values;
        private ulong checksum;

        public void Prepare()
        {
            values = new uint[Count];
            for (var i = 0; i < Count; i++) values[i] = unchecked((uint)i * 2654435761U);
        }

        public void Execute()
        {
            ulong total = 0;
            unchecked
            {
                for (var pass = 0; pass < Passes; pass++)
                    for (var i = 0; i < Count; i++)
                    {
                        uint value = values[i] ^ (uint)pass;
                        value ^= value << 13;
                        value ^= value >> 17;
                        value ^= value << 5;
                        total += value;
                    }
            }
            checksum = total;
        }

        public WorkProof CaptureProof()
        {
            // Independently computed integer golden, not a timing threshold.
            if (checksum != 2250143349721856UL)
                throw new InvalidOperationException("Synthetic work produced an unexpected checksum.");
            return new WorkProof
            {
                InputFingerprint = HashInputs(),
                OutputFingerprint = Hash(checksum.ToString(CultureInfo.InvariantCulture)),
                WorkCounters = new Dictionary<string, double>
                {
                    ["elements"] = Count,
                    ["passes"] = Passes,
                    ["processedElements"] = Count * Passes
                }
            };
        }

        private string HashInputs()
        {
            // Canonical consumed inputs: uint32 little-endian Count, Passes,
            // then every actual array element. This is outside Execute's clock.
            var bytes = new byte[8 + values.Length * 4];
            WriteUInt32(bytes, 0, (uint)Count);
            WriteUInt32(bytes, 4, (uint)Passes);
            for (var i = 0; i < values.Length; i++) WriteUInt32(bytes, 8 + i * 4, values[i]);
            using (var sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
        }

        private static void WriteUInt32(byte[] bytes, int offset, uint value)
        {
            bytes[offset] = unchecked((byte)value);
            bytes[offset + 1] = unchecked((byte)(value >> 8));
            bytes[offset + 2] = unchecked((byte)(value >> 16));
            bytes[offset + 3] = unchecked((byte)(value >> 24));
        }

        private static string Hash(string value)
        {
            using (var sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(value))).Replace("-", "").ToLowerInvariant();
        }
    }
}
