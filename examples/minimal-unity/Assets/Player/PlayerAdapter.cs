using System.Collections;
using System.Globalization;
using UnityBench.Player;
using UnityEngine.Scripting;

namespace UnityBench.Example
{
    // Example player.adapter. The campaign names it as UnityBench.Example.PlayerAdapter.Run.
    // It runs over many frames (an IEnumerator), records the frame times of its first frames as "warmup" and the rest as
    // "steady", and times one deterministic computation. [Preserve] keeps the type and method when IL2CPP strips the player.
    [Preserve]
    public static class PlayerAdapter
    {
        [Preserve]
        public static IEnumerator Run(PlayerContext ctx)
        {
            var frames = int.Parse(ctx.Arg("frames", "300"), CultureInfo.InvariantCulture);
            var warmupFrames = int.Parse(ctx.Arg("warmup_frames", "30"), CultureInfo.InvariantCulture);
            var input = Work.MakeInput();
            ctx.Headline("frame steady p50", "frame steady p95");

            var recorder = ctx.RecordFrames("frame", "warmup");
            for (var frame = 0; frame < frames; frame++)
            {
                if (frame == warmupFrames)
                {
                    recorder.Stop();
                    recorder = ctx.RecordFrames("frame", "steady");
                }
                Work.XorShiftPasses(input);   // CPU work every frame, so a frame time means something
                yield return null;
            }
            recorder.Stop();

            for (var round = 0; round < ctx.Rounds; round++)
                ctx.Measure("xorshift", () => new WorkResult(WorkResult.Sha256Hex(Work.XorShiftPasses(input).ToString(CultureInfo.InvariantCulture)))
                    .Count("elements", Work.Elements).Count("passes", Work.Passes));
        }
    }
}
