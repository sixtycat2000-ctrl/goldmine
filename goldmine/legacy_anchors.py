"""Bounded legacy source candidates; shared C1 verification decides bindings."""
from __future__ import annotations

import re

from .deadline import check_deadline
from .lexical import fts_quote, normalize
from .metadata_prelookup import whole_metadata_predicate


def patterns(text):
    expressions = []
    for spelling in (text, normalize(text)):
        words = list(dict.fromkeys(re.findall(r"[^\W_]+", spelling, re.UNICODE)))
        if words:
            expressions.append(" AND ".join(fts_quote(word) for word in words))
    return tuple(dict.fromkeys(expressions))


class LegacyCandidates:
    def __init__(self, resolver):
        self.r = resolver
        self.completed = set()

    def lookup(self, anchor, *, whole_metadata, body_only=False):
        r, ids = self.r, []
        if not body_only:
            for pattern in patterns(anchor.raw):
                sql = ("SELECT DISTINCT s.id FROM fts JOIN segments s ON s.id=fts.rowid "
                       "LEFT JOIN files f ON f.id=s.file WHERE fts MATCH ? AND s.kind='name'")
                values = ["name : (" + pattern + ") OR body : (" + pattern + ")"]
                if whole_metadata:
                    with whole_metadata_predicate(r.db, r.allowed, derived=False) as function:
                        sql += f" AND {function}(s.id,s.file,s.kind,f.id,f.path,f.info,NULL,NULL,NULL,NULL,NULL,?)=1"
                        ids.extend(r._candidate_ids(sql + " ORDER BY s.id", [*values, anchor.raw]))
                else:
                    ids.extend(r._candidate_ids(sql + " ORDER BY s.id", values))
            # Binary equality is indexed and cannot turn a fragment into a path.
            ids.extend(r._candidate_ids(
                "SELECT s.id FROM files f JOIN segments s ON s.file=f.id "
                "WHERE f.path=? AND s.kind='name' ORDER BY s.id", [anchor.raw]))
        if not whole_metadata:
            for pattern in patterns(anchor.raw):
                ids.extend(r.body_ids("body : (" + pattern + ")"))
        return self.fields(r.read(ids))

    @staticmethod
    def fields(rows):
        return {row.id: {"path", "basename", "title"} if row.kind == "name" else {"body"}
                for row in rows}

    def enumerate(self, file_id=None):
        """Read at most2000 unique sources across every route, with a sentinel."""
        r = self.r
        if None not in self.completed and file_id not in self.completed:
            last = 0
            while True:
                check_deadline()
                suffix = " AND file=?" if file_id is not None else ""
                values = (last, file_id) if file_id is not None else (last,)
                pairs = r.execute("SELECT id,file FROM segments WHERE id>?" + suffix + " ORDER BY id LIMIT 400", values).fetchall()
                if not pairs:
                    break
                last = pairs[-1][0]
                ids = [sid for sid, fid in pairs if fid in r.allowed]
                # read() shares the request-wide unique budget with FTS,
                # metadata, other anchors and remainder witnesses.
                r.read(ids)
            self.completed.add(file_id)
        return [row for row in r.rows.values() if row.file in r.allowed
                and (file_id is None or row.file == file_id)]
