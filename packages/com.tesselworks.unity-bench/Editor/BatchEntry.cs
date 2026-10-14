// Batch entry for the built-in Editor workloads and project adapters.
//
//   Unity -batchmode -nographics -projectPath <p> -logFile <log> -executeMethod UnityBench.Editor.BatchEntry.Run -unityBenchRequest <request.json>
//
// No -quit: the session exits the Editor itself when the result file is written. One process runs one workload.
// Progress lives in SessionState, so it survives the script reload that an edit causes. The same source compiles on
// Unity 6.7 (Mono) and Unity 7 (CoreCLR): static state is rebuilt on every load, from both the static constructor
// (Mono domain reload) and [OnCodeLoaded] (CoreCLR code reload); Recover is idempotent.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using UnityEditor;
using UnityEditor.Compilation;
using UnityEngine;
#if UNITY_6000_7_OR_NEWER
using Unity.Scripting.LifecycleManagement;
#endif

namespace UnityBench.Editor
{
    [InitializeOnLoad]
    public static partial class BatchEntry
    {
        const string Key = "UnityBench.Session";
        const double StepTimeoutSeconds = 900;
        const string RestorePrefix = "restore:";

        static State s;

        static BatchEntry() { Recover(); }
#if UNITY_6000_7_OR_NEWER
        [OnCodeLoaded] static void CodeLoaded() { Recover(); }
        [OnCodeUnloading] static void CodeUnloading() { Detach(); }
#endif

        static void Recover()
        {
            var json = SessionState.GetString(Key, "");
            if (string.IsNullOrEmpty(json)) return;
            s = JsonUtility.FromJson<State>(json);
            // A load after a compile finished is the reload an edit step waits for. A load during a Play Mode step is
            // the domain/code reload that entering or exiting Play Mode did cause; with reload disabled none happens.
            if (s.started && !s.reloaded && (s.compileEnd != 0 || IsPlay(s.req.kind))) s.reloaded = true;
            Save();
            Detach();
            EditorApplication.update += Tick;
            EditorApplication.playModeStateChanged += OnPlayMode;
            CompilationPipeline.compilationFinished += OnCompilationFinished;
            CompilationPipeline.assemblyCompilationFinished += OnAssemblyCompiled;
        }

        static void Detach()
        {
            EditorApplication.update -= Tick;
            EditorApplication.playModeStateChanged -= OnPlayMode;
            CompilationPipeline.compilationFinished -= OnCompilationFinished;
            CompilationPipeline.assemblyCompilationFinished -= OnAssemblyCompiled;
        }

        static void Save() { if (s != null) SessionState.SetString(Key, JsonUtility.ToJson(s)); }
        static long Now => Stopwatch.GetTimestamp();
        static double Ms(long from, long to) => (to - from) * 1000.0 / Stopwatch.Frequency;
        static bool Idle => !EditorApplication.isCompiling && !EditorApplication.isUpdating;
        static string Arg(string name)
        {
            var args = Environment.GetCommandLineArgs();
            var i = Array.IndexOf(args, name);
            return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
        }

        public static void Run()
        {
            Request request;
            try { request = JsonUtility.FromJson<Request>(File.ReadAllText(Arg("-unityBenchRequest"))); }
            catch (Exception e)
            {
                UnityEngine.Debug.LogError("[UnityBench] cannot read request: " + e.Message);
                EditorApplication.Exit(2);
                return;
            }
            s = new State { req = request };
            s.stepStart = Now;
            Save();
            Recover();
        }

        static void Tick()
        {
            if (s == null || s.phase == "done") return;
            try { MemoryPoll(); Advance(); }
            catch (Exception e) { s.error = (e is TargetInvocationException && e.InnerException != null ? e.InnerException : e).ToString(); Complete(1); }
        }

        static void Advance()
        {
            if (!string.IsNullOrEmpty(s.error)) throw new InvalidOperationException(s.error);
            if (Ms(s.stepStart, Now) > StepTimeoutSeconds * 1000) throw new TimeoutException("step " + s.phase + " " + s.cursor);
            if (s.phase == "setup") Setup(); else RunStep();
        }

        // ---- setup: Editor settings the variant asks for, then the plan ---------------------------------------------

        static void Setup()
        {
            switch (s.setupStage)
            {
                case 0:
                    if (!Idle) return;
                    var want = s.req.codeOptimization;
                    if (want != "default" && !string.Equals(CompilationPipeline.codeOptimization.ToString(), want, StringComparison.OrdinalIgnoreCase))
                    {
                        // Session-only setting (it does not persist to the next Editor process), so nothing needs restoring.
                        s.started = true; s.reloaded = false; s.compileEnd = 0; s.stepStart = Now; s.setupStage = 1; Save();
                        CompilationPipeline.codeOptimization = (CodeOptimization)Enum.Parse(typeof(CodeOptimization), want, true);
                    }
                    else s.setupStage = 2;
                    return;
                case 1:
                    // Changing code optimization recompiles and reloads. If nothing happened within 30 s, carry on.
                    if (s.compileEnd != 0 && s.reloaded && Idle) { s.setupStage = 2; s.started = false; }
                    else if (s.compileEnd == 0 && Idle && Ms(s.stepStart, Now) > 30000) { s.setupStage = 2; s.started = false; }
                    return;
                case 2:
                    if (!Idle) return;
                    s.started = false;
                    BuildPlan();
                    MemoryStart();
                    s.phase = "run"; s.stepStart = Now; Save();
                    return;
            }
        }

        static void BuildPlan()
        {
            var req = s.req;
            switch (req.kind)
            {
                case "editor.script_edit":
                case "editor.reload_leak":
                    foreach (var t in req.targets)
                    {
                        var full = Path.Combine(ProjectRoot, t.path);
                        if (!File.Exists(full)) throw new FileNotFoundException("target script not found: " + t.path);
                        s.originals.Add(new Pair { k = t.path, v = File.ReadAllText(full) });
                    }
                    // An api edit changes the assembly's public surface, so putting the file back is itself an API change. Each api
                    // edit is therefore followed by an untimed restore step, which keeps the next edit measuring only what it claims.
                    for (var r = 0; r < req.rounds; r++)
                        foreach (var t in req.targets)
                        {
                            s.plan.Add(new Step { name = t.id, round = r });
                            if (t.change == "api") s.plan.Add(new Step { name = RestorePrefix + t.id, round = r });
                        }
                    break;
                case "editor.play":
                case "editor.play_leak":
                    PrepareScene(req.scene);
                    if (req.domainReload == "disabled")
                    {
                        s.origPlayEnabled = EditorSettings.enterPlayModeOptionsEnabled;
                        s.origPlayOptions = EditorSettings.enterPlayModeOptions.ToString();
                        s.changedPlayOptions = true;
                        EditorSettings.enterPlayModeOptionsEnabled = true;
                        EditorSettings.enterPlayModeOptions = EnterPlayModeOptions.DisableDomainReload;
                    }
                    for (var r = 0; r < req.rounds; r++) { s.plan.Add(new Step { name = "enter", round = r }); s.plan.Add(new Step { name = "exit", round = r }); }
                    break;
                case "editor.adapter":
                    s.plan.Add(new Step { name = "adapter", round = 0 });
                    break;
                case "editor.scene_play":
                    PrepareScene(req.scene);
                    s.plan.Add(new Step { name = "scene", round = 0 });
                    break;
                case "player.build_time":
                    PreparePlayerSettings();
                    // Configured scenes are used as they are (a prebuild hook may still create them); otherwise a default scene is generated.
                    if (req.scenes.Count > 0) s.buildScenes.AddRange(req.scenes);
                    else { PrepareScene(null); s.buildScenes.Add(s.scenePath); }
                    for (var r = 0; r < req.rounds; r++) s.plan.Add(new Step { name = "build", round = r });
                    break;
                default:
                    throw new InvalidOperationException("Unknown workload kind " + req.kind);
            }
            if (req.memorySnapshot && IsLeak(req.kind)) s.plan.Add(new Step { name = SnapshotStep, round = 0 });
        }

        static string ProjectRoot => Path.GetDirectoryName(Application.dataPath);

        // A configured scene is opened as is; otherwise a default scene (camera, light) is generated under Assets/UnityBench.
        static void PrepareScene(string scene)
        {
            if (!string.IsNullOrEmpty(scene))
            {
                if (!File.Exists(Path.Combine(ProjectRoot, scene))) throw new FileNotFoundException("scene not found: " + scene);
                UnityEditor.SceneManagement.EditorSceneManager.OpenScene(scene, UnityEditor.SceneManagement.OpenSceneMode.Single);
                s.scenePath = scene;
                return;
            }
            s.scenePath = "Assets/UnityBench/Generated/Empty.unity";
            Directory.CreateDirectory(Path.Combine(ProjectRoot, "Assets/UnityBench/Generated"));
            var created = UnityEditor.SceneManagement.EditorSceneManager.NewScene(UnityEditor.SceneManagement.NewSceneSetup.DefaultGameObjects, UnityEditor.SceneManagement.NewSceneMode.Single);
            UnityEditor.SceneManagement.EditorSceneManager.SaveScene(created, s.scenePath);
            AssetDatabase.Refresh();
        }

        // ---- steps -------------------------------------------------------------------------------------------------

        static void RunStep()
        {
            if (s.cursor >= s.plan.Count) { Complete(0); return; }
            var step = s.plan[s.cursor];
            if (step.name == SnapshotStep) { SnapshotStepRun(step); return; }
            switch (s.req.kind)
            {
                case "editor.script_edit":
                case "editor.reload_leak": EditStep(step); break;
                case "editor.play":
                case "editor.play_leak": PlayStep(step); break;
                case "editor.adapter": AdapterStep(step); break;
                case "editor.scene_play": SceneStep(step); break;
                case "player.build_time": BuildStep(step); break;
            }
        }

        static void Begin(Step step)
        {
            s.started = true; s.reloaded = false; s.compileEnd = 0; s.assemblies = 0; s.enteredPlay = s.enteredEdit = 0;
            // Markers let the log parser attribute reload and compile blocks to this step.
            Mark("begin", step);
            s.stepStart = Now; Save();
        }

        static void End(Step step)
        {
            Mark("end", step);
            MemoryStep();
            s.cursor++; s.started = false; s.stepStart = Now; Save();
        }

        static void Mark(string what, Step step) =>
            UnityEngine.Debug.LogFormat(LogType.Log, LogOption.NoStacktrace, null, "[UnityBench] {0} {1} {2}", what, step.name, step.round);

        static void AddSample(string name, double ms, int round, params string[] info)
        {
            var sample = new SampleRecord { name = name, ms = ms, round = round, phase = "", hash = "", workGroup = "" };   // the runner derives the phase
            for (var i = 0; i + 1 < info.Length; i += 2) sample.info.Add(new Pair { k = info[i], v = info[i + 1] });
            s.samples.Add(sample);
        }

        static string Flag(bool v) => v ? "true" : "false";

        // editor.script_edit: change a script, refresh, time compile and compile+reload+idle.
        //   comment: append a comment (no API change; Unity recompiles that assembly and, with it, only what must be)
        //   api:     append a public class whose const changes each round (a public API change; dependents recompile)
        static void EditStep(Step step)
        {
            var restoring = step.name.StartsWith(RestorePrefix);
            var id = restoring ? step.name.Substring(RestorePrefix.Length) : step.name;
            if (!s.started)
            {
                if (!Idle) return;
                Begin(step);
                var target = s.req.targets.Find(t => t.id == id);
                var original = s.originals.Find(p => p.k == target.path).v;
                var suffix = restoring ? "" : target.change == "api"
                    ? "\npublic static class UnityBenchProbe_" + id.Replace('-', '_').Replace('.', '_') + " { public const int Round = " + step.round + "; }\n"
                    : "\n// unity-bench edit " + step.round + "\n";
                File.WriteAllText(Path.Combine(ProjectRoot, target.path), original + suffix);
                AssetDatabase.Refresh();
                return;
            }
            // Compile finished, the reload after it happened, and the Editor is idle again.
            if (s.compileEnd == 0 || !s.reloaded || !Idle) return;
            if (!restoring)
            {
                var info = new[] { "assemblies", s.assemblies.ToString(), "reloaded", Flag(s.reloaded) };
                AddSample(id + "/compile", Ms(s.stepStart, s.compileEnd), step.round, info);
                AddSample(id + "/compile+reload", Ms(s.stepStart, Now), step.round, info);
            }
            LeakPoint();   // after the reload an edit caused, restore steps included
            End(step);
        }

        // editor.play: EnterPlaymode until EnteredPlayMode plus one more frame; ExitPlaymode until EnteredEditMode.
        static void PlayStep(Step step)
        {
            if (step.name == "enter")
            {
                if (!s.started)
                {
                    if (!Idle || EditorApplication.isPlaying) return;
                    Begin(step);
                    EditorApplication.EnterPlaymode();
                    return;
                }
                if (s.enteredPlay != 0 && EditorApplication.isPlaying && Time.frameCount > s.enteredFrame)
                {
                    AddSample("play_enter", Ms(s.stepStart, Now), step.round, "enteredPlayModeMs", Json.Num(Ms(s.stepStart, s.enteredPlay)), "reloaded", Flag(s.reloaded));
                    End(step);
                }
            }
            else
            {
                if (!s.started)
                {
                    if (!EditorApplication.isPlaying || EditorApplication.isCompiling) return;
                    Begin(step);
                    EditorApplication.ExitPlaymode();
                    return;
                }
                if (s.enteredEdit != 0)
                {
                    AddSample("play_exit", Ms(s.stepStart, s.enteredEdit), step.round, "reloaded", Flag(s.reloaded));
                    LeakPoint();   // one reading per enter+exit cycle
                    End(step);
                }
            }
        }

        // editor.scene_play: enter Play Mode with the scene open; the package's scene workload (UnityBench.Player.SceneWorkload) starts when Play Mode
        // is entered (OnPlayMode), writes the result file itself and reports the exit code here. Then Play Mode is left and the Editor exits.
        static void SceneStep(Step step)
        {
            if (!s.started)
            {
                if (!Idle || EditorApplication.isPlaying) return;
                Begin(step);
                EditorApplication.EnterPlaymode();
                return;
            }
            if (!s.sceneDone) return;
            if (EditorApplication.isPlaying)
            {
                if (!s.sceneExiting) { s.sceneExiting = true; Save(); EditorApplication.ExitPlaymode(); }
                return;
            }
            if (s.sceneCode != 0) { s.error = "the scene workload failed (see its result file)"; return; }
            End(step);
        }

        static void StartSceneWorkload()
        {
            UnityBench.Player.SceneWorkload.RunInEditor(s.req.outPath, Arg("-unityBenchRequest"), code =>
            {
                if (s == null) return;
                s.sceneDone = true; s.sceneCode = code; Save();
            });
        }

        // editor.adapter: call the project's entry point once. It measures its own work through AdapterContext.
        static void AdapterStep(Step step)
        {
            if (!Idle) return;
            Begin(step);
            var method = FindAdapter(s.req.method);
            method.Invoke(null, new object[] { new AdapterContext(s.req, s.samples, s.distributions, s.workGroups, s.headline, s.notes) });
            End(step);
        }

        static MethodInfo FindAdapter(string name) => FindStatic(name, new[] { typeof(AdapterContext) });

        // A public or non-public static method looked up as Namespace.Type.Method with the given parameter types.
        internal static MethodInfo FindStatic(string name, Type[] parameters)
        {
            var dot = name.LastIndexOf('.');
            var typeName = name.Substring(0, dot);
            var methodName = name.Substring(dot + 1);
            // Looked up through the compilation pipeline's assembly list; AppDomain.GetAssemblies can return unloaded assemblies.
            foreach (var kind in new[] { AssembliesType.Editor, AssembliesType.PlayerWithoutTestAssemblies })
                foreach (var assembly in CompilationPipeline.GetAssemblies(kind))
                {
                    var type = Type.GetType(typeName + ", " + assembly.name, false);
                    var method = type?.GetMethod(methodName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static, null, parameters, null);
                    if (method != null) return method;
                }
            throw new MissingMethodException("method not found: " + name + "(" + string.Join(", ", Array.ConvertAll(parameters, t => t.Name)) + ")");
        }

        // ---- events ------------------------------------------------------------------------------------------------

        static void OnCompilationFinished(object context)
        {
            if (s == null || !s.started || s.compileEnd != 0) return;
            if (s.phase == "setup" || IsEdit(s.req.kind)) { s.compileEnd = Now; Save(); }
        }

        static void OnAssemblyCompiled(string assembly, CompilerMessage[] messages)
        {
            if (s == null || !s.started) return;
            s.assemblies++;
            foreach (var m in messages)
                if (m.type == CompilerMessageType.Error) s.error = "compile error in " + assembly + ": " + m.message;
            Save();
        }

        static void OnPlayMode(PlayModeStateChange change)
        {
            if (s == null || !s.started || (!IsPlay(s.req.kind) && s.req.kind != "editor.scene_play") || s.cursor >= s.plan.Count) return;
            var step = s.plan[s.cursor];
            if (change == PlayModeStateChange.EnteredPlayMode && step.name == "enter") { s.enteredPlay = Now; s.enteredFrame = Time.frameCount; Save(); }
            if (change == PlayModeStateChange.EnteredEditMode && step.name == "exit") { s.enteredEdit = Now; Save(); }
            if (change == PlayModeStateChange.EnteredPlayMode && step.name == "scene" && !s.sceneDone) StartSceneWorkload();
        }

        // ---- result ------------------------------------------------------------------------------------------------

        static void Complete(int code)
        {
            Detach();
            s.phase = "done";
            // Always put edited scripts and changed settings back, even after a failure. The runner checks the scripts too.
            foreach (var o in s.originals)
            {
                try { File.WriteAllText(Path.Combine(ProjectRoot, o.k), o.v); } catch { }
            }
            try
            {
                if (s.changedPlayOptions)
                {
                    EditorSettings.enterPlayModeOptionsEnabled = s.origPlayEnabled;
                    EditorSettings.enterPlayModeOptions = (EnterPlayModeOptions)Enum.Parse(typeof(EnterPlayModeOptions), s.origPlayOptions);
                }
            }
            catch { }
            MemoryEnd();
            // The scene workload writes its own result (also when it failed, with the reason); the Editor's records have nothing to add to it.
            if (!(s.req.kind == "editor.scene_play" && s.sceneDone)) File.WriteAllText(s.req.outPath, ResultWriter.Build(s, code == 0));
            SessionState.EraseString(Key);
            EditorApplication.Exit(code);
        }
    }
}
