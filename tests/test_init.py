"""v2 item 20 (F4): the `goldmine init` state machine — every branch with
monkeypatched stdin, --yes, resume-after-interrupt, the scope invariant,
TCC pre-warning, and the 100 GB-class guard. Fully offline: `--backend mock`
drives ingest; ModelManager / scan_root_media are monkeypatched per test."""

from __future__ import annotations

import builtins
import json
import tomllib
from pathlib import Path

import pytest

from goldmine import init as init_mod
from goldmine.backends.mock import MockBackend
from goldmine.cli import main
from goldmine.store import Store

STATE = "init_state.json"


# ------------------------------------------------------------------ helpers


def make_corpus(tmp_path: Path) -> Path:
    docs = tmp_path / "corpus"
    docs.mkdir()
    (docs / "k8s.md").write_text(
        "kubernetes pods evict nodes under pressure quickly today\n",
        encoding="utf-8",
    )
    (docs / "sched.txt").write_text(
        "the scheduler toleration keeps kubernetes pods alive\n"
        "more clusters and nodes text here\n",
        encoding="utf-8",
    )
    return docs


def config_file(tmp_home: Path) -> Path:
    # F12: with GOLDMINE_HOME set (the tmp_home fixture always sets it), init
    # derives its config path from that same home — never the XDG default.
    return tmp_home / "gmhome" / "config.toml"


def config_roots(tmp_home: Path) -> list[str]:
    data = tomllib.loads(config_file(tmp_home).read_text(encoding="utf-8"))
    return list(data.get("init", {}).get("roots", []))


def answer_with(answers: list[str]):
    """Monkeypatch builtins.input with a scripted queue; exhaustion = EOF
    (init treats it like Ctrl-C: save + exit 130)."""
    it = iter(answers)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError("no more scripted answers") from None

    return fake_input


@pytest.fixture
def input_queue(monkeypatch):
    def _set(answers: list[str]):
        monkeypatch.setattr(builtins, "input", answer_with(answers))

    return _set


# ------------------------------------------------------------------ the paths


def test_yes_with_root_ingests_demos_and_records_roots(tmp_home, tmp_path, capsys):
    docs = make_corpus(tmp_path)
    rc = main(["--backend", "mock", "init", "--yes", "--root", str(docs)])
    assert rc == 0
    out = capsys.readouterr().out
    assert init_mod.INVARIANT in out
    assert "step 6/8" in out and "ingest:" in out
    assert 'search "' in out  # the demo ran
    # the scope invariant record: config holds exactly the user-given root
    assert config_roots(tmp_home) == [str(docs.resolve())]
    # the index is real and vectorized
    db = tmp_home / "gmhome" / "default.sqlite"
    assert db.exists()
    store = Store(db)
    try:
        assert store.counts().vectors >= 2
    finally:
        store.close()
    # a finished run wipes the state file
    assert not (tmp_home / "gmhome" / STATE).exists()


def test_status_shows_indexed_roots(tmp_home, tmp_path, capsys):
    docs = make_corpus(tmp_path)
    main(["--backend", "mock", "init", "--yes", "--root", str(docs)])
    capsys.readouterr()
    rc = main(["--backend", "mock", "--json", "status"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["roots"] == [str(docs.resolve())]
    rc = main(["--backend", "mock", "status"])
    assert rc == 0
    assert f"roots: {docs.resolve()}" in capsys.readouterr().out


def test_yes_without_root_creates_dedicated_folder_only(tmp_home, tmp_path, capsys):
    # an existing, unchosen folder with juicy files — must never be swept
    documents = tmp_home / "home" / "Documents"
    documents.mkdir(parents=True)
    (documents / "do-not-index.txt").write_text(
        "secret sweeper bait " * 5, encoding="utf-8"
    )
    rc = main(["--backend", "mock", "init", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    dedicated = documents / "goldmine"
    assert dedicated.is_dir()
    assert config_roots(tmp_home) == [str(dedicated)]
    assert "drop files in and run" in out  # empty dedicated folder -> honest skip
    assert "demo skipped" in out
    # the bait file is NOT in the index
    store = Store(tmp_home / "gmhome" / "default.sqlite")
    try:
        assert store.counts().files == 0
    finally:
        store.close()


def test_interactive_folders_b_branch_and_tcc_prewarning(tmp_home, tmp_path, capsys, input_queue):
    docs = make_corpus(tmp_path)
    library_private = tmp_home / "home" / "Library" / "private"
    library_private.mkdir(parents=True)
    input_queue(["b", str(library_private), str(docs), "", "n"])
    rc = main(["--backend", "mock", "init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Full Disk Access" in out  # the TCC pre-warning for ~/Library
    assert config_roots(tmp_home) == [
        str(library_private.resolve()),
        str(docs.resolve()),
    ]


def test_interactive_rejects_missing_folder(tmp_home, tmp_path, capsys, input_queue):
    docs = make_corpus(tmp_path)
    input_queue(["b", "/no/such/dir", str(docs), "", "n"])
    rc = main(["--backend", "mock", "init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no such folder" in out
    assert config_roots(tmp_home) == [str(docs.resolve())]


def test_resume_after_interrupt_at_folders(tmp_home, tmp_path, capsys, input_queue):
    docs = make_corpus(tmp_path)
    # states 1-3 complete, then the folder prompts run dry (EOF = interrupt)
    input_queue(["b", str(docs)])
    rc = main(["--backend", "mock", "init"])
    assert rc == 130
    state = json.loads((tmp_home / "gmhome" / STATE).read_text())
    assert state["step"] == 4
    assert state["choices"]["model"] == "mock"  # completed steps persist
    capsys.readouterr()
    # re-run resumes at state 4 without redoing env/model
    input_queue(["b", str(docs), "", "n"])
    rc = main(["--backend", "mock", "init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "resuming init at step 4/8 (folders)" in out
    assert out.count("step 1/8") == 0
    assert "step 7/8" in out
    assert config_roots(tmp_home) == [str(docs.resolve())]


def test_restart_discards_saved_progress(tmp_home, tmp_path, capsys):
    docs = make_corpus(tmp_path)
    gmhome = tmp_home / "gmhome"
    gmhome.mkdir(parents=True)
    (gmhome / STATE).write_text(
        json.dumps({"step": 6, "choices": {"roots": ["/bogus"]}}), encoding="utf-8"
    )
    rc = main(["--backend", "mock", "init", "--restart", "--yes", "--root", str(docs)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "resuming" not in out
    assert config_roots(tmp_home) == [str(docs.resolve())]


def test_env_failure_yes_exits_4(tmp_home, capsys, monkeypatch):
    from goldmine import doctor

    monkeypatch.setattr(
        doctor,
        "check_python",
        lambda: doctor.CheckResult(
            "python", "fail", "3.11 < 3.12", "pyenv install 3.12"
        ),
    )
    rc = main(["--backend", "mock", "init", "--yes"])
    assert rc == 4
    captured = capsys.readouterr()
    assert "environment checks failed" in captured.err
    assert "pyenv install 3.12" in captured.err


def test_env_failure_interactive_recheck_then_skip(tmp_home, tmp_path, capsys, input_queue, monkeypatch):
    from goldmine import doctor

    calls = {"n": 0}

    def flaky_numpy():
        calls["n"] += 1
        if calls["n"] < 2:
            return doctor.CheckResult("numpy", "fail", "not importable", "pip install numpy")
        return doctor.CheckResult("numpy", "ok", "1.26.0")

    monkeypatch.setattr(doctor, "check_numpy", flaky_numpy)
    input_queue(["", "b", str(make_corpus(tmp_path)), "", "n"])
    rc = main(["--backend", "mock", "init"])
    assert rc == 0
    assert "pip install numpy" in capsys.readouterr().out


def test_model_skip_lexical_interactive(tmp_home, tmp_path, capsys, input_queue, monkeypatch):
    docs = make_corpus(tmp_path)
    # a real (llama) backend selection, but the model is nowhere on disk —
    # hermetic: repo models/ exists on this box, so resolve() is patched
    monkeypatch.setattr(
        "goldmine.model.ModelManager.resolve", lambda self, key: (None, "missing")
    )
    input_queue(["s", "b", str(docs), "", "n"])
    rc = main(["--backend", "llama", "init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "keyword-only" in out
    assert "no vectors in the index yet — demo skipped" in out
    store = Store(tmp_home / "gmhome" / "default.sqlite")
    try:
        assert store.counts().vectors == 0
        assert store.counts().files == 2
    finally:
        store.close()


def test_model_adopt_via_model_path_flag(tmp_home, tmp_path, capsys, input_queue, monkeypatch):
    docs = make_corpus(tmp_path)
    adopted = Path("/fake/model.gguf")

    def fake_adopt(self, path, key, alias=None):
        assert path == Path("/fake/x.gguf")
        return adopted

    monkeypatch.setattr("goldmine.model.ModelManager.adopt", fake_adopt)
    monkeypatch.setattr(
        "goldmine.model.ModelManager.check",
        lambda self: {"ok": True, "live_embed": "ok (embedded 'ping' -> dim 768)",
                      "dim": 768},
    )
    monkeypatch.setattr(
        "goldmine.backends.create_backend", lambda cfg, name=None: MockBackend(dim=768)
    )
    input_queue(["b", str(docs), "", "n"])
    rc = main(["--backend", "llama", "init", "--model-path", "/fake/x.gguf"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"adopted {adopted}" in out
    assert "live embed ok" in out
    store = Store(tmp_home / "gmhome" / "default.sqlite")
    try:
        assert store.counts().vectors >= 2
    finally:
        store.close()


def test_probe_failure_is_nonfatal_warning(tmp_home, tmp_path, capsys, input_queue, monkeypatch):
    docs = make_corpus(tmp_path)
    monkeypatch.setattr(
        "goldmine.model.ModelManager.resolve",
        lambda self, key: (Path("/fake/model.gguf"), "state"),
    )
    monkeypatch.setattr(
        "goldmine.model.ModelManager.check",
        lambda self: {
            "ok": False,
            "live_embed": "failed: unsupported arch",
            "brew_trap": "use the release asset",
        },
    )
    monkeypatch.setattr(
        "goldmine.backends.create_backend", lambda cfg, name=None: MockBackend(dim=768)
    )
    input_queue(["b", str(docs), "", "n"])
    rc = main(["--backend", "llama", "init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "warn: failed: unsupported arch" in out
    assert "non-fatal" in out


def test_media_flag_installs_and_sets_config(tmp_home, tmp_path, capsys, monkeypatch):
    docs = make_corpus(tmp_path)

    class FakeManager:
        def __init__(self, cfg, err=None, **kw):
            pass

        def resolve(self, key):
            return (None, "missing")

        def install(self, key):
            return Path("/fake") / key

    monkeypatch.setattr("goldmine.model.ModelManager", FakeManager)
    monkeypatch.setattr(
        "goldmine.ocr.ensure_helper",
        lambda cfg: (Path("/fake/bin/goldmine-ocr"), "compiled on demand"),
    )
    rc = main(["--backend", "mock", "init", "--yes", "--media", "--root", str(docs)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "speech transcription on" in out
    assert "OCR helper ready" in out
    data = tomllib.loads(config_file(tmp_home).read_text(encoding="utf-8"))
    assert data["ingest"]["asr"] == "auto"
    assert data["ocr"]["enabled"] is True
    assert config_roots(tmp_home) == [str(docs.resolve())]


def test_media_decline_states_the_consequence(
    tmp_home, tmp_path, capsys, input_queue, monkeypatch
):
    docs = make_corpus(tmp_path)
    # pin the no-whisper branch (this machine resolves whisper; the honest
    # skip note for that case is test_media_skip_is_honest_when_whisper_resolves_anyway)
    monkeypatch.setattr("goldmine.init._asr_ready", lambda cfg: False)
    input_queue(["b", str(docs), "", "n"])
    rc = main(["--backend", "mock", "init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "audio/screenshots stay metadata-only" in out
    data = tomllib.loads(config_file(tmp_home).read_text(encoding="utf-8"))
    assert "asr" not in data.get("ingest", {})
    assert "enabled" not in data.get("ocr", {})


def test_media_skip_is_honest_when_whisper_resolves_anyway(
    tmp_home, tmp_path, capsys, input_queue, monkeypatch
):
    """F7: a skipped media install must NOT claim 'audio/screenshots stay
    metadata-only' when whisper already resolves — asr defaults to auto, so
    transcription WILL run. The copy says so, names the off switch, and the
    final summary matches the effective state."""
    docs = make_corpus(tmp_path)
    monkeypatch.setattr("goldmine.init._asr_ready", lambda cfg: True)
    input_queue(["b", str(docs), "", "n"])
    rc = main(["--backend", "mock", "init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "media install skipped" in out
    assert "speech transcription will still run on audio files" in out
    assert "config ingest.asr=off" in out
    assert "audio/screenshots stay metadata-only" not in out
    # the summary line matches the effective state: transcription IS on
    assert "speech transcription on (whisper)" in out


def test_media_yes_skip_honest_note(tmp_home, tmp_path, capsys, monkeypatch):
    """F7, --yes without --media: the same honesty note applies."""
    docs = make_corpus(tmp_path)
    monkeypatch.setattr("goldmine.init._asr_ready", lambda cfg: True)
    rc = main(["--backend", "mock", "init", "--yes", "--root", str(docs)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped (--yes stays lean)" in out
    assert "speech transcription will still run on audio files" in out


def test_media_skip_keeps_the_metadata_only_copy_without_whisper(
    tmp_home, tmp_path, capsys, input_queue, monkeypatch
):
    """F7 control: without whisper the old copy is accurate and stays."""
    docs = make_corpus(tmp_path)
    monkeypatch.setattr("goldmine.init._asr_ready", lambda cfg: False)
    input_queue(["b", str(docs), "", "n"])
    rc = main(["--backend", "mock", "init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "audio/screenshots stay metadata-only" in out
    assert "media install skipped" not in out


# ------------------------------------------------------------------- guards


def test_100gb_guard_refuses_yes_without_confirmation(tmp_home, tmp_path, capsys, monkeypatch):
    from goldmine.output import RootMedia

    docs = make_corpus(tmp_path)
    monkeypatch.setattr(
        "goldmine.output.scan_root_media",
        lambda roots, **kw: [RootMedia(root=str(roots[0]), files=150_000)],
    )
    rc = main(["--backend", "mock", "init", "--yes", "--root", str(docs)])
    assert rc == 4
    err = capsys.readouterr().err
    assert "100 GB-class" in err
    assert "without confirmation" in err


def test_100gb_guard_interactive_confirm_accepts(tmp_home, tmp_path, capsys, input_queue, monkeypatch):
    from goldmine.output import RootMedia

    docs = make_corpus(tmp_path)
    monkeypatch.setattr(
        "goldmine.output.scan_root_media",
        lambda roots, **kw: [RootMedia(root=str(roots[0]), files=150_000)],
    )
    input_queue(["b", str(docs), "y", "", "n"])
    rc = main(["--backend", "mock", "init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "100 GB-class" in out
    assert config_roots(tmp_home) == [str(docs.resolve())]


def test_big_folder_guard_interactive_decline_keeps_asking(tmp_home, tmp_path, capsys, input_queue, monkeypatch):
    from goldmine.output import RootMedia

    docs = make_corpus(tmp_path)
    big = tmp_path / "big"
    big.mkdir()
    (big / "f.txt").write_text("x", encoding="utf-8")

    def fake_scan(roots, **kw):
        return [
            RootMedia(root=str(r), files=20_000 if str(r).endswith("/big") else 2)
            for r in roots
        ]

    monkeypatch.setattr("goldmine.output.scan_root_media", fake_scan)
    # decline the big folder (guard confirm), then add the small one
    input_queue(["b", str(big), "n", str(docs), "", "n"])
    rc = main(["--backend", "mock", "init", "--skip-demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "20,000 files" in out  # the guard fired for `big`
    assert config_roots(tmp_home) == [str(docs.resolve())]


def test_over_10k_files_warns_but_yes_with_root_proceeds(tmp_home, tmp_path, capsys, monkeypatch):
    from goldmine.output import RootMedia

    docs = make_corpus(tmp_path)
    monkeypatch.setattr(
        "goldmine.output.scan_root_media",
        lambda roots, **kw: [RootMedia(root=str(roots[0]), files=12_345)],
    )
    rc = main(["--backend", "mock", "init", "--yes", "--root", str(docs)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "12,345 files" in out and "a long first ingest" in out
    assert "proceeding with the explicitly given root" in out


# --------------------------------------------------------------- unit pieces


def test_pick_demo_term_prefers_body_words(tmp_home, tmp_path):
    docs = make_corpus(tmp_path)
    rc = main(["--backend", "mock", "init", "--yes", "--root", str(docs), "--skip-demo"])
    assert rc == 0
    store = Store(tmp_home / "gmhome" / "default.sqlite")
    try:
        term = init_mod.pick_demo_term(store)
    finally:
        store.close()
    assert term is not None
    assert term.lower() in {"kubernetes", "pods", "nodes", "text"}


def test_pick_demo_term_empty_index(tmp_home):
    (tmp_home / "gmhome").mkdir(parents=True)
    store = Store(tmp_home / "gmhome" / "default.sqlite")
    try:
        assert init_mod.pick_demo_term(store) is None
    finally:
        store.close()


# ----------------------------------------------------------------- F12: homes


def test_two_goldmine_homes_are_two_independent_inits(
    tmp_home, tmp_path, capsys, monkeypatch, input_queue
):
    """F12: init derives ALL persisted paths from the same home resolution —
    two different GOLDMINE_HOMEs give two independent init states, and nothing
    leaks into the real XDG config."""
    xdg = tmp_home / "home" / ".config" / "goldmine" / "config.toml"

    home_a = tmp_path / "sandbox-a"
    home_b = tmp_path / "sandbox-b"
    docs = make_corpus(tmp_path)

    # home A: a full non-interactive run — config + index land inside A only
    monkeypatch.setenv("GOLDMINE_HOME", str(home_a))
    assert main(["--backend", "mock", "init", "--yes", "--root", str(docs)]) == 0
    capsys.readouterr()
    assert config_roots_from(home_a) == [str(docs.resolve())]
    assert (home_a / "default.sqlite").exists()
    assert not xdg.exists()  # never the real ~/.config (the split-brain bug)

    # home B: an interrupted run — the resume state lands inside B only, and
    # A's persisted files are untouched
    monkeypatch.setenv("GOLDMINE_HOME", str(home_b))
    input_queue([])  # EOF at the folders prompt -> save + exit 130
    assert main(["--backend", "mock", "init"]) == 130
    capsys.readouterr()
    state = json.loads((home_b / "init_state.json").read_text())
    assert state["step"] == 4  # stopped at folders, choices persisted
    assert not (home_b / "config.toml").exists()  # no roots chosen yet
    assert config_roots_from(home_a) == [str(docs.resolve())]  # A independent
    assert not (home_a / "init_state.json").exists()
    assert not xdg.exists()


def config_roots_from(home: Path) -> list[str]:
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    return list(data.get("init", {}).get("roots", []))
