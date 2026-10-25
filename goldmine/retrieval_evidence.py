"""Choose stored evidence within a file; file admission and ranking stay upstream.

Name fields never prove a body location. At most three content segments survive
selection, and supporting segments must add query evidence to the primary one.
All text comes from the request's Store snapshot, never from source files.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re

import numpy as np

from .deadline import check_deadline
from .read_source import fts_projection
from .evidence import segment_evidence_types
from .lexical import normalize

#: search.v7 window dominance (evidence-window-witness-001): the semantic
#: primary yields only to a candidate that concentrates at least WINDOW_MARGIN
#: more distinct query units inside one WINDOW_CHARS-character window of its
#: stored text while carrying at least as many body units. The matching rule
#: below is frozen by the preregistration; FTS/stem positions are not used.
#:
#: span-window-v2-001 tiers the takeover: a WINDOW_MARGIN lead still swaps
#: unconditionally; a margin-1 lead swaps only between two semantically
#: qualified windows whose z-scores stay within WINDOW_Z_PARITY of each other
#: (a lexical-only or far-weaker window never wins on one extra unit).
WINDOW_CHARS = 200
WINDOW_MARGIN = 2
WINDOW_Z_PARITY = 1.5
WINDOW_POSITION_CAP = 64

#: rank-window-001 (all-gates-program-001 Step C): within one admitted file,
#: a cross-encoder may re-decide the PLAIN semantic ladder's window among
#: lexically co-qualified candidates (unit count at least the primary's).
#: Takeover needs a score lead of RERANK_TAKEOVER_MARGIN; tier-1/2 window
#: verdicts are never overturned (division of labor with the window rules).
RERANK_TAKEOVER_MARGIN = 0.01
RERANK_POOL_CAP = 3
RERANK_POOL_DOC_SECONDS = 0.22
RERANK_HEADROOM_SECONDS = 2.0
_LATIN_TOKEN = re.compile(r"[0-9a-z]+")


def window_units(text, units):
    """Most distinct query units whose occurrences fit in one WINDOW_CHARS window.

    Latin units match whole [0-9a-z]+ tokens of the normalized text (the last
    latin unit also by prefix, mirroring its FTS prefix pattern); CJK units match
    any of their grams and phrase units their text by substring. At most
    WINDOW_POSITION_CAP positions per unit are considered.
    """
    if not text or not units:
        return 0
    body = normalize(text)
    tokens = None
    positions = []
    for index, unit in enumerate(units):
        found = []
        if unit.kind == "latin":
            if tokens is None:
                tokens = [(m.start(), m.group(0)) for m in _LATIN_TOKEN.finditer(body)]
            prefix = bool(unit.patterns) and unit.patterns[0].endswith("*")
            for start, token in tokens:
                if token == unit.text or (prefix and token.startswith(unit.text)):
                    found.append(start)
                    if len(found) >= WINDOW_POSITION_CAP:
                        break
        else:
            needles = ([p[1:-1].replace('""', '"') for p in unit.patterns]
                       if unit.kind == "cjk" and unit.patterns else [unit.text])
            for needle in needles:
                at = body.find(needle) if needle else -1
                while at >= 0 and len(found) < WINDOW_POSITION_CAP:
                    found.append(at)
                    at = body.find(needle, at + 1)
                if len(found) >= WINDOW_POSITION_CAP:
                    break
        positions.extend((offset, index) for offset in found)
    if not positions:
        return 0
    positions.sort()
    best, start, inside = 0, 0, {}
    for end, (offset, index) in enumerate(positions):
        inside[index] = inside.get(index, 0) + 1
        while offset - positions[start][0] >= WINDOW_CHARS:
            first = positions[start][1]
            inside[first] -= 1
            if not inside[first]:
                del inside[first]
            start += 1
        best = max(best, len(inside))
    return best


def collect_body_units(db, units, rows):
    """Verify bounded recall rows against *body*, excluding copied FTS.name.

    Each Latin unit's body MATCH runs once per query, unfiltered by the
    recalled rowids; the posting set is intersected in Python. Asserting the
    MATCH per 400-rowid batch re-evaluated the phrase for every candidate
    rowid and multiplied statement cost ~100x on long public queries.
    """
    segment_files = dict(rows)
    matched: dict[int, set[int]] = {}
    ids = set(segment_files)
    cjk_units = [(i, unit) for i, unit in enumerate(units) if unit.patterns and unit.kind != "latin"]
    for i, unit in enumerate(units):
        check_deadline()
        if unit.kind != "latin" and unit.patterns:
            continue
        # FTS's token/phrase/prefix semantics are authoritative for Latin.
        pattern = unit.patterns[0] if unit.patterns else '"' + unit.text.replace('"', '""') + '"'
        found = db.execute(
            "SELECT rowid FROM fts WHERE fts MATCH ?", ("body : (" + pattern + ")",))
        hits = {sid for (sid,) in found}
        for sid in hits & ids:
            matched.setdefault(sid, set()).add(i)
    if cjk_units and ids:
        text_sql, _ = fts_projection(db)
        ordered = sorted(ids)
        for start in range(0, len(ordered), 400):
            batch = ordered[start:start + 400]
            check_deadline()
            marks = ",".join("?" for _ in batch)
            found = db.execute(
                f"SELECT rowid,{text_sql} FROM fts WHERE rowid IN ({marks})", batch,
            )
            for sid, text in found:
                normalized = normalize(text or "")
                for i, unit in cjk_units:
                    if unit.kind == "phrase":
                        present = unit.text in normalized
                    else:
                        # A run contributes one identity, even for many grams.
                        grams = [p[1:-1].replace('""', '"') for p in unit.patterns]
                        present = any(gram in normalized for gram in grams)
                    if present:
                        matched.setdefault(sid, set()).add(i)
    return {sid: frozenset(value) for sid, value in matched.items()}, segment_files


def semantic_segments(table, zvals, scores, file_ids):
    """Top three vector rows per displayed file, with stable segment tie breaks."""
    if table is None or zvals is None or not file_ids:
        return {}
    positions = np.flatnonzero(np.isin(table.file_ids, list(file_ids)))
    if not positions.size:
        return {}
    ids = np.asarray(table.ids)
    files = np.asarray(table.file_ids)
    order = positions[np.lexsort((ids[positions], -zvals[positions], files[positions]))]
    grouped = files[order]
    starts = np.flatnonzero(np.r_[True, grouped[1:] != grouped[:-1]])
    ends = np.r_[starts[1:], len(order)]
    # Offsets must not cross into the next file's group.
    top = np.unique(np.concatenate([starts[starts + offset < ends] + offset for offset in range(3)]))
    out = {}
    for pos in order[top]:
        fid = int(files[pos])
        out.setdefault(fid, []).append((int(ids[pos]), float(zvals[pos]), float(scores[pos])))
    return out


@dataclass(frozen=True)
class EvidenceSelection:
    primary: int
    supporting: tuple[int, ...] = ()
    evidence_type: str = "content"
    media_evidence_type: str | None = None
    #: search.v7 disclosure: densest-window unit count of the primary and, when
    #: window dominance replaced the semantic top, the segment it displaced.
    window_units: int | None = None
    demoted_semantic_primary: int | None = None
    #: rank-window-001 disclosure: when the cross-encoder replaced the plain
    #: semantic ladder's window, the segment it displaced and both scores.
    rank_demoted: int | None = None
    rank_scores: tuple[float, float] | None = None


_SENT_SPLIT = re.compile(r"(?<=[。！？.!?\n])\s*")


def _sentence_hits(text, units):
    """(max, sum) of distinct query units per sentence — the concentration
    prior that orders (never decides) the rank pool."""
    body = normalize(text)
    if not body or not units:
        return (0, 0)
    hits = []
    for sent in _SENT_SPLIT.split(body):
        if not sent.strip():
            continue
        sent_tokens = {m.group(0) for m in _LATIN_TOKEN.finditer(sent)}
        count = 0
        for unit in units:
            if unit.kind == "latin":
                prefix = bool(unit.patterns) and unit.patterns[0].endswith("*")
                if any(tok == unit.text or (prefix and tok.startswith(unit.text))
                       for tok in sent_tokens):
                    count += 1
            else:
                needles = ([p[1:-1].replace('""', '"') for p in unit.patterns]
                           if unit.kind == "cjk" and unit.patterns else [unit.text])
                if any(n and n in sent for n in needles):
                    count += 1
        hits.append(count)
    return (max(hits) if hits else 0, sum(hits))


def _rank_pool_cap():
    """Docs the challenge may afford from the remaining request deadline."""
    from .deadline import current_deadline
    deadline = current_deadline()
    if deadline is None:
        return RERANK_POOL_CAP
    affordable = int((deadline.remaining() - RERANK_HEADROOM_SECONDS) / RERANK_POOL_DOC_SECONDS)
    return min(RERANK_POOL_CAP, affordable)


def _rank_challenge(store, file, primary, content, by_id, units, windows, semantic_z,
                    query_text, ranker, budget, lexical_units):
    """Cross-encoder arbitration of the plain semantic ladder's window.

    Returns (new_primary, demoted, scores) or (primary, None, None) when the
    challenge does not fire or the rank head is unavailable/sick. Fires only
    under a per-request file budget; never overturns a window-rule verdict
    (the caller passes primaries whose demoted state is None)."""
    if ranker is None or not query_text:
        return primary, None, None
    if budget is not None and budget[0] <= 0:
        return primary, None, None
    if not units.get(primary):
        return primary, None, None
    pool = [row[0] for row in content
            if row[0] != primary and len(units.get(row[0], ())) >= len(units.get(primary, ()))]
    concentration = {sid: _sentence_hits(by_id[sid][5], lexical_units) for sid in pool}
    pool.sort(key=lambda sid: (-len(units.get(sid, ())), -concentration[sid][1],
                               -windows.get(sid, 0), sid))
    pool = pool[:_rank_pool_cap()]
    if not pool:
        return primary, None, None
    docs = [by_id[primary][5]] + [by_id[sid][5] for sid in pool]
    try:
        scores, _meta = ranker.rank(query_text, docs)
    except Exception:
        return primary, None, None
    if budget is not None:
        budget[0] -= 1
    best = max(range(len(scores)), key=lambda i: scores[i])
    if best != 0 and scores[best] >= scores[0] + RERANK_TAKEOVER_MARGIN:
        return pool[best - 1], primary, (scores[0], scores[best])
    return primary, None, None


def select_evidence(store, file, fallback, lexical, semantic, *, semantic_only=False, z_gate=3.0,
                    query_text=None, ranker=None, rank_budget=None):
    """Select the primary pointer and complementary real-text supporting IDs."""
    units = lexical.segment_units if lexical is not None else {}
    exact = lexical.exact.get(file.id) if lexical is not None else None
    candidate_ids = {sid for sid in units if lexical.segment_files[sid] == file.id}
    candidate_ids.update(sid for sid, _, _ in semantic)
    if fallback is not None:
        candidate_ids.add(fallback)
    if exact is not None:
        candidate_ids.add(exact.segment_id)
    name = store.db.execute("SELECT id FROM segments WHERE file=? AND kind='name' ORDER BY id LIMIT 1", (file.id,)).fetchone()
    if name:
        candidate_ids.add(name[0])
    if not candidate_ids:
        return None
    marks = ",".join("?" for _ in candidate_ids)
    text_sql, _ = fts_projection(store.db, "f")
    rows = store.db.execute(
        f"SELECT s.id,s.kind,s.modality,s.loc,s.loc_end,{text_sql} "
        f"FROM segments s LEFT JOIN fts f ON f.rowid=s.id WHERE s.file=? AND s.id IN ({marks})",
        (file.id, *sorted(candidate_ids)),
    ).fetchall()
    by_id = {r[0]: r for r in rows}
    semantic_z = {sid: z for sid, z, _ in semantic}
    content = [r for r in rows if r[1] != "name" and r[5] and r[5].strip()]
    content_ids = {r[0] for r in content}
    content.sort(key=lambda r: (
        -len(units.get(r[0], ())), -semantic_z.get(r[0], float("-inf")), r[0],
    ))
    semantic_order = sorted(
        (sid for sid, z, _ in semantic if sid in content_ids and math.isfinite(z) and z >= z_gate),
        key=lambda sid: (-semantic_z[sid], sid),
    )
    body_exact = exact is not None and exact.segment_id in content_ids and exact.source in ("body", "title")
    # A title lookup is currently attached to the name row. Stored kind,
    # not the lookup source label, determines whether a pointer is content.
    pure_metadata = (exact is not None and exact.segment_id in by_id
                     and by_id[exact.segment_id][1] == "name" and query_text is not None
                     and normalize(query_text.strip()) == normalize(exact.query_term))
    windows, demoted = {}, None
    rank_demoted, rank_scores = None, None
    if semantic_only:
        primary = fallback
    elif body_exact or pure_metadata:
        primary = exact.segment_id
    elif semantic_order:
        primary = semantic_order[0]
        query_units_meta = lexical.units if lexical is not None else []
        if query_units_meta:
            for row in content:
                check_deadline()
                windows[row[0]] = window_units(row[5], query_units_meta)
            challenger = max((row[0] for row in content), key=lambda sid: (
                windows[sid], len(units.get(sid, ())), semantic_z.get(sid, float("-inf")), -sid))
            margin = windows[challenger] - windows[primary]
            if (challenger != primary and len(units.get(challenger, ())) >= len(units.get(primary, ()))
                    and (margin >= WINDOW_MARGIN
                         or (margin == WINDOW_MARGIN - 1
                             and semantic_z.get(challenger, float("-inf")) >= z_gate
                             and semantic_z.get(challenger, float("-inf"))
                             >= semantic_z.get(primary, float("-inf")) - WINDOW_Z_PARITY))):
                demoted, primary = primary, challenger
        # rank-window-001: the cross-encoder re-decides only the PLAIN ladder's
        # window (a tier-1/2 verdict above stands) and only among candidates
        # lexically co-qualified with the primary.
        if demoted is None and primary in content_ids and query_text and ranker is not None:
            new_primary, rank_demoted, rank_scores = _rank_challenge(
                store, file, primary, content, by_id, units, windows, semantic_z,
                query_text, ranker, rank_budget, query_units_meta)
            if rank_demoted is not None:
                primary = new_primary
    elif content and units.get(content[0][0]):
        primary = content[0][0]
    elif fallback in semantic_z and semantic_z[fallback] >= z_gate:
        primary = fallback
    else:
        primary = name[0] if name else fallback
    if primary is None or primary not in by_id:
        return None
    covered = set(units.get(primary, ()))
    supporting = []
    # Pure semantic ablation preserves its historical primary+neighbors only.
    # Protect the hybrid semantic channel even when words duplicate the primary.
    # Candidates are not a claim that their semantic meanings are independent.
    allow_support = not semantic_only and primary in content_ids
    if allow_support:
        other_semantic = next((sid for sid in semantic_order if sid != primary), None)
        if other_semantic is not None:
            supporting.append(other_semantic)
            covered.update(units.get(other_semantic, ()))
    remaining = [r for r in content if r[0] != primary]
    semantic_rank = {sid: rank for rank, sid in enumerate(semantic_order)}
    while allow_support and remaining and len(supporting) < 2:
        remaining = [r for r in remaining if r[0] not in supporting]
        if not remaining:
            break
        row = max(remaining, key=lambda r: (
            len(set(units.get(r[0], ())) - covered),
            -semantic_rank.get(r[0], len(semantic_order)), -r[0],
        ))
        added = set(units.get(row[0], ())) - covered
        if not added:
            next_semantic = next((sid for sid in semantic_order
                                  if sid != primary and sid not in supporting), None)
            if next_semantic is not None:
                supporting.append(next_semantic)
            break
        supporting.append(row[0])
        covered.update(added)
        remaining.remove(row)
    row = by_id[primary]
    evidence_type, media_type = segment_evidence_types(
        row[1], row[2], row[5], file.kind, file.extractor_version, file.info, row[3],
    )
    return EvidenceSelection(primary, tuple(supporting), evidence_type, media_type,
                             window_units=windows.get(primary), demoted_semantic_primary=demoted,
                             rank_demoted=rank_demoted, rank_scores=rank_scores)
