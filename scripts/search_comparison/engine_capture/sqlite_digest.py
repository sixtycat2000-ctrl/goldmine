"""Cross-runtime physical SQLite identity. Only QMD's declared LLM cache may vary."""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import struct

from answer_eval.protocol import EvaluationError


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def physical(path):
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"))
        result = {"schema": [[n, s] for n, s in tables if n != "llm_cache"], "tables": {},
                  "user_version": db.execute("PRAGMA user_version").fetchone()[0]}
        for name, sql in tables:
            if name == "llm_cache" or (sql or "").upper().startswith("CREATE VIRTUAL TABLE"):
                continue
            if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) is None:
                raise EvaluationError("unexpected native SQLite table name")
            rows = []
            for row in db.execute('SELECT * FROM "' + name + '"'):
                values = []
                for v in row:
                    if isinstance(v, bytes):
                        v = {"bytes": len(v), "sha256": hashlib.sha256(v).hexdigest()}
                    elif type(v) in (int, float):
                        if not math.isfinite(v) or type(v) is int and abs(v) > (1 << 53) - 1:
                            raise EvaluationError("unsupported non-exact cross-runtime SQL numeric value")
                        v = {"number_f64_be": struct.pack(">d", float(v)).hex()}
                    values.append(v)
                rows.append(_sha(values))
            result["tables"][name] = {"rows": len(rows), "sha256": _sha(sorted(rows))}
        return result
