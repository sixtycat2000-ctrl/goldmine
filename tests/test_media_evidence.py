"""Content evidence is stored OCR/ASR text, never an answer-bearing filename."""

import pytest

from goldmine.backends.mock import MockBackend
from goldmine.chunker import CHUNKER_VERSION
from goldmine.config import Config
from goldmine.ingest import IngestPipeline
from goldmine.ingest import asr, media, rules
from goldmine.ingest.commit import IngestReport
from goldmine.ingest.extraction import _Outcome
from goldmine.ingest.walk import Candidate
from goldmine.ocr import OcrLine, OcrPage
from goldmine.search import Searcher, parse_filters
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = Config()
    cfg.home = tmp_path / "home"
    monkeypatch.setattr(media, "sips_path", lambda: None)
    store = Store(tmp_path / "media.sqlite")
    backend = MockBackend(dim=768)
    pipeline = IngestPipeline(cfg, store, backend)
    yield cfg, store, pipeline, backend
    pipeline._close_media_tmp()
    store.close()


def candidate(tmp_path, kind="image"):
    path = tmp_path / ("opaque-asset.png" if kind == "image" else "opaque-asset.wav")
    path.write_bytes(b"fixture bytes; content comes from the mocked extractor, not this name")
    return Candidate(path, kind, path.stat().st_size, path.stat().st_mtime)


def commit(pipeline, cand, outcome):
    report = IngestReport()
    # Direct commit tests enter after the normal run() identity preparation.
    pipeline._prepare_backend(report, startup=False)
    pipeline._commit(cand, "fixture-sha", outcome, report, batch=32, embed=pipeline.backend)
    return pipeline.store.find_by_path(str(cand.path))


def test_ocr_unique_body_and_cjk_use_actual_ocr_segment(setup, tmp_path, monkeypatch):
    _, store, pipeline, _ = setup
    cand = candidate(tmp_path)
    text = "harbor access approved QZ-9427\n中文知识库管理"
    monkeypatch.setattr(rules, "is_screenshot", lambda p: True)
    monkeypatch.setattr(pipeline, "_ocr_screenshot", lambda p, **kw: (text, 1.2, "ok"))
    rec = commit(pipeline, cand, pipeline._extract_image(cand, pipeline.backend.capability()))
    assert rec.info["ocr_status"] == "complete"
    assert rec.info["evidence_types"] == ["name_metadata", "ocr_text"]
    for query in ("QZ-9427", "知识库管理"):
        hit = Searcher(store, None).search(query, scorer="lexical")[0]
        assert hit.segment == "ocr" and hit.evidence_type == "content"
        assert hit.media_evidence_type == "ocr_text"
        assert hit.loc >= 1 and "QZ-9427" in store.segment_text(hit.segment_id)
    assert store.db.execute("SELECT count(*) FROM segments WHERE kind='ocr' AND vec IS NOT NULL").fetchone()[0]


def test_long_ocr_text_embeds_in_legal_chunks_and_keeps_ocr_line_locations(setup, tmp_path, monkeypatch):
    from test_pdf_evidence import CharacterTokenizer

    cfg, store, _, _ = setup
    backend = CharacterTokenizer(dim=768)
    pipeline = IngestPipeline(cfg, store, backend)
    cand = candidate(tmp_path)
    text = "recognized harbor words\n" * 100
    monkeypatch.setattr(rules, "is_screenshot", lambda p: True)
    monkeypatch.setattr(pipeline, "_ocr_screenshot", lambda p, **kw: (text, 1.0, "ok"))
    rec = commit(pipeline, cand, pipeline._extract_image(cand, backend.capability()))
    rows = store.db.execute("SELECT loc,loc_end FROM segments WHERE file=? AND kind='ocr' ORDER BY id", (rec.id,)).fetchall()
    # ocr-rehydrate-001: the stored text is rehydrated (line breaks join into
    # spaces), so the split count halves; the loc span still records the RAW
    # line count of the OCR output, and multiple legal chunks still exist.
    # ocr-rehydrate-001: stored text is rehydrated (lines join into spaces),
    # so 100 raw OCR lines produce ~51 chunks of joined prose; the span
    # still starts at line 1 and spans the material.
    assert len(rows) > 1 and rows[0][0] == 1 and rows[-1][1] >= 50
    assert all(start >= 1 and end >= start for start, end in rows)


def test_photo_name_and_actual_date_work_without_semantic_backend(setup, tmp_path, monkeypatch):
    _, store, pipeline, _ = setup
    cand = candidate(tmp_path)
    cand = Candidate(cand.path.with_name("summer-album.png"), cand.kind, cand.size, cand.mtime)
    cand.path.write_bytes(b"photo")
    monkeypatch.setattr(rules, "is_screenshot", lambda p: False)
    monkeypatch.setattr(media, "sips_path", lambda: "stored-metadata-probe")
    from goldmine.ingest.exif import CaptureResult
    monkeypatch.setattr(media, "exif_capture", lambda p: CaptureResult(("2026:02:14 12:00:00", 1771070400.0), "imageio", "complete"))
    rec = commit(pipeline, cand, pipeline._extract_image(cand, pipeline.backend.capability()))
    query, filters = parse_filters("summer after:2026-01-01 before:2027-01-01")
    hit = Searcher(store, None).search(query, filters=filters, scorer="lexical")[0]
    assert hit.segment == "name" and hit.loc is None
    assert hit.evidence_type == hit.media_evidence_type == "name_metadata"
    assert rec.info["taken"] == "2026:02:14 12:00:00"
    assert rec.info["evidence_types"] == ["name_metadata"]
    assert Searcher(store, None).search("a spaceship in the picture", scorer="hybrid") == []


@pytest.mark.parametrize("status,expected", [("off", "unavailable"), ("no-text", "no_text"),
                                             ("low-confidence", "low_confidence"), ("error", "error")])
def test_missing_empty_low_confidence_failed_ocr_remain_metadata(setup, tmp_path, monkeypatch, status, expected):
    _, store, pipeline, _ = setup
    cand = candidate(tmp_path)
    monkeypatch.setattr(rules, "is_screenshot", lambda p: True)
    monkeypatch.setattr(pipeline, "_ocr_screenshot", lambda p, **kw: (None, None, status))
    rec = commit(pipeline, cand, pipeline._extract_image(cand, pipeline.backend.capability()))
    assert rec.capability == "metadata_only"
    assert rec.info["ocr_status"] == expected
    assert rec.info["evidence_types"] == ["name_metadata"]
    assert not store.db.execute("SELECT 1 FROM segments WHERE kind='ocr'").fetchone()
    assert Searcher(store, None).search("QZ-9427", scorer="lexical") == []


def test_low_confidence_policy_does_not_promote_dropped_words(setup, tmp_path, monkeypatch):
    _, _, pipeline, _ = setup
    monkeypatch.setattr(pipeline, "_ocr_probe", lambda: True)

    def drain():
        batch, pipeline._ocr_batch = pipeline._ocr_batch, []
        for req in batch:
            req.page = OcrPage(lines=[OcrLine("invented low confidence text", 0.1)])
            req.done.set()
        pipeline._ocr_batch_leader = False

    monkeypatch.setattr(pipeline, "_drain_ocr_batch", drain)
    assert pipeline._ocr_screenshot(tmp_path / "asset.png") == (None, None, "low-confidence")


def test_asr_body_retains_source_seconds_and_records_provenance(setup, tmp_path, monkeypatch):
    _, store, pipeline, _ = setup
    cand = candidate(tmp_path, "audio")
    monkeypatch.setattr(asr, "resolve_tools", lambda cfg: object())
    monkeypatch.setattr(asr, "transcribe", lambda p, cfg: asr.AsrResult(
        "en", [asr.AsrSegment("beacon frequency ZX-7731 approved", 73.25, 89.5)], duration_s=100.0,
    ))
    rec = commit(pipeline, cand, pipeline._extract_media(cand))
    hit = Searcher(store, None).search("ZX-7731", scorer="lexical")[0]
    assert (hit.loc, hit.loc_end) == (73.25, 89.5)
    assert hit.segment == "audio" and hit.media_evidence_type == "asr_text"
    assert store.segment_text(hit.segment_id) == "beacon frequency ZX-7731 approved"
    assert rec.info["evidence_types"] == ["asr_text", "name_metadata"]
    assert rec.info["asr_status"] == "complete"


def test_no_speech_and_missing_asr_do_not_create_transcript_templates(setup, tmp_path, monkeypatch):
    _, store, pipeline, _ = setup
    cand = candidate(tmp_path, "audio")
    monkeypatch.setattr(asr, "resolve_tools", lambda cfg: None)
    rec = commit(pipeline, cand, pipeline._extract_media(cand))
    assert rec.info["asr_status"] == "unavailable" and rec.capability == "metadata_only"
    monkeypatch.setattr(asr, "resolve_tools", lambda cfg: object())
    monkeypatch.setattr(asr, "transcribe", lambda p, cfg: asr.AsrResult("en", [], duration_s=100.0, dropped=4))
    rec = commit(pipeline, cand, pipeline._extract_media(cand))
    assert rec.info["asr_status"] == "no_speech" and rec.info["asr_dropped"] == 4
    assert rec.capability == "metadata_only"
    assert not store.db.execute("SELECT 1 FROM segments WHERE kind='audio'").fetchone()


@pytest.mark.parametrize("kind,info,ready,skip", [
    ("image", {"screenshot": True, "ocr_status": "unavailable"}, True, False),
    ("image", {"screenshot": True, "ocr_status": "complete", "ocr_chars": 50}, True, True),
    ("image", {"screenshot": True, "ocr_status": "no_text", "ocr": "no text"}, True, True),
    ("pdf", {"ocr_page_states": {"3": "unavailable"}}, True, False),
    ("pdf", {"ocr_page_states": {"3": "no_text"}}, True, True),
    ("audio", {"asr_status": "unavailable"}, True, False),
    ("audio", {"asr_status": "unavailable"}, False, True),
    ("audio", {"asr_status": "complete"}, True, True),
    ("audio", {"asr_status": "no_speech"}, True, True),
])
def test_tools_arriving_only_redo_incomplete_files(setup, tmp_path, monkeypatch, kind, info, ready, skip):
    _, store, pipeline, _ = setup
    if kind == "image":
        # This test isolates tool arrival after the separate EXIF metadata
        # repair has completed; legacy date repair has its own regression.
        info = {**info, "exif_version": media.EXIF_METADATA_VERSION, "exif_status": "absent"}
    path = tmp_path / "asset"
    fid = store.upsert_file(FileRec(path=str(path), kind=kind, size=10, mtime=2.0, sha256="same", state="done"))
    store.mark_file(fid, state="done", capability="metadata_only", detail=None, info=info,
                    extractor_version=None, chunker_version=CHUNKER_VERSION)
    monkeypatch.setattr(pipeline, "_ocr_probe", lambda: ready)
    monkeypatch.setattr(asr, "resolve_tools", lambda cfg: object() if ready else None)
    from goldmine.ingest import extract_pdf
    monkeypatch.setattr(extract_pdf, "pdftoppm_path", lambda: "pdftoppm" if ready else None)
    assert pipeline._skip_unchanged(Candidate(path, kind, 10, 2.0), "same", False) is skip


def test_empty_reextract_removes_previous_content_instead_of_claiming_it(setup, tmp_path):
    _, store, pipeline, _ = setup
    cand = candidate(tmp_path, "audio")
    rec = commit(pipeline, cand, _Outcome(capability="metadata_only", detail="no speech", info={"asr_status": "no_speech"}))
    store.replace_segments(rec.id, [SegmentRec(kind="audio", modality="none", loc=1, loc_end=2, text_for_fts="old transcript")])
    rec = commit(pipeline, cand, _Outcome(capability="metadata_only", detail="no speech", info={"asr_status": "no_speech"}))
    assert rec.info["evidence_types"] == ["name_metadata"]
    assert Searcher(store, None).search("old transcript", scorer="lexical") == []
