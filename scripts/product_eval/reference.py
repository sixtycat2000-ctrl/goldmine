"""Narrow read-only compatibility for the pinned original schema-v4 build.

The source checkout is verified at the original commit by runner.py before this
adapter is used. These adapters add evidence sidecars; they do not change its
search, expansion, budget selection, or rendered pack bytes.
"""

from pathlib import Path
import sqlite3

from answer_eval.protocol import EvaluationError


def open_evaluation_store(path, *, original=False):
    from goldmine.store import Store

    if not original:
        return Store(path, ro=True)
    if Store.SCHEMA_VERSION != 4:
        raise EvaluationError("original-store adapter requires pinned schema-v4 production")
    # The original constructor has no ro keyword and runs writable DDL even for
    # a current index. Avoid it rather than modifying the historical source.
    store = Store.__new__(Store)
    store.path = Path(path)
    store.db = sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    store.db.execute("PRAGMA query_only=ON")
    store.db.execute("PRAGMA busy_timeout=60000")
    store._vec_cache = None
    store._vec_cache_key = None
    store._vec_generation = 0
    if store.db.execute("PRAGMA user_version").fetchone()[0] != 4:
        store.db.close()
        raise EvaluationError("original evaluation needs its separately prepared schema-v4 index")
    return store


def add_original_segment_sidecars(store, settings, hits, meta):
    """Recreate the original collection only to identify emitted source rows."""
    from goldmine.pack import NeighborIndex, collect_records, merge_parts

    collected, _, _ = collect_records(NeighborIndex(store), hits, settings.context)
    by_number = {record.n: record for record in collected}
    for record in meta["records"]:
        source = by_number.get(record["n"])
        if source is None or any(record[key] != getattr(source.hit, key) for key in ("path", "kind", "loc", "loc_end")):
            raise EvaluationError("original pack record does not match its unmodified collector")
        parts = source.parts
        if record["text"] != source.text:
            slim = [part for part in parts if part["role"] == "hit"]
            if record["n"] != 1 or not slim or record["text"] != merge_parts(slim):
                raise EvaluationError("original pack body differs from its full/slim source collection")
            parts = slim
        record["segment_id"] = source.hit.segment_id
        record["segments"] = [{key: part[key] for key in ("segment_id", "role", "loc", "loc_end")} for part in parts]
    return meta
