"""Documentation checks are mechanical; quality and live proof remain separate."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import documentation_lib as docs
from acceptance_lib.evidence import InvalidEvidence, artifact, capture_command, sha256, write_json
from acceptance_lib.registry import CHECKS, TASK_TESTS


@pytest.fixture
def docs_root(tmp_path):
    """A tiny synthetic tree for parser counterexamples, not release evidence."""
    root = tmp_path / "synthetic-source"
    root.mkdir()
    for name in docs.DOCUMENTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Fixture documentation\n")
    (root / "README.md").write_text("# Fixture\n```sh\n" + docs.QUICKSTART + "\n```\n")
    (root / "docs/MCP.md").write_text("# Fixture MCP\n```sh\n" + docs.MCP_INSTALL + "\n```\n")
    for name, text in (("pyproject.toml", '[project]\nversion="0.2.0"\n'),
                       ("goldmine/__init__.py", '__version__="0.2.0"\n'), ("LICENSE", "Fixture license\n"),
                       ("scripts/documentation_lib.py", "# fixture\n"), ("scripts/check_documentation.py", "# fixture\n"),
                       ("scripts/distribution_lib.py", "# fixture\n")):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_current_canonical_docs_links_version_and_reviewed_quickstarts():
    state = docs.document_state(ROOT)
    assert set(state["documents_sha256"]) == set(docs.DOCUMENTS)
    assert state["local_links"] > 0
    assert state["version"] == __import__("goldmine").__version__
    assert CHECKS["release.docs"].kind == "documentation"
    assert TASK_TESTS["P2-07"] == ("test_documentation.py",)


@pytest.mark.parametrize("broken", ["[missing](absent.md)", "[escape](../../outside.md)"])
def test_broken_or_escaping_local_links_are_not_success(docs_root, broken):
    (docs_root / "docs/CI.md").write_text(broken)
    with pytest.raises(InvalidEvidence, match="documentation link"):
        docs.document_state(docs_root)


def test_external_urls_do_not_create_network_requirements(docs_root):
    (docs_root / "docs/CI.md").write_text("[upstream](https://example.invalid/a)\n[local](MCP.md)\n")
    assert docs.document_state(docs_root)["local_links"] == 1


def test_version_mismatch_requires_explicit_reconciliation(docs_root):
    (docs_root / "goldmine/__init__.py").write_text('__version__="9.9.9"\n')
    with pytest.raises(InvalidEvidence, match="versions differ"):
        docs.document_state(docs_root)


def test_anchor_validation_ignores_fenced_examples_but_checks_real_links(docs_root):
    path = docs_root / "docs/CI.md"
    path.write_text("# Available target\n```md\n[fake](#unavailable)\n```\n[actual](#available-target)\n")
    docs.document_state(docs_root)
    path.write_text("# Available target\n[actual](#unavailable)\n")
    with pytest.raises(InvalidEvidence, match="documentation anchor"):
        docs.document_state(docs_root)


@pytest.mark.parametrize("document,old,new", [
    ("README.md", "--lexical-only", "--download-everything"),
    ("docs/MCP.md", "'.[mcp]'", "'goldmine[mcp]'"),
])
def test_changed_markdown_commands_cannot_be_arbitrarily_executed(docs_root, document, old, new):
    path = docs_root / document
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(InvalidEvidence, match="fixed command registry"):
        docs.document_state(docs_root)


def test_missing_docs_inputs_return_two_and_do_not_create_state(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/check_documentation.py")], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "MISSING"
    assert list(tmp_path.iterdir()) == []


def test_fixed_commands_cover_actual_cjk_backup_sdk_and_cli_help(tmp_path):
    commands = docs.commands(tmp_path, sys.executable)
    assert commands["quickstart"][0] == ["/bin/sh", "-eu", "-c", docs.QUICKSTART]
    assert commands["mcp-install"][0][-1] == docs.MCP_INSTALL
    assert {"search-json", "backup", "reindex", "provenance", "provenance-mcp", "mcp-protocol"} <= commands.keys()
    assert all(name in commands for name in ("help-watch", "help-serve", "help-mcp"))
    assert all(not str(cwd).startswith(str(ROOT)) for _, cwd in commands.values())


def test_forged_print_pass_receipt_fails_fixed_command_validation(monkeypatch, docs_root, tmp_path):
    # Synthetic orchestrator counterexample; no installed/quality success claimed.
    from acceptance_lib import delivery
    delivery_id = {"wheel_sha256": "1" * 64}
    monkeypatch.setattr(delivery, "verify_bundle", lambda _: delivery_id)
    before = docs.document_state(docs_root)
    out = tmp_path / "capture"
    out.mkdir()
    artifacts = {}
    for name in ("source/sample/notes.md", "source/sample.sqlite", "backup.sqlite", "outside-source/mcp_smoke.py"):
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic orchestration fixture\n")
        artifacts[name] = artifact(out, target)
    distribution = out / "distribution.json"
    write_json(distribution, {})
    directory = out / "fake"
    capture_command(directory, "docs.quickstart", [sys.executable, "-c", 'print("PASS")'], cwd=out,
                    identity={"documentation": before, "distribution": delivery_id}, suite_id=docs.SUITE)
    snapshot = {**before["modules_sha256"], **{name: sha256(docs_root / name) for name in ("pyproject.toml", "README.md", "LICENSE")}}
    value = {"schema": 1, "kind": docs.SUITE, "before": before, "after": copy.deepcopy(before),
             "distribution": {"manifest_path": str(distribution), "sha256": sha256(distribution)},
             "distribution_identity": delivery_id, "source_snapshot": snapshot, "python": sys.executable,
             "wheelhouse": str(out), "artifacts": artifacts, "commands": {key: artifact(out, directory / "manifest.json") for key in docs.commands(out, sys.executable)}}
    write_json(out / "manifest.json", value)
    with pytest.raises(InvalidEvidence, match="command/identity/exit status"):
        docs.verify(docs_root, out / "manifest.json")
    # The same capture cannot inherit a later source or document change.
    (docs_root / "docs/CI.md").write_text("# Edited after capture\n")
    with pytest.raises(InvalidEvidence, match="changed after"):
        docs.verify(docs_root, out / "manifest.json")
