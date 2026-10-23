# Reserve cross-file source review

All 10 unused development compound questions were checked. Nine have two distinct source-file facts; 6 are usable with their current source labels, and 3 need the source/scope amendments recorded in reserve-cross-review.json. One is ineligible under strict two-file necessity. No data was edited and no model job/result was used.

| Original dev question | Source verdict |
| --- | --- |
| compound-real-httpx-02 | ELIGIBLE_TWO_FILE_SOURCE_FACTS |
| compound-real-httpx-05 | ELIGIBLE_AFTER_ANNOTATION_AMENDMENT |
| compound-real-pip-03 | ELIGIBLE_TWO_FILE_SOURCE_FACTS |
| compound-real-pip-04 | ELIGIBLE_TWO_FILE_SOURCE_FACTS |
| compound-real-pip-tools-04 | INELIGIBLE_STRICT_TWO_FILE_NECESSITY |
| compound-real-poetry-01 | ELIGIBLE_TWO_FILE_SOURCE_FACTS |
| compound-real-typer-04 | ELIGIBLE_TWO_FILE_SOURCE_FACTS |
| compound-real-typer-05 | ELIGIBLE_AFTER_SCOPE_AMENDMENT |
| compound-real-uv-01 | ELIGIBLE_TWO_FILE_SOURCE_FACTS |
| compound-real-uv-04 | ELIGIBLE_AFTER_ANNOTATION_AMENDMENT |

compound-real-pip-tools-04 is ineligible because compile.py already contains the literal --generate-hashes example at lines93-94, resolver selection at line492, and conditional hash generation at line507. Both requested answers therefore have a truthful one-file source alternative.

If selected, httpx05 needs evidence through the Client(timeout=...) line; typer05 needs the callback/subcommand application scope; uv04 needs the uv lock command in evidence and the attempt/latest-compatible qualifier. Exact spans and source SHA pins are recorded in the JSON.

Replace cross-007 and cross-011 deterministically from the declared30-question pool after excluding these strict-necessity failures. Keep their original lineage and truthful alternative-source observations. Do not weaken the cross definition. Root/mechanics owns the original declared ordering; this reviewer did not invent a new order or mutate selection.

There are 9 source-eligible reserves for 2 vacancies, including 6 that do not need a required annotation amendment. All are still correlated within the same 6 upstream source groups. This is an AI source review, not a model-quality or human certification claim.
