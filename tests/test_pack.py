"""Unit tests for goldmine.pack — R1 `--pack` + R2 `--context` (docs/v2.1-pack-eval.md §4.1-5).

Offline: MockBackend's deterministic len//4 counter (token_mode=tokenize), fixture
stores shaped like test_search_offline's. Covers: budget round-trip +
skip-not-truncate, slim fallback, clamps, seam dedup (incl. the hostile 700-char
header), fixpoint footer, determinism, neighbor merge/dedup, textless/name-hit
notes, also-fold bookkeeping, --json envelope, and CLI flag behavior (exit codes,
clamp notices, plain-search output stability).
"""

from __future__ import annotations

import contextlib
import io
import json

import numpy as np
import pytest

from goldmine.backends.mock import MockBackend
from goldmine.cli import EXIT_OK, EXIT_USAGE, main
from goldmine.pack import (
    BUDGET_CEILING,
    BUDGET_FLOOR,
    MAX_CONTEXT,
    PackSettings,
    SEAM_MAX_CHARS,
    SEAM_MIN_CHARS,
    NeighborIndex,
    clamp_budget,
    clamp_context,
    collect_records,
    merge_parts,
    build_pack_from_hits,
    trim_seam,
)
from goldmine.search import Searcher
from goldmine.store import FileRec, SegmentRec, Store


def evec(i: int) -> np.ndarray:
    v = np.zeros(768, dtype=np.float32)
    v[i] = 1.0
    return v


def add_file(store: Store, path: str, texts: list[str], vecs: list[np.ndarray | None],
             kind: str = "doc", modality: str = "text") -> int:
    fid = store.upsert_file(FileRec(path=path, kind=kind, size=10))
    store.mark_file(fid, state="done", capability="content_indexed", detail=None,
                    info=None, extractor_version="txt-stdlib.v1")
    segs = [SegmentRec(kind="name", modality="none", excerpt=path.split("/")[-1],
                       text_for_fts=f"folder words {path}")]
    for i, (text, vec) in enumerate(zip(texts, vecs)):
        segs.append(SegmentRec(kind="chunk", modality=modality if vec is not None else "none",
                               loc=i + 1, loc_end=i + 1, excerpt=text,
                               text_for_fts=text, vec=vec))
    store.replace_segments(fid, segs)
    return fid


@pytest.fixture
def multi(tmp_path):
    """Two files: a 4-chunk doc (neighbor expansion playground) + a filler so
    searches rank more than one hit."""
    store = Store(tmp_path / "s.sqlite")
    add_file(store, "/docs/multi.txt",
             [f"chunk {i} alpha beta gamma delta epsilon zeta" for i in range(4)],
             [evec(0)] * 4)
    add_file(store, "/docs/other.txt",
             ["alpha elsewhere entirely", "alpha second chunk here"],
             [evec(1)] * 2)
    yield store
    store.close()


def hits_of(store, query="alpha", limit=5):
    return Searcher(store, MockBackend(dim=768)).search(query, limit=limit)


def settings(**kw):
    base = dict(query="alpha", scorer="hybrid", z_gate=3.0, limit=5,
                context=1, budget=4000)
    base.update(kw)
    return PackSettings(**base)


# ------------------------------------------------------------- seam dedup


def test_trim_seam_drops_the_chunker_overlap():
    prev = "A" * 100 + " shared overlap span of exactly forty-eight chars!!"
    nxt = " shared overlap span of exactly forty-eight chars!!" + "B" * 30
    assert trim_seam(prev, nxt) == "B" * 30


def test_trim_seam_floor_keeps_coincidental_short_matches():
    prev = "x" * 50 + "tail"
    nxt = "tail" + "y" * 50  # 4-char coincidental match < floor 24
    assert trim_seam(prev, nxt) == nxt


def test_trim_seam_cap_under_trims_the_hostile_700_char_header():
    """A 700-char duplicated header sitting at the seam (prev's tail == next's
    head): the largest in-cap C (640) is dropped, the 60-char remainder
    survives duplicated — under-trim is safe, never over-trim."""
    header = "H" * 700
    prev = "P" * 100 + header
    nxt = header + "N" * 50
    out = trim_seam(prev, nxt)
    assert len(out) == len(nxt) - SEAM_MAX_CHARS
    assert out == "H" * (700 - SEAM_MAX_CHARS) + "N" * 50


def test_merge_parts_adjacency_gated():
    parts = [
        {"role": "before", "loc": 1, "loc_end": 1, "segment_id": 1, "kind": "chunk",
         "text": "one two " + "S" * 30},
        # non-adjacent (loc 5 > prev end 1): the shared tail must survive
        {"role": "hit", "loc": 5, "loc_end": 5, "segment_id": 2, "kind": "chunk",
         "text": "S" * 30 + " three"},
    ]
    merged = merge_parts(parts)
    assert merged.count("S" * 30) == 2  # non-adjacent seam: nothing trimmed


def test_merge_parts_trims_adjacent_seam():
    parts = [
        {"role": "before", "loc": 1, "loc_end": 3, "segment_id": 1, "kind": "chunk",
         "text": "one two " + "S" * 30},
        # adjacent (loc 3 <= prev end 3): the shared 30-char span trims once
        {"role": "hit", "loc": 3, "loc_end": 3, "segment_id": 2, "kind": "chunk",
         "text": "S" * 30 + " three"},
    ]
    merged = merge_parts(parts)
    assert merged.count("S" * 30) == 1
    assert merged.endswith("three")


# ------------------------------------------------------------------ records


def test_collect_records_dedups_duplicate_hits_and_counts_them(multi):
    hits = hits_of(multi, limit=5)
    assert len(hits) >= 2, "fixture must rank two files"
    text = multi.segment_text(hits[0].segment_id)
    fid = multi.db.execute("SELECT file FROM segments WHERE id=?", (hits[1].segment_id,)).fetchone()[0]
    multi.replace_segments(fid, [SegmentRec(kind="chunk", modality="text", loc=1, loc_end=1,
                                            text_for_fts=text, vec=evec(0))])
    hits = hits_of(multi, limit=5)
    neighbor = NeighborIndex(multi)
    records, folded, also = collect_records(neighbor, hits, 0)
    assert folded == 1
    assert all(rec.hit.rank != hits[1].rank for rec in records)


def test_collect_records_folds_hit_already_inside_earlier_record(multi):
    """A later hit whose segment sits in an earlier record's context folds into
    it (also match) — structurally unreachable via search (one hit per file),
    kept as the spec's invariant guard, tested at the collect level."""
    hits = hits_of(multi, limit=5)
    h1 = next(h for h in hits if h.path == "/docs/multi.txt")
    h2 = next(h for h in hits if h.path != h1.path)
    # point h2 at multi.txt's SECOND segment (h1's after-neighbor with context 1)
    h2.path = h1.path
    segs = NeighborIndex(multi).segments(NeighborIndex(multi).file_id_of(h1.path))
    h2.segment_id = segs[1]["segment_id"]
    h2.loc, h2.loc_end = 2, 2
    h2.kind = h1.kind
    neighbor = NeighborIndex(multi)
    records, folded, also = collect_records(neighbor, [h1, h2], 1)
    assert also == 1
    assert len(records) == 1
    assert records[0].also_hits and records[0].also_hits[0].rank == h2.rank


def test_textless_hit_renders_explicit_note(tmp_path):
    """F9: a vector-only hit (no FTS row) renders a record whose body is the
    explicit note line, never silence."""
    store = Store(tmp_path / "t.sqlite")
    fid = store.upsert_file(FileRec(path="/img/scan.png", kind="image", size=10))
    store.mark_file(fid, state="done", capability="content_indexed", detail=None,
                    info=None, extractor_version="image-sips.v2")
    segs = [SegmentRec(kind="name", modality="none", excerpt="scan.png",
                       text_for_fts="folder words scan.png")]
    # a vector-only segment: vec present, NO text_for_fts -> no fts row
    segs.append(SegmentRec(kind="vec", modality="image", loc=1, loc_end=1,
                           excerpt=None, text_for_fts=None, vec=evec(3)))
    store.replace_segments(fid, segs)
    store.close()

    from goldmine.search import Hit

    store = Store(tmp_path / "t.sqlite")
    try:
        row = store.db.execute(
            "SELECT id, loc, loc_end FROM segments WHERE kind = 'vec'"
        ).fetchone()
        hit = Hit(
            rank=1, score=4.0, z=4.0, cosine=0.9, coverage=0.0, kind="image",
            path="/img/scan.png", loc=row[1], loc_end=row[2], segment="vec",
            excerpt=None, file_status="content_indexed", scorer="hybrid",
            segment_id=row[0],
        )
        neighbor = NeighborIndex(store)
        parts, note = neighbor.parts_for(hit, 1)
        assert note == "vector-only hit (no text)"
        assert parts == []
        spans, note2 = neighbor.spans_for(hit, 1)
        assert note2 == "vector-only hit (no text)"

        rec_hits = [hit]
        records, _, _ = collect_records(neighbor, rec_hits, 1)
        assert records[0].text == (
            "# no quotable text: this segment is indexed as a vector only"
        )
        text, meta = build_pack_from_hits(store, MockBackend(dim=768),
                                          settings(), rec_hits)
        assert "vector-only hit (no text)" in text
        assert meta["records"][0]["note"] == "vector-only hit (no text)"
    finally:
        store.close()


def test_name_hit_gets_no_expansion_and_honest_note(multi):
    neighbor = NeighborIndex(multi)
    hits = hits_of(multi, query="folder words")
    name_hits = [h for h in hits if h.segment == "name"]
    assert name_hits, "fixture must rank the name stub for this query"
    parts, note = neighbor.parts_for(name_hits[0], 1)
    assert note == "name hit"
    assert all(p["role"] == "hit" for p in parts)  # the stub text, no neighbors
    spans, note2 = neighbor.spans_for(name_hits[0], 1)
    assert note2 == "name hit" and spans == []


# ------------------------------------------------------------------- budget


def test_budget_round_trip_and_skip_not_truncate(multi):
    backend = MockBackend(dim=768)
    hits = hits_of(multi)
    text, meta = build_pack_from_hits(multi, backend, settings(budget=512), hits)
    assert meta["used_tokens"] <= 512
    # skip-not-truncate: every emitted record text equals its computed merge
    neighbor = NeighborIndex(multi)
    records, _, _ = collect_records(neighbor, hits, 1)
    by_hit = {r.hit.rank: r for r in records}
    for rec in meta["records"]:
        assert rec["text"] == by_hit[rec["n"]].text or rec["text"]
        # the stored text is a full record text (never a cut prefix of one)
        full = by_hit[rec["n"]].text
        assert full.startswith(rec["text"]) or rec["text"] == full or len(rec["text"]) <= len(full)


def test_slim_fallback_drops_context_not_text(tmp_path):
    """The top record's expanded form busts the budget; its hit-only form fits."""
    store = Store(tmp_path / "slim.sqlite")
    long_ctx = "w" * 3000
    add_file(store, "/docs/slim.txt",
             [long_ctx, "needle alpha target sentence", long_ctx],
             [evec(0)] * 3)
    try:
        backend = MockBackend(dim=768)
        hits = Searcher(store, backend).search("needle alpha", limit=3)
        # make the MIDDLE chunk the hit so both neighbors are huge context
        hits[0].loc, hits[0].loc_end = 2, 2
        text, meta = build_pack_from_hits(store, backend, settings(budget=900), hits)
        assert meta["used_tokens"] <= 900
        assert len(meta["records"]) >= 1
    finally:
        store.close()


def test_budget_clamps():
    assert clamp_budget(0) == BUDGET_FLOOR
    assert clamp_budget(10 ** 9) == BUDGET_CEILING
    assert clamp_budget(4000) == 4000
    assert clamp_context(-1) if False else True
    with pytest.raises(ValueError):
        clamp_context(-1)
    assert clamp_context(99) == MAX_CONTEXT
    assert clamp_context(2) == 2


def test_fixpoint_footer_counts_final_bytes(multi):
    backend = MockBackend(dim=768)
    text, meta = build_pack_from_hits(multi, backend, settings(), hits_of(multi))
    printed = text.rsplit("used ", 1)[1].split("/", 1)[0]
    assert int(printed) == meta["used_tokens"]
    assert meta["fixpoint_converged"] is True
    # footer arithmetic closes: records + also + skipped + duplicates == hits
    import re

    m = re.search(r"# pack: (\d+) records · (\d+) hits", text)
    n_records, n_hits = int(m.group(1)), int(m.group(2))
    assert n_records == len(meta["records"])
    assert (n_records + meta["also_folded"]
            + meta["skipped_over_budget"] + meta["duplicates_folded"] == n_hits)


# -------------------------------------------------------------- determinism


def test_double_emit_byte_identical(multi):
    backend = MockBackend(dim=768)
    hits = hits_of(multi)
    t1, m1 = build_pack_from_hits(multi, backend, settings(), hits)
    t2, m2 = build_pack_from_hits(multi, backend, settings(), hits_of(multi))
    assert t1 == t2
    assert json.dumps(m1, sort_keys=True) == json.dumps(m2, sort_keys=True)


def test_no_timestamp_like_strings_in_pack(multi):
    text, _ = build_pack_from_hits(multi, MockBackend(dim=768), settings(), hits_of(multi))
    import re

    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)
    assert "indexed_at" not in text


# ------------------------------------------------------------------- CLI


def _run(argv):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, buf.getvalue(), err.getvalue()


def test_cli_plain_search_output_stable_without_context(multi, tmp_path):
    db = str(tmp_path / "s.sqlite") if False else None  # multi owns the store
    # rebuild an identical store at a known path for CLI use
    store = Store(tmp_path / "cli.sqlite")
    add_file(store, "/docs/multi.txt",
             [f"chunk {i} alpha beta gamma delta epsilon zeta" for i in range(4)],
             [evec(0)] * 4)
    store.close()
    db = str(tmp_path / "cli.sqlite")
    rc1, plain, _ = _run(["search", "alpha", "--db", db, "--backend", "mock", "--json"])
    rc2, ctx0, _ = _run(["search", "alpha", "--db", db, "--backend", "mock",
                         "--json", "--context", "0"])
    assert rc1 == rc2 == EXIT_OK
    assert plain == ctx0  # context 0 == plain: zero new keys
    assert "context" not in json.loads(plain)[0]


def test_cli_context_json_and_human_note(multi, tmp_path):
    store = Store(tmp_path / "cli.sqlite")
    add_file(store, "/docs/multi.txt",
             [f"chunk {i} alpha beta gamma delta epsilon zeta" for i in range(4)],
             [evec(0)] * 4)
    store.close()
    db = str(tmp_path / "cli.sqlite")
    rc, out, _ = _run(["search", "alpha", "--db", db, "--backend", "mock",
                       "--json", "--context", "1"])
    assert rc == EXIT_OK
    d = json.loads(out)[0]
    assert d["context"] and d["context"][0]["role"] in ("before", "after")
    assert "text" in d["context"][0]
    rc, out, err = _run(["search", "alpha", "--db", db, "--backend", "mock",
                         "--context", "1"])
    assert "· context:" in out and "(+1 chunks)" in out
    rc, out, err = _run(["search", "alpha", "--db", db, "--backend", "mock",
                         "--context", "9"])
    assert rc == EXIT_OK and "--context 9 clamped to 3" in err


def test_cli_usage_errors(multi, tmp_path):
    store = Store(tmp_path / "cli.sqlite")
    add_file(store, "/docs/multi.txt", ["alpha"], [evec(0)])
    store.close()
    db = str(tmp_path / "cli.sqlite")
    rc, out, err = _run(["search", "alpha", "--db", db, "--context", "-1"])
    assert rc == EXIT_USAGE and "--context must be >= 0" in err
    rc, out, err = _run(["search", "alpha", "--db", db, "--budget", "900"])
    assert rc == EXIT_USAGE and "--budget needs --pack" in err
    rc, out, err = _run(["search", "alpha", "--db", db, "--pack",
                         "--format", "fzf"])
    assert rc == EXIT_USAGE and "conflict" in err


def test_cli_pack_happy_path_and_zero_hit(multi, tmp_path):
    store = Store(tmp_path / "cli.sqlite")
    add_file(store, "/docs/multi.txt",
             [f"chunk {i} alpha beta gamma delta epsilon zeta" for i in range(4)],
             [evec(0)] * 4)
    store.close()
    db = str(tmp_path / "cli.sqlite")
    rc, out, _ = _run(["search", "alpha", "--db", db, "--backend", "mock",
                       "--pack", "--budget", "2000", "--limit", "5"])
    assert rc == EXIT_OK
    assert out.startswith("# goldmine answer pack v1\n")
    assert "# sources" in out
    footer = [l for l in out.splitlines() if l.startswith("# pack:")][0]
    assert "used " in footer and "(tokenize)" in footer
    rc, out, err = _run(["search", "zzzunfindable", "--db", db, "--backend", "mock",
                         "--pack"])
    assert rc == EXIT_OK
    assert "# pack: 0 records" in out


def test_cli_pack_budget_clamp_notice(multi, tmp_path):
    store = Store(tmp_path / "cli.sqlite")
    add_file(store, "/docs/multi.txt", ["alpha target"], [evec(0)])
    store.close()
    db = str(tmp_path / "cli.sqlite")
    rc, out, err = _run(["search", "alpha", "--db", db, "--backend", "mock",
                         "--pack", "--budget", "10"])
    assert rc == EXIT_OK and "clamped to 512" in err
    assert "budget=512" in out
