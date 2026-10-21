"""Choose stored evidence within a file; file admission and ranking stay upstream.

Name fields never prove a body location. At most three content segments survive
selection, and supporting segments must add query evidence to the primary one.
All text comes from the request's Store snapshot, never from source files.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .deadline import check_deadline
from .evidence import segment_evidence_types
from .lexical import normalize


def collect_body_units(db, units, rows):
    """Verify bounded recall rows against *body*, excluding copied FTS.name."""
    segment_files = dict(rows)
    matched: dict[int, set[int]] = {}
    ids = list(segment_files)
    cjk_units = [(i, unit) for i, unit in enumerate(units) if unit.patterns and unit.kind != "latin"]
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        marks = ",".join("?" for _ in batch)
        for i, unit in enumerate(units):
            check_deadline()
            if unit.kind != "latin" and unit.patterns:
                continue
            # FTS's token/phrase/prefix semantics are authoritative for Latin.
            pattern = unit.patterns[0] if unit.patterns else '"' + unit.text.replace('"', '""') + '"'
            found = db.execute(
                f"SELECT rowid FROM fts WHERE fts MATCH ? AND rowid IN ({marks})",
                ("body : (" + pattern + ")", *batch),
            )
            for (sid,) in found:
                matched.setdefault(sid, set()).add(i)
        if cjk_units:
            found = db.execute(
                f"SELECT rowid,coalesce(original,body) FROM fts WHERE rowid IN ({marks})", batch,
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


def select_evidence(store, file, fallback, lexical, semantic, *, semantic_only=False, z_gate=3.0,
                    query_text=None):
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
    rows = store.db.execute(
        "SELECT s.id,s.kind,s.modality,s.loc,s.loc_end,coalesce(f.original,f.body) "
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
    if semantic_only:
        primary = fallback
    elif body_exact or pure_metadata:
        primary = exact.segment_id
    elif semantic_order:
        primary = semantic_order[0]
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
    return EvidenceSelection(primary, tuple(supporting), evidence_type, media_type)
