"""Build a final preregistration from explicit assets, without opening scores."""
from datetime import datetime, timezone
from pathlib import Path

from acceptance_lib.evidence import external_ref, read_json, sha256
from answer_eval.protocol import EvaluationError, canonical_sha256, fields
from product_eval.bundle import load_bundle
from product_eval.ledger import _read
from product_eval.runner import check_build, production_identity
from .protocol import ACCEPTANCE, KIND, dependencies, file_ref, files, fixed_arms, runtime


def create(spec_path, root):
    spec_path, root = Path(spec_path).resolve(), Path(root).resolve()
    spec = read_json(spec_path)
    keys = {"schema", "bundle", "candidate_root", "candidate_build", "candidate_index", "reference_root", "reference_build",
            "reference_index", "reference_parameters", "reference_seal_capture", "model", "server", "reranker", "ledger",
            "parameters", "experiments"}
    fields(spec, keys, "joint freeze specification")
    if spec["schema"] != 1 or Path(spec["candidate_root"]).resolve() != root:
        raise EvaluationError("joint freeze specification must select the current final candidate")
    for key in keys - {"schema", "reference_seal_capture", "parameters", "experiments", "reranker"}:
        if not isinstance(spec[key], str) or not Path(spec[key]).is_absolute():
            raise EvaluationError("joint specification requires explicit absolute asset/source paths: " + key)
    if (not isinstance(spec["parameters"], dict) or not spec["parameters"]
            or any(not isinstance(path, str) or not Path(path).is_absolute() for path in spec["parameters"].values())
            or spec["reranker"] is not None and (not isinstance(spec["reranker"], str) or not Path(spec["reranker"]).is_absolute())):
        raise EvaluationError("joint parameter/reranker assets require explicit absolute paths")
    bundle = load_bundle(spec["bundle"])
    candidate_corpus, reference_corpus = (check_build(bundle, spec[key]) for key in ("candidate_build", "reference_build"))
    arms = {name: {"parameters": read_json(Path(path)), "parameters_sha256": canonical_sha256(read_json(Path(path)))}
            for name, path in spec["parameters"].items()}
    fixed_arms(arms, spec["experiments"])
    state, records = _read(Path(spec["ledger"]))
    if len(records) != 1 or records[0]["payload"].get("event") != "initialized":
        raise EvaluationError("joint freeze requires the one initialized, never-exposed ledger")
    seal = external_ref(spec_path.parent, spec["reference_seal_capture"], manifest=True)
    source = production_identity(root)
    reference_source = production_identity(spec["reference_root"], reference_run=True)
    reference_parameters = read_json(Path(spec["reference_parameters"]))
    frozen_at = datetime.now(timezone.utc).isoformat()
    environment_path = bundle.root / bundle.manifest["reference_environment"]["path"]
    authorization = {"schema": 1, "purpose": "final_joint_release_audit", "frozen_at": frozen_at,
                     "bundle_identity": bundle.identity, "candidate_code_sha256": source["source_sha256"],
                     "parameters_sha256": canonical_sha256(arms), "dependency_lock_sha256": sha256(environment_path),
                     "ledger_anchor_sha256": state["anchor_sha256"]}
    return {"schema": 1, "kind": KIND, "frozen_at": frozen_at, "bundle": str(bundle.root), "bundle_identity": bundle.identity,
            "candidate_root": str(root), "candidate_source": source, "environment": runtime(), "evaluator_files": files(root),
            "dependency_files": dependencies(root), "targets_sha256": sha256(root / "evals/acceptance_targets.json"),
            "model": file_ref(spec["model"]), "server": file_ref(spec["server"]),
            "reranker": file_ref(spec["reranker"]) if spec["reranker"] else None,
            "candidate": {"build": str(candidate_corpus.parent), "build_sha256": sha256(candidate_corpus.parent / "build.json"),
                          "index": file_ref(spec["candidate_index"])},
            "reference": {"production_root": str(Path(spec["reference_root"]).resolve()), "production": reference_source,
                          "build": str(reference_corpus.parent), "build_sha256": sha256(reference_corpus.parent / "build.json"),
                          "index": file_ref(spec["reference_index"]), "parameters": reference_parameters,
                          "parameters_sha256": canonical_sha256(reference_parameters),
                          "seal_capture": {"manifest_path": str(seal), "sha256": sha256(seal)},
                          "sealed_report": file_ref(seal.parent / "sealed/run.json")},
            "ledger": {"path": str(Path(spec["ledger"]).resolve()), "anchor_sha256": state["anchor_sha256"]},
            "arms": arms, "experiments": spec["experiments"], "acceptance": ACCEPTANCE, "authorization": authorization}
