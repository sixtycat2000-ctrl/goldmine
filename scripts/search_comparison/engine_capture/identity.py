"""Actual local implementation closure, including eager source dependencies."""
from pathlib import Path

from acceptance_lib.resource_policy import MODULES
from answer_eval.protocol import EvaluationError, canonical_sha256, sha256_file
from search_comparison.source_views.run import implementation_identity as source_implementation


def implementation():
    root = Path(__file__).resolve().parents[3]
    files = dict(source_implementation()["files"])
    for name in MODULES:
        path = root / "scripts/acceptance_lib" / name
        if path.is_symlink():
            raise EvaluationError("observer implementation contains a source alias", status="MISSING")
        files[path.relative_to(root).as_posix()] = sha256_file(path)
    for path in sorted((root / "scripts/search_comparison").rglob("*")):
        if path.is_symlink():
            raise EvaluationError("comparison implementation contains a source alias", status="MISSING")
        if path.is_file() and path.suffix in (".py", ".mjs"):
            files[path.relative_to(root).as_posix()] = sha256_file(path)
    files["scripts/capture_comparison_engines.py"] = sha256_file(root / "scripts/capture_comparison_engines.py")
    return {"files": files, "sha256": canonical_sha256(files)}
