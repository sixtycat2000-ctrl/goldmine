"""Capture versus edit dates, bounded malformed metadata and one-time repairs."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import struct
import sqlite3
import time

import pytest

from goldmine.backends.mock import MockBackend
from goldmine.config import Config
from goldmine.ingest import IngestPipeline, exif, media
from goldmine.store import Store
from goldmine.util import ToolError, ToolResult


def exif_jpeg(*, original="2016:12:23 15:59:38", modified="2017:02:19 23:57:51", offset=None, endian="<"):
    """Small format-valid metadata fixture; it need not encode any image pixels."""
    header = (b"II" if endian == "<" else b"MM") + struct.pack(endian + "HI", 42, 8)
    first_count = 1 + bool(modified)
    exif_offset = 8 + 2 + 12 * first_count + 4
    date_count = bool(original) + bool(offset)
    data_offset = exif_offset + 2 + 12 * date_count + 4
    data = bytearray()

    def entry(tag, text):
        encoded = text.encode("ascii") + b"\0"
        position = data_offset + len(data)
        data.extend(encoded)
        return struct.pack(endian + "HHII", tag, 2, len(encoded), position)

    first = [entry(0x0132, modified)] if modified else []
    first.append(struct.pack(endian + "HHII", 0x8769, 4, 1, exif_offset))
    fields = []
    if original:
        fields.append(entry(0x9003, original))
    if offset:
        fields.append(entry(0x9011, offset))
    tiff = (header + struct.pack(endian + "H", first_count) + b"".join(first) + b"\0" * 4
            + struct.pack(endian + "H", date_count) + b"".join(fields) + b"\0" * 4 + data)
    payload = b"Exif\0\0" + tiff
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + b"\xff\xd9"


@pytest.mark.parametrize("endian", ["<", ">"])
def test_true_original_beats_ifd0_modified_and_does_not_require_sips(endian, tmp_path, monkeypatch):
    path = tmp_path / "camera.jpg"
    payload = exif_jpeg(endian=endian)
    path.write_bytes(payload)
    monkeypatch.setattr(exif, "_imageio_capture", lambda _: pytest.fail("JPEG must not use an ambiguous native fallback"))
    monkeypatch.setattr(media, "sips_path", lambda: None)
    value = media.exif_taken(path)
    assert value[0] == "2016-12-23 15:59:38"
    assert value[1] == time.mktime(time.strptime("2016-12-23 15:59:38", "%Y-%m-%d %H:%M:%S"))
    assert path.read_bytes() == payload


def test_modified_time_without_original_is_not_capture(tmp_path, monkeypatch):
    path = tmp_path / "edited.jpg"
    path.write_bytes(exif_jpeg(original=None))
    monkeypatch.setattr(exif, "_imageio_capture", lambda _: pytest.fail("JPEG must fail closed"))
    assert media.exif_taken(path) is None


def test_duplicate_exif_blocks_and_truncated_trailing_header_are_not_a_date(tmp_path):
    first, second = exif_jpeg(), exif_jpeg(original="2023:05:17 10:00:00")
    path = tmp_path / "ambiguous.jpg"
    for payload in (first[:-2] + second[2:], first[:-2] + b"\xff\xe0\xff\xff"):
        path.write_bytes(payload)
        assert media.exif_taken(path) is None


@pytest.mark.parametrize("offset,utc", [("+05:30", "2016-12-23T10:29:38+00:00"), ("-07:00", "2016-12-23T22:59:38+00:00")])
def test_explicit_offset_sets_epoch_without_changing_camera_wall_clock(tmp_path, offset, utc):
    path = tmp_path / "camera.jpg"
    path.write_bytes(exif_jpeg(offset=offset))
    iso, epoch = media.exif_taken(path)
    assert iso == "2016-12-23 15:59:38"
    assert epoch == datetime.fromisoformat(utc).timestamp()


@pytest.mark.parametrize("value,offset", [("2024:02:31 09:00:00", None), ("0000:00:00 00:00:00", None),
                                          ("2024:05:17 09:30:00", "+99:00"), ("2024:05:17 09:30:00", "UTC")])
def test_invalid_capture_or_offset_is_explicitly_missing(value, offset):
    assert exif.parse_capture_time(value, offset) is None


@pytest.mark.parametrize("mutation", ["truncated", "bad_magic", "ifd_offset", "ifd_count", "ifd_cycle", "ascii_size", "ascii_offset", "no_terminator", "segment_length", "sos_before_exif"])
def test_malformed_tiff_and_jpeg_fail_closed(mutation, tmp_path):
    value = bytearray(exif_jpeg())
    tiff = 12
    if mutation == "truncated":
        value = value[:-15]
    elif mutation == "bad_magic":
        value[tiff + 2:tiff + 4] = b"\x2b\0"
    elif mutation == "ifd_offset":
        struct.pack_into("<I", value, tiff + 4, 0xFFFFFFF0)
    elif mutation == "ifd_count":
        struct.pack_into("<H", value, tiff + 8, 65535)
    elif mutation == "ifd_cycle":
        struct.pack_into("<I", value, tiff + 30, 8)
    elif mutation == "ascii_size":
        struct.pack_into("<I", value, tiff + 44, 0xFFFFFFFF)
    elif mutation == "ascii_offset":
        struct.pack_into("<I", value, tiff + 48, 0xFFFFFFFF)
    elif mutation == "no_terminator":
        value[-3] = ord("X")
    elif mutation == "segment_length":
        value[4:6] = b"\0\x01"
    elif mutation == "sos_before_exif":
        value = b"\xff\xd8\xff\xda" + value[2:]
    path = tmp_path / "bad.jpg"
    path.write_bytes(value)
    assert media.exif_taken(path) is None


def test_header_bytes_segments_and_reads_are_bounded(monkeypatch):
    class CountReads(io.BytesIO):
        largest = 0
        total = 0
        def read(self, count=-1):
            assert count >= 0
            self.largest = max(self.largest, count)
            value = super().read(count)
            self.total += len(value)
            return value
    segment = b"\xff\xe2\0\x02"
    stream = CountReads(segment * 1000)
    assert exif._jpeg_capture(stream) is None
    assert stream.tell() == 4 * exif.MAX_JPEG_SEGMENTS and stream.largest <= 65533
    monkeypatch.setattr(exif, "MAX_HEADER_BYTES", 16)
    stream = CountReads(b"\xff\xe2\xff\xff" + b"a" * 65533)
    assert exif._jpeg_capture(stream) is None and stream.total == 4


@pytest.mark.parametrize("raw", ["null", "[]", "bad JSON", '{}', '{"DateTimeOriginal":2024,"OffsetTimeOriginal":null}',
                               '{"DateTime":"2017:02:19 23:57:51"}', "x" * 257])
def test_malformed_native_response_is_not_capture(monkeypatch, tmp_path, raw):
    path = tmp_path / "other.heic"
    path.write_bytes(b"non-JPEG native fixture")
    monkeypatch.setattr(exif.shutil, "which", lambda _: "/native/osascript")
    monkeypatch.setattr(exif, "run_tool", lambda *args, **kwargs: ToolResult(0, raw, "", False))
    assert media.exif_taken(path) is None


def test_native_dependency_failure_timeout_and_safe_argv(monkeypatch, tmp_path):
    path = tmp_path / "quoted ' and `echo ignored` 中文.heic"
    path.write_bytes(b"non-JPEG native fixture")
    calls = []
    monkeypatch.setattr(exif.shutil, "which", lambda _: "/native/osascript")
    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return ToolResult(0, json.dumps({"DateTimeOriginal": "2024:05:17 09:30:00", "OffsetTimeOriginal": "+00:00"}), "", False)
    monkeypatch.setattr(exif, "run_tool", execute)
    assert media.exif_taken(path)[0] == "2024-05-17 09:30:00"
    argv, kwargs = calls[0]
    assert argv[-1] == str(path.resolve()) and argv[-2] == exif._IMAGEIO_SCRIPT
    assert str(path) not in exif._IMAGEIO_SCRIPT and kwargs == {"timeout_s": exif.IMAGEIO_TIMEOUT_S, "max_output": 1024}
    monkeypatch.setattr(exif.shutil, "which", lambda _: None)
    assert media.exif_taken(path) is None
    monkeypatch.setattr(exif.shutil, "which", lambda _: "/native/osascript")
    monkeypatch.setattr(exif, "run_tool", lambda *args, **kwargs: ToolResult(None, "", "", True))
    assert media.exif_taken(path) is None
    def missing(*args, **kwargs):
        raise ToolError("missing native metadata reader")
    monkeypatch.setattr(exif, "run_tool", missing)
    assert media.exif_taken(path) is None


def test_legacy_metadata_only_photo_is_repaired_once_then_unchanged(tmp_path, monkeypatch):
    cfg = Config(home=str(tmp_path / "home"))
    path = tmp_path / "photos/edited.jpg"
    path.parent.mkdir()
    path.write_bytes(exif_jpeg(original=None))
    backend = MockBackend()
    monkeypatch.setattr(media, "sips_path", lambda: None)
    with closing(Store(tmp_path / "index.sqlite")) as store:
        pipeline = IngestPipeline(cfg, store, backend, lexical_only=True)
        pipeline.run([str(path.parent)])
        record = store.find_by_path(str(path))
        with store.write_transaction():
            store.db.execute("UPDATE files SET info=?,content_date=? WHERE id=?", (json.dumps({"taken": "2017-02-19 23:57:51"}), 1487548671.0, record.id))
        report = pipeline.run([str(path.parent)])
        repaired = store.find_by_path(str(path))
        assert report.unchanged == 0 and repaired.info["taken"] is None and repaired.content_date is None
        assert repaired.info["exif_version"] == exif.METADATA_VERSION
        def expensive(*args, **kwargs):
            pytest.fail("unchanged image must not invoke extraction/OCR/embed/ASR")
        monkeypatch.setattr(pipeline, "_extract", expensive)
        monkeypatch.setattr(backend, "embed_texts", expensive)
        monkeypatch.setattr(backend, "embed_image", expensive)
        monkeypatch.setattr(pipeline, "_ocr_screenshot", expensive)
        report = pipeline.run([str(path.parent)])
        assert report.unchanged == 1
        assert store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_capture_date_clear_and_info_replacement_roll_back_together(tmp_path):
    from goldmine.ingest.commit import IngestReport
    from goldmine.store import FileRec

    with closing(Store(tmp_path / "index.sqlite")) as store:
        fid = store.upsert_file(FileRec(path=str(tmp_path / "photo.jpg"), kind="image", size=1))
        store.mark_file(fid, state="done", capability="metadata_only", detail=None,
                        info={"taken": "old edit time"}, extractor_version=None, content_date=1234.0)
        with store.write_transaction():
            store.db.execute("CREATE TRIGGER fixture_refuse_date_clear BEFORE UPDATE OF content_date ON files "
                             "WHEN OLD.content_date IS NOT NULL AND NEW.content_date IS NULL "
                             "BEGIN SELECT RAISE(ABORT,'fixture clear failure'); END")
        pipeline = IngestPipeline(Config(home=str(tmp_path / "home")), store, None, lexical_only=True)
        with pytest.raises(sqlite3.IntegrityError, match="fixture clear failure"):
            pipeline._finish_file(fid, capability="metadata_only", detail=None,
                                  info={"taken": None, "exif_version": exif.METADATA_VERSION},
                                  extractor_version=None, report=IngestReport(), kind="image", content_date=None)
        record = store.get_file(fid)
        assert record.content_date == 1234.0 and record.info == {"taken": "old edit time"}


def test_native_metadata_reader_arrival_repairs_once(tmp_path, monkeypatch):
    cfg = Config(home=str(tmp_path / "home"))
    path = tmp_path / "camera.heic"
    path.write_bytes(b"synthetic non-JPEG fixture; native reader is a stub")
    monkeypatch.setattr(exif.shutil, "which", lambda _: None)
    with closing(Store(tmp_path / "index.sqlite")) as store:
        pipeline = IngestPipeline(cfg, store, None, lexical_only=True)
        pipeline.run([str(path)])
        assert store.find_by_path(str(path)).info["exif_reader"] is None
        monkeypatch.setattr(exif.shutil, "which", lambda _: "/native/osascript")
        monkeypatch.setattr(exif, "run_tool", lambda *args, **kwargs: ToolResult(0, json.dumps({"DateTimeOriginal": "2024:05:17 09:30:00", "OffsetTimeOriginal": None}), "", False))
        report = pipeline.run([str(path)])
        record = store.find_by_path(str(path))
        assert report.unchanged == 0 and record.info["taken"] == "2024-05-17 09:30:00"
        assert record.info["exif_reader"] == "imageio" and record.content_date is not None
        monkeypatch.setattr(exif, "run_tool", lambda *args, **kwargs: pytest.fail("unchanged metadata must not rerun ImageIO"))
        assert pipeline.run([str(path)]).unchanged == 1


@pytest.mark.parametrize("first", [ToolResult(None, "", "", True), ToolResult(1, "", "native failure", False),
                                   ToolResult(0, "not JSON", "", False), ToolResult(0, "null", "", False)])
def test_transient_native_read_retries_next_ordinary_ingest_then_skips(first, tmp_path, monkeypatch):
    path = tmp_path / "camera.heic"
    path.write_bytes(b"non-JPEG metadata fixture")
    monkeypatch.setattr(exif.shutil, "which", lambda _: "/native/osascript")
    calls = []
    valid = ToolResult(0, json.dumps({"DateTimeOriginal": "2024:05:17 09:30:00", "OffsetTimeOriginal": "+00:00"}), "", False)
    def execute(*args, **kwargs):
        calls.append(1)
        return first if len(calls) == 1 else valid
    monkeypatch.setattr(exif, "run_tool", execute)
    with closing(Store(tmp_path / "index.sqlite")) as store:
        pipeline = IngestPipeline(Config(home=str(tmp_path / "home")), store, None, lexical_only=True)
        pipeline.run([str(path)])
        failed = store.find_by_path(str(path))
        assert failed.info["exif_status"] == "error" and failed.info["taken"] is None and failed.content_date is None
        assert pipeline.run([str(path)]).unchanged == 0
        repaired = store.find_by_path(str(path))
        assert repaired.info["exif_status"] == "complete" and repaired.info["taken"] == "2024-05-17 09:30:00"
        assert repaired.content_date == datetime(2024, 5, 17, 9, 30, tzinfo=timezone.utc).timestamp()
        assert pipeline.run([str(path)]).unchanged == 1 and len(calls) == 2


@pytest.mark.parametrize("response,status", [({"DateTimeOriginal": None, "OffsetTimeOriginal": None}, "absent"),
                                             ({"unsupported": True}, "unsupported")])
def test_successful_native_absence_or_unsupported_is_stable(response, status, tmp_path, monkeypatch):
    path = tmp_path / "no-exif.heic"
    path.write_bytes(b"non-JPEG metadata fixture")
    monkeypatch.setattr(exif.shutil, "which", lambda _: "/native/osascript")
    monkeypatch.setattr(exif, "run_tool", lambda *args, **kwargs: ToolResult(0, json.dumps(response), "", False))
    with closing(Store(tmp_path / "index.sqlite")) as store:
        pipeline = IngestPipeline(Config(home=str(tmp_path / "home")), store, None, lexical_only=True)
        pipeline.run([str(path)])
        record = store.find_by_path(str(path))
        assert record.info["exif_status"] == status and record.info["taken"] is None and record.content_date is None
        monkeypatch.setattr(exif, "run_tool", lambda *args, **kwargs: pytest.fail("successful absence must not churn"))
        assert pipeline.run([str(path)]).unchanged == 1


@pytest.mark.parametrize("prior_kind", ["current_same_bytes", "legacy_same_bytes", "current_changed_bytes"])
def test_transient_refresh_preserves_only_proven_current_date_on_same_bytes(prior_kind, tmp_path, monkeypatch):
    path = tmp_path / "camera.heic"
    path.write_bytes(b"non-JPEG metadata fixture")
    monkeypatch.setattr(exif.shutil, "which", lambda _: "/native/osascript")
    monkeypatch.setattr(exif, "run_tool", lambda *args, **kwargs: ToolResult(0, json.dumps({"DateTimeOriginal": "2024:05:17 09:30:00", "OffsetTimeOriginal": "+00:00"}), "", False))
    with closing(Store(tmp_path / "index.sqlite")) as store:
        pipeline = IngestPipeline(Config(home=str(tmp_path / "home")), store, None, lexical_only=True)
        pipeline.run([str(path)])
        before = store.find_by_path(str(path))
        if prior_kind == "legacy_same_bytes":
            with store.write_transaction():
                store.db.execute("UPDATE files SET info=?,content_date=1234 WHERE id=?", (json.dumps({"taken": "old modified date"}), before.id))
        elif prior_kind == "current_changed_bytes":
            path.write_bytes(b"changed non-JPEG metadata fixture")
        monkeypatch.setattr(exif, "run_tool", lambda *args, **kwargs: ToolResult(None, "", "", True))
        assert pipeline.run([str(path)], reindex=True).unchanged == 0
        after = store.find_by_path(str(path))
        assert after.info["exif_status"] == "error"
        if prior_kind == "current_same_bytes":
            assert after.info["taken"] == before.info["taken"] and after.content_date == before.content_date
            assert after.info["taken_source"] == "Exif.DateTimeOriginal"
        else:
            assert after.info["taken"] is None and after.content_date is None and "taken_source" not in after.info
        # Preserving a known date never masks the failed refresh / retry marker.
        monkeypatch.setattr(exif, "run_tool", lambda *args, **kwargs: ToolResult(0, json.dumps({"DateTimeOriginal": None, "OffsetTimeOriginal": None}), "", False))
        assert pipeline.run([str(path)]).unchanged == 0
        assert store.find_by_path(str(path)).content_date is None
        assert pipeline.run([str(path)]).unchanged == 1


def test_store_default_none_keeps_nonimage_content_date(tmp_path):
    from goldmine.store import FileRec

    with closing(Store(tmp_path / "index.sqlite")) as store:
        fid = store.upsert_file(FileRec(path=str(tmp_path / "doc.txt"), kind="doc", size=1))
        for value in (1234.0, None):
            store.mark_file(fid, state="done", capability="metadata_only", detail=None,
                            info=None, extractor_version=None, content_date=value)
        assert store.get_file(fid).content_date == 1234.0


def test_concurrent_thumbnail_workers_share_one_live_scratch_directory(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import tempfile
    from goldmine.ingest import extraction

    pipeline = IngestPipeline(Config(home=str(tmp_path / "home")), None, None, lexical_only=True)
    entered, release = threading.Event(), threading.Event()
    allocated = []
    real = tempfile.TemporaryDirectory
    def allocate(**kwargs):
        # Hold the first constructor so the peer has time to reach the same
        # initialization path; without synchronization both allocate and one
        # TemporaryDirectory is cleaned while its worker still needs it.
        allocated.append(1)
        if len(allocated) == 1:
            entered.set()
            assert release.wait(5)
        return real(**kwargs)
    monkeypatch.setattr(extraction.tempfile, "TemporaryDirectory", allocate)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(pipeline._media_tmp_dir)
            assert entered.wait(5)
            second = pool.submit(pipeline._media_tmp_dir)
            release.set()
            one, two = first.result(5), second.result(5)
        assert one == two and one.is_dir() and len(allocated) == 1
        (one / "worker-thumbnail").write_bytes(b"scratch ownership fixture")
        assert (two / "worker-thumbnail").read_bytes() == b"scratch ownership fixture"
    finally:
        pipeline._close_media_tmp()
    assert not one.exists()
