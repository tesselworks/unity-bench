using System;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using UnityEngine;

namespace UnityBench.Player
{
    // Tiny JSON writer for the result file. JsonUtility cannot write dictionaries or real nulls.
    static class ResultJson
    {
        public static string Quote(string v)
        {
            if (v == null) return "null";
            var b = new StringBuilder("\"");
            foreach (var c in v)
            {
                switch (c)
                {
                    case '\\': b.Append("\\\\"); break;
                    case '"': b.Append("\\\""); break;
                    case '\n': b.Append("\\n"); break;
                    case '\r': b.Append("\\r"); break;
                    case '\t': b.Append("\\t"); break;
                    default: if (c < 0x20) b.Append("\\u").Append(((int)c).ToString("x4")); else b.Append(c); break;
                }
            }
            return b.Append('"').ToString();
        }

        public static string Num(double v) => double.IsNaN(v) || double.IsInfinity(v) ? "null" : v.ToString("R", CultureInfo.InvariantCulture);
        public static string Bool(bool v) => v ? "true" : "false";
        public static string OrNull(string v) => string.IsNullOrEmpty(v) ? "null" : Quote(v);

        // What this player is, so a report can say which runtime produced the numbers.
        public static string Describe()
        {
            var backend =
#if ENABLE_IL2CPP
                "il2cpp";
#elif ENABLE_MONO
                "mono";
#else
                "coreclr";
#endif
            var b = new StringBuilder("{");
            b.Append("\"unityVersion\":").Append(Quote(Application.unityVersion));
            b.Append(",\"runtime\":").Append(Quote(RuntimeInformation.FrameworkDescription.Replace("\"", "'")));
            b.Append(",\"runtimeKind\":").Append(Quote(backend));
            b.Append(",\"backend\":").Append(Quote(backend));
            b.Append(",\"codeOptimization\":").Append(Quote(Debug.isDebugBuild ? "Development build" : "Release build"));
            b.Append(",\"development\":").Append(Bool(Debug.isDebugBuild));
            b.Append(",\"platform\":").Append(Quote(Application.platform.ToString()));
            b.Append(",\"batchMode\":").Append(Bool(Application.isBatchMode));
            b.Append(",\"processorCount\":").Append(SystemInfo.processorCount);
            b.Append(",\"cpu\":").Append(Quote(SystemInfo.processorType));
            b.Append(",\"gpu\":").Append(Quote(SystemInfo.graphicsDeviceName));
            b.Append(",\"graphicsDevice\":").Append(Quote(SystemInfo.graphicsDeviceType.ToString()));
            b.Append(",\"memoryMb\":").Append(SystemInfo.systemMemorySize);
            return b.Append('}').ToString();
        }
    }
}
