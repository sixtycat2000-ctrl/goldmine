"""PDF pointers must name actual pages; one file may supply disjoint facts."""

import pytest
import numpy as np

from goldmine.backends.mock import MockBackend
from goldmine.chunker import CHUNKER_VERSION, Chunk
from goldmine.config import Config
from goldmine.ingest import IngestPipeline
from goldmine.ingest.commit import IngestReport
from goldmine.ingest.extract_pdf import PdfResult
from goldmine.ingest.extraction import _Piece
from goldmine.pack import NeighborIndex, PackSettings, build_pack_from_hits
from goldmine.search import Searcher, parse_filters
from goldmine.store import FileRec, SegmentRec, Store
from test_search_offline import FixedBackend, evec


def add_pdf(store, path, pages):
    fid = store.upsert_file(FileRec(path=path, kind="pdf", state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts=path)] + [
        SegmentRec(kind="chunk", modality="none", loc=number, loc_end=number,
                   text_for_fts=text, excerpt=text) for number, text in pages
    ])
    return fid


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "pages.sqlite")
    yield store
    store.close()


def test_twenty_pages_choose_actual_numeric_anchor_and_stay_one_hit(index):
    pages = [(n, "harbor depth background explanation") for n in range(1, 21)]
    pages[2] = (3, "harbor depth approved 2209 meters")
    pages[16] = (17, "harbor depth approved 7788 meters")
    add_pdf(index, "/docs/harbor-depth-2209.pdf", pages)
    for query, page in [("harbor depth 2209", 3), ("harbor depth 7788", 17)]:
        hits = Searcher(index, None).search(query, scorer="lexical")
        assert len(hits) == 1
        assert hits[0].loc == hits[0].loc_end == page
        assert hits[0].segment != "name"
        assert query.split()[-1] in index.segment_text(hits[0].segment_id)


def test_filename_match_cannot_invent_a_pdf_body_page(index):
    add_pdf(index, "/docs/harbor.pdf", [(17, "a different topic with no relevant body word")])
    hit = Searcher(index, None).search("harbor", scorer="lexical")[0]
    assert hit.segment == "name" and hit.loc is None
    assert hit.evidence_type == "name_metadata"
    assert hit.supporting_segment_ids == []


def test_body_anchor_wins_over_other_page_semantic_peak(index):
    fid = add_pdf(index, "/docs/source.pdf", [(3, "approved harbor 2209"), (17, "approved harbor 7788")])
    with index.write_transaction():
        for loc, vec in [(3, evec(1)), (17, evec(0))]:
            index.db.execute("UPDATE segments SET vec=?,modality='text' WHERE file=? AND loc=?",
                             (np.asarray(vec, dtype=np.float16).tobytes(), fid, loc))
    for i in range(25):
        other = add_pdf(index, f"/docs/fill-{i}.pdf", [(1, "background material")])
        with index.write_transaction():
            index.db.execute("UPDATE segments SET vec=?,modality='text' WHERE file=? AND kind!='name'",
                             (evec(1).astype(np.float16).tobytes(), other))
    hit = Searcher(index, FixedBackend(evec(0))).search("harbor 2209", why=True)[0]
    # anchor.v2-intent: a bare year-like number is ambient in prose, so the
    # page-17 semantic peak legitimately wins selection, disclosed softly (7694837).
    assert hit.loc == 17 and hit.admission_reason != "verified_exact"
    assert "exact_source" not in hit.why and "anchor_contract_soft" in hit.why
    # The body-anchor-beats-semantic-peak pin survives for an obligatory
    # anchor shape: the quoted page-3 literal is selected and verified exact.
    query, filters = parse_filters('harbor "2209"')
    exact = Searcher(index, FixedBackend(evec(0))).search(query, filters=filters, why=True)[0]
    assert exact.loc == 3 and "2209" in index.segment_text(exact.segment_id)
    assert exact.admission_reason == "verified_exact"
    assert exact.why["display_evidence"]["body_query_units"] == 2
    assert exact.why["display_evidence"]["score_scope"] == "file"


def test_two_distant_facts_supply_both_real_segments_in_one_record(index):
    add_pdf(index, "/docs/source.pdf", [(3, "northern harbor approved depth 2209"),
                                      (17, "southern beacon approved frequency 7788")])
    hits = Searcher(index, None).search("harbor 2209 beacon 7788", scorer="lexical")
    assert len(hits) == 1
    assert len(hits[0].supporting_segment_ids) == 1
    ids = [hits[0].segment_id, *hits[0].supporting_segment_ids]
    assert {index.segment_row(sid)[1] for sid in ids} == {3, 17}
    text, meta = build_pack_from_hits(index, None, PackSettings(query="harbor 2209 beacon 7788", context=0), hits,
                                    citation_id_of=lambda sid: "stored-citation-" + str(sid))
    assert "northern harbor approved depth 2209" in text
    assert "southern beacon approved frequency 7788" in text
    assert all("stored-citation-" + str(sid) in text for sid in ids)
    assert len(meta["records"]) == 1


def test_pdf_context_does_not_jump_missing_unreadable_pages(index):
    add_pdf(index, "/docs/source.pdf", [(3, "harbor needle"), (17, "distant beacon")])
    hit = Searcher(index, None).search("harbor", scorer="lexical")[0]
    parts, _ = NeighborIndex(index).parts_for(hit, 1)
    assert [part["loc"] for part in parts] == [3]


def test_normal_and_ocr_chunks_keep_page_numbers_and_global_cap(tmp_path, monkeypatch):
    from goldmine.ingest import extract_pdf

    cfg = Config()
    pipeline = IngestPipeline(cfg, None, None, lexical_only=True)
    pages = [(3, ("first-page text\n" * 250)), (17, ("second-page material\n" * 250))]
    monkeypatch.setattr(extract_pdf, "extract_pages", lambda *a, **kw: PdfResult(pages=pages, total=20))
    outcome = pipeline._extract_pdf(tmp_path / "source.pdf", 500)
    assert CHUNKER_VERSION == "chunk.v6-char-tail"
    assert len(outcome.chunks) > 2
    assert {piece.chunk.loc for piece in outcome.chunks} == {3, 17}
    assert all(piece.chunk.loc == piece.chunk.loc_end and piece.location_unit == "page" for piece in outcome.chunks)
    assert all(("first-page" in piece.chunk.text) != ("second-page" in piece.chunk.text) for piece in outcome.chunks)
    capped = pipeline._extract_pdf(tmp_path / "source.pdf", 2)
    assert len(capped.chunks) == 2 and capped.partial
    assert capped.info["partial"] and capped.info["read_to"] == 3


def test_empty_unreadable_pdf_page_is_reported_not_erased(tmp_path, monkeypatch):
    from goldmine.ingest import extract_pdf

    pipeline = IngestPipeline(Config(), None, None, lexical_only=True)
    monkeypatch.setattr(pipeline, "_ocr_probe", lambda: False)
    monkeypatch.setattr(extract_pdf, "extract_pages", lambda *a, **kw: PdfResult(
        pages=[(3, "known text " * 40), (17, "")], total=20,
    ))
    outcome = pipeline._extract_pdf(tmp_path / "source.pdf", 100)
    assert outcome.info["thin_pages"] == [17]
    assert outcome.info["ocr_page_states"] == {"17": "unavailable"}
    assert {p.chunk.loc for p in outcome.chunks} == {3}


class CharacterTokenizer(MockBackend):
    def max_input_tokens(self):
        return 160

    def token_pieces(self, text):
        return [ch.encode("utf-8") for ch in text]

    def count_tokens(self, texts):
        return [len(text) for text in texts]

    def embed_texts(self, texts):
        assert all(len(text) < self.max_input_tokens() for text in texts)
        return super().embed_texts(texts)


@pytest.mark.parametrize("unit,start,end", [("page", 17, 17), ("second", 73.25, 89.5), ("line", 410, 411)])
def test_ctx_guard_resplit_retains_source_location(tmp_path, unit, start, end):
    backend = CharacterTokenizer(dim=768)
    pipeline = IngestPipeline(Config(), None, backend)
    piece = _Piece(Chunk("alpha beta\n" * 90, start, end), True,
                   kind="audio" if unit == "second" else "chunk", location_unit=unit)
    split = pipeline._ctx_guard_split([piece], "source")
    assert len(split) > 1
    assert all(len("title: source | text: " + p.chunk.text) < 160 for p in split)
    if unit in ("page", "second"):
        assert all((p.chunk.loc, p.chunk.loc_end) == (start, end) for p in split)
    else:
        assert split[0].chunk.loc == start and split[-1].chunk.loc > start


def test_pending_replacement_not_admitted_until_complete_snapshot(index):
    fid = add_pdf(index, "/docs/source.pdf", [(17, "old harbor 2209")])
    reader = Store(index.path, ro=True)
    try:
        with reader.read_snapshot():
            assert Searcher(reader, None).search("harbor", scorer="lexical")
            index.upsert_file(FileRec(path="/docs/source.pdf", kind="pdf", state="pending", sha256="new-sha"))
            assert Searcher(reader, None).search("harbor", scorer="lexical")
        with reader.read_snapshot():
            assert Searcher(reader, None).search("harbor", scorer="lexical") == []
        pipeline = IngestPipeline(Config(), index, None, lexical_only=True)
        pipeline._commit_segments(fid, [SegmentRec(kind="chunk", modality="none", loc=3, loc_end=3, text_for_fts="new beacon")],
                                  capability="content_indexed", detail=None, info={}, extractor_version="pdftotext@test",
                                  report=IngestReport(), kind="pdf")
        with reader.read_snapshot():
            hit = Searcher(reader, None).search("beacon", scorer="lexical")[0]
            assert hit.loc == 3 and reader.get_file(fid).sha256 == "new-sha"
    finally:
        reader.close()
