"""Replay the source-bound base/+10 preparation receipts without starting models."""
from __future__ import annotations

import json
from pathlib import Path

from acceptance_lib.evidence import InvalidEvidence, external_ref, read_command_run, read_json, sha256
from .identity import assert_extension, canonical_sha, evaluator_identity, index_identity
from .labels import load_labels
from .materialization import build_receipt, check_corpus, sources_for

SUITE = "distill.prepare.v1"


def command_argv(python, work_index, corpus):
    return [str(python), "-m", "goldmine.cli", "ingest", "--code", "--db", str(work_index),
            "--backend", "llama", "--json", str(corpus)]


def command_env(root, out, assets):
    return {"GOLDMINE_HOME": str(out / "state"), "GOLDMINE_CONFIG": str(out / "config.toml"),
            "GOLDMINE_MODEL": assets["model"]["path"], "GOLDMINE_LLAMA_SERVER": assets["server"]["path"],
            "GOLDMINE_OCR_AUTOCOMPILE": "0", "PYTHONPATH": str(root), "PYTHONNOUSERSITE": "1"}


def validate_index(identity, sources, corpus, model_sha):
    fingerprint = json.loads(identity["meta"].get("fingerprint_json") or "{}")
    if not isinstance(fingerprint, dict) or fingerprint.get("backend") != "llama" or fingerprint.get("model_sha256") != model_sha:
        raise InvalidEvidence("prepared index backend/model differs from actual native preparation")
    files = identity["files"]
    if {row["path"] for row in files.values()} != {str(corpus / name) for name in sources}:
        raise InvalidEvidence("prepared index file registry differs from exact frozen source split")
    for fid, row in files.items():
        source = sources[str(Path(row["path"]).relative_to(corpus))]
        if row["sha256"] != source["asset"]["sha256"] or row["state"] != "done" or row["capability"] != "content_indexed":
            raise InvalidEvidence("recognized document is changed, incomplete or missing content extraction")
        candidates = [(sid, segment) for sid, segment in identity["segments"].items()
                      if segment["file"] == int(fid) and segment["kind"] != "name"]
        if source["kind"] == "screenshot":
            candidates = [(sid, segment) for sid, segment in candidates if segment["kind"] == "ocr"]
        if not any(segment["vec"] is not None and sid in identity["fts"] and
                   (identity["fts"][sid][2] or identity["fts"][sid][1] or "").strip()
                   for sid, segment in candidates):
            raise InvalidEvidence("recognized document lacks real stored text/vector evidence (including screenshot OCR)")


def _phase(root, out, prep, phase, count, unchanged, snapshot):
    manifest_path = external_ref(out, prep["commands"][phase], manifest=True)
    manifest, receipt, stdout = read_command_run(manifest_path, check_id=f"distill.prepare.{phase}", suite_id=SUITE)
    identity = {"source_files": prep["source_files"], "assets": prep["assets"], "labels_sha256": prep["labels"]["sha256"],
                "source_manifest_sha256": prep["source_manifest"]["sha256"],
                "build_sha256": prep["builds"][phase]["sha256"]}
    if manifest["identity"] != identity or receipt["timed_out"] or receipt["exit_code"] != 0:
        raise InvalidEvidence("native preparation receipt failed or uses different source/assets/materialization")
    if receipt["argv"] != command_argv(prep["assets"]["python"]["path"], prep["working_index"], prep["corpus_root"]):
        raise InvalidEvidence("preparation must execute the fixed llama ingest command on one working index/root")
    if receipt["cwd"] != str(out) or receipt["env"] != command_env(root, out, prep["assets"]):
        raise InvalidEvidence("native preparation runtime environment differs from recorded explicit source/assets")
    report = read_json(stdout)
    if not isinstance(report, dict):
        raise InvalidEvidence("native preparation stdout must be the actual ingest JSON object")
    files = report.get("files", {})
    metrics = report.get("metrics")
    counters = ("scanned", "content_indexed", "unchanged", "failed", "unsupported", "metadata_only", "partial", "deleted")
    if (not isinstance(files, dict) or any(type(files.get(key)) is not int for key in counters) or
            report.get("dry_run") is not False or report.get("cancelled") is not False or report.get("failures") != [] or
            any(files.get(key) != 0 for key in ("failed", "unsupported", "metadata_only", "partial", "deleted")) or
            files.get("scanned") != count + unchanged or files.get("content_indexed") != count or files.get("unchanged") != unchanged):
        raise InvalidEvidence("native preparation did not completely ingest the exact base/+10 split")
    if not isinstance(metrics, dict) or any(type(metrics.get(key)) is not int or metrics[key] <= 0
                                          for key in ("model_spawns", "embedding_http_requests", "embed_calls", "embed_inputs")):
        raise InvalidEvidence("native preparation lacks observed actual model spawn/embedding calls")
    if not isinstance(report.get("fingerprint"), dict) or report["fingerprint"].get("id") != snapshot["meta"]["fingerprint_id"]:
        raise InvalidEvidence("raw native ingest fingerprint differs from prepared snapshot")
    return receipt


def validate_preparation(path, db_path, labels_path, corpus_root, incremental_db):
    path, root = Path(path).resolve(), Path(__file__).resolve().parents[2]
    out, corpus = path.parent, Path(corpus_root).resolve()
    prep = read_json(path)
    required = {"schema", "kind", "status", "source_files", "assets", "labels", "source_manifest", "corpus_root",
                "working_index", "config", "builds", "commands", "indexes", "identities"}
    if not isinstance(prep, dict) or set(prep) != required or type(prep["schema"]) is not int or prep["schema"] != 1 or prep["kind"] != "goldmine.distill-preparation" or prep["status"] != "PREPARED":
        raise InvalidEvidence("missing complete source-bound distill preparation")
    if prep["source_files"] != evaluator_identity(root):
        raise InvalidEvidence("distill preparation evaluator/runtime source identity changed")
    if not isinstance(prep["assets"], dict) or set(prep["assets"]) != {"python", "model", "server", "ocr"}:
        raise InvalidEvidence("preparation must bind actual Python/model/llama-server/OCR assets")
    for ref in prep["assets"].values():
        if not Path(ref["path"]).is_absolute():
            raise InvalidEvidence("native assets require explicit absolute paths")
        external_ref(out, ref)
    if external_ref(out, prep["labels"]) != Path(labels_path).resolve() or prep["corpus_root"] != str(corpus):
        raise InvalidEvidence("evaluation labels/root differ from prepared inputs")
    if prep["working_index"] != str(out / "state/index.sqlite"):
        raise InvalidEvidence("preparation must retain one explicitly owned working index")
    config = external_ref(out, prep["config"])
    if config != out / "config.toml" or config.read_bytes() != b"":
        raise InvalidEvidence("preparation must use its recorded empty/default config")
    ocr_copy = out / "state/bin/goldmine-ocr"
    if not ocr_copy.is_file() or sha256(ocr_copy) != prep["assets"]["ocr"]["sha256"]:
        raise InvalidEvidence("actual prepared OCR helper differs from pinned native binary")
    labels_data, _ = load_labels(Path(labels_path), corpus)
    source_path, sources = sources_for(labels_path, labels_data)
    if external_ref(out, prep["source_manifest"]) != source_path:
        raise InvalidEvidence("preparation source manifest differs from frozen labels")
    base_names = {row["path"] for row in labels_data["documents"] if row["split"] == "base"}
    base_sources = {name: row for name, row in sources.items() if name in base_names}
    if set(prep["indexes"]) != {"base", "incremental"} or set(prep["builds"]) != set(prep["indexes"]) or set(prep["identities"]) != set(prep["indexes"]) or set(prep["commands"]) != set(prep["indexes"]):
        raise InvalidEvidence("preparation must contain exactly both base and incremental phases")
    snapshots = {}
    for phase, input_db, phase_sources in (("base", db_path, base_sources), ("incremental", incremental_db, sources)):
        db = external_ref(out, prep["indexes"][phase])
        if db != Path(input_db).resolve():
            raise InvalidEvidence("evaluation index differs from preparation snapshot")
        build = read_json(external_ref(out, prep["builds"][phase]))
        if phase == "incremental":
            check_corpus(corpus, sources, build)
        elif build != build_receipt(base_sources):
            raise InvalidEvidence("base materialization receipt does not match the exact frozen base subset")
        snapshots[phase] = index_identity(db)
        recorded = read_json(external_ref(out, prep["identities"][phase]))
        if recorded != snapshots[phase]:
            raise InvalidEvidence("all-row snapshot identity differs from preparation receipt")
        validate_index(recorded, phase_sources, corpus, prep["assets"]["model"]["sha256"])
    assert_extension(snapshots["base"], snapshots["incremental"])
    first = _phase(root, out, prep, "base", len(base_names), 0, snapshots["base"])
    second = _phase(root, out, prep, "incremental", 10, len(base_names), snapshots["incremental"])
    if first["finished_at"] > second["started_at"]:
        raise InvalidEvidence("incremental preparation started before base ingest completed")
    return {"preparation_sha256": sha256(path), "source_files_sha256": canonical_sha(prep["source_files"]),
            "base_identity_sha256": canonical_sha(snapshots["base"]),
            "incremental_identity_sha256": canonical_sha(snapshots["incremental"]),
            "python_path": prep["assets"]["python"]["path"], "python_sha256": prep["assets"]["python"]["sha256"],
            "model_sha256": prep["assets"]["model"]["sha256"], "server_sha256": prep["assets"]["server"]["sha256"],
            "ocr_sha256": prep["assets"]["ocr"]["sha256"], "real_quality_claim": False}
