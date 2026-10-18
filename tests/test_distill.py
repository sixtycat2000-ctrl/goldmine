"""Tier-1/2: distill — dedupe, deterministic clustering, TF-IDF, cards.

The clustering must be byte-stable across runs and input-order-independent
given the canonical (file_id, segment_id) order; card lines must be traceable
to stored excerpts (zero synthesized prose).
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from goldmine.backends.mock import MockBackend
from goldmine.config import Config
from goldmine.distill import (
    Card,
    build_cards,
    cluster,
    dedupe,
    render_json,
    render_markdown,
    tfidf_terms,
)
from goldmine.distill import CItem, DistillRefused
from goldmine.ingest import IngestPipeline
from goldmine.store import Store


def _vec(seed: float) -> np.ndarray:
    vec = np.full(768, 0.1 + seed * 0.01, dtype=np.float32)
    vec[int(seed) % 768] += 5.0
    return vec / np.linalg.norm(vec)


def _item(sid: int, fid: int, vec: np.ndarray, text: str = "text") -> CItem:
    return CItem(
        sid=sid, fid=fid, vec=vec.astype(np.float32), text=text,
        path=f"/p/{fid}.txt", kind="doc", loc=1, loc_end=2,
    )


# ------------------------------------------------------------------ dedupe


def test_exact_dup_removed_by_normalized_text():
    import unicodedata

    decomposed = unicodedata.normalize("NFD", "café crème")
    precomposed = unicodedata.normalize("NFC", "café crème")
    assert decomposed != precomposed  # different bytes, same NFC form
    items = [
        _item(1, 1, _vec(1.0), decomposed),
        _item(2, 1, _vec(2.0), precomposed),
    ]
    kept, removed = dedupe(items)
    assert removed == 1 and len(kept) == 1 and kept[0].sid == 1


def test_near_dup_greedy_097():
    base = _vec(3.0)
    twin = base + 0.01  # cosine ~0.9999
    twin /= np.linalg.norm(twin)
    far = np.zeros(768, np.float32)
    far[5] = 1.0
    items = [
        _item(1, 1, base, "text one"),
        _item(2, 1, twin, "text two"),
        _item(3, 1, far, "text three"),
    ]
    kept, removed = dedupe(items)
    assert removed == 1
    assert [i.sid for i in kept] == [1, 3]


def test_dedupe_independent_of_input_order():
    base = _vec(3.0)
    twin = (base + 0.01) / np.linalg.norm(base + 0.01)
    items_a = [_item(1, 1, base, "t-a"), _item(2, 2, twin, "t-b")]
    items_b = [_item(2, 2, twin, "t-b"), _item(1, 1, base, "t-a")]
    canonical = lambda items: sorted(items, key=lambda i: (i.fid, i.sid))
    kept_a, _ = dedupe(canonical(items_a))
    kept_b, _ = dedupe(canonical(items_b))
    # greedy keeps the FIRST item in CANONICAL order — (fid, sid) decides
    assert [i.sid for i in kept_a] == [1]
    assert [i.sid for i in kept_b] == [1]


# --------------------------------------------------------------- clustering


def test_centroid_is_normalized_running_mean_hand_case():
    a = np.zeros(768, np.float32)
    a[0] = 1.0
    b = np.zeros(768, np.float32)
    b[1] = 1.0
    items = [_item(1, 1, a), _item(2, 1, b)]
    # tau 0.7: a founds; cos(b, a)=0 -> b founds; both singletons, min=1 keeps
    clusters, dropped = cluster(items, threshold=0.70, min_members=1)
    assert len(clusters) == 2 and dropped == 0

    # a third vector between them joins cluster 0 (first >= tau)
    c = (a + b) / np.sqrt(2)
    clusters, _ = cluster([_item(1, 1, a), _item(2, 1, b), _item(3, 1, c)],
                          threshold=0.70, min_members=1)
    assert [i.sid for i in clusters[0]] == [1, 3]
    assert [i.sid for i in clusters[1]] == [2]


def test_cluster_deterministic_across_runs_and_shuffles():
    rng_vectors = [
        (_vec(s), f"text {s} common words everywhere") for s in (1, 2, 3, 10, 11, 20)
    ]
    items = [
        _item(sid, fid, vec, text)
        for (vec, text), (sid, fid) in zip(rng_vectors, [(1, 1), (2, 1), (3, 2), (4, 2), (5, 3), (6, 3)])
    ]
    canonical = sorted(items, key=lambda i: (i.fid, i.sid))
    run1 = [
        [i.sid for i in c] for c in cluster(canonical, 0.70, 1)[0]
    ]
    for _ in range(3):
        shuffled = canonical[::-1]
        rebuilt = sorted(shuffled, key=lambda i: (i.fid, i.sid))  # canonical rebuild
        run2 = [
            [i.sid for i in c] for c in cluster(rebuilt, 0.70, 1)[0]
        ]
        assert run1 == run2


def test_min_members_drops_singletons():
    a, b, far = _vec(1.0), _vec(1.5), np.eye(768, dtype=np.float32)[7]
    items = [
        _item(1, 1, a, "alpha"), _item(2, 1, b, "beta"), _item(3, 2, far, "gamma"),
    ]
    clusters, dropped = cluster(items, threshold=0.70, min_members=2)
    assert dropped == 1
    assert len(clusters) == 1 and [i.sid for i in clusters[0]] == [1, 2]


# ------------------------------------------------------------------ tf-idf


def test_tfidf_hand_case():
    cluster_texts = [
        ["the scheduler evicts pods under pressure", "pod pressure again"],
        ["sourdough starter smells sour", "bread dough rises"],
        ["kubernetes scheduler pressure", "pod eviction"],
    ]
    terms = tfidf_terms(cluster_texts[0], cluster_texts)
    assert terms
    assert "pressure" in terms or "pod" in terms
    # stopwords and short tokens never appear
    assert "the" not in terms and "in" not in terms


# ------------------------------------------------- end-to-end on an index


@pytest.fixture
def indexed(tmp_path):
    root = tmp_path / "corpus"
    (root / "k8s").mkdir(parents=True)
    (root / "food").mkdir()
    for i in range(4):
        (root / "k8s" / f"k{i}.txt").write_text(
            f"The scheduler evicts a pod under node pressure {i}. "
            "Tolerations keep the pod running. " * 3,
            encoding="utf-8",
        )
    for i in range(4):
        (root / "food" / f"f{i}.txt").write_text(
            f"Feed the sourdough starter daily {i}. Bulk ferment the dough. " * 3,
            encoding="utf-8",
        )
    (root / "loner.txt").write_text(
        "Completely unrelated quantum flux capacitor notes.", encoding="utf-8"
    )
    cfg = Config()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "index.sqlite")
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    report = pipeline.run([str(root)])
    assert report.content_indexed == 9
    return store


def test_build_cards_on_index(indexed):
    cards, stats = build_cards(
        indexed, threshold=0.70, min_members=3, max_chunks=50_000, kinds=None
    )
    assert stats["corpus_chunks"] >= 9
    assert stats["clusters_kept"] >= 2
    sizes = [c.n_chunks for c in cards]
    assert sizes == sorted(sizes, reverse=True)  # sorted by member count desc
    # topic cards are the k8s trio+ and the food trio+
    joined_terms = " ".join(t for c in cards for t in c.terms)
    assert "pod" in joined_terms or "pressure" in joined_terms
    assert "sourdough" in joined_terms or "starter" in joined_terms
    # verbatim representative: text traceable to a stored excerpt
    excerpts = {
        row[0] for row in indexed.db.execute(
            "SELECT excerpt FROM segments WHERE excerpt IS NOT NULL"
        ).fetchall()
    }
    for card in cards:
        from goldmine.util import excerpt as make_excerpt

        assert make_excerpt(card.rep_text) in excerpts
        assert card.fingerprint_id == indexed.meta("fingerprint_id")
        assert "greedy-leader.v1" in card.distill_algo


def test_cards_roundtrip_through_store(indexed):
    cards, stats = build_cards(
        indexed, threshold=0.70, min_members=3, max_chunks=50_000, kinds=None
    )
    indexed.save_cards(cards)
    rows = indexed.cards()
    assert len(rows) == len(cards)
    assert all(row.fingerprint_id == indexed.meta("fingerprint_id") for row in rows)
    weights = indexed.db.execute(
        "SELECT COUNT(*) FROM card_chunks"
    ).fetchone()[0]
    assert weights == sum(c.n_chunks for c in cards)


def test_max_chunks_refuses(indexed):
    with pytest.raises(DistillRefused) as excinfo:
        build_cards(
            indexed, threshold=0.70, min_members=3, max_chunks=2, kinds=None
        )
    assert "max-chunks" in str(excinfo.value)


def test_below_two_chunks_refuses(tmp_path):
    cfg = Config()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "index.sqlite")
    with pytest.raises(DistillRefused):
        build_cards(store, threshold=0.7, min_members=3, max_chunks=10, kinds=None)
    store.close()


def test_render_markdown_and_json(indexed):
    cards, stats = build_cards(
        indexed, threshold=0.70, min_members=3, max_chunks=50_000, kinds=None
    )
    md = render_markdown(cards, f"{stats['clusters_kept']} topics from "
                                f"{stats['corpus_chunks']} chunks "
                                f"(τ=0.70, min=3, algo {stats['algo']})")
    assert md.startswith("## goldmine cards —")
    assert "### 1." in md
    assert all(line.startswith("> ") for line in md.splitlines() if line.startswith(">"))
    payload = json.loads(json.dumps(render_json(cards, stats)))
    first = payload["cards"][0]
    assert set(first) >= {
        "id", "terms", "size", "n_files", "representative", "citations",
        "fingerprint_id", "distill_algo",
    }
    assert first["citations"], "citations list for ALL members"
    assert first["citations"][0]["path"]
