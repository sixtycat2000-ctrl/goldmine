# Optional vector cache

SQLite stores the authoritative fp16 vectors and segment identities. The optional
cache stores fp32 vectors and aligned segment IDs, file IDs and modalities as four
immutable numpy files beside the index, under `<index>.vectors/`.

The default remains SQLite. Building does not enable the cache:

```sh
goldmine reindex vectors --db library.sqlite
goldmine reindex vectors --db library.sqlite --enable-cache
goldmine reindex vectors --db library.sqlite --disable-cache
```

The second command rebuilds and explicitly opts in; the third only disables it.
Measure the frozen comparison first. The maintenance adapter uses these functions:

```python
from goldmine.vector_cache import build_vector_cache, set_vector_cache_enabled

receipt = build_vector_cache(store)
# Enable only after the recorded P2-02 performance comparison meets its gates.
set_vector_cache_enabled(store, True)
# To return to the default:
set_vector_cache_enabled(store, False)
```

The build receipt includes `cache_root`, `manifest_sha256`, generation, row count,
index identity and the existing enable state. `load_vector_cache(store)` is also
available for direct verification without changing the enable state. Its
`verify_checksums=True` option hashes every file, including the full matrix.

The first build installs three `gm_vector_epoch_*` triggers and protocol metadata
inside a writer transaction. Every segment insert, update and delete changes
`vector_epoch` in that same transaction, including old schema-v6 writers: their
unchanged writer scopes remove only the `gm_writer_*` fences and preserve these
separately named triggers. A rollback also rolls back the epoch. This is additive
schema metadata; no table is rebuilt and the schema version stays v6. A repaired
trigger installation receives a new epoch UUID, so a reset or missing counter
cannot make an old cache valid again.

The builder streams a consistent SQLite read snapshot into a new generation,
flushes and fsyncs all four files, fsyncs the generation directory, then publishes
one complete manifest with atomic replacement and directory fsync. A concurrent
ingest can make that generation obsolete before publication; the reader rejects
its old epoch. Readers already holding an older mmap can finish using it.

Reads validate exact epoch-trigger definitions, protocol/schema version, index
UUID, embedding fingerprint and epoch, then all array names, shapes, dtypes,
immutable file timestamps, numpy headers and exact file lengths. The small
identity arrays are always checksummed. Normal reads check the large matrix's
timestamp, header and length; explicit full verification also detects content
changes that preserve timestamps and length. This is a local derived-cache
integrity check, not an authenticated defense against replacement of the index
and manifest together. Any detected cache defect falls back to SQLite. Request
timeout and cancellation exceptions propagate rather than starting a fallback.

No generation is automatically deleted. Interrupted `.building-*` directories,
unpublished generations and superseded `g-*` generations can be removed manually
when no reader or builder uses that index. Keeping them until then preserves
existing mappings, including platforms that cannot delete open mapped files.
Removing the entire cache directory is also safe while the index is idle: the
next query uses SQLite. It does not remove any authoritative vectors.

The P2-02 frozen benchmark must measure cold-load p50 and fresh-process peak RSS
against the same SQLite fixtures, query order and score checks. Default enablement
requires both improvements to reach 30%, plus the small-index regression gates.
Mechanical cache tests alone do not establish those performance results.
