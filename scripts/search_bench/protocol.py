"""Pair actual application requests with actual official-SDK stdio requests."""
from __future__ import annotations

import json
import os
import sys
import time

from acceptance_lib.evidence import artifact, sha256, write_json


async def paired(session, queries, count):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from goldmine.application import Application
    from goldmine.backends.serve import connect_or_cold
    from goldmine.config import load
    from goldmine.mcp_contracts import project_search, search_request

    cfg = load(config_path=session.config, env=session.env)
    app = Application(session.db, lambda: connect_or_cold(cfg, "llama")[0])
    params = StdioServerParameters(command=session.python,
        args=["-m", "goldmine.cli", "mcp", "--db", str(session.db), "--backend", "llama"], env=session.env)
    samples = []
    async with Client(stdio_client(params, errlog=sys.__stderr__), mode="legacy", read_timeout_seconds=15) as client:
        # Handshake and the first model/cache requests are deliberately excluded
        # from warm-call overhead, but their count is fixed in the report.
        for i in range(3 + count):
            query = queries[i % len(queries)]
            arguments = {"query": query["query"], "limit": 5, "mode": "fast", "context": 0}
            order = ("direct", "stdio") if i % 2 == 0 else ("stdio", "direct")
            result = {}
            for arm in order:
                started = time.perf_counter()
                if arm == "direct":
                    payload = project_search(app.search(search_request(arguments, timeout_s=5)), pack=False)
                else:
                    response = await client.call_tool("goldmine_search", arguments)
                    payload = response.structured_content or json.loads(response.content[0].text)
                    if response.is_error:
                        raise ValueError(f"MCP request failed: {payload}")
                elapsed = (time.perf_counter() - started) * 1000
                result[arm] = {"wall_ms": elapsed, "payload": payload}
            if i < 3:
                continue
            row = {"ordinal": i-3, "query_id": query["id"], "arguments": arguments, "order": list(order),
                   "direct": result["direct"], "stdio": result["stdio"],
                   "identity": session.identity}
            path = session.out / "pairs" / f"{i-3:03}.json"
            write_json(path, row)
            samples.append({"query_id": query["id"], "direct_ms": result["direct"]["wall_ms"],
                            "stdio_ms": result["stdio"]["wall_ms"],
                            "equal": result["direct"]["payload"] == result["stdio"]["payload"],
                            "via": result["stdio"]["payload"].get("metadata", {}).get("via"),
                            "raw_pair": artifact(session.out, path)})
    return samples
