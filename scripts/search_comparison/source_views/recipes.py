"""A fixed local converter vocabulary, not an arbitrary command/plugin schema."""
from __future__ import annotations

from pathlib import Path
import os
import platform
import plistlib
import re
import sys

from answer_eval.protocol import EvaluationError, canonical_sha256, fields, parse_json, sha, sha256_file
from acceptance_lib.evidence import read_command_run
from .sources import read_bounded

SUITE = "comparison.source-views/v1"
PROFILES = {"doc": "utf8-full-nfc-lf/v1", "code": "utf8-full-nfc-lf/v1",
            "pdf": "poppler-text-or-vision-full/v1", "screenshot": "vision-full/v1",
            "audio": "whispercpp-full-no-vad/v1", "image": "jpeg-original-metadata/v1"}
TOOLS = {"python", "pdfinfo", "pdftotext", "pdftoppm", "ocr", "ffprobe", "ffmpeg", "whisper"}
MODELS = {"asr"}
LIMITS = {"command_timeout_s": 600, "max_pages": 5000, "max_duration_ms": 7200000,
          "max_output_bytes": 64 * 1024 * 1024}
PROBE_ARGS = {"python": ["--version"], "pdfinfo": ["-v"], "pdftotext": ["-v"],
              "pdftoppm": ["-v"], "ocr": ["--version"], "ffprobe": ["-version"],
              "ffmpeg": ["-version"], "whisper": ["--help"]}

# These flags are derived from the pinned existing Poppler/Vision/whisper.cpp
# clients. No seek, duration cap, remote URI, download or fallback command exists.
RECIPES = {
    "pdf.probe": ["{pdfinfo}", "{input}"],
    "pdf.text": ["{pdftotext}", "-f", "{page}", "-l", "{page}", "-enc", "UTF-8", "{input}", "-"],
    "pdf.raster": ["{pdftoppm}", "-f", "{page}", "-l", "{page}", "-r", "150", "-scale-to-x", "1400", "-scale-to-y", "-1", "-png", "{input}", "{prefix}"],
    "image.ocr": ["{python}", "-I", "-B", "{stdin_bridge}", "--input", "{stdin_file}", "--binary", "{ocr}"],
    "audio.probe": ["{ffprobe}", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", "{input}"],
    "audio.decode": ["{ffmpeg}", "-hide_banner", "-loglevel", "error", "-i", "{input}", "-ac", "1", "-ar", "16000", "-f", "wav", "-y", "{wav}"],
    "audio.asr": ["{whisper}", "-m", "{asr}", "-l", "auto", "-ojf", "-of", "{prefix}", "-np", "-f", "{wav}"],
}
ALLOWED_TOKENS = TOOLS | MODELS | {"input", "page", "prefix", "wav", "stdin_bridge", "stdin_file"}
TOKEN = re.compile(r"\{([a-z_]+)\}\Z")


def runtime_identity():
    """Pin host/runtime without loading Vision, audio, or a model."""
    product = None
    if platform.system() == "Darwin":
        path = Path("/System/Library/CoreServices/SystemVersion.plist")
        try:
            value = plistlib.loads(read_bounded(path, 64 * 1024))
            product = {key: value[key] for key in ("ProductName", "ProductVersion", "ProductBuildVersion")}
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise EvaluationError("cannot pin the macOS Vision runtime build", status="MISSING") from error
        if any(not isinstance(v, str) or not v for v in product.values()):
            raise EvaluationError("macOS product runtime identity is incomplete", status="MISSING")
    executable = Path(sys.executable).resolve()
    return {"system": platform.system(), "release": platform.release(), "kernel": platform.version(),
            "machine": platform.machine(), "macos_product": product, "python_version": sys.version,
            "python_executable": str(executable), "python_sha256": sha256_file(executable)}


def compile_recipe(name, values):
    if name not in RECIPES:
        raise EvaluationError("unknown fixed converter recipe", status="MISSING")
    result = []
    for arg in RECIPES[name]:
        if not isinstance(arg, str) or not arg or "\x00" in arg:
            raise EvaluationError("invalid fixed executable template")
        match = TOKEN.fullmatch(arg)
        if match:
            key = match[1]
            if key not in ALLOWED_TOKENS or key not in values:
                raise EvaluationError("unresolved/unknown fixed converter token", status="MISSING")
            value = values[key]
            if not isinstance(value, str) or not value or "\x00" in value:
                raise EvaluationError("converter token must resolve to one nonempty argv item", status="MISSING")
            result.append(value)
        elif "{" in arg or "}" in arg:
            raise EvaluationError("malformed/embedded executable placeholder", status="MISSING")
        else:
            result.append(arg)
    return result


def _external(ref, bundle_root, *, limit=None):
    fields(ref, {"path", "sha256"}, "explicit converter reference")
    sha(ref["sha256"], "explicit converter SHA")
    name = ref["path"]
    if not isinstance(name, str) or not Path(name).is_absolute() or "\x00" in name:
        raise EvaluationError("converter inputs must be explicit absolute local files", status="MISSING")
    path = Path(name).resolve()
    if path.is_relative_to(bundle_root) or not path.is_file():
        raise EvaluationError("converter resource cannot alias hidden bundle data or be absent", status="MISSING")
    if limit is not None and path.stat().st_size > limit:
        raise EvaluationError("oversized converter reference", status="MISSING")
    if sha256_file(path) != ref["sha256"]:
        raise EvaluationError("converter resource pin changed", status="MISSING")
    return path


def validate_config(path, bundle, implementation_sha256):
    path = Path(path).resolve()
    if path.is_relative_to(bundle.root):
        raise EvaluationError("conversion config must be outside hidden source bundle data")
    config = parse_json(read_bounded(path).decode("utf-8"), "converter config")
    fields(config, {"schema", "evidence_mode", "manifest_sha256", "implementation_sha256", "profiles",
                    "tools", "models", "limits", "offline", "fallback", "runtime"}, "converter config")
    mode = "fixture" if bundle.fixture else "real"
    if (type(config["schema"]) is not int or config["schema"] != 1 or config["evidence_mode"] != mode
            or config["manifest_sha256"] != bundle.identity["manifest_sha256"]
            or config["implementation_sha256"] != implementation_sha256
            or config["profiles"] != PROFILES or config["limits"] != LIMITS or config["runtime"] != runtime_identity()
            or config["offline"] is not True or config["fallback"] != "forbidden"):
        raise EvaluationError("unresolved or changed frozen converter profile/identity", status="MISSING")
    if not isinstance(config["tools"], dict) or set(config["tools"]) != TOOLS or not isinstance(config["models"], dict) or set(config["models"]) != MODELS:
        raise EvaluationError("every fixed tool/model role needs an explicit pin", status="MISSING")
    resources = {}
    for group in ("tools", "models"):
        for role, resource in config[group].items():
            required = {"path", "sha256", "bytes", "provenance", "license"}
            fields(resource, required | ({"probe"} if group == "tools" else set()), "converter resource")
            actual = _external({k: resource[k] for k in ("path", "sha256")}, bundle.root)
            if type(resource["bytes"]) is not int or resource["bytes"] <= 0 or actual.stat().st_size != resource["bytes"]:
                raise EvaluationError("converter resource byte count differs", status="MISSING")
            if role == "asr" and not bundle.fixture and (resource["bytes"] != 574041195 or resource["sha256"] != "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"):
                raise EvaluationError("real fixed ASR profile requires its pinned whisper model", status="MISSING")
            if group == "tools" and not os.access(actual, os.X_OK):
                raise EvaluationError("converter executable is unavailable", status="MISSING")
            _external(resource["provenance"], bundle.root, limit=8 * 1024 * 1024)
            _external(resource["license"], bundle.root, limit=8 * 1024 * 1024)
            if group == "tools" and not bundle.fixture:
                probe_ref = resource["probe"]
                fields(probe_ref, {"path", "sha256"}, "converter probe")
                probe = _external(probe_ref, bundle.root, limit=8 * 1024 * 1024)
                manifest, receipt, stdout = read_command_run(probe, check_id="comparison.converter.probe." + role, suite_id=SUITE)
                if (receipt["exit_code"] != 0 or receipt["timed_out"]
                        or receipt["argv"] != [str(actual), *PROBE_ARGS[role]]
                        or manifest["identity"] != {"role": role, "sha256": resource["sha256"]}):
                    raise EvaluationError("converter help/version receipt does not bind this fixed executable", status="MISSING")
                if not stdout.read_bytes() and not (probe.parent / receipt["stderr"]["path"]).read_bytes():
                    raise EvaluationError("converter probe has no raw version/help output", status="MISSING")
            elif group == "tools" and resource["probe"] is not None:
                raise EvaluationError("mechanical resources must explicitly mark probe=null")
            resources[role] = str(actual)
    # Validate the entire registry, including recipes not used by one fixture.
    dummy = {key: "literal" for key in ALLOWED_TOKENS}
    for name in RECIPES:
        compile_recipe(name, dummy)
    return config, resources, {"path": str(path), "sha256": sha256_file(path), "resources_sha256": canonical_sha256(config)}
