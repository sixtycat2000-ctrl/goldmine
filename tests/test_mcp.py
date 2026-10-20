"""Real official SDK clients exercise all tools and a clean stdio subprocess."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.optional_mcp


def fixture_index(tmp_path):
    from goldmine.backends.mock import MockBackend
    from goldmine.store import FileRec, SegmentRec, Store

    db = tmp_path / "index.sqlite"
    store = Store(db)
    if store.meta("index_uuid") is None:
        store.set_meta("index_uuid", str(uuid4()))
    fid = store.upsert_file(FileRec(path=str(tmp_path / "source-that-does-not-exist.txt"), kind="doc", state="done",
                                    capability="content_indexed"))
    text = "Orbit maintenance checklist. Inspect the copper gasket every Tuesday."
    store.replace_segments(fid, [SegmentRec(kind="chunk", modality="text", loc=1, loc_end=2,
                                           text_for_fts=text, vec=MockBackend().embed_texts([text])[0])])
    store.close()
    return db


def decode(result):
    assert result.structured_content is None  # one body, not two protocol copies
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


def test_real_sdk_inprocess_search_get_context_and_errors(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from goldmine.application import Application
    from goldmine.backends.mock import MockBackend
    from goldmine.mcp_server import create_server

    db = fixture_index(tmp_path)
    server = create_server(Application(db, lambda: MockBackend(dim=768)))
    import subprocess
    def no_actions(*args, **kwargs):
        raise AssertionError("retrieving indexed evidence must not execute a command")
    monkeypatch.setattr(subprocess, "Popen", no_actions)

    async def exercise():
        async with Client(server) as client:
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == {"goldmine_search", "goldmine_get", "goldmine_context", "goldmine_status"}
            result = await client.call_tool("goldmine_search", {"query": "copper gasket", "context": 0})
            assert not result.is_error
            data = decode(result)
            cid = data["hits"][0]["citation"]["citation_id"]
            for name, args in [("goldmine_get", {"citation_id": cid}),
                               ("goldmine_context", {"citation_ids": [cid]}),
                               ("goldmine_search", {"query": "copper gasket", "pack": True, "context": 0})]:
                returned = await client.call_tool(name, args)
                assert not returned.is_error
                body = decode(returned)
                assert "copper gasket" in body["context_text"]
                assert cid in body["context_text"]
                assert "hits" not in body and "citations" not in body
            status = await client.call_tool("goldmine_status", {})
            assert decode(status)["counts"]["files"] == 1
            for name, args in [("goldmine_search", {"query": "x", "limit": 0}),
                               ("goldmine_search", {"query": "中文" * 1500}),
                               ("goldmine_get", {"citation_id": "/etc/passwd"}),
                               ("goldmine_status", {"sql": "DROP TABLE files"}),
                               ("goldmine_search", {"query": "x", "filters": {"unknown": 1}})]:
                bad = await client.call_tool(name, args)
                assert bad.is_error
                assert decode(bad)["error"]["kind"] == "invalid_params"

    anyio.run(exercise)


def test_real_sdk_stdio_session_round_trip(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    db = fixture_index(tmp_path)
    env = {**os.environ, "GOLDMINE_HOME": str(tmp_path / "home"), "PYTHONPATH": str(ROOT)}
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "goldmine", "mcp", "--db", str(db), "--backend", "mock"], env=env, cwd=tmp_path)

    async def exercise():
        async with Client(stdio_client(params, errlog=sys.__stderr__), read_timeout_seconds=15, mode="legacy") as client:
            assert len((await client.list_tools()).tools) == 4
            result = await client.call_tool("goldmine_search", {"query": "copper gasket", "pack": True, "context": 0})
            assert not result.is_error
            assert "copper gasket" in decode(result)["context_text"]

    anyio.run(exercise)


def test_client_cancellation_does_not_start_another_backend(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    import time
    from mcp import Client
    from goldmine.application import Application
    from goldmine.backends.mock import MockBackend
    from goldmine.deadline import check_deadline
    from goldmine.mcp_server import create_server

    calls = []
    class Slow(MockBackend):
        def embed_texts(self, texts):
            calls.append("embed")
            for _ in range(100):
                time.sleep(.005)
                check_deadline()
            calls.append("late")
            return super().embed_texts(texts)

        def close(self):
            calls.append("closed")
            super().close()

    server = create_server(Application(fixture_index(tmp_path), lambda: Slow(dim=768)))
    async def exercise():
        async with Client(server) as client:
            with anyio.move_on_after(.05):
                await client.call_tool("goldmine_search", {"query": "copper gasket"})
            await anyio.sleep(.15)
    anyio.run(exercise)
    assert "embed" in calls and "closed" in calls and "late" not in calls


def test_serial_queue_rejects_a_third_waiter(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    import time
    from mcp import Client
    from goldmine.application import Application
    from goldmine.backends.mock import MockBackend
    from goldmine.mcp_server import create_server

    class Slow(MockBackend):
        def embed_texts(self, texts):
            time.sleep(.15)
            return super().embed_texts(texts)

    server = create_server(Application(fixture_index(tmp_path), lambda: Slow()))
    results = []
    async def exercise():
        async with Client(server) as client:
            async def request():
                results.append(await client.call_tool("goldmine_search", {"query": "copper gasket"}))
            async with anyio.create_task_group() as tasks:
                for _ in range(4):
                    tasks.start_soon(request)
    anyio.run(exercise)
    assert len(results) == 4
    assert sum(result.is_error for result in results) == 1
    assert any("queue is full" in result.content[0].text for result in results if result.is_error)


def test_mcp_context_contains_stored_neighbors_and_round_trip_citations(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from goldmine.application import Application
    from goldmine.backends.mock import MockBackend
    from goldmine.mcp_server import create_server
    from goldmine.store import FileRec, SegmentRec, Store

    db = tmp_path / "context.sqlite"
    store = Store(db)
    fid = store.upsert_file(FileRec(path="/nonexistent/source.txt", state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", loc=i, loc_end=i, text_for_fts=text)
                                for i, text in [(1, "needle unique hit"), (2, "omega stored neighbor")]])
    store.close()
    async def exercise():
        async with Client(create_server(Application(db, lambda: MockBackend()))) as client:
            plain = decode(await client.call_tool("goldmine_search", {"query": "needle", "context": 0}))
            expanded = decode(await client.call_tool("goldmine_search", {"query": "needle", "context": 1}))
            assert "context" not in plain["hits"][0]
            neighbor = next(part for part in expanded["hits"][0]["context"] if part["role"] != "hit")
            assert neighbor["text"] == "omega stored neighbor"
            back = decode(await client.call_tool("goldmine_get", {"citation_id": neighbor["citation"]["citation_id"]}))
            assert neighbor["text"] in back["context_text"]
    anyio.run(exercise)
