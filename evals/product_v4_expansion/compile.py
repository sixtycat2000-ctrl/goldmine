"""Assemble source-curated shards into a DRAFT bundle; never score or freeze it.

Run from any working directory with Python 3.12+. Rebuilding a FROZEN corpus is
refused. Asset/oracle/licence paths remain corpus-relative and content-addressed.
"""
from collections import Counter
from pathlib import Path
import hashlib
import json
import shutil
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from product_eval.bundle import _validate_source, load_bundle, REFERENCE_COMMIT
from product_eval.coverage import audit_sample_ids, coverage_missing
from product_eval.splits import make_splits, source_groups


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ref(path):
    return {"path": path.relative_to(HERE).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def copy(fragment, relative):
    src, dst = fragment / relative, HERE / relative
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.read_bytes() == src.read_bytes():
        return
    shutil.copyfile(src, dst)


def main():
    manifest_path = HERE / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text())["state"] == "FROZEN":
        raise SystemExit("Refuse to rebuild an immutable FROZEN bundle; curate a distinct future suite.")
    sources, queries, views = {}, [], {}
    for name in ("text", "compound", "media", "root", "noise"):
        fragment = HERE / name
        shard = json.loads((fragment / "shard.json").read_text())
        for source in shard["sources"]:
            if source["path"] in sources:
                raise ValueError("duplicate source path: " + source["path"])
            for relative in ([source["asset"]["path"], source["provenance"]["license_file"]["path"]]
                             + [view["path"] for view in source["views"]]):
                copy(fragment, relative)
            sources[source["path"]] = source
            _validate_source(HERE, source, views)
        if (fragment / "licenses").exists():
            for path in (fragment / "licenses").rglob("*"):
                if path.is_file():
                    copy(fragment, path.relative_to(fragment))
        queries.extend(shard["queries"])
    # Stable/staging documentation URLs can change. Retain the exact publisher
    # PDF bytes outside assets/ so page transformations remain reproducible.
    for path in (HERE / "root/provenance/original-pdfs").glob("*.pdf"):
        copy(HERE / "root", path.relative_to(HERE / "root"))
    queries.sort(key=lambda row: row["id"])
    mapping = source_groups(sources, queries, views)
    for row in queries:
        if row["expect"]:
            groups = {mapping[e["path"]] for e in row["expect"]}
            assert len(groups) == 1
            row["source_group"] = groups.pop()
    missing = coverage_missing(queries, sources)
    if missing:
        raise ValueError("; ".join(missing))
    query_path = HERE / "queries.jsonl"
    query_path.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in queries), encoding="utf-8")
    environment = json.loads((ROOT / "evals/product_v4/reference-environment.json").read_text())
    environment["note"] = "Original production rebuild environment pinned before ranking changes. Corpus was source-curated before any new-corpus relevance run; actual reference preparation must verify this lock and exact original commit. No reference scores are claimed by this input bundle."
    environment_path = HERE / "reference-environment.json"
    dump(environment_path, environment)
    splits_path = HERE / "splits.json"
    dump(splits_path, make_splits(queries))
    query_ref = ref(query_path)
    audit_path = HERE / "label-audit.json"
    audit_ref = None
    if audit_path.exists() and json.loads(audit_path.read_text())["queries_sha256"] == query_ref["sha256"]:
        audit_ref = ref(audit_path)
    manifest = {"schema": 1, "suite_id": "product.v4", "state": "DRAFT", "evidence_mode": "real",
                "reference_source_commit": REFERENCE_COMMIT, "reference_environment": ref(environment_path),
                "sources": [sources[path] for path in sorted(sources)], "queries": query_ref,
                "splits": ref(splits_path), "label_audit": audit_ref}
    dump(manifest_path, manifest)
    bundle = load_bundle(HERE, require_complete=False)
    dump(HERE / "audit-selection.json", {"schema": 1, "seed": 20261021,
                                         "queries_sha256": query_ref["sha256"],
                                         "question_ids": audit_sample_ids(queries)})
    split = json.loads(splits_path.read_text())
    by_id = {q["id"]: q for q in queries}
    summary = {"schema": 1, "state": "DRAFT_PENDING_INDEPENDENT_AUDIT", "questions": len(queries),
               "classes": dict(Counter(q["primary_class"] for q in queries)),
               "sources": len(sources), "answerable_source_groups": len({q["source_group"] for q in queries if q["expect"]}),
               "asset_bytes": sum((HERE / s["asset"]["path"]).stat().st_size for s in sources.values()),
               "folds": {name: {"questions": len(split[name]), "classes": dict(Counter(by_id[q]["primary_class"] for q in split[name]))} for name in ("dev", "holdout")},
               "bundle_identity": bundle.identity, "missing": bundle.missing,
               "authors": "AI agents /root, /root/review_ir_statistics, /root/review_architecture; no human review claimed",
               "relevance_runs_during_curation": 0,
               "known_pre_freeze_source_discoveries": ["Real JPEG DateTimeOriginal versus modified DateTime disagreement; generic capture metadata fix independently tested.", "Genuine .rst source files unsupported by original production; candidate plaintext support added and original unsupported outcomes remain fixed-denominator misses.", "Historical scanned PDF OCR column-order errors retained in actual OCR outputs; source-verified oracle paragraphs preserve the true facts.", "ASR uppercase/punctuation mismatch and partial literal evidence protocol clarified and adversarially tested before relevance scoring."]}
    dump(HERE / "curation-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
