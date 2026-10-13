// Opt-in entry of the player half. Inert unless the player was started with one of
//   -unityBenchAdapter <Namespace.Type.Method> -unityBenchOut <result.json> [-unityBenchArgs <request.json | {json}>]
//   -unityBenchSignal <path>
// so a package that is in the project but not driven by the runner changes nothing in a build.
using System;
using UnityEngine;

namespace UnityBench.Player
{
    public static partial class PlayerBoot
    {
        public static string Arg(string name)
        {
            var args = Environment.GetCommandLineArgs();
            var i = Array.IndexOf(args, name);
            return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
        }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        static void Boot()
        {
            if (Application.isEditor) return;
            var signal = Arg("-unityBenchSignal");
            if (signal != null) UnityBenchPlayerProbe.Attach(signal);
            var adapter = Arg("-unityBenchAdapter");
            if (adapter != null) AdapterRunner.Attach(adapter, Arg("-unityBenchOut"), Arg("-unityBenchArgs"));
        }
    }
}
