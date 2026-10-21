"""Read-only common-table audit of the exact original schema, without imports.

This facade cannot search, migrate, prepare, or alter an index. The caller has
already verified its explicit historical source root and pinned SQLite bytes.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .evidence import InvalidEvidence


class OriginalReadback:
    def __init__(self, path):
        self.db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
        self.db.execute("PRAGMA query_only=ON")
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 4:
            self.db.close()
            raise InvalidEvidence("original readback requires its separately prepared schema-v4 SQLite index")

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def segment_text(self, sid):
        row = self.db.execute("SELECT coalesce(original,body) FROM fts WHERE rowid=?", (sid,)).fetchone()
        return row[0] if row else None

    @contextmanager
    def read_snapshot(self):
        self.db.execute("BEGIN")
        try:
            yield self
        finally:
            self.db.rollback()

    def close(self):
        self.db.close()
