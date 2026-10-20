#!/usr/bin/env python3
"""Exercise an installed v4 producer and installed v6 consumer on disposable data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import venv

from acceptance_lib.evidence import artifact, capture_command, read_command_run, sha256, write_json
from distribution_lib import inspect_wheel
from native_build_lib.source import BuildError


def stored_state(path: Path) -> dict:
    """Read original rows without invoking either product's migration code."""
    digest = hashlib.sha256()
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in db.execute("PRAGMA table_info(fts)")}
        # v5 tokenizes its search column and preserves verbatim text separately.
        # Compare the stored original, never the intentionally derived tokens.
        body = "coalesce(original,body)" if "original" in columns else "body"
        text = db.execute(f"SELECT rowid,name,{body} FROM fts ORDER BY rowid").fetchall()
        for sid, vector in db.execute("SELECT id,vec FROM segments WHERE vec IS NOT NULL ORDER BY id"):
            digest.update(str(sid).encode())
            digest.update(vector)
        files = db.execute("SELECT id,path,state FROM files ORDER BY id").fetchall()
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    return {"schema": version, "vectors_sha256": digest.hexdigest(),
            "text_sha256": hashlib.sha256(json.dumps(text, ensure_ascii=False).encode()).hexdigest(),
            "files": files, "integrity": integrity}


def upgrade(old_wheel: Path, wheel: Path, wheelhouse: Path, out: Path) -> dict:
    if out.exists() and any(out.iterdir()):
        raise BuildError("upgrade output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    current = inspect_wheel(wheel)
    cwd = out / "outside-source"
    cwd.mkdir()
    home = out / "state"
    home.mkdir()
    config = home / "config.toml"
    config.write_text("")
    commands = []
    identity = {"wheel_sha256": current["sha256"], "old_wheel_sha256": sha256(old_wheel)}

    def environment(which):
        result = {k: v for k, v in os.environ.items()
                  if not k.startswith(("GOLDMINE_", "DYLD_")) and k not in ("PYTHONPATH", "PYTHONHOME")}
        result.update({"GOLDMINE_HOME": str(home), "GOLDMINE_CONFIG": str(config),
                       "PATH": str(out / which / "bin"), "PYTHONNOUSERSITE": "1",
                       "PIP_DISABLE_PIP_VERSION_CHECK": "1", "GOLDMINE_OCR_AUTOCOMPILE": "0"})
        return result

    def command(name, which, argv, allowed=(0,)):
        directory = out / "commands" / name
        capture_command(directory, f"distribution.upgrade.{name}", [str(v) for v in argv],
                        cwd=cwd, env=environment(which), identity=identity,
                        suite_id="distribution.upgrade.v1", timeout=180)
        _, receipt, stdout = read_command_run(directory / "manifest.json")
        commands.append({"name": name, "manifest": artifact(out, directory / "manifest.json")})
        if receipt["exit_code"] not in allowed or receipt["timed_out"]:
            raise BuildError(f"upgrade command failed: {name}; logs: {directory}")
        for stream in (stdout, directory / "stderr.log"):
            if "Traceback (most recent call last)" in stream.read_text():
                raise BuildError(f"upgrade command exposed traceback: {name}")
        return stdout.read_text()

    for which, package, extra in (("old", old_wheel, ""), ("new", wheel, "[mcp]")):
        venv.EnvBuilder(with_pip=True).create(out / which)
        python = out / which / "bin/python"
        command(f"install-{which}", which, [python, "-m", "pip", "install", "--no-index",
                "--find-links", wheelhouse, str(package.resolve()) + extra])
        actual = json.loads(command(f"provenance-{which}", which, [python, "-c",
            "import goldmine,json; print(json.dumps({'file':goldmine.__file__}))"]))
        if not Path(actual["file"]).is_relative_to(out / which):
            raise BuildError("upgrade imported source checkout instead of installed wheel")
    old, new = out / "old/bin/goldmine", out / "new/bin/goldmine"
    corpus = cwd / "corpus"
    corpus.mkdir()
    document = corpus / "upgrade-proof.txt"
    document.write_text("Zircon lantern records the harbor meeting and project invoice AB-12.\n" * 8)
    db = home / "upgrade.sqlite"
    report = json.loads(command("v4-ingest", "old", [old, "ingest", corpus, "--db", db, "--backend", "mock", "--json"]))
    if report.get("files", {}).get("failed") != 0:
        raise BuildError("legacy producer did not successfully ingest fixture")
    before = stored_state(db)
    if before["schema"] != 4:
        raise BuildError("legacy producer must create an actual schema v4 index")
    hits = json.loads(command("v4-readonly", "new", [new, "search", "zircon", "--lexical", "--db", db, "--json"]))
    if not hits or stored_state(db) != before:
        raise BuildError("new read-only search failed or mutated legacy index")
    backup = home / "pre-upgrade.sqlite"
    command("v4-backup", "new", [new, "backup", "--db", db, "--out", backup, "--json"])
    if stored_state(backup) != before:
        raise BuildError("backup did not preserve the pre-upgrade state")
    command("v6-reindex", "new", [new, "reindex", "lexical", "--db", db, "--json"])
    after = stored_state(db)
    if after["schema"] != 6 or after["integrity"] != "ok":
        raise BuildError("explicit derived upgrade did not reach schema v6")
    for field in ("vectors_sha256", "text_sha256", "files"):
        if before[field] != after[field]:
            raise BuildError(f"derived migration changed original {field}")
    protocol = cwd / "citation-smoke.py"
    protocol.write_text(_CITATION)
    python = out / "new/bin/python"
    citation = json.loads(command("citation-create", "new", [python, protocol, new, db, "search"]))
    command("citation-read", "new", [python, protocol, new, db, "get", citation["citation_id"]])
    document.write_text("Zircon lantern was revised; its old harbor statement is obsolete.\n" * 8)
    command("v6-modify", "new", [new, "ingest", corpus, "--db", db, "--backend", "mock", "--json"])
    stale = json.loads(command("citation-stale", "new", [python, protocol, new, db, "stale", citation["citation_id"]]))
    if stale.get("stale_refused") is not True:
        raise BuildError("changed stored evidence did not invalidate the installed MCP citation")
    future = home / "future.sqlite"
    with sqlite3.connect(db) as src, sqlite3.connect(future) as target:
        src.backup(target)
        target.execute("PRAGMA user_version=7")
    future_hash = sha256(future)
    refused = json.loads(command("future-schema", "new", [new, "status", "--db", future, "--json"], allowed=(1,)))
    if refused.get("error", {}).get("kind") != "index_schema_newer" or sha256(future) != future_hash:
        raise BuildError("future schema was not refused without modification")
    # No process holds this disposable database now. Restore a consistent
    # pre-upgrade copy and exercise the genuinely old installed executable.
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    shutil.copyfile(backup, db)
    restored = stored_state(db)
    old_hits = json.loads(command("rollback-read", "old", [old, "search", "zircon", "--lexical", "--db", db, "--json"]))
    if restored != before or not old_hits:
        raise BuildError("backup rollback did not restore old executable compatibility")
    result = {"schema": 1, "kind": "goldmine.installed-upgrade", "identity": identity,
              "fixture": "synthetic/mock; schema and stored-byte mechanics only", "before": before,
              "after": after, "restored": restored, "readonly_legacy": True,
              "future_schema_refused": True, "stale_citation_refused": True,
              "commands": commands}
    write_json(out / "manifest.json", result)
    return result


_CITATION = '''import anyio,json,os,sys
from mcp import Client
from mcp.client.stdio import StdioServerParameters,stdio_client
async def main():
    params=StdioServerParameters(command=sys.argv[1],args=['mcp','--db',sys.argv[2],'--backend','mock'],env=dict(os.environ))
    async with Client(stdio_client(params),mode='legacy',read_timeout_seconds=15) as client:
        mode=sys.argv[3]
        if mode=='search':
            result=await client.call_tool('goldmine_search',{'query':'zircon lantern','limit':5})
            assert not result.is_error, result
            payload=result.structured_content or json.loads(result.content[0].text)
            hits=payload['hits']
            content=[h['citation'] for h in hits if h.get('citation',{}).get('evidence_type')=='content']
            assert content, hits
            print(json.dumps({'citation_id':content[0]['citation_id']}))
        else:
            result=await client.call_tool('goldmine_get',{'citation_id':sys.argv[4]})
            if mode=='get':
                assert not result.is_error, result
                print(json.dumps({'readable':True}))
            else:
                assert result.is_error, result
                payload=result.structured_content or json.loads(result.content[0].text)
                error=payload['error']
                assert error['kind']=='stale_citation', error
                print(json.dumps({'stale_refused':True}))
anyio.run(main)
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-wheel", "wheel", "wheelhouse", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        upgrade(args.old_wheel.resolve(), args.wheel.resolve(), args.wheelhouse.resolve(), args.out.resolve())
        print("installed upgrade smoke: PASS")
        return 0
    except (ValueError, OSError, BuildError, sqlite3.Error) as exc:
        print(f"installed upgrade smoke: FAIL — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
