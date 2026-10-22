"""Private-production-first source-only OCR probe. Never compiles/runs a helper."""
import hashlib
import json
from pathlib import Path
import sys

if len(sys.argv) != 4:
    raise SystemExit("fixed request path/SHA and installation required")
request, pin, installation = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
if request.is_symlink() or request.resolve() != request or hashlib.sha256(request.read_bytes()).hexdigest() != pin:
    raise SystemExit("request bytes/path changed")
value = json.loads(request.read_bytes())
owner = Path(value["owner"])
missing = owner / "absent-dependency/not-provisioned"
if (value["scenario"] != "goldmine-ocr-absent" or value["installation"] != str(installation)
        or owner.resolve() != owner or not (owner / "cell-ownership.json").is_file() or missing.exists()):
    raise SystemExit("unregistered missing-OCR request")
sys.path.insert(0, str(installation))
import goldmine
from goldmine import config, ocr
if any(not Path(m.__file__).resolve().is_relative_to(installation / "goldmine") for m in (goldmine, config, ocr)):
    raise SystemExit("private source selection imported a shadow package")
helper, reason = ocr.ensure_helper(config.load(Path(value["configuration"])), autocompile=False)
if helper is not None or not reason:
    raise SystemExit("absent helper did not return explicit unavailability")
print(json.dumps({"status": "EXPECTED_MISSING", "role": "ocr", "path": str(missing), "reason": reason,
                  "autocompile": False, "modules": {m.__name__: str(Path(m.__file__).resolve()) for m in (goldmine, config, ocr)}}))
