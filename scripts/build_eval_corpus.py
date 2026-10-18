#!/usr/bin/env python3
"""Build the real-corpus eval index (v2 item D1, docs/v2/01-search-relevance.md §5.1).

The eval corpus is REAL files on this machine, hash-pinned by
`evals/corpus_manifest.json` (the committed, verifiable part). External repos
(DigUp, foresight-rea) are pinned live — they are stable. The GOLDMINE-SELF
slice is pinned as committed BYTES: `--refresh-manifest` copies each pinned
goldmine file into `evals/corpus_snapshot/` and the manifest records the
snapshot paths, so a normal dev day (editing search.py, store.py, …) no
longer invalidates the corpus or the labels (04-eval F5: the v1 manifest
drifted in 11 self files within one day). foresight-rea still stays
byte-local — its files are NEVER copied into the repo (decompiled
third-party code, docs/v2/01 §7 risk 9).

Sources:
  1. ~/git/DigUp            — Swift app + docs (fusion/search provenance)
  2. ~/git/mac/work/foresight-rea/app-src — reverse-engineered Foresight app
     (docs in Chinese + English, C++, Dart, Python, proto)
  3. evals/corpus_snapshot/ — goldmine itself, snapshot-pinned (this repo's
     package sources + docs, bytes as of the last --refresh-manifest)
  4. the coverage fixture   — scripts/make_coverage_fixture.py output
     (deterministic, seeded; regenerated if absent) for media / pdf-page /
     noise labels (evals/media_queries.jsonl already points at it)

What this tool does:
  --refresh-manifest  snapshot the goldmine-self files, rewrite
                      evals/corpus_manifest.json from the curated lists
                      (sha256 + size per file; fixture + its version record)
  --verify            re-hash every manifest file, report drift, exit 1 on any
                      (labels referencing a drifted corpus are stale)
  (default)           ingest the corpus into the eval index via the SAME
                      pipeline as `python -m goldmine.cli ingest` (code on),
                      then verify the manifest. Idempotent: unchanged files
                      are skipped by the normal sha256/mtime path.

Usage:
  python3 scripts/build_eval_corpus.py --refresh-manifest
  python3 scripts/build_eval_corpus.py --verify
  python3 scripts/build_eval_corpus.py                     # mock backend
  python3 scripts/build_eval_corpus.py --backend llama     # real embeddings
  python3 scripts/build_eval_corpus.py --subset 10 --db /tmp/eval.sqlite
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from goldmine.config import Config  # noqa: E402
from goldmine.ingest import IngestPipeline  # noqa: E402
from goldmine.store import Store  # noqa: E402

EVALS_DIR = REPO_ROOT / "evals"
DEFAULT_MANIFEST = EVALS_DIR / "corpus_manifest.json"
DEFAULT_DB = EVALS_DIR / "eval.sqlite"
DEFAULT_FIXTURE = Path("/tmp/gm_covfix")
SNAPSHOT_DIR = EVALS_DIR / "corpus_snapshot"

CORPUS_VERSION = "v2-2026-10-17"

# ------------------------------------------------------------- curated sources
# Curated subsets of the three real repos. Ingestion takes the whole source
# dirs (realistic distractor files included — a real disk has noise); the
# manifest pins the files the LABELS depend on, so label-relevant drift is
# loud while the index stays a real corpus.

DIGUP_ROOT = Path.home() / "git" / "DigUp"
DIGUP_FILES = [
    "README.md", "CONTRIBUTING.md", "Package.swift",
    # DigUpKit (search/index core — the fusion provenance)
    "Sources/DigUpKit/Searcher.swift",
    "Sources/DigUpKit/Indexer.swift",
    "Sources/DigUpKit/CodeExtractor.swift",
    "Sources/DigUpKit/SearchText.swift",
    "Sources/DigUpKit/Vectors.swift",
    "Sources/DigUpKit/Rules.swift",
    "Sources/DigUpKit/Database.swift",
    "Sources/DigUpKit/IndexStore.swift",
    "Sources/DigUpKit/Model.swift",
    "Sources/DigUpKit/ModelFiles.swift",
    "Sources/DigUpKit/Extractors.swift",
    "Sources/DigUpKit/MediaExtractors.swift",
    "Sources/DigUpKit/Estimator.swift",
    "Sources/DigUpKit/Crawler.swift",
    "Sources/DigUpKit/FolderSelection.swift",
    "Sources/DigUpKit/Worker.swift",
    # LlamaRuntime (llama.cpp embedding host)
    "Sources/LlamaRuntime/LlamaEmbedder.swift",
    "Sources/LlamaRuntime/LlamaModels.swift",
    "Sources/LlamaRuntime/ImageSizing.swift",
    # CLI entry (eval runner reference)
    "Sources/digup/main.swift",
    "Sources/digup/IndexService.swift",
    "Sources/digup/WorkerService.swift",
    # App layer subset
    "Sources/DigUpApp/AppModel.swift",
    "Sources/DigUpApp/Engine.swift",
    "Sources/DigUpApp/SearchPanel.swift",
    "Sources/DigUpApp/ModelDownload.swift",
    "Sources/DigUpApp/FolderWatcher.swift",
    "Sources/DigUpApp/PowerPolicy.swift",
    "Sources/DigUpApp/Onboarding.swift",
    "Sources/DigUpApp/Thumbnails.swift",
    "Sources/DigUpApp/CodeEditors.swift",
    "Sources/DigUpApp/Estimates.swift",
    # tests + fixtures + evals DigUp itself ships
    "Tests/DigUpKitTests/SearchTextTests.swift",
    "Tests/DigUpKitTests/CodeTests.swift",
    "Tests/DigUpKitTests/LongFilesTests.swift",
    "scripts/testbed/make_docs.swift",
    "evals/multilingual.json",
    "evals/code.json",
    "worker/digup_worker.py",
]

FORESIGHT_ROOT = Path.home() / "git" / "mac" / "work" / "foresight-rea" / "app-src"
FORESIGHT_FILES = [
    "README.md", "Makefile",
    "docs/architecture.md",
    "docs/rpc-contracts.md",
    "docs/live-findings.md",
    "docs/evidence-map.md",
    "docs/CORRECTIONS.md",
    "docs/DEPENDENCIES.md",
    "proto/rag_service.proto",
    "native/launch_agent/main.cc",
    "native/launch_agent/rag_service_impl.h",
    "native/launch_agent/rag_service_impl.cc",
    "native/launch_agent/retrieval_pipeline.cc",
    "native/launch_agent/rag_sqlite_memory_store.cc",
    "native/launch_agent/sqlite_vector_udf.cc",
    "native/launch_agent/chunking.cc",
    "native/launch_agent/model_loader.cc",
    "native/launch_agent/embedding_gemma_api.cc",
    "native/launch_agent/model_paths.cpp",
    "server/foresight_server.py",
    "app/demo_cli.py",
    "tests/test_server.py",
    "tests/live_test.py",
    "ui/MODULES.md",
    "ui/STATE-MACHINES.md",
    "ui/prompts.md",
    "ui/model-catalog.json",
    "ui/src/rag_orchestrator.dart",
    "ui/src/audio_pipeline.dart",
    "ui/src/graph_and_mapreduce.dart",
    "ui/src/chat_insight_service.dart",
    "ui/src/inference_pipeline.dart",
    "ui/src/notes_enhancer.dart",
    "ui/src/model_catalog.dart",
    "ui/src/model_download_controller.dart",
    "ui/src/models/contact.dart",
    "ui/src/models/document.dart",
    "ui/src/models/insight.dart",
    "ui/src/models/structured_transcript.dart",
    "ui/src/models/meeting_transcript_link.dart",
    "ui/src/models/on_device_model.dart",
]

GOLDMINE_FILES = [  # relative to REPO_ROOT; snapshot-copied at refresh
    "README.md", "ARCHITECTURE.md", "SPEC.md",
    "goldmine/search.py",
    "goldmine/store.py",
    "goldmine/chunker.py",
    "goldmine/config.py",
    "goldmine/fingerprints.py",
    "goldmine/distill.py",
    "goldmine/util.py",
    "goldmine/migrations.py",
    "goldmine/backends/base.py",
    "goldmine/backends/llama.py",
    "goldmine/backends/mock.py",
    "goldmine/ingest/pipeline.py",
    "goldmine/ingest/walk.py",
    "goldmine/ingest/rules.py",
    "goldmine/ingest/extract_pdf.py",
    "goldmine/ingest/extract_text.py",
    "goldmine/ingest/media.py",
    "docs/v2/REDESIGN.md",
    "docs/v2/01-search-relevance.md",
]
# scripts/eval_relevance.py was REMOVED from the corpus (v2): it quotes the
# noise-query strings verbatim (see the ZERO_HIT_QUERY constant and the
# evals/real.jsonl noise ids), so indexing it made real-110 structurally
# unpassable — any working engine must rank the leak (04-eval F6). Its former
# label query (real-045) is retired with it. NOTE: never quote a noise query's
# literal text in an ingested file — the leak resurrects itself (v2 rebuild
# found exactly that in this file's own comment).

#: directory roots ingested into the eval index (whole trees, realistic noise
#: included). The live copies of the snapshot-PINNED files are excluded at
#: ingest (see build_index) so the snapshot copy is the single indexed
#: instance of each — label paths and label-relevant bytes stay durable while
#: the rest of the tree keeps its realistic-drift noise.
INGEST_DIR_ROOTS = [
    DIGUP_ROOT,
    FORESIGHT_ROOT,
    REPO_ROOT / "goldmine",
    REPO_ROOT / "docs",
    REPO_ROOT / "scripts",
]

SOURCE_NOTES = {
    "digup": "DigUp Swift app — the fusion/z-score/setsScale provenance goldmine re-implements",
    "foresight": "foresight-rea app-src — reverse-engineered Google AI Edge Foresight (CJK docs, C++/Dart/Python/proto); LOCAL-ONLY, never commit bytes",
    "goldmine": "goldmine itself (package + docs)",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _file_entry(path: Path, source: str) -> dict:
    return {
        "path": str(path),
        "source": source,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


# ------------------------------------------------------------ fixture handling

def _ensure_fixture(root: Path) -> dict:
    """The coverage fixture (seed 20261016). Returns its manifest dict merged
    with the fixture_version.json staleness record, (re)building the fixture
    when absent (deterministic, offline)."""
    marker = root / "manifest.json"
    if not marker.is_file():
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import make_coverage_fixture  # noqa: E402

        print(f"fixture absent at {root} — regenerating (seeded, offline)")
        make_coverage_fixture.build(root, seed=make_coverage_fixture.SEED, force=True)
    fix = json.loads(marker.read_text())
    version_path = root / "fixture_version.json"
    if version_path.is_file():
        # the staleness fingerprint (version + generator mtime + file count)
        # rides in the manifest: a regenerated fixture invalidates the
        # corpus_sha loudly (04-eval F5)
        fix["version"] = json.loads(version_path.read_text())
    return fix


def _fixture_entries(root: Path, fix_manifest: dict) -> list[dict]:
    out = []
    for f in fix_manifest.get("files", []):
        p = root / f["path"]
        if not p.is_file():
            continue
        entry = {"path": f["path"], "kind": f.get("kind", "doc"),
                 "bytes": p.stat().st_size}
        # say-TTS / ffmpeg output is machine-dependent: existence-only checks
        # (marked requires) — everything else is seeded-deterministic, so its
        # hash pins the corpus.
        if f.get("requires"):
            entry["requires"] = f["requires"]
        else:
            entry["sha256"] = _sha256(p)
        out.append(entry)
    return out


# --------------------------------------------------------- self-source snapshot

def _snapshot_goldmine() -> list[str]:
    """Copy the CURRENT bytes of every pinned goldmine-self file into
    evals/corpus_snapshot/<rel> and return the rel paths. The snapshot is the
    label-relevant corpus: verify_manifest hashes it (never drifts on a dev
    day), the index ingests it, and labels reference its paths."""
    rels: list[str] = []
    for rel in GOLDMINE_FILES:
        src = REPO_ROOT / rel
        if not src.is_file():
            raise SystemExit(f"ERROR: goldmine snapshot source missing: {src}")
        dst = SNAPSHOT_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        rels.append(rel)
    return rels


# --------------------------------------------------------------- manifest I/O

def write_manifest(manifest_path: Path, fixture_root: Path) -> dict:
    sections: list[tuple[str, Path, list[str]]] = [
        ("digup", DIGUP_ROOT, DIGUP_FILES),
        ("foresight", FORESIGHT_ROOT, FORESIGHT_FILES),
    ]
    files: list[dict] = []
    for source, root, rels in sections:
        missing = [r for r in rels if not (root / r).is_file()]
        if missing:
            raise SystemExit(
                f"ERROR: curated {source} files missing on this machine: {missing}"
            )
        files.extend(_file_entry(root / r, source) for r in rels)

    # goldmine-self: snapshot FIRST, then pin the snapshot bytes. "origin"
    # keeps the live path for humans; labels reference the snapshot path.
    snapshot_rels = _snapshot_goldmine()
    for rel in snapshot_rels:
        snap = SNAPSHOT_DIR / rel
        entry = _file_entry(snap, "goldmine")
        entry["origin"] = str(REPO_ROOT / rel)
        files.append(entry)

    fix_manifest = _ensure_fixture(fixture_root)
    fixture_entries = _fixture_entries(fixture_root, fix_manifest)

    manifest = {
        "schema": 1,
        "corpus_version": CORPUS_VERSION,
        "created": "2026-10-17",
        "ingest_dir_roots": [str(p) for p in INGEST_DIR_ROOTS] + [
            str(REPO_ROOT / name) for name in ("README.md", "ARCHITECTURE.md", "SPEC.md")
        ] + [str(SNAPSHOT_DIR)],
        "fixture": {
            "root": str(fixture_root),
            "generator": "scripts/make_coverage_fixture.py",
            "seed": fix_manifest.get("seed"),
            "version": fix_manifest.get("version"),
            "files": fixture_entries,
        },
        "sources": {
            "digup": {"root": str(DIGUP_ROOT), "note": SOURCE_NOTES["digup"]},
            "foresight": {"root": str(FORESIGHT_ROOT),
                          "note": SOURCE_NOTES["foresight"]},
            "goldmine": {"root": str(SNAPSHOT_DIR),
                         "note": SOURCE_NOTES["goldmine"] +
                                 " — snapshot-pinned bytes (evals/corpus_snapshot/)"},
        },
        "files": files,
        "notes": [
            "goldmine-self corpus files are snapshot-pinned under "
            "evals/corpus_snapshot/ ('origin' holds the live path); the index "
            "and the labels consume the snapshot, so dev-day edits cannot rot "
            "them — refresh the manifest deliberately to move the snapshot",
            "foresight-rea is decompiled third-party code — local-only, its "
            "bytes are never copied into the repo (docs/v2/01 risk 9)",
            "fixture files marked 'requires' (say/ffmpeg) are existence-checked only",
            "fixture 'version' record (version + generator mtime + file count) "
            "is a corpus_sha input: a regenerated fixture invalidates loudly",
            "scripts/eval_relevance.py and docs/TEST_PLAN.md are excluded from "
            "the index — they quote the noise-query strings (self-leak, F6)",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def verify_manifest(manifest: dict) -> list[str]:
    """Returns a list of drift problems (empty = clean)."""
    problems: list[str] = []
    for entry in manifest["files"]:
        p = Path(entry["path"])
        if not p.is_file():
            problems.append(f"missing: {p}")
            continue
        if p.stat().st_size != entry["bytes"]:
            problems.append(f"size drift: {p}")
        elif _sha256(p) != entry["sha256"]:
            problems.append(f"sha256 drift: {p}")
    fixture = manifest.get("fixture") or {}
    root = Path(fixture.get("root", str(DEFAULT_FIXTURE)))
    if not root.is_dir():
        problems.append(f"fixture root missing: {root} (rerun build_eval_corpus.py)")
    else:
        recorded_version = fixture.get("version")
        version_path = root / "fixture_version.json"
        if recorded_version is not None:
            if not version_path.is_file():
                problems.append(f"fixture version record missing: {version_path}")
            elif json.loads(version_path.read_text()) != recorded_version:
                problems.append(
                    f"fixture version drift: {version_path} differs from the "
                    "manifest record — the fixture was regenerated; refresh "
                    "the manifest and re-baseline deliberately")
        for entry in fixture.get("files", []):
            p = root / entry["path"]
            if not p.is_file():
                problems.append(f"fixture missing: {p}")
            elif "sha256" in entry and _sha256(p) != entry["sha256"]:
                problems.append(f"fixture sha256 drift: {p} "
                                f"(requires={entry.get('requires', '-')})")
    return problems


def corpus_sha(manifest: dict) -> str:
    """Stable hash over the pinned corpus (corpus_version + sorted file hashes
    + the fixture version record)."""
    h = hashlib.sha256()
    h.update(manifest["corpus_version"].encode())
    for entry in sorted(manifest["files"], key=lambda e: e["path"]):
        h.update(entry["path"].encode())
        h.update(entry["sha256"].encode())
    fixture = manifest.get("fixture") or {}
    # the fixture's staleness fingerprint is a sha input: regenerating the
    # fixture (new version/generator/file-count record) invalidates loudly
    h.update(json.dumps(fixture.get("version"), sort_keys=True).encode())
    for entry in sorted(fixture.get("files", []), key=lambda e: e["path"]):
        h.update(entry["path"].encode())
        h.update(entry.get("sha256", "requires-only").encode())
    return h.hexdigest()[:16]


# ------------------------------------------------------------------- ingest

def _patch_walk_file_roots() -> None:
    """walk.py's file-root branch calls an undefined `_handle_file` (latent
    NameError — walk.py:74). We ingest three individual goldmine root-level
    .md files as roots, so install the obviously-intended helper here instead
    of editing walk.py (owned by another wave; noted in the build report)."""
    import importlib

    from goldmine.ingest import rules
    # goldmine.ingest/__init__ re-exports the walk() FUNCTION as `walk`,
    # shadowing the submodule — importlib is the reliable way to the module
    walk_mod = importlib.import_module("goldmine.ingest.walk")

    if getattr(walk_mod, "_handle_file", None) is not None:
        return  # fixed upstream — nothing to do

    def _handle_file(root: Path, *, code: bool, max_file_bytes: int):
        name = root.name
        if rules.is_secret(name):
            yield walk_mod.SkipNotice(str(root), "key or password file")
            return
        if rules.looks_minified(name):
            yield walk_mod.SkipNotice(str(root), "minified")
            return
        if rules.is_license(name):
            yield walk_mod.SkipNotice(str(root), "license")
            return
        st = root.stat()
        if st.st_size > max_file_bytes:
            yield walk_mod.SkipNotice(str(root), "size cap")
            return
        ext = root.suffix.lstrip(".").lower()
        head = (walk_mod._read_head(root) if ext in rules.TS_EXTS else None)
        decision = rules.classify(name, ext, code=code, head=head)
        if decision.kind is not None:
            yield walk_mod.Candidate(path=root, kind=decision.kind,
                                    size=st.st_size, mtime=st.st_mtime)
        else:
            yield walk_mod.SkipNotice(str(root),
                                      decision.skip_reason or "unknown type")

    walk_mod._handle_file = _handle_file


def build_index(db: Path, backend_name: str, manifest: dict, *,
                subset: int | None = None) -> dict:
    from goldmine.backends import create_backend

    fixture_root = Path(manifest["fixture"]["root"])
    roots: list[str] = list(manifest["ingest_dir_roots"]) + [str(fixture_root)]
    if subset is not None:
        # tests: a curated N-file subset (manifest order) + the fixture root
        # (small, deterministic; keeps pdf-page/media labels addressable)
        roots = [str(Path(e["path"]).resolve())
                 for e in manifest["files"][:subset]]
        roots.append(str(fixture_root))

    _patch_walk_file_roots()
    cfg = Config(home=str(db.parent / ".evalhome"))
    cfg.backend = backend_name
    store = Store(db)
    backend = create_backend(cfg, backend_name)
    try:
        pipeline = IngestPipeline(cfg, store, backend)
        # label-leakage guards: the fixture GENERATOR contains every fixture
        # sentence verbatim, and the fixture's own manifest.json lists every
        # fixture filename — neither may be searchable, or fixture queries
        # rank the metadata above the fixture files. Same disease for the
        # noise labels (04-eval F6): eval_relevance.py and TEST_PLAN.md quote
        # the noise-query strings verbatim, so indexing them makes real-110
        # structurally unpassable. Finally, the live copies of the
        # snapshot-pinned goldmine files are excluded so the snapshot copy is
        # the single indexed instance (no duplicate-content ranking races).
        report = pipeline.run(roots, code=True,
                              exclude=["make_coverage_fixture.py",
                                       "manifest.json",
                                       "eval_relevance.py",
                                       "TEST_PLAN.md"]
                              + [str(REPO_ROOT / rel) for rel in GOLDMINE_FILES])
    finally:
        store.close()
        backend.close()
    counts = _quick_counts(db)
    return {"report": report.to_json(), "counts": counts}


def _quick_counts(db: Path) -> dict:
    store = Store(db)
    try:
        c = store.counts()
        return {"files": c.files, "segments": c.segments, "vectors": c.vectors}
    finally:
        store.close()


# ---------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--backend", default="mock", help="mock | llama")
    parser.add_argument("--fixture-root", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--refresh-manifest", action="store_true",
                        help="rewrite the manifest from the curated lists")
    parser.add_argument("--verify", action="store_true",
                        help="verify manifest hashes; exit 1 on drift")
    parser.add_argument("--subset", type=int, default=None,
                        help="ingest only the first N manifest files (tests)")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    if args.refresh_manifest:
        manifest = write_manifest(manifest_path, Path(args.fixture_root))
        n_fix = len(manifest["fixture"]["files"])
        print(f"manifest: {manifest_path} — {len(manifest['files'])} pinned files "
              f"+ {n_fix} fixture files, corpus_version {manifest['corpus_version']}")
        print(f"goldmine-self snapshot: {SNAPSHOT_DIR} "
              f"({len(manifest['ingest_dir_roots'])} ingest roots, snapshot "
              "bytes pinned)")
        return 0

    if not manifest_path.is_file():
        print(f"no manifest at {manifest_path} — run with --refresh-manifest first",
              file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())

    drift = verify_manifest(manifest)
    if drift:
        print(f"corpus drift ({len(drift)}):", file=sys.stderr)
        for d in drift[:20]:
            print(f"  {d}", file=sys.stderr)
        if args.verify:
            return 1
        print("labels reference a drifted corpus — refusing to ingest "
              "(refresh the manifest consciously and review labels)",
              file=sys.stderr)
        return 1
    if args.verify:
        print(f"verify: clean — {len(manifest['files'])} files, "
              f"corpus_sha {corpus_sha(manifest)}")
        return 0

    db = Path(args.db)
    result = build_index(db, args.backend, manifest, subset=args.subset)
    rep = result["report"]
    files = rep["files"]
    print(
        f"built: {db} — scanned {files['scanned']}, content-indexed "
        f"{files['content_indexed']}, metadata-only {files['metadata_only']}, "
        f"unsupported {files['unsupported']}, failed {files['failed']} "
        f"(backend {args.backend})"
    )
    print(f"index: {result['counts']}")
    return 0 if files["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
