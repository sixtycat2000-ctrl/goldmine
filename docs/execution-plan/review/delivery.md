# Delivery / QA / SRE adversarial review

Reviewed 2026-10-21 against source `769483752f49fb0bf8241dca778b1895b9a1b70b`. This is an independent delivery review of the plan. No production code changed during review. Findings below identify original locations; revised files naturally have different line numbers. The revisions preserve quality targets and distinguish implementation, local evidence, and unavailable hosted evidence.

| ID | Severity | Original location | Counterexample / risk | Required revision and closure evidence |
|---|---|---|---|---|
| DELIV-01 | High | P0.md:146,156; ACCEPTANCE.md:53; P2.md:86-88 | A clean clone lacks model/BEIR/system-wallpaper/SDK data; existing offline tests skip. Strict no-skip offline cannot pass. | Canonical profiles `offline`, `assets`, `native`, `optional-mcp`; hermetic fixtures, fixed collection and nodeid classification migration. Prove offline works with no external tools/models/cache/SDK. Required external profiles remain MISSING when unavailable. |
| DELIV-02 | High | ACCEPTANCE.md:56,137; P2.md:125-129 | P2-07 needs release PASS before VERIFIED; release requires all 23 tasks including P2-07 VERIFIED. | Validate primitive evidence only; derive P2-07/M3 after local_rc PASS. Reject replacing raw checks with task status. |
| DELIV-03 | High | P2.md:95; inputs.example.json:48-49 | No Git remote exists; hosted run is mandatory while push is unauthorized, so local delivery cannot finish. | Separate local_rc / hosted_release and local / hosted CI evidence. Missing hosted run is explicit MISSING, cannot block local work or be called hosted success. |
| DELIV-04 | High | P2.md:107,111; build_llama_tar.sh:52-63,85,94-123 | llama-server dynamically depends on libraries omitted from its tar; wrong source tag and unknown patch continue; version failure is swallowed; old staging can leak old OCR. | Exact revision/patch, fresh staging, architecture verification, dylib dependency closure or proven static build, isolated extracted runtime. Wrong revision, patch, missing dylib and stale staging must fail. Build/twine inspect only explicit current-run Python assets. |
| DELIV-05 | Medium-high | ACCEPTANCE.md:49,54; P1.md:44,63; tasks.json | Early task checks require future abilities and repeatedly expose holdout. Optional experiments serialize independent MCP/CI work. | task-check validates dev/local scope; implementation dependencies separated from evidence milestones. One final sealed holdout audit after all candidate choices freeze; M1/M2 use dev. Final profiles reuse the same raw audit, never rerun to choose a result. |
| DELIV-06 | Medium-high | CONTRACTS.md:158-166; test_live_serve.py:269-293 | Existing gate uses 22 samples, warm then cold, no p95 assertion; fixed pressure can masquerade as normal guard behavior. | Warm/cold each at least 30 successful fresh processes, interleaved paired queries, raw JSON/exit/time, nearest-rank p50/p95. Controlled and real-guard runs separate; MCP uses paired per-query overhead before quantile. |
| DELIV-07 | Medium | P2.md:28-41 | Default float64 generation at 500k×768 needs about 3.1 GB before fp16 conversion; ru_maxrss accumulates between cases and can invalidate improvement measurements. | Chunked float32 normalized fixture generation, independent generation/query processes per scale/distribution/arm, model-child RSS separate, explicit byte units. Verify generation high-water cannot contaminate search peak. |
| DELIV-08 | Medium | P0.md:141,145-148; CONTRACTS.md:30-54; inputs.example.json | Some run paths have no bound manifest SHA; reported exit code is not independently tied to structured results; same run can pretend to be two arms. | Schema2 explicit path_base, file/run SHA refs, fixed trusted registry, machine receipts plus stdout/stderr/JUnit/results, recomputation, strict identities and path containment. Reject missing/duplicate/unknown checks, edited summaries, nonfinite metrics and same-run formal comparisons. Local hashes are integrity/provenance checks, not authentication against wholesale file forgery. |

## Read-only verification

- `python3 -m pytest --collect-only -q -m 'not live' tests/`: exit0, 995/1020 tests collected, 25 live deselected.
- All five evaluation-script help commands inspected: eval_real exit1 with argparse `95% CI` formatting error; eval_answer/eval_public/eval_distill/bench exit0.
- `GOLDMINE_PUBLIC_DATA=/tmp/goldmine-review-no-beir-cache python3 -m pytest -q -rS -p no:cacheprovider tests/test_eval_public.py::test_public_manifest_vs_file_b7`: exit0 but one skip, demonstrating DELIV-01.
- `git remote -v`: empty, demonstrating DELIV-03.
- `otool -L vendor/llama.cpp/build/bin/llama-server`: multiple @rpath llama/ggml libraries and Homebrew OpenSSL dependencies; build script copies only executable/OCR, demonstrating DELIV-04.
- actionlint and shellcheck were unavailable; their successful execution is not claimed. No full suite, live models, native rebuild or external messaging was run during review.

## Disposition

All eight findings are incorporated in ACCEPTANCE.md, P2.md, AGENT_PROMPT.md and inputs.example.json; corresponding contract/task/P0 changes are owned by the coordinating agent. This closes plan revision only. Runtime fixes, hermetic marker migration, raw-evidence validation, release tests and all real quality measurements still require implementation and acceptance evidence.
