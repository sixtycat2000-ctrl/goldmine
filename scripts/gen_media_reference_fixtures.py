#!/usr/bin/env python3
"""Generate the media reference-vector fixtures (digup-plan-002 T6).

Deterministic inputs for tests/test_reference_media_vectors.py — 2 images +
2 audio windows. The images exercise both budget branches (photo 140 down-
scale; screenshot 280 with a different rounding path); the audio covers a
3 s chirp (speech-less, RMS ≫ gate) and a short 2 s sine.

The photo JPEG is produced by the SAME sips command the ingest path uses
(media.thumbnail_image: jpeg q90, -Z bounded, never upscaled) — the checked-in
BYTES are authoritative; regeneration is a reviewable act, not a rebuild.

Usage:
  python3 scripts/gen_media_reference_fixtures.py   # writes tests/fixtures_data/media/
"""
from __future__ import annotations

import hashlib
import math
import struct
import subprocess
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "tests" / "fixtures_data" / "media"


def _png(w: int, h: int, pixel) -> bytes:
    rows = b""
    for y in range(h):
        rows += b"\x00" + bytes(pixel(x, y, c) for x in range(w) for c in range(3))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))


def photo_source_png() -> bytes:
    """1024x768 RGB gradient (same recipe as the helper's --selftest-resize)."""
    return _png(1024, 768, lambda x, y, c: (x * 7 + y * 13 + c * 29) & 255)


def screenshot_source_png() -> bytes:
    """1280x800 high-contrast 32px bars (UI-like)."""
    def pixel(x, y, c):
        band = ((x // 32) + (y // 32)) % 2
        rgb = (235, 240, 45) if band else (c * 40 + 20, 25, 30)
        return rgb[c] & 255
    return _png(1280, 800, pixel)


def wav_bytes(seconds: float, sample_fn) -> bytes:
    sr = 16000
    n = int(sr * seconds)
    samples = [max(-32767, min(32767, int(sample_fn(i / sr) * 32767)))
               for i in range(n)]
    return (b"RIFF" + struct.pack("<I", 36 + 2 * n) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            + b"data" + struct.pack("<I", 2 * n)
            + struct.pack(f"<{n}h", *samples))


def chirp_3s() -> bytes:
    return wav_bytes(3.0, lambda t: 0.1 * math.sin(
        2 * math.pi * (200 + (2000 - 200) * t / 3.0) * t))


def sine_2s() -> bytes:
    return wav_bytes(2.0, lambda t: 0.05 * math.sin(2 * math.pi * 440 * t))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / ".src"
    tmp.mkdir(exist_ok=True)
    # photo: 1024x768 source at/below the 1024 budget -> sips format-converts
    src = tmp / "photo-src.png"
    src.write_bytes(photo_source_png())
    dst = OUT / "ref-photo-b140.jpg"
    subprocess.run(["/usr/bin/sips", "-s", "format", "jpeg",
                    "-s", "formatOptions", "90%", "-Z", "1024",
                    str(src), "--out", str(dst)], check=True)
    # screenshot: PNG stays PNG (UI text crisp), at/below 1536
    (OUT / "ref-shot-b280.png").write_bytes(screenshot_source_png())
    (OUT / "ref-audio-3s.wav").write_bytes(chirp_3s())
    (OUT / "ref-audio-2s.wav").write_bytes(sine_2s())
    for path in sorted(p for p in OUT.iterdir() if p.is_file()):
        print(f"{path.name:24s} {path.stat().st_size:>9d}  "
              f"sha256 {hashlib.sha256(path.read_bytes()).hexdigest()}")
    for stale in (tmp / "photo-src.png",):
        stale.unlink(missing_ok=True)
    tmp.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
