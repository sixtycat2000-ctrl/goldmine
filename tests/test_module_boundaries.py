"""Keep the entry small and prevent adapters leaking into retrieval/storage."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "goldmine"


def test_cli_is_entry_and_compatibility_surface():
    source = (ROOT / "cli.py").read_text()
    meaningful = [line for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert len(meaningful) <= 350
    from goldmine.cli import build_parser, main, render_hits
    from goldmine.parser import build_parser as parser
    from goldmine.render import render_hits as renderer

    assert build_parser is parser and render_hits is renderer and callable(main)


def test_core_does_not_import_command_or_protocol_adapters():
    names = ["application.py", "search.py", "pack.py", "store.py", "citations.py",
             "storage_connections.py", "contracts.py"]
    for name in names:
        tree = ast.parse((ROOT / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in {"cli", "commands", "mcp_server"}, name


def test_command_modules_have_no_entry_import_or_cross_command_dependency():
    for file in (ROOT / "commands").glob("*.py"):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "cli", file
                assert node.module != "commands", file
                assert not (node.level == 1 and node.module), file
