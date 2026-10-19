"""SQLite store: schema (ARCHITECTURE §6 DDL, verbatim), all SQL, fp16 blobs.

Single writer, one connection, main thread only. Vectors live beside their rows so
a crash can never desync them. Re-ingest replaces a file's segments in ONE
transaction (V5d atomic segment replacement).

FTS direction note (V3, normative): SQLite bm25() is LOWER-is-better. We only ever
ORDER BY bm25(...) ascending (best first) and rank off that order; the raw bm25
value is never added into a higher-is-better score.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import migrations
from .util import pack_fp16

BLOCK_ROWS = 8192  # fp16 -> f32 conversion block (DigUp pattern)

_DDL = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files(
    id                INTEGER PRIMARY KEY,
    path              TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL DEFAULT 'file',
    kind              TEXT NOT NULL,
    size              INTEGER NOT NULL,
    mtime             REAL,
    sha256            TEXT,
    state             TEXT NOT NULL DEFAULT 'pending',
    capability        TEXT,
    detail            TEXT,
    info              TEXT,
    extractor_version TEXT,
    chunker_version   TEXT,
    embed_id          TEXT,
    content_date      REAL,
    indexed_at        REAL
);
CREATE INDEX IF NOT EXISTS files_state ON files(state, kind);
CREATE INDEX IF NOT EXISTS files_capability ON files(capability);
CREATE INDEX IF NOT EXISTS files_kind_cap ON files(kind, capability);

CREATE TABLE IF NOT EXISTS segments(
    id       INTEGER PRIMARY KEY,
    file     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    modality TEXT NOT NULL,
    loc      REAL,
    loc_end  REAL,
    excerpt  TEXT,
    vec      BLOB
);
CREATE INDEX IF NOT EXISTS segments_file ON segments(file);
CREATE INDEX IF NOT EXISTS segments_vec_file ON segments(file) WHERE vec IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    name, body, original UNINDEXED,
    tokenize = "porter unicode61 remove_diacritics 2 categories 'L* N* Co M*'"
);

CREATE TABLE IF NOT EXISTS cards(
    id            INTEGER PRIMARY KEY,
    title_terms   TEXT NOT NULL,
    size          INTEGER NOT NULL,
    n_files       INTEGER NOT NULL,
    fingerprint_id TEXT NOT NULL,
    distill_algo  TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS card_chunks(
    card   INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    chunk  INTEGER NOT NULL REFERENCES segments(id),
    weight REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS card_chunks_card ON card_chunks(card);
"""


@dataclass
class FileRec:
    path: str
    source: str = "file"
    kind: str = "doc"
    size: int = 0
    mtime: float | None = None
    sha256: str | None = None
    state: str = "pending"
    capability: str | None = None
    detail: str | None = None
    info: dict | None = None
    extractor_version: str | None = None
    # v2 per-file identity (05 §4.2): chunker semantics + embed identity id.
    # NULL embed_id on vectorless files (they cannot be embed-stale);
    # content_date stays NULL until an extractor populates it (05 §4.1c).
    chunker_version: str | None = None
    embed_id: str | None = None
    content_date: float | None = None
    id: int | None = None
    indexed_at: float | None = None


@dataclass
class SegmentRec:
    kind: str                      # name|chunk|page|audio|frame|image
    modality: str                  # none|text|image|audio ('none' => no vector)
    loc: float | None = None
    loc_end: float | None = None
    excerpt: str | None = None
    text_for_fts: str | None = None   # body text for FTS (None => no fts row)
    original_fts: str | None = None   # as-written text when normalization changed it
    vec: np.ndarray | None = None     # float32 (768,) — packed to fp16 on insert


@dataclass
class FtsRow:
    segment_id: int
    file_id: int
    name: str
    body: str
    original: str | None
    rank: float                     # bm25(fts,3.0,1.0): LOWER is BETTER (see module doc)


@dataclass
class VectorTable:
    ids: list[int]
    file_ids: list[int]
    modalities: list[str]
    matrix: np.ndarray              # (N, 768) float32, rows aligned with ids

    def __len__(self) -> int:
        return len(self.ids)


@dataclass
class StatusCounts:
    files: int = 0
    segments: int = 0
    vectors: int = 0
    bytes: int = 0
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)   # kind -> capability -> n
    totals: dict[str, int] = field(default_factory=dict)               # capability -> n
    states: dict[str, int] = field(default_factory=dict)               # state -> n


@dataclass
class CardRow:
    id: int
    title_terms: str
    size: int
    n_files: int
    fingerprint_id: str
    distill_algo: str
    created_at: float


def _file_row(row) -> FileRec:
    (fid, path, source, kind, size, mtime, sha, state, capability, detail, info,
     extractor, chunker_version, embed_id, content_date, indexed_at) = row
    return FileRec(
        path=path,
        source=source,
        kind=kind,
        size=size,
        mtime=mtime,
        sha256=sha,
        state=state,
        capability=capability,
        detail=detail,
        info=json.loads(info) if info else None,
        extractor_version=extractor,
        chunker_version=chunker_version,
        embed_id=embed_id,
        content_date=content_date,
        id=fid,
        indexed_at=indexed_at,
    )


_SELECT_FILE = """SELECT id, path, source, kind, size, mtime, sha256, state, capability,
detail, info, extractor_version, chunker_version, embed_id, content_date, indexed_at
FROM files"""


def connect_index(path: Path | str, *, ro: bool = False) -> sqlite3.Connection:
    """The ONE sqlite3.connect for the package (storage critique 03 F6): every
    caller — Store, doctor's deep checks, migrations' backup verification —
    goes through here so the pragmas are set exactly once and identically.

    Read-write: WAL (persistent), busy_timeout 60 s, foreign_keys ON,
    synchronous=NORMAL. The index is derived, rebuildable data — WAL+NORMAL is
    SQLite's recommended pairing for that case (commits ~4.4x cheaper than the
    FULL default, measured 23.9 -> 5.4 ms per small commit; an app crash still
    preserves every committed transaction, only a power loss may drop the last
    one).

    Read-only (``ro=True``): a mode=ro URI — never creates the file, never
    writes, no journal/WAL pragmas. For short-lived inspection connections
    (doctor --deep, backup verification).

    DELIBERATE EXCEPTION — migrations._vacuum_into keeps a plain connect: its
    connection must stay OUT of WAL mode so the VACUUM INTO copy is written as
    a plain single-file (rollback-journal) database — a WAL-flagged copy is
    exactly what read-only verification chokes on (03 F4).
    """
    path = Path(path)
    if ro:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60.0)
        con.execute("PRAGMA busy_timeout = 60000")
        return con
    con = sqlite3.connect(str(path), timeout=60.0)
    # busy_timeout 60000 per the ARCHITECTURE §6 DDL. (Build note said 5000;
    # the binding DDL wins — see DEVIATION notes in the build report.)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 60000")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA synchronous = NORMAL")
    return con


class Store:
    SCHEMA_VERSION = migrations.TARGET  # single source: migrations.py owns the ladder

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = connect_index(self.path)
        # ORDERING (storage critique 03 F5): user_version is read BEFORE any
        # DDL. The old order — executescript(_DDL) then version check — meant
        # (a) a FAILED migration left a v1 file with pre-created v2-shaped
        # tables (the DDL ran in autocommit before the ladder rolled back),
        # and (b) an old build opening a future schema re-created any table
        # that future schema had dropped, before refusing. Now: refuse future
        # schemas first, hand pre-TARGET files straight to the ladder (the
        # ladder owns EVERY schema write for them), and only fresh (0) /
        # already-current files see the idempotent _DDL.
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if version > self.SCHEMA_VERSION:
            self.db.close()
            raise RuntimeError(
                f"index {self.path} has schema v{version}, newer than this build "
                f"(v{self.SCHEMA_VERSION}) — upgrade goldmine"
            )
        if 0 < version < self.SCHEMA_VERSION:
            # The tested one-per-version ladder (migrations.py): existing
            # pre-TARGET files are upgraded in ONE additive transaction per
            # open, with a pre-migration snapshot when the file is large.
            # NO _DDL before this — a failed migration must leave the file
            # exactly at its old version, with no current-shaped tables or
            # indexes pre-created around it.
            try:
                migrations.migrate(self.db, path=self.path)
            except Exception:
                self.db.close()
                raise
        else:
            # Fresh database (version 0, no tables) or already-current file:
            # create/ensure the full current shape, idempotently. Fresh dbs are
            # then stamped at TARGET by the ladder's fresh-db rule (migrations
            # rule 3) — no ladder rows touched, no history entry.
            self.db.executescript(_DDL)
            if version == 0:
                try:
                    migrations.migrate(self.db, path=self.path)
                except Exception:
                    self.db.close()
                    raise
        self.db.commit()
        # -- VectorTable cache (storage critique 03 F2): load_vectors() reloaded
        # ALL vector blobs + rebuilt the Python lists on every search (measured
        # 102-118 ms per query at 49k vectors / 100k segments). The cache is
        # keyed on the write-generation counter bumped by EVERY Store path that
        # touches segments (single-writer discipline: there are no others) plus
        # PRAGMA data_version, which changes when a DIFFERENT connection
        # commits (same-connection writes are covered by the generation).
        # Keyed invalidation via (count, max id) was rejected: deleting the
        # highest-id vector segment and inserting a fresh one can leave both
        # unchanged while the data differs.
        self._vec_cache: VectorTable | None = None
        self._vec_cache_key: tuple[int, int] | None = None
        self._vec_generation = 0

    # -- meta ---------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        self.db.commit()

    # -- files ----------------------------------------------------------------

    def upsert_file(self, rec: FileRec) -> int:
        """Insert/update the file row (state pending on fresh insert) and refresh
        its per-file 'name' segment + FTS row (name = filename, body = folder words,
        or slug+host for URLs). Returns the file id."""
        import time

        info_json = json.dumps(rec.info) if rec.info else None
        with self.db:
            cur = self.db.execute(
                """INSERT INTO files(path, source, kind, size, mtime, sha256, state,
                   capability, detail, info, extractor_version, chunker_version,
                   embed_id, indexed_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     source = excluded.source,
                     kind = excluded.kind,
                     size = excluded.size,
                     mtime = excluded.mtime,
                     sha256 = excluded.sha256,
                     state = excluded.state,
                     capability = excluded.capability,
                     detail = excluded.detail,
                     info = excluded.info,
                     extractor_version = excluded.extractor_version,
                     chunker_version = coalesce(excluded.chunker_version,
                                                files.chunker_version),
                     embed_id = coalesce(excluded.embed_id, files.embed_id),
                     indexed_at = excluded.indexed_at
                   RETURNING id""",
                (
                    rec.path, rec.source, rec.kind, rec.size, rec.mtime, rec.sha256,
                    rec.state, rec.capability, rec.detail, info_json,
                    rec.extractor_version, rec.chunker_version, rec.embed_id,
                    rec.indexed_at or time.time(),
                ),
            )
            # lastrowid is NOT reliable after DO UPDATE — RETURNING always is.
            fid = cur.fetchone()[0]
            # Refresh the name row so it always reflects the current record.
            # FTS5 does not participate in FK cascades: the doomed segments'
            # fts rows must go first, or a later segment-id reuse collides
            # with the orphaned row (sqlite3.IntegrityError, P0).
            self.db.execute(
                "DELETE FROM fts WHERE rowid IN "
                "(SELECT id FROM segments WHERE file = ? AND kind = 'name')",
                (fid,),
            )
            self.db.execute("DELETE FROM segments WHERE file = ? AND kind = 'name'", (fid,))
            seg = SegmentRec(
                kind="name",
                modality="none",
                excerpt=_display_name(rec.path),
                text_for_fts=_folder_words(rec.path, rec.source),
            )
            sid = self._insert_segment(fid, seg)
            self._insert_fts(sid, fid, seg, is_name=True)
        return int(fid)

    def mark_file(
        self,
        fid: int,
        *,
        state: str,
        capability: str | None,
        detail: str | None,
        info: dict | None,
        extractor_version: str | None,
        chunker_version: str | None = None,
        embed_id: str | None = None,
        content_date: float | None = None,
    ) -> None:
        """Finalize a file row. v2 identity plumbing: `chunker_version` /
        `embed_id` write the per-file identity at commit time (05 §4.2) — the
        pipeline passes the live values; embed_id stays NULL on vectorless
        files. Both columns are always written with the given values.
        `content_date` (05 §4.1c, C10) is written only when the extractor
        produced one (EXIF taken epoch); None keeps any existing value."""
        import time

        with self.db:
            self.db.execute(
                """UPDATE files SET state = ?, capability = ?, detail = ?, info = ?,
                   extractor_version = ?, chunker_version = ?, embed_id = ?,
                   content_date = coalesce(?, content_date),
                   indexed_at = ? WHERE id = ?""",
                (
                    state, capability, detail,
                    json.dumps(info) if info is not None else None,
                    extractor_version, chunker_version, embed_id,
                    content_date,
                    time.time(), fid,
                ),
            )

    def find_by_path(self, path: str) -> FileRec | None:
        row = self.db.execute(f"{_SELECT_FILE} WHERE path = ?", (path,)).fetchone()
        return _file_row(row) if row else None

    def get_file(self, fid: int) -> FileRec | None:
        row = self.db.execute(f"{_SELECT_FILE} WHERE id = ?", (fid,)).fetchone()
        return _file_row(row) if row else None

    def unchanged(self, path: str, size: int, sha: str) -> bool:
        """True when a done file with the same size and content hash is indexed."""
        row = self.db.execute(
            "SELECT size, sha256 FROM files WHERE path = ? AND state = 'done'",
            (path,),
        ).fetchone()
        return bool(row) and row[0] == size and row[1] == sha

    def _delete_segments_and_fts(self, fid: int) -> None:
        """Delete ALL of a file's segment rows AND their FTS rows. FTS5 is a
        virtual table: it takes no part in the files->segments FK cascade, so
        every file deletion must clear fts explicitly (orphaned rows both
        poison search and collide with reused segment ids, P0)."""
        self.db.execute(
            "DELETE FROM fts WHERE rowid IN (SELECT id FROM segments WHERE file = ?)",
            (fid,),
        )
        self.db.execute("DELETE FROM segments WHERE file = ?", (fid,))
        self._vec_generation += 1

    def delete_content_segments(self, fid: int) -> None:
        """Delete a file's non-name segments + their FTS rows, KEEPING the name
        row. P1-5: a file that re-ingests as `failed` must not keep its stale
        text searchable; search additionally excludes failed files."""
        self.db.execute(
            "DELETE FROM fts WHERE rowid IN "
            "(SELECT id FROM segments WHERE file = ? AND kind != 'name')",
            (fid,),
        )
        self.db.execute("DELETE FROM segments WHERE file = ? AND kind != 'name'", (fid,))
        self._vec_generation += 1

    def delete_file(self, fid: int) -> None:
        with self.db:
            self._delete_segments_and_fts(fid)
            self.db.execute("DELETE FROM files WHERE id = ?", (fid,))

    def prune_under(self, roots: list[str]) -> int:
        """Delete files outside `roots`; returns the count (V5d deletions)."""
        file_roots, url_roots = _split_roots(roots)
        removed = 0
        with self.db:
            for fid, path, source in self.db.execute(
                "SELECT id, path, source FROM files"
            ).fetchall():
                if _under_roots(path, source, file_roots, url_roots):
                    continue
                self._delete_segments_and_fts(fid)
                self.db.execute("DELETE FROM files WHERE id = ?", (fid,))
                removed += 1
        return removed

    def pending(self) -> list[FileRec]:
        rows = self.db.execute(f"{_SELECT_FILE} WHERE state = 'pending' ORDER BY id").fetchall()
        return [_file_row(r) for r in rows]

    def failures(self, limit: int = 10) -> list[FileRec]:
        rows = self.db.execute(
            f"{_SELECT_FILE} WHERE capability = 'failed' ORDER BY indexed_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_file_row(r) for r in rows]

    def reasons(self, capability: str) -> list[tuple[str, int]]:
        rows = self.db.execute(
            "SELECT coalesce(detail, 'unknown'), COUNT(*) FROM files "
            "WHERE capability = ? GROUP BY detail ORDER BY COUNT(*) DESC",
            (capability,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def counts(self) -> StatusCounts:
        out = StatusCounts()
        out.files, out.bytes = self.db.execute(
            "SELECT COUNT(*), coalesce(SUM(size), 0) FROM files"
        ).fetchone()
        out.segments = self.db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
        out.vectors = self.db.execute(
            "SELECT COUNT(*) FROM segments WHERE vec IS NOT NULL"
        ).fetchone()[0]
        for kind, capability, n in self.db.execute(
            "SELECT kind, capability, COUNT(*) FROM files GROUP BY kind, capability"
        ).fetchall():
            out.by_kind.setdefault(kind, {})[capability or "pending"] = n
        for capability, n in self.db.execute(
            "SELECT coalesce(capability, 'pending'), COUNT(*) FROM files GROUP BY capability"
        ).fetchall():
            out.totals[capability] = n
        for state, n in self.db.execute(
            "SELECT state, COUNT(*) FROM files GROUP BY state"
        ).fetchall():
            out.states[state] = n
        return out

    # -- segments + FTS -------------------------------------------------------

    def _insert_segment(self, fid: int, seg: SegmentRec) -> int:
        vec_blob = pack_fp16(seg.vec) if seg.vec is not None else None
        cur = self.db.execute(
            """INSERT INTO segments(file, kind, modality, loc, loc_end, excerpt, vec)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (fid, seg.kind, seg.modality, seg.loc, seg.loc_end, seg.excerpt, vec_blob),
        )
        self._vec_generation += 1
        return int(cur.lastrowid)

    def _insert_fts(self, segment_id: int, fid: int, seg: SegmentRec, is_name: bool = False) -> None:
        """fts(name, body, original) row, rowid == segments.id.

        name column: the filename (URL slug for webpages) on every row, so name
        matches rank a file's rows together (bm25 weight 3.0).
        body: NFC-normalized text. original: the text as written when
        normalization changed it (DigUp SearchText pattern).
        """
        if seg.text_for_fts is None:
            return
        name_col = _display_name(self.db.execute(
            "SELECT path, source FROM files WHERE id = ?", (fid,)
        ).fetchone())
        from .util import normalize_text

        body, changed = normalize_text(seg.text_for_fts)
        original = seg.original_fts
        if original is None and changed:
            original = seg.text_for_fts
        self.db.execute(
            "INSERT INTO fts(rowid, name, body, original) VALUES(?, ?, ?, ?)",
            (segment_id, name_col, body, original),
        )

    def fts_text(
        self,
        fid: int,
        kind: str,
        loc: float | None,
        text: str,
        vec_ok: bool,
        *,
        loc_end: float | None = None,
        excerpt_text: str | None = None,
        original: str | None = None,
        vec: np.ndarray | None = None,
        modality: str | None = None,
    ) -> int:
        """Insert ONE content segment row + its FTS row; returns the segment id.

        DEVIATION from MODULE_MAP signature `fts_text(fid, kind, loc, text, vec_ok)`:
        MODULE_MAP omits how the caller learns the new segment id (fts.rowid must
        equal segments.id) and how a vector attaches. This version returns the
        segment id and accepts optional loc_end/excerpt/original/vec/modality;
        `vec_ok=False` forces the row vectorless (digits-only text, thin PDF page).
        """
        seg = SegmentRec(
            kind=kind,
            modality=modality or ("text" if vec_ok else "none"),
            loc=loc,
            loc_end=loc_end,
            excerpt=excerpt_text,
            text_for_fts=text,
            original_fts=original,
            vec=vec if vec_ok else None,
        )
        with self.db:
            sid = self._insert_segment(fid, seg)
            self._insert_fts(sid, fid, seg)
        return sid

    def replace_segments(self, fid: int, segs: list[SegmentRec]) -> None:
        """ONE transaction: delete the file's old segments (and FTS rows) and
        insert the new set. A failure anywhere rolls back to the old segments."""
        with self.db:
            old_ids = [
                r[0] for r in self.db.execute(
                    "SELECT id FROM segments WHERE file = ?", (fid,)
                ).fetchall()
            ]
            self.db.executemany(
                "DELETE FROM fts WHERE rowid = ?", [(i,) for i in old_ids]
            )
            self.db.execute("DELETE FROM segments WHERE file = ?", (fid,))
            for seg in segs:
                sid = self._insert_segment(fid, seg)
                self._insert_fts(sid, fid, seg, is_name=(seg.kind == "name"))

    def load_vectors(self) -> VectorTable:
        """All vectors as an (N, 768) float32 matrix, fp16->f32 in blocks.

        Cached per Store instance (storage critique 03 F2): a fresh load reads
        every blob + rebuilds the Python lists — 102 ms measured at 49k vectors
        — which search used to pay on EVERY query. Invalidated by the segment
        write-generation counter (bumped by _insert_segment /
        _delete_segments_and_fts / delete_content_segments — every Store path
        that touches segments) combined with PRAGMA data_version (changes when
        a DIFFERENT connection commits). The returned table is shared between
        callers: treat it as immutable — never mutate ids/matrix in place.
        """
        data_version = int(self.db.execute("PRAGMA data_version").fetchone()[0])
        key = (self._vec_generation, data_version)
        if self._vec_cache is not None and key == self._vec_cache_key:
            return self._vec_cache
        ids: list[int] = []
        file_ids: list[int] = []
        modalities: list[str] = []
        matrix_blocks: list[np.ndarray] = []
        cur = self.db.execute(
            "SELECT id, file, modality, vec FROM segments WHERE vec IS NOT NULL ORDER BY id"
        )
        blob_bytes = 768 * 2  # fp16 LE BLOB contract (D7)
        while True:
            batch = cur.fetchmany(BLOCK_ROWS)
            if not batch:
                break
            # batch unpack (2.4x the per-row loop, 03 F7): join the block's
            # blobs into one buffer, one frombuffer + one astype for all of
            # them; a corrupt-size blob is skipped exactly as before, with
            # ids/files/modalities kept aligned to the kept rows.
            rows = [
                (seg_id, file_id, modality, blob)
                for seg_id, file_id, modality, blob in batch
                if blob is not None and len(blob) == blob_bytes
            ]
            if not rows:
                continue
            raw = np.frombuffer(b"".join(r[3] for r in rows), dtype="<f2")
            matrix_blocks.append(
                raw.astype(np.float32).reshape(len(rows), 768)
            )
            ids.extend(r[0] for r in rows)
            file_ids.extend(r[1] for r in rows)
            modalities.extend(r[2] for r in rows)
        if matrix_blocks:
            matrix = np.concatenate(matrix_blocks) if len(matrix_blocks) > 1 \
                else matrix_blocks[0]
        else:
            matrix = np.empty((0, 768), dtype=np.float32)
        table = VectorTable(
            ids=ids,
            file_ids=file_ids,
            modalities=modalities,
            matrix=matrix,
        )
        self._vec_cache = table
        self._vec_cache_key = key
        return table

    def segment_row(self, segment_id: int) -> tuple[str, float | None, float | None, str | None]:
        row = self.db.execute(
            "SELECT kind, loc, loc_end, excerpt FROM segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
        return (row[0], row[1], row[2], row[3]) if row else ("chunk", None, None, None)

    def segment_text(self, segment_id: int) -> str | None:
        """The segment's text as written: coalesce(original, body) from its
        fts row (DigUp Searcher.swift:354-358 pattern) — «» snippet windows
        are built from THIS, never from tokenizer-normalized text. None when
        the segment has no fts row (e.g. a pure image vector)."""
        row = self.db.execute(
            "SELECT coalesce(original, body) FROM fts WHERE rowid = ?",
            (segment_id,),
        ).fetchone()
        return row[0] if row else None

    # -- keyword search -------------------------------------------------------

    def search_fts(self, match: str, limit: int = 1000) -> list[FtsRow]:
        """FTS5 OR-match, best first. bm25 is lower-is-better: ORDER BY ascending."""
        rows = self.db.execute(
            """SELECT fts.rowid, s.file, fts.name, fts.body, fts.original,
                      bm25(fts, 3.0, 1.0) AS rank
               FROM fts JOIN segments s ON s.id = fts.rowid
               WHERE fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (match, limit),
        ).fetchall()
        return [
            FtsRow(
                segment_id=r[0], file_id=r[1], name=r[2], body=r[3],
                original=r[4], rank=r[5],
            )
            for r in rows
        ]

    def coverage(self, patterns: list[str], file_ids: list[int]) -> dict[int, float]:
        """Share of `patterns` found anywhere in each file's rows (0..1)."""
        if not patterns or not file_ids:
            return {}
        placeholders = ",".join("?" for _ in file_ids)
        out: dict[int, float] = {}
        for pattern in patterns:
            rows = self.db.execute(
                f"""SELECT DISTINCT s.file FROM fts JOIN segments s ON s.id = fts.rowid
                    WHERE fts MATCH ? AND s.file IN ({placeholders})""",
                (pattern, *file_ids),
            ).fetchall()
            for (fid,) in rows:
                out[fid] = out.get(fid, 0.0) + 1.0 / len(patterns)
        return out

    # -- cards ------------------------------------------------------------------

    def cards(self) -> list[CardRow]:
        rows = self.db.execute(
            "SELECT id, title_terms, size, n_files, fingerprint_id, distill_algo, created_at "
            "FROM cards ORDER BY size DESC, id"
        ).fetchall()
        return [CardRow(*r) for r in rows]

    def save_cards(self, cards: list) -> None:
        """Store distill cards. Each card needs attributes title_terms (JSON str),
        size, n_files, fingerprint_id, distill_algo, created_at, and optionally
        chunk_weights: list[(segment_id, weight)] for card_chunks."""
        import time

        with self.db:
            # One card generation is kept (cards pin a fingerprint_id).
            self.db.execute("DELETE FROM card_chunks")
            self.db.execute("DELETE FROM cards")
            for card in cards:
                cur = self.db.execute(
                    """INSERT INTO cards(title_terms, size, n_files, fingerprint_id,
                       distill_algo, created_at) VALUES(?, ?, ?, ?, ?, ?)""",
                    (
                        card.title_terms,
                        card.size,
                        card.n_files,
                        card.fingerprint_id,
                        card.distill_algo,
                        getattr(card, "created_at", None) or time.time(),
                    ),
                )
                for chunk_id, weight in getattr(card, "chunk_weights", []) or []:
                    self.db.execute(
                        "INSERT INTO card_chunks(card, chunk, weight) VALUES(?, ?, ?)",
                        (cur.lastrowid, chunk_id, weight),
                    )

    def close(self) -> None:
        try:
            self.db.commit()
        except sqlite3.Error:
            pass
        self.db.close()


# -- helpers --------------------------------------------------------------------


def _display_name(path_row) -> str:
    """FTS name column: filename, or URL slug+host for webpages."""
    if isinstance(path_row, str):
        path, source = path_row, "file"
    else:
        path, source = path_row[0], path_row[1]
    if source == "url":
        from urllib.parse import urlsplit

        parts = urlsplit(path)
        slug = [t for t in (parts.netloc, *parts.path.split("/")) if t]
        return " ".join(slug)
    return Path(path).name


def _folder_words(path: str, source: str) -> str:
    """FTS body for the name row: folder-path words, or host for URLs."""
    if source == "url":
        from urllib.parse import urlsplit

        return urlsplit(path).netloc.lower()
    import re

    parts = [p for p in Path(path).parts[:-1] if p not in ("/",)]
    words = re.findall(r"[a-z0-9]+", " ".join(parts).lower())
    return " ".join(words)


def _split_roots(roots: list[str]) -> tuple[list[str], list[str]]:
    file_roots: list[str] = []
    url_roots: list[str] = []
    for root in roots:
        if root.startswith(("http://", "https://")):
            url_roots.append(root.rstrip("/"))
        else:
            file_roots.append(str(Path(root)))
    return file_roots, url_roots


def _under_roots(path: str, source: str, file_roots: list[str], url_roots: list[str]) -> bool:
    if source == "url":
        return any(path == r or path.startswith(r + "/") for r in url_roots)
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in file_roots)
