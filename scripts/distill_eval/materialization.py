"""Materialize only frozen document assets; labels/licenses never enter the corpus."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import shutil
import stat

from acceptance_lib.evidence import InvalidEvidence, external_ref, read_json, sha256, verify_artifact
from product_eval.materialization import POLICY, expected_flags, materialize, validate


def sources_for(labels_path, labels_data):
    manifest_path = external_ref(Path(labels_path).parent, labels_data["product_manifest"], manifest=True)
    manifest = read_json(manifest_path)
    rows = {row["path"]: row for row in manifest["sources"]}
    names = {row["path"] for row in labels_data["documents"]}
    if names != set(rows):
        raise InvalidEvidence("distill source manifest must contain exactly the labelled document registry")
    for name in names:
        if str(PurePosixPath(name)) != name or "\\" in name:
            raise InvalidEvidence("source paths must be canonical portable relative names")
    return manifest_path, rows


def check_corpus(corpus, sources, receipt):
    corpus = Path(corpus)
    if corpus.is_symlink() or not corpus.is_dir():
        raise InvalidEvidence("distill corpus must be an actual source directory")
    parents = {parent.as_posix() for name in sources for parent in PurePosixPath(name).parents
               if parent.as_posix() != "."}
    actual = set()
    for node in corpus.rglob("*"):
        name, mode = node.relative_to(corpus).as_posix(), node.lstat().st_mode
        if stat.S_ISREG(mode):
            actual.add(name)
        elif not stat.S_ISDIR(mode) or name not in parents:
            raise InvalidEvidence("unregistered/symlinked/special corpus node")
    if actual != set(sources):
        raise InvalidEvidence("corpus contains missing or extra source files (labels must remain outside)")
    expected = {name: row["asset"]["sha256"] for name, row in sources.items()}
    if set(receipt) != {"schema", "files", "materialization"} or type(receipt["schema"]) is not int or receipt["schema"] != 1 or receipt["files"] != expected:
        raise InvalidEvidence("distill build receipt differs from frozen document bytes")
    for name, digest in expected.items():
        if sha256(corpus / name) != digest:
            raise InvalidEvidence("materialized source bytes changed")
    validate(corpus, sources, receipt["materialization"])


def build_receipt(sources):
    flags = expected_flags(sources)
    return {"schema": 1, "files": {name: row["asset"]["sha256"] for name, row in sources.items()},
            "materialization": {"policy": POLICY, "expected": flags, "observed": flags}}


def add_sources(corpus, manifest_root, additions, all_sources):
    corpus = Path(corpus)
    corpus.mkdir(parents=True, exist_ok=True)
    for name, row in additions.items():
        target = corpus / name
        if target.exists() or target.is_symlink():
            raise InvalidEvidence("refuse to replace an existing materialized source")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(verify_artifact(Path(manifest_root), row["asset"]), target)
        materialize(target, row)
    receipt = build_receipt(all_sources)
    # A receipt is issued only after native flag read-back and exact registry checks.
    check_corpus(corpus, all_sources, receipt)
    return receipt
