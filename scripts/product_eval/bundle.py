"""Content-addressed portable corpus, independent oracle views, frozen labels."""

from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
import tempfile
import unicodedata
import re
from pathlib import Path, PurePosixPath

from answer_eval.protocol import (EvaluationError, canonical_sha256, fields, nonempty,
                                  read_json, read_text, reference, sha, sha256_file,
                                  validate_location)
from .coverage import audit_sample_ids, coverage_missing
from .schema import validate_query
from .splits import source_groups, validate_splits

REFERENCE_COMMIT = "769483752f49fb0bf8241dca778b1895b9a1b70b"


@dataclass(frozen=True)
class ProductBundle:
    root: Path
    manifest: dict
    queries: list[dict]
    sources: dict[str, dict]
    views: dict[tuple[str, str], dict]
    splits: dict | None
    missing: list[str]
    identity: dict


def normalized(text):
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def relative_path(value, label):
    nonempty(value, label)
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or "\\" in value or value in (".", "") or p.as_posix() != value:
        raise EvaluationError(label + ": require safe corpus-relative path")
    return p


def _validate_source(root, source, views):
    fields(source, {"path", "asset", "kind", "source_group", "parents", "provenance", "views"}, "source")
    relative_path(source["path"], "source.path")
    if source["kind"] not in ("doc", "code", "pdf", "audio", "video", "image", "screenshot", "web"):
        raise EvaluationError("unknown source kind")
    nonempty(source["source_group"], "source.source_group")
    if not isinstance(source["parents"], list) or any(not isinstance(x, str) for x in source["parents"]):
        raise EvaluationError("source.parents must list related source paths")
    if not isinstance(source["views"], list) or not source["views"]:
        raise EvaluationError("source requires independently frozen text/metadata oracle views")
    asset = reference(root, source["asset"], "source.asset")
    if not PurePosixPath(source["asset"]["path"]).is_relative_to("assets"):
        raise EvaluationError("source assets must be under assets/, separate from labels and compiler")
    provenance = source["provenance"]
    fields(provenance, {"type", "origin", "license", "license_file", "generation"}, "provenance")
    if provenance["type"] not in ("real", "synthetic_canary"):
        raise EvaluationError("source must distinguish real material and synthetic canary")
    nonempty(provenance["license"], "provenance.license")
    nonempty(provenance["generation"], "provenance.generation")
    reference(root, provenance["license_file"], "license_file")
    origin = provenance["origin"]
    if not isinstance(origin, dict) or origin.get("kind") not in ("git", "url", "generator"):
        raise EvaluationError("source origin requires git/url/generator provenance")
    required = {"git": {"kind", "repository", "revision", "path"},
                "url": {"kind", "url", "revision"},
                "generator": {"kind", "id", "revision"}}[origin["kind"]]
    fields(origin, required, "origin")
    for key in required:
        nonempty(origin[key], "origin." + key)
    if origin["kind"] == "generator" and provenance["type"] != "synthetic_canary":
        raise EvaluationError("generated canaries cannot be labeled real")
    if origin["kind"] == "url" and not origin["url"].startswith(("https://", "http://")):
        raise EvaluationError("public source URL must use http/https")
    ids = set()
    for v in source["views"]:
        fields(v, {"id", "path", "sha256", "evidence_type", "location", "storage_field", "normalization"}, "source view")
        nonempty(v["id"], "view.id")
        if v["id"] in ids:
            raise EvaluationError("duplicate source view ID")
        ids.add(v["id"])
        if v["evidence_type"] not in ("content_text", "ocr_text", "asr_text", "name_metadata"):
            raise EvaluationError("unknown source view evidence_type")
        validate_location(v["location"], source["kind"], v["evidence_type"])
        if v["evidence_type"] == "name_metadata":
            nonempty(v["storage_field"], "metadata storage_field")
        elif v["storage_field"] is not None:
            raise EvaluationError("only metadata views may specify storage_field")
        if v["normalization"] != "nfc-lf-v1" or not PurePosixPath(v["path"]).is_relative_to("views"):
            raise EvaluationError("oracle views must be under views/ and use nfc-lf-v1")
        text = read_text(reference(root, {"path": v["path"], "sha256": v["sha256"]}, "view"))
        if not text or normalized(text) != text:
            raise EvaluationError("oracle view must be nonempty, normalized NFC/LF")
        plaintext = {".py", ".md", ".rst", ".txt", ".rs", ".go", ".js", ".ts", ".c", ".cpp", ".toml", ".json"}
        if asset.suffix.lower() in plaintext and v["evidence_type"] == "content_text":
            raw = normalized(read_text(asset))
            loc = v["location"]
            if loc is None or loc["unit"] != "line":
                raise EvaluationError("plain text oracle must use real source line locations")
            expected = "".join(raw.splitlines(keepends=True)[loc["start"] - 1:loc["end"]])
            if text != expected:
                raise EvaluationError("plain text oracle differs from the actual pinned source lines")
        if v["evidence_type"] == "name_metadata" and v["storage_field"] == "basename" and text != Path(source["path"]).name:
            raise EvaluationError("basename oracle does not match actual source filename")
        if v["location"] is not None and v["location"]["unit"] == "line":
            if v["location"]["end"] - v["location"]["start"] + 1 != len(text.splitlines()):
                raise EvaluationError("oracle line range does not match its actual text lines")
        if v["location"] is not None and v["location"]["unit"] == "page" and v["location"]["start"] != v["location"]["end"]:
            raise EvaluationError("PDF oracle views must identify one actual page each")
        views[(source["path"], v["id"])] = {**v, "text": text}
    return asset


def _labels(queries, sources, views):
    seen = set()
    for q in queries:
        validate_query(q)
        if q["id"] in seen:
            raise EvaluationError("duplicate product question ID")
        seen.add(q["id"])
        if q["primary_class"] == "semantic":
            for tag, kinds in (("code", {"code"}), ("docs", {"doc", "pdf", "web"})):
                if tag in q["tags"] and not any(sources.get(e["path"], {}).get("kind") in kinds for e in q["expect"]):
                    raise EvaluationError("semantic code/docs tag disagrees with actual annotated source kind")
        for e in q["expect"]:
            if e["path"] not in sources or (e["path"], e["view_id"]) not in views:
                raise EvaluationError("expected source/view does not exist")
            source, view = sources[e["path"]], views[(e["path"], e["view_id"])]
            if e["kind"] != source["kind"] or e["evidence_type"] != view["evidence_type"]:
                raise EvaluationError("expected source kind/evidence_type differs from oracle")
            if e.get("storage_field") != view["storage_field"]:
                raise EvaluationError("metadata storage field does not match oracle")
            loc, parent = e["location"], view["location"]
            if (loc is None) != (parent is None):
                raise EvaluationError("expected location missing or invented")
            if loc is not None and (loc["unit"] != parent["unit"] or loc["start"] < parent["start"] or loc["end"] > parent["end"]):
                raise EvaluationError("expected page/line/time range is outside oracle view")
            s = e["span"]
            if view["text"][s["start_char"]:s["end_char"]] != s["text"]:
                raise EvaluationError("fact span does not match frozen source text at its offsets")
            # Text line labels must contain the exact marked span, not merely a
            # plausible line elsewhere in the same file.
            if loc is not None and loc["unit"] == "line":
                a = parent["start"] + view["text"][:s["start_char"]].count("\n")
                b = parent["start"] + view["text"][:max(s["start_char"], s["end_char"] - 1)].count("\n")
                if not loc["start"] <= a <= b <= loc["end"]:
                    raise EvaluationError("fact character offsets do not lie within claimed lines")
        folded_query = " ".join(q["q"].casefold().split())
        identifier_lookup = q["primary_class"] == "lookup" and re.fullmatch(r"[\w./:%-]+", q["q"].strip()) is not None
        metadata_name = any(e["evidence_type"] == "name_metadata" and e.get("storage_field") in ("basename", "path")
                            and normalized(q["q"].strip()) == views[(e["path"], e["view_id"])]["text"] for e in q["expect"])
        for view in views.values():
            if not (identifier_lookup or metadata_name) and folded_query in " ".join(view["text"].casefold().split()):
                raise EvaluationError("complete evaluation question leaks into indexed source text")
    mapping = source_groups(sources, queries, views)
    for q in queries:
        if q["expect"]:
            groups = {mapping[e["path"]] for e in q["expect"]}
            if len(groups) != 1 or q["source_group"] not in groups:
                raise EvaluationError("question source_group disagrees with duplicate/compound connected component")
        elif not q["source_group"].startswith("noise:"):
            raise EvaluationError("noise requires an explicit isolated noise: source group")


def load_bundle(path, *, require_complete=True, diagnostic=False) -> ProductBundle:
    root = Path(path).resolve()
    m = read_json(root / "manifest.json")
    fields(m, {"schema", "suite_id", "state", "evidence_mode", "reference_source_commit", "reference_environment",
               "sources", "queries", "splits", "label_audit"}, "product manifest")
    if type(m["schema"]) is not int or m["schema"] != 1 or m["suite_id"] != "product.v4":
        raise EvaluationError("unsupported product bundle schema/suite")
    if m["state"] not in ("DRAFT", "FROZEN") or m["evidence_mode"] not in ("real", "fixture"):
        raise EvaluationError("product bundle state/evidence mode invalid")
    if m["evidence_mode"] == "fixture" and not diagnostic:
        raise EvaluationError("synthetic protocol fixtures are not quality evidence", status="MISSING")
    if m["reference_source_commit"] != REFERENCE_COMMIT:
        raise EvaluationError("first reference production code must be pinned before current ranking changes")
    environment = read_json(reference(root, m["reference_environment"], "reference_environment"))
    fields(environment, {"schema", "python", "numpy", "production_commit", "note"}, "reference environment")
    if type(environment["schema"]) is not int or environment["schema"] != 1 or environment["production_commit"] != REFERENCE_COMMIT:
        raise EvaluationError("reference environment identity does not match original production")
    nonempty(environment["python"], "reference Python version")
    nonempty(environment["numpy"], "reference numpy version")
    if not isinstance(m["sources"], list):
        raise EvaluationError("sources must be a list")
    sources, views = {}, {}
    portable_names = set()
    for source in m["sources"]:
        _validate_source(root, source, views)
        if source["path"] in sources:
            raise EvaluationError("duplicate corpus source path")
        portable = unicodedata.normalize("NFKC", source["path"]).casefold()
        if portable in portable_names:
            raise EvaluationError("source paths collide on case-insensitive/normalized filesystems")
        portable_names.add(portable)
        sources[source["path"]] = source
    for path in portable_names:
        if any(parent.as_posix() in portable_names for parent in PurePosixPath(path).parents if parent.as_posix() != "."):
            raise EvaluationError("corpus source paths conflict as both a file and a parent directory")
    qpath = reference(root, m["queries"], "queries")
    from answer_eval.protocol import parse_json
    queries = [parse_json(line, "product query") for line in read_text(qpath).splitlines() if line.strip()]
    _labels(queries, sources, views)
    missing = coverage_missing(queries, sources)
    split = None
    if m["splits"] is None:
        missing.append("frozen grouped dev/holdout split is missing")
    else:
        split = read_json(reference(root, m["splits"], "splits"))
        validate_splits(split, queries)
    if m["label_audit"] is None:
        missing.append("independent fixed-sample label review is missing")
    else:
        audit = read_json(reference(root, m["label_audit"], "label_audit"))
        fields(audit, {"schema", "queries_sha256", "reviews"}, "label audit")
        if audit["schema"] != 1 or audit["queries_sha256"] != m["queries"]["sha256"] or not isinstance(audit["reviews"], list):
            raise EvaluationError("label audit identity/schema mismatch")
        reviewed = set()
        for review in audit["reviews"]:
            fields(review, {"question_id", "reviewer", "verdict", "notes"}, "label review")
            nonempty(review["reviewer"], "label reviewer")
            nonempty(review["notes"], "label review notes")
            if review["question_id"] in reviewed or review["question_id"] not in {q["id"] for q in queries} or review["verdict"] != "verified_from_source":
                raise EvaluationError("duplicate/unknown/unverified label audit row")
            reviewed.add(review["question_id"])
        if not set(audit_sample_ids(queries)) <= reviewed:
            missing.append("fixed seed label audit must review eight questions per class")
    if m["state"] != "FROZEN":
        missing.append("bundle is DRAFT; source/labels/folds are not final frozen quality inputs")
    identity = {"manifest_sha256": sha256_file(root / "manifest.json"),
                "queries_sha256": m["queries"]["sha256"], "corpus_sha256": canonical_sha256(m["sources"]),
                "splits_sha256": m["splits"]["sha256"] if m["splits"] else None,
                "reference_source_commit": REFERENCE_COMMIT}
    bundle = ProductBundle(root, m, queries, sources, views, split, missing, identity)
    if require_complete and missing:
        raise EvaluationError("product.v4 incomplete: " + "; ".join(missing), status="MISSING")
    return bundle


def build_corpus(bundle: ProductBundle, output, *, allow_draft=False):
    if bundle.missing and not allow_draft:
        raise EvaluationError("incomplete product bundle; explicit diagnostic draft build required", status="MISSING")
    target = Path(output).resolve()
    if target.exists():
        raise EvaluationError("refuse existing build destination")
    if target.is_relative_to(bundle.root):
        raise EvaluationError("build outside the immutable source bundle")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".goldmine-product-", dir=target.parent))
    try:
        from .materialization import POLICY, expected_flags, materialize

        observed = {}
        for path, source in bundle.sources.items():
            destination = temporary / "corpus" / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(reference(bundle.root, source["asset"], "source.asset"), destination)
            if sha256_file(destination) != source["asset"]["sha256"]:
                raise EvaluationError("copied source identity mismatch")
            observed[path] = materialize(destination, source)
        receipt = {"schema": 2, "suite_id": "product.v4", "identity": bundle.identity,
                   "files": {p: s["asset"]["sha256"] for p, s in bundle.sources.items()},
                   "materialization": {"policy": POLICY, "expected": expected_flags(bundle.sources), "observed": observed},
                   "diagnostic_only": bool(bundle.missing) or bundle.manifest["evidence_mode"] == "fixture"}
        (temporary / "build.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        temporary.rename(target)
        return receipt
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
