"""Pinned tools, frozen converted source bytes and prebuilt URI identities."""

from pathlib import Path

from answer_eval.protocol import EvaluationError, fields, read_json, sha256_file
from product_eval.bundle import load_bundle, relative_path
from .adapters import DocumentMap


def repo_path(root, value):
    relative_path(value, "comparison path")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise EvaluationError("comparison path escapes repository root")
    return path


def load_comparison(path, root):
    root = Path(root).resolve()
    manifest = read_json(Path(path))
    fields(manifest, {"schema", "diagnostic_only", "quality_evidence", "suite_id", "bundle", "view", "systems", "missing"}, "comparison manifest")
    if manifest["schema"] != 1 or manifest["diagnostic_only"] is not True or manifest["quality_evidence"] is not False or manifest["suite_id"] != "product.v4":
        raise EvaluationError("only explicit diagnostic product.v4 comparison is implemented")
    fields(manifest["bundle"], {"path", "manifest_sha256"}, "comparison bundle")
    bundle_path = repo_path(root, manifest["bundle"]["path"])
    if sha256_file(bundle_path / "manifest.json") != manifest["bundle"]["manifest_sha256"]:
        raise EvaluationError("comparison bundle identity changed")
    bundle = load_bundle(bundle_path, require_complete=False)
    view = manifest["view"]
    fields(view, {"name", "converter", "corpus_root", "parent_manifest", "documents"}, "comparison view")
    if view["name"] != "text-common" or view["converter"] != "source-nfc-lf/v1":
        raise EvaluationError("comparison requires the frozen common text conversion")
    corpus = repo_path(root, view["corpus_root"])
    parent = view["parent_manifest"]
    fields(parent, {"path", "sha256"}, "parent common-view manifest")
    if sha256_file(repo_path(root, parent["path"])) != parent["sha256"]:
        raise EvaluationError("parent common-view manifest changed")
    for document in view["documents"]:
        fields(document, {"source_path", "view_path", "sha256"}, "comparison document")
        relative_path(document["view_path"], "common-view document")
        source = bundle.sources.get(document["source_path"])
        if source is None or sha256_file(corpus / document["view_path"]) != document["sha256"]:
            raise EvaluationError("common-view source mapping or bytes changed")
        text = (corpus / document["view_path"]).read_text(encoding="utf-8")
        content = [bundle.views[(source["path"], v["id"])]["text"] for v in source["views"] if v["evidence_type"] == "content_text"]
        if content != [text]:
            raise EvaluationError("common-view conversion differs from its frozen source oracle")
    if {d["source_path"] for d in view["documents"]} != set(bundle.sources):
        raise EvaluationError("common view must map every source; do not shrink the corpus")
    if set(manifest["systems"]) != {"goldmine", "qmd", "recoll"}:
        raise EvaluationError("comparison requires all three named systems")
    for name, system in manifest["systems"].items():
        required = {"mode", "version", "provenance"}
        required |= {"binary", "index", "collection", "environment"} if name == "qmd" else {"binary", "config_dir", "data_dir"} if name == "recoll" else set()
        fields(system, required, name + " system")
        if system.get("mode") != "lexical":
            raise EvaluationError("only lexical mode is measured; semantic modes remain unmeasured")
        if name == "qmd":
            fields(system["environment"], {"XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"}, "QMD environment")
        for ref in system.get("provenance", []):
            fields(ref, {"path", "sha256"}, name + " provenance")
            if sha256_file(repo_path(root, ref["path"])) != ref["sha256"]:
                raise EvaluationError(name + " pinned tool/provenance bytes changed")
    qmd = manifest["systems"]["qmd"]
    mapping = DocumentMap(corpus, view["documents"], collection=qmd["collection"], index=qmd["index"])
    return manifest, bundle, mapping
