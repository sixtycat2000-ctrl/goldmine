"""Official SDK stdio adapter; all evidence operations use Application.

The SDK is imported only when this explicit optional entry is started. The
queue has one active model operation plus at most two waiting requests.
"""
from __future__ import annotations

from dataclasses import asdict
import json

from . import __version__
from .application import Application
from .backends import BackendUnavailable
from .backends.base import EmbeddingError
from .contracts import ApplicationError, ContextRequest
from .deadline import Deadline, RequestTimeout, request_cancellation, request_timeout
from .fingerprints import FingerprintMismatch
from .mcp_contracts import DESCRIPTIONS, SCHEMAS, project_context, project_search, search_request


def create_server(application: Application):
    import anyio
    from jsonschema import Draft202012Validator
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

    lock = anyio.Lock()
    pending = 0

    def reply(payload, error=False):
        # Explicit CallToolResult prevents duplicating context_text in the SDK's
        # structured and unstructured representations.
        return CallToolResult(content=[TextContent(text=json.dumps(payload, ensure_ascii=False,
                                                    allow_nan=False, separators=(",", ":")))], is_error=error)

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name=name, description=DESCRIPTIONS[name],
                         input_schema=schema, annotations=ToolAnnotations(read_only_hint=True,
                                                                           open_world_hint=False))
                                     for name, schema in SCHEMAS.items()])

    def invoke(name, arguments, deadline):
        with request_timeout(deadline=deadline):
            with request_cancellation(anyio.from_thread.check_cancelled):
                anyio.from_thread.check_cancelled()
                if name == "goldmine_search":
                    result = application.search(search_request(arguments, timeout_s=deadline.remaining()))
                    return project_search(result, pack=arguments.get("pack", False))
                if name == "goldmine_get":
                    return project_context(application.get(arguments["citation_id"], budget=arguments.get("budget", 2000),
                                                            timeout_s=deadline.remaining()))
                if name == "goldmine_context":
                    return project_context(application.context(ContextRequest(arguments["citation_ids"],
                        neighbors=arguments.get("neighbors", 1), budget=arguments.get("budget", 4000),
                        timeout_s=deadline.remaining())))
                return asdict(application.status(timeout_s=deadline.remaining()))

    async def call_tool(context, params):
        nonlocal pending
        name, arguments = params.name, params.arguments or {}
        if name not in SCHEMAS:
            return reply({"error": {"kind": "invalid_params", "message": "unknown read-only tool"}}, True)
        errors = list(Draft202012Validator(SCHEMAS[name]).iter_errors(arguments))
        if errors:
            return reply({"error": {"kind": "invalid_params", "message": errors[0].message[:400]}}, True)
        deadline = Deadline.after(10 if arguments.get("mode") == "quality" else 5)
        if pending >= 3:
            return reply({"error": {"kind": "timeout", "message": "request queue is full (two waiting)"}}, True)
        pending += 1
        acquired = False
        try:
            with anyio.fail_after(deadline.remaining()):
                await lock.acquire()
            acquired = True
            payload = await anyio.to_thread.run_sync(invoke, name, arguments, deadline,
                                                    abandon_on_cancel=False)
            return reply(payload)
        except (RequestTimeout, TimeoutError):
            return reply({"error": {"kind": "timeout", "message": "request deadline exceeded"}}, True)
        except ApplicationError as exc:
            return reply({"error": {"kind": exc.kind, "message": str(exc)}}, True)
        except FingerprintMismatch as exc:
            return reply({"error": {"kind": "fingerprint_mismatch", "message": str(exc)}}, True)
        except (BackendUnavailable, EmbeddingError) as exc:
            return reply({"error": {"kind": "missing_capability", "message": str(exc)}}, True)
        finally:
            pending -= 1
            if acquired:
                lock.release()

    return Server("goldmine", version=__version__, on_list_tools=list_tools, on_call_tool=call_tool)


def run_stdio(application: Application) -> None:
    import anyio
    import os
    import signal
    import sys
    from mcp.server.stdio import stdio_server

    server = create_server(application)

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            if sys.platform == "win32":
                await server.run(read_stream, write_stream, server.create_initialization_options())
                return
            with anyio.open_signal_receiver(signal.SIGTERM) as signals:
                async with anyio.create_task_group() as group:
                    async def stop_on_signal():
                        async for _ in signals:
                            group.cancel_scope.cancel()
                            break
                    group.start_soon(stop_on_signal)
                    try:
                        await server.run(read_stream, write_stream, server.create_initialization_options())
                    finally:
                        group.cancel_scope.cancel()

    try:
        anyio.run(run)
    except* (BrokenPipeError, anyio.BrokenResourceError, anyio.ClosedResourceError):
        # the client went away (EPIPE) — exit quietly, no traceback (DigUp
        # IndexService.swift:331-336). SDK task groups wrap these exceptions;
        # except* removes only disconnect errors, preserving unrelated failures.
        # devnull dup2 also prevents a second error at interpreter shutdown.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                os.close(devnull)
        except (OSError, ValueError):
            pass
