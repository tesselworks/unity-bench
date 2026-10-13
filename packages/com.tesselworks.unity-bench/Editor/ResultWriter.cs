using System;
using System.Runtime.InteropServices;
using System.Text;
using Unity.Jobs.LowLevel.Unsafe;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Compilation;
using UnityEngine;

namespace UnityBench.Editor
{
    // Writes the result file documented in docs/adapters.md. Numbers that arrive as strings (sample info) are written as numbers.
    static class ResultWriter
    {
        public static string Build(State s, bool completed)
        {
            var b = new StringBuilder();
            b.Append("{\"schema\":1,\"completed\":").Append(Json.Bool(completed));
            b.Append(",\"error\":").Append(Json.OrNull(s.error));
            b.Append(",\"environment\":").Append(Environment(s));
            b.Append(",\"samples\":[");
            for (var i = 0; i < s.samples.Count; i++)
            {
                var m = s.samples[i];
                b.Append(i > 0 ? "," : "").Append("{\"name\":").Append(Json.Quote(m.name)).Append(",\"phase\":").Append(Json.Quote(m.phase));
                b.Append(",\"round\":").Append(m.round).Append(",\"ms\":").Append(Json.Num(m.ms));
                b.Append(",\"hash\":").Append(Json.OrNull(m.hash));
                b.Append(",\"counters\":{");
                for (var c = 0; c < m.counters.Count; c++) b.Append(c > 0 ? "," : "").Append(Json.Quote(m.counters[c].k)).Append(':').Append(Json.Num(m.counters[c].v));
                b.Append("},\"info\":").Append(Object(m.info)).Append('}');
            }
            b.Append("],\"distributions\":[");
            for (var i = 0; i < s.distributions.Count; i++)
            {
                var d = s.distributions[i];
                b.Append(i > 0 ? "," : "").Append("{\"name\":").Append(Json.Quote(d.name)).Append(",\"phase\":").Append(Json.Quote(d.phase));
                b.Append(",\"hash\":").Append(Json.OrNull(d.hash)).Append(",\"counters\":{");
                for (var c = 0; c < d.counters.Count; c++) b.Append(c > 0 ? "," : "").Append(Json.Quote(d.counters[c].k)).Append(':').Append(Json.Num(d.counters[c].v));
                b.Append("},\"values\":[");
                for (var v = 0; v < d.values.Count; v++) b.Append(v > 0 ? "," : "").Append(Json.Num(d.values[v]));
                b.Append("]}");
            }
            b.Append("],\"notes\":").Append(Object(s.notes)).Append('}');
            return b.ToString();
        }

        static string Object(System.Collections.Generic.List<Pair> pairs)
        {
            var b = new StringBuilder("{");
            for (var i = 0; i < pairs.Count; i++)
            {
                var v = pairs[i].v;
                var value = bool.TryParse(v, out var flag) ? Json.Bool(flag)
                    : double.TryParse(v, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var number) && !double.IsNaN(number) && !double.IsInfinity(number) ? Json.Num(number)
                    : Json.Quote(v);
                b.Append(i > 0 ? "," : "").Append(Json.Quote(pairs[i].k)).Append(':').Append(value);
            }
            return b.Append('}').ToString();
        }

        static string Environment(State s)
        {
            var mono = Type.GetType("Mono.Runtime") != null;
            var b = new StringBuilder("{");
            b.Append("\"unityVersion\":").Append(Json.Quote(Application.unityVersion));
            b.Append(",\"runtime\":").Append(Json.Quote(RuntimeInformation.FrameworkDescription + " (CLR " + System.Environment.Version + ")"));
            b.Append(",\"runtimeKind\":").Append(Json.Quote(mono ? "mono" : "coreclr"));
            b.Append(",\"codeOptimization\":").Append(Json.Quote(CompilationPipeline.codeOptimization.ToString()));
            b.Append(",\"backend\":").Append(Json.Quote(PlayerSettings.GetScriptingBackend(NamedBuildTarget.Standalone).ToString()));
            b.Append(",\"enterPlayModeOptionsEnabled\":").Append(Json.Bool(s.changedPlayOptions ? true : EditorSettings.enterPlayModeOptionsEnabled));
            b.Append(",\"enterPlayModeOptions\":").Append(Json.Quote(s.changedPlayOptions ? EnterPlayModeOptions.DisableDomainReload.ToString() : EditorSettings.enterPlayModeOptions.ToString()));
            b.Append(",\"jobWorkerCount\":").Append(JobsUtility.JobWorkerCount);
            b.Append(",\"processorCount\":").Append(SystemInfo.processorCount);
            b.Append(",\"platform\":").Append(Json.Quote(Application.platform.ToString()));
            b.Append(",\"batchMode\":").Append(Json.Bool(Application.isBatchMode));
            b.Append(",\"graphicsDevice\":").Append(Json.Quote(SystemInfo.graphicsDeviceType.ToString()));
            return b.Append('}').ToString();
        }
    }
}
