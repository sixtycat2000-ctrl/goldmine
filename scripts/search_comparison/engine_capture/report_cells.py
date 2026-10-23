"""Closed complete per-cell records, including raw windows and prepared text."""
from answer_eval.protocol import EvaluationError,canonical_sha256,fields
from .protocol import CLASSES
from . import report_prepared as prepared,report_streams as streams

REQUIRED={'arm','view','status','error','summary','resource_status'}
OPTIONAL={'owner','prepared_snapshot','interrupted','resource_preflight','performance_status','update_status','position_status'}
LARGE={'rows','commands','resource_observations','preparation'}
ROW_FIELDS={'id','q','primary_class','source_group','ordinal','status','result','metrics','error'}


def validate_rows(rows,n,ids_sha):
    if not isinstance(rows,list) or len(rows)!=n:raise EvaluationError('complete query stream lost its fixed denominator',status='MISSING')
    ids=[]
    for ordinal,row in enumerate(rows,1):
        fields(row,ROW_FIELDS,'complete query observation')
        if (type(row['ordinal']) is not int or row['ordinal']!=ordinal or row['primary_class'] not in CLASSES
                or any(not isinstance(row[k],str) or not row[k] for k in ('id','q','source_group'))
                or row['status'] not in {'OK','MISSING','TRUNCATED'}):
            raise EvaluationError('complete query ordinal/identity/disposition differs',status='MISSING')
        ids.append(row['id'])
    if len(ids)!=len(set(ids)) or canonical_sha256(ids)!=ids_sha:
        raise EvaluationError('complete query IDs omitted/duplicated/reordered',status='MISSING')


def _metadata(value,view,arm,owner):
    if (not isinstance(value,dict) or not REQUIRED<=set(value) or set(value)-REQUIRED-OPTIONAL
            or value['view']!=view or value['arm']!=arm
            or value.get('owner',str(owner))!=str(owner)):
        raise EvaluationError('complete cell identity/metadata schema changed',status='MISSING')


def _raw_rows(value,prefix):
    if not isinstance(value,list) or any(not isinstance(r,dict) or set(r)!={'path','file'} or not isinstance(r['path'],str)
            or not r['path'].startswith(prefix) for r in value):
        raise EvaluationError('complete raw artifact stream escaped its fixed cell',status='MISSING')
    names=[r['path'] for r in value]
    if names!=sorted(set(names)):raise EvaluationError('raw artifact stream omitted/duplicated/reordered',status='MISSING')
    if any(not isinstance(r['file'],dict) or set(r['file'])!={'kind','bytes','sha256'} or r['file']['kind']!='file'
           or type(r['file']['bytes']) is not int or r['file']['bytes']<0 for r in value):
        raise EvaluationError('raw artifact metadata lost its exact typed byte identity',status='MISSING')
    return {r['path']:r['file'] for r in value}


def publish(owner,value,raw,*,n,ids_sha,budget):
    root=owner/'report-data';root.mkdir()
    meta={k:v for k,v in value.items() if k not in LARGE}
    _metadata(meta,value['view'],value['arm'],owner)
    validate_rows(value['rows'],n,ids_sha)
    if any(not isinstance(value[k],list) for k in ('commands','resource_observations')):
        raise EvaluationError('complete command/resource registries must be lists',status='MISSING')
    shell={'format':streams.FORMAT,'metadata':meta,
           'rows':streams.put_stream(root/'rows.jsonl',value['rows'],material=True,budget=budget),
           'commands':streams.put_stream(root/'commands.jsonl',value['commands'],budget=budget),
           'resources':streams.put_stream(root/'resources.jsonl',value['resource_observations'],budget=budget),
           'raw_inventory':streams.put_stream(root/'raw-inventory.jsonl',({'path':p,'file':raw[p]} for p in sorted(raw)),budget=budget),
           'has_preparation':'preparation' in value,
           'preparation':prepared.publish(root,value['preparation'],budget=budget) if 'preparation' in value else None}
    return streams.put_json(root/'cell.json',shell,budget=budget)


def read(owner,ref,*,view,arm,n,ids_sha,budget):
    root=owner/'report-data';shell=streams.get_json(ref,root/'cell.json',budget)
    fields(shell,{'format','metadata','rows','commands','resources','raw_inventory','has_preparation','preparation'},'complete cell framing')
    if shell['format']!=streams.FORMAT or type(shell['has_preparation']) is not bool:
        raise EvaluationError('complete cell format/preparation-presence changed',status='MISSING')
    _metadata(shell['metadata'],view,arm,owner)
    rows=streams.get_stream(shell['rows'],root/'rows.jsonl',budget,material=True)
    validate_rows(rows,n,ids_sha)
    value={**shell['metadata'],'rows':rows,
           'commands':streams.get_stream(shell['commands'],root/'commands.jsonl',budget),
           'resource_observations':streams.get_stream(shell['resources'],root/'resources.jsonl',budget)}
    raw=_raw_rows(streams.get_stream(shell['raw_inventory'],root/'raw-inventory.jsonl',budget),'arms/'+view+'/'+arm+'/')
    if shell['has_preparation']:
        value['preparation']=prepared.read(root,shell['preparation'],budget)
    elif shell['preparation'] is not None:raise EvaluationError('absent preparation was substituted by a partial summary',status='MISSING')
    return value,raw
