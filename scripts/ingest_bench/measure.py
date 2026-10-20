"""Observe real stage calls without replacing their results."""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
import hashlib
import json
from threading import Lock
import time
from unittest.mock import patch


class Meter:
    def __init__(self):
        self.calls, self.milliseconds = Counter(), Counter()
        self._lock = Lock()
        self._patches = ExitStack()

    def timed(self, name, operation):
        def invoke(*args, **kwargs):
            started = time.perf_counter()
            try:
                return operation(*args, **kwargs)
            finally:
                with self._lock:
                    self.calls[name] += 1
                    self.milliseconds[name] += (time.perf_counter() - started) * 1000
        return invoke

    def __enter__(self):
        from goldmine import ocr
        from goldmine.ingest import asr, pipeline, walk
        from goldmine.ingest.commit import CommitMixin
        from goldmine.ingest.extraction import ExtractMixin

        for owner, attribute, name in ((pipeline, "sha256_file", "hash"), (ocr, "recognize", "ocr"),
                (asr, "transcribe", "asr"), (CommitMixin, "_commit", "commit_inclusive"),
                (ExtractMixin, "_extract", "extract")):
            self._patches.enter_context(patch.object(owner, attribute, self.timed(name, getattr(owner, attribute))))
        original = walk.walk_files

        def measured_walk(*args, **kwargs):
            iterator = iter(original(*args, **kwargs))
            while True:
                try:
                    item = self.timed("scan_next", next)(iterator)
                except StopIteration:
                    return
                yield item
        self._patches.enter_context(patch.object(walk, "walk_files", measured_walk))
        return self

    def __exit__(self, *args):
        self._patches.close()

    def snapshot(self):
        return {"calls": dict(self.calls), "milliseconds": dict(self.milliseconds)}


class BackendProxy:
    """Delegate genuine model calls; only an explicitly requested fault is injected."""
    def __init__(self, backend, meter, *, fail_embed=False):
        self.backend, self.meter, self.fail_embed = backend, meter, fail_embed

    def __getattr__(self, name):
        operation = getattr(self.backend, name)
        if name in ("startup", "embed_texts", "embed_image", "embed_audio_wav"):
            def invoke(*args, **kwargs):
                if self.fail_embed and name.startswith("embed_"):
                    from goldmine.backends.base import EmbeddingError

                    self.fail_embed = False
                    raise EmbeddingError("controlled benchmark fault before embedding response")
                return operation(*args, **kwargs)
            return self.meter.timed(name, invoke)
        return operation


def stored_snapshot(store):
    result = {}
    for table in ("files", "segments", "fts", "cards", "card_chunks"):
        rows = store.db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid").fetchall()
        encoded = json.dumps(rows, ensure_ascii=False, default=lambda value: {"bytes": value.hex()},
                             separators=(",", ":"), allow_nan=False).encode()
        result[table] = {"rows": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    return result
