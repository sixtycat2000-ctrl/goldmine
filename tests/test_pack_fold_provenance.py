"""Synthetic EX-PACK-01 mechanics; byte counts are NOT model token costs."""
from __future__ import annotations

from dataclasses import replace
import re
import socket
import subprocess

import pytest

from goldmine.citations import read_evidence, resolve_citation
from goldmine.pack import MAX_RECORDS, NeighborIndex, PackSettings, build_pack_from_hits
from goldmine.search import Hit
from goldmine.store import FileRec, SegmentRec, Store


class ByteCounter:
    def count_tokens(self, texts):
        return [len(text.encode("utf-8")) for text in texts]


@pytest.fixture(autouse=True)
def no_process_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("synthetic fold test attempted a process or network call")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "mechanical.sqlite")
    yield value
    value.close()


def source(store, name, texts, *, supports=(), rank=1, positions=None):
    path = "/synthetic/" + name
    fid = store.upsert_file(FileRec(path=path, state="done", capability="content_indexed",
                                   sha256="b" * 64))
    locations = positions or [(i * 10 + 1, i * 10 + 5) for i in range(len(texts))]
    store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", loc=start,
                                           loc_end=end, text_for_fts=text)
                                 for text, (start, end) in zip(texts, locations)])
    ids = [r[0] for r in store.db.execute("SELECT id FROM segments WHERE file=? ORDER BY id", (fid,))]
    hit = Hit(rank, 2.0, None, None, 1.0, "doc", path, locations[0][0], locations[0][1],
              "chunk", None, "content_indexed", "lexical", segment_id=ids[0],
              supporting_segment_ids=[ids[i] for i in supports])
    return hit, ids


def packed(store, hits, *, budget=8000, context=0):
    text, meta = build_pack_from_hits(
        store, ByteCounter(), PackSettings(query="mechanical", scorer="lexical",
                                          budget=budget, context=context), hits,
        citation_id_of=lambda sid: read_evidence(store, sid).citation.citation_id)
    assert meta["used_tokens"] == len(text.encode("utf-8")) <= budget
    assert meta["fixpoint_converged"] is True
    assert int(re.search(r"used (\d+)/", text)[1]) == meta["used_tokens"]
    assert (len(meta["records"]) + meta["duplicates_folded"] + meta["also_folded"]
            + meta["skipped_over_budget"] + meta["skipped_by_record_limit"]) == len(hits)
    for record in meta["records"]:
        assert record["tokens"] == len(record["text"].encode("utf-8"))
    return text, meta


def citations(store, ids):
    return [read_evidence(store, sid).citation.citation_id for sid in ids]


def test_same_layout_supporting_secondary_remains_separate_with_every_citation(store):
    texts = ["The first complete synthetic source sentence.", "The second complete synthetic source sentence."]
    first, one = source(store, "first.txt", texts, supports=(1,))
    second, two = source(store, "second.txt", texts, supports=(1,), rank=2)
    text, meta = packed(store, [first, second])
    assert len(meta["records"]) == 2 and meta["duplicates_folded"] == 0
    assert [record["path"] for record in meta["records"]] == [first.path, second.path]
    for record, ids in zip(meta["records"], [one, two]):
        assert [p["segment_id"] for p in record["segments"]] == ids
        assert record["also_hits"] == []
        for sid in ids:
            evidence = read_evidence(store, sid)
            assert evidence.text in record["text"]
            assert evidence.citation.citation_id in text
            assert resolve_citation(store, evidence.citation.citation_id) == evidence


@pytest.mark.parametrize("reverse", [False, True])
def test_three_contributing_parts_remain_complete_in_either_rank_order(store, reverse):
    texts = [f"Independent whole synthetic paragraph {i}." for i in range(3)]
    first, one = source(store, "three-a.txt", texts, supports=(1, 2))
    second, two = source(store, "three-b.txt", texts, supports=(1, 2), rank=2)
    hits = [second, first] if reverse else [first, second]
    hits = [replace(hit, rank=i + 1) for i, hit in enumerate(hits)]
    text, meta = packed(store, hits)
    assert len(meta["records"]) == 2 and meta["duplicates_folded"] == 0
    assert all(cid in text for cid in citations(store, one + two))


def test_existing_single_primary_exact_duplicate_fold_is_preserved(store):
    first, one = source(store, "one-a.txt", ["Same exact whole synthetic body."])
    second, two = source(store, "one-b.txt", ["Same exact whole synthetic body."], rank=2)
    text, meta = packed(store, [first, second])
    assert len(meta["records"]) == 1 and meta["duplicates_folded"] == 1
    assert [h["segment_id"] for h in meta["records"][0]["also_hits"]] == two
    assert all(cid in text for cid in citations(store, one + two))


def test_multipart_primary_may_absorb_a_representable_single_primary_secondary(store):
    first, one = source(store, "neighbor-a.txt", ["First whole line.", "Second whole line."],
                        positions=[(1, 1), (2, 2)])
    second, two = source(store, "single-b.txt", ["First whole line.\nSecond whole line."], rank=2,
                         positions=[(1, 2)])
    text, meta = packed(store, [first, second], context=1)
    assert len(meta["records"]) == 1 and meta["duplicates_folded"] == 1
    assert len(meta["records"][0]["segments"]) == 2
    assert all(cid in text for cid in citations(store, one + two))


def test_one_visible_supporting_part_is_not_mistaken_for_representable_primary(store):
    texts = ["", "The only visible complete supporting sentence."]
    first, one = source(store, "empty-primary-a.txt", texts, supports=(1,))
    second, two = source(store, "empty-primary-b.txt", texts, supports=(1,), rank=2)
    neighbor = NeighborIndex(store)
    assert [p["segment_id"] for p in neighbor.parts_for(second, 0)[0]] == [two[1]]
    text, meta = packed(store, [first, second])
    assert len(meta["records"]) == 2 and meta["duplicates_folded"] == 0
    assert all(cid in text for cid in citations(store, one + two))


def test_separate_secondary_fails_whole_budget_honestly_without_losing_count(store):
    texts = ["A complete primary source sentence.", "A complete supporting source sentence."]
    first, one = source(store, "budget-a.txt", texts, supports=(1,))
    second, two = source(store, "budget-b.txt", texts, supports=(1,), rank=2)
    _, single = packed(store, [first])
    budget = single["used_tokens"] + 25
    text, meta = packed(store, [first, second], budget=budget)
    assert len(meta["records"]) == 1 and meta["duplicates_folded"] == 0
    assert meta["skipped_over_budget"] == 1
    assert all(cid in text for cid in citations(store, one))
    assert all(cid not in text for cid in citations(store, two))
    assert meta["records"][0]["text"] == single["records"][0]["text"]


def test_fitting_primary_seeds_keep_both_sources_without_unselected_support_citations(store):
    texts = ["Entire short primary source.", "Entire optional context sentence." * 1000]
    first, one = source(store, "slim-a.txt", texts, supports=(1,))
    second, two = source(store, "slim-b.txt", texts, supports=(1,), rank=2)
    text, meta = packed(store, [first, second], budget=1500)
    # Revised allocation budgets both fitting whole primaries first. Their
    # identical emitted primary-only material may then fold, keeping both
    # true sources, while neither enormous optional support is emitted.
    assert len(meta["records"]) == 1 and meta["duplicates_folded"] == 1
    assert meta["skipped_over_budget"] == 0
    assert [h["segment_id"] for h in meta["records"][0]["also_hits"]] == [two[0]]
    assert meta["records"][0]["text"] == texts[0]
    assert all(cid in text for cid in citations(store, [one[0], two[0]]))
    assert all(cid not in text for cid in citations(store, [one[1], two[1]]))


def test_record_cap_accounts_for_complete_separate_multipart_sources(store):
    hits, all_ids = [], []
    for i in range(MAX_RECORDS + 1):
        hit, ids = source(store, f"cap-{i}.txt", ["Same full primary.", "Same full support."],
                          supports=(1,), rank=i + 1)
        hits.append(hit); all_ids.append(ids)
    text, meta = packed(store, hits, budget=32768)
    assert len(meta["records"]) == MAX_RECORDS
    assert meta["duplicates_folded"] == 0 and meta["skipped_by_record_limit"] == 1
    assert all(cid in text for ids in all_ids[:MAX_RECORDS] for cid in citations(store, ids))
    assert all(cid not in text for cid in citations(store, all_ids[-1]))


def test_distinct_same_file_seeds_preserve_each_real_body_without_repeated_expansion(store):
    first, ids = source(store, "same-file.txt", ["First original sentence.", "Second original sentence."],
                        positions=[(1, 1), (2, 2)])
    second = replace(first, segment_id=ids[1], loc=2, loc_end=2, rank=2)
    text, meta = packed(store, [first, second], context=1)
    # Both whole seeds are now independently present. Neighbor expansion
    # does not repeat an already emitted primary to manufacture a fold.
    assert len(meta["records"]) == 2 and meta["also_folded"] == 0 and meta["duplicates_folded"] == 0
    assert all(record["also_hits"] == [] for record in meta["records"])
    assert [record["text"] for record in meta["records"]] == ["First original sentence.", "Second original sentence."]
    assert all(cid in text for cid in citations(store, ids))


def test_guard_never_folds_similar_but_different_facts_and_remains_deterministic(store):
    first, _ = source(store, "different-a.txt", ["The synthetic limit is 431."])
    second, _ = source(store, "different-b.txt", ["The synthetic limit is 432."], rank=2)
    left, lm = packed(store, [first, second])
    right, rm = packed(store, [first, second])
    assert left == right and lm == rm
    assert len(lm["records"]) == 2 and lm["duplicates_folded"] == 0
