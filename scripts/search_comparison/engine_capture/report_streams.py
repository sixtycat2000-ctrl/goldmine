"""Finite typed framing primitives; limits are pinned by implementation identity."""
import hashlib
import json
from pathlib import Path

from answer_eval.protocol import EvaluationError, fields, parse_json, sha256_file
from .protocol import local_path, reference

FORMAT = 'comparison.complete-engine-report/v2'
LIMITS = {'header_bytes':8*1024*1024,'material_record_bytes':64*1024*1024,
          'metadata_record_bytes':4*1024*1024,'stream_bytes':4*1024**3,'stream_records':50000,
          'typed_aggregate_bytes':1024**3,'root_admin_bytes':16*1024*1024,'cell_bytes':4*1024**3}


def contract():return dict(LIMITS)


def encode(value):
    try:return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()+b'\n'
    except (RecursionError,UnicodeError,ValueError,TypeError) as exc:raise EvaluationError('unrepresentable complete report record',status='MISSING') from exc


class WriterBudget:
    def __init__(self):self.bytes=0
    def reserve(self,size):
        if self.bytes+size>LIMITS['typed_aggregate_bytes']:
            raise EvaluationError('complete typed material exceeds preregistered aggregate cap; partial bytes retained',status='MISSING')
        self.bytes+=size


def put_json(path,value,*,budget=None):
    data=encode(value)
    if len(data)>LIMITS['header_bytes']:raise EvaluationError('complete report header exceeds accepted8MiB cap',status='MISSING')
    if budget is not None:budget.reserve(len(data))
    with path.open('xb') as stream:stream.write(data)
    return ref(path)


def ref(path,**extra):
    return {'path':str(path),'bytes':path.stat().st_size,'sha256':sha256_file(path),**extra}


def put_stream(path,values,*,material=False,budget=None):
    rows=0;size=0;limit=LIMITS['material_record_bytes' if material else 'metadata_record_bytes']
    with path.open('xb') as stream:
        for value in values:
            data=encode(value);rows+=1;size+=len(data)
            if len(data)>limit or rows>LIMITS['stream_records'] or size>LIMITS['stream_bytes']:
                raise EvaluationError('complete report stream/record exceeds preregistered bound; partial bytes retained',status='MISSING')
            if budget is not None:budget.reserve(len(data))
            stream.write(data)
    return ref(path,rows=rows,record_kind='material' if material else 'metadata')


class Budget:
    def __init__(self,*,root_bytes=0):
        self.bytes=root_bytes;self.paths=set()
        if root_bytes>LIMITS['typed_aggregate_bytes']:raise EvaluationError('complete root exceeds typed aggregate cap',status='MISSING')
    def bound(self,value,path,limit,*,stream=False):
        fields(value,{'path','sha256','bytes'}|({'rows','record_kind'} if stream else set()),'complete report component')
        if (type(value['bytes']) is not int or value['bytes']<0 or value['bytes']>limit
                or value['path']!=str(path) or str(path) in self.paths):
            raise EvaluationError('report component path/length/alias exceeds fixed registry',status='MISSING')
        actual=reference({k:value[k] for k in ('path','sha256')},bound=limit)
        if actual.stat().st_size!=value['bytes']:raise EvaluationError('report component byte length differs',status='MISSING')
        self.bytes+=value['bytes'];self.paths.add(str(path))
        if self.bytes>LIMITS['typed_aggregate_bytes']:raise EvaluationError('complete typed material exceeds preregistered aggregate cap',status='MISSING')
        return actual


def get_json(value,path,budget):
    path=budget.bound(value,path,LIMITS['header_bytes'])
    with path.open('rb') as stream:data=stream.read(LIMITS['header_bytes']+1)
    if len(data)!=value['bytes'] or hashlib.sha256(data).hexdigest()!=value['sha256']:
        raise EvaluationError('report header changed during bounded read',status='MISSING')
    try:return parse_json(data.decode(),'complete report header')
    except (EvaluationError,RecursionError,UnicodeError) as exc:raise EvaluationError('invalid report header JSON/encoding/depth',status='MISSING') from exc


def get_stream(value,path,budget,*,material=False):
    kind='material' if material else 'metadata'
    if (not isinstance(value,dict) or type(value.get('rows')) is not int or not 0<=value['rows']<=LIMITS['stream_records']
            or value.get('record_kind')!=kind):raise EvaluationError('invalid complete stream denominator/kind',status='MISSING')
    path=budget.bound(value,path,LIMITS['stream_bytes'],stream=True)
    limit=LIMITS['material_record_bytes' if material else 'metadata_record_bytes'];result=[];digest=hashlib.sha256()
    with path.open('rb') as stream:
        for _ in range(value['rows']):
            data=stream.readline(limit+1)
            if len(data)>limit or not data.endswith(b'\n') or data==b'\n':raise EvaluationError('truncated/blank/oversized complete report record',status='MISSING')
            digest.update(data)
            try:result.append(parse_json(data.decode(),'complete report stream'))
            except (EvaluationError,RecursionError,UnicodeError) as exc:raise EvaluationError('invalid report record JSON/encoding/depth',status='MISSING') from exc
        if stream.read(1):raise EvaluationError('extra complete report stream records',status='MISSING')
    if digest.hexdigest()!=value['sha256']:raise EvaluationError('report stream changed during bounded read',status='MISSING')
    return result
