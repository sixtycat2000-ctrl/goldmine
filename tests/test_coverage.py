"""Item 10 self-checks: the media-eval manifest and the coverage KPI harness.

- evals/media_queries.jsonl: 50 labeled queries in 01 §5.1's schema
  ({id, q, expect:[{path, lines?, grade}], tag} + optional needs:[asr|ocr|exif]);
  every expect path must exist in the deterministic coverage fixture.
- make_coverage_fixture: fast/offline (speech + ffmpeg stubbed), path list
  is seed-deterministic.
- coverage_report --fixture --gate: end-to-end KPI over the fixture with the
  mock multimodal backend; every enforced floor passes offline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

QUERIES = REPO_ROOT / "evals" / "media_queries.jsonl"

#: video-eval-001 pinned the clip corpus with ABSOLUTE repo paths (frozen
#: asset bytes). On any other checkout — CI runners included — those roots do
#: not exist; rebase them onto THIS repo root instead of failing portability.
_ORIGINAL_ROOT = "/Users/yul/git/goldmine"


def _portable(path: str) -> Path:
    p = Path(path)
    if p.is_absolute() and str(p).startswith(_ORIGINAL_ROOT) and not p.is_file():
        return REPO_ROOT / str(p)[len(_ORIGINAL_ROOT):].lstrip("/")
    return p

# video-eval-001: +8 keyframe-retrieval questions (tag video, needs frames,
# absolute clip paths + expect loc — see the schema assertions below)
TAG_COUNTS = {"text": 10, "audio": 10, "screenshot": 10, "image": 10, "pdf-scanned": 10, "video": 8}
NEEDS_VALUES = {"asr", "ocr", "exif", "frames"}


@pytest.fixture
def fast_fixture(monkeypatch, tmp_path):
    """make_coverage_fixture with the slow external tools stubbed: speech -> a
    1 s tone (path identical, content irrelevant offline), mp4 not created
    (manifest records created=false — exactly the honest-degradation path)."""
    import make_coverage_fixture as mcf

    monkeypatch.setattr(
        mcf, "speech_wav",
        lambda path: (mcf.synth_wav(path, 2.5, freqs=(220.0, 330.0)), False)[1],
    )
    monkeypatch.setattr(mcf, "ffmpeg_mp4", lambda path, seconds: False)
    out = tmp_path / "covfix"
    manifest = mcf.build(out, force=True)
    return out, manifest


def load_queries() -> list[dict]:
    lines = QUERIES.read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_media_queries_schema():
    queries = load_queries()
    assert len(queries) == 58
    ids = [q["id"] for q in queries]
    assert len(set(ids)) == 58
    assert all(q["id"].startswith("media-") for q in queries)
    counts: dict[str, int] = {}
    for q in queries:
        assert q["q"].strip(), f"{q['id']}: empty query"
        assert isinstance(q["expect"], list) and q["expect"], f"{q['id']}: no expect"
        for entry in q["expect"]:
            assert set(entry) <= {"path", "lines", "grade", "loc"}, f"{q['id']}: {entry}"
            if q.get("tag") == "video":
                # video-eval-001: keyframe questions point at the committed
                # clip corpus (absolute) with the keyframe moment in `loc`
                assert Path(entry["path"]).is_absolute(), "video expects are absolute"
                assert "loc" in entry
            else:
                assert Path(entry["path"]).is_absolute() is False, "fixture-relative paths"
            if "grade" in entry:
                assert 0 <= entry["grade"] <= 3
            if "lines" in entry:
                assert entry["lines"][0] <= entry["lines"][1]
        counts[q["tag"]] = counts.get(q["tag"], 0) + 1
        needs = q.get("needs", [])
        assert isinstance(needs, list)
        assert set(needs) <= NEEDS_VALUES, f"{q['id']}: unknown needs {needs}"
    assert counts == TAG_COUNTS
    # the needs markers exist so item 12's runner can skip until ASR/OCR/EXIF
    tagged = {n for q in queries for n in q.get("needs", [])}
    assert tagged == NEEDS_VALUES


def test_media_queries_expect_paths_exist_in_fixture(fast_fixture):
    out, manifest = fast_fixture
    known = {f["path"] for f in manifest["files"]}
    on_disk = {
        f["path"] for f in manifest["files"] if f["created"] and (out / f["path"]).is_file()
    }
    for q in load_queries():
        for entry in q["expect"]:
            if Path(entry["path"]).is_absolute():
                # video-eval-001: clip-corpus expects live in the repo, pinned
                # by corpus_manifest.json (not the fixture). _portable rebases
                # the frozen absolute pins onto this checkout for portability.
                assert _portable(entry["path"]).is_file(), f"{q['id']}: {entry['path']} missing"
                continue
            assert entry["path"] in known, f"{q['id']}: {entry['path']} not in fixture"
            assert entry["path"] in on_disk, f"{q['id']}: {entry['path']} not on disk"


def test_fixture_path_list_is_seed_deterministic(monkeypatch, tmp_path):
    import make_coverage_fixture as mcf

    monkeypatch.setattr(
        mcf, "speech_wav",
        lambda path: (mcf.synth_wav(path, 2.5, freqs=(220.0, 330.0)), False)[1],
    )
    monkeypatch.setattr(mcf, "ffmpeg_mp4", lambda path, seconds: False)
    a = mcf.build(tmp_path / "a", force=True)
    b = mcf.build(tmp_path / "b", force=True)
    assert [f["path"] for f in a["files"]] == [f["path"] for f in b["files"]]
    # budget contract
    assert len(a["files"]) >= 25
    # F1 BYTE-DETERMINISM: every file (incl. the zip/tar.gz/docx/epub
    # containers and manifest.json itself) hashes identically across two
    # builds — no wall clock anywhere in the tree, or the tracked corpus
    # manifest's sha pins break on a fresh machine.
    import hashlib

    def _digest(root):
        out = {}
        for f in a["files"]:
            if not f["created"]:
                continue
            out[f["path"]] = hashlib.sha256(
                (root / f["path"]).read_bytes()
            ).hexdigest()
        # manifest.json is deterministic modulo the root path it describes
        body = json.loads((root / "manifest.json").read_text())
        body.pop("root")
        out["manifest.json"] = hashlib.sha256(
            json.dumps(body, indent=1).encode()
        ).hexdigest()
        return out

    assert _digest(tmp_path / "a") == _digest(tmp_path / "b")


def test_fixture_containers_and_surrogate_are_deterministic(tmp_path):
    """F1 unit view: the zip containers carry pinned epoch metadata, the
    gzip/tar archive is byte-stable, and the say-wav surrogate hashes the
    transcript+tool+duration bucket — never the volatile raw bytes."""
    import gzip
    import hashlib
    import io
    import tarfile
    import zipfile

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from conftest import make_docx, pinned_zipinfo

    for out in (tmp_path / "x", tmp_path / "y"):
        out.mkdir()
        make_docx(out / "r.docx", ["body"], title="T")
        with zipfile.ZipFile(out / "d.zip", "w") as zf:
            zf.writestr(pinned_zipinfo("inner.txt"), "payload")
        with open(out / "b.tar.gz", "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w") as tf:
                    info = tarfile.TarInfo("i.log")
                    info.size = 4
                    info.mtime = 0
                    tf.addfile(info, io.BytesIO(b"body"))
    for name in ("r.docx", "d.zip", "b.tar.gz"):
        a = (tmp_path / "x" / name).read_bytes()
        b = (tmp_path / "y" / name).read_bytes()
        assert a == b, f"{name} is not byte-deterministic"
    # the zip entries really carry the pinned timestamp + mode
    with zipfile.ZipFile(tmp_path / "x" / "d.zip") as zf:
        zi = zf.getinfo("inner.txt")
        assert zi.date_time == (1980, 1, 1, 0, 0, 0)
    # surrogate: stable for identical inputs, sensitive to real drift causes
    import make_coverage_fixture as mcf

    base = mcf.speech_surrogate("say", 300.0)
    assert base == mcf.speech_surrogate("say", 299.0)  # pacing jitter stays in-bucket
    assert base != mcf.speech_surrogate("say", 100.0)  # truncated audio moves it
    assert base != mcf.speech_surrogate("other", 300.0)  # tool change moves it
    assert mcf.speech_surrogate("say", 300.0) == hashlib.sha256(
        json.dumps({"tool": "say",
                    "transcript_sha256": hashlib.sha256(
                        mcf.SPEECH_SCRIPT.encode()).hexdigest(),
                    "duration_bucket": "4-to-7min"}, sort_keys=True).encode()
    ).hexdigest()


def test_scanned_pdf_pages_have_no_text_layer(fast_fixture):
    """The raster pages must read as scanned: pdftotext finds no text in
    atlas-scanned.pdf and only the text pages of the hybrid."""
    import subprocess

    from goldmine.ingest.extract_pdf import pdftotext_path

    if pdftotext_path() is None:
        pytest.skip("pdftotext not installed")
    out, _manifest = fast_fixture
    scanned = subprocess.run(
        [pdftotext_path(), str(out / "pdf" / "atlas-scanned.pdf"), "-"],
        capture_output=True, text=True,
    )
    assert scanned.stdout.strip() == ""
    hybrid = subprocess.run(
        [pdftotext_path(), str(out / "pdf" / "atlas-hybrid.pdf"), "-"],
        capture_output=True, text=True,
    )
    assert "breakwater repair estimate" in hybrid.stdout
    assert "SANDBAR" not in hybrid.stdout.upper()  # raster pages carry no text


#: per-page query tokens the OCR preflight (media-045..050) must be able to
#: recover. min_hits=2 marks the load-bearing tokens the design repeats on
#: two lines — Vision's text detector drops a line now and then
#: (ocr-restore-001 baseline lost KB-77 and SIGNED BY THE HARBOR OFFICE).
_PAGE_QUERY_TOKENS = [
    {"DEPTH 42": 2, "METERS": 1},                    # hybrid page 3  — media-045
    {"SANDBAR": 2, "KB-77": 2},                      # hybrid page 4  — media-046
    {"LEVIATHAN PUMP": 2, "4820 EUR": 1},            # scanned page 1 — media-047/049
    {"VAT 12 PERCENT": 1},                           # scanned page 2 — media-048
    {"HARBOR OFFICE": 2, "STAMP RECEIVED": 1},       # scanned page 3 — media-050
]


def test_scanned_pages_render_enough_text_for_ocr_provenance():
    """ocr-restore-001: pdf_evidence only records info.ocr_pages (the stored
    OCR page provenance the full-media preflight demands) when Vision OCR
    recovers >= THIN_PAGE_CHARS on a page. The scanned page line sets must
    therefore carry realistic amounts of text, stay inside the bitmap font's
    charset, and keep every needs:ocr query token present (fragile tokens on
    two lines)."""
    import make_coverage_fixture as mcf
    from goldmine.ingest.extract_pdf import THIN_PAGE_CHARS

    pages = list(mcf.HYBRID_SCAN_PAGE_LINES) + list(mcf.SCANNED_RECEIPT_PAGE_LINES)
    assert len(pages) == len(_PAGE_QUERY_TOKENS)
    for lines in pages:
        for line in lines:
            assert set(line) <= set(mcf._FONT) | {" "}, f"charset: {line!r}"
        rendered = "\n".join(lines)  # what one clean OCR pass can recover
        assert len(rendered) >= THIN_PAGE_CHARS, (
            f"page {lines[0]!r}: {len(rendered)} chars < {THIN_PAGE_CHARS} — "
            "OCR would classify it low_text and drop ocr_pages provenance")
    for lines, page_tokens in zip(pages, _PAGE_QUERY_TOKENS):
        upper = [ln.upper() for ln in lines]
        for token, need in page_tokens.items():
            hits = sum(token in ln for ln in upper)
            assert hits >= need, (
                f"{token!r} appears {hits}x (< {need}) on page {lines[0]!r}")


def test_screenshot_pngs_contain_rendered_text(fast_fixture):
    """The screenshot PNGs decode and their text rows are not blank: OCR
    (needs:ocr queries) will have real pixels to read when it lands."""
    import zlib

    out, _manifest = fast_fixture
    for name in ("Screenshot 2026-10-11 at 09.15.42.png",
                 "Screenshot 2026-10-12 at 14.02.11.png"):
        data = (out / "images" / "screenshots" / name).read_bytes()
        assert data.startswith(b"\x89PNG")
        # uncompress the first IDAT and check dark pixels exist (the glyphs)
        idx = data.find(b"IDAT")
        length = int.from_bytes(data[idx - 4:idx], "big")
        raw = zlib.decompress(data[idx + 4: idx + 4 + length])
        dark = sum(1 for b in raw if b < 32)
        assert dark > 500, f"{name}: no rendered glyph pixels"


@pytest.fixture
def fixture_db(fast_fixture, tmp_path):
    import coverage_report as cr

    cr.DEFAULT_FIXTURE_DIR = None  # unused; run_fixture takes the dir directly
    db = cr.run_fixture(fast_fixture[0])
    return db


def test_coverage_report_fixture_gate_passes(fixture_db):
    import coverage_report as cr

    report = cr.collect(fixture_db)
    assert report["backend"] == "mock" and report["multimodal"]
    checks = cr.evaluate(report)
    by_name = {c["name"]: c for c in checks}
    for name in ("doc:txt", "doc:md", "doc:html", "doc:docx", "doc:eml", "doc:epub"):
        assert by_name[name]["status"] == "ok", by_name[name]
    assert by_name["code"]["status"] == "ok", by_name["code"]
    assert by_name["pdf-text"]["status"] == "ok", by_name["pdf-text"]
    assert by_name["archives-zero-by-design"]["status"] == "ok"
    # scanned pdfs: metadata_only fixtures stay ADVISORY; with the media
    # capability live (digup-plan-002) thin/scanned pages gain page-image
    # vectors and become content_indexed — the bucket then disappears
    # entirely, which is the good outcome, not a gap.
    if "pdf-scanned" in by_name:
        assert by_name["pdf-scanned"]["status"] == "advisory"
    else:
        assert report["multimodal"], "bucket may only vanish with media vectors"
    failed = [c for c in checks if c["status"] == "fail"]
    assert failed == [], failed
    # audio/video: enforced only with ffprobe (the media window cutter)
    if report["ffprobe"]:
        assert by_name["audio"]["status"] == "ok", by_name["audio"]
        assert by_name["video"]["status"] in ("ok", "skipped")


def test_coverage_report_gate_exit_codes(monkeypatch, fixture_db, capsys):
    import coverage_report as cr

    monkeypatch.setattr(
        cr, "run_fixture", lambda dir_, *, stale="rebuild": fixture_db
    )
    assert cr.main(["--fixture", "--gate"]) == 0
    # a violated floor must exit 1
    report = cr.collect(fixture_db)
    report["fine"]["doc:txt"] = {"content_indexed": 0, "metadata_only": 2}
    monkeypatch.setattr(cr, "collect", lambda db: report)
    assert cr.main([str(fixture_db), "--gate"]) == 1
    out = capsys.readouterr().err
    assert "GATE FAILED" in out and "doc:txt" in out


def test_coverage_floors_text_only_backend_expectation():
    """Under llama (text-only), media floors invert: the honest expectation is
    metadata-only; silent content-indexing or silent failures are gate misses."""
    import coverage_report as cr

    base = {"db": "x", "backend": "llama", "multimodal": False, "ffprobe": True,
            "per_kind": {}}
    honest = dict(base, fine={
        "audio": {"metadata_only": 49, "failed": 1},   # 98% honest metadata-only
        "video": {"metadata_only": 10},
        "image:photo": {"metadata_only": 5},
    })
    checks = {c["name"]: c for c in cr.evaluate(honest)}
    assert checks["audio"]["status"] == "ok"
    assert checks["video"]["status"] == "ok"
    assert checks["image:photo"]["status"] == "ok"
    assert "text-only backend" in checks["audio"]["detail"]

    # half the audio missing entirely (neither indexed nor honestly skipped)
    degraded = dict(base, fine={"audio": {"metadata_only": 50, "unsupported": 50}})
    checks = {c["name"]: c for c in cr.evaluate(degraded)}
    assert checks["audio"]["status"] == "fail"


# ----------------------------------------------- fixture staleness (F9)


def test_fixture_version_file_written_and_matches_generator(fast_fixture):
    import make_coverage_fixture as mcf

    out, manifest = fast_fixture
    stored = json.loads((out / "fixture_version.json").read_text())
    assert stored == mcf.fixture_version_record(manifest)
    assert stored["version"] == mcf.FIXTURE_VERSION
    assert stored["files"] == len(
        [f for f in manifest["files"] if f.get("created")]
    )
    # F1: the generator is fingerprinted by CONTENT, not mtime — an mtime
    # record made every fresh clone's rebuild look stale (gate exit 2)
    import hashlib

    assert stored["generator_sha256"] == hashlib.sha256(
        (REPO_ROOT / "scripts" / "make_coverage_fixture.py").read_bytes()
    ).hexdigest()


def test_ensure_fresh_fixture_rebuilds_on_mismatch(monkeypatch, tmp_path):
    """F9: a stale fixture_version.json (old version constant / drifted
    generator / wrong file count) triggers an automatic forced rebuild."""
    import coverage_report as cr
    import make_coverage_fixture as mcf

    out = tmp_path / "covfix"
    out.mkdir()
    manifest = {"root": str(out),
                "files": [{"path": "a.txt", "kind": "doc", "created": True}]}
    (out / "manifest.json").write_text(json.dumps(manifest))
    (out / "fixture_version.json").write_text(json.dumps(
        {"version": 0, "generator_mtime": 1.0, "files": 3}))

    calls: list[bool] = []

    def fake_build(dir, seed=mcf.SEED, force=False):
        calls.append(force)
        return manifest

    monkeypatch.setattr(mcf, "build", fake_build)
    cr.ensure_fresh_fixture(out)
    # check-build (fast-returns) then the forced rebuild of the stale tree
    assert calls == [False, True]


def test_ensure_fresh_fixture_fresh_tree_needs_no_rebuild(monkeypatch, tmp_path):
    import coverage_report as cr
    import make_coverage_fixture as mcf

    out = tmp_path / "covfix"
    out.mkdir()
    manifest = {"root": str(out),
                "files": [{"path": "a.txt", "kind": "doc", "created": True}]}
    (out / "manifest.json").write_text(json.dumps(manifest))
    (out / "fixture_version.json").write_text(
        json.dumps(mcf.fixture_version_record(manifest))
    )
    calls: list[bool] = []

    def fake_build(dir, seed=mcf.SEED, force=False):
        calls.append(force)
        return manifest

    monkeypatch.setattr(mcf, "build", fake_build)
    cr.ensure_fresh_fixture(out)
    assert calls == [False]  # matched: the check-build, no forced rebuild


def test_ensure_fresh_fixture_no_rebuild_fails_loudly(monkeypatch, tmp_path, capsys):
    """F9: --no-rebuild turns the staleness into a loud refusal (exit 2),
    never a silent report over an outdated tree."""
    import coverage_report as cr
    import make_coverage_fixture as mcf

    out = tmp_path / "covfix"
    out.mkdir()
    manifest = {"root": str(out), "files": []}
    (out / "manifest.json").write_text(json.dumps(manifest))
    (out / "fixture_version.json").write_text(json.dumps(
        {"version": 0, "generator_mtime": 1.0, "files": 3}))

    calls: list[bool] = []

    def fake_build(dir, seed=mcf.SEED, force=False):
        calls.append(force)
        return manifest

    monkeypatch.setattr(mcf, "build", fake_build)
    with pytest.raises(SystemExit) as exc:
        cr.ensure_fresh_fixture(out, stale="fail")
    assert exc.value.code == 2
    assert calls == [False]  # refused BEFORE any rebuild
    assert "stale" in capsys.readouterr().err


def test_ensure_fresh_fixture_missing_version_file_is_stale(monkeypatch, tmp_path):
    """F9: a fixture built by an older generator (no fixture_version.json) is
    stale by definition."""
    import coverage_report as cr
    import make_coverage_fixture as mcf

    out = tmp_path / "covfix"
    out.mkdir()
    manifest = {"root": str(out), "files": []}
    (out / "manifest.json").write_text(json.dumps(manifest))
    calls: list[bool] = []

    def fake_build(dir, seed=mcf.SEED, force=False):
        calls.append(force)
        return manifest

    monkeypatch.setattr(mcf, "build", fake_build)
    cr.ensure_fresh_fixture(out)
    assert calls == [False, True]  # missing version file -> forced rebuild
