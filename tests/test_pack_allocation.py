"""Whole-envelope/source mechanics; synthetic byte counts are not model costs."""
from __future__ import annotations

from dataclasses import replace
import re
import socket
import subprocess

import pytest

from goldmine.chunker import est_tokens
from goldmine.citations import read_evidence, resolve_citation
from goldmine.deadline import RequestTimeout, request_cancellation
from goldmine.pack import (BudgetTooSmall, FIXPOINT_ROUNDS, MAX_RECORDS, NeighborIndex,
                           PackRecord, PackSettings, build_pack_from_hits, material_text)
from goldmine.pack_allocation import _fold
from goldmine.search import Hit
from goldmine.store import FileRec, SegmentRec, Store


class ByteCounter:
    def __init__(self):
        self.calls = []

    def count_tokens(self, texts):
        self.calls.extend(texts)
        return [len(text.encode("utf-8")) for text in texts]


@pytest.fixture(autouse=True)
def no_process_network_or_vectors(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pack allocation invoked process/network/vector loading")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(Store, "load_vectors", forbidden)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "allocation.sqlite")
    yield value
    value.close()


def source(store, name, texts, *, primary=0, supports=(), rank=1, kind="doc", positions=None):
    path = "/synthetic/" + name
    fid = store.upsert_file(FileRec(path=path, state="done", capability="content_indexed", kind=kind,
        sha256="c" * 64, extractor_version="asr-fixture" if kind == "audio" else "fixture"))
    locations = positions or [(i * 10 + 1, i * 10 + 5) for i in range(len(texts))]
    store.replace_segments(fid, [SegmentRec(kind="audio" if kind == "audio" else "chunk", modality="none",
        loc=start, loc_end=end, text_for_fts=text) for text, (start, end) in zip(texts, locations)])
    ids = [r[0] for r in store.db.execute("SELECT id FROM segments WHERE file=? ORDER BY id", (fid,))]
    hit = Hit(rank, 2., None, None, 1., kind, path, *locations[primary],
        "audio" if kind == "audio" else "chunk", None, "content_indexed", "lexical",
        segment_id=ids[primary], supporting_segment_ids=[ids[i] for i in supports])
    return hit, ids


def cid(store, sid):
    return read_evidence(store, sid).citation.citation_id


def pack(store, hits, *, context=0, budget=8000, backend="bytes", query="synthetic mechanics"):
    counter = ByteCounter() if backend == "bytes" else backend
    text, meta = build_pack_from_hits(store, counter, PackSettings(query=query, context=context, budget=budget),
        hits, citation_id_of=lambda sid: cid(store, sid))
    expected = est_tokens(text) if meta["token_mode"] == "estimate" else len(text.encode("utf-8"))
    assert meta["used_tokens"] == expected <= budget
    assert meta["fixpoint_converged"]
    assert int(re.search(r"used (\d+)/", text)[1]) == expected
    assert len(meta["records"]) + meta["duplicates_folded"] + meta["also_folded"] + meta["skipped_over_budget"] + meta["skipped_by_record_limit"] == len(hits)
    assert [r["n"] for r in meta["records"]] == list(range(1, len(meta["records"]) + 1))
    return text, meta


def test_oversized_middle_seed_does_not_block_later_small_primary(store):
    first, one = source(store, "first.txt", ["Entire first source fact."])
    huge, two = source(store, "oversized.txt", ["Oversized whole source. " * 1000], rank=2)
    last, three = source(store, "last.txt", ["Entire later source fact."], rank=3)
    text, meta = pack(store, [first, huge, last], budget=2200)
    assert [r["path"] for r in meta["records"]] == [first.path, last.path]
    assert meta["skipped_over_budget"] == 1
    assert all(cid(store, sid) in text for sid in one + three)
    assert all(cid(store, sid) not in text for sid in two)


def test_optional_first_support_cannot_starve_another_primary(store):
    first, one = source(store, "first.txt", ["Primary one.", "Optional whole support." * 1000], supports=(1,))
    second, two = source(store, "second.txt", ["Primary two."], rank=2)
    text, meta = pack(store, [first, second], budget=2200)
    assert len(meta["records"]) == 2 and meta["skipped_over_budget"] == 0
    assert [p["segment_id"] for p in meta["records"][0]["segments"]] == [one[0]]
    assert cid(store, one[1]) not in text and cid(store, two[0]) in text


def test_support_rounds_precede_neighbor_expansion(store):
    first, one = source(store, "first.txt", ["NEIGHBOR_ONE", "PRIMARY_ONE", "SUPPORT_ONE"], primary=1, supports=(2,))
    second, two = source(store, "second.txt", ["NEIGHBOR_TWO", "PRIMARY_TWO", "SUPPORT_TWO"], primary=1, supports=(2,), rank=2)
    counter = ByteCounter()
    text, meta = pack(store, [first, second], context=1, budget=12000, backend=counter)
    first_seen = lambda token: next(i for i, value in enumerate(counter.calls) if token in value)
    assert first_seen("PRIMARY_ONE") < first_seen("SUPPORT_ONE")
    assert first_seen("PRIMARY_TWO") < first_seen("SUPPORT_ONE")
    assert first_seen("SUPPORT_TWO") < min(first_seen("NEIGHBOR_ONE"), first_seen("NEIGHBOR_TWO"))
    for record, ids in zip(meta["records"], [one, two]):
        assert {p["segment_id"] for p in record["segments"]} == set(ids)
        for sid in ids:
            assert read_evidence(store, sid).text in record["text"]
            assert cid(store, sid) in text


@pytest.mark.parametrize("kind,locations", [("pdf", [(3, 3), (17, 17)]), ("audio", [(1.2, 2.4), (50.1, 54.7)])])
def test_disjoint_support_keeps_actual_page_or_time_and_citations(store, kind, locations):
    hit, ids = source(store, "source." + kind, ["First complete fact.", "Separate complete fact."],
                      kind=kind, positions=locations, supports=(1,))
    text, meta = pack(store, [hit])
    assert len(meta["records"]) == 1
    assert [(p["loc"], p["loc_end"]) for p in meta["records"][0]["segments"]] == locations
    assert "p.3–17" not in text
    for sid in ids:
        evidence = resolve_citation(store, cid(store, sid))
        assert evidence.text in meta["records"][0]["text"] and evidence.citation.citation_id in text


def test_neighbor_order_uses_ordinal_distance_for_text_and_actual_pages_for_pdf(store):
    hit, _ = source(store, "text.txt", ["FAR_ORDINAL", "NEAR_LEFT", "PRIMARY", "NEAR_RIGHT"],
        primary=2, positions=[(1, 1), (100, 100), (101, 101), (999, 999)])
    counter = ByteCounter()
    pack(store, [hit], context=2, budget=12000, backend=counter)
    first_seen = lambda token: next(i for i, value in enumerate(counter.calls) if token in value)
    assert first_seen("NEAR_LEFT") < first_seen("NEAR_RIGHT") < first_seen("FAR_ORDINAL")
    pdf, ids = source(store, "pages.pdf", ["PAGE_ONE", "SAME_PAGE", "PDF_PRIMARY", "NEXT_PAGE", "PAGE_TWELVE"],
        primary=2, kind="pdf", positions=[(1, 1), (10, 10), (10, 10), (11, 11), (12, 12)])
    counter = ByteCounter()
    text, meta = pack(store, [pdf], context=1, backend=counter)
    first_seen = lambda token: next(i for i, value in enumerate(counter.calls) if token in value)
    assert first_seen("SAME_PAGE") < first_seen("NEXT_PAGE")
    assert "PAGE_ONE" not in text and "PAGE_TWELVE" not in text
    assert {p["loc"] for p in meta["records"][0]["segments"]} == {10, 11}


@pytest.mark.parametrize("failure", ["over_budget", "nonconvergent"])
def test_final_fold_rollback_removes_deleted_seed_source_association(store, failure):
    first, one = source(store, "first.txt", ["Same complete source."])
    second, two = source(store, "removed.txt", ["Same complete source."], rank=2)

    class FoldFails(ByteCounter):
        def count_tokens(self, texts):
            self.calls.extend(texts)
            return [(100000 if failure == "over_budget" else len(value.encode()) + len(self.calls))
                    if "also match" in value else len(value.encode()) for value in texts]

    counter = FoldFails()
    text, meta = pack(store, [first, second], backend=counter, budget=10000)
    assert any("also match" in value and second.path in value for value in counter.calls)
    assert len(meta["records"]) == 1 and meta["skipped_over_budget"] == 1
    assert meta["duplicates_folded"] == meta["also_folded"] == 0
    assert meta["records"][0]["also_hits"] == []
    assert second.path not in text and cid(store, two[0]) not in text
    assert cid(store, one[0]) in text


def test_primary_only_duplicate_fold_keeps_both_complete_sources(store):
    first, one = source(store, "first.txt", ["Same complete source."])
    second, two = source(store, "second.txt", ["Same complete source."], rank=2)
    text, meta = pack(store, [first, second])
    assert len(meta["records"]) == 1 and meta["duplicates_folded"] == 1
    assert all(cid(store, sid) in text for sid in one + two)
    assert first.path in text and second.path in text


def test_same_primary_with_secondary_unique_support_cannot_also_fold(store):
    first, ids = source(store, "same.txt", ["Shared primary.", "Unique secondary support."])
    second = replace(first, rank=2, supporting_segment_ids=[ids[1]])
    text, meta = pack(store, [first, second])
    assert len(meta["records"]) == 2 and meta["also_folded"] == 0
    assert meta["records"][1]["also_hits"] == []
    assert "Unique secondary support." in meta["records"][1]["text"]
    assert cid(store, ids[1]) in text


def test_same_primary_duplicate_is_a_real_also_association(store):
    first, ids = source(store, "same.txt", ["One complete body."])
    text, meta = pack(store, [first, replace(first, rank=2)])
    assert len(meta["records"]) == 1 and meta["also_folded"] == 1
    assert [h["segment_id"] for h in meta["records"][0]["also_hits"]] == ids
    assert cid(store, ids[0]) in text


def test_seam_sid_membership_cannot_discard_only_full_later_hit(store):
    overlap = " the warranty lasts exactly forty-five calendar days; "
    first, ids = source(store, "seam.txt", ["Prefix original." + overlap, overlap + "Suffix original."],
                        positions=[(1, 3), (3, 5)])
    later = replace(first, rank=2, segment_id=ids[1], loc=3, loc_end=5)
    parts, _ = NeighborIndex(store).parts_for(first, 1)
    joined = material_text(parts, first)
    assert read_evidence(store, ids[1]).text not in joined  # existing newline seam
    first_record = PackRecord(1, first, parts=parts, text=joined,
                             citation_ids={sid: cid(store, sid) for sid in ids})
    later_parts, _ = NeighborIndex(store).parts_for(later, 0)
    second_record = PackRecord(2, later, parts=later_parts, text=material_text(later_parts, later),
                              citation_ids={ids[1]: cid(store, ids[1])})
    records, duplicates, also = _fold([first_record, second_record])
    assert len(records) == 2 and duplicates == also == 0
    assert read_evidence(store, ids[1]).text in records[1].text


def test_unselected_future_neighbor_cannot_claim_later_skipped_hit(store):
    first, ids = source(store, "same.txt", ["Complete short seed.", "Huge neighbor whole body." * 1000],
                        positions=[(1, 1), (2, 2)])
    second = replace(first, rank=2, segment_id=ids[1], loc=2, loc_end=2)
    text, meta = pack(store, [first, second], context=1, budget=1800)
    assert len(meta["records"]) == 1 and meta["skipped_over_budget"] == 1
    assert meta["also_folded"] == 0 and meta["records"][0]["also_hits"] == []
    assert cid(store, ids[0]) in text and cid(store, ids[1]) not in text


def test_changed_numbers_or_secondary_support_prevent_duplicate_fold(store):
    first, _ = source(store, "first.txt", ["Rate is 45.", "First extra fact."], supports=(1,))
    second, two = source(store, "second.txt", ["Rate is 45.", "Different extra fact."], supports=(1,), rank=2)
    third, _ = source(store, "third.txt", ["Rate is 46."], rank=3)
    text, meta = pack(store, [first, second, third])
    assert len(meta["records"]) == 3 and meta["duplicates_folded"] == 0
    assert all(cid(store, sid) in text for sid in two)


def test_first_24_attempt_cap_is_explicit_even_with_identical_duplicates(store):
    hits, ids = [], []
    for i in range(MAX_RECORDS + 1):
        h, source_ids = source(store, f"duplicate-{i}.txt", ["Same complete body."], rank=i + 1)
        hits.append(h); ids.extend(source_ids)
    text, meta = pack(store, hits, budget=32768)
    assert len(meta["records"]) == 1 and meta["duplicates_folded"] == MAX_RECORDS - 1
    assert meta["skipped_by_record_limit"] == 1
    assert all(cid(store, sid) in text for sid in ids[:-1])
    assert cid(store, ids[-1]) not in text and hits[-1].path not in text


@pytest.mark.parametrize("kind", ["name", "vector_only"])
def test_metadata_or_vector_only_seed_remains_honest_and_can_be_skipped(store, kind):
    path = "/synthetic/metadata.txt"
    fid = store.upsert_file(FileRec(path=path, state="done", capability="metadata_only",
                                   info={"note": "whole stored metadata"}))
    store.replace_segments(fid, [SegmentRec(kind="name" if kind == "name" else "chunk", modality="none",
                                            text_for_fts="owned folder" if kind == "name" else None)])
    sid = store.db.execute("SELECT id FROM segments WHERE file=?", (fid,)).fetchone()[0]
    hit = Hit(1, 1, None, None, 0, "doc", path, None, None, "name" if kind == "name" else "chunk",
              None, "metadata_only", "lexical", segment_id=sid)
    text, meta = pack(store, [hit])
    assert meta["records"][0]["segments"] == []
    assert "no quotable" in text and cid(store, sid) in text
    if kind == "name":
        assert "whole stored metadata" in text and meta["records"][0]["note"] == "name_metadata"


@pytest.mark.parametrize("backend", [None, "flaky"])
def test_empty_and_tokenizer_loss_keep_one_honest_mode_and_determinism(store, backend):
    class Flaky:
        calls = 0
        def count_tokens(self, texts):
            self.calls += 1
            return [1] if self.calls == 1 else None
    selected = Flaky() if backend else None
    text, meta = pack(store, [], backend=selected, budget=512)
    assert meta["token_mode"] == "estimate" and meta["records"] == []
    assert meta["used_tokens"] == est_tokens(text)
    repeated, _ = pack(store, [], backend=Flaky() if backend else None, budget=512)
    assert repeated == text


@pytest.mark.parametrize("invalid", [True, -1, 1.5, float("inf"), float("nan")])
def test_invalid_token_counts_are_not_estimate_fallback(store, invalid):
    class Invalid:
        def count_tokens(self, texts):
            return [invalid]
    with pytest.raises(ValueError, match="invalid token count"):
        pack(store, [], backend=Invalid())


def test_oversized_empty_envelope_fails_without_unmeasured_success(store):
    with pytest.raises(BudgetTooSmall):
        pack(store, [], budget=512, query="x" * 9000)


def test_cancellation_after_seed_or_during_count_propagates_no_partial_pack(store):
    first, _ = source(store, "first.txt", ["Complete seed.", "Complete support."], supports=(1,))
    counter = ByteCounter()
    def cancel():
        if any("Complete support." in value for value in counter.calls):
            raise RequestTimeout("fixture cancellation while allocating support")
    with request_cancellation(cancel), pytest.raises(RequestTimeout, match="fixture cancellation"):
        pack(store, [first], backend=counter)
    class TimeoutCounter:
        def count_tokens(self, texts):
            raise RequestTimeout("fixture deadline in tokenizer")
    with pytest.raises(RequestTimeout, match="fixture deadline"):
        pack(store, [first], backend=TimeoutCounter())


def test_large_pdf_provider_is_scanned_fixed_times_and_count_work_is_linear(store, monkeypatch):
    from goldmine import pack as module
    texts = [f"Complete synthetic same-page source block {i}." for i in range(1000)]
    hit, ids = source(store, "many.pdf", texts, kind="pdf", positions=[(3, 3)] * len(texts))
    original = NeighborIndex.parts_for
    providers = []
    class Counted(list):
        iterations = 0
        def __iter__(self):
            self.iterations += 1
            return super().__iter__()
    def wrapped(self, *args, **kwargs):
        parts, note = original(self, *args, **kwargs)
        counted = Counted(parts); providers.append(counted)
        return counted, note
    monkeypatch.setattr(NeighborIndex, "parts_for", wrapped)
    counter = ByteCounter()
    text, meta = pack(store, [hit], backend=counter, budget=2200)
    assert len(providers) == 1 and providers[0].iterations == 3
    # One seed + one attempt per remaining same-page chunk + final fixpoint,
    # at most FIXPOINT_ROUNDS+1 counts each, plus probe/JSON record count.
    assert len(counter.calls) <= (len(texts) + 1) * (FIXPOINT_ROUNDS + 1) + 2
    assert meta["records"][0]["segments"][0]["segment_id"] == ids[0]


@pytest.mark.parametrize("kind", ["doc", "code", "audio"])
@pytest.mark.parametrize("gap", ["over_budget", "unrenderable"])
@pytest.mark.parametrize("bounds", ["same", "disjoint"])
def test_noncontiguous_neighbors_keep_whole_literals_after_middle_gap(store, kind, gap, bounds):
    # Equal line/time bounds do not prove that two selected units are adjacent.
    seam = "The calibration threshold is 123 and requires three signatures; "
    first = "PREFIX ORIGINAL. " + seam
    last = seam + "TAIL C COMPLETE CONDITION."
    hit, ids = source(store, "neighbor-gap." + kind,
        [first, "Oversized excluded middle. " * 1000, last], kind=kind,
        positions=[(1, 1)] * 3 if bounds == "same" else [(1, 2), (3, 4), (5, 6)])
    if gap == "unrenderable":
        # Real stored middle unit remains an ownership/order boundary even
        # though NeighborIndex's renderable provider cannot return it.
        with store.write_transaction():
            store.db.execute("DELETE FROM fts WHERE rowid=?", (ids[1],))
            store.db.execute("UPDATE segments SET vec=?,modality='text' WHERE id=?",
                             (bytes((0, 60, 0, 0)), ids[1]))  # synthetic fp16 [1, 0]
        assert store.db.execute("SELECT vec IS NOT NULL FROM segments WHERE id=?", (ids[1],)).fetchone()[0]
    text, meta = pack(store, [hit], context=2, budget=2200)
    record = meta["records"][0]
    assert [p["segment_id"] for p in record["segments"]] == [ids[0], ids[2]]
    assert first in record["text"] and last in record["text"]
    assert record["text"].count(seam) == 2
    assert record["text"].count("# stored evidence:") == 2
    assert all(resolve_citation(store, cid(store, sid)).text in record["text"] for sid in (ids[0], ids[2]))
    assert cid(store, ids[0]) in text and cid(store, ids[2]) in text
    assert cid(store, ids[1]) not in text and "Oversized excluded middle." not in text
    if bounds == "disjoint" and kind in {"doc", "code"}:
        assert "# [1] " + hit.path + " L1-2, L5-6" in text
        assert "# [1] " + hit.path + " L1-6" not in text


@pytest.mark.parametrize("kind", ["doc", "code", "pdf"])
def test_true_contiguous_neighbor_only_material_preserves_existing_seam_bytes(store, kind):
    # This revision changes only newly gapped selections, not the established
    # neighbor-only presentation for genuinely consecutive stored units.
    seam = "The calibration threshold is 123 and requires three signatures; "
    first = "PREFIX ORIGINAL. " + seam
    last = seam + "TAIL C COMPLETE CONDITION."
    hit, ids = source(store, "neighbor-contiguous." + kind, [first, last],
                      kind=kind, positions=[(1, 1)] * 2)
    provider = NeighborIndex(store)
    parts, _ = provider.parts_for(hit, 1)
    legacy_text = material_text(parts, hit)
    text, meta = pack(store, [hit], context=1, budget=2200)
    record = meta["records"][0]
    assert [p["segment_id"] for p in record["segments"]] == ids
    assert record["text"] == legacy_text
    assert record["text"].count(seam) == 1 and "# stored evidence:" not in record["text"]
    assert all(cid(store, sid) in text for sid in ids)


@pytest.mark.parametrize("kind", ["doc", "audio"])
def test_gapped_neighbor_header_and_footer_list_only_actual_parts(store, kind):
    hit, ids = source(store, "neighbor-spans." + kind,
        ["Primary whole source.", "Nearby whole source.", "Rejected middle. " * 1000,
         "Later whole source."], kind=kind,
        positions=[(1, 2), (3, 4), (5, 6), (7, 8)])
    text, meta = pack(store, [hit], context=3, budget=2200)
    assert [p["segment_id"] for p in meta["records"][0]["segments"]] == [ids[0], ids[1], ids[3]]
    header = next(line for line in text.splitlines() if line.startswith("[1] "))
    footer = next(line for line in text.splitlines() if line.startswith("# [1] " + hit.path))
    if kind == "doc":
        assert "after L3-4, after L7-8" in header and "after L3-8" not in header
        assert footer.endswith(" L1-2, L3-4, L7-8") and "L1-8" not in footer
    else:
        assert "after 3s-4s, after 7s-8s" in header
        assert footer.endswith(" 1s-2s, 3s-4s, 7s-8s")
    assert cid(store, ids[2]) not in text


@pytest.mark.parametrize("gap", ["over_budget", "unrenderable"])
@pytest.mark.parametrize("pages", ["same_page", "different_pages"])
def test_pdf_chunk_neighbors_keep_whole_literal_across_missing_middle(store, gap, pages):
    # Actual PDF text extraction stores kind=chunk, not only kind=page.
    # Its same-page grouping must not override a known source-order hole.
    seam = "The calibration threshold is 123 and requires three signatures; "
    first = "PDF PREFIX ORIGINAL. " + seam
    last = seam + "PDF TAIL COMPLETE CONDITION."
    locations = [(3, 3)] * 3 if pages == "same_page" else [(3, 3), (4, 4), (5, 5)]
    hit, ids = source(store, "neighbor-gap.pdf", [first, "Huge omitted PDF text. " * 1000, last],
                      kind="pdf", positions=locations)
    assert store.db.execute("SELECT kind FROM segments WHERE id=?", (ids[0],)).fetchone()[0] == "chunk"
    if gap == "unrenderable":
        with store.write_transaction():
            store.db.execute("DELETE FROM fts WHERE rowid=?", (ids[1],))
            store.db.execute("UPDATE segments SET vec=?,modality='text' WHERE id=?",
                             (bytes((0, 60, 0, 0)), ids[1]))
    text, meta = pack(store, [hit], context=2, budget=2200)
    record = meta["records"][0]
    assert [p["segment_id"] for p in record["segments"]] == [ids[0], ids[2]]
    assert first in record["text"] and last in record["text"]
    assert record["text"].count(seam) == 2 and record["text"].count("# stored evidence:") == 2
    assert all(resolve_citation(store, cid(store, sid)).text in record["text"] for sid in (ids[0], ids[2]))
    assert cid(store, ids[0]) in text and cid(store, ids[2]) in text and cid(store, ids[1]) not in text
    if pages == "different_pages":
        footer = next(line for line in text.splitlines() if line.startswith("# [1] " + hit.path))
        assert footer.endswith(" p.3, p.5") and "p.3-5" not in footer
