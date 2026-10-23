"""Mechanical pack regressions only; no models or tokenizer processes.

ByteCounter is a deterministic UTF-8 byte counter, NOT a model tokenizer.
The production interface calls its mode 'tokenize'; that name is not a claim
that these synthetic receipt counts predict real token costs or quality.
"""
from __future__ import annotations

import copy
import hashlib
import re
import socket
import subprocess

import pytest

from goldmine.citations import read_evidence, resolve_citation
from goldmine.pack import (
    MAX_RECORDS, NeighborIndex, PackSettings, build_pack_from_hits,
    collect_records, material_text, merge_parts,
)
from goldmine.search import Hit
from goldmine.store import FileRec, SegmentRec, Store

SEAM = " the shared warranty lasts exactly forty-five calendar days; "
TAIL = "payment is due within eighteen calendar days."


class ByteCounter:
    """Mechanical only: one UTF-8 byte per synthetic budget unit."""
    def count_tokens(self, texts):
        return [len(text.encode("utf-8")) for text in texts]


@pytest.fixture(autouse=True)
def no_external_process_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("mechanical proposal tests may not launch processes or use network")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "mechanical.sqlite")
    try:
        yield value
    finally:
        value.close()


def hit_for(path="/synthetic/source.txt", *, sid=1, kind="doc", loc=1, end=3, rank=1):
    return Hit(rank, 2.0, None, None, 1.0, kind, path, loc, end,
               "chunk", None, "content_indexed", "lexical", segment_id=sid)


def part(sid, loc, end, text, *, role="supporting", kind="chunk"):
    return {"segment_id": sid, "loc": loc, "loc_end": end,
            "text": text, "role": role, "kind": kind}


def add_source(store, name, specs, *, kind="doc", hit_index=0, supports=()):
    path = "/synthetic/" + name
    fid = store.upsert_file(FileRec(path=path, kind=kind, state="done",
                                   capability="content_indexed", sha256="a" * 64))
    store.replace_segments(fid, [
        SegmentRec(kind=item.get("kind", "chunk"), modality="none",
                   loc=item["loc"], loc_end=item["end"], text_for_fts=item.get("text"))
        for item in specs
    ])
    ids = [row[0] for row in store.db.execute(
        "SELECT id FROM segments WHERE file=? ORDER BY id", (fid,))]
    selected = specs[hit_index]
    hit = hit_for(path, sid=ids[hit_index], kind=kind,
                  loc=selected["loc"], end=selected["end"])
    hit.segment = selected.get("kind", "chunk")
    hit.supporting_segment_ids = [ids[i] for i in supports]
    return hit, ids


def overlapping_source(store, name="overlap.txt"):
    return add_source(store, name, [
        {"loc": 1, "end": 3, "text": "First original fact." + SEAM},
        {"loc": 3, "end": 5, "text": SEAM + TAIL},
        {"loc": 5, "end": 7, "text": TAIL + " Final original fact."},
    ], supports=(1, 2))


def pack(store, hits, *, budget=8000, context=0):
    return build_pack_from_hits(
        store, ByteCounter(), PackSettings(query="mechanical", scorer="lexical",
                                          context=context, budget=budget), hits,
        citation_id_of=lambda sid: read_evidence(store, sid).citation.citation_id,
    )


def assert_accounting(text, meta, hits, budget):
    assert meta["used_tokens"] == len(text.encode("utf-8")) <= budget
    assert meta["fixpoint_converged"] is True
    printed = re.search(r"used (\d+)/", text)
    assert printed and int(printed[1]) == meta["used_tokens"]
    assert (len(meta["records"]) + meta["duplicates_folded"] + meta["also_folded"]
            + meta["skipped_over_budget"] + meta["skipped_by_record_limit"]) == len(hits)
    for record in meta["records"]:
        assert record["tokens"] == len(record["text"].encode("utf-8"))


def test_supporting_overlap_run_dedups_and_preserves_each_full_literal_part(store):
    hit, ids = overlapping_source(store)
    neighbor = NeighborIndex(store)
    parts, _ = neighbor.parts_for(hit, 0)
    before = copy.deepcopy(parts)
    records, folded, also = collect_records(neighbor, [hit], 0)
    record = records[0]
    assert record.text.count("# stored evidence:") == 1
    assert record.text.startswith("# stored evidence: L1-7\n")
    assert record.text.count(SEAM) == 1 and record.text.count(TAIL) == 1
    assert [p["segment_id"] for p in record.parts] == ids
    assert record.parts == before == parts
    # The second fact crosses the trimmed prefix and retained suffix. An
    # inserted newline at the seam would fail this exact literal assertion.
    for item in parts:
        assert item["text"] in record.text
    assert folded == also == 0


def test_full_citations_locations_and_stored_readback_survive_dedup(store):
    hit, ids = overlapping_source(store)
    original = {sid: read_evidence(store, sid) for sid in ids}
    before_hash = hashlib.sha256(store.path.read_bytes()).hexdigest()
    text, meta = pack(store, [hit])
    assert_accounting(text, meta, [hit], 8000)
    assert meta["records"][0]["segments"] == [
        {"segment_id": sid, "role": "hit" if i == 0 else "supporting",
         "loc": 1 + i * 2, "loc_end": 3 + i * 2}
        for i, sid in enumerate(ids)
    ]
    for evidence in original.values():
        citation = evidence.citation
        assert text.count(citation.citation_id) == 1
        returned = resolve_citation(store, citation.citation_id)
        assert returned == evidence
        assert returned.text in meta["records"][0]["text"]
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before_hash


def test_disjoint_same_file_ranges_keep_gap_markers_and_repeated_boilerplate(store):
    hit, _ = add_source(store, "disjoint.txt", [
        {"loc": 1, "end": 3, "text": "First fact." + SEAM},
        {"loc": 30, "end": 35, "text": SEAM + " Last fact."},
    ], supports=(1,))
    text = collect_records(NeighborIndex(store), [hit], 0)[0][0].text
    assert text.count("# stored evidence:") == 2
    assert "# stored evidence: L1-3\n" in text and "# stored evidence: L30-35\n" in text
    assert "# stored evidence: L1-35" not in text
    assert text.count(SEAM) == 2
    assert "First fact." + SEAM + " Last fact." not in text


@pytest.mark.parametrize("middle", [
    {"loc": 1, "end": 1, "text": "Unselected real middle content."},
    {"loc": 1, "end": 1, "text": None, "kind": "vec"},
], ids=["unselected-fts", "vector-only-no-fts"])
def test_same_line_hidden_stored_segment_breaks_run_even_if_neighbor_cache_omits_it(store, middle):
    hit, ids = add_source(store, "same-line.txt", [
        {"loc": 1, "end": 1, "text": "First fact." + SEAM}, middle,
        {"loc": 1, "end": 1, "text": SEAM + " Last fact."},
    ], supports=(2,))
    neighbor = NeighborIndex(store)
    parts, _ = neighbor.parts_for(hit, 0)
    assert [p["segment_id"] for p in parts] == [ids[0], ids[2]]
    text = collect_records(neighbor, [hit], 0)[0][0].text
    assert text.count("# stored evidence:") == 2 and text.count(SEAM) == 2
    assert "First fact." + SEAM + " Last fact." not in text


@pytest.mark.parametrize("kind,segment", [("doc", "chunk"), ("code", "chunk"),
                                         ("webpage", "chunk"), ("image", "ocr")])
def test_known_line_domains_allow_proven_overlap(kind, segment):
    parts = [part(10, 1, 3, "first" + SEAM, role="hit", kind=segment),
             part(11, 3, 5, SEAM + "last", kind=segment)]
    text = material_text(parts, hit_for(kind=kind), source_order={10: 0, 11: 1})
    assert text.count(SEAM) == 1 and text.count("# stored evidence:") == 1
    assert all(p["text"] in text for p in parts)


@pytest.mark.parametrize("kind,first,second,order", [
    ("doc", part(10, 1, 3, "a" + SEAM, role="hit"), part(11, 3, 5, SEAM + "b"), None),
    ("doc", part(10, 1, 3, "a" + SEAM, role="hit"), part(11, 3, 5, SEAM + "b"), {10: 0}),
    ("doc", part(10, 1, 3, "a" + SEAM, role="hit"), part(11, 3, 5, SEAM + "b"), {10: 0, 11: 2}),
    ("doc", part(10, 1, 3, "a" + SEAM, role="hit"), part(11, 4, 5, SEAM + "b"), {10: 0, 11: 1}),
    ("doc", part(10, 1, 3, "a" + SEAM, role="hit"), part(11, None, 5, SEAM + "b"), {10: 0, 11: 1}),
    ("doc", part(10, 1, 5, "a" + SEAM, role="hit"), part(11, 3, 4, SEAM + "b"), {10: 0, 11: 1}),
    ("doc", part(10, 3, 5, "a" + SEAM, role="hit"), part(11, 1, 5, SEAM + "b"), {10: 0, 11: 1}),
    ("doc", part(10, 1, 3, "a" + SEAM, role="hit"), part(11, 3, 5, SEAM + "b", kind="ocr"), {10: 0, 11: 1}),
    ("doc", part(10, 1, 3, "a" + SEAM, role="hit", kind="page"), part(11, 3, 5, SEAM + "b", kind="page"), {10: 0, 11: 1}),
    ("audio", part(10, 1, 3, "a" + SEAM, role="hit"), part(11, 3, 5, SEAM + "b"), {10: 0, 11: 1}),
    ("video", part(10, 1, 3, "a" + SEAM, role="hit"), part(11, 3, 5, SEAM + "b"), {10: 0, 11: 1}),
    ("unknown", part(10, 1, 3, "a" + SEAM, role="hit"), part(11, 3, 5, SEAM + "b"), {10: 0, 11: 1}),
], ids=["no-proof", "incomplete-proof", "order-gap", "line-gap", "unknown-location",
        "nested-end", "backwards-start", "different-unit-kind", "page-units",
        "audio-seconds", "video-seconds", "unknown-domain"])
def test_unproven_runs_remain_explicitly_separate(kind, first, second, order):
    text = material_text([first, second], hit_for(kind=kind), source_order=order)
    assert text.count("# stored evidence:") == 2 and text.count(SEAM) == 2
    assert first["text"] in text and second["text"] in text


def test_pdf_page_groups_and_non_supporting_bytes_stay_unchanged():
    pages = [part(1, 3, 3, "page three" + SEAM, role="hit", kind="page"),
             part(2, 17, 17, SEAM + "page seventeen", kind="page")]
    expected = "# stored evidence: p.3\n" + pages[0]["text"] + "\n\n# stored evidence: p.17\n" + pages[1]["text"]
    assert material_text(pages, hit_for(kind="pdf"), source_order={1: 0, 2: 1}) == expected
    normal = [part(1, 1, 3, "normal" + SEAM, role="hit"),
              part(2, 3, 5, SEAM + "context", role="after")]
    expected_normal = "normal" + SEAM + "\ncontext"
    assert merge_parts(normal) == expected_normal
    assert material_text(normal, hit_for(), source_order={1: 0, 2: 1}) == expected_normal


@pytest.mark.parametrize("overlap", ["tiny", "長い同一の重なりテキストです。" * 3, "H" * 700])
def test_existing_floor_cap_and_unicode_overlap_remain_bounded(overlap):
    parts = [part(1, 1, 3, "prefix " + overlap, role="hit"),
             part(2, 3, 5, overlap + " suffix")]
    text = material_text(parts, hit_for(), source_order={1: 0, 2: 1})
    if len(overlap) < 24:
        assert text.count(overlap) == 2
    elif len(overlap) > 640:
        assert text.count("H") == 760  # only the existing 640-char cap may trim
    else:
        assert text.count(overlap) == 1
    assert all(p["text"] in text for p in parts)


def test_proven_adjacency_without_matching_seam_keeps_both_whole_parts():
    parts = [part(1, 1, 3, "Entire first independently stored paragraph.", role="hit"),
             part(2, 3, 5, "Entire second independently stored paragraph.")]
    text = material_text(parts, hit_for(), source_order={1: 0, 2: 1})
    assert text == "# stored evidence: L1-5\n" + parts[0]["text"] + "\n" + parts[1]["text"]


def test_joined_overlap_preserves_leading_and_trailing_stored_whitespace():
    parts = [part(1, 1, 3, " \t first " + SEAM, role="hit"),
             part(2, 3, 5, SEAM + " last \t ")]
    text = material_text(parts, hit_for(), source_order={1: 0, 2: 1})
    assert all(p["text"] in text for p in parts)


def test_newly_identical_material_preserves_every_contributing_source_citation(store):
    first, first_ids = overlapping_source(store, "three-parts.txt")
    second, second_ids = add_source(store, "two-parts.txt", [
        {"loc": 1, "end": 5, "text": "First original fact." + SEAM + TAIL},
        {"loc": 5, "end": 7, "text": TAIL + " Final original fact."},
    ], supports=(1,))
    second.rank = 2
    text, meta = pack(store, [first, second])
    assert_accounting(text, meta, [first, second], 8000)
    assert len(meta["records"]) == 2 and meta["duplicates_folded"] == 0
    assert meta["records"][0]["text"] == meta["records"][1]["text"]
    for sid in first_ids + second_ids:
        assert read_evidence(store, sid).citation.citation_id in text


def test_identical_legacy_presentation_does_not_fold_different_actual_gap_material(store):
    specs = [{"loc": 1, "end": 1, "text": "First fact." + SEAM},
             {"loc": 1, "end": 1, "text": SEAM + " Last fact."}]
    first, first_ids = add_source(store, "no-gap.txt", specs, supports=(1,))
    second, second_ids = add_source(store, "with-gap.txt", [specs[0],
        {"loc": 1, "end": 1, "text": None, "kind": "vec"}, specs[1]], supports=(2,))
    second.rank = 2
    text, meta = pack(store, [first, second])
    assert_accounting(text, meta, [first, second], 8000)
    assert len(meta["records"]) == 2 and meta["duplicates_folded"] == 0
    assert meta["records"][0]["text"] != meta["records"][1]["text"]
    for sid in first_ids + [second_ids[0], second_ids[2]]:
        assert read_evidence(store, sid).citation.citation_id in text
    assert read_evidence(store, second_ids[1]).citation.citation_id not in text


def test_existing_single_part_exact_duplicate_fold_and_citations_remain(store):
    spec = [{"loc": 1, "end": 1, "text": "Identical whole source fact."}]
    first, first_ids = add_source(store, "original-a.txt", spec)
    second, second_ids = add_source(store, "original-b.txt", spec)
    second.rank = 2
    text, meta = pack(store, [first, second])
    assert_accounting(text, meta, [first, second], 8000)
    assert len(meta["records"]) == 1 and meta["duplicates_folded"] == 1
    for sid in first_ids + second_ids:
        assert read_evidence(store, sid).citation.citation_id in text


def test_whole_cited_envelope_is_recounted_after_mechanical_dedup(store):
    hit, _ = overlapping_source(store)
    large_text, large_meta = pack(store, [hit])
    tight_budget = large_meta["used_tokens"] + 10
    text, meta = pack(store, [hit], budget=tight_budget)
    assert_accounting(text, meta, [hit], tight_budget)
    assert meta["records"][0]["text"] == large_meta["records"][0]["text"]
    assert len(meta["records"][0]["segments"]) == 3
    assert text.endswith(f"used {meta['used_tokens']}/{tight_budget} tokens (tokenize)\n")


def test_nonfitting_whole_seed_is_skipped_before_later_small_hit(store):
    first, _ = overlapping_source(store)
    huge, _ = add_source(store, "huge.txt", [{"loc": 1, "end": 1, "text": "Oversized complete source." * 1000}])
    last, _ = add_source(store, "later.txt", [{"loc": 1, "end": 1, "text": "Later tiny complete source."}])
    huge.rank, last.rank = 2, 3
    hits = [first, huge, last]
    text, meta = pack(store, hits, budget=1800)
    assert_accounting(text, meta, hits, 1800)
    assert [r["path"] for r in meta["records"]] == [first.path, last.path]
    assert meta["records"][0]["text"] == collect_records(NeighborIndex(store), [first], 0)[0][0].text
    assert meta["skipped_over_budget"] == 1
    assert "Oversized complete source." not in text and "Later tiny complete source." in text


def test_top_slim_fallback_keeps_whole_hit_and_only_its_citation(store):
    hit, ids = add_source(store, "slim.txt", [
        {"loc": 1, "end": 1, "text": "Entire short primary source sentence."},
        {"loc": 100, "end": 120, "text": "Entire optional context." * 1000},
    ], supports=(1,))
    text, meta = pack(store, [hit], budget=1500)
    assert_accounting(text, meta, [hit], 1500)
    assert meta["records"][0]["text"] == "Entire short primary source sentence."
    assert [p["segment_id"] for p in meta["records"][0]["segments"]] == [ids[0]]
    assert read_evidence(store, ids[0]).citation.citation_id in text
    assert read_evidence(store, ids[1]).citation.citation_id not in text


def test_oversized_single_part_is_omitted_without_prefix_truncation(store):
    hit, _ = add_source(store, "huge-only.txt", [{"loc": 1, "end": 1, "text": "Never cut this source." * 1000}])
    text, meta = pack(store, [hit], budget=1500)
    assert_accounting(text, meta, [hit], 1500)
    assert meta["records"] == [] and meta["skipped_over_budget"] == 1
    assert "Never cut this source." not in text


def test_record_cap_and_support_source_order_stay_internal(store):
    hits = []
    for i in range(MAX_RECORDS + 1):
        hit, _ = add_source(store, f"source-{i}.txt", [{"loc": 1, "end": 1, "text": f"Independent whole source {i}."}])
        hit.rank = i + 1
        hits.append(hit)
    text, meta = pack(store, hits, budget=32768)
    assert_accounting(text, meta, hits, 32768)
    assert len(meta["records"]) == MAX_RECORDS and meta["skipped_by_record_limit"] == 1
    assert "source_order" not in str(meta) and "source_order" not in text
    hit, _ = overlapping_source(store, "private-proof.txt")
    parts, _ = NeighborIndex(store).parts_for(hit, 0)
    assert all(set(p) == {"role", "loc", "loc_end", "segment_id", "kind", "text"} for p in parts)
