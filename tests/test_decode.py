"""Tier-1: decode ladder (BOM trio, utf-8, declared charset, lossy, override)."""

from __future__ import annotations

from goldmine.ingest.extract_text import decode_bytes


def test_utf8_bom():
    text, detail = decode_bytes("hello".encode("utf-8-sig"))
    assert text == "hello" and detail is None


def test_utf16_le_bom():
    text, detail = decode_bytes("héllo wörld".encode("utf-16"))
    assert text == "héllo wörld" and detail is None


def test_utf16_be_bom():
    text, detail = decode_bytes(b"\xfe\xff" + "abc".encode("utf-16-be"))
    assert text == "abc" and detail is None


def test_utf32_le_bom_wins_over_utf16():
    # starts with FF FE 00 00 — must read as utf-32, not utf-16
    data = "héllo".encode("utf-32")
    assert data[:4] == b"\xff\xfe\x00\x00"
    text, _ = decode_bytes(data)
    assert text == "héllo"


def test_pure_utf8():
    text, detail = decode_bytes("plain übersetzen".encode("utf-8"))
    assert text == "plain übersetzen" and detail is None


def test_invalid_utf8_declared_charset_rescues():
    data = "café".encode("windows-1252")  # é = 0xE9, invalid as utf-8
    text, detail = decode_bytes(data, declared_charset="windows-1252")
    assert text == "café" and detail is None


def test_invalid_utf8_no_declaration_lossy():
    data = "café — crème".encode("windows-1252")
    text, detail = decode_bytes(data)
    assert detail == "lossy-decode: windows-1252"
    assert "caf" in text  # lossy but readable


def test_override_wins():
    data = "café".encode("windows-1252")
    text, detail = decode_bytes(data, override="windows-1252")
    assert text == "café" and detail is None
    # override even beats a BOM
    text, detail = decode_bytes("x".encode("utf-16"), override="utf-8")
    assert detail is not None  # garbage decode attempted per the override
    # unknown codec name: ignored, ladder continues
    text, detail = decode_bytes("ok".encode("utf-8"), override="not-a-codec")
    assert text == "ok"
