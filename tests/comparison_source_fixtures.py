"""Declared synthetic bytes and converter executables; never product-quality data."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

from answer_eval.protocol import sha256_file
from product_eval import materialization
from product_eval.bundle import REFERENCE_COMMIT, normalized
from search_comparison.source_views.recipes import PROFILES, TOOLS, LIMITS, runtime_identity
from search_comparison.source_views.run import implementation_identity


STUB = '''#!PYTHON
import json,pathlib,sys,wave
role=pathlib.Path(sys.argv[0]).name.removeprefix("fixture-")
args=sys.argv[1:]
def val(key):return args[args.index(key)+1]
if role=='pdfinfo':
 print('Pages: 2')
elif role=='pdftotext':
 page=int(val('-f'));data=pathlib.Path(args[-2]).read_bytes()
 if page==2 and b'FAIL_TEXT_2' in data:sys.exit(3)
 print('FULL FIRST PAGE\\n\\f' if page==1 else '\\f',end='')
elif role=='pdftoppm':
 pathlib.Path(args[-1]+'-'+val('-f')+'.png').write_bytes(b'SYNTHETIC_FULL_PAGE_RASTER')
elif role=='ocr':
 item=json.loads(sys.stdin.readline())
 print(json.dumps({**item,'texts':[{'text':'FULL IMAGE OCR NOT AN ORACLE','confidence':.1,'box':[0.,0.,1.,1.]}],'ms':1.}))
elif role=='ffprobe':
 print('1.000')
elif role=='ffmpeg':
 data=pathlib.Path(val('-i')).read_bytes()
 with wave.open(args[-1],'wb') as out:
  out.setnchannels(1);out.setframerate(16000);out.setsampwidth(2)
  out.writeframes(b'\\0\\0'*(8000 if b'SHORT_DECODE' in data else 16000))
elif role=='whisper':
 pathlib.Path(val('-of')+'.json').write_text(json.dumps({'transcription':[{'text':'WHOLE CLIP FIRST','offsets':{'from':0,'to':500}},{'text':'WHOLE CLIP SECOND','offsets':{'from':500,'to':1000}}]}))
else:sys.exit(9)
'''


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def ref(path):
    return {"path": str(path.resolve()), "sha256": sha256_file(path)}


def make_fixture(root, *, pdf_suffix=b"", audio_suffix=b""):
    root.mkdir()
    bundle = root / "bundle"
    bundle.mkdir()
    license_file = bundle / "licenses/MIT.txt"
    license_file.parent.mkdir()
    license_file.write_text("MIT-licensed tiny synthetic test fixtures; no actual media/model claim.\n")
    bodies = [("docs/a.md", "doc", "Full original e\u0301 and 中文.\r\n".encode()),
              ("code/a.py", "code", b'print("complete original source")\n'),
              ("pdf/a.pdf", "pdf", b"SYNTHETIC_PDF_TWO_PAGES" + pdf_suffix),
              ("screens/cap.png", "screenshot", b"SYNTHETIC_SCREENSHOT_NOT_REAL_PIXELS"),
              ("photos/p.jpg", "image", b"\xff\xd8\xff\xd9"),
              ("audio/a.flac", "audio", b"SYNTHETIC_CONTROLLED_AUDIO" + audio_suffix)]
    sources = []
    for name, kind, body in bodies:
        asset = bundle / "assets" / name
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(body)
        is_text = kind in ("doc", "code")
        text = normalized(body.decode()) if is_text else Path(name).name if kind == "image" else "ANNOTATED_ONLY_NOT_FULL_SOURCE"
        evidence_type = "content_text" if is_text or kind == "pdf" else "name_metadata" if kind == "image" else "asr_text" if kind == "audio" else "ocr_text"
        location = None if kind == "image" else {"unit": "page" if kind == "pdf" else "second" if kind == "audio" else "line", "start": 2 if kind == "pdf" else 0 if kind == "audio" else 1, "end": 2 if kind == "pdf" else 1}
        view = bundle / "views" / (name + ".txt")
        view.parent.mkdir(parents=True, exist_ok=True)
        view.write_text(text)
        sources.append({"path": name, "kind": kind, "asset": {"path": asset.relative_to(bundle).as_posix(), "sha256": sha256_file(asset)},
            "source_group": "synthetic:" + kind, "parents": [], "provenance": {"type": "synthetic_canary", "origin": {"kind": "generator", "id": "source-view-control-stub", "revision": "1"},
                "license": "MIT synthetic fixture", "license_file": {"path": "licenses/MIT.txt", "sha256": sha256_file(license_file)},
                "generation": "Tiny declared controlled synthetic bytes; not actual PDF/image/audio/model evidence."},
            "views": [{"id": "independent-fixture-view", "path": view.relative_to(bundle).as_posix(), "sha256": sha256_file(view),
                       "evidence_type": evidence_type, "location": location, "storage_field": "basename" if kind == "image" else None, "normalization": "nfc-lf-v1"}]})
    opaque = lambda name: {"path": name, "sha256": "0" * 64}
    manifest = {"schema": 1, "suite_id": "product.v4", "state": "FROZEN", "evidence_mode": "fixture", "reference_source_commit": REFERENCE_COMMIT,
                "reference_environment": opaque("NEVER-OPEN-environment.json"), "sources": sources, "queries": opaque("NEVER-OPEN-queries.jsonl"),
                "splits": opaque("NEVER-OPEN-splits.json"), "label_audit": opaque("NEVER-OPEN-label-audit.json")}
    dump(bundle / "manifest.json", manifest)
    resources = root / "resources"
    resources.mkdir()
    provenance = resources / "fixture-provenance.json"
    dump(provenance, {"evidence_mode": "fixture", "status": "CONTROLLED_STUB_NOT_OFFICIAL_TOOL", "models_or_real_media_loaded": False})
    fixture_license = resources / "license.txt"
    fixture_license.write_text("Synthetic test fixture license marker; not a real software/model provenance claim.\n")
    tools = {}
    for role in TOOLS:
        path = Path(sys.executable).resolve() if role == "python" else resources / ("fixture-" + role)
        if role != "python":
            path.write_text(STUB.replace("#!PYTHON", "#!" + sys.executable))
            path.chmod(0o755)
        tools[role] = {**ref(path), "bytes": path.stat().st_size, "provenance": ref(provenance), "license": ref(fixture_license), "probe": None}
    model = resources / "not-a-real-model.bin"
    model.write_bytes(b"CONTROLLED_TEST_MODEL_NO_INFERENCE")
    config = {"schema": 1, "evidence_mode": "fixture", "manifest_sha256": sha256_file(bundle / "manifest.json"),
              "implementation_sha256": implementation_identity()["sha256"], "profiles": PROFILES,
              "tools": tools, "models": {"asr": {**ref(model), "bytes": model.stat().st_size, "provenance": ref(provenance), "license": ref(fixture_license)}},
              "limits": LIMITS, "offline": True, "fallback": "forbidden", "runtime": runtime_identity()}
    config_path = root / "config.json"
    dump(config_path, config)
    return {"root": root, "bundle": bundle, "pin": config["manifest_sha256"], "config": config_path, "sources": sources}


@contextmanager
def synthetic_native_policy():
    """Simulate only the reviewed metadata API; do not call macOS ctypes/xattr."""
    flags = {}
    def apply(path, source):
        flags[Path(path).name] = materialization.CAPTURE_VALUE.hex() if source["kind"] == "screenshot" else None
        return flags[Path(path).name]
    def read(path):
        return flags.get(Path(path).name)
    with patch.object(materialization, "materialize", side_effect=apply), patch.object(materialization, "read_flag", side_effect=read), \
         patch("search_comparison.source_views.convert.jpeg_fields", return_value={"DateTimeOriginal": None, "OffsetTimeOriginal": None, "capture_status": "not_established", "reader": "synthetic-only-no-native"}):
        yield flags
