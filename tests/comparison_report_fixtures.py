"""Synthetic framing grammar only; no real sources, labels, indexes or engines."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from answer_eval.protocol import canonical_sha256,sha256_file
from product_eval.schema import CLASSES
from search_comparison.adapters import DocumentMap
from search_comparison.engine_capture import protocol,run
from search_comparison.engine_capture.results import parse_result
from search_comparison.source_views.run import write_new


def prepared(engine,owner,docs):
    body='COMPLETE DECLARED SYNTHETIC BODY 中文\nsecond full line\n'
    sources={d['source_path']:{'file':{'path':str(owner/d['view_path']),'sha256':hashlib.sha256(body.encode()).hexdigest(),'info':{'semantic':'synthetic','nested':{'captured':'2026-01-02'}}},
                                 'parts':[{'id':n,'text':body,'vector':{'bytes':8,'sha256':'b'*64}}],
                                 'body':body,'status':'DECLARED_SYNTHETIC_ONLY'} for n,d in enumerate(docs)}
    value={'sources':sources,'opaque_unknown_header':{'must_retain':[1,'中文',{'deeper':True}]}}
    if engine=='goldmine':value.update(schema=6,meta={'fixture':'not an actual SQLite index'})
    if engine=='qmd':
        value.update(documents=[{'id':n,'path':d['view_path'],'hash':d['hash'],'body':body} for n,d in enumerate(docs)],
                     vectors=[{'hash':d['hash'],'seq':0,'total_chunks':1,'model':'declared-synthetic-model'} for d in docs],
                     vector_rows=[{'hash_seq':d['hash']+'_0','bytes':8,'sha256':'b'*64} for d in docs],
                     fts=[{'rowid':n,'body':body} for n in range(len(docs))],configuration={'fixture':True},physical={'fixture':True})
    if engine=='recoll':
        value.update(files={'declared-native-file':{'kind':'file','bytes':7,'sha256':'c'*64}},
                     metadata=[{'ordinal':n,'url':(owner/d['view_path']).as_uri(),'ipath':'','full_fixture_body':body} for n,d in enumerate(docs)])
    return {'stable':value,'stable_sha256':canonical_sha256(value),'observed_files':{'declared-only':'d'*64},
            'documents':[{'uri':'qmd://sources/'+d['view_path']+'?index=comparison',**d} for d in docs] if engine=='qmd' else []}


def report(owner,*,n=210,max_windows=True,excerpt=19):
    owner.mkdir()
    corpus=Path('/declared-synthetic-corpus-never-created')
    docs=[{'view_path':f'd{i:03d}.txt','source_path':f'd{i:03d}.txt','hash':hashlib.sha256(('synthetic-'+str(i)).encode()).hexdigest()} for i in range(122)]
    mapping=DocumentMap(corpus,docs)
    requests=[{'id':f'synthetic-{i:03d}','q':'synthetic query','primary_class':CLASSES[i//30],
               'source_group':'declared-synthetic-only'} for i in range(n)]
    arms=list(protocol.MANDATORY+protocol.CONTROLS);systems=[]
    write_new(owner/'ownership.json',{'schema':1,'suite_id':protocol.SUITE,'nonce':str(uuid.uuid4()),'owner':str(owner),'plan_sha256':'a'*64})
    write_new(owner/'plan.json',{'fixture':'framing grammar only; not a valid engine plan or quality receipt'})
    for view in ('native','common'):
        for arm in arms:
            engine=arm.split('.')[0];cell=owner/'arms'/view/arm;cell.mkdir(parents=True)
            audit=prepared(engine,corpus,docs)
            rows=[];raw_outputs=[]
            for ordinal,request in enumerate(requests,1):
                count=100 if max_windows and ordinal==1 else 1 if engine=='goldmine' else 0
                if engine=='goldmine':
                    payload=[{'rank':i+1,'score':.5,'z':1.,'cosine':.7,'coverage':1.,'kind':'doc',
                              'path':str(corpus/d['view_path']),'loc':1,'loc_end':2,'segment':'chunk','excerpt':'x'*excerpt,
                              'file_status':'content_indexed','scorer':'hybrid'} for i,d in enumerate(docs[:count])]
                    if arm in ('goldmine.quality-rerank','goldmine.quality-multi'):
                        payload={'hits':payload,'settings':{'scorer':'hybrid','mode':'quality','rerank':'local' if arm.endswith('rerank') else 'off',
                                 'multi_query':arm.endswith('multi'),'limit':100,'reranker':{'executed':True,'candidates':min(count,20),'model':'declared-no-model'},
                                 'query_plan':{'queries':1,'expanded':False,'subquery_failures':[]}},'fallback_reason':None}
                    raw=json.dumps(payload,ensure_ascii=False,indent=2)
                elif engine=='qmd':
                    raw=json.dumps([{'docid':'#'+d['hash'][:6],'score':.5,'file':'qmd://sources/'+d['view_path']+'?index=comparison',
                                     'title':'declared synthetic','snippet':'full emitted synthetic snippet','line':1} for d in docs[:count]])
                else:
                    raw='Recoll query: synthetic\n'+str(count)+' results\n'+''.join(base64.b64encode((corpus/d['view_path']).as_uri().encode()).decode()+'\n' for d in docs[:count])
                parsed=parse_result(raw,arm,mapping,audit);qrel={'relevance':{} if request['primary_class']=='noise' else {'d000.txt':3}}
                rows.append({**request,'ordinal':ordinal,'status':'OK','result':parsed,'metrics':run._metrics(request,qrel,parsed),'error':None})
                raw_outputs.append(raw)
            rawdir=cell/'raw-fixture';rawdir.mkdir()
            # One bounded raw fixture is enough to authenticate framing; it is
            # deliberately not masquerading as210 real engine commands.
            (rawdir/'stdout.log').write_text(raw_outputs[0]);(rawdir/'stderr.log').write_bytes(b'')
            write_new(rawdir/'manifest.json',{'declared_fixture':'not an actual command/resource receipt','all_actual_engines_models_queries':0})
            command={'path':str(rawdir/'manifest.json'),'sha256':sha256_file(rawdir/'manifest.json')}
            snapshot=cell/'prepared-index';snapshot.mkdir();(snapshot/'fixture.bin').write_bytes(b'not-real-index')
            systems.append({'view':view,'arm':arm,'owner':str(cell),'rows':rows,'preparation':audit,
                            'prepared_snapshot':{'path':str(snapshot),'files':protocol.inventory(snapshot)},'commands':[command],
                            'resource_observations':[{'command':command,'declared_fixture':True,'observation':{'sampled_resource_status':'MISSING'}}],
                            'resource_status':'MISSING','status':'CAPTURED_DEV_RESULTS','error':None,'summary':run.summarize(rows),
                            'interrupted':False,'resource_preflight':{'declared_fixture':True},'performance_status':'NOT_CAPTURED',
                            'update_status':'NOT_CAPTURED','position_status':'NOT_COMPARABLE_UNVERIFIED_ORIGINAL_COORDINATES'})
    return {'schema':1,'suite_id':protocol.SUITE,'evidence_mode':'fixture','owner':str(owner),'plan':{'path':str(owner/'plan.json'),'sha256':sha256_file(owner/'plan.json')},
            'source_identity':{'declared_synthetic':True,'never_real_corpus':True},'projection_identity':{'ids_sha256':canonical_sha256([r['id'] for r in requests])},
            'implementation':{'declared_fixture':'not a real frozen implementation'},'registered_arms':arms,'n_fixed_queries':n,'source_count':122,
            'systems':systems,'status':'CAPTURED_DEV_RESULTS','full_comparison_status':'MISSING_PERFORMANCE_UPDATE_RESOURCE_PHASES',
            'resource_status':'MISSING','resource_setup_errors':[],'quality_evidence':False,'release_eligible':False,'artifacts':protocol.inventory(owner)}


def original19byte_lower_bound():
    """Exact independent minimal-metadata family; deliberately not a receipt."""
    corpus=Path('/declared-synthetic-corpus-never-created')
    documents=[{'view_path':f'd{n:03d}.txt','source_path':f'd{n:03d}.txt'} for n in range(100)]
    mapping=DocumentMap(corpus,documents)
    hits=[{'rank':1,'score':.5,'z':1.,'cosine':.7,'coverage':1.,'kind':'doc','path':str(corpus/documents[0]['view_path']),
           'loc':1,'loc_end':2,'segment':'chunk','excerpt':'x'*19,'file_status':'content_indexed','scorer':'hybrid'}]
    systems=[]
    for view in ('native','common'):
        for arm in protocol.MANDATORY+protocol.CONTROLS:
            if arm.startswith('goldmine.'):
                payload=hits
                if arm in ('goldmine.quality-rerank','goldmine.quality-multi'):
                    payload={'hits':payload,'settings':{'scorer':'hybrid','mode':'quality','rerank':'local' if arm.endswith('rerank') else 'off',
                             'multi_query':arm.endswith('multi'),'limit':100,'reranker':{'executed':True,'candidates':1,'model':'synthetic-not-an-engine-completion'},
                             'query_plan':{'queries':1,'expanded':False,'subquery_failures':[]}},'fallback_reason':None}
                raw=json.dumps(payload,ensure_ascii=False,indent=2)
            elif arm.startswith('qmd.'):raw='[]'
            else:raw='Recoll query: synthetic\n0 results\n'
            parsed=parse_result(raw,arm,mapping,{'documents':[]});rows=[]
            for n in range(210):
                request={'id':f'synthetic-{n:03d}','q':'synthetic query','primary_class':CLASSES[n//30],'source_group':'synthetic-only'}
                qrel={'id':request['id'],'relevance':{} if request['primary_class']=='noise' else {'d000.txt':3}}
                rows.append({**request,'ordinal':n+1,'status':'OK','result':parsed,'metrics':run._metrics(request,qrel,parsed),'error':None})
            systems.append({'arm':arm,'view':view,'rows':rows})
    return {'schema':1,'systems':systems,'evidence_mode':'fixture','quality_evidence':False,'release_eligible':False}
