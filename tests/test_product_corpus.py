"""Portable labeled source inputs; incomplete real curation cannot pass."""

import copy
import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from answer_eval.protocol import EvaluationError, canonical_json, sha256_file
from product_eval.bundle import build_corpus, load_bundle
from product_eval.splits import make_splits, validate_splits

SEED = Path(__file__).resolve().parents[1] / "evals/product_v4"


def clone(tmp_path):
    path = tmp_path / "bundle"
    shutil.copytree(SEED, path)
    return path


def rewrite_queries(root, change):
    manifest = json.loads((root / "manifest.json").read_text())
    qpath = root / manifest["queries"]["path"]
    questions = [json.loads(line) for line in qpath.read_text().splitlines()]
    change(questions)
    qpath.write_text("".join(canonical_json(q) + "\n" for q in questions))
    manifest["queries"]["sha256"] = sha256_file(qpath)
    (root / "manifest.json").write_text(json.dumps(manifest))


def split_fixture():
    """420 synthetic COUNTER rows test partition mechanics, not relevance."""
    queries = []
    for primary in ("semantic", "cjk", "lookup", "pdf-page", "media-text", "compound", "noise"):
        for i in range(60):
            tags = [(("ocr", "asr", "metadata")[i // 20])] if primary == "media-text" else []
            queries.append({"id": f"{primary}-{i}", "primary_class": primary,
                            "source_group": f"{primary}:group:{i//5}", "tags": tags})
    return queries


def test_real_seed_is_portable_audited_and_explicitly_incomplete():
    bundle = load_bundle(SEED, require_complete=False)
    assert len(bundle.queries) == 28
    assert all(s["provenance"]["type"] == "real" and s["provenance"]["license"] == "MIT" for s in bundle.sources.values())
    assert any("28/420" in item for item in bundle.missing)
    assert any("pdf-page: 0/60" in item for item in bundle.missing)
    assert any("media-text: 0/60" in item for item in bundle.missing)
    with pytest.raises(EvaluationError) as error:
        load_bundle(SEED)
    assert error.value.status == "MISSING"


def test_copy_to_two_roots_preserves_identity_and_only_sources_are_indexed(tmp_path):
    bundle = load_bundle(SEED, require_complete=False)
    left, right = tmp_path / "left", tmp_path / "right"
    assert build_corpus(bundle, left, allow_draft=True) == build_corpus(bundle, right, allow_draft=True)
    for path, source in bundle.sources.items():
        assert sha256_file(left / "corpus" / path) == sha256_file(right / "corpus" / path) == source["asset"]["sha256"]
    assert not list((left / "corpus").rglob("queries.jsonl"))
    assert not list((left / "corpus").rglob("label-audit.json"))
    assert {p.relative_to(left / "corpus").as_posix() for p in (left / "corpus").rglob("*") if p.is_file()} == set(bundle.sources)
    relocated = clone(tmp_path)
    assert load_bundle(relocated, require_complete=False).identity == bundle.identity
    with pytest.raises(EvaluationError, match="existing"):
        build_corpus(bundle, left, allow_draft=True)


@pytest.mark.parametrize("change,match", [
    (lambda q: q[0]["expect"][0]["location"].update(start=9999, end=9999), "outside|claimed"),
    (lambda q: q[0]["expect"][0]["span"].update(start_char=1, end_char=25), "offset|span"),
    (lambda q: q[0]["expect"][0].update(view_id="missing-page"), "view"),
    (lambda q: q[0]["expect"][0]["location"].update(time=[0, 1]), "unknown"),
    (lambda q: q[0].update(unregistered=True), "unknown"),
    (lambda q: q.append(copy.deepcopy(q[0])), "duplicate"),
    (lambda q: q[0].update(source_group="split-away-from-my-source"), "source_group"),
    (lambda q: q[0]["required_facts"][0].update(sufficient_evidence=[["fake-fact"]]), "known evidence"),
])
def test_invalid_annotation_is_not_a_better_retrieval_score(tmp_path, change, match):
    root = clone(tmp_path)
    rewrite_queries(root, change)
    with pytest.raises(EvaluationError, match=match):
        load_bundle(root, require_complete=False)


def test_asset_traversal_and_symlink_escape_fail(tmp_path):
    root = clone(tmp_path)
    m = json.loads((root / "manifest.json").read_text())
    m["sources"][0]["asset"]["path"] = "../outside.py"
    (root / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(EvaluationError, match="relative"):
        load_bundle(root, require_complete=False)
    m["sources"][0]["asset"]["path"] = "assets/escape.py"
    outside = tmp_path / "outside.py"
    outside.write_text("outside scope")
    (root / "assets/escape.py").symlink_to(outside)
    (root / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(EvaluationError, match="escapes"):
        load_bundle(root, require_complete=False)


def test_exact_lookup_can_occur_in_source_but_full_question_cannot(tmp_path):
    bundle = load_bundle(SEED, require_complete=False)
    assert any(q["q"] == "chunker.py" for q in bundle.queries)
    root = clone(tmp_path)
    # A natural-language query copied exactly from a source is forbidden,
    # including when a curator tries to keep it under the lookup category.
    source = next(iter(bundle.views.values()))["text"].splitlines()[0]
    rewrite_queries(root, lambda q: q[0].update(q=source))
    with pytest.raises(EvaluationError, match="leaks"):
        load_bundle(root, require_complete=False)


def test_grouped_split_is_deterministic_complete_and_not_score_driven():
    queries = split_fixture()
    a, b = make_splits(queries), make_splits(queries)
    assert a == b
    validate_splits(a, queries)
    assert len(a["dev"]) == len(a["holdout"]) == 210
    lookup = {q["id"]: q for q in queries}
    assert not ({lookup[i]["source_group"] for i in a["dev"]} & {lookup[i]["source_group"] for i in a["holdout"]})
    with pytest.raises(EvaluationError) as error:
        make_splits(queries[:10])
    assert error.value.status == "MISSING"
    bad = copy.deepcopy(a)
    bad["holdout"].append(bad["dev"][0])
    with pytest.raises(EvaluationError, match="partition|duplicate"):
        validate_splits(bad, queries)


def test_coupled_ocr_compound_groups_preserve_hard_bounds_and_can_balance():
    queries = split_fixture()
    ocr = [q for q in queries if q["primary_class"] == "media-text" and "ocr" in q["tags"]]
    cursor = 0
    for group, size in enumerate((4, 4, 4, 3, 3, 2)):
        for q in ocr[cursor:cursor + size]:
            q["source_group"] = f"compound:group:{group}"
        cursor += size
    assert cursor == 20
    split = make_splits(queries)
    validate_splits(split, queries)
    assert split == make_splits(queries)
    lookup = {q["id"]: q for q in queries}
    for fold in ("dev", "holdout"):
        rows = [lookup[qid] for qid in split[fold]]
        assert sum(q["primary_class"] == "compound" for q in rows) == 30
        assert sum(q["primary_class"] == "media-text" and "ocr" in q["tags"] for q in rows) == 10
    assert not ({lookup[qid]["source_group"] for qid in split["dev"]}
                & {lookup[qid]["source_group"] for qid in split["holdout"]})


def test_near_duplicate_source_cannot_be_declared_an_independent_group(tmp_path):
    from product_eval.splits import source_groups
    sources = {"a.txt": {"source_group": "a", "parents": [], "asset": {"sha256": "a"}, "views": [{"id": "body", "sha256": "v", "evidence_type": "content_text"}]},
               "b.txt": {"source_group": "b", "parents": [], "asset": {"sha256": "b"}, "views": [{"id": "body", "sha256": "v", "evidence_type": "content_text"}]}}
    views = {("a.txt", "body"): {"text": "same source"}, ("b.txt", "body"): {"text": "same source"}}
    groups = source_groups(sources, [], views)
    assert groups["a.txt"] == groups["b.txt"]


@pytest.mark.parametrize("edge,linked", [
    ("same_basename_only", False),
    ("same_asset", True),
    ("same_content_view", True),
    ("explicit_parent", True),
])
def test_same_filename_is_not_a_duplicate_edge_but_real_provenance_still_is(edge, linked):
    import hashlib
    from product_eval.splits import source_groups

    def digest(text):
        return hashlib.sha256(text.encode()).hexdigest()

    paths = ("zipfile/__init__.py", "ctypes/__init__.py")
    texts = ("ZIP_STORED = 0\n", "class c_void_p: pass\n")
    sources, views = {}, {}
    for index, (path, text) in enumerate(zip(paths, texts)):
        sources[path] = {"source_group": f"group:{index}", "parents": [],
                         "asset": {"sha256": digest(text)},
                         "views": [{"id": "body", "sha256": digest(text), "evidence_type": "content_text"},
                                   {"id": "basename", "sha256": digest("__init__.py"), "evidence_type": "name_metadata"}]}
        views[(path, "body")] = {"text": text}
        views[(path, "basename")] = {"text": "__init__.py"}
    if edge == "same_asset":
        sources[paths[1]]["asset"] = copy.deepcopy(sources[paths[0]]["asset"])
    elif edge == "same_content_view":
        sources[paths[1]]["views"][0] = copy.deepcopy(sources[paths[0]]["views"][0])
        views[(paths[1], "body")] = copy.deepcopy(views[(paths[0], "body")])
    elif edge == "explicit_parent":
        sources[paths[1]]["parents"] = [paths[0]]
    groups = source_groups(sources, [], views)
    assert (groups[paths[0]] == groups[paths[1]]) is linked
