/*
 * REAL RUN OUTPUT (amendments-006 R1): verbatim stdout of the actual machine,
 * captured on this machine on 2026-11-02:
 *
 *   python3 -m goldmine search "copper gasket" --db evals/eval.sqlite --no-color
 *   → exit 0
 *
 * The database is the repository's own frozen eval library; the search is
 * read-only. These numbers are not authored copy — they are the program's
 * output, shown byte-for-byte (gate G7 exempts this file for that reason).
 */
export const REAL_RUN_META = {
  command: 'python3 -m goldmine search "copper gasket" --db evals/eval.sqlite',
  note_zh: "真实运行 · 仓库自带评测库 · 只读检索",
  note_en: "real run · the repo's own eval library · read-only search",
} as const;

export const REAL_RUN_LINES: string[] = [
  'via: cold (2.18s)',
  '1. score 2.33  z 0.33  cov 1.00   code   /Users/yul/git/goldmine/scripts/probe_reranker.py  L47-75',
  '   "…timeout") canaries = [ ("What is the capital of France?", "Paris is the capital of France.", "«Copper» «gaskets» should be inspected on Tuesdays."), ("How do I sort a Python list in place?", "Use list.sort()…"',
];
