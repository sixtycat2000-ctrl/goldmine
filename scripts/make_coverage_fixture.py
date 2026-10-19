#!/usr/bin/env python3
"""Build the deterministic mixed-kind coverage fixture (v2 B8, 02 §9).

A small (<100 MB, <60 s), SEEDED, fully local tree exercising every kind the
v2 coverage KPI talks about: txt/md (incl. CJK), html, docx, pdf (text-layer,
hybrid text+scanned, fully-scanned raster pages), code in 5 languages,
screenshot-named PNGs WITH rendered text (the DigUp OCR regression class),
non-screenshot images, 35 s + 5 min WAV, a 40 s MP4, .eml + .epub, and noise
(pem, node_modules, .git project, minified.js, archives).

Optional external tools degrade honestly and are recorded in the manifest:
  ffmpeg -> video/mp4 + scanned-page rasters stay (rasters are pure numpy)
  say    -> the 5 min WAV carries spoken content (tone fallback otherwise)

Outputs <out>/manifest.json: every file the fixture defines, with kind,
optional `requires` tool, and whether it was created. The say-TTS wav (whose
raw bytes are environment-dependent, F1) additionally carries a
content-stable `speech_surrogate_sha256` (tool + transcript + duration
bucket). The media-eval self-check (tests/test_media_queries.py) and
coverage_report.py --fixture consume that manifest.

Usage: python3 scripts/make_coverage_fixture.py [--out DIR] [--seed N] [--force]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import wave
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import make_docx, make_pdf, pinned_zipinfo  # committed test builders

SEED = 20261016

#: F9 (fixture staleness): bump when the fixture definition changes. The
#: generated fixture_version.json (version + generator mtime + file count)
#: lets coverage_report.py --fixture detect and rebuild an outdated tree
#: instead of reporting numbers over files this generator no longer writes.
FIXTURE_VERSION = 1

_GENERATOR_PATH = Path(__file__).resolve()


def fixture_version_record(manifest: dict, generator: Path | None = None) -> dict:
    """The staleness fingerprint stored beside the fixture: the version
    constant, the GENERATOR's CONTENT hash (F1: machine-independent — the old
    mtime fingerprint made every fresh clone's rebuild look stale and tripped
    the retrieval gate's pre-flight), and the created-file count."""
    generator = Path(generator) if generator else _GENERATOR_PATH
    return {
        "version": FIXTURE_VERSION,
        "generator_sha256": hashlib.sha256(generator.read_bytes()).hexdigest(),
        "files": len([f for f in manifest.get("files", []) if f.get("created")]),
    }

# ---------------------------------------------------------------- bitmap font

#: 5x7 bitmap font (classic HD44780-style rows, 5 bits MSB-left). Used to
#: rasterize deterministic text into PNGs and scanned-PDF pages so OCR (v2
#: item B4) has REAL pixels to read later — the fixture's queries for
#: needs:ocr stay truthful instead of pointing at blank images.
_FONT: dict[str, tuple[int, ...]] = {
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
    "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x06, 0x08, 0x10, 0x1F),
    "3": (0x1F, 0x01, 0x02, 0x06, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    " ": (0, 0, 0, 0, 0, 0, 0),
    "-": (0, 0, 0, 0x1F, 0, 0, 0),
    ".": (0, 0, 0, 0, 0, 0x0C, 0x0C),
    ":": (0, 0x0C, 0x0C, 0, 0x0C, 0x0C, 0),
    "/": (0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10),
}

_FONT_CHARS = sorted(_FONT)


def render_text_rgb(lines: list[str], scale: int = 4, pad: int = 24) -> np.ndarray:
    """Deterministic black-on-white raster of `lines` via the bitmap font.

    v2 note (B5): the canvas is padded to a >= 320px min side — real macOS
    screenshots are far taller than the text they carry, and v2 refuses
    images below DigUp's minImageSide 256 from embedding. A bare text strip
    (764x176) would be refused BY POLICY and tank the coverage floors."""
    height = pad * 2 + len(lines) * 8 * scale
    width = pad * 2 + max(
        (len(line) * 6 - 1) * scale for line in lines
    ) if lines else pad * 2
    height = max(height, 320)
    width = max(width, 320)
    img = np.full((height, width, 3), 255, dtype=np.uint8)
    y0 = pad
    for line in lines:
        x0 = pad
        for ch in line.upper():
            glyph = _FONT.get(ch)
            if glyph is None:
                x0 += 6 * scale
                continue
            for row, bits in enumerate(glyph):
                for col in range(5):
                    if bits & (1 << (4 - col)):
                        img[
                            y0 + row * scale: y0 + (row + 1) * scale,
                            x0 + col * scale: x0 + (col + 1) * scale,
                        ] = 0
            x0 += 6 * scale
        y0 += 8 * scale
    return img


def encode_png(rgb: np.ndarray) -> bytes:
    """RGB uint8 array -> PNG bytes (stdlib zlib+struct, no PIL)."""
    h, w, _ = rgb.shape
    raw = b"".join(
        b"\x00" + rgb[y].tobytes() for y in range(h)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def write_png(path: Path, rgb: np.ndarray) -> None:
    path.write_bytes(encode_png(rgb))


# ----------------------------------------------------------------- pdf raster

def make_pdf_pages(path: Path, pages: list[tuple[str, object]]) -> None:
    """Raw PDF builder (conftest.make_pdf shape) whose pages are either
    ("text", str) — Tj text runs pdftotext recovers — or ("image", np.ndarray
    RGB) — a FlateDecode image XObject with NO text (a scanned page)."""
    import zlib as _zlib

    objects: list[bytes] = []

    def obj(number: int, body: bytes) -> bytes:
        return b"%d 0 obj\n%s\nendobj\n" % (number, body)

    def text_stream(text: str) -> bytes:
        parts = [b"BT /F1 12 Tf 72 720 Td 14 TL"]
        for line in text.splitlines():
            escaped = (
                line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            )
            parts.append(b"(%s) Tj T*" % escaped.encode("utf-8"))
        parts.append(b"ET")
        content = b"\n".join(parts)
        return b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)

    n = len(pages)
    page_ids = [3 + 3 * i for i in range(n)]
    font_id = 3 + 3 * n
    objects.append(obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects.append(obj(2, b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n)))
    for i, (kind, payload) in enumerate(pages):
        page_id = page_ids[i]
        if kind == "text":
            content = text_stream(payload)
            objects.append(obj(
                page_id,
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Contents %d 0 R /Resources << /Font << /F1 %d 0 R >> >> >>"
                % (page_id + 1, font_id),
            ))
            objects.append(obj(page_id + 1, content))
            objects.append(obj(page_id + 2, b"<< /Null true >>"))  # unused slot
        else:  # image page (scanned)
            rgb = np.ascontiguousarray(payload, dtype=np.uint8)
            h, w, _ = rgb.shape
            data = _zlib.compress(rgb.tobytes(), 6)
            draw = b"q %d 0 0 %d 0 0 cm /Im0 Do Q" % (w, h)
            objects.append(obj(
                page_id,
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
                b"/Contents %d 0 R /Resources << /XObject << /Im0 %d 0 R >> >> >>"
                % (w, h, page_id + 1, page_id + 2),
            ))
            objects.append(obj(
                page_id + 1,
                b"<< /Length %d >>\nstream\n%s\nendstream" % (len(draw), draw),
            ))
            objects.append(obj(
                page_id + 2,
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
                b"/Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream"
                % (w, h, len(data), data),
            ))
    objects.append(obj(
        font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    ))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (len(objects) + 1)
    for number, body in enumerate(objects, start=1):
        offsets[number] = len(out)
        out.extend(body)
    xref_pos = len(out)
    out.extend(b"xref\n0 %d\n" % (len(objects) + 1))
    out.extend(b"0000000000 65535 f \n")
    for number in range(1, len(objects) + 1):
        out.extend(b"%010d 00000 n \n" % offsets[number])
    out.extend(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref_pos)
    )
    path.write_bytes(bytes(out))


# ----------------------------------------------------------------- epub / eml

def make_epub(path: Path, title: str, chapters: list[tuple[str, str]]) -> None:
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    manifest = "".join(
        f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(chapters))
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-id="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{escape(title)}</dc:title>"
        '<dc:identifier id="uid">gm-covfix</dc:identifier>'
        '<dc:language>en</dc:language></metadata>'
        f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        # F1: pinned ZipInfo entries — no wall-clock timestamps in the bytes
        zf.writestr(pinned_zipinfo("mimetype"), "application/epub+zip")
        zf.writestr(pinned_zipinfo("META-INF/container.xml"), container)
        zf.writestr(pinned_zipinfo("OEBPS/content.opf"), opf)
        for i, (heading, body) in enumerate(chapters):
            zf.writestr(
                pinned_zipinfo(f"OEBPS/ch{i}.xhtml"),
                f"<html><body><h1>{escape(heading)}</h1>"
                f"<p>{escape(body)}</p></body></html>",
            )


def make_eml(path: Path, from_: str, subject: str, date: str, body: str,
             attachment: tuple[str, bytes] | None = None) -> None:
    import base64

    if attachment is None:
        raw = (
            f"From: {from_}\nSubject: {subject}\nDate: {date}\n"
            "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n"
            f"{body}\n"
        )
    else:
        name, blob = attachment
        b64 = base64.b64encode(blob).decode()
        raw = (
            f"From: {from_}\nSubject: {subject}\nDate: {date}\n"
            'MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="GMFIX"\n\n'
            "--GMFIX\nContent-Type: text/plain; charset=utf-8\n\n"
            f"{body}\n--GMFIX\n"
            f'Content-Type: application/pdf; name="{name}"\n'
            f'Content-Disposition: attachment; filename="{name}"\n'
            "Content-Transfer-Encoding: base64\n\n"
            f"{b64}\n--GMFIX--\n"
        )
    path.write_bytes(raw.replace("\n", "\r\n").encode("utf-8"))


# ---------------------------------------------------------------------- audio

def synth_wav(path: Path, seconds: float, freqs: tuple[float, ...] = (440.0,)) -> None:
    """Deterministic multi-tone 16 kHz mono PCM16 WAV (numpy synthesis)."""
    rate = 16000
    t = np.arange(int(rate * seconds)) / rate
    wave_data = np.zeros_like(t)
    for f in freqs:
        wave_data += np.sin(2 * np.pi * f * t)
    pcm = (8000 / max(1, len(freqs)) * wave_data).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


#: Fixed narration script for the 5 min WAV — the ASR-dependent eval queries
#: quote sentences from THIS text (labels are the script, so they are
#: deterministic even though the TTS voice is machine-dependent).
SPEECH_SCRIPT = """\
Lighthouse keeper logbook, October the fourteenth.
The tide gauge recorded 4.2 meters at dawn, and the swell stayed below one meter all day.
The beacon FL-33 flashed three times every fifteen seconds through the evening fog.
I trimmed the lantern wick twice; a clean wick keeps the flame steady at full brightness.
The fog signal sounded every 90 seconds, and I logged each sounding in the margin.
The relief boat brought flour, lamp oil, and two new brushes for the lens frame.
I rewound the tide clock at noon and checked it against the harbor office wireless.
A sealskin floated past the north jetty; the gulls were loud but harmless.
The barometer fell slowly after supper; I doubled the mooring lines on the dinghy.
I noted the keeper before me preferred kerosene; the new electric filament hums faintly.
The logbook candle burned two inches; I will ask the harbor office for a spare box.
Radio check with the mainland at eight bells; signal strength was fair to good.
"""


def speech_wav(path: Path) -> bool:
    """5 min WAV via macOS `say` (fixed script, repeated to fill the time);
    deterministic tone fallback when `say` is absent."""
    say = shutil.which("say")
    if say is None:
        synth_wav(path, 5 * 60, freqs=(220.0, 330.0))
        return False
    script = " ".join([SPEECH_SCRIPT] * 4)  # ~4-6 min at typical TTS pace
    try:
        result = subprocess.run(
            [say, "-o", str(path), "--data-format=LEI16@16000"],
            input=script.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        if result.returncode == 0 and path.is_file() and path.stat().st_size > 1000:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    synth_wav(path, 5 * 60, freqs=(220.0, 330.0))
    return False


#: F1 (say-wav surrogate): `say` output bytes vary between builds (voice
#: pacing moves the RIFF size), so the TTS wav cannot be pinned by raw sha.
#: The manifest pins it as requires:say PLUS this CONTENT-STABLE surrogate:
#: sha256 over tool + transcript hash + a coarse duration bucket. The bucket
#: edges (90 s / 4 min / 7 min) sit far from any sane pacing of this fixed
#: ~5 min script, so the surrogate cannot flip between two builds — while a
#: truncated file, a voiceless fallback, or a wrong transcript still moves it.
SPEECH_BUCKETS: tuple[tuple[float, str], ...] = (
    (90.0, "under-90s"), (240.0, "90s-to-4min"), (420.0, "4-to-7min"),
)


def speech_surrogate(tool: str, duration_s: float) -> str:
    """Content-stable surrogate hash for the environment-dependent TTS wav."""
    bucket = next(
        (label for limit, label in SPEECH_BUCKETS if duration_s < limit),
        "over-7min",
    )
    payload = json.dumps(
        {
            "tool": tool,
            "transcript_sha256": hashlib.sha256(
                SPEECH_SCRIPT.encode("utf-8")
            ).hexdigest(),
            "duration_bucket": bucket,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def wav_duration_s(path: Path) -> float:
    """Seconds of audio in a RIFF wav (frames/rate); a size estimate fallback
    for a header the wave module refuses (LEI16@16000 mono = 32000 B/s)."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 16000)
    except Exception:
        return path.stat().st_size / 32000.0


def ffmpeg_mp4(path: Path, seconds: int) -> bool:
    """40 s MP4: ffmpeg testsrc2 color bars + 440 Hz tone (offline lavfi)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=12",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
             "-t", str(seconds), "-pix_fmt", "yuv420p",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-c:a", "aac", "-b:a", "32k", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        return result.returncode == 0 and path.is_file()
    except (OSError, subprocess.TimeoutExpired):
        return False


# ------------------------------------------------------------------- fixture

def build(out: Path, seed: int = SEED, force: bool = False) -> dict:
    rng = np.random.default_rng(seed)
    if out.exists() and not force:
        print(f"fixture exists at {out} (use --force to rebuild)")
        return json.loads((out / "manifest.json").read_text())
    if out.exists():
        shutil.rmtree(out)

    files: list[dict] = []

    def record(rel: str, kind: str, *, requires: str | None = None,
               created: bool = True) -> None:
        files.append(
            {"path": rel, "kind": kind, **({"requires": requires} if requires else {}),
             "created": created}
        )

    # -- text family ----------------------------------------------------
    (out / "txt").mkdir(parents=True)
    (out / "txt" / "field-notes-lighthouse.txt").write_text(
        "Lighthouse field notes.\n"
        "The Fresnel lens rotates twice per minute on the clockwork drive.\n"
        "Keeper supply token LH-2209 covers lamp oil and wick trimmers.\n"
        "The tide gauge float was recalibrated after the autumn storm.\n",
        encoding="utf-8",
    )
    record("txt/field-notes-lighthouse.txt", "doc")
    (out / "txt" / "brewlog-sourdough.txt").write_text(
        "Sourdough brewlog.\n"
        "Starter feeding ratio one two two, hydration 78 percent.\n"
        "Bulk ferment 4 hours at 24 degrees, proof 90 minutes.\n"
        "The crust blistered nicely; crumb was open and glossy.\n",
        encoding="utf-8",
    )
    record("txt/brewlog-sourdough.txt", "doc")

    (out / "md" / "obsidian").mkdir(parents=True)
    (out / "md" / "obsidian" / "reef-tank.md").write_text(
        "# Reef Tank Log\n\n"
        "Calcium dosing 12 ml per day; alkalinity held at 8.3 dKH.\n"
        "The hammer coral opened fully after the light schedule change.\n"
        "Maintenance: clean the protein skimmer cup weekly.\n",
        encoding="utf-8",
    )
    record("md/obsidian/reef-tank.md", "doc")
    (out / "md" / "obsidian" / "zh-notes.md").write_text(
        "# 维护记录\n\n"
        "水族馆的过滤器需要每两周清洗一次。\n"
        "灯塔的透镜每分钟旋转两次,潮汐计读数为 4.2 米。\n"
        "酸面团的喂养比例是一比二比二,水合度为百分之七十八。\n",
        encoding="utf-8",
    )
    record("md/obsidian/zh-notes.md", "doc")

    (out / "html").mkdir(parents=True)
    (out / "html" / "meteor-shower.html").write_text(
        """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Meteor Shower Log</title></head><body>
<h1>Perseid observing notes</h1>
<p>Counted 61 meteors between 2 and 4 am; the brightest left a train lasting
eight seconds near Cassiopeia.</p>
<p>The finder chart was taped to the pier leg; red flashlight batteries held.</p>
</body></html>""",
        encoding="utf-8",
    )
    record("html/meteor-shower.html", "doc")

    (out / "docx").mkdir(parents=True)
    make_docx(
        out / "docx" / "quarterly-report.docx",
        ["Quarterly operations report.",
         "Widget throughput reached 412 units this quarter.",
         "The leviathan pump prototype passed its bench trial."],
        title="Quarterly Report Q3",
    )
    record("docx/quarterly-report.docx", "doc")

    # -- pdf: text layer, hybrid, fully scanned ---------------------------
    # (text pages exceed extract_pdf.THIN_PAGE_CHARS=200 so they get vectors)
    (out / "pdf").mkdir(parents=True)
    make_pdf(
        out / "pdf" / "atlas-text-layer.pdf",
        [
            "Harbor dredging report page one. The channel markers were reset after the "
            "spring survey and the dredge volume totaled 19000 cubic meters of clean sand, "
            "enough to replenish both the north beach and the dune toe.",
            "Breakwater inspection page two. Engineers found no new cracks after the "
            "autumn storm; the concrete armor units settled by two centimeters, well "
            "within tolerance, and the ferry wake limits were tightened in May.",
            "Dredge disposal page three. The spoil ground east of the bar accepted "
            "eleven barge loads; monitoring buoys will report turbidity weekly until "
            "the seabed recolonizes, expected within two seasons.",
        ],
        title="Harbor Atlas",
    )
    record("pdf/atlas-text-layer.pdf", "pdf")
    make_pdf_pages(
        out / "pdf" / "atlas-hybrid.pdf",
        [
            ("text",
             "Hybrid chart page one. The breakwater repair estimate is 48000 EUR and "
             "covers twelve armor units, marine grout, and a four-week window with the "
             "tide constraints the divers logged in the appendix tables."),
            ("text",
             "Hybrid chart page two. Ferry wake limits were tightened in May after the "
             "harbormaster measured wave heights at the fuel dock; operators must now "
             "hold eight knots inside the fairway and twelve outside the bar."),
            ("image", render_text_rgb(
                ["HARBOR CHART PAGE 3", "DEPTH 42 METERS AT THE BAR", "SURVEY MARKER SM-11"])),
            ("image", render_text_rgb(
                ["HARBOR CHART PAGE 4", "SANDBAR MARKER KB-77", "SHOAL WARNING IN FORCE"])),
        ],
    )
    record("pdf/atlas-hybrid.pdf", "pdf")
    make_pdf_pages(
        out / "pdf" / "atlas-scanned.pdf",
        [
            ("image", render_text_rgb(
                ["RECEIPT 9001", "TOTAL 4820 EUR", "LEVIATHAN PUMP ORDER"])),
            ("image", render_text_rgb(
                ["RECEIPT 9002", "VAT 12 PERCENT", "PAYMENT DUE IN 30 DAYS"])),
            ("image", render_text_rgb(
                ["RECEIPT 9003", "SIGNED BY THE HARBOR OFFICE", "STAMP RECEIVED OCT 16"])),
        ],
    )
    record("pdf/atlas-scanned.pdf", "pdf")

    # -- code: 5 languages inside a .git project ---------------------------
    proj = out / "code" / "proj"
    (proj / ".git").mkdir(parents=True)
    (proj / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (proj / "main.py").write_text(
        "def fetch_tide_gauge(station):\n"
        "    \"\"\"Fetch the tide gauge reading for a station.\"\"\"\n"
        "    return {station: 4.2}\n",
        encoding="utf-8",
    )
    (proj / "tool.go").write_text(
        "package main\n\n// parseBeacon decodes a beacon flash pattern.\n"
        "func parseBeacon(code string) int { return len(code) }\n",
        encoding="utf-8",
    )
    (proj / "lib.rs").write_text(
        "pub fn depth_sensor(meters: f64) -> bool {\n"
        "    // true when the sensor is below the alarm depth\n"
        "    meters > 40.0\n}\n",
        encoding="utf-8",
    )
    (proj / "app.js").write_text(
        "function chartPlotter(canvas) {\n"
        "  // draws the harbor chart overlay\n"
        "  return { plot: () => canvas };\n}\n",
        encoding="utf-8",
    )
    (proj / "util.c").write_text(
        "#include <stdio.h>\n\n"
        "int sonar_ping(int channel) {\n    /* returns echo delay in ms */\n"
        "    return channel * 2;\n}\n",
        encoding="utf-8",
    )
    for rel in ("main.py", "tool.go", "lib.rs", "app.js", "util.c"):
        record(f"code/proj/{rel}", "code")
    record("code/proj/.git/HEAD", "noise")

    # -- images: screenshots with rendered text + plain photos -------------
    shots = out / "images" / "screenshots"
    shots.mkdir(parents=True)
    write_png(
        shots / "Screenshot 2026-10-11 at 09.15.42.png",
        render_text_rgb([
            "GOLDMINE PANEL 01",
            "ERROR CODE 4471",
            "EXTERNALLY MANAGED ENVIRONMENT",
            "SEE THE PACKAGE DOCKERFILE",
        ]),
    )
    record("images/screenshots/Screenshot 2026-10-11 at 09.15.42.png", "screenshot")
    write_png(
        shots / "Screenshot 2026-10-12 at 14.02.11.png",
        render_text_rgb([
            "GOLDMINE PANEL 02",
            "UPLOAD FAILED",
            "RETRY TOKEN RT-9042",
            "CHECK THE HARBOR PROXY",
        ]),
    )
    record("images/screenshots/Screenshot 2026-10-12 at 14.02.11.png", "screenshot")
    photos = out / "images" / "photos"
    photos.mkdir(parents=True)
    # v2 note (B5): photos must clear the DigUp minImageSide 256 floor — a
    # 240px photo is refused BY DESIGN now, which would tank the image:photo
    # coverage floor for a policy reason, not a pipeline bug.
    photo_rgb = rng.integers(0, 256, size=(256, 320, 3), dtype=np.uint8)
    write_png(photos / "photo-harbor-bridge.png", photo_rgb)
    record("images/photos/photo-harbor-bridge.png", "image")
    write_png(
        photos / "photo-lighthouse.png",
        rng.integers(0, 256, size=(256, 320, 3), dtype=np.uint8),
    )
    record("images/photos/photo-lighthouse.png", "image")

    # -- audio / video ------------------------------------------------------
    audio = out / "audio"
    audio.mkdir(parents=True)
    synth_wav(audio / "tone-diagnostic-35s.wav", 35.0, freqs=(440.0,))
    record("audio/tone-diagnostic-35s.wav", "audio")
    speech_ok = speech_wav(audio / "logbook-speech-5min.wav")
    record("audio/logbook-speech-5min.wav", "audio",
           requires="say" if speech_ok else None)
    if speech_ok:
        # F1: say bytes are environment-dependent — pin the content-stable
        # surrogate (tool + transcript + coarse duration bucket) instead.
        files[-1]["speech_surrogate_sha256"] = speech_surrogate(
            "say", wav_duration_s(audio / "logbook-speech-5min.wav")
        )
    video = out / "video"
    video.mkdir(parents=True)
    mp4_ok = ffmpeg_mp4(video / "bars-tone-40s.mp4", 40)
    record("video/bars-tone-40s.mp4", "video", requires="ffmpeg" if mp4_ok else None,
           created=mp4_ok)

    # -- mail / books -------------------------------------------------------
    mail = out / "mail"
    mail.mkdir(parents=True)
    make_eml(
        mail / "filter-maintenance.eml",
        from_="Dana Wong <dana@example.org>",
        subject="Aquarium filter maintenance schedule",
        date="Mon, 12 Oct 2026 09:30:00 +0800",
        body=(
            "The canister filter needs a rinse every two weeks.\n"
            "Order replacement sponges part number FX-8841 before month end."
        ),
    )
    record("mail/filter-maintenance.eml", "doc")
    make_eml(
        mail / "invoice-q3.eml",
        from_="alice@example.net",
        subject="Invoice attachment quarter three",
        date="Thu, 15 Oct 2026 11:00:00 -0700",
        body="Invoice quarter three attached; total 4820 EUR for the leviathan pump.",
        attachment=("invoice-q3.pdf", b"%PDF-1.4 fixture attachment bytes"),
    )
    record("mail/invoice-q3.eml", "doc")
    make_eml(
        mail / "build-notification.eml",
        from_="bot@example.com",
        subject="Build notification 5571",
        date="Wed, 14 Oct 2026 02:11:00 +0000",
        body="Build 5571 green. All tests passed on the staging cluster.",
    )
    record("mail/build-notification.eml", "doc")

    books = out / "books"
    books.mkdir(parents=True)
    make_epub(
        books / "coastal-almanac.epub",
        title="Coastal Almanac",
        chapters=[
            ("Lighthouse manual", "The Fresnel lens rotates twice per minute."),
            ("Harbor log", "The tide gauge recorded 4.2 meters at dawn."),
            ("Weather tables", "Fog forms on 14 nights per October on average."),
        ],
    )
    record("books/coastal-almanac.epub", "doc")

    # -- noise: secrets, minified, archives, node_modules --------------------
    noise = out / "noise"
    noise.mkdir(parents=True)
    (noise / "server.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\nfixturenope\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    record("noise/server.pem", "noise")
    (noise / "app.min.js").write_text(
        "var a=1;var b=2;function f(){return a+b}" * 20, encoding="utf-8"
    )
    record("noise/app.min.js", "noise")
    with zipfile.ZipFile(noise / "data.zip", "w") as zf:
        # F1: pinned entry metadata — the zip must be byte-identical per build
        zf.writestr(pinned_zipinfo("inner.txt"), "zipped fixture content")
    record("noise/data.zip", "archive")
    # F1: a plain "w:gz" tarfile stamps time.time() into the gzip header; wrap
    # the gzip stream ourselves with mtime=0 (and no stored filename), and pin
    # the TarInfo mtime explicitly — the archive is byte-identical per build.
    blob = b"fixture tar content\n"
    info = tarfile.TarInfo("inner.log")
    info.size = len(blob)
    info.mtime = 0
    with open(noise / "backup.tar.gz", "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tf:
                import io

                tf.addfile(info, io.BytesIO(blob))
    record("noise/backup.tar.gz", "archive")
    nm = noise / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    record("noise/node_modules/left-pad/index.js", "noise")

    manifest = {
        "seed": seed,
        "root": str(out),
        # F1: no elapsed_s here — a wall-clock field made manifest.json itself
        # non-deterministic; build time belongs to the CLI's stdout only.
        "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    # F9: the staleness fingerprint — coverage_report --fixture checks it
    # and rebuilds the tree when it no longer matches this generator
    (out / "fixture_version.json").write_text(
        json.dumps(fixture_version_record(manifest), indent=1)
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="/tmp/gm_covfix", help="fixture root")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--force", action="store_true", help="rebuild if present")
    args = parser.parse_args(argv)

    t0 = time.perf_counter()
    manifest = build(Path(args.out).expanduser(), seed=args.seed, force=args.force)
    total = sum(
        (Path(manifest["root"]) / f["path"]).stat().st_size
        for f in manifest["files"] if f["created"]
    )
    print(
        f"fixture: {manifest['root']} — {len(manifest['files'])} files, "
        f"{total / 1e6:.1f} MB, built in {time.perf_counter() - t0:.1f}s "
        f"(seed {manifest['seed']})"
    )
    missing = [
        f["path"] for f in manifest["files"]
        if f["created"] and not (Path(manifest["root"]) / f["path"]).is_file()
    ]
    if missing:
        print(f"ERROR: missing created files: {missing}")
        return 1
    if total > 100 << 20:
        print(f"ERROR: fixture exceeds the 100 MB budget ({total} bytes)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
