"""A closed section registry for every existing full prepared audit shape."""
from copy import deepcopy

from answer_eval.protocol import EvaluationError,fields
from . import report_streams as streams

SECTIONS = {('documents',):list,('stable','sources'):dict,('stable','documents'):list,
            ('stable','vectors'):list,('stable','vector_rows'):list,('stable','fts'):list,
            ('stable','metadata'):list,('stable','files'):dict}


def _parent(header,keys):
    parent=header
    for key in keys[:-1]:parent=parent.get(key,{}) if isinstance(parent,dict) else {}
    return parent


def _marker(keys,kind):return {'$complete_report_section':list(keys),'kind':'dictionary' if kind is dict else 'list'}


def publish(root,value,*,budget=None):
    header=deepcopy(value);sections=[]
    for keys,kind in SECTIONS.items():
        parent=_parent(header,keys)
        if not isinstance(parent,dict) or keys[-1] not in parent:continue
        part=parent[keys[-1]]
        if type(part) is not kind:raise EvaluationError('prepared section type differs from closed framing registry',status='MISSING')
        parent[keys[-1]]=_marker(keys,kind)
        name='prepared-'+'.'.join(keys)+'.jsonl'
        values=({'key':key,'value':part[key]} for key in sorted(part)) if kind is dict else ({'ordinal':n,'value':v} for n,v in enumerate(part))
        sections.append({'section':list(keys),'kind':'dictionary' if kind is dict else 'list','component':streams.put_stream(root/name,values,material=True,budget=budget)})
    return {'header':streams.put_json(root/'prepared-header.json',header,budget=budget),'sections':sections}


def read(root,value,budget):
    fields(value,{'header','sections'},'complete prepared framing')
    header=streams.get_json(value['header'],root/'prepared-header.json',budget)
    if not isinstance(value['sections'],list):raise EvaluationError('prepared sections must be ordered registry',status='MISSING')
    expected=[]
    for keys,kind in SECTIONS.items():
        parent=_parent(header,keys)
        if not isinstance(parent,dict) or keys[-1] not in parent:continue
        if parent[keys[-1]]!=_marker(keys,kind):raise EvaluationError('prepared section was inlined/substituted outside its typed registry',status='MISSING')
        expected.append(keys)
    positions=[]
    for section in value['sections']:
        fields(section,{'section','kind','component'},'complete prepared section')
        if not isinstance(section['section'],list) or any(not isinstance(k,str) for k in section['section']):raise EvaluationError('malformed prepared section identity',status='MISSING')
        positions.append(tuple(section['section']))
    if positions!=expected:raise EvaluationError('prepared section registry omitted/duplicated/reordered',status='MISSING')
    for section in value['sections']:
        fields(section,{'section','kind','component'},'complete prepared section')
        keys=tuple(section['section']);kind=SECTIONS.get(keys)
        if kind is None or section['kind']!=('dictionary' if kind is dict else 'list'):raise EvaluationError('unknown prepared section/kind',status='MISSING')
        rows=streams.get_stream(section['component'],root/('prepared-'+'.'.join(keys)+'.jsonl'),budget,material=True)
        if kind is dict:
            if (any(not isinstance(r,dict) or set(r)!={'key','value'} or not isinstance(r['key'],str) for r in rows)
                    or [r['key'] for r in rows]!=sorted({r['key'] for r in rows})):raise EvaluationError('prepared source/inventory dictionary duplicated/reordered',status='MISSING')
            part={r['key']:r['value'] for r in rows}
        else:
            if any(not isinstance(r,dict) or set(r)!={'ordinal','value'} or type(r['ordinal']) is not int or r['ordinal']!=n for n,r in enumerate(rows)):
                raise EvaluationError('prepared full-body/vector/list section omitted/reordered',status='MISSING')
            part=[r['value'] for r in rows]
        parent=_parent(header,keys)
        if not isinstance(parent,dict) or parent.get(keys[-1])!=_marker(keys,kind):raise EvaluationError('duplicate/substituted prepared section',status='MISSING')
        parent[keys[-1]]=part
    return header
