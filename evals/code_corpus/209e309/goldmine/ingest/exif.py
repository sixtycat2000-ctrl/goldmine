"""Capture-time metadata: bounded JPEG Exif TIFF reader, optional macOS ImageIO.

IFD0 DateTime is an edit timestamp. Only ExifIFD DateTimeOriginal establishes
capture time. The returned string preserves that camera wall clock. Explicit
OffsetTimeOriginal determines the epoch; without it, the existing host-local
date-filter convention is retained, without inferring a photographer timezone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import shutil
import struct

from ..util import ToolError, run_tool

METADATA_VERSION = "exif-original.v1"
MAX_HEADER_BYTES = 4 * 1024 * 1024
MAX_JPEG_SEGMENTS = 256
MAX_IFD_ENTRIES = 512
IMAGEIO_TIMEOUT_S = 5.0
IMAGEIO_EXECUTABLE = "/usr/bin/osascript"

# Fixed local script, with the input path passed as an argv item and bridged
# through a file URL. No path interpolation, Apple Events, or external script.
_IMAGEIO_SCRIPT = '''ObjC.import("Foundation"); ObjC.import("ImageIO");
function run(args) {
    if (args.length !== 1) throw new Error("one input path required");
    var source = $.CGImageSourceCreateWithURL($.NSURL.fileURLWithPath(args[0]), null);
    if (!source) return JSON.stringify({unsupported: true});
    var properties = $.CGImageSourceCopyPropertiesAtIndex(source, 0, null);
    if (!properties) throw new Error("ImageIO properties unavailable");
    var exif = ObjC.deepUnwrap(ObjC.castRefToObject(properties))["{Exif}"] || {};
    return JSON.stringify({DateTimeOriginal: exif.DateTimeOriginal || null,
                           OffsetTimeOriginal: exif.OffsetTimeOriginal || null});
}'''


@dataclass(frozen=True)
class CaptureResult:
    """A missing date is separate from a transient failure that needs retry."""

    taken: tuple[str, float] | None
    reader: str | None
    status: str  # complete | absent | unsupported | unavailable | error


@dataclass(frozen=True)
class CameraPosition:
    """Bounded IFD0 Make + GPS IFD read; camera fields may be partially absent."""

    make: str | None
    gps: str | None
    reader: str | None
    status: str  # complete | absent | error

MAX_EXIF_ASCII = 64  # camera make bound, bytes including the terminator


def parse_capture_time(value, offset=None):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}", value):
        return None
    try:
        stamp = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
        if offset is not None:
            if not isinstance(offset, str) or not re.fullmatch(r"[+-](?:[01]\d|2[0-3]):[0-5]\d", offset):
                return None
            minutes = (int(offset[1:3]) * 60 + int(offset[4:6])) * (-1 if offset[0] == "-" else 1)
            epoch = stamp.replace(tzinfo=timezone(timedelta(minutes=minutes))).timestamp()
        else:
            epoch = stamp.timestamp()  # Compatibility: host local time, not a UTC claim.
        return (stamp.strftime("%Y-%m-%d %H:%M:%S"), epoch) if math.isfinite(epoch) else None
    except (ValueError, OverflowError, OSError):
        return None


def _tiff_camera_gps(payload):
    """Read IFD0 Make and the GPS IFD only, under the same bounded rules.

    Never follows thumbnail chains or any IFD beyond the two pointers this
    product's contracts name (ExifIFD for capture time, GPSIFD for position).
    Returns None on any bound/shape violation instead of guessing."""
    if len(payload) < 8 or payload[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if payload[:2] == b"II" else ">"

    def number(offset, fmt):
        width = struct.calcsize(fmt)
        if offset < 0 or offset + width > len(payload):
            raise ValueError("Exif offset outside APP1 payload")
        return struct.unpack_from(endian + fmt, payload, offset)[0]

    def directory(offset):
        count = number(offset, "H")
        if count > MAX_IFD_ENTRIES or offset < 8 or offset + 2 + 12 * count + 4 > len(payload):
            raise ValueError("Exif directory count/trailer exceeds bounded payload")
        entries = {}
        for index in range(count):
            start = offset + 2 + index * 12
            tag, kind, size = number(start, "H"), number(start + 2, "H"), number(start + 4, "I")
            if tag in entries:
                raise ValueError("ambiguous duplicate Exif tag")
            entries[tag] = (kind, size, start + 8)
        return entries

    def ascii_value(entry, expected_size):
        kind, size, offset = entry
        if kind != 2 or size != expected_size:
            raise ValueError("Exif ASCII must have its bounded type/size")
        start = offset if size <= 4 else number(offset, "I")
        if start < 8 or start + size > len(payload):
            raise ValueError("Exif ASCII points outside APP1")
        raw = payload[start:start + size]
        if raw[-1:] != b"\0" or b"\0" in raw[:-1]:
            raise ValueError("Exif ASCII is not terminated exactly once")
        return raw[:-1].decode("ascii")

    try:
        if number(2, "H") != 42:
            return None
        first = directory(number(4, "I"))
        make = None
        if 0x010F in first:
            kind, size, _ = first[0x010F]
            if kind != 2 or not 1 <= size <= MAX_EXIF_ASCII:
                raise ValueError("camera Make exceeds its bounded ASCII size")
            value = ascii_value(first[0x010F], size)
            if value.isprintable():
                make = value
        gps = None
        pointer = first.get(0x8825)
        if pointer is not None and pointer[:2] == (4, 1):
            entries = directory(number(pointer[2], "I"))
            required = (0x0001, 0x0002, 0x0003, 0x0004)
            if all(tag in entries for tag in required):
                lat_ref = ascii_value(entries[0x0001], 2)
                lon_ref = ascii_value(entries[0x0003], 2)
                if lat_ref not in ("N", "S") or lon_ref not in ("E", "W"):
                    raise ValueError("GPS hemisphere refs must be single ASCII letters")
                def dms(tag):
                    kind, size, offset = entries[tag]
                    if kind != 5 or size != 3:
                        raise ValueError("GPS coordinate must be three RATIONALs")
                    start = number(offset, "I")
                    if start < 8 or start + 24 > len(payload):
                        raise ValueError("GPS RATIONALs point outside APP1")
                    values = [number(start + 8 * i, "I") / number(start + 8 * i + 4, "I")
                              for i in range(3)]
                    if any(d == 0 for d in values[1:]) or not all(0 <= v < 180 for v in values):
                        raise ValueError("GPS components out of range")
                    return values[0] + values[1] / 60 + values[2] / 3600
                lat, lon = dms(0x0002), dms(0x0004)
                if lat > 90 or lon >= 180:
                    raise ValueError("GPS degrees out of range")
                gps = f"{lat:.4f} {lat_ref}, {lon:.4f} {lon_ref}"
        return {"Make": make, "GPS": gps}
    except (ValueError, ZeroDivisionError, UnicodeError, struct.error):
        return None


def camera_and_position(path: Path):
    """Bounded JPEG-only reader for camera Make and GPS coordinates."""
    try:
        with Path(path).open("rb") as stream:
            if stream.read(2) != b"\xff\xd8":
                return CameraPosition(None, None, None, "absent")
            metadata = _jpeg_capture(stream)
    except OSError:
        return CameraPosition(None, None, None, "error")
    fields = metadata or {}
    make = fields.get("Make")
    gps = fields.get("GPS")
    status = "complete" if (make or gps) else "absent"
    return CameraPosition(make, gps, "jpeg-exif", status)


def _tiff_capture(payload):
    """Read two IFDs only (IFD0 -> ExifIFD), never recursive thumbnail/GPS chains."""
    if len(payload) < 8 or payload[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if payload[:2] == b"II" else ">"

    def number(offset, fmt):
        width = struct.calcsize(fmt)
        if offset < 0 or offset + width > len(payload):
            raise ValueError("Exif offset outside APP1 payload")
        return struct.unpack_from(endian + fmt, payload, offset)[0]

    def directory(offset):
        if offset < 8:
            raise ValueError("Exif directory overlaps its header")
        count = number(offset, "H")
        if count > MAX_IFD_ENTRIES or offset + 2 + 12 * count + 4 > len(payload):
            raise ValueError("Exif directory count/trailer exceeds bounded payload")
        entries = {}
        for index in range(count):
            start = offset + 2 + index * 12
            tag, kind, size = number(start, "H"), number(start + 2, "H"), number(start + 4, "I")
            if tag in entries:
                raise ValueError("ambiguous duplicate Exif tag")
            entries[tag] = (kind, size, start + 8)
        return entries

    def ascii_value(entry, expected_size):
        kind, size, offset = entry
        if kind != 2 or size != expected_size:
            raise ValueError("Exif date/offset must have its bounded ASCII type/size")
        start = offset if size <= 4 else number(offset, "I")
        if start < 8 or start + size > len(payload):
            raise ValueError("Exif ASCII points outside APP1")
        raw = payload[start:start + size]
        if raw[-1:] != b"\0" or b"\0" in raw[:-1]:
            raise ValueError("Exif ASCII is not terminated exactly once")
        return raw[:-1].decode("ascii")

    try:
        if number(2, "H") != 42:
            return None
        first_offset = number(4, "I")
        first = directory(first_offset)
        pointer = first.get(0x8769)  # ExifIFDPointer, not IFD0 DateTime 0x0132.
        if pointer is None or pointer[:2] != (4, 1):
            return None
        exif_offset = number(pointer[2], "I")
        if exif_offset == first_offset:
            return None
        entries = directory(exif_offset)
        if 0x9003 not in entries:
            return None
        original = ascii_value(entries[0x9003], 20)
        offset = ascii_value(entries[0x9011], 7) if 0x9011 in entries else None
        return {"DateTimeOriginal": original, "OffsetTimeOriginal": offset}
    except (ValueError, UnicodeError, struct.error):
        return None


def _jpeg_capture(stream):
    """At most 256 header segments / 4MiB; never read or decode image pixels."""
    metadata, found = None, False
    for _ in range(MAX_JPEG_SEGMENTS):
        if stream.tell() >= MAX_HEADER_BYTES or stream.read(1) != b"\xff":
            return None
        marker = stream.read(1)
        # JPEG permits fill bytes before a marker; cap even this tiny loop.
        for _ in range(32):
            if marker != b"\xff":
                break
            marker = stream.read(1)
        if marker in (b"\xd9", b"\xda"):
            return metadata  # EOI/SOS terminates the complete metadata header.
        if marker in (b"", b"\xff", b"\x00", b"\xd8"):
            return None
        if marker == b"\x01" or b"\xd0" <= marker <= b"\xd7":
            continue
        length = stream.read(2)
        if len(length) != 2:
            return None
        size = int.from_bytes(length, "big") - 2
        if size < 0 or stream.tell() + size > MAX_HEADER_BYTES:
            return None
        payload = stream.read(size)  # JPEG segment lengths cap a read at 65533B.
        if len(payload) != size:
            return None
        if marker == b"\xe1" and payload.startswith(b"Exif\0\0"):
            if found:
                return None  # Duplicate/ambiguous Exif blocks are not a date.
            found = True
            body = payload[6:]
            metadata = {**(_tiff_capture(body) or {}), **(_tiff_camera_gps(body) or {})}
    return None


def _imageio_capture(path):
    executable = shutil.which(IMAGEIO_EXECUTABLE)
    if executable is None:
        return CaptureResult(None, None, "unavailable")
    try:
        result = run_tool([executable, "-l", "JavaScript", "-e", _IMAGEIO_SCRIPT, str(path.resolve())], timeout_s=IMAGEIO_TIMEOUT_S, max_output=1024)
        if result.rc or result.timed_out or len(result.stdout) > 256:
            return CaptureResult(None, "imageio", "error")
        data = json.loads(result.stdout)
        if data == {"unsupported": True}:
            return CaptureResult(None, "imageio", "unsupported")
        if (not isinstance(data, dict) or set(data) != {"DateTimeOriginal", "OffsetTimeOriginal"}
                or (data["DateTimeOriginal"] is not None and not isinstance(data["DateTimeOriginal"], str))
                or (data["OffsetTimeOriginal"] is not None and not isinstance(data["OffsetTimeOriginal"], str))):
            return CaptureResult(None, "imageio", "error")
        if data["DateTimeOriginal"] is None:
            return CaptureResult(None, "imageio", "absent")
        value = parse_capture_time(data["DateTimeOriginal"], data["OffsetTimeOriginal"])
        return CaptureResult(value, "imageio", "complete" if value else "unsupported")
    except (ToolError, OSError, ValueError, TypeError):
        return CaptureResult(None, "imageio", "error")


def capture(path: Path):
    try:
        with Path(path).open("rb") as stream:
            jpeg = stream.read(2) == b"\xff\xd8"
            metadata = _jpeg_capture(stream) if jpeg else None
        if not jpeg:
            return _imageio_capture(Path(path))
        if not metadata:
            return CaptureResult(None, "jpeg-exif", "absent")
        value = parse_capture_time(metadata.get("DateTimeOriginal"), metadata.get("OffsetTimeOriginal"))
        return CaptureResult(value, "jpeg-exif", "complete" if value else "unsupported")
    except OSError:
        return CaptureResult(None, None, "error")


def taken(path: Path):
    """Compatibility tuple API; ingest uses capture() to retain retry state."""
    return capture(path).taken


def reader(path: Path):
    """Record missing native support so ordinary ingest can retry when it arrives."""
    try:
        with Path(path).open("rb") as stream:
            if stream.read(2) == b"\xff\xd8":
                return "jpeg-exif"
    except OSError:
        return None
    return "imageio" if shutil.which(IMAGEIO_EXECUTABLE) else None
