"""Tier-2: v2 image preprocessing (media.prepare_image / sips) — item B5,
docs/v2/02 §5.4. Uses the REAL /usr/bin/sips on generated images (macOS
built-in; the suite is macOS-only anyway) and skips elsewhere."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import tiny_png

from goldmine.ingest import media
from goldmine.util import ToolError

pytestmark = pytest.mark.skipif(
    media.sips_path() is None, reason="sips not available (macOS built-in)"
)


def test_thumbnail_photo_dimensions_and_format(tmp_path):
    """600x400 PNG -> JPEG thumbnail, dims preserved (sips -Z never upscales),
    no refusal; the caller embeds THIS file's bytes."""
    src = tmp_path / "photo.png"
    src.write_bytes(tiny_png(600, 400))
    prep = media.prepare_image(src, tmp_path / "out", screenshot=False)
    assert prep.refused is None and prep.path is not None
    assert (prep.width, prep.height) == (600, 400)
    assert prep.path.suffix == ".jpg" and prep.path.is_file()
    assert prep.taken is None           # no EXIF on a generated PNG


def test_thumbnail_screenshot_png(tmp_path):
    """Screenshots keep PNG at the 1536 budget (DigUp Indexer.swift:371-373)."""
    src = tmp_path / "Screenshot 2026-01-01 at 10.00.00.png"
    src.write_bytes(tiny_png(800, 600))
    prep = media.prepare_image(src, tmp_path / "out", screenshot=True)
    assert prep.refused is None and prep.path is not None
    assert prep.path.suffix == ".png"


def test_thumbnail_downscales_large_source(tmp_path):
    """A 3000x2000 photo thumbnails to at most 1024 on the long side."""
    src = tmp_path / "big.png"
    src.write_bytes(tiny_png(3000, 2000))
    prep = media.prepare_image(src, tmp_path / "out", screenshot=False)
    assert prep.refused is None
    assert max(prep.width, prep.height) <= media.PHOTO_THUMB_MAX_SIDE


def test_min_side_refusal(tmp_path):
    """min side < 256 (DigUp minImageSide, Extractors.swift:43): machine-stable
    refusal, thumbnail deleted."""
    src = tmp_path / "banner.png"
    src.write_bytes(tiny_png(300, 100))
    prep = media.prepare_image(src, tmp_path / "out", screenshot=False)
    assert prep.path is None
    assert "small image" in prep.refused and "300x100" in prep.refused


def test_undecodable_image_raises_runtime_error(tmp_path):
    """sips rc != 0 on garbage: RuntimeError carrying the tool's message —
    the pipeline maps it to an honest metadata_only row."""
    src = tmp_path / "garbage.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)
    with pytest.raises(RuntimeError, match="sips"):
        media.prepare_image(src, tmp_path / "out")


def test_prepare_image_without_sips_raises_toolerror(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "sips_path", lambda: None)
    src = tmp_path / "photo.png"
    src.write_bytes(tiny_png(600, 400))
    with pytest.raises(ToolError):
        media.prepare_image(src, tmp_path / "out")


def test_parse_sips_creation():
    """sips -g creation format: Apple's colons, '<nil>' when absent
    [measured]."""
    iso, epoch = media.parse_sips_creation("2024:05:17 09:30:00")
    assert iso == "2024-05-17 09:30:00"
    assert epoch == pytest.approx(time.mktime(time.strptime(
        "2024-05-17 09:30:00", "%Y-%m-%d %H:%M:%S")))
    assert media.parse_sips_creation("<nil>") is None
    assert media.parse_sips_creation(None) is None
    assert media.parse_sips_creation("not a date") is None


def test_image_dimensions(tmp_path):
    src = tmp_path / "photo.png"
    src.write_bytes(tiny_png(640, 480))
    assert media.image_dimensions(src) == (640, 480)
    assert media.image_dimensions(tmp_path / "missing.png") is None


def test_exif_taken_from_real_camera_file():
    """Portable generated HEIC with real EXIF; no OS wallpaper dependency."""
    target = Path(__file__).parent / "fixtures_data/exif-date.heic"
    taken = media.exif_taken(target)
    assert taken is not None
    iso, epoch = taken
    assert iso == "2024-05-17 09:30:00"
    assert 946684800 < epoch < 4102444800


def test_thumb_byte_cap_constant():
    """The post-thumbnail cap is the ~8 MB refusal (v1's 20 MB raw cap is
    gone) — pinned so a silent policy change fails here."""
    assert media.IMAGE_THUMB_MAX_BYTES == 8 << 20
    assert media.IMAGE_THUMB_MIN_SIDE == 256
