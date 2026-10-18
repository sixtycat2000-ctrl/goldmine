"""Schema migration ladder + index backups (v2 05 §4.1 / §4.8).

The ONLY place that writes schema versions. One private function per version:
``MIGRATIONS[n]`` upgrades ``n-1 -> n`` and returns ``n``. All steps of one
migrate() call run inside ONE transaction (BEGIN IMMEDIATE), each ending with
its own ``PRAGMA user_version = n`` stamp, and completed steps are recorded in
``meta.migration_history_json`` as ``{to, at, app_version, ms, notes}``.

Ladder rules (05 §4.1, each normative):
1. One function per version — never a combined "upgrade to latest".
2. Migrations never read source files, never embed, never call tools; they
   transform stored rows only (the v1->v2 backfill recomputes bookkeeping from
   the KEPT legacy fingerprint — nothing is re-embedded).
3. Fresh databases are created directly at TARGET by Store's v2 DDL; the ladder
   only does real work for existing pre-TARGET files.
4. Fixture-tested: tests/test_migrations.py builds a v1-format index, runs the
   ladder, and asserts the B2 zero-stale invariant (migrated embed ids equal
   fresh-index ids).
5. History in meta (see above).
6. Backups before migrating: when the file is larger than ``snapshot_threshold``
   (1 MB default), a consistent snapshot is taken via VACUUM INTO — the same
   mechanism as ``goldmine backup`` — to ``<index>.pre-v<N>.sqlite`` before the
   ladder runs.

Unknown FUTURE schemas (user_version > TARGET) are refused exactly like the
v1 store did: RuntimeError "... newer than this build ... upgrade goldmine".

Backups (05 §4.8): ``create_backup()`` is the engine behind ``goldmine backup``
— VACUUM INTO a timestamped file under ``<home>/backups/``, verify it opens +
passes PRAGMA integrity_check, prune older backups beyond ``keep``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

TARGET = 2  # == Store.SCHEMA_VERSION; single source, imported by store.py

#: The chunker id a v1 index really ran with (REDESIGN §6 / 05:152,264). The
#: backfill stamps THIS, never the current CHUNKER_VERSION: staleness then
#: honestly reports the migrated rows chunker-stale, and the next plain ingest
#: re-chunks them under the D3 token chunker (pipeline skip-fix, 02 G10).
#: Stamping the current constant would LIE ("already chunk.v2-tok") and the
#: re-chunk would never happen for upgraded indexes.
V1_CHUNKER_VERSION = "chunk.v1"

#: Files smaller than this migrate without a pre-migration snapshot (05 §4.1
#: rule 6 — cheap insurance, user deletes the snapshot later).
SNAPSHOT_THRESHOLD_BYTES = 1 << 20


def _user_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def _db_path(db: sqlite3.Connection) -> Path | None:
    for _, name, file in db.execute("PRAGMA database_list").fetchall():
        if name == "main" and file:
            return Path(file)
    return None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _append_history(db: sqlite3.Connection, entry: dict) -> None:
    row = db.execute(
        "SELECT value FROM meta WHERE key = 'migration_history_json'"
    ).fetchone()
    try:
        history = json.loads(row[0]) if row else []
    except json.JSONDecodeError:
        history = []
    if not isinstance(history, list):
        history = []
    history.append(entry)
    _set_meta(db, "migration_history_json", json.dumps(history))


# ------------------------------------------------------------------- ladder


def migrate(
    db: sqlite3.Connection,
    *,
    path: Path | None = None,
    snapshot_threshold: int = SNAPSHOT_THRESHOLD_BYTES,
) -> list[int]:
    """Bring `db` up to TARGET. Returns the list of applied step versions
    (empty when already current, or when a fresh v2-shaped database just needs
    its stamp — rule 3). Raises RuntimeError for a newer schema, exactly the
    v1 refusal shape."""
    if path is None:
        path = _db_path(db)
    version = _user_version(db)
    if version > TARGET:
        raise RuntimeError(
            f"index {path or '?'} has schema v{version}, newer than this build "
            f"(v{TARGET}) — upgrade goldmine"
        )
    if version == TARGET:
        return []
    if version == 0 and "embed_id" in _columns(db, "files"):
        # Fresh database: Store's DDL already created the full v2 shape; stamp
        # directly (rule 3). No history entry — nothing was migrated.
        db.execute(f"PRAGMA user_version = {TARGET}")
        db.commit()
        return []

    notes = []
    snapshot = _pre_migration_snapshot(path, TARGET, threshold=snapshot_threshold)
    if snapshot:
        notes.append(f"snapshot {Path(snapshot).name}")

    db.execute("PRAGMA busy_timeout = 60000")
    db.execute("BEGIN IMMEDIATE")
    applied: list[int] = []
    try:
        version = _user_version(db)  # re-read inside the write lock
        if version > TARGET:
            raise RuntimeError(
                f"index {path or '?'} has schema v{version}, newer than this "
                f"build (v{TARGET}) — upgrade goldmine"
            )
        for target in range(version + 1, TARGET + 1):
            step = MIGRATIONS.get(target)
            if step is None:
                raise RuntimeError(
                    f"no migration registered for v{target} "
                    f"(have {sorted(MIGRATIONS)})"
                )
            started = time.monotonic()
            reached = step(db, version)  # each step stamps its own user_version
            _append_history(db, {
                "to": reached,
                "at": time.time(),
                "app_version": _app_version(),
                "ms": round((time.monotonic() - started) * 1000.0, 1),
                "notes": "; ".join(notes) or None,
            })
            notes = []  # the snapshot note belongs to the first step only
            applied.append(reached)
            version = reached
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - nothing to roll back
            pass
        raise
    return applied


def _app_version() -> str:
    from . import __version__

    return __version__


# ------------------------------------------------------------ v1 -> v2 (additive)


def _add_column_if_missing(
    db: sqlite3.Connection, table: str, column: str, decl: str
) -> bool:
    if column in _columns(db, table):
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


def _v1_to_v2(db: sqlite3.Connection, from_version: int) -> int:
    """v1 -> v2 — PURELY ADDITIVE (2026-10-16 review C2/C6): per-file identity
    columns + NULL-safe content_date; NO cards DDL (that DDL defers to v2.1's
    E1), no destructive step, nothing re-embedded.

    Backfill (the migrateVectorNames move, 05 §3):
    - files.chunker_version = 'chunk.v1' (V1_CHUNKER_VERSION — the chunker v1
      actually ran; REDESIGN §6). v1 had exactly one chunker, so bookkeeping
      catches up to reality — and stays BEHIND the current CHUNKER_VERSION on
      purpose: the D3 token re-chunk must actually happen for upgraded
      indexes, so migrated rows report chunker-stale and the next plain
      ingest re-does them (skip-fix, 02 G10).
    - files.embed_id = the embed identity id RECOMPUTED from the kept-verbatim
      legacy fingerprint per the B2 binding rule (prompts_doc parsed from the
      legacy scheme's d='…' part; preprocessing EXCLUDED) — set ONLY on
      vec-bearing files (vectorless files cannot be embed-stale), and NEVER a
      copy of meta.fingerprint_id (that hash still covers preprocessing).
    - meta gains embed_id / embed_identity_json derived the same way, plus a
      chunker_backfill note naming the constant and the re-chunk consequence;
      the legacy meta.fingerprint_json is KEPT verbatim for audit/downgrade.
    """
    from . import fingerprints

    _add_column_if_missing(db, "files", "chunker_version", "TEXT")   # NULL = pre-v2
    _add_column_if_missing(db, "files", "embed_id", "TEXT")          # NULL = pre-v2
    _add_column_if_missing(db, "files", "content_date", "REAL")      # NULL-safe

    db.execute(
        "UPDATE files SET chunker_version = ? WHERE chunker_version IS NULL",
        (V1_CHUNKER_VERSION,),
    )
    _set_meta(db, "chunker_backfill_note", (
        f"v1->v2 migration backfilled chunker_version as {V1_CHUNKER_VERSION!r} "
        "(the chunker v1 actually ran); these rows are chunker-stale by design "
        "and the next plain ingest re-chunks them"
    ))

    fp_json = db.execute(
        "SELECT value FROM meta WHERE key = 'fingerprint_json'"
    ).fetchone()
    embed_id = None
    identity_json = None
    if fp_json:
        try:
            fp = json.loads(fp_json[0])
            if isinstance(fp, dict):
                embed_id = fingerprints.embed_id_from_fingerprint(fp)
                identity_json = fingerprints.canonical(
                    fingerprints.embed_identity(fp)
                ).decode()
        except (json.JSONDecodeError, ValueError, TypeError):
            # unparsable legacy fingerprint: leave embed_id NULL — staleness
            # will honestly report those files and the next ingest re-does them
            embed_id = None
    if embed_id:
        db.execute(
            "UPDATE files SET embed_id = ? WHERE embed_id IS NULL AND id IN "
            "(SELECT DISTINCT file FROM segments WHERE vec IS NOT NULL)",
            (embed_id,),
        )
        _set_meta(db, "embed_id", embed_id)
        _set_meta(db, "embed_identity_json", identity_json or "")

    _set_meta(db, "schema_version", str(TARGET))
    db.execute("PRAGMA user_version = 2")
    return 2


MIGRATIONS: dict[int, callable] = {2: _v1_to_v2}


# ----------------------------------------------------------------- backups
#
# The backup mechanism (05 §4.8) doubles as the pre-migration snapshot source:
# VACUUM INTO produces a single-file, consistent copy (WAL content included)
# while readers keep working.


def _vacuum_into(db_path: Path, dest: Path) -> Path:
    dest = Path(dest)
    if dest.exists():
        dest.unlink()
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA busy_timeout = 60000")
        # inline literal (quoted): parameter binding in VACUUM INTO is not
        # supported uniformly across sqlite builds
        quoted = str(dest).replace("'", "''")
        con.execute(f"VACUUM INTO '{quoted}'")
    finally:
        con.close()
    return dest


def verify_backup(dest: Path) -> str:
    """Open the copy read-only and run PRAGMA integrity_check; returns 'ok' or
    raises RuntimeError with the finding (including 'not a database')."""
    con = sqlite3.connect(f"file:{Path(dest)}?mode=ro", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"backup {dest} failed integrity_check: {exc}") from exc
    finally:
        con.close()
    finding = result[0] if result else "no result"
    if finding != "ok":
        raise RuntimeError(f"backup {dest} failed integrity_check: {finding}")
    return "ok"


def _pre_migration_snapshot(
    db_path: Path | None, to_version: int, threshold: int
) -> Path | None:
    """Rule 6: snapshot <index>.sqlite -> <index>.pre-v<N>.sqlite when the file
    is larger than `threshold` bytes. Returns the snapshot path or None."""
    if db_path is None or not Path(db_path).exists():
        return None
    if Path(db_path).stat().st_size <= threshold:
        return None
    dest = Path(str(db_path) + f".pre-v{to_version}.sqlite")
    _vacuum_into(Path(db_path), dest)
    verify_backup(dest)
    return dest


def backup_dir_for(home: Path) -> Path:
    return Path(home) / "backups"


def backup_name_for(db_path: Path, at: float | None = None) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(at) if at else time.localtime())
    return f"{Path(db_path).stem}-{ts}.sqlite"


def backups_of(db_path: Path, home: Path) -> list[Path]:
    """Existing backups of this index under <home>/backups, oldest first
    (timestamped names sort chronologically)."""
    bdir = backup_dir_for(home)
    prefix = f"{Path(db_path).stem}-"
    if not bdir.is_dir():
        return []
    return sorted(p for p in bdir.iterdir() if p.name.startswith(prefix))


def prune_backups(db_path: Path, home: Path, keep: int) -> list[Path]:
    """Delete oldest backups beyond `keep`; returns the pruned paths."""
    keep = max(0, int(keep))
    existing = backups_of(db_path, home)
    pruned = existing[: max(0, len(existing) - keep)]
    for path in pruned:
        path.unlink()
    return pruned


def create_backup(
    db_path: Path,
    *,
    out: Path | None = None,
    keep: int = 5,
    home: Path | None = None,
) -> dict:
    """`goldmine backup` engine: VACUUM INTO a consistent single-file copy,
    verify it opens + passes integrity_check, prune older backups beyond
    `keep`. Default destination: <home>/backups/<index>-<YYYYMMDD-HHMMSS>.sqlite
    (the goldmine home lives under ~/Library, which Time Machine covers by
    default — see the command help)."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"no index at {db_path}")
    if out is None:
        if home is None:
            raise ValueError("home is required when out is not given")
        out_dir = backup_dir_for(home)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / backup_name_for(db_path)
    else:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
    _vacuum_into(db_path, out)
    verify_backup(out)
    pruned: list[Path] = []
    if home is not None and out.parent == backup_dir_for(home):
        pruned = prune_backups(db_path, home, keep)
    return {
        "path": str(out),
        "bytes": out.stat().st_size,
        "integrity": "ok",
        "pruned": [str(p) for p in pruned],
        "source": str(db_path),
    }


def last_backup(db_path: Path, home: Path) -> tuple[Path, float] | None:
    """(newest backup path, mtime) for this index, or None when there is none.
    doctor --deep reports the age (05 §4.8 backup dir health)."""
    existing = backups_of(db_path, home)
    if not existing:
        return None
    newest = max(existing, key=lambda p: p.stat().st_mtime)
    return newest, newest.stat().st_mtime
