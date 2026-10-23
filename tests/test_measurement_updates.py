"""Typed operations against tiny real SQLite, with explicitly synthetic ingestion."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from answer_eval.protocol import EvaluationError, sha256_file
from search_comparison.measurements import filesystem as fs, update_capture, audits, storage
from search_comparison.measurements.updates import compare
from measurement_executor_fixtures import fake_plan, marker, gm_database


class SyntheticIngest:
    """Test-only model-free producer, never reachable through the public CLI."""
    def __init__(self,owner,*,stale_delete=False,fail_update=False):
        self.owner=owner;self.kind='goldmine';self.replay=None;self.events=[]
        self.commands=SimpleNamespace(blocked=False,interrupted=False,context={},env={})
        self.rows={};self.stale_delete=stale_delete;self.fail_update=fail_update;self.known={}
    def state(self,op,args,*,binding=None):
        self.events.append({'operation':op})
        value=fs.execute({'schema':1,'suite_id':fs.SUITE,'owner':str(self.owner),'operation':op,'kind':self.kind,'arguments':args})
        if op in {'source_write','source_remove','source_witness'}:
            self.rows[args['corpus']]=deepcopy(args['entries'])
            self.known.update({r['source_path']:deepcopy(r) for r in args['entries']+args['changed']})
        return value
    def ingest(self,values):
        self.events.append({'operation':'synthetic-ingest','corpus':values['selected_corpus']})
        rows=deepcopy(self.rows[values['selected_corpus']]);db=Path(values['goldmine_db'])
        if self.fail_update and len(rows)==122 and 'update/incremental' in str(db):
            raise EvaluationError('controlled ordinary-update failure; no hidden retry')
        if self.stale_delete and len(rows)==121:
            rows=list(self.known.values())
        if db.exists():db.unlink()
        gm_database(db,Path(values['selected_corpus']),rows)
        import sqlite3
        with sqlite3.connect(db) as conn:
            for n,row in enumerate(rows,1):
                # Actual current working bytes, or retained original bytes only
                # for the deliberately bad deletion simulator.
                path=Path(values['selected_corpus'])/row['view_path']
                content=path.read_text() if path.exists() else Path(row['input']).read_text()
                conn.execute('UPDATE fts SET body=? WHERE rowid=?',(content,n))
                conn.execute('UPDATE segments SET vec=? WHERE id=?',(hashlib.sha256(content.encode()).digest(),n))
    def call(self,label,argv,*,phase='prepare',binding=None):
        if '--db' in argv:
            path=argv[argv.index('--db')+1]
            values=next(s['values'] for s in self.states.values() if s['values']['goldmine_db']==path)
            self.ingest(values)
        return b'{}'


def fixture(tmp_path,monkeypatch,**kwargs):
    root=tmp_path.resolve(); owner=root/'owned'; marker(owner)
    plan,packet,_=fake_plan(root)
    documents=[]
    for n,(source,data) in enumerate(plan.bundle.sources.items()):
        path=root/'immutable'/('view-'+str(n)+'.md');path.parent.mkdir(exist_ok=True);path.write_text('declared source '+str(n)+' 中文\n')
        documents.append({'source_path':source,'view_path':path.name,'sha256':sha256_file(path)})
    view={'name':'common','corpus':str(root/'immutable'),'documents':documents}
    ledger=SyntheticIngest(owner,**kwargs);ledger.states={}
    def state_for(cell,plan,view,arm,state):
        if state not in ledger.states:
            ledger.states[state]={'state':state,'values':{'python':'/declared-synthetic-never-executed-python','goldmine_db':str(owner/state/'index.sqlite'),'goldmine_config':str(owner/state/'fixture.toml'),
                'embedding_sha256':'b'*64,'selected_corpus':view['corpus']},'view':view,'env':{},'configuration':{}}
        ledger.states[state]['view']=view
        return ledger.states[state]
    def prepare(engine,arm,commands,values,view,registration):ledger.ingest(values)
    monkeypatch.setattr(update_capture,'for_cell',state_for)
    monkeypatch.setattr(update_capture,'_prepare',prepare)
    return ledger,plan,packet,view


def test_all19_typed_operations_full_membership_body_vectors_and_same_corpus(tmp_path,monkeypatch):
    ledger,plan,packet,view=fixture(tmp_path,monkeypatch)
    result=update_capture.execute(ledger,plan,view,'goldmine.fast',packet['source_cohorts'])
    assert result['status']=='CAPTURED' and len(result['operations'])==19
    assert set(result['stages'])=={'base112','incremental122','fresh122','lifecycle122','removed121','replaced122','replacement_fresh122'}
    for name,count in [('base112',112),('incremental122',122),('removed121',121),('replaced122',122)]:
        assert len(result['stages'][name]['logical']['sources'])==count
        assert len(result['stages'][name]['source_witness']['sources'])==count
    for left,right in [('incremental122','fresh122'),('replaced122','replacement_fresh122')]:
        assert result['stages'][left]['source_witness']==result['stages'][right]['source_witness']
        assert compare(result['stages'][left]['logical'],result['stages'][right]['logical'])['status']=='LOGICAL_EQUAL'
    changed=compare(result['stages']['lifecycle122']['logical'],result['stages']['replaced122']['logical'])
    assert changed['changes']['body']==[packet['source_cohorts']['replace_target']]
    assert changed['changes']['vectors']==[packet['source_cohorts']['replace_target']]
    assert all(Path(s['snapshot']['path']).is_dir() for s in result['stages'].values())


def test_actual_stale_deletion_is_not_removed_by_expected_denominator(tmp_path,monkeypatch):
    ledger,plan,packet,view=fixture(tmp_path,monkeypatch,stale_delete=True)
    result=update_capture.execute(ledger,plan,view,'goldmine.fast',packet['source_cohorts'])
    assert result['status']=='FAIL' and len(result['operations'])==19
    removed=result['stages']['removed121']
    assert len(removed['logical']['sources'])==122
    op=next(r for r in result['operations'] if r['operation'].get('stage')=='removed121' and r['operation']['kind']=='readonly_audit_and_closed_snapshot')
    assert op['result']['stale_active']==[packet['source_cohorts']['remove']]
    assert op['status']=='FAIL'


def test_failed_incremental_retains_all_operations_and_independent_fresh_evidence(tmp_path,monkeypatch):
    ledger,plan,packet,view=fixture(tmp_path,monkeypatch,fail_update=True)
    result=update_capture.execute(ledger,plan,view,'goldmine.fast',packet['source_cohorts'])
    assert result['status']=='FAIL' and len(result['operations'])==19
    assert any(r['status']=='MISSING' and 'controlled ordinary-update failure' in r['error'] for r in result['operations'])
    assert len(result['stages']['incremental122']['logical']['sources'])==112
    assert len(result['stages']['fresh122']['logical']['sources'])==122
    assert result['comparisons']['incremental122']['counts']['added']==10


def test_normalizer_failure_retains_closed_actual_index(tmp_path,monkeypatch):
    ledger,plan,packet,view=fixture(tmp_path,monkeypatch)
    original=audits.readback
    def reject(*args,**kwargs):raise EvaluationError('controlled missing normalization proof')
    monkeypatch.setattr(audits,'readback',reject)
    result=update_capture.execute(ledger,plan,view,'goldmine.fast',packet['source_cohorts'])
    assert result['status']=='MISSING'
    assert len(result['stages'])==7 and all(v['logical'] is None and Path(v['snapshot']['path']).is_dir() for v in result['stages'].values())
    assert all(v['error']=='controlled missing normalization proof' for v in result['stages'].values())
