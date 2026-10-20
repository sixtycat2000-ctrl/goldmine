# Optional local reranking experiment

The fixed candidate is Qwen3-Reranker-0.6B, converted with its classification head
to `gorse-io/Qwen3-Reranker-0.6B-GGUF`. This is a third-party GGUF conversion of the
[Qwen model](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B), not a new embedding
model. The [conversion card](https://huggingface.co/gorse-io/Qwen3-Reranker-0.6B-GGUF)
describes the rank-head conversion.

| Pin | Value |
|---|---|
| Revision | `257c2d8ebe01523d5a93bd9eab2698b9c7f95ee1` |
| File | `Qwen3-Reranker-0.6B-Q8_0.gguf` |
| Bytes | 639153376 |
| SHA256 | `a4e436162177a8b147940cd40fc952ca42ce91d39b0ca3247fa94770a4cb9990` |
| License | Apache-2.0 |
| Tested runtime source | llama.cpp `4d756bc72bf00a4aacf410ae15a2d315f3db400d`, b11461, existing reviewed patch |

Installation is explicit: `goldmine model install Qwen3-Reranker-0.6B-Q8_0`.
Search/MCP never download it. `--mode quality --rerank local` loads a separate
request-owned server. At most twenty already admitted segments are ranked; each
uses at most 384 tokens of this model's tokenizer. The source query is bounded
to 256 tokens, and truncation is reported. Equal scores use stable segment IDs.
The whole quality request, including startup/tokenization, has a ten-second
deadline. Invalid confidence/index sets or unavailable assets preserve the
original ranking with an explicit fallback reason; expired/cancelled requests
stop. Request completion closes the owned server. There is no reused idle model.

The 2026-10-21 capability probe tested the actual `/tokenize` and `/v1/rerank`
endpoints with English, code and Chinese canaries in both document orders:
six checks passed. A real backend test also verified a long Chinese input was
bounded to 384 tokens and the child was reaped. Raw local evidence is under
`reports/execution/P1-07/`.

This establishes runtime compatibility only. The complete product train/dev set
does not exist yet; nDCG improvement, simple-query regressions, no-answer gates and
quality-mode resource gates are unmeasured. The feature remains off by default
and reports `quality_validation=unvalidated_experiment`. It is neither an accepted
quality optimization nor an experiment rejected for lack of compatibility.
