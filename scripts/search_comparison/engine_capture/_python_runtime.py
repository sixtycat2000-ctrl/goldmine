"""Fixed native numerical runtime observation; never imports Goldmine/models."""
import hashlib
import json
from pathlib import Path
import sys
import numpy

p = Path(numpy.__file__).resolve()
print(json.dumps({"python": str(Path(sys.executable).resolve()), "version": sys.version,
                  "numpy": {"path": str(p), "version": numpy.__version__, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}}, sort_keys=True))
