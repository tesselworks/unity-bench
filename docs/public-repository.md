# Pre-release checklist

Short list to walk before tagging a release or announcing the repository.

- [ ] `python3 -I -m unittest discover -s tests` passes in CI on Linux and Windows.
- [ ] Run `examples/minimal.toml` (small `repeats`/`rounds`) on every Editor version named in the README. Every built-in Editor workload and the example adapter produce numbers; `player.build_time` and `player.startup` ran at least once per Editor.
- [ ] Run `examples/minimal.toml` twice with the same cache directory: the second report says `prepared: reused <key>` for every variant, and `unity-bench cache list` shows the entries. `cold` logs show the isolated cache size (`Total cache size`) and an import count equal to the asset count.
- [ ] The sample report in the README is from a real run, with n, machine and date stated (it was taken with v0.2 and re-formatted for v0.3; replace it with a v0.3 run).
- [ ] No private project names beyond the allowed ones (MatterKit, Spill, described generically), no private repository links, no personal paths or hostnames, no credentials, in files, test fixtures or git history. The log fixtures in `tests/fixtures/` are short excerpts with project paths replaced.
- [ ] `LICENSE` (MIT) present; `pyproject.toml` and README agree.
- [ ] The Unity package compiles with no warnings on each supported Editor and the `.meta` files are committed.
- [ ] No binaries, Library folders or run directories are tracked.
- [ ] Known limits are stated in the README (batchmode/no-graphics, small n, no significance test).
- [ ] A published result names the framework commit, the options and the project commits (`summary.md`), and the raw `results.jsonl` and `campaign.json` ship with it, so anyone can regenerate the tables (`docs/methodology.md#reproducing-a-published-result`).
- [ ] `pip install .` into a clean environment, then `unity-bench plan examples/minimal.toml` and a run of a small campaign finds the Unity package.

## When the repository is made public (security)

Self-hosted runners run on the owner's own machines. A pull request from a fork can change a workflow file, so the settings below must hold before and right after the repository turns public.

- [ ] Every organization runner group still has "Allow public repositories" off (`gh api orgs/tesselworks/actions/runner-groups --jq '.runner_groups[]|[.name,.allows_public_repositories]'`, every value `false`). Never turn it on for a group whose runners are personal or CI machines. With it off, a fork PR that changes `runs-on` to a self-hosted label never gets a runner.
- [ ] Right after the visibility change, require approval for all external contributors' workflow runs: Settings → Actions → General → "Require approval for all external contributors". This can only be set on a public repository. Or use `gh api -X PUT repos/tesselworks/unity-bench/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors`.
- [ ] Default `GITHUB_TOKEN` permission is read-only: Settings → Actions → General → Workflow permissions. Every workflow also declares `permissions:` itself.
- [ ] No workflow uses `pull_request_target` or `workflow_run` on PR code, and no workflow reads secrets. CI runs only on GitHub-hosted runners (`ubuntu-latest`, `windows-latest`).
- [ ] Optional: allow only GitHub-owned and verified actions, and require full-SHA pinning.
- [ ] The read-only deploy keys that consumers used while the repository was private (`MatterKit benchmark consumer read-only`, `spill read`) can be removed once consumers fetch it publicly. Remove them in the same change that drops `UNITY_BENCH_READ_KEY` from those workflows.
