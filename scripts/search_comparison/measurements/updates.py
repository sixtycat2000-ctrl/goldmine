"""Logical update differences, retaining raw identities/history separately.

These pure adapters consume already authenticated read-only audit payloads.
They do not certify an actual preparation or relax the raw receipt requirement.
"""
from __future__ import annotations
from collections import Counter
import hashlib
import json
import math

from answer_eval.protocol import EvaluationError, canonical_sha256, parse_json

INFO_POLICY = "strict-json-info-preserve-unknown-except-top-level-ocr-ms/v1"
MAX_INFO_BYTES = 1024 * 1024


def _semantic_info(raw):
    """Keep every unknown/nested field; exclude one producer-proven time only.

    SQL NULL means no recorded info. A JSON null/string/list is not an object.
    parse_json rejects duplicate keys (also nested) and nonfinite numbers before
    the sole exclusion is considered, so invalid metrics cannot disappear.
    """
    if raw is None:
        return None, {}
    if not isinstance(raw, str):
        raise EvaluationError("files.info must be SQL NULL or JSON object text")
    try:
        if len(raw.encode("utf-8")) > MAX_INFO_BYTES:
            raise EvaluationError("files.info exceeds1MiB; no semantic fields dropped")
        info = parse_json(raw, "files.info")
        if not isinstance(info, dict):
            raise EvaluationError("files.info JSON must be an object")
        canonical_sha256(info)  # Also reject invalid UTF-8 surrogate text.
    except (RecursionError, UnicodeError) as exc:
        raise EvaluationError("files.info contains unbounded nesting or invalid UTF-8") from exc
    volatile = {}
    if "ocr_ms" in info:
        value = info.pop("ocr_ms")
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise EvaluationError("files.info.ocr_ms must be finite nonnegative timing")
        volatile["ocr_ms"] = value
    return info, volatile


def _sorted(rows):
    return sorted(rows, key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _text(text):
    if text is None:
        return None
    if not isinstance(text, str):
        raise EvaluationError("actual stored text must be complete Unicode or explicit null")
    raw = text.encode()
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _vector(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"bytes", "sha256"} or type(value["bytes"]) is not int or value["bytes"] <= 0
            or not isinstance(value["sha256"], str) or len(value["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in value["sha256"])):
        raise EvaluationError("actual vector byte identity is incomplete")
    return value


def goldmine(audit):
    stable = audit["stable"]
    if audit["stable_sha256"] != canonical_sha256(stable) or stable["schema"] != 6:
        raise EvaluationError("actual Goldmine read-only audit identity differs")
    fields = ("source", "kind", "size", "sha256", "state", "capability", "detail", "extractor_version", "chunker_version", "embed_id", "content_date")
    result, ids, volatile_info = {}, set(), {}
    for path, row in stable["sources"].items():
        file = row["file"]
        metadata = {k: file[k] for k in fields}
        if file["detail"] is not None and not isinstance(file["detail"], str):
            raise EvaluationError("files.detail must be semantic text or SQL NULL")
        metadata["info"], volatile_info[path] = _semantic_info(file["info"])
        bodies, vectors = [], []
        for part in row["parts"]:
            if part["id"] in ids:
                raise EvaluationError("duplicate actual Goldmine segment ID")
            ids.add(part["id"])
            for k in ("loc", "loc_end"):
                if part[k] is not None and (type(part[k]) not in (int, float) or not math.isfinite(part[k])):
                    raise EvaluationError("nonfinite stored source location")
            anchor = {k: part[k] for k in ("kind", "modality", "loc", "loc_end")}
            bodies.append({**anchor, "text": _text(part["text"])})
            vectors.append({**anchor, "vector": _vector(part["vector"])})
        result[path] = {"metadata": metadata, "body": _sorted(bodies), "vectors": _sorted(vectors), "capability": row["status"]}
    return {"engine": "goldmine", "sources": result, "model_identity": stable["meta"].get("fingerprint_json"),
            "semantic_sha256": canonical_sha256(result), "raw_identity": audit["stable_sha256"],
            "info_policy": INFO_POLICY, "excluded_info_values_retained": volatile_info,
            "excluded_from_logical_comparison_only": ["physical file/segment IDs", "absolute working index paths", "mtime/indexed_at", "files.info.ocr_ms (exact top-level path only; raw value retained)"],
            "raw_full_fields_retained": True}


def qmd(raw, mapping, expected, *, semantic, view="common"):
    """Use active membership, but never discard retained tombstones/vector rows."""
    if (raw.get("schema") != 1 or raw.get("scope") != "readonly-update-audit" or type(semantic) is not bool or view not in {"native", "common"}
            or not isinstance(mapping, dict) or not set(expected) <= set(mapping)):
        raise EvaluationError("invalid declared update mapping/audit")
    supported = {p for p in expected if view == "common" or p.rsplit(".", 1)[-1] in {"md", "rst", "py", "c"}}
    documents, vectors, vector_rows = raw["documents"], raw["vectors"], raw["vector_rows"]
    if any(not isinstance(r, dict) for r in documents + vectors + vector_rows):
        raise EvaluationError("every update audit row must be an object")
    if len({r["id"] for r in documents}) != len(documents) or any(type(r["active"]) is not int or r["active"] not in (0, 1) or r["collection"] != "sources" or r["path"] not in mapping for r in documents):
        raise EvaluationError("duplicate document IDs/unknown active state/collection")
    if len({r["source_path"] for r in mapping.values()}) != len(mapping):
        raise EvaluationError("two view files cannot collapse one original source identity")
    if semantic and (not isinstance(raw.get("fingerprint"), str) or not raw["fingerprint"]):
        raise EvaluationError("semantic model fingerprint is absent")
    model_path = raw.get("configuration", {}).get("models", {}).get("embed")
    if semantic and (not isinstance(model_path, str) or not model_path.startswith("/")):
        raise EvaluationError("actual semantic configuration model path is absent")
    if any(_text(r["body"])["sha256"] != r["hash"] for r in documents):
        raise EvaluationError("actual active/inactive body hash is inconsistent")
    active = [r for r in documents if r["active"] == 1]
    if len({r["path"] for r in active}) != len(active) or any(r["path"] not in mapping for r in active):
        raise EvaluationError("duplicate or unregistered actual active source path")
    if {r["rowid"] for r in raw["fts"]} != {r["id"] for r in active} or len(raw["fts"]) != len(active):
        raise EvaluationError("FTS active-row denominator differs")
    if raw.get("fts_verified_against_active_full_bodies") is not True:
        raise EvaluationError("actual FTS/body proof absent")
    actual_keys = [r["hash_seq"] for r in vector_rows]
    if len(actual_keys) != len(set(actual_keys)):
        raise EvaluationError("duplicate actual vector key")
    by_key = {r["hash_seq"]: _vector({k: r[k] for k in ("bytes", "sha256")}) for r in vector_rows}
    eligible = set(raw["query_eligible_keys"])
    if len(eligible) != len(raw["query_eligible_keys"]) or not eligible <= set(by_key):
        raise EvaluationError("eligible-vector membership malformed/omitted")
    live_hashes = {r["hash"] for r in active}
    expected_eligible = {r["hash"] + "_" + str(r["seq"]) for r in vectors if r["hash"] in live_hashes}
    if eligible != expected_eligible:
        raise EvaluationError("query-eligible vector join contradicts actual active membership")
    result = {}
    for row in active:
        if _text(row["body"])["sha256"] != row["hash"] or row["hash"] != mapping[row["path"]]["sha256"]:
            raise EvaluationError("stored active full body differs from registered actual source bytes")
        vv = [v for v in vectors if v["hash"] == row["hash"]]
        if semantic:
            if not vv or any(type(v["total_chunks"]) is not int or v["total_chunks"] < 1 for v in vv):
                raise EvaluationError("active source lacks required embedding metadata")
            n = vv[0]["total_chunks"]
            if (len(vv) != n or {v["seq"] for v in vv} != set(range(n)) or any(type(v["seq"]) is not int or v["total_chunks"] != n or v["embed_fingerprint"] != raw["fingerprint"] or v["model"] != model_path for v in vv)):
                raise EvaluationError("active chunk/fingerprint denominator differs")
        vv = [{"metadata": v, "vector": by_key[v["hash"] + "_" + str(v["seq"])]} for v in vv]
        result[mapping[row["path"]]["source_path"]] = {"metadata": {"active": 1, "hash": row["hash"], "title": row["title"]},
            "body": _text(row["body"]), "vectors": _sorted(vv), "capability": "ACTIVE_FULL_STORED_TEXT"}
    history = {"inactive_documents": [r for r in documents if r["active"] == 0],
               "noneligible_vector_metadata": [r for r in vectors if r["hash"] not in live_hashes],
               "noneligible_vector_rows": [r for r in vector_rows if r["hash_seq"] not in eligible]}
    paths = {r["path"] for r in active}
    for p in set(expected) - paths:
        result[mapping[p]["source_path"]] = {"metadata": {"registered_view_sha256": mapping[p]["sha256"]},
            "body": None, "vectors": [], "capability": "MISSING_ACTIVE_DOCUMENT" if p in supported else "UNSUPPORTED_NATIVE_FORMAT"}
    return {"engine": "qmd", "sources": result, "model_identity": raw["fingerprint"],
            "semantic_sha256": canonical_sha256(result), "active_membership": {"missing": sorted(supported - paths), "stale_active": sorted(paths - set(expected))},
            "source_count_expected": len(set(expected)), "unsupported_native": sorted(set(expected) - supported),
            "retained_history": history, "history_sha256": canonical_sha256(history), "physical_identity": raw["physical"],
            "raw_full_fields_retained": True}


def recoll(metadata, bodies, known, expected):
    """Bodies must come from raw stored_body() with source reads actually denied.

    The all-known mapping permits an actual deleted-but-active row to be reported
    rather than erased by a smaller expected-stage mapping. This pure normalizer
    does not replace that command/access/raw framing authentication.
    """
    if (len(metadata) != len(bodies) or not set(expected) <= set(known)
            or len({r["xdocid"] for r in metadata}) != len(metadata)):
        raise EvaluationError("actual Recoll stored-row denominator/identity differs")
    grouped = {}
    for n, (m, body) in enumerate(zip(metadata, bodies)):
        if (m["source_path"] not in known or m["ordinal"] != n or not isinstance(body, bytes) or len(body) != int(m["dbytes"])):
            raise EvaluationError("Recoll unknown source/ordinal or incomplete actual stored body")
        body.decode("utf-8")
        grouped.setdefault(m["source_path"], []).append({"ipath": m["ipath"], "mtype": m["mtype"],
            "body_bytes": len(body), "body_sha256": hashlib.sha256(body).hexdigest()})
    result = {}
    for p in set(expected) | set(grouped):
        parts = _sorted(grouped.get(p, []))
        result[p] = {"metadata": {"registered_view_sha256": known[p]["sha256"]}, "body": parts, "vectors": [],
                     "capability": "OBSERVED_INDEXED_TEXT" if any(x["body_bytes"] for x in parts) else "METADATA_ONLY" if parts else "UNINDEXED"}
    return {"engine": "recoll", "sources": result, "model_identity": None, "semantic_sha256": canonical_sha256(result),
            "active_membership": {"missing": sorted(set(expected) - set(grouped)), "stale_active": sorted(set(grouped) - set(expected))},
            "optional_semantic_status": "UNPROVISIONED_SEPARATE_FROM_REGISTERED_LEXICAL_MODES", "raw_full_fields_retained": True}


def compare(left, right):
    if left["engine"] != right["engine"]:
        raise EvaluationError("incremental/fresh comparison must use the same engine")
    if left["engine"] == "goldmine" and (left.get("info_policy") != INFO_POLICY or right.get("info_policy") != INFO_POLICY):
        raise EvaluationError("Goldmine semantic-info normalization policy differs/absent")
    ls, rs = left["sources"], right["sources"]
    shared = set(ls) & set(rs)
    differences = {name: sorted(p for p in shared if ls[p][name] != rs[p][name]) for name in ("metadata", "body", "vectors", "capability")}
    differences.update(added=sorted(set(rs) - set(ls)), removed=sorted(set(ls) - set(rs)))
    model_equal = left["model_identity"] == right["model_identity"]
    return {"status": "LOGICAL_EQUAL" if model_equal and not any(differences.values()) else "LOGICAL_DIFFERENCES_OBSERVED",
            "model_identity_equal": model_equal, "n_left": len(ls), "n_right": len(rs), "changes": differences,
            "counts": {k: len(v) for k, v in differences.items()}, "vector_policy": "bitwise-digest-differences-retained-no-tuned-tolerance",
            "authentication": "requires strict command/source/index replay; pure comparison is not real preparation proof",
            "quality_evidence": False, "release_eligible": False}
