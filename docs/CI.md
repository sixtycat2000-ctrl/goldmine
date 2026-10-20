# Local and hosted validation

The normal CI workflow declares Python3.12/3.13 on Linux and macOS arm64, a separate macOS native-tools job, separate MCP/Foresight SDK jobs, and wheel installation outside the checkout. The declared hosted matrix is not a claim that those jobs have run. This checkout has no configured remote/runner; hosted evidence remains MISSING until an actual run URL and its artifacts exist.

Local and hosted jobs use the same entry:

```sh
python3 scripts/ci_validation.py --profile base --out reports/ci/base-001
python3 scripts/ci_validation.py --profile native --out reports/ci/native-001
python3 scripts/ci_validation.py --profile mcp --out reports/ci/mcp-001
python3 scripts/ci_validation.py --profile foresight --out reports/ci/foresight-001
```

Run each from an environment with its corresponding requirements lock. base does not need downloaded models, BEIR caches or native media tools. SDK jobs fail if their required tests skip. The fixed registry verifies original collection identities and raw JUnit, stdout/stderr and subprocess receipts. Source changes during a run produce MISSING, so final evidence is captured from a frozen candidate. Never rerun sealed holdout to choose a winner.

Live validation is manual and requires an explicitly configured reference host labeled `goldmine-reference` plus a real pinned inputs file. An absent model, corpus, result adapter, required test or complete grade set is nonzero. Defining this workflow does not install a runner, create an automation or publish an asset.

```sh
python3 scripts/ci_validation.py --profile live --inputs /absolute/inputs.json --out reports/ci/live-001
```

The failure counterexamples live in `test_acceptance_runner.py`, `test_distribution.py` and `test_ci_contract.py`: a unit failure (even printing PASS), empty collection, missing real dependency, required skip, edited report/logs and missing wheel command must never turn green. The action references are pinned to verified upstream tag commits. Runner architecture is measured; labels alone are insufficient.

The workflow syntax was checked locally with official actionlint1.7.12 (release archive SHA256 `aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f`). The local invocation disabled optional ShellCheck integration because ShellCheck was not installed; that check is not claimed.

Each scoped invocation writes ci-context.json binding its original report to
the runtime/validator/test/lock/workflow file hashes before and after execution.
After all four complete from a frozen candidate, combine those contexts with:

```sh
python3 scripts/verify_ci_local.py --base /absolute/base/ci-context.json --native /absolute/native/ci-context.json --mcp /absolute/mcp/ci-context.json --foresight /absolute/foresight/ci-context.json --actionlint /absolute/actionlint --out reports/execution/ci-local-001
```

This runs the fixed failure-injection tests and actionlint itself, then replays
every scoped raw report. It does not invoke a future release gate while testing
CI. Content changes invalidate earlier evidence; an unchanged-content Git
commit or derived tasks/PROGRESS update does not.

Lifecycle evidence uses separate explicit pressure profiles:

```sh
python3 scripts/validate_live.py --suite watch --profile real --model /absolute/model.gguf --server /absolute/llama-server --db /absolute/index.sqlite --out reports/execution/watch-real-001
```

Repeat with controlled, and with serve. Every original lifecycle node must
execute; missing models/tools, empty collection and required skips produce
MISSING. The watch suite includes its real600-second duty test. Controlled
sets the documented pressure override; real removes it. No daemon is installed.

Sources: [GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), [official actionlint release](https://github.com/rhysd/actionlint/releases/tag/v1.7.12). These describe tool capabilities, not this repository's hosted results.
