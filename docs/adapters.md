---
type: guide
date: 2026-10-10
tags: [unity, benchmark, adapter]
---

# Consumer project boundary

Keep production assets, proprietary assemblies, project workloads and project-specific parity rules in the consumer repository. Unity Bench owns fixed scheduling, isolated checkouts, process lifetime, request/result evidence, host observation and report replay. It does not own a project's geometry, content compiler, scene setup or build policy.

A consumer pins an exact reviewed framework revision and supplies its own campaign configuration. Each variant also pins an exact consumer source revision. The Python runner's `repository` field identifies the consumer repository; its `projectPath` stays within the isolated consumer checkout. The adapter command runs code available at that revision and connects to the versioned request/result contract in [architecture](architecture.md).

For Editor workloads, reference `packages/com.tesselworks.unity-bench` from an immutable framework checkout or an exact Git revision with the package subdirectory. Do not point production consumers at a moving branch. The [minimal Unity example](../examples/minimal-unity/README.md) uses a local package reference only because both are committed together in this repository.

An adapter prepares the project's required inputs before measurement, performs the complete registered work, and reports actual runtime/backend, input/output fingerprints and work counters. Work equality is a project contract, not something the framework can infer. Translate older receipt formats explicitly; do not relabel old measurement archives as v1 campaigns without a reviewed compatibility mapping.

Connect one workload and verify it before expanding coverage. Preparation, import, reload, Play transitions and Player scenes have different clocks and completion boundaries. A synthetic protocol fixture is not proof that those production integrations exist.
