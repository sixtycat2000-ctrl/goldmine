"""Observed competitor output, exact file identities, and honest denominators."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from answer_eval.protocol import EvaluationError
from search_comparison.adapters import DocumentMap, parse_goldmine, parse_qmd, parse_recoll
from search_comparison.metrics import score_files, summarize
from search_comparison.process import run_process
from search_comparison.runner import recompute_report

FIXTURES = Path(__file__).parent / "fixtures_data/comparison"


@pytest.fixture
def mapping():
    provenance = json.loads((FIXTURES / "provenance.json").read_text())
    return DocumentMap(provenance["corpus_root"], provenance["documents"])


def fixture(name):
    return (FIXTURES / name).read_text()


def test_real_output_fixture_bytes_match_recorded_captures():
    provenance = json.loads((FIXTURES / "provenance.json").read_text())
    for name, expected in provenance["streams"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == expected
    assert hashlib.sha256((FIXTURES / provenance["goldmine"]["output"]).read_bytes()).hexdigest() == provenance["goldmine"]["sha256"]


def test_real_qmd_and_recoll_ranks_map_to_same_source_identities(mapping):
    qmd = parse_qmd(fixture("qmd-overlap.stdout"), mapping)
    recoll = parse_recoll(fixture("recoll-overlap.stdout"), mapping)
    assert [hit["path"] for hit in qmd["hits"]] == ["goldmine/chunker.py", "docs/DESIGN_DECISIONS.md", "goldmine/pack.py"]
    assert [hit["path"] for hit in recoll["hits"]] == ["docs/DESIGN_DECISIONS.md", "goldmine/chunker.py", "goldmine/pack.py"]
    assert recoll["reported_total"] == 3
    assert [hit["file_rank"] for hit in qmd["hits"]] == [1, 2, 3]


def test_real_goldmine_list_output_preserves_engine_order_without_resorting(mapping):
    rows = parse_goldmine(fixture("goldmine-sem01.stdout"), mapping)["hits"]
    assert rows[0]["path"] == "goldmine/pack.py" and rows[1]["path"] == "goldmine/chunker.py"
    # Admission policies may make scores nonmonotonic. CLI order is binding.
    assert rows[0]["score"] < rows[1]["score"]


def test_file_duplicates_collapse_by_first_appearance(mapping):
    rows = json.loads(fixture("qmd-overlap.stdout"))
    rows.insert(1, copy.deepcopy(rows[0]))
    parsed = parse_qmd(json.dumps(rows), mapping)
    assert parsed["duplicate_file_rows"] == 1
    assert parsed["hits"][1]["engine_rank"] == 3 and parsed["hits"][1]["file_rank"] == 2


@pytest.mark.parametrize("mutation", ["bool_score", "unknown_key", "outside_collection", "outside_index", "parent_path", "bad_docid"])
def test_unmapped_or_schema_changed_qmd_output_is_never_silently_dropped(mapping, mutation):
    rows = json.loads(fixture("qmd-overlap.stdout"))
    if mutation == "bool_score":
        rows[0]["score"] = True
    elif mutation == "unknown_key":
        rows[0]["invented"] = "surprise formatter"
    elif mutation == "outside_collection":
        rows[0]["file"] = "qmd://private/goldmine/chunker.py.md?index=comparison"
    elif mutation == "outside_index":
        rows[0]["file"] = "qmd://common/goldmine/chunker.py.md?index=private"
    elif mutation == "parent_path":
        rows[0]["file"] = "qmd://common/../private/file.md?index=comparison"
    else:
        rows[0]["docid"] = "#unknown"
    with pytest.raises(EvaluationError):
        parse_qmd(json.dumps(rows), mapping)


@pytest.mark.parametrize("mutation", ["bad_base64", "count", "extra_field", "outside", "missing_header"])
def test_recoll_recommended_base64_format_is_strict(mapping, mutation):
    lines = fixture("recoll-overlap.stdout").splitlines()
    if mutation == "bad_base64":
        lines[2] = "not-base64!"
    elif mutation == "count":
        lines[1] = "2 results"
    elif mutation == "extra_field":
        lines[2] += " Zm9v"
    elif mutation == "outside":
        lines[2] = base64.b64encode(b"file:///unregistered/private.txt").decode()
    else:
        lines = lines[1:]
    with pytest.raises(EvaluationError):
        parse_recoll("\n".join(lines), mapping)


def test_empty_outputs_are_zero_hits_not_missing(mapping):
    assert not parse_qmd("[]", mapping)["hits"]
    assert not parse_goldmine("[]", mapping)["hits"]
    assert not parse_recoll("Recoll query: Query(foo)\n0 results\n", mapping)["hits"]


def test_encoded_spaces_unicode_and_punctuation_have_prebuilt_exact_identity(tmp_path):
    documents = [{"source_path": "source.txt", "view_path": "中文 # %.md"}]
    mapping = DocumentMap(tmp_path, documents)
    url = (tmp_path / "中文 # %.md").as_uri()
    stdout = "Recoll query: Query(foo)\n1 results\n" + base64.b64encode(url.encode()).decode() + " \n"
    assert parse_recoll(stdout, mapping)["hits"][0]["path"] == "source.txt"
    assert mapping.qmd_uri("qmd://common/%E4%B8%AD%E6%96%87%20%23%20%25.md?index=comparison") == "source.txt"


def question(qid="q1", primary="semantic"):
    return {"id": qid, "q": "independent unit fixture", "primary_class": primary, "source_group": "unit:q1",
            "expect": [] if primary == "noise" else [{"path": "a", "grade": 3}, {"path": "a", "grade": 2}, {"path": "b", "grade": 1}]}


def row(q, paths=None, status="OK"):
    paths = [] if paths is None else paths
    return {**{key: q[key] for key in ("id", "q", "primary_class", "source_group")}, "status": status,
            "hits": [{"path": path} for path in paths], "metrics": score_files(q, paths) if status == "OK" else {}}


def test_document_ndcg_uses_unique_file_qrels_and_failed_rows_do_not_shrink():
    q1, q2, noise = question(), question("q2"), question("noise", "noise")
    assert score_files(q1, ["a", "b"])["nDCG@10"] == 1
    with pytest.raises(EvaluationError, match="duplicate"):
        score_files(q1, ["a", "a"])
    report = summarize([row(q1, ["a"]), row(q2, status="MISSING"), row(noise)])
    assert report["answerable"]["metrics"]["success@1"] == {"sum": 1, "n": 2, "value": .5}
    assert report["answerable"]["n_measured"] == 1 and report["errors"] == ["q2"]
    assert report["noise"]["metrics"]["empty_result"] == {"sum": 1, "n": 1, "value": 1}


def test_recompute_rejects_changed_metrics_and_query_denominators():
    q = question()
    rows = [row(q, ["a"])]
    report = {"configuration": {"diagnostic_only": True}, "quality_evidence": False, "n_queries": 1,
              "questions": [q], "systems": {"system": {"rows": rows, "summary": summarize(rows)}}}
    assert recompute_report(report)
    edited = copy.deepcopy(report)
    edited["systems"]["system"]["rows"][0]["metrics"]["success@1"] = 0
    with pytest.raises(EvaluationError, match="metrics"):
        recompute_report(edited)
    edited = copy.deepcopy(report)
    edited["systems"]["system"]["rows"] = []
    with pytest.raises(EvaluationError, match="denominator"):
        recompute_report(edited)


def test_raw_recompute_rejects_reordered_hits_even_with_matching_edited_metrics(tmp_path, mapping):
    raw = tmp_path / "raw"
    raw.mkdir()
    output = raw / "qmd-001.stdout"
    output.write_text(fixture("qmd-overlap.stdout"))
    q = question()
    q["expect"] = [{"path": "goldmine/chunker.py", "grade": 3}]
    parsed = parse_qmd(output.read_text(), mapping)
    saved = {**row(q), **parsed, "process": {"stdout": output.name}}
    saved["metrics"] = score_files(q, [hit["path"] for hit in saved["hits"]])
    report = {"configuration": {"diagnostic_only": True}, "quality_evidence": False, "n_queries": 1,
              "questions": [q], "raw_sha256": {"raw/" + output.name: hashlib.sha256(output.read_bytes()).hexdigest()},
              "systems": {"qmd": {"rows": [saved], "summary": summarize([saved])}}}
    assert recompute_report(report, raw_root=tmp_path, mapping=mapping)
    edited = copy.deepcopy(report)
    changed = edited["systems"]["qmd"]["rows"][0]
    changed["hits"] = changed["hits"][::-1]
    changed["metrics"] = score_files(q, [hit["path"] for hit in changed["hits"]])
    edited["systems"]["qmd"]["summary"] = summarize([changed])
    with pytest.raises(EvaluationError, match="raw CLI"):
        recompute_report(edited, raw_root=tmp_path, mapping=mapping)
    output.write_text("[]")
    with pytest.raises(EvaluationError, match="checksum"):
        recompute_report(report, raw_root=tmp_path, mapping=mapping)


def test_process_arguments_are_literal_and_timeout_is_bounded(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    literal = "$(touch should-never-exist); `echo nope`"
    run = run_process([sys.executable, "-c", "import sys;print(sys.argv[1])", literal],
                      cwd=work, env=dict(os.environ), raw_prefix=tmp_path / "literal")
    assert run["exit_code"] == 0
    assert (tmp_path / "literal.stdout").read_text().strip() == literal
    assert not (work / "should-never-exist").exists()
    run = run_process([sys.executable, "-c", "import time;time.sleep(5)"],
                      cwd=work, env=dict(os.environ), raw_prefix=tmp_path / "timeout", timeout=.05)
    assert run["timed_out"] and run["wall_seconds"] < 2
