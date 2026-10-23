"""Tiny real owned files/SQLite plus explicit mechanical-only raw subprocesses."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from answer_eval.protocol import EvaluationError, sha256_file, canonical_sha256
from search_comparison.measurements import filesystem as fs, database, registered, storage
from search_comparison.measurements.ledger import Ledger
from search_comparison.measurements.updates import goldmine, compare
from measurement_executor_fixtures import marker, source, gm_database, qmd_database, fake_plan


@pytest.fixture
def cell(tmp_path):
    root=tmp_path.resolve(); owner=root/'owned'; marker(owner)
    row=source(root/'immutable'); return owner,row


def request(owner,operation,arguments,kind='goldmine'):
    return {'schema':1,'suite_id':fs.SUITE,'owner':str(owner),'operation':operation,'kind':kind,'arguments':arguments}


def write(owner,row):
    return fs.execute(request(owner,'source_write',{'corpus':str(owner/'update/corpus'),'view':'common','entries':[row],'changed':[row]}))


def test_actual_source_materialization_hashes_and_physical_identity(cell):
    owner,row=cell; first=write(owner,row)
    again=fs.execute(request(owner,'source_witness',{'corpus':str(owner/'update/corpus'),'view':'common','entries':[row],'changed':[]}))
    assert first==again and set(first['sources'])=={row['source_path']}
    assert first['sources'][row['source_path']]['inode']>0
    assert (owner/'update/corpus'/row['view_path']).read_bytes()==Path(row['input']).read_bytes()


@pytest.mark.parametrize('mutation',['outside','alias','unknown-state','missing-owner','wrong-kind','foreign-change','remove-still-expected'])
def test_state_boundary_rejects_before_any_source_change(cell,mutation):
    owner,row=cell; args={'corpus':str(owner/'update/corpus'),'view':'common','entries':[row],'changed':[row]}; op='source_write'; kind='goldmine'
    if mutation=='outside':args['corpus']=str(owner.parent/'outside')
    elif mutation=='alias':
        (owner/'link').symlink_to(owner.parent, target_is_directory=True); args['corpus']=str(owner/'link/update/corpus')
    elif mutation=='unknown-state':args['corpus']=str(owner/'anything')
    elif mutation=='missing-owner':(owner/'cell-ownership.json').unlink()
    elif mutation=='wrong-kind':kind='other'
    elif mutation=='foreign-change':args['changed']=[{**row,'view_path':'unregistered.txt'}]
    else:op='source_remove'
    before={str(p):p.read_bytes() for p in owner.rglob('*') if p.is_file() and not p.is_symlink()}
    with pytest.raises((EvaluationError,FileNotFoundError)):fs.execute(request(owner,op,args,kind))
    assert {str(p):p.read_bytes() for p in owner.rglob('*') if p.is_file() and not p.is_symlink()}==before
    assert not (owner.parent/'outside').exists()


def test_closed_snapshot_does_not_discard_live_wal(cell):
    owner,row=cell; db=owner/'states/cold/index.sqlite'; gm_database(db,owner/'update/corpus',[row])
    Path(str(db)+'-wal').write_bytes(b'not closed')
    with pytest.raises(EvaluationError,match='closed'):fs.execute(request(owner,'index_snapshot',{'index':str(db),'stage':'base112'}))
    assert not (owner/'snapshots/base112').exists()


def test_real_sqlite_clone_snapshot_and_cache_boundaries(cell):
    owner,row=cell; db=owner/'states/warm/index.sqlite'; gm_database(db,owner/'update/corpus',[row])
    result=fs.execute(request(owner,'index_snapshot',{'index':str(db),'stage':'base112'}))
    cold=owner/'states/cold/index.sqlite'
    observed=fs.execute(request(owner,'index_clone',{'index':str(cold),'source':result['snapshot'],'reset_cold':False}))
    assert observed['file_sha256']==sha256_file(db)
    with pytest.raises(EvaluationError):fs.execute(request(owner,'index_clone',{'index':str(db),'source':result['snapshot'],'reset_cold':True}))
    (Path(result['snapshot']['path'])/'index.sqlite').write_bytes(b'tampered')
    with pytest.raises(EvaluationError):fs.verify_snapshot(result['snapshot'])


def test_qmd_real_path_and_only_registered_cache_exclusion(tmp_path):
    owner=tmp_path.resolve()/'owned';marker(owner,'qmd')
    path=owner/'states/warm/xdg/cache/qmd/comparison.sqlite'; qmd_database(path)
    assert fs.index_path(owner,str(path),'qmd')==path
    first=fs.index_witness(path,'qmd')
    with sqlite3.connect(path) as db:db.execute("INSERT INTO llm_cache VALUES('h','cached output','now')")
    second=fs.index_witness(path,'qmd')
    assert first['stable_sha256']==second['stable_sha256'] and first['cache_sha256']!=second['cache_sha256']
    with sqlite3.connect(path) as db:db.execute("UPDATE documents SET body='actual mutation'")
    assert fs.index_witness(path,'qmd')['stable_sha256']!=first['stable_sha256']


def test_full_database_readback_preserves_stale_and_semantic_info(cell):
    owner,row=cell;write(owner,row); path=owner/'update/incremental/index.sqlite'; gm_database(path,owner/'update/corpus',[row])
    raw=database.goldmine(path,owner/'update/corpus',[row],'b'*64)
    assert raw['stable']['sources'][row['source_path']]['parts'][0]['vector']['bytes']==4
    before=goldmine(raw)
    with sqlite3.connect(path) as db:db.execute("UPDATE files SET info=?",('{"exif_reader":"imageio","ocr_ms":2}',))
    after=goldmine(database.goldmine(path,owner/'update/corpus',[row],'b'*64))
    assert compare(before,after)['counts']=={'metadata':1,'body':0,'vectors':0,'capability':0,'added':0,'removed':0}
    # Removing bytes does not erase the actual stale DB row from the audit.
    (owner/'update/corpus'/row['view_path']).unlink()
    assert set(database.goldmine(path,owner/'update/corpus',[row],'b'*64)['stable']['sources'])=={row['source_path']}
    with pytest.raises(EvaluationError):database.goldmine(path,owner/'update/corpus',[],'b'*64)


@pytest.mark.parametrize('mutation',['tamper','delete','omit','unknown-role','path-drift','writable','symlink'])
def test_complete_registered_inventory_is_not_a_summary(cell,mutation):
    owner,row=cell; plan,_,_=fake_plan(owner)
    plan.value['engines']['qmd']={'complete':{'inventory':[{'path':'x','sha256':'a'*64},{'path':'y','sha256':'b'*64}]}}
    bindings=registered.registration(owner,plan); ref=bindings['qmd']['artifact']; path=Path(ref['path'])
    if mutation=='unknown-role':bindings['qmd']['engine']='other'
    elif mutation=='path-drift':bindings['qmd']['artifact']['path']=str(owner/'wrong.json')
    elif mutation=='delete':path.unlink()
    elif mutation=='symlink':
        path.chmod(0o644);other=owner/'copied.json';other.write_bytes(path.read_bytes());path.unlink();path.symlink_to(other)
    elif mutation=='writable':path.chmod(0o644)
    else:
        path.chmod(0o644); value=json.loads(path.read_text())
        if mutation=='omit':value['complete_descriptor']['complete']['inventory'].pop()
        else:value['complete_descriptor']['complete']['inventory'][0]['sha256']='f'*64
        path.write_text(json.dumps(value));path.chmod(0o444)
        bindings['qmd']['artifact']['sha256']=sha256_file(path)
    with pytest.raises((EvaluationError,FileNotFoundError)):registered.descriptor(bindings['qmd'],plan)


@pytest.mark.parametrize('mutation',['drop','extra','bad-type','truncated','duplicate-json-key','tamper'])
def test_bounded_stream_has_exact_rows_and_strict_json(cell,mutation):
    owner,row=cell; path=owner/'stream.jsonl'; ref=storage.lines(path,[{'n':1},{'n':2}])
    if mutation=='drop':ref['rows']=1
    elif mutation=='extra':ref['rows']=3
    elif mutation=='bad-type':ref['rows']=True
    else:
        data=path.read_bytes()
        if mutation=='truncated':path.write_bytes(data[:-1])
        elif mutation=='duplicate-json-key':path.write_bytes(b'{"n":1,"n":2}\n{"n":2}\n')
        else:path.write_bytes(b'{"n":9}\n{"n":2}\n')
        if mutation!='tamper':ref['sha256']=sha256_file(path)
    with pytest.raises(EvaluationError):storage.read_lines(ref,2)


@pytest.mark.native
def test_actual_typed_worker_capture_and_disk_replay_no_children_on_replay(cell,monkeypatch):
    owner,row=cell
    context={'registered_at':'2026-01-01T00:00:00+00:00','implementation':{'fixture':True}}
    env={'PATH':'/usr/bin:/bin','PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1'}
    ledger=Ledger(owner,env,context,'goldmine',fixture=True)
    args={'corpus':str(owner/'update/corpus'),'view':'common','entries':[row],'changed':[row]}
    observed=ledger.state('source_write',args)
    saved=ledger.finish()
    assert len(saved['commands'])==1 and len(saved['resource_observations'])==1
    assert saved['resource_status']=='MISSING'  # fixture observations cannot certify a real run
    import search_comparison.engine_capture.commands as commands
    monkeypatch.setattr(commands,'capture_command',lambda *a,**k:pytest.fail('read-only replay spawned a process'))
    replay=Ledger(owner,env,context,'goldmine',replay=saved,fixture=True)
    assert replay.state('source_write',args)==observed
    assert replay.finish()==saved


def test_query_derived_limit_is_explicit_missing_with_original_raw_ref(cell,monkeypatch):
    owner,_=cell;monkeypatch.setattr(storage,'RECORD_BYTES',128)
    ledger=type('MechanicalCell',(),{'owner':owner,'replay':None})()
    original={'ordinal':1,'id':'synthetic','status':'OK','command':{'path':'/declared-original-raw/manifest.json','sha256':'c'*64},'wall_ms':1.0,'result':{'material':'x'*1000}}
    result=storage.result(ledger,{'ordinal':1},original)
    assert result['status']=='MISSING' and result['command']==original['command']
    assert result['result']['status']=='MISSING_DERIVED_RECORD_LIMIT'
    assert not (owner/'results/00001.json').exists()
    ledger.replay={}
    assert storage.result(ledger,{'ordinal':1},original)==result


def test_stage_derived_limit_propagates_to_cell_and_retains_raw(cell,monkeypatch):
    owner,_=cell;monkeypatch.setattr(storage,'RECORD_BYTES',5000)
    (owner/'raw-stdout.log').write_text('complete original actual tiny-fixture stream '+('x'*10000))
    ledger=type('MechanicalCell',(),{'owner':owner,'replay':None})()
    value={'updates':{'status':'CAPTURED','stages':{'base112':{'derived':'x'*10000}}},'dependency':{'status':'MISSING','stage':None},
           'commands':[],'resource_observations':[],'events':[],'requests':[]}
    ref=storage.seal_cell(ledger,value)
    assert value['updates']['status']=='MISSING' and (owner/'raw-stdout.log').stat().st_size>10000
    loaded=storage.load_cell(ref,owner)
    assert loaded['updates']['stages']['base112']['status']=='MISSING_DERIVED_RECORD_LIMIT'
    assert not (owner/'stage-data/base112.json').exists()


def test_stream_count_limit_does_not_publish_success_prefix(cell):
    owner,_=cell; path=owner/'too-many.jsonl'
    with pytest.raises(EvaluationError):storage.lines(path,[None]*50001)
    assert not path.exists()


@pytest.mark.native
def test_ledger_validation_error_keeps_every_attempt_and_blocks_later_child(cell,monkeypatch):
    owner,_=cell
    context={'registered_at':'2026-01-01T00:00:00+00:00','mechanical_fixture_only':True}
    env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'}
    ledger=Ledger(owner,env,context,'goldmine',fixture=True)
    argv=[str(Path(sys.executable).absolute()),'-I','-S','-B','-c','import time;print("fixture");time.sleep(.06)']
    import search_comparison.engine_capture.resources as resources
    actual=resources.validate;calls=[]
    def corrupt_second(*args,**kwargs):
        calls.append(args)
        if len(calls)==2:raise EvaluationError('controlled publication proof missing')
        return actual(*args,**kwargs)
    monkeypatch.setattr(resources,'validate',corrupt_second)
    ledger.call('mechanical.first',argv)
    with pytest.raises(EvaluationError,match='publication'):ledger.call('mechanical.second',argv)
    with pytest.raises(EvaluationError):ledger.call('mechanical.third-never-launched',argv)
    result=ledger.finish()
    assert len(result['commands'])==2 and len(result['resource_observations'])==2
    assert result['resource_observations'][-1]['disposition']=='resource_validation_error'
    assert result['resource_status']=='MISSING' and ledger.commands.blocked
    assert len(result['events'])==3 and result['events'][-1]['command'] is None


@pytest.mark.parametrize('mutation',['segment','fts','cjk','lookup','missing-derived'])
def test_database_rejects_orphan_or_missing_query_index_rows(cell,mutation):
    owner,row=cell;dbpath=owner/'update/incremental/index.sqlite';gm_database(dbpath,owner/'update/corpus',[row])
    with sqlite3.connect(dbpath) as db:
        if mutation=='segment':db.execute("INSERT INTO segments VALUES(999,999,'text','text',1,1,NULL,'orphan')")
        elif mutation=='fts':db.execute("INSERT INTO fts VALUES(999,'orphan','body',NULL)")
        elif mutation=='cjk':db.execute("INSERT INTO fts_cjk VALUES(999,'orphan')")
        elif mutation=='lookup':db.execute("INSERT INTO lookup_terms VALUES('bad','bad',999,1,'body','raw')")
        else:db.execute('DROP TABLE lookup_terms')
    with pytest.raises(EvaluationError):database.goldmine(dbpath,owner/'update/corpus',[row],'b'*64)


def test_query_index_only_drift_cannot_be_hidden_by_unchanged_original_body(cell):
    from search_comparison.measurements.update_capture import compare_stages
    owner,row=cell;dbpath=owner/'update/incremental/index.sqlite';gm_database(dbpath,owner/'update/corpus',[row])
    with sqlite3.connect(dbpath) as db:db.execute("UPDATE fts SET original='same full original body',body='derived before'")
    def logical():
        raw=database.goldmine(dbpath,owner/'update/corpus',[row],'b'*64);value=goldmine(raw);value['query_index']=raw['stable']['query_index'];return value
    before=logical()
    with sqlite3.connect(dbpath) as db:db.execute("UPDATE fts SET body='derived changed while original unchanged'")
    after=logical()
    assert compare(before,after)['status']=='LOGICAL_EQUAL' # existing pure content adapter scope
    result=compare_stages(before,after)
    assert result['status']=='LOGICAL_DIFFERENCES_OBSERVED' and result['counts']['query_index']==1
    assert result['counts']['body']==result['counts']['vectors']==0


def test_physical_rowids_are_excluded_but_source_association_is_verified(cell):
    from search_comparison.measurements.update_capture import compare_stages
    owner,row=cell;dbpath=owner/'update/incremental/index.sqlite';gm_database(dbpath,owner/'update/corpus',[row])
    def logical():
        raw=database.goldmine(dbpath,owner/'update/corpus',[row],'b'*64);value=goldmine(raw);value['query_index']=raw['stable']['query_index'];return value
    before=logical()
    with sqlite3.connect(dbpath) as db:
        db.execute('UPDATE files SET id=2');db.execute('UPDATE segments SET id=10,file=2')
        db.execute('UPDATE fts SET rowid=10');db.execute('UPDATE fts_cjk SET rowid=10')
        db.execute('UPDATE lookup_terms SET file_id=2,segment_id=10')
    assert compare_stages(before,logical())['status']=='LOGICAL_EQUAL'
