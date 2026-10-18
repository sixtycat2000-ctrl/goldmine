"""v2 item 5 (B1·B2): model manifest + manager + `goldmine model` CLI.

Fully offline: no network, no real downloads. Download paths are exercised
against `file://` sources and injected fake urlopen responses; manifest pins
are verified against the repo's real model files when present (B7) and skipped
on clones without them.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import urllib.error
from dataclasses import dataclass
from pathlib import Path

import pytest

from goldmine import model as model_mod
from goldmine.config import load
from goldmine.model import (
    DISK_SLACK_BYTES,
    ModelError,
    ModelManager,
    set_config_llama_model,
)
from goldmine.model_manifest import (
    DEFAULT_EMBEDDING_KEY,
    MANIFEST,
    ModelFile,
    by_key,
    repo_relpath,
)

FAKE_SERVER = Path(__file__).resolve().parent / "fake_llama_server.py"


# ------------------------------------------------------------------ helpers


def fake_entry(key: str, path: Path, role: str = "embedding", dim: int = 768):
    """A manifest entry pinning a REAL temp file (its own bytes+sha)."""
    data = path.read_bytes()
    return ModelFile(
        key=key, role=role, repo="fake-org/fake-models", revision="deadbeef",
        name=path.name, bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(), dim=dim,
    )


@dataclass(frozen=True)
class LocalFile(ModelFile):
    """Entry whose url() points at a local file (offline download tests)."""

    source_uri: str = ""

    def url(self) -> str:
        return self.source_uri


def local_entry(key: str, path: Path, role: str = "embedding") -> LocalFile:
    return LocalFile(
        key=key, role=role, repo="fake-org/fake-models", revision="deadbeef",
        name=path.name, bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        source_uri=path.resolve().as_uri(),
    )


def make_manager(tmp_home, entries, repo_models: Path | None = None, **kwargs):
    cfg = load(config_path=tmp_home / "none.toml", env={
        "GOLDMINE_HOME": str(tmp_home / "gmhome"),
    })
    return ModelManager(
        cfg, manifest=entries, repo_models=repo_models or tmp_home / "no-repo",
        err=lambda m: None, **kwargs,
    )


# ------------------------------------------------------------- manifest (B7)


def test_manifest_shape_and_urls():
    keys = [e.key for e in MANIFEST]
    assert keys == [
        "embeddinggemma-2-Q8_0", "mmproj-embeddinggemma-2-Q8_0",
        "whisper-large-v3-turbo-q5_0", "silero-vad-v5.1.2",
    ]
    roles = {e.role for e in MANIFEST}
    assert roles == {"embedding", "mmproj", "asr", "vad"}
    embed = by_key("embeddinggemma-2-Q8_0")
    assert embed.url() == (
        "https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve/"
        "bfcd298762cc34d0357ece5ebdd31791a3a374d8/embeddinggemma-2-Q8_0.gguf"
    )
    # the llama.py pin and the manifest pin are the same bytes (05 §4.3)
    from goldmine.backends.llama import PINNED_MODEL_SHA256

    assert embed.sha256 == PINNED_MODEL_SHA256
    assert embed.dim == 768
    assert DEFAULT_EMBEDDING_KEY == "embeddinggemma-2-Q8_0"


def test_manifest_vad_bytes_pin_is_the_corrected_value():
    """B7: the VAD pin is 885,098 bytes — the first draft's 885,000 typo class
    is exactly what the manifest-vs-file test exists to catch."""
    vad = by_key("silero-vad-v5.1.2")
    assert vad.bytes == 885_098
    assert len(vad.sha256) == 64


def test_manifest_matches_files_on_disk():
    """B7 (normative): every manifest entry's bytes AND sha256 against the
    real file, whenever the repo models are present (skipped on lean clones)."""
    repo_models = Path(__file__).resolve().parents[1] / "models"
    present = [
        e for e in MANIFEST
        if (repo_models / repo_relpath(e)).is_file()
    ]
    if not present:
        pytest.skip("no repo model files present (lean clone) — B7 ran nothing")
    for entry in present:
        path = repo_models / repo_relpath(entry)
        data = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            while block := f.read(1 << 22):
                data.update(block)
                size += len(block)
        assert size == entry.bytes, f"{entry.key}: bytes {size} != pin {entry.bytes}"
        assert data.hexdigest() == entry.sha256, f"{entry.key}: sha256 mismatch"


# -------------------------------------------------------------- resolution


def test_resolution_order_state_over_models_dir_over_repo(tmp_home):
    home = tmp_home / "gmhome"
    models_dir = home / "models"
    models_dir.mkdir(parents=True)
    repo = tmp_home / "repo"
    repo.mkdir()
    state_file = tmp_home / "adopted.bin"
    state_file.write_bytes(b"adopted-bytes")
    entry = fake_entry("m1", state_file)

    manager = make_manager(tmp_home, [entry], repo_models=repo)
    # nothing anywhere: missing
    assert manager.resolve("m1") == (None, "missing")

    # repo copy found
    repo_copy = repo / entry.name
    repo_copy.write_bytes(state_file.read_bytes())
    assert manager.resolve("m1") == (repo_copy, "repo")

    # models dir beats repo
    dest = models_dir / entry.name
    dest.write_bytes(state_file.read_bytes())
    assert manager.resolve("m1") == (dest, "models-dir")

    # state override beats everything (adopt --from elsewhere, no copy)
    elsewhere = tmp_home / "elsewhere.bin"
    elsewhere.write_bytes(state_file.read_bytes())
    manager.adopt(elsewhere, "m1")
    assert manager.resolve("m1") == (elsewhere.resolve(), "state")


def test_models_dir_env_override(tmp_home):
    override = tmp_home / "custom-models"
    override.mkdir()
    payload = tmp_home / "p.bin"
    payload.write_bytes(b"0123456789")
    entry = fake_entry("m1", payload)
    cfg = load(config_path=tmp_home / "none.toml", env={
        "GOLDMINE_HOME": str(tmp_home / "gmhome"),
        "GOLDMINE_MODELS": str(override),
    })
    manager = ModelManager(cfg, manifest=[entry],
                           repo_models=tmp_home / "no-repo", err=lambda m: None)
    (override / entry.name).write_bytes(payload.read_bytes())
    assert manager.models_dir() == override
    assert manager.resolve("m1")[1] == "models-dir"


def test_listing_marks_installed_and_missing(tmp_home):
    payload = tmp_home / "p.bin"
    payload.write_bytes(b"0123456789")
    entries = [fake_entry("have-it", payload), fake_entry("absent", payload)]
    manager = make_manager(tmp_home, entries)
    manager.adopt(payload, "have-it")
    rows = {r["key"]: r for r in manager.listing()}
    assert rows["have-it"]["installed"] is True
    assert rows["have-it"]["source"] == "state"
    assert rows["absent"]["installed"] is False
    assert rows["absent"]["source"] is None
    assert rows["have-it"]["bytes"] == 10


# ------------------------------------------------------------------ adopt


def test_adopt_registers_without_copying(tmp_home):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"payload-bytes-here")
    entry = fake_entry("m1", payload)
    manager = make_manager(tmp_home, [entry])
    path = manager.adopt(payload, "m1", alias="daily")
    assert path == payload.resolve()
    state = manager.load_state()
    assert state["models"]["m1"]["path"] == str(payload.resolve())
    assert state["models"]["m1"]["source"] == "adopt"
    assert state["models"]["m1"]["alias"] == "daily"
    # no copy happened anywhere under <home>
    assert not (manager.home / "models" / payload.name).exists()


def test_adopt_refuses_non_pinned_content(tmp_home):
    good = tmp_home / "good.gguf"
    good.write_bytes(b"good")
    entry = fake_entry("m1", good)
    manager = make_manager(tmp_home, [entry])
    bad = tmp_home / "bad.gguf"
    bad.write_bytes(b"totally-different-content")
    with pytest.raises(ModelError) as excinfo:
        manager.adopt(bad, "m1")
    assert "does not match the pinned m1" in str(excinfo.value)
    with pytest.raises(ModelError):
        manager.adopt(tmp_home / "ghost.gguf", "m1")


def test_unknown_key_is_usage_error(tmp_home):
    manager = make_manager(tmp_home, MANIFEST)
    with pytest.raises(ModelError) as excinfo:
        manager.entry("no-such-model")
    assert excinfo.value.usage is True
    assert excinfo.value.fix_hint


# ---------------------------------------------------------------- install


def test_install_from_path_registers_no_download(tmp_home):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"model-content-1234")
    entry = fake_entry("m1", payload)
    manager = make_manager(tmp_home, [entry])
    path = manager.install("m1", from_path=payload, alias="p1")
    assert path == payload.resolve()
    assert manager.resolve("m1")[1] == "state"


def test_install_already_installed_short_circuits(tmp_home):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"model-content-1234")
    entry = fake_entry("m1", payload)
    manager = make_manager(tmp_home, [entry])
    dest = manager.install_dest(entry)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(payload.read_bytes())
    downloads = []
    manager._download = lambda *a, **k: downloads.append(a)  # must not run
    path = manager.install("m1")
    assert path == dest
    assert not downloads
    assert manager.load_state()["models"]["m1"]["path"] == str(dest)


def test_install_download_file_url_verifies_and_renames(tmp_home):
    src = tmp_home / "src.bin"
    src.write_bytes(os.urandom(300_000))
    entry = local_entry("m1", src)
    manager = make_manager(tmp_home, [entry])
    dest = manager.install("m1")
    assert dest == manager.models_dir() / entry.name
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == entry.sha256
    assert not dest.with_name(dest.name + ".partial").exists()
    # sidecar in the backend's format makes llama.py's cache instantly valid
    sidecar = json.loads(dest.with_name(dest.name + ".sha256").read_text())
    assert sidecar["sha256"] == entry.sha256
    assert sidecar["size"] == entry.bytes
    assert manager.load_state()["models"]["m1"]["source"] == "download"


def test_install_download_resumes_from_partial_on_206(tmp_home):
    src_bytes = os.urandom(120)
    src = tmp_home / "src.bin"
    src.write_bytes(src_bytes)
    entry = local_entry("m1", src)
    manager = make_manager(tmp_home, [entry])

    # a .partial already holding the first half; the fake server answers 206
    # with exactly the remaining bytes (the Range contract).
    dest = manager.install_dest(entry)
    partial = dest.with_name(dest.name + ".partial")
    dest.parent.mkdir(parents=True)
    partial.write_bytes(src_bytes[:60])

    class Fake206:
        status = 206

        def __init__(self, payload):
            self._payload = payload
            self._pos = 0

        def read(self, n=-1):
            if self._pos >= len(self._payload):
                return b""
            chunk = self._payload[self._pos:self._pos + max(1, n)]
            self._pos += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    requests: list[object] = []

    def fake_open_url(request, timeout=None):
        requests.append(request)
        assert request.get_header("Range") == "bytes=60-", "Range must resume"
        return Fake206(src_bytes[60:])

    manager._open_url = fake_open_url
    out = manager.install("m1")
    assert out == dest
    assert dest.read_bytes() == src_bytes  # first 60 from .partial + 60 fresh


def test_install_download_restarts_when_server_ignores_range(tmp_home):
    """file:// (and any 200-answering origin) ignores Range: the transfer must
    restart from byte zero, not append a second full copy."""
    src = tmp_home / "src.bin"
    src.write_bytes(b"abcdefgh" * 8)
    entry = local_entry("m1", src)
    manager = make_manager(tmp_home, [entry])
    dest = manager.install_dest(entry)
    dest.parent.mkdir(parents=True)
    stale = dest.with_name(dest.name + ".partial")
    stale.write_bytes(b"stale-garbage-bytes")  # not a prefix of the payload
    out = manager.install("m1")
    assert out == dest
    assert dest.read_bytes() == b"abcdefgh" * 8  # verified: no stale tail


def test_install_retries_with_backoff_then_fails(tmp_home):
    src = tmp_home / "src.bin"
    src.write_bytes(b"x" * 40)
    entry = local_entry("m1", src)
    slept: list[float] = []
    manager = make_manager(tmp_home, [entry], sleep=slept.append)

    calls = {"n": 0}

    def always_failing(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("connection reset")

    manager._open_url = always_failing
    with pytest.raises(ModelError) as excinfo:
        manager.install("m1")
    assert "failed after" in str(excinfo.value)
    assert calls["n"] == 1 + len(model_mod.RETRY_BACKOFF_S)
    assert slept == list(model_mod.RETRY_BACKOFF_S)
    # nothing was ever written: no dest, no .partial
    dest = manager.install_dest(entry)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".partial").exists()


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://fake/uri", code, "Error", None, None)


@pytest.mark.parametrize("code", [404, 410])
def test_install_dead_pin_fails_immediately_no_backoff(tmp_home, code):
    """03 F9: a 404/410 pin will never answer — the manager must fail on the
    FIRST attempt (the old code burned the full 2/5/15/30 s = 52 s backoff),
    naming the manifest entry in the message."""
    src = tmp_home / "src.bin"
    src.write_bytes(b"x" * 40)
    entry = local_entry("m1", src)
    slept: list[float] = []
    manager = make_manager(tmp_home, [entry], sleep=slept.append)

    calls = {"n": 0}

    def dead_pin(request, timeout=None):
        calls["n"] += 1
        raise _http_error(code)

    manager._open_url = dead_pin
    with pytest.raises(ModelError) as excinfo:
        manager.install("m1")
    message = str(excinfo.value)
    assert str(code) in message
    assert entry.name in message       # the manifest file name is in the message
    assert entry.repo in message
    assert calls["n"] == 1             # one attempt, zero retries
    assert slept == []                 # zero backoff slept
    assert not manager.install_dest(entry).exists()


@pytest.mark.parametrize("code", [429, 500, 503])
def test_install_transient_http_errors_still_back_off(tmp_home, code):
    """429/5xx are transient: full backoff, then the honest failure."""
    src = tmp_home / "src.bin"
    src.write_bytes(b"x" * 40)
    entry = local_entry("m1", src)
    slept: list[float] = []
    manager = make_manager(tmp_home, [entry], sleep=slept.append)

    def flaky(request, timeout=None):
        raise _http_error(code)

    manager._open_url = flaky
    with pytest.raises(ModelError) as excinfo:
        manager.install("m1")
    assert "HTTP " + str(code) in str(excinfo.value)
    assert slept == list(model_mod.RETRY_BACKOFF_S)


def test_install_disk_precheck_names_missing_bytes(tmp_home, monkeypatch):
    src = tmp_home / "src.bin"
    src.write_bytes(b"x" * 4096)
    entry = local_entry("m1", src)

    class Tiny:
        free = 1000

    monkeypatch.setattr(
        model_mod.shutil, "disk_usage",
        lambda path: Tiny(),
    )
    manager = make_manager(tmp_home, [entry])
    with pytest.raises(ModelError) as excinfo:
        manager.install("m1")
    message = str(excinfo.value)
    assert "not enough disk space" in message
    # names the missing bytes (comma-formatted) — the actionable number
    assert f"{entry.bytes + DISK_SLACK_BYTES - 1000:,}" in message


def test_install_corrupt_download_fails_verify_no_final_file(tmp_home):
    src = tmp_home / "src.bin"
    src.write_bytes(b"right" * 20)
    entry = local_entry("m1", src)
    manager = make_manager(tmp_home, [entry])

    def lying_server(request, timeout=None):
        # serves DIFFERENT bytes than the pin
        other = tmp_home / "other.bin"
        other.write_bytes(b"wrong" * 20)
        return model_mod.urllib.request.urlopen(other.resolve().as_uri(), timeout=timeout)

    manager._open_url = lying_server
    with pytest.raises(ModelError) as excinfo:
        manager.install("m1")
    assert "failed verification" in str(excinfo.value)
    dest = manager.install_dest(entry)
    assert not dest.exists()  # final name only after verify (DigUp rule)
    assert not dest.with_name(dest.name + ".partial").exists()


# ----------------------------------------------------------------- verify


def test_verify_recomputes_against_pin(tmp_home):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"payload-bytes-here")
    entry = fake_entry("m1", payload)
    manager = make_manager(tmp_home, [entry])
    manager.adopt(payload, "m1")
    (result,) = manager.verify("m1")
    assert result["bytes_ok"] and result["sha_ok"]

    # tamper: same length, different content -> bytes ok, sha fails (B7: both)
    payload.write_bytes(b"PAYLOAD-BYTES-HERE")
    (result,) = manager.verify("m1")
    assert result["bytes_ok"] is True
    assert result["sha_ok"] is False

    # restore, then verify-all: installed + missing entries both reported
    payload.write_bytes(b"payload-bytes-here")
    manager2 = make_manager(
        tmp_home, [entry, fake_entry("absent", payload)]
    )
    manager2.adopt(payload, "m1")
    results = {r["key"]: r for r in manager2.verify()}
    assert results["m1"]["sha_ok"] is True
    assert results["absent"]["error"] == "not installed"


# -------------------------------------------------------------------- use


def test_use_writes_config_model_line(tmp_home, monkeypatch):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"payload-bytes-here")
    entry = fake_entry("m1", payload)  # role embedding, dim 768
    config_file = tmp_home / "config.toml"
    monkeypatch.setenv("GOLDMINE_CONFIG", str(config_file))
    cfg = load(config_path=None, env={
        "GOLDMINE_HOME": str(tmp_home / "gmhome"),
        "GOLDMINE_CONFIG": str(config_file),
    })
    manager = ModelManager(cfg, manifest=[entry],
                           repo_models=tmp_home / "no-repo", err=lambda m: None)
    with pytest.raises(ModelError) as excinfo:
        manager.use("m1")  # not installed yet -> refuses to write
    assert "not installed" in str(excinfo.value)
    assert not config_file.exists()

    manager.adopt(payload, "m1")
    path = manager.use("m1")
    assert path == payload.resolve()
    text = config_file.read_text()
    assert f'model = "{payload.resolve()}"' in text
    assert "[llama]" in text
    # and config.load actually applies it
    cfg2 = load(config_path=config_file, env={"GOLDMINE_HOME": str(tmp_home / "gmhome")})
    assert cfg2.llama.model == payload.resolve()
    assert cfg2.llama.model_configured is True


def test_use_preserves_other_config_content(tmp_home, monkeypatch):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"payload-bytes-here")
    entry = fake_entry("m1", payload)
    config_file = tmp_home / "config.toml"
    config_file.write_text(
        '[backend]\ndefault = "llama"\n\n[llama]\nctx = 1024\nmodel = "/old/path.gguf"\n\n[search]\nlimit = 7\n'
    )
    monkeypatch.setenv("GOLDMINE_CONFIG", str(config_file))
    cfg = load(config_path=None, env={
        "GOLDMINE_HOME": str(tmp_home / "gmhome"),
        "GOLDMINE_CONFIG": str(config_file),
    })
    manager = ModelManager(cfg, manifest=[entry],
                           repo_models=tmp_home / "no-repo", err=lambda m: None)
    manager.adopt(payload, "m1")
    manager.use("m1")
    text = config_file.read_text()
    assert f'model = "{payload.resolve()}"' in text
    assert "/old/path.gguf" not in text
    assert 'default = "llama"' in text   # other sections untouched
    assert "ctx = 1024" in text          # other llama keys untouched
    assert "limit = 7" in text
    assert config_file.with_name(config_file.name + ".bak").exists()
    cfg2 = load(config_path=config_file, env={})
    assert cfg2.llama.ctx == 1024 and cfg2.llama.model == payload.resolve()


def test_use_inserts_into_existing_llama_section_without_model(tmp_home, monkeypatch):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"payload-bytes-here")
    entry = fake_entry("m1", payload)
    config_file = tmp_home / "config.toml"
    config_file.write_text("[llama]\nctx = 512\n")
    monkeypatch.setenv("GOLDMINE_CONFIG", str(config_file))
    cfg = load(config_path=None, env={
        "GOLDMINE_HOME": str(tmp_home / "gmhome"),
        "GOLDMINE_CONFIG": str(config_file),
    })
    manager = ModelManager(cfg, manifest=[entry],
                           repo_models=tmp_home / "no-repo", err=lambda m: None)
    manager.adopt(payload, "m1")
    manager.use("m1")
    cfg2 = load(config_path=config_file, env={})
    assert cfg2.llama.ctx == 512
    assert cfg2.llama.model == payload.resolve()


def test_use_rejects_non_embedding_roles(tmp_home):
    payload = tmp_home / "vad.bin"
    payload.write_bytes(b"vad-bytes")
    entry = fake_entry("vad1", payload, role="vad")
    manager = make_manager(tmp_home, [entry])
    manager.adopt(payload, "vad1")
    with pytest.raises(ModelError) as excinfo:
        manager.use("vad1")
    assert "role 'vad'" in str(excinfo.value)


def test_set_config_llama_model_edge_cases(tmp_path):
    cfg_file = tmp_path / "c.toml"
    # empty/absent file -> creates the section
    set_config_llama_model(cfg_file, "/x/y.gguf")
    assert cfg_file.read_text() == '[llama]\nmodel = "/x/y.gguf"\n'
    # quoted path roundtrip
    cfg_file.write_text("")
    set_config_llama_model(cfg_file, '/weird "path"/m.gguf')
    parsed = load(config_path=cfg_file, env={})
    assert str(parsed.llama.model) == '/weird "path"/m.gguf'
    # a commented-out model line is NOT replaced
    cfg_file.write_text("[llama]\n# model = \"/old.gguf\"\n")
    set_config_llama_model(cfg_file, "/new.gguf")
    assert "# model = " in cfg_file.read_text()
    assert 'model = "/new.gguf"' in cfg_file.read_text()


# ------------------------------------------------------------- llama resolve


def test_resolve_server_path_home_bin_beats_vendor(tmp_path, monkeypatch):
    import goldmine.backends.llama as llama
    from goldmine.config import LlamaCfg

    vendor = tmp_path / "vendor-llama-server"
    vendor.write_text("#!/bin/sh\n"); vendor.chmod(0o755)
    brew = tmp_path / "brew-llama-server"
    brew.write_text("#!/bin/sh\n"); brew.chmod(0o755)
    home_bin = tmp_path / "home" / "bin" / "llama-server"
    home_bin.parent.mkdir(parents=True)
    home_bin.write_text("#!/bin/sh\n"); home_bin.chmod(0o755)
    monkeypatch.setattr(llama, "VENDOR_SERVER", vendor)
    monkeypatch.setattr(llama, "BREW_SERVER", brew)

    cfg = LlamaCfg(home=tmp_path / "home")
    resolved, source = llama.resolve_server_path(cfg)
    assert source == "home" and resolved == home_bin

    # without the home copy: vendor (checkout guard passes in this repo)
    home_bin.unlink()
    resolved, source = llama.resolve_server_path(cfg)
    assert source == "vendor" and resolved == vendor
    # and home/bin sits between config and vendor
    assert llama.home_server_path(cfg) == home_bin


def test_resolve_model_path_default_chain_home_before_repo(tmp_path, monkeypatch):
    import goldmine.backends.llama as llama
    from goldmine.config import LlamaCfg

    pinned_name = llama.PINNED_MODEL_BASENAME
    repo_models = tmp_path / "repo-models"
    repo_models.mkdir()
    (repo_models / pinned_name).write_bytes(b"repo copy")
    monkeypatch.setattr(llama, "REPO_MODELS", repo_models)
    # simulate the unconfigured built-in default pointing nowhere (an installed
    # copy has no ~/git/goldmine checkout)
    absent_default = tmp_path / "absent-default.gguf"

    # configured explicit wins even when missing
    cfg = LlamaCfg(
        model=tmp_path / "explicit.gguf", model_configured=True,
        home=tmp_path / "home",
    )
    assert llama.resolve_model_path(cfg) == tmp_path / "explicit.gguf"

    # unconfigured + default missing -> home/models pinned copy wins
    cfg = LlamaCfg(model=absent_default, home=tmp_path / "home")
    home_copy = tmp_path / "home" / "models" / pinned_name
    home_copy.parent.mkdir(parents=True)
    home_copy.write_bytes(b"home copy")
    assert llama.resolve_model_path(cfg) == home_copy

    # home absent -> repo models copy
    home_copy.unlink()
    assert llama.resolve_model_path(cfg) == repo_models / pinned_name

    # both absent -> the configured value itself (error names it later)
    monkeypatch.setattr(llama, "REPO_MODELS", tmp_path / "empty")
    assert llama.resolve_model_path(cfg) == absent_default

    # legacy-default-present leg (dev checkout): the built-in default value,
    # when the file exists, is honored before home/repo (keeps every existing
    # configuration — and the offline test fixtures — working unchanged)
    present_default = tmp_path / "present-default.gguf"
    present_default.write_bytes(b"legacy default copy")
    cfg = LlamaCfg(model=present_default, home=tmp_path / "home")
    assert llama.resolve_model_path(cfg) == present_default


# ------------------------------------------------------------------- CLI


def _run_cli(monkeypatch, tmp_home, *argv, env=None):
    from goldmine.cli import main

    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    return main(list(argv))


def test_cli_model_list_json(tmp_home, capsys):
    from goldmine.cli import main

    rc = main(["model", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    keys = {row["key"] for row in payload["models"]}
    assert keys == {e.key for e in MANIFEST}
    for row in payload["models"]:
        assert set(row) >= {
            "key", "role", "repo", "revision", "name", "bytes", "sha256",
            "installed", "path", "source",
        }


def test_cli_model_unknown_key_exit_2(tmp_home, capsys):
    from goldmine.cli import main

    rc = main(["model", "install", "not-a-model"])
    assert rc == 2
    err = capsys.readouterr().err
    # v2 item 17: the universal error shape replaced v1's "hint:" line
    assert "unknown model" in err and "fix: " in err


def test_cli_models_hidden_alias_add_maps_to_install(tmp_home, capsys, monkeypatch):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"payload-bytes-here")
    monkeypatch.setattr(model_mod, "MANIFEST", [fake_entry("m1", payload)])
    from goldmine.cli import main

    rc = main(["models", "add", "m1", "--from", str(payload)])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(payload.resolve()) in out
    # registry recorded it under <home>
    state = json.loads(
        (tmp_home / "gmhome" / "models.json").read_text()
    )
    assert state["models"]["m1"]["path"] == str(payload.resolve())


def test_cli_model_install_from_and_use(tmp_home, capsys, monkeypatch):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"payload-bytes-here")
    entry = fake_entry("m1", payload)
    monkeypatch.setattr(model_mod, "MANIFEST", [entry])
    config_file = tmp_home / "config.toml"
    monkeypatch.setenv("GOLDMINE_CONFIG", str(config_file))
    from goldmine.cli import main

    assert main(["model", "install", "m1", "--from", str(payload)]) == 0
    rc = main(["model", "use", "m1"])
    assert rc == 0
    assert f'model = "{payload.resolve()}"' in config_file.read_text()
    # verify through the CLI: exit 0 + table row
    rc = main(["model", "verify", "m1"])
    assert rc == 0
    assert "sha ok" in capsys.readouterr().out


def test_cli_model_use_not_installed_exit_1(tmp_home, capsys, monkeypatch):
    payload = tmp_home / "p.gguf"
    payload.write_bytes(b"payload-bytes-here")
    monkeypatch.setattr(model_mod, "MANIFEST", [fake_entry("m1", payload)])
    from goldmine.cli import main

    rc = main(["model", "use", "m1"])
    assert rc == 1
    assert "not installed" in capsys.readouterr().err


def test_cli_model_check_with_fake_server(tmp_home, capsys, monkeypatch):
    """Offline live-probe shape check: fake llama-server + unpinned model name
    (no manifest sha enforcement), expect green + dim 768."""
    FAKE_SERVER.chmod(
        FAKE_SERVER.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    model = tmp_home / "fake-embedding-model.gguf"
    model.write_bytes(b"GGUF" + b"\x00" * 1024)
    monkeypatch.setenv("GOLDMINE_LLAMA_SERVER", str(FAKE_SERVER))
    monkeypatch.setenv("GOLDMINE_MODEL", str(model))
    from goldmine.cli import main

    rc = main(["model", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    findings = payload["check"]
    assert findings["ok"] is True
    assert findings["dim"] == 768
    assert findings["server_source"] == "env/config"
    assert "dim 768" in findings["live_embed"]
    assert findings["brew_trap"] is None


def test_cli_model_check_bad_server_exit_4(tmp_home, capsys, monkeypatch):
    crash = tmp_home / "crash-server"
    crash.write_text("#!/bin/sh\necho 'arch gemma-embedding2 unknown' >&2\nexit 1\n")
    crash.chmod(0o755)
    model = tmp_home / "fake-embedding-model.gguf"
    model.write_bytes(b"GGUF")
    monkeypatch.setenv("GOLDMINE_LLAMA_SERVER", str(crash))
    monkeypatch.setenv("GOLDMINE_MODEL", str(model))
    from goldmine.cli import main

    rc = main(["model", "check"])
    assert rc == 4
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "failed" in out  # live embed failure surfaced


def test_cli_version_full_json(tmp_home, capsys):
    from goldmine.cli import main

    rc = main(["version", "--json"])
    assert rc == 0
    basic = json.loads(capsys.readouterr().out)
    assert basic["goldmine"] == "0.2.0"
    assert basic["python"] and basic["platform"]

    rc = main(["version", "--full", "--json"])
    assert rc == 0
    full = json.loads(capsys.readouterr().out)
    assert set(full) >= {
        "goldmine", "git", "llama_server", "model", "schema_version_supported",
        "backends", "home",
    }
    assert set(full["llama_server"]) == {"path", "source", "build"}
    assert isinstance(full["schema_version_supported"], int)
    assert set(full["backends"]) >= {"llama", "mock"}
    # model sha comes from the sidecar (no recompute), source labeled
    if full["model"]["sha256"]:
        assert full["model"]["sha256_source"] == "sidecar"


def test_cli_version_full_human(tmp_home, capsys):
    from goldmine.cli import main

    rc = main(["version", "--full"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith(f"goldmine 0.2.0")
    for marker in ("git:", "llama-server:", "model:", "schema:", "backends:", "home:"):
        assert marker in out


def test_models_alias_is_argv_rewrite_and_hidden():
    from goldmine.cli import _rewrite_models_alias, build_parser

    assert _rewrite_models_alias(["models", "list"]) == ["model", "list"]
    assert _rewrite_models_alias(["models", "add", "x"]) == ["model", "install", "x"]
    assert _rewrite_models_alias(
        ["--json", "--db", "/x.sqlite", "models", "verify", "y"]
    ) == ["--json", "--db", "/x.sqlite", "model", "verify", "y"]
    # --db consumes its value: "models" as a VALUE is never rewritten
    assert _rewrite_models_alias(["--db", "models", "search", "q"]) == [
        "--db", "models", "search", "q",
    ]
    assert _rewrite_models_alias(["search", "models"]) == ["search", "models"]
    # and the alias never registers a parser (truly hidden)
    help_text = build_parser().format_help()
    assert "models" not in {
        line.split()[0] for line in help_text.splitlines() if line.startswith("    ")
    }
