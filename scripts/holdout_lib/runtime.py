"""Fixed joint audit execution; no default command can select this implicitly."""
from contextlib import contextmanager
from pathlib import Path

from acceptance_lib.evidence import artifact, external_ref, read_json, sha256, write_json
from answer_eval.protocol import EvaluationError, canonical_sha256
from product_eval.results import validate_report
from product_eval.runner import evaluate, production_content, production_identity, select_production
from .ledger import JointAuthorization
from .protocol import load_freeze


@contextmanager
def record_candidate_calls(bundle, split, usage):
    """Observe existing returned metadata, preserving hits/text/meta untouched."""
    from goldmine.search import parse_filters
    from product_eval import candidate

    expected = [q for q in bundle.queries if q["id"] in set(bundle.splits[split])]
    original = candidate.retrieve
    def observe(store, backend, cfg, query, filters, parameters):
        position = len(usage)
        if position >= len(expected) or parse_filters(expected[position]["q"]) != (query, filters):
            raise EvaluationError("joint observed query order differs from the full fixed holdout registry")
        result = original(store, backend, cfg, query, filters, parameters)
        usage.append({"id": expected[position]["id"], "query_sha256": canonical_sha256(expected[position]["q"]),
                      "quality_settings": result[2]["quality_settings"], "fallback_reason": result[2]["fallback_reason"]})
        return result
    candidate.retrieve = observe
    try:
        yield
    finally:
        candidate.retrieve = original


def check_reranker(frozen):
    if frozen["reranker"] is None:
        return
    from goldmine.config import Config
    from goldmine.rerank import from_config
    from goldmine.model_manifest import DEFAULT_RERANKER_KEY, by_key

    db = Path(frozen["candidate"]["index"]["path"])
    cfg = Config(home=db.parent / ".product-eval-home")
    cfg.llama.server, cfg.llama.server_configured = Path(frozen["server"]["path"]), True
    selected = from_config(cfg)
    try:
        entry = by_key(DEFAULT_RERANKER_KEY)
        if (selected.model.resolve() != Path(frozen["reranker"]["path"]).resolve()
                or selected.model.stat().st_size != entry.bytes or sha256(selected.model) != entry.sha256
                or frozen["reranker"]["sha256"] != entry.sha256):
            raise EvaluationError("actual production reranker resolution differs from its frozen native model")
    finally:
        selected.close()


def execute(freeze_path, ledger, output, root):
    frozen, bundle, order = load_freeze(freeze_path, root)
    if Path(ledger).resolve() != Path(frozen["ledger"]["path"]).resolve():
        raise EvaluationError("joint command ledger differs from the frozen absolute ledger")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    select_production(root)
    check_reranker(frozen)
    authorization = JointAuthorization(frozen, order)
    audit = {"schema": 1, "kind": "goldmine.joint-holdout-audit/v1", "freeze_sha256": sha256(Path(freeze_path)),
             "production": production_identity(root), "exposure": authorization.exposure, "arms": {}}
    # The entire registry is fixed before the first exposure. Each arm's unique
    # pre-query start and raw completed-file hashes are appended irreversibly.
    for arm in order:
        authorization.select(arm)
        usage = []
        with record_candidate_calls(bundle, "holdout", usage):
            report = evaluate(bundle, Path(frozen["candidate"]["build"]), external_ref(Path(freeze_path).parent, frozen["candidate"]["index"]),
                production_root=root, backend_name="llama", split="holdout", parameters=frozen["arms"][arm]["parameters"],
                model=external_ref(Path(freeze_path).parent, frozen["model"]), server=external_ref(Path(freeze_path).parent, frozen["server"]),
                freeze=frozen["authorization"], ledger=ledger, _joint_authorizer=authorization)
        validate_report(bundle, report)
        if [row["id"] for row in usage] != [row["id"] for row in report["rows"]]:
            raise EvaluationError("joint usage observations are incomplete")
        report_path, usage_path = output / (arm + ".json"), output / (arm + "-usage.json")
        write_json(report_path, report)
        write_json(usage_path, usage)
        authorization.complete(sha256(report_path), sha256(usage_path))
        audit["arms"][arm] = {"report": artifact(output, report_path), "usage": artifact(output, usage_path)}
    # This repeats all byte/protocol/native checks but never queries models.
    after, _, _ = load_freeze(freeze_path, root)
    if after != frozen or production_content(audit["production"]) != production_content(production_identity(root)):
        raise EvaluationError("joint frozen inputs changed during the audit")
    authorization.finish()
    write_json(output / "joint.json", audit)
    return {"status": "COMPLETE", "audit_sha256": sha256(output / "joint.json"), "arms": order, "release_eligible": False}
