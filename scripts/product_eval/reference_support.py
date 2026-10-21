"""Only proven original unknown file types become fixed-denominator misses.

Recognized formats with absent tools, failed extraction, policy skips, missing
assets or corrupt index rows remain MISSING. Candidate never uses this path.
"""
from pathlib import Path
import subprocess
import sys

from answer_eval.protocol import (EvaluationError, canonical_json, canonical_sha256,
                                  fields, parse_json, sha256_file)
from .bundle import REFERENCE_COMMIT

_JSON_KEY = 'product.original_support_json'
_SHA_KEY = 'product.original_support_sha256'


def _collect(store, bundle, corpus, production_root, identity):
    from .runner import production_content, production_identity

    production_content(identity)
    actual = production_identity(production_root, reference_run=True)
    if actual != identity:
        raise EvaluationError('original support receipt requires the exact clean original source identity')
    corpus = Path(corpus).resolve()
    paths = sorted(bundle.sources)
    inventory = {path:bundle.sources[path]['asset']['sha256'] for path in paths}
    for relative, digest in inventory.items():
        path = (corpus/relative).resolve()
        if not path.is_relative_to(corpus) or sha256_file(path) != digest:
            raise EvaluationError('original unsupported classification source bytes changed')
    probe = Path(__file__).with_name('_original_type_probe.py')
    import numpy as np
    numpy_root = Path(np.__file__).resolve().parent.parent
    argv = [sys.executable,'-I',str(probe.resolve()),str(Path(production_root).resolve()),str(corpus),str(numpy_root)]
    try:
        result = subprocess.run(argv,input=canonical_json({'paths':paths}),capture_output=True,text=True,
                                cwd=Path(production_root).resolve(),timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvaluationError(f'isolated original classifier unavailable: {exc}',status='MISSING') from exc
    if result.returncode:
        raise EvaluationError('isolated original classifier failed: '+result.stderr[-2000:],status='MISSING')
    observed = parse_json(result.stdout,'exact-original classification probe')
    fields(observed,{'module_files','runtime_numpy','walker_settings','observations'},'original classification probe')
    expected_modules = {name:identity['files'][name] for name in ('goldmine/ingest/rules.py','goldmine/ingest/walk.py','goldmine/store.py')}
    expected_numpy = {'version':np.__version__,'init_sha256':sha256_file(Path(np.__file__))}
    if (observed['module_files'] != expected_modules or observed['runtime_numpy'] != expected_numpy
            or production_identity(production_root,reference_run=True) != identity):
        raise EvaluationError('original classifier imported changed/shadowed source files')
    if (not isinstance(observed['observations'],list) or len(observed['observations']) != len(paths)
            or {row['path']:row['asset_sha256'] for row in observed['observations']} != inventory):
        raise EvaluationError('original classifier observation inventory does not match immutable source assets')
    unsupported = []
    for row in observed['observations']:
        decision, walk = row['classifier'], row['walker']
        if (decision != {'kind':None,'skip_reason':None}
                or walk != {'type':'SkipNotice','kind':None,'reason':'unknown type','is_dir':False}):
            continue
        path = str((corpus/row['path']).resolve())
        indexed = store.db.execute('SELECT id,sha256,state,capability,detail FROM files WHERE path=?',(path,)).fetchone()
        if indexed is None:
            raise EvaluationError('original unknown source is absent from actual preparation rows',status='MISSING')
        segments = store.db.execute('SELECT s.kind,s.modality,s.loc,s.loc_end,s.vec,f.name,f.body,f.original,s.excerpt '
                                    'FROM segments s LEFT JOIN fts f ON f.rowid=s.id WHERE s.file=? ORDER BY s.id',
                                    (indexed[0],)).fetchall()
        metadata = row['name_metadata']
        expected_name = ('name','none',None,None,None,metadata['name'],metadata['body'],None,metadata['name'])
        state = {'sha256':indexed[1],'state':indexed[2],'capability':indexed[3],'detail':indexed[4]}
        if (state != {'sha256':None,'state':'done','capability':'unsupported','detail':'unknown type'}
                or segments != [expected_name]):
            raise EvaluationError('original unknown source lacks the exact unsupported row / authentic single name metadata segment',status='MISSING')
        state.update({'content_segments':0,'vector_segments':0,'name_segments':1,'name_metadata':metadata})
        unsupported.append({'path':row['path'],'asset_sha256':row['asset_sha256'],'classifier':decision,'walker':walk,'index':state})
    return {'schema':1,'protocol':'product-original-unknown-type/v1','reference_commit':REFERENCE_COMMIT,
            'production':identity,'bundle_identity_sha256':canonical_sha256(bundle.identity),
            'source_inventory_sha256':canonical_sha256(inventory),
            'probe_script_sha256':sha256_file(probe),'classification':observed,'unsupported':unsupported}


def prepare_original_support(store,bundle,corpus,production_root,identity):
    """After actual exact-original ingest only; persist the recomputable receipt."""
    receipt = _collect(store,bundle,corpus,production_root,identity)
    store.set_meta(_JSON_KEY,canonical_json(receipt))
    store.set_meta(_SHA_KEY,canonical_sha256(receipt))
    return receipt


def verify_original_support(store,bundle,corpus,production_root,identity):
    """Recompute authority; a caller/edited receipt cannot supply a skip list."""
    text, digest = store.meta(_JSON_KEY), store.meta(_SHA_KEY)
    if not text or not digest:
        raise EvaluationError('original type preparation receipt is missing; explicitly rebuild',status='MISSING')
    recorded = parse_json(text,'original type preparation receipt')
    actual = _collect(store,bundle,corpus,production_root,identity)
    if recorded != actual or digest != canonical_sha256(actual):
        raise EvaluationError('original unsupported preparation receipt differs from recomputed source/classifier/index state')
    return actual
