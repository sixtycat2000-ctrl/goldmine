"""Whole stored material: primary seeds, then supports, then neighbors.

Allocation owns an accepted-parts ledger. Rendered records, folds, citations and
numbers are disposable views, rebuilt after every rollback. No source files,
models, ranking, or oracle labels are involved here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

from .deadline import check_deadline
from .pack import (
    BudgetTooSmall, FIXPOINT_ROUNDS, MAX_RECORDS, PackRecord,
    TEXTLESS_NOTE, TEXTLESS_NOTE_SHORT, has_source_gap, material_text, render_pack,
)


@dataclass(frozen=True)
class _Candidate:
    hit: object
    by_id: dict[int, dict]
    seed: tuple[int, ...]
    supports: tuple[int, ...]
    neighbors: tuple[tuple[float, int, int], ...]
    note: str | None
    metadata: str | None = None


def _candidates(store, neighbor, hits, context, *, with_citations):
    out = []
    for hit in hits[:MAX_RECORDS]:
        check_deadline()
        parts, note = neighbor.parts_for(hit, context)
        by_id = {part["segment_id"]: part for part in parts}
        metadata = None
        if hit.segment == "name" and with_citations:
            row = store.db.execute("SELECT path,source,kind,info FROM files WHERE path=?", (hit.path,)).fetchone()
            value = {"path": row[0], "source": row[1], "kind": row[2],
                     "info": json.loads(row[3]) if row[3] else None} if row else {}
            metadata = ("# stored name metadata; no quotable document body\n"
                        + json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False))
            parts, by_id, note = [], {}, "name_metadata"
        seed = tuple(p["segment_id"] for p in parts if p["role"] == "hit")
        supports = tuple(sid for sid in dict.fromkeys(hit.supporting_segment_ids[:2])
                         if sid in by_id and by_id[sid]["role"] == "supporting")
        fid = neighbor.file_id_of(hit.path)
        order = {s["segment_id"]: i for i, s in enumerate(neighbor.segments(fid))} if fid is not None else {}
        primary_position = order.get(hit.segment_id, 0)
        neighbors = []
        for part in parts:
            if part["role"] in ("hit", "supporting"):
                continue
            sid = part["segment_id"]
            distance = (abs(part["loc"] - hit.loc) if hit.kind == "pdf"
                        and part["loc"] is not None and hit.loc is not None
                        else abs(order.get(sid, primary_position) - primary_position))
            neighbors.append((distance, order.get(sid, 0), sid))
        out.append(_Candidate(hit, by_id, seed, supports, tuple(neighbors), note, metadata))
    return out


def _fold(records):
    """Fold only complete actual material with representable source identity."""
    out = []
    duplicates = also = 0
    for record in records:
        check_deadline()
        absorbed = False
        for prior in out:
            represented = {p["segment_id"] for p in prior.parts}
            represented.update(h.segment_id for h in prior.also_hits if h.segment_id is not None)
            # A trimmed seam may list a SID without preserving its full raw
            # literal. Never discard that SID's only complete later record.
            whole_parts = bool(record.parts) and all(p["text"] in prior.text for p in record.parts)
            if (record.hit.segment_id in represented and whole_parts
                    and all(p["segment_id"] in represented for p in record.parts)):
                prior.also_hits.append(record.hit)
                prior.citation_ids.update(record.citation_ids)
                also += 1
                absorbed = True
                break
            primary_only = (len(record.parts) == 1
                            and record.parts[0]["segment_id"] == record.hit.segment_id)
            if (record.hit.segment != "name" and primary_only and record.hit.path != prior.hit.path
                    and record.text == prior.text
                    and material_text(record.parts, record.hit) == material_text(prior.parts, prior.hit)):
                prior.also_hits.append(record.hit)
                prior.citation_ids.update(record.citation_ids)
                duplicates += 1
                absorbed = True
                break
        if not absorbed:
            out.append(record)
    for number, record in enumerate(out, 1):
        record.n = number
    return out, duplicates, also


def allocate_pack(store, neighbor, settings, hits, *, index_line, count, token_mode,
                  citation_id_of=None):
    """Return measured text/records/counts; each accepted addition is undoable."""
    candidates = _candidates(store, neighbor, hits, settings.context,
                             with_citations=citation_id_of is not None)
    accepted: dict[int, list[int]] = {}
    events: list[tuple[int, int | None]] = []
    citation_cache = {}
    capped = len(hits) - len(candidates)

    def citation(sid):
        if sid not in citation_cache:
            check_deadline()
            citation_cache[sid] = citation_id_of(sid)
        return citation_cache[sid]

    def materialize(*, fold):
        records = []
        for index in sorted(accepted):
            check_deadline()
            candidate = candidates[index]
            parts = [candidate.by_id[sid] for sid in accepted[index]]
            parts.sort(key=lambda p: (p["loc"] is None, p["loc"] or 0, p["loc_end"] or 0, p["segment_id"]))
            note = candidate.note
            order = None
            if candidate.metadata is not None:
                text = candidate.metadata
            elif parts:
                # Skip/continue may leave holes even without a supporting role.
                # Include unrenderable stored units in the contiguity proof.
                order = neighbor.source_order(candidate.hit.path)
                text = material_text(parts, candidate.hit, source_order=order)
            else:
                text, note = TEXTLESS_NOTE, TEXTLESS_NOTE_SHORT
            record = PackRecord(len(records) + 1, candidate.hit, parts=parts, text=text, note=note,
                                source_gap=has_source_gap(parts, order))
            if citation_id_of is not None:
                ids = list(dict.fromkeys([p["segment_id"] for p in parts]
                    + ([candidate.hit.segment_id] if candidate.hit.segment_id is not None else [])))
                record.citation_ids = {sid: citation(sid) for sid in ids}
            records.append(record)
        return _fold(records) if fold else (records, 0, 0)

    def measure(*, fold=False):
        records, duplicates, also = materialize(fold=fold)
        skipped = len(candidates) - len(accepted)
        guess = 0
        for _ in range(FIXPOINT_ROUNDS + 1):
            check_deadline()
            text = render_pack(records, settings, index_line=index_line, n_hits=len(hits),
                n_folded=duplicates, n_also=also, n_skipped=skipped, used_tokens=guess,
                token_mode=token_mode, n_capped=capped)
            used = count(text)
            if used == guess:
                return text, used, True, records, duplicates, also
            guess = used
        return text, used, False, records, duplicates, also

    def fits(result):
        return result[2] and result[1] <= settings.budget

    def try_part(index, sid):
        check_deadline()
        accepted[index].append(sid)
        if fits(measure()):
            events.append((index, sid))
            return True
        accepted[index].pop()
        return False

    # Budget every primary before spending tokens on optional expansion.
    for index, candidate in enumerate(candidates):
        check_deadline()
        accepted[index] = list(candidate.seed)
        if fits(measure()):
            events.append((index, None))
        else:
            del accepted[index]

    selected_sids = {sid for ids in accepted.values() for sid in ids}
    for slot in range(2):
        for index in sorted(accepted):
            support = candidates[index].supports
            if slot < len(support) and support[slot] not in selected_sids:
                sid = support[slot]
                if try_part(index, sid):
                    selected_sids.add(sid)

    neighbors = sorted((distance, index, position, sid) for index in accepted
                       for distance, position, sid in candidates[index].neighbors)
    attempted_neighbors = set()
    for _, index, _, sid in neighbors:
        check_deadline()
        if sid in selected_sids or sid in attempted_neighbors:
            continue
        attempted_neighbors.add(sid)
        if try_part(index, sid):
            selected_sids.add(sid)

    # Final folding/renumbering can change the envelope size. Every undo
    # rebuilds all associations from the ledger, never from a folded view.
    final = measure(fold=True)
    while not fits(final) and events:
        index, sid = events.pop()
        if sid is None:
            del accepted[index]
        else:
            accepted[index].remove(sid)
        final = measure(fold=True)
    if not fits(final):
        raise BudgetTooSmall(f"budget_too_small: pack envelope requires {final[1]} tokens; "
                             f"budget is {settings.budget} ({token_mode})")
    text, used, _, records, duplicates, also = final
    skipped = len(candidates) - len(accepted)
    assert len(records) + duplicates + also + skipped + capped == len(hits)
    return text, records, duplicates, also, skipped, capped, used
