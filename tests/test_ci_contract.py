"""CI must preserve strict failures and refuse unprepared live execution."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_validation


def test_live_ci_needs_existing_inputs_and_creates_no_green_report(tmp_path, capsys):
    out = tmp_path / "ci"
    assert ci_validation.main(["--profile", "live", "--out", str(out)]) == 2
    assert "MISSING" in capsys.readouterr().err
    assert not (out / "manifest.json").exists()


@pytest.mark.parametrize(("status", "code"), [("FAIL", 1), ("MISSING", 2)])
def test_ci_does_not_coerce_failed_or_incomplete_checks_to_zero(tmp_path, monkeypatch, status, code):
    # Hosted runners strip GOLDMINE_* from the pytest subprocess while leaving
    # GITHUB_ACTIONS set; the nested call must not divert into hosted validation.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(ci_validation, "run", lambda *args, **kw: {"status": status})
    monkeypatch.setattr(ci_validation, "validate", lambda *args: {})
    assert ci_validation.main(["--profile", "base", "--out", str(tmp_path / "ci")]) == code


def test_ci_revalidates_raw_report_before_claiming_success(tmp_path, monkeypatch):
    from acceptance_lib.evidence import InvalidEvidence
    monkeypatch.setattr(ci_validation, "run", lambda *args, **kw: {"status": "PASS"})
    def invalid(*args):
        raise InvalidEvidence("missing raw JUnit")
    monkeypatch.setattr(ci_validation, "validate", invalid)
    assert ci_validation.main(["--profile", "base", "--out", str(tmp_path / "ci")]) == 2
