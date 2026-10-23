"""Versioned complete engine reports; logical consumers receive every record."""
from pathlib import Path

from answer_eval.protocol import EvaluationError,canonical_sha256,fields,sha256_file
from .protocol import SUITE,expected_arms,inventory,local_path,read,reference
from . import report_accounting as accounting,report_cells as cells,report_streams as streams

HEADER={'schema','suite_id','evidence_mode','owner','plan','source_identity','projection_identity','implementation',
        'registered_arms','n_fixed_queries','source_count','status','full_comparison_status','resource_status',
        'resource_setup_errors','quality_evidence','release_eligible'}
WIRE={'schema','format','limits','header','cells','raw_administration','publication','status','resource_status'}
ADMIN={'ownership.json','plan.json'}
ERRORS=(EvaluationError,OSError,UnicodeError,ValueError,TypeError,KeyError)


def _header(value,owner):
    fields(value,HEADER,'complete report header')
    if (type(value['schema']) is not int or value['schema']!=1 or value['suite_id']!=SUITE
            or value['owner']!=str(owner) or value['evidence_mode'] not in {'fixture','real'}
            or type(value['n_fixed_queries']) is not int or not 1<=value['n_fixed_queries']<=210
            or type(value['source_count']) is not int or not 1<=value['source_count']<=122
            or value['quality_evidence'] is not False or value['release_eligible'] is not False
            or not isinstance(value['projection_identity'],dict) or 'ids_sha256' not in value['projection_identity']):
        raise EvaluationError('complete report source/query/schema identity differs',status='MISSING')
    if value['evidence_mode']=='real' and (value['n_fixed_queries']!=210 or value['source_count']!=122):
        raise EvaluationError('real complete report lost its fixed122/210 denominators',status='MISSING')
    arms=expected_arms(value['registered_arms'],fixture=value['evidence_mode']=='fixture')
    return [(v,a) for v in ('native','common') for a in arms]


def _prefixes(arms):return ['arms/'+v+'/'+a+'/report-data/' for v in ('native','common') for a in arms]


def raw_artifacts(owner,arms):
    prefixes=_prefixes(arms)
    return {p:v for p,v in inventory(owner).items() if p!='report.json' and not any(p.startswith(k) for k in prefixes)}


def _partition(raw,matrix):
    admin={p:raw[p] for p in sorted(ADMIN) if p in raw}
    groups={pair:{} for pair in matrix}
    for path,value in raw.items():
        if path in ADMIN:continue
        found=[pair for pair in matrix if path.startswith('arms/'+pair[0]+'/'+pair[1]+'/')]
        if len(found)!=1:raise EvaluationError('unregistered root/cell artifact outside complete report ownership',status='MISSING')
        groups[found[0]][path]=value
    if set(admin)!=ADMIN:raise EvaluationError('complete root administration omitted its plan/ownership',status='MISSING')
    return admin,groups


def statuses(quality,resource,publication):
    if publication=='FAIL':return 'MISSING','FAIL','MISSING_PERFORMANCE_UPDATE_RESOURCE_PHASES'
    if publication!='VERIFIED':return 'MISSING','FAIL' if resource=='FAIL' else 'MISSING','MISSING_PERFORMANCE_UPDATE_RESOURCE_PHASES'
    return quality,resource,'MISSING_PERFORMANCE_UPDATE_PHASES' if resource=='PASS' else 'MISSING_PERFORMANCE_UPDATE_RESOURCE_PHASES'


def _logical(header,systems,raw,publication):
    quality,resource,full=statuses(header['status'],header['resource_status'],publication['status'])
    return {**header,'systems':systems,'artifacts':dict(sorted(raw.items())),
            'status':quality,'resource_status':resource,'full_comparison_status':full,
            'framing':{'format':streams.FORMAT,'limits':streams.contract(),'publication':publication}}


def publish(owner,report):
    owner=local_path(str(owner))
    header={k:v for k,v in report.items() if k not in {'systems','artifacts'}}
    matrix=_header(header,owner)
    if [(r.get('view'),r.get('arm')) for r in report['systems']]!=matrix:
        raise EvaluationError('complete publication cannot shrink/reorder its fixed matrix',status='MISSING')
    raw=raw_artifacts(owner,header['registered_arms'])
    if canonical_sha256(report['artifacts'])!=canonical_sha256(raw):raise EvaluationError('raw bytes changed before complete publication',status='MISSING')
    admin,groups=_partition(raw,matrix)
    budget=streams.WriterBudget();registry=[];errors=[];interrupted=False
    for pair,system in zip(matrix,report['systems']):
        view,arm=pair;cell=owner/'arms'/view/arm;cell.mkdir(parents=True,exist_ok=True)
        if interrupted:
            reason='publication interrupted; remaining fixed cell components retained as MISSING without further body writes'
            errors.append({'view':view,'arm':arm,'error':reason})
            registry.append({'view':view,'arm':arm,'state':'MISSING','component':None,'error':reason})
            continue
        try:
            ref=cells.publish(cell,system,groups[pair],n=header['n_fixed_queries'],ids_sha=header['projection_identity']['ids_sha256'],budget=budget)
            registry.append({'view':view,'arm':arm,'state':'PUBLISHED','component':ref,'error':None})
        except (*ERRORS,KeyboardInterrupt) as exc:
            interrupted=isinstance(exc,KeyboardInterrupt)
            reason=str(exc) or type(exc).__name__
            errors.append({'view':view,'arm':arm,'error':reason})
            registry.append({'view':view,'arm':arm,'state':'MISSING','component':None,'error':reason})
    publication=accounting.final(owner,matrix,admin,errors=errors,typed_bytes=budget.bytes)
    wire={'schema':2,'format':streams.FORMAT,'limits':streams.contract(),'header':header,'cells':registry,
          'raw_administration':admin,'publication':publication,
          'status':statuses(header['status'],header['resource_status'],publication['status'])[0],
          'resource_status':statuses(header['status'],header['resource_status'],publication['status'])[1]}
    if len(streams.encode(wire))+budget.bytes>streams.LIMITS['typed_aggregate_bytes']:
        publication['errors'].append({'view':None,'arm':None,'error':'complete root plus components exceeds typed aggregate cap'})
        publication['status']='MISSING' if publication['status']!='FAIL' else 'FAIL'
        wire['status'],wire['resource_status'],_=statuses(header['status'],header['resource_status'],publication['status'])
    try:streams.put_json(owner/'report.json',wire)
    except EvaluationError as exc:
        # An oversized complete header is never silently thinned into proof.
        # Existing full cell components/raw/index bytes remain untouched.
        failure={'schema':2,'format':streams.FORMAT,'limits':streams.contract(),'status':'MISSING','resource_status':wire['resource_status'],
                 'publication_error':str(exc),'owner':str(owner),'plan':header['plan'],'cells':registry,
                 'raw_authority':'complete retained per-cell components/raw commands/index snapshots; complete replay unavailable',
                 'quality_evidence':False,'release_eligible':False}
        streams.put_json(owner/'report.json',failure)
        publication['errors'].append({'view':None,'arm':None,'error':str(exc)})
        publication['status']='MISSING' if publication['status']!='FAIL' else 'FAIL'
    return _logical(header,report['systems'],raw,publication)


def _load(path):
    path=local_path(str(Path(path).absolute()))
    reference({'path':str(path),'sha256':sha256_file(path)},bound=streams.LIMITS['header_bytes'])
    if path.name!='report.json':raise EvaluationError('complete report replay requires its fixed root path',status='MISSING')
    owner=path.parent;wire=read(path)
    if (not isinstance(wire,dict) or set(wire)!=WIRE or type(wire.get('schema')) is not int or wire['schema']!=2
            or wire.get('format')!=streams.FORMAT or canonical_sha256(wire['limits'])!=canonical_sha256(streams.contract())):
        raise EvaluationError('complete report framing is legacy/incomplete; strict replay unavailable',status='MISSING')
    matrix=_header(wire['header'],owner)
    if not isinstance(wire['cells'],list) or len(wire['cells'])!=len(matrix):raise EvaluationError('complete cell registry lost its fixed denominator',status='MISSING')
    for (view,arm),row in zip(matrix,wire['cells']):
        fields(row,{'view','arm','state','component','error'},'complete cell reference')
        if row['view']!=view or row['arm']!=arm:raise EvaluationError('complete cell registry duplicated/reordered',status='MISSING')
        if row['state']!='PUBLISHED' or row['error'] is not None or row['component'] is None:
            raise EvaluationError('incomplete publication retains full raw evidence; complete replay MISSING',status='MISSING')
    if not isinstance(wire['publication'],dict) or wire['publication'].get('status') not in {'VERIFIED','FAIL'} or wire['publication'].get('errors')!=[]:
        raise EvaluationError('complete report publication/aggregate evidence is MISSING',status='MISSING')
    if not isinstance(wire['raw_administration'],dict) or set(wire['raw_administration'])!=ADMIN:
        raise EvaluationError('complete root administrative registry changed',status='MISSING')
    budget=streams.Budget(root_bytes=path.stat().st_size);systems=[];raw=dict(wire['raw_administration'])
    for (view,arm),row in zip(matrix,wire['cells']):
        system,files=cells.read(owner/'arms'/view/arm,row['component'],view=view,arm=arm,
                                n=wire['header']['n_fixed_queries'],ids_sha=wire['header']['projection_identity']['ids_sha256'],budget=budget)
        systems.append(system);raw.update(files)
    actual=inventory(owner);actual.pop('report.json',None)
    prefixes=_prefixes(wire['header']['registered_arms'])
    frames={str(owner/p) for p in actual if any(p.startswith(k) for k in prefixes)}
    if frames!=budget.paths or canonical_sha256(raw)!=canonical_sha256(raw_artifacts(owner,wire['header']['registered_arms'])):
        raise EvaluationError('complete raw/derived artifact registry omitted/changed/added bytes',status='MISSING')
    admin,_=_partition(raw,matrix)
    publication=accounting.final(owner,matrix,admin,errors=[],typed_bytes=budget.bytes-path.stat().st_size)
    if canonical_sha256(publication)!=canonical_sha256(wire['publication']):
        raise EvaluationError('post-publication byte census/resource disposition changed',status='MISSING')
    logical=_logical(wire['header'],systems,raw,publication)
    if (wire['status'],wire['resource_status'])!=(logical['status'],logical['resource_status']):
        raise EvaluationError('complete publication was relabeled as captured proof',status='MISSING')
    return logical


def load(path):
    try:return _load(path)
    except (*ERRORS,RecursionError) as exc:
        raise EvaluationError('complete report replay MISSING: '+str(exc),status='MISSING') from exc
