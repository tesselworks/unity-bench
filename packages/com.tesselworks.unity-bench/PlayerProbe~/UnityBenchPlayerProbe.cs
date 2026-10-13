// Player-side half of player.startup. The runner copies this folder into the project's Assets/ for campaigns that use
// player.* workloads (the package itself stays Editor-only). It does nothing unless the player is launched with
//   -unityBenchSignal <path>
// in which case it writes <path> once the first frame has been completed, and quits.
using System;
using System.IO;
using System.Runtime.InteropServices;
using UnityEngine;

namespace UnityBench.Player
{
    public sealed class UnityBenchPlayerProbe : MonoBehaviour
    {
        string signalPath;
        int startFrame;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        static void Boot()
        {
            var args = Environment.GetCommandLineArgs();
            var i = Array.IndexOf(args, "-unityBenchSignal");
            if (i < 0 || i + 1 >= args.Length) return;
            var go = new GameObject("UnityBenchPlayerProbe");
            DontDestroyOnLoad(go);
            go.AddComponent<UnityBenchPlayerProbe>().signalPath = args[i + 1];
        }

        void Start() { startFrame = Time.frameCount; }

        void Update()
        {
            if (Time.frameCount <= startFrame + 1) return;   // the first full frame has completed
            enabled = false;
            var backend =
#if ENABLE_IL2CPP
                "il2cpp";
#elif ENABLE_MONO
                "mono";
#else
                "coreclr";
#endif
            var json = "{\"schema\":1,\"environment\":{\"unityVersion\":\"" + Application.unityVersion + "\",\"runtime\":\"" +
                       RuntimeInformation.FrameworkDescription.Replace("\"", "'") + "\",\"backend\":\"" + backend + "\",\"development\":" +
                       (Debug.isDebugBuild ? "true" : "false") + ",\"platform\":\"" + Application.platform + "\"},\"frame\":" + Time.frameCount +
                       ",\"realtimeSinceStartupMs\":" + (Time.realtimeSinceStartupAsDouble * 1000.0).ToString("R", System.Globalization.CultureInfo.InvariantCulture) + "}";
            var temp = signalPath + ".tmp";
            File.WriteAllText(temp, json);
            File.Move(temp, signalPath);   // the move is the signal; the runner reads its modification time
            Application.Quit();
        }
    }
}
