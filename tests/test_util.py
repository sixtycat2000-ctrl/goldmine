"""Tier-1 unit tests: util primitives (fp16, excerpt, MPEG-TS, magic, run_tool)."""

from __future__ import annotations

import sys

import numpy as np
import pytest

from goldmine import util


def test_fp16_roundtrip():
    vec = np.linspace(-1, 1, 768, dtype=np.float32)
    blob = util.pack_fp16(vec)
    assert len(blob) == 1536
    back = util.unpack_fp16(blob)
    assert back.dtype == np.float32
    assert np.allclose(back, vec.astype(np.float16).astype(np.float32), atol=1e-3)


def test_pack_rejects_wrong_dim():
    with pytest.raises(ValueError):
        util.pack_fp16(np.zeros(767, dtype=np.float32))


def test_unpack_roundtrip_vs_hand_packed():
    vec = np.arange(768, dtype=np.float32) / 768
    hand = vec.astype("<f2").tobytes()
    assert util.pack_fp16(vec) == hand
    assert np.allclose(util.unpack_fp16(hand), vec.astype(np.float16).astype(np.float32))


def test_excerpt_digup_semantics():
    assert util.excerpt("a  b\t\nc", 100) == "a b c"
    long = "x" * 300
    out = util.excerpt(long, 240)
    assert out == "x" * 240 + "…"
    assert len(util.excerpt("  spaced   out  ", 10).strip()) <= 10


def test_normalize_text():
    n, c = util.normalize_text("caf\u00e9")  # precomposed
    assert n == "caf\u00e9" and c is False
    raw = "e\u0301"  # e + combining acute (decomposed)
    n, c = util.normalize_text(raw)
    assert n == "\u00e9" and c is True


def test_mpeg_ts_heads():
    # 188-byte packets
    good_188 = b"\x47" + b"\x00" * 187 + b"\x47" + b"\x00" * 187 + b"\x47" + b"\x00" * 12
    assert len(good_188) == 389 + 1 or len(good_188) >= 389
    assert util.is_mpeg_ts(good_188)
    # 192-byte packets (4-byte timecode)
    good_192 = b"\x00" * 4 + b"\x47" + b"\x00" * 187
    good_192 += b"\x00" * 4 + b"\x47" + b"\x00" * 187
    good_192 += b"\x00" * 4 + b"\x47" + b"\x00" * 8
    assert util.is_mpeg_ts(good_192)
    # typescript text: not a transport stream
    ts = ("const x: number = 1; // " * 20).encode()[:600]
    assert not util.is_mpeg_ts(ts)
    assert not util.is_mpeg_ts(b"\x47 short")


def test_sniff_image():
    assert util.sniff_image(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == "jpeg"
    assert util.sniff_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "png"
    assert util.sniff_image(b"GIF89a" + b"\x00" * 8) == "gif"
    assert util.sniff_image(b"RIFF\x00\x00\x00\x00WEBP") == "webp"
    assert util.sniff_image(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8) == "heic"
    assert util.sniff_image(b"not an image at all") is None


def test_sha256_helpers(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"goldmine")
    assert util.sha256_file(p) == util.sha256_bytes(b"goldmine")
    import hashlib

    assert util.sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()


def test_human_size():
    assert util.human_size(0) == "0 B"
    assert util.human_size(1023) == "1023 B"
    assert util.human_size(41 * 1024 * 1024) == "41.0 MB"


def test_run_tool_basic():
    result = util.run_tool([sys.executable, "-c", "print('out')"], timeout_s=10)
    assert result.rc == 0
    assert result.stdout.strip() == "out"
    assert not result.timed_out


def test_run_tool_timeout_kills():
    result = util.run_tool(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
        timeout_s=1.0,
    )
    assert result.timed_out
    assert result.rc is None
    assert "started" in result.stdout


def test_run_tool_output_cap():
    result = util.run_tool(
        [sys.executable, "-c", "print('x' * 200000)"], timeout_s=15, max_output=1000
    )
    assert result.rc == 0
    assert len(result.stdout) == 1000


def test_run_tool_missing_binary():
    with pytest.raises(util.ToolError):
        util.run_tool(["definitely-not-a-real-binary-xyz"], timeout_s=5)


# --------------------------------------------- snippet machinery (§5.6, D4)


def test_snippet_prose_density_window():
    """The window anchors on term density, not text start: the marked region
    is the needle cluster, caps applied on both sides."""
    text = ("intro filler paragraph. " * 12
            + "the needle sits here and the needle repeats nearby. "
            + "trailing filler paragraph. " * 12)
    out = util.snippet(text, ["needle"], width=120)
    assert out is not None
    assert out.count("«needle»") == 2            # the whole cluster in one window
    assert "sits" in out                          # window covers the cluster
    assert out.startswith("…") and out.endswith("…")


def test_snippet_marks_every_distinct_term():
    text = "alpha here beta there alpha again beta end"
    out = util.snippet(text, ["alpha", "beta"], width=200)
    assert out == "«alpha» here «beta» there «alpha» again «beta» end"


def test_snippet_no_match_returns_none():
    # the caller keeps the STORED excerpt for a pure-semantic hit
    assert util.snippet("nothing relevant here", ["needle"], width=120) is None


def test_snippet_cjk_needs_no_spaces():
    text = "这是一段很长的中文文本。" * 10 + "关键词出现在这里。" + "更多中文内容继续写。" * 10
    out = util.snippet(text, ["关键词"], width=60)
    assert out is not None
    assert "«关键词»" in out
    assert out.startswith("…")  # anchored at the match, not the text start
    assert "出现在这里" in out


def test_snippet_case_and_accent_fold():
    out = util.snippet("The Café opens early.", ["cafe"], width=80)
    assert out == "The «Café» opens early."


def test_snippet_prefix_term_marks_whole_word():
    # while-typing last term: FTS matched kubernetes via prefix; the mark
    # extends through the whole word (DigUp inflects)
    out = util.snippet("kubernetes cluster setup", ["kubernet"], width=80,
                       prefix_term="kubernet")
    assert out == "«kubernetes» cluster setup"
    # without the prefix flag the same scan is whole-word only -> no match
    assert util.snippet("kubernetes cluster setup", ["kubernet"], width=80) is None


def test_term_matches_word_boundaries_and_offsets():
    text = "needle needles pin needle"
    assert util.term_matches("needle", text) == [(0, 6), (19, 25)]
    assert util.term_matches("needle", text, prefix=True) == [
        (0, 6), (7, 14), (19, 25)  # 'needles' marked whole under prefix
    ]
