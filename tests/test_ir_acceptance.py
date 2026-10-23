"""Reject fabricated, stale and incomplete IR/answer acceptance evidence."""
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'tests'))
from acceptance_lib.checks import Inputs
from acceptance_lib import ir_checks as ir
from acceptance_lib.evidence import InvalidEvidence,capture_command,read_json,sha256,write_json
from answer_eval_fixtures import make_run

@pytest.fixture
def targets():
    return read_json(ROOT/'evals/acceptance_targets.json')

def ref(path,manifest=False):
    return {'manifest_path' if manifest else 'path':str(path),'sha256':sha256(path)}

@pytest.mark.parametrize('name',['product','answer','public'])
def test_no_inputs_always_missing(name,targets):
    result=ir.check(ROOT,Inputs({'schema':2,'path_base':str(ROOT)}),name,targets)
    assert result[0]=='MISSING'
    assert 'missing explicit input' in result[1]['reason']

def test_draft_never_becomes_quality_pass(targets):
    i=Inputs({'schema':2,'path_base':str(ROOT),'product':{'source_manifest':ref(ROOT/'evals/product_v4/manifest.json')}})
    status,details=ir.product_dev(ROOT,i,targets)
    assert status=='MISSING' and 'incomplete' in details['reason']

def test_only_explicit_real_dev_flags_allowed():
    base=['python','script']
    valued={'--bundle':'/b','--build':'/c','--db':'/d','--production-root':'/p','--backend':'llama','--model':'/m','--server':'/s','--parameters':'/a','--split':'dev'}
    argv=base+sum(([key,value] for key,value in valued.items()),[])+['--json']
    assert ir._flags(argv)['--split']=='dev'
    for extra in ['--diagnostic','--prepare','--validate','--seal-reference','--freeze']:
        with pytest.raises(InvalidEvidence): ir._flags(argv+[extra])
    changed=argv.copy(); changed[changed.index('dev')]='holdout'
    with pytest.raises(InvalidEvidence): ir._flags(changed)
    changed=argv.copy(); changed[changed.index('llama')]='mock'
    with pytest.raises(InvalidEvidence): ir._flags(changed)

def test_print_pass_wrapper_is_not_trusted_execution(tmp_path):
    directory=tmp_path/'wrapper'
    capture_command(directory,'answer.compare',[sys.executable,'-c',"print('PASS')"],cwd=ROOT,suite_id='answer-eval/v3',identity={'evaluator_files':ir.evaluator_files(ROOT,'answer')})
    with pytest.raises(InvalidEvidence,match='registered Python/script'):
        ir._command(directory/'manifest.json',ROOT,kind='answer',suite='answer-eval/v3',check_id='answer.compare')

def test_actual_trusted_missing_compare_captured_and_script_sha_enforced(tmp_path):
    directory=tmp_path/'capture'
    capture_command(directory,'answer.compare',[sys.executable,str(ROOT/'scripts/eval_answer.py'),'--compare',str(tmp_path/'absent'),str(tmp_path/'absent2'),'--json'],cwd=ROOT,suite_id='answer-eval/v3',identity={'evaluator_files':ir.evaluator_files(ROOT,'answer')})
    _,receipt,stdout=ir._command(directory/'manifest.json',ROOT,kind='answer',suite='answer-eval/v3',check_id='answer.compare')
    assert receipt['exit_code']==2 and read_json(stdout)['release_eligible'] is False
    manifest=read_json(directory/'manifest.json')
    manifest['identity']['evaluator_files']['scripts/eval_answer.py']='0'*64
    write_json(directory/'manifest.json',manifest)
    with pytest.raises(InvalidEvidence,match='scripts changed'):
        ir._command(directory/'manifest.json',ROOT,kind='answer',suite='answer-eval/v3',check_id='answer.compare')

@pytest.mark.parametrize('mode',['fixture','real'])
def test_complete_synthetic80_job_records_cannot_pass_by_relabeling(mode,tmp_path,targets):
    a=make_run(tmp_path/'a',run_id='a',mode=mode)
    b=make_run(tmp_path/'b',run_id='b',mode=mode)
    i=Inputs({'schema':2,'path_base':str(tmp_path),'answer':{'reference_run':ref(a/'run.json',True),'candidate_run':ref(b/'run.json',True),'preregistration':ref(a/'preregistration.json')}})
    status,details=ir.answer(ROOT,i,targets)
    assert status=='MISSING'
    assert 'fixture' in details['reason'] or 'synthetic/mock' in details['reason']

def test_historical20_or_schema1_is_not_new_answer_evidence(tmp_path,targets):
    original=tmp_path/'run.json'; write_json(original,{'schema':1,'answers':[{}]*20})
    i=Inputs({'schema':2,'path_base':str(tmp_path),'answer':{'reference_run':ref(original,True),'candidate_run':ref(original,True)}})
    status,details=ir.answer(ROOT,i,targets)
    assert status=='MISSING' and ('missing=' in details['reason'] or 'legacy' in details['reason'])

def test_public_gap_has_concrete_raw_export_migration(targets):
    status,details=ir.public(ROOT,Inputs({'schema':2,'path_base':str(ROOT)}),targets)
    assert status=='MISSING'
    plan=details['required_raw_export']
    assert set(plan['datasets'])=={'scifact','nfcorpus','arguana'}
    assert set(plan['arms'])=={'lexical','semantic','rrf','hybrid'}
    assert 'post_cut_ranked_docids' in plan['per_query_required'] and len(plan['migration'])>=4


def test_asr_witness_must_occur_literally_in_actual_cited_segment():
    from types import SimpleNamespace
    import hashlib
    from product_eval.alignment import asr_word_spans

    canonical = 'THE WITNESS DID NOT BRING THE SIGNED REPORT HOME TODAY'
    observed = 'The witness did not bring the signed report home today.'
    span = {**asr_word_spans(canonical, observed)[0], 'evidence_type': 'asr_text'}
    store = SimpleNamespace(segment_text=lambda sid: observed if sid == 7 else '')
    ir._observed_readback(store, 7, span)
    changed = canonical.lower()  # Same tokens; different actual case/punctuation bytes.
    span['word_alignment']['observed_text'] = changed
    span['word_alignment']['observed_sha256'] = hashlib.sha256(changed.encode()).hexdigest()
    with pytest.raises(InvalidEvidence, match='actual pinned SQLite segment'):
        ir._observed_readback(store, 7, span)
