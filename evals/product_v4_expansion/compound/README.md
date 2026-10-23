# Real cross-file compound source shard

`shard.json` contains 60 new questions, 24 complete original upstream files,
12 independent repository groups, and 120 exact source fact spans. Every
question requires two facts from two distinct files. Each repository contributes
five questions; each file is used by five questions, below the eight-question
limit. This is a source-first draft, not a frozen/audited benchmark or a quality
result. No model, recognition, retrieval, ranking or holdout run was used.

Copy `assets/`, `views/`, and `licenses/` into the aggregate bundle unchanged.
References begin `assets/compound/`, `views/compound/`, and
`licenses/compound/`. Corpus paths begin `compound/<repo>/` and preserve actual
upstream paths/extensions. Every source has an immutable full Git commit and
complete original file bytes, whose SHA256 is checked against `acquisition.json`.
The licence files retain the original upstream permissions/copyright notices.
Oracle views apply only NFC/LF normalization to the source text. Do not index
questions, views, licences, or compiler/provenance records as source content.

The repository families are FastAPI, Rich, pytest, Pydantic, HTTPX, Ruff, Poetry,
Typer, uv, pip, pip-tools and Flask. All use `source_group=upstream:<repoid>` so
related PDF manuals and real page screenshots remain in the same evaluation
fold. This includes `upstream:pytest`, rather than treating its RST files as
independent from the pytest PDF manual. The real RST names were retained; the
production unsupported-extension gap was reported separately for correction.

Two incomplete cached pairs were repaired with genuine sources. The missing
pip-tools documentation URL was replaced with the actual
`piptools/scripts/compile.py` at the already pinned commit, paired with its
README. The Flask Git transport retry succeeded and pinned
`d086db856be187255b8ec61ef409357393020f32`; its genuine async-await and errorhandling
RST files and BSD licence were downloaded. No generated documentation or
template was substituted. GitHub's unauthenticated API quota was unavailable;
ordinary first-party Git/raw-source acquisition was used without an API quota
workaround.

`annotations.json` is the manually authored source-first question/fact worksheet.
`build_shard.py` computes unique contiguous Unicode spans, real line locations,
source/view/licence/span hashes and typed labels. The actual product bundle
source and label validators passed all 24 sources and all 60 questions. See
`validation.json` for the exact structural results. Independent label auditing,
merging, grouped splitting and any later quality runs remain separate steps.
