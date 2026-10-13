using System;
using System.Globalization;
using System.Text;

namespace UnityBench.Editor
{
    // Tiny JSON writer for the result file. JsonUtility cannot write dictionaries or real nulls.
    static class Json
    {
        public static string Quote(string v)
        {
            if (v == null) return "null";
            var b = new StringBuilder("\"");
            foreach (var c in v)
            {
                switch (c)
                {
                    case '\\': b.Append("\\\\"); break;
                    case '"': b.Append("\\\""); break;
                    case '\n': b.Append("\\n"); break;
                    case '\r': b.Append("\\r"); break;
                    case '\t': b.Append("\\t"); break;
                    default: if (c < 0x20) b.Append("\\u").Append(((int)c).ToString("x4")); else b.Append(c); break;
                }
            }
            return b.Append('"').ToString();
        }

        public static string Num(double v) =>
            double.IsNaN(v) || double.IsInfinity(v) ? "null" : v.ToString("R", CultureInfo.InvariantCulture);

        public static string Bool(bool v) => v ? "true" : "false";
        public static string OrNull(string v) => string.IsNullOrEmpty(v) ? "null" : Quote(v);
    }
}
