"""Bind production's actual native resolvers instead of inferring them from PATH."""
from pathlib import Path

from answer_eval.protocol import EvaluationError, fields, parse_json


def verify(value, engine, paths, configuration):
    fields(value, {"schema", "scope", "installation", "configuration", "server_source", "modules", "tools"}, "effective native readback")
    expected_roles = set(engine["tools"])
    modules = {"goldmine": "__init__.py", "goldmine.config": "config.py", "goldmine.ocr": "ocr.py", "goldmine.backends.llama": "backends/llama.py",
               "goldmine.ingest.asr": "ingest/asr.py", "goldmine.ingest.exif": "ingest/exif.py", "goldmine.ingest.extract_pdf": "ingest/extract_pdf.py", "goldmine.ingest.media": "ingest/media.py"}
    if (value["schema"] != 1 or value["scope"] != "source-only-effective-resolvers-no-tool-or-model-execution"
            or value["installation"] != paths["installation"] or value["configuration"] != configuration
            or value["server_source"] != "env/config"
            or value["modules"] != {k: str(Path(paths["installation"]) / "goldmine" / relative) for k, relative in modules.items()}
            or not isinstance(value["tools"], dict) or set(value["tools"]) != expected_roles):
        raise EvaluationError("actual native resolver source/config/role identity differs", status="MISSING")
    for role in sorted(expected_roles):
        row = value["tools"][role]
        if (not isinstance(row, dict) or set(row) != {"selected", "resolved", "sha256", "bytes"}
                or not isinstance(row["selected"], str) or not Path(row["selected"]).is_absolute()
                or str(Path(row["selected"]).resolve()) != paths[role]
                or row["resolved"] != paths[role] or row["sha256"] != engine["tools"][role]["sha256"]
                or type(row["bytes"]) is not int or row["bytes"] != engine["tools"][role]["bytes"]):
            raise EvaluationError("effective native tool does not equal its registered path/SHA/bytes: " + role, status="MISSING")
    return value


def readback(commands, values, engine, *, after=False):
    argv = [values["python"], "-I", "-B", str(Path(__file__).with_name("_native_runtime.py")), values["installation"], values["goldmine_config"]]
    raw = commands.call("goldmine.native-runtime-after" if after else "goldmine.native-runtime-before", argv, phase="audit" if after else "prepare")
    return verify(parse_json(raw.decode("utf-8"), "effective native resolver stdout"), engine, values, values["goldmine_config"])
