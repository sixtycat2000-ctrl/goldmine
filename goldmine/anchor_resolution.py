"""Bounded, read-only source witnesses for query identity and lexical scope."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePath
import re
import unicodedata

from .deadline import check_deadline
from .lexical import (ExactMatch, cjk_runs, fts_quote, grams, identifier_tokens,
                      normalize, path_identifier_tokens, query_units, term_forms, _is_cjk)
from .query_obligations import (MAX_SOURCE_ROWS, Anchor, compact_alias, lexemes,
                                literal_equal, typed_normalize)


class VerificationBound(ValueError):
    """Recall was bounded; never infer absence from a truncated view."""


@dataclass(frozen=True)
class Binding:
    anchor_id: int
    file_id: int
    segment_id: int
    field: str
    raw: str
    match_type: str
    level: int
    query_term: str
    # Qualified reconstruction records its actual name-row witness separately.
    component_segment_id: int | None = None
    component_raw: str | None = None

    def exact(self):
        return ExactMatch(self.level, self.segment_id, self.field, self.raw,
                          self.match_type, self.query_term)


@dataclass(frozen=True)
class SourceRow:
    id: int
    file: int
    kind: str
    text: str | None
    path: str
    file_kind: str | None
    info: dict | None


def basename(path):
    return PurePath(path).name


def body_surfaces(value):
    """Maximal real surfaces; old derived keys may have dropped punctuation."""
    out = []
    for _, _, raw in lexemes(value):
        out.append(raw)
        if any(_is_cjk(c) for c in raw):
            for part in "".join(" " if _is_cjk(c) else c for c in raw).split():
                out.extend(actual for _, _, actual in lexemes(part))
    return tuple(dict.fromkeys(out))


def metadata_surfaces(value):
    # Metadata fragments cannot prove a numeric literal after losing part of
    # its actual component. Classify after NFKC, then compare whole real
    # components through the common typed gate; never enumerate sign glyphs.
    components = []
    for part in re.split(r"[/\\]", value):
        components.append(part)
        if "." in part and part.rsplit(".", 1)[1].isalpha():
            components.append(part.rsplit(".", 1)[0])
    numeric_components = {typed_normalize(word) for part in components for word in part.split()}
    out = [value]
    for raw in path_identifier_tokens(value):
        surface = unicodedata.normalize("NFKC", raw)
        if surface[:1].isdecimal() and typed_normalize(raw) not in numeric_components:
            continue
        out.append(raw)
    out.extend(components)
    return tuple(dict.fromkeys(out))


def literal_surfaces(query, value):
    """Original contiguous windows; never assemble across rows or punctuation."""
    count = len(query.split())
    if not count:
        return
    words = list(re.finditer(r"\S+", value))
    for i in range(max(0, len(words) - count + 1)):
        check_deadline()
        actual = value[words[i].start():words[i + count - 1].end()]
        for candidate in (actual, actual.strip("`'\"()[]{},;")):
            if literal_equal(query, candidate):
                yield candidate


def literal_occurs(query, value):
    """Contiguous typed phrase, with original whitespace/punctuation intact."""
    return typed_normalize(query) in typed_normalize(value)


class Resolver:
    """One request cache; source rows inspected by recall share a fixed bound."""
    def __init__(self, store, allowed):
        self.db, self.allowed = store.db, allowed
        self.rows: dict[int, SourceRow] = {}
        self.attempted: set[int] = set()
        self.witnesses: set[int] = set()
        self.queries = 0

    def execute(self, sql, values=()):
        check_deadline()
        self.queries += 1
        return self.db.execute(sql, values)

    def read(self, ids):
        ids = list(dict.fromkeys(ids))
        missing = sorted(set(ids) - self.attempted)
        if len(self.attempted) + len(missing) > MAX_SOURCE_ROWS:
            raise VerificationBound("anchor_verification_bound")
        for start in range(0, len(missing), 400):
            batch = missing[start:start + 400]
            self.attempted.update(batch)
            marks = ",".join("?" for _ in batch)
            found = self.execute(
                "SELECT s.id,s.file,s.kind,coalesce(t.original,t.body),f.path,f.kind,f.info,t.original,t.body "
                "FROM segments s JOIN files f ON f.id=s.file "
                "LEFT JOIN fts t ON t.rowid=s.id WHERE s.id IN (" + marks + ")", batch,
            ).fetchall()
            for sid, fid, kind, text, path, file_kind, info, original, body in found:
                check_deadline()
                if original is not None and normalize(original) != body:
                    raise ValueError("stored original/body normalization is inconsistent")
                value = json.loads(info) if info is not None else None
                if value is not None and not isinstance(value, dict):
                    raise ValueError("invalid stored anchor metadata")
                self.rows[sid] = SourceRow(sid, fid, kind, text, path, file_kind, value)
        return [self.rows[sid] for sid in ids if sid in self.rows and self.rows[sid].file in self.allowed]

    def _candidate_ids(self, sql, values):
        ids = [r[0] for r in self.execute(sql + " LIMIT ?", (*values, MAX_SOURCE_ROWS + 1))]
        if len(ids) > MAX_SOURCE_ROWS:
            raise VerificationBound("anchor_verification_bound")
        return ids

    @staticmethod
    def _field(row, field):
        if field == "body":
            return row.text if row.kind != "name" else None
        if row.kind != "name":
            raise ValueError("metadata anchor pointer is not a name segment")
        if field == "path":
            return row.path
        if field == "basename":
            return basename(row.path)
        value = (row.info or {}).get("title")
        return value if field == "title" and isinstance(value, str) else None

    @staticmethod
    def _derived_terms(field, value):
        if field in {"basename", "path"}:
            return {value, *path_identifier_tokens(value)}
        return set(identifier_tokens(value))

    def lookup(self, anchor, *, whole_metadata=False):
        keys = list(dict.fromkeys(key for key, _ in term_forms(anchor.raw)))
        marks = ",".join("?" for _ in keys)
        ids = self._candidate_ids(
            "SELECT DISTINCT segment_id FROM lookup_terms WHERE normalized_term IN (" + marks
            + ")" + (" AND source!='body'" if whole_metadata else "") + " ORDER BY segment_id", keys,
        )
        self.read(ids)
        if any(sid not in self.rows for sid in ids):
            raise ValueError("anchor lookup points at a missing stored segment")
        fields_by_row = self._lookup_fields(ids, keys)
        output = []
        for row in self.read(ids):
            fields = fields_by_row.get(row.id, ())
            for field in sorted(fields):
                value = self._field(row, field)
                if whole_metadata:
                    candidates = [value] if field != "body" else []
                elif field in {"basename", "path"}:
                    candidates = metadata_surfaces(value)
                else:
                    candidates = body_surfaces(value)
                for actual in candidates:
                    same = literal_equal(anchor.raw, actual)
                    if not same and (whole_metadata or not compact_alias(anchor.raw, actual)):
                        continue
                    if anchor.kind == "path" and not literal_equal(anchor.raw, value):
                        continue
                    level = (0 if field in {"path", "basename"} and literal_equal(anchor.raw, value)
                             else 1 if same else 2)
                    output.append(Binding(anchor.id, row.file, row.id, field, actual,
                                          "raw" if same else "alias", level, anchor.raw))
        return list(dict.fromkeys(output))

    def _lookup_fields(self, ids, keys):
        fields = {}
        key_marks = ",".join("?" for _ in keys)
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            terms = self.execute(
                "SELECT segment_id,raw_term,source,file_id,normalized_term,match_type "
                "FROM lookup_terms WHERE segment_id IN (" + marks + ") AND normalized_term IN ("
                + key_marks + ") ORDER BY segment_id,source,raw_term", (*batch, *keys),
            )
            for sid, raw, field, fid, normalized, match_type in terms:
                check_deadline()
                row = self.rows[sid]
                if row.file not in self.allowed:
                    continue
                value = self._field(row, field)
                if (fid != row.file or value is None or (normalized, match_type) not in term_forms(raw)
                        or raw not in self._derived_terms(field, value)):
                    raise ValueError("anchor lookup pointer does not match its actual stored source")
                fields.setdefault(sid, set()).add(field)
        return fields

    def body_ids(self, pattern, *, file_id=None, cjk=False):
        table = "fts_cjk" if cjk else "fts"
        suffix, values = (" AND s.file=?", [file_id]) if file_id is not None else ("", [])
        return self._candidate_ids(
            f"SELECT DISTINCT s.id FROM {table} JOIN segments s ON s.id={table}.rowid "
            f"WHERE {table} MATCH ? AND s.kind!='name'" + suffix + " ORDER BY s.id",
            [pattern, *values],
        )

    def recall(self, text, *, file_id=None):
        """FTS is recall only; the complete raw surface is checked afterwards."""
        words = re.findall(r"[^\W_]+", normalize(text), re.UNICODE)
        ids = []
        if words:
            expression = "body : (" + " AND ".join(fts_quote(w) for w in dict.fromkeys(words)) + ")"
            ids.extend(self.body_ids(expression, file_id=file_id))
        if cjk_runs(text):
            patterns = [fts_quote(g) for run in cjk_runs(text) for g in grams(run)]
            ids.extend(self.body_ids(" OR ".join(patterns), file_id=file_id, cjk=True))
        return self.read(ids)

    def bind(self, anchor):
        found = self.lookup(anchor)
        for row in self.recall(anchor.raw):
            for raw in body_surfaces(row.text or ""):
                if literal_equal(anchor.raw, raw):
                    found.append(Binding(anchor.id, row.file, row.id, "body", raw, "raw", 1, anchor.raw))
            if anchor.kind in {"literal", "path"}:
                for raw in literal_surfaces(anchor.raw, row.text or ""):
                    found.append(Binding(anchor.id, row.file, row.id, "body", raw, "raw", 1, anchor.raw))
        if not found:
            found = self._qualified(anchor)
        # One deterministic witness for each logical source-field binding;
        # repeated occurrences in a long file cannot multiply per-hit why.
        unique = {}
        for b in sorted(set(found), key=lambda b: (b.level, b.segment_id, b.raw)):
            key = (b.anchor_id, b.file_id, b.field, b.match_type)
            unique.setdefault(key, b)
        return list(unique.values())

    def _qualified(self, anchor):
        parts = anchor.raw.split(".")
        if len(parts) != 2 or not all(p.isidentifier() for p in parts):
            return []
        module, symbol = parts
        names = self.lookup(Anchor(-1, 0, 0, 0, module + ".py"), whole_metadata=True)
        names += self.lookup(Anchor(-1, 0, 0, 0, "__init__.py"), whole_metadata=True)
        owners = {}
        for b in names:
            row = self.rows[b.segment_id]
            path = PurePath(row.path)
            if (row.file_kind == "code" and b.field == "basename"
                    and (literal_equal(path.name, module + ".py")
                         or (path.name == "__init__.py" and literal_equal(path.parent.name, module)))):
                owners.setdefault(b.file_id, b)
        output = []
        for fid, name in sorted(owners.items()):
            for row in self.recall(symbol, file_id=fid):
                for raw in body_surfaces(row.text or ""):
                    if literal_equal(symbol, raw):
                        output.append(Binding(anchor.id, fid, row.id, "body", raw,
                                              "qualified_components", 2, anchor.raw,
                                              name.segment_id, name.raw))
        return output

    def remainder(self, file_id, text, phrases, terms_of):
        """Every body unit has a same-file real row witness; no name-column support."""
        witnesses = []
        for unit in query_units(text, terms_of(text), []):
            patterns = unit.patterns if unit.kind == "cjk" else ("body : (" + unit.patterns[0] + ")",)
            for pattern in patterns:
                ids = self.body_ids(pattern, file_id=file_id, cjk=unit.kind == "cjk")
                rows = self.read(ids)
                # CJK derived rows include copied names: require actual body.
                if unit.kind == "cjk":
                    gram = pattern[1:-1].replace('""', '"')
                    rows = [r for r in rows if gram in normalize(r.text or "")]
                if not rows:
                    return None
                witnesses.append((unit.kind, unit.text, rows[0].id))
        for phrase in phrases:
            rows = [r for r in self.recall(phrase, file_id=file_id)
                    if literal_occurs(phrase, r.text or "")]
            if not rows:
                return None
            witnesses.append(("phrase", phrase, rows[0].id))
        self.witnesses.update(sid for _, _, sid in witnesses)
        return tuple(witnesses)
