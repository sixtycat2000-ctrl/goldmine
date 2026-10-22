"""Preregistration, dependency attribution and owned-output denial mechanics."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from answer_eval.protocol import EvaluationError, sha256_file
from search_comparison.measurements import packet as compiler, registered, storage
from search_comparison.measurements import _dependency_worker as missing_role
from measurement_executor_fixtures import fake_plan, marker


def compilable(root):
    plan,_,_=fake_plan(root)
    from measurement_executor_fixtures import requests
    from search_comparison.measurements.selection import ARMS
    plan.arms=ARMS
    plan.projection={'requests':requests(),'reference':{'path':str(root/'synthetic-export.json'),'sha256':'c'*64},'identity':{'declared_synthetic':True}}
    return plan


@pytest.mark.parametrize('mutation',['seed','drop-slot','swap','prime','cohort','old-compiler','code','extra-field','bool-schema','bool-ordinal'])
def test_exact_current_packet_required_and_old_compiler_not_silently_accepted(tmp_path,mutation):
    root=tmp_path.resolve();plan=compilable(root)
    p=compiler.compile_packet(plan,registered_at='2026-01-02T00:00:00+00:00')
    if mutation=='seed':p['seed']='other'
    elif mutation=='drop-slot':p['schedule'].pop()
    elif mutation=='swap':p['schedule'][0],p['schedule'][1]=p['schedule'][1],p['schedule'][0]
    elif mutation=='prime':next(s for s in p['schedule'] if s['prime_ordinal'] is not None)['prime_ordinal']=1
    elif mutation=='cohort':p['source_cohorts']['base112'][0]=p['source_cohorts']['add10'][0]
    elif mutation=='old-compiler':p['executor']={'status':'NOT_IMPLEMENTED_BY_THIS_COMPILER'}
    elif mutation=='code':p['implementation']['sha256']='e'*64
    elif mutation=='bool-schema':p['schema']=True
    elif mutation=='bool-ordinal':p['schedule'][0]['ordinal']=True
    else:p['executor']['command_hook']=['/bin/sh']
    path=root/'execution-packet.json';path.write_text(json.dumps(p))
    with pytest.raises(EvaluationError):compiler.load_packet({'path':str(path),'sha256':sha256_file(path)},plan)


def test_current_packet_has_all_fixed_denominators_and_entrypoint_closure(tmp_path):
    root=tmp_path.resolve();plan=compilable(root)
    p=compiler.compile_packet(plan,registered_at='2026-01-02T00:00:00+00:00')
    path=root/'execution-packet.json';path.write_text(json.dumps(p))
    assert compiler.load_packet({'path':str(path),'sha256':sha256_file(path)},plan)==p
    assert p['denominators']['all_slots']==1728
    assert 'scripts/capture_comparison_measurements.py' in p['implementation']['files']
    assert p['quality_evidence'] is False and p['holdout_entry'] is None


@pytest.mark.parametrize('arm',['qmd.search','qmd.vsearch','qmd.query','recoll.default','recoll.anyterm'])
def test_missing_role_validates_unchanged_full_descriptor_before_deliberate_absence(tmp_path,monkeypatch,arm):
    root=tmp_path.resolve();owner=root/'owned';marker(owner,arm.split('.')[0])
    plan,_,_=fake_plan(root)
    name=arm.split('.')[0]
    descriptor={'model_roles':{'embed':{'resource':{'path':'/declared-original-embedding'}}},'tools':{'recollindex':{'path':'/declared-original-indexer'}}}
    plan.value['engines'][name]=descriptor
    binding=registered.registration(owner,plan)[name]
    calls=[]
    def resources(engine,value,**kwargs):
        calls.append(deepcopy(value))
        assert kwargs['fixture'] is False and kwargs['require_canary'] is False
        if len(calls)>1 and arm!='qmd.search':raise EvaluationError('declared absent role, after baseline success')
        return {}
    monkeypatch.setattr(missing_role,'engine_resources',resources)
    request={'schema':1,'owner':str(owner),'scenario':'qmd-embedding-absent' if name=='qmd' else 'recoll-indexer-absent','arm':arm,'registration':binding,'installation':'/declared-fixture-installation','configuration':None}
    result=missing_role.probe(request)
    assert calls[0]==descriptor and len(calls)==2
    assert calls[1]!=calls[0] and calls[1]['model_roles' if name=='qmd' else 'tools']
    assert result['status']==('NOT_AFFECTED_UNUSED_ROLE' if arm=='qmd.search' else 'EXPECTED_MISSING')


def test_unrelated_broken_asset_cannot_masquerade_as_deliberate_missing_role(tmp_path,monkeypatch):
    root=tmp_path.resolve();owner=root/'owned';marker(owner,'qmd');plan,_,_=fake_plan(root)
    plan.value['engines']['qmd']={'model_roles':{'embed':{'resource':{'path':'/fixture'}}}}
    binding=registered.registration(owner,plan)['qmd']
    def broken(*args,**kwargs):raise EvaluationError('unrelated actual tool SHA drift')
    monkeypatch.setattr(missing_role,'engine_resources',broken)
    with pytest.raises(EvaluationError,match='unrelated'):
        missing_role.probe({'schema':1,'owner':str(owner),'scenario':'qmd-embedding-absent','arm':'qmd.query','registration':binding,'installation':'/fixture','configuration':None})


@pytest.mark.parametrize('flag',['--fixture','--command','--shell','--holdout','--skip-resource','--retry-failures'])
def test_public_cli_has_no_fixture_command_skip_or_retry_escape(flag):
    import importlib.util
    entry=Path(compiler.__file__).resolve().parents[2]/'capture_comparison_measurements.py'
    spec=importlib.util.spec_from_file_location('fixture_measurement_entry',entry);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with pytest.raises(SystemExit) as exc:module.main(['capture',flag])
    assert exc.value.code==2


@pytest.mark.parametrize('kind',['source-overlap','baseline-overlap','engine-overlap','symlink-alias'])
def test_capture_output_denied_before_any_directory_or_registration(tmp_path,monkeypatch,kind):
    from search_comparison.measurements import run
    root=tmp_path.resolve(); parent=root/'parent';parent.mkdir()
    protected=parent/'new-output'/'protected';protected.mkdir(parents=True)
    plan,packet,baseline=fake_plan(root)
    plan.bundle.root=root/'bundle'
    plan.path=protected/'plan.json';plan.path.write_text('{}')
    plan.projection={'reference':{'path':str(root/'export.json'),'sha256':'a'*64}}
    pref={'path':str(root/'packet.json'),'sha256':'a'*64};Path(pref['path']).write_text('{}')
    bref={'path':str(root/'base/report.json'),'sha256':'b'*64};Path(bref['path']).parent.mkdir();Path(bref['path']).write_text('{}')
    if kind=='source-overlap':plan.views={'native':{'corpus':str(protected)}}
    elif kind=='baseline-overlap':bref={'path':str(protected/'report.json'),'sha256':sha256_file(plan.path)};Path(bref['path']).write_text('{}')
    elif kind=='engine-overlap':plan.engines={'goldmine.fast':{'installation':str(protected)}}
    else:
        (parent/'alias').symlink_to(root,target_is_directory=True)
    # Correct refs for the boundary-only fixture; no actual plan loading.
    pref['sha256']=sha256_file(Path(pref['path']));bref['sha256']=sha256_file(Path(bref['path']))
    monkeypatch.setattr(run,'load',lambda *args:(plan,packet,baseline))
    monkeypatch.setattr(run,'_execute',lambda *args,**kwargs:pytest.fail('protected output reached execution'))
    output=protected/'new' if kind!='symlink-alias' else parent/'alias/new'
    before={str(p) for p in root.rglob('*')}
    with pytest.raises(EvaluationError):run.capture(pref,bref,output,parent)
    assert {str(p) for p in root.rglob('*')}==before


def test_final_derived_root_bytes_cannot_avoid_finite_administration_budget():
    report={'artifacts':{'registered-assets/qmd.json':{'bytes':storage.ROOT_ADMIN_BYTES,'allocated_bytes':storage.ROOT_ADMIN_BYTES}},'status':'CAPTURED_REQUIRED_MEASUREMENTS','full_comparison_status':'CAPTURED_REQUIRED_DEV_COMPARISON','resource_status':'PASS'}
    actual=storage.root_accounting(report)
    assert actual['root_administration']['status']=='FAIL' and actual['resource_status']=='FAIL'
    assert actual['full_comparison_status']=='MISSING'
