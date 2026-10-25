"""Explicit upgrade works on missing sources; diagnostics never upgrade."""
import json
import sqlite3

from goldmine import migrations
from goldmine.cli import main
from goldmine.config import Config
from goldmine.doctor import check_index
from tests.test_lexical_migration import legacy_index, vector_digest


def test_doctor_read_only_then_cli_explicit_upgrade_preserves_vectors(tmp_path, monkeypatch, capsys):
    db = tmp_path / "legacy.sqlite"
    legacy_index(db)
    with sqlite3.connect(db) as con:
        digest = vector_digest(con)
    assert check_index(Config(), db).status == "warn"
    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
    monkeypatch.setattr("goldmine.cli._make_backend", lambda *args: (_ for _ in ()).throw(AssertionError("model created")))
    assert main(["reindex", "lexical", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["component"] == "lexical"
    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET
        assert vector_digest(con) == digest
    assert main(["reindex", "lexical", "--db", str(db), "--json"]) == 0


def test_reindex_missing_does_not_create_index(tmp_path, capsys):
    db = tmp_path / "absent" / "index.sqlite"
    assert main(["reindex", "lexical", "--db", str(db), "--json"]) == 4
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == "no_index"
    assert not db.exists()


def test_vector_cache_build_is_explicit_and_disable_does_not_rebuild(tmp_path, capsys):
    from goldmine.store import Store

    db = tmp_path / "legacy.sqlite"
    legacy_index(db)
    assert main(["reindex", "vectors", "--db", str(db), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report["enabled"] and report["rows"] == 1
    assert main(["reindex", "vectors", "--db", str(db), "--enable-cache", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"]
    from contextlib import closing

    with closing(Store(db, ro=True)) as store:
        assert store.meta("vector_cache_enabled") == "1"
    assert main(["reindex", "vectors", "--db", str(db), "--disable-cache", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "DISABLED"
