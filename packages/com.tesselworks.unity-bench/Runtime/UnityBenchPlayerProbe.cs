// Player-side half of player.startup. Started by PlayerBoot only when the player was launched with
//   -unityBenchSignal <path>
// It writes <path> once the first frame has been completed, and quits.
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

        internal static void Attach(string signalPath)
        {
            var go = new GameObject("UnityBenchPlayerProbe");
            DontDestroyOnLoad(go);
            go.AddComponent<UnityBenchPlayerProbe>().signalPath = signalPath;
        }

        void Start() { startFrame = Time.frameCount; }

        void Update()
        {
            if (Time.frameCount <= startFrame + 1) return;   // the first full frame has completed
            enabled = false;
            var json = "{\"schema\":1,\"environment\":" + ResultJson.Describe() + ",\"frame\":" + Time.frameCount +
                       ",\"realtimeSinceStartupMs\":" + ResultJson.Num(Time.realtimeSinceStartupAsDouble * 1000.0) + "}";
            var temp = signalPath + ".tmp";
            File.WriteAllText(temp, json);
            File.Move(temp, signalPath);   // the move is the signal; the runner reads its modification time
            Application.Quit();
        }
    }
}
