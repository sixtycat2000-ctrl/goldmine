"""Counterexamples for frozen real labels and stored-source excerpt verification."""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from acceptance_lib.evidence import InvalidEvidence, artifact, read_json, write_json
from distill_eval.labels import load_labels
from distill_eval.scoring import excerpt_audit, normalized_span
from goldmine.distill import Card, CItem
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def frozen_labels(tmp_path):
    # Schema mechanics only; never submitted as actual real quality evidence.
    license_path = tmp_path / "LICENSE"
    license_path.write_text("MIT fixture content\n")
    topics = [f"topic-{i}" for i in range(8)]
    sources, documents = [], []
    for i in range(112):
        name = f"document-{i}.txt"
        asset = tmp_path / "assets" / name
        asset.parent.mkdir(exist_ok=True)
        asset.write_text(f"Fixture document {i}.\n")
        role = "topic" if i < 96 or i >= 102 else "cross_topic" if i < 100 else "noise"
        assigned = [topics[i % 8]] if role == "topic" else topics[:2] if role == "cross_topic" else []
        sources.append({"path": name, "source_group": f"independent-{i}", "asset": artifact(tmp_path, asset),
                        "provenance": {"type": "real", "license": "MIT", "license_file": artifact(tmp_path, license_path)}})
        documents.append({"path": name, "topics": assigned, "role": role,
                          "split": "base" if i < 102 else "incremental",
                          "case_tags": ["language:en" if i % 2 else "language:zh", "paraphrase", "duplicate"]})
    manifest = tmp_path / "product.json"
    write_json(manifest, {"schema": 1, "state": "FROZEN", "evidence_mode": "real", "sources": sources})
    data = {"schema": 1, "suite_id": "distill.real.v1", "state": "FROZEN", "evidence_mode": "real",
            "frozen_at": "2026-10-20T00:00:00+00:00", "labeling_protocol": "independent-before-clustering",
            "reviewers": ["fixture reviewer"], "product_manifest": {"manifest_path": "product.json", "sha256": artifact(tmp_path, manifest)["sha256"]},
            "topics": topics, "documents": documents}
    path = tmp_path / "labels.json"
    write_json(path, data)
    return path, data, manifest


def test_schema_requires_full_independent_source_coverage(frozen_labels, tmp_path):
    path, data, _ = frozen_labels
    _, labels = load_labels(path, tmp_path / "indexed")
    assert len(labels) == 112
    data["documents"] = data["documents"][:12]
    write_json(path, data)
    with pytest.raises(InvalidEvidence, match="at least100"):
        load_labels(path, tmp_path / "indexed")


@pytest.mark.parametrize("field,value", [("state", "DRAFT"), ("evidence_mode", "synthetic"),
                                           ("labeling_protocol", "derived-from-clustering")])
def test_labels_cannot_be_derived_from_successful_clusters(frozen_labels, tmp_path, field, value):
    path, data, _ = frozen_labels
    data[field] = value
    write_json(path, data)
    with pytest.raises(InvalidEvidence, match="frozen real labels"):
        load_labels(path, tmp_path)


def test_duplicate_paths_and_edited_assets_are_rejected(frozen_labels, tmp_path):
    path, data, _ = frozen_labels
    data["documents"][1]["path"] = data["documents"][0]["path"]
    write_json(path, data)
    with pytest.raises(InvalidEvidence, match="duplicated"):
        load_labels(path, tmp_path / "indexed")
    data["documents"][1]["path"] = "document-1.txt"
    write_json(path, data)
    (tmp_path / "assets/document-0.txt").write_text("tampered after freeze")
    with pytest.raises(InvalidEvidence, match="changed artifact"):
        load_labels(path, tmp_path / "indexed")


def test_duplicating_eight_sources_cannot_satisfy_eight_per_topic(frozen_labels, tmp_path):
    path, data, manifest = frozen_labels
    product = read_json(manifest)
    for source in product["sources"]:
        source["source_group"] = "same-real-origin"
    write_json(manifest, product)
    data["product_manifest"]["sha256"] = artifact(tmp_path, manifest)["sha256"]
    write_json(path, data)
    with pytest.raises(InvalidEvidence, match="independent base source"):
        load_labels(path, tmp_path / "indexed")


def test_excerpt_maps_original_offsets_and_rejects_tokenized_case():
    original = "  Zircon\n lantern\tOriginal CASE. 中文知识库。"
    span = normalized_span(original, "Zircon lantern Original CASE. 中文知识库。")
    assert original[span[0]:span[1]].startswith("Zircon\n lantern")
    assert normalized_span(original, "zircon lantern original case.") is None


def test_excerpt_must_match_its_exact_stored_location_and_citation(tmp_path):
    store = Store(tmp_path / "source.sqlite")
    try:
        vector = np.zeros(768, dtype=np.float32)
        vector[0] = 1
        fid = store.upsert_file(FileRec(path="/explicit/proof.txt", kind="doc", state="done", capability="content_indexed"))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="text", loc=11, loc_end=12,
                                               text_for_fts="Zircon Lantern Original CASE.", vec=vector)])
        sid = store.db.execute("SELECT id FROM segments WHERE kind='chunk'").fetchone()[0]
        member = CItem(sid, fid, vector, "Zircon Lantern Original CASE.", "/explicit/proof.txt", "doc", 11, 12)
        card = Card("Zircon", "terms", ["Zircon"], [{"text": member.text, "path": member.path, "kind": "doc", "loc": 11, "loc_end": 12}], [member], 1)
        assert excerpt_audit(store, [card])["passed"] == 1
        card.excerpts[0]["loc"] = 99
        assert excerpt_audit(store, [card])["passed"] == 0
        card.excerpts[0]["loc"] = 11
        card.excerpts[0]["text"] = "zircon lantern original case."
        assert excerpt_audit(store, [card])["passed"] == 0
    finally:
        store.close()


def test_strict_real_cli_returns_missing_when_labels_are_absent(tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("distill_real_cli", Path(__file__).resolve().parents[1] / "scripts/eval_distill.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.main(["--report-real", str(tmp_path / "missing.sqlite"), "--labels", str(tmp_path / "missing.json"),
                        "--corpus-root", str(tmp_path), "--json"]) == 2
    assert '"status": "MISSING"' in capsys.readouterr().out
