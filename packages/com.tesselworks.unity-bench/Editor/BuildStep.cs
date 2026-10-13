using System;
using System.Diagnostics;
using System.IO;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace UnityBench.Editor
{
    // player.build_time: one BuildPipeline.BuildPlayer per round, timed with a stopwatch around the call.
    //   clean:       output folder deleted and the build cache cleaned before every round
    //   incremental: round 0 is a full build (first-use); later rounds rebuild the same output with nothing changed
    public static partial class BatchEntry
    {
        const string PlayerName = "UnityBenchPlayer";

        static BuildTarget HostTarget()
        {
            var requested = s.req.buildTarget;
            if (!string.IsNullOrEmpty(requested)) return (BuildTarget)Enum.Parse(typeof(BuildTarget), requested, true);
            switch (Application.platform)
            {
                case RuntimePlatform.OSXEditor: return BuildTarget.StandaloneOSX;
                case RuntimePlatform.WindowsEditor: return BuildTarget.StandaloneWindows64;
                default: return BuildTarget.StandaloneLinux64;
            }
        }

        static void PreparePlayerSettings()
        {
            var backend = s.req.backend;
            if (string.IsNullOrEmpty(backend)) return;
            // Looked up by name so one source file compiles on editors that do not have every ScriptingImplementation.
            var name = backend == "mono" ? "Mono2x" : backend == "il2cpp" ? "IL2CPP" : "CoreCLR";
            if (!Enum.TryParse(name, true, out ScriptingImplementation implementation))
                throw new NotSupportedException("scripting backend " + backend + " is not available in Unity " + Application.unityVersion);
            PlayerSettings.SetScriptingBackend(NamedBuildTarget.Standalone, implementation);
        }

        static void BuildStep(Step step)
        {
            if (!Idle) return;
            Begin(step);
            var output = s.req.outputDir;
            var location = Path.Combine(output, PlayerName + PlayerExtension());
            var clean = s.req.buildMode == "clean" || step.round == 0;
            if (clean && Directory.Exists(output)) Directory.Delete(output, true);
            Directory.CreateDirectory(output);
            var options = BuildOptions.None;
            if (s.req.development) options |= BuildOptions.Development;
            if (s.req.buildMode == "clean") options |= BuildOptions.CleanBuildCache;
            var timer = Stopwatch.StartNew();
            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = new[] { s.scenePath }, locationPathName = location, target = HostTarget(), options = options
            });
            timer.Stop();
            if (report.summary.result != BuildResult.Succeeded)
                throw new InvalidOperationException("build " + report.summary.result + " with " + report.summary.totalErrors + " error(s)");
            AddSample("build", timer.Elapsed.TotalMilliseconds, step.round,
                "reportTotalMs", Json.Num(report.summary.totalTime.TotalMilliseconds), "outputBytes", report.summary.totalSize.ToString(), "mode", s.req.buildMode);
            s.notes.RemoveAll(p => p.k == "playerExecutable");
            s.notes.Add(new Pair { k = "playerExecutable", v = PlayerExecutable(location) });
            End(step);
        }

        static string PlayerExtension()
        {
            switch (Application.platform)
            {
                case RuntimePlatform.OSXEditor: return ".app";
                case RuntimePlatform.WindowsEditor: return ".exe";
                default: return "";
            }
        }

        // A macOS .app is a bundle; the runnable file is inside it.
        static string PlayerExecutable(string location)
        {
            if (!location.EndsWith(".app")) return location;
            var macos = Path.Combine(location, "Contents", "MacOS");
            foreach (var file in Directory.GetFiles(macos)) return file;
            return location;
        }
    }
}
