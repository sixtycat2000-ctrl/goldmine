"""Distill v2: calibrated-τ topic cards over the index's vectors (R6).

Design of record: docs/v2.1-r6-distill.md (D1–D15 + the 2026-10-19 review
amendments F1–F11). Algorithm, normative:

  corpus = all vectorized segments in scope, ordered by (file_id, segment_id)
  1. exact dedupe by sha256 of normalized text
  2. near-dup removal: greedy, drop when cosine >= 0.97 to an already-kept chunk
  3. τ calibration (measure_tau): sampled pairwise cosines (all pairs when
     N <= 300, else a 300-row stride gram — deterministic, no seed), 256-bin
     histogram, Otsu t* = bin-center of argmax between-class variance, the
     hump/valley + mode-proximity fallback to the P90 percentile path, clamp
     [0.55, 0.85]. tau_source ∈ otsu|floor|ceil|percentile|manual|inherited.
     REAL corpora are unimodal and EXPECTED to land on 'percentile' (F2a).
  4. leader clustering with the average-link admission guard (cluster_v2):
     a chunk joins the FIRST cluster (lowest index) where cos-to-centroid >= τ
     AND mean-cos-to-members = (S_c·x̂)/n_c >= τ_lo = max(τ−0.10, 0.35) (D4);
     else it founds a new cluster. The guard reads the running member sum —
     never a centroid shortcut (F7: the two disagree near the boundary).
  5. clusters < min_members are dropped — never silently: the full size
     histogram (kept + dropped) is reported in stats and --preview.
  6. cards sorted by (size desc, first member); card = medoid primary excerpt
     + up to 2 coverage excerpts (greedy max-min-cosine-distance, stop < 0.25)
     + filtered tf-idf terms (df >= 2 distinct members, bigram competition)
     + extractive title (first sentence of the medoid member).

Constants are module-level names (testable, documented) — deliberately NOT
config surface (kill-list spirit, 06 §4.9).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field

import numpy as np

from .search import STOPWORDS
from .store import Store
from .util import excerpt as make_excerpt
from .util import normalize_text, pack_fp16, sha256_bytes

ALGO_FAMILY = "leader-avglink"          # D7: view identity family
ALGO = "leader-avglink.v2"              # v2 algo string (run rows + cards)
NEAR_DUP_COSINE = 0.97
TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# -- τ calibration (D1/D2, §2.2) ---------------------------------------------
CALIB_SAMPLE_ROWS = 300        # stride gram rows when N > 300 (<= 44,850 pairs)
CALIB_BINS = 256               # histogram bins over [0, 1]
TAU_FLOOR = 0.55
TAU_CEIL = 0.85
MODE_PROXIMITY_K = 1.2         # fallback when |t* - mode| <= k·σ (B9; k
                               # calibrated on the hard canary: t* lands
                               # 1.04σ from the mode there, 1.0 misses)
HUMP_REL_HEIGHT = 0.20         # hump = local max >= 0.20 · dominant peak
VALLEY_REL_DEPTH = 0.60        # real valley: min between humps <= 0.60 · smaller peak

# -- admission guard (D4) ------------------------------------------------------
GUARD_MARGIN = 0.10            # τ_lo = max(τ - 0.10, 0.35)
GUARD_FLOOR = 0.35

# -- two-level topics (D5, §2.4) ----------------------------------------------
SPLIT_MIN_N = 12               # consider splitting clusters with n >= 12
SPLIT_MARGIN = 0.12            # split when s̄ < τ - 0.12
SPLIT_TAU_SUB = 0.08           # recursive leader at τ_sub = τ + 0.08
SPLIT_ACCEPT_GAIN = 0.05       # accept when size-weighted children s̄ improves >= 0.05
SPLIT_MAX_DEPTH = 2

# -- excerpts (D11) -------------------------------------------------------------
COVERAGE_STOP_DISTANCE = 0.25  # stop when the next pick is < 0.25 from all picked
EXCERPTS_PER_CARD = 3          # primary + up to 2 coverage
TITLE_MAX_CHARS = 70

# -- incremental (D8/D9, F4/F5/F8) ------------------------------------------------
DELETE_RATIO_LIMIT = 0.20      # > 20% member-file deletions force a full run

_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{6,}$")
_UNIT_RE = re.compile(r"^\d+(\.\d+)?(kb|mb|gb|tb|px|hz)$")

HISTOGRAM_BUCKETS = ("1", "2", "3-5", "6-10", "11-25", "26-100", "100+")

#: The --json stats key set (§6.3 "EXACTLY", plus F5's
#: chunks_reassigned_into_existing_cards). The internal stats dict carries
#: more (view_id, member_files, embed_id, scope) for run rows and preview —
#: render_json projects to exactly this list so the published schema is
#: additive-only from here.
PUBLIC_STATS_KEYS = (
    "corpus_chunks", "after_dedupe", "duplicates_removed", "clusters_kept",
    "clusters_dropped_small", "histogram", "tau", "tau_raw", "tau_source",
    "tau_inherited", "algo", "mode", "run_id", "corpus_hash",
    "reclustered_chunks", "chunks_reassigned_into_existing_cards",
    "dissolved_cards", "new_cards", "budget", "fingerprint_id",
)


class DistillRefused(RuntimeError):
    """Corpus exceeds --max-chunks: refuse (never truncate)."""


@dataclass
class CItem:
    """One vectorized segment in the distill corpus."""

    sid: int                       # segment id
    fid: int                       # file id
    vec: np.ndarray                # (768,) float32, L2-normalized (decoded fp16)
    text: str                      # FTS body (the chunk text)
    path: str
    kind: str
    loc: float | None
    loc_end: float | None


@dataclass
class Scope:
    """Distill scope (D13/§6.2): kinds, path roots, mtime floor."""

    kinds: set[str] | None = None
    under: list[str] | None = None
    after: float | None = None     # epoch seconds (files.mtime >= after)

    def canonical_json(self) -> str:
        """Canonical form: sorted keys, sorted lists — the view/run identity
        input (D7/D9). Determinism contract: no dict-order leakage."""
        return json.dumps(
            {
                "under": sorted(self.under) if self.under else None,
                "kinds": sorted(self.kinds) if self.kinds else None,
                "after": self.after,
            },
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )


@dataclass
class TauResult:
    """τ calibration outcome (§2.2). Pure function of the deduped matrix."""

    tau: float
    tau_source: str                # otsu|floor|ceil|percentile|manual|inherited
    tau_raw: float
    histogram: list[int]           # 256 bins, clamped indexing (F6)
    quartiles: tuple[float, float, float]
    fallback_reason: str | None = None   # why the percentile path fired (preview)


@dataclass
class Card:
    """A v2 card (or header row). Store contract: save_run serializes these."""

    title: str
    title_kind: str                # terms|sentence
    terms: list[str]
    excerpts: list[dict]           # [{text,path,kind,loc,loc_end,role}]
    members: list[CItem]
    n_files: int
    parent: "Card | None" = None
    status: str = "active"         # active|header|dissolved
    centroid: np.ndarray | None = None   # (768,) float32, renormalized mean
    fingerprint_id: str = ""
    distill_algo: str = ALGO
    created_at: float = 0.0
    chunk_weights: list[tuple[int, float]] = field(default_factory=list)
    topic_id: int = 0
    run_id: str = ""
    view_id: str = ""

    @property
    def terms_json(self) -> str:
        """Store contract: cards.terms is a JSON array string."""
        return json.dumps(self.terms, ensure_ascii=False)

    @property
    def excerpts_json(self) -> str:
        return json.dumps(self.excerpts, ensure_ascii=False)

    @property
    def size(self) -> int:
        """Store contract: cards.size is the member chunk count."""
        return len(self.members)

    @property
    def first_member_key(self) -> tuple[int, int]:
        return (self.members[0].fid, self.members[0].sid)

    @property
    def member_citations(self) -> list[tuple[str, str, float | None, float | None]]:
        return [(m.path, m.kind, m.loc, m.loc_end) for m in self.members]


def canonical_json(obj) -> str:
    """Sorted keys, no whitespace — every hash input goes through here."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def view_id_for(scope: Scope) -> str:
    """D7: view identity = sha256(algo family + canonical scope)[:16]. τ is
    deliberately NOT part of the view (a τ change is a full rebuild of the
    same view — D8)."""
    return sha256_bytes(f"{ALGO}{scope.canonical_json()}".encode("utf-8"))[:16]


def run_id_for(algo: str, tau: float, scope: Scope, corpus_hash: str, mode: str) -> str:
    """Run identity (§4.1): sha256 over the content tuple. Repeat identical
    runs collide by design — store.save_run resolves the collision with
    ON CONFLICT(id) DO UPDATE (F3)."""
    payload = f"{algo}|{repr(float(tau))}|{scope.canonical_json()}|{corpus_hash}|{mode}"
    return sha256_bytes(payload.encode("utf-8"))


def corpus_hash_for(store: Store, scope: Scope) -> str:
    """D9 (audit-only, F4): sha256 over {embed_id, files: [[path, sha256], …]}
    of in-scope vector-bearing files. embed_id INSIDE the hash so a re-embed
    under unchanged paths never passes as the same corpus — but eligibility
    compares embed_id directly (F4); this hash is run identity/diffing only."""
    embed_id = store.meta("embed_id") or ""
    files = [[path, sha] for path, sha in _scoped_files(store, scope)]
    payload = canonical_json({"embed_id": embed_id, "files": sorted(files)})
    return sha256_bytes(payload.encode("utf-8"))


def _scoped_files(store: Store, scope: Scope) -> list[tuple[str, str | None]]:
    """In-scope vector-bearing files as (path, sha256), sorted — the corpus_hash
    file list (D9). Same scoping rules as _corpus (§6.2)."""
    from .store import _split_roots, _under_roots

    rows = store.db.execute(
        """SELECT DISTINCT f.path, f.sha256, f.kind, f.source FROM files f
           JOIN segments s ON s.file = f.id WHERE s.vec IS NOT NULL"""
    ).fetchall()
    file_roots, url_roots = _split_roots(scope.under or [])
    out: list[tuple[str, str | None]] = []
    for path, sha, kind, source in rows:
        if scope.kinds is not None and kind not in scope.kinds:
            continue
        if scope.under and not _under_roots(path, source, file_roots, url_roots):
            continue
        out.append((path, sha))
    return sorted(out)


# ------------------------------------------------------------------- tokenize


def _is_term_token(tok: str) -> bool:
    """D10 term filter: len >= 3, contains a letter, letters >= digits, not
    hex-shaped, not unit-shaped. Stopwords are filtered at tokenization."""
    if len(tok) < 3 or not any(ch.isalpha() for ch in tok):
        return False
    letters = sum(1 for ch in tok if ch.isalpha())
    digits = sum(1 for ch in tok if ch.isdigit())
    if letters < digits:
        return False
    if _HEX_RE.match(tok) or _UNIT_RE.match(tok):
        return False
    return True


def _tokenize_terms(text: str) -> list[str]:
    """Tokens eligible as card terms (D10)."""
    return [
        tok for tok in (t.lower() for t in TOKEN_RE.findall(text))
        if tok not in STOPWORDS and _is_term_token(tok)
    ]


def _tokenize_bigrams(text: str) -> list[str]:
    """Adjacent eligible-token pairs within one member's text (D10)."""
    toks = _tokenize_terms(text)
    return [f"{a} {b}" for a, b in zip(toks, toks[1:])]


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


# ----------------------------------------------------------------- calibration


def _sample_pair_cosines(matrix: np.ndarray) -> np.ndarray:
    """Deterministic pairwise sample (D1): all N(N−1)/2 pairs when N <= 300,
    else rows i_k = round(k·N/300) (duplicates collapsed, order preserved —
    stride kills RNG), gram over the sampled rows, upper triangle."""
    n = matrix.shape[0]
    if n <= CALIB_SAMPLE_ROWS:
        gram = matrix @ matrix.T
        iu = np.triu_indices(n, k=1)
        return gram[iu]
    rows: list[int] = []
    seen: set[int] = set()
    for k in range(CALIB_SAMPLE_ROWS):
        i = round(k * n / CALIB_SAMPLE_ROWS)
        if i not in seen:
            seen.add(i)
            rows.append(i)
    ms = matrix[rows]
    gram = ms @ ms.T
    iu = np.triu_indices(len(rows), k=1)
    return gram[iu]


def _histogram_of(vals: np.ndarray) -> np.ndarray:
    """256 bins over [0, 1]; F6: clamp negatives to bin 0, values >= 1 to the
    last bin — never wrap."""
    hist = np.zeros(CALIB_BINS, dtype=np.int64)
    bins = np.clip((vals * CALIB_BINS).astype(np.int64), 0, CALIB_BINS - 1)
    for b in bins:
        hist[b] += 1
    return hist


def _otsu_cut(hist: np.ndarray) -> int:
    """t* = argmax over cuts t ∈ 1..255 of ω0·ω1·(μ0−μ1)² (bin-index moments).
    Returns 0 when the histogram is degenerate (all mass in one bin)."""
    total = int(hist.sum())
    if total == 0:
        return 0
    if int(hist.max()) == total:
        return 0  # single-bin degenerate → caller falls back (§2.2.4)
    bin_idx = np.arange(CALIB_BINS, dtype=np.float64)
    sum_all = float((hist * bin_idx).sum())
    omega0 = 0.0
    mu0 = 0.0
    best_t, best_var = 1, -1.0
    for t in range(1, CALIB_BINS):
        count = int(hist[t - 1])
        if count:
            omega0 += count
            mu0 += (t - 1) * count
        omega1 = total - omega0
        if omega0 == 0 or omega1 == 0:
            continue
        mu0m = mu0 / omega0
        mu1m = (sum_all - mu0) / omega1
        var = omega0 * omega1 * (mu0m - mu1m) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def _smoothed(hist: np.ndarray) -> np.ndarray:
    kernel = np.ones(5, dtype=np.float64) / 5.0
    return np.convolve(hist.astype(np.float64), kernel, mode="same")


def _local_maxima(s: np.ndarray) -> list[int]:
    """Local maxima of the smoothed histogram, EDGES INCLUDED (all mass at
    cosine ~0 or ~1 is a real hump — synthetic corpora live there)."""
    out = []
    if s[0] > s[1]:
        out.append(0)
    for b in range(1, CALIB_BINS - 1):
        if s[b] > s[b - 1] and s[b] >= s[b + 1]:
            out.append(b)
    if s[CALIB_BINS - 1] > s[CALIB_BINS - 2]:
        out.append(CALIB_BINS - 1)
    return out


def measure_tau(matrix: np.ndarray, *, manual: float | None = None) -> TauResult:
    """τ calibration (§2.2). Pure function of the deduped decoded-fp16 matrix
    — no Store, no backend. `manual` (--threshold/config) overrides the cut
    (tau_source='manual') while steps 2–4 still populate histogram/quartiles."""
    vals = _sample_pair_cosines(matrix)
    hist = _histogram_of(vals)
    quartiles = tuple(float(q) for q in np.percentile(vals, [25, 50, 75]))
    fallback_reason: str | None = None

    if manual is not None:
        return TauResult(
            tau=float(manual), tau_source="manual", tau_raw=float(manual),
            histogram=[int(x) for x in hist], quartiles=quartiles,  # type: ignore[arg-type]
        )

    sigma = float(np.std(vals)) if vals.size else 0.0
    t_star = _otsu_cut(hist)
    tau_raw = (t_star + 0.5) / CALIB_BINS

    on_otsu_path = t_star > 0
    if not on_otsu_path:
        fallback_reason = "degenerate single-bin histogram (single hump)"

    if on_otsu_path:
        s = _smoothed(hist)
        dominant = int(np.argmax(s))
        humps = [b for b in _local_maxima(s) if s[b] >= HUMP_REL_HEIGHT * s[dominant]]
        if len(humps) < 2:
            on_otsu_path = False
            fallback_reason = "fewer than two histogram humps (unimodal)"
        else:
            top_two = sorted(humps, key=lambda b: (-s[b], b))[:2]
            a, b = min(top_two), max(top_two)
            valley_min = float(s[a:b + 1].min())
            smaller_peak = float(min(s[a], s[b]))
            if valley_min > VALLEY_REL_DEPTH * smaller_peak:
                on_otsu_path = False
                fallback_reason = "no real valley between the top two humps"
            elif abs(tau_raw - (dominant + 0.5) / CALIB_BINS) <= MODE_PROXIMITY_K * sigma:
                on_otsu_path = False
                fallback_reason = "mode proximity (t* within k·σ of the mode)"

    if on_otsu_path:
        tau = min(max(tau_raw, TAU_FLOOR), TAU_CEIL)
        source = "otsu"
        if tau_raw < TAU_FLOOR:
            source = "floor"
        elif tau_raw > TAU_CEIL:
            source = "ceil"
        return TauResult(
            tau=tau, tau_source=source, tau_raw=tau_raw,
            histogram=[int(x) for x in hist], quartiles=quartiles,  # type: ignore[arg-type]
        )

    # percentile fallback (B9/F2a): real corpora are unimodal — expected path
    tau_raw = float(np.percentile(vals, 90)) if vals.size else TAU_FLOOR
    tau = min(max(tau_raw, TAU_FLOOR), TAU_CEIL)
    return TauResult(
        tau=tau, tau_source="percentile", tau_raw=tau_raw,
        histogram=[int(x) for x in hist], quartiles=quartiles,  # type: ignore[arg-type]
        fallback_reason=fallback_reason,
    )


# ----------------------------------------------------------------- clustering


def _bucket_for(n: int) -> str:
    if n <= 2:
        return str(n)
    if n <= 5:
        return "3-5"
    if n <= 10:
        return "6-10"
    if n <= 25:
        return "11-25"
    if n <= 100:
        return "26-100"
    return "100+"


def size_histogram(clusters: list[list[CItem]]) -> dict[str, int]:
    """Full size histogram over ALL clusters — kept and dropped merged (§2.3,
    06 §4.2 "no longer silently")."""
    out = dict.fromkeys(HISTOGRAM_BUCKETS, 0)
    for c in clusters:
        out[_bucket_for(len(c))] += 1
    return out


def cluster_v2(
    items: list[CItem], tau: float, min_members: int = 3
) -> tuple[list[list[CItem]], dict[str, int]]:
    """Leader clustering with the average-link admission guard (D4).

    Deterministic given the (fid, sid) order: running member sums S_c (NEVER
    a centroid shortcut for the guard — F7), renormalized centroids compared
    for the primary gate, first-index-wins among clusters passing BOTH gates,
    new cluster on failure. Clusters below min_members are dropped; the
    returned histogram covers kept + dropped buckets (§2.3)."""
    tau_lo = max(tau - GUARD_MARGIN, GUARD_FLOOR)
    sums: list[np.ndarray] = []
    counts: list[int] = []
    clusters: list[list[CItem]] = []
    for item in items:
        assigned = False
        if sums:
            sum_mat = np.stack(sums)
            norms = np.linalg.norm(sum_mat, axis=1)
            norms[norms == 0.0] = 1.0
            cents = sum_mat / norms[:, None]
            sims = cents @ item.vec
            means = (sum_mat @ item.vec) / np.asarray(counts, dtype=np.float32)
            hits = np.nonzero(sims >= tau)[0]
            for idx in hits:  # ascending index — first qualifying card (F8c)
                i = int(idx)
                if means[i] >= tau_lo:
                    sums[i] += item.vec
                    counts[i] += 1
                    clusters[i].append(item)
                    assigned = True
                    break
        if not assigned:
            sums.append(item.vec.astype(np.float32).copy())
            counts.append(1)
            clusters.append([item])

    kept = [c for c in clusters if len(c) >= min_members]
    return kept, size_histogram(clusters)


# ------------------------------------------------------- excerpts / content


def _pick_excerpts(members: list[CItem]) -> list[int]:
    """Medoid (primary) + up to 2 coverage picks (D11). Medoid = argmax mean
    cosine to the other members over the within-cluster gram (n=1 → that
    member); coverage = greedy max min-cosine-DISTANCE, stop at < 0.25.
    Ties → lowest segment id (members are in canonical order)."""
    n = len(members)
    if n == 1:
        return [0]
    mat = np.stack([m.vec for m in members])
    gram = mat @ mat.T
    row_sums = gram.sum(axis=1)
    means = (row_sums - np.diag(gram)) / (n - 1)
    best = min(range(n), key=lambda i: (-float(means[i]), members[i].sid))
    picked = [best]
    while len(picked) < EXCERPTS_PER_CARD:
        max_cos = gram[:, picked].max(axis=1)
        dist = 1.0 - max_cos
        for i in picked:
            dist[i] = -1.0
        cand = min(range(n), key=lambda i: (-float(dist[i]), members[i].sid))
        if dist[cand] < COVERAGE_STOP_DISTANCE:
            break
        picked.append(cand)
    return picked


def _trim_words(text: str, limit: int = TITLE_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    out = ""
    for word in text.split():
        if out and len(out) + 1 + len(word) > limit:
            break
        out = f"{out} {word}".strip()
    return out or text[:limit]


def _title_for(medoid_text: str, terms: list[str]) -> tuple[str, str]:
    """Extractive title (§2.5): first sentence of the medoid member's full
    text, word-boundary-trimmed at 70 chars; fallback to the v1 terms-join
    when no sentence boundary exists. Returns (title, title_kind)."""
    parts = _SENT_SPLIT.split(medoid_text.strip())
    first = parts[0].strip() if parts else ""
    if len(parts) > 1 and first:
        return _trim_words(first), "sentence"
    joined = " ".join(terms)
    if joined:
        return _trim_words(joined), "terms"
    return _trim_words(make_excerpt(medoid_text, TITLE_MAX_CHARS)), "terms"


def cluster_terms(
    own_members: list[CItem],
    all_groups: list[list[CItem]],
    top: int = 8,
) -> list[str]:
    """Card terms (D10): filtered tokens, member-df >= 2 gate, tf-idf scored
    (tf × log(n_groups / cluster-df), ties → alphabetical), eligible bigrams
    compete on the same score and replace both halves when they outscore
    both. `all_groups` = kept cards + headers (n_groups never 0 in v2 — the
    mega-cluster degenerate case is gone; max(1, ·) as belt-and-braces)."""
    n_groups = max(1, len(all_groups))

    # per-member token sets → member-df (distinct CHUNKS, D10/§2.5)
    member_tokens: list[set[str]] = []
    member_bigrams: list[set[str]] = []
    for member in (m for group in all_groups for m in group):
        member_tokens.append(set(_tokenize_terms(member.text)))
        member_bigrams.append(set(_tokenize_bigrams(member.text)))
    df_members: dict[str, int] = {}
    for toks in member_tokens:
        for term in toks:
            df_members[term] = df_members.get(term, 0) + 1
    df_bigram_members: dict[str, int] = {}
    for toks in member_bigrams:
        for term in toks:
            df_bigram_members[term] = df_bigram_members.get(term, 0) + 1

    # cluster-level df for the idf (v1 formula kept: few clusters → low idf)
    df_cluster: dict[str, int] = {}
    df_bigram_cluster: dict[str, int] = {}
    for toks, bigs in zip(member_tokens, member_bigrams):
        for term in toks:
            df_cluster[term] = df_cluster.get(term, 0) + 1
        for term in bigs:
            df_bigram_cluster[term] = df_bigram_cluster.get(term, 0) + 1

    own_tokens: dict[str, int] = {}
    own_bigrams: dict[str, int] = {}
    for member in own_members:
        for term in _tokenize_terms(member.text):
            own_tokens[term] = own_tokens.get(term, 0) + 1
        for term in _tokenize_bigrams(member.text):
            own_bigrams[term] = own_bigrams.get(term, 0) + 1

    def score(count: int, dfc: int) -> float:
        return count * math.log(n_groups / dfc) if dfc else 0.0

    uni = sorted(
        (
            (-(score(cnt, df_cluster[t])), t)
            for t, cnt in own_tokens.items()
            if df_members.get(t, 0) >= 2 and t in df_cluster
        ),
    )
    bi = sorted(
        (
            (-(score(cnt, df_bigram_cluster[t])), t)
            for t, cnt in own_bigrams.items()
            if df_bigram_members.get(t, 0) >= 2 and t in df_bigram_cluster
        ),
    )

    scores: dict[str, float] = {t: -neg for neg, t in uni}
    scores.update({t: -neg for neg, t in bi})

    # top-8 unigrams first; then a bigram that OUTSCORES BOTH halves replaces
    # them in the final list (D10). Deterministic: unigram rank order, then
    # bigram descending score / alphabetical.
    picked: list[str] = [t for _neg, t in uni][:top]
    for bigram in (t for _neg, t in bi):
        halves = list(dict.fromkeys(bigram.split(" ")))
        if not all(h in picked for h in halves):
            continue
        if all(scores.get(bigram, 0.0) > scores.get(h, 0.0) for h in halves):
            for h in halves:
                picked.remove(h)
            picked.append(bigram)
    return picked[:top]


# -------------------------------------------------------------------- corpus


def _corpus(store: Store, scope: Scope) -> list[CItem]:
    """All vectorized in-scope segments, canonical (file_id, segment_id) order,
    with FTS text (§2.1/§6.2). URL sources never enter distill scope — cards
    cluster file content."""
    from .store import _split_roots, _under_roots

    table = store.load_vectors()  # ids/file_ids ordered by segment id
    if len(table) == 0:
        return []
    file_info = {
        row[0]: (row[1], row[2], row[3])
        for row in store.db.execute(
            "SELECT id, path, kind, mtime FROM files"
        ).fetchall()
    }
    texts = {
        row[0]: row[1] or ""
        for row in store.db.execute("SELECT rowid, body FROM fts").fetchall()
    }
    seg_meta = {
        r[0]: (r[1], r[2], r[3], r[4])
        for r in store.db.execute(
            "SELECT s.id, f.path, f.kind, s.loc, s.loc_end "
            "FROM segments s JOIN files f ON f.id = s.file "
            "WHERE s.vec IS NOT NULL"
        ).fetchall()
    }
    file_roots, url_roots = _split_roots(scope.under or [])

    items: list[CItem] = []
    for sid, fid, vec in zip(table.ids, table.file_ids, table.matrix):
        info = seg_meta.get(sid)
        if info is None:
            continue
        path, kind, loc, loc_end = info
        finfo = file_info.get(fid)
        if finfo is None:
            continue
        fpath, _kind2, mtime = finfo
        if scope.kinds is not None and kind not in scope.kinds:
            continue
        if scope.under and not _under_roots(fpath, "url" if fpath.startswith(
                ("http://", "https://")) else "file", file_roots, url_roots):
            continue
        if scope.after is not None and (mtime or 0.0) < scope.after:
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


def _check_corpus_bound(items: list[CItem], max_chunks: int) -> None:
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


# --------------------------------------------------------------- card build


def _build_card(
    members: list[CItem],
    all_groups: list[list[CItem]],
    *,
    fingerprint_id: str,
    algo: str,
    status: str = "active",
) -> Card:
    """One card's content (§2.5/§2.6): medoid + coverage excerpts, filtered
    terms, extractive title, member weights = cos-to-final-centroid."""
    mat = np.stack([m.vec for m in members])
    centroid = mat.sum(axis=0)
    norm = float(np.linalg.norm(centroid)) or 1.0
    centroid = centroid / norm
    weights = [float(v @ centroid) for v in mat]
    picks = _pick_excerpts(members)
    excerpts: list[dict] = []
    for rank, idx in enumerate(picks):
        m = members[idx]
        excerpts.append({
            "text": make_excerpt(m.text),
            "path": m.path,
            "kind": m.kind,
            "loc": m.loc,
            "loc_end": m.loc_end,
            "role": "primary" if rank == 0 else "coverage",
        })
    terms = cluster_terms(members, all_groups)
    title, title_kind = _title_for(members[picks[0]].text, terms)
    return Card(
        title=title,
        title_kind=title_kind,
        terms=terms,
        excerpts=excerpts,
        members=list(members),
        n_files=len({m.fid for m in members}),
        centroid=centroid,
        status=status,
        fingerprint_id=fingerprint_id,
        distill_algo=algo,
        created_at=time.time(),
        chunk_weights=[
            (members[i].sid, round(weights[i], 6)) for i in range(len(members))
        ],
    )


def build_cards_v2(
    kept: list[CItem],
    *,
    tau: float,
    min_members: int,
    fingerprint_id: str,
) -> tuple[list[Card], list[list[CItem]], dict[str, int]]:
    """Clusters → card objects (§2). Returns (active_cards, kept_clusters,
    histogram). M3 adds split_two_level between the leader pass and this."""
    clusters, hist = cluster_v2(kept, tau, min_members)
    algo = f"{ALGO} t={tau:.2f} min={min_members}"
    cards = [
        _build_card(
            members, clusters,
            fingerprint_id=fingerprint_id, algo=algo,
        )
        for members in clusters
    ]
    cards.sort(key=lambda c: (-c.size, c.first_member_key))
    return cards, clusters, hist


# --------------------------------------------------------------- run helpers


def header_line(stats: dict) -> str:
    """Run header (§6.3): algo, τ + source, scope summary, corpus hash [:12]."""
    scope_bits = []
    if stats.get("scope_under"):
        scope_bits.append(f"under={','.join(stats['scope_under'])}")
    if stats.get("scope_kinds"):
        scope_bits.append(f"kinds={','.join(stats['scope_kinds'])}")
    if stats.get("scope_after"):
        scope_bits.append(f"after={stats['scope_after']}")
    scope_str = f" · scope: {' '.join(scope_bits)}" if scope_bits else ""
    return (
        f"{stats['algo']} · τ={stats['tau']:.4f} ({stats['tau_source']}) "
        f"· {stats['clusters_kept']} cards from "
        f"{stats['corpus_chunks']:,} chunks · corpus {stats['corpus_hash'][:12]}"
        f"{scope_str}"
    )


SCALE_NOTE = (
    "τ calibrated on pairwise cosines; assignments compare cos-to-centroid, "
    "which reads ~1.1–1.6× higher"
)


def render_preview(
    stats: dict,
    tau_result: TauResult,
    dropped_preview: list[tuple[int, list[str]]],
    intent: str,
) -> str:
    """--preview report (§6.1): corpus size, τ (+raw/source/reason), pairwise
    quartiles, size histogram, top terms of the 10 largest dropped clusters,
    the B10 scale note, and the full-vs-incremental intent. Writes NOTHING."""
    lines = [
        "distill preview (dry run — nothing written)",
        (
            f"corpus: {stats['corpus_chunks']:,} chunks "
            f"({stats['after_dedupe']:,} after dedupe, "
            f"{stats['duplicates_removed']:,} duplicates removed)"
        ),
        (
            f"tau: {tau_result.tau:.4f} (raw {tau_result.tau_raw:.4f}, "
            f"source {tau_result.tau_source}"
            + (
                f", fallback: {tau_result.fallback_reason}"
                if tau_result.fallback_reason
                else ""
            ) + ")"
        ),
        (
            f"pairwise quartiles: p25 {tau_result.quartiles[0]:.3f} / "
            f"p50 {tau_result.quartiles[1]:.3f} / p75 {tau_result.quartiles[2]:.3f}"
        ),
        f"size histogram (kept + dropped): {json.dumps(stats['histogram'])}",
    ]
    if dropped_preview:
        lines.append("largest dropped clusters (top terms):")
        for size, terms in dropped_preview:
            lines.append(f"  {size:>4} chunks — {', '.join(terms) or '(no terms)'}")
    lines.append(f"note: {SCALE_NOTE}")
    lines.append(f"intent: {intent}")
    lines.append(f"view: {stats['view_id']} · corpus_hash {stats['corpus_hash'][:12]}")
    return "\n".join(lines)


# ------------------------------------------------------------------ rendering


def _fmt_loc(kind: str, loc, loc_end) -> str:
    if loc is None:
        return ""
    if kind == "pdf":
        if loc_end is not None and loc_end != loc:
            return f"p.{int(loc)}-{int(loc_end)}"
        return f"p.{int(loc)}"
    if kind in ("audio", "video"):
        def _hmmss(sec: float) -> str:
            sec = int(sec)
            h, rem = divmod(sec, 3600)
            m, s = divmod(rem, 60)
            if h:
                return f"{h}:{m:02d}:{s:02d}"
            return f"{m}:{s:02d}"

        end = loc_end if loc_end is not None else loc
        return f"{_hmmss(loc)}-{_hmmss(end)}"
    if loc_end is not None and loc_end != loc:
        return f"L{int(loc)}-{int(loc_end)}"
    return f"L{int(loc)}"


def _anchor(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s]+", "-", slug).strip("-") or "card"


def render_markdown(
    cards: list[Card],
    header: str,
    max_citations: int = 10,
    limit_cards: int | None = None,
) -> str:
    """Markdown v2 (§6.3): H1 + run header, TOC, per card the terms line and
    up to 3 excerpts as blockquotes each followed by its citation as a
    markdown link; per-member citations capped at 10 + `… +N more chunks`.
    limit_cards=None shows all; 0 shows none (zero-means-default, P3-13)."""
    shown = [c for c in cards if c.status == "active"]
    headers = [c for c in cards if c.status == "header"]
    if limit_cards is not None:
        shown = shown[:limit_cards]
    lines = ["# goldmine cards v2", "", header, ""]
    if shown:
        lines.append("## Contents")
        lines.append("")
        for card in shown:
            anchor = _anchor(f"{card.topic_id}. {card.title}")
            lines.append(f"{card.topic_id}. [{card.title}](#{anchor})")
        lines.append("")
    for card in shown:
        lines.append(
            f"### {card.topic_id}. {card.title or '(no title)'}   "
            f"[{card.size} chunks · {card.n_files} files]"
        )
        if card.terms:
            lines.append(f"keywords: {', '.join(card.terms)}")
        for ex in card.excerpts:
            lines.append(f"> {ex['text']}")
            loc_str = _fmt_loc(ex["kind"], ex["loc"], ex["loc_end"])
            cite = f"{ex['path']}:{loc_str}" if loc_str else ex["path"]
            lines.append(f"  — [`{cite}`](file://{ex['path']})")
        shown_members = card.member_citations[:max_citations]
        for path, kind, loc, loc_end in shown_members:
            loc_str = _fmt_loc(kind, loc, loc_end)
            suffix = f", {loc_str}" if loc_str else ""
            lines.append(f"  — {path} ({kind}{suffix})")
        if len(card.member_citations) > max_citations:
            lines.append(
                f"  … +{len(card.member_citations) - max_citations} more chunks"
            )
        lines.append("")
    for header_card in headers:
        lines.append(f"## {header_card.title}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(cards: list[Card], stats: dict) -> dict:
    """--json v2 (§6.3): schema goldmine.cards/v2, NO wall-clock fields (D14);
    the `llm` card key is present and always null in R6 (additive-only schema)."""

    def _parent_id(card: Card) -> int | None:
        # headers sit after all cards in the ordered list and carry their own
        # topic_id (§2.6) — parent_id is that id, None for top-level cards
        return card.parent.topic_id if card.parent is not None else None

    return {
        "schema": "goldmine.cards/v2",
        "stats": {k: stats[k] for k in PUBLIC_STATS_KEYS if k in stats},
        "cards": [
            {
                "id": card.topic_id,
                "parent_id": _parent_id(card),
                "title": card.title,
                "title_kind": card.title_kind,
                "terms": card.terms,
                "excerpts": card.excerpts,
                "size": card.size,
                "n_files": card.n_files,
                "status": card.status,
                "citations": [
                    {"path": p, "kind": k, "loc": loc, "loc_end": loc_end}
                    for p, k, loc, loc_end in card.member_citations
                ],
                "distill_algo": card.distill_algo,
                "run_id": card.run_id,
                "llm": None,
            }
            for card in cards
        ],
    }


# ------------------------------------------------------------- run orchestration
#
# One entry point (run_distill) serves the CLI and the canary eval: it decides
# full-vs-incremental (§4.3, F8), executes the pipeline, and returns the run's
# cards + stats. store.save_run persists it in ONE transaction.


def eligibility(
    prev_run: dict | None,
    *,
    tau_manual: float | None,
    refresh: bool,
    prev_member_files: set[str],
    current_member_files: set[str],
    embed_id: str,
) -> tuple[bool, str]:
    """Incremental eligibility (§4.3, F4/F8): a previous run for the view;
    τ equal (an explicit --threshold must match); embed_id equal; vanished
    member files ≤ 20% (a MODIFIED file is not deleted — F8b); not --refresh.
    corpus_hash NEVER gates eligibility (F4) — audit identity only."""
    if refresh:
        return False, "--refresh forces a full recompute"
    if prev_run is None:
        return False, "no previous run for this view"
    # the run row's algo string is parameterized ("… t=0.72 min=3"); the
    # eligibility comparison is on the algo FAMILY (tau is checked apart)
    if str(prev_run.get("algo", "")).split()[0] != ALGO:
        return False, f"algo changed ({prev_run.get('algo')} -> {ALGO})"
    if tau_manual is not None and repr(float(tau_manual)) != repr(float(prev_run["tau"])):
        return False, "explicit --threshold differs from the previous run's tau"
    if str(prev_run.get("stats", {}).get("embed_id")) != str(embed_id):
        return False, "embed identity changed since the previous run"
    if prev_member_files:
        deleted = prev_member_files - current_member_files
        ratio = len(deleted) / len(prev_member_files)
        if ratio > DELETE_RATIO_LIMIT:
            return False, (
                f"{len(deleted)}/{len(prev_member_files)} member files deleted "
                f"({ratio:.0%} > {DELETE_RATIO_LIMIT:.0%} budget)"
            )
    return True, "eligible"


def _dropped_preview(
    kept: list[CItem], min_members: int, tau: float
) -> list[tuple[int, list[str]]]:
    """Top terms of the 10 largest dropped clusters (--preview, §6.1)."""
    all_clusters, _hist = cluster_v2(kept, tau, min_members=1)
    dropped = [c for c in all_clusters if len(c) < min_members]
    dropped.sort(key=lambda c: (-len(c), (c[0].fid, c[0].sid)))
    out: list[tuple[int, list[str]]] = []
    for cluster in dropped[:10]:
        terms = cluster_terms(cluster, all_clusters)
        out.append((len(cluster), terms))
    return out


def _incremental_run(
    store: Store,
    *,
    scope: Scope,
    view_id: str,
    prev_run: dict,
    kept: list[CItem],
    items: list[CItem],
    min_members: int,
    fingerprint_id: str,
    embed_id: str,
    corpus_hash: str,
    current_member_files: set[str],
    refresh: bool = False,
) -> dict:
    """The incremental path (§4.3, D8): dissolve → snapshot → assign new
    chunks → leader remainder → one save_run. Decision points pinned by F8:
    (a) the membership snapshot happens BEFORE this run's mutations; (b) only
    vanished paths counted toward the deletion budget (checked in
    eligibility()); (c) a joining chunk takes the FIRST qualifying card,
    lowest index — never "nearest"; (d) the guard's member set is the
    ORIGINAL members loaded once — chunks joining this run never raise the
    bar mid-run. τ is inherited (tau_source='inherited', D8) and calibration
    is SKIPPED — no new sampled gram."""
    tau = float(prev_run["tau"])
    algo = f"{ALGO} t={tau:.2f} min={min_members}"
    mode = "incremental"

    # (the dissolve sweep settles PRIOR state: tombstones + below-min cards)
    dissolve = store.dissolve_pass(view_id, min_members)

    # F8a: snapshot BEFORE assignment
    view_cards = store.load_view_cards(view_id)
    member_sids = store.view_member_chunk_ids(view_id)
    new_items = [it for it in kept if it.sid not in member_sids]

    # F8d: ORIGINAL member vectors loaded once per card
    table = store.load_vectors()
    vec_by_sid = dict(zip(table.ids, table.matrix))
    member_mats: dict[int, np.ndarray] = {}
    for card in view_cards:
        vecs = [vec_by_sid[sid] for sid in card.members if sid in vec_by_sid]
        member_mats[card.id] = (
            np.stack(vecs) if vecs else np.zeros((0, table.matrix.shape[1]), np.float32)
        )

    # F8c: FIRST qualifying card wins (both gates), not the nearest
    tau_lo = max(tau - GUARD_MARGIN, GUARD_FLOOR)
    joins: dict[int, list[CItem]] = {}
    remainder: list[CItem] = []
    for item in new_items:
        placed = False
        for card in view_cards:  # ascending id == creation order
            if card.centroid is None:
                continue
            if float(card.centroid @ item.vec) < tau:
                continue
            mat = member_mats[card.id]
            if mat.shape[0] == 0 or float((mat @ item.vec).mean()) < tau_lo:
                continue
            joins.setdefault(card.id, []).append(item)
            placed = True
            break
        if not placed:
            remainder.append(item)

    # the remainder founds new cards (same leader as a full run)
    new_clusters, hist = cluster_v2(remainder, tau, min_members)
    dropped_small = sum(hist.values()) - len(new_clusters)

    citem_by_sid = {it.sid: it for it in items}
    cards: list[Card] = []

    # updated existing cards: frozen memberships + this run's joins; the
    # centroid is the renormalized mean over the FULL member set (never an
    # online fp16 update) and weights are cos-to-FINAL-centroid (§2.6)
    for card in view_cards:
        joined = joins.get(card.id, [])
        members = [citem_by_sid[sid] for sid in card.members if sid in citem_by_sid]
        members.extend(joined)
        mat = np.stack([m.vec for m in members])
        centroid = mat.sum(axis=0)
        norm = float(np.linalg.norm(centroid)) or 1.0
        centroid = centroid / norm
        weights = [float(v @ centroid) for v in mat]
        cards.append(Card(
            title=card.title,
            title_kind=card.title_kind,
            terms=json.loads(card.terms) if card.terms else [],
            excerpts=json.loads(card.excerpts) if card.excerpts else [],
            members=members,
            n_files=len({m.fid for m in members}),
            status="active",
            centroid=centroid,
            fingerprint_id=card.fingerprint_id,
            distill_algo=card.distill_algo,   # provenance of ORIGINAL build
            created_at=card.created_at,
            chunk_weights=[
                (members[i].sid, round(weights[i], 6)) for i in range(len(members))
            ],
        ))

    algo_str = f"{ALGO} t={tau:.2f} min={min_members}"
    for members in new_clusters:
        cards.append(_build_card(
            members, new_clusters,
            fingerprint_id=fingerprint_id, algo=algo_str,
        ))
    cards.sort(key=lambda c: (-c.size, c.first_member_key))

    rid = run_id_for(algo, tau, scope, corpus_hash, mode)
    stats = {
        "corpus_chunks": len(items),
        "after_dedupe": len(kept),
        "duplicates_removed": len(items) - len(kept),
        "clusters_kept": len(cards),
        "clusters_dropped_small": dropped_small,
        # final-state size distribution over active cards (existing + new)
        "histogram": size_histogram([c.members for c in cards]),
        "tau": round(tau, 4),
        "tau_raw": round(tau, 4),
        "tau_source": "inherited",
        "tau_inherited": True,
        "algo": algo,
        "mode": mode,
        "run_id": rid,
        "corpus_hash": corpus_hash,
        # F5 honest accounting: existing chunks sent back through the leader
        # (dissolve survivors) vs new chunks absorbed by existing cards
        "reclustered_chunks": dissolve.reentered,
        "chunks_reassigned_into_existing_cards": sum(len(v) for v in joins.values()),
        "dissolved_cards": dissolve.dissolved,
        "new_cards": len(new_clusters),
        "budget": None,
        "fingerprint_id": fingerprint_id,
        "view_id": view_id,
        "scope_under": sorted(scope.under) if scope.under else None,
        "scope_kinds": sorted(scope.kinds) if scope.kinds else None,
        "scope_after": scope.after,
        "member_files": sorted(current_member_files),
        "embed_id": embed_id,
        "fallback_reason": None,
    }
    for i, card in enumerate(cards, start=1):
        card.topic_id = i
        card.run_id = rid
        card.view_id = view_id
    return {"cards": cards, "stats": stats, "tau": TauResult(
        tau=tau, tau_source="inherited", tau_raw=tau,
        histogram=[], quartiles=(0.0, 0.0, 0.0),
    ), "view_id": view_id}


def run_distill(
    store: Store,
    *,
    scope: Scope,
    threshold: float | None = None,
    min_members: int = 3,
    max_chunks: int = 50000,
    refresh: bool = False,
    preview: bool = False,
) -> dict:
    """The v2 distill run (§2/§4). Returns a result dict:

    - preview=True: {"preview": <report text>, "stats": …, "tau": TauResult,
      "intent": …} — NOTHING is written (no save_run, no cards mutation).
    - else: {"cards": [Card], "stats": {…}} — the caller persists via
      store.save_run (kept separate so the eval harness can score runs
      in memory and the CLI owns the transaction boundary)."""
    items = _corpus(store, scope)
    _check_corpus_bound(items, max_chunks)
    kept, removed = dedupe(items)

    view_id = view_id_for(scope)
    fingerprint_id = store.meta("fingerprint_id") or ""
    embed_id = store.meta("embed_id") or ""
    corpus_hash = corpus_hash_for(store, scope)
    prev_run = store.latest_run(view_id)

    # F8a: the member-file snapshot BEFORE any mutation
    prev_member_files = set(
        (prev_run.get("stats") or {}).get("member_files") or []
    ) if prev_run else set()
    current_member_files = {path for path, _sha in _scoped_files(store, scope)}
    ok, reason = eligibility(
        prev_run,
        tau_manual=threshold,
        refresh=refresh,
        prev_member_files=prev_member_files,
        current_member_files=current_member_files,
        embed_id=embed_id,
    )

    if ok and not preview:
        inc = _incremental_run(
            store,
            scope=scope,
            view_id=view_id,
            prev_run=prev_run,
            kept=kept,
            items=items,
            min_members=min_members,
            fingerprint_id=fingerprint_id,
            embed_id=embed_id,
            corpus_hash=corpus_hash,
            current_member_files=current_member_files,
            refresh=refresh,
        )
        return inc

    # ---- full run (§2) ----
    tau_result = measure_tau(
        np.stack([it.vec for it in kept]) if kept else np.zeros((0, 768), np.float32),
        manual=threshold,
    )
    tau = tau_result.tau
    algo = f"{ALGO} t={tau:.2f} min={min_members}"
    mode = "full"
    cards, clusters, hist = build_cards_v2(
        kept, tau=tau, min_members=min_members, fingerprint_id=fingerprint_id,
    )
    rid = run_id_for(algo, tau, scope, corpus_hash, mode)
    stats = {
        "corpus_chunks": len(items),
        "after_dedupe": len(kept),
        "duplicates_removed": removed,
        "clusters_kept": len(cards),
        "clusters_dropped_small": sum(hist.values()) - len(clusters),
        "histogram": hist,
        "tau": round(tau, 4),
        "tau_raw": round(tau_result.tau_raw, 4),
        "tau_source": tau_result.tau_source,
        "tau_inherited": False,
        "algo": algo,
        "mode": mode,
        "run_id": rid,
        "corpus_hash": corpus_hash,
        "reclustered_chunks": 0,
        "chunks_reassigned_into_existing_cards": 0,
        "dissolved_cards": 0,
        "new_cards": len(cards),
        "budget": None,
        "fingerprint_id": fingerprint_id,
        # identity inputs the run rows carry for §4.3 eligibility (not JSON):
        "view_id": view_id,
        "scope_under": sorted(scope.under) if scope.under else None,
        "scope_kinds": sorted(scope.kinds) if scope.kinds else None,
        "scope_after": scope.after,
        "member_files": sorted(current_member_files),
        "embed_id": embed_id,
        "fallback_reason": tau_result.fallback_reason,
    }
    for i, card in enumerate(cards, start=1):
        card.topic_id = i
        card.run_id = rid
        card.view_id = view_id
    if preview:
        if ok:
            intent = f"incremental ({reason})"
        elif refresh:
            intent = "full recompute (--refresh)"
        else:
            intent = f"full recompute ({reason})"
        return {
            "preview": render_preview(
                stats, tau_result, _dropped_preview(kept, min_members, tau), intent,
            ),
            "stats": stats,
            "tau": tau_result,
            "cards": cards,
            "intent": intent,
        }
    return {"cards": cards, "stats": stats, "tau": tau_result, "view_id": view_id}
