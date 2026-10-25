"""Synthetic complete-writer/cache framing; no real engine/model or420 inputs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from answer_eval.protocol import EvaluationError, sha256_file
from search_comparison.measurements import filesystem, performance, selection, slots, storage
from measurement_executor_fixtures import qmd_database, requests


def cell(owner, slot):
    path=owner/'cells'/slot['view']/slot['arm'];path.mkdir(parents=True,exist_ok=True)
    return SimpleNamespace(owner=path,replay=None)


def small(owner):
    owner.mkdir()
    slot={'ordinal':1,'id':'declared-synthetic','view':'native','arm':'qmd.query','role':'cache_prime',
          'regime':'warm_cli','restore_pre_query_snapshot':False,'prime_ordinal':None}
    row=performance.missing(slot,'declared mechanical writer; no measured engine/query')
    row.update(pre_index={'stable_sha256':'a'*64,'cache':[{'hash':'b'*64,'result':'0.5','created_at':'2026-01-02T00:00:00.000Z'}]},
               post_index={'stable_sha256':'a'*64,'cache':[{'hash':'c'*64,'result':'0.5','created_at':'2026-01-02T00:00:00.000Z'}]},
               clock={'declared_synthetic':True,'wall_ms':12.5,'resource_execution_ms':2.5,'cleanup_ms':3.5})
    ledger=cell(owner,slot);actual,entry=slots.retain(ledger,slot,row)
    assert actual==row
    return ledger,slot,row,entry


def rewrite_index(owner, rows):
    path=owner/'slots.jsonl'
    path.write_text(''.join(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n' for r in rows))
    return {'format':slots.FORMAT,'path':str(path),'sha256':sha256_file(path),'rows':len(rows)}


def test_actual1728_writer_complete1000row_cache_roundtrip_and_final_cell_census(tmp_path):
    owner=tmp_path.resolve()/'complete-cache-writer';owner.mkdir()
    schedule=selection.schedule(selection.select_requests(requests()))
    observations,entries,states=[],[],{}
    warmed=0
    for slot in schedule:
        row=performance.missing(slot,'declared synthetic cache-shape lower bound, not engine measurements')
        row['clock']={'declared_synthetic':True,'wall_ms':12.5,'execution_ms':5.5,'cleanup_ms':2.5,'publication_ms':1.5}
        ledger=cell(owner,slot)
        if slot['arm']=='qmd.query' and slot['regime']!='cold_cli':
            warmed+=1
            if slot['view'] not in states:
                path=ledger.owner/'states/warm/xdg/cache/qmd/comparison.sqlite';qmd_database(path)
                states[slot['view']]={'path':path,'seen':set()}
            state=states[slot['view']]
            row['pre_index']=filesystem.index_witness(state['path'],'qmd')
            if slot['id'] not in state['seen']:
                state['seen'].add(slot['id'])
                with sqlite3.connect(state['path']) as db:
                    for n in range(40):
                        key=hashlib.sha256((slot['id']+'/'+str(n)).encode()).hexdigest()
                        db.execute('INSERT OR REPLACE INTO llm_cache VALUES(?,?,?)',(key,'0.5','2026-01-02T00:00:00.000Z'))
                    db.execute('DELETE FROM llm_cache WHERE hash NOT IN (SELECT hash FROM llm_cache ORDER BY hash LIMIT 1000)')
            row['post_index']=filesystem.index_witness(state['path'],'qmd')
        row=storage.result(ledger,slot,row)
        retained,entry=slots.retain(ledger,slot,row)
        assert retained==row
        observations.append(row);entries.append(entry)
    # Exercise the exact old writer shape as a control, retaining all its bytes.
    legacy=storage.lines(tmp_path.resolve()/'legacy-inline.jsonl',observations)
    old=Path(legacy['path']);old_report={'artifacts':{'slots.jsonl':{'bytes':old.stat().st_size,'allocated_bytes':old.stat().st_blocks*512}},
                                       'status':'MISSING','full_comparison_status':'MISSING','resource_status':'MISSING'}
    assert old.stat().st_size>storage.ROOT_ADMIN_BYTES
    assert storage.root_accounting(old_report)['root_administration']['status']=='FAIL'
    rootref=slots.index(owner,schedule,entries)
    assert rootref['rows']==1344 and warmed==126  # qmd.query warm+prime slots: 2 views x 63, arm-set invariant
    assert slots.observations(rootref,schedule,owner)==observations
    assert all(len(filesystem.index_witness(s['path'],'qmd')['cache'])==1000 for s in states.values())
    index=Path(rootref['path']);new_report={'artifacts':{'slots.jsonl':{'bytes':index.stat().st_size,'allocated_bytes':index.stat().st_blocks*512}},
                                          'status':'MISSING','full_comparison_status':'MISSING','resource_status':'MISSING'}
    assert storage.root_accounting(new_report)['root_administration']['status']=='PASS'
    assert index.stat().st_size<1024*1024
    assert all('pre_index' not in r and 'clock' not in r for r in storage.read_lines({k:rootref[k] for k in ('path','sha256','rows')},1344))
    for view in selection.VIEWS:
        for arm in selection.ARMS:
            ledger=SimpleNamespace(owner=owner/'cells'/view/arm,replay=None)
            value={'updates':{'status':'MISSING','stages':{}},'dependency':{'status':'MISSING','stage':None},
                   'commands':[],'resource_observations':[],'events':[],'requests':[]}
            ref=storage.seal_cell(ledger,value)
            assert storage.load_cell(ref,ledger.owner)
            complete=storage.disk(ledger.owner)
            slot_paths=list((ledger.owner/'slots').glob('*.json'))
            assert len(slot_paths)==96
            assert complete['logical_bytes']>=sum(p.stat().st_size for p in slot_paths)
            assert complete['allocated_bytes']>=sum(p.stat().st_blocks*512 for p in slot_paths)
            assert complete['logical_bytes']<4*1024**3
    assert sum(storage.disk(owner/'cells'/v/a)['logical_bytes'] for v in selection.VIEWS for a in selection.ARMS)>old.stat().st_size


@pytest.mark.parametrize('mutation',['drop','extra','swap','duplicate','other-id','other-arm','other-view','bool-ordinal','unknown-field'])
def test_root_ref_registry_rejects_changed_fixed_denominator_or_order(tmp_path,mutation):
    owner=tmp_path.resolve()/'owned';ledger,slot,row,entry=small(owner)
    second={**slot,'ordinal':2,'id':'declared-synthetic-two'}
    _,entry2=slots.retain(ledger,second,{**row,'ordinal':2,'id':second['id']})
    schedule=[slot,second];values=[deepcopy(entry),deepcopy(entry2)]
    if mutation=='drop':values.pop()
    elif mutation=='extra':values.append(deepcopy(entry2))
    elif mutation=='swap':values.reverse()
    elif mutation=='duplicate':values[1]=deepcopy(entry)
    elif mutation=='other-id':values[0]['id']='other'
    elif mutation=='other-arm':values[0]['arm']='qmd.search'
    elif mutation=='other-view':values[0]['view']='common'
    elif mutation=='bool-ordinal':values[0]['ordinal']=True
    else:values[0]['clock']={'invented':True}
    ref=rewrite_index(owner,values)
    with pytest.raises(EvaluationError):slots.entries(ref,schedule,owner)


@pytest.mark.parametrize('mutation',['byte-tamper','delete','size','bool-size','outside','alias','schedule','clock','cache','duplicate-key','non-object','unknown-format'])
def test_full_slot_and_every_semantic_payload_are_authenticated_before_replay(tmp_path,mutation):
    owner=tmp_path.resolve()/'owned';ledger,slot,row,entry=small(owner)
    ref=slots.index(owner,[slot],[entry]);path=Path(entry['observation']['path'])
    values=[deepcopy(entry)]
    if mutation=='byte-tamper':path.write_bytes(path.read_bytes()+b' ')
    elif mutation=='delete':path.unlink()
    elif mutation=='size':values[0]['observation']['bytes']+=1
    elif mutation=='bool-size':values[0]['observation']['bytes']=True
    elif mutation=='outside':values[0]['observation']['path']=str(tmp_path.resolve()/'outside.json')
    elif mutation=='alias':
        external=tmp_path.resolve()/'alias-target.json';external.write_bytes(path.read_bytes());path.unlink();path.symlink_to(external)
    else:
        value=json.loads(path.read_text())
        if mutation=='schedule':value['slot']['prime_ordinal']=999
        elif mutation=='clock':value['observation']['clock']['cleanup_ms']+=1
        elif mutation=='cache':value['observation']['post_index']['cache'][0]['result']='0.8'
        elif mutation=='unknown-format':value['format']='comparison.measurement-slot-shards/v3'
        if mutation=='duplicate-key':data=b'{"schema":2,"schema":2}\n'
        elif mutation=='non-object':data=b'[]\n'
        else:data=json.dumps(value,separators=(',',':')).encode()+b'\n'
        path.write_bytes(data)
        if mutation in ('clock','cache'):
            # Even a maliciously rehashed derived shard cannot change raw replay.
            with pytest.raises(EvaluationError):
                ledger.replay={};slots.retain(ledger,slot,row)
            return
        values[0]['observation'].update(sha256=sha256_file(path),bytes=path.stat().st_size)
    ref=rewrite_index(owner,values)
    with pytest.raises(EvaluationError):slots.observations(ref,[slot],owner)


def test_slot_record_limit_remains_missing_without_prefix_or_fake_hash(tmp_path,monkeypatch):
    owner=tmp_path.resolve()/'owned';ledger,slot,row,entry=small(owner)
    Path(entry['observation']['path']).unlink()
    monkeypatch.setattr(storage,'RECORD_BYTES',1500)
    row['post_index']['cache'].append({'full-retained-raw-authority':'x'*4096})
    actual,entry=slots.retain(ledger,slot,row)
    assert actual['status']=='MISSING' and actual['storage_error']
    assert actual['post_index']==row['post_index'] and actual['clock']==row['clock']
    marker=entry['observation']
    assert marker['status']=='MISSING_DERIVED_RECORD_LIMIT' and 'sha256' not in marker
    assert not Path(marker['path']).exists()
    ref=slots.index(owner,[slot],[entry])
    with pytest.raises(EvaluationError,match='MISSING'):slots.observations(ref,[slot],owner)


def test_derived_shards_alone_can_fail_unchanged_final4gib_cell_allowance(tmp_path):
    owner=tmp_path.resolve()/'owned';ledger,slot,row,entry=small(owner)
    # Sparse owned fixture tests actual logical accounting, not a changed cap.
    path=ledger.owner/'declared-sparse-raw-artifact'
    with path.open('xb') as stream:stream.truncate(4*1024**3)
    # Avoid inventory() hashing a4GiB sparse fixture: disk census receives an
    # exact declared metadata fixture, while real slot allocation is measured.
    original_inventory=storage.inventory
    try:
        storage.inventory=lambda root:{'declared-sparse-raw-artifact':{'bytes':path.stat().st_size},
                                       'slots/00001.json':{'bytes':Path(entry['observation']['path']).stat().st_size}}
        result=storage.disk(ledger.owner)
    finally:storage.inventory=original_inventory
    assert result['logical_bytes']>4*1024**3
    assert result['allocated_bytes']>=Path(entry['observation']['path']).stat().st_blocks*512
