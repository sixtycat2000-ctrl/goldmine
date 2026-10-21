"""Isolated exact-original classification probe; no extraction/model calls.

Invoked by reference_support.py with Python -I. Never accepts an unsupported
list: it walks and classifies every registered immutable source itself.
"""
from pathlib import Path
import hashlib
import importlib
import json
import sys


def main():
    original, corpus, numpy_root = (Path(value).resolve() for value in sys.argv[1:])
    payload = json.loads(sys.stdin.read())
    sys.path.insert(0, str(original))
    # -I excludes user site-packages. Permit only the explicitly observed
    # active numpy installation required by original ingest/store imports.
    sys.path.append(str(numpy_root))
    rules = importlib.import_module('goldmine.ingest.rules')
    walk = importlib.import_module('goldmine.ingest.walk')
    storage = importlib.import_module('goldmine.store')
    from goldmine.config import Config
    import numpy as np

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    modules = {}
    for module in (rules, walk, storage):
        path = Path(module.__file__).resolve()
        relative = path.relative_to(original).as_posix()
        modules[relative] = sha(path)
    # Product prepare exposes no ingest size/exclusion override. Derive the
    # same actual original Config default here, not from receipt assertions.
    max_bytes = int(Config(home=corpus.parent/'.product-eval-type-probe-home').ingest.max_file_mb) * (1 << 20)
    walked = {str(item.path): item for item in walk.walk_files([corpus], code=True, exclude=[], max_file_bytes=max_bytes)}
    observations = []
    for relative in payload['paths']:
        path = (corpus/relative).resolve()
        if not path.is_relative_to(corpus) or not path.is_file() or path.is_symlink():
            raise ValueError('registered source escapes corpus or is not a regular file')
        decision = walk._decide_file(path, path.name, code=True)
        item = walked.get(str(path))
        observations.append({'path':relative,'asset_sha256':sha(path),
                             'classifier':{'kind':decision.kind,'skip_reason':decision.skip_reason},
                             'name_metadata':{'name':storage._display_name(str(path)),
                                              'body':storage._folder_words(str(path),'file')},
                             'walker':{'type':type(item).__name__ if item is not None else None,
                                       'kind':getattr(item,'kind',None),'reason':getattr(item,'reason',None),
                                       'is_dir':getattr(item,'is_dir',False)}})
    print(json.dumps({'module_files':modules,'runtime_numpy':{'version':np.__version__,'init_sha256':sha(Path(np.__file__))},
                      'walker_settings':{'code':True,'exclude':[],'max_file_bytes':max_bytes},
                      'observations':observations},ensure_ascii=False,sort_keys=True))


if __name__ == '__main__':
    main()
