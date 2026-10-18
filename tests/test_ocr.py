"""Tier-1/2: OCR client (goldmine/ocr.py) + xattr screenshot detection
(v2 item B4). The helper is a Python stub honoring the JSON-lines contract —
the real Swift helper is exercised by the GOLDMINE_LIVE suite only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FAKE_OCR_TEXT, write_fake_ocr_helper

from goldmine.config import Config
from goldmine.ingest import rules
from goldmine import ocr


@pytest.fixture
def cfg(tmp_home):
    c = Config()
    c.home = tmp_home / "gmhome"
    return c


@pytest.fixture
def fake_helper(tmp_home, monkeypatch):
    path = write_fake_ocr_helper(tmp_home)
    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(path))
    return path


# ------------------------------------------------------------------- client


def test_available_and_handshake(cfg, fake_helper):
    assert ocr.ocr_available(cfg) is True
    assert ocr.handshake(cfg) == ocr.HELPER_VERSION


def test_unavailable_without_helper(cfg):
    assert ocr.helper_path(cfg) is None
    assert ocr.ocr_available(cfg) is False
    assert ocr.handshake(cfg) is None
    with pytest.raises(ocr.OcrUnavailable):
        ocr.recognize_one(Path("/nonexistent.png"), cfg)


def test_recognize_parses_lines_and_filters_confidence(cfg, fake_helper, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"png")
    page = ocr.recognize_one(img, cfg)
    assert page.error is None
    assert page.ms == pytest.approx(5.0)
    assert [line.text for line in page.lines] == [
        FAKE_OCR_TEXT, "low confidence junk",
    ]
    # confidence filter: the 0.20 line is dropped, the 0.97 line kept
    assert page.text() == FAKE_OCR_TEXT
    assert page.text(min_confidence=0.0).count("\n") == 1  # both lines


def test_recognize_batch_ids_roundtrip(cfg, fake_helper, tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"a"); b.write_bytes(b"b")
    pages = ocr.recognize([a, b], cfg)
    assert sorted(pages) == ["0", "1"]
    assert pages["0"].text() == FAKE_OCR_TEXT
    assert pages["1"].text() == FAKE_OCR_TEXT


def test_recognize_empty_input(cfg, fake_helper):
    assert ocr.recognize([], cfg) == {}


def test_recognize_timeout_raises(cfg, fake_helper, monkeypatch, tmp_path):
    img = tmp_path / "slow.png"
    img.write_bytes(b"png")
    monkeypatch.setenv("FAKE_OCR_SLEEP", "2")
    with pytest.raises(ocr.OcrTimeout):
        ocr.recognize([img], cfg, timeout_per_image=0.05, cold_start_s=0.05)


def test_recognize_helper_crash_raises_unavailable(cfg, tmp_home, monkeypatch, tmp_path):
    bad = tmp_path / "bad-helper"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(bad))
    with pytest.raises(ocr.OcrUnavailable):
        ocr.recognize_one(tmp_path / "x.png", cfg)


def test_ensure_helper_respects_autocompile_off(cfg, tmp_home):
    # tmp_home sets GOLDMINE_OCR_AUTOCOMPILE=0: no compile attempt, honest note
    path, note = ocr.ensure_helper(cfg)
    assert path is None
    assert note is not None and "not installed" in note


# ----------------------------------------------------------- error JSON line


def test_error_line_becomes_page_error(cfg, tmp_home, monkeypatch, tmp_path):
    stub = tmp_path / "err-helper"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "for raw in sys.stdin:\n"
        "    req=json.loads(raw)\n"
        "    print(json.dumps({'id':req['id'],'path':req['path'],"
        "'error':'cannot decode image'}))\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(stub))
    page = ocr.recognize_one(tmp_path / "garbage.png", cfg)
    assert page.error == "cannot decode image"
    assert page.text() == ""


# ---------------------------------------------------- xattr screenshot detect


def test_xattr_screenshot_detection(tmp_path):
    shot = tmp_path / "vacation-photo.png"      # non-screenshot NAME
    shot.write_bytes(b"x")
    assert rules.is_screenshot(shot) is False
    assert rules.set_screenshot_xattr(shot) is True
    # xattr wins over the innocent name (DigUp Rules.swift:140-144 order)
    assert rules.is_screenshot(shot) is True


def test_screenshot_prefix_fallback_still_works():
    assert rules.is_screenshot("Screenshot 2026-01-01 at 10.00.00.png")
    assert rules.is_screenshot("Screen Shot 2026-01-01.png")
    assert rules.is_screenshot("CleanShot 03.png")
    assert rules.is_screenshot("SCR-0412.png")
    assert not rules.is_screenshot("vacation.png")


def test_xattr_absent_file_prefix_only(tmp_path):
    # a bare filename that does not exist: prefix path only, never a crash
    assert rules.is_screenshot("Screenshot no-file.png") is True
    assert rules.is_screenshot("photo.png") is False


# ------------------------------------------------------------- live (opt-in)


@pytest.mark.live  # 02 F5: was env-gated only, polluting the offline skip count
def test_live_real_helper_handshake_and_ocr(cfg, tmp_path):
    """GOLDMINE_LIVE=1 only: the compiled Swift helper must exist at
    <home>/bin/goldmine-ocr (scripts/build_ocr_helper.sh) and read a real
    rendered-text image."""
    import os

    if os.environ.get("GOLDMINE_LIVE") != "1":
        pytest.skip("GOLDMINE_LIVE=1 only")
    helper = ocr.helper_path(cfg)
    if helper is None:
        helper = ocr.compile_helper(cfg)  # dev fallback, one live compile
    assert helper is not None, "live helper build failed"
    assert ocr.handshake(cfg) == ocr.HELPER_VERSION
    # render text via sips from the probe corpus screenshot if present
    target = Path("/tmp/gm_covfix/images/screenshots/"
                  "Screenshot 2026-10-11 at 09.15.42.png")
    if not target.is_file():
        pytest.skip("live corpus screenshot not present")
    page = ocr.recognize_one(target, cfg)
    assert page.error is None
    text = page.text()
    assert "EXTERNALLY MANAGED ENVIRONMENT" in text.upper()
