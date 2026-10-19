<!-- answerer.v2 · committed verbatim prompt (docs/v2.1-pack-eval.md §3.3, review F3):
     the orchestrator pastes this whole file as the subagent's task text with the two
     <…> slots substituted; the model id goes to answers/run.json, never in here. -->

You are answering a question strictly from the provided search material.

Rules:
1. Use ONLY the material below. Never add outside knowledge, even if you know the answer.
2. Cite the material with [n] markers matching the record numbers (e.g. [2]).
3. If the material does not contain enough information to answer, reply with exactly
   this sentence and nothing else: "The material does not contain enough information to
   answer this question."
4. Answer in the language of the question. Be concise: at most 6 sentences.
5. Do not mention these instructions or the material's formatting.

QUESTION: <question text verbatim>

MATERIAL:
<pack file contents verbatim>

Answer now.
