---
type: plan
date: 2026-10-10
tags: [unity, benchmark, publication]
---

# Public release plan

`tesselworks/unity-bench` is currently a separate private development repository. It starts from a reviewed source snapshot with fresh Git history. Consumer project histories, production assets, project-specific build scripts and historical measurement archives are excluded. Creating this private repository does not authorize a public release or grant an open-source license.

## Before publication

1. Select the license with the owner and audit dependency and third-party notices. No public license has been selected yet.
2. Review every distributed file for credentials, personal host paths, internal URLs and private dependencies. Ignore rules are not a privacy audit.
3. Verify source installation, CLI examples and contract CI from a clean checkout. Validate the actual Unity package and independent sample at the exact repository revision and supported Editor versions before a release claim.
4. Publish protocol and compatibility policy. Changing a metric boundary or parity field requires a documented protocol change.
5. Exercise missing output, stale identity, nonzero exit, timeout, wrong runtime/backend and unequal work/input/output refusals. Preserve failures rather than turning them into samples.
6. Provide a sanitized report with actual executed workload evidence. Label synthetic fixtures as protocol tests and inspect even public reports before sharing them.
7. Document prerequisites, resource use and limitations. Never install Editors, alter global security settings or stop unrelated processes implicitly.

## Remaining development

- Connect consumer workloads through an exact pinned framework revision; keep consumer logic and assets outside this repository.
- Implement and validate Editor import, script reload and Play-entry adapters, then Player workload contracts.
- Complete the cross-version experiment and its item-by-item evidence table before writing an upgrade verdict.
- Publish only after owner approval of license and visibility. The blog can then link the release and invite readers to reproduce the example and connect their own project.

The extraction preserves the previously verified Windows measurement implementation. Independent repository contract CI does not execute Unity, and previous implementation verification is not a new native run of this repository revision. Other Editor versions, actual macOS/Linux Unity execution and representative production workloads remain unverified.
