"""SQLite truth, immutable mmap generations, failure recovery and invalidation."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from goldmine import vector_cache as vc
from goldmine import _vector_cache_files as files
from goldmine.deadline import RequestTimeout
from goldmine.store import FileRec, SegmentRec, Store
from goldmine.util import pack_fp16

ROOT = Path(__file__).resolve().parents[1]


def vectors(seed, count=3):
    rng = np.random.default_rng(seed)
    matrix = rng.normal(size=(count, 768)).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    return [SegmentRec(kind="chunk", modality=("text", "image", "audio")[i % 3], vec=vec)
            for i, vec in enumerate(matrix)]


@pytest.fixture
def indexed(tmp_path):
    store = Store(tmp_path / "index # %.sqlite")
    fid = store.upsert_file(FileRec(path="/corpus/source.txt", kind="doc"))
    store.replace_segments(fid, vectors(1))
    yield store, fid
    store.close()


def assert_same(left, right):
    assert right is not None
    assert np.array_equal(left.ids, right.ids)
    assert np.array_equal(left.file_ids, right.file_ids)
    assert np.array_equal(left.modalities, right.modalities)
    assert np.array_equal(left.matrix, right.matrix)


def manifest(store):
    return json.loads((vc.cache_directory(store) / "manifest.json").read_text())


def test_build_is_explicit_opt_in_and_matches_sqlite_exactly(indexed):
    store, _ = indexed
    truth = store.load_vectors()
    receipt = vc.build_vector_cache(store)
    assert not receipt["enabled"] and store.meta("vector_cache_enabled") is None
    assert receipt["cache_root"] == str(vc.cache_directory(store).resolve())
    assert hashlib.sha256((vc.cache_directory(store) / "manifest.json").read_bytes()).hexdigest() == receipt["manifest_sha256"]
    packed = vc.load_vector_cache(store, verify_checksums=True)
    assert_same(truth, packed)
    assert isinstance(packed.matrix, np.memmap) and not packed.matrix.flags.writeable
    assert not isinstance(store.load_vectors().matrix, np.memmap)
    vc.set_vector_cache_enabled(store, True)
    cold = store.load_vectors()
    assert isinstance(cold.matrix, np.memmap) and cold is store.load_vectors()
    assert_same(truth, cold)
    vc.set_vector_cache_enabled(store, False)
    assert not isinstance(store.load_vectors().matrix, np.memmap)


def test_empty_generation_and_corrupt_blob_skip_match_sqlite(tmp_path):
    with_store = Store(tmp_path / "empty.sqlite")
    try:
        vc.build_vector_cache(with_store)
        assert_same(with_store.load_vectors(), vc.load_vector_cache(with_store))
        fid = with_store.upsert_file(FileRec(path="/bad", kind="doc"))
        with with_store.write_transaction():
            with_store.db.execute("INSERT INTO segments(file,kind,modality,vec) VALUES(?, 'chunk','text',?)", (fid, b"short"))
        vc.build_vector_cache(with_store)
        assert_same(with_store.load_vectors(), vc.load_vector_cache(with_store))
    finally:
        with_store.close()


def test_epoch_tracks_raw_v6_writer_and_rollback_atomically(indexed):
    store, fid = indexed
    vc.build_vector_cache(store)
    vc.set_vector_cache_enabled(store, True)
    before = store.load_vectors()
    epoch = int(store.meta("vector_epoch"))
    sid = int(before.ids[0])
    # These are the pre-cache v6 writer's unchanged write scopes. No new
    # _vec_generation hook is involved in these direct SQL mutations.
    with pytest.raises(RuntimeError, match="rollback"):
        with store.write_transaction():
            store.db.execute("UPDATE segments SET vec=? WHERE id=?", (pack_fp16(vectors(2, 1)[0].vec), sid))
            assert int(store.meta("vector_epoch")) == epoch + 1
            assert vc.load_vector_cache(store) is None
            store.load_vectors()
            raise RuntimeError("rollback")
    assert int(store.meta("vector_epoch")) == epoch
    assert_same(before, store.load_vectors())
    with store.write_transaction():
        store.db.execute("UPDATE segments SET vec=? WHERE id=?", (pack_fp16(vectors(3, 1)[0].vec), sid))
    assert int(store.meta("vector_epoch")) == epoch + 1
    assert vc.load_vector_cache(store) is None
    changed = store.load_vectors()
    assert not isinstance(changed.matrix, np.memmap)
    assert not np.array_equal(before.matrix, changed.matrix)
    assert store.load_vectors() is changed
    with store.write_transaction():
        store.db.execute("INSERT INTO segments(file,kind,modality,vec) VALUES(?, 'chunk','text',?)", (fid, pack_fp16(vectors(4, 1)[0].vec)))
    assert int(store.meta("vector_epoch")) == epoch + 2
    with store.write_transaction():
        store.db.execute("DELETE FROM segments WHERE id=?", (sid,))
    assert int(store.meta("vector_epoch")) == epoch + 3
    assert vc._triggers_match(store.db)


def test_foreign_process_write_invalidates_once_then_memory_cache_hits(indexed):
    store, fid = indexed
    vc.build_vector_cache(store)
    vc.set_vector_cache_enabled(store, True)
    cached = store.load_vectors()
    program = "from pathlib import Path; import sys; from goldmine.store import Store; s=Store(Path(sys.argv[1])); s.delete_content_segments(int(sys.argv[2])); s.close()"
    subprocess.run([sys.executable, "-c", program, str(store.path), str(fid)], cwd=ROOT, check=True)
    fresh = store.load_vectors()
    assert len(cached) == 3 and len(fresh) == 0
    assert not isinstance(fresh.matrix, np.memmap) and store.load_vectors() is fresh


def test_parent_read_snapshot_keeps_matching_old_generation(indexed):
    store, fid = indexed
    vc.build_vector_cache(store)
    reader = Store(store.path, ro=True)
    try:
        with reader.read_snapshot():
            old = vc.load_vector_cache(reader)
            assert old is not None
            store.replace_segments(fid, vectors(5, 2))
            assert_same(old, vc.load_vector_cache(reader))
        assert vc.load_vector_cache(reader) is None
        assert len(reader.load_vectors()) == 2
    finally:
        reader.close()


def test_concurrent_ingest_during_build_publishes_safe_obsolete_snapshot(indexed, monkeypatch):
    store, fid = indexed
    writer = Store(store.path)
    original = vc.write_generation
    old = store.load_vectors()

    def interleave(*args, **kwargs):
        writer.replace_segments(fid, vectors(6, 2))
        return original(*args, **kwargs)

    monkeypatch.setattr(vc, "write_generation", interleave)
    try:
        receipt = vc.build_vector_cache(store)
        assert receipt["rows"] == len(old)
        assert vc.load_vector_cache(store) is None
        assert len(store.load_vectors()) == 2
    finally:
        writer.close()


def test_republish_keeps_old_mmap_and_never_deletes_generations(indexed):
    store, fid = indexed
    first = vc.build_vector_cache(store)
    old = vc.load_vector_cache(store)
    copied = old.matrix.copy()
    store.replace_segments(fid, vectors(7, 2))
    second = vc.build_vector_cache(store)
    assert first["generation"] != second["generation"]
    assert (vc.cache_directory(store) / first["generation"]).is_dir()
    assert np.array_equal(old.matrix, copied)
    assert len(vc.load_vector_cache(store)) == 2


@pytest.mark.parametrize("failure", ["missing", "truncate", "header", "ids", "manifest", "epoch", "fingerprint", "uuid", "trigger"])
def test_cache_defects_fall_back_without_stale_or_misaligned_rows(indexed, failure):
    store, _ = indexed
    truth = store.load_vectors()
    vc.build_vector_cache(store)
    vc.set_vector_cache_enabled(store, True)
    root = vc.cache_directory(store)
    saved = manifest(store)
    matrix = root / saved["generation"] / "matrix.npy"
    if failure == "missing":
        matrix.unlink()
    elif failure == "truncate":
        with matrix.open("r+b") as stream:
            stream.truncate(160)
    elif failure == "header":
        stat = matrix.stat()
        with matrix.open("r+b") as stream:
            stream.write(b"BROKEN")
        os.utime(matrix, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif failure == "ids":
        path = matrix.with_name("ids.npy")
        stat = path.stat()
        with path.open("r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            stream.write(b"X")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif failure == "manifest":
        (root / "manifest.json").write_text('{"version":1,"version":1}')
    elif failure == "epoch":
        store.set_meta("vector_epoch", "999")
    elif failure == "fingerprint":
        store.set_meta("fingerprint_id", "changed")
    elif failure == "uuid":
        from uuid import uuid4
        store.set_meta("index_uuid", str(uuid4()))
    elif failure == "trigger":
        with store.write_transaction():
            store.db.execute("DROP TRIGGER gm_vector_epoch_update")
            store.db.execute("CREATE TRIGGER gm_vector_epoch_update AFTER UPDATE ON segments BEGIN SELECT 1; END")
    assert vc.load_vector_cache(store) is None
    assert_same(truth, store.load_vectors())
    assert not isinstance(store.load_vectors().matrix, np.memmap)


def test_trigger_repair_has_new_identity_before_any_cache_can_revalidate(indexed):
    store, _ = indexed
    vc.build_vector_cache(store)
    first = store.meta("vector_cache_epoch_uuid")
    with store.write_transaction():
        store.db.execute("DROP TRIGGER gm_vector_epoch_update")
        store.db.execute("DELETE FROM meta WHERE key='vector_epoch'")
    vc.install_vector_cache_protocol(store)
    assert store.meta("vector_cache_epoch_uuid") != first
    assert vc.load_vector_cache(store) is None


def test_full_checksum_audit_detects_matrix_bit_change_even_if_stamp_restored(indexed):
    store, _ = indexed
    vc.build_vector_cache(store)
    saved = manifest(store)
    path = vc.cache_directory(store) / saved["generation"] / "matrix.npy"
    stat = path.stat()
    with path.open("r+b") as stream:
        stream.seek(-1, os.SEEK_END)
        value = stream.read(1)
        stream.seek(-1, os.SEEK_END)
        stream.write(bytes([value[0] ^ 1]))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert vc.load_vector_cache(store, verify_checksums=True) is None


def test_disk_full_keeps_previously_published_manifest(indexed, monkeypatch):
    store, _ = indexed
    vc.build_vector_cache(store)
    before = (vc.cache_directory(store) / "manifest.json").read_bytes()

    def disk_full(path):
        raise OSError(errno.ENOSPC, "injected disk full")

    monkeypatch.setattr(files, "_fsync_file", disk_full)
    with pytest.raises(OSError, match="disk full"):
        vc.build_vector_cache(store)
    assert (vc.cache_directory(store) / "manifest.json").read_bytes() == before
    assert vc.load_vector_cache(store) is not None


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL crash injection requires POSIX")
@pytest.mark.parametrize("stage", ["half_write", "before_manifest"])
def test_sigkill_builder_cannot_publish_partial_generation(indexed, stage):
    store, _ = indexed
    vc.build_vector_cache(store)
    before = (vc.cache_directory(store) / "manifest.json").read_bytes()
    program = textwrap.dedent('''
        import os, signal, sys
        from pathlib import Path
        from goldmine import vector_cache as vc
        from goldmine import _vector_cache_files as files
        from goldmine.store import Store
        store = Store(Path(sys.argv[1]))
        def stop(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGKILL)
        if sys.argv[2] == "half_write":
            files._fsync_file = stop
        else:
            vc.publish_manifest = stop
        vc.build_vector_cache(store)
    ''')
    run = subprocess.run([sys.executable, "-c", program, str(store.path), stage], cwd=ROOT)
    assert run.returncode == -signal.SIGKILL
    assert (vc.cache_directory(store) / "manifest.json").read_bytes() == before
    assert vc.load_vector_cache(store, verify_checksums=True) is not None


def test_request_timeout_is_never_a_cache_fallback(indexed, monkeypatch):
    store, _ = indexed
    vc.build_vector_cache(store)
    vc.set_vector_cache_enabled(store, True)

    def expired(*args):
        raise RequestTimeout("injected request expiry")

    monkeypatch.setattr(vc, "read_manifest", expired)
    with pytest.raises(RequestTimeout, match="expiry"):
        store.load_vectors()


def test_enable_never_builds_and_read_only_never_mutates(indexed):
    store, _ = indexed
    with pytest.raises(ValueError, match="build"):
        vc.set_vector_cache_enabled(store, True)
    assert not vc.cache_directory(store).exists()
    reader = Store(store.path, ro=True)
    try:
        assert vc.load_vector_cache(reader) is None
        with pytest.raises(ValueError, match="writable"):
            vc.build_vector_cache(reader)
        assert reader.meta("vector_epoch") is None
    finally:
        reader.close()


def test_mapped_identity_scalars_bind_as_sqlite_integers_without_copying_arrays(indexed):
    store, _ = indexed
    vc.build_vector_cache(store)
    vc.set_vector_cache_enabled(store, True)
    table = store.load_vectors()
    assert type(table.ids[0]) is type(table.file_ids[0]) is int
    assert type(table.modalities[0]) is str
    assert all(type(sid) is int for sid in table.ids)
    assert store.db.execute("SELECT typeof(?)", (table.ids[0],)).fetchone()[0] == "integer"
    assert store.db.execute("SELECT id FROM segments WHERE id=?", (table.ids[0],)).fetchone()[0] == table.ids[0]
    assert np.shares_memory(np.asarray(table.ids, dtype=np.int64), table.ids._array)
    assert np.shares_memory(np.asarray(table.file_ids, dtype=np.int64), table.file_ids._array)
    assert np.shares_memory(np.asarray(table.modalities), table.modalities._array)
    assert table.ids == list(table.ids)
    from goldmine.distill import Scope, _corpus

    items = _corpus(store, Scope())
    assert len(items) == 3 and all(type(item.sid) is type(item.fid) is int for item in items)
    assert json.loads(json.dumps([item.sid for item in items])) == list(table.ids)
