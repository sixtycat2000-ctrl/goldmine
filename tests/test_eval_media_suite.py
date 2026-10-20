"""Missing content must not shrink the complete-media denominator."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eval_real
from eval_media_suite import inspect_index
from goldmine.store import FileRec, SegmentRec, Store


def test_suite_query_sets_keep_historical_denominators():
    queries = eval_real.load_queries()
    legacy = eval_real.eligible_queries(queries)
    full = eval_real.eligible_queries(queries, eval_real.SKIPPED_NEEDS)
    assert len(legacy) == 150 and len(full) == 169
    assert sum(q["tag"] != "noise" for q in full) == 164
    assert len({q["id"] for q in full} - {q["id"] for q in legacy}) == 19
    assert eval_real.should_skip({"needs": ["asr", "ocr"]}, {"asr"})


@pytest.mark.parametrize("need,kind", [("asr", "audio"), ("ocr", "image"), ("exif", "image")])
def test_filename_or_tool_presence_is_not_content(tmp_path, need, kind):
    db = tmp_path / "index.sqlite"
    store = Store(db)
    file_id = store.upsert_file(FileRec(path=str(tmp_path / "target"), kind=kind))
    store.replace_segments(file_id, [SegmentRec(kind="name", modality="none", text_for_fts="asr ocr camera")])
    store.close()
    queries = [{"id": "media", "tag": kind, "needs": [need],
                "expect": [{"path": "target", "grade": 3}]}]
    before = db.read_bytes()
    report = inspect_index(db, queries, tmp_path)
    assert report["answerable"] == 1 and report["eligible_ids"] == ["media"]
    assert report["missing"][0]["capability"] == need
    assert db.read_bytes() == before


def test_stored_transcript_and_ocr_have_independent_checks(tmp_path):
    db = tmp_path / "index.sqlite"
    store = Store(db)
    audio = store.upsert_file(FileRec(path=str(tmp_path / "speech"), kind="audio"))
    store.replace_segments(audio, [SegmentRec(kind="audio", modality="text", loc=0,
                                            loc_end=12, text_for_fts="real indexed transcript")])
    photo = store.upsert_file(FileRec(path=str(tmp_path / "shot"), kind="image"))
    store.replace_segments(photo, [SegmentRec(kind="ocr", modality="text", text_for_fts="actual OCR")])
    store.close()
    queries = [{"id": need, "tag": "media", "needs": [need],
                "expect": [{"path": file, "grade": 3}]}
               for need, file in [("asr", "speech"), ("ocr", "shot")]]
    a = inspect_index(db, queries, tmp_path)
    b = inspect_index(db, queries, tmp_path)
    assert a == b and a["missing"] == []
