"""Distill: deterministic extractive topic cards over the index's vectors.

Algorithm (normative — SPEC §4 / ARCHITECTURE §2.3 / D4, no RNG anywhere):
  corpus = all segments with vectors, ordered by (file_id, segment_id)
  1. exact dedupe by sha256 of normalized text
  2. near-dup removal: greedy, drop when cosine >= 0.97 to an already-kept chunk
  3. greedy leader clustering, tau (0.70): each chunk joins the FIRST cluster
     whose centroid (renormalized running sum) has cosine >= tau, else founds a
     new one; ties on equal similarity go to the lowest cluster index
  4. clusters < min_members are dropped (count reported)
  5. cards sorted by member count desc, ties by earliest first member
  6. card = verbatim representative excerpt (member closest to the final
     centroid, ties -> lowest segment id) + TF-IDF top terms + citations.
     NEVER synthesized prose — extractive only.

DEVIATION from MODULE_MAP: `build_cards` returns (cards, stats) — the CLI needs
the dropped-cluster count / corpus size for its header line, and the store
contract (save_cards) already carries the rest on the Card objects.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field

import numpy as np

from .search import STOPWORDS
from .store import Store
from .util import excerpt as make_excerpt
from .util import normalize_text, sha256_bytes

ALGO = "greedy-leader.v1"
NEAR_DUP_COSINE = 0.97
TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class DistillRefused(RuntimeError):
    """Corpus exceeds --max-chunks: refuse (never truncate)."""


@dataclass
class CItem:
    """One vectorized segment in the distill corpus."""

    sid: int                       # segment id
    fid: int                       # file id
    vec: np.ndarray                # (768,) float32, L2-normalized
    text: str                      # FTS body (the chunk text)
    path: str
    kind: str
    loc: float | None
    loc_end: float | None


@dataclass
class Card:
    terms: list[str]

    @property
    def title_terms(self) -> str:
        """Store contract: cards.title_terms is a JSON array string."""
        return json.dumps(self.terms, ensure_ascii=False)

    @property
    def size(self) -> int:
        """Store contract: cards.size is the member chunk count."""
        return self.n_chunks
    members: list[tuple[str, str, float | None, float | None]]  # (path,kind,loc,loc_end)
    rep_text: str
    rep_cite: tuple[str, str, float | None, float | None]
    n_chunks: int
    n_files: int
    # store contract extras (store.save_cards reads these via getattr)
    fingerprint_id: str = ""
    distill_algo: str = ALGO
    created_at: float = 0.0
    chunk_weights: list[tuple[int, float]] = field(default_factory=list)
    topic_id: int = 0


def _tokenize(text: str) -> list[str]:
    return [
        tok for tok in (t.lower() for t in TOKEN_RE.findall(text))
        if len(tok) >= 2 and tok not in STOPWORDS
    ]


def _tokenize_terms(text: str) -> list[str]:
    """Tokens eligible as card terms: must contain a letter, so pure
    numbers/ids ("000", "130") never become a card's key terms."""
    return [tok for tok in _tokenize(text) if any(ch.isalpha() for ch in tok)]


# ------------------------------------------------------------------- dedupe


def dedupe(items: list[CItem]) -> tuple[list[CItem], int]:
    """Exact duplicates by sha256 of normalized text, then greedy near-dups at
    cosine >= 0.97 in (fid, sid) order. Returns (kept, removed_count)."""
    seen_hashes: set[str] = set()
    exact_unique: list[CItem] = []
    removed = 0
    for item in items:
        normalized, _changed = normalize_text(item.text)
        digest = sha256_bytes(normalized.encode("utf-8"))
        if digest in seen_hashes:
            removed += 1
            continue
        seen_hashes.add(digest)
        exact_unique.append(item)

    kept: list[CItem] = []
    kept_mat = np.zeros((0, int(items[0].vec.shape[0]) if items else 768), np.float32)
    tail: list[np.ndarray] = []

    def _max_sim(vec: np.ndarray) -> float:
        parts = []
        if kept_mat.shape[0]:
            parts.append(kept_mat @ vec)
        if tail:
            parts.append(np.stack(tail) @ vec)
        if not parts:
            return -1.0
        return float(np.concatenate(parts).max())

    for item in exact_unique:
        if _max_sim(item.vec) >= NEAR_DUP_COSINE:
            removed += 1
            continue
        kept.append(item)
        tail.append(item.vec)
        if len(tail) >= 256:  # bounded re-stack: O(n) stacks, O(n^2) flops total
            kept_mat = np.vstack([kept_mat, np.stack(tail)])
            tail = []
    return kept, removed


# ----------------------------------------------------------------- clustering


def cluster(
    items: list[CItem], threshold: float = 0.70, min_members: int = 3
) -> tuple[list[list[CItem]], int]:
    """Greedy leader clustering. Deterministic given the (fid, sid) order:
    running-sum centroids renormalized after each assignment, first-index-wins
    on ties. Clusters smaller than min_members are dropped (count returned)."""
    sums: list[np.ndarray] = []
    clusters: list[list[CItem]] = []
    for item in items:
        assigned = False
        if sums:
            cent = np.stack([s / float(np.linalg.norm(s)) for s in sums])
            sims = cent @ item.vec
            hits = np.nonzero(sims >= threshold)[0]
            if hits.size:
                idx = int(hits[0])  # FIRST centroid >= tau (ties -> lowest index)
                sums[idx] += item.vec  # renormalized at the next comparison
                clusters[idx].append(item)
                assigned = True
        if not assigned:
            sums.append(item.vec.astype(np.float32).copy())
            clusters.append([item])

    kept = [c for c in clusters if len(c) >= min_members]
    dropped = len(clusters) - len(kept)
    return kept, dropped


# -------------------------------------------------------------------- tf-idf


def tfidf_terms(
    member_texts: list[str], all_cluster_texts: list[list[str]], top: int = 8
) -> list[str]:
    """Top `top` terms by tf-idf: tf = term count in this cluster's members;
    idf over cluster-level document frequency (SPEC §4). Ties -> alphabetical."""
    n_clusters = max(1, len(all_cluster_texts))
    df: dict[str, int] = {}
    tokenized_clusters = [ _tokenize_terms(t) for t in (
        " ".join(texts) for texts in all_cluster_texts
    ) ]
    for tokens in tokenized_clusters:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    own_tokens = _tokenize_terms(" ".join(member_texts))
    tf: dict[str, int] = {}
    for token in own_tokens:
        tf[token] = tf.get(token, 0) + 1
    scored = sorted(
        (
            (-(count * math.log(n_clusters / df[term])), term)
            for term, count in tf.items()
            if term in df
        ),
    )
    return [term for _neg_score, term in scored[:top]]


# --------------------------------------------------------------- build cards


def _corpus(store: Store, kinds: set[str] | None) -> list[CItem]:
    """All vectorized segments ordered by (file_id, segment_id), with FTS text."""
    table = store.load_vectors()  # ids/file_ids ordered by segment id
    if len(table) == 0:
        return []
    kind_by_file = {
        row[0]: row[1]
        for row in store.db.execute("SELECT id, kind FROM files").fetchall()
    }
    texts = {
        row[0]: row[1] or ""
        for row in store.db.execute("SELECT rowid, body FROM fts").fetchall()
    }
    rows = store.db.execute(
        """SELECT s.id, f.path, f.kind, s.loc, s.loc_end
           FROM segments s JOIN files f ON f.id = s.file
           WHERE s.vec IS NOT NULL"""
    ).fetchall()
    meta = {r[0]: r for r in rows}

    items: list[CItem] = []
    for sid, fid, vec in zip(table.ids, table.file_ids, table.matrix):
        info = meta.get(sid)
        if info is None:
            continue
        _sid, path, kind, loc, loc_end = info
        if kinds is not None and kind not in kinds:
            continue
        items.append(
            CItem(
                sid=sid, fid=fid, vec=np.asarray(vec, dtype=np.float32),
                text=texts.get(sid, ""), path=path, kind=kind,
                loc=loc, loc_end=loc_end,
            )
        )
    items.sort(key=lambda it: (it.fid, it.sid))  # canonical order (normative)
    return items


def build_cards(
    store: Store,
    *,
    threshold: float,
    min_members: int,
    max_chunks: int,
    kinds: set[str] | None,
) -> tuple[list[Card], dict]:
    items = _corpus(store, kinds)
    total = len(items)
    if total > max_chunks:
        raise DistillRefused(
            f"distill corpus has {total:,} vectorized chunks, above the "
            f"--max-chunks cap of {max_chunks:,} — raise the cap to proceed "
            "(refusing, not truncating)"
        )
    if total < 2:
        raise DistillRefused(
            f"distill needs at least 2 vectorized chunks (found {total}) — "
            "ingest content first"
        )

    kept, removed = dedupe(items)
    clusters, dropped_small = cluster(kept, threshold=threshold, min_members=min_members)

    fingerprint_id = store.meta("fingerprint_id") or ""
    algo = f"{ALGO} t={threshold:.2f} min={min_members}"
    all_cluster_texts: list[list[str]] = [
        [m.text for m in members] for members in clusters
    ]
    cards: list[Card] = []
    for members in clusters:
        centroid = np.sum([m.vec for m in members], axis=0)
        norm = float(np.linalg.norm(centroid)) or 1.0
        centroid = centroid / norm
        weights = [float(m.vec @ centroid) for m in members]
        best = min(  # max weight, ties -> lowest segment id (members are sorted)
            range(len(members)), key=lambda i: (-weights[i], members[i].sid)
        )
        rep = members[best]
        terms = tfidf_terms(
            [m.text for m in members],
            all_cluster_texts,
        )
        cards.append((
            (members[0].fid, members[0].sid),  # earliest first member (tie-break)
            Card(
                terms=terms,
                members=[
                    (m.path, m.kind, m.loc, m.loc_end) for m in members
                ],
                rep_text=rep.text,
                rep_cite=(rep.path, rep.kind, rep.loc, rep.loc_end),
                n_chunks=len(members),
                n_files=len({m.fid for m in members}),
                fingerprint_id=fingerprint_id,
                distill_algo=algo,
                created_at=time.time(),
                chunk_weights=[
                    (members[i].sid, round(weights[i], 6)) for i in range(len(members))
                ],
            ),
        ))

    cards.sort(key=lambda pair: (-pair[1].n_chunks, pair[0]))
    ordered = [card for _first, card in cards]
    for i, card in enumerate(ordered, start=1):
        card.topic_id = i
    cards = ordered
    stats = {
        "corpus_chunks": total,
        "after_dedupe": len(kept),
        "duplicates_removed": removed,
        "clusters_kept": len(cards),
        "clusters_dropped_small": dropped_small,
        "algo": algo,
        "threshold": threshold,
        "min_members": min_members,
        "fingerprint_id": fingerprint_id,
    }
    return cards, stats


# ------------------------------------------------------------------ rendering


def _fmt_loc(kind: str, loc, loc_end) -> str:
    if loc is None:
        return ""
    if kind == "pdf":
        if loc_end is not None and loc_end != loc:
            return f"p.{int(loc)}-{int(loc_end)}"
        return f"p.{int(loc)}"
    if kind in ("audio", "video"):
        return f"{loc:g}s-{(loc_end if loc_end is not None else loc):g}s"
    if loc_end is not None and loc_end != loc:
        return f"L{int(loc)}-{int(loc_end)}"
    return f"L{int(loc)}"


def render_markdown(cards: list[Card], header: str, max_citations: int = 10) -> str:
    lines = [f"## goldmine cards — {header}", ""]
    for card in cards:
        title = " ".join(card.terms) or "(no terms)"
        lines.append(
            f"### {card.topic_id}. {title}   [{card.n_chunks} chunks · "
            f"{card.n_files} files]"
        )
        lines.append(f"> {make_excerpt(card.rep_text)}")
        shown = card.members[:max_citations]
        for path, kind, loc, loc_end in shown:
            loc_str = _fmt_loc(kind, loc, loc_end)
            suffix = f", {loc_str}" if loc_str else ""
            lines.append(f"  — {path} ({kind}{suffix})")
        if len(card.members) > max_citations:
            lines.append(
                f"  … +{len(card.members) - max_citations} more chunks"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(cards: list[Card], stats: dict) -> dict:
    return {
        "stats": stats,
        "cards": [
            {
                "id": card.topic_id,
                "terms": card.terms,
                "size": card.n_chunks,
                "n_files": card.n_files,
                "representative": {
                    "text": card.rep_text,
                    "path": card.rep_cite[0],
                    "kind": card.rep_cite[1],
                    "loc": card.rep_cite[2],
                    "loc_end": card.rep_cite[3],
                },
                "citations": [
                    {
                        "path": path, "kind": kind,
                        "loc": loc, "loc_end": loc_end,
                    }
                    for path, kind, loc, loc_end in card.members
                ],
                "fingerprint_id": card.fingerprint_id,
                "distill_algo": card.distill_algo,
            }
            for card in cards
        ],
    }


def header_line(stats: dict) -> str:
    return (
        f"{stats['clusters_kept']} topics from {stats['corpus_chunks']:,} chunks "
        f"(τ={stats['threshold']:.2f}, min={stats['min_members']}, algo {stats['algo']})"
    )
