"""Bounded EXIF camera/GPS reader and the D4 source revision (2026-10-23)."""
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from goldmine.ingest.exif import camera_and_position, capture


def build_jpeg(exif_tiff: bytes) -> bytes:
    """Minimal header-only JPEG: SOI + APP1(Exif) + EOI — the parsers only
    read the header, so no pixel payload is needed (hermetic, no sips)."""
    app1 = b"Exif\x00\x00" + exif_tiff
    return b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xd9"


def encode_tiff(make=None, date=None, offset=None, gps=None):
    """Little-endian TIFF: IFD0 with Make/Exif/GPS pointers as requested."""
    def ascii_bytes(value):
        raw = value.encode("ascii") + b"\x00"
        return raw + b"\x00" * ((4 - len(raw) % 4) % 4)

    ifd0_count = 1 + (date is not None) + (gps is not None)
    ifd0_off = 8
    cursor = ifd0_off + 2 + 12 * ifd0_count + 4
    blocks = {}
    if make is not None:
        blocks["make"] = ascii_bytes(make)
        make_off, cursor = cursor, cursor + len(blocks["make"])
    exif_entries = []
    if date is not None:
        exif_entries.append((0x9003, 2, ascii_bytes(date)))
    if offset is not None:
        exif_entries.append((0x9011, 2, ascii_bytes(offset)))
    exif_off = cursor if exif_entries else None
    if exif_entries:
        cursor += 2 + 12 * len(exif_entries) + 4
        for _, _, raw in exif_entries:
            blocks[id(raw)] = raw
    gps_off = None
    if gps is not None:
        lat_deg, lat_ref, lon_deg, lon_ref = gps
        entries = [
            (0x0001, 2, ascii_bytes(lat_ref)),
            (0x0002, 5, b"".join(struct.pack("<II", n, 1) for n in lat_deg)),
            (0x0003, 2, ascii_bytes(lon_ref)),
            (0x0004, 5, b"".join(struct.pack("<II", n, 1) for n in lon_deg)),
        ]
        gps_off = cursor
        cursor += 2 + 12 * 4 + 4
        for _, _, raw in entries:
            blocks[id(raw)] = raw

    payload = bytearray(b"II\x2a\x00" + struct.pack("<I", ifd0_off))
    payload += struct.pack("<H", ifd0_count)
    entries = []
    if make is not None:
        entries.append((0x010F, 2, len(make) + 1, make_off))
    if exif_off is not None:
        entries.append((0x8769, 4, 1, exif_off))
    if gps_off is not None:
        entries.append((0x8825, 4, 1, gps_off))
    for tag, kind, size, off in entries:
        payload += struct.pack("<HHI", tag, kind, size)
        if kind == 2 and size <= 4:
            payload += (blocks["make"][:4] if tag == 0x010F else b"\x00\x00N\x00")[:4]
        elif kind == 4 and size == 1:
            payload += struct.pack("<I", off)
        else:
            payload += struct.pack("<I", off)
    payload += struct.pack("<I", 0)
    if exif_entries:
        payload += struct.pack("<H", len(exif_entries))
        data_cursor = exif_off + 2 + 12 * len(exif_entries) + 4
        for tag, kind, raw in exif_entries:
            size = {0x9003: 20, 0x9011: 7}[tag]
            payload += struct.pack("<HHI", tag, kind, size)
            payload += struct.pack("<I", data_cursor)
            data_cursor += len(raw)
        payload += struct.pack("<I", 0)
        for _, _, raw in exif_entries:
            payload += raw
    if gps_off is not None:
        payload += struct.pack("<H", 4)
        data_cursor = gps_off + 2 + 12 * 4 + 4
        for tag, kind, raw in [(0x0001, 2, None), (0x0002, 5, None), (0x0003, 2, None), (0x0004, 5, None)]:
            if tag == 0x0001:
                payload += struct.pack("<HHI", tag, 2, 2) + ascii_bytes(lat_ref)[:4]
                continue
            if tag == 0x0003:
                payload += struct.pack("<HHI", tag, 2, 2) + ascii_bytes(lon_ref)[:4]
                continue
            payload += struct.pack("<HHI", tag, kind, 3) + struct.pack("<I", data_cursor)
            data_cursor += 24
        payload += struct.pack("<I", 0)
        payload += b"".join(struct.pack("<II", n, 1) for n in lat_deg)
        payload += b"".join(struct.pack("<II", n, 1) for n in lon_deg)
    payload += blocks.get("make", b"")
    return bytes(payload)


def test_make_date_and_gps_roundtrip(tmp_path):
    from make_coverage_fixture import encode_exif_tiff

    tiff = encode_exif_tiff("Canon", "2026:10:14 09:26:03", "+08:00",
                            (35, 18, 33), "N", (139, 47, 12), "E")
    photo = tmp_path / "photo-lighthouse.jpg"
    photo.write_bytes(build_jpeg(tiff))
    taken = capture(photo).taken
    assert taken is not None and taken[0] == "2026-10-14 09:26:03"
    camera = camera_and_position(photo)
    assert camera.make == "Canon" and camera.status == "complete"
    assert camera.gps == "35.3092 N, 139.7867 E"


def test_partial_fields_are_independent(tmp_path):
    tiff = encode_tiff(make="Fujifilm")  # no date, no GPS
    photo = tmp_path / "a.jpg"
    photo.write_bytes(build_jpeg(tiff))
    assert capture(photo).status in ("absent", "unsupported")
    camera = camera_and_position(photo)
    assert camera.make == "Fujifilm" and camera.gps is None


def test_no_exif_is_absent_and_non_jpeg_never_parsed(tmp_path):
    jpg = tmp_path / "plain.jpg"
    jpg.write_bytes(b"\xff\xd8" + b"\xff\xd9")
    assert camera_and_position(jpg).status == "absent"
    png = tmp_path / "plain.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert camera_and_position(png).status == "absent"


def test_oversized_make_is_rejected_not_guessed(tmp_path):
    tiff = encode_tiff(make="X" * 65)
    photo = tmp_path / "big.jpg"
    photo.write_bytes(build_jpeg(tiff))
    camera = camera_and_position(photo)
    assert camera.make is None  # bound violation -> no value, never truncation


def test_fixture_generator_emits_real_exif(tmp_path):
    """D4 revision: the fixture photos genuinely carry camera/date/GPS bytes
    that this product's bounded readers verify end-to-end."""
    from make_coverage_fixture import encode_exif_tiff

    tiff = encode_exif_tiff("Canon", "2026:10:14 09:26:03", "+08:00",
                            (35, 18, 33), "N", (139, 47, 12), "E")
    photo = tmp_path / "photo-lighthouse.jpg"
    photo.write_bytes(build_jpeg(tiff))
    assert camera_and_position(photo).make == "Canon"
    assert capture(photo).taken[0] == "2026-10-14 09:26:03"
    assert camera_and_position(photo).gps == "35.3092 N, 139.7867 E"
