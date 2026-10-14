using UnityBench.Player;
using UnityEngine;

namespace UnityBench.Example
{
    // Example scene content for the scene workloads (player.scene, editor.scene_play): a floor, a pile of rigidbodies falling on it,
    // and a little garbage every frame so the GC counters have something to show. Without `reportWork` it makes no Unity Bench call at
    // all, which is the zero-code start: the report has frames and markers and says "work not verified". Bodies.unity has it off,
    // BodiesHooked.unity has it on: the hooks tell the report what the scene did, and the Work column turns into "same".
    public sealed class BodySpawner : MonoBehaviour
    {
        public int bodies = 150;
        public bool reportWork;

        static string sink;   // keeps the garbage observable, so nothing optimises it away
        uint seed = 12345;

        void Start()
        {
            var floor = GameObject.CreatePrimitive(PrimitiveType.Cube);
            floor.transform.localScale = new Vector3(14, 1, 14);
            floor.transform.position = new Vector3(0, -0.5f, 0);
            for (var i = 0; i < bodies; i++)
            {
                var body = GameObject.CreatePrimitive(i % 2 == 0 ? PrimitiveType.Sphere : PrimitiveType.Cube);
                body.transform.position = new Vector3(Next() * 8 - 4, 2 + i * 0.12f, Next() * 8 - 4);
                body.AddComponent<Rigidbody>();
            }
            if (reportWork) Bench.Mark("bodies", bodies);   // one call: what this scene does, compared between variants
        }

        void Update()
        {
            if (reportWork) Bench.Begin("churn");
            for (var i = 0; i < 8; i++) sink = new string('x', 64 + i) + Time.frameCount;   // 8 small strings per frame
            if (reportWork) Bench.End("churn");
        }

        // A tiny generator of our own, so the layout is the same on every runtime.
        float Next()
        {
            seed = unchecked(seed * 1664525u + 1013904223u);
            return (seed >> 8) / (float)(1 << 24);
        }
    }
}
