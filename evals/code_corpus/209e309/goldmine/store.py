"""SQLite store: schema (ARCHITECTURE §6 DDL, verbatim), all SQL, fp16 blobs.

Single writer, one connection, main thread only. Vectors live beside their rows so
a crash can never desync them. Re-ingest replaces a file's segments in ONE
transaction (V5d atomic segment replacement).

FTS direction note (V3, normative): SQLite bm25() is LOWER-is-better. We only ever
ORDER BY bm25(...) ascending (best first) and rank off that order; the raw bm25
value is never added into a higher-is-better score.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import migrations
from .util import pack_fp16, unpack_fp16
from .storage_connections import connect_index, open_index, read_snapshot  # connect_index compatibility
from .storage_writes import writer_scope

BLOCK_ROWS = 8192  # fp16 -> f32 conversion block (DigUp pattern)


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
    ids: Sequence[int]
    file_ids: Sequence[int]
    modalities: Sequence[str]
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
    """The v2 cards row as `cards()` returns it (active by default)."""

    id: int
    run_id: str
    view_id: str
    title: str
    title_kind: str
    terms: str            # JSON array string
    size: int
    n_files: int
    status: str
    fingerprint_id: str
    distill_algo: str
    created_at: float


@dataclass
class ViewCard:
    """A card of a view with its decoded centroid + member chunk ids and the
    serialized content — the incremental anchor (§4.3). Members and weights
    are the ORIGINAL state loaded ONCE before any mutation (F8a/F8d)."""

    id: int
    run_id: str
    title: str
    title_kind: str
    terms: str                 # JSON array string (carried over verbatim)
    excerpts: str              # JSON string (carried over verbatim)
    status: str
    size: int
    n_files: int
    centroid: np.ndarray | None
    fingerprint_id: str
    distill_algo: str
    created_at: float
    members: list[int] = field(default_factory=list)
    weights: list[tuple[int, float]] = field(default_factory=list)


@dataclass
class DistillView:
    name: str
    scope_json: str
    params_json: str
    created_at: float


@dataclass
class DissolveResult:
    """dissolve_pass outcome (§4.4): `dissolved` cards fell below min_members
    (rows kept as history); `reentered` chunks are the dissolved cards'
    surviving members — they re-enter the run's remainder and count toward
    the F5 reclustered stat."""

    dissolved: int = 0
    reentered: int = 0


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



class Store:
    SCHEMA_VERSION = migrations.TARGET  # single source: migrations.py owns the ladder

    def __init__(self, path: Path, *, ro: bool = False, timeout_s: float = 60.0):
        self.path = Path(path)
        self.ro = ro
        self.db = open_index(self.path, ro=ro, timeout_s=timeout_s)
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
        self._vec_cache_key: tuple | None = None
        self._vec_generation = 0

    def read_snapshot(self):
        return read_snapshot(self.db)

    def write_transaction(self):
        return writer_scope(self.db)

    # -- meta ---------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.write_transaction():
            if value is None:
                self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self.db.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    # -- files ----------------------------------------------------------------

    def upsert_file(self, rec: FileRec) -> int:
        """Insert/update the file row (state pending on fresh insert) and refresh
        its per-file 'name' segment + FTS row (name = filename, body = folder words,
        or slug+host for URLs). Returns the file id."""
        import time

        info_json = json.dumps(rec.info) if rec.info else None
        with self.write_transaction():
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
        replace_content_date: bool = False,
    ) -> None:
        """Finalize a file row. v2 identity plumbing: `chunker_version` /
        `embed_id` write the per-file identity at commit time (05 §4.2) — the
        pipeline passes the live values; embed_id stays NULL on vectorless
        files. Both columns are always written with the given values.
        `content_date` (05 §4.1c, C10) is written only when the extractor
        produced one (EXIF taken epoch); None keeps any existing value unless
        replace_content_date explicitly marks authoritative image metadata,
        where an absent capture date must clear a previous edit timestamp."""
        import time

        with self.write_transaction():
            self.db.execute(
                """UPDATE files SET state = ?, capability = ?, detail = ?, info = ?,
                   extractor_version = ?, chunker_version = ?, embed_id = ?,
                   content_date = CASE WHEN ? THEN ? ELSE coalesce(?, content_date) END,
                   indexed_at = ? WHERE id = ?""",
                (
                    state, capability, detail,
                    json.dumps(info) if info is not None else None,
                    extractor_version, chunker_version, embed_id,
                    replace_content_date, content_date, content_date,
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
        with self.write_transaction():
            self.db.execute(
                "DELETE FROM fts WHERE rowid IN "
                "(SELECT id FROM segments WHERE file = ? AND kind != 'name')",
                (fid,),
            )
            self.db.execute("DELETE FROM segments WHERE file = ? AND kind != 'name'", (fid,))
            self._vec_generation += 1

    def delete_file(self, fid: int) -> None:
        with self.write_transaction():
            self._delete_segments_and_fts(fid)
            self.db.execute("DELETE FROM files WHERE id = ?", (fid,))

    def prune_under(self, roots: list[str]) -> int:
        """Delete files outside `roots`; returns the count (V5d deletions)."""
        file_roots, url_roots = _split_roots(roots)
        removed = 0
        with self.write_transaction():
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
        from .lexical import normalize
        from .lexical_store import insert_derived

        body = normalize(seg.text_for_fts)
        original = seg.original_fts
        if original is None and body != seg.text_for_fts:
            original = seg.text_for_fts
        self.db.execute(
            "INSERT INTO fts(rowid, name, body, original) VALUES(?, ?, ?, ?)",
            (segment_id, normalize(name_col), body, original),
        )
        insert_derived(self.db, segment_id, fid, seg.kind, name_col, original if original is not None else body)

    def reindex_lexical(self) -> dict:
        """Explicit stored-row rebuild: no extraction, source reads or models."""
        from .lexical_store import rebuild_derived

        with self.write_transaction():
            return rebuild_derived(self.db, exact=self.SCHEMA_VERSION >= 6)

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
        with self.write_transaction():
            sid = self._insert_segment(fid, seg)
            self._insert_fts(sid, fid, seg)
        return sid

    def replace_segments(self, fid: int, segs: list[SegmentRec]) -> None:
        """ONE transaction: delete the file's old segments (and FTS rows) and
        insert the new set. A failure anywhere rolls back to the old segments."""
        with self.write_transaction():
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
        """Immutable vectors from memory, an explicitly enabled pack, or SQLite.

        The optional persistent cache is built only by explicit maintenance.
        Its epoch and file checks share the caller's snapshot. Any cache defect
        falls back to the SQLite fp16 truth; request deadlines still propagate.
        """
        with nullcontext() if self.db.in_transaction else self.read_snapshot():
            data_version = int(self.db.execute("PRAGMA data_version").fetchone()[0])
            enabled = self.meta("vector_cache_enabled") == "1"
            key = (self._vec_generation, data_version, self.db.total_changes,
                   self.meta("vector_epoch"), enabled)
            if self._vec_cache is not None and key == self._vec_cache_key:
                return self._vec_cache
            table = None
            if enabled:
                from .vector_cache import load_vector_cache

                table = load_vector_cache(self)
            if table is None:
                table = self._load_vectors_sqlite()
            self._vec_cache = table
            self._vec_cache_key = key
            return table

    def _load_vectors_sqlite(self) -> VectorTable:
        """SQLite truth: block fp16 conversion with aligned corrupt-row skips."""
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
        from .read_source import fts_projection

        text_sql, _ = fts_projection(self.db)
        row = self.db.execute(
            f"SELECT {text_sql} FROM fts WHERE rowid = ?",
            (segment_id,),
        ).fetchone()
        return row[0] if row else None

    # -- keyword search -------------------------------------------------------

    def search_fts(self, match: str, limit: int = 1000) -> list[FtsRow]:
        """FTS5 OR-match, best first. bm25 is lower-is-better: ORDER BY ascending."""
        from .read_source import fts_projection

        _, original_sql = fts_projection(self.db, "fts")
        rows = self.db.execute(
            f"""SELECT fts.rowid, s.file, fts.name, fts.body, {original_sql},
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

    _CARD_COLUMNS = (
        "id, run_id, view_id, title, title_kind, terms, size, n_files, status, "
        "fingerprint_id, distill_algo, created_at"
    )

    def cards(self, *, status: str = "active") -> list[CardRow]:
        """Cards of the index, size desc (v1 order kept). Default lists ACTIVE
        cards only — dissolved rows are history, headers are section rows."""
        if status:
            rows = self.db.execute(
                f"SELECT {self._CARD_COLUMNS} FROM cards WHERE status = ? "
                "ORDER BY size DESC, id",
                (status,),
            ).fetchall()
        else:
            rows = self.db.execute(
                f"SELECT {self._CARD_COLUMNS} FROM cards ORDER BY size DESC, id"
            ).fetchall()
        return [CardRow(*r) for r in rows]

    def save_run(self, run: dict, cards: list) -> None:
        """Persist one distill run (§3.4): the distill_runs row + its cards +
        card_chunks in ONE transaction, replacing the view's live cards
        (per-view replace — other views' cards are untouched). `run` carries
        id/algo/tau/tau_source/scope_json/corpus_hash/mode/stats; cards need
        run_id/view_id/title/title_kind/terms/excerpts/size/n_files/status/
        parent/centroid/fingerprint_id/distill_algo/chunk_weights.

        F3: a repeat identical run (same content tuple → same run id) updates
        the same history row in place (ON CONFLICT DO UPDATE) instead of
        raising IntegrityError — history is append-only up to
        identical-content repeats. Dissolved rows are never deleted here:
        they are history (§4.4)."""
        import time as _time

        now = _time.time()
        with self.write_transaction():
            self.db.execute(
                """INSERT INTO distill_runs(id, algo, tau, tau_source, scope_json,
                   corpus_hash, mode, stats_json, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     stats_json = excluded.stats_json,
                     created_at = excluded.created_at""",
                (
                    run["id"], run["algo"], float(run["tau"]), run["tau_source"],
                    run["scope_json"], run["corpus_hash"], run["mode"],
                    json.dumps(run["stats"], ensure_ascii=False), now,
                ),
            )
            doomed = self.db.execute(
                "SELECT id FROM cards WHERE view_id = ? AND status != 'dissolved'",
                (run.get("view_id", ""),),
            ).fetchall()
            if doomed:
                ph = ",".join("?" for _ in doomed)
                self.db.execute(
                    f"DELETE FROM card_chunks WHERE card IN ({ph})",
                    tuple(r[0] for r in doomed),
                )
                self.db.execute(
                    "DELETE FROM cards WHERE view_id = ? AND status != 'dissolved'",
                    (run.get("view_id", ""),),
                )
            rowids: dict[int, int] = {}
            for card in cards:
                centroid_blob = None
                vec = getattr(card, "centroid", None)
                if vec is not None:
                    centroid_blob = pack_fp16(vec)
                cur = self.db.execute(
                    """INSERT INTO cards(run_id, view_id, title, title_kind, terms,
                       excerpts, size, n_files, parent_id, centroid, status,
                       fingerprint_id, distill_algo, created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                    (
                        card.run_id, card.view_id, card.title, card.title_kind,
                        card.terms_json, card.excerpts_json, card.size,
                        card.n_files, centroid_blob, card.status,
                        card.fingerprint_id, card.distill_algo,
                        getattr(card, "created_at", None) or now, now,
                    ),
                )
                rowids[id(card)] = int(cur.lastrowid)
                for chunk_id, weight in getattr(card, "chunk_weights", []) or []:
                    self.db.execute(
                        "INSERT INTO card_chunks(card, chunk, weight) VALUES(?, ?, ?)",
                        (cur.lastrowid, chunk_id, weight),
                    )
            # parent linkage second pass: header rows must exist first (FK on)
            for card in cards:
                if card.parent is not None:
                    self.db.execute(
                        "UPDATE cards SET parent_id = ? WHERE id = ?",
                        (rowids[id(card.parent)], rowids[id(card)]),
                    )

    def load_view_cards(self, view_id: str, *, status: str = "active") -> list[ViewCard]:
        """A view's live cards with decoded centroids + member chunk ids and
        the full serialized content (§4.3 step 1 — the snapshot taken BEFORE
        any mutation, F8a)."""
        rows = self.db.execute(
            """SELECT id, run_id, title, title_kind, terms, excerpts, status,
               size, n_files, centroid, fingerprint_id, distill_algo, created_at
               FROM cards WHERE view_id = ? AND status = ? ORDER BY id""",
            (view_id, status),
        ).fetchall()
        cards = [
            ViewCard(
                id=r[0], run_id=r[1], title=r[2], title_kind=r[3], terms=r[4],
                excerpts=r[5], status=r[6], size=r[7], n_files=r[8],
                centroid=unpack_fp16(r[9]) if r[9] is not None else None,
                fingerprint_id=r[10], distill_algo=r[11], created_at=r[12],
            )
            for r in rows
        ]
        if cards:
            ph = ",".join("?" for _ in cards)
            by_id = {c.id: c for c in cards}
            for card_id, chunk_id, weight in self.db.execute(
                f"""SELECT card, chunk, weight FROM card_chunks
                    WHERE card IN ({ph}) ORDER BY card, chunk""",
                tuple(c.id for c in cards),
            ).fetchall():
                vc = by_id.get(card_id)
                if vc is not None:
                    vc.members.append(chunk_id)
                    vc.weights.append((chunk_id, weight))
        return cards

    def view_member_chunk_ids(self, view_id: str) -> set[int]:
        """Segment ids held by the view's ACTIVE cards (the 'new chunks'
        complement, §4.3 step 2)."""
        rows = self.db.execute(
            """SELECT cc.chunk FROM card_chunks cc
               JOIN cards c ON c.id = cc.card
               WHERE c.view_id = ? AND c.status = 'active'""",
            (view_id,),
        ).fetchall()
        return {r[0] for r in rows}

    def dissolve_pass(self, view_id: str, min_members: int) -> DissolveResult:
        """§4.4: the tombstone sweep + below-min dissolve for one view, in
        ONE transaction. (1) card_chunks rows pointing at deleted segments go
        (the v2 DDL keeps the chunk FK CASCADE-free on purpose — this sweep
        owns removals); (2) every affected card's size/n_files/centroid is
        recomputed from its surviving members; (3) a card below min_members
        becomes status='dissolved' — its ROW survives as history/diffing
        (§4.4) while its surviving members' card_chunks rows are released so
        they re-enter the run's remainder. Returns the DissolveResult counts.

        Recompute reads the Store's decoded fp16 vectors — the same decode
        path clustering used, so decisions stay self-consistent (06 §6)."""
        import time

        with self.write_transaction():
            self.db.execute(
                """DELETE FROM card_chunks WHERE card IN
                   (SELECT id FROM cards WHERE view_id = ? AND status = 'active')
                   AND chunk NOT IN (SELECT id FROM segments)""",
                (view_id,),
            )
            rows = self.db.execute(
                """SELECT c.id, cc.chunk FROM cards c
                   JOIN card_chunks cc ON cc.card = c.id
                   WHERE c.view_id = ? AND c.status = 'active'""",
                (view_id,),
            ).fetchall()
            members_by_card: dict[int, list[int]] = {}
            for card_id, chunk_id in rows:
                members_by_card.setdefault(card_id, []).append(chunk_id)
            result = DissolveResult()
            if not members_by_card:
                return result
            table = self.load_vectors()
            vec_by_sid = dict(zip(table.ids, table.matrix))
            for card_id, members in members_by_card.items():
                n = len(members)
                if n < min_members:
                    self.db.execute(
                        "DELETE FROM card_chunks WHERE card = ?", (card_id,)
                    )
                    self.db.execute(
                        "UPDATE cards SET status = 'dissolved', size = ?, "
                        "updated_at = ? WHERE id = ?",
                        (n, time.time(), card_id),
                    )
                    result.dissolved += 1
                    result.reentered += n
                    continue
                file_ids = {
                    r[0] for r in self.db.execute(
                        f"""SELECT DISTINCT file FROM segments
                            WHERE id IN ({",".join("?" for _ in members)})""",
                        tuple(members),
                    ).fetchall()
                }
                vecs = [vec_by_sid[sid] for sid in members if sid in vec_by_sid]
                centroid_blob = None
                if vecs:
                    mean = np.mean(np.stack(vecs), axis=0)
                    norm = float(np.linalg.norm(mean))
                    if norm > 0.0:
                        mean = mean / norm
                    centroid_blob = pack_fp16(mean)
                self.db.execute(
                    """UPDATE cards SET size = ?, n_files = ?, centroid = ?,
                       updated_at = ? WHERE id = ?""",
                    (n, len(file_ids), centroid_blob, time.time(), card_id),
                )
            return result

    def latest_run(self, view_id: str) -> dict | None:
        """The view's live run row (created_at desc, id asc tiebreak — §4.1)
        with stats_json parsed, or None. Runs link to their view through the
        snapshotted stats_json.view_id (the DDL keeps distill_runs scope-keyed,
        not view-keyed)."""
        for row in self.db.execute(
            """SELECT id, algo, tau, tau_source, scope_json, corpus_hash, mode,
               stats_json FROM distill_runs ORDER BY created_at DESC, id ASC"""
        ).fetchall():
            try:
                stats = json.loads(row[7])
            except json.JSONDecodeError:
                continue
            if isinstance(stats, dict) and stats.get("view_id") == view_id:
                return {
                    "id": row[0], "algo": row[1], "tau": row[2],
                    "tau_source": row[3], "scope_json": row[4],
                    "corpus_hash": row[5], "mode": row[6], "stats": stats,
                }
        return None

    # -- distill views -----------------------------------------------------------

    def upsert_view(self, name: str, scope_json: str, params_json: str) -> None:
        import time as _time

        with self.write_transaction():
            self.db.execute(
                """INSERT INTO distill_views(name, scope_json, params_json, created_at)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     scope_json = excluded.scope_json,
                     params_json = excluded.params_json""",
                (name, scope_json, params_json, _time.time()),
            )

    def get_view(self, name: str) -> DistillView | None:
        row = self.db.execute(
            "SELECT name, scope_json, params_json, created_at FROM distill_views "
            "WHERE name = ?",
            (name,),
        ).fetchone()
        return DistillView(*row) if row else None

    def meta_note(self) -> str | None:
        """The cards-v2 rebuild announcement (§3.3), if present."""
        return self.meta("cards_v2_rebuild_note")

    def cards_empty(self) -> bool:
        row = self.db.execute(
            "SELECT COUNT(*) FROM cards WHERE status = 'active'"
        ).fetchone()
        return row[0] == 0

    def close(self) -> None:
        try:
            if self.ro:
                self.db.rollback()
            else:
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
