"""Exact1.44.2 CLI stored-text audit, with original-source reads denied."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path
import re

from answer_eval.protocol import EvaluationError

FIELDS = "url ipath mtype dbytes xdocid"
DENIED_PROBE = ('import pathlib,sys\ntry: pathlib.Path(sys.argv[1]).read_bytes()\n'
                'except PermissionError: print("DENIED_SOURCE_READ");sys.exit(0)\n'
                'print("UNEXPECTED_READ");sys.exit(1)')


def directory_query(corpus):
    return "dir:" + json.dumps(str(Path(corpus).resolve()), ensure_ascii=False)


def sandbox_profile(corpus):
    return '(version 1)\n(allow default)\n(deny file-read-data (subpath ' + json.dumps(str(Path(corpus).resolve()), ensure_ascii=False) + '))\n(deny network*)\n'


def metadata_argv(binary, config, corpus):
    return [str(binary), "-c", str(config), "-E", "-n", "0", "-N", "-F", FIELDS, "--", directory_query(corpus)]


def body_argv(sandbox, profile, binary, config, corpus, ordinal):
    if type(ordinal) is not int or ordinal < 0:
        raise EvaluationError("invalid stored-text document ordinal")
    return [str(sandbox), "-f", str(profile), str(binary), "-c", str(config), "-b", "-d", "-n", f"{ordinal}-1", "--", directory_query(corpus)]


def parse_metadata(text, mapping):
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].startswith("Recoll query: ") or re.fullmatch(r"[0-9]+ results", lines[1]) is None:
        raise EvaluationError("complete Recoll exact-count metadata framing differs", status="MISSING")
    count = int(lines[1].split()[0])
    if len(lines[2:]) != count:
        raise EvaluationError("Recoll metadata row denominator was truncated", status="MISSING")
    result, seen = [], set()
    for ordinal, line in enumerate(lines[2:]):
        parts, values = line.split(), {}
        if not parts or len(parts) % 2:
            raise EvaluationError("Recoll named base64 fields are malformed")
        for key, value in zip(parts[::2], parts[1::2]):
            if key not in FIELDS.split() or key in values:
                raise EvaluationError("unknown/duplicate stored-document metadata field")
            try:
                values[key] = base64.b64decode(value, validate=True).decode("utf-8")
            except (UnicodeError, binascii.Error) as exc:
                raise EvaluationError("Recoll named field is not strict base64 UTF8") from exc
        if (set(values) - {"ipath"} != {"url", "mtype", "dbytes", "xdocid"}
                or re.fullmatch(r"[0-9]+", values["dbytes"]) is None
                or re.fullmatch(r"[1-9][0-9]*", values["xdocid"]) is None or values["xdocid"] in seen):
            raise EvaluationError("Recoll stored-document type/identity is incomplete")
        seen.add(values["xdocid"])
        result.append({**values, "ipath": values.get("ipath", ""), "ordinal": ordinal,
                       "source_path": mapping.recoll_url(values["url"])})
    return result


def stored_body(raw, metadata):
    """One -b/-d result: exact URL LF, whole stored UTF8 body, one extra LF."""
    prefix = metadata["url"].encode("utf-8") + b"\n"
    if b"\n" in metadata["url"].encode("utf-8") or not raw.startswith(prefix) or not raw.endswith(b"\n"):
        raise EvaluationError("Recoll one-document stored-text framing differs", status="MISSING")
    body = raw[len(prefix):-1]
    fallback = ("Cant turn to text:" + metadata["url"] + " | " + metadata["ipath"]).encode("utf-8")
    if body == fallback or len(body) != int(metadata["dbytes"]):
        raise EvaluationError("stored text missing/denied; source re-extraction is forbidden", status="MISSING")
    try:
        body.decode("utf-8")
    except UnicodeError as exc:
        raise EvaluationError("Recoll stored body is not complete UTF8") from exc
    return body


def coverage(view, metadata, bodies):
    if len(metadata) != len(bodies):
        raise EvaluationError("stored body audit omitted registered documents")
    result = {}
    for document in view["documents"]:
        parts = [({**m, "body_sha256": hashlib.sha256(b).hexdigest(), "body_bytes": len(b)})
                 for m, b in zip(metadata, bodies) if m["source_path"] == document["source_path"]]
        status = "OBSERVED_INDEXED_TEXT" if any(p["body_bytes"] for p in parts) else "METADATA_ONLY" if parts else "UNINDEXED"
        if view["name"] == "common":
            expected = (Path(view["corpus"]) / document["view_path"]).read_bytes()
            if len(parts) != 1 or bodies[parts[0]["ordinal"]] != expected:
                raise EvaluationError("common input lacks exact full stored plain-text readback", status="MISSING")
            status = "COMPLETE_COMMON_TEXT"
        result[document["source_path"]] = {"status": status, "parts": parts}
    return result
