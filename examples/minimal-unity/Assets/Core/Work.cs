namespace UnityBench.Example
{
    // Deterministic managed computation used by the example adapter. Every other assembly references this one,
    // so editing this file shows the cost of changing something widely referenced.
    public static class Work
    {
        public const int Elements = 65536;
        public const int Passes = 16;

        public static uint[] MakeInput()
        {
            var values = new uint[Elements];
            for (var i = 0; i < values.Length; i++) values[i] = unchecked((uint)i * 2654435761U);
            return values;
        }

        // Integer-only, so the result is bit-identical on every runtime.
        public static ulong XorShiftPasses(uint[] values)
        {
            ulong total = 0;
            unchecked
            {
                for (var pass = 0; pass < Passes; pass++)
                    for (var i = 0; i < values.Length; i++)
                    {
                        var value = values[i] ^ (uint)pass;
                        value ^= value << 13;
                        value ^= value >> 17;
                        value ^= value << 5;
                        total += value;
                    }
            }
            return total;
        }
    }
}
