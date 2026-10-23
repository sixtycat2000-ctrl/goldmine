"""Reuse audited source validation without opening any query/split/audit file."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import unicodedata

from answer_eval.protocol import EvaluationError, canonical_sha256, fields, parse_json, sha, sha256_file
from product_eval.bundle import _validate_source, relative_path, REFERENCE_COMMIT

REAL_MANIFEST = "b8351272194fed76e438d30d452a3fff9393f99a7e5b1264f710490cc65677b0"
REAL_KINDS = {"doc": 41, "code": 19, "pdf": 12, "screenshot": 20, "image": 10, "audio": 20}
MAX_SOURCE_BYTES = 512 * 1024 * 1024


def read_bounded(path, limit=8 * 1024 * 1024):
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise EvaluationError("input exceeds fixed byte bound", status="MISSING")
    return raw


def source_path(root, name, prefix):
    relative_path(name, "source reference")
    target = root / name
    if target.relative_to(root).parts[0] != prefix:
        raise EvaluationError("source reference has the wrong fixed directory")
    for path in [target, *target.parents]:
        if path == root:
            break
        if path.is_symlink():
            raise EvaluationError("source reference contains a symlink")
    if not target.resolve().is_relative_to(root):
        raise EvaluationError("source reference escapes bundle")
    return target


def checked_source_ref(root, ref, prefix, *, limit):
    fields(ref, {"path", "sha256"}, "source reference")
    sha(ref["sha256"], "source reference SHA")
    path = source_path(root, ref["path"], prefix)
    if not path.is_file() or path.stat().st_size > limit or sha256_file(path) != ref["sha256"]:
        raise EvaluationError("missing/changed/oversized source reference", status="MISSING")
    return path


def asset_inventory(root):
    files = set()
    for path in (root / "assets").rglob("*"):
        if path.is_symlink():
            raise EvaluationError("source asset inventory contains a symlink")
        if path.is_file():
            files.add(path.relative_to(root).as_posix())
    return files


@dataclass(frozen=True)
class SourceBundle:
    root: Path
    manifest: dict
    sources: dict[str, dict]
    identity: dict
    source_files: dict[str, str]
    provenance_files: dict[str, str]
    fixture: bool

    @property
    def missing(self):
        return []  # Native copying consumes source completeness, never labels.

    def asset(self, source):
        return checked_source_ref(self.root, source["asset"], "assets", limit=MAX_SOURCE_BYTES)

    def verify(self):
        if sha256_file(self.root / "manifest.json") != self.identity["manifest_sha256"]:
            raise EvaluationError("source manifest changed during capture", status="MISSING")
        for source in self.sources.values():
            self.asset(source)
        if asset_inventory(self.root) != set(self.source_files):
            raise EvaluationError("source asset inventory changed", status="MISSING")
        for name, digest in self.provenance_files.items():
            path = source_path(self.root, name, name.split("/")[0])
            if not path.is_file() or sha256_file(path) != digest:
                raise EvaluationError("source provenance changed during capture", status="MISSING")


def load_sources(path, manifest_sha256, *, fixture=False):
    supplied = Path(path).absolute()
    if supplied.is_symlink():
        raise EvaluationError("bundle root must not be a symlink")
    root = supplied.resolve()
    if (root / "manifest.json").is_symlink():
        raise EvaluationError("source manifest must not be a symlink")
    sha(manifest_sha256, "manifest SHA")
    if sha256_file(root / "manifest.json") != manifest_sha256:
        raise EvaluationError("source manifest pin mismatch", status="MISSING")
    m = parse_json(read_bounded(root / "manifest.json").decode("utf-8"), "source manifest")
    fields(m, {"schema", "suite_id", "state", "evidence_mode", "reference_source_commit", "reference_environment",
               "sources", "queries", "splits", "label_audit"}, "source-only manifest")
    if type(m["schema"]) is not int or m["schema"] != 1 or m["suite_id"] != "product.v4" or m["state"] != "FROZEN":
        raise EvaluationError("require frozen product.v4 source manifest", status="MISSING")
    if m["evidence_mode"] != ("fixture" if fixture else "real"):
        raise EvaluationError("real and mechanical source modes cannot be interchanged", status="MISSING")
    if m["reference_source_commit"] != REFERENCE_COMMIT:
        raise EvaluationError("original reference identity changed")
    if not isinstance(m["sources"], list) or not m["sources"]:
        raise EvaluationError("source list is empty/invalid")
    if not fixture and (manifest_sha256 != REAL_MANIFEST or len(m["sources"]) != 122):
        raise EvaluationError("real comparison requires the exact frozen122-source bundle", status="MISSING")
    # Validate opaque label refs as metadata only. No reference()/read of these.
    for ref in [m["queries"], m["splits"], m["label_audit"], m["reference_environment"]]:
        fields(ref, {"path", "sha256"}, "opaque non-source reference")
        relative_path(ref["path"], "opaque reference path")
        sha(ref["sha256"], "opaque reference SHA")
    sources, names, assets, provenance = {}, set(), {}, {}
    for source in m["sources"]:
        fields(source, {"path", "asset", "kind", "source_group", "parents", "provenance", "views"}, "source")
        relative_path(source["path"], "source path")
        portable = unicodedata.normalize("NFKC", source["path"]).casefold()
        if portable in names:
            raise EvaluationError("source names collide on portable filesystems")
        names.add(portable)
        checked_source_ref(root, source["asset"], "assets", limit=MAX_SOURCE_BYTES)
        if source["asset"]["path"] in assets:
            raise EvaluationError("two sources alias the same source asset")
        assets[source["asset"]["path"]] = source["asset"]["sha256"]
        refs = [(source["provenance"]["license_file"], "licenses")]
        refs.extend(({"path": v["path"], "sha256": v["sha256"]}, "views") for v in source["views"])
        for ref, prefix in refs:
            checked_source_ref(root, ref, prefix, limit=8 * 1024 * 1024)
            provenance[ref["path"]] = ref["sha256"]
        verified_views = {}
        _validate_source(root, source, verified_views)
        if not fixture and source["provenance"]["type"] != "real":
            raise EvaluationError("synthetic source cannot enter a real comparison")
        sources[source["path"]] = source
    if any(parent.as_posix() in names for name in names for parent in Path(name).parents if parent.as_posix() != "."):
        raise EvaluationError("source name is both a file and parent directory")
    if any(parent not in sources for s in sources.values() for parent in s["parents"]):
        raise EvaluationError("source has an unregistered related parent")
    if not fixture and dict(Counter(s["kind"] for s in sources.values())) != REAL_KINDS:
        raise EvaluationError("frozen source-kind denominator changed")
    if asset_inventory(root) != set(assets):
        raise EvaluationError("source asset inventory has omitted/extra files", status="MISSING")
    identity = {"manifest_sha256": manifest_sha256, "queries_sha256": m["queries"]["sha256"],
                "corpus_sha256": canonical_sha256(m["sources"]), "splits_sha256": m["splits"]["sha256"],
                "reference_source_commit": REFERENCE_COMMIT}
    return SourceBundle(root, m, sources, identity, assets, provenance, fixture)
