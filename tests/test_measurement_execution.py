"""Pure schedule semantics and a complete MISSING matrix; no real engines."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from answer_eval.protocol import EvaluationError, sha256_file
from search_comparison.measurements import performance, slots, storage
from search_comparison.measurements.run import _execute
from search_comparison.measurements.selection import ARMS, VIEWS
from measurement_executor_fixtures import fake_plan, fake_event


class QueryFixture:
    """Declared controlled adapter, never accepted by the public real entry."""
    def __init__(self,root,*,mutation=False,fail=False,timeout=False,cache=None):
        self.owner=root; self.replay=None;self.kind='goldmine';self.events=[]
        self.commands=SimpleNamespace(blocked=False,interrupted=False,context={},env={})
        self.mutation=mutation;self.fail=fail;self.timeout=timeout;self.cache=cache;self.operations=[]
    def state(self,op,args,*,binding=None):
        self.operations.append((op,args,binding))
        stable='d'*64 if self.mutation and binding and binding.get('edge')=='after' else 'a'*64
        return {'stable_sha256':stable,'cache':self.cache}
    def call(self,label,argv,*,phase='audit',binding=None):
        self.operations.append(('query',argv,binding))
        self.events.append(fake_event(label,code=124 if self.fail else 0,timed_out=self.timeout))
        if self.fail:raise EvaluationError('controlled failure')
        return json.dumps([{'rank':1,'score':.5,'path':str(self.owner/'source.txt'),'excerpt':'synthetic body'}]).encode()


def runtime(root):
    state={'state':'states/cold','values':{'python':'/synthetic/python','goldmine_db':str(root/'states/cold/index.sqlite'),'goldmine_config':str(root/'cfg')},'env':{},'configuration':{},'view':{'corpus':str(root)},'baseline_witness':{'stable_sha256':'a'*64}}
    return {'error':None,'snapshot':{'declared_synthetic':True},'prepared':{},'states':{'cold':deepcopy(state),'warm':deepcopy(state)}}


def slot(*,warm=False,prime=None):
    return {'ordinal':1 if prime is None else 2,'id':'synthetic-1','view':'common','arm':'goldmine.fast','role':'measured','regime':'warm_cli' if warm else 'cold_cli','restore_pre_query_snapshot':not warm,'prime_ordinal':prime}


def view(root):return {'corpus':str(root),'documents':[{'source_path':'fixture/source.txt','view_path':'source.txt'}]}


def test_actual_query_parse_clocks_and_restore_are_separate(tmp_path):
    root=tmp_path.resolve(); ledger=QueryFixture(root); r=runtime(root); s=slot()
    row=performance.attempt(ledger,r,s,{'q':'--literal $() `not shell`'},view(root),[])
    assert row['status']=='OK' and row['wall_ms']==12.5
    assert [x[0] for x in ledger.operations]==['index_clone','index_witness','query','index_witness']
    argv=next(x[1] for x in ledger.operations if x[0]=='query')
    assert argv[-2:]==['--','--literal $() `not shell`'] and '--no-serve' in argv
    assert row['command']==ledger.events[0]['command']


def test_cold_cache_nonempty_rejects_before_query(tmp_path):
    root=tmp_path.resolve(); ledger=QueryFixture(root,cache=[{'hash':'x'}]); row=performance.attempt(ledger,runtime(root),slot(),{'q':'synthetic'},view(root),[])
    assert row['status']=='MISSING' and not any(x[0]=='query' for x in ledger.operations)


def test_failed_prime_does_not_become_warm_success(tmp_path):
    root=tmp_path.resolve(); ledger=QueryFixture(root)
    row=performance.attempt(ledger,runtime(root),slot(warm=True,prime=1),{'q':'synthetic'},view(root),[{'status':'FAIL'}])
    assert row['status']=='MISSING' and 'prime failed' in row['error']
    assert not any(x[0]=='query' for x in ledger.operations)


def test_content_mutation_fails_current_and_blocks_later_cell_slots(tmp_path):
    root=tmp_path.resolve(); ledger=QueryFixture(root,mutation=True); r=runtime(root)
    first=performance.attempt(ledger,r,slot(),{'q':'synthetic'},view(root),[])
    assert first['status']=='FAIL' and r['error']
    count=len(ledger.operations)
    second=performance.attempt(ledger,r,slot(),{'q':'synthetic'},view(root),[])
    assert second['status']=='MISSING' and len(ledger.operations)==count


def test_failed_timed_out_attempt_keeps_duration_and_raw_reference(tmp_path):
    root=tmp_path.resolve();ledger=QueryFixture(root,fail=True,timeout=True)
    row=performance.attempt(ledger,runtime(root),slot(),{'q':'synthetic'},view(root),[])
    assert row['status']=='TIMEOUT' and row['wall_ms']==12.5 and row['command'] is not None


def test_parser_failure_keeps_real_attempt_not_missing_placeholder(tmp_path):
    root=tmp_path.resolve();ledger=QueryFixture(root)
    original=ledger.call
    def malformed(*args,**kwargs):original(*args,**kwargs);return b'{'
    ledger.call=malformed
    row=performance.attempt(ledger,runtime(root),slot(),{'q':'synthetic'},view(root),[])
    assert row['status']=='PARSE_ERROR' and row['wall_ms']==12.5


def test_complete18_by1728_missing_matrix_replays_without_any_children(tmp_path,monkeypatch):
    root=tmp_path.resolve()/'capture'; root.mkdir()
    plan,packet,baseline=fake_plan(root)
    ref={'path':str(tmp_path.resolve()/'declared-unused-packet.json'),'sha256':'a'*64}
    baseref={'path':str(tmp_path.resolve()/'declared-unused-base.json'),'sha256':'b'*64}
    import search_comparison.engine_capture.commands as commands
    monkeypatch.setattr(commands,'capture_command',lambda *a,**k:pytest.fail('unavailable matrix must not start a child'))
    report=_execute(root,ref,baseref,plan,packet,baseline)
    assert report['status']=='MISSING' and report['full_comparison_status']=='MISSING'
    assert len(report['systems'])==14 and report['slots']['rows']==1344
    observed=slots.observations(report['slots'],packet['schedule'],root)
    assert all(r['status']=='MISSING' and r['command'] is None for r in observed)
    assert sum(r['n_fixed'] for r in report['timing']['cells'])==840
    for pair,cellref in zip([(v,a) for v in VIEWS for a in ARMS],report['systems']):
        cell=storage.load_cell(cellref,root/'cells'/pair[0]/pair[1])
        assert len(cell['updates']['operations'])==19 and cell['commands']==[]
    assert _execute(root,ref,baseref,plan,packet,baseline,saved=report)==report
    cellref=report['systems'][0]; first=Path(cellref['path']).parent
    (first/'unregistered.bin').write_bytes(b'extra')
    with pytest.raises(EvaluationError,match='extra artifacts'):storage.load_cell(cellref,first)


def test_query_result_shard_preserves_complete_payload_and_ref(tmp_path):
    root=tmp_path.resolve(); cell=SimpleNamespace(owner=root,replay=None)
    row={'ordinal':1,'result':{'raw':'fixture'*1000}}
    observed=storage.result(cell,{'ordinal':1},row)
    assert observed['result']['path']==str(root/'results/00001.json')
    cell.replay={}
    assert storage.result(cell,{'ordinal':1},row)==observed
    (root/'results/00001.json').write_text('{}')
    with pytest.raises(EvaluationError):storage.result(cell,{'ordinal':1},row)


def test_warm_cache_must_equal_immediate_prime_post_state(tmp_path):
    root=tmp_path.resolve();ledger=QueryFixture(root);r=runtime(root)
    observations=[{'status':'OK','post_index':{'stable_sha256':'a'*64,'cache':[{'unexpected':'intervening cache mutation'}]}}]
    row=performance.attempt(ledger,r,slot(warm=True,prime=1),{'q':'synthetic'},view(root),observations)
    assert row['status']=='MISSING' and 'cache differs' in row['error']
    assert not any(op[0]=='query' for op in ledger.operations) and r['error']


def test_valid_same_query_prime_preserves_cache_and_measures_warm(tmp_path):
    root=tmp_path.resolve();ledger=QueryFixture(root);r=runtime(root)
    prime={'status':'OK','post_index':{'stable_sha256':'a'*64,'cache':None}}
    row=performance.attempt(ledger,r,slot(warm=True,prime=1),{'q':'synthetic'},view(root),[prime])
    assert row['status']=='OK'
    assert [op[0] for op in ledger.operations]==['index_witness','query','index_witness']


def test_first_prepare_invalid_available_phase_does_not_abort_fixed_denominators():
    from search_comparison.measurements.preparation import first_preparation
    system={'resource_observations':[{'label':'goldmine.ingest','command':None,'disposition':'resource_validation_error','error':'missing raw publication'}], 'prepared_snapshot':None}
    result=first_preparation(system,'goldmine')
    assert result['status']=='MISSING' and result['events'][0]['command'] is None
    assert result['events'][0]['wall_ms'] is None and result['phase_wall_ms_sum'] is None


def test_final_source_failure_stays_complete_missing_report_and_replays(tmp_path,monkeypatch):
    root=tmp_path.resolve()/'capture';root.mkdir();plan,packet,baseline=fake_plan(root)
    def source_changed():raise EvaluationError('controlled immutable source drift')
    plan.bundle.verify=source_changed
    ref={'path':str(tmp_path.resolve()/'declared-unused.json'),'sha256':'b'*64}
    report=_execute(root,ref,ref,plan,packet,baseline)
    assert report['final_integrity']['status']=='MISSING' and report['slots']['rows']==1344
    assert len(report['systems'])==14 and report['status']=='MISSING'
    assert all(r['complete_service_latency'] is None for r in report['timing']['cells'])
    assert _execute(root,ref,ref,plan,packet,baseline,saved=report)==report


@pytest.mark.native
def test_actual_controlled_cli_cold_prime_warm_and_replay_authenticate_argv(tmp_path,monkeypatch):
    """Actual stdlib fixture process; fixture-policy MISSING, never Goldmine/model evidence."""
    import sys
    from search_comparison.measurements.ledger import Ledger
    from search_comparison.measurements.filesystem import snapshot_ref
    from measurement_executor_fixtures import marker, source, gm_database
    root=tmp_path.resolve(); owner=root/'owned';marker(owner)
    row=source(root/'immutable'); prepared=root/'closed-prepared';prepared.mkdir()
    gm_database(prepared/'index.sqlite',root/'immutable',[row])
    package=root/'declared-fixture-package/goldmine';package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    (package/'__main__.py').write_text('import json,sqlite3,sys\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index("--db")+1])\ndb=sqlite3.connect(p.as_uri()+"?mode=ro",uri=True)\npath=db.execute("SELECT path FROM files").fetchone()[0]\ndb.close()\nprint(json.dumps([{"rank":1,"score":0.5,"path":path,"excerpt":"declared synthetic CLI fixture; no engine/model"}]))\n')
    env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1','PYTHONPATH':str(package.parent)}
    ctx={'registered_at':'2026-01-01T00:00:00+00:00','declared_fixture_package':{'path':str(package/'__main__.py'),'sha256':sha256_file(package/'__main__.py')}}
    view_value={'corpus':str(root/'immutable'),'documents':[{k:row[k] for k in ('source_path','view_path','sha256')}]}
    def state(name):
        return {'state':'states/'+name,'values':{'python':str(Path(sys.executable).absolute()),'goldmine_db':str(owner/'states'/name/'index.sqlite'),'goldmine_config':str(owner/'unused-fixture.toml')},'view':view_value,'env':env,'configuration':{'declared_fixture':True}}
    cold, warm=state('cold'),state('warm');snap=snapshot_ref(prepared)
    def run(ledger,*,changed_query=False):
        warm_witness=ledger.state('index_clone',{'index':warm['values']['goldmine_db'],'source':snap,'reset_cold':False})
        r={'error':None,'snapshot':snap,'prepared':{},'states':{'cold':deepcopy(cold),'warm':deepcopy(warm)}}
        for s in r['states'].values():s['baseline_witness']=warm_witness
        s1=slot();s2={**slot(warm=True),'ordinal':2,'role':'cache_prime'};s3={**slot(warm=True,prime=2),'ordinal':3}
        observations=[]
        for s in (s1,s2,s3):
            observations.append(performance.attempt(ledger,r,s,{'q':'altered literal' if changed_query else 'synthetic $() 中文'},view_value,observations))
        return observations
    ledger=Ledger(owner,env,ctx,'goldmine',fixture=True)
    observed=run(ledger);saved=ledger.finish()
    assert [r['status'] for r in observed]==['OK','OK','OK']
    assert all(r['clock']['wall_ms']>0 and r['resource_status']=='MISSING' for r in observed)
    assert saved['resource_status']=='MISSING' and len(saved['commands'])>3
    import search_comparison.engine_capture.commands as commands
    monkeypatch.setattr(commands,'capture_command',lambda *a,**k:pytest.fail('disk replay started a child'))
    replay=Ledger(owner,env,ctx,'goldmine',fixture=True,replay=saved)
    assert run(replay)==observed and replay.finish()==saved
    wrong=Ledger(owner,env,ctx,'goldmine',fixture=True,replay=saved)
    changed=run(wrong,changed_query=True)
    assert changed[0]['status']!='OK' and wrong.commands.blocked
