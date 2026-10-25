"""A4 (digup-p2-closeout-001): broken-pipe discipline. `goldmine search | head`
exits quietly — no traceback, no "Exception ignored" shutdown noise (DigUp
IndexService.swift:331-336). Same for the MCP stdio server when the client
vanishes.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

LOUD = ("Traceback", "BrokenPipeError", "Exception ignored")


def _spawn_with_closed_stdout(argv, **kwargs):
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs
    )
    proc.stdout.close()  # the reader leaves before the child's first write
    err = proc.stderr.read().decode("utf-8", "replace")
    rc = proc.wait(timeout=120)
    return rc, err


def test_help_into_closed_pipe_exits_quietly():
    rc, err = _spawn_with_closed_stdout([sys.executable, "-m", "goldmine", "help"])
    assert rc == 0, err
    for marker in LOUD:
        assert marker not in err, marker


def test_search_into_closed_pipe_exits_quietly(tmp_path):
    from test_mcp import fixture_index

    db = fixture_index(tmp_path)
    rc, err = _spawn_with_closed_stdout(
        [sys.executable, "-m", "goldmine", "search", "copper gasket",
         "--lexical", "--db", str(db)]
    )
    assert rc == 0, err
    for marker in LOUD:
        assert marker not in err, marker


@pytest.mark.optional_mcp
def test_mcp_stdio_client_gone_exits_quietly(tmp_path):
    pytest.importorskip("mcp")
    from test_mcp import fixture_index

    db = fixture_index(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "goldmine", "mcp", "--db", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    proc.stdout.close()  # client died before the handshake reply
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "epipe-test", "version": "0"}},
    })
    try:
        proc.stdin.write((request + "\n").encode("utf-8"))
        proc.stdin.flush()
    except BrokenPipeError:
        pass  # server already exited — also fine
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
    err = proc.stderr.read().decode("utf-8", "replace")
    rc = proc.wait(timeout=120)
    assert rc == 0, err
    for marker in LOUD:
        assert marker not in err, marker
