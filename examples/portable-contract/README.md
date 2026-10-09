---
type: guide
date: 2026-10-09
tags: [benchmark, framework, protocol]
---

# Portable contract fixture

`adapter.py` exercises the process/request/result contract with fixed integer work.
It honestly reports `unityVersion: not-unity` and `backend: python-contract-fixture`.
It is not a Unity compatibility check, a Unity workload, or an upgrade baseline.
The integration test creates a separate Git repository containing this adapter,
commits it, and executes a complete campaign through real detached worktrees and
fresh processes. No consumer project code or Unity installation is used.

After committing the source in a local checkout, generate an immutable configuration:

```sh
python3 examples/portable-contract/make-config.py --repository /absolute/framework-repository --revision <40-character-commit> --out /absolute/new-config.json
python3 unity-bench.py plan /absolute/new-config.json
python3 unity-bench.py run /absolute/new-config.json --out /absolute/new-campaign
```

Run these commands from this repository root. Configuration generation checks the committed adapter exists; it does not create commits or modify the repository. Python must be installed on the execution host.
