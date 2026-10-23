"""Search scoring: brute-force cosine, z-scores, coverage, fusion, baselines.

Fusion (normative, SPEC §3 / V3):
  candidates = {file: best z >= z_gate} ∪ {file: coverage >= 0.5}
  score      = w_m*max(z, 0) + w_c*coverage^1.5 + kind_hint*1.0 (flat, never stacks)
  (w_m, w_c) = (1.0, 2.0); lookup queries (ANY term digit-bearing and
  alphanumeric-mixed or >= 4 digits) invert to (0.5, 4.0)
  bm25 is LOWER-is-better: used for ordering/ranks only, never added to score.
Missing vectors: vectorless chunks compete lexically only; zero-vector files can
still qualify on coverage; their z/cosine render as None.
The z gate is the SINGLE qualification rule. v2.1 calibration A/B (2026-10-17,
n=146 llama eval, scripts/eval_real.py harness): gate 2.0 + floor -> gate 3.0
moves s@5 0.747 -> 0.781, MRR 0.646 -> 0.665, zero-hit honesty 0.4 -> 0.6 with
zero per-tag regressions; train and holdout agree. The v2.0 two-bar spelling
(`z >= 2.0 AND (cov > 0 OR z >= 3.0)`) is subsumed — at gate = floor the
second bar is absorbed algebraically, and the A/B measured the two spellings
byte-identical on every query. A single sensitivity sweep peaked near 3.5
(s@5 0.822, still zero s@5 regressions): recorded as the v2.1 G1 calibration
candidate, deliberately NOT shipped (one sweep — overfit risk).
"""

from __future__ import annotations

import calendar
from contextlib import nullcontext
import datetime
import hashlib
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from .backends.base import query_prompt
from .admission import AdmissionParameters, SEARCH_BEHAVIOR_VERSION, admit_files
from .lexical import normalize as normalize_lexical
from .retrieval_lexical import collect_lexical, add_verified_witnesses
from .anchor_admission import resolve_obligations
from .deadline import check_deadline
from .retrieval_evidence import select_evidence, semantic_segments
from .store import Store
from .util import snippet as make_snippet

if TYPE_CHECKING:  # pragma: no cover
    from .backends.base import EmbeddingBackend

# DigUp Searcher.stopwords, English subset for v1.
STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and", "or",
    "my", "me", "that", "this", "these", "those", "from", "about", "where", "what",
    "which", "who", "when", "is", "was", "are", "were", "be", "it", "its", "some",
    "any", "find", "show", "file", "files", "photo", "photos", "picture", "image",
    "images", "screenshot", "screenshots", "video", "videos", "pdf", "document",
    "documents", "recording",
})

#: Kind words the query asks for ("…in a video", "screenshot of…"): a soft,
#: capped +1.0 boost for a matching file kind — never a filter, never stacked.
KIND_WORDS: dict[str, str] = {
    "screenshot": "image", "screenshots": "image",
    "photo": "image", "photos": "image", "picture": "image", "pictures": "image",
    "image": "image", "images": "image",
    "pdf": "pdf", "pdfs": "pdf", "paper": "pdf", "papers": "pdf",
    "document": "doc", "documents": "doc", "note": "doc", "notes": "doc",
    "video": "video", "videos": "video", "clip": "video", "movie": "video",
    "audio": "audio", "song": "audio", "songs": "audio", "podcast": "audio",
    "podcasts": "audio", "voice": "audio", "sound": "audio",
    "webpage": "webpage", "webpages": "webpage", "website": "webpage",
    "websites": "webpage", "site": "webpage", "sites": "webpage",
    "url": "webpage", "urls": "webpage",
}

_MEANING_WEIGHT = 1.0
_WORDS_WEIGHT = 2.0
_LOOKUP_MEANING_WEIGHT = 0.5
_LOOKUP_WORDS_WEIGHT = 4.0
_TINY_GROUP = 20
_STD_FLOOR = 0.02

#: Near-duplicate fold threshold (SPEC distill near-dup parity): segment
#: vectors this close are the same text for display purposes.
_NEAR_DUP_COSINE = 0.97

_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")


def _like_escape(text: str) -> str:
    """Escape SQL LIKE wildcards so a path prefix matches literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pair_cosine(a: "np.ndarray | None", b: "np.ndarray | None") -> float | None:
    """Cosine of two ALREADY unit-normalized hit vectors (None when either is
    missing — vectorless segments fold on text identity only)."""
    if a is None or b is None:
        return None
    return float(np.dot(a, b))


# ------------------------------------------------------ query filters (D6)

VALID_KINDS = frozenset(
    {"doc", "pdf", "code", "image", "audio", "video", "webpage"}
)

_FILTER_RE = re.compile(r"^(kind|under|after|before|site):(.*)$", re.IGNORECASE)
_PHRASE_RE = re.compile(r'"([^"]*)"')
_RELATIVE_RE = re.compile(r"^(\d+)([dwm])$")

#: C10 date coalesce: content_date wins, urls fall back to their fetched_at,
#: files to mtime, undated rows to epoch 0 (NULL-safe on every branch).
_DATE_EXPR = (
    "COALESCE(content_date,"
    " CASE WHEN source = 'url' THEN json_extract(info, '$.fetched_at') END,"
    " mtime, 0)"
)


@dataclass
class QueryFilters:
    """Parsed out of the query string before scoring (§5.7): hard filters,
    applied pre-fusion at the _allowed_files seam. `echo` carries the values
    as typed (for --json filters_applied); `phrases` are quoted exact phrases
    that join the FTS pattern list."""

    kinds: set[str] = field(default_factory=set)
    under: list[str] = field(default_factory=list)
    after: float | None = None        # epoch seconds, inclusive lower bound
    before: float | None = None       # epoch seconds, inclusive upper bound
    site: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    echo: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(
            self.kinds or self.under or self.after is not None
            or self.before is not None or self.site or self.phrases
        )


def _date_to_epoch(value: str) -> float | None:
    """'YYYY-MM-DD' (local midnight) or Nd/Nw/Nm back from today; None when
    the value is not a date (the token stays a literal search term)."""
    v = value.strip()
    m = _RELATIVE_RE.match(v.lower())
    if m:
        try:
            n, unit = int(m.group(1)), m.group(2)
            today = datetime.date.today()
            if unit == "d":
                target = today - datetime.timedelta(days=n)
            elif unit == "w":
                target = today - datetime.timedelta(weeks=n)
            else:  # constant-time calendar arithmetic, including hostile inputs
                year, month0 = divmod(today.year * 12 + today.month - 1 - n, 12)
                if not 1 <= year <= 9999:
                    return None
                month = month0 + 1
                target = datetime.date(
                    year, month, min(today.day, calendar.monthrange(year, month)[1]),
                )
            return datetime.datetime(target.year, target.month, target.day).timestamp()
        except (OverflowError, ValueError):
            return None
    try:
        d = datetime.datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        return None
    return d.timestamp()


def parse_filters(query: str) -> tuple[str, QueryFilters]:
    """Split `query` into (clean_query, filters).

    Grammar (§5.7): kind:pdf|doc · under:PATH (~ expanded) · after:/before:
    YYYY-MM-DD|Nd|Nw|Nm · site:HOST · "exact phrase". Prefixes are
    case-insensitive and may appear anywhere; an unknown prefix (or a known
    prefix with a value that does not parse) stays in the query as LITERAL
    text — filters never error."""
    flt = QueryFilters()
    phrases = [p for p in _PHRASE_RE.findall(query) if p.strip()]
    remainder = _PHRASE_RE.sub(" ", query)
    kept: list[str] = []
    for token in remainder.split():
        m = _FILTER_RE.match(token)
        if m is not None:
            key, value = m.group(1).lower(), m.group(2)
            if key == "kind":
                kinds = {k.lower() for k in value.split("|") if k.strip()}
                if kinds and kinds <= VALID_KINDS:
                    flt.kinds |= kinds
                    flt.echo.setdefault("kind", []).extend(sorted(kinds))
                    continue
            elif key == "under":
                expanded = os.path.expanduser(value)
                if expanded:
                    flt.under.append(expanded)
                    flt.echo.setdefault("under", []).append(value)
                    continue
            elif key in ("after", "before"):
                ts = _date_to_epoch(value)
                if ts is not None:
                    setattr(flt, key, ts)
                    flt.echo[key] = value
                    continue
            elif key == "site":
                if value.strip():
                    flt.site.append(value.strip().lower())
                    flt.echo.setdefault("site", []).append(value.strip())
                    continue
        kept.append(token)
    for p in phrases:
        flt.phrases.append(p)
        flt.echo.setdefault("phrases", []).append(p)
    return " ".join(kept), flt


@dataclass
class Hit:
    rank: int
    score: float
    z: float | None
    cosine: float | None
    coverage: float
    kind: str
    path: str
    loc: float | None
    loc_end: float | None
    segment: str
    excerpt: str | None
    file_status: str | None
    scorer: str
    # -- v2 item 15 presentation additions (all optional; the stored excerpt
    #    above is the search-API contract and stays unchanged) --
    segment_id: int | None = None      # fts row for snippet text fetch
    excerpt_marked: str | None = None   # «»-marked density-window snippet
    duplicate_of: int | None = None     # rank of the hit this near-duplicates
    bm25_rank: int | None = None        # 1-based best bm25 rank (None = no match)
    why: dict | None = None             # score decomposition (--why renders it)
    supporting_segment_ids: list[int] = field(default_factory=list)
    evidence_type: str | None = None
    media_evidence_type: str | None = None
    query_ids: list[str] = field(default_factory=list)
    rerank_score: float | None = None
    admission_reason: str | None = None


def _terms_of(text: str) -> list[str]:
    normalized = normalize_lexical(text)
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_SPLIT.split(normalized):
        if len(tok) >= 2 and tok not in STOPWORDS and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


class Searcher:
    """Candidate generation and fusion. No DB writes, no backend lifecycle."""

    def __init__(self, store: Store, backend: "EmbeddingBackend | None", *, admission_parameters: AdmissionParameters | None = None):
        self.store = store
        self.backend = backend
        self.admission_parameters = admission_parameters or AdmissionParameters()

    # ------------------------------------------------------------ public API

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        scorer: str = "hybrid",
        z_gate: float = 3.0,
        kinds: set[str] | None = None,
        filters: QueryFilters | None = None,
        snippet_width: int | None = 240,
        why: bool = False,
        diagnostics: dict | None = None,
        obligation_sink: list | None = None,
    ) -> list[Hit]:
        """`query` is the CLEAN query when `filters` is given (parse_filters
        already removed the filter tokens; callers pass the pair through).
        `snippet_width` controls the render-time «» snippet decoration of the
        final hits (None disables it); the stored excerpt is never touched.
        `why=True` attaches the per-hit score decomposition (`--why`)."""
        snapshot = nullcontext() if self.store.db.in_transaction else self.store.read_snapshot()
        try:
            with snapshot:
                return self._search(query, limit=limit, scorer=scorer, z_gate=z_gate, kinds=kinds,
                                    filters=filters, snippet_width=snippet_width, why=why,
                                    diagnostics=diagnostics, obligation_sink=obligation_sink)
        except sqlite3.Error:
            check_deadline()  # SQLite interruption must retain RequestTimeout.
            raise

    def _search(self, query, *, limit, scorer, z_gate, kinds, filters, snippet_width, why,
                diagnostics, obligation_sink):
        if scorer not in ("hybrid", "lexical", "semantic", "rrf"):
            raise ValueError(f"unknown scorer '{scorer}'")
        check_deadline()
        if diagnostics is not None:
            diagnostics.clear()
        if limit == 0:
            return []
        terms = self.terms(query)
        semantic_text = query
        if filters is not None and filters.phrases:
            semantic_text = (query + " " + " ".join(filters.phrases)).strip()

        want_semantic = (
            scorer in ("hybrid", "semantic", "rrf")
            and self.backend is not None
            and bool(semantic_text)
        )
        want_lexical = scorer in ("hybrid", "lexical", "rrf")

        # -- words ----------------------------------------------------------
        word_rank: dict[int, int] = {}        # file -> 1-based rank, best first
        keyword_segment: dict[int, int] = {}  # file -> best (first) matching segment
        coverage: dict[int, float] = {}
        lexical = None
        if want_lexical:
            lexical = collect_lexical(
                self.store, query, terms, filters.phrases if filters is not None else [], self.terms,
            )
            word_rank = lexical.word_rank
            keyword_segment = lexical.keyword_segment
            coverage = lexical.coverage

        allowed = self._allowed_files(kinds, filters)
        phrases = filters.phrases if filters is not None else []
        anchor = resolve_obligations(
            self.store, query, phrases, allowed, self.terms,
            inspected=lexical.segment_files if lexical is not None else (),
        )
        if diagnostics is not None:
            diagnostics.update(anchor.diagnostics())
        if obligation_sink is not None:
            obligation_sink.append((query, anchor))
        if anchor.reason:
            return []
        if anchor.constrained:
            allowed &= anchor.allowed
        if lexical is not None:
            lexical.exact = anchor.exact
            witness_rows = {(b.segment_id, b.file_id) for b in anchor.bindings if b.field == "body"}
            witness_rows.update((sid, fid) for (_, fid), proof in anchor.witnesses.items()
                                for _, _, sid in proof)
            add_verified_witnesses(self.store, lexical, query, phrases, terms, sorted(witness_rows))

        # -- meaning ----------------------------------------------------------
        best: dict[int, tuple[float, float, int]] = {}  # file -> (z, cosine, seg_id)
        z_by_file: dict[int, float] = {}
        table = None
        scores = None
        fids_arr: "np.ndarray | None" = None
        zvals: "np.ndarray | None" = None
        margins: dict[int, float | None] = {}
        if want_semantic:
            table = self.store.load_vectors()
            if len(table) > 0:
                check_deadline()
                qvec = self.backend.embed_texts([query_prompt(semantic_text)])
                check_deadline()
                q = np.asarray(qvec[0], dtype=np.float32)
                scores = table.matrix @ q
                zvals = self.zscores(scores, np.asarray(table.modalities))
                # vectorized per-file aggregation (04 §5 companion fix:
                # ~106ms of Python @500k -> ~2ms numpy); semantics are pinned
                # by test_vectorized_aggregation_matches_python_loop
                fids_arr = np.asarray(table.file_ids, dtype=np.int64)
                best, z_by_file = self._aggregate_by_file(
                    fids_arr,
                    zvals,
                    scores,
                    np.asarray(table.ids, dtype=np.int64),
                )

        # -- candidates + fusion ------------------------------------------------
        if self.admission_parameters.semantic_segment_margin is not None and fids_arr is not None:
            margins = self._segment_margins(fids_arr, zvals, files=allowed)
        reasons = admit_files(
            scorer=scorer, z_by_file=z_by_file, coverage=coverage, word_rank=word_rank,
            exact=lexical.exact if lexical is not None else {},
            rejected_exact=lexical.rejected_exact if lexical is not None else set(),
            z_gate=z_gate, parameters=self.admission_parameters, segment_margins=margins,
        )
        candidates = set(reasons) & allowed
        if not candidates:
            return []

        lookup = self.is_lookup(terms)
        w_m = _LOOKUP_MEANING_WEIGHT if lookup else _MEANING_WEIGHT
        w_c = _LOOKUP_WORDS_WEIGHT if lookup else _WORDS_WEIGHT
        hints = self.kind_hints(query)
        kind_by_file = self._kinds_of(candidates)

        if scorer == "lexical":
            ordered = sorted(candidates, key=lambda f: word_rank.get(f, 10**9))
            scored = {f: w_c * (coverage.get(f, 0.0) ** 1.5) for f in ordered}
        elif scorer == "semantic":
            scored = {
                f: max(z_by_file.get(f, 0.0), 0.0) for f in candidates
            }
            ordered = sorted(candidates, key=lambda f: (-scored[f], f))
        elif scorer == "rrf":
            semantic_rank = [
                f for f in sorted(z_by_file, key=lambda f: (-z_by_file[f], f))
                if f in candidates
            ]
            lexical_rank = [f for f in sorted(candidates, key=lambda f: word_rank.get(f, 10**9))
                            if f in word_rank]
            fused = self.rrf(semantic_rank, lexical_rank)
            scored = {f: fused.get(f, 0.0) for f in candidates}
            ordered = sorted(candidates, key=lambda f: (-scored[f], f))
        else:
            scored = {}
            for f in candidates:
                z = z_by_file.get(f)
                score = w_m * max(z, 0.0) if z is not None else 0.0
                score += w_c * (coverage.get(f, 0.0) ** 1.5)
                if kind_by_file.get(f) in hints:
                    score += 1.0  # flat kind hint, capped, never stacks
                scored[f] = score
            ordered = sorted(candidates, key=lambda f: (-scored[f], f))

        if lexical is not None and lexical.exact:
            original_order = {fid:pos for pos,fid in enumerate(ordered)}
            ordered.sort(key=lambda fid: (lexical.exact[fid].level if fid in lexical.exact else 3, original_order[fid], fid))

        # -- why margins (only the DISPLAYED hits need best-vs-next z; the
        # v2.0 code re-aggregated all rows for at most `limit` consumers)
        if why and want_semantic and fids_arr is not None:
            margins = self._segment_margins(fids_arr, zvals, files=set(ordered[:limit]))

        hits: list[Hit] = []
        selected_files: set[int] = set()
        evidence_vectors = semantic_segments(table, zvals, scores, set(ordered[:limit]))
        for fid in ordered[:limit]:
            z = z_by_file.get(fid)
            cosine = None
            meaning_seg = None
            if fid in best:
                cosine = best[fid][1]
                meaning_seg = best[fid][2]
            if scorer == "hybrid":
                # meaning segment when it stands out, else the keyword segment
                use_meaning = (z is not None and z >= z_gate) or fid not in keyword_segment
            elif scorer == "semantic":
                use_meaning = True
            else:
                use_meaning = fid not in keyword_segment
            seg_id = meaning_seg if use_meaning else keyword_segment.get(fid)
            if lexical is not None and fid in lexical.exact:
                seg_id = lexical.exact[fid].segment_id
            if seg_id is None:
                continue
            rec = self.store.get_file(fid)
            if rec is None:
                continue
            evidence = select_evidence(
                self.store, rec, seg_id, lexical, evidence_vectors.get(fid, []),
                semantic_only=scorer == "semantic", z_gate=z_gate, query_text=semantic_text,
            )
            if evidence is None:
                continue
            seg_id = evidence.primary
            seg_kind, loc, loc_end, seg_excerpt = self.store.segment_row(seg_id)
            hit = Hit(
                rank=len(hits) + 1,
                score=scored[fid],
                z=z,
                cosine=cosine,
                coverage=coverage.get(fid, 0.0),
                kind=rec.kind,
                path=rec.path,
                loc=loc,
                loc_end=loc_end,
                segment=seg_kind,
                excerpt=seg_excerpt,
                file_status=rec.capability,
                scorer=scorer,
                segment_id=seg_id,
                bm25_rank=word_rank.get(fid),
                supporting_segment_ids=list(evidence.supporting),
                evidence_type=evidence.evidence_type,
                media_evidence_type=evidence.media_evidence_type,
                admission_reason=reasons[fid],
            )
            if why:
                hit.why = self._why_of(
                    hit,
                    scorer=scorer,
                    w_m=w_m,
                    w_c=w_c,
                    lookup=lookup,
                    z_gate=z_gate,
                    hints=hints,
                    margin=margins.get(fid),
                )
                hit.why.update(admission_reason=hit.admission_reason, search_behavior_version=SEARCH_BEHAVIOR_VERSION)
                if anchor.constrained:
                    hit.why["anchor_contract"] = anchor.why(fid)
                local_units = lexical.segment_units.get(seg_id, frozenset()) if lexical is not None else frozenset()
                local_vector = next(((sz, sc) for sid, sz, sc in evidence_vectors.get(fid, []) if sid == seg_id), None)
                hit.why["display_evidence"] = {
                    "segment_id": seg_id, "evidence_type": hit.evidence_type,
                    "media_evidence_type": hit.media_evidence_type,
                    "body_query_units": len(local_units),
                    "query_units": lexical.unit_count if lexical is not None else 0,
                    "segment_z": local_vector[0] if local_vector is not None else None,
                    "segment_cosine": local_vector[1] if local_vector is not None else None,
                    "score_scope": "file",
                }
                if lexical is not None and fid in lexical.exact:
                    exact = lexical.exact[fid]
                    hit.why.update({
                        "exact_source": exact.source, "exact_match_type": exact.match_type,
                        "exact_string": exact.raw_term, "exact_priority": exact.level,
                        "promotion_reason": "verified exact source with supported remaining query",
                    })
            hits.append(hit)
            selected_files.add(fid)

        if not anchor.represented(selected_files):
            if diagnostics is not None:
                diagnostics["reason"] = "anchor_not_in_selected_limit"
            return []

        # -- presentation (item 15): near-dup fold bookkeeping, then «» snippets.
        # One segment_text fetch per hit, shared by both passes (F4: the v2.0
        # code fetched the same text twice per hit).
        texts = {
            h.segment_id: (self.store.segment_text(h.segment_id) or "")
            for h in hits if h.segment_id is not None
        }
        self._mark_near_dups(hits, table, texts)
        self._decorate_snippets(
            hits,
            self._mark_terms(terms, filters),
            snippet_width,
            prefix_term=terms[-1] if terms else None,  # FTS prefix-last parity
            texts=texts,
        )
        return hits

    # ---------------------------------------------------------- static parts

    @staticmethod
    def terms(text: str) -> list[str]:
        """Content words: NFC, lowercased alphanumeric tokens, >= 2 chars, deduped,
        stopwords and kind-words dropped (kind-words are stopwords too)."""
        return [t for t in _terms_of(text)]

    @staticmethod
    def kind_hints(text: str) -> set[str]:
        return {
            KIND_WORDS[word]
            for word in _TOKEN_SPLIT.split(unicodedata.normalize("NFC", text).lower())
            if word in KIND_WORDS
        }

    @staticmethod
    def is_lookup(terms: list[str]) -> bool:
        """Codes/IDs ('K7Q2LM', 'TRX50') and long numbers mean nothing to an
        embedding but everything to keywords: weigh words 4x, meaning 0.5x.
        ANY qualifying term fires (v2.1 A/B, 01-search F3): 'LH-2209'
        tokenizes to ['lh', '2209'] — the old every-term rule needed the
        letter-only 'lh' to carry digits too, so mixed code+word queries
        never fired. lookup MRR 0.594 -> 0.609 on the n=146 llama eval, no
        query regressed; combined with the gate raise: s@5 0.747 -> 0.788."""
        if not terms:
            return False
        return any(
            any(ch.isdigit() for ch in t)
            and (any(ch.isalpha() for ch in t) or len(t) >= 4)
            for t in terms
        )

    @staticmethod
    def zscores(scores: "np.ndarray", modalities: "np.ndarray") -> "np.ndarray":
        """Per-modality centering; groups < 20 vectors use the global mean; the
        std has a 0.02 floor (V3 tiny-group fix, DigUp-measured)."""
        scores = np.asarray(scores, dtype=np.float64)
        if scores.size == 0:
            return scores.copy()
        modalities = np.asarray(modalities)
        global_mean = float(scores.mean())
        centered = scores.copy()
        for mod in np.unique(modalities):
            group = scores[modalities == mod]
            mean = float(group.mean()) if group.size >= _TINY_GROUP else global_mean
            centered[modalities == mod] = scores[modalities == mod] - mean
        mean = float(centered.mean())
        std = max(float(centered.std()), _STD_FLOOR)
        return (centered - mean) / std

    @staticmethod
    def _aggregate_by_file(
        file_ids: "np.ndarray",
        zvals: "np.ndarray",
        scores: "np.ndarray",
        seg_ids: "np.ndarray",
    ) -> "tuple[dict[int, tuple[float, float, int]], dict[int, float]]":
        """Vectorized replacement for the per-row Python fusion loop
        (search.py v1:155-160, 106ms @500k measured, 04 §3.1). Returns
        (best, z_by_file) where best[file] = (max z, cosine at the FIRST row
        achieving it, that row's segment id) and z_by_file[file] = max z —
        byte-identical semantics to the loop, including the tie-break: the
        old `z > best[fid][0]` kept the earliest max row, and the stable
        groupby below keeps original row order within each file.

        No row is trimmed before aggregation on purpose: a per-file max can
        sit at ANY global z-rank (one file's lesser rows can monopolize the
        global top-K), so row-level argpartition pre-filtering is unsafe.
        argpartition belongs AFTER exact aggregation, on the small per-file
        max array (see the candidate/top-K step in search() and bench.py).
        """
        if file_ids.size == 0:
            return {}, {}
        order = np.argsort(file_ids, kind="stable")
        fids = file_ids[order]
        z = np.asarray(zvals, dtype=np.float64)[order]
        starts = np.flatnonzero(np.r_[True, fids[1:] != fids[:-1]])
        max_z = np.maximum.reduceat(z, starts)
        # first row per group achieving the group max (tie-break parity)
        sizes = np.diff(np.r_[starts, len(fids)])
        hit_rows = np.flatnonzero(z == np.repeat(max_z, sizes))
        firsts = np.r_[True, fids[hit_rows][1:] != fids[hit_rows][:-1]]
        best_rows = order[hit_rows[firsts]]
        group_fids = fids[starts]
        best = {
            int(f): (float(mz), float(sc), int(si))
            for f, mz, sc, si in zip(
                group_fids, max_z, np.asarray(scores)[best_rows], seg_ids[best_rows]
            )
        }
        z_by_file = {int(f): float(mz) for f, mz in zip(group_fids, max_z)}
        return best, z_by_file

    @staticmethod
    def rrf(rank_a: list[int], rank_b: list[int], k: int = 60) -> dict[int, float]:
        """Reciprocal-rank fusion baseline: sum of 1/(k + position), 1-based."""
        fused: dict[int, float] = {}
        for rank_list in (rank_a, rank_b):
            for position, item in enumerate(rank_list):
                fused[item] = fused.get(item, 0.0) + 1.0 / (k + position + 1)
        return fused

    # ------------------------------------------------------------- internals

    def _allowed_files(
        self, kinds: set[str] | None, filters: QueryFilters | None = None
    ) -> set[int]:
        """The candidate seam (§5.7): kind/under/date/site filters apply HERE,
        pre-fusion, as an intersection with the candidate set — fusion math is
        untouched. `--kinds` and `kind:` intersect (honest: no widening)."""
        # P1-5: a `failed` file's content segments were already dropped at the
        # store layer; exclude the rows here too so no stale row can surface.
        where = ["state = 'done'"]
        params: list[object] = []
        effective = set(kinds) if kinds is not None else None
        if filters is not None and filters.kinds:
            effective = (
                effective & filters.kinds if effective is not None else set(filters.kinds)
            )
        if effective is not None:
            if not effective:
                return set()
            where.append("kind IN (%s)" % ", ".join("?" for _ in effective))
            params.extend(sorted(effective))
        if filters is not None:
            for prefix in filters.under:
                # file paths only; `= OR dir-prefix` so under:/docs cannot
                # match /docs-other (wildcards escaped, literal paths)
                where.append(
                    "(source = 'file' AND (path = ? OR path LIKE ? ESCAPE '\\'))"
                )
                params.append(prefix)
                params.append(_like_escape(prefix) + "/%")
            if filters.after is not None:
                # C10 coalesce: content_date, else url fetched_at, else mtime,
                # else epoch 0 (undated rows cannot pass an `after`)
                where.append(f"{_DATE_EXPR} >= ?")
                params.append(filters.after)
            if filters.before is not None:
                where.append(f"{_DATE_EXPR} <= ?")
                params.append(filters.before)
            for host in filters.site:
                where.append("source = 'url' AND path LIKE '%' || ? || '%'")
                params.append(host)
        rows = self.store.db.execute(
            f"SELECT id FROM files WHERE {' AND '.join(where)}", tuple(params)
        ).fetchall()
        return {r[0] for r in rows}

    def _kinds_of(self, file_ids: set[int]) -> dict[int, str]:
        if not file_ids:
            return {}
        placeholders = ",".join("?" for _ in file_ids)
        rows = self.store.db.execute(
            f"SELECT id, kind FROM files WHERE id IN ({placeholders})",
            tuple(file_ids),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    # --------------------------------------------- item 15: why / fold / snippet

    def _why_of(
        self,
        hit: Hit,
        *,
        scorer: str,
        w_m: float,
        w_c: float,
        lookup: bool,
        z_gate: float,
        hints: set[str],
        margin: float | None,
    ) -> dict:
        """Per-hit score decomposition (§5.8): the components the scoring loop
        used, compactly. `score_parts` values sum to the hit's score."""
        parts: dict[str, float] = {}
        if scorer in ("hybrid", "semantic") and hit.z is not None:
            parts["z"] = w_m * max(hit.z, 0.0)
        if scorer in ("hybrid", "lexical"):
            parts["cov"] = w_c * (hit.coverage ** 1.5)
        kind_boost = 1.0 if scorer == "hybrid" and hit.kind in hints else 0.0
        if kind_boost:
            parts["kind"] = kind_boost
        if scorer == "rrf":
            parts["rrf"] = hit.score
        return {
            "w_m": w_m if scorer in ("hybrid", "semantic") else None,
            "w_c": w_c if scorer in ("hybrid", "lexical") else None,
            "z_gate": z_gate,
            "lookup": bool(lookup),
            "z": hit.z,
            "cosine": hit.cosine,
            "coverage": hit.coverage,
            "bm25_rank": hit.bm25_rank,
            "kind_hint": sorted(hints),
            "kind_boost": kind_boost,
            "score_parts": parts,
            "score": hit.score,
            "segment_margin_z": margin,
        }

    @staticmethod
    def _mark_terms(terms: list[str], filters: QueryFilters | None) -> list[str]:
        """Words the «» snippet marks: the FTS query terms plus, when filters
        are present, the words of any quoted phrase. A phrase whose words the
        term splitter drops entirely (e.g. a CJK phrase — FTS still indexes
        it) is marked as the raw phrase text."""
        out = list(terms)
        if filters is not None:
            for phrase in filters.phrases:
                words = Searcher.terms(phrase)
                if not words and phrase.strip():
                    words = [phrase.strip()]
                for t in words:
                    if t not in out:
                        out.append(t)
        return out

    def _mark_near_dups(
        self, hits: list[Hit], table, texts: dict[int, str] | None = None
    ) -> None:
        """Cross-file near-duplicate fold (item 15.2a): a hit whose segment
        text is identical (sha256) or whose segment vector sits within
        _NEAR_DUP_COSINE of a HIGHER-RANKED kept hit folds under it
        (`duplicate_of`). Bookkeeping only — order, ranks and the hit list
        itself are unchanged; the renderers decide how to display the fold.
        `texts` maps segment_id -> text when the caller already fetched it
        (search() shares one fetch between this and snippet decoration)."""
        if len(hits) < 2:
            return
        shas: dict[int, str] = {}
        for h in hits:
            if texts is not None:
                text = texts.get(h.segment_id, "")
            elif h.segment_id is not None:
                text = self.store.segment_text(h.segment_id) or ""
            else:
                text = ""
            shas[h.rank] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        vecs = self._hit_vectors(table, [h.segment_id for h in hits])
        kept: list[Hit] = []
        for h in hits:
            for k in kept:
                if shas[h.rank] and shas[h.rank] == shas[k.rank]:
                    h.duplicate_of = k.rank
                    break
                cos = _pair_cosine(vecs.get(h.segment_id), vecs.get(k.segment_id))
                if cos is not None and cos >= _NEAR_DUP_COSINE:
                    h.duplicate_of = k.rank
                    break
            if h.duplicate_of is None:
                kept.append(h)

    @staticmethod
    def _hit_vectors(table, seg_ids: list[int | None]) -> dict[int, "np.ndarray"]:
        """{segment_id: unit vector} for the hit segments only — the vector
        table is id-ordered, so a searchsorted lookup; no full dict build."""
        if table is None or len(table) == 0:
            return {}
        ids = np.asarray(table.ids, dtype=np.int64)
        matrix = table.matrix
        if not bool(np.all(ids[1:] >= ids[:-1])):  # defensive: ORDER BY id normally
            order = np.argsort(ids, kind="stable")
            ids = ids[order]
            matrix = matrix[order]
        out: dict[int, np.ndarray] = {}
        for sid in {s for s in seg_ids if s is not None}:
            i = int(np.searchsorted(ids, sid))
            if i < len(ids) and int(ids[i]) == sid:
                v = np.asarray(matrix[i], dtype=np.float32)
                norm = float(np.linalg.norm(v))
                if norm > 0.0:
                    out[sid] = v / norm
        return out

    def _decorate_snippets(
        self,
        hits: list[Hit],
        terms: list[str],
        width: int | None,
        prefix_term: str | None = None,
        texts: dict[int, str] | None = None,
    ) -> None:
        """«»-marked term-density snippets (§5.6, item 15.1): fetch the hit
        segment's text as written (`fts.original`/`body`, ≤ limit rows) and
        mark query terms. Presentation only — the stored excerpt (the search
        API contract) is never modified; a missing body or a no-term-match
        (pure-semantic hit) leaves the stored excerpt in place. `texts` is the
        shared per-hit text fetch from search() (one fetch, two consumers)."""
        if width is None or width <= 0 or not terms:
            return
        for h in hits:
            if h.segment_id is None:
                continue
            text = (
                (texts or {}).get(h.segment_id)
                or self.store.segment_text(h.segment_id)
            )
            if not text:
                continue
            h.excerpt_marked = make_snippet(
                text, terms, width=width, prefix_term=prefix_term
            )

    @staticmethod
    def _segment_margins(
        file_ids: "np.ndarray",
        zvals: "np.ndarray",
        files: "set[int] | None" = None,
    ) -> "dict[int, float | None]":
        """file -> best z minus next-best z across its segments (§5.8
        `segment_margin_z`: a margin ≈ 0 means the file-level hit is spread
        thin across pages/chunks). None when the file has a single vector row.
        `files` restricts the computation to those file ids (the displayed
        hits) — margins for rows nobody renders are not worth aggregating.
        Same stable-groupby shape as _aggregate_by_file: mask ONE instance of
        the per-file max, re-reduce."""
        if file_ids.size == 0:
            return {}
        if files is not None and not files:
            return {}
        order = np.argsort(file_ids, kind="stable")
        fids = file_ids[order]
        z = np.asarray(zvals, dtype=np.float64)[order]
        if files is not None:
            keep = np.fromiter((int(f) in files for f in fids), dtype=bool, count=len(fids))
            if not keep.any():
                return {}
            fids = fids[keep]
            z = z[keep]
        starts = np.flatnonzero(np.r_[True, fids[1:] != fids[:-1]])
        max_z = np.maximum.reduceat(z, starts)
        sizes = np.diff(np.r_[starts, len(fids)])
        hit_rows = np.flatnonzero(z == np.repeat(max_z, sizes))
        firsts = np.r_[True, fids[hit_rows][1:] != fids[hit_rows][:-1]]
        best_rows = hit_rows[firsts]
        z2 = z.copy()
        z2[best_rows] = -np.inf
        max2 = np.maximum.reduceat(z2, starts)
        out: dict[int, float | None] = {}
        for f, mz, m2 in zip(fids[starts], max_z, max2):
            out[int(f)] = float(mz - m2) if np.isfinite(m2) else None
        return out
