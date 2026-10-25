"""Tier-2: ASR (v2 item 6, 02 §5.1) — guard, merge, pipeline wiring, fallbacks.

All offline: whisper-cli is a stub script emitting a canned -ojf JSON (with
per-token p values and one junk segment), models are fake manifest-named
files in a tmp models dir. Skipif patterns follow tests/test_media.py.
"""

from __future__ import annotations

import json
import math
import struct
import sys
import wave
from pathlib import Path

import pytest

from goldmine.chunker import est_tokens
from goldmine.config import load
from goldmine.ingest import asr as asr_mod
from goldmine.ingest import media
from goldmine.store import Store

pytestmark = pytest.mark.skipif(
    media.ffprobe_path() is None,
    reason="ffprobe not installed (brew install ffmpeg)",
)

RATE = 16000


# ------------------------------------------------------------- fixtures

def write_wav(path: Path, seconds: float) -> Path:
    """16 kHz mono PCM16 WAV with a soft tone (content is irrelevant — the
    fake whisper-cli ignores the audio and replays canned output)."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        frames = bytearray()
        for i in range(int(RATE * seconds)):
            frames += struct.pack("<h", int(2000 * math.sin(2 * math.pi * 220 * i / RATE)))
        w.writeframes(bytes(frames))
    return path


def _tok(text: str, p: float) -> dict:
    return {"text": text, "p": p, "id": 50, "t_dtw": -1}


def canned_payload() -> dict:
    """-ojf shape verified live against whisper.cpp 1.9.5 (brew):
    {"result": {"language": ...}, "transcription": [{"offsets":
    {"from": ms, "to": ms}, "text": ..., "tokens": [{"text", "p"}]}]}.

    Includes both junk classes the B1-amended guard must drop:
      - seg D: mean token p ~0.10 (hallucination on noise)
      - seg E: 36 s span with 4 chars -> ~6.7 chars/min (< 20)
    """
    good = 0.97
    segs = [
        {"offsets": {"from": 0, "to": 6000},
         "text": " lighthouse keeper logbook october the fourteenth",
         "tokens": [_tok(" [_BEG_]", 0.94), _tok(" lighthouse", good),
                    _tok(" keeper", good), _tok(" logbook", good)]},
        {"offsets": {"from": 6000, "to": 12000},
         "text": " the tide gauge recorded 4.2 meters at dawn",
         "tokens": [_tok(" the", good), _tok(" tide", good), _tok(" gauge", good)]},
        {"offsets": {"from": 12000, "to": 18000},
         "text": " the beacon FL-33 flashed three times every fifteen seconds",
         "tokens": [_tok(" the", good), _tok(" beacon", good), _tok(" FL-33", good)]},
        # D: low mean token p -> dropped
        {"offsets": {"from": 18000, "to": 24000},
         "text": " thank you thank you thank you",
         "tokens": [_tok(" thank", 0.10), _tok(" you", 0.11)]},
        # E: long span, high p, almost no text -> yield gate drops it
        {"offsets": {"from": 24000, "to": 60000},
         "text": " hmm",
         "tokens": [_tok(" hmm", 0.99)]},
    ]
    # F..K: 60s..120s, six 10 s segments of normal speech
    lines = [
        " i trimmed the lantern wick twice",
        " a clean wick keeps the flame steady",
        " the fog signal sounded every 90 seconds",
        " the relief boat brought flour and lamp oil",
        " i rewound the tide clock at noon",
        " radio check with the mainland at eight bells",
    ]
    for i, line in enumerate(lines):
        start = 60000 + i * 10000
        segs.append({
            "offsets": {"from": start, "to": start + 10000},
            "text": line,
            "tokens": [_tok(" word", good), _tok(" word2", good)],
        })
    return {"result": {"language": "en"}, "transcription": segs}


FAKE_WHISPER = r'''#!/usr/bin/env python3
import json, os, sys, time

args = sys.argv[1:]
of = None
if "-of" in args:
    of = args[args.index("-of") + 1]
sleep = float(os.environ.get("FAKE_WHISPER_SLEEP", "0") or 0)
rc = int(os.environ.get("FAKE_WHISPER_RC", "0") or 0)
if sleep:
    time.sleep(sleep)
if rc:
    sys.stderr.write("fake whisper-cli: synthetic failure\n")
    sys.exit(rc)
if of:
    with open(of + ".json", "w") as f:
        json.dump(json.loads(os.environ["FAKE_WHISPER_JSON"]), f)
sys.exit(0)
'''


def make_fake_whisper(tmp_path: Path) -> Path:
    stub = tmp_path / "fake-whisper-cli"
    stub.write_text(FAKE_WHISPER)
    stub.chmod(0o755)
    return stub


def make_fake_models(tmp_path: Path) -> Path:
    models = tmp_path / "models"
    models.mkdir()
    (models / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"GGML-fake-asr")
    (models / "ggml-silero-v5.1.2.bin").write_bytes(b"GGML-fake-vad")
    return models


@pytest.fixture
def asr_cfg(tmp_path, monkeypatch):
    """Config wired to the stub whisper + fake models (hermetic)."""
    stub = make_fake_whisper(tmp_path)
    models = make_fake_models(tmp_path)
    monkeypatch.setenv("GOLDMINE_WHISPER", str(stub))
    monkeypatch.setenv("FAKE_WHISPER_JSON", json.dumps(canned_payload()))
    cfg = load()
    cfg.home = tmp_path / "home"
    cfg.models.dir = models
    return cfg


# ------------------------------------------------------------- guard units

def test_guard_drops_low_mean_p_and_low_yield_segments():
    kept, dropped = asr_mod._guard_segments(canned_payload()["transcription"])
    texts = " ".join(s.text for s in kept)
    assert dropped == 2
    assert "thank you" not in texts       # mean token p 0.10 < 0.50
    assert "hmm" not in texts             # ~6.7 chars/min < 20
    assert "lighthouse" in texts
    assert all(s.start_s < s.end_s for s in kept)


def test_guard_without_token_probs_is_noop():
    """B1: builds without -ojf token probs get NO confidence guard (whisper-cli
    emits no no_speech_prob/avg_logprob — the guard must not guess)."""
    raw = [{"offsets": {"from": 0, "to": 5000}, "text": " no probs here",
            "tokens": [{"text": " no"}, {"text": " probs"}]}]
    kept, dropped = asr_mod._guard_segments(raw)
    assert dropped == 0 and len(kept) == 1


def test_merge_segments_bounds_and_locs():
    """40 s span cap + ~450 est-token cap (02 F8: tokens replaced the 900-char
    cap so transcript chunks pack like text chunks)."""
    segs = [
        asr_mod.AsrSegment("a" * 100, 0.0, 15.0),
        asr_mod.AsrSegment("b" * 100, 15.0, 30.0),
        asr_mod.AsrSegment("c" * 100, 30.0, 45.0),   # span 45s > 40 -> new
        asr_mod.AsrSegment("d" * 1704, 45.0, 55.0),  # 100+1+1704 chars = 451 est-tok -> new
        asr_mod.AsrSegment("e" * 10, 55.0, 60.0),
    ]
    merged = asr_mod.merge_segments(segs)
    assert len(merged) == 3
    assert merged[0].start_s == 0.0 and merged[0].end_s == 30.0
    assert merged[1].start_s == 30.0 and merged[1].end_s == 45.0
    assert merged[2].start_s == 45.0 and merged[2].end_s == 60.0
    for m in merged:
        assert m.end_s - m.start_s <= 40.0
        assert est_tokens(m.text) <= asr_mod.MERGE_MAX_TOKENS


def test_merge_respects_max_tokens():
    """Dense segments merge up to the token budget, never past it."""
    segs = [asr_mod.AsrSegment("x" * 700, float(i), float(i + 1)) for i in range(5)]
    merged = asr_mod.merge_segments(segs)
    for m in merged:
        assert est_tokens(m.text) <= asr_mod.MERGE_MAX_TOKENS


def test_merge_packs_finer_than_the_old_char_cap():
    """The under-fill fix, pinned: two ~850-char segments inside 40 s now merge
    (the old 900-char cap split them); a >450-token candidate still splits."""
    a = asr_mod.AsrSegment("word " * 170, 0.0, 18.0)   # 850 chars
    b = asr_mod.AsrSegment("word " * 170, 18.0, 36.0)  # 850 chars
    merged = asr_mod.merge_segments([a, b])
    assert len(merged) == 1  # 1701 chars = ~425 est-tokens <= 450: packed
    giant = asr_mod.AsrSegment("word " * 400, 40.0, 41.0)  # 2000 chars ~ 500 est-tok
    assert len(asr_mod.merge_segments([merged[0], giant])) == 2


# --------------------------------------------------------- resolve + config

def test_resolve_tools_found_via_env(asr_cfg):
    tools = asr_mod.resolve_tools(asr_cfg)
    assert tools is not None
    assert tools.whisper == asr_cfg.ingest.asr_whisper  # GOLDMINE_WHISPER
    assert tools.model.name == "ggml-large-v3-turbo-q5_0.bin"
    assert tools.vad.name == "ggml-silero-v5.1.2.bin"


def test_resolve_tools_missing_whisper_is_none(asr_cfg, tmp_path):
    asr_cfg.ingest.asr_whisper = str(tmp_path / "absent-whisper-cli")
    assert asr_mod.resolve_tools(asr_cfg) is None


def test_resolve_tools_missing_models_is_none(asr_cfg, tmp_path, monkeypatch):
    asr_cfg.models.dir = asr_cfg.home  # no models there
    # and no repo dev copy either (this checkout HAS models/whisper/)
    import goldmine.model as model_mod

    monkeypatch.setattr(model_mod, "REPO_MODELS", tmp_path / "empty-repo")
    assert asr_mod.resolve_tools(asr_cfg) is None


def test_resolve_whisper_cli_order(tmp_path, monkeypatch):
    import shutil as _shutil

    brew = tmp_path / "brew-whisper"
    brew.write_text("#!/bin/sh\n")
    brew.chmod(0o755)
    pathbin = tmp_path / "path-whisper"
    monkeypatch.setattr(asr_mod, "BREW_WHISPER", str(brew))
    cfg = load()
    cfg.ingest.asr_whisper = ""
    assert asr_mod.resolve_whisper_cli(cfg) == str(brew)      # brew beats PATH
    monkeypatch.setattr(asr_mod, "BREW_WHISPER", str(tmp_path / "absent"))
    monkeypatch.setattr(_shutil, "which", lambda name: str(pathbin))
    assert asr_mod.resolve_whisper_cli(cfg) == str(pathbin)   # PATH fallback
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    cfg.ingest.asr_whisper = "/explicit/whisper-cli"
    assert asr_mod.resolve_whisper_cli(cfg) == "/explicit/whisper-cli"  # explicit wins


def test_config_asr_keys_parse(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        "[ingest]\nasr = \"always\"\nasr_max_minutes = 15.5\n"
        "asr_language = \"zh\"\nasr_whisper = \"/w/whisper-cli\"\n",
        encoding="utf-8",
    )
    cfg = load(config_path=cfg_file)
    assert cfg.ingest.asr == "always"
    assert cfg.ingest.asr_max_minutes == 15.5
    assert cfg.ingest.asr_language == "zh"
    assert cfg.ingest.asr_whisper == "/w/whisper-cli"
    # invalid mode is refused loudly (falls back to the default)
    cfg_file.write_text("[ingest]\nasr = \"sometimes\"\n", encoding="utf-8")
    cfg2 = load(config_path=cfg_file)
    assert cfg2.ingest.asr == "auto"


# ---------------------------------------------------------- transcribe unit

def test_transcribe_canned_stub(tmp_path, asr_cfg):
    wav = write_wav(tmp_path / "speech.wav", 3.0)
    result = asr_mod.transcribe(wav, asr_cfg)
    assert result.language == "en"
    assert result.dropped == 2
    # A+B+C merged (0-18s), F..I merged (60-100s), J+K merged (100-120s)
    assert len(result.segments) == 3
    first = result.segments[0]
    assert first.start_s == 0.0 and first.end_s == 18.0
    assert "lighthouse keeper logbook" in first.text
    assert "thank you" not in first.text
    assert result.segments[1].start_s == 60.0
    assert result.segments[2].end_s == 120.0


def test_transcribe_timeout_raises(asr_cfg, tmp_path, monkeypatch):
    wav = write_wav(tmp_path / "speech.wav", 1.0)
    monkeypatch.setenv("FAKE_WHISPER_SLEEP", "3")
    with pytest.raises(asr_mod.AsrError, match="timeout"):
        asr_mod.transcribe(wav, asr_cfg, timeout_s=0.5)


def test_transcribe_nonzero_rc_raises(asr_cfg, tmp_path, monkeypatch):
    wav = write_wav(tmp_path / "speech.wav", 1.0)
    monkeypatch.setenv("FAKE_WHISPER_RC", "7")
    with pytest.raises(asr_mod.AsrError, match="rc=7"):
        asr_mod.transcribe(wav, asr_cfg)


# ------------------------------------------------------------ pipeline path

def _rows(store, name):
    return store.db.execute(
        "SELECT f.path, f.capability, f.detail, f.info FROM files f "
        "WHERE f.path LIKE ?", (f"%{name}%",)
    ).fetchall()


def test_pipeline_asr_transcript_segments_with_vectors(tmp_path, asr_cfg):
    """wav + fake whisper -> kind='audio', modality='text' transcript segments
    with FTS bodies AND vectors under a plain TEXT-ONLY mock (the llama
    shape: cap.text True, cap.audio False)."""
    from goldmine.backends.mock import MockBackend
    from goldmine.ingest import IngestPipeline

    root = tmp_path / "corpus"
    root.mkdir()
    write_wav(root / "logbook.wav", 3.0)

    store = Store(asr_cfg.home / "idx.sqlite")
    try:
        with MockBackend(dim=768) as backend:  # text-only default mock
            report = IngestPipeline(asr_cfg, store, backend).run([str(root)])
        assert report.failed == 0
        row = _rows(store, "logbook.wav")[0]
        assert row[1] == "content_indexed"
        info = json.loads(row[3])
        assert info["language"] == "en"
        assert info["asr_dropped"] == 2
        assert info["asr_segments"] == 3
        segs = store.db.execute(
            "SELECT s.kind, s.modality, s.loc, s.loc_end, s.vec IS NOT NULL, "
            "fts.body FROM segments s JOIN files f ON s.file = f.id "
            "JOIN fts ON fts.rowid = s.id WHERE f.path LIKE '%logbook.wav' "
            "AND s.kind != 'name'"
        ).fetchall()
        assert len(segs) == 3
        for kind, modality, loc, loc_end, has_vec, body in segs:
            assert kind == "audio"
            assert modality == "text"
            assert has_vec  # transcript vectors under a text-capable backend
            assert body.strip()
        assert segs[0][2] == 0.0 and segs[0][3] == 18.0   # merged locs (s)
        # extractor identity: the ASR family is current, never stale
        from goldmine import fingerprints

        assert "asr-whisper.v2" in fingerprints.current_extractors()
        rec = store.db.execute(
            "SELECT extractor_version FROM files WHERE path LIKE '%logbook.wav'"
        ).fetchone()
        assert rec[0] == "asr-whisper.v2"
    finally:
        store.close()


def test_pipeline_whisper_missing_falls_back(tmp_path, asr_cfg):
    """ingest.asr=auto + no whisper: raw-window path under multimodal mock,
    metadata-only under a text-only mock (02 §5.1 decision matrix)."""
    from goldmine.backends.mock import MockBackend
    from goldmine.ingest import IngestPipeline

    asr_cfg.ingest.asr_whisper = str(tmp_path / "absent-whisper-cli")
    root = tmp_path / "corpus"
    root.mkdir()
    write_wav(root / "tone.wav", 2.5)  # B1: >=2 s survives the tail gate

    # multimodal: v1 raw-window path (media-ffmpeg extractor)
    store = Store(asr_cfg.home / "mm.sqlite")
    try:
        with MockBackend(dim=768, multimodal=True) as backend:
            report = IngestPipeline(asr_cfg, store, backend).run([str(root)])
        assert report.failed == 0
        row = _rows(store, "tone.wav")[0]
        assert row[1] == "content_indexed"
        assert row[3] and json.loads(row[3]).get("windows") == 1
        seg = store.db.execute(
            "SELECT modality FROM segments s JOIN files f ON s.file = f.id "
            "WHERE f.path LIKE '%tone.wav' AND s.kind != 'name'"
        ).fetchone()
        assert seg[0] == "audio"  # raw window vector, not a transcript
    finally:
        store.close()

    # text-only: metadata-only with the honest backend reason
    root2 = tmp_path / "corpus2"
    root2.mkdir()
    write_wav(root2 / "tone.wav", 1.0)
    store2 = Store(asr_cfg.home / "to.sqlite")
    try:
        with MockBackend(dim=768) as backend:
            report = IngestPipeline(asr_cfg, store2, backend).run([str(root2)])
        assert report.failed == 0
        row = _rows(store2, "tone.wav")[0]
        assert row[1] == "metadata_only"
        assert "audio embedding" in (row[2] or "")
    finally:
        store2.close()


def test_pipeline_asr_off_keeps_v1_path(tmp_path, asr_cfg):
    from goldmine.backends.mock import MockBackend
    from goldmine.ingest import IngestPipeline

    asr_cfg.ingest.asr = "off"
    root = tmp_path / "corpus"
    root.mkdir()
    write_wav(root / "tone.wav", 2.5)  # B1: >=2 s survives the tail gate
    store = Store(asr_cfg.home / "off.sqlite")
    try:
        with MockBackend(dim=768, multimodal=True) as backend:
            IngestPipeline(asr_cfg, store, backend).run([str(root)])
        row = _rows(store, "tone.wav")[0]
        assert row[1] == "content_indexed"
        assert json.loads(row[3]).get("windows") == 1  # raw windows, no ASR
    finally:
        store.close()


def test_pipeline_asr_always_failure_is_failed_row(tmp_path, asr_cfg, monkeypatch):
    """ingest.asr=always: a crashing whisper fails the file honestly (no
    silent raw-window fallback)."""
    from goldmine.backends.mock import MockBackend
    from goldmine.ingest import IngestPipeline

    asr_cfg.ingest.asr = "always"
    monkeypatch.setenv("FAKE_WHISPER_RC", "3")
    root = tmp_path / "corpus"
    root.mkdir()
    write_wav(root / "speech.wav", 1.0)
    store = Store(asr_cfg.home / "fail.sqlite")
    try:
        with MockBackend(dim=768, multimodal=True) as backend:
            report = IngestPipeline(asr_cfg, store, backend).run([str(root)])
        assert report.failed == 1
        row = _rows(store, "speech.wav")[0]
        assert row[1] == "failed"
        assert "asr failed" in (row[2] or "") and "rc=3" in (row[2] or "")
    finally:
        store.close()


def test_pipeline_asr_timeout_is_failed_row(tmp_path, asr_cfg, monkeypatch):
    """The subprocess timeout path: file failed with an honest detail."""
    from goldmine.backends.mock import MockBackend
    from goldmine.ingest import IngestPipeline

    asr_cfg.ingest.asr = "always"
    monkeypatch.setenv("FAKE_WHISPER_SLEEP", "3")
    root = tmp_path / "corpus"
    root.mkdir()
    write_wav(root / "speech.wav", 1.0)
    store = Store(asr_cfg.home / "slow.sqlite")
    try:
        real_transcribe = asr_mod.transcribe
        monkeypatch.setattr(
            asr_mod, "transcribe",
            lambda p, c: real_transcribe(p, c, timeout_s=0.5),
        )
        with MockBackend(dim=768) as backend:
            report = IngestPipeline(asr_cfg, store, backend).run([str(root)])
        assert report.failed == 1
        row = _rows(store, "speech.wav")[0]
        assert row[1] == "failed"
        assert "timeout" in (row[2] or "")
    finally:
        store.close()


def test_pipeline_asr_always_no_speech_is_metadata(tmp_path, asr_cfg, monkeypatch):
    """always + VAD finds nothing -> honest metadata_only, never raw windows."""
    from goldmine.backends.mock import MockBackend
    from goldmine.ingest import IngestPipeline

    asr_cfg.ingest.asr = "always"
    empty = json.dumps({"result": {"language": "en"}, "transcription": []})
    monkeypatch.setenv("FAKE_WHISPER_JSON", empty)
    root = tmp_path / "corpus"
    root.mkdir()
    write_wav(root / "silence.wav", 1.0)
    store = Store(asr_cfg.home / "empty.sqlite")
    try:
        with MockBackend(dim=768, multimodal=True) as backend:
            report = IngestPipeline(asr_cfg, store, backend).run([str(root)])
        assert report.failed == 0
        row = _rows(store, "silence.wav")[0]
        assert row[1] == "metadata_only"
        assert "no speech" in (row[2] or "")
    finally:
        store.close()


def test_pipeline_asr_max_minutes_partial(tmp_path, asr_cfg):
    """Over-cap source: partial + read_end honesty (02 §5.1 budgets)."""
    from goldmine.backends.mock import MockBackend
    from goldmine.ingest import IngestPipeline

    asr_cfg.ingest.asr_max_minutes = 1.0 / 60.0  # 1 second cap
    root = tmp_path / "corpus"
    root.mkdir()
    write_wav(root / "long.wav", 3.0)
    store = Store(asr_cfg.home / "cap.sqlite")
    try:
        with MockBackend(dim=768) as backend:
            report = IngestPipeline(asr_cfg, store, backend).run([str(root)])
        assert report.failed == 0
        assert report.partial == 1
        row = _rows(store, "long.wav")[0]
        info = json.loads(row[3])
        assert info["partial"] is True
        assert info["read_end"] == 1.0
    finally:
        store.close()


def test_lexical_only_never_runs_asr(tmp_path, asr_cfg, monkeypatch):
    from goldmine.ingest import IngestPipeline

    calls = []
    real = asr_mod.transcribe
    monkeypatch.setattr(
        asr_mod, "transcribe",
        lambda p, c: calls.append(p) or real(p, c),
    )
    root = tmp_path / "corpus"
    root.mkdir()
    write_wav(root / "tone.wav", 1.0)
    store = Store(asr_cfg.home / "lex.sqlite")
    try:
        IngestPipeline(asr_cfg, store, None, lexical_only=True).run([str(root)])
        assert calls == []
        row = _rows(store, "tone.wav")[0]
        assert row[1] == "metadata_only"
    finally:
        store.close()
