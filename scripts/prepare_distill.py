#!/usr/bin/env python3
"""Prepare one real native base index, then extend that index by the frozen10 files."""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import shutil
import sqlite3
import sys

from acceptance_lib.evidence import (InvalidEvidence, artifact, capture_command, read_command_run,
                                     sha256, write_json)
from distill_eval.identity import assert_extension, evaluator_identity, index_identity
from distill_eval.labels import load_labels
from distill_eval.materialization import add_sources, check_corpus, sources_for
from distill_eval.preparation import SUITE, command_argv, command_env, validate_index, validate_preparation
from distill_eval.runner import copy_index


def prepare(labels_path, out, model, server, ocr):
    root = Path(__file__).resolve().parents[1]
    out, labels_path = Path(out).resolve(), Path(labels_path).resolve()
    corpus = out / "corpus"
    labels_data, _ = load_labels(labels_path, corpus)  # Refuse DRAFT before any native/model work.
    manifest_path, sources = sources_for(labels_path, labels_data)
    base_names = {row["path"] for row in labels_data["documents"] if row["split"] == "base"}
    base_sources = {name: source for name, source in sources.items() if name in base_names}
    assets = {name: {"path": str(Path(path).absolute() if name == "python" else Path(path).resolve()),
                     "sha256": sha256(Path(path))}
              for name, path in (("python", sys.executable), ("model", model), ("server", server), ("ocr", ocr))}
    if any(not os.access(assets[name]["path"], os.X_OK) for name in ("python", "server", "ocr")):
        raise InvalidEvidence("explicit native/Python executables are missing or not executable")
    source_files = evaluator_identity(root)
    out.mkdir(parents=True, exist_ok=False)
    config = out / "config.toml"
    config.write_bytes(b"")
    helper = out / "state/bin/goldmine-ocr"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(assets["ocr"]["path"], helper)
    helper.chmod(0o755)
    work_index = out / "state/index.sqlite"
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("GOLDMINE_", "PYTHON")) and key not in ("PIP_NO_INDEX", "PIP_FIND_LINKS")}
    environment.update(command_env(root, out, assets))
    prep = {"schema": 1, "kind": "goldmine.distill-preparation", "status": "PREPARED", "source_files": source_files,
            "assets": assets, "labels": {"path": str(labels_path), "sha256": sha256(labels_path)},
            "source_manifest": {"path": str(manifest_path), "sha256": sha256(manifest_path)},
            "corpus_root": str(corpus), "working_index": str(work_index), "config": artifact(out, config),
            "builds": {}, "commands": {}, "indexes": {}, "identities": {}}
    snapshots = {}
    for phase, additions, registry in (("base", base_sources, base_sources),
                                        ("incremental", {name: row for name, row in sources.items() if name not in base_names}, sources)):
        if phase == "incremental":
            check_corpus(corpus, base_sources, build)
        build = add_sources(corpus, manifest_path.parent, additions, registry)
        build_path = out / f"build-{phase}.json"
        write_json(build_path, build)
        prep["builds"][phase] = artifact(out, build_path)
        command_identity = {"source_files": source_files, "assets": assets, "labels_sha256": prep["labels"]["sha256"],
                            "source_manifest_sha256": prep["source_manifest"]["sha256"], "build_sha256": sha256(build_path)}
        command_dir = out / "commands" / phase
        capture_command(command_dir, f"distill.prepare.{phase}",
                        command_argv(assets["python"]["path"], work_index, corpus), cwd=out, env=environment,
                        identity=command_identity, suite_id=SUITE, timeout=3600, owned_process_group=True)
        manifest = command_dir / "manifest.json"
        _, receipt, _ = read_command_run(manifest)
        if receipt["exit_code"] != 0 or receipt["timed_out"]:
            raise InvalidEvidence(f"native {phase} ingest incomplete; retain raw receipts; no PREPARED claim")
        target = out / f"{phase}.sqlite"
        copy_index(work_index, target)
        snapshots[phase] = index_identity(target)
        validate_index(snapshots[phase], registry, corpus, assets["model"]["sha256"])
        identity_path = out / f"identity-{phase}.json"
        write_json(identity_path, snapshots[phase])
        prep["commands"][phase] = {"manifest_path": str(manifest.relative_to(out)), "sha256": sha256(manifest)}
        prep["indexes"][phase] = artifact(out, target)
        prep["identities"][phase] = artifact(out, identity_path)
        if phase == "incremental":
            assert_extension(snapshots["base"], snapshots[phase])
    if evaluator_identity(root) != source_files or sha256(labels_path) != prep["labels"]["sha256"] or sha256(manifest_path) != prep["source_manifest"]["sha256"]:
        raise InvalidEvidence("source/labels changed during native preparation")
    for ref in assets.values():
        if sha256(Path(ref["path"])) != ref["sha256"]:
            raise InvalidEvidence("native/model assets changed during preparation")
    path = out / "preparation.json"
    # Do not publish PREPARED until the same strict verifier accepts every receipt.
    pending = out / "preparation.pending.json"
    write_json(pending, prep)
    validate_preparation(pending, out / "base.sqlite", labels_path, corpus, out / "incremental.sqlite")
    pending.rename(path)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("labels", "out", "model", "server", "ocr-helper"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.labels, args.out, args.model, args.server, args.ocr_helper)
    except (InvalidEvidence, ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"distill preparation MISSING: {exc}", file=sys.stderr)
        return 2
    print(f"PREPARED only; no quality claim: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
