"""Actual original classifier/ingest anti-forgery mechanics, no quality run."""

from pathlib import Path
import json
import subprocess
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = "769483752f49fb0bf8241dca778b1895b9a1b70b"


def test_exact_original_unknown_type_receipt_cannot_hide_tool_policy_asset_or_index_failures(tmp_path):
    checkout = tmp_path / "original"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", "--quiet", str(ROOT), str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", "--quiet", REFERENCE], check=True)
    program = textwrap.dedent('''
        from pathlib import Path
        import copy, hashlib, json, sys
        from types import SimpleNamespace
        scripts, original, work = map(Path, sys.argv[1:])
        sys.path.insert(0, str(scripts))
        from answer_eval.protocol import EvaluationError, canonical_json, canonical_sha256
        from product_eval.runner import production_identity, select_production
        from product_eval.reference_support import prepare_original_support, verify_original_support
        from product_eval.evidence import index_capability_missing
        from product_eval.scoring import score_query
        select_production(original)
        from goldmine.config import Config
        from goldmine.backends import create_backend
        from goldmine.ingest import IngestPipeline
        from goldmine.store import Store

        corpus = work / 'corpus'
        corpus.mkdir()
        raw = b'The original type capability fixture contains text, without any relevance claim.\\n'
        for name in ('unknown.rst', 'supported.txt', 'secret.rst'):
            (corpus/name).write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        sources = {name:{'asset':{'sha256':digest},'kind':'doc','views':[]} for name in ('unknown.rst','supported.txt','secret.rst')}
        bundle = SimpleNamespace(identity={'unit':'mechanical-only'},sources=sources,queries=[])
        cfg = Config(home=work/'.home')
        backend = create_backend(cfg,'mock')
        store = Store(work/'index.sqlite')
        imported = IngestPipeline(cfg,store,backend).run([str(corpus)],code=True)
        assert imported.failed == 0
        identity = production_identity(original,reference_run=True)
        receipt = prepare_original_support(store,bundle,corpus,original,identity)
        assert [row['path'] for row in receipt['unsupported']] == ['unknown.rst']
        assert verify_original_support(store,bundle,corpus,original,identity) == receipt
        context = {'production_root':original,'production':identity}
        strict = index_capability_missing(store,bundle,corpus)
        allowed = index_capability_missing(store,bundle,corpus,original_reference=context)
        assert any(value.startswith('unknown.rst:') for value in strict)
        assert not any(value.startswith('unknown.rst:') for value in allowed)
        assert any(value.startswith('secret.rst:') for value in allowed)
        # Unsupported is a miss, never a removed question or fabricated hit.
        question = {'primary_class':'semantic','expect':[{'evidence_id':'e0','path':'unknown.rst','grade':3}],
                    'required_facts':[{'fact_id':'f0','sufficient_evidence':[['e0']]}]}
        scored = score_query(question,[],[])
        assert scored['success@5'] == scored['all_required_facts@pack'] == 0

        rejected = []
        def fails(label, operation):
            try:
                operation()
            except EvaluationError:
                rejected.append(label)
            else:
                raise AssertionError('accepted forged original support: '+label)

        # Receipt edits remain rejected even if the attacker recomputes its SHA.
        forged = copy.deepcopy(receipt)
        forged['unsupported'].append({**forged['unsupported'][0],'path':'supported.txt'})
        store.set_meta('product.original_support_json',canonical_json(forged))
        store.set_meta('product.original_support_sha256',canonical_sha256(forged))
        fails('fake_saved_list',lambda:verify_original_support(store,bundle,corpus,original,identity))
        store.set_meta('product.original_support_json',canonical_json(receipt))
        store.set_meta('product.original_support_sha256',canonical_sha256(receipt))
        fails('fake_caller_list',lambda:index_capability_missing(store,bundle,corpus,original_reference={'unsupported':['supported.txt']}))

        (corpus/'unknown.rst').write_bytes(raw+b'changed')
        fails('changed_asset',lambda:verify_original_support(store,bundle,corpus,original,identity))
        (corpus/'unknown.rst').write_bytes(raw)
        moved = copy.deepcopy(bundle)
        moved.sources['renamed.txt'] = moved.sources.pop('unknown.rst')
        (corpus/'renamed.txt').write_bytes(raw)
        fails('changed_type',lambda:verify_original_support(store,moved,corpus,original,identity))
        (corpus/'renamed.txt').unlink()

        fid = store.db.execute('SELECT id FROM files WHERE path=?',(str(corpus/'unknown.rst'),)).fetchone()[0]
        store.db.execute("UPDATE files SET detail='key or password file' WHERE id=?",(fid,))
        fails('changed_policy_reason',lambda:verify_original_support(store,bundle,corpus,original,identity))
        store.db.execute("UPDATE files SET detail='unknown type' WHERE id=?",(fid,))
        sid = store.db.execute("INSERT INTO segments(file,kind,modality,loc,loc_end) VALUES(?,?,?,?,?)",(fid,'doc','text',1,1)).lastrowid
        fails('invented_segment',lambda:verify_original_support(store,bundle,corpus,original,identity))
        store.db.execute('DELETE FROM segments WHERE id=?',(sid,))
        name_sid = store.db.execute("SELECT id FROM segments WHERE file=? AND kind='name'",(fid,)).fetchone()[0]
        name_body = next(row['name_metadata']['body'] for row in receipt['classification']['observations'] if row['path']=='unknown.rst')
        store.db.execute('UPDATE fts SET body=? WHERE rowid=?',(name_body+' invented content',name_sid))
        fails('invented_name_body',lambda:verify_original_support(store,bundle,corpus,original,identity))
        store.db.execute('UPDATE fts SET body=? WHERE rowid=?',(name_body,name_sid))
        store.db.execute("UPDATE fts SET original='invented original' WHERE rowid=?",(name_sid,))
        fails('invented_name_original',lambda:verify_original_support(store,bundle,corpus,original,identity))
        store.db.execute('UPDATE fts SET original=NULL WHERE rowid=?',(name_sid,))
        store.db.execute('UPDATE segments SET vec=? WHERE id=?',(b'fake-vector',name_sid))
        fails('invented_name_vector',lambda:verify_original_support(store,bundle,corpus,original,identity))
        store.db.execute('UPDATE segments SET vec=NULL WHERE id=?',(name_sid,))
        store.db.execute("UPDATE segments SET kind='chunk' WHERE id=?",(name_sid,))
        fails('changed_name_kind',lambda:verify_original_support(store,bundle,corpus,original,identity))
        store.db.execute("UPDATE segments SET kind='name' WHERE id=?",(name_sid,))

        # A recognized image with missing OCR/tool support is not an extension
        # miss. This row simulates the failure without invoking native OCR.
        image = b'\\x89PNG\\r\\n\\x1a\\nmechanical missing-tool fixture'
        (corpus/'missing.png').write_bytes(image)
        bundle.sources['missing.png'] = {'asset':{'sha256':hashlib.sha256(image).hexdigest()},'kind':'image','views':[]}
        store.db.execute("INSERT INTO files(path,source,kind,size,state,capability,detail) VALUES(?,?,?,?,?,?,?)",
                         (str(corpus/'missing.png'),'file','image',len(image),'done','unsupported','OCR tool unavailable'))
        updated = prepare_original_support(store,bundle,corpus,original,identity)
        assert [row['path'] for row in updated['unsupported']] == ['unknown.rst']
        missing = index_capability_missing(store,bundle,corpus,original_reference=context)
        assert any(value.startswith('missing.png:') for value in missing)
        rejected.append('recognized_missing_tool_never_bypassed')
        backend.close()
        store.close()
        print(json.dumps({'rejected':rejected,'unsupported':['unknown.rst'],'quality_evidence':False}))
    ''')
    result = subprocess.run([sys.executable, "-c", program, str(ROOT / "scripts"), str(checkout), str(tmp_path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert len(receipt["rejected"]) == 11 and receipt["quality_evidence"] is False
