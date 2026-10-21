"""Fixed source-only Goldmine effective resolver readback. Never runs a tool/model."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

if len(sys.argv) != 3 or any(not Path(p).is_absolute() for p in sys.argv[1:]):
    raise SystemExit("fixed absolute installation and config arguments required")
installation, config = (Path(p).resolve() for p in sys.argv[1:])
sys.path.insert(0, str(installation))
import goldmine
from goldmine import config as configuration, ocr
from goldmine.backends import llama
from goldmine.ingest import asr, exif, extract_pdf, media

modules = (goldmine, configuration, ocr, llama, asr, exif, extract_pdf, media)
if any(not Path(m.__file__).resolve().is_relative_to(installation / "goldmine") for m in modules):
    raise SystemExit("native readback imported a shadow production package")
cfg = configuration.load(config)
server, server_source = llama.resolve_server_path(cfg.llama)
selected = {"python": sys.executable, "server": server, "ocr": ocr.helper_path(cfg), "whisper": asr.resolve_whisper_cli(cfg),
            "ffmpeg": media.ffmpeg_path(), "ffprobe": media.ffprobe_path(), "pdfinfo": extract_pdf.pdfinfo_path(),
            "pdftotext": extract_pdf.pdftotext_path(), "pdftoppm": extract_pdf.pdftoppm_path(),
            "sips": media.sips_path(), "imageio": shutil.which(exif.IMAGEIO_EXECUTABLE)}
tools = {}
for role, path in selected.items():
    if path is None:
        tools[role] = None
        continue
    selected_path = Path(path).absolute()
    actual = selected_path.resolve()
    digest = hashlib.sha256()
    with actual.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    tools[role] = {"selected": str(selected_path), "resolved": str(actual), "sha256": digest.hexdigest(), "bytes": actual.stat().st_size}
print(json.dumps({"schema": 1, "scope": "source-only-effective-resolvers-no-tool-or-model-execution", "installation": str(installation),
                  "configuration": str(config), "server_source": server_source, "modules": {m.__name__: str(Path(m.__file__).resolve()) for m in modules},
                  "tools": tools}, ensure_ascii=False))
