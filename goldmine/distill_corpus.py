"""SQL-scoped distill preflight and bounded stored-vector decoding."""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import numpy as np

from .deadline import check_deadline


def scope_sql(scope):
    clauses, values = ["s.vec IS NOT NULL", "length(s.vec)=1536"], []
    if scope.kinds is not None:
        clauses.append("f.kind IN (SELECT value FROM json_each(?))")
        import json

        values.append(json.dumps(sorted(scope.kinds)))
    if scope.under:
        prefixes = []
        for root in scope.under:
            root = str(Path(root).expanduser()) if not root.startswith(("http://", "https://")) else root
            root = root.rstrip("/")
            prefixes.append("(f.path=? OR substr(f.path,1,?)=?)")
            values.extend((root, len(root) + 1, root + "/"))
        clauses.append("(" + " OR ".join(prefixes) + ")")
    if scope.after is not None:
        clauses.append("coalesce(f.mtime,0)>=?")
        values.append(scope.after)
    return " AND ".join(clauses), values


def scoped_files(store, scope):
    where, values = scope_sql(scope)
    return store.db.execute(
        "SELECT DISTINCT f.path,f.sha256 FROM files f JOIN segments s ON s.file=f.id "
        f"WHERE {where} ORDER BY f.path", values,
    ).fetchall()


def load_corpus(store, scope, *, max_chunks=None):
    from .distill import CItem, DistillRefused

    with nullcontext() if store.db.in_transaction else store.read_snapshot():
        where, values = scope_sql(scope)
        count = store.db.execute(
            f"SELECT count(*) FROM segments s JOIN files f ON f.id=s.file WHERE {where}", values,
        ).fetchone()[0]
        if max_chunks is not None:
            if count > max_chunks:
                raise DistillRefused(f"distill corpus has {count:,} vectorized chunks, above the "
                                     f"--max-chunks cap of {max_chunks:,} — refusing, not truncating")
            if count < 2:
                raise DistillRefused(f"distill needs at least 2 vectorized chunks (found {count})")
        cursor = store.db.execute(
            "SELECT s.id,s.file,f.path,f.kind,s.loc,s.loc_end,coalesce(t.original,t.body,''),s.vec "
            "FROM segments s JOIN files f ON f.id=s.file LEFT JOIN fts t ON t.rowid=s.id "
            f"WHERE {where} ORDER BY s.file,s.id", values,
        )
        items = []
        while rows := cursor.fetchmany(512):
            check_deadline()
            matrix = np.frombuffer(b"".join(row[7] for row in rows), dtype="<f2").astype(np.float32).reshape(len(rows), 768)
            for row, vector in zip(rows, matrix):
                sid, fid, path, kind, loc, loc_end, text, _ = row
                items.append(CItem(sid=sid, fid=fid, vec=vector, text=text, path=path,
                                   kind=kind, loc=loc, loc_end=loc_end))
        return items
