using System;
using System.Collections.Generic;

namespace UnityBench.Editor
{
    // Plain serializable records. They go through JsonUtility (request in, session state across reloads), which has no
    // dictionaries and turns null strings into "", so every string here treats "" as "not set".

    [Serializable] class Pair { public string k, v; }
    [Serializable] class Number { public string k; public double v; }
    [Serializable] class Target { public string id, path, change; }

    [Serializable]
    class Request
    {
        public int schema;
        public string kind, workload, variant, outPath;
        public string codeOptimization = "default";   // default | debug | release
        public int rounds = 5;
        public List<Target> targets = new List<Target>();           // editor.script_edit
        public string domainReload = "default", scene = "";         // editor.play
        public string method = "";                                   // editor.adapter
        public List<Pair> args = new List<Pair>();
        public string buildMode = "clean", outputDir = "", backend = "", buildTarget = "", prebuild = "";   // player.build_time
        public string apiCompatibility = "", il2cppCodeGeneration = "", stripping = "";             // player.build_time, "" = keep the project's setting
        public List<string> scenes = new List<string>();
        public bool development;
    }

    [Serializable]
    class SampleRecord
    {
        public string name, phase, hash, workGroup;
        public double ms;
        public int round;
        public List<Pair> info = new List<Pair>();
        public List<Number> counters = new List<Number>();
    }

    [Serializable]
    class DistributionRecord
    {
        public string name, phase, hash, workGroup;
        public List<double> values = new List<double>();
        public List<Number> counters = new List<Number>();
    }

    [Serializable] class WorkGroupRecord { public string name, hash; public List<Number> counters = new List<Number>(); }
    [Serializable] class Step { public string name; public int round; }

    [Serializable]
    class State
    {
        public Request req;
        public string phase = "setup";                 // setup, run, done
        public int setupStage;
        public string scenePath = "";
        public List<string> buildScenes = new List<string>();
        public List<Step> plan = new List<Step>();
        public int cursor;
        public bool started, reloaded;
        public long stepStart, compileEnd, enteredPlay, enteredEdit;
        public int enteredFrame, assemblies;
        public string error = "";
        public List<SampleRecord> samples = new List<SampleRecord>();
        public List<DistributionRecord> distributions = new List<DistributionRecord>();
        public List<WorkGroupRecord> workGroups = new List<WorkGroupRecord>();
        public List<string> headline = new List<string>();
        public List<Pair> notes = new List<Pair>();
        public List<Pair> originals = new List<Pair>();   // project-relative path -> original file text, restored on exit
        public string origPlayOptions = "";
        public bool origPlayEnabled, changedPlayOptions;
        public bool sceneDone, sceneExiting;               // editor.scene_play: the in-Play-Mode run finished / Play Mode is being left
        public int sceneCode;
    }
}
