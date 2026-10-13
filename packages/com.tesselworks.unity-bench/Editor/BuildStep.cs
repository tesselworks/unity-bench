using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
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

        // Applies what the campaign asks for ("" = keep the project's setting), then records what the build will actually use.
        static void PreparePlayerSettings()
        {
            var backend = s.req.backend;
            if (!string.IsNullOrEmpty(backend))
            {
                // Looked up by name so one source file compiles on editors that do not have every ScriptingImplementation.
                var name = backend == "mono" ? "Mono2x" : backend == "il2cpp" ? "IL2CPP" : "CoreCLR";
                if (!Enum.TryParse(name, true, out ScriptingImplementation implementation))
                    throw new NotSupportedException("scripting backend " + backend + " is not available in Unity " + Application.unityVersion);
                PlayerSettings.SetScriptingBackend(NamedBuildTarget.Standalone, implementation);
            }
            // By reflection: the enum types behind these differ between Unity 6.7 and 7. A setting that cannot be applied fails the process.
            if (!string.IsNullOrEmpty(s.req.apiCompatibility)) WritePlayerSetting("SetApiCompatibilityLevel", s.req.apiCompatibility, "apiCompatibility");
            if (!string.IsNullOrEmpty(s.req.il2cppCodeGeneration)) WritePlayerSetting("SetIl2CppCodeGeneration", s.req.il2cppCodeGeneration, "il2cppCodeGeneration");
            if (!string.IsNullOrEmpty(s.req.stripping)) WritePlayerSetting("SetManagedStrippingLevel", s.req.stripping, "stripping");

            BuildNote("build.scriptingBackend", PlayerSettings.GetScriptingBackend(NamedBuildTarget.Standalone).ToString());
            BuildNote("build.apiCompatibility", ReadPlayerSetting("GetApiCompatibilityLevel"));
            BuildNote("build.il2cppCodeGeneration", ReadPlayerSetting("GetIl2CppCodeGeneration"));
            BuildNote("build.il2cppCompiler", ReadPlayerSetting("GetIl2CppCompilerConfiguration"));
            BuildNote("build.stripping", ReadPlayerSetting("GetManagedStrippingLevel"));
            BuildNote("build.development", Flag(s.req.development));
            BuildNote("build.target", HostTarget().ToString());
            string apis;
            try { apis = string.Join(", ", Array.ConvertAll(PlayerSettings.GetGraphicsAPIs(HostTarget()), a => a.ToString())); }
            catch { apis = "unavailable"; }
            BuildNote("build.graphicsApis", apis);
        }

        static void BuildNote(string key, string value) => s.notes.Add(new Pair { k = key, v = value });

        // A public static PlayerSettings method whose first parameter is the NamedBuildTarget.
        static MethodInfo PlayerSettingsMethod(string name, int parameters)
        {
            foreach (var m in typeof(PlayerSettings).GetMethods(BindingFlags.Public | BindingFlags.Static))
            {
                var p = m.GetParameters();
                if (m.Name == name && p.Length == parameters && p[0].ParameterType == typeof(NamedBuildTarget)) return m;
            }
            return null;
        }

        static string ReadPlayerSetting(string getter)
        {
            try { return PlayerSettingsMethod(getter, 1)?.Invoke(null, new object[] { NamedBuildTarget.Standalone })?.ToString() ?? "unavailable"; }
            catch { return "unavailable"; }
        }

        static void WritePlayerSetting(string setter, string value, string label)
        {
            var method = PlayerSettingsMethod(setter, 2);
            if (method == null) throw new NotSupportedException("build." + label + " does not exist in Unity " + Application.unityVersion + " (PlayerSettings." + setter + " not found)");
            var type = method.GetParameters()[1].ParameterType;
            object parsed;
            try { parsed = Enum.Parse(type, value, true); }
            catch (Exception)
            {
                throw new NotSupportedException("build." + label + " '" + value + "' is not available in Unity " + Application.unityVersion + "; valid: " + string.Join(", ", Enum.GetNames(type)));
            }
            method.Invoke(null, new object[] { NamedBuildTarget.Standalone, parsed });
        }

        static void BuildStep(Step step)
        {
            if (!Idle) return;
            Begin(step);
            // The project's own preparation (create a scene, bake data), untimed, before the output is cleaned and the stopwatch starts.
            if (!string.IsNullOrEmpty(s.req.prebuild))
                try { FindStatic(s.req.prebuild, Type.EmptyTypes).Invoke(null, null); }
                catch (TargetInvocationException e) { throw new InvalidOperationException("prebuild " + s.req.prebuild + " failed: " + e.InnerException, e.InnerException); }
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
                scenes = s.buildScenes.ToArray(), locationPathName = location, target = HostTarget(), options = options
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
