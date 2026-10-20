"""Disk-only IR evidence adapters; never run models or expose sealed holdout.

Registry integration (owned by runner's maintainer): use kind="ir" with argument
product/answer/public, dispatch to check(root, inputs, argument, targets).

product.reference_run/candidate_run remain command-run manifest refs, suite_id
product.v4 and check_id product.reference/product.candidate. Their receipt argv
must invoke this checkout's eval_product.py with explicit --bundle/--build/--db/
--production-root/--backend llama/--model/--server/--parameters/--split dev/--json;
only the original reference additionally has --reference-code. Each command
identity has exactly evaluator_files, production_source, index_sha256 and
server_sha256 (the explicitly selected local llama-server executable).
evaluator_files comes from evaluator_files(root, "product"); production_source
is product_eval.runner.production_identity(the explicit production checkout).
Index identity_manifest files bind exactly the schema1 product.index fields
checked below. The existing source_manifest/queries/splits/parameters/model refs
are also required. Quality multi-query additionally needs single_query_run,
single_query_index and single_query_parameters with the same candidate source.

answer.reference_run/candidate_run reference their v3 run.json, not summaries.
Add answer.compare_run: command-run manifest for eval_answer.py --compare REF_DIR
CAND_DIR --json, suite_id answer-eval/v3, check_id answer.compare. Its identity
has evaluator_files, reference_source and candidate_source. variables.code in
each v3 run must equal that complete production identity; candidate_source must
equal the current checkout. Existing answer.preregistration must pin both runs.
This consumes complete provided answer/judge jobs, without launching inference.

Public currently returns MISSING with an exact export migration: the current
eval_public.py CLI emits only aggregate metrics/CI and its internal rows discard
ranked document IDs. Those aggregates cannot establish three-dataset/four-arm
paired qrels correctness. public() gives the required raw export shape and steps;
it never turns that lack of evidence into a green advisory gate.
"""

from __future__ import annotations

from contextlib import closing
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
import sys

from .evidence import (InvalidEvidence, external_ref, is_sha, process_status,
                       read_command_run, read_json, sha256)


def evaluator_files(root: Path, kind: str) -> dict:
    """Exact trusted evaluator implementation needed in capture_command identity."""
    root = Path(root).resolve()
    if kind == "product":
        paths = [root / "scripts/eval_product.py", *sorted((root / "scripts/product_eval").glob("*.py")),
                 *sorted((root / "scripts/answer_eval").glob("*.py"))]
    elif kind == "answer":
        paths = [root / "scripts/eval_answer.py", *sorted((root / "scripts/answer_eval").glob("*.py"))]
    else:
        paths = [root / "scripts/eval_public.py", root / "scripts/eval_real.py"]
    return {path.relative_to(root).as_posix(): sha256(path) for path in paths}


def _current_source(root):
    from product_eval.runner import production_identity

    return production_identity(root)


def _target_file(root, targets):
    path = Path(root) / "evals/acceptance_targets.json"
    if not isinstance(targets, dict) or read_json(path) != targets or targets.get("contract_sha256") != sha256(Path(root) / "docs/execution-plan/CONTRACTS.md"):
        raise InvalidEvidence("IR target bytes/contract differ from the registered current targets")
    return path


def _path(value, cwd):
    path = Path(value)
    return (Path(cwd) / path).resolve() if not path.is_absolute() else path.resolve()


def _command(path, root, *, kind, suite, check_id):
    manifest, receipt, stdout = read_command_run(path, suite_id=suite, check_id=check_id)
    argv = receipt["argv"]
    if (len(argv) < 2 or _path(argv[0], receipt["cwd"]) != Path(sys.executable).resolve()
            or _path(argv[1], receipt["cwd"]) != (Path(root) / f"scripts/eval_{'answer' if kind == 'answer' else 'product'}.py").resolve()
            or Path(receipt["cwd"]).resolve() != Path(root).resolve()):
        raise InvalidEvidence("IR receipt must invoke the registered Python/script directly from this checkout")
    identity = manifest["identity"]
    if not isinstance(identity, dict) or identity.get("evaluator_files") != evaluator_files(root, kind):
        raise InvalidEvidence("IR command evaluator scripts changed or lack their exact SHA mapping")
    return manifest, receipt, stdout


def _flags(argv):
    valued = {"--bundle", "--build", "--db", "--production-root", "--backend", "--model", "--server", "--parameters", "--split", "--out"}
    boolean = {"--json", "--reference-code"}
    result, i = {}, 2
    while i < len(argv):
        flag = argv[i]
        if flag in result or flag not in valued | boolean:
            raise InvalidEvidence("unexpected/repeated product argv option: " + flag)
        if flag in boolean:
            result[flag] = True
        elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            result[flag] = argv[i + 1]
            i += 1
        else:
            raise InvalidEvidence("product argv option lacks a value")
        i += 1
    required = valued - {"--out"} | {"--json"}
    if not required <= result.keys() or result["--backend"] != "llama" or result["--split"] != "dev":
        raise InvalidEvidence("formal product evidence needs all explicit real-model dev options; holdout is never read here")
    return result


def _index(inputs, name, report):
    entry = inputs.get(f"product.{name}_index")
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "identity_manifest"}:
        raise InvalidEvidence("product index needs path/SHA and exact identity_manifest")
    path = external_ref(inputs.base, {key: entry[key] for key in ("path", "sha256")})
    identity = read_json(external_ref(inputs.base, entry["identity_manifest"], manifest=True))
    expected = {"schema": 1, "kind": "product.index", "index_sha256": sha256(path),
                **{key: report[key] for key in ("bundle_identity", "production", "backend", "backend_identity", "index_identity")}}
    if identity != expected:
        raise InvalidEvidence("product index identity manifest differs from actual index/run")
    return path


def _readback(report, index, corpus, bundle):
    """Verify claimed citation atoms against the actual pinned SQLite source."""
    from goldmine.store import Store
    from answer_eval.protocol import canonical_sha256
    from product_eval.evidence import index_capability_missing, read_segment_spans, relative_source

    checked = 0
    with closing(Store(index, ro=True)) as store, store.read_snapshot():
        if (store.meta("product.bundle_identity") != canonical_sha256(bundle.identity)
                or store.meta("product.prepared_code") != canonical_sha256(report["production"])
                or store.meta("product.backend") != "llama"):
            raise InvalidEvidence("product database preparation identity/model mismatch")
        if index_capability_missing(store, bundle, corpus):
            raise InvalidEvidence("required media capabilities/source content are missing from the pinned product index")
        for row in report["rows"]:
            for record in row["pack_records"]:
                source = relative_source(record["path"], corpus, bundle)
                ids = {part["segment_id"] for part in record["segments"]}
                if record["segment_id"] is not None:
                    ids.add(record["segment_id"])
                for sid in ids:
                    actual = store.db.execute("SELECT f.path FROM segments s JOIN files f ON f.id=s.file WHERE s.id=?", (sid,)).fetchone()
                    if actual is None or Path(actual[0]).resolve() != Path(record["path"]).resolve():
                        raise InvalidEvidence("emitted record includes an absent/out-of-scope source segment")
                if source not in bundle.sources:
                    raise InvalidEvidence("emitted record path outside frozen corpus")
            spans = [span for hit in row["hits"] for span in hit["spans"]] + row["emitted_spans"]
            for span in spans:
                sid = int(span["citation_id"].split(":")[-1])
                actual = read_segment_spans(store, sid, span["path"], str(corpus / span["path"]), bundle)
                supported = False
                for proof in actual:
                    if (any(span[key] != proof[key] for key in ("path", "view_id", "evidence_type", "location", "citation_id"))
                            or not proof["start_char"] <= span["start_char"] < span["end_char"] <= proof["end_char"]):
                        continue
                    offset = span["start_char"] - proof["start_char"]
                    if proof["text"][offset:offset + len(span["text"])] == span["text"]:
                        supported = True
                        break
                if not supported:
                    raise InvalidEvidence("citation span does not read back from its actual pinned SQLite segment")
                checked += 1
    return checked


def _product_arm(root, inputs, name, bundle):
    from product_eval.results import validate_report
    from product_eval.runner import check_build, production_identity

    path = inputs.file(f"product.{name}_run", manifest=True)
    manifest, receipt, stdout = _command(path, root, kind="product", suite="product.v4", check_id="product." + name)
    status, reason = process_status(receipt)
    if status != "PASS":
        return status, {"reason": reason, "arm": name}
    flags = _flags(receipt["argv"])
    reference = name == "reference"
    if flags.get("--reference-code", False) != reference:
        raise InvalidEvidence("product original/candidate command role differs")
    cwd = receipt["cwd"]
    production_root = _path(flags["--production-root"], cwd)
    if not reference and production_root != Path(root).resolve():
        raise InvalidEvidence("product candidate came from an earlier/different checkout")
    production = production_identity(production_root, reference_run=reference)
    report = validate_report(bundle, read_json(stdout))
    if not isinstance(report["backend_identity"], dict) or not isinstance(report["parameters"], dict):
        raise InvalidEvidence("product backend/parameter identities must be mappings")
    if (report["production"] != production or report["split"] != "dev" or report["exposure"] is not None
            or report["sealed_reference"] or report["reference_verified"] != reference
            or report["role"] != ("reference" if reference else "candidate") or report["backend"] != "llama"):
        raise InvalidEvidence("product run source/split/role is not the current unsealed real dev experiment")
    index = _index(inputs, name, report)
    server = _path(flags["--server"], cwd)
    if not server.is_file() or not server.stat().st_size:
        raise InvalidEvidence("original explicit llama-server binary is unavailable")
    if manifest["identity"] != {"evaluator_files": evaluator_files(root, "product"), "production_source": production,
                                "index_sha256": sha256(index), "server_sha256": sha256(server)}:
        raise InvalidEvidence("product command identity does not bind current production/index bytes")
    if (_path(flags["--bundle"], cwd) != bundle.root or _path(flags["--db"], cwd) != index
            or _path(flags["--model"], cwd) != inputs.file("models.embedding")):
        raise InvalidEvidence("product argv differs from the pinned corpus/index/model assets")
    if report["backend_identity"].get("model_sha256") != inputs.get("models.embedding.sha256"):
        raise InvalidEvidence("product raw backend identity differs from the pinned real model")
    parameter_name = "single_query_parameters" if name == "single_query" else "parameters"
    parameters = inputs.file("product." + parameter_name)
    if _path(flags["--parameters"], cwd) != parameters or read_json(parameters) != report["parameters"]:
        raise InvalidEvidence("product parameters differ from frozen explicit inputs")
    corpus = check_build(bundle, _path(flags["--build"], cwd))
    checked = _readback(report, index, corpus, bundle)
    return "PASS", {"report": report, "run_id": manifest["run_id"], "readback_atoms": checked}


def _product(root, inputs, targets):
    from product_eval.bundle import load_bundle
    from product_eval.gates import check_targets, compare_reference

    target_path = _target_file(root, targets)
    source = inputs.file("product.source_manifest")
    bundle = load_bundle(source.parent)  # FROZEN, audited, full420, no mock/draft shrink.
    if (source.resolve() != bundle.root / "manifest.json"
            or inputs.file("product.queries").resolve() != (bundle.root / bundle.manifest["queries"]["path"]).resolve()
            or inputs.file("product.splits").resolve() != (bundle.root / bundle.manifest["splits"]["path"]).resolve()):
        raise InvalidEvidence("product explicit question/fold identities differ from the bundle")
    arms = {}
    for name in ("reference", "candidate"):
        status, details = _product_arm(root, inputs, name, bundle)
        if status != "PASS":
            return status, details
        arms[name] = details
    if arms["reference"]["run_id"] == arms["candidate"]["run_id"]:
        raise InvalidEvidence("product paired arms reused one captured run")
    reference, candidate = (arms[name]["report"] for name in ("reference", "candidate"))
    if reference["backend_identity"] != candidate["backend_identity"]:
        raise InvalidEvidence("product reference/candidate embedding identities differ")
    gate = check_targets(candidate, target_path)
    security = {"legal_query_exception_rate": 0, "invalid_citation_rate": 0, "out_of_scope_read_rate": 0}
    if any(targets["product"][name] != 0 for name in security):
        raise InvalidEvidence("product deterministic safety targets must remain zero")
    details = {"scope": "dev_task_check", "release_eligible": False, "gate": gate, "security": security,
               "n_queries": len(candidate["rows"]), "bundle_identity": bundle.identity,
               "paired_original": compare_reference(reference, candidate),
               "run_ids": [arms[name]["run_id"] for name in ("reference", "candidate")],
               "readback_atoms": {name: arms[name]["readback_atoms"] for name in arms}}
    if candidate["parameters"].get("multi_query"):
        status, control = _product_arm(root, inputs, "single_query", bundle)
        if status != "PASS":
            return status, control
        single = control["report"]
        if control["run_id"] in details["run_ids"]:
            raise InvalidEvidence("compound control reused another captured run")
        if single["parameters"] != {**candidate["parameters"], "multi_query": False} or single["production"] != candidate["production"]:
            raise InvalidEvidence("compound control differs beyond same-candidate multi_query flag")
        a = single["summary"]["classes"]["compound"]["all_required_facts@pack"]
        b = candidate["summary"]["classes"]["compound"]["all_required_facts@pack"]
        if not a["n"] or a["n"] != b["n"]:
            raise InvalidEvidence("compound fact/control denominators differ")
        observed, baseline = Decimal(str(b["sum"])) / b["n"], Decimal(str(a["sum"])) / a["n"]
        details["compound"] = {"n": b["n"], "value": float(observed), "gain": float(observed - baseline)}
        if observed < Decimal(str(targets["product"]["compound_all_facts"])) or observed - baseline < Decimal(str(targets["product"]["compound_gain"])):
            gate["failed"].append({"check": "compound.experimental_acceptance", **details["compound"]})
    return ("FAIL" if gate["failed"] else "PASS"), details


def _answer(root, inputs, targets):
    from answer_eval.protocol import canonical_sha256, load_run, timestamp
    from answer_eval.workflow import compare_runs

    _target_file(root, targets)
    paths = {name: inputs.file("answer." + name + "_run", manifest=True) for name in ("reference", "candidate")}
    if any(path.name != "run.json" for path in paths.values()):
        raise InvalidEvidence("answer inputs must pin complete v3 run.json directories")
    runs = {name: load_run(path.parent) for name, path in paths.items()}
    preregistration = inputs.file("answer.preregistration")
    if any(run.manifest["preregistration"]["sha256"] != sha256(preregistration) for run in runs.values()):
        raise InvalidEvidence("answer preregistration differs from frozen explicit input")
    for run in runs.values():
        corpus = read_json(run.root / run.manifest["corpus"]["path"])
        if not isinstance(corpus, dict) or corpus.get("test_only") is True or corpus.get("diagnostic_only") is True or corpus.get("evidence_mode") == "fixture":
            raise InvalidEvidence("synthetic/mock answer corpus is not formal grading evidence")
        source = run.manifest["variables"]["code"]
        if (not isinstance(source, dict) or set(source) != {"git_commit", "source_sha256", "files"}
                or not isinstance(source["git_commit"], str) or not re.fullmatch("[a-f0-9]{40}", source["git_commit"])
                or not isinstance(source["files"], dict) or not {"goldmine/__init__.py", "pyproject.toml"} <= source["files"].keys()
                or not all(isinstance(name, str) and is_sha(value) for name, value in source["files"].items())
                or source["source_sha256"] != canonical_sha256(source["files"])):
            raise InvalidEvidence("answer source needs full code SHA/files identity, not a summary revision label")
    current = _current_source(root)
    if runs["candidate"].manifest["variables"]["code"] != current:
        raise InvalidEvidence("answer candidate pack/job source is not the current production identity")
    path = inputs.file("answer.compare_run", manifest=True)
    manifest, receipt, stdout = _command(path, root, kind="answer", suite="answer-eval/v3", check_id="answer.compare")
    if timestamp(receipt["started_at"], "compare capture") < max(timestamp(run.manifest["completed_at"], "answer job completion") for run in runs.values()):
        raise InvalidEvidence("answer comparison capture predates completed answer/judge jobs")
    expected_argv = [receipt["argv"][0], receipt["argv"][1], "--compare", str(paths["reference"].parent), str(paths["candidate"].parent), "--json"]
    if receipt["argv"] != expected_argv:
        raise InvalidEvidence("answer receipt must capture the exact independent real comparison; no diagnostic flag")
    if manifest["identity"] != {"evaluator_files": evaluator_files(root, "answer"), "candidate_source": current,
                                "reference_source": runs["reference"].manifest["variables"]["code"]}:
        raise InvalidEvidence("answer compare receipt lacks exact source provenance")
    actual, code = compare_runs(paths["reference"].parent, paths["candidate"].parent)
    if canonical_sha256(read_json(stdout)) != canonical_sha256(actual) or receipt["exit_code"] != code or receipt["timed_out"]:
        raise InvalidEvidence("answer raw comparison differs from revalidated complete jobs")
    t = targets["answer"]
    for arm in ("reference", "candidate"):
        summary = actual[arm]
        if summary["n_answerable"] < t["answerable_n"] or summary["trap"]["n"] < t["trap_n"]:
            raise InvalidEvidence("answer target requires complete60 answerable/20trap, not historical20 jobs")
    if (actual["practical_drop_threshold"] not in t["practical_drop_allowed"]
            or t["trap_answer_mean_min"] != 1.5 or t["trap_min_rate"] != .9
            or t["min_source_groups_for_claim"] != 12 or t["null_audit_required"] is not False
            or actual["calibration_status"] != "uncalibrated" or actual["false_positive_bound"] is not None):
        raise InvalidEvidence("answer targets/calibration representation changed")
    return ("FAIL" if code else "PASS"), {"scope": "provided_complete_answer_jobs", "release_eligible": False,
                                           "comparison": actual, "capture_run_id": manifest["run_id"]}


def public(root, inputs, targets):
    """Fail closed on the concrete current export gap, with an executable migration."""
    try:
        _target_file(root, targets)
        from .checks import public_assets

        public_assets(root, inputs)
        captured = {}
        for name in ("reference", "candidate"):
            path = inputs.file(f"public.{name}_run", manifest=True)
            manifest, receipt, stdout = read_command_run(path)
            captured[name] = {"run_id": manifest["run_id"], "exit_code": receipt["exit_code"], "stdout_fields": sorted(read_json(stdout))}
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError) as error:
        return "MISSING", {"reason": str(error), "required_raw_export": _PUBLIC_EXPORT}
    return "MISSING", {"reason": "eval_public --json currently exports aggregate metrics/CI; no verified ranked-docid/qrels raw export is available", "captures": captured, "required_raw_export": _PUBLIC_EXPORT}


_PUBLIC_EXPORT = {
    "suite": "beir-mini.raw/v1", "backend": "llama", "datasets": ["scifact", "nfcorpus", "arguana"],
    "arms": ["lexical", "semantic", "rrf", "hybrid"],
    "per_query_required": ["id", "post_cut_ranked_docids", "query_text_sha256", "qrels_sha256"],
    "migration": [
        "Preserve kept_ids in run_dataset_arms rows; export every pinned test-qid for all three datasets and four arms, without intersections.",
        "Bind corpus/query/qrels/zip/model/index and production/evaluator SHA in independent reference/candidate command receipts; retain pinned qrels bytes.",
        "Recompute score_query_public from ranked docids and pinned qrels, reject duplicate/unknown docs/qids and verify aggregates.",
        "Apply each dataset's candidate hybrid nDCG@10 drop <= targets.regression.public_ndcg_absolute_drop (.02); advisory stdout/exit0 alone cannot pass.",
        "Capture offline real llama runs only after cache preparation; acceptance must not download datasets or start inference implicitly.",
    ],
}


def check(root, inputs, name, targets):
    """Fixed runner adapter: invalid/missing identity is MISSING, genuine drop FAIL."""
    if name == "public":
        return public(root, inputs, targets)
    implementation = {"product": _product, "answer": _answer}.get(name)
    if implementation is None:
        raise InvalidEvidence("unknown IR check: " + name)
    try:
        return implementation(Path(root), inputs, targets)
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        return "MISSING", {"reason": str(error), "scope": "dev_only_no_model_execution"}


def product_dev(root, inputs, targets):
    return check(root, inputs, "product", targets)


def answer(root, inputs, targets):
    return check(root, inputs, "answer", targets)
