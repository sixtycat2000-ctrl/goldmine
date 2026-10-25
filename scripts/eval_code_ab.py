"""Code-retrieval A/B arm runner (digup-plan-001 P1-1+P1-2).

One invocation = one arm: opens an already-ingested db, runs the frozen
query set (evals/code_queries.jsonl) against the corpus snapshot root,
reuses eval_real's metric core verbatim (score_query/summarize — file-level
grading via the no-`lines` expect branch), and writes a per-arm receipt
fragment. The adoption comparison lives in the execution report, not here.

Run shape (each arm from ITS OWN tree so `import goldmine` resolves to that
arm's code):

  PYTHONPATH=<tree>/scripts \
  GOLDMINE_LLAMA_SERVER=<abs>/vendor/llama.cpp/build/bin/llama-server \
  GOLDMINE_MODEL=<abs>/models/embeddinggemma-2-Q8_0.gguf \
  python3 scripts/eval_code_ab.py --arm base --db /tmp/ab/base.sqlite \
      --fixture-root <abs>/evals/code_corpus/209e309 --out /tmp/ab/base.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval_real import run_eval, summarize  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def load_code_queries(path: Path) -> list[dict]:
    queries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        queries.append(json.loads(line))
    return sorted(queries, key=lambda q: q["id"])


def subset_metrics(rows: list[dict], tag: str) -> dict:
    sub = [r["metrics"] for r in rows if r["query"]["tag"] == tag]
    if not sub:
        return {}
    n = len(sub)
    ndcg = [m["ndcg"] for m in sub if m["ndcg"] is not None]
    return {
        "n": n,
        "success1": sum(m["success1"] for m in sub) / n,
        "success5": sum(m["success5"] for m in sub) / n,
        "mrr": sum(m["rr"] for m in sub) / n,
        "ndcg10": (sum(ndcg) / len(ndcg)) if ndcg else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, help="label: base | new")
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--fixture-root", type=Path,
                    default=REPO / "evals/code_corpus/209e309")
    ap.add_argument("--queries", type=Path, default=REPO / "evals/code_queries.jsonl")
    ap.add_argument("--backend", default="llama")
    ap.add_argument("--scorer", default="hybrid")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    queries = load_code_queries(args.queries)
    rows = run_eval(args.db, args.backend, queries, args.fixture_root,
                    scorer=args.scorer)

    targeted = [r for r in rows if r["query"]["tag"] != "noise"]
    noise = [r for r in rows if r["query"]["tag"] == "noise"]

    # predicate audit: which queries fire is_code_query under THIS arm's code
    # (baseline predates the predicate — audit records None, not a failure)
    audit: dict[str, bool | None] = {}
    try:
        from goldmine.search import is_code_query
        for q in queries:
            audit[q["id"]] = is_code_query(q["q"])
    except ImportError:
        for q in queries:
            audit[q["id"]] = None

    receipt = {
        "arm": args.arm,
        "queries_path": str(args.queries),
        "fixture_root": str(args.fixture_root),
        "db": str(args.db),
        "backend": args.backend,
        "scorer": args.scorer,
        "overall": summarize(targeted),
        "by_tag": {
            tag: subset_metrics(rows, tag)
            for tag in ("ident", "words", "brand")
        },
        "predicate_audit": audit,
        "noise_zero_hits": {
            r["query"]["id"]: r["metrics"]["n_hits"] == 0 for r in noise
        },
        "per_query": [
            {
                "id": r["query"]["id"],
                "tag": r["query"]["tag"],
                "metrics": r["metrics"],
                "hit_paths": r["hit_paths"],
            }
            for r in rows
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, indent=1), encoding="utf-8")

    ov = receipt["overall"]
    print(
        f"arm={args.arm} n={ov['success@5']['n']} "
        f"s@1={ov['success@1']['value']:.3f} s@5={ov['success@5']['value']:.3f} "
        f"mrr={ov['MRR']['value']:.3f}"
    )
    for tag, m in receipt["by_tag"].items():
        if m:
            print(f"  {tag}: n={m['n']} s@5={m['success5']:.3f} mrr={m['mrr']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
