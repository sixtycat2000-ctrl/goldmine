"""Read actual original schema4 bytes from candidate acceptance, without migration."""
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_lib import ir_checks as ir
from acceptance_lib.evidence import InvalidEvidence, sha256
from acceptance_lib.sqlite_readback import OriginalReadback
from answer_eval.protocol import EvaluationError, canonical_json, canonical_sha256
from product_eval.evidence import read_segment_spans


def test_genuine_original_schema4_readback_and_unsupported_forgery_rejection(tmp_path):
    original = tmp_path / "original"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", "--quiet", str(ROOT), str(original)], check=True)
    subprocess.run(["git", "-C", str(original), "checkout", "--detach", "--quiet", "769483752f49fb0bf8241dca778b1895b9a1b70b"], check=True)
    raw = "The original readback fixture contains one contiguous independently stored source fact.\n"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    corpus, index = tmp_path / "corpus", tmp_path / "original.sqlite"
    corpus.mkdir()
    for name in ("supported.txt", "unknown.rst"):
        (corpus / name).write_text(raw)
    sources = {name: {"asset": {"sha256": digest}, "kind": "doc", "views": []} for name in ("supported.txt", "unknown.rst")}
    definition = {"id": "body", "evidence_type": "content_text", "location": {"unit": "line", "start": 1, "end": 1}}
    sources["supported.txt"]["views"] = [definition]
    bundle = SimpleNamespace(identity={"mechanical_fixture": True}, sources=sources, queries=[],
                             views={("supported.txt", "body"): {**definition, "text": raw}})
    from product_eval.materialization import POLICY, expected_flags
    flags = expected_flags(sources)
    build = {"schema": 2, "suite_id": "product.v4", "identity": bundle.identity,
             "files": {name: digest for name in sources}, "diagnostic_only": True,
             "materialization": {"policy": POLICY, "expected": flags, "observed": flags}}
    (tmp_path / "build.json").write_text(json.dumps(build))
    program = textwrap.dedent('''
        import json, sys
        from pathlib import Path
        from types import SimpleNamespace
        scripts, original, corpus, index = map(Path, sys.argv[1:])
        sys.path.insert(0,str(scripts))
        from product_eval.runner import select_production,production_identity
        from product_eval.reference_support import prepare_original_support
        from answer_eval.protocol import canonical_sha256,sha256_file
        select_production(original)
        from goldmine.config import Config
        from goldmine.backends import create_backend
        from goldmine.ingest import IngestPipeline
        from goldmine.store import Store
        data=json.loads(sys.stdin.read())
        bundle=SimpleNamespace(**data)
        cfg=Config(home=index.parent/'home')
        backend=create_backend(cfg,'mock')
        store=Store(index)
        assert Store.SCHEMA_VERSION==4
        assert IngestPipeline(cfg,store,backend).run([str(corpus)],code=True).failed==0
        source=production_identity(original,reference_run=True)
        prepare_original_support(store,bundle,corpus,original,source)
        for key,value in {'product.bundle_identity':canonical_sha256(bundle.identity),
                          'product.build_receipt_sha256':sha256_file(corpus.parent/'build.json'),
                          'product.prepared_code_schema':'production-identity/v1',
                          'product.prepared_code':canonical_sha256(source),'product.backend':'llama'}.items():
            store.set_meta(key,value)
        store.close()
        backend.close()
        print(json.dumps(source))
    ''')
    result = subprocess.run([sys.executable, "-c", program, str(ROOT / "scripts"), str(original), str(corpus), str(index)],
                            input=json.dumps({"identity": bundle.identity, "sources": sources, "queries": []}), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    production = json.loads(result.stdout)
    with closing(OriginalReadback(index)) as audit:
        sid = audit.db.execute("SELECT s.id FROM segments s JOIN files f ON f.id=s.file WHERE f.path=? AND s.kind!='name'", (str(corpus / "supported.txt"),)).fetchone()[0]
        spans = read_segment_spans(audit, sid, "supported.txt", str(corpus / "supported.txt"), bundle)
        assert spans
    report = {"reference_verified": True, "production": production,
              "index_identity": {"build_receipt_sha256": sha256(tmp_path / "build.json")},
              "rows": [{"pack_records": [], "hits": [{"spans": spans}], "emitted_spans": []}]}
    context = {"production_root": original, "production": production}
    before = sha256(index)
    assert ir._readback(report, index, corpus, bundle, original_reference=context) == len(spans)
    assert sha256(index) == before
    with closing(sqlite3.connect(index)) as writable:
        assert writable.execute("PRAGMA user_version").fetchone()[0] == 4
        stored = json.loads(writable.execute("SELECT value FROM meta WHERE key='product.original_support_json'").fetchone()[0])
        forged = copy.deepcopy(stored)
        forged["unsupported"].append({**forged["unsupported"][0], "path": "supported.txt"})
        writable.execute("UPDATE meta SET value=? WHERE key='product.original_support_json'", (canonical_json(forged),))
        writable.execute("UPDATE meta SET value=? WHERE key='product.original_support_sha256'", (canonical_sha256(forged),))
        writable.commit()
    with pytest.raises(EvaluationError, match="differs from recomputed"):
        ir._readback(report, index, corpus, bundle, original_reference=context)
    with pytest.raises(InvalidEvidence, match="explicit source context"):
        ir._readback(report, index, corpus, bundle)


def test_original_readback_rejects_other_schema_without_upgrading(tmp_path):
    index = tmp_path / "wrong.sqlite"
    with closing(sqlite3.connect(index)) as connection:
        connection.execute("PRAGMA user_version=6")
    before = sha256(index)
    with pytest.raises(InvalidEvidence, match="schema-v4"):
        OriginalReadback(index)
    assert sha256(index) == before
