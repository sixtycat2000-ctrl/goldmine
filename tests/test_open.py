"""v2 item 18 (F2'): `goldmine open` — plan/execute opener with a uniform
degrade chain. Everything runs monkeypatched: no GUI app, no osascript, no
mdls is ever spawned (headless-safe)."""

from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from goldmine.cli import main
from goldmine.opener import (
    QUICKTIME_SCRIPT,
    OpenPlan,
    clock_fmt,
    kind_for_path,
    plan_open,
    run_plan,
)

# ------------------------------------------------------------------- helpers


@pytest.fixture
def db_path(tmp_home):
    return tmp_home / "gmhome" / "default.sqlite"


@pytest.fixture(autouse=True)
def _clean_editor_env(monkeypatch):
    monkeypatch.delenv("EDITOR", raising=False)


def patch_default_app(monkeypatch, bundle):
    import goldmine.opener as op

    monkeypatch.setattr(op, "default_app_bundle", lambda path: bundle)


class FakeProc:
    def __init__(self, returncode=0, stderr=b"", stdout=b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.fixture
def spawn_log(monkeypatch):
    """Record every subprocess.run call made through goldmine.opener."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv and argv[0] == "/usr/bin/osascript":
            return FakeProc(returncode=0, stderr=b"")  # quicktime happy path
        return FakeProc(returncode=0)

    monkeypatch.setattr("goldmine.opener.subprocess.run", fake_run)
    return calls


# --------------------------------------------------------------- kind / clock


def test_kind_for_path_extension_map():
    assert kind_for_path("/x/a.pdf") == "pdf"
    assert kind_for_path("/x/a.PDF") == "pdf"
    assert kind_for_path("/x/a.png") == "image"
    assert kind_for_path("/x/a.m4a") == "audio"
    assert kind_for_path("/x/a.mov") == "video"
    assert kind_for_path("/x/a.py") == "code"
    assert kind_for_path("/x/a.txt") == "doc"
    assert kind_for_path("/x/a.unknownext") == "doc"  # honest default: plain open


def test_clock_fmt():
    assert clock_fmt(6.2) == "0:06"
    assert clock_fmt(66.0) == "1:06"
    assert clock_fmt(3661.0) == "1:01:01"
    assert clock_fmt(0) == "0:00"
    assert clock_fmt(-5) == "0:00"


# ------------------------------------------------------------- plan: jumps


def test_plan_quicktime_jump_and_ms_math(monkeypatch):
    patch_default_app(monkeypatch, "com.apple.QuickTimePlayerX")
    plan = plan_open("/m/rec.mov", "audio", 6.2)
    assert plan.how == "quicktime" and plan.jumped
    assert plan.argv[0] == "/usr/bin/osascript"
    assert plan.argv[1] == "-e"
    assert plan.argv[2] == QUICKTIME_SCRIPT
    assert "set current time of theDocument to theTime" in QUICKTIME_SCRIPT
    assert plan.argv[3] == "/m/rec.mov"
    assert plan.argv[4] == "6200"  # 6.2 s -> whole milliseconds (locale-safe)
    assert plan.result_line() == (
        "opened /m/rec.mov (audio) in QuickTime Player at 0:06"
    )


def test_plan_iina_url_scheme(monkeypatch):
    patch_default_app(monkeypatch, "com.colliderli.iina")
    plan = plan_open("/m/rec with space.mov", "video", 6.25)
    assert plan.how == "iina" and plan.jumped
    assert plan.argv[0] == "open"
    assert plan.argv[1] == (
        "iina://open?url=file:///m/rec%20with%20space.mov&mpv_start=6.2"
    )


@pytest.mark.parametrize("bundle", ["org.videolan.vlc", None])
def test_plan_unknown_player_falls_back_to_open_with_instruction(
    monkeypatch, bundle
):
    patch_default_app(monkeypatch, bundle)
    plan = plan_open("/m/rec.mov", "audio", 66.0)
    assert plan.how == "open" and not plan.jumped
    assert plan.argv == ["open", "/m/rec.mov"]
    assert plan.result_line() == (
        "opened /m/rec.mov (audio) at start — jump to 1:06"
    )


def test_plan_audio_without_loc_is_plain_open(monkeypatch):
    patch_default_app(monkeypatch, "com.apple.QuickTimePlayerX")
    plan = plan_open("/m/rec.mov", "audio", None)  # never crashes on missing loc
    assert plan.how == "open" and not plan.jumped
    assert plan.note == ""


def test_plan_pdf_loc_states_page_jump_honestly():
    plan = plan_open("/d/p.pdf", "pdf", 4)
    assert plan.how == "open"
    assert plan.result_line() == (
        "opened /d/p.pdf (pdf) — opened at file start (page jump not supported)"
    )


def test_plan_editor_line_hint(monkeypatch):
    monkeypatch.setenv("EDITOR", "vim")
    plan = plan_open("/c/a.py", "code", 120)
    assert plan.how == "editor" and plan.jumped
    assert plan.argv == ["vim", "+120", "/c/a.py"]
    assert plan.result_line() == "opened /c/a.py (code) in vim at line 120"

    # an editor not in the +LINE set gets no line hint, stated plainly
    monkeypatch.setenv("EDITOR", "code")
    plan = plan_open("/c/a.py", "code", 120)
    assert plan.argv == ["code", "/c/a.py"] and not plan.jumped

    # $EDITOR default is vi (basename of a path'd editor still matches)
    monkeypatch.setenv("EDITOR", "/usr/bin/vim")
    plan = plan_open("/c/a.py", "code", 3)
    assert plan.argv == ["/usr/bin/vim", "+3", "/c/a.py"]


def test_plan_code_defaults_to_vi_when_editor_unset():
    plan = plan_open("/c/a.py", "code", None)
    assert plan.argv == ["vi", "/c/a.py"]


def test_plan_doc_uses_editor_only_when_set():
    plan = plan_open("/n/notes.md", "doc", None)  # EDITOR unset
    assert plan.how == "open"

    import os

    os.environ["EDITOR"] = "nano"
    plan = plan_open("/n/notes.md", "doc", 7)
    assert plan.argv == ["nano", "+7", "/n/notes.md"]


# ------------------------------------------------------- run_plan: degrade chain


def test_run_plan_quicktime_success(spawn_log, monkeypatch):
    patch_default_app(monkeypatch, "com.apple.QuickTimePlayerX")
    plan = plan_open("/m/rec.mov", "audio", 6.0)
    out = run_plan(plan)
    assert out.how == "quicktime"
    argv, kwargs = spawn_log[0]
    assert argv[0] == "/usr/bin/osascript" and argv[-1] == "6000"
    assert kwargs.get("timeout")  # -1712 must not wedge the CLI


def test_run_plan_quicktime_not_allowed_degrades(monkeypatch, capsys):
    patch_default_app(monkeypatch, "com.apple.QuickTimePlayerX")

    def refuse(argv, **kwargs):
        if argv[0] == "/usr/bin/osascript":
            return FakeProc(returncode=1, stderr=b"execution error: (-1743)")
        return FakeProc(returncode=0)

    monkeypatch.setattr("goldmine.opener.subprocess.run", refuse)
    plan = plan_open("/m/rec.mov", "audio", 6.0)
    out = run_plan(plan)
    assert out.how == "open" and out.argv == ["open", "/m/rec.mov"]
    err = capsys.readouterr().err
    assert "-1743" in err and "opened at start" in err  # honesty line, exit 0
    assert out.result_line() == "opened /m/rec.mov (audio) at start — jump to 0:06"


def test_run_plan_editor_missing_degrades(monkeypatch, capsys):
    def missing_editor(argv, **kwargs):
        if argv[0] == "vi":
            raise FileNotFoundError("vi")
        return FakeProc(returncode=0)

    monkeypatch.setattr("goldmine.opener.subprocess.run", missing_editor)
    plan = plan_open("/c/a.py", "code", 1)
    out = run_plan(plan)
    assert out.how == "open"
    assert "vi: not found" in capsys.readouterr().err


def test_run_plan_dry_run_spawns_nothing(monkeypatch, capsys):
    patch_default_app(monkeypatch, None)  # no mdls either

    def explode(argv, **kwargs):
        raise AssertionError("dry-run must not spawn: " + shlex.join(argv))

    monkeypatch.setattr("goldmine.opener.subprocess.run", explode)
    plan = plan_open("/m/rec.mov", "audio", 6.0)
    out = run_plan(plan, dry_run=True)
    assert out.argv  # plan is intact for printing
    assert capsys.readouterr().err == ""


def test_run_plan_osascript_timeout_degrades(monkeypatch, capsys):
    patch_default_app(monkeypatch, "com.apple.QuickTimePlayerX")

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv[0], timeout=15)

    monkeypatch.setattr("goldmine.opener.subprocess.run", slow)
    plan = plan_open("/m/rec.mov", "audio", 6.0)
    out = run_plan(plan)
    assert out.how == "open"
    assert "timed out" in capsys.readouterr().err


# ------------------------------------------------------------- cmd_open e2e


def test_cmd_open_path_dry_run_json(tmp_home, tmp_path, capsys):
    f = tmp_path / "clip.m4a"
    f.write_bytes(b"id3")
    rc = main(["open", "--path", str(f), "--loc", "6.2", "--dry-run", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "goldmine/1"
    assert payload["command"] == "open"
    assert payload["kind"] == "audio"
    assert payload["loc"] == 6.2
    assert payload["dry_run"] is True
    # tmp files are not Spotlight-indexed: mdls says nothing -> plain open
    assert payload["how"] == "open"


def test_cmd_open_hit_re_runs_search(tmp_home, db_path, fixture_tree, capsys):
    assert main(["--backend", "mock", "--db", str(db_path),
                 "ingest", str(fixture_tree), "--quiet"]) == 0
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path),
               "open", "--hit", "1", "--query", "tolerations", "--dry-run"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "notes.txt" in captured.out  # opened <path> (doc) …
    assert "would run: open " in captured.err


def test_cmd_open_hit_out_of_range_shape(tmp_home, db_path, fixture_tree, capsys):
    assert main(["--backend", "mock", "--db", str(db_path),
                 "ingest", str(fixture_tree), "--quiet"]) == 0
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path),
               "open", "--hit", "99", "--query", "tolerations"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "why: " in err and "fix: " in err
    assert "no hit 99" in err


def test_cmd_open_hit_needs_query(tmp_home, capsys):
    rc = main(["open", "--hit", "1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "go together" in err and "fix: " in err


def test_cmd_open_missing_path_error_shape(tmp_home, capsys):
    rc = main(["open", "--path", "/definitely/not/here.txt"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no such file" in err
    assert "fix: goldmine search <terms>" in err


def test_cmd_open_nothing_given(tmp_home, capsys):
    rc = main(["open"])
    assert rc == 2
    assert "nothing to open" in capsys.readouterr().err


def test_cmd_open_hit_lexical_degrade_note(
    tmp_home, db_path, fixture_tree, capsys, monkeypatch
):
    from goldmine.backends import BackendUnavailable

    def refuse(cfg, name=None):
        raise BackendUnavailable("no backend here")

    monkeypatch.setattr("goldmine.cli._make_backend", refuse)
    assert main(["--backend", "mock", "--db", str(db_path),
                 "ingest", str(fixture_tree), "--lexical-only", "--quiet"]) == 0
    capsys.readouterr()
    rc = main(["--db", str(db_path),
               "open", "--hit", "1", "--query", "tolerations", "--dry-run"])
    assert rc == 0
    assert "ranked lexically" in capsys.readouterr().err
