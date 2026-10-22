# Local and hosted validation

The normal CI workflow declares Python3.12/3.13 on Linux and macOS arm64, a separate macOS native-tools job, separate MCP SDK jobs, and wheel installation outside the checkout. The declared hosted matrix is not a claim that those jobs have run. This checkout has no configured remote/runner; hosted evidence remains MISSING until an actual run URL and its artifacts exist.

Local and hosted jobs use the same entry:

```sh
python3 scripts/ci_validation.py --profile base --out reports/ci/base-001
python3 scripts/ci_validation.py --profile native --out reports/ci/native-001
python3 scripts/ci_validation.py --profile mcp --out reports/ci/mcp-001
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
After all three complete from a frozen candidate, combine those contexts with:

```sh
python3 scripts/verify_ci_local.py --base /absolute/base/ci-context.json --native /absolute/native/ci-context.json --mcp /absolute/mcp/ci-context.json --actionlint /absolute/actionlint --out reports/execution/ci-local-001
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

## Read-only hosted evidence

The hosted adapter accepts only an explicitly named GitHub.com repository,
run ID, latest complete attempt and full40-character HEAD SHA. The current
checkout must be clean and committed at exactly that SHA, including its CI
workflow, validators, test inventory and dependency locks. A metadata-only
commit also requires a matching hosted run; this stricter hosted identity is
separate from local content-based replay. The supported events are push and
workflow_dispatch; fork and pull-request merge identities are rejected.

The fixed workflow names its seven expanded jobs and artifacts:
base-ubuntu-latest-3.12, base-ubuntu-latest-3.13, base-macos-14-3.12,
base-macos-14-3.13, native-macos-arm64, optional-mcp,
and package-install. Source jobs attach their actual OS, architecture, Python,
repository/run/attempt/SHA/job/workspace to the same ci_validation receipts.
The package wrapper calls the existing build/smoke scripts and records wheel,
exact sdist rebuild, twine and all three outside-checkout install environments.
Native release binaries and manual live validation retain their separate gates.

After an actual authorized run exists, collection performs GET requests only:

```sh
python3 scripts/collect_ci_hosted.py collect --repository OWNER/REPO --run-id RUN_ID --run-attempt ATTEMPT --head-sha FULL40SHA --out reports/execution/P0-06/ci-hosted-001
python3 scripts/collect_ci_hosted.py verify --manifest reports/execution/P0-06/ci-hosted-001/manifest.json
```

Those capitalized values are placeholders, not evidence or executable defaults.
No command creates a remote, pushes, dispatches a workflow, installs a runner,
posts a message or executes downloaded artifact code. Private repositories need
a pre-existing GH_TOKEN or GITHUB_TOKEN with Actions read and Contents read;
select it using --token-env. Tokens and signed download URLs stay out of argv
and persisted logs. Proxy environment variables are ignored for GitHub TLS.

Collection saves the original API responses before and after downloading every
artifact. Artifact IDs, same-run source identities, successful job/required-step
conclusions, upload intervals, SHA256 archive digests and sizes must agree.
Missing, expired, duplicate or unexpected artifacts/jobs are MISSING. A partial
rerun with retained earlier artifacts is incomplete; the adapter never deletes
or replaces remote artifacts. Future payload/host/layout changes require an
explicit adapter revision rather than heuristic fallback.

The verifier checks the complete ZIP directory before extraction, including
implicit ancestor case aliases, symlinks, absolute/traversing/duplicate paths,
file-prefix collisions and fixed expansion bounds. It independently bounds
nested wheels before reading metadata/modules. Source/JUnit scopes are replayed
against the current registry and actual argv; original runner paths are compared
through the captured workspace without modifying receipts. Downloaded scripts
or wheels are never executed during replay.

The --offline switch validates structure and raw mechanics but always exits2
with MISSING for hosted authenticity. Final hosted PASS additionally requires
fresh TLS API revalidation of the same completed run, attempt, workflow, jobs,
artifact identities/digests and source bytes. An unavailable API, URL alone,
local report, edited green summary, print('PASS') or synthetic API fixture cannot
satisfy that gate. Saved hashes provide consistency, not an attestation against
an attacker who controls the trusted committed source and every local file.

In inputs.json, evidence.ci_hosted is the pinned manifest_path/SHA256 pair;
hosted contains exactly repository, run_id, run_attempt and head_sha and must
match the bundle. The local_rc profile is unchanged. hosted_release adds this
actual hosted proof to all local_rc requirements; local failures remain failures.

Engineering limits are content-independent: each GET has an owned isolated
stdlib worker and a120-second absolute parent deadline (30-second socket
inactivity cap); timeout kills/reaps only that worker and retains failure bytes.
API JSON is bounded16MiB. Each outer ZIP is at most2GiB,4GiB expanded,
100000 entries and2GiB per member, with a2000:1 expansion ceiling. Nested
wheels are bounded64MiB compressed,128MiB expanded,10000 entries and8MiB
per member; JUnit parsing is bounded64MiB; source sdists are bounded256MiB
expanded. Exceeding a bound is MISSING, never partial success.

API protocol sources: [GitHub run/attempt API](https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2026-03-10),
[job API](https://docs.github.com/en/rest/actions/workflow-jobs?apiVersion=2026-03-10),
[artifact/digest/download API](https://docs.github.com/en/rest/actions/artifacts?apiVersion=2026-03-10),
[committed contents API](https://docs.github.com/en/rest/repos/contents?apiVersion=2026-03-10),
and [runner network domains](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
These define interfaces; they do not establish this repository's hosted success.
