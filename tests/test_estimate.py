"""A3 (digup-p2-closeout-001): `goldmine estimate` — read-only first-ingest
time estimate from DigUp priors (Estimator.swift:5-16). Metadata only; the
command has no --db surface and must not create an index anywhere.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from conftest import tiny_png, tiny_wav_bytes

from goldmine.commands import estimate as est


@pytest.fixture
def tree(tmp_path, make_pdf_fixture) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.txt").write_text("hello " * 800, encoding="utf-8")   # 4.8 KB -> 2 chunks
    (root / "b.md").write_text("world " * 100, encoding="utf-8")    # 600 B -> 1 chunk
    make_pdf_fixture(root / "doc.pdf", ["page one", "page two", "page three"])
    (root / "pic.png").write_bytes(tiny_png())
    (root / "clip.wav").write_bytes(tiny_wav_bytes(2.0))
    (root / "main.py").write_text("x = 1\n" * 200, encoding="utf-8")
    return root


@pytest.mark.native
def test_estimate_json_shape_and_counts(tree):
    result = est.estimate_root(tree, code=True)
    assert result["files"] == {"doc": 2, "pdf": 1, "image": 1, "audio": 1, "code": 1}
    assert result["doc_chunks"] == 2 + 1  # 4800B//2000 + max(1, 600//2000)
    from goldmine.ingest.extract_pdf import pdfinfo_path

    if pdfinfo_path() is not None:
        assert result["pdf_pages_total"] == 3
        assert result["pdf_pages_first_pass"] == 3  # <= 30 pages: all first pass
        assert "pdf pages" not in result["degraded"]
    else:
        assert result["pdf_pages_total"] == est.ASSUMED_PDF_PAGES
        assert "pdf pages" in result["degraded"]
    assert result["seconds"] > 0 and result["total_seconds"] >= result["seconds"]
    assert any("calibration pending" in n for n in result["notes"])
    assert any("whisper" in n for n in result["notes"])  # the honest audio caveat


@pytest.mark.native
def test_estimate_long_pdf_first_pass_split(tmp_path, make_pdf_fixture):
    root = tmp_path / "p"
    root.mkdir()
    make_pdf_fixture(root / "big.pdf", [f"page {i}" for i in range(1, 41)])
    from goldmine.ingest.extract_pdf import pdfinfo_path

    result = est.estimate_root(root, code=False)
    if pdfinfo_path() is not None:
        assert result["pdf_pages_total"] == 40
        assert result["pdf_pages_first_pass"] == est.PDF_FIRST_PASS_PAGES  # 30
        assert result["long_files"] == 1
        assert result["later_seconds"] == pytest.approx(10 * est.SECONDS_PER_PDF_PAGE)


def test_estimate_never_writes_an_index(tree, tmp_path, capsys):
    args = argparse.Namespace(root=str(tree), json=False, code=False)
    rc = est.cmd_estimate(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "estimate for" in out and "min" in out
    assert "calibration pending" in out
    # read-only proof: nothing index-shaped appears anywhere under the tree's tmp
    assert list(tmp_path.rglob("*.sqlite*")) == []
    assert list(tree.rglob("*.sqlite*")) == []


def test_estimate_cli_json_subprocess(tree):
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "goldmine", "estimate", str(tree), "--json"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["files"]["doc"] == 2 and data["seconds"] > 0


def test_estimate_missing_root_is_a_usage_error(tmp_path):
    args = argparse.Namespace(root=str(tmp_path / "nope"), json=False, code=False)
    from goldmine.errors import UserFacingError

    with pytest.raises(UserFacingError) as excinfo:
        est.cmd_estimate(args)
    assert excinfo.value.exit_code == 2


def test_estimate_degraded_notes_when_tools_missing(tree, monkeypatch):
    """pdfinfo/ffprobe missing: loud degradation, stated priors (DigUp Estimator
    falls back to the file header; goldmine falls back to stated assumptions)."""
    monkeypatch.setattr(est, "pdf_info", lambda p: {})
    monkeypatch.setattr(est, "pdfinfo_path", lambda: None)
    monkeypatch.setattr(est, "ffprobe_path", lambda: None)
    monkeypatch.setattr(est, "ffprobe_duration", lambda p: None)
    result = est.estimate_root(tree, code=True)
    assert "pdf pages" in result["degraded"] and "av durations" in result["degraded"]
    notes = " | ".join(result["notes"])
    assert f"assumed {est.ASSUMED_PDF_PAGES} pages/pdf" in notes
    assert "60s audio / 30s video" in notes
    assert result["pdf_pages_total"] == est.ASSUMED_PDF_PAGES


def test_estimate_screenshot_guess_costs_more(tmp_path):
    root = tmp_path / "shots"
    root.mkdir()
    (root / "Screenshot 2026-10-25 at 10.00.00.png").write_bytes(tiny_png())
    (root / "photo.png").write_bytes(tiny_png())
    result = est.estimate_root(root, code=False)
    assert result["screenshot_guess"] == 1
    # seconds is rounded to one decimal for the report (0.44 -> 0.4)
    assert result["seconds"] == pytest.approx(
        est.SECONDS_PER_SCREENSHOT + est.SECONDS_PER_IMAGE, abs=0.06
    )
