"""Human/JSON renderers for ingest + search output (v2 F1: extracted from cli.py).

Pure functions: data in, strings out — no store access, no exit codes, no
argparse. `render_hits` and `render_ingest_summary` are re-exported from
goldmine.cli (the pinned test import surface: test_search_v2_output imports
render_hits from cli) and lazily imported by init.py; this module exists so
command bodies stop owning rendering.

Do-not-touch (repo invariant): render_hits' fold thresholds (0.5× strong/weak
cut, duplicate folding) are DigUp-derived, eval-pinned behavior.
"""

from __future__ import annotations

import json

from .search import QueryFilters


def render_ingest_summary(report, json_mode: bool) -> str:
    if json_mode:
        return json.dumps(report.to_json(), indent=2, ensure_ascii=False)
    files = report.to_json()["files"]
    urls = sum(1 for r in report.roots if r.startswith(("http://", "https://")))
    lines = [
        f"ingest: {len(report.roots)} roots, {files['scanned']} files"
        + (f", {urls} URLs" if urls else "")
        + ("  (dry run)" if report.dry_run else "")
    ]

    def kinds_line(capability: str) -> str:
        order = ("doc", "pdf", "code", "webpage", "image", "audio", "video")
        parts = [f"{kind} {report.by_kind[kind][capability]}"
                 for kind in order if report.by_kind.get(kind, {}).get(capability)]
        return f"({', '.join(parts)})" if parts else ""

    def reasons_line(reasons: list[dict]) -> str:
        parts = [f"{r['reason']} {r['count']}" for r in reasons]
        return f"({' · '.join(parts)})" if parts else ""

    line = f"  content-indexed  {files['content_indexed']}"
    if kinds_line("content_indexed"):
        line += "   " + kinds_line("content_indexed")
    lines.append(line)
    line = f"  metadata-only    {files['metadata_only']}"
    if report.metadata_reasons:
        line += "   " + reasons_line(report.metadata_reasons)
    lines.append(line)
    line = f"  unsupported      {files['unsupported']}"
    if report.unsupported_reasons:
        line += "   " + reasons_line(report.unsupported_reasons)
    lines.append(line)
    line = f"  failed           {files['failed']}"
    if report.failures:
        parts = [f"{f['path']}: {f['detail']}" for f in report.failures[:3]]
        if len(report.failures) > 3:
            parts.append(f"+{len(report.failures) - 3} more")
        line += "   (" + " · ".join(parts) + ")"
    lines.append(line)
    lines.append(
        f"  skipped-unchanged {files['unchanged']} · deleted {files['deleted']}"
        f" · partial reads {files['partial']}"
    )
    if not report.dry_run:
        fp = report.fingerprint
        fp_str = ""
        if fp:
            fp_str = (
                f" · fingerprint {fp['backend']}/{fp['model_id']}/{fp['dim']}/{fp['id']}"
            )
        lines.append(
            f"vectors: {report.vectors:,} · segments: {report.segments:,}{fp_str}"
        )
    for notice in report.notices:
        lines.append(f"notice: [{notice['code']}] {notice['message']}")
    return "\n".join(lines)


def render_hits(
    hits,
    json_mode: bool,
    excerpt_chars: int = 240,
    *,
    why: bool = False,
    raw: bool = False,
    filters: QueryFilters | None = None,
    context_of: dict | None = None,
) -> str:
    """Render search hits. Human mode folds: near-duplicates under their
    primary («+N duplicates»), hits below 0.5× the top score under a trailing
    «weaker matches» line (DigUp strongCount, Searcher.swift:278-281).
    --json keeps every hit; additive keys appear only when they carry data:
    `why` (--why), `excerpt_marked` (--raw), `duplicate_of` (when folded),
    `filters_applied` (when filters were parsed from the query),
    `context`/`context_note` (--context N, v2.1 R2: neighbor spans with texts,
    or the honest no-expansion note).

    `context_of` maps rank -> (spans_with_text, note): spans are
    [{role: before|after, loc, loc_end, segment_id, text}] in position order,
    note is the no-expansion reason ("name hit" / "vector-only hit (no text)")
    or None. Human mode renders one note line under the hit; --json attaches
    the payload keys. Absent (None) = --context 0: zero new keys, byte-stable
    output."""
    if json_mode:
        out = []
        for h in hits:
            d = {
                "rank": h.rank,
                "score": round(h.score, 4),
                "z": None if h.z is None else round(h.z, 4),
                "cosine": None if h.cosine is None else round(h.cosine, 4),
                "coverage": round(h.coverage, 4),
                "kind": h.kind,
                "path": h.path,
                "loc": h.loc,
                "loc_end": h.loc_end,
                "segment": h.segment,
                "excerpt": h.excerpt,
                "file_status": h.file_status,
                "scorer": h.scorer,
            }
            if raw and h.excerpt_marked:
                d["excerpt_marked"] = h.excerpt_marked
            if why and h.why is not None:
                d["why"] = _round_json(h.why)
            if h.duplicate_of is not None:
                d["duplicate_of"] = h.duplicate_of
            if filters is not None and bool(filters):
                d["filters_applied"] = dict(filters.echo)
            if context_of is not None and h.rank in context_of:
                spans, note = context_of[h.rank]
                d["context"] = [
                    {"role": s["role"], "loc": s["loc"], "loc_end": s["loc_end"],
                     "text": s["text"]}
                    for s in spans if s["role"] != "hit"
                ]
                if note is not None:
                    d["context_note"] = note
            out.append(d)
        return json.dumps(out, indent=2, ensure_ascii=False)
    if not hits:
        return ""
    from .util import excerpt as make_excerpt
    # lazy: pack.py imports this module's _render_loc (cycle break)
    from .pack import format_context_note

    # weaker-matches fold (item 15.2b): the strong prefix is every hit scoring
    # >= 0.5× the top (DigUp strongCount: cut at the FIRST hit below; top == 0
    # folds nothing). Human format only — --json keeps the full list.
    top = hits[0].score
    strong, weaker = hits, []
    if top > 0:
        cut = 0
        for h in hits:
            if h.score < top * 0.5:
                break
            cut += 1
        strong, weaker = hits[:cut], hits[cut:]
    dup_under: dict[int, int] = {}
    for h in hits:
        if h.duplicate_of is not None:
            dup_under[h.duplicate_of] = dup_under.get(h.duplicate_of, 0) + 1

    lines = []
    for h in strong:
        if h.z is None:
            z_str = "no-vector" if h.file_status == "metadata_only" else "—"
        else:
            z_str = f"{h.z:.2f}"
        loc_str = _render_loc(h.kind, h.loc, h.loc_end)
        lines.append(
            f"{h.rank}. score {h.score:.2f}  z {z_str}  cov {h.coverage:.2f}   "
            f"{h.kind:<6} {h.path}{'  ' + loc_str if loc_str else ''}"
        )
        if h.excerpt_marked:
            # «»-marked density-window snippet, already width-capped
            body = h.excerpt_marked
        else:
            body = h.excerpt or ""
            if not excerpt_chars:
                body = ""  # 0 disables excerpts entirely (P3-13)
            elif len(body) > excerpt_chars:
                body = make_excerpt(body, excerpt_chars)
        if body:
            lines.append(f'   "{body}"')
        if why and h.why is not None:
            lines.append(f"   {_render_why(h)}")
        if context_of is not None and h.rank in context_of:
            spans, note = context_of[h.rank]
            note_line = format_context_note(h.kind, spans, note)
            if note_line:
                lines.append(note_line)
        n_dups = dup_under.get(h.rank, 0)
        if n_dups:
            lines.append(f"   +{n_dups} duplicate{'' if n_dups == 1 else 's'}")
    if weaker:
        lines.append(f"… +{len(weaker)} weaker matches")
    return "\n".join(lines)


def _round_json(value):
    """Round floats for stable --json output; dicts/lists recursively."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _round_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_json(v) for v in value]
    return value


def _render_why(h) -> str:
    """One compact per-hit confidence line (§5.8): the additive score parts,
    then the non-scorable context (bm25 rank, lookup weights, segment margin)."""
    w = h.why or {}
    parts = w.get("score_parts") or {}
    terms = []
    if "z" in parts:
        terms.append(f"{w.get('w_m', 0):g}·z {h.z:.2f}")
    if "cov" in parts:
        terms.append(f"{w.get('w_c', 0):g}·cov^1.5 {h.coverage:.2f}")
    if parts.get("kind"):
        terms.append(f"kind {h.kind} 1.0")
    if "rrf" in parts:
        terms.append(f"rrf {parts['rrf']:.4f}")
    line = " + ".join(terms) if terms else "score"
    line += f" = {h.score:.2f}"
    extras = []
    if w.get("bm25_rank"):
        extras.append(f"bm25 #{w['bm25_rank']}")
    if w.get("lookup"):
        extras.append("lookup weights")
    if w.get("kind_hint"):
        extras.append("hints " + ",".join(w["kind_hint"]))
    margin = w.get("segment_margin_z")
    if margin is not None:
        extras.append(f"seg-margin {margin:.2f}σ")
    if extras:
        line += " · " + " · ".join(extras)
    return f"why: {line}"


def _render_loc(kind: str, loc, loc_end) -> str:
    if loc is None:
        return ""
    if kind == "pdf":
        if loc_end is not None and loc_end != loc:
            return f"p.{int(loc)}-{int(loc_end)}"
        return f"p.{int(loc)}"
    if kind in ("audio", "video"):
        if loc_end is not None:
            return f"{loc:g}s-{loc_end:g}s"
        return f"{loc:g}s"
    if loc_end is not None and loc_end != loc:
        return f"L{int(loc)}-{int(loc_end)}"
    return f"L{int(loc)}"
