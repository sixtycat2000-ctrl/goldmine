"""BEIR ZIP identities and independently recomputable post-cut ranked results."""
from __future__ import annotations

import hashlib
import math
from contextlib import closing
from pathlib import Path
import sqlite3
import zipfile

from answer_eval.protocol import EvaluationError, canonical_sha256, parse_json, sha256_file

SUITE = "beir-mini.raw/v1"
DATASETS = ("scifact", "nfcorpus", "arguana")
ARMS = ("lexical", "semantic", "rrf", "hybrid")
FETCH_LIMIT, TOP_K = 20, 10


def text_sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def preparation_identity(production, *, reference=False):
    from product_eval.runner import production_content

    content = production_content(production)
    return {"public.production_schema": "production-identity/v1" if reference else "production-content/v1",
            "public.production": canonical_sha256(production if reference else content)}


def load_archive(path: Path, entry: dict):
    """Read qrels/corpus/query bytes from the pinned ZIP, never an edited extraction."""
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
        raise EvaluationError(f"public ZIP missing or differs from its pin: {path}", status="MISSING")
    raw = {}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise EvaluationError("public ZIP contains duplicate member names")
        for key, suffix in (("corpus", "corpus.jsonl"), ("queries", "queries.jsonl"), ("qrels", "qrels/test.tsv")):
            matches = [name for name in names if name == suffix or name.endswith("/" + suffix)]
            if len(matches) != 1:
                raise EvaluationError(f"public ZIP must have exactly one {suffix}")
            member = matches[0]
            if archive.getinfo(member).file_size > 64 * 1024 * 1024:
                raise EvaluationError("public mini-suite member exceeds the bounded parser limit")
            raw[key] = (member, archive.read(member))

    def rows(key):
        seen, result = set(), {}
        for number, line in enumerate(raw[key][1].decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = parse_json(line, f"{key}:{number}")
            if (not isinstance(row, dict) or not isinstance(row.get("_id"), str) or not row["_id"]
                    or row["_id"] in seen or not isinstance(row.get("text"), str)):
                raise EvaluationError(f"public {key}: duplicate/invalid ID or text")
            seen.add(row["_id"])
            result[row["_id"]] = row
        return result

    corpus, query_rows = rows("corpus"), rows("queries")
    if len(corpus) != entry["n_docs"]:
        raise EvaluationError("public corpus denominator differs from manifest")
    if any(not isinstance(row.get("title", ""), str) for row in corpus.values()):
        raise EvaluationError("public corpus title must be text")
    queries = {qid: row["text"] for qid, row in query_rows.items()}
    qrels = {}
    for number, line in enumerate(raw["qrels"][1].decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.strip().split("\t")
        if number == 1 and parts == ["query-id", "corpus-id", "score"]:
            continue
        if len(parts) not in (3, 4):
            raise EvaluationError("public qrels must have three or four TSV columns")
        qid, docid = parts[0], parts[-2]
        try:
            grade = int(parts[-1])
        except ValueError as error:
            raise EvaluationError("public qrel grade is not an integer") from error
        if (not qid or not docid or qid not in queries or not queries[qid]
                or not entry["grade_range"][0] <= grade <= entry["grade_range"][1]
                or docid in qrels.get(qid, {})):
            raise EvaluationError("public qrels have missing query text, duplicate pair or invalid grade")
        qrels.setdefault(qid, {})[docid] = grade  # Dangling docids remain in IDCG.
    if len(qrels) != entry["n_queries_test"] or not qrels:
        raise EvaluationError("public test-query denominator differs from manifest; intersection is forbidden")
    identity = {"zip_sha256": entry["sha256"], "zip_bytes": entry["bytes"],
                "members": {key: {"member": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                            for key, (name, data) in raw.items()},
                "doc_ids_sha256": canonical_sha256(sorted(corpus)),
                "query_ids_sha256": canonical_sha256(sorted(qrels)),
                "n_docs": len(corpus), "n_queries_test": len(qrels)}
    return {"identity": identity, "corpus": corpus, "queries": queries, "qrels": qrels}


def score(docids, grades, max_grade):
    gains = [grades.get(docid, 0) for docid in docids]
    relevant = [i + 1 for i, grade in enumerate(gains) if grade == max_grade]
    ideal = sorted(grades.values(), reverse=True)[:TOP_K]
    dcg = sum(grade / math.log2(i + 2) for i, grade in enumerate(gains[:TOP_K]) if grade > 0)
    idcg = sum(grade / math.log2(i + 2) for i, grade in enumerate(ideal) if grade > 0)
    return {"success1": float(bool(relevant and relevant[0] == 1)),
            "success5": float(bool(relevant and relevant[0] <= 5)),
            "rr": 1 / relevant[0] if relevant else 0.0,
            "ndcg": dcg / idcg if idcg else None, "n_hits": len(docids), "noise": False}


def ranked_row(qid, fetched, data, max_grade):
    if (not isinstance(fetched, list) or len(fetched) > FETCH_LIMIT
            or any(not isinstance(docid, str) or docid not in data["corpus"] for docid in fetched)
            or len(fetched) != len(set(fetched))):
        raise EvaluationError("public rankings have duplicate/unknown docids or exceed fetch limit")
    if qid not in data["qrels"]:
        raise EvaluationError("public ranking query is outside the pinned test set")
    kept = [docid for docid in fetched if docid != qid][:TOP_K]
    return {"id": qid, "query_text_sha256": text_sha(data["queries"][qid]),
            "qrels_sha256": data["identity"]["members"]["qrels"]["sha256"],
            "fetched_ranked_docids": fetched, "post_cut_ranked_docids": kept,
            "metrics": score(kept, data["qrels"][qid], max_grade)}


def summarize(rows):
    result = {}
    for label, field in (("nDCG@10", "ndcg"), ("success@5", "success5"), ("success@1", "success1"), ("MRR", "rr")):
        values = [row["metrics"][field] for row in rows if row["metrics"][field] is not None]
        total = sum(values)
        result[label] = {"n": len(values), "sum": total, "value": total / len(values) if values else None}
    return result


def validate_arms(arms, data, entry):
    if not isinstance(arms, dict) or set(arms) != set(ARMS):
        raise EvaluationError("public raw export needs all four retrieval arms")
    recomputed = {}
    for arm in ARMS:
        value = arms[arm]
        if not isinstance(value, dict) or set(value) != {"rows", "metrics"} or not isinstance(value["rows"], list):
            raise EvaluationError("public arm requires raw rows and recomputable metrics")
        rows = value["rows"]
        if [row.get("id") for row in rows if isinstance(row, dict)] != sorted(data["qrels"]):
            raise EvaluationError("public query IDs/order/denominator differ; intersections are forbidden")
        expected = [ranked_row(row["id"], row["fetched_ranked_docids"], data, entry["grade_range"][1]) for row in rows]
        if rows != expected or value["metrics"] != summarize(expected):
            raise EvaluationError("public row hashes/post-cut IDs/metrics differ from actual pinned qrels")
        recomputed[arm] = summarize(expected)
    return recomputed


def inspect_index(path, data, production, corpus_root, backend_name, *, reference=False):
    """Verify preparation metadata and the complete file/source mapping read-only."""
    from eval_public import doc_md_bytes, docid_to_filename

    path, corpus_root = Path(path).resolve(), Path(corpus_root).resolve()
    wal = Path(str(path) + "-wal")
    if not path.is_file() or (wal.exists() and wal.stat().st_size):
        raise EvaluationError("public index must be explicitly prepared and checkpointed before scoring", status="MISSING")
    expected_meta = {"public.input_identity": canonical_sha256(data["identity"]),
                     **preparation_identity(production, reference=reference), "public.backend": backend_name}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise EvaluationError("public index must use its explicitly checkpointed single-file preparation")
        actual_meta = dict(connection.execute("SELECT key,value FROM meta WHERE key LIKE 'public.%'"))
        if actual_meta != expected_meta:
            raise EvaluationError("public index preparation source/input/backend identity differs")
        actual = list(connection.execute("SELECT path,sha256,state,capability FROM files"))
        expected = {str(corpus_root / docid_to_filename(docid)): hashlib.sha256(doc_md_bytes(doc)).hexdigest()
                    for docid, doc in data["corpus"].items()}
        if (len(actual) != len(expected) or any(row[0] not in expected or row[1] != expected[row[0]]
                                              or row[2] != "done" or row[3] != "content_indexed" for row in actual)):
            raise EvaluationError("public index has missing/extra/failed documents or changed corpus bytes")
        if backend_name == "llama" and connection.execute("SELECT COUNT(*) FROM segments WHERE kind!='name' AND vec IS NOT NULL").fetchone()[0] < len(expected):
            raise EvaluationError("public real index lacks content embeddings")
        fingerprint = connection.execute("SELECT value FROM meta WHERE key='fingerprint_json'").fetchone()
        if fingerprint is None:
            raise EvaluationError("public index lacks its prepared embedding fingerprint")
    return {"path": str(path), "sha256": sha256_file(path), "metadata": expected_meta,
            "fingerprint": parse_json(fingerprint[0], "public index fingerprint")}
