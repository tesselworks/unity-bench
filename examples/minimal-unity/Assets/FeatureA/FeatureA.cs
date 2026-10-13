namespace UnityBench.Example
{
    // A leaf: nothing references this assembly, so editing it recompiles only this assembly.
    public static class FeatureA
    {
        public static ulong Checksum() => Work.XorShiftPasses(Work.MakeInput());
    }
}
