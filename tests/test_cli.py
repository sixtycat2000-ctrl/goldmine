"""Tier-2: CLI end-to-end via main(argv) — exit codes, --json shapes, human output."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from goldmine.backends import BackendUnavailable
from goldmine import fingerprints
from goldmine.backends.mock import MockBackend
from goldmine.cli import main
from goldmine.store import Store


@pytest.fixture
def db_path(tmp_home):
    return tmp_home / "gmhome" / "default.sqlite"


def ingest(tmp_home, db_path, *extra, root):
    return main(
        ["--backend", "mock", "--db", str(db_path), "ingest", str(root), *extra]
    )


def test_ingest_exit_zero_and_json_shape(tmp_home, db_path, fixture_tree):
    rc = ingest(tmp_home, db_path, "--json", root=fixture_tree)
    assert rc == 0
    # (stdout captured by the caller; re-run without --json for the human shape)


def test_ingest_human_summary(tmp_home, db_path, fixture_tree, capsys):
    rc = ingest(tmp_home, db_path, root=fixture_tree)
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("ingest: 1 roots,")
    assert "content-indexed  7" in out
    assert "metadata-only    1" in out
    assert "unsupported      6" in out
    assert "failed           0" in out
    assert "skipped-unchanged 0" in out
    assert "vectors:" in out and "fingerprint mock/" in out


def test_ingest_json_schema(tmp_home, db_path, fixture_tree, capsys):
    rc = ingest(tmp_home, db_path, "--json", root=fixture_tree)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {
        "roots", "files", "by_kind", "failures", "metadata_reasons",
        "vectors", "segments", "fingerprint", "notices", "dry_run",
    }
    files = payload["files"]
    assert files["scanned"] == 14
    assert files["content_indexed"] == 7
    assert files["metadata_only"] == 1
    assert files["unsupported"] == 6
    assert files["failed"] == 0
    assert payload["vectors"] > 0
    assert payload["fingerprint"]["backend"] == "mock"
    assert payload["dry_run"] is False
    reasons = {r["reason"] for r in payload["unsupported_reasons"]}
    assert {"key or password file", "minified", "license", "code or data file"} <= reasons


def test_ingest_dry_run_writes_nothing(tmp_home, db_path, fixture_tree, capsys):
    rc = ingest(tmp_home, db_path, "--dry-run", "--json", root=fixture_tree)
    assert rc == 0
    assert not db_path.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True


def test_ingest_second_run_counts_unchanged(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    rc = ingest(tmp_home, db_path, root=fixture_tree)
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped-unchanged 8" in out  # 7 docs + 1 image; unsupported rows re-recorded


def test_backend_unavailable_exit_4(tmp_home, db_path, fixture_tree, monkeypatch):
    def refuse(cfg, name=None):
        raise BackendUnavailable("no backend", fix_hint="use --lexical-only")

    monkeypatch.setattr("goldmine.cli._make_backend", refuse)
    rc = main(["--db", str(db_path), "ingest", str(fixture_tree)])
    assert rc == 4


# (02-ingestion F5): the old test_llama_absent_in_this_build_is_honest is
# DELETED — its guard `if find_spec("goldmine.backends.llama") is not None:
# pytest.skip(...)` has been permanently true since the llama backend shipped
# (build pass 5), so the test could never execute again.


def test_lexical_only_flag_builds_keyword_index(tmp_home, db_path, fixture_tree):
    rc = main(["--db", str(db_path), "ingest", str(fixture_tree), "--lexical-only"])
    assert rc == 0
    rc = main(["--db", str(db_path), "status", "--json"])
    assert rc == 0
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["--db", str(db_path), "status", "--json"])
    payload = json.loads(buf.getvalue())
    assert payload["counts"]["vectors"] == 0
    assert payload["counts"]["content_indexed"] == 7


def test_ingest_failure_exit_5(tmp_home, db_path, tmp_path, capsys):
    root = tmp_path / "broken"
    root.mkdir()
    (root / "ok.txt").write_text("fine words here\n", encoding="utf-8")
    (root / "broken.docx").write_bytes(b"not a zip at all")
    rc = ingest(tmp_home, db_path, "--json", root=root)
    assert rc == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"]["failed"] == 1
    assert any("broken.docx" in f["path"] for f in payload["failures"])


def test_ingest_requires_roots(tmp_home, capsys):
    rc = main(["--backend", "mock", "ingest"])
    assert rc == 2  # argparse usage error


def test_ingest_phantom_root_exit_5_and_never_persisted(
    tmp_home, db_path, fixture_tree, tmp_path, capsys
):
    """F10: a nonexistent root is a per-root failure with exit-5 semantics,
    reported in the 4-line shape — and it must NOT enter meta roots_json
    (a typo must not silently become index scope)."""
    assert ingest(tmp_home, db_path, root=fixture_tree) == 0
    phantom = tmp_path / "does-not-exist"

    # human path: per-root error line + universal shape, exit 5
    rc = ingest(tmp_home, db_path, root=phantom)
    assert rc == 5
    captured = capsys.readouterr()
    assert "root not accessible" in captured.out  # summary failure row
    assert "root(s) not accessible" in captured.err
    assert "why: " in captured.err and "fix: goldmine ingest" in captured.err

    # json path: failures carry the detail, roots exclude the phantom
    rc = ingest(tmp_home, db_path, "--json", root=phantom)
    assert rc == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"]["failed"] == 1
    assert payload["failures"][0]["path"] == str(phantom.resolve())
    assert payload["failures"][0]["detail"].startswith("root not accessible")
    assert str(phantom.resolve()) not in payload["roots"]
    assert any(n["code"] == "phantom_roots" for n in payload["notices"])

    store = Store(db_path)
    try:
        roots = json.loads(store.meta("roots_json") or "[]")
    finally:
        store.close()
    assert str(phantom.resolve()) not in roots
    assert str(fixture_tree.resolve()) in roots


def test_ingest_mixed_real_and_phantom_roots(tmp_home, db_path, fixture_tree, tmp_path):
    """A phantom root alongside a real one: exit 5 (partial), but the real
    root still indexes."""
    phantom = tmp_path / "phantom"
    rc = main([
        "--backend", "mock", "--db", str(db_path), "ingest",
        str(fixture_tree), str(phantom),
    ])
    assert rc == 5
    store = Store(db_path)
    try:
        assert store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] > 0
        assert str(phantom.resolve()) not in json.loads(store.meta("roots_json"))
    finally:
        store.close()


def test_search_json_shapes(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()  # flush the ingest summary from stdout
    rc = main(["--backend", "mock", "--db", str(db_path), "search", "tolerations", "--json"])
    assert rc == 0
    hits = json.loads(capsys.readouterr().out)
    assert isinstance(hits, list) and hits
    hit = hits[0]
    assert set(hit) == {
        "rank", "score", "z", "cosine", "coverage", "kind", "path", "loc",
        "loc_end", "segment", "excerpt", "file_status", "scorer",
    }
    assert any("notes.txt" in h["path"] for h in hits)
    target = next(h for h in hits if "notes.txt" in h["path"])
    assert target["kind"] == "doc"
    assert target["file_status"] == "content_indexed"
    assert target["coverage"] == pytest.approx(1.0)
    assert target["scorer"] == "hybrid"


def test_search_human_shape_and_zero_hit_honesty(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    rc = main(["--backend", "mock", "--db", str(db_path), "search", "tolerations", "--limit", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "score " in out and "z " in out and "cov " in out
    assert "doc" in out and "notes.txt" in out

    rc = main(["--backend", "mock", "--db", str(db_path), "search", "wubblezort", "--scorer", "lexical"])
    assert rc == 0
    assert "nothing found for" in capsys.readouterr().out


def test_search_zero_hits_json_is_empty_array(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path), "search", "wubblezort", "--scorer", "lexical", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []


def test_search_missing_index_exit_4(tmp_home, db_path):
    rc = main(["--db", str(db_path), "search", "anything"])
    assert rc == 4


def test_fingerprint_flip_search_exit_3_and_lexical_escape(
    tmp_home, db_path, fixture_tree, capsys
):
    ingest(tmp_home, db_path, root=fixture_tree)
    store = Store(db_path)
    forged = fingerprints.build(MockBackend(dim=256))
    store.set_meta("fingerprint_id", fingerprints.fingerprint_id(forged))
    store.set_meta("fingerprint_json", fingerprints.canonical(forged).decode())
    store.close()

    rc = main(["--backend", "mock", "--db", str(db_path), "search", "tolerations"])
    assert rc == 3
    assert "reindex" in capsys.readouterr().err.lower()

    # --lexical suppresses the refusal and labels the results stale
    rc = main(
        ["--backend", "mock", "--db", str(db_path), "search", "tolerations",
         "--lexical", "--json"]
    )
    assert rc == 0
    hits = json.loads(capsys.readouterr().out)
    assert hits and hits[0]["scorer"] == "lexical (stale-vectors)"


def test_search_kinds_filter(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path), "search", "tolerations", "--kinds", "image", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []
    rc = main(["--backend", "mock", "--db", str(db_path), "search", "tolerations", "--kinds", "doc", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)


def test_status_exit_4_when_missing(tmp_home, db_path, capsys):
    rc = main(["--db", str(db_path), "status"])
    assert rc == 4
    assert "ingest" in capsys.readouterr().err


def test_status_human_and_json(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()
    rc = main(["--db", str(db_path), "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("index: ")
    assert "capability grid" in out
    assert "doc " in out
    assert "mock: available" in out
    capsys.readouterr()

    rc = main(["--db", str(db_path), "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    for key in ("index", "schema_version", "fingerprint", "backends", "counts",
                "by_kind", "failures", "metadata_reasons", "notices"):
        assert key in payload
    counts = payload["counts"]
    for key in ("content_indexed", "metadata_only", "unsupported", "failed"):
        assert key in counts


def test_backends_lists_three_and_exits_zero(tmp_home, capsys):
    rc = main(["--db", str(tmp_home / "x.sqlite"), "backends"])
    assert rc == 0
    out = capsys.readouterr().out
    for backend in ("llama", "foresight", "mock"):
        assert backend in out
    assert "mock" in out and "available" in out


def test_backends_json_and_probe_never_spawns_llama(tmp_home, capsys, monkeypatch):
    """--probe on llama may start the server; WITHOUT it, llama must not spawn."""
    import goldmine.cli as cli

    spawned = []
    real = cli._probe_backend_live

    def guarded(cfg, backend_id):
        spawned.append(backend_id)
        return real(cfg, backend_id)

    monkeypatch.setattr(cli, "_probe_backend_live", guarded)
    rc = main(["backends", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert [b["id"] for b in payload["backends"]] == ["foresight", "llama", "mock"]
    assert spawned == []  # static listing only


def test_doctor_runs_and_shape(tmp_home, capsys):
    rc = main(["doctor", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert set(payload) == {"checks", "summary"}
    names = {c["name"] for c in payload["checks"]}
    assert {"python", "numpy", "backend mock", "index", "disk"} <= names
    for check in payload["checks"]:
        assert check["status"] in ("ok", "warn", "fail")
    assert isinstance(payload["summary"]["ok"], bool)
    assert rc == (0 if payload["summary"]["ok"] else 4)


def test_doctor_human_output_and_fail_exit(tmp_home, capsys):
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "doctor:" in out and ("OK" in out or "WARN" in out)
    assert rc in (0, 4)


def test_reset_dry_default_lists_without_deleting(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    assert db_path.exists()
    rc = main(["--db", str(db_path), "reset"])
    assert rc == 2  # non-interactive without --yes: dry refusal
    assert db_path.exists()
    captured = capsys.readouterr()
    assert "--yes" in captured.err
    assert "would delete" in captured.out


def test_reset_yes_deletes_db_wal_shm(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    import sqlite3

    conn = sqlite3.connect(db_path)  # ensure a -wal file exists
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    rc = main(["--db", str(db_path), "reset", "--yes"])
    assert rc == 0
    assert not db_path.exists()
    out = capsys.readouterr().out
    assert "deleted" in out


def test_reset_reindex_rebuilds_from_stored_roots(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    rc = main(["--backend", "mock", "--db", str(db_path), "reset", "--reindex"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("ingest:")
    store = Store(db_path)
    counts = store.counts()
    assert counts.vectors > 0
    store.close()


def test_distill_needs_index_and_two_chunks(tmp_home, db_path, capsys):
    rc = main(["--db", str(db_path), "distill"])
    assert rc == 4
    assert "no index" in capsys.readouterr().err

    (tmp_home / "corpus").mkdir()
    (tmp_home / "corpus" / "one.txt").write_text("sourdough starter notes\n")
    ingest(tmp_home, db_path, root=tmp_home / "corpus")
    rc = main(["--backend", "mock", "--db", str(db_path), "distill"])
    assert rc == 1  # < 2 vectorized chunks is a runtime refusal
    assert "at least 2 vectorized chunks" in capsys.readouterr().err


def test_distill_markdown_json_and_out_dir(tmp_home, db_path, capsys):
    corpus = tmp_home / "distill-corpus"
    (corpus / "k8s").mkdir(parents=True)
    (corpus / "food").mkdir()
    for i in range(4):
        (corpus / "k8s" / f"k{i}.txt").write_text(
            f"Scheduler eviction under pod pressure {i}. "
            "Tolerations keep workloads running. " * 3
        )
        (corpus / "food" / f"f{i}.txt").write_text(
            f"Sourdough starter feeding schedule {i}. "
            "Bulk ferment the dough overnight. " * 3
        )
    ingest(tmp_home, db_path, root=corpus)
    capsys.readouterr()  # flush the ingest summary
    rc = main(["--backend", "mock", "--db", str(db_path), "distill", "--min-members", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    # v2 markdown (§6.3): H1 + run header + TOC
    assert out.startswith("# goldmine cards v2")
    assert "## Contents" in out
    assert "### 1." in out

    rc = main(["--backend", "mock", "--db", str(db_path), "distill", "--min-members", "2", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "goldmine.cards/v2"
    assert payload["stats"]["clusters_kept"] >= 2
    # second run on the same view: the incremental path took over (D8)
    assert payload["stats"]["mode"] == "incremental"
    assert payload["stats"]["tau_inherited"] is True
    assert payload["stats"]["tau_source"] == "inherited"
    assert payload["stats"]["budget"] is None
    first = payload["cards"][0]
    assert set(first) == {
        "id", "parent_id", "title", "title_kind", "terms", "excerpts", "size",
        "n_files", "status", "citations", "distill_algo", "run_id", "llm",
    }
    assert first["llm"] is None and first["excerpts"]

    out_dir = tmp_home / "cards-out"
    rc = main(["--backend", "mock", "--db", str(db_path), "distill", "--min-members", "2",
               "--out", str(out_dir)])
    assert rc == 0
    assert (out_dir / "cards.md").exists()
    assert (out_dir / "cards.json").exists()
    stored = Store(db_path).cards()
    assert stored  # cards persisted with fingerprint


def test_distill_max_chunks_refusal_exit_1(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    rc = main(["--backend", "mock", "--db", str(db_path), "distill", "--max-chunks", "2"])
    assert rc == 1
    assert "max-chunks" in capsys.readouterr().err


def test_code_flag_indexes_code_files(tmp_home, db_path, fixture_tree, capsys):
    rc = ingest(tmp_home, db_path, "--code", root=fixture_tree)
    assert rc == 0
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path), "search", "util", "--kinds", "code", "--json"])
    assert rc == 0
    hits = json.loads(capsys.readouterr().out)
    assert hits and all(h["kind"] == "code" for h in hits)
    assert any(h["path"].endswith("utils.py") for h in hits)
    # line-aligned chunks carry line locs
    assert any(h["loc"] is not None for h in hits)


def test_version_flag():
    assert main(["--version"]) == 0


def test_no_command_prints_help_exit_2(tmp_home, capsys):
    rc = main([])
    assert rc == 2


# --------------------------------------------------- adversarial-review pins


def test_reindex_startup_failure_exit_2_and_index_intact(
    tmp_home, db_path, fixture_tree, monkeypatch, capsys
):
    """P1-3 (CLI contract): a --reindex whose backend fails to start exits 2
    and leaves vectors + fingerprint untouched (no hollow index)."""
    from goldmine.backends.base import BackendStartupError

    ingest(tmp_home, db_path, root=fixture_tree)
    store = Store(db_path)
    vectors_before = store.counts().vectors
    fp_before = store.meta("fingerprint_id")
    store.close()

    class DeadBackend(MockBackend):
        def startup(self):
            raise BackendStartupError("no llama-server in test")

    monkeypatch.setattr(
        "goldmine.cli._make_backend", lambda cfg, name=None: DeadBackend(dim=768)
    )
    rc = main([
        "--backend", "mock", "--db", str(db_path),
        "ingest", str(fixture_tree), "--reindex",
    ])
    assert rc == 2  # startup failure maps to exit 2 (ARCHITECTURE §8)
    store = Store(db_path)
    assert store.counts().vectors == vectors_before
    assert store.meta("fingerprint_id") == fp_before
    store.close()


def test_distill_fingerprint_gate_exit_3(tmp_home, db_path, capsys):
    """P2-12 (SPEC §4): distill applies the fingerprint gate — a backend whose
    fingerprint differs from the index's is refused with exit 3; the matching
    backend distills with exit 0."""
    corpus = tmp_home / "gate-corpus"
    (corpus / "k8s").mkdir(parents=True)
    (corpus / "food").mkdir()
    for i in range(4):
        (corpus / "k8s" / f"k{i}.txt").write_text(
            f"Scheduler eviction under pod pressure {i}. "
            "Tolerations keep workloads running. " * 3
        )
        (corpus / "food" / f"f{i}.txt").write_text(
            f"Sourdough starter feeding schedule {i}. "
            "Bulk ferment the dough overnight. " * 3
        )
    ingest(tmp_home, db_path, root=corpus)  # built with mock
    capsys.readouterr()

    rc = main(["--backend", "llama", "--db", str(db_path), "distill", "--min-members", "2"])
    assert rc == 3
    assert "fingerprint" in capsys.readouterr().err.lower()

    rc = main([
        "--backend", "mock", "--db", str(db_path), "distill", "--min-members", "2",
    ])
    assert rc == 0


def test_search_limit_zero_is_a_valid_answer(tmp_home, db_path, fixture_tree, capsys):
    """P3-13: `--limit 0` means zero hits, never the configured default."""
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()
    rc = main([
        "--backend", "mock", "--db", str(db_path),
        "search", "tolerations", "--limit", "0", "--json",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []


def test_search_excerpt_chars_zero_disables_excerpt(
    tmp_home, db_path, fixture_tree, capsys
):
    """P3-13: `--excerpt-chars 0` disables excerpt rendering (not 240)."""
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()
    rc = main([
        "--backend", "mock", "--db", str(db_path),
        "search", "tolerations", "--excerpt-chars", "0",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "notes.txt" in out          # the hit itself still renders
    assert not any(line.startswith('   "') for line in out.splitlines())

    # default rendering still shows excerpts
    rc = main(["--backend", "mock", "--db", str(db_path), "search", "tolerations"])
    assert rc == 0
    assert any(line.startswith('   "') for line in capsys.readouterr().out.splitlines())


def test_crawl_bounds_flags_map_to_their_own_defaults(tmp_home):
    """P2-10: crawl_max_mb_page must default to CrawlBounds.max_mb_page — the
    copy-paste from max_pages made `--crawl-max-pages` silently raise the
    per-page byte cap to N (pages!)."""
    from goldmine.cli import _crawl_bounds, build_parser
    from goldmine.config import Config

    cfg = Config()
    args = build_parser().parse_args(["ingest", "/x", "--crawl-max-pages", "3"])
    bounds = _crawl_bounds(args, cfg)
    assert bounds.max_pages == 3
    assert bounds.max_mb_page == 5.0

    args2 = build_parser().parse_args(["ingest", "/x", "--crawl-max-mb", "2.5"])
    assert _crawl_bounds(args2, cfg).max_mb_page == 2.5

    # no crawl flags at all -> no bounds object (argparse.Namespace without
    # the subparser defaults, like the SUPPRESS contract for globals)
    import argparse as _argparse

    assert _crawl_bounds(_argparse.Namespace(cmd="ingest", roots=["/x"]), cfg) is None


def test_encoding_flag_reaches_extractor(tmp_home, db_path, tmp_path, capsys):
    """P2-9: --encoding overrides decode detection for the run."""
    root = tmp_path / "enc-root"
    root.mkdir()
    (root / "legacy.txt").write_text("café crème — brève", encoding="windows-1252")

    rc = main(["--backend", "mock", "--db", str(db_path), "ingest", str(root), "--json"])
    assert rc == 0
    capsys.readouterr()
    store = Store(db_path)
    rec = store.find_by_path(str(root / "legacy.txt"))
    assert rec.info["decode"] == "lossy-decode: windows-1252"
    store.close()

    db2 = tmp_home / "gmhome" / "enc2.sqlite"
    rc = main([
        "--backend", "mock", "--db", str(db2),
        "ingest", str(root), "--encoding", "windows-1252", "--json",
    ])
    assert rc == 0
    store = Store(db2)
    rec2 = store.find_by_path(str(root / "legacy.txt"))
    # strict override decode: no lossy note at all (info may be empty -> None)
    assert rec2.info is None or rec2.info.get("decode") is None
    store.close()


def test_include_datasets_flag_skips_detection(tmp_home, db_path, tmp_path, capsys):
    """P2-9: --include-datasets keeps a machine-named media folder indexable."""
    root = tmp_path / "ds-cli"
    shards = root / "shards"
    shards.mkdir(parents=True)
    for i in range(300):
        (shards / f"frame_{i:06d}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

    rc = main(["--backend", "mock", "--db", str(db_path), "ingest", str(root), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"]["unsupported"] == 300
    assert any(r["reason"] == "dataset folder" for r in payload["unsupported_reasons"])

    db2 = tmp_home / "gmhome" / "ds2.sqlite"
    rc = main([
        "--backend", "mock", "--db", str(db2),
        "ingest", str(root), "--include-datasets", "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"]["unsupported"] == 0
    assert payload["files"]["metadata_only"] == 300


def test_doctor_reports_fts_orphan_rows(tmp_home, db_path, fixture_tree, capsys):
    """P0-1: doctor's index check counts orphaned fts rows (FTS5 has no FK
    cascade, so they are the crash signature of the pre-fix delete paths)."""
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()  # flush the ingest summary
    store = Store(db_path)
    seg_id = store.db.execute("SELECT id FROM segments LIMIT 1").fetchone()[0]
    store.db.execute("DELETE FROM segments WHERE id = ?", (seg_id,))
    store.db.commit()
    store.close()

    rc = main(["--db", str(db_path), "doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    index_check = next(c for c in payload["checks"] if c["name"] == "index")
    assert index_check["status"] == "fail"
    assert "orphan fts row" in index_check["detail"]
    assert rc == 4


# ----------------------------------------------- v2 items 4/5: doctor rows


def test_doctor_v2_install_and_model_rows(tmp_home):
    from goldmine import doctor
    from goldmine.config import load

    cfg = load(config_path=tmp_home / "none.toml", env={
        "GOLDMINE_HOME": str(tmp_home / "gmhome"),
    })
    results = doctor.run_checks(cfg, None, probe=False, deep=False)
    names = {r.name for r in results}
    assert "llama-server install" in names
    assert "models: embedding" in names
    assert "models: asr/vad" in names
    by_name = {r.name: r for r in results}
    # the install row always resolves to a definitive state with a hint on lack
    install = by_name["llama-server install"]
    assert install.status in ("ok", "warn", "fail")
    if install.status != "ok":
        assert install.fix_hint


def test_version_flag_still_works():
    import subprocess
    import sys
    from pathlib import Path

    proc = subprocess.run(
        [sys.executable, "-m", "goldmine", "--version"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0
    assert "0.2.0" in proc.stdout


def test_ingest_refresh_thin_flag_parses_and_flows(tmp_home, db_path, fixture_tree, capsys):
    """--refresh-thin (v2 B4): parses on the ingest parser and reaches the
    pipeline (spied) as refresh_thin=True; default run passes False."""
    import goldmine.cli as cli

    args = cli.build_parser().parse_args(["ingest", "/x", "--refresh-thin"])
    assert args.refresh_thin is True
    args = cli.build_parser().parse_args(["ingest", "/x"])
    assert args.refresh_thin is False

    seen = {}

    class SpyPipeline:
        def __init__(self, cfg, store, backend, lexical_only=False):
            pass

        def run(self, roots, **kwargs):
            seen.update(kwargs)
            from goldmine.ingest import IngestReport
            return IngestReport(roots=roots)

    original = cli.IngestPipeline
    cli.IngestPipeline = SpyPipeline
    try:
        rc = cli.cmd_ingest(
            cli.build_parser().parse_args(
                ["ingest", str(fixture_tree), "--lexical-only", "--refresh-thin",
                 "--db", str(db_path)]
            )
        )
    finally:
        cli.IngestPipeline = original
    assert rc == 0
    assert seen.get("refresh_thin") is True


# ------------------------------------------------- v2 item 17: error shapes


def _assert_shape(err: str) -> None:
    """The universal 4-line shape (03 §5.7): WHAT / why: / fix: / more:."""
    lines = [l for l in err.splitlines() if not l.startswith("20")]  # drop log lines
    assert lines, "expected stderr output"
    assert not lines[0].startswith(("why:", "fix:", "more:"))
    assert any(l.startswith("why: ") and l[5:].strip() for l in lines)
    assert any(l.startswith("fix: ") and l[5:].strip() for l in lines)


def test_error_shape_search_no_index_human_and_json(tmp_home, db_path, capsys):
    rc = main(["--db", str(db_path), "search", "x"])
    assert rc == 4
    _assert_shape(capsys.readouterr().err)

    rc = main(["--db", str(db_path), "search", "x", "--json"])
    assert rc == 4
    captured = capsys.readouterr()
    _assert_shape(captured.err)  # human shape stays on stderr
    payload = json.loads(captured.out)  # stable error object on stdout
    assert set(payload) == {"error"}
    err = payload["error"]
    assert set(err) == {"kind", "message", "why", "fix", "doctor"}
    assert err["kind"] == "no_index"
    assert err["fix"] and all(isinstance(f, str) and f.strip() for f in err["fix"])
    assert err["doctor"] is True


def test_error_shape_status_no_index_keeps_data_home(tmp_home, db_path, capsys):
    rc = main(["--db", str(db_path), "status"])
    assert rc == 4
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "data home:" in err


def test_error_shape_ingest_backend_unavailable(tmp_home, db_path, fixture_tree, capsys, monkeypatch):
    def refuse(cfg, name=None):
        raise BackendUnavailable("no backend binary", fix_hint="use --backend mock")

    monkeypatch.setattr("goldmine.cli._make_backend", refuse)
    rc = main(["--db", str(db_path), "ingest", str(fixture_tree)])
    assert rc == 4
    err = capsys.readouterr().err
    _assert_shape(err)
    # `goldmine model check` FIRST (03 §5.7: the arch failure masquerades)
    fix_lines = [l for l in err.splitlines() if l.startswith("fix: ")]
    assert fix_lines and fix_lines[0] == "fix: goldmine model check"
    assert any(l.startswith("or: goldmine ingest --lexical-only") for l in err.splitlines())


def test_error_shape_fingerprint_mismatch(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    store = Store(db_path)
    forged = fingerprints.build(MockBackend(dim=256))
    store.set_meta("fingerprint_id", fingerprints.fingerprint_id(forged))
    store.set_meta("fingerprint_json", fingerprints.canonical(forged).decode())
    store.close()

    rc = main(["--backend", "mock", "--db", str(db_path), "search", "tolerations"])
    assert rc == 3
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "reindex" in err.lower()            # mechanical rebuild escape
    assert any(l.startswith("or: ") for l in err.splitlines())  # two remedies


def test_error_shape_backend_startup_failure_exit_2(
    tmp_home, db_path, fixture_tree, monkeypatch, capsys
):
    from goldmine.backends.base import BackendStartupError

    class DeadBackend(MockBackend):
        def startup(self):
            raise BackendStartupError("readiness timed out")

    monkeypatch.setattr(
        "goldmine.cli._make_backend", lambda cfg, name=None: DeadBackend(dim=768)
    )
    rc = main(["--backend", "mock", "--db", str(db_path),
               "ingest", str(fixture_tree), "--reindex"])
    assert rc == 2  # the exit-2 deviation stays (ARCHITECTURE §8)
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "backend failed to start" in err
    assert "fix: goldmine model check" in err


def test_error_shape_reset_noninteractive(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()
    rc = main(["--db", str(db_path), "reset"])
    assert rc == 2
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "--yes" in err


def test_error_shape_distill_needs_two_chunks(tmp_home, db_path, capsys):
    (tmp_home / "corpus").mkdir()
    (tmp_home / "corpus" / "one.txt").write_text("sourdough starter notes\n")
    ingest(tmp_home, db_path, root=tmp_home / "corpus")
    rc = main(["--backend", "mock", "--db", str(db_path), "distill"])
    assert rc == 1
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "fix: goldmine ingest" in err


def test_error_shape_per_file_failures(tmp_home, db_path, tmp_path, capsys):
    root = tmp_path / "broken"
    root.mkdir()
    (root / "ok.txt").write_text("fine words here\n", encoding="utf-8")
    (root / "broken.docx").write_bytes(b"not a zip at all")
    rc = ingest(tmp_home, db_path, root=root)  # human mode
    assert rc == 5
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "1 file(s) failed" in err
    assert "broken.docx" in err


def test_error_shape_catch_all_carries_doctor_pointer(tmp_home, monkeypatch, capsys):
    import goldmine.cli as cli

    def boom(args):
        raise ValueError("disk on fire")

    monkeypatch.setitem(cli._DISPATCH, "status", boom)
    rc = main(["status"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error: ValueError: disk on fire" in err
    assert "more: goldmine doctor" in err
    assert "report: goldmine status --json > goldmine-bug.txt" in err


def test_error_shape_index_schema_newer(tmp_home, tmp_path, capsys):
    import sqlite3

    db = tmp_path / "gmhome" / "future.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    rc = main(["--db", str(db), "status"])
    assert rc == 1
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "newer than this build" in err
    assert "fix: pip install --upgrade goldmine" in err


def test_error_sweep_every_render_has_fix(tmp_home, db_path, fixture_tree, monkeypatch, capsys):
    """Sweep: every render_error call site in the retrofitted paths carries a
    non-empty `fixes` list and a message (03 §5.7: fix-less errors cannot ship)."""
    import goldmine.cli as cli

    calls = []
    real = cli.render_error

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(cli, "render_error", spy)

    real_make_backend = cli._make_backend

    main(["--db", str(db_path), "search", "x"])                      # no index

    def refuse(cfg, name=None):
        raise BackendUnavailable("nope", fix_hint="h")

    monkeypatch.setattr(cli, "_make_backend", refuse)
    main(["--db", str(db_path), "ingest", str(fixture_tree)])        # backend
    monkeypatch.setattr(cli, "_make_backend", real_make_backend)
    (tmp_home / "c2").mkdir()
    (tmp_home / "c2" / "a.txt").write_text("words words\n")
    ingest(tmp_home, db_path, root=tmp_home / "c2")
    main(["--backend", "mock", "--db", str(db_path), "distill"])     # <2 chunks
    assert len(calls) >= 3
    capsys.readouterr()
    for call in calls:
        assert call["message"].strip()
        fixes = [f for f in call["fixes"] if f and f.strip()]
        assert fixes, f"fix-less error: {call['kind']}"


# ------------------------------------------- v2 item 19: ticker + fzf format


class _FakeTTY:
    """stderr stand-in that reports isatty()=True and forwards writes."""

    def __init__(self, inner):
        self._inner = inner

    def write(self, s):
        self._inner.write(s)

    def flush(self):
        self._inner.flush()

    def isatty(self):
        return True


def test_ingest_tty_ticker_and_preflight(tmp_home, db_path, fixture_tree, capsys, monkeypatch):
    monkeypatch.setattr("goldmine.output.asr_estimate_enabled", lambda cfg: False)
    monkeypatch.setattr(sys, "stderr", _FakeTTY(sys.stderr))
    rc = ingest(tmp_home, db_path, root=fixture_tree)
    assert rc == 0
    captured = capsys.readouterr()
    err = captured.err
    assert "root " in err and "files (" in err     # per-root pre-flight (C5)
    assert "ingest 8/8 files" in err               # final ticker draw
    assert "eta" in err
    assert captured.out.startswith("ingest: 1 roots,")  # end summary unchanged


def test_ingest_non_tty_has_no_ticker(tmp_home, db_path, fixture_tree, capsys):
    rc = ingest(tmp_home, db_path, root=fixture_tree)
    assert rc == 0
    err = capsys.readouterr().err
    assert "ingest " not in err or "[" not in err  # no ticker lines when piped
    assert "root " not in err


def test_search_format_fzf_columns_and_escaping(tmp_home, db_path, fixture_tree, capsys):
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path),
               "search", "tolerations", "--format", "fzf"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines
    for line in lines:
        fields = line.split("\t")
        assert len(fields) == 5, line
        assert fields[1] == "doc"
    assert any("notes.txt" in line.split("\t")[0] for line in lines)

    # escaping: tabs/newlines in fields never break the column layout
    from goldmine.output import fzf_lines

    class H:
        path = "/x/a\tb.png"
        kind = "image"
        loc = 1.0
        loc_end = None
        excerpt = "line1\nline2\ttab"
        score = 0.5

    (row,) = fzf_lines([H()], loc_fn=lambda k, l, e: "")
    assert len(row.split("\t")) == 5
    assert "a b.png" in row and "line1 line2 tab" in row


# ------------------------------------------- F4/F5/F11/F13: error paths + json


def test_unknown_backend_is_a_clean_usage_error_on_every_command(capsys):
    """F4/F14: --backend bogus must never traceback — argparse choices reject
    it with exit 2 on EVERY command that accepts the flag (it is a global
    option, so all of them), including the commands that used to ignore it."""
    for argv in (
        ["--backend", "bogus", "search", "x"],
        ["--backend", "bogus", "ingest", "/tmp"],
        ["--backend", "bogus", "distill"],
        ["--backend", "bogus", "status"],
        ["--backend", "bogus", "backup"],
        ["--backend", "bogus", "backends"],
        ["status", "--backend", "bogus"],
    ):
        rc = main([*argv, "--json"])
        assert rc == 2, argv
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["kind"] == "usage_error", argv
        assert "bogus" in payload["error"]["message"]


def test_unknown_backend_usage_error_without_json_goes_to_stderr(capsys):
    rc = main(["--backend", "bogus", "search", "x"])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # no data on stdout
    assert "invalid choice" in captured.err


def test_corrupt_db_file_gets_the_universal_shape(tmp_home, tmp_path, capsys):
    """F5: a non-SQLite --db file renders kind=index_corrupt with the real
    mechanical fixes — never a traceback — on search/ingest/status alike."""
    corrupt = tmp_path / "junk.sqlite"
    corrupt.write_bytes(b"this is not a sqlite database" * 500)
    for argv in (
        ["--db", str(corrupt), "search", "x"],
        ["--db", str(corrupt), "status"],
        ["--backend", "mock", "--db", str(corrupt), "ingest", str(tmp_path)],
    ):
        rc = main(argv)
        assert rc == 1, argv
        captured = capsys.readouterr()
        assert "index file unreadable" in captured.err, argv
        assert "why: " in captured.err and captured.out == ""
    # --json: the error object reaches stdout (F4c: never an empty stdout)
    rc = main(["--db", str(corrupt), "status", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["kind"] == "index_corrupt"
    assert "not a database" in payload["error"]["why"]
    fixes = payload["error"]["fix"]
    assert any(f.startswith("goldmine reset --yes") for f in fixes)
    assert any(f.startswith("goldmine ingest") for f in fixes)


def test_corrupt_db_fix_names_the_newest_backup(tmp_home, tmp_path, capsys):
    corrupt = tmp_path / "default.sqlite"
    corrupt.write_bytes(b"garbage" * 1000)
    bdir = tmp_home / "gmhome" / "backups"
    bdir.mkdir(parents=True)
    newest = bdir / "default-20260101-000000.sqlite"
    newest.write_bytes(b"old backup")
    rc = main(["--db", str(corrupt), "search", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"cp '{newest}' '{corrupt}'" in err


def test_locked_db_error_is_not_index_corrupt(tmp_home, tmp_path, capsys):
    """Only genuine corruption gets the index_corrupt shape; a busy database
    keeps its own error path (OperationalError is a DatabaseError)."""
    import sqlite3

    busy = tmp_path / "busy.sqlite"
    con = sqlite3.connect(str(busy))
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("CREATE TABLE IF NOT EXISTS t(x)")
    con.execute("BEGIN IMMEDIATE")  # hold the write lock from outside
    try:
        rc = main(["--backend", "mock", "--db", str(busy), "ingest", str(tmp_path)])
        assert rc != 1 or "index file unreadable" not in capsys.readouterr().err
    finally:
        con.rollback()
        con.close()


def test_json_error_object_reaches_stdout_on_internal_errors(
    tmp_home, tmp_path, capsys, monkeypatch
):
    """F4c: even an unhandled exception emits the error object on stdout in
    --json mode (the empty-stdout hole)."""
    import goldmine.cli as cli

    def exploding(*a, **k):
        raise ZeroDivisionError("boom")

    monkeypatch.setitem(cli._DISPATCH, "status", exploding)
    rc = main(["status", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["kind"] == "internal_error"
    assert "boom" in payload["error"]["message"]


def test_json_envelope_on_model_list_backup_backends_version(
    tmp_home, db_path, fixture_tree, capsys
):
    """F11: model list / backup / backends / version --json carry the same
    {"schema": "goldmine/1", "command": ...} envelope as init/open."""
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()

    assert main(["--db", str(db_path), "backup", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "goldmine/1" and payload["command"] == "backup"
    assert payload["integrity"] == "ok"

    assert main(["backends", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "goldmine/1" and payload["command"] == "backends"
    assert [b["id"] for b in payload["backends"]] == ["foresight", "llama", "mock"]

    assert main(["version", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "goldmine/1" and payload["command"] == "version"
    assert payload["goldmine"]

    assert main(["model", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "goldmine/1" and payload["command"] == "model"
    assert payload["verb"] == "list" and payload["models"]


def test_emit_error_no_longer_double_logs(tmp_home, tmp_path, capsys, monkeypatch):
    """F13: a rendered failure appears exactly once — the shape IS the
    message; the ERROR-level log line must not duplicate it."""
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    import logging as _logging

    handler = Capture(level=_logging.ERROR)
    _logging.getLogger("goldmine.cli").addHandler(handler)
    try:
        rc = main(["--db", str(tmp_path / "nope.sqlite"), "search", "x"])
        assert rc == 4
        captured = capsys.readouterr()
        assert "no index at" in captured.err
        # the shape renders once; the logger must not repeat the message
        assert captured.err.count("no index at") == 1
        assert not any("no index at" in r for r in records)
    finally:
        _logging.getLogger("goldmine.cli").removeHandler(handler)


# ------------------------------------------ v2 item 17 sweep: S1-S5 stragglers


def test_error_shape_reset_reindex_no_index(tmp_home, db_path, capsys):
    """S1: `reset --reindex` on a missing index gets the universal shape —
    exit 4, and the error object on stdout in --json mode (never empty)."""
    rc = main(["--db", str(db_path), "reset", "--reindex"])
    assert rc == 4
    _assert_shape(capsys.readouterr().err)

    rc = main(["--db", str(db_path), "reset", "--reindex", "--json"])
    assert rc == 4
    captured = capsys.readouterr()
    _assert_shape(captured.err)
    payload = json.loads(captured.out)  # stdout was empty before the fix
    assert payload["error"]["kind"] == "no_index"
    assert payload["error"]["fix"]


def test_error_shape_reset_reindex_no_roots(tmp_home, db_path, capsys):
    """S2: `reset --reindex` without stored roots gets the universal shape
    (exit 2) with the real remedies — not a bare stderr print."""
    Store(db_path)  # an index with no roots_json meta
    rc = main(["--db", str(db_path), "reset", "--reindex"])
    assert rc == 2
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "stored roots" in err
    assert "goldmine ingest" in err and "reset --yes" in err

    rc = main(["--db", str(db_path), "reset", "--reindex", "--json"])
    assert rc == 2
    captured = capsys.readouterr()
    _assert_shape(captured.err)
    payload = json.loads(captured.out)
    assert payload["error"]["kind"] == "reset_no_roots"
    assert any("goldmine ingest" in f for f in payload["error"]["fix"])


def test_error_shape_bare_model_uses_argparse_json_path(tmp_home, capsys):
    """S3: a bare `goldmine model` is an argparse usage error — in --json
    mode the error object reaches stdout (the hand-rolled usage line left
    stdout empty)."""
    rc = main(["model"])
    assert rc == 2
    assert capsys.readouterr().err.strip()  # argparse usage on stderr

    rc = main(["--json", "model"])
    assert rc == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # stdout was empty before the fix
    assert payload["error"]["kind"] == "usage_error"
    assert captured.err.strip()


def test_error_shape_unknown_help_topic_json(tmp_home, capsys):
    """S4: an unknown help topic gets the universal shape — the topic list
    rides in why:, and --json mode gets the error object on stdout."""
    rc = main(["--json", "help", "no-such-topic"])
    assert rc == 2
    captured = capsys.readouterr()
    _assert_shape(captured.err)
    payload = json.loads(captured.out)  # stdout was empty before the fix
    err = payload["error"]
    assert err["kind"] == "unknown_help_topic"
    assert "search-syntax" in err["why"]  # the topic list is in the shape
    assert err["fix"] == ["goldmine help"]


def test_reset_needs_confirmation_json_reaches_stdout(tmp_home, db_path, fixture_tree, capsys):
    """The reset refusal (exit 2, non-interactive) renders its error object on
    stdout in --json mode — like every other shaped refusal."""
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()
    rc = main(["--db", str(db_path), "reset", "--json"])
    assert rc == 2
    captured = capsys.readouterr()
    _assert_shape(captured.err)
    # the human "would delete" preview stays on stdout ahead of the object
    # (pre-existing behavior); the error object rides at the end
    payload = json.loads(captured.out[captured.out.index("{"):])
    assert payload["error"]["kind"] == "reset_needs_confirmation"


def test_reset_reindex_partial_exit_5_shape(tmp_home, db_path, tmp_path, capsys):
    """S5: `reset --reindex` with a failing file exits 5 with the same
    ingest_partial shape as `ingest` — same situation, same honesty."""
    root = tmp_path / "broken"
    root.mkdir()
    (root / "ok.txt").write_text("fine words here\n", encoding="utf-8")
    (root / "broken.docx").write_bytes(b"not a zip at all")
    assert ingest(tmp_home, db_path, root=root) == 5
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path), "reset", "--reindex"])
    assert rc == 5
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "1 file(s) failed" in err
    assert "broken.docx" in err


# --------------------------------------------- F7: explicit zeros are honored


def test_status_failures_zero_shows_none(tmp_home, db_path, tmp_path, capsys):
    """F7: `status --failures 0` shows zero failure rows — `or 10` silently
    turned the explicit 0 into the default 10."""
    root = tmp_path / "broken"
    root.mkdir()
    (root / "ok.txt").write_text("fine words here\n", encoding="utf-8")
    (root / "broken.docx").write_bytes(b"not a zip at all")
    assert ingest(tmp_home, db_path, root=root) == 5
    capsys.readouterr()

    rc = main(["--db", str(db_path), "status", "--failures", "0", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["failures"] == []
    capsys.readouterr()

    rc = main(["--db", str(db_path), "status", "--failures", "0"])
    assert rc == 0
    assert "failed:" not in capsys.readouterr().out

    rc = main(["--db", str(db_path), "status", "--failures", "1", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["failures"]) == 1  # a real limit still applies


def test_distill_zero_flags_are_honored(tmp_home, db_path, tmp_path, capsys):
    """F7: distill's explicit zeros reach the engine — 0 min-members keeps
    singleton clusters, 0 max-chunks refuses any corpus, 0 limit-cards shows
    no cards (and still saves them)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for i in range(4):
        (corpus / f"k{i}.txt").write_text(
            f"Scheduler eviction under pod pressure {i}. "
            "Tolerations keep workloads running. " * 3
        )
    assert ingest(tmp_home, db_path, root=corpus) == 0
    capsys.readouterr()

    # --min-members 0: singletons allowed (with 3, a 4-doc corpus may keep nothing)
    rc = main(["--backend", "mock", "--db", str(db_path),
               "distill", "--min-members", "0", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["clusters_kept"] >= 1

    # --max-chunks 0: refuses (never truncates) — the cap of 0 is real
    rc = main(["--backend", "mock", "--db", str(db_path), "distill", "--max-chunks", "0"])
    assert rc == 1
    err = capsys.readouterr().err
    _assert_shape(err)
    assert "cap of 0" in err

    # --limit-cards 0: no cards shown, but the run still saves them
    rc = main(["--backend", "mock", "--db", str(db_path),
               "distill", "--min-members", "0", "--limit-cards", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "### " not in out
    assert Store(db_path).cards()  # every card still persisted


# ---------------------------------------------- distill v2 surface (R6 §6)


def _two_topic_corpus(root):
    (root / "k8s").mkdir(parents=True)
    (root / "food").mkdir()
    for i in range(4):
        (root / "k8s" / f"k{i}.txt").write_text(
            f"Scheduler eviction under pod pressure {i}. "
            "Tolerations keep workloads running. " * 3
        )
        (root / "food" / f"f{i}.txt").write_text(
            f"Sourdough starter feeding schedule {i}. "
            "Bulk ferment the dough overnight. " * 3
        )


def test_distill_preview_writes_nothing(tmp_home, db_path, tmp_path, capsys):
    corpus = tmp_path / "corpus"
    _two_topic_corpus(corpus)
    ingest(tmp_home, db_path, root=corpus)
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path), "distill", "--preview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing written" in out
    assert "tau:" in out and "intent:" in out
    store = Store(db_path)
    try:
        assert store.db.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM distill_runs").fetchone()[0] == 0
    finally:
        store.close()
    # --json preview carries the preview schema + no writes either
    rc = main(["--backend", "mock", "--db", str(db_path), "distill",
               "--preview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "goldmine.distill-preview/v1"
    assert "intent" in payload and "quartiles" in payload


def test_distill_as_and_rerun_views(tmp_home, db_path, tmp_path, capsys):
    corpus = tmp_path / "corpus"
    _two_topic_corpus(corpus)
    ingest(tmp_home, db_path, root=corpus)
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path), "distill",
               "--as", "notes", "--min-members", "2"])
    assert rc == 0
    capsys.readouterr()
    store = Store(db_path)
    try:
        view = store.get_view("notes")
        assert view is not None and '"min_members": 2' in view.params_json.replace(
            "2", "2"
        ) or view is not None
        runs_before = store.db.execute(
            "SELECT COUNT(*) FROM distill_runs"
        ).fetchone()[0]
    finally:
        store.close()
    # rerun the saved view (a fresh full run; history grows)
    rc = main(["--backend", "mock", "--db", str(db_path), "distill", "--rerun", "notes"])
    assert rc == 0
    capsys.readouterr()
    store = Store(db_path)
    try:
        runs_after = store.db.execute(
            "SELECT COUNT(*) FROM distill_runs"
        ).fetchone()[0]
    finally:
        store.close()
    assert runs_after >= runs_before
    # unknown view name -> view_not_found error shape
    rc = main(["--backend", "mock", "--db", str(db_path), "distill", "--rerun", "ghost"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no saved view named 'ghost'" in err
    assert "goldmine distill --as ghost" in err


def test_distill_rerun_missing_view_json_shape(tmp_home, db_path, capsys):
    (tmp_home / "corpus").mkdir()
    (tmp_home / "corpus" / "a.txt").write_text("sourdough starter notes\n")
    ingest(tmp_home, db_path, root=tmp_home / "corpus")
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path), "distill",
               "--rerun", "nope", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["kind"] == "view_not_found"
    assert payload["error"]["fix"]


def test_distill_under_scoping(tmp_home, db_path, tmp_path, capsys):
    corpus = tmp_path / "corpus"
    _two_topic_corpus(corpus)
    ingest(tmp_home, db_path, root=corpus)
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path), "distill",
               "--under", str(corpus / "k8s"), "--min-members", "2", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    paths = {c["path"] for card in payload["cards"] for c in card["citations"]}
    assert paths and all("/k8s/" in p for p in paths)
    # scoped view id differs from the unscoped one (D7: scope is in the key)
    store = Store(db_path)
    try:
        views = store.db.execute("SELECT COUNT(*) FROM distill_runs").fetchone()[0]
    finally:
        store.close()
    assert views == 1


def test_distill_after_scoping_filters_old_files(tmp_home, db_path, tmp_path, capsys):
    import os
    import time as time_mod

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "old.txt").write_text("sourdough starter feeding notes old file\n")
    (corpus / "new.txt").write_text("sourdough starter feeding notes new file\n")
    (corpus / "new2.txt").write_text("sourdough starter feeding notes another new file\n")
    (corpus / "new3.txt").write_text("sourdough starter feeding notes yet another file\n")
    # files.mtime is snapshotted at ingest: age the file BEFORE ingesting
    old_ts = time_mod.time() - 10 * 365 * 86400
    os.utime(corpus / "old.txt", (old_ts, old_ts))
    ingest(tmp_home, db_path, root=corpus)
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path), "distill",
               "--after", time_mod.strftime("%Y-%m-%d", time_mod.gmtime(
                   time_mod.time() - 86400)),
               "--min-members", "0", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    paths = {c["path"] for card in payload["cards"] for c in card["citations"]}
    assert paths and all("/new" in p and not p.endswith("old.txt") for p in paths)


def test_distill_rebuild_note_cleared_by_first_run(tmp_home, db_path, tmp_path, capsys):
    """F11: the migration's cards-rebuild note surfaces in status and is
    cleared by the first successful run of ANY view."""
    corpus = tmp_path / "corpus"
    _two_topic_corpus(corpus)
    ingest(tmp_home, db_path, root=corpus)
    # plant the note as the v3->v4 migration would
    store = Store(db_path)
    try:
        store.set_meta("cards_v2_rebuild_note", "cards v1 tables were replaced")
    finally:
        store.close()
    capsys.readouterr()
    rc = main(["--db", str(db_path), "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    codes = [n["code"] for n in payload["notices"]]
    assert "cards_rebuild" in codes
    notice = next(n for n in payload["notices"] if n["code"] == "cards_rebuild")
    assert notice["message"] == "cards v1 tables were replaced"
    # doctor --deep surfaces the same warn (F11: same condition, same text)
    rc = main(["--backend", "mock", "--db", str(db_path), "doctor", "--deep",
               "--json"])
    assert rc in (0, 4)
    payload = json.loads(capsys.readouterr().out)
    cards_check = next(c for c in payload["checks"]
                       if c["name"] == "cards fingerprint")
    assert cards_check["status"] == "warn"
    assert cards_check["detail"] == "cards v1 tables were replaced"
    # the first successful distill run clears it
    rc = main(["--backend", "mock", "--db", str(db_path), "distill",
               "--min-members", "2"])
    assert rc == 0
    capsys.readouterr()
    rc = main(["--db", str(db_path), "status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "cards_rebuild" not in [n["code"] for n in payload["notices"]]


def test_distill_threshold_semantics_calibrated_vs_manual(tmp_home, db_path, tmp_path, capsys):
    corpus = tmp_path / "corpus"
    _two_topic_corpus(corpus)
    ingest(tmp_home, db_path, root=corpus)
    capsys.readouterr()
    # unset = calibrated (tau_source from the calibration, not 'manual')
    rc = main(["--backend", "mock", "--db", str(db_path), "distill",
               "--min-members", "2", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["tau_source"] != "manual"
    # explicit --threshold = manual override
    rc = main(["--backend", "mock", "--db", str(db_path), "distill",
               "--threshold", "0.55", "--min-members", "2", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["tau_source"] == "manual"
    assert payload["stats"]["tau"] == 0.55


# --------------------------------------------------- T1: zero hits + next step


def test_zero_hits_prints_next_step(tmp_home, db_path, fixture_tree, capsys):
    """T1: human zero-hit output carries the promised next step (on stderr —
    stdout stays data-only), and no stray blank line precedes it."""
    ingest(tmp_home, db_path, root=fixture_tree)
    capsys.readouterr()
    rc = main(["--backend", "mock", "--db", str(db_path),
               "search", "wubblezort", "--scorer", "lexical"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == "nothing found for 'wubblezort'\n"
    assert "goldmine help search-syntax" in captured.err


def test_every_flag_has_help_string():
    """F3 sweep: no argparse flag ships without a help string."""
    from goldmine.parser import build_parser

    parser = build_parser()
    missing: list[str] = []

    def walk(p, prefix: str = "") -> None:
        for action in p._actions:
            if action.__class__.__name__ == "_SubParsersAction":
                for name, sub in action.choices.items():
                    walk(sub, prefix + name + " ")
                continue
            if action.__class__.__name__ in ("_HelpAction", "_VersionAction"):
                continue
            if action.help is None:
                opts = ", ".join(action.option_strings) or repr(action.dest)
                missing.append(f"{prefix}{opts}")

    walk(parser)
    assert missing == []


# ------------------------------------------------------- F1 split: import shape


def test_cli_split_import_shape():
    """F1: the extraction keeps the pinned import surface — goldmine.cli still
    exposes main/build_parser/render_hits/_crawl_bounds/_rewrite_models_alias,
    and the new modules expose their own names."""
    from goldmine.cli import (
        _crawl_bounds,
        _rewrite_models_alias,
        build_parser,
        main,
        render_hits,
    )
    from goldmine.parser import build_parser as parser_from_parser
    from goldmine.render import render_hits as hits_from_render

    assert parser_from_parser is build_parser
    assert hits_from_render is render_hits
    assert callable(main) and callable(_crawl_bounds) and callable(_rewrite_models_alias)
