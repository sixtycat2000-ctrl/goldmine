"""Distill v3: ensemble co-association topic cards over the index's vectors.

Design of record: reports/execution/P2-04/distill-purity-diagnosis-001/
design-proposal.md + design-revision-002.md (two adversarial review rounds).
Algorithm, normative:

  corpus = all vectorized segments in scope, ordered by (file_id, segment_id)
  1. exact dedupe by sha256 of normalized text
  2. near-dup removal: greedy, drop when cosine >= 0.97 to an already-kept chunk
  3. centering: mu = mean of the deduped row-normalized vectors; cmat = the
     rows renormalized after subtracting mu. On real corpora the raw cosine
     space is a narrow hub (cross-topic median ~0.6); centering restores
     threshold discriminability (cross median ~0) without changing ranks.
  4. deterministic ensemble (96 runs): spherical k-means with farthest-first
     init (start offsets 0..3, lowest-index ties, farthest-chunk revival) over
     BOTH spaces {centered, raw} x k in {20..64 step 4} (k_eff capped at n//4;
     above N_DENSE the k grid scales by n//ENS_K_SCALE and the run set shrinks
     to ENS_LARGE_K x 2 starts). co[i,j] = fraction of runs placing i and j in
     the same cluster. No RNG anywhere.
  5. fine leader: chunks in canonical order join the FIRST group whose mean
     co-association to the current members >= CO_LEADER_F (tau).
  6. dual-channel agglomerative remerge. Two groups merge while EITHER
     (a) co-channel: mean pair co >= REMERGE_RE AND min pair co >= REMERGE_MN
         (average-link merge with a complete-link guard — no junk may ride)
     OR (b) geo-channel: centered-centroid cosine >= GEO_THETA AND the minimum
         centered cosine across the two groups' pairs >= GEO_MINC (repairs
         same-topic fragments on cleanly separable corpora, where the
     ensemble consistently splits topics and fragment co is exactly 0).
     Deterministic: candidates compete on (channel, score), strict >, (a,b)
     ascending scan; block statistics are recomputed after every merge.
  7. groups < min_members are dropped — never silently: the full size
     histogram (kept + dropped) is reported in stats and --preview.
  8. cards sorted by (size desc, first member); card = medoid primary excerpt
     + up to 2 coverage excerpts + filtered tf-idf terms + extractive title.

tau semantics (v3): tau IS the co-association admission threshold F
(CO_LEADER_F). It is an architecture constant, not calibrated per corpus —
the v2 pairwise-cosine calibration (Otsu/P90) was retired after real-corpus
diagnosis showed it lands on the within-topic median of a hub distribution
(reports/execution/P2-04/distill-purity-diagnosis-001/README.md). The
co-association distribution itself is still reported honestly (histogram +
quartiles). tau_source ∈ {fixed, manual, inherited}; --threshold overrides F;
incremental runs inherit the previous run's F unchanged.

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

ALGO_FAMILY = "ens-coassoc"          # view identity family (D7 semantics)
ALGO = "ens-coassoc.v3"              # v3 algo string (run rows + cards)
NEAR_DUP_COSINE = 0.97
TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# -- ensemble (deterministic; no RNG) ------------------------------------------
ENS_K_GRID = (20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60, 64)
ENS_STARTS = (0, 1, 2, 3)            # farthest-first start offsets
ENS_LARGE_K = (20, 28, 36, 44, 52, 60)   # reduced grid above N_DENSE
SKM_ITERS = 300
SKM_TOL = 1e-7                       # np.allclose(newC, C, atol=SKM_TOL)

# -- co-association consumption -------------------------------------------------
CO_LEADER_F = 0.45                   # tau: fine-leader admission threshold
REMERGE_RE = 0.22                    # co-channel mean gate
REMERGE_MN = 0.10                    # co-channel complete-link guard
GEO_THETA = 0.50                     # geo-channel centered-centroid gate
GEO_MINC = 0.20                      # geo-channel min centered-pair guard

# -- scale tiers -----------------------------------------------------------------
N_DENSE = 8000                       # n <= N_DENSE: exact chunk-level co
MICRO_FLOOR = 256
MICRO_SQRT_K = 8                     # m0 = min(n, max(256, 8*isqrt(n)))
ENS_K_SCALE = 1024                   # micro tier: s = n // 1024 (floor)
CO_ACC_ROWS = 256                    # dense-tier row-blocked accumulation

# -- incremental (centered-space cosine gates) -----------------------------------
INC_JOIN_COS = 0.30
INC_GUARD_COS = 0.20
DELETE_RATIO_LIMIT = 0.20      # > 20% member-file deletions force a full run

# -- report ---------------------------------------------------------------------------
CALIB_BINS = 256               # 256-bin histogram over [0, 1] (report only)
CALIB_SAMPLE_ROWS = 300        # stride-gram rows for the co distribution report

# -- excerpts (D11) -------------------------------------------------------------
COVERAGE_STOP_DISTANCE = 0.25  # stop when the next pick is < 0.25 from all picked
EXCERPTS_PER_CARD = 3          # primary + up to 2 coverage
TITLE_MAX_CHARS = 70

_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{6,}$")
_UNIT_RE = re.compile(r"^\d+(\.\d+)?(kb|mb|gb|tb|px|hz)$")

HISTOGRAM_BUCKETS = ("1", "2", "3-5", "6-10", "11-25", "26-100", "100+")

#: The --json stats key set (§6.3 "EXACTLY"). The internal stats dict carries
#: more (view_id, member_files, embed_id, scope, center_mu, ens_mode…) for run
#: rows and preview — render_json projects to exactly this list so the
#: published schema is additive-only from here.
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
    """Co-association threshold report. tau is the architecture constant F
    (or the --threshold override / the inherited value); histogram and
    quartiles describe the observed co-association distribution."""

    tau: float
    tau_source: str                # fixed|manual|inherited
    tau_raw: float
    histogram: list[int]           # 256 bins over [0, 1], clamped indexing
    quartiles: tuple[float, float, float]
    fallback_reason: str | None = None   # always None in v3 (kept for schema)


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
    centroid: np.ndarray | None = None   # (768,) float32, RAW-space mean
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
        """Store contract: cards.excerpts is a JSON array string."""
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


# ----------------------------------------------------- co-association report


def _sample_pair_values(matrix: np.ndarray) -> np.ndarray:
    """Deterministic upper-triangle sample of a square value matrix (the
    co-association report input): all pairs when N <= CALIB_SAMPLE_ROWS, else
    rows i_k = round(k·N/300) (duplicates collapsed, order preserved)."""
    n = matrix.shape[0]
    if n <= CALIB_SAMPLE_ROWS:
        iu = np.triu_indices(n, k=1)
        return matrix[iu]
    rows: list[int] = []
    seen: set[int] = set()
    for k in range(CALIB_SAMPLE_ROWS):
        i = round(k * n / CALIB_SAMPLE_ROWS)
        if i not in seen:
            seen.add(i)
            rows.append(i)
    sub = matrix[np.ix_(rows, rows)]
    iu = np.triu_indices(len(rows), k=1)
    return sub[iu]


def _histogram_of(vals: np.ndarray) -> np.ndarray:
    """256 bins over [0, 1]; values are clamped into range — never wrap."""
    hist = np.zeros(CALIB_BINS, dtype=np.int64)
    bins = np.clip((vals * CALIB_BINS).astype(np.int64), 0, CALIB_BINS - 1)
    for b in bins:
        hist[b] += 1
    return hist


def co_tau_report(co: np.ndarray, *, manual: float | None = None) -> TauResult:
    """The v3 tau report: tau = F (constant or --threshold override); the
    observed co-association distribution is reported, never decides."""
    vals = _sample_pair_values(co)
    hist = _histogram_of(vals)
    quartiles = tuple(float(q) for q in np.percentile(vals, [25, 50, 75])) \
        if vals.size else (0.0, 0.0, 0.0)
    tau = float(manual) if manual is not None else float(CO_LEADER_F)
    return TauResult(
        tau=tau, tau_source="manual" if manual is not None else "fixed",
        tau_raw=tau, histogram=[int(x) for x in hist],
        quartiles=quartiles,  # type: ignore[arg-type]
    )


# ----------------------------------------------------------------- clustering


def _renorm_rows(m: np.ndarray) -> np.ndarray:
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)


def skm_det(M: np.ndarray, k: int, start: int = 0) -> np.ndarray:
    """Deterministic spherical k-means.

    Farthest-first init from canonical index `start mod n`; every argmax tie
    takes the lowest index; an empty cluster revives as the chunk farthest
    from its previous center. Zero RNG — the whole v3 core is reproducible
    byte-for-byte on identical input."""
    n = len(M)
    centers = [start % n]
    d = 1.0 - M @ M[centers[0]]
    for _ in range(k - 1):
        nxt = int(np.argmax(d))
        centers.append(nxt)
        d = np.minimum(d, 1.0 - M @ M[nxt])
    C = M[centers].copy()
    for _ in range(SKM_ITERS):
        lab = (M @ C.T).argmax(axis=1)
        newC = np.zeros_like(C)
        for j in range(k):
            ids = np.nonzero(lab == j)[0]
            if len(ids):
                s = M[ids].sum(axis=0)
                newC[j] = s / max(float(np.linalg.norm(s)), 1e-12)
            else:
                newC[j] = M[int(np.argmin(M @ C[j]))]
        if np.allclose(newC, C, atol=SKM_TOL):
            C = newC
            break
        C = newC
    return (M @ C.T).argmax(axis=1)


def _dense_coassociation(n: int, spaces: tuple[np.ndarray, ...]) -> np.ndarray:
    """Exact chunk-level co-association, row-blocked accumulation (the n×n
    outer product is never materialized whole)."""
    co = np.zeros((n, n), np.float32)
    runs = 0
    for M in spaces:
        for k in ENS_K_GRID:
            k_eff = max(2, min(k, n // 4))
            for start in ENS_STARTS:
                lab = skm_det(M, k_eff, start)
                oh = np.zeros((n, k_eff), np.float32)
                oh[np.arange(n), lab] = 1.0
                for r0 in range(0, n, CO_ACC_ROWS):
                    co[r0:r0 + CO_ACC_ROWS] += \
                        oh[r0:r0 + CO_ACC_ROWS] @ oh.T
                runs += 1
    co /= runs
    np.fill_diagonal(co, 1.0)
    return co, runs


def _micro_coassociation(n: int, cmat: np.ndarray):
    """Micro-tier co-association (n > N_DENSE): fold the ensemble onto
    m0 = min(n, max(256, 8·isqrt(n))) micro clusters. co_micro[a,b] is the
    exact mean chunk-level co-association between cells a and b; the diagonal
    is defined as literal 1.0 (the 0/0 formula never applies). Same-cell
    chunks are never separated — the accepted approximation of this tier.
    micro_split_rate (report-only monitoring): mean share of cells whose
    chunks land in more than one cluster across the ensemble runs."""
    m0 = min(n, max(MICRO_FLOOR, MICRO_SQRT_K * math.isqrt(n)))
    micro_lab = skm_det(cmat, m0, 0)
    sizes = np.bincount(micro_lab, minlength=m0).astype(np.float64)
    cell_members = [np.nonzero(micro_lab == c)[0] for c in range(m0)]
    s = n // ENS_K_SCALE
    P = np.zeros((m0, m0), np.float64)
    split_total = 0
    runs = 0
    for k in ENS_LARGE_K:
        k_eff = max(2, min(k * s, n // 4))
        for start in (0, 1):
            lab = skm_det(cmat, k_eff, start)
            A = np.zeros((m0, k_eff), np.float64)
            np.add.at(A, (micro_lab, lab), 1.0)
            P += A @ A.T
            for c in range(m0):
                members = cell_members[c]
                if members.size and np.unique(lab[members]).size > 1:
                    split_total += 1
            runs += 1
    outer = np.outer(sizes, sizes)
    co_micro = P / (outer * runs)
    np.fill_diagonal(co_micro, 1.0)
    return co_micro, micro_lab, runs, split_total / (runs * m0)


def build_coassociation(mat: np.ndarray):
    """Ensemble co-association over the deduped corpus.

    Returns (co, cmat, mode, runs, micro_lab) — micro_lab is None in the
    dense tier. Dense-tier exactness and the micro fold formula are pinned
    by unit tests against brute force."""
    n = len(mat)
    mu = mat.mean(axis=0)
    cmat = _renorm_rows(mat - mu)
    if n <= N_DENSE:
        co, runs = _dense_coassociation(n, (cmat, mat))
        return co, cmat, "dense", runs, None
    co_micro, micro_lab, runs, split = _micro_coassociation(n, cmat)
    return co_micro, cmat, "micro", runs, micro_lab


def co_leader_fine(co: np.ndarray, f: float) -> list[list[int]]:
    """Fine leader over the co-association matrix: canonical order, a chunk
    joins the FIRST group (creation order) whose members' mean co to it >= f;
    else it founds a new group. Same first-wins contract as the v2 leader."""
    groups: list[list[int]] = []
    for i in range(len(co)):
        placed = False
        for gi in range(len(groups)):
            if float(co[groups[gi], i].mean()) >= f:
                groups[gi].append(i)
                placed = True
                break
        if not placed:
            groups.append([i])
    return groups


def remerge_dual(
    co: np.ndarray, cmat: np.ndarray, groups: list[list[int]],
    re: float = REMERGE_RE, mn: float = REMERGE_MN,
    theta_c: float = GEO_THETA, min_c: float = GEO_MINC,
) -> list[list[int]]:
    """Dual-channel agglomerative remerge.

    A candidate pair merges while EITHER the co-channel (mean pair co >= re
    AND min pair co >= mn — average-link merge, complete-link guard) OR the
    geo-channel (centered-centroid cosine >= theta_c AND min centered pair
    cosine >= min_c) qualifies. Candidates compete on (channel, score) —
    co-channel (1.0, mean) outranks geo-channel (0.5, cc); exact ties break
    by the smallest (a, b) — identical decisions to a full ascending rescan
    with strict >.

    Complexity (review N5): pair statistics are computed EXACTLY per pair
    (np.ix_ block mean/min over the CURRENT members — the same arithmetic a
    full rescan would evaluate) but only maintained for pairs involving a
    merged group; qualified pairs are kept in a small candidate dict. The
    initial all-singleton pass is vectorized (block mean == block min == the
    scalar co entry)."""
    mm = [list(g) for g in groups]
    G = len(mm)
    cents = []
    for g in mm:
        s_ = cmat[g].sum(axis=0)
        cents.append(s_ / max(float(np.linalg.norm(s_)), 1e-12))

    def _pair_qual(a: int, b: int):
        """(channel, score) for a qualifying pair, else None. Exact stats."""
        block = co[np.ix_(mm[a], mm[b])]
        mean, mnv = float(block.mean()), float(block.min())
        if mean >= re and mnv >= mn:
            return (1.0, mean)
        cc = float(cents[a] @ cents[b])
        if cc >= theta_c:
            if min_c > 0.0:
                pmin = float(np.min(cmat[mm[a]] @ cmat[mm[b]].T))
                if pmin < min_c:
                    return None
            return (0.5, cc)
        return None

    candidates: dict[tuple[int, int], tuple[float, float]] = {}

    def _refresh(a: int, live: list[int]) -> None:
        for other in live:
            if other == a:
                continue
            key = (a, other) if a < other else (other, a)
            q = _pair_qual(a, other)
            if q is not None:
                candidates[key] = q
            else:
                candidates.pop(key, None)

    # initial pass: every group is a singleton at entry for the fine-leader
    # output — mean == min == the scalar co entry (vectorized candidate scan)
    if all(len(g) == 1 for g in mm):
        co_thr = np.maximum(re, mn)
        iu = np.triu_indices(G, k=1)
        ok_co = co[np.ix_([g[0] for g in mm], [g[0] for g in mm])] >= co_thr
        cc_mat = np.stack(cents) @ np.stack(cents).T
        for a, b in zip(*iu):
            if ok_co[a, b]:
                candidates[(a, b)] = (1.0, float(co[mm[a][0], mm[b][0]]))
            elif float(cc_mat[a, b]) >= theta_c:
                if min_c > 0.0:
                    pmin = float(cmat[mm[a][0]] @ cmat[mm[b][0]])
                    if pmin < min_c:
                        continue
                candidates[(a, b)] = (0.5, float(cc_mat[a, b]))
    else:
        live = list(range(G))
        for a in live:
            _refresh(a, live)

    live = list(range(G))
    while candidates:
        best_key, best_q = None, None
        for key, q in candidates.items():
            if best_q is None or q > best_q or (
                q == best_q and key < best_key
            ):
                best_key, best_q = key, q
        a, b = best_key
        mm[a] = mm[a] + mm[b]
        s_ = cmat[mm[a]].sum(axis=0)
        cents[a] = s_ / max(float(np.linalg.norm(s_)), 1e-12)
        live.remove(b)
        for key in [k for k in candidates if k[0] == b or k[1] == b]:
            del candidates[key]
        _refresh(a, live)
    return [mm[i] for i in sorted(live)]


def cluster_leader(
    items: list[CItem], tau: float, tau_lo: float,
    min_members: int = 3,
) -> tuple[list[list[CItem]], dict[str, int]]:
    """Leader clustering with the average-link admission guard (v2's D4,
    parameterized). The caller decides the space: items carry whatever
    vectors it wants compared (the incremental remainder passes centered
    vectors). Deterministic given the (fid, sid) order."""
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


def ens_coassoc_v3(kept: list[CItem], min_members: int = 3, *, f: float | None = None):
    """The v3 full-run clustering core.

    Returns (kept_groups, all_groups, mu, mode, runs, co) where groups are
    lists of indices into `kept`; co is the (chunk- or micro-level)
    co-association matrix used for the report."""
    mat = _renorm_rows(np.stack([it.vec for it in kept]).astype(np.float32))
    mu = mat.mean(axis=0)
    f_val = float(f) if f is not None else CO_LEADER_F
    co, cmat, mode, runs, micro_lab = build_coassociation(mat)
    if mode == "dense":
        groups = co_leader_fine(co, f_val)
        merged = remerge_dual(co, cmat, groups)
        kept_groups = [g for g in merged if len(g) >= min_members]
        return kept_groups, merged, mu, mode, runs, co
    # micro tier: cluster at micro-cell level, then expand cells to chunks.
    # The remerge's geometric channel indexes cmat by CELL id, so it receives
    # the per-cell centered centroid matrix — never the chunk-level cmat.
    unit_by_cell: dict[int, list[int]] = {}
    for i, cell in enumerate(micro_lab):
        unit_by_cell.setdefault(int(cell), []).append(i)
    cell_ids = sorted(unit_by_cell)
    cell_cents = []
    for cell in cell_ids:
        members = unit_by_cell[cell]
        v = cmat[members].sum(axis=0)
        cell_cents.append(v / max(float(np.linalg.norm(v)), 1e-12))
    cell_cmat = np.stack(cell_cents) if cell_cents else cmat[:0]
    id_of_cell = {cell: idx for idx, cell in enumerate(cell_ids)}
    micro_groups = co_leader_fine(co, f_val)
    micro_merged = remerge_dual(co, cell_cmat, micro_groups)
    all_groups = [
        [i for cell in g for i in unit_by_cell[cell_ids[int(cell)]]]
        for g in micro_merged
    ]
    return (
        [g for g in all_groups if len(g) >= min_members],
        all_groups, mu, mode, runs, co,
    )


# ----------------------------------------------------------------- excerpts / content


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
    both."""
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
        for row in store.db.execute("SELECT rowid, coalesce(original, body) FROM fts").fetchall()
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
    into: Card | None = None,
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
    card = into if into is not None else Card(
        title="", title_kind="terms", terms=[], excerpts=[], members=[],
        n_files=0,
    )
    card.title = title
    card.title_kind = title_kind
    card.terms = terms
    card.excerpts = excerpts
    card.members = list(members)
    card.n_files = len({m.fid for m in members})
    card.centroid = centroid
    card.status = status
    card.fingerprint_id = fingerprint_id
    card.distill_algo = algo
    card.created_at = time.time()
    card.chunk_weights = [
        (members[i].sid, round(weights[i], 6)) for i in range(len(members))
    ]
    return card


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


def build_cards(
    kept: list[CItem],
    groups_kept: list[list[int]],
    groups_all: list[list[int]],
    *,
    tau: float,
    min_members: int,
    fingerprint_id: str,
) -> list[Card]:
    """Groups → card objects (§2). Cards are active leaves only (the v2 D5
    header level was retired with the s̄ trigger — see diagnosis). Ordering
    (-size, first member); topic ids assigned over that order (§2.6)."""
    algo = f"{ALGO} t={tau:.2f} min={min_members}"
    all_groups_items = [[kept[i] for i in g] for g in groups_all]
    cards: list[Card] = []
    for g in groups_kept:
        cards.append(_build_card(
            [kept[i] for i in g], all_groups_items,
            fingerprint_id=fingerprint_id, algo=algo,
        ))
    cards.sort(key=lambda c: (-c.size, c.first_member_key))
    for i, card in enumerate(cards, start=1):
        card.topic_id = i
    return cards


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
    "τ is the co-association admission threshold (ensemble co-cluster "
    "frequency in [0, 1]), not a cosine scale"
)

#: F2 (preview τ honesty): shown when the preview's fresh τ report is NOT
#: what an actual run would use — an incremental-eligible run inherits the
#: previous run's τ (D8) and skips the report entirely.
PREVIEW_TAU_INHERIT_NOTE = (
    "τ shown is a fresh full-run value; an actual run would inherit "
    "τ={tau:.4f} (tau_source=inherited)"
)


def render_preview(
    stats: dict,
    tau_result: TauResult,
    dropped_preview: list[tuple[int, list[str]]],
    intent: str,
    *,
    inherited_tau: float | None = None,
) -> str:
    """--preview report (§6.1): corpus size, τ (+raw/source), co-association
    quartiles, size histogram, top terms of the 10 largest dropped groups,
    the scale note, and the full-vs-incremental intent. Writes NOTHING."""
    lines = [
        "distill preview (dry run — nothing written)",
        (
            f"corpus: {stats['corpus_chunks']:,} chunks "
            f"({stats['after_dedupe']:,} after dedupe, "
            f"{stats['duplicates_removed']:,} duplicates removed)"
        ),
        (
            f"tau: {tau_result.tau:.4f} (raw {tau_result.tau_raw:.4f}, "
            f"source {tau_result.tau_source})"
        ),
    ]
    if inherited_tau is not None:
        lines.append(
            "note: " + PREVIEW_TAU_INHERIT_NOTE.format(tau=inherited_tau)
        )
    lines.extend([
        (
            f"co-association quartiles: p25 {tau_result.quartiles[0]:.3f} / "
            f"p50 {tau_result.quartiles[1]:.3f} / p75 {tau_result.quartiles[2]:.3f}"
        ),
        f"size histogram (kept + dropped): {json.dumps(stats['histogram'])}",
    ])
    if dropped_preview:
        lines.append("largest dropped groups (top terms):")
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
            return f"{m:02d}:{s:02d}"

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
    limit_cards=None shows all; 0 shows none (zero-means-default, P3-13).
    v3 cards are all active; header-group branches remain for historical
    rows rendered from older runs."""
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
    printed_headers: set[int] = set()
    header_by_child: dict[int, Card] = {}
    for h in headers:
        for child in getattr(h, "children", []) or []:
            header_by_child[id(child)] = h

    def _emit_header(h: Card) -> None:
        if id(h) in printed_headers:
            return
        printed_headers.add(id(h))
        lines.append(f"## {h.title or '(no section title)'}")
        lines.append("")

    for card in shown:
        h = header_by_child.get(id(card))
        if h is not None:
            _emit_header(h)
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
    return "\n".join(lines).rstrip() + "\n"


def render_json(cards: list[Card], stats: dict) -> dict:
    """--json v2 (§6.3): schema goldmine.cards/v2, NO wall-clock fields (D14);
    the `llm` card key is present and always null (additive-only schema)."""

    def _parent_id(card: Card) -> int | None:
        # v3 emits active cards only; header rows from historical runs keep
        # their parent linkage — parent_id is that id, None for top-level
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
    member files ≤ 20% (a MODIFIED file is not deleted — F8b); the previous
    run carries the v3 internal state (center_mu — v2-era rows predate it);
    not --refresh. corpus_hash NEVER gates eligibility (F4) — audit identity
    only."""
    if refresh:
        return False, "--refresh forces a full recompute"
    if prev_run is None:
        return False, "no previous run for this view"
    # the run row's algo string is parameterized ("… t=0.45 min=3"); the
    # eligibility comparison is on the algo FAMILY (tau is checked apart)
    if str(prev_run.get("algo", "")).split()[0] != ALGO:
        return False, f"algo changed ({prev_run.get('algo')} -> {ALGO})"
    if tau_manual is not None and repr(float(tau_manual)) != repr(float(prev_run["tau"])):
        return False, "explicit --threshold differs from the previous run's tau"
    if str(prev_run.get("stats", {}).get("embed_id")) != str(embed_id):
        return False, "embed identity changed since the previous run"
    if not (prev_run.get("stats", {}) or {}).get("center_mu"):
        return False, "previous run predates center_mu (full recompute required)"
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
    kept: list[CItem], groups_all: list[list[int]], min_members: int,
) -> list[tuple[int, list[str]]]:
    """Top terms of the 10 largest dropped groups (--preview, §6.1)."""
    dropped = [g for g in groups_all if len(g) < min_members]
    dropped.sort(key=lambda g: (-len(g), (kept[g[0]].fid, kept[g[0]].sid)))
    all_groups_items = [[kept[i] for i in g] for g in groups_all]
    out: list[tuple[int, list[str]]] = []
    for group in dropped[:10]:
        terms = cluster_terms([kept[i] for i in group], all_groups_items)
        out.append((len(group), terms))
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
    bar mid-run. τ (=F) is inherited (tau_source='inherited', D8) and the
    ensemble is SKIPPED — new chunks join by centered-space cosine gates."""
    tau = float(prev_run["tau"])
    prev_stats = prev_run.get("stats", {}) or {}
    mu = np.asarray(prev_stats["center_mu"], dtype=np.float32)
    algo = f"{ALGO} t={tau:.2f} min={min_members}"
    mode = "incremental"

    # (the dissolve sweep settles PRIOR state: tombstones + below-min cards)
    dissolve = store.dissolve_pass(view_id, min_members)

    # F8a: snapshot BEFORE assignment
    view_cards = store.load_view_cards(view_id)
    member_sids = store.view_member_chunk_ids(view_id)
    new_items = [it for it in kept if it.sid not in member_sids]

    # F8d: ORIGINAL member vectors loaded once per card, centered once
    table = store.load_vectors()
    vec_by_sid = dict(zip(table.ids, table.matrix))
    member_cvecs: dict[int, np.ndarray] = {}
    for card in view_cards:
        vecs = [vec_by_sid[sid] for sid in card.members if sid in vec_by_sid]
        if vecs:
            cv = np.stack(vecs) - mu
            member_cvecs[card.id] = _renorm_rows(cv)
        else:
            member_cvecs[card.id] = np.zeros((0, mu.shape[0]), np.float32)

    # F8c: FIRST qualifying card wins (both gates), not the nearest
    joins: dict[int, list[CItem]] = {}
    remainder: list[CItem] = []
    for item in new_items:
        xc = (item.vec.astype(np.float32) - mu)
        xc = xc / max(float(np.linalg.norm(xc)), 1e-12)
        placed = False
        for card in view_cards:  # ascending id == creation order
            if card.centroid is None:
                continue
            cc = card.centroid.astype(np.float32) - mu
            cc = cc / max(float(np.linalg.norm(cc)), 1e-12)
            if float(cc @ xc) < INC_JOIN_COS:
                continue
            mat = member_cvecs[card.id]
            if mat.shape[0] == 0 or float((mat @ xc).mean()) < INC_GUARD_COS:
                continue
            joins.setdefault(card.id, []).append(item)
            placed = True
            break
        if not placed:
            remainder.append(item)

    # the remainder clusters itself: centered-space leader at the incremental
    # gates (tau=INC_JOIN_COS, guard=INC_GUARD_COS). Card building consumes
    # the ORIGINAL raw-vector items (centroids are stored raw-space).
    def _centered(it: CItem) -> CItem:
        v = it.vec.astype(np.float32) - mu
        return CItem(sid=it.sid, fid=it.fid,
                     vec=v / max(float(np.linalg.norm(v)), 1e-12),
                     text=it.text, path=it.path, kind=it.kind,
                     loc=it.loc, loc_end=it.loc_end)

    raw_by_sid = {it.sid: it for it in remainder}
    centered_remainder = [_centered(it) for it in remainder]
    new_clusters, hist = cluster_leader(
        centered_remainder, INC_JOIN_COS, INC_GUARD_COS, min_members,
    )
    new_clusters = [[raw_by_sid[m.sid] for m in c] for c in new_clusters]
    dropped_small = sum(hist.values()) - len(new_clusters)

    citem_by_sid = {it.sid: it for it in items}
    cards: list[Card] = []

    # updated existing cards: frozen memberships + this run's joins; the
    # centroid is the renormalized RAW-space mean over the FULL member set
    # and weights are cos-to-FINAL-centroid (§2.6)
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

    all_new_groups = new_clusters  # kept clusters only for term idf
    for cluster in new_clusters:
        cards.append(_build_card(
            cluster, all_new_groups,
            fingerprint_id=fingerprint_id, algo=algo,
        ))
    ordered = [c for c in cards if c.status == "active"]
    ordered.sort(key=lambda c: (-c.size, c.first_member_key))
    cards = ordered

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
        # chained-increment state (M1): forwarded verbatim so the next
        # incremental run against THIS run stays eligible
        "center_mu": [float(x) for x in mu],
        "ens_mode": prev_stats.get("ens_mode"),
        "ens_runs": prev_stats.get("ens_runs"),
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
    """The v3 distill run (§2/§4). Returns a result dict:

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
    groups_kept, groups_all, mu, mode, runs, co = ens_coassoc_v3(
        kept, min_members, f=float(threshold) if threshold is not None else None,
    )
    tau_result = co_tau_report(co, manual=threshold)
    tau = tau_result.tau
    algo = f"{ALGO} t={tau:.2f} min={min_members}"
    mode_str = "full"
    cards = build_cards(
        kept, groups_kept, groups_all,
        tau=tau, min_members=min_members, fingerprint_id=fingerprint_id,
    )
    rid = run_id_for(algo, tau, scope, corpus_hash, mode_str)
    stats = {
        "corpus_chunks": len(items),
        "after_dedupe": len(kept),
        "duplicates_removed": removed,
        "clusters_kept": len(cards),
        "clusters_dropped_small": len(groups_all) - len(groups_kept),
        "histogram": size_histogram([[kept[i] for i in g] for g in groups_all]),
        "tau": round(tau, 4),
        "tau_raw": round(tau_result.tau_raw, 4),
        "tau_source": tau_result.tau_source,
        "tau_inherited": False,
        "algo": algo,
        "mode": mode_str,
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
        "fallback_reason": None,
        # v3 internal state (persisted in the run row; not in PUBLIC_STATS)
        "center_mu": [float(x) for x in mu],
        "ens_mode": mode,
        "ens_runs": runs,
    }
    for i, card in enumerate(cards, start=1):
        card.topic_id = i
        card.run_id = rid
        card.view_id = view_id
    if preview:
        if ok:
            intent = f"incremental ({reason})"
            # F2 τ honesty: the fresh value above is NOT what an actual run
            # would use — incremental inherits the previous run's τ (D8)
            # and skips the ensemble entirely. Say so, loudly.
            inherited_tau = float(prev_run["tau"])
        elif refresh:
            intent = "full recompute (--refresh)"
            inherited_tau = None
        else:
            intent = f"full recompute ({reason})"
            inherited_tau = None
        return {
            "preview": render_preview(
                stats, tau_result,
                _dropped_preview(kept, groups_all, min_members), intent,
                inherited_tau=inherited_tau,
            ),
            "stats": stats,
            "tau": tau_result,
            "cards": cards,
            "intent": intent,
        }
    return {"cards": cards, "stats": stats, "tau": tau_result, "view_id": view_id}
