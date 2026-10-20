# Local distribution checks

The native asset recipe pins llama.cpp `b11461` to commit `4d756bc72bf00a4aacf410ae15a2d315f3db400d` and the exact repository patch. It refuses another revision, unknown patch state or unrelated source edits. It does not reset an existing checkout.

```sh
python3 scripts/build_native.py --out reports/execution/native-001 --jobs 2
```

Each build uses a new workspace/staging directory, static llama libraries, disabled OpenSSL, embedded Metal code, actual arm64 verification, system-only dynamic dependencies, ad hoc signature checks and version probes after extraction. The tar includes llama-server, goldmine-ocr, the upstream license and a file-hash manifest. `--dev-skip-ocr` produces a visibly dev-only archive that cannot pass release acceptance. A Python interpreter without `_lzma` requires the explicit `xz` build tool for this recipe. No binary is uploaded.

For Python artifacts, prepare an isolated build-tool environment from `requirements/build.lock`, then build from a temporary snapshot:

```sh
python3 scripts/build_python.py --python /absolute/build-venv/bin/python --out reports/execution/python-001
python3 scripts/build_python.py --python /absolute/build-venv/bin/python --sdist /absolute/goldmine-0.2.0.tar.gz --out reports/execution/sdist-rebuild-001
```

The snapshot records source-file hashes and current Git/diff identity, checks the copy did not race source changes, and runs the backend without index access. Runtime models, user data, evaluation answers and checkout build products are outside that source snapshot. Inspect only the exact wheel/sdist paths from this run with twine; never include a native tar in `twine check`.

Prepare an explicit local wheelhouse before an offline install smoke. MCP dependencies have their own lock. Run separate environments:

```sh
python3 scripts/smoke_install.py --wheel /absolute/goldmine-0.2.0-py3-none-any.whl --wheelhouse /absolute/wheelhouse --profile base --out reports/execution/install-base-001
python3 scripts/smoke_install.py --wheel /absolute/goldmine-0.2.0-py3-none-any.whl --wheelhouse /absolute/wheelhouse --profile mcp --out reports/execution/install-mcp-001
```

Installation uses `pip --no-index`, removes checkout `PYTHONPATH`, redirects product state through GOLDMINE_HOME/CONFIG and asserts the imported package lives in the new venv. Smoke checks import every required subpackage, perform actual lexical ingest/search, exercise missing-backend diagnostics, and validate real optional-SDK stdio initialization plus path refusal. Add `--native-tar PATH --model PATH` to perform an actual embedding request with extracted binaries and a previously prepared local model. That request closes its own backend.

These are local candidate assets at the package's current development version. Passing installation does not imply product quality, hosted CI, holdout or publication acceptance.

The required wheel inventory also checks quality/query_plan/rerank/admission,
PDF evidence and vector-cache modules. Installed provenance records the SHA of
every Python source in the venv and compares it with the actual wheel contents.
The base environment's PATH contains its venv only, so preinstalled ffmpeg,
poppler, OCR and llama-server cannot accidentally satisfy missing-tool checks.
With --native-tar, OCR recognizes a locally rendered proof image; an actual
768-dimensional embedding is required when --model is supplied.

For installed upgrade mechanics, build the explicit pre-task source revision
in another snapshot and pass both actual wheels to the disposable probe:

```sh
python3 scripts/build_python.py --python /absolute/build-venv/bin/python --revision 769483752f49fb0bf8241dca778b1895b9a1b70b --out reports/execution/legacy-python-001
python3 scripts/upgrade_smoke.py --old-wheel /absolute/old.whl --wheel /absolute/new.whl --wheelhouse /absolute/wheelhouse --out reports/execution/upgrade-001
```

This uses mock vectors only for stored-byte/schema mechanics, clearly labelled
in its report. The old installed CLI creates v4; the new CLI reads without
migrating, backs up and explicitly upgrades to v6. Original text/vector hashes
must remain intact. Installed MCP reads a citation, rejects it after an edit,
and the CLI refuses a future schema. Restoring the backup must work with the
old installed executable.

After the new wheel/base-native/MCP/sdist/upgrade runs, use
scripts/verify_distribution.py with --build, --sdist-build, --base, --mcp,
--rebuilt, --upgrade, --native, --python and --out pointing to their exact
manifests/smoke.json files. It runs twine on only those Python artifacts,
rechecks every required raw command and emits the distribution bundle consumed
by local_rc. It refuses stale candidate source, substituted wheels, missing
commands and native dependency/architecture/signature gaps.
