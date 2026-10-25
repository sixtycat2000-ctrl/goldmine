"""Bounded, read-only source witnesses for query identity and lexical scope."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
import re
import unicodedata

from .deadline import check_deadline
from .read_source import fts_projection, read_source_profile
from .metadata_prelookup import decode_info, whole_metadata_predicate
from .legacy_anchors import LegacyCandidates
from .lexical import (ExactMatch, cjk_runs, fts_quote, grams, identifier_tokens,
                      normalize, path_identifier_tokens, query_units, term_forms, _is_cjk)
from .query_obligations import (MAX_SOURCE_ROWS, QUOTE_PROVENANCE_SEGMENT_CAP,
                                Anchor, compact_alias, lexemes,
                                literal_equal, path_plausible, quote_normalize,
                                typed_normalize)


class VerificationBound(ValueError):
    """Recall was bounded; never infer absence from a truncated view."""


#: Code rows where a bound surface is DEFINED rather than mentioned: a
#: def/class-style declaration, a (possibly annotated) assignment, or a C
#: macro, each at a line start. Stdlib modules name every public function in
#: their header `__all__` list, so without this the exact pointer landed on
#: the header chunk instead of the definition.
_DEFINITION_KEYWORDS = r"(?:async\s+def|def|class|function|fn|struct|enum|interface|trait|type)"
_DEFINITION_SITE = (r"^\s*{kw}\s+{name}\b"
                    r"|^\s*(?:(?:const|let|var|static|final)\s+)?{name}\s*(?::[^=\n]*)?=(?!=)"
                    r"|^\s*#\s*define\s+{name}\b")


def definition_site(text, surface):
    """True when `surface`'s last dotted component is defined in `text`."""
    if not text or not surface:
        return False
    name = re.escape(surface.rsplit(".", 1)[-1])
    pattern = _DEFINITION_SITE.format(kw=_DEFINITION_KEYWORDS, name=name)
    return re.search(pattern, text, re.MULTILINE) is not None


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


def remainder_units(text, terms_of):
    """Pure clause-level unit preparation (SC-01): identical for every
    eligible file, so owners prepare it once per clause, not per file."""
    return query_units(text, terms_of(text), [])


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


_BRACKETS = "()[]{}（）［］｛｝"


def _bracket_stripped(text):
    start, end = 0, len(text)
    while start < end and text[start] in _BRACKETS:
        start += 1
    while end > start and text[end - 1] in _BRACKETS:
        end -= 1
    return text[start:end]


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
            # A numeric-leading fragment is still a real component when the
            # stored component differs only by wrapping bracket punctuation
            # (photo_2018_Aug_04_(3551).jpg -> 3551); signs, decimal points,
            # commas and letters never bridge that gap.
            if not any(part != _bracket_stripped(part) and _bracket_stripped(part) == surface
                       for part in components):
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
        self.profile = read_source_profile(self.db)
        self.legacy = None if self.profile.exact else LegacyCandidates(self)
        self.rows: dict[int, SourceRow] = {}
        self.attempted: set[int] = set()
        self.fetched: set[int] = set()
        self.witnesses: set[int] = set()
        self.queries = 0
        self._quote_cache: dict[int, str | None] = {}

    def execute(self, sql, values=()):
        check_deadline()
        self.queries += 1
        return self.db.execute(sql, values)

    def read(self, ids, *, preread=False):
        """Fetch source rows once. Preread rows (lexical recall seeds) are
        cached but do not consume the obligation-verification budget; the
        bound keeps meaning only for obligation verification."""
        ids = list(dict.fromkeys(ids))
        missing = sorted(set(ids) - self.fetched)
        if not preread and len(self.attempted) + len(missing) > MAX_SOURCE_ROWS:
            raise VerificationBound("anchor_verification_bound")
        for start in range(0, len(missing), 400):
            batch = missing[start:start + 400]
            if not preread:
                self.attempted.update(batch)
            self.fetched.update(batch)
            marks = ",".join("?" for _ in batch)
            text_sql, original_sql = fts_projection(self.db, "t")
            found = self.execute(
                f"SELECT s.id,s.file,s.kind,{text_sql},f.path,f.kind,f.info,{original_sql},t.body "
                "FROM segments s JOIN files f ON f.id=s.file "
                "LEFT JOIN fts t ON t.rowid=s.id WHERE s.id IN (" + marks + ")", batch,
            ).fetchall()
            for sid, fid, kind, text, path, file_kind, info, original, body in found:
                check_deadline()
                self.profile.validate_original(original, body)
                value = decode_info(info)
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

    def lookup(self, anchor, *, whole_metadata=False, body_only=False):
        if self.legacy is not None:
            fields = self.legacy.lookup(anchor, whole_metadata=whole_metadata, body_only=body_only)
            output = self._bindings(anchor, fields, whole_metadata)
            if not output and not whole_metadata and not body_only:
                fields = self.legacy.fields(self.legacy.enumerate())
                output = self._bindings(anchor, fields, False)
            return output
        keys = ([normalize(anchor.raw)] if whole_metadata else
                list(dict.fromkeys(key for key, _ in term_forms(anchor.raw))))
        if whole_metadata:
            with whole_metadata_predicate(self.db, self.allowed, derived=True) as function:
                ids = self._candidate_ids(
                    "SELECT DISTINCT l.segment_id FROM lookup_terms l "
                    "LEFT JOIN segments s ON s.id=l.segment_id LEFT JOIN files f ON f.id=s.file "
                    "WHERE l.normalized_term=? AND l.source!='body' AND l.match_type='raw' "
                    f"AND {function}(s.id,s.file,s.kind,f.id,f.path,f.info,l.file_id,l.source,"
                    "l.raw_term,l.normalized_term,l.match_type,?)=1 ORDER BY l.segment_id", [keys[0], anchor.raw],
                )
        else:
            marks = ",".join("?" for _ in keys)
            ids = self._candidate_ids(
                "SELECT DISTINCT segment_id FROM lookup_terms WHERE normalized_term IN (" + marks
                + ")" + (" AND source='body'" if body_only else "") + " ORDER BY segment_id", keys,
            )
        self.read(ids)
        if any(sid not in self.rows for sid in ids):
            raise ValueError("anchor lookup points at a missing stored segment")
        fields = self._lookup_fields(ids, keys, whole_metadata=whole_metadata, body_only=body_only)
        return self._bindings(anchor, fields, whole_metadata)

    def _bindings(self, anchor, fields_by_row, whole_metadata):
        output = []
        for row in self.read(fields_by_row):
            for field in sorted(fields_by_row[row.id]):
                value = self._field(row, field)
                if value is None:
                    continue
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

    def _lookup_fields(self, ids, keys, *, whole_metadata=False, body_only=False):
        fields = {}
        key_marks = ",".join("?" for _ in keys)
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            raw_only = (" AND source!='body' AND match_type='raw'" if whole_metadata else
                        " AND source='body'" if body_only else "")
            terms = self.execute(
                "SELECT segment_id,raw_term,source,file_id,normalized_term,match_type "
                "FROM lookup_terms WHERE segment_id IN (" + marks + ") AND normalized_term IN ("
                + key_marks + ")" + raw_only + " ORDER BY segment_id,source,raw_term", (*batch, *keys),
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

    def quote_corpus(self, file_id):
        """Whitespace-run-normalized concatenation of one owner file's stored
        per-segment original texts in segment-id order — the quote-provenance
        surface. None when the file has at least QUOTE_PROVENANCE_SEGMENT_CAP
        non-name segments: never prove quoting from a truncated view. Preread
        semantics (design §2.6): fetched+cached, never attempted, never
        bound-checked."""
        if file_id not in self._quote_cache:
            ids = [r[0] for r in self.execute(
                "SELECT id FROM segments WHERE file=? AND kind!='name' "
                "ORDER BY id LIMIT ?", (file_id, QUOTE_PROVENANCE_SEGMENT_CAP)
            ).fetchall()]
            text = None
            if len(ids) < QUOTE_PROVENANCE_SEGMENT_CAP:
                rows = self.read(ids, preread=True)
                text = quote_normalize(" ".join(r.text or "" for r in rows))
            self._quote_cache[file_id] = text
        return self._quote_cache[file_id]

    def body_ids(self, pattern, *, file_id=None, cjk=False):
        table = "fts_cjk" if cjk else "fts"
        if file_id is not None:
            # File-first shape (SC-01): the existing file index is the outer
            # relation and the FTS rowid a constrained inner lookup, so a
            # common term cannot repeatedly walk the global posting list for
            # every owner file. Identical DISTINCT/ORDER/limit and non-name
            # predicates; result/error equivalence is pinned by
            # tests/test_request_local_reuse.py. Divergence is limited to a
            # malformed pattern combined with a file that has no non-name
            # segments (patterns are fts_quote-built in production).
            return self._candidate_ids(
                f"SELECT DISTINCT s.id FROM segments s CROSS JOIN {table} ON {table}.rowid=s.id "
                f"WHERE s.file=? AND s.kind!='name' AND {table} MATCH ? ORDER BY s.id",
                [file_id, pattern],
            )
        return self._candidate_ids(
            f"SELECT DISTINCT s.id FROM {table} JOIN segments s ON s.id={table}.rowid "
            f"WHERE {table} MATCH ? AND s.kind!='name' ORDER BY s.id",
            [pattern],
        )

    def recall(self, text, *, file_id=None):
        """FTS is recall only; the complete raw surface is checked afterwards."""
        words = re.findall(r"[^\W_]+", normalize(text), re.UNICODE)
        ids = []
        if words:
            expression = "body : (" + " AND ".join(fts_quote(w) for w in dict.fromkeys(words)) + ")"
            ids.extend(self.body_ids(expression, file_id=file_id))
        if self.profile.cjk and cjk_runs(text):
            patterns = [fts_quote(g) for run in cjk_runs(text) for g in grams(run)]
            ids.extend(self.body_ids(" OR ".join(patterns), file_id=file_id, cjk=True))
        return self.read(ids)

    def literal_rows(self, text, *, file_id=None):
        rows = [row for row in self.recall(text, file_id=file_id)
                if literal_occurs(text, row.text or "")]
        if not rows and self.legacy is not None:
            rows = [row for row in self.legacy.enumerate(file_id)
                    if row.kind != "name" and literal_occurs(text, row.text or "")]
        return rows

    def bind(self, anchor, *, body_only=False):
        found = self.lookup(anchor, body_only=body_only)
        for row in self.recall(anchor.raw):
            for raw in body_surfaces(row.text or ""):
                if literal_equal(anchor.raw, raw):
                    found.append(Binding(anchor.id, row.file, row.id, "body", raw, "raw", 1, anchor.raw))
            if anchor.kind in {"literal", "path"}:
                for raw in literal_surfaces(anchor.raw, row.text or ""):
                    found.append(Binding(anchor.id, row.file, row.id, "body", raw, "raw", 1, anchor.raw))
        if not found and not body_only:
            found = self._qualified(anchor)
        if not found and not body_only and anchor.kind == "path" and path_plausible(anchor.raw):
            found = self._path_suffix(anchor)
        # One deterministic witness for each logical source-field binding;
        # repeated occurrences in a long file cannot multiply per-hit why.
        # Among a code file's body rows the definition outranks a mention.
        unique = {}
        for b in sorted(set(found), key=lambda b: (b.level, not self._defines(b), b.segment_id, b.raw)):
            key = (b.anchor_id, b.file_id, b.field, b.match_type)
            unique.setdefault(key, b)
        return list(unique.values())

    def _defines(self, binding):
        if binding.field != "body":
            return False
        row = self.rows.get(binding.segment_id)
        if row is None or row.file_kind != "code":
            return False
        return definition_site(row.text, binding.raw)

    def _path_suffix(self, anchor):
        """Bounded files.path suffix recall for plausible path anchors whose
        ordinary routes (lookup/recall/qualified) produced nothing. Verified
        against the same-snapshot stored path at a separator boundary; the
        tier never outranks an existing whole-path/basename/alias binding."""
        def escaped(value):
            # GLOB: '[' opens a class ('[[]' is a literal '['); ']' outside a
            # class and all other characters are literal.
            return (value.replace("[", "[[]")
                    .replace("*", "[*]").replace("?", "[?]"))
        rows = self.execute(
            "SELECT id,path FROM files WHERE path GLOB ? ORDER BY id LIMIT ?",
            ("*" + escaped(anchor.raw), MAX_SOURCE_ROWS + 1),
        ).fetchall()
        if len(rows) > MAX_SOURCE_ROWS:
            raise VerificationBound("anchor_verification_bound")
        output = []
        for fid, path in rows:
            if fid not in self.allowed:
                continue
            tails = []
            remainder = path
            while "/" in remainder or "\\" in remainder:
                remainder = re.split(r"[/\\]", remainder, maxsplit=1)[1]
                tails.append(remainder)
            tail = next((t for t in tails if literal_equal(anchor.raw, t)), None)
            if tail is None:
                continue
            name = self.execute(
                "SELECT id FROM segments WHERE file=? AND kind='name' ORDER BY id LIMIT 1",
                (fid,)).fetchone()
            if name is None:
                continue
            output.append(Binding(anchor.id, fid, name[0], "path", tail,
                                  "path_suffix", 3, anchor.raw))
        return output

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

    def remainder(self, file_id, text, phrases, terms_of, units=None):
        """Every body unit has a same-file real row witness; no name-column support."""
        if units is None:
            units = query_units(text, terms_of(text), [])
        witnesses = []
        for unit in units:
            patterns = unit.patterns if unit.kind == "cjk" else ("body : (" + unit.patterns[0] + ")",)
            for pattern in patterns:
                if unit.kind == "cjk" and not self.profile.cjk:
                    rows = [row for row in self.legacy.enumerate(file_id) if row.kind != "name"]
                else:
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
            rows = self.literal_rows(phrase, file_id=file_id)
            if not rows:
                return None
            witnesses.append(("phrase", phrase, rows[0].id))
        self.witnesses.update(sid for _, _, sid in witnesses)
        return tuple(witnesses)
