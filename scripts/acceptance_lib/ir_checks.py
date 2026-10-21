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

Public consumes beir-mini.raw/v1 captured by capture_public.py: all pinned test
qids, three datasets and four arms, actual fetched and post-cut doc IDs, ZIP
member hashes, source/index/model/server identities. It reads qrels directly
from each pinned ZIP and recomputes all rows and aggregates without inference.
The historical --gate remains advisory and cannot establish this regression.
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
        paths = [root / "scripts/eval_public.py", root / "scripts/eval_real.py",
                 *sorted((root / "scripts/product_eval").glob("*.py")),
                 *sorted((root / "scripts/answer_eval").glob("*.py")),
                 *sorted((root / "scripts/public_eval").glob("*.py"))]
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
            or _path(argv[1], receipt["cwd"]) != (Path(root) / f"scripts/eval_{kind}.py").resolve()
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


def _readback(report, index, corpus, bundle, *, original_reference=None):
    """Verify claimed citation atoms against the actual pinned SQLite source."""
    from goldmine.store import Store
    from answer_eval.protocol import canonical_sha256
    from product_eval.evidence import index_capability_missing, read_segment_spans, relative_source
    from product_eval.runner import check_build, production_content
    from .sqlite_readback import OriginalReadback

    checked = 0
    corpus = Path(corpus).resolve()
    if check_build(bundle, corpus.parent).resolve() != corpus:
        raise InvalidEvidence("product readback corpus differs from its materialized build")
    build_digest = sha256(corpus.parent / "build.json")
    if report["index_identity"].get("build_receipt_sha256") != build_digest:
        raise InvalidEvidence("product raw report differs from the actual materialized build receipt")
    reference = report["reference_verified"]
    if reference != (original_reference is not None):
        raise InvalidEvidence("product original readback requires its verified explicit source context")
    with closing(OriginalReadback(index) if reference else Store(index, ro=True)) as store, store.read_snapshot():
        production = report["production"]
        schema = "production-identity/v1" if reference else "production-content/v1"
        digest = canonical_sha256(production if reference else production_content(production))
        if (store.meta("product.bundle_identity") != canonical_sha256(bundle.identity)
                or store.meta("product.build_receipt_sha256") != build_digest
                or store.meta("product.prepared_code_schema") != schema or store.meta("product.prepared_code") != digest
                or store.meta("product.backend") != "llama"):
            raise InvalidEvidence("product database preparation identity/model mismatch")
        if index_capability_missing(store, bundle, corpus, original_reference=original_reference):
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
                _observed_readback(store, sid, span)
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


def _observed_readback(store, segment_id, span):
    """ASR's optional case/punctuation witness must be actual stored bytes."""
    from product_eval.alignment import validate_word_alignment
    from product_eval.bundle import normalized

    observed = validate_word_alignment(span)
    if observed is not None and observed not in normalized(store.segment_text(segment_id) or ""):
        raise InvalidEvidence("ASR observed witness is absent from its actual pinned SQLite segment")


def _product_arm(root, inputs, name, bundle):
    from product_eval.results import validate_report
    from product_eval.runner import check_build, production_content, production_identity

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
    observed_source = report["production"]
    if (observed_source != production if reference else production_content(observed_source) != production_content(production)):
        raise InvalidEvidence("product captured source content differs from the current explicit production checkout")
    if not isinstance(report["backend_identity"], dict) or not isinstance(report["parameters"], dict):
        raise InvalidEvidence("product backend/parameter identities must be mappings")
    if (report["split"] != "dev" or report["exposure"] is not None
            or report["sealed_reference"] or report["reference_verified"] != reference
            or report["role"] != ("reference" if reference else "candidate") or report["backend"] != "llama"):
        raise InvalidEvidence("product run source/split/role is not the current unsealed real dev experiment")
    index = _index(inputs, name, report)
    server = _path(flags["--server"], cwd)
    if not server.is_file() or not server.stat().st_size:
        raise InvalidEvidence("original explicit llama-server binary is unavailable")
    if manifest["identity"] != {"evaluator_files": evaluator_files(root, "product"), "production_source": observed_source,
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
    context = {"production_root": production_root, "production": observed_source} if reference else None
    checked = _readback(report, index, corpus, bundle, original_reference=context)
    return "PASS", {"report": report, "run_id": manifest["run_id"], "readback_atoms": checked}


def _product(root, inputs, targets):
    from product_eval.bundle import load_bundle
    from product_eval.gates import check_targets, compare_reference
    from product_eval.runner import production_content

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
        if single["parameters"] != {**candidate["parameters"], "multi_query": False} or production_content(single["production"]) != production_content(candidate["production"]):
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
    from product_eval.runner import production_content

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
    observed_source = runs["candidate"].manifest["variables"]["code"]
    if production_content(observed_source) != production_content(current):
        raise InvalidEvidence("answer candidate pack/job source is not the current production identity")
    path = inputs.file("answer.compare_run", manifest=True)
    manifest, receipt, stdout = _command(path, root, kind="answer", suite="answer-eval/v3", check_id="answer.compare")
    if timestamp(receipt["started_at"], "compare capture") < max(timestamp(run.manifest["completed_at"], "answer job completion") for run in runs.values()):
        raise InvalidEvidence("answer comparison capture predates completed answer/judge jobs")
    expected_argv = [receipt["argv"][0], receipt["argv"][1], "--compare", str(paths["reference"].parent), str(paths["candidate"].parent), "--json"]
    if receipt["argv"] != expected_argv:
        raise InvalidEvidence("answer receipt must capture the exact independent real comparison; no diagnostic flag")
    if manifest["identity"] != {"evaluator_files": evaluator_files(root, "answer"), "candidate_source": observed_source,
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


def _public_argv(receipt, root, inputs, report, role, *, prepare=False):
    argv = receipt["argv"]
    prefix = argv[:2] + ["--raw"] + (["--prepare"] if prepare else [])
    expected = prefix + ["--cache-root", str(Path(report["cache_root"]).resolve()),
                         "--production-root", str(Path(report["production_root"]).resolve()),
                         "--backend", "llama", "--model", str(inputs.file("models.embedding")), "--server"]
    if argv[:len(expected)] != expected or len(argv) != len(expected) + (3 if role == "reference" else 2):
        raise InvalidEvidence("public command must use the exact offline raw invocation with explicit assets")
    server = _path(argv[len(expected)], receipt["cwd"])
    if argv[len(expected) + 1:] != (["--reference-code", "--json"] if role == "reference" else ["--json"]) or not server.is_file():
        raise InvalidEvidence("public role/server arguments differ from the fixed capture invocation")
    return server


def _public_prepare(root, inputs, role, report, scoring_manifest, scoring_receipt, public_inputs):
    """An independent actual prepare command binds the final immutable DBs."""
    from answer_eval.protocol import timestamp
    from product_eval.runner import production_content
    from public_eval.protocol import DATASETS, SUITE

    manifest, receipt, stdout = _command(inputs.file(f"public.{role}_prepare", manifest=True), root,
                                        kind="public", suite=SUITE, check_id=f"public.{role}_prepare")
    status, reason = process_status(receipt)
    if status != "PASS":
        raise InvalidEvidence("public independent preparation did not complete: " + reason)
    prepared = read_json(stdout)
    required = set(report) - {"advisory_published_baselines"} | {"status"}
    if not isinstance(prepared, dict) or set(prepared) != required or prepared["status"] != "PREPARED":
        raise InvalidEvidence("public preparation requires complete actual finalized-index stdout, not self-declared DB metadata")
    if (manifest["run_id"] == scoring_manifest["run_id"]
            or timestamp(receipt["finished_at"], "public preparation completion") > timestamp(scoring_receipt["started_at"], "public scoring start")):
        raise InvalidEvidence("public scoring must follow its separate completed preparation capture")
    server = _public_argv(receipt, root, inputs, prepared, role, prepare=True)
    # A later candidate commit with byte-identical production is compatible;
    # retain the actually observed full prepare identity in its receipt.
    source, scored_source = prepared["production"], report["production"]
    if (source != scored_source if role == "reference" else production_content(source) != production_content(scored_source)):
        raise InvalidEvidence("public preparation source differs from the scored production content")
    for key in required - {"status", "datasets", "production"}:
        if prepared[key] != report[key]:
            raise InvalidEvidence("public preparation and scoring changed " + key)
    expected_datasets = {name: {key: report["datasets"][name][key] for key in ("input_identity", "index")} for name in DATASETS}
    if prepared["datasets"] != expected_datasets:
        raise InvalidEvidence("public finalized preparation indexes/ZIP identities differ from the actual scored databases")
    identity = {"evaluator_files": evaluator_files(root, "public"), "production_source": source,
                "model_sha256": prepared["model_sha256"], "server_sha256": sha256(server), "public_inputs": public_inputs}
    if manifest["identity"] != identity:
        raise InvalidEvidence("public preparation capture lacks exact evaluator/source/ZIP/model/server bindings")
    return manifest["run_id"]


def _public_arm(root, inputs, role):
    from product_eval.runner import production_content, production_identity
    from public_eval.protocol import ARMS, DATASETS, SUITE, inspect_index, load_archive, validate_arms
    from eval_public import CORPUS_MD_DIR, PUBLIC_MANIFEST_JSON, dataset_dir, load_manifest, zip_path

    manifest, receipt, stdout = _command(inputs.file(f"public.{role}_run", manifest=True), root,
                                        kind="public", suite=SUITE, check_id="public." + role)
    status, reason = process_status(receipt)
    if status != "PASS":
        return status, {"reason": reason, "role": role}
    report = read_json(stdout)
    required = {"schema", "suite", "backend", "role", "diagnostic_only", "production_root", "production", "manifest_sha256",
                "cache_root", "model_sha256", "server_sha256", "environment", "parameters", "datasets",
                "advisory_published_baselines", "release_eligible", "backend_identity"}
    if (not isinstance(report, dict) or set(report) != required or report["schema"] != 1 or report["suite"] != SUITE
            or report["backend"] != "llama" or report["role"] != role or report["diagnostic_only"] is not False
            or report["release_eligible"] is not False or report["advisory_published_baselines"] is not True):
        raise InvalidEvidence("public evidence must be the complete real raw/v1 export; advisory/mock summaries are insufficient")
    production_root, cache = Path(report["production_root"]).resolve(), Path(report["cache_root"]).resolve()
    if role == "candidate" and production_root != Path(root).resolve():
        raise InvalidEvidence("public candidate came from a different checkout")
    production = production_identity(production_root, reference_run=role == "reference")
    observed_source = report["production"]
    if (observed_source != production if role == "reference" else production_content(observed_source) != production_content(production)):
        raise InvalidEvidence("public captured source content differs from the explicit current checkout")
    if report["manifest_sha256"] != sha256(PUBLIC_MANIFEST_JSON):
        raise InvalidEvidence("public production/official manifest bytes changed")
    server = _public_argv(receipt, root, inputs, report, role)
    if (report["model_sha256"] != inputs.get("models.embedding.sha256") or report["server_sha256"] != sha256(server)
            or report["parameters"] != {"fetch_limit": 20, "top_k": 10, "drop_self_doc_before_cut": True}):
        raise InvalidEvidence("public model/server identity or fetch/drop/cut semantics differ")
    fingerprint = report["backend_identity"]
    if (not isinstance(fingerprint, dict) or fingerprint.get("model_sha256") != report["model_sha256"]
            or fingerprint.get("dim") != 768):
        raise InvalidEvidence("public run lacks the actual real embedding fingerprint")
    official = load_manifest()
    if not isinstance(report["datasets"], dict) or set(report["datasets"]) != set(DATASETS):
        raise InvalidEvidence("public export must include all three datasets")
    summaries, indexes = {}, {}
    for name in DATASETS:
        value, entry = report["datasets"][name], official["datasets"][name]
        if not isinstance(value, dict) or set(value) != {"input_identity", "index", "arms"}:
            raise InvalidEvidence("public dataset export lacks its input/index/ranking identities")
        data = load_archive(zip_path(cache, name), entry)
        if value["input_identity"] != data["identity"]:
            raise InvalidEvidence("public ZIP corpus/query/qrels identities differ")
        expected_index = dataset_dir(cache, name) / "raw-index.sqlite"
        index = inspect_index(expected_index, data, observed_source, dataset_dir(cache, name) / CORPUS_MD_DIR, "llama", reference=role == "reference")
        if value["index"] != index or index["fingerprint"].get("backend") != "llama" or any(index["fingerprint"].get(key) != item for key, item in fingerprint.items()):
            raise InvalidEvidence("public actual prepared index/fingerprint differs from the export")
        indexes[name] = index["sha256"]
        summaries[name] = validate_arms(value["arms"], data, entry)
    identity = {"evaluator_files": evaluator_files(root, "public"), "production_source": observed_source,
                "model_sha256": report["model_sha256"], "server_sha256": report["server_sha256"], "indexes_sha256": indexes,
                "public_inputs": {"manifest_sha256": report["manifest_sha256"],
                                  "datasets": {name: report["datasets"][name]["input_identity"] for name in DATASETS}}}
    if manifest["identity"] != identity:
        raise InvalidEvidence("public command identity lacks exact evaluator/source/model/server/index bindings")
    environment = report["environment"]
    if (not isinstance(environment, dict) or set(environment) != {"python", "numpy"}
            or not all(isinstance(value, str) and value for value in environment.values())):
        raise InvalidEvidence("public dependency runtime identity is missing")
    preparation_run = _public_prepare(root, inputs, role, report, manifest, receipt, identity["public_inputs"])
    return "PASS", {"run_id": manifest["run_id"], "preparation_run_id": preparation_run, "report": report, "summaries": summaries}


def public(root, inputs, targets):
    """Recompute every real public ranking; missing inputs never trigger inference."""
    try:
        _target_file(root, targets)
        from .checks import public_assets

        assets = public_assets(root, inputs)
        arms = {}
        for role in ("reference", "candidate"):
            status, value = _public_arm(root, inputs, role)
            if status != "PASS":
                return status, value
            arms[role] = value
        reference, candidate = (arms[role]["report"] for role in ("reference", "candidate"))
        run_ids = [arms[role][key] for role in arms for key in ("preparation_run_id", "run_id")]
        if len(set(run_ids)) != 4:
            raise InvalidEvidence("public original/candidate preparations and scorings need four independent captures")
        for key in ("backend_identity", "model_sha256", "server_sha256", "environment", "parameters", "manifest_sha256"):
            if reference[key] != candidate[key]:
                raise InvalidEvidence("public paired experiment changed " + key)
        drop_limit = Decimal(str(targets["regression"]["public_ndcg_absolute_drop"]))
        comparisons = {}
        for name in assets["datasets"]:
            old = arms["reference"]["summaries"][name]["hybrid"]["nDCG@10"]
            new = arms["candidate"]["summaries"][name]["hybrid"]["nDCG@10"]
            delta = Decimal(str(new["sum"])) / new["n"] - Decimal(str(old["sum"])) / old["n"]
            comparisons[name] = {"n": new["n"], "reference": old["value"], "candidate": new["value"],
                                 "delta": float(delta), "passed": delta >= -drop_limit}
        return ("PASS" if all(row["passed"] for row in comparisons.values()) else "FAIL"), {
            "scope": "public_regression_only", "release_eligible": False, "datasets": comparisons,
            "all_four_arms": {role: value["summaries"] for role, value in arms.items()},
            "run_ids": run_ids, "absolute_drop_limit": float(drop_limit),
            "published_metrics": "advisory context; not used in the paired gate"}
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        return "MISSING", {"reason": str(error), "required_raw_export": _PUBLIC_EXPORT}


_PUBLIC_EXPORT = {
    "suite": "beir-mini.raw/v1", "backend": "llama", "datasets": ["scifact", "nfcorpus", "arguana"],
    "arms": ["lexical", "semantic", "rrf", "hybrid"],
    "per_query_required": ["id", "post_cut_ranked_docids", "query_text_sha256", "qrels_sha256"],
    "migration": [
        "Capture independent original/candidate preparations with capture_public.py --prepare into public.reference_prepare/candidate_prepare; never download while accepting evidence.",
        "Capture capture_public.py --role reference/candidate with all explicit cache/model/server/production-root assets; retain original pinned ZIPs and checkpointed indexes.",
        "Every fixed test qid/four-arm fetched and post-cut doc-ID list is recomputed against qrels read from each actual pinned ZIP.",
        "Each dataset candidate hybrid nDCG@10 drop must be <= targets.regression.public_ndcg_absolute_drop (.02); old advisory stdout/exit0 cannot pass.",
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
