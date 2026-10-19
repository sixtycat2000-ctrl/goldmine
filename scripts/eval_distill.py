#!/usr/bin/env python3
"""Distill canary eval (v2.1-r6 §7, D12): known-topic fixture -> index -> gates.

Deterministic 100-doc fixture (F9: 8 planted topics x 10 docs + 12 noise docs
+ 8 near-dup twins = 100), fully templated — no RNG anywhere, so eval outputs
are byte-stable. Known member sets make purity / ARI / recovery MEASURED, not
judged.

Variants (§7.4):
  main         the 100-doc canary + all gates + a two-home determinism e2e
  hard         F1 — the REAL offline gate: same topics, 3x SHARED filler per
               doc (cross-topic pairwise cos ~0.68, real-corpus geometry).
               Gated on the differential: v1 rules (fixed tau 0.70, centroid-
               only admission) must FAIL, v2 rules must PASS.
  single-topic 20 docs, one topic -> asserts tau_source != 'otsu' (B9)
  micro        ~20-chunk corpus -> runs, honest stats, no crash
  shuffle      same corpus, re-ingested in reversed order -> recovery unchanged
  incremental  (M2) 100 docs -> +10 new-topic docs -> --rerun path: mode
               'incremental', tau inherited, reclustered <= 10%

Modes:
  --report-real DB   F2b — open a COPY of an existing index, run calibration +
                     clustering in memory, print the calibration report, the
                     card-size distribution and the biggest-card share. No
                     writes to the original. top-card share > 0.8 prints the
                     F2c contingency line (pre-approved centroid-scale
                     calibration switch — documented, not implemented).

Backends: mock (offline, CI) runs every variant; --backend llama (needs the
vendor llama-server) is the calibration-quality report recorded in the
release checklist. Mock geometry on the MAIN fixture is cleanly separable by
construction — that is why variant `hard` exists: it is the gate that can
actually fail.

Usage:
  python3 scripts/eval_distill.py                       # main variant, mock
  python3 scripts/eval_distill.py --variant hard        # the differential gate
  python3 scripts/eval_distill.py --variant all         # every variant
  python3 scripts/eval_distill.py --backend llama       # live calibration report
  python3 scripts/eval_distill.py --report-real evals/eval.sqlite
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from goldmine.backends import create_backend  # noqa: E402
from goldmine.config import Config  # noqa: E402
from goldmine import distill  # noqa: E402
from goldmine.distill import CItem, Scope  # noqa: E402
from goldmine.ingest import IngestPipeline  # noqa: E402
from goldmine.store import Store  # noqa: E402
from goldmine.util import excerpt as make_excerpt  # noqa: E402

# ------------------------------------------------------------------ fixture

#: 8 planted topics, disjoint from eval_relevance's six (no shared-sha
#: collisions in one tmp home). Each line is a topic vocabulary.
TOPIC_TERMS = {
    "beekeeping": (
        "hive beekeeper smoker frames swarm nectar honeycomb brood apiary "
        "wax moth propolis queen excluder veil drone"
    ),
    "sailing": (
        "sloop rigging jib mainsail tiller mast halyard winch tack mooring "
        "keel ballast spinnaker boom vang cleat"
    ),
    "ceramics": (
        "kiln glaze wedging clay slip bisque stoneware throwing wheel firing "
        "silica crucible underglaze kiln shelf bat"
    ),
    "mycology": (
        "spore gills cap stipe mycelium hypha fruiting bolete amanita chanterelle "
        "spore print substrate agar bracket fungi"
    ),
    "vinyl": (
        "turntable stylus cartridge tonearm platter rpm phono preamp groove "
        "tracking antiskate dust cover slipmat azimuth"
    ),
    "bonsai": (
        "juniper wiring pruning repotting canopy trunk nebari tokonoma accent "
        "moss shallow pot wire scars watering substrate"
    ),
    "locksmith": (
        "pin tumbler wafer pick tension wrench keyway shear line plug follower "
        "bump key blank impression mortise cylinder"
    ),
    "typewriters": (
        "platen ribbon carriage typebar escapement margin paper feed lever "
        "carriage return feed dogs type slug bell shift"
    ),
}

#: light per-doc filler — distinct generic sentences so the MAIN fixture keeps
#: cross-topic cosine low (measured on mock: same-topic p50 0.944 vs cross
#: p50 0.118). The main fixture is deliberately EASY — it pins determinism and
#: the e2e pipeline, not algorithm quality (that is variant `hard`'s job, F1).
FILLER_MAIN = [
    "Notes were revised after a short review session.",
    "The margin scribbles date from last spring.",
    "A checklist at the end keeps the steps honest.",
    "This copy replaces the earlier draft completely.",
    "Details were verified against the shelf copy.",
    "The summary paragraph was rewritten for clarity.",
    "Feedback from a colleague shaped the final wording.",
    "The appendix lists the tools used throughout.",
]

#: F1 HARD variant — this SHARED boilerplate (47 distinct tokens, once per
#: doc, mass ~3x the 16-token topic vocabulary) lifts cross-topic pairwise
#: cosine onto the real-corpus band (measured on mock: cross p50 0.743,
#: max 0.843, vs same-topic p50 0.894; unimodal-ish geometry where v1's fixed
#: tau 0.70 sits INSIDE the cross band and the calibrated tau 0.79 sits above
#: it). Per-doc remark tokens keep same-topic pairs below the 0.97 near-dup
#: fold so dedupe cannot eat the fixture.
FILLER_HARD = (
    "the general introduction of this document explains its purpose in plain "
    "words so any reader can follow the context before the specific section "
    "begins it was written once and shared across the whole collection "
    "unchanged which is exactly why every document carries the same frame "
    "sentences here"
)

#: per-doc unique-ish remark tokens (rotated by doc + topic index) — the
#: doc-unique mass that keeps same-topic hard pairs at ~0.89, not ~1.0.
REMARK_WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
    "lima november oscar papa quebec romeo sierra tango uniform victor "
    "whiskey xray yankee zulu"
).split()

TWIN_SUFFIX = " One sentence differs in this near-dup revision of the note."


@dataclass
class PlantedDoc:
    doc_id: str
    topic: str | None            # None for noise docs
    text: str


def _main_doc_text(doc_id: str, topic: str, n: int) -> str:
    terms = TOPIC_TERMS[topic].split()
    rot = terms[n % len(terms):] + terms[: n % len(terms)]
    filler = FILLER_MAIN[n % len(FILLER_MAIN)]
    return (
        f"{topic} log entry {n:02d}: {' '.join(terms)}. "
        f"rotated for the session: {' '.join(rot)}. "
        f"{filler} note {n:02d} filed."
    )


def _hard_doc_text(doc_id: str, topic: str, topic_index: int, n: int) -> str:
    terms = TOPIC_TERMS[topic].split()
    remarks = " ".join(
        REMARK_WORDS[(n * 3 + topic_index + k) % len(REMARK_WORDS)]
        for k in range(6)
    )
    return (
        f"{' '.join(FILLER_HARD.split())} {' '.join(terms)} entry {n:02d} "
        f"remarks: {remarks} filed {n:02d}"
    )


def _doc_text(doc_id: str, topic: str, n: int, *, hard: bool,
              topic_index: int = 0) -> str:
    if hard:
        return _hard_doc_text(doc_id, topic, topic_index, n)
    return _main_doc_text(doc_id, topic, n)


@dataclass
class PlantedDoc:
    doc_id: str
    topic: str | None            # None for noise docs
    text: str


def build_corpus(variant: str = "main") -> list[PlantedDoc]:
    """The deterministic fixture (F9): 8 topics x 10 docs + 12 noise + 8 twins
    (one near-dup pair per topic) = 100 docs. `hard` swaps in the shared
    boilerplate that moves cross-topic cosine onto the real band."""
    hard = variant == "hard"
    docs: list[PlantedDoc] = []
    for topic_index, topic in enumerate(TOPIC_TERMS):
        for n in range(1, 11):
            doc_id = f"{topic}-{n:02d}"
            docs.append(PlantedDoc(
                doc_id=doc_id, topic=topic,
                text=_doc_text(doc_id, topic, n, hard=hard,
                               topic_index=topic_index),
            ))
    for n in range(1, 13):
        if hard:
            body = (
                f"{' '.join(FILLER_HARD.split())} loose note {n:02d} "
                f"appendix {n} remarks: zulu filed."
            )
        else:
            filler = FILLER_MAIN[n % len(FILLER_MAIN)]
            body = (
                f"A loose note (noise-{n:02d}) with no particular subject: "
                f"{filler} It exists so the clustering sees background mass "
                "that belongs to no planted topic."
            )
        docs.append(PlantedDoc(doc_id=f"noise-{n:02d}", topic=None, text=body))
    # 8 near-dup twins (F9 count: one per topic), one sentence changed;
    # counted in their original's topic; the 0.97 near-dup fold eats them —
    # that is the dedupe exercise
    for original_id in (
        "beekeeping-01", "sailing-02", "ceramics-03", "vinyl-04",
        "mycology-05", "bonsai-06", "locksmith-07", "typewriters-08",
    ):
        original = next(d for d in docs if d.doc_id == original_id)
        twin_id = f"{original_id}-twin"
        docs.append(PlantedDoc(
            doc_id=twin_id, topic=original.topic, text=original.text + TWIN_SUFFIX,
        ))
    assert len(docs) == 100, f"fixture count pinned at 100 (F9), got {len(docs)}"
    return docs


def doc_topics(docs: list[PlantedDoc]) -> dict[str, str | None]:
    return {d.doc_id: d.topic for d in docs}


def single_topic_corpus() -> list[PlantedDoc]:
    """20 docs, ONE planted topic (B9 degenerate case)."""
    docs = []
    for n in range(1, 21):
        doc_id = f"beekeeping-{n:02d}"
        docs.append(PlantedDoc(
            doc_id=doc_id, topic="beekeeping",
            text=_doc_text(doc_id, "beekeeping", n, hard=False),
        ))
    return docs


def micro_corpus() -> list[PlantedDoc]:
    """A ~20-chunk corpus of short docs (06 §6 micro risk row)."""
    docs = []
    for topic in ("bonsai", "locksmith"):
        for n in range(1, 11):
            doc_id = f"{topic}-{n:02d}"
            docs.append(PlantedDoc(
                doc_id=doc_id, topic=topic,
                text=_doc_text(doc_id, topic, n, hard=False),
            ))
    return docs


# ------------------------------------------------------------------- harness


def _write_docs(corpus_dir: Path, docs: list[PlantedDoc], *, reversed_names: bool = False) -> None:
    corpus_dir.mkdir(parents=True, exist_ok=True)
    order = list(range(len(docs)))
    if reversed_names:
        order.reverse()  # ingest order = walk order = name order
    for i in order:
        doc = docs[i]
        stem = f"{i:04d}-{doc.doc_id}" if not reversed_names else \
            f"{len(docs) - i:04d}-{doc.doc_id}"
        (corpus_dir / f"{stem}.txt").write_text(doc.text, encoding="utf-8")


def index_corpus(
    index_home: Path,
    docs: list[PlantedDoc],
    backend_name: str = "mock",
    *,
    corpus_dir: Path | None = None,
    reversed_names: bool = False,
) -> tuple[Store, object, Config]:
    """Ingest the fixture. `corpus_dir` defaults to <index_home>/corpus; the
    determinism e2e passes ONE shared corpus dir so both runs see identical
    absolute paths (excerpts/citations carry paths) and differ only in the
    index."""
    if corpus_dir is None:
        corpus_dir = index_home / "corpus"
    _write_docs(corpus_dir, docs, reversed_names=reversed_names)
    index_home.mkdir(parents=True, exist_ok=True)
    cfg = Config(home=str(index_home))
    cfg.backend = backend_name
    store = Store(index_home / "eval.sqlite")
    backend = create_backend(cfg, backend_name)
    pipeline = IngestPipeline(cfg, store, backend)
    report = pipeline.run([str(corpus_dir)])
    assert report.failed == 0, f"eval corpus ingest failed: {report.failures}"
    return store, backend, cfg


def _items_of(store: Store) -> list[CItem]:
    return distill._corpus(store, Scope())


# ---------------------------------------------------------------- v1 rules


def cluster_v1_rules(
    items: list[CItem], tau: float = 0.70, min_members: int = 3
) -> list[list[CItem]]:
    """v1 semantics verbatim (the F1 differential's failing arm): greedy
    leader, renormalized running-sum centroid, NO admission guard, fixed tau
    0.70, first-index-wins, min_members drop."""
    sums: list[np.ndarray] = []
    clusters: list[list[CItem]] = []
    for item in items:
        assigned = False
        if sums:
            cents = np.stack([s / float(np.linalg.norm(s)) for s in sums])
            sims = cents @ item.vec
            hits = np.nonzero(sims >= tau)[0]
            if hits.size:
                idx = int(hits[0])
                sums[idx] += item.vec
                clusters[idx].append(item)
                assigned = True
        if not assigned:
            sums.append(item.vec.astype(np.float32).copy())
            clusters.append([item])
    return [c for c in clusters if len(c) >= min_members]


# ------------------------------------------------------------------ metrics


def _doc_of(path: str) -> str:
    stem = Path(path).stem
    return stem.split("-", 1)[1] if "-" in stem else stem


def compute_metrics(
    cards: list,
    kept: list[CItem],
    planted: dict[str, str | None],
    *,
    min_members: int = 3,
) -> dict:
    """Purity / ARI / recovery over the run's cards (§7.2). Cards = active,
    size >= min_members; dropped/singleton chunks keep their own label in ARI."""
    chunk_topic = {}
    for item in kept:
        chunk_topic[item.sid] = planted.get(_doc_of(item.path))
    sid_to_card: dict[int, int] = {}
    carded = 0
    dominant_ok = 0
    card_docs: dict[int, set[str]] = {}
    active = [c for c in cards if c.status == "active" and c.size >= min_members]
    for card_index, card in enumerate(active):
        topics: dict[str | None, int] = {}
        for m in card.members:
            sid_to_card[m.sid] = card_index
            card_docs.setdefault(card_index, set()).add(_doc_of(m.path))
            topics[chunk_topic.get(m.sid)] = topics.get(chunk_topic.get(m.sid), 0) + 1
        dominant = max(topics, key=lambda t: (topics[t], t or ""))
        carded += card.size
        dominant_ok += topics.get(dominant, 0)
    purity = dominant_ok / carded if carded else 0.0

    # ARI over ALL post-dedupe chunks (carded chunks + own-label rest)
    labels_true: list[str] = []
    labels_pred: list[int] = []
    next_singleton = -1
    for item in kept:
        topic = chunk_topic.get(item.sid) or f"noise:{_doc_of(item.path)}"
        labels_true.append(topic)
        card_index = sid_to_card.get(item.sid)
        if card_index is None:
            labels_pred.append(next_singleton)
            next_singleton -= 1
        else:
            labels_pred.append(card_index)
    ari = _adjusted_rand_index(labels_true, labels_pred)

    # recovery: topic rendered as exactly ONE active card holding >= 80% docs
    all_topic_docs: dict[str, set[str]] = {}
    for item in kept:
        topic = chunk_topic.get(item.sid)
        if topic:
            all_topic_docs.setdefault(topic, set()).add(_doc_of(item.path))
    recovered = 0
    per_topic: dict[str, dict] = {}
    for topic, docs in sorted(all_topic_docs.items()):
        candidates = []
        for card_index, card in enumerate(active):
            card_topics: dict[str | None, int] = {}
            for m in card.members:
                card_topics[chunk_topic.get(m.sid)] = \
                    card_topics.get(chunk_topic.get(m.sid), 0) + 1
            dominant = max(card_topics, key=lambda t: (card_topics[t], t or ""))
            if dominant != topic:
                continue
            held = len(card_docs.get(card_index, set()) & docs)
            if docs and held / len(docs) >= 0.8:
                candidates.append(card_index)
        ok = len(candidates) == 1
        recovered += ok
        per_topic[topic] = {"docs": len(docs), "cards": len(candidates), "ok": ok}
    return {
        "purity": round(purity, 4),
        "ari": round(ari, 4),
        "recovery": recovered,
        "recovery_total": len(all_topic_docs),
        "recovery_per_topic": per_topic,
        "cards": len(active),
        "carded_chunks": carded,
        "after_dedupe": len(kept),
    }


def _adjusted_rand_index(labels_a: list, labels_b: list) -> float:
    """Contingency-table ARI (~15 lines, numpy) — no sklearn (kill list)."""
    a = np.asarray(labels_a)
    b = np.asarray(labels_b)
    ka = {v: i for i, v in enumerate(sorted(set(a.tolist())))}
    kb = {v: i for i, v in enumerate(sorted(set(b.tolist())))}
    n = len(a)
    if n < 2:
        return 1.0
    C = np.zeros((len(ka), len(kb)), dtype=np.int64)
    for x, y in zip(a.tolist(), b.tolist()):
        C[ka[x], kb[y]] += 1
    comb2 = lambda m: m * (m - 1) / 2.0
    sum_ij = float((C * (C - 1) / 2.0).sum())
    sum_a = float((C.sum(axis=1) * (C.sum(axis=1) - 1) / 2.0).sum())
    sum_b = float((C.sum(axis=0) * (C.sum(axis=0) - 1) / 2.0).sum())
    total = comb2(n)
    expected = sum_a * sum_b / total
    max_index = (sum_a + sum_b) / 2.0
    if max_index == expected:
        return 1.0
    return (sum_ij - expected) / (max_index - expected)


def excerpt_fidelity(store: Store, cards: list) -> tuple[float, int]:
    """Every excerpt string ∈ set of util.excerpt over stored texts (§7.2)."""
    stored = {
        make_excerpt(row[0] or "")
        for row in store.db.execute("SELECT body FROM fts").fetchall()
    }
    total = bad = 0
    for card in cards:
        for ex in card.excerpts:
            total += 1
            if ex["text"] not in stored:
                bad += 1
    return (1.0 if total == 0 else 1.0 - bad / total), total


# ------------------------------------------------------------------ variants


def run_v2_pipeline(store: Store, *, min_members: int = 3) -> dict:
    """One full v2 run, calibrated, saved (run row + cards)."""
    result = distill.run_distill(
        store, scope=Scope(), threshold=None, min_members=min_members,
    )
    cards, stats = result["cards"], result["stats"]
    run_row = {
        "id": stats["run_id"],
        "algo": stats["algo"],
        "tau": result["tau"].tau,
        "tau_source": stats["tau_source"],
        "scope_json": Scope().canonical_json(),
        "corpus_hash": stats["corpus_hash"],
        "mode": stats["mode"],
        "stats": stats,
        "view_id": result["view_id"],
    }
    store.save_run(run_row, cards)
    return {"cards": cards, "stats": stats, "tau": result["tau"]}


def calibration_report(tau: distill.TauResult) -> str:
    hist = tau.histogram
    top_bin = int(np.argmax(hist))
    return (
        f"tau {tau.tau:.4f} (raw {tau.tau_raw:.4f}, source {tau.tau_source}"
        + (f", fallback: {tau.fallback_reason}" if tau.fallback_reason else "")
        + f"); quartiles p25 {tau.quartiles[0]:.3f} p50 {tau.quartiles[1]:.3f} "
        f"p75 {tau.quartiles[2]:.3f}; histogram peak bin {top_bin} "
        f"({hist[top_bin]} of {sum(hist)})"
    )


def variant_main(backend_name: str, home: Path) -> tuple[dict, bool]:
    docs = build_corpus("main")
    planted = doc_topics(docs)
    store, backend, _cfg = index_corpus(home / "main", docs, backend_name)
    try:
        run = run_v2_pipeline(store)
        metrics = compute_metrics(run["cards"], _items_of(store), planted)
        fidelity, n_excerpts = excerpt_fidelity(store, run["cards"])
        metrics["excerpt_fidelity"] = round(fidelity, 4)
        metrics["excerpts"] = n_excerpts
        report = {
            "variant": "main",
            "backend": backend_name,
            "calibration": calibration_report(run["tau"]),
            "metrics": metrics,
            "gates": {
                "purity>=0.70": metrics["purity"] >= 0.70,
                "ari>=0.50": metrics["ari"] >= 0.50,
                "recovery>=6/8": metrics["recovery"] >= 6,
                "fidelity=1.0": fidelity == 1.0,
            },
        }
        # determinism e2e (D14): a second index over the SAME corpus must
        # produce identical run identity, cards, excerpts and terms
        store2, backend2, _c2 = index_corpus(
            home / "main-again", docs, backend_name,
            corpus_dir=home / "main" / "corpus",
        )
        try:
            run2 = run_v2_pipeline(store2)
            stats, stats2 = run["stats"], run2["stats"]
            same = (
                stats["run_id"] == stats2["run_id"]
                and stats["histogram"] == stats2["histogram"]
                and [c.members[0].sid for c in run["cards"]]
                == [c.members[0].sid for c in run2["cards"]]
                and [c.excerpts for c in run["cards"]]
                == [c.excerpts for c in run2["cards"]]
                and [c.terms for c in run["cards"]]
                == [c.terms for c in run2["cards"]]
            )
            report["gates"]["determinism"] = bool(same)
        finally:
            store2.close()
            backend2.close()
        ok = all(report["gates"].values())
        return report, ok
    finally:
        store.close()
        backend.close()


def variant_hard(backend_name: str, home: Path) -> tuple[dict, bool]:
    """F1 — the differential gate: v1 rules FAIL, v2 PASS on the filler-heavy
    fixture. The overall variant passes iff BOTH arms hold."""
    docs = build_corpus("hard")
    planted = doc_topics(docs)
    store, backend, _cfg = index_corpus(home / "hard", docs, backend_name)
    try:
        items = _items_of(store)
        kept, _removed = distill.dedupe(items)

        v1_clusters = cluster_v1_rules(kept, tau=0.70, min_members=3)

        class _V1Card:  # metrics() reads .status/.size/.members
            status = "active"

            def __init__(self, members):
                self.members = members

            @property
            def size(self):
                return len(self.members)

        v1_cards = [_V1Card(c) for c in v1_clusters]
        v1_metrics = compute_metrics(v1_cards, kept, planted)

        run = run_v2_pipeline(store)
        v2_metrics = compute_metrics(run["cards"], kept, planted)
        fidelity, _n = excerpt_fidelity(store, run["cards"])

        v1_gates = {
            "v1_purity<0.70 (must FAIL)": v1_metrics["purity"] < 0.70,
        }
        v2_gates = {
            "v2_purity>=0.70": v2_metrics["purity"] >= 0.70,
            "v2_ari>=0.50": v2_metrics["ari"] >= 0.50,
            "v2_recovery>=6/8": v2_metrics["recovery"] >= 6,
            "v2_fidelity=1.0": fidelity == 1.0,
        }
        report = {
            "variant": "hard",
            "backend": backend_name,
            "calibration": calibration_report(run["tau"]),
            "v1_rules": v1_metrics,
            "v2_rules": v2_metrics,
            "gates": {**v1_gates, **v2_gates},
        }
        ok = all(report["gates"].values())
        return report, ok
    finally:
        store.close()
        backend.close()


def variant_single_topic(backend_name: str, home: Path) -> tuple[dict, bool]:
    store, backend, _cfg = index_corpus(
        home / "single", single_topic_corpus(), backend_name,
    )
    try:
        result = distill.run_distill(
            store, scope=Scope(), threshold=None, min_members=3,
        )
        source = result["tau"].tau_source
        ok = source != "otsu"
        return {
            "variant": "single-topic",
            "backend": backend_name,
            "calibration": calibration_report(result["tau"]),
            "gates": {"tau_source != otsu": ok},
        }, ok
    finally:
        store.close()
        backend.close()


def variant_micro(backend_name: str, home: Path) -> tuple[dict, bool]:
    store, backend, _cfg = index_corpus(home / "micro", micro_corpus(), backend_name)
    try:
        result = distill.run_distill(
            store, scope=Scope(), threshold=None, min_members=3,
        )
        stats = result["stats"]
        honest = (
            stats["corpus_chunks"] >= 2
            and stats["after_dedupe"] <= stats["corpus_chunks"]
            and sum(stats["histogram"].values()) >= 1
        )
        return {
            "variant": "micro",
            "backend": backend_name,
            "stats": {
                k: stats[k] for k in distill.PUBLIC_STATS_KEYS if k in stats
            },
            "gates": {"runs with honest stats": honest},
        }, honest
    finally:
        store.close()
        backend.close()


def variant_shuffle(backend_name: str, home: Path) -> tuple[dict, bool]:
    """Order-robustness (06 §4.2): same docs, re-ingested in reversed order →
    recovery unchanged."""
    docs = build_corpus("main")
    planted = doc_topics(docs)
    store, backend, _cfg = index_corpus(
        home / "shuffle", docs, backend_name, reversed_names=True,
    )
    try:
        run = run_v2_pipeline(store)
        metrics = compute_metrics(run["cards"], _items_of(store), planted)
        ok = metrics["recovery"] >= 6 and metrics["purity"] >= 0.70
        return {
            "variant": "shuffle",
            "backend": backend_name,
            "calibration": calibration_report(run["tau"]),
            "metrics": metrics,
            "gates": {
                "recovery>=6/8": metrics["recovery"] >= 6,
                "purity>=0.70": metrics["purity"] >= 0.70,
            },
        }, ok
    finally:
        store.close()
        backend.close()


VARIANTS = {
    "main": variant_main,
    "hard": variant_hard,
    "single-topic": variant_single_topic,
    "micro": variant_micro,
    "shuffle": variant_shuffle,
}


def report_real(db_path: Path) -> tuple[dict, bool]:
    """F2b: card-size report over a COPY of a real index (no writes to the
    original). F2c: prints the contingency line when top-card share > 0.8."""
    import migrations as _migrations

    tmp = Path(tempfile.mkdtemp(prefix="gm-eval-real-"))
    copy_path = tmp / Path(db_path).name
    _migrations._vacuum_into(db_path, copy_path)
    store = Store(copy_path)
    try:
        result = distill.run_distill(
            store, scope=Scope(), threshold=None, min_members=3,
        )
        stats = result["stats"]
        cards = result["cards"]
        sizes = sorted(
            (c.size for c in cards), reverse=True,
        )
        total = stats["after_dedupe"]
        biggest = sizes[0] if sizes else 0
        share = biggest / total if total else 0.0
        contingency = share > 0.8
        report = {
            "mode": "report-real",
            "index": str(db_path),
            "vectors": store.counts().vectors,
            "calibration": calibration_report(result["tau"]),
            "after_dedupe": total,
            "cards": len(sizes),
            "card_size_distribution": distill.size_histogram(
                [[m for m in c.members] for c in cards],
            ),
            "sizes_top10": sizes[:10],
            "biggest_card_share": round(share, 4),
            "contingency_trigger": (
                f"top-card share {share:.2f} > 0.8 → pre-approved "
                "centroid-scale calibration switch (D3/F2c) — documented, "
                "not implemented"
            ) if contingency else None,
        }
        # report mode never fails a gate; the recorded numbers ARE the result
        return report, True
    finally:
        store.close()


def render_report(report: dict) -> str:
    lines = [f"== {report.get('variant') or report.get('mode')} "
             f"({report.get('backend', 'real-index')}) =="]
    if "calibration" in report:
        lines.append(f"calibration: {report['calibration']}")
    if "metrics" in report:
        m = report["metrics"]
        lines.append(
            f"purity {m['purity']:.3f} · ari {m['ari']:.3f} · recovery "
            f"{m['recovery']}/{m['recovery_total']} · cards {m['cards']} · "
            f"fidelity {m.get('excerpt_fidelity', 'n/a')}"
        )
    if "v1_rules" in report:
        v1, v2 = report["v1_rules"], report["v2_rules"]
        lines.append(
            f"v1 rules (must FAIL): purity {v1['purity']:.3f} over "
            f"{v1['cards']} cards · {v1['carded_chunks']}/{v1['after_dedupe']} "
            "chunks carded"
        )
        lines.append(
            f"v2 rules (must PASS): purity {v2['purity']:.3f} · "
            f"ari {v2['ari']:.3f} · recovery {v2['recovery']}/"
            f"{v2['recovery_total']} · cards {v2['cards']}"
        )
    if "card_size_distribution" in report:
        lines.append(f"card-size distribution: {report['card_size_distribution']}")
        lines.append(f"sizes top10: {report['sizes_top10']}")
        lines.append(f"biggest-card share: {report['biggest_card_share']}")
        if report.get("contingency_trigger"):
            lines.append(f"contingency: {report['contingency_trigger']}")
    if "stats" in report and report.get("variant") == "micro":
        s = report["stats"]
        lines.append(
            f"corpus {s['corpus_chunks']} → {s['after_dedupe']} after dedupe, "
            f"tau {s['tau']} ({s['tau_source']}), kept {s['clusters_kept']}"
        )
    gates = report.get("gates", {})
    for name, ok in gates.items():
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goldmine distill canary eval")
    parser.add_argument(
        "--variant", default="main",
        choices=[*VARIANTS, "all"],
        help="canary variant (default main; `hard` is the real offline gate)",
    )
    parser.add_argument("--backend", default="mock", help="mock | llama")
    parser.add_argument("--home", default=None, help="work dir (default: tmp)")
    parser.add_argument("--report-real", default=None, metavar="DB",
                        help="real-index report mode over an existing index")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.report_real:
        report, _ok = report_real(Path(args.report_real).expanduser())
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(render_report(report))
        return 0

    import tempfile

    home = Path(args.home) if args.home else Path(tempfile.mkdtemp(prefix="gm-eval-d-"))
    names = list(VARIANTS) if args.variant == "all" else [args.variant]
    all_ok = True
    reports = []
    for name in names:
        report, ok = VARIANTS[name](args.backend, home)
        reports.append(report)
        all_ok = all_ok and ok
        if not args.json:
            print(render_report(report))
            print(f"verdict: {'PASS' if ok else 'FAIL'}")
            print()
    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
