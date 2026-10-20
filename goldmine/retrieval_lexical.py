"""Bounded lexical recall and file-level coverage, shared by all search adapters."""

from __future__ import annotations

from .deadline import check_deadline
from .lexical import (
    ExactMatch, LexicalResult, cjk_runs, fts_quote, identifier_tokens,
    normalize, query_units, term_forms,
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


def _exact_matches(db, query, phrases):
    whole = normalize(" ".join(part for part in (query, *phrases) if part).strip())
    identifiers = list(identifier_tokens(" ".join((query, *phrases))))
    forms = {}
    for token in dict.fromkeys([whole, *identifiers]):
        for term, match_type in term_forms(token):
            forms.setdefault(term, []).append((token, match_type))
    matches = {}
    terms = list(forms)
    for start in range(0, len(terms), 400):
        batch = terms[start:start + 400]
        marks = ",".join("?" for _ in batch)
        rows = db.execute(
            "SELECT normalized_term,raw_term,file_id,segment_id,source,match_type FROM lookup_terms "
            f"WHERE normalized_term IN ({marks}) ORDER BY file_id,segment_id,source,raw_term LIMIT 1000", batch,
        ).fetchall()
        for normalized, raw, fid, sid, source, stored_type in rows:
            for token, query_type in forms[normalized]:
                is_raw = stored_type == query_type == "raw"
                if source in ("path", "basename") and is_raw and normalized == whole:
                    level = 0
                elif source in ("body", "title") and is_raw:
                    level = 1
                else:
                    level = 2
                candidate = ExactMatch(level, sid, source, raw, "raw" if is_raw else "alias", token)
                old = matches.get(fid)
                if old is None or (candidate.level, candidate.segment_id, candidate.source) < (old.level, old.segment_id, old.source):
                    matches[fid] = candidate
    return matches


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
    if "lookup_terms" in tables:
        for fid, match in _exact_matches(store.db, query, phrases).items():
            rest = normalize(" ".join((query, *phrases))).replace(normalize(match.query_term), " ", 1)
            rest_units = query_units(rest, terms_of(rest), [])
            supported = True
            for unit in rest_units:
                if unit.kind == "latin":
                    supported &= store.coverage(list(unit.patterns), [fid]).get(fid, 0.0) == 1.0
                elif "fts_cjk" in tables:
                    supported &= _cjk_coverage(store.db, unit.patterns, [fid]).get(fid, 0.0) >= 1.0 - 1e-9
                else:
                    supported = False
            if supported:
                result.exact[fid] = match
            else:
                result.rejected_exact.add(fid)
    return result
