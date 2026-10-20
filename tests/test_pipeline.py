"""Tier-2: pipeline end-to-end on the fixture corpus — mock backend and lexical-only.

Covers: capability matrix assignment, content-hash skip, atomic replace on change,
deletion pruning, dry-run writes nothing, fingerprint refusal on ingest.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from conftest import tiny_wav_bytes

from goldmine import fingerprints
from goldmine.backends import BackendUnavailable
from goldmine.backends.base import document_prompt
from goldmine.backends.mock import MockBackend
from goldmine.config import Config
from goldmine.fingerprints import FingerprintMismatch, build
from goldmine.ingest import IngestPipeline, media
from goldmine.store import Store


@pytest.fixture
def cfg(tmp_home):
    c = Config()
    c.home = tmp_home / "gmhome"
    c.backend = "mock"
    return c


def _capabilities(store):
    return {
        row[0]: (row[1], row[2])
        for row in store.db.execute("SELECT path, capability, detail FROM files").fetchall()
    }


def test_capability_matrix_with_mock_backend(cfg, fixture_tree):
    store = Store(cfg.home / "index.sqlite")
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    report = pipeline.run([str(fixture_tree)])
    caps = _capabilities(store)

    def cap(name):
        matches = {p: v for p, v in caps.items() if p.endswith(name)}
        assert matches, f"{name} not ingested at all"
        return next(iter(matches.values()))

    # text formats extract and embed with the mock backend
    for name in ("notes.txt", "guide.md", "page.html", "report.docx",
                 "latin1.txt", "bom.txt", "numbertext.txt"):
        assert cap(name)[0] == "content_indexed", name
    # media: the DEFAULT mock is text-only — metadata-only with the honest reason
    assert cap("Screenshot 2026-01-01 at 10.00.00.png") == (
        "metadata_only", "backend lacks image embedding"
    )
    # secrets / minified / license: unsupported with stable reasons
    assert cap("secret.pem") == ("unsupported", "key or password file")
    assert cap("app.min.js") == ("unsupported", "minified")
    assert cap("LICENSE") == ("unsupported", "license")
    # hidden files still classified: .env surfaces as a secret
    assert any(p.endswith("/.env") and v[0] == "unsupported" for p, v in caps.items())
    # code files off by default
    assert cap("main.py") == ("unsupported", "code or data file")
    # skipped folders never walked
    assert not any("node_modules" in p for p in caps)
    assert not any("__pycache__" in p for p in caps)

    assert report.scanned == 14  # 8 candidates + 6 unsupported file rows
    assert report.content_indexed == 7
    assert report.metadata_only == 1
    assert report.unsupported == 6
    assert report.failed == 0
    assert report.vectors > 0
    assert report.fingerprint is not None
    assert report.fingerprint["backend"] == "mock"

    # vectors are stored fp16, L2-normalized
    table = store.load_vectors()
    assert len(table) == report.vectors
    norms = np.linalg.norm(table.matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=0.01)

    # document prompts were applied by the pipeline (not the backend): the mock
    # vector for a prompted chunk differs from the raw-text vector
    body = store.db.execute(
        "SELECT fts.body FROM fts JOIN segments s ON s.id = fts.rowid "
        "WHERE s.kind='chunk' AND s.file=(SELECT id FROM files WHERE path LIKE '%notes.txt')"
    ).fetchone()[0]
    assert body.strip()
    mock = MockBackend(dim=768)
    prompted = mock.embed_texts([document_prompt("notes", body)])[0]
    raw = mock.embed_texts([body])[0]
    assert not np.allclose(prompted, raw)

    # searchable right away through the store's FTS
    rows = store.search_fts('"tolerations"*', limit=10)
    assert any("notes.txt" in r.name for r in rows)
    store.close()


def test_lexical_only_pipeline(cfg, fixture_tree):
    store = Store(cfg.home / "lex.sqlite")
    pipeline = IngestPipeline(cfg, store, None, lexical_only=True)
    report = pipeline.run([str(fixture_tree)])
    assert report.content_indexed == 7
    assert report.vectors == 0
    assert store.load_vectors().ids == []
    assert any(n["code"] == "lexical_only" for n in report.notices)
    # still keyword-searchable
    rows = store.search_fts('"sourdough"*', limit=5)
    assert rows
    store.close()


def test_no_backend_without_lexical_only_is_refusal(cfg, fixture_tree):
    """SPEC §2: an unavailable backend is a refusal unless --lexical-only."""
    store = Store(cfg.home / "refused.sqlite")
    with pytest.raises(BackendUnavailable):
        IngestPipeline(cfg, store, None, lexical_only=False)
    store.close()


def test_unchanged_skip_and_atomic_replace_and_prune(cfg, fixture_tree):
    store = Store(cfg.home / "index.sqlite")
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    pipeline.run([str(fixture_tree)])
    old_seg_ids = [
        r[0] for r in store.db.execute(
            "SELECT id FROM segments WHERE file="
            "(SELECT id FROM files WHERE path LIKE '%notes.txt')"
        ).fetchall()
    ]

    # second run: every candidate (7 docs + 1 image) unchanged
    report2 = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(fixture_tree)])
    assert report2.unchanged == 8

    # modify one file -> atomic replace (new segment ids), delete another -> pruned
    notes = fixture_tree / "docs" / "notes.txt"
    notes.write_text("Brand new content about revictions.\n", encoding="utf-8")
    gone = fixture_tree / "docs" / "guide.md"
    gone.unlink()
    report3 = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(fixture_tree)])
    assert report3.unchanged == 6
    assert report3.deleted == 1
    new_seg_ids = [
        r[0] for r in store.db.execute(
            "SELECT id FROM segments WHERE file="
            "(SELECT id FROM files WHERE path LIKE '%notes.txt')"
        ).fetchall()
    ]
    assert new_seg_ids and set(new_seg_ids).isdisjoint(old_seg_ids)
    assert not store.find_by_path(str(gone))
    rows = store.search_fts('"revictions"*', limit=5)
    assert rows

    # roots merged in meta
    roots = json.loads(store.meta("roots_json"))
    assert str(fixture_tree) in roots
    store.close()


def test_reindex_clears_and_rebuilds(cfg, fixture_tree):
    store = Store(cfg.home / "index.sqlite")
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(fixture_tree)])
    assert store.counts().vectors > 0
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run(
        [str(fixture_tree)], reindex=True
    )
    assert report.unchanged == 0  # reindex re-extracts everything
    assert store.counts().vectors > 0
    store.close()


# ------------------------------------------------------- adversarial-review pins


def _fts_orphans(store) -> int:
    return store.db.execute(
        "SELECT COUNT(*) FROM fts WHERE rowid NOT IN (SELECT id FROM segments)"
    ).fetchone()[0]


def test_modify_first_ingested_file_no_fts_orphans(cfg, tmp_path):
    """P0-1(a): re-ingesting a modified file refreshes its name segment. The
    doomed segment's FTS row must be deleted with it — FTS5 has no FK cascade,
    and an orphaned row collides (IntegrityError) when the segment id is reused."""
    root = tmp_path / "p0a"
    root.mkdir()
    (root / "a.txt").write_text("alpha first content about wobbegongs\n", encoding="utf-8")
    (root / "b.txt").write_text("beta second content about wobbegongs\n", encoding="utf-8")
    store = Store(cfg.home / "p0a.sqlite")
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert store.counts().segments >= 4

    (root / "a.txt").write_text("alpha rewritten content about wobbegongs\n", encoding="utf-8")
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert report.failed == 0
    assert report.unchanged == 1
    assert _fts_orphans(store) == 0
    bodies = [r[0] for r in store.db.execute("SELECT body FROM fts").fetchall()]
    assert not any("first content" in b for b in bodies)
    assert store.search_fts('"rewritten"')
    store.close()


def test_delete_last_file_then_add_new_no_fts_orphans(cfg, tmp_path):
    """P0-1(b): deleting a file (prune path) must clear its FTS rows, or the
    NEXT ingest crashes with IntegrityError when a new file reuses the freed
    segment ids — and the deleted file's text would stay searchable."""
    root = tmp_path / "p0b"
    root.mkdir()
    (root / "a.txt").write_text("alpha stays put about quokkas\n", encoding="utf-8")
    (root / "b.txt").write_text("beta leaves the index about quokkas\n", encoding="utf-8")
    store = Store(cfg.home / "p0b.sqlite")
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])

    (root / "b.txt").unlink()
    report2 = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert report2.deleted == 1
    assert _fts_orphans(store) == 0

    (root / "c.txt").write_text("gamma arrives fresh about quokkas\n", encoding="utf-8")
    report3 = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert report3.failed == 0
    assert _fts_orphans(store) == 0
    assert store.search_fts('"arrives"')
    assert not store.search_fts('"leaves"')
    store.close()


def test_second_root_ingest_never_prunes_first_root(cfg, tmp_path):
    """P1-2: compute_deletions speaks only for the roots of THIS run —
    ingesting root B into a DB that has root A must not delete A's files."""
    r1 = tmp_path / "r1"
    r1.mkdir()
    (r1 / "a.txt").write_text("alpha root one words\n", encoding="utf-8")
    r2 = tmp_path / "r2"
    r2.mkdir()
    (r2 / "b.txt").write_text("beta root two words\n", encoding="utf-8")
    store = Store(cfg.home / "tworoot.sqlite")
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    pipeline.run([str(r1)])
    report2 = pipeline.run([str(r2)])
    assert report2.deleted == 0
    assert store.find_by_path(str(r1 / "a.txt")) is not None
    assert store.find_by_path(str(r2 / "b.txt")) is not None

    # a file that vanishes under a CURRENT root is still pruned
    (r1 / "a.txt").unlink()
    report3 = pipeline.run([str(r1)])
    assert report3.deleted == 1
    assert store.find_by_path(str(r1 / "a.txt")) is None
    assert store.find_by_path(str(r2 / "b.txt")) is not None
    store.close()


def test_reindex_failed_startup_keeps_index_intact(cfg, fixture_tree):
    """P1-3: --reindex clears vectors only AFTER backend startup + the
    fingerprint gate pass. A startup failure must not leave a hollow index
    that the sha-skip would never rebuild."""
    from goldmine.backends.base import BackendStartupError

    store = Store(cfg.home / "reindex.sqlite")
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(fixture_tree)])
    vectors_before = store.counts().vectors
    segments_before = store.counts().segments
    fp_before = store.meta("fingerprint_id")

    class DeadBackend(MockBackend):
        def startup(self):
            raise BackendStartupError("spawn failed in test")

    with pytest.raises(BackendStartupError):
        IngestPipeline(cfg, store, DeadBackend(dim=768)).run(
            [str(fixture_tree)], reindex=True
        )
    assert store.counts().vectors == vectors_before
    assert store.counts().segments == segments_before
    assert store.meta("fingerprint_id") == fp_before
    store.close()


def test_keyboard_interrupt_during_startup_closes_backend(cfg, fixture_tree):
    """P1-4: startup happens inside run()'s try/finally — a Ctrl-C there must
    close the backend (no orphaned llama-server child) and propagate."""
    store = Store(cfg.home / "ki.sqlite")
    closed: list[bool] = []

    class InterruptedBackend(MockBackend):
        def startup(self):
            raise KeyboardInterrupt

        def close(self):
            closed.append(True)
            super().close()

    with pytest.raises(KeyboardInterrupt):
        IngestPipeline(cfg, store, InterruptedBackend(dim=768)).run([str(fixture_tree)])
    assert closed == [True]
    store.close()


def test_failed_reingest_drops_stale_searchable_text(cfg, tmp_path):
    """P1-5: a file that re-ingests as failed loses its old content segments
    (and their FTS rows) — dead text must not stay searchable."""
    from conftest import make_docx

    root = tmp_path / "stale"
    root.mkdir()
    doc = root / "report.docx"
    make_docx(doc, ["Quarterly zanzibar findings body."], title="Report")
    store = Store(cfg.home / "stale.sqlite")
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert store.search_fts('"zanzibar"')

    doc.write_bytes(b"this is not a zip any more")
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert report.failed == 1
    rec = store.find_by_path(str(doc))
    assert rec.state == "failed" and rec.capability == "failed"
    assert not store.search_fts('"zanzibar"')          # stale text gone
    assert _fts_orphans(store) == 0
    kinds = [r[0] for r in store.db.execute(
        "SELECT kind FROM segments WHERE file = ?", (rec.id,)
    ).fetchall()]
    assert kinds == ["name"]                            # name row survives
    store.close()


def test_encoding_override_flows_through_pipeline(cfg, tmp_path):
    """P2-9: pipeline-level encoding override replaces decode detection."""
    root = tmp_path / "enc"
    root.mkdir()
    (root / "legacy.txt").write_text("café crème — brève", encoding="windows-1252")
    store = Store(cfg.home / "enc1.sqlite")
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    rec = store.find_by_path(str(root / "legacy.txt"))
    assert rec.info["decode"] == "lossy-decode: windows-1252"
    store.close()

    store2 = Store(cfg.home / "enc2.sqlite")
    IngestPipeline(cfg, store2, MockBackend(dim=768)).run(
        [str(root)], encoding_override="windows-1252"
    )
    rec2 = store2.find_by_path(str(root / "legacy.txt"))
    # strict override decode: no lossy note (info empty -> stored as None)
    assert rec2.info is None or rec2.info.get("decode") is None
    store2.close()


def test_dataset_folders_skipped_by_default_included_on_flag(cfg, tmp_path):
    """P2-9: a folder of 300 machine-named media files is detected as a dataset
    (unsupported rows, machine-stable reason); --include-datasets skips the
    detection entirely."""
    root = tmp_path / "ds-root"
    root.mkdir()
    shards = root / "shards"
    shards.mkdir()
    for i in range(300):
        (shards / f"frame_{i:06d}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    (root / "note.txt").write_text("regular note words\n", encoding="utf-8")

    store = Store(cfg.home / "ds1.sqlite")
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert report.unsupported == 300
    n = store.db.execute(
        "SELECT COUNT(*) FROM files WHERE detail = 'dataset folder'"
    ).fetchone()[0]
    assert n == 300
    store.close()

    store2 = Store(cfg.home / "ds2.sqlite")
    report2 = IngestPipeline(cfg, store2, MockBackend(dim=768)).run(
        [str(root)], include_datasets=True
    )
    assert report2.unsupported == 0
    assert report2.metadata_only == 300  # default mock: text-only => metadata-only
    assert not store2.db.execute(
        "SELECT COUNT(*) FROM files WHERE detail = 'dataset folder'"
    ).fetchone()[0]
    store2.close()


def test_relative_root_then_absolute_is_no_churn(cfg, tmp_path, monkeypatch):
    """P2-11: roots are absolutized at the walk boundary — ingesting `sub`
    relative to a parent cwd stores the same absolute rows as ingesting the
    resolved path, so the re-run is skipped-unchanged, not delete+re-add."""
    parent = tmp_path / "relparent"
    sub = parent / "sub"
    sub.mkdir(parents=True)
    (sub / "note.txt").write_text("stable words about revictions\n", encoding="utf-8")
    store = Store(cfg.home / "rel.sqlite")
    monkeypatch.chdir(parent)
    IngestPipeline(cfg, store, MockBackend(dim=768)).run(["sub"])
    rec = store.find_by_path(str(sub / "note.txt"))
    assert rec is not None  # stored under the absolute form

    monkeypatch.chdir(tmp_path)  # different CWD on purpose
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(sub)])
    assert report.unchanged == 1
    assert report.deleted == 0
    assert store.db.execute(
        "SELECT COUNT(*) FROM files WHERE path = ?", (str(sub / "note.txt"),)
    ).fetchone()[0] == 1
    store.close()


def test_dry_run_writes_nothing(cfg, fixture_tree):
    db = cfg.home / "dry.sqlite"
    pipeline = IngestPipeline(cfg, None, None, lexical_only=True)
    report = pipeline.run([str(fixture_tree)], dry_run=True)
    assert report.dry_run
    assert report.scanned > 0
    assert not db.exists()
    assert report.vectors == 0
    assert any(n["code"] == "cap_impact" for n in report.notices)


def test_fingerprint_mismatch_refuses_ingest(cfg, fixture_tree):
    store = Store(cfg.home / "index.sqlite")
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(fixture_tree)])
    # flip the recorded fingerprint: a different model identity
    forged = build(MockBackend(dim=256))
    store.set_meta("fingerprint_id", fingerprints.fingerprint_id(forged))
    store.set_meta("fingerprint_json", fingerprints.canonical(forged).decode())
    with pytest.raises(FingerprintMismatch):
        IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(fixture_tree)])
    store.close()


def test_mixed_vector_and_vectorless_chunks(cfg, tmp_path):
    """Digits-only text stays vectorless (FTS only), lettered text gets vectors."""
    root = tmp_path / "mixed"
    root.mkdir()
    (root / "digits.txt").write_text("4815162342 0099887766\n", encoding="utf-8")
    (root / "words.txt").write_text("real words about tolerate systems\n", encoding="utf-8")
    store = Store(cfg.home / "mixed.sqlite")
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert report.content_indexed == 2
    table = store.load_vectors()
    paths_with_vecs = {store.get_file(fid).path for fid in table.file_ids}
    assert str(root / "words.txt") in paths_with_vecs
    assert str(root / "digits.txt") not in paths_with_vecs
    # ...but the digits file is still content-indexed via FTS
    assert store.search_fts('"4815162342"', limit=5)
    store.close()


# ------------------------------------------------------------------ media (v2)
#
# The multimodal pipeline: with a backend that declares image/audio capability
# (foresight live; MockBackend(multimodal=True) offline) media files become
# content-indexed. The default mock stays text-only — those pins live above.


def _mm_mock() -> MockBackend:
    return MockBackend(dim=768, multimodal=True)


def test_multimodal_mock_indexes_image_content(cfg, fixture_tree):
    """v2 image path: sips thumbnail -> embed_image(thumbnail). The fixture
    screenshot is a real 600x400 PNG (below the 1024 budget, so no upscale);
    the vector is the THUMBNAIL's bytes-hash, and the row carries thumb info.
    Exact-vector equality is covered by test_media-style direct calls; here we
    pin shape + no-FTS + info (OCR is off in offline tests: no helper)."""
    store = Store(cfg.home / "mm-image.sqlite")
    report = IngestPipeline(cfg, store, _mm_mock()).run([str(fixture_tree)])
    png = fixture_tree / "docs" / "Screenshot 2026-01-01 at 10.00.00.png"

    rec = store.find_by_path(str(png))
    assert rec and rec.capability == "content_indexed"
    assert rec.extractor_version == "image-sips.v2"
    rows = store.db.execute(
        "SELECT id, kind, modality, loc, loc_end, excerpt, vec FROM segments "
        "WHERE file = ?", (rec.id,),
    ).fetchall()
    image_rows = [r for r in rows if r[1] == "image"]
    assert len(image_rows) == 1
    seg_id, kind, modality, loc, loc_end, excerpt_text, blob = image_rows[0]
    assert kind == "image" and modality == "image"
    assert loc is None and loc_end is None          # images carry no location
    assert excerpt_text == png.name                 # filename as excerpt
    assert blob is not None and len(blob) == 768 * 2
    # media vectors carry NO prompt; media segments have no FTS row of their
    # own (the name row carries keywords)
    assert store.db.execute(
        "SELECT COUNT(*) FROM fts WHERE rowid = ?", (seg_id,)
    ).fetchone()[0] == 0
    # the screenshot flag + thumbnail info survive; OCR honestly absent
    assert rec.info.get("screenshot") is True
    assert rec.info.get("thumb") == "600x400"
    assert rec.info.get("ocr") == "unavailable"    # helper absent in tests
    assert report.content_indexed == 8 and report.metadata_only == 0
    store.close()


def test_multimodal_mock_audio_windows_with_loc_times(cfg, tmp_path, monkeypatch):
    """wav file -> one 'audio' segment per window with start/end seconds as
    loc/loc_end. Window extraction is monkeypatched so the offline suite never
    needs ffmpeg (real windowing is covered by test_media + the live suite)."""
    from goldmine.ingest.media import MediaPiece

    root = tmp_path / "audio-root"
    root.mkdir()
    clip = root / "clip.wav"
    clip.write_bytes(tiny_wav_bytes(0.2))

    def fake_windows(path, tmp, **kwargs):
        assert kwargs.get("max_minutes") == cfg.ingest.audio_max_minutes
        one, two = tmp / "w0.wav", tmp / "w1.wav"
        one.write_bytes(tiny_wav_bytes(0.05))
        two.write_bytes(tiny_wav_bytes(0.05))
        return [
            MediaPiece(path=one, start=0.0, end=30.0),
            MediaPiece(path=two, start=25.0, end=40.0),
        ]

    monkeypatch.setattr("goldmine.ingest.media.ffprobe_path", lambda: "/fake/ffprobe")
    monkeypatch.setattr("goldmine.ingest.media.ffprobe_duration", lambda p: 40.0)
    monkeypatch.setattr("goldmine.ingest.media.audio_windows", fake_windows)

    store = Store(cfg.home / "mm-audio.sqlite")
    report = IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(clip))
    assert rec and rec.capability == "content_indexed"
    assert rec.info["duration"] == 40.0 and rec.info["windows"] == 2
    rows = store.db.execute(
        "SELECT kind, modality, loc, loc_end, excerpt, vec FROM segments "
        "WHERE file = ? AND kind = 'audio' ORDER BY loc", (rec.id,),
    ).fetchall()
    assert len(rows) == 2
    assert [(r[2], r[3]) for r in rows] == [(0.0, 30.0), (25.0, 40.0)]
    for kind, modality, _loc, _loc_end, label, blob in rows:
        assert kind == "audio" and modality == "audio"
        assert label.startswith("audio ") and len(label) < 240
        assert blob is not None and len(blob) == 768 * 2
    assert report.content_indexed == 1
    assert report.vectors >= 2
    store.close()


def test_multimodal_audio_cap_marks_partial(cfg, tmp_path, monkeypatch):
    """A source longer than audio_max_minutes is capped: partial + read_end."""
    from goldmine.ingest.media import MediaPiece

    root = tmp_path / "cap-root"
    root.mkdir()
    (root / "long.wav").write_bytes(tiny_wav_bytes(0.2))

    def fake_windows(path, tmp, **kwargs):
        one = tmp / "w0.wav"
        one.write_bytes(tiny_wav_bytes(0.05))
        return [MediaPiece(path=one, start=0.0, end=kwargs["max_minutes"] * 60)]

    monkeypatch.setattr("goldmine.ingest.media.ffprobe_path", lambda: "/fake/ffprobe")
    monkeypatch.setattr("goldmine.ingest.media.ffprobe_duration", lambda p: 3600.0)
    monkeypatch.setattr("goldmine.ingest.media.audio_windows", fake_windows)
    cfg.ingest.audio_max_minutes = 0.5  # 30 s cap

    store = Store(cfg.home / "mm-cap.sqlite")
    IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(root / "long.wav"))
    assert rec and rec.capability == "content_indexed"
    assert rec.info["partial"] is True
    assert rec.info["read_end"] == 30.0
    store.close()


def test_multimodal_all_silent_audio_is_metadata_only(cfg, tmp_path, monkeypatch):
    root = tmp_path / "silent-root"
    root.mkdir()
    (root / "quiet.wav").write_bytes(tiny_wav_bytes(0.2))
    monkeypatch.setattr("goldmine.ingest.media.ffprobe_path", lambda: "/fake/ffprobe")
    monkeypatch.setattr("goldmine.ingest.media.ffprobe_duration", lambda p: 31.0)
    monkeypatch.setattr("goldmine.ingest.media.audio_windows", lambda path, tmp, **k: [])
    store = Store(cfg.home / "mm-silent.sqlite")
    IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(root / "quiet.wav"))
    assert rec and rec.capability == "metadata_only"
    assert "silent" in rec.detail
    store.close()


def test_multimodal_undecodable_image_is_metadata_only(cfg, tmp_path):
    """v2: the 20 MB raw-byte cap is GONE (sips thumbnails bound the payload);
    a file sips cannot decode — whatever its size — is an honest metadata-only
    row carrying the tool's error, never a failed one."""
    root = tmp_path / "big-root"
    root.mkdir()
    big = root / "big.png"
    with open(big, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.truncate((20 << 20) + 1)  # sparse: size without 20 MB of writes
    store = Store(cfg.home / "mm-big.sqlite")
    report = IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(big))
    assert rec and rec.capability == "metadata_only"
    assert "sips could not decode" in rec.detail
    n = store.db.execute(
        "SELECT COUNT(*) FROM segments WHERE modality = 'image'"
    ).fetchone()[0]
    assert n == 0
    assert report.metadata_only == 1
    store.close()


def test_lexical_only_with_multimodal_backend_stays_metadata_only(cfg, fixture_tree):
    """--lexical-only means no embeddings at all: media has no text for FTS."""
    store = Store(cfg.home / "mm-lex.sqlite")
    pipeline = IngestPipeline(cfg, store, _mm_mock(), lexical_only=True)
    report = pipeline.run([str(fixture_tree)])
    assert report.vectors == 0
    caps = _capabilities(store)
    shot = next(v for p, v in caps.items() if p.endswith(".png"))
    assert shot[0] == "metadata_only"
    assert "lexical-only" in shot[1]
    store.close()


def test_dry_run_previews_media_capability(cfg, fixture_tree):
    """--dry-run reflects the backend's declared media capability (no DB writes)."""
    report_mm = IngestPipeline(cfg, None, _mm_mock()).run(
        [str(fixture_tree)], dry_run=True
    )
    assert report_mm.dry_run
    assert report_mm.content_indexed == 8     # 7 docs + the screenshot
    assert report_mm.metadata_only == 0
    # no backend (lexical-only construction): the image stays metadata-only
    report_lex = IngestPipeline(cfg, None, None, lexical_only=True).run(
        [str(fixture_tree)], dry_run=True
    )
    assert report_lex.content_indexed == 7
    assert report_lex.metadata_only == 1


@pytest.mark.skipif(
    media.ffmpeg_path() is None or media.ffprobe_path() is None,
    reason="ffmpeg/ffprobe not installed (brew install ffmpeg)",
)
def test_video_soundtrack_windows_from_real_ffmpeg(cfg, tmp_path):
    """Video v1 = ffprobe metadata + soundtrack windows (frames are a stretch)."""
    from goldmine.util import run_tool

    root = tmp_path / "video-root"
    root.mkdir()
    video = root / "clip.mp4"
    result = run_tool(
        [media.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-shortest", "-y", str(video)],
        timeout_s=60.0,
    )
    if result.rc != 0:
        pytest.skip(f"lavfi source unavailable: {result.stderr.strip()[:120]}")

    store = Store(cfg.home / "mm-video.sqlite")
    report = IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(video))
    assert rec and rec.capability == "content_indexed"
    assert rec.info["duration"] == pytest.approx(2.0, abs=0.3)
    rows = store.db.execute(
        "SELECT kind, modality, loc, loc_end, vec FROM segments "
        "WHERE file = ? AND kind = 'audio'", (rec.id,),
    ).fetchall()
    assert len(rows) == 1                      # frames: deliberately not built
    assert rows[0][1] == "audio"
    assert rows[0][2] == 0.0 and rows[0][3] == pytest.approx(2.0, abs=0.3)
    assert rows[0][4] is not None
    assert not store.db.execute(
        "SELECT COUNT(*) FROM segments WHERE file = ? AND kind = 'frame'", (rec.id,)
    ).fetchone()[0]
    assert report.content_indexed == 1
    store.close()


@pytest.mark.skipif(
    media.ffmpeg_path() is None or media.ffprobe_path() is None,
    reason="ffmpeg/ffprobe not installed (brew install ffmpeg)",
)
def test_video_without_audio_stream_is_metadata_only(cfg, tmp_path):
    """A soundtrack-less video is honestly metadata-only, never failed."""
    from goldmine.util import run_tool

    root = tmp_path / "mute-root"
    root.mkdir()
    video = root / "mute.mp4"
    result = run_tool(
        [media.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-y", str(video)],
        timeout_s=60.0,
    )
    if result.rc != 0:
        pytest.skip(f"lavfi source unavailable: {result.stderr.strip()[:120]}")

    store = Store(cfg.home / "mm-mute.sqlite")
    report = IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(video))
    assert rec and rec.capability == "metadata_only"
    assert rec.detail == "no audio stream"
    assert report.failed == 0
    store.close()


# ======================================================= v2 OCR (item B4) ====
#
# The helper is the conftest Python stub (GOLDMINE_OCR_HELPER); screenshots
# are real >= minSide PNGs so the sips thumbnail path also runs for real.

@pytest.fixture
def fake_ocr(tmp_home, monkeypatch):
    from conftest import write_fake_ocr_helper

    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(write_fake_ocr_helper(tmp_home)))
    return True


@pytest.fixture
def fake_ocr_long(tmp_home, monkeypatch):
    """Same stub, canned text > 200 chars so thin PDF pages actually upgrade."""
    from conftest import FAKE_OCR_LONG_TEXT, write_fake_ocr_helper

    monkeypatch.setenv(
        "GOLDMINE_OCR_HELPER",
        str(write_fake_ocr_helper(tmp_home, text=FAKE_OCR_LONG_TEXT)),
    )
    return True


def _screenshot(tmp_path, name="Screenshot 2026-06-01 at 09.00.00.png"):
    from conftest import tiny_png

    root = tmp_path / "shots"
    root.mkdir(exist_ok=True)
    shot = root / name
    shot.write_bytes(tiny_png(600, 400))
    return root, shot


def test_screenshot_ocr_under_multimodal_is_fts_only(cfg, tmp_path, fake_ocr):
    """Foresight-like policy (DigUp Indexer.swift:510-513): the image vector
    EXISTS, so the OCR words are FTS-only — kind='ocr', text indexed, vec NULL."""
    from conftest import FAKE_OCR_TEXT

    root, shot = _screenshot(tmp_path)
    store = Store(cfg.home / "ocr-mm.sqlite")
    report = IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(shot))
    assert rec and rec.capability == "content_indexed"
    assert rec.extractor_version == "image-sips-ocr.v2"
    assert rec.info["ocr_chars"] == len(FAKE_OCR_TEXT)
    rows = store.db.execute(
        "SELECT s.kind, s.modality, f.body, s.vec FROM segments s "
        "LEFT JOIN fts f ON f.rowid = s.id "
        "WHERE s.file = ? AND s.kind IN ('image','ocr')", (rec.id,),
    ).fetchall()
    by_kind = {r[0]: r for r in rows}
    assert set(by_kind) == {"image", "ocr"}
    img_mod, img_fts, img_vec = by_kind["image"][1], by_kind["image"][2], by_kind["image"][3]
    ocr_mod, ocr_fts, ocr_vec = by_kind["ocr"][1], by_kind["ocr"][2], by_kind["ocr"][3]
    assert img_mod == "image" and img_vec is not None and img_fts is None
    assert ocr_mod == "none" and ocr_vec is None     # the no-vector policy
    assert ocr_fts == FAKE_OCR_TEXT                  # exact lookup works
    # the OCR text is searchable via FTS
    hits = store.db.execute(
        "SELECT rowid FROM fts WHERE fts MATCH ?", ('"externally managed"',)
    ).fetchall()
    assert hits
    assert report.content_indexed == 1
    store.close()


def test_screenshot_ocr_under_text_only_backend_gets_text_vector(cfg, tmp_path, fake_ocr):
    """A5 policy: under a text-only backend (llama stand-in = default mock)
    there is no image vector, so the OCR text IS embedded (document-prompted);
    ingest.ocr_text_vector is the switch."""
    from conftest import FAKE_OCR_TEXT
    from goldmine.backends.base import document_prompt

    root, shot = _screenshot(tmp_path)
    store = Store(cfg.home / "ocr-llama.sqlite")
    backend = MockBackend(dim=768)                  # text-only
    report = IngestPipeline(cfg, store, backend).run([str(root)])
    rec = store.find_by_path(str(shot))
    assert rec and rec.capability == "content_indexed"   # FTS text counts (SPEC)
    assert rec.extractor_version == "image-sips-ocr.v2"
    row = store.db.execute(
        "SELECT s.kind, s.modality, f.body, s.vec FROM segments s "
        "LEFT JOIN fts f ON f.rowid = s.id "
        "WHERE s.file = ? AND s.kind = 'ocr'", (rec.id,),
    ).fetchone()
    assert row is not None
    kind, modality, fts_text, blob = row
    assert modality == "text" and blob is not None
    assert fts_text == FAKE_OCR_TEXT
    # the vector is the prompted TEXT embedding (deterministic mock)
    from goldmine.util import unpack_fp16
    expected = MockBackend(dim=768).embed_texts(
        [document_prompt(shot.stem, FAKE_OCR_TEXT)]
    )[0]
    assert np.allclose(unpack_fp16(blob), expected, atol=0.01)
    # no image segment was created (nothing to embed)
    assert store.db.execute(
        "SELECT COUNT(*) FROM segments WHERE file = ? AND kind = 'image'",
        (rec.id,),
    ).fetchone()[0] == 0
    assert report.content_indexed == 1
    store.close()


def test_screenshot_ocr_fts_only_when_text_vector_disabled(cfg, tmp_path, fake_ocr):
    """ingest.ocr_text_vector=False: keyword-only everywhere (the DigUp
    policy applied unconditionally) — still content_indexed via FTS text."""
    root, shot = _screenshot(tmp_path)
    cfg.ingest.ocr_text_vector = False
    store = Store(cfg.home / "ocr-novec.sqlite")
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    rec = store.find_by_path(str(shot))
    assert rec and rec.capability == "content_indexed"
    row = store.db.execute(
        "SELECT modality, vec FROM segments WHERE file = ? AND kind = 'ocr'",
        (rec.id,),
    ).fetchone()
    assert row == ("none", None)
    assert report.vectors == 0
    store.close()


def test_screenshot_ocr_unavailable_stays_honest(cfg, tmp_path):
    """No helper: the v1 behavior — image vector under multimodal, no OCR seg,
    info.ocr='unavailable' (the later-ingest upgrade marker)."""
    root, shot = _screenshot(tmp_path)
    store = Store(cfg.home / "ocr-off.sqlite")
    IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(shot))
    assert rec.capability == "content_indexed"
    assert rec.info.get("ocr") == "unavailable"
    assert store.db.execute(
        "SELECT COUNT(*) FROM segments WHERE file = ? AND kind = 'ocr'",
        (rec.id,),
    ).fetchone()[0] == 0
    store.close()


def test_text_only_screenshot_upgrades_when_ocr_arrives(cfg, tmp_path, monkeypatch):
    """The A5 upgrade path: indexed metadata_only under a text-only backend
    BEFORE the helper existed -> plain ingest re-does it once OCR is ready
    (sha unchanged), and never churns after (info.ocr marker)."""
    from conftest import FAKE_OCR_TEXT, write_fake_ocr_helper

    helper = write_fake_ocr_helper(tmp_path)
    root, shot = _screenshot(tmp_path)
    store = Store(cfg.home / "ocr-upgrade.sqlite")
    monkeypatch.delenv("GOLDMINE_OCR_HELPER", raising=False)       # first ingest: NO helper
    report1 = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    rec = store.find_by_path(str(shot))
    assert report1.metadata_only == 1
    assert rec.detail == "backend lacks image embedding"
    assert rec.info.get("ocr") == "unavailable"

    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(helper))
    report2 = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert report2.unchanged == 0                    # re-done despite same sha
    rec = store.find_by_path(str(shot))
    assert rec.capability == "content_indexed"       # gained OCR text
    assert rec.info.get("ocr_chars") == len(FAKE_OCR_TEXT)

    report3 = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(root)])
    assert report3.unchanged == 1                    # marker stops the churn
    store.close()


def test_min_side_refusal_through_pipeline(cfg, tmp_path):
    """An image whose thumbnail min side < 256 is unsupported with the
    DigUp minImageSide reason (B5)."""
    from conftest import tiny_png

    root = tmp_path / "tiny-root"
    root.mkdir()
    (root / "tiny.png").write_bytes(tiny_png(300, 100))
    store = Store(cfg.home / "mm-minside.sqlite")
    report = IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(root / "tiny.png"))
    assert rec and rec.capability == "unsupported"
    assert "small image" in rec.detail and "DigUp minImageSide" in rec.detail
    assert report.unsupported == 1
    store.close()


def test_screenshots_share_one_helper_spawn(tmp_path, cfg, monkeypatch):
    """02 F7: same-wave screenshots drain through ONE goldmine-ocr spawn (the
    helper's stdin batch contract), not one spawn per screenshot — ~0.15 s
    spawn overhead and a Vision cold start each, up to `workers` concurrent
    ~100 MB Vision hosts. Per-image answers stay isolated (every row still
    gets its own OCR text)."""
    from conftest import tiny_png

    counter = tmp_path / "spawns.txt"
    helper = tmp_path / "counting-goldmine-ocr"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('goldmine-ocr 1')\n"
        "    sys.exit(0)\n"
        "lines = [{'text': 'externally managed environment',\n"
        "          'confidence': 0.97, 'box': [0.0, 0.9, 0.5, 0.05]}]\n"
        "reqs = []\n"
        "for raw in sys.stdin:\n"
        "    raw = raw.strip()\n"
        "    if not raw:\n"
        "        continue\n"
        "    reqs.append(json.loads(raw))\n"
        "with open(\"" + str(counter) + "\", 'a') as f:\n"
        "    f.write(str(len(reqs)))\n"
        "for req in reqs:\n"
        "    print(json.dumps({'id': req['id'], 'path': req['path'],\n"
        "                      'texts': lines, 'ms': 5.0}))\n"
    )
    helper.chmod(0o755)
    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(helper))
    # deterministic gather: quiescence window long enough that any realistic
    # worker jitter (< 1 s between arrivals) still lands in one batch, with a
    # cap far above anything the test can hit
    monkeypatch.setattr("goldmine.ingest.extraction._OCR_BATCH_GATHER_S", 1.0)
    monkeypatch.setattr("goldmine.ingest.extraction._OCR_BATCH_GATHER_MAX_S", 30.0)
    cfg.ingest.workers = 4

    root = tmp_path / "shots"
    root.mkdir()
    names = [f"Screenshot 2026-06-0{i} at 09.00.00.png" for i in (1, 2, 3, 4)]
    for name in names:
        (root / name).write_bytes(tiny_png(600, 400))
    (root / "vacation.png").write_bytes(tiny_png(600, 400))  # no OCR (not a shot)

    store = Store(cfg.home / "ocr-batch.sqlite")
    try:
        report = IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
        assert report.failed == 0
        spawns = counter.read_text() if counter.exists() else ""
        assert spawns == "4", (
            f"expected ONE spawn answering all 4 shots, got counter={spawns!r}"
        )
        for name in names:
            rec = store.find_by_path(str(root / name))
            assert rec and rec.capability == "content_indexed"
            assert rec.info.get("ocr_chars") == len("externally managed environment")
    finally:
        store.close()


def test_ocr_batch_failure_marks_each_row_off(cfg, tmp_path, monkeypatch):
    """A batch-level helper failure (crash -> rc != 0) marks every member
    "off" honestly — never a failed row — and the rows stay re-doable (the
    'unavailable' marker drives _needs_ocr_redo)."""
    from conftest import tiny_png

    bad = tmp_path / "bad-helper"
    bad.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    bad.chmod(0o755)
    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(bad))
    monkeypatch.setattr("goldmine.ingest.extraction._OCR_BATCH_GATHER_S", 1.0)
    monkeypatch.setattr("goldmine.ingest.extraction._OCR_BATCH_GATHER_MAX_S", 30.0)
    cfg.ingest.workers = 4

    root = tmp_path / "shots"
    root.mkdir()
    names = [f"Screenshot 2026-07-0{i} at 09.00.00.png" for i in (1, 2)]
    for name in names:
        (root / name).write_bytes(tiny_png(600, 400))
    store = Store(cfg.home / "ocr-badbatch.sqlite")
    try:
        report = IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
        assert report.failed == 0
        for name in names:
            rec = store.find_by_path(str(root / name))
            # the image vector still exists (multimodal): content_indexed, but
            # the honest per-row "unavailable" marker is set (and re-doable)
            assert rec and rec.capability == "content_indexed"
            assert rec.info.get("ocr") == "unavailable"
            assert not rec.info.get("ocr_chars")
    finally:
        store.close()


def test_exif_taken_reaches_content_date(cfg, tmp_path, monkeypatch):
    """EXIF creation (sips -g creation via prepare_image) -> info.taken and
    files.content_date (05 §4.1c/C10) on the media commit path."""
    import time as _time

    root, shot = _screenshot(tmp_path)
    taken = ("2024-05-17 09:30:00", 1715928600.0)
    real_prep = media.prepare_image

    def prep_with_exif(path, tmp, *, screenshot=False):
        prep = real_prep(path, tmp, screenshot=screenshot)
        prep.taken, prep.taken_epoch = taken
        return prep

    monkeypatch.setattr(media, "prepare_image", prep_with_exif)
    store = Store(cfg.home / "mm-exif.sqlite")
    IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    rec = store.find_by_path(str(shot))
    assert rec and rec.info.get("taken") == "2024-05-17 09:30:00"
    assert rec.content_date == pytest.approx(1715928600.0)
    store.close()


# ------------------------------------------------- scanned-PDF thin upgrade --


@pytest.mark.skipif(
    __import__("goldmine.ingest.extract_pdf", fromlist=["pdftoppm_path"])
    .pdftoppm_path() is None,
    reason="pdftoppm not installed (brew install poppler)",
)
def test_hybrid_pdf_thin_page_upgraded_by_ocr(cfg, tmp_path, fake_ocr_long, make_pdf_fixture):
    """One text page + one thin page: with OCR available the thin page is
    rendered (real pdftoppm) + OCR'd (stub) and becomes vector-eligible
    chunks; the extractor version records visionocr so G10 can find it."""
    from conftest import FAKE_OCR_LONG_TEXT

    sentence = "The keeper maintained the lighthouse log diligently."
    long_text = "\n".join([sentence] * 20)   # lines fit the 612pt page
    pdf = make_pdf_fixture(
        tmp_path / "hybrid.pdf",
        [long_text, "scant"],                    # page 2 < 200 chars
        title="Hybrid Atlas",
    )
    store = Store(cfg.home / "ocr-hybrid.sqlite")
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(tmp_path)])
    rec = store.find_by_path(str(pdf))
    assert rec and rec.capability == "content_indexed"
    assert rec.info.get("ocr_pages") == [2]
    assert "thin_text" not in rec.info           # nothing stayed thin
    assert rec.extractor_version.startswith("pdftotext+visionocr@")
    assert rec.detail == "ocr: 1 page(s)"
    # page 2's OCR text became normal chunks (FTS + vector), not 'page' rows
    rows = store.db.execute(
        "SELECT kind FROM segments WHERE file = ? AND kind = 'page'",
        (rec.id,),
    ).fetchall()
    assert not rows
    hit = store.db.execute(
        "SELECT rowid FROM fts WHERE fts MATCH ?", ('"harbor chart depth"',)
    ).fetchone()
    assert hit is not None
    assert report.content_indexed == 1
    store.close()


@pytest.mark.skipif(
    __import__("goldmine.ingest.extract_pdf", fromlist=["pdftoppm_path"])
    .pdftoppm_path() is None,
    reason="pdftoppm not installed (brew install poppler)",
)
def test_fully_scanned_pdf_becomes_content_indexed(cfg, tmp_path, fake_ocr_long, make_pdf_fixture):
    """No text layer at all: v1's metadata_only 'page needs OCR (scanned)'
    becomes content_indexed once every page OCRs (02 §5.2 Scope B)."""
    pdf = make_pdf_fixture(tmp_path / "scan.pdf", ["", ""])
    store = Store(cfg.home / "ocr-scan.sqlite")
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(tmp_path)])
    rec = store.find_by_path(str(pdf))
    assert rec and rec.capability == "content_indexed"
    assert rec.info.get("ocr_pages") == [1, 2]
    assert report.metadata_only == 0 and report.content_indexed == 1
    store.close()


@pytest.mark.skipif(
    __import__("goldmine.ingest.extract_pdf", fromlist=["pdftoppm_path"])
    .pdftoppm_path() is None,
    reason="pdftoppm not installed (brew install poppler)",
)
def test_thin_pdf_without_ocr_stays_honest(cfg, tmp_path, make_pdf_fixture):
    """No helper: the v1 thin-page row (FTS-only, no vector, thin marker)
    is kept — never faked."""
    pdf = make_pdf_fixture(
        tmp_path / "thin.pdf", [("real " * 20 + "\n") * 3, "scant"]
    )
    store = Store(cfg.home / "ocr-none.sqlite")
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(tmp_path)])
    rec = store.find_by_path(str(pdf))
    assert rec and rec.capability == "content_indexed"
    assert rec.info.get("thin_text") is True
    assert rec.info.get("thin_pages") == [2]
    assert rec.extractor_version.startswith("pdftotext@")
    assert not rec.extractor_version.startswith("pdftotext+visionocr")
    page = store.db.execute(
        "SELECT modality, vec FROM segments WHERE file = ? AND kind = 'page'",
        (rec.id,),
    ).fetchone()
    assert page == ("none", None)
    store.close()


# --------------------------------------------- --refresh-thin + G10 re-do ----


@pytest.mark.skipif(
    __import__("goldmine.ingest.extract_pdf", fromlist=["pdftoppm_path"])
    .pdftoppm_path() is None,
    reason="pdftoppm not installed (brew install poppler)",
)
def test_refresh_thin_selectively_redoes_only_thin_files(
    cfg, tmp_path, monkeypatch, make_pdf_fixture
):
    """--refresh-thin re-processes thin-marked files even when sha unchanged,
    and leaves clean files skipped."""
    from conftest import write_fake_ocr_helper

    sentence = "The keeper maintained the lighthouse log diligently."
    long_text = "\n".join([sentence] * 20)   # lines fit the 612pt page
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    make_pdf_fixture(pdfs / "thin.pdf", [("real " * 20 + "\n") * 3, "scant"])
    make_pdf_fixture(pdfs / "clean.pdf", [long_text, long_text])
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "n.txt").write_text("plain text file\n", encoding="utf-8")

    store = Store(cfg.home / "refresh.sqlite")
    from conftest import FAKE_OCR_LONG_TEXT

    helper = write_fake_ocr_helper(tmp_path, text=FAKE_OCR_LONG_TEXT)
    monkeypatch.delenv("GOLDMINE_OCR_HELPER", raising=False)     # first ingest: NO helper
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(tmp_path)])
    rec = store.find_by_path(str(pdfs / "thin.pdf"))
    assert rec.info.get("thin_text") is True

    # helper lands; plain ingest would SKIP everything (sha unchanged,
    # versions current); --refresh-thin re-does ONLY the thin file
    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(helper))
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run(
        [str(tmp_path)], refresh_thin=True
    )
    assert report.unchanged == 2              # clean.pdf + n.txt
    rec = store.find_by_path(str(pdfs / "thin.pdf"))
    assert rec.info.get("ocr_pages") == [2]   # upgraded now
    assert "thin_text" not in rec.info
    clean = store.find_by_path(str(pdfs / "clean.pdf"))
    assert clean.info.get("ocr_pages") is None
    store.close()


@pytest.mark.skipif(
    __import__("goldmine.ingest.extract_pdf", fromlist=["pdftoppm_path"])
    .pdftoppm_path() is None,
    reason="pdftoppm not installed (brew install poppler)",
)
def test_refresh_thin_redoes_scanned_metadata_only_rows(
    cfg, tmp_path, monkeypatch, make_pdf_fixture
):
    """The no-text-layer row (metadata_only, detail 'page needs OCR (scanned)')
    is refresh-thin eligible even though it never had segments."""
    from conftest import write_fake_ocr_helper

    helper = write_fake_ocr_helper(tmp_path)
    pdf = make_pdf_fixture(tmp_path / "scan.pdf", [""])
    store = Store(cfg.home / "refresh-scan.sqlite")
    monkeypatch.delenv("GOLDMINE_OCR_HELPER", raising=False)
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(tmp_path)])
    rec = store.find_by_path(str(pdf))
    assert rec.capability == "metadata_only"
    assert "page needs OCR" in rec.detail

    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(helper))
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run(
        [str(tmp_path)], refresh_thin=True
    )
    assert report.unchanged == 0
    rec = store.find_by_path(str(pdf))
    assert rec.capability == "content_indexed"
    store.close()


def test_g10_stale_extractor_version_redone_by_plain_ingest(
    cfg, fixture_tree
):
    """The v2 re-do path (02 G10): a done row whose extractor_version is
    outside the current registry is re-extracted on the NEXT ordinary ingest —
    no flags, no refusal. Same mechanism as chunker staleness (D3)."""
    store = Store(cfg.home / "g10.sqlite")
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(fixture_tree)])
    doc = fixture_tree / "docs" / "notes.txt"
    rec = store.find_by_path(str(doc))
    assert rec.extractor_version == "txt-stdlib.v1"

    with store.write_transaction():
        store.db.execute(
            "UPDATE files SET extractor_version = 'txt-stdlib.v0-ancient' "
            "WHERE path = ?", (str(doc),)
        )
    store.db.commit()
    report = IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(fixture_tree)])
    # every OTHER done file skipped; the stale one re-extracted
    assert report.unchanged == report.scanned - report.unsupported - 1
    rec = store.find_by_path(str(doc))
    assert rec.extractor_version == "txt-stdlib.v1"   # re-extracted & current

    # and the stale chunker dimension rides the same check
    with store.write_transaction():
        store.db.execute(
            "UPDATE files SET chunker_version = 'chunk.v0-ancient' WHERE path = ?",
            (str(doc),)
        )
    store.db.commit()
    IngestPipeline(cfg, store, MockBackend(dim=768)).run([str(fixture_tree)])
    rec = store.find_by_path(str(doc))
    assert rec.chunker_version is not None
    assert rec.chunker_version != "chunk.v0-ancient"
    store.close()


def test_g10_old_image_version_redone(cfg, tmp_path):
    """v1 image rows (image-bytes.v1) are stale by design: plain ingest
    re-does them through the sips path (the B5 thumbnail upgrade)."""
    from conftest import tiny_png

    root, shot = _screenshot(tmp_path)
    store = Store(cfg.home / "g10-image.sqlite")
    IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    with store.write_transaction():
        store.db.execute(
            "UPDATE files SET extractor_version = 'image-bytes.v1' WHERE path = ?",
            (str(shot),),
        )
    store.db.commit()
    report = IngestPipeline(cfg, store, _mm_mock()).run([str(root)])
    assert report.unchanged == 0
    rec = store.find_by_path(str(shot))
    assert rec.extractor_version == "image-sips.v2"
    assert rec.info.get("thumb") == "600x400"
    store.close()
