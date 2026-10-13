// Runs a player.adapter entry point inside the built player, writes the result file (docs/adapters.md) and quits.
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace UnityBench.Player
{
    sealed class AdapterRunner : MonoBehaviour
    {
        [Serializable] class Pair { public string k, v; }
        [Serializable]
        class Request
        {
            public string workload = "", variant = "";
            public int rounds = 1;
            public List<Pair> args = new List<Pair>();
        }

        string method, outPath, requestArg;
        PlayerContext context;

        internal static void Attach(string method, string outPath, string requestArg)
        {
            // A player in the background must keep running, or an unfocused window would pause the measurement.
            Application.runInBackground = true;
            var go = new GameObject("UnityBenchAdapterRunner");
            DontDestroyOnLoad(go);
            var runner = go.AddComponent<AdapterRunner>();
            runner.method = method; runner.outPath = outPath; runner.requestArg = requestArg;
        }

        // Unity runs Start as a coroutine. The adapter's own coroutine (and any it yields) is pumped here, so the result is written
        // in the same frame the adapter finishes in, even if the adapter's tool asked the player to quit.
        IEnumerator Start()
        {
            Exception failure = null;
            IEnumerator routine = null;
            try
            {
                context = NewContext();
                routine = Begin(Find(method));
            }
            catch (Exception e) { failure = Unwrap(e); }
            var stack = new Stack<IEnumerator>();
            if (routine != null) stack.Push(routine);
            while (stack.Count > 0)
            {
                object current = null;
                bool moved;
                try
                {
                    moved = stack.Peek().MoveNext();
                    if (moved) current = stack.Peek().Current;
                }
                catch (Exception e) { failure = Unwrap(e); break; }
                if (!moved) { stack.Pop(); continue; }
                if (current is IEnumerator nested) stack.Push(nested); else yield return current;
            }
            Finish(failure);
        }

        void Update() { context?.Tick(); }

        PlayerContext NewContext()
        {
            if (string.IsNullOrEmpty(outPath)) throw new InvalidOperationException("-unityBenchOut <result.json> is required");
            var request = new Request();
            if (!string.IsNullOrEmpty(requestArg))
            {
                var text = requestArg.TrimStart().StartsWith("{") ? requestArg : File.ReadAllText(requestArg);
                request = JsonUtility.FromJson<Request>(text) ?? request;
            }
            var args = new Dictionary<string, string>();
            foreach (var p in request.args) args[p.k] = p.v;
            return new PlayerContext(request.workload, request.variant, request.rounds, args);
        }

        IEnumerator Begin(MethodInfo adapter)
        {
            var result = adapter.Invoke(null, new object[] { context });
            return result as IEnumerator ?? Done();
        }

        static IEnumerator Done() { yield break; }

        static Exception Unwrap(Exception e) => e is TargetInvocationException && e.InnerException != null ? e.InnerException : e;

        // Assembly by assembly: Type.GetType(name) alone would only look in the calling assembly.
        static MethodInfo Find(string name)
        {
            var dot = name.LastIndexOf('.');
            var typeName = name.Substring(0, dot);
            var methodName = name.Substring(dot + 1);
            foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                var type = assembly.GetType(typeName, false);
                var found = type?.GetMethod(methodName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static, null, new[] { typeof(PlayerContext) }, null);
                if (found != null) return found;
            }
            throw new MissingMethodException("adapter not found: " + name + "(UnityBench.Player.PlayerContext). Is the type [Preserve]d and in a player assembly?");
        }

        void Finish(Exception failure)
        {
            var code = failure == null ? 0 : 1;
            try
            {
                context?.StopRecorders();
                var temp = outPath + ".tmp";
                File.WriteAllText(temp, Build(failure));
                if (File.Exists(outPath)) File.Delete(outPath);
                File.Move(temp, outPath);
            }
            catch (Exception e) { Debug.LogException(e); code = 3; }
            Application.Quit(code);
        }

        string Build(Exception failure)
        {
            var b = new StringBuilder();
            b.Append("{\"schema\":1,\"completed\":").Append(ResultJson.Bool(failure == null));
            b.Append(",\"error\":").Append(ResultJson.OrNull(failure?.ToString()));
            b.Append(",\"environment\":").Append(ResultJson.Describe());
            b.Append(",\"samples\":[");
            var samples = context?.Samples ?? new List<PlayerContext.Sample>();
            for (var i = 0; i < samples.Count; i++)
            {
                var s = samples[i];
                b.Append(i > 0 ? "," : "").Append("{\"name\":").Append(ResultJson.Quote(s.Name)).Append(",\"phase\":").Append(ResultJson.OrNull(s.Phase));
                b.Append(",\"round\":").Append(s.Round).Append(",\"ms\":").Append(ResultJson.Num(s.Ms));
                b.Append(",\"hash\":").Append(ResultJson.OrNull(s.Hash)).Append(",\"workGroup\":").Append(ResultJson.OrNull(s.WorkGroup)).Append(",\"counters\":").Append(Counters(s.Counters)).Append('}');
            }
            b.Append("],\"distributions\":[");
            var distributions = context?.Distributions ?? new List<PlayerContext.Distribution>();
            for (var i = 0; i < distributions.Count; i++)
            {
                var d = distributions[i];
                b.Append(i > 0 ? "," : "").Append("{\"name\":").Append(ResultJson.Quote(d.Name)).Append(",\"phase\":").Append(ResultJson.Quote(d.Phase));
                b.Append(",\"hash\":").Append(ResultJson.OrNull(d.Hash)).Append(",\"workGroup\":").Append(ResultJson.OrNull(d.WorkGroup)).Append(",\"counters\":").Append(Counters(d.Counters)).Append(",\"values\":[");
                for (var v = 0; v < d.Values.Length; v++) b.Append(v > 0 ? "," : "").Append(ResultJson.Num(d.Values[v]));
                b.Append("]}");
            }
            b.Append("],\"workGroups\":{");
            var groups = context?.WorkGroups ?? new List<PlayerContext.WorkGroup>();
            for (var i = 0; i < groups.Count; i++)
                b.Append(i > 0 ? "," : "").Append(ResultJson.Quote(groups[i].Name)).Append(":{\"hash\":").Append(ResultJson.OrNull(groups[i].Hash))
                    .Append(",\"counters\":").Append(Counters(groups[i].Counters)).Append('}');
            b.Append("},\"headline\":[");
            var headline = context?.HeadlineItems ?? new List<string>();
            for (var i = 0; i < headline.Count; i++) b.Append(i > 0 ? "," : "").Append(ResultJson.Quote(headline[i]));
            b.Append("],\"notes\":{");
            var notes = context?.Notes ?? new List<KeyValuePair<string, string>>();
            for (var i = 0; i < notes.Count; i++) b.Append(i > 0 ? "," : "").Append(ResultJson.Quote(notes[i].Key)).Append(':').Append(ResultJson.Quote(notes[i].Value));
            return b.Append("}}").ToString();
        }

        static string Counters(Dictionary<string, double> counters)
        {
            var b = new StringBuilder("{");
            if (counters != null)
            {
                var first = true;
                foreach (var c in counters) { b.Append(first ? "" : ",").Append(ResultJson.Quote(c.Key)).Append(':').Append(ResultJson.Num(c.Value)); first = false; }
            }
            return b.Append('}').ToString();
        }
    }
}
