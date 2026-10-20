"""Answer-material packs (`search --pack`, R1) + neighbor context (`--context N`, R2).

Spec: docs/v2.1-pack-eval.md (§1 neighbors, §2 pack). Shape decisions D1-D10,
with the review amendments F8/F9 applied:

- Pack = flat records: `#` header block, `[n]` records with verbatim merged
  text, `# sources` footer (D1). The WHOLE stdout text is budget-measured via
  ``backend.count_tokens`` (estimator fallback; the mode is decided by ONE
  probe call and printed in the header) (D2).
- Skip-not-truncate: the fill stops at the first record that does not fit
  whole; the TOP record may fall back to its slim form (hit chunk only) —
  dropping context is not truncation (D3).
- Record order = hit rank order; no re-sorting (D4). Only identical complete
  material folds, retaining every source association; a hit whose segment sits inside an
  earlier record folds into it (``also match``) — structurally unreachable
  while search yields one hit per file, kept as the spec's invariant guard.
- Neighbors: same file, non-name text segments, position order, N each side
  (clamp 3), merged before/hit/after; the hit's loc/score stay the header's
  authoritative pointer (D6/D7).
- Seam dedup: char-aligned suffix==prefix trim, C in [24, 640], gated on
  line-range adjacency, PDF pages excluded (D8).
- Determinism: no timestamps, fixed float precision, fixed orderings; the
  footer ``used N`` is a render-measure fixpoint (D10). Non-convergence drops
  the last record and retries (F8) — the footer never prints an unmeasured
  number while any record can be dropped.
- A textless/vector-only hit renders as a record with an explicit note line
  (F9); the footer counts also-folded hits explicitly so
  ``records + also + skipped + duplicates == hits`` closes.

Every failure mode this module can raise is a plain ``ValueError`` — the CLI
layer converts them to the universal error shape (usage errors) before they
reach a user.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Callable

from .chunker import est_tokens
from .render import _render_loc
from .search import Hit, QueryFilters  # noqa: F401  (QueryFilters re-export)

PACK_SCHEMA = "goldmine.pack/v1"
SEAM_MIN_CHARS = 24       # below this a suffix==prefix match is coincidence
SEAM_MAX_CHARS = 640      # a 48-token overlap never exceeds this (URL-heavy)
MAX_RECORDS = 24          # hard cap; budget is the real constraint
MAX_CONTEXT = 3           # --context clamp (D6): <= 7 chunks ~= 3150 tokens
DEFAULT_BUDGET = 4000
BUDGET_FLOOR = 512        # distill §4.6b parity clamps
BUDGET_CEILING = 32768
FIXPOINT_ROUNDS = 3       # D10: converges <= 3 rounds in practice

_SEAM_EXCLUDED_KINDS = frozenset({"pdf", "page"})  # page text is independent

#: F9 note lines — clearly NOT verbatim material (the `#` prefix is the
#: header/footer comment grammar; a textless record carries no quotable text
#: and says so instead of rendering silence).
TEXTLESS_NOTE = "# no quotable text: this segment is indexed as a vector only"
NAME_HIT_NOTE = "name hit"
TEXTLESS_NOTE_SHORT = "vector-only hit (no text)"


class BudgetTooSmall(ValueError):
    """Even the empty pack cannot fit its complete, measured envelope."""

    code = "budget_too_small"


# ----------------------------------------------------------------- settings


@dataclass
class PackSettings:
    """The pack-relevant slice of the search flags (§2.1). `context` is the
    EFFECTIVE radius (already clamped); `query` is the clean query."""

    query: str = ""
    scorer: str = "hybrid"
    z_gate: float = 3.0
    limit: int = 20
    context: int = 1
    budget: int = DEFAULT_BUDGET
    kinds: set | None = None
    filters: QueryFilters | None = None
    why: bool = False


def clamp_budget(n: int) -> int:
    """Token budget clamp, distill §4.6b parity (512..32768)."""
    return max(BUDGET_FLOOR, min(BUDGET_CEILING, int(n)))


def clamp_context(n: int) -> int:
    """--context clamp (D6): >= 0, <= 3. Negative is a caller bug (the CLI
    turns it into a usage error before reaching us)."""
    n = int(n)
    if n < 0:
        raise ValueError(f"--context must be >= 0, got {n}")
    return min(n, MAX_CONTEXT)


# --------------------------------------------------------------- seam dedup


def trim_seam(prev_text: str, next_text: str) -> str:
    """Drop next's leading chars when they duplicate prev's tail (the
    chunker's 48-token rewind overlap, D8). Char-aligned because both chunks
    are .strip()ed slices of the same cleaned text; adjacency is the CALLER's
    gate. Under-trim (a missed overlap) is safe; the 24-char floor keeps
    coincidental short matches from eating real content."""
    hi = min(SEAM_MAX_CHARS, len(prev_text), len(next_text))
    for c in range(hi, SEAM_MIN_CHARS - 1, -1):
        if prev_text[-c:] == next_text[:c]:
            return next_text[c:]
    return next_text


# --------------------------------------------------------------- neighbors


class NeighborIndex:
    """Per-file renderable-segment cache + text fetch (§1.6). The candidate
    set is the file's non-name segments that HAVE an FTS row (name stubs and
    text-less vector rows have nothing to expand); ordered by (loc, loc_end,
    id). One SQL per file, cached across the records of that file."""

    _SQL = (
        "SELECT s.id, s.kind, s.loc, s.loc_end FROM segments s "
        "WHERE s.file = ? AND s.kind != 'name' "
        "AND EXISTS (SELECT 1 FROM fts WHERE fts.rowid = s.id) "
        "ORDER BY s.loc, s.loc_end, s.id"
    )

    def __init__(self, store):
        self.store = store
        self._by_file: dict[int, list[dict]] = {}
        self._path_to_id: dict[str, int | None] = {}
        self._texts: dict[int, str] = {}

    def file_id_of(self, path: str) -> int | None:
        if path not in self._path_to_id:
            row = self.store.db.execute(
                "SELECT id FROM files WHERE path = ?", (path,)
            ).fetchone()
            self._path_to_id[path] = row[0] if row else None
        return self._path_to_id[path]

    def segments(self, file_id: int) -> list[dict]:
        if file_id not in self._by_file:
            rows = self.store.db.execute(self._SQL, (file_id,)).fetchall()
            self._by_file[file_id] = [
                {"segment_id": r[0], "kind": r[1], "loc": r[2], "loc_end": r[3]}
                for r in rows
            ]
        return self._by_file[file_id]

    def texts(self, seg_ids: list[int]) -> dict[int, str]:
        missing = [s for s in seg_ids if s not in self._texts]
        if missing:
            ph = ",".join("?" for _ in missing)
            rows = self.store.db.execute(
                f"SELECT rowid, coalesce(original, body) FROM fts "
                f"WHERE rowid IN ({ph})",
                tuple(missing),
            ).fetchall()
            for r in rows:
                self._texts[r[0]] = r[1] or ""
        return {s: self._texts[s] for s in seg_ids if s in self._texts}

    def parts_for(self, hit: Hit, context: int) -> tuple[list[dict], str | None]:
        """The hit's own text + up to `context` neighbors each side, position
        order. Returns (parts, note): note is NAME_HIT_NOTE for a name-segment
        hit (no expansion — §1.1), TEXTLESS_NOTE_SHORT for a hit whose segment
        has no FTS row (F9), None otherwise. Parts carry
        {role, loc, loc_end, segment_id, kind, text}; role one of
        before/hit/after."""
        if hit.segment_id is None:
            return [], TEXTLESS_NOTE_SHORT
        fid = self.file_id_of(hit.path)
        segs = self.segments(fid) if fid is not None else []
        idx = next(
            (i for i, s in enumerate(segs) if s["segment_id"] == hit.segment_id),
            None,
        )
        if idx is None:
            # name hits keep their stub text (filename + folder words);
            # a non-name segment missing from fts is vector-only (F9)
            if hit.segment == "name":
                texts = self.texts([hit.segment_id])
                text = texts.get(hit.segment_id, "")
                part = {
                    "role": "hit", "loc": hit.loc, "loc_end": hit.loc_end,
                    "segment_id": hit.segment_id, "kind": hit.segment,
                    "text": text,
                }
                return ([part] if text else []), NAME_HIT_NOTE
            return [], TEXTLESS_NOTE_SHORT
        lo, hi = max(0, idx - context), min(len(segs), idx + context + 1)
        chosen = segs[lo:hi]
        if hit.kind == "pdf" and hit.loc is not None:
            chosen = [s for s in segs if s["loc"] is not None
                      and abs(s["loc"] - hit.loc) <= context]
        supporting = set(getattr(hit, "supporting_segment_ids", [])[:2])
        chosen = list({s["segment_id"]: s for s in chosen + [
            s for s in segs if s["segment_id"] in supporting
        ]}.values())
        chosen.sort(key=lambda s: (s["loc"] is None, s["loc"] or 0, s["loc_end"] or 0, s["segment_id"]))
        texts = self.texts([s["segment_id"] for s in chosen])
        parts: list[dict] = []
        for s in chosen:
            text = texts.get(s["segment_id"])
            if not text:
                continue
            role = "hit" if s["segment_id"] == hit.segment_id else "supporting" if s["segment_id"] in supporting else (
                "before"
                if s["loc"] is not None and hit.loc is not None and s["loc"] < hit.loc
                else "after"
            )
            parts.append({
                "role": role, "loc": s["loc"], "loc_end": s["loc_end"],
                "segment_id": s["segment_id"], "kind": s["kind"], "text": text,
            })
        if not parts:
            return [], TEXTLESS_NOTE_SHORT
        return parts, None

    def spans_for(self, hit: Hit, context: int) -> tuple[list[dict], str | None]:
        """Neighbor spans without fetching text (the list-mode note line):
        [{role, loc, loc_end}], plus the same note as parts_for."""
        if hit.segment_id is None:
            return [], TEXTLESS_NOTE_SHORT
        if hit.segment == "name":
            return [], NAME_HIT_NOTE
        fid = self.file_id_of(hit.path)
        segs = self.segments(fid) if fid is not None else []
        idx = next(
            (i for i, s in enumerate(segs) if s["segment_id"] == hit.segment_id),
            None,
        )
        if idx is None:
            return [], TEXTLESS_NOTE_SHORT
        lo, hi = max(0, idx - context), min(len(segs), idx + context + 1)
        out = []
        chosen = segs if hit.kind == "pdf" else segs[lo:hi]
        for s in chosen:
            if s["segment_id"] == hit.segment_id:
                continue
            if hit.kind == "pdf" and (s["loc"] is None or hit.loc is None
                                      or abs(s["loc"] - hit.loc) > context):
                continue
            role = (
                "before"
                if s["loc"] is not None and hit.loc is not None and s["loc"] < hit.loc
                else "after"
            )
            out.append({"role": role, "loc": s["loc"], "loc_end": s["loc_end"]})
        return out, None


def merge_parts(parts: list[dict]) -> str:
    """Concatenate position-ordered parts, deduping the chunker overlap at
    each seam (D8): adjacency-gated (next starts at/before prev's end),
    char-aligned suffix==prefix trim, PDF pages excluded."""
    out = ""
    prev: dict | None = None
    for p in parts:
        text = p["text"]
        if out and prev is not None:
            adjacent = (
                p["loc"] is not None
                and prev["loc_end"] is not None
                and p["loc"] <= prev["loc_end"]
            )
            if adjacent and p["kind"] not in _SEAM_EXCLUDED_KINDS:
                text = trim_seam(out, text)
        out = f"{out}\n{text}".strip() if out else text
        prev = p
    return out


def format_context_note(kind: str, spans: list[dict], note: str | None) -> str:
    """The human list-mode note line under a hit (§1.3): the two sides with
    their covered spans and the neighbor count, or the honest no-expansion
    note (`name hit` / `vector-only hit (no text)`). Empty when there is
    nothing to say (context 0 can never reach here — the CLI gates it)."""
    if note is not None:
        return f"   · context: {note}"
    sides = []
    for role in ("before", "after"):
        side = [s for s in spans if s["role"] == role]
        if side:
            sides.append(
                f"{role} {_render_loc(kind, side[0]['loc'], side[-1]['loc_end'])}"
            )
    if not sides:
        return ""
    return f"   · context: {', '.join(sides)} (+{len(spans)} chunks)"


# ----------------------------------------------------------------- records


@dataclass
class PackRecord:
    n: int
    hit: Hit
    parts: list[dict] = field(default_factory=list)
    text: str = ""
    note: str | None = None       # F9: TEXTLESS_NOTE_SHORT when no quotable text
    also_hits: list[Hit] = field(default_factory=list)
    citation_ids: dict[int, str] = field(default_factory=dict)

    @property
    def score(self) -> float:
        return self.hit.score


def collect_records(
    neighbor: NeighborIndex, hits: list[Hit], context: int
) -> tuple[list[PackRecord], int, int]:
    """Rank-order hits -> records with neighbor expansion (§1.4). Returns
    (records, duplicates_folded, also_folded). Identical complete material folds
    with source associations; a hit whose segment is already inside an earlier
    record folds into it (also_hits); a segment renders at most once per
    pack (§1.4 rule 3)."""
    records: list[PackRecord] = []
    used_segments: set[int] = set()
    seg_to_record: dict[int, PackRecord] = {}
    folded = 0
    also = 0
    by_material: dict[str, PackRecord] = {}

    for hit in hits:
        if hit.segment_id is not None and hit.segment_id in seg_to_record:
            seg_to_record[hit.segment_id].also_hits.append(hit)
            also += 1
            continue
        parts, note = neighbor.parts_for(hit, context)
        # cross-record dedup: drop parts whose segment already rendered
        parts = [
            p for p in parts
            if p["role"] == "hit" or p["segment_id"] not in used_segments
        ]
        rec = PackRecord(n=len(records) + 1, hit=hit, parts=parts, note=note)
        rec.text = material_text(parts, hit)
        if note == TEXTLESS_NOTE_SHORT:
            rec.text = TEXTLESS_NOTE
        # Similar vectors can contain different dates/numbers or different
        # neighboring facts. Only identical complete emitted material folds.
        prior = by_material.get(rec.text) if rec.parts and hit.segment != "name" else None
        if prior is not None and prior.hit.path != hit.path:
            prior.also_hits.append(hit)
            folded += 1
            if hit.segment_id is not None:
                seg_to_record[hit.segment_id] = prior
            continue
        records.append(rec)
        if rec.parts and hit.segment != "name":
            by_material.setdefault(rec.text, rec)
        for p in parts:
            used_segments.add(p["segment_id"])
            seg_to_record[p["segment_id"]] = rec
        if len(records) >= MAX_RECORDS:
            break
    return records, folded, also


def material_text(parts: list[dict], hit: Hit) -> str:
    """Keep disjoint spans visibly separate, so page 3 never implies page 17."""
    separate = any(p["role"] == "supporting" for p in parts) or (
        hit.kind == "pdf" and len({p["loc"] for p in parts}) > 1
    )
    if not separate:
        return merge_parts(parts)
    groups = []
    for part in parts:
        if groups and hit.kind == "pdf" and groups[-1][0]["loc"] == part["loc"]:
            groups[-1].append(part)
        else:
            groups.append([part])
    return "\n\n".join(
        f"# stored evidence: {_render_loc(hit.kind, group[0]['loc'], group[-1]['loc_end'])}\n"
        + merge_parts(group) for group in groups
    )


# ---------------------------------------------------------------- rendering


def render_pack(
    records: list[PackRecord],
    settings: PackSettings,
    *,
    index_line: str,
    n_hits: int,
    n_folded: int,
    n_also: int,
    n_skipped: int,
    used_tokens: int,
    token_mode: str,
    n_capped: int = 0,
) -> str:
    """The exact pack grammar (§2.2). Deterministic: fixed orderings, fixed
    float precision, no timestamps."""
    lines = [
        "# goldmine answer pack v1",
        f"# query: {settings.query}",
        f"# search: scorer={settings.scorer} z_gate={settings.z_gate:g} "
        f"limit={settings.limit} context={settings.context} "
        f"budget={settings.budget} mode={token_mode}",
        f"# index: {index_line}",
        "# the material below is verbatim from the index; cite sources as [n]",
        "",
    ]
    for rec in records:
        h = rec.hit
        loc = _render_loc(h.kind, h.loc, h.loc_end)
        z_part = (
            f"(z {h.z:.2f}, cov {h.coverage:.2f})" if h.z is not None
            else f"(cov {h.coverage:.2f})"
        )
        head = f"[{rec.n}] {h.path} ({h.kind}) {loc} · score {h.score:.2f} {z_part}"
        ctx = [p for p in rec.parts if p["role"] != "hit"]
        if ctx:
            sides = []
            for role in ("before", "after"):
                side = [p for p in ctx if p["role"] == role]
                if side:
                    sides.append(
                        f"{role} {_render_loc(h.kind, side[0]['loc'], side[-1]['loc_end'])}"
                    )
            if sides:
                head += f" · context ±{settings.context} ({', '.join(sides)})"
        if rec.note is not None:
            head += f" · {rec.note}"
        for other in rec.also_hits:
            oloc = _render_loc(other.kind, other.loc, other.loc_end)
            head += f" · also match [{other.rank}] score {other.score:.2f} {oloc}"
        lines.append(head)
        if settings.why and h.why is not None:
            lines.append("# why: " + json.dumps(h.why, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":"), allow_nan=False))
        lines.append(rec.text if rec.text else TEXTLESS_NOTE)
        lines.append("")
    cited = []
    for rec in records:
        h = rec.hit
        if any(p["role"] == "supporting" for p in rec.parts):
            span = ", ".join(dict.fromkeys(_render_loc(h.kind, p["loc"], p["loc_end"]) for p in rec.parts))
        elif rec.parts:
            span = _render_loc(h.kind, rec.parts[0]["loc"], rec.parts[-1]["loc_end"])
        else:
            span = _render_loc(h.kind, h.loc, h.loc_end)
        also = "".join(f" + hit[{o.rank}]" for o in rec.also_hits)
        cited.append(f"# [{rec.n}] {h.path} {span}{also}")
        for other in rec.also_hits:
            cited.append(f"# [{rec.n}] also source: {other.path} {_render_loc(other.kind, other.loc, other.loc_end)}")
        included = {p["segment_id"] for p in rec.parts}
        if h.segment_id is not None:
            included.add(h.segment_id)
        included.update(other.segment_id for other in rec.also_hits if other.segment_id is not None)
        for segment_id, citation_id in rec.citation_ids.items():
            if segment_id in included:
                cited.append(f"# [{rec.n}] citation: {citation_id}")
    lines.append("# sources")
    lines.extend(cited)
    lines.append(
        f"# pack: {len(records)} records · {n_hits} hits · {n_folded} duplicates "
        f"folded · {n_also} also folded · {n_skipped} skipped (budget) · "
        f"used {used_tokens}/{settings.budget} tokens ({token_mode})"
        + (f" · {n_capped} skipped (record limit)" if n_capped else "")
    )
    return "\n".join(lines) + "\n"


def tokens_of(text: str, backend) -> tuple[int, str]:
    """(count, mode): the backend's own counter when it has one (llama
    /tokenize), else the chars//4 estimator (§2.3)."""
    if backend is not None:
        counts = backend.count_tokens([text])
        if counts is not None:
            if (len(counts) != 1 or isinstance(counts[0], bool)
                    or not isinstance(counts[0], (int, float))
                    or not math.isfinite(counts[0]) or counts[0] < 0
                    or int(counts[0]) != counts[0]):
                raise ValueError("invalid token count returned by backend")
            return int(counts[0]), "tokenize"
    return est_tokens(text), "estimate"


def index_line_of(store) -> str:
    """The `# index:` header line: fingerprint id + model id + dim."""
    fp_id = store.meta("fingerprint_id") or "none"
    fp_json = store.meta("fingerprint_json")
    model_id, dim = "?", "?"
    if fp_json:
        try:
            fp = json.loads(fp_json)
        except (ValueError, TypeError):
            fp = {}
        model_id = fp.get("model_id", "?")
        dim = fp.get("dim", "?")
    return f"{fp_id} {model_id} dim={dim}"


# ----------------------------------------------------------------- pipeline


def build_pack_from_hits(
    store,
    backend,
    settings: PackSettings,
    hits: list[Hit],
    *,
    citation_id_of: Callable[[int], str] | None = None,
) -> tuple[str, dict]:
    try:
        return _build_pack(store, backend, settings, hits, citation_id_of=citation_id_of)
    except _TokenizationLost:
        # Re-select and recount the entire envelope under one fixed estimator.
        # Never combine an exact probe with later estimated record/footer sizes.
        return _build_pack(store, None, settings, hits, citation_id_of=citation_id_of)


class _TokenizationLost(RuntimeError):
    pass


def _build_pack(store, backend, settings, hits, *, citation_id_of=None):
    """Assemble + budget + render the pack for ALREADY-SEARCHED hits (§2.3).
    The caller owns the search (one search per invocation); this function is
    pure presentation + budgeting. Returns (pack_text, meta) where meta is
    the `--pack --json` payload AND the eval sidecar body (additive keys
    allowed by goldmine.pack/v1)."""
    neighbor = NeighborIndex(store)
    records, folded, also = collect_records(neighbor, hits, settings.context)
    capped = len(hits) - len(records) - folded - also
    if citation_id_of is not None:
        for rec in records:
            segment_ids = list(dict.fromkeys(
                [p["segment_id"] for p in rec.parts]
                + ([rec.hit.segment_id] if rec.hit.segment_id is not None else [])
                + [other.segment_id for other in rec.also_hits if other.segment_id is not None]
            ))
            rec.citation_ids = {sid: citation_id_of(sid) for sid in segment_ids}
            if rec.hit.segment == "name":
                rec.parts = []
                row = store.db.execute("SELECT path,source,kind,info FROM files WHERE path=?",
                                       (rec.hit.path,)).fetchone()
                metadata = {"path": row[0], "source": row[1], "kind": row[2],
                            "info": json.loads(row[3]) if row[3] else None} if row else {}
                rec.text = ("# stored name metadata; no quotable document body\n"
                            + json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False))
                rec.note = "name_metadata"
    index_line = index_line_of(store)
    # token mode is decided BEFORE selection by one probe call (§2.2): the
    # header prints it, so it must be known before the first render
    _, token_mode = tokens_of("x", backend)

    def count(text):
        if token_mode == "estimate":
            return est_tokens(text)
        used, actual_mode = tokens_of(text, backend)
        if actual_mode != "tokenize":
            raise _TokenizationLost()
        return used

    def _render(chosen: list[PackRecord], skipped: int, guess: int) -> tuple[str, int]:
        text = render_pack(
            chosen, settings, index_line=index_line, n_hits=len(hits),
            n_folded=folded, n_also=also, n_skipped=skipped,
            used_tokens=guess, token_mode=token_mode, n_capped=capped,
        )
        used = count(text)
        return text, used

    def _fixpoint(chosen: list[PackRecord], skipped: int):
        """Footer used-count as a fixpoint (D10): render with N, measure,
        repeat until the printed number equals the measured one. Returns
        (text, used, converged)."""
        guess = 0
        text, used = _render(chosen, skipped, guess)
        for _ in range(FIXPOINT_ROUNDS):
            if used == guess:
                return text, used, True
            guess = used
            text, used = _render(chosen, skipped, guess)
        return text, used, False

    # greedy fill in rank order; skip-not-truncate (§2.3): the first record
    # that does not fit WHOLE stops the fill. Slim fallback: the TOP record
    # may drop its context (a smaller valid record, never a cut).
    chosen: list[PackRecord] = []
    for rec in records:
        candidate = chosen + [rec]
        text, used, ok = _fixpoint(candidate, len(records) - len(candidate))
        if ok and used <= settings.budget:
            chosen = candidate
            continue
        if not chosen and len(rec.parts) > 1:
            slim = PackRecord(
                n=rec.n, hit=rec.hit,
                parts=[p for p in rec.parts if p["role"] == "hit"],
                note=rec.note,
                citation_ids=rec.citation_ids,
            )
            slim.text = material_text(slim.parts, slim.hit) if slim.parts else TEXTLESS_NOTE
            text, used, ok = _fixpoint([slim], len(records) - 1)
            if ok and used <= settings.budget:
                chosen = [slim]
                continue
        break

    # F8: a non-converged fixpoint drops the LAST record and redoes — the
    # footer never ships an unmeasured used-count while a record can go.
    skipped = len(records) - len(chosen)
    text, used, converged = _fixpoint(chosen, skipped)
    while not converged and chosen:
        chosen = chosen[:-1]
        skipped = len(records) - len(chosen)
        text, used, converged = _fixpoint(chosen, skipped)

    if not converged or used > settings.budget:
        raise BudgetTooSmall(
            f"budget_too_small: pack envelope requires {used} tokens; "
            f"budget is {settings.budget} ({token_mode})"
        )

    meta = {
        "schema": PACK_SCHEMA,
        "query": settings.query,
        "settings": {
            "scorer": settings.scorer,
            "z_gate": settings.z_gate,
            "limit": settings.limit,
            "context": settings.context,
            "budget": settings.budget,
            "token_mode": token_mode,
        },
        "index": {"line": index_line},
        "records": [_record_json(rec, backend, settings, count_of=count) for rec in chosen],
        "duplicates_folded": folded,
        "also_folded": also,
        "skipped_over_budget": skipped,
        "skipped_by_record_limit": capped,
        "used_tokens": used,
        "token_mode": token_mode,
        "fixpoint_converged": converged,
    }
    return text, meta


def _record_json(rec: PackRecord, backend, settings: PackSettings, *, count_of=None) -> dict:
    h = rec.hit
    out = {
        "n": rec.n,
        "segment_id": h.segment_id,
        "segments": [
            {"segment_id": p["segment_id"], "role": p["role"],
             "loc": p["loc"], "loc_end": p["loc_end"]}
            for p in rec.parts
        ],
        "path": h.path,
        "kind": h.kind,
        "loc": h.loc,
        "loc_end": h.loc_end,
        "score": round(h.score, 4),
        "z": None if h.z is None else round(h.z, 4),
        "cosine": None if h.cosine is None else round(h.cosine, 4),
        "coverage": round(h.coverage, 4),
        "context": [
            {"role": p["role"], "loc": p["loc"], "loc_end": p["loc_end"]}
            for p in rec.parts
            if p["role"] != "hit"
        ],
        "also_hits": [
            {"rank": o.rank, "score": round(o.score, 4),
             "path": o.path, "segment_id": o.segment_id,
             "loc": o.loc, "loc_end": o.loc_end}
            for o in rec.also_hits
        ],
        "text": rec.text,
        "tokens": count_of(rec.text) if count_of else tokens_of(rec.text, backend)[0],
    }
    if rec.note is not None:
        out["note"] = rec.note
    if settings.why and h.why is not None:
        out["why"] = h.why
    return out
