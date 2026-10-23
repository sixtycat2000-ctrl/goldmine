"""Bounded framing mechanics only; synthetic declarations are never quality."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from answer_eval.protocol import EvaluationError,canonical_sha256,sha256_file
from search_comparison.engine_capture import protocol,report_framing as framing,report_streams as streams,report_cells,report_prepared,report_accounting
from search_comparison.source_views.run import write_new
from comparison_report_fixtures import report,original19byte_lower_bound


def wire(owner):return protocol.read(owner/'report.json')


def replace_wire(owner,value):
    (owner/'report.json').write_bytes(streams.encode(value))


def cell_shell(owner,value=None):
    value=value or wire(owner)
    ref=value['cells'][0]['component'];path=Path(ref['path'])
    return value,ref,path,protocol.read(path)


def replace_cell(owner,value,ref,path,shell):
    path.write_bytes(streams.encode(shell));ref.update(bytes=path.stat().st_size,sha256=sha256_file(path));replace_wire(owner,value)


def test_exact_existing19byte_family_exceeds_generic8mib_but_complete18x210_roundtrips(tmp_path):
    root=tmp_path.resolve();legacy=original19byte_lower_bound();path=root/'legacy.json';write_new(path,legacy)
    assert path.stat().st_size==8388927 and protocol.read.__defaults__==(8*1024*1024,)
    with pytest.raises(EvaluationError) as exc:protocol.read(path)
    assert exc.value.status=='MISSING'
    owner=root/'complete';original=report(owner,n=210,max_windows=True)
    observed=framing.publish(owner,original)
    assert (owner/'report.json').stat().st_size<streams.LIMITS['header_bytes']
    assert observed==framing.load(owner/'report.json')
    assert {k:v for k,v in observed.items() if k!='framing'}==original
    assert observed['framing']['publication']['status']=='VERIFIED'
    assert len(observed['systems'])==18 and all(len(s['rows'])==210 for s in observed['systems'])
    for system in observed['systems']:
        first=system['rows'][0]['result']
        assert len(first['raw_rows'])==100 and len(first['native10_raw_rows'])==10 and len(first['file_cut'])==10
        assert first['window']['files']==[f'd{i:03d}.txt' for i in range(10)]
        assert len(system['preparation']['stable']['sources'])==122
        assert system['preparation']['stable']['opaque_unknown_header']=={'must_retain':[1,'中文',{'deeper':True}]}
        assert system['prepared_snapshot']['files'] and system['commands'] and system['resource_observations']
        cell=Path(system['owner']);counts=report_accounting.census(cell)
        saved=next(r for r in observed['framing']['publication']['cells'] if r['view']==system['view'] and r['arm']==system['arm'])
        assert all(saved[k]==counts[k] for k in ('logical_bytes','allocated_bytes','files'))
        assert any(p.name=='rows.jsonl' for p in (cell/'report-data').iterdir())
    assert observed['quality_evidence'] is False and observed['release_eligible'] is False


@pytest.mark.parametrize('mutation',['drop','extra','swap','duplicate','view','arm','bad-state','omit-error','limits','header-source','header-count','bool-schema'])
def test_full_root_registry_denies_mutations_before_materialization(tmp_path,mutation):
    owner=tmp_path.resolve()/'owned';framing.publish(owner,report(owner,n=2,max_windows=False));value=wire(owner)
    if mutation=='drop':value['cells'].pop()
    elif mutation=='extra':value['cells'].append(deepcopy(value['cells'][0]))
    elif mutation=='swap':value['cells'][0],value['cells'][1]=value['cells'][1],value['cells'][0]
    elif mutation=='duplicate':value['cells'][1]=deepcopy(value['cells'][0])
    elif mutation=='view':value['cells'][0]['view']='common'
    elif mutation=='arm':value['cells'][0]['arm']='qmd.search'
    elif mutation=='bad-state':value['cells'][0]['state']='SKIPPED'
    elif mutation=='omit-error':value['cells'][0].pop('error')
    elif mutation=='limits':value['limits']['material_record_bytes']+=1
    elif mutation=='header-source':value['header']['source_count']=True
    elif mutation=='header-count':value['header']['n_fixed_queries']=True
    else:value['schema']=True
    replace_wire(owner,value)
    with pytest.raises(EvaluationError) as exc:framing.load(owner/'report.json')
    assert exc.value.status=='MISSING'


@pytest.mark.parametrize('mutation',['delete','append','wrong-size','bool-size','outside','alias','unknown-derived','raw-mutation','raw-omission','raw-bool-bytes','source-provenance','source-sha','vector-sha','index-snapshot'])
def test_exact_component_and_complete_raw_inventory_denies_missing_or_changed_proof(tmp_path,mutation):
    owner=tmp_path.resolve()/'owned';framing.publish(owner,report(owner,n=2,max_windows=False));value,ref,path,shell=cell_shell(owner)
    rowref=shell['rows'];rows=Path(rowref['path'])
    if mutation=='delete':rows.unlink()
    elif mutation=='append':rows.write_bytes(rows.read_bytes()+b'{}\n')
    elif mutation=='wrong-size':rowref['bytes']+=1
    elif mutation=='bool-size':rowref['bytes']=True
    elif mutation=='outside':rowref['path']=str(tmp_path.resolve()/'outside.jsonl')
    elif mutation=='alias':
        target=tmp_path.resolve()/'external.jsonl';target.write_bytes(rows.read_bytes());rows.unlink();rows.symlink_to(target)
    elif mutation=='unknown-derived':(rows.parent/'unregistered.json').write_text('{}')
    elif mutation=='raw-mutation':(path.parents[1]/'raw-fixture/stdout.log').write_text('changed actual fixture raw')
    elif mutation=='raw-omission':
        raw=Path(shell['raw_inventory']['path']);entries=[json.loads(line) for line in raw.read_text().splitlines()];entries.pop()
        raw.write_bytes(b''.join(streams.encode(r) for r in entries));shell['raw_inventory'].update(bytes=raw.stat().st_size,sha256=sha256_file(raw),rows=len(entries))
    elif mutation=='raw-bool-bytes':
        raw=Path(shell['raw_inventory']['path']);entries=[json.loads(line) for line in raw.read_text().splitlines()]
        next(r for r in entries if r['path'].endswith('/stderr.log'))['file']['bytes']=False
        raw.write_bytes(b''.join(streams.encode(r) for r in entries));shell['raw_inventory'].update(bytes=raw.stat().st_size,sha256=sha256_file(raw))
    elif mutation=='index-snapshot':(path.parents[1]/'prepared-index/fixture.bin').write_bytes(b'altered closed snapshot')
    else:
        source=Path(shell['preparation']['sections'][1]['component']['path']);data=source.read_bytes()
        if mutation=='source-provenance':data=data.replace(b'"semantic":"synthetic"',b'"semantic":"forged"')
        elif mutation=='source-sha':data=data.replace(b'"sha256":"',b'"sha256":"0',1)
        else:data=data.replace(('b'*64).encode(),('e'*64).encode(),1)
        source.write_bytes(data)
    replace_cell(owner,value,ref,path,shell)
    with pytest.raises(EvaluationError) as exc:framing.load(owner/'report.json')
    assert exc.value.status=='MISSING'


@pytest.mark.parametrize('mutation',['drop','extra','swap','duplicate','id','bool-ordinal','duplicate-json','truncated','non-object'])
def test_query_stream_has_exact_fixed210_order_and_strict_json(tmp_path,mutation):
    owner=tmp_path.resolve()/'owned';framing.publish(owner,report(owner,n=2,max_windows=False));value,ref,path,shell=cell_shell(owner)
    rowref=shell['rows'];target=Path(rowref['path']);rows=[json.loads(line) for line in target.read_text().splitlines()]
    if mutation=='drop':rows.pop()
    elif mutation=='extra':rows.append(deepcopy(rows[-1]))
    elif mutation=='swap':rows.reverse()
    elif mutation=='duplicate':rows[1]=deepcopy(rows[0])
    elif mutation=='id':rows[0]['id']='another-id'
    elif mutation=='bool-ordinal':rows[0]['ordinal']=True
    elif mutation=='non-object':rows[0]=[]
    data=b''.join(streams.encode(r) for r in rows)
    if mutation=='duplicate-json':data=data.replace(b'"ordinal":1',b'"ordinal":1,"ordinal":1',1)
    elif mutation=='truncated':data=data[:-1]
    target.write_bytes(data);rowref.update(bytes=len(data),sha256=sha256_file(target),rows=len(rows))
    replace_cell(owner,value,ref,path,shell)
    with pytest.raises(EvaluationError) as exc:framing.load(owner/'report.json')
    assert exc.value.status=='MISSING'


@pytest.mark.parametrize('mutation',['drop','duplicate','swap','unknown','inline'])
def test_prepared_sections_cannot_omit_full_bodies_vectors_or_move_them_to_header(tmp_path,mutation):
    owner=tmp_path.resolve()/'owned';framing.publish(owner,report(owner,n=1,max_windows=False));value,ref,path,shell=cell_shell(owner)
    prep=shell['preparation'];sections=prep['sections']
    if mutation=='drop':sections.pop()
    elif mutation=='duplicate':sections.append(deepcopy(sections[-1]))
    elif mutation=='swap':sections.reverse()
    elif mutation=='unknown':sections[0]['section']=['new','unknown']
    else:
        header=Path(prep['header']['path']);data=protocol.read(header);data['stable']['sources']={}
        header.write_bytes(streams.encode(data));prep['header'].update(bytes=header.stat().st_size,sha256=sha256_file(header))
    replace_cell(owner,value,ref,path,shell)
    with pytest.raises(EvaluationError) as exc:framing.load(owner/'report.json')
    assert exc.value.status=='MISSING'


@pytest.mark.parametrize('kind',['material','metadata'])
def test_exact_record_bound_and_next_byte_missing_retains_partial_bytes(tmp_path,monkeypatch,kind):
    root=tmp_path.resolve();payload={'complete':'x'*50};size=len(streams.encode(payload))
    monkeypatch.setitem(streams.LIMITS,kind+'_record_bytes',size)
    exact=streams.put_stream(root/'exact.jsonl',[payload],material=kind=='material')
    assert streams.get_stream(exact,root/'exact.jsonl',streams.Budget(),material=kind=='material')==[payload]
    with pytest.raises(EvaluationError) as exc:streams.put_stream(root/'over.jsonl',[payload,{'complete':'x'*51}],material=kind=='material')
    assert exc.value.status=='MISSING' and (root/'over.jsonl').read_bytes()==streams.encode(payload)


@pytest.mark.parametrize('kind',['bytes','records','aggregate'])
def test_finite_stream_and_aggregate_bounds_have_exact_controls(tmp_path,monkeypatch,kind):
    root=tmp_path.resolve();payload={'complete':'x'*50};size=len(streams.encode(payload))
    if kind=='bytes':monkeypatch.setitem(streams.LIMITS,'stream_bytes',size*2)
    elif kind=='records':monkeypatch.setitem(streams.LIMITS,'stream_records',2)
    else:monkeypatch.setitem(streams.LIMITS,'typed_aggregate_bytes',size*2)
    budget=streams.WriterBudget();ref=streams.put_stream(root/'exact.jsonl',[payload,payload],budget=budget)
    assert streams.get_stream(ref,root/'exact.jsonl',streams.Budget())==[payload,payload]
    with pytest.raises(EvaluationError):streams.put_stream(root/'over.jsonl',[payload,payload,payload],budget=streams.WriterBudget())
    if kind=='aggregate':
        monkeypatch.setitem(streams.LIMITS,'typed_aggregate_bytes',size*2-1)
        with pytest.raises(EvaluationError):streams.get_stream(ref,root/'exact.jsonl',streams.Budget())


def test_final_postpublication_shards_can_cross_unchanged4gib_budget(tmp_path,monkeypatch):
    owner=tmp_path.resolve()/'owned';original=report(owner,n=1,max_windows=False)
    actual=report_accounting.census
    def fixture_census(path):
        result=actual(path)
        if Path(path).name=='goldmine.fast' and Path(path).parent.name=='native':
            assert (Path(path)/'report-data/rows.jsonl').exists()
            result['logical_bytes']+=4*1024**3 # declared metadata fixture, no huge file/allocation
        return result
    monkeypatch.setattr(report_accounting,'census',fixture_census)
    result=framing.publish(owner,original)
    assert streams.LIMITS['cell_bytes']==4*1024**3
    assert result['resource_status']=='FAIL' and result['status']=='MISSING'
    assert framing.load(owner/'report.json')==result


def test_interrupted_publication_keeps_fixed18_and_partial_bytes_missing(tmp_path,monkeypatch):
    owner=tmp_path.resolve()/'owned';original=report(owner,n=2,max_windows=False)
    raw_before=deepcopy(original['artifacts']);calls=[]
    def interrupted(cell,*args,**kwargs):
        calls.append(str(cell));target=cell/'report-data';target.mkdir();(target/'rows.jsonl').write_bytes(b'partial fixture retained\n')
        raise KeyboardInterrupt()
    monkeypatch.setattr(report_cells,'publish',interrupted)
    result=framing.publish(owner,original);manifest=wire(owner)
    assert result['status']=='MISSING' and len(calls)==1 and len(manifest['cells'])==18
    assert all(r['state']=='MISSING' for r in manifest['cells'])
    assert framing.raw_artifacts(owner,original['registered_arms'])==raw_before
    assert (owner/'arms/native/goldmine.fast/report-data/rows.jsonl').read_bytes()==b'partial fixture retained\n'
    with pytest.raises(EvaluationError) as exc:framing.load(owner/'report.json')
    assert exc.value.status=='MISSING'


@pytest.mark.parametrize('kind',['prepared-header','aggregate'])
def test_component_limit_never_publishes_successful_prefix_as_complete(tmp_path,monkeypatch,kind):
    owner=tmp_path.resolve()/'owned';original=report(owner,n=1,max_windows=False);raw=deepcopy(original['artifacts'])
    if kind=='prepared-header':
        monkeypatch.setitem(streams.LIMITS,'header_bytes',32*1024)
        original['systems'][0]['preparation']['stable']['opaque_unknown_header']['oversize']='x'*(64*1024)
    else:monkeypatch.setitem(streams.LIMITS,'typed_aggregate_bytes',128*1024)
    result=framing.publish(owner,original);manifest=wire(owner)
    assert result['status']=='MISSING' and result['framing']['publication']['errors']
    assert len(manifest['cells'])==18 and any(r['state']=='MISSING' and r['component'] is None for r in manifest['cells'])
    assert framing.raw_artifacts(owner,original['registered_arms'])==raw
    assert (owner/'arms/native/goldmine.fast/prepared-index/fixture.bin').read_bytes()==b'not-real-index'
    assert any((owner/'arms').glob('*/*/report-data/prepared-stable.sources.jsonl'))
    with pytest.raises(EvaluationError) as exc:framing.load(owner/'report.json')
    assert exc.value.status=='MISSING'


def test_root_administration_limit_does_not_hide_derived_cell_accounting(tmp_path,monkeypatch):
    owner=tmp_path.resolve()/'owned';original=report(owner,n=1,max_windows=False)
    admin=sum((owner/p).stat().st_blocks*512 for p in ('ownership.json','plan.json'))
    monkeypatch.setitem(streams.LIMITS,'root_admin_bytes',streams.LIMITS['header_bytes']+admin-1)
    result=framing.publish(owner,original)
    assert result['resource_status']=='FAIL' and result['status']=='MISSING'
    assert result['framing']['publication']['root_administration']['status']=='FAIL'
    assert framing.load(owner/'report.json')==result


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="capture replay binds the Darwin ps/lsof resource-observer identity")
def test_existing_strict_capture_verifier_keeps_logical_api_and_starts_no_child_on_replay(tmp_path,monkeypatch):
    from comparison_engine_fixtures import data,missing_plan
    from comparison_source_fixtures import synthetic_native_policy
    from search_comparison.engine_capture import run,commands
    root=tmp_path.resolve();fixture=data(root/'fixture')
    with synthetic_native_policy():
        path,pin,projection=missing_plan(root,fixture)
        actual=run.capture(path,pin,root/'capture',root,fixture=True)
        monkeypatch.setattr(commands,'capture_command',lambda *a,**k:pytest.fail('replay launched child'))
        assert run.verify_capture(root/'capture/report.json')==actual
        assert len(actual['systems'])==18 and len(actual['systems'][0]['rows'])==7


def test_cli_emits_bounded_wire_while_python_api_retains_complete_rows(tmp_path,monkeypatch,capsys):
    from search_comparison.engine_capture import run
    owner=tmp_path.resolve()/'owned';logical=framing.publish(owner,report(owner,n=1,max_windows=False))
    entry=Path(run.__file__).resolve().parents[2]/'capture_comparison_engines.py'
    spec=importlib.util.spec_from_file_location('declared_fixture_framing_entry',entry);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(run,'verify_capture',lambda path:logical)
    assert module.main(['verify','--report',str(owner/'report.json')])==0
    output=capsys.readouterr().out;value=json.loads(output)
    assert value['format']==streams.FORMAT and 'systems' not in value and len(logical['systems'])==18
    assert len(output.encode())<=streams.LIMITS['header_bytes']
