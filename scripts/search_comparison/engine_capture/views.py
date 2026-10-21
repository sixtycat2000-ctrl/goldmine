"""Same physical source views for every arm, including filesystem date fields."""
from __future__ import annotations

from pathlib import Path

from answer_eval.protocol import EvaluationError, canonical_sha256, fields, sha256_file
from product_eval.runner import check_build
from search_comparison.source_views.run import verify_capture
from .protocol import inventory, reference


def snapshot(view):
    root = Path(view["corpus"])
    actual = inventory(root)
    if set(actual) != {d["view_path"] for d in view["documents"]}:
        raise EvaluationError("view corpus contains missing/extra original identities", status="MISSING")
    files = {}
    for d in view["documents"]:
        path = root / d["view_path"]
        info = path.stat()
        if actual[d["view_path"]]["sha256"] != d["sha256"]:
            raise EvaluationError("view source bytes changed", status="MISSING")
        files[d["view_path"]] = {**actual[d["view_path"]], "mtime_ns": info.st_mtime_ns,
                                 "ctime_ns": info.st_ctime_ns, "birthtime": getattr(info, "st_birthtime", None),
                                 "device": info.st_dev, "inode": info.st_ino}
    return {"corpus": str(root), "files": files, "sha256": canonical_sha256(files)}


def load_views(value, bundle):
    fields(value, {"native", "common"}, "two registered comparison views")
    views, errors = {}, {}
    for name in ("native", "common"):
        try:
            if value[name] is None:
                raise EvaluationError("unresolved source view", status="MISSING")
            if name == "native":
                fields(value[name], {"build"}, "native source view")
                receipt = reference(value[name]["build"])
                corpus = check_build(bundle, receipt.parent)
                documents = [{"source_path": s["path"], "view_path": s["path"], "sha256": s["asset"]["sha256"]}
                             for s in bundle.sources.values()]
                binding = {"build": value[name]["build"]}
            else:
                fields(value[name], {"report", "config"}, "full-source common view")
                report_path, config_path = (reference(value[name][key]) for key in ("report", "config"))
                report = verify_capture(report_path, bundle.root, bundle.identity["manifest_sha256"], config_path, fixture=bundle.fixture)
                expected = "MECHANICAL_CAPTURE_COMPLETE" if bundle.fixture else "CAPTURED_FULL_SOURCES"
                if report["status"] != expected:
                    raise EvaluationError("common view lacks actual complete all-source conversion", status="MISSING")
                corpus = report_path.parent / "common/corpus"
                documents = [{"source_path": row["source_path"], "view_path": row["common_path"], "sha256": row["common_text_sha256"]}
                             for row in report["sources"]]
                binding = value[name]
            if len(documents) != len(bundle.sources) or {d["source_path"] for d in documents} != set(bundle.sources):
                raise EvaluationError("view cannot reduce original source denominator")
            views[name] = {"name": name, "corpus": str(corpus.resolve()), "documents": documents, "binding": binding}
            views[name]["identity"] = snapshot(views[name])
        except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
            errors[name] = str(exc)
    return views, errors


def verify_view(view):
    if snapshot(view) != view["identity"]:
        raise EvaluationError("physical view bytes/dates/inodes changed during engine capture", status="MISSING")
