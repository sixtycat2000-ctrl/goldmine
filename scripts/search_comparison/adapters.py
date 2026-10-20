"""Strict parsers for outputs observed from the installed competitor versions."""

from __future__ import annotations

import base64
import binascii
import math
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, unquote_to_bytes, urlsplit

from answer_eval.protocol import EvaluationError, parse_json


class DocumentMap:
    def __init__(self, corpus_root, documents, *, collection="common", index="comparison"):
        self.root = Path(corpus_root).resolve()
        self.collection, self.index = collection, index
        self.local = {}
        self.qmd = {}
        for document in documents:
            path = document["view_path"]
            absolute = (self.root / path).resolve()
            if not absolute.is_relative_to(self.root) or absolute in self.local or path in self.qmd:
                raise EvaluationError("duplicate or unsafe comparison document mapping")
            self.local[absolute] = document["source_path"]
            self.qmd[path] = document["source_path"]

    def file_path(self, path):
        if not isinstance(path, str):
            raise EvaluationError("result file path is not text")
        resolved = Path(path).resolve()
        if resolved not in self.local:
            raise EvaluationError("result is not in the frozen document mapping: " + path)
        return self.local[resolved]

    def qmd_uri(self, uri):
        if not isinstance(uri, str):
            raise EvaluationError("QMD URI is not text")
        parsed = urlsplit(uri)
        query = parse_qs(parsed.query, strict_parsing=True)
        path = unquote(parsed.path).removeprefix("/")
        if (parsed.scheme != "qmd" or parsed.netloc != self.collection or parsed.fragment
                or query not in ({}, {"index": [self.index]}) or path not in self.qmd):
            raise EvaluationError("QMD URI is not in the frozen collection mapping: " + uri)
        return self.qmd[path]

    def recoll_url(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise EvaluationError("Recoll returned an unmapped or non-file URL")
        try:
            path = unquote_to_bytes(parsed.path).decode("utf-8")
        except UnicodeError as error:
            raise EvaluationError("Recoll URL is not UTF-8") from error
        return self.file_path(path)


def _file_results(rows):
    """First appearance determines document rank; never sort engine scores."""
    seen, result, duplicates = set(), [], 0
    for row in rows:
        if row["path"] in seen:
            duplicates += 1
            continue
        seen.add(row["path"])
        result.append({**row, "file_rank": len(result) + 1})
    return {"hits": result, "duplicate_file_rows": duplicates}


def parse_qmd(stdout, mapping: DocumentMap, *, limit=10):
    data = parse_json(stdout, "QMD JSON stdout")
    if not isinstance(data, list) or len(data) > limit:
        raise EvaluationError("QMD JSON must be a bounded result list")
    rows = []
    for rank, row in enumerate(data, 1):
        if (not isinstance(row, dict) or not {"docid", "score", "file", "title"} <= row.keys()
                or set(row) - {"docid", "score", "file", "title", "line", "snippet", "context", "body"}
                or not isinstance(row["docid"], str) or re.fullmatch(r"#[0-9a-f]{6,64}", row["docid"]) is None
                or type(row["score"]) not in (int, float) or not math.isfinite(row["score"])
                or not isinstance(row["title"], str)
                or ("line" in row and (type(row["line"]) is not int or row["line"] < 1))
                or any(not isinstance(row[key], str) for key in ("snippet", "context", "body") if key in row)):
            raise EvaluationError("QMD result shape/type differs from the pinned formatter")
        rows.append({"path": mapping.qmd_uri(row["file"]), "engine_rank": rank,
                     "engine_id": row["docid"], "score": row["score"], "native_uri": row["file"]})
    return _file_results(rows)


def parse_recoll(stdout, mapping: DocumentMap, *, limit=10):
    lines = stdout.splitlines()
    if len(lines) < 2 or not lines[0].startswith("Recoll query: "):
        raise EvaluationError("Recoll output is missing its observed query header")
    count = re.fullmatch(r"([0-9]+) results", lines[1])
    if count is None:
        raise EvaluationError("Recoll output is missing its observed result-count header")
    total = int(count[1])
    encoded = [line.split() for line in lines[2:] if line.strip()]
    if len(encoded) != min(total, limit) or any(len(fields) != 1 for fields in encoded):
        raise EvaluationError("Recoll -F url row count/field count mismatch")
    rows = []
    for rank, fields in enumerate(encoded, 1):
        try:
            url = base64.b64decode(fields[0], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError) as error:
            raise EvaluationError("Recoll field is not strict base64 UTF-8") from error
        rows.append({"path": mapping.recoll_url(url), "engine_rank": rank,
                     "engine_id": url, "score": None, "native_uri": url})
    return {**_file_results(rows), "reported_total": total, "processed_query": lines[0]}


def parse_goldmine(stdout, mapping: DocumentMap, *, limit=10):
    data = parse_json(stdout, "goldmine JSON stdout")
    if not isinstance(data, list) or len(data) > limit:
        raise EvaluationError("goldmine output lacks a bounded hit list")
    rows = []
    for rank, row in enumerate(data, 1):
        if (not isinstance(row, dict) or row.get("rank") != rank or not isinstance(row.get("path"), str)
                or type(row.get("score")) not in (int, float) or not math.isfinite(row["score"])):
            raise EvaluationError("goldmine hit rank/path/score invalid")
        rows.append({"path": mapping.file_path(row["path"]), "engine_rank": rank,
                     "engine_id": row.get("citation", row.get("segment_id")), "score": row["score"], "native_uri": row["path"]})
    return _file_results(rows)
