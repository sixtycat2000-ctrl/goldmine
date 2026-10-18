"""Tier-1/2: HTML stripping, charset handling, URL canonicalization."""

from __future__ import annotations

from goldmine.ingest.extract_html import (
    canonicalize,
    charset_declared,
    extract_html_file,
    html_to_text,
)
from tests.conftest import HTML_PAGE


def test_charset_declared_meta():
    assert charset_declared(b'<html><meta charset="utf-8">') == "utf-8"
    assert charset_declared(HTML_PAGE.encode("windows-1252")) == "windows-1252"


def test_charset_declared_http_equiv():
    head = b'<meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">'
    assert charset_declared(head).lower() == "iso-8859-1"


def test_charset_declared_none():
    assert charset_declared(b"<html><body>plain</body></html>") is None


def test_html_to_text_strips_script_style_head():
    text, title = html_to_text(HTML_PAGE)
    assert "tracking" not in text          # <script> content dropped
    assert "color: red" not in text        # <style> dropped
    assert title == "Brève histoire du café"  # <title> survives head-dropping
    assert "Café Notes" in text            # h1 with named entity decoded
    assert "First paragraph about roasting beans élightly." in text
    assert "a comment" not in text         # comment dropped


def test_html_to_text_block_structure():
    text, _ = html_to_text(
        "<p>one</p><p>two</p><div>three</div><ul><li>a</li><li>b</li></ul>"
    )
    lines = [l for l in text.split("\n") if l.strip()]
    assert lines == ["one", "two", "three", "a", "b"]


def test_html_to_text_numeric_entities():
    text, _ = html_to_text("<p>&#39;quoted&#39; &amp; &#x263A;</p>")
    assert "'quoted' & ☺" in text


def test_html_to_text_malformed_keeps_partial():
    text, _ = html_to_text("<p>ok text<div>unclosed")
    assert "ok text" in text


def test_extract_html_file_declared_charset(tmp_path):
    p = tmp_path / "page.html"
    p.write_text(HTML_PAGE, encoding="windows-1252")
    ex = extract_html_file(p)
    assert ex.detail and ex.detail.startswith("declared-charset")
    assert "Brève histoire du café" in (ex.title or "")
    assert "roasting beans élightly" in ex.text
    assert ex.tool_version == "html-stdlib.v1"


def test_extract_html_file_utf16_bom(tmp_path):
    p = tmp_path / "page_utf16.html"
    p.write_text("<html><head><title>Tïtle</title></head><body>bödy</body></html>",
                 encoding="utf-16")
    ex = extract_html_file(p)
    assert ex.detail is None  # BOM handled before any charset guessing
    assert ex.text == "bödy"
    assert ex.title == "Tïtle"


def test_extract_html_file_encoding_override(tmp_path):
    p = tmp_path / "page.html"
    p.write_text("<html><body>naïve</body></html>", encoding="windows-1252")
    ex = extract_html_file(p, encoding_override="windows-1252")
    assert "naïve" in ex.text


# ---------------------------------------------------------------- canonicalize


def test_canonicalize_lowercase_scheme_host():
    assert canonicalize("HTTP://Example.COM/A") == "http://example.com/A"


def test_canonicalize_drops_fragment():
    assert canonicalize("https://x.dev/a#top") == "https://x.dev/a"


def test_canonicalize_drops_default_port():
    assert canonicalize("http://x.dev:80/a") == "http://x.dev/a"
    assert canonicalize("https://x.dev:443/") == "https://x.dev/"
    assert canonicalize("http://x.dev:8080/a") == "http://x.dev:8080/a"


def test_canonicalize_resolves_dot_segments():
    assert canonicalize("https://x.dev/a/../b") == "https://x.dev/b"
    assert canonicalize("https://x.dev/a/./b") == "https://x.dev/a/b"


def test_canonicalize_dedupe_forms():
    forms = {
        canonicalize("https://x.dev/a"),
        canonicalize("https://x.dev/a/"),
        canonicalize("https://x.dev/a#frag"),
        canonicalize("HTTPS://X.dev/a"),
    }
    assert len(forms) == 1
    assert forms == {"https://x.dev/a"}


def test_canonicalize_keeps_query():
    assert canonicalize("https://x.dev/s?q=1&r=2") == "https://x.dev/s?q=1&r=2"


def test_canonicalize_empty_path_becomes_root():
    assert canonicalize("https://x.dev") == "https://x.dev/"
