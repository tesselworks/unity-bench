using System.Globalization;
using UnityBench.Editor;

namespace UnityBench.Example
{
    // Example editor.adapter. The campaign names it as UnityBench.Example.HashAdapter.Run.
    // Each ctx.Measure call times only the lambda, then records the hash and counters it returns.
    public static class HashAdapter
    {
        public static void Run(AdapterContext ctx)
        {
            var input = Work.MakeInput();
            var blocks = int.Parse(ctx.Arg("sha256_blocks", "32"), CultureInfo.InvariantCulture);
            var block = new byte[1 << 20];
            for (var i = 0; i < block.Length; i++) block[i] = unchecked((byte)(i * 31 + 7));

            for (var round = 0; round < ctx.Rounds; round++)
            {
                ctx.Measure("xorshift", () =>
                {
                    var total = Work.XorShiftPasses(input);
                    return new WorkResult(WorkResult.Sha256Hex(total.ToString(CultureInfo.InvariantCulture)))
                        .Count("elements", Work.Elements).Count("passes", Work.Passes);
                });

                ctx.Measure("sha256", () =>
                {
                    // The same 1 MiB block hashed repeatedly: cheap to run, sensitive to the runtime's crypto speed.
                    var last = "";
                    for (var i = 0; i < blocks; i++) last = WorkResult.Sha256Hex(block);
                    return new WorkResult(last).Count("blocks", blocks).Count("bytes", (double)blocks * block.Length);
                });
            }
        }
    }
}
