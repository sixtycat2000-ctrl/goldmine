#!/usr/bin/env python3
"""Relevance eval harness (TEST_PLAN §5): synthetic corpus -> index -> score.

Deterministic ~30-doc corpus across 6 topics; 12 queries with known targets.
Runs every scorer (hybrid | lexical | semantic | rrf) and reports R@1 / R@5 /
MRR overall and on the lookup subset. Quality judgments between scorers are
REPORTED, not asserted — the harness exists to make the τ=0.70 / near-dup 0.97
defaults falsifiable.

Usage:
  python3 scripts/eval_relevance.py                 # mock backend, offline
  python3 scripts/eval_relevance.py --backend llama # real embeddings
  python3 scripts/eval_relevance.py --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from goldmine.backends import create_backend  # noqa: E402
from goldmine.config import Config  # noqa: E402
from goldmine.ingest import IngestPipeline  # noqa: E402
from goldmine.search import Searcher  # noqa: E402
from goldmine.store import Store  # noqa: E402

# ------------------------------------------------------------------ corpus

TOPIC_TERMS = {
    "k8s": (
        "kubernetes pod eviction scheduler kubelet taint toleration node pressure "
        "workload cluster container restart backoff limit"
    ),
    "sourdough": (
        "sourdough starter feeding flour hydration bulk ferment proofing crust "
        "oven spring levain autolyse stretch fold crumb"
    ),
    "pcbuild": (
        "TRX50 motherboard bios chipset memory expo pcie lanes Cooler thermal "
        "paste fan curve power supply wattage build"
    ),
    "rome": (
        "roman republic consul senate plebeian tribune punic wars caesar legion "
        "gracchi marius sulla empire republic aquila"
    ),
    "pandas": (
        "pandas dataframe groupby merge index multiindex loc iloc NaNfillna "
        "pivot melt apply vectorize dtype chunk"
    ),
    "lease": (
        "apartment lease tenant landlord deposit notice eviction rent clause "
        "renewal sublet inspection habitability terminate"
    ),
}

# 5 docs carry a lookup token whose answer lives in exactly one doc
LOOKUP_TOKENS = {
    "k8s-2": "K7Q2LM",
    "pcbuild-1": "TRX50",
    "pandas-3": "ZB4400",
    "lease-2": "RF7712",
    "rome-3": "SPQR44",
}

PARAGRAPHS = [
    "{terms}. This note {idline} walks through the operational details and the "
    "practical trade-offs an engineer or hobbyist meets along the way.",
    "The second paragraph of {id} expands on the core procedure: {terms}. "
    "Every step is written down so a colleague can reproduce it later.",
    "A third paragraph records the failure modes seen in practice with {terms}, "
    "and how the checklist prevents most of them. {extra}",
    "Background reading for {id}: the vocabulary above ({first_words}) is the "
    "fastest way to find this document again from a keyword search.",
    "Finally, {id} keeps a short changelog: dates, owners, and the one-line "
    "summary of each revision touching {terms}.",
]


@dataclass
class Doc:
    doc_id: str
    topic: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


def build_corpus() -> list[Doc]:
    """30 docs / 6 topics, fully deterministic (no RNG at all)."""
    plan = [
        ("k8s", 5), ("sourdough", 5), ("pcbuild", 5),
        ("rome", 5), ("pandas", 5), ("lease", 5),
    ]
    docs: list[Doc] = []
    for topic, count in plan:
        terms = TOPIC_TERMS[topic]
        for n in range(1, count + 1):
            doc_id = f"{topic}-{n}"
            extra = ""
            if doc_id in LOOKUP_TOKENS:
                extra = (
                    f"The reference code for this document is {LOOKUP_TOKENS[doc_id]}; "
                    "quote it when filing follow-ups."
                )
            paragraphs = [
                template.format(
                    id=doc_id,
                    idline=f"({doc_id})",
                    terms=terms,
                    extra=extra,
                    first_words=" ".join(terms.split()[:4]),
                )
                for template in PARAGRAPHS
            ]
            docs.append(Doc(doc_id=doc_id, topic=topic, paragraphs=paragraphs))

    # 3 near-duplicate pairs (one sentence changed) — exercise dedupe, and the
    # duplicate shares the topic so R@5 credit still applies
    dup_ids = ["k8s-1", "sourdough-1", "pandas-1"]
    for doc_id in dup_ids:
        original = next(d for d in docs if d.doc_id == doc_id)
        twin = Doc(
            doc_id=doc_id + "-twin",
            topic=original.topic,
            paragraphs=list(original.paragraphs),
        )
        twin.paragraphs[1] += " One sentence differs in this near-duplicate revision."
        docs.append(twin)

    # 2 hard negatives sharing vocabulary across topics
    cross1 = Doc(
        doc_id="cross-1", topic="k8s",
        paragraphs=[(
            "Cross-topic memo: pandas groupby on a dataframe of kubernetes pod "
            "eviction events; the lease of a node resembles a tenant clause."
        )],
    )
    cross2 = Doc(
        doc_id="cross-2", topic="lease",
        paragraphs=[(
            "Cross-topic memo: the roman republic appointed a consul like a "
            "scheduler assigns workloads; sourdough metaphors included."
        )],
    )
    docs.extend([cross1, cross2])
    return docs


# ------------------------------------------------------------------ queries

#: (query, target doc, topic siblings for R@5 credit, is_lookup)
QUERIES: list[tuple[str, str, list[str], bool]] = [
    ("pod eviction scheduler toleration pressure", "k8s-3", ["k8s-1", "k8s-2", "k8s-4", "k8s-5"], False),
    ("kubernetes node taint restart backoff", "k8s-4", ["k8s-1", "k8s-2", "k8s-3", "k8s-5"], False),
    ("the reference code K7Q2LM", "k8s-2", ["k8s-1", "k8s-3", "k8s-4", "k8s-5"], True),
    ("sourdough starter feeding hydration autolyse", "sourdough-2", ["sourdough-1", "sourdough-3", "sourdough-4", "sourdough-5"], False),
    ("oven spring crust crumb levain", "sourdough-3", ["sourdough-1", "sourdough-2", "sourdough-4", "sourdough-5"], False),
    ("TRX50 bios chipset memory expo", "pcbuild-1", ["pcbuild-2", "pcbuild-3", "pcbuild-4", "pcbuild-5"], True),
    ("roman republic consul senate punic wars", "rome-1", ["rome-2", "rome-3", "rome-4", "rome-5"], False),
    ("reference code SPQR44 follow-up", "rome-3", ["rome-1", "rome-2", "rome-4", "rome-5"], True),
    ("pandas dataframe groupby merge multiindex", "pandas-2", ["pandas-1", "pandas-3", "pandas-4", "pandas-5"], False),
    ("reference code ZB4400", "pandas-3", ["pandas-1", "pandas-2", "pandas-4", "pandas-5"], True),
    ("apartment lease tenant deposit notice clause", "lease-1", ["lease-2", "lease-3", "lease-4", "lease-5"], False),
    ("RF7712 renewal inspection", "lease-2", ["lease-1", "lease-3", "lease-4", "lease-5"], True),
]
ZERO_HIT_QUERY = "zzzq wubblezort"


# ------------------------------------------------------------------- harness


def index_corpus(index_home: Path, backend_name: str = "mock") -> tuple[Store, object, Config]:
    corpus_dir = index_home / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for doc in build_corpus():
        (corpus_dir / f"{doc.doc_id}.txt").write_text(doc.text, encoding="utf-8")

    cfg = Config(home=str(index_home))
    cfg.backend = backend_name
    store = Store(index_home / "eval.sqlite")
    backend = create_backend(cfg, backend_name)
    pipeline = IngestPipeline(cfg, store, backend)
    report = pipeline.run([str(corpus_dir)])
    assert report.failed == 0, f"eval corpus ingest failed: {report.failures}"
    return store, backend, cfg


def rank_of_target(hits, target: str, siblings: list[str]) -> tuple[int | None, int | None]:
    """1-based rank of the target doc (by filename) and of the first sibling."""
    paths = [Path(h.path).stem for h in hits]
    target_rank = paths.index(target) + 1 if target in paths else None
    sibling_rank = next(
        (i + 1 for i, stem in enumerate(paths) if stem in siblings), None
    )
    return target_rank, sibling_rank


def run_eval(index_home: Path, backend_name: str = "mock") -> dict:
    store, backend, _cfg = index_corpus(index_home, backend_name)
    try:
        searcher = Searcher(store, backend)
        scorers = ("hybrid", "lexical", "semantic", "rrf")
        results: dict[str, dict] = {}
        for scorer in scorers:
            rows = []
            for query, target, siblings, is_lookup in QUERIES:
                hits = searcher.search(query, limit=5, scorer=scorer)
                target_rank, sibling_rank = rank_of_target(hits, target, siblings)
                rows.append({
                    "query": query,
                    "target": target,
                    "lookup": is_lookup,
                    "target_rank": target_rank,
                    "credit_rank": target_rank or sibling_rank,  # R@5 credit set
                })
            results[scorer] = _summarize(rows)
        # zero-hit honesty: recorded per scorer; lexical MUST be empty (no term
        # matches), hybrid/semantic may surface z-gate candidates by design
        results["_zero_hit"] = {
            "query": ZERO_HIT_QUERY,
            "hits": {
                scorer: len(searcher.search(ZERO_HIT_QUERY, limit=5, scorer=scorer))
                for scorer in scorers
            },
        }
        return results
    finally:
        store.close()
        backend.close()


def _summarize(rows: list[dict]) -> dict:
    def rates(subset):
        n = len(subset)
        r1 = sum(1 for r in subset if r["target_rank"] == 1) / n
        r5 = sum(1 for r in subset if (r["credit_rank"] or 99) <= 5) / n
        mrr = sum(
            1.0 / r["target_rank"] for r in subset if r["target_rank"]
        ) / n
        return {"R@1": round(r1, 3), "R@5": round(r5, 3), "MRR": round(mrr, 3)}

    lookup = [r for r in rows if r["lookup"]]
    non_lookup = [r for r in rows if not r["lookup"]]
    return {
        "overall": rates(rows),
        "lookup": rates(lookup),
        "non_lookup": rates(non_lookup),
        "per_query": rows,
    }


def render_table(results: dict) -> str:
    lines = [
        f"eval: relevance over {len(QUERIES)} queries "
        f"(target-or-sibling-in-top-5 credit; lookup subset n="
        f"{sum(1 for q in QUERIES if q[3])})",
        f"{'scorer':<10} {'overall R@1':>11} {'R@5':>6} {'MRR':>6}   "
        f"{'lookup R@1':>10} {'R@5':>6} {'MRR':>6}",
    ]
    for scorer in ("hybrid", "lexical", "semantic", "rrf"):
        s = results[scorer]
        o, l = s["overall"], s["lookup"]
        lines.append(
            f"{scorer:<10} {o['R@1']:>11.3f} {o['R@5']:>6.3f} {o['MRR']:>6.3f}   "
            f"{l['R@1']:>10.3f} {l['R@5']:>6.3f} {l['MRR']:>6.3f}"
        )
    zero = results.get("_zero_hit", {})
    hits = zero.get("hits", {})
    zero_str = ", ".join(f"{s} {n}" for s, n in hits.items()) if hits else "n/a"
    lines.append(f"zero-hit query {zero.get('query')!r} -> hits: {zero_str} (exit-0 path)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goldmine relevance eval")
    parser.add_argument("--backend", default="mock", help="mock | llama")
    parser.add_argument("--home", default=None, help="work dir (default: tmp)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    import tempfile

    home = Path(args.home) if args.home else Path(tempfile.mkdtemp(prefix="gm-eval-"))
    results = run_eval(home, backend_name=args.backend)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(render_table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
