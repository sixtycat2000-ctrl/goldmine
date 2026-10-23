"""Mechanical assembly of explicit AI-curated source-first entries.
No question text or fact is generated here. Does not run retrieval or split data.
"""
from pathlib import Path
import sys,json,hashlib,unicodedata,collections
BASE=Path(__file__).resolve().parents[1]
ROOT=BASE.parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from product_eval.bundle import _validate_source,_labels,normalized
from product_eval.schema import validate_query
from product_eval.splits import source_groups

def sha(raw):return hashlib.sha256(raw).hexdigest()
def ref(path):return {'path':path.relative_to(BASE).as_posix(),'sha256':sha(path.read_bytes())}
def dump(path,value):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
repos={r['key']:r for r in json.loads((BASE/'metadata/repositories.json').read_text())}
downloads=json.loads((BASE/'metadata/source-downloads.json').read_text())
licenses={'requests':'MIT','click':'BSD-3-Clause','cpython':'PSF-2.0','vue-zh':'CC-BY-4.0','vite-ja':'MIT','vite-ko':'MIT'}
sources=[];views={};source_lookup={}
for download in downloads:
    key,path=download['key'],download['path']
    original=BASE/download['asset_path']
    raw=original.read_bytes()
    assert sha(raw)==download['sha256']
    text=normalized(raw.decode('utf-8'))
    viewpath=BASE/'views/text'/key/(path+'.txt')
    viewpath.parent.mkdir(parents=True,exist_ok=True);viewpath.write_text(text,encoding='utf-8',newline='')
    sourcepath='text/'+key+'/'+path
    body={'id':'body',**ref(viewpath),'evidence_type':'content_text','location':{'unit':'line','start':1,'end':len(text.splitlines())},'storage_field':None,'normalization':'nfc-lf-v1'}
    source={'path':sourcepath,'asset':ref(original),'kind':download['kind'],'source_group':'source:'+sourcepath,'parents':[],
            'provenance':{'type':'real','origin':{'kind':'git','repository':repos[key]['repository'],'revision':download['revision'],'path':path},
                          'license':licenses[key],'license_file':ref(BASE/'licenses/text'/f'{key}-LICENSE.txt'),
                          'generation':'Verbatim upstream bytes at pinned commit; canonical view changes only NFC/LF. No question, answer key or compiler inserted into source.'},'views':[body]}
    if download['primary_class']=='lookup':
        namepath=BASE/'views/text'/key/(path+'.basename.txt')
        namepath.write_text(Path(path).name,encoding='utf-8',newline='')
        source['views'].append({'id':'basename',**ref(namepath),'evidence_type':'name_metadata','location':None,'storage_field':'basename','normalization':'nfc-lf-v1'})
    _validate_source(BASE,source,views)
    sources.append(source);source_lookup[(key,path)]=source

queries=[];counts=collections.Counter();errors=[]
for primary,filename in [('semantic','semantic-authoring.json'),('cjk','cjk-authoring.json'),('lookup','lookup-authoring.json')]:
    authoring=BASE/'metadata'/filename
    if not authoring.exists():continue
    for entry in json.loads(authoring.read_text()):
        source=source_lookup[(entry['repository'],entry['path'])]
        counts[primary]+=1
        qid=f'text-{primary}-{counts[primary]:03d}'
        viewid=entry.get('view_id','body')
        text=views[(source['path'],viewid)]['text']
        anchor=normalized(entry['anchor'])
        found=[];cursor=0
        while (at:=text.find(anchor,cursor))>=0:
            found.append(at);cursor=at+1
        if len(found)!=1:
            errors.append({'id':qid,'q':entry['q'],'source':source['path'],'anchor_matches':len(found),'anchor':anchor})
            continue
        start=found[0];end=start+len(anchor)
        location=None if viewid=='basename' else {'unit':'line','start':1+text[:start].count('\n'),'end':1+text[:end-1].count('\n')}
        expected={'evidence_id':'e0','path':source['path'],'kind':source['kind'],'grade':3,'evidence_type':views[(source['path'],viewid)]['evidence_type'],
                  'location':location,'span':{'start_char':start,'end_char':end,'text':anchor,'sha256':sha(anchor.encode())},'view_id':viewid}
        if viewid=='basename':expected['storage_field']='basename'
        tags=[('code' if source['kind']=='code' else 'docs'),'real']+entry.get('tags',[])
        q={'id':qid,'primary_class':primary,'tags':tags,'q':entry['q'],'expect':[expected],
           'required_facts':[{'fact_id':'f0','description':entry['fact'],'sufficient_evidence':[['e0']]}],'needs':[],'source_group':source['source_group']}
        validate_query(q);queries.append(q)
if errors:
    dump(BASE/'metadata/anchor-errors.json',errors)
    print(json.dumps(errors,ensure_ascii=False,indent=2))
    raise SystemExit(2)
(BASE/'metadata/anchor-errors.json').unlink(missing_ok=True)
_labels(queries,{s['path']:s for s in sources},views)
usage=collections.Counter(e['path'] for q in queries for e in q['expect'])
assert max(usage.values(),default=0)<=8
assert len({q['q'] for q in queries})==len(queries)
dump(BASE/'shard.json',{'sources':sources,'queries':queries})
summary={'schema':1,'status':'AI_CURATED_AWAITING_INDEPENDENT_AUDIT','author':'OpenAI Codex AI curator; no human review claimed','retrieval_exposure':'NONE: no candidate ranking, holdout output or relevance run was read or executed',
         'sources':len(sources),'questions':len(queries),'classes':dict(counts),'tags':dict(collections.Counter(t for q in queries for t in q['tags'])),
         'groups_per_class':{primary:len({q['source_group'] for q in queries if q['primary_class']==primary}) for primary in counts},'max_questions_per_source':max(usage.values(),default=0),
         'raw_bytes':sum((BASE/s['asset']['path']).stat().st_size for s in sources),'shard_sha256':sha((BASE/'shard.json').read_bytes()),
         'pending':['independent fixed-sample source label audit by another reviewer','aggregate420 duplicate/connected-group audit','root-owned grouped dev/sealed-holdout split and final freeze']}
dump(BASE/'metadata/curation-summary.json',summary)
print(json.dumps(summary,ensure_ascii=False,indent=2))
