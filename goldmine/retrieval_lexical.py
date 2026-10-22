"""Bounded lexical recall and file-level coverage, shared by all search adapters."""

from __future__ import annotations

from .deadline import check_deadline
from .lexical import (
    LexicalResult, fts_quote, normalize, query_units,
)
from .retrieval_evidence import collect_body_units


def _cjk_rows(db, patterns):
    if not patterns:
        return []
    return db.execute(
        "SELECT s.file,s.id,bm25(fts_cjk) FROM fts_cjk JOIN segments s ON s.id=fts_cjk.rowid "
        "WHERE fts_cjk MATCH ? ORDER BY bm25(fts_cjk),s.id LIMIT 1000",
        (" OR ".join(dict.fromkeys(patterns)),),
    ).fetchall()


def _cjk_coverage(db, patterns, file_ids):
    found = {}
    if not patterns:
        return found
    for pattern in patterns:
        check_deadline()
        for start in range(0, len(file_ids), 400):
            batch = file_ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            rows = db.execute(
                "SELECT DISTINCT s.file FROM fts_cjk JOIN segments s ON s.id=fts_cjk.rowid "
                f"WHERE fts_cjk MATCH ? AND s.file IN ({marks})", (pattern, *batch),
            ).fetchall()
            for (fid,) in rows:
                found[fid] = found.get(fid, 0.0) + 1 / len(patterns)
    return found


def _phrase_files(db, phrase, candidate_rows):
    """A phrase cannot be assembled from different segments or scattered grams."""
    found = {}
    for fid, sid, _ in candidate_rows:
        row = db.execute("SELECT name,coalesce(original,body) FROM fts WHERE rowid=?", (sid,)).fetchone()
        if row and any(phrase in normalize(text or "") for text in row):
            found.setdefault(fid, sid)
    return found


def collect_lexical(store, query: str, terms: list[str], phrases: list[str], terms_of) -> LexicalResult:
    units = query_units(query, terms, phrases)
    latin_patterns = [u.patterns[0] for u in units if u.kind == "latin"]
    latin_patterns += [fts_quote(u.text) for u in units if u.kind == "phrase" and not u.patterns]
    latin_rows = store.search_fts(" OR ".join(latin_patterns), limit=1000) if latin_patterns else []
    result = LexicalResult()
    for position, row in enumerate(latin_rows, 1):
        result.word_rank.setdefault(row.file_id, position)
        result.keyword_segment.setdefault(row.file_id, row.segment_id)
    tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    cjk_patterns = [pattern for unit in units if unit.kind in ("cjk", "phrase") for pattern in unit.patterns]
    cjk_rows = _cjk_rows(store.db, cjk_patterns) if "fts_cjk" in tables else []
    result.unit_count = len(units)
    result.segment_units, result.segment_files = collect_body_units(
        store.db, units,
        [(row.segment_id, row.file_id) for row in latin_rows] + [(sid, fid) for fid, sid, _ in cjk_rows],
    )
    cjk_rank = {}
    for position, (fid, sid, _) in enumerate(cjk_rows, 1):
        cjk_rank.setdefault(fid, position)
        result.keyword_segment.setdefault(fid, sid)
    candidate_files = list(dict.fromkeys([*result.word_rank, *cjk_rank]))
    if units and candidate_files:
        if latin_patterns:
            result.coverage = {
                fid: coverage * len(latin_patterns) / len(units)
                for fid, coverage in store.coverage(latin_patterns, candidate_files).items()
            }
        for unit in units:
            if not unit.patterns or unit.kind == "latin" or "fts_cjk" not in tables:
                continue
            if unit.kind == "phrase":
                coverage = {fid:1.0 for fid in _phrase_files(store.db, unit.text, cjk_rows)}
            else:
                coverage = _cjk_coverage(store.db, unit.patterns, candidate_files)
            for fid, value in coverage.items():
                result.coverage[fid] = result.coverage.get(fid, 0.0) + value / len(units)
    if cjk_rows:
        ranks = result.word_rank.copy()
        combined = sorted(candidate_files, key=lambda fid: (
            -sum(1 / (60 + rank) for rank in (ranks.get(fid), cjk_rank.get(fid)) if rank is not None), fid,
        ))
        result.word_rank = {fid:pos for pos,fid in enumerate(combined, 1) if result.coverage.get(fid, 0) > 0}
    return result


def add_verified_witnesses(store, result, query, phrases, terms, witness_rows):
    """Display candidates get actual unit readback; no score/coverage changes."""
    units = query_units(query, terms, phrases)
    extra, files = collect_body_units(store.db, units, witness_rows)
    for sid, values in extra.items():
        result.segment_units[sid] = result.segment_units.get(sid, frozenset()) | values
    result.segment_files.update(files)
