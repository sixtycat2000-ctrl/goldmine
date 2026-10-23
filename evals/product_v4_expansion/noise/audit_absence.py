"""Inventory/search frozen source VIEWS only; no rankings or model calls.

Zero literal hits alone are never treated as an absence proof. Per-question
source-first reasons remain in absence-notes.json and require reviewer judgment.
An inventory receipt binds that judgment to exactly the checked source set.
"""
from pathlib import Path
import argparse, datetime, hashlib, json, sys

BASE = Path(__file__).resolve().parent

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

parser = argparse.ArgumentParser()
parser.add_argument('--manifest', type=Path, help='Complete aggregate manifest; view refs resolve at its parent')
parser.add_argument('--shards', type=Path, nargs='+', help='Current shard manifests; partial review only')
args = parser.parse_args()
if bool(args.manifest) == bool(args.shards):
    parser.error('provide either --manifest or --shards')
inputs = [args.manifest] if args.manifest else args.shards
inventory, views, seen = [], [], set()
for input_path in inputs:
    document = json.loads(input_path.read_text())
    for source in document['sources']:
        for view in source['views']:
            path = (input_path.parent / view['path']).resolve()
            raw = path.read_bytes()
            if sha(raw) != view['sha256']:
                raise SystemExit(f'view SHA changed: {path}')
            identity = (source['path'], view['id'], view['sha256'])
            if identity in seen:
                continue
            seen.add(identity)
            text = raw.decode('utf-8')
            inventory.append({'source': source['path'], 'view_id': view['id'], 'sha256': view['sha256'],
                              'evidence_type': view['evidence_type'], 'view_file': str(path), 'characters': len(text)})
            views.append((source['path'], view['id'], text))
queries = json.loads((BASE/'shard.json').read_text())['queries']
notes = {note['id']: note for note in json.loads((BASE/'absence-notes.json').read_text())['notes']}
reviews = []
for question in queries:
    note = notes[question['id']]
    hits = []
    for needle in note['discriminators']:
        for source, view_id, text in views:
            folded, target = text.casefold(), needle.casefold()
            # Source text is not rewritten and offsets here are search-context
            # diagnostics only; casefold can alter character count. They never
            # become evidence labels or source span proofs.
            cursor, count, contexts = 0, 0, []
            while (at := folded.find(target, cursor)) >= 0:
                count += 1
                if len(contexts) < 3:
                    contexts.append(folded[max(0, at-100):at+len(target)+140])
                cursor = at+max(1, len(target))
            if count:
                hits.append({'discriminator': needle, 'source': source, 'view_id': view_id,
                             'literal_matches': count, 'casefolded_contexts': contexts})
    reviews.append({'id': question['id'], 'subtype': question['tags'][0],
                    'question_sha256': sha(question['q'].encode()),
                    'source_first_absence_reason': note['source_first_absence_reason'], 'literal_discriminator_hits': hits})
inventory.sort(key=lambda v:(v['source'], v['view_id']))
encoded=json.dumps(inventory,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
receipt={'schema':1,'status':'COMPLETE_INVENTORY_PENDING_SOURCE_REASON_REVIEW' if args.manifest else 'PARTIAL_INVENTORY_ONLY',
         'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
         'curator':'OpenAI Codex AI curator; no human review claimed',
         'method':'Literal discriminator search in every registered source view plus explicit source-first scope reasons. No hit-count rule proves absence.',
         'rank_exposure':'NONE','manifest_inputs':[{'path':str(p.resolve()),'sha256':sha(p.read_bytes())} for p in inputs],
         'noise_shard_sha256':sha((BASE/'shard.json').read_bytes()),
         'source_files':len({v['source'] for v in inventory}),'source_views':len(inventory),'inventory_sha256':sha(encoded),
         'inventory':inventory,'reviews':reviews}
(BASE/'absence-inventory.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({key:receipt[key] for key in ('status','source_files','source_views','inventory_sha256','noise_shard_sha256')},indent=2))
for review in reviews:
    if review['literal_discriminator_hits']:
        print(json.dumps({'id':review['id'],'hits':review['literal_discriminator_hits']},ensure_ascii=False))
