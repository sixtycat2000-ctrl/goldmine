# Answer evaluation v3

This package consumes explicitly supplied local job records. It never calls a
model, sends another task a message, regenerates a corpus, or converts old scores
into new evidence. Legacy `evals/answer/` jobs remain advisory. Their 20-question
protocol and `evals/answer_baseline.json` cannot pass a v3 gate.

From the repository root:

```bash
python3 scripts/eval_answer.py --validate-run RUN --json
python3 scripts/eval_answer.py --compare BASE_RUN CANDIDATE_RUN --json
python3 scripts/eval_answer.py --update-baseline --yes --out BASE_RUN --baseline evals/answer_v3/baseline.json --json
python3 scripts/eval_answer.py --gate --out CANDIDATE_RUN --baseline evals/answer_v3/baseline.json --json
```

Exit codes are 0 for a completed passing operation, 1 for an observed engineering
drop/refusal failure, and 2 for missing or incomparable data. Diagnostic operations
can exit 0 but always have `diagnostic_only=true` and `release_eligible=false`.
Fixture data requires explicit `--diagnostic`; self comparison also requires it.

## Immutable run directory

The entry point is `run.json`. All referenced paths must resolve inside that run,
including through symlinks. Filenames do not substitute for content hashes.

```text
run.json
questions.jsonl
corpus.json
preregistration.json
answerer.txt
grader.txt
packs/<question_id>.md
requests_a1/<question_id>.txt
requests_a2/<question_id>.txt
answers_a1/<question_id>.md + <question_id>.json
answers_a2/<question_id>.md + <question_id>.json
judge_prompts_a1/<question_id>.txt
judge_prompts_a2/<question_id>.txt
grades_a1_j1/<question_id>.json
grades_a1_j2/<question_id>.json
grades_a2_j1/<question_id>.json
grades_a2_j2/<question_id>.json
```

`protocol.load_run` is the executable schema. It rejects unknown fields, duplicate
JSON keys, missing question IDs, nonfinite numbers, booleans as scores, missing
facts, unknown evidence IDs, and mismatched round/model/prompt/pack identities.
The required counts are at least 20 each of single, multi, cross and trap. Source
and label correctness is verified by the separate corpus audit; hashing its
manifest is not a claim that this reader independently established every label.

`run.json` has `schema=3`, `protocol="answer-eval/v3"`, unique `run_id`,
`evidence_mode="real"` or `"fixture"`, timezone-bearing `started_at/completed_at`,
artifact references `questions/corpus/preregistration/answerer_prompt/grader_prompt`
as `{path,sha256}`, plus `source_groups_sha256`, `models`, `settings`, `tokenizer`
and `variables`. Models contain answerer/judge `{model_id,model_sha256}`; settings
contain the corresponding exact engine parameter objects. The tokenizer records
`{model_id,sha256,mode}` with mode tokenize/estimate. Variables record ranking,
chunker, embedding, lexical, reranker, pack and code identities/settings. Their
differences are reported as experimental changes, not silently invalidated.

Questions have `id,class,q,source_group,expect,required_facts`. Classes are single,
multi, cross, trap. Every fact has `fact_id,description,sufficient_evidence`, using
the plan's AND/OR evidence alternatives. Expect entries contain evidence_id,
corpus-relative path, kind, grade, evidence_type, location and span; metadata also
requires storage_field. Span offsets are zero-based, half-open Unicode character
positions and include exact text/SHA. Page/line/second locations use the explicit
typed union in the plan. Trap expect and facts must be empty.

## Register before running jobs

`preregistration.json` freezes schema/protocol, registered_at, questions/corpus/
source_groups hashes, models/settings, both prompt hashes, tokenizer,
`score_scale=[0,2]`, `practical_drop_threshold` (default 0.10; permitted 0.10, 0.15,
0.20, 0.25, 0.30), `trap_min_rate=0.90`, `bootstrap_unit="source_group"`, integer
bootstrap_seed and bootstrap_reps (1000..100000). Both runs bind the same file
bytes. Registration must precede both runs. Timestamps are recorded provenance;
an external execution receipt/ledger is needed to establish when jobs actually ran.

The answerer template contains `<question text verbatim>` and
`<pack file contents verbatim>` once each. `protocol.answer_request` renders it
without passing expected facts to the answerer. Save the exact request in the
corresponding requests directory. The grader template contains `<QUESTION>`,
`<FACTS_JSON>` and `<ANSWER>` once each; `protocol.grader_request` renders the
question, frozen fact/evidence definitions, and that answer. Save the exact judge
request. Arbitrary substitutions or old-pack requests fail validation.

Answer sidecars bind schema, run_id, question_id, answer_round, unique execution_id,
completed_at, answer_sha256, pack_sha256, answerer_prompt_sha256, request_sha256,
answerer_model and answerer_settings. Each grade binds schema, run_id, question_id,
answer_round, judge_round, unique execution_id, completed_at, score, refused,
fabricated_fact, fact_results, answer_sha256, pack_sha256, grader_prompt_sha256,
judge_model and judge_settings. Fact results are `{fact_id,supported,evidence_ids}`.
The answer and grade rounds are independent jobs; copied execution IDs are refused.

## Interpret the result

Each answerable score is the mean of four 0..2 judgments. A mean drop strictly
greater than the registered practical threshold fails. Traps independently require
both answers to be marked refused by both judges, each answer's mean >=1.5, and
no judge marking fabricated_fact. At least 90% of all traps must pass.

Paired intervals sample whole source groups and preserve query weights. Fewer
than 12 independent groups cannot support a significant-improvement claim. Text
changes and score variations are diagnostics. The result remains
`calibration_status="uncalibrated"`, `false_positive_bound=null`; no run-level
false-positive guarantee follows from four judgments. The optional independent
null audit is not implemented by this package.

Baseline creation only creates a new receipt outside its source run, preserving
all file hashes. Existing baselines are never overwritten. A receipt gate
rehashes every file and recomputes its summary before comparing two runs. Reusing
the original run requires explicit --diagnostic and cannot be release evidence.

The synthetic records in `tests/answer_eval_fixtures.py` are unit test inputs,
including tests of the real-record reader branch. They provide no genuine model
grading evidence and must never be installed as `evals/answer_v3` results.
