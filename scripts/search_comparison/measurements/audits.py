"""Actual stage readback, closed snapshots, and independent disk-only checks."""
from pathlib import Path

from answer_eval.protocol import EvaluationError, parse_json, sha256_file
from search_comparison.adapters import DocumentMap
from search_comparison.engine_capture.protocol import inventory
from search_comparison.engine_capture.recoll import DENIED_PROBE, metadata_argv, parse_metadata, body_argv, stored_body
from search_comparison.engine_capture.sqlite_digest import physical
from . import database, updates
from .filesystem import verify_index_snapshot
from .states import index, changed_configuration


def _mapping(known):
    return {d["view_path"]: {"source_path": d["source_path"], "sha256": d["sha256"]} for d in known}


def readback(ledger, state, known, expected):
    values, view, engine = state["values"], state["view"], ledger.kind
    if engine == "goldmine":
        raw = ledger.state("goldmine_audit", {"index": index(state, engine), "corpus": view["corpus"],
                           "known": known, "embedding_sha256": values["embedding_sha256"]})
        logical = updates.goldmine(raw)
        logical["query_index"] = raw["stable"]["query_index"]
    elif engine == "qmd":
        raw = ledger.call("measurement.qmd.update-audit", [values["node"], str(Path(__file__).with_name("_qmd_update_audit.mjs")),
                          values["qmd_package"], values["qmd_db"], state["configuration"]["path"], values.get("embed", "-")])
        raw = parse_json(raw.decode("utf-8"), "full QMD history audit stdout")
        if (raw["configuration"] != state["configuration"]["value"]
                or any(not Path(p).is_relative_to(Path(values["installation"])) for p in raw["native_paths"])):
            raise EvaluationError("actual QMD config/native import escaped frozen registration")
        logical = updates.qmd(raw, _mapping(known), [d["view_path"] for d in expected], semantic=values["semantic"], view=view["name"])
    else:
        mapping = DocumentMap(view["corpus"], known, collection="sources", index="comparison")
        raw = ledger.call("measurement.recoll.metadata", metadata_argv(values["recollq"], values["recoll_config_dir"], view["corpus"]))
        metadata = parse_metadata(raw.decode("utf-8"), mapping)
        for d in expected:
            denied = ledger.call("measurement.recoll.denied-source", [values["sandbox"], "-f", values["sandbox_profile"], values["python"], "-I", "-S", "-B", "-c", DENIED_PROBE,
                                 str(Path(view["corpus"]) / d["view_path"])])
            if denied != b"DENIED_SOURCE_READ\n":
                raise EvaluationError("stored text audit source-read policy is ineffective")
        bodies = [stored_body(ledger.call("measurement.recoll.stored-body", body_argv(values["sandbox"], values["sandbox_profile"], values["recollq"],
                             values["recoll_config_dir"], view["corpus"], row["ordinal"])), row) for row in metadata]
        if view["name"] == "common":
            for d in expected:
                parts = [body for row, body in zip(metadata, bodies) if row["source_path"] == d["source_path"]]
                if len(parts) != 1 or parts[0] != Path(d["input"]).read_bytes():
                    raise EvaluationError("actual common stored body is not the complete current registered input")
        raw = {"metadata": metadata, "body_sha256": [__import__("hashlib").sha256(b).hexdigest() for b in bodies],
               "body_bytes": [len(b) for b in bodies], "source_reads_denied": [d["source_path"] for d in expected]}
        known_sources = {d["source_path"]: d for d in known}
        logical = updates.recoll(metadata, bodies, known_sources, [d["source_path"] for d in expected])
    return raw, logical


def stage(ledger, state, name, known, expected):
    changed_configuration(ledger, state)
    witness = ledger.state("source_witness", {"corpus": state["view"]["corpus"], "view": state["view"]["name"], "entries": expected, "changed": []})
    before = ledger.state("index_witness", {"index": index(state, ledger.kind)}, binding={"stage": name, "edge": "before-audit"})
    raw, logical, error = None, None, None
    try:
        raw, logical = readback(ledger, state, known, expected)
    except (EvaluationError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        error = str(exc)
    after = ledger.state("index_witness", {"index": index(state, ledger.kind)}, binding={"stage": name, "edge": "after-audit"})
    if before != after:
        raise EvaluationError("read-only stage audit changed the closed index/cache")
    snapshot = ledger.state("index_snapshot", {"index": index(state, ledger.kind), "stage": name})
    if snapshot["witness"] != after:
        raise EvaluationError("closed snapshot differs from the audited stage")
    result = {"stage": name, "source_witness": witness, "index_witness": after, "raw": raw, "logical": logical, "error": error,
              "snapshot": snapshot["snapshot"], "known": known, "expected": expected, "corpus": state["view"]["corpus"]}
    verify_stage(result, ledger.kind, state["values"])
    return result


def verify_stage(value, engine, values):
    root = verify_index_snapshot(value["snapshot"], value["index_witness"], engine)
    if engine == "goldmine" and value["raw"] is not None:
        actual = database.goldmine(root / "index.sqlite", value["corpus"], value["known"], values["embedding_sha256"])
        # Absolute observation path is not logical content; whole SQLite bytes
        # and all source/file/part fields are independently checked here.
        if actual["stable"] != value["raw"]["stable"] or actual["stable_sha256"] != value["raw"]["stable_sha256"]:
            raise EvaluationError("retained stage SQLite differs from full actual source/body/vector readback")
    elif engine == "qmd" and value["raw"] is not None:
        if (sha256_file(root / "index.sqlite") != value["raw"]["database_sha256"]
                or physical(root / "index.sqlite") != value["raw"]["physical"]):
            raise EvaluationError("retained QMD stage SQLite differs from native full-history audit")
    elif engine == "recoll" and inventory(root) != value["index_witness"]["stable"]:
        raise EvaluationError("retained Recoll index differs from actual stored-text audit snapshot")
