"""Record exact pytest identities and separate declared capability profiles."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import socket
import subprocess

import pytest

from .evidence import write_json
from .registry import LIVE_TESTS, TASK_TESTS, classify

_collection_skips: list[str] = []
_original_popen = subprocess.Popen
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
NATIVE_TOOLS = {"sips", "pdftotext", "pdfinfo", "pdftoppm", "ffmpeg", "ffprobe",
                "swiftc", "textutil", "osascript", "whisper-cli", "llama-server", "goldmine-ocr"}


def pytest_addoption(parser):
    parser.addoption("--acceptance-profile", default=None)
    parser.addoption("--acceptance-inventory", default=None)


def pytest_configure(config):
    for marker in ("assets", "native", "optional_mcp", "optional_foresight"):
        config.addinivalue_line("markers", f"{marker}: explicit acceptance capability owner")
    if os.environ.get("GOLDMINE_ACCEPTANCE_HERMETIC") == "1":
        def guarded_popen(args, *pos, **kw):
            first = args[0] if isinstance(args, (list, tuple)) and args else args
            if isinstance(first, (str, bytes, os.PathLike)):
                name = Path(os.fsdecode(first)).name
                if name in NATIVE_TOOLS:
                    raise FileNotFoundError(f"native tool disabled in hermetic profile: {name}")
            return _original_popen(args, *pos, **kw)

        def loopback_only(address):
            if isinstance(address, (str, bytes)):
                return  # local Unix socket
            host = address[0]
            try:
                local = host == "localhost" or ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = False
            if not local:
                raise OSError("external network disabled in hermetic acceptance")

        def connect(sock, address):
            loopback_only(address)
            return _original_connect(sock, address)

        def connect_ex(sock, address):
            loopback_only(address)
            return _original_connect_ex(sock, address)

        subprocess.Popen = guarded_popen
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex


def pytest_unconfigure(config):
    subprocess.Popen = _original_popen
    socket.socket.connect = _original_connect
    socket.socket.connect_ex = _original_connect_ex


def pytest_ignore_collect(collection_path, config):
    profile = config.getoption("--acceptance-profile")
    if collection_path.name == "test_foresight_offline.py":
        return profile not in (None, "optional-foresight")
    if collection_path.name == "test_mcp.py":
        return profile not in (None, "optional-mcp", "P1-09", "P1-10")
    return None


def pytest_collectreport(report):
    if report.skipped:
        _collection_skips.append(report.nodeid)


def pytest_collection_modifyitems(config, items):
    profile = config.getoption("--acceptance-profile")
    selected, deselected, all_items = [], [], []
    for item in items:
        markers = [m.name for m in item.iter_markers()]
        owner = classify(item.nodeid, markers)
        all_items.append({"nodeid": item.nodeid, "profile": owner})
        if profile in TASK_TESTS:
            keep = Path(item.nodeid.split("::")[0]).name in TASK_TESTS[profile] and owner != "live"
        elif profile in LIVE_TESTS:
            keep = Path(item.nodeid.split("::")[0]).name in LIVE_TESTS[profile] and owner == "live"
        else:
            keep = profile is None or owner == profile
        if keep:
            item.user_properties.append(("acceptance_nodeid", item.nodeid))
            selected.append(item)
        else:
            deselected.append(item)
    config._acceptance_items = all_items
    config._acceptance_selected = [i.nodeid for i in selected]
    config._acceptance_deselected = [i.nodeid for i in deselected]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption("--acceptance-inventory")
    if path:
        write_json(Path(path), {"schema": 1,
            "collected": getattr(session.config, "_acceptance_items", []),
            "selected": getattr(session.config, "_acceptance_selected", []),
            "deselected": getattr(session.config, "_acceptance_deselected", []),
            "collection_skips": _collection_skips, "exit_code": int(exitstatus)})
