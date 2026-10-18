"""Offline tests for the real-corpus eval harness (v2 items D1/D2).

Covers: label validation against the committed manifest, media needs-tag
skipping, frozen-split holdout isolation, the scoring semantics (location
constraints, graded nDCG), the regression gate's trip/noise behavior, and an
end-to-end mock run over a small manifest subset.

The manifest sources are real files on THIS machine (docs/v2/01 §5.1 corpus);
tests that need them skip cleanly elsewhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as Hit

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import build_eval_corpus as bec  # noqa: E402
import eval_real as er  # noqa: E402

MANIFEST_PATH = er.MANIFEST_JSON


def _manifest() -> dict | None:
    if not MANIFEST_PATH.is_file():
        return None
    manifest = json.loads(MANIFEST_PATH.read_text())
    if not all(Path(r).exists() for r in manifest["ingest_dir_roots"]):
        return None
    fixture_root = Path(manifest["fixture"]["root"])
    if not fixture_root.is_dir():
        # the fixture lives under /tmp and macOS cleans it; it is seeded and
        # deterministic, so rebuild it instead of skipping (the label
        # validator must run where the labels are maintained — 01-search F8)
        sys.path.insert(0, str(REPO / "scripts"))
        import make_coverage_fixture

        make_coverage_fixture.build(fixture_root,
                                    seed=make_coverage_fixture.SEED, force=True)
    if not fixture_root.is_dir():
        return None
    return manifest


def _queries() -> list[dict]:
    return er.load_queries()


# ------------------------------------------------------------ label validity

def test_real_jsonl_validates_against_manifest():
    manifest = _manifest()
    if manifest is None:  # corpus sources not on this machine
        pytest.skip("eval corpus sources absent")
    fixture_root = Path(manifest["fixture"]["root"])
    problems = er.validate(_queries(), manifest, fixture_root)
    assert problems == []


def test_query_ids_unique_and_tags_present():
    queries = _queries()
    ids = [q["id"] for q in queries]
    assert len(ids) == len(set(ids))
    assert all(q.get("tag") for q in queries)
    # the authored set is ~120 queries; the media set merges on top
    authored = [q for q in queries if q["id"].startswith("real-")]
    assert 110 <= len(authored) <= 130


# --------------------------------------------------------- media-tag skipping

def test_media_needs_skipping():
    queries = _queries()
    eligible = er.eligible_queries(queries)
    skipped = {q["id"] for q in queries} - {q["id"] for q in eligible}
    expected_skipped = {
        q["id"] for q in queries
        if set(q.get("needs") or []) & er.SKIPPED_NEEDS
    }
    assert skipped == expected_skipped
    assert skipped, "the media set must contain needs:asr/ocr/exif queries"
    # every skipped query comes from the media file (never an authored one)
    assert all(i.startswith("media-") for i in skipped)
    # opting in restores exactly the opted capability
    restored = er.eligible_queries(queries, include_needs={"ocr"})
    assert {q["id"] for q in restored} >= (
        {q["id"] for q in eligible}
        | {q["id"] for q in queries if set(q.get("needs") or []) & {"ocr"}}
    )


# ------------------------------------------------------- holdout isolation

def test_splits_holdout_isolation():
    queries = _queries()
    eligible_ids = {q["id"] for q in er.eligible_queries(queries)}
    if not er.SPLITS_JSON.is_file():
        pytest.skip("splits.json not committed yet")
    splits = json.loads(er.SPLITS_JSON.read_text())
    train, holdout = set(splits["train"]), set(splits["holdout"])
    assert not (train & holdout), "train/holdout must be disjoint"
    assert train | holdout == eligible_ids, "splits must cover eligible ids"
    # ~70/30, stratified: every tag with >= 2 queries appears in both pools
    assert 0.6 < len(train) / len(eligible_ids) < 0.8
    by_tag: dict[str, set[str]] = {}
    for q in er.eligible_queries(queries):
        by_tag.setdefault(q["tag"], set()).add(q["id"])
    for tag, ids in by_tag.items():
        if len(ids) >= 2:
            assert ids & train and ids & holdout, f"tag {tag} not stratified"
    # tuning split excludes the holdout, full runs see everything
    sel_train = er.select_split(er.eligible_queries(queries), "train", splits)
    assert {q["id"] for q in sel_train} == train
    sel_holdout = er.select_split(er.eligible_queries(queries), "holdout", splits)
    assert {q["id"] for q in sel_holdout} == holdout
    assert not ({q["id"] for q in sel_train} & {q["id"] for q in sel_holdout})
    # frozen full-run refuses silently-stale splits (ids the splits don't know)
    stale = {"train": sorted(train)[:5], "holdout": sorted(holdout)[:2]}
    with pytest.raises(SystemExit):
        er.select_split(er.eligible_queries(queries), "frozen", stale)


def test_write_splits_is_deterministic():
    queries = _queries()
    tmp = Path("/tmp/gm_test_splits.json")
    a = er.write_splits(queries, tmp)
    b = er.write_splits(queries, tmp)
    assert a == b


# ------------------------------------------------------------ scoring units

def _hit(path, kind="doc", loc=None, loc_end=None):
    return Hit(path=path, kind=kind, loc=loc, loc_end=loc_end)


FX = Path("/tmp/gm_covfix")


def test_score_query_success_and_location():
    q = {"id": "t1", "q": "x", "tag": "semantic",
         "expect": [{"path": "/a/b.md", "lines": [10, 20], "grade": 3}]}
    # right file, overlapping chunk lines -> success everywhere
    hits = [_hit("/a/other.md"), _hit("/a/b.md", loc=5, loc_end=15)]
    m = er.score_query(q, hits, FX)
    assert (m["success1"], m["success5"], m["rr"]) == (0.0, 1.0, 0.5)
    # right file at rank 1 but wrong stretch -> NOT a success (location rule)
    hits = [_hit("/a/b.md", loc=90, loc_end=99)]
    m = er.score_query(q, hits, FX)
    assert m["success5"] == 0.0
    # ...but nDCG still credits it as the same-file-other-location grade (3-1)
    assert 0.0 < m["ndcg"] < 1.0


def test_score_query_pages_containment():
    q = {"id": "t2", "q": "x", "tag": "pdf-page",
         "expect": [{"path": "pdf/x.pdf", "pages": [2, 2], "grade": 3}]}
    resolved = er._resolve("pdf/x.pdf", FX)  # canonical (/private/tmp on macOS)
    # page 2 contained (DigUp at() semantics: pages must fall inside)
    m = er.score_query(q, [_hit(resolved, kind="pdf", loc=2.0)], FX)
    assert m["success1"] == 1.0
    # page 3 outside the range
    m = er.score_query(q, [_hit(resolved, kind="pdf", loc=3.0)], FX)
    assert m["success1"] == 0.0
    # a line-loc doc hit never satisfies a pages expectation
    m = er.score_query(q, [_hit(resolved, kind="doc", loc=2)], FX)
    assert m["success1"] == 0.0


def test_score_query_noise_and_grades():
    noise = {"id": "t3", "q": "x", "tag": "noise", "expect": []}
    m = er.score_query(noise, [], FX)
    assert m["noise"] and m["n_hits"] == 0 and m["ndcg"] is None
    m = er.score_query(noise, [_hit("/a/b.md")] * 4, FX)
    assert m["noise"] and m["n_hits"] == 4  # violation, counted by the runner
    # grade-1 siblings never solve a query; grade-2 does
    q = {"id": "t4", "q": "x", "tag": "semantic",
         "expect": [{"path": "/a/one.md", "grade": 1}, {"path": "/a/two.md", "grade": 2}]}
    m = er.score_query(q, [_hit("/a/one.md"), _hit("/a/two.md")], FX)
    assert m["success1"] == 0.0 and m["success5"] == 1.0 and m["rr"] == 0.5


# --------------------------------------------------------------- statistics

def test_binom_ci_reference_point():
    """Exact Clopper-Pearson, scipy-free (04-eval F2): the n=146 llama
    baseline s@5 = 109/146 = 0.747 must land at [0.668, 0.815]."""
    lo, hi = er.binom_ci(109, 146)
    assert abs(lo - 0.668) < 0.001 and abs(hi - 0.815) < 0.001
    # degenerate cases: k=0 lower stays 0; k=n upper pins at 1 and the lower
    # is the exact one-sided bound 0.025**(1/n)
    assert er.binom_ci(0, 10) == (0.0, 1.0 - 0.025 ** (1 / 10)) or \
        abs(er.binom_ci(0, 10)[1] - (1 - 0.025 ** 0.1)) < 1e-6
    lo10, hi10 = er.binom_ci(10, 10)
    assert abs(lo10 - 0.025 ** 0.1) < 1e-6 and hi10 == 1.0


def test_mcnemar_exact_hand_values():
    """Exact McNemar on discordant pairs: 7 same-direction flips out of 7
    discordant -> p = 2 * 0.5^7 = 0.0156 (significant); 5/5 -> 0.0625
    (not); 0 discordant -> 1.0. This trips ~2 flips earlier than the paired
    bootstrap's effective wire at n=146 (10 flips)."""
    assert er.mcnemar_exact(7, 0) == pytest.approx(2 * 0.5 ** 7)
    assert er.mcnemar_exact(7, 0) < 0.05
    assert er.mcnemar_exact(5, 0) == pytest.approx(2 * 0.5 ** 5)
    assert er.mcnemar_exact(5, 0) > 0.05
    assert er.mcnemar_exact(4, 3) == pytest.approx(1.0)
    assert er.mcnemar_exact(0, 0) == 1.0


def test_mock_backend_banner_mechanics_only():
    """04-eval F3a: the mock run must label itself as a non-quality signal."""
    rows = [_row("t1", "semantic", [{"path": "/a/b.md", "grade": 2}],
                 [{"path": "/a/b.md", "score": 0.9, "duplicate_of": None}])]
    current = {name: {"value": None, "n": 0} for name in er.TRACKED_METRICS}
    out = er.render(rows, current, {}, split="all", backend="mock")
    assert "mechanics-only" in out and "φ(mock,llama)≈0.009" in out
    out_llama = er.render(rows, current, {}, split="all", backend="llama")
    assert "mechanics-only" not in out_llama


# --------------------------------------------------------------- gate logic

def _fake_run(values: dict[str, float]):
    """metrics summary + per-query maps for `n` queries at the given values."""
    return values


def _mk(n: int, val: float):
    return {f"q{i:03d}": val for i in range(n)}


def _summary(pq: dict, name=None):
    vals = list(pq.values())
    return {"value": sum(vals) / len(vals), "n": len(vals)}


def test_gate_green_on_identical_baseline():
    pq = {"success@5": _mk(100, 1.0), "success@1": _mk(100, 0.6),
          "MRR": _mk(100, 0.7), "nDCG@10": _mk(100, 0.7),
          "lookup success@1": _mk(30, 0.9), "zero-hit honesty": _mk(5, 1.0)}
    current = {k: _summary(v) for k, v in pq.items()}
    baseline = {"metrics": current, "per_query": pq}
    gate = er.compare_to_baseline(current, pq, baseline)
    assert gate["tripped"] == []
    assert all(m["verdict"] == "ok" for m in gate["metrics"].values())


def test_gate_trips_on_artificially_lowered_baseline():
    # baseline artificially HIGH (lowered baseline = easier target in reverse);
    # we test the documented direction: current DROPS > 0.02 vs baseline with
    # a consistent, beyond-noise drop.
    n = 100
    cur_pq = {"success@5": {f"q{i:03d}": (1.0 if i < 60 else 0.0) for i in range(n)}}
    base_pq = {"success@5": {f"q{i:03d}": (1.0 if i < 85 else 0.0) for i in range(n)}}
    current = {"success@5": _summary(cur_pq["success@5"])}
    baseline = {
        "metrics": {"success@5": _summary(base_pq["success@5"])},
        "per_query": base_pq,
    }
    gate = er.compare_to_baseline(current, cur_pq, baseline)
    assert "success@5" in gate["tripped"]
    m = gate["metrics"]["success@5"]
    assert m["delta"] == pytest.approx(-0.25)
    assert "1 query = " in m["note"]  # counts/uncertainty context (B12)
    assert "exact McNemar" in m["note"]  # primary statistic for s@5/s@1


def test_gate_reports_small_drop_as_noise_not_regression():
    # a single-query flip at n=100 (-0.01) is below the 0.02 threshold; a
    # single flip at a small n is inside the bootstrap CI -> reported, ok
    n = 100
    cur_pq = {"success@5": {f"q{i:03d}": (1.0 if i < 99 else 0.0) for i in range(n)}}
    base_pq = {"success@5": {f"q{i:03d}": 1.0 for i in range(n)}}
    current = {"success@5": _summary(cur_pq["success@5"])}
    baseline = {"metrics": {"success@5": _summary(base_pq["success@5"])},
                "per_query": base_pq}
    gate = er.compare_to_baseline(current, cur_pq, baseline)
    assert gate["tripped"] == []
    assert gate["metrics"]["success@5"]["verdict"] == "ok"  # -0.01 < 0.02

    n = 10  # one flip = -0.10, but at this n the CI swallows it
    cur_pq = {"success@5": {f"q{i:03d}": (1.0 if i < 9 else 0.0) for i in range(n)}}
    base_pq = {"success@5": {f"q{i:03d}": 1.0 for i in range(n)}}
    current = {"success@5": _summary(cur_pq["success@5"])}
    baseline = {"metrics": {"success@5": _summary(base_pq["success@5"])},
                "per_query": base_pq}
    gate = er.compare_to_baseline(current, cur_pq, baseline)
    v = gate["metrics"]["success@5"]
    assert v["verdict"] in ("ok", "drop-within-noise")
    assert "ADVISORY" in v["note"] or "noise" in v["note"] or v["verdict"] == "ok"
    # either way it must not hard-fail at n=10 with one flipped query


def test_gate_without_per_query_never_trips():
    # a baseline without per-query values cannot test significance: the drop
    # is reported (drop-within-noise), the gate stays green — the old
    # unpaired-CI fallback was dead code for schema-1 baselines and is gone
    n = 100
    cur_pq = {"success@5": {f"q{i:03d}": 1.0 for i in range(n)}}
    current = {"success@5": _summary(cur_pq["success@5"])}
    baseline = {"metrics": {"success@5": {"value": 0.95, "n": n}}}
    gate = er.compare_to_baseline(current, cur_pq, baseline)
    assert gate["tripped"] == []  # +0.05 improvement
    assert "no paired per-query data" in gate["metrics"]["success@5"]["note"]

    # ...and a big drop without paired data is loud but cannot fail
    cur_pq_drop = {"success@5": {f"q{i:03d}": (1.0 if i < 60 else 0.0)
                                 for i in range(n)}}
    current = {"success@5": _summary(cur_pq_drop["success@5"])}  # 0.60 vs 1.0
    baseline = {"metrics": {"success@5": {"value": 1.0, "n": n}}}
    gate = er.compare_to_baseline(current, cur_pq_drop, baseline)
    assert gate["tripped"] == []
    assert gate["metrics"]["success@5"]["verdict"] == "drop-within-noise"
    assert "no paired per-query data" in gate["metrics"]["success@5"]["note"]


def test_gate_advisory_metrics_cannot_trip():
    """04-eval F2: lookup (n=16) and zero-hit honesty (n=5) subsets are too
    small for any CI to gate — even a beyond-noise, statistically significant
    drop stays advisory and never enters 'tripped'."""
    n = 16
    cur_pq = {"lookup success@1": {f"q{i:03d}": (1.0 if i < 6 else 0.0)
                                   for i in range(n)}}
    base_pq = {"lookup success@1": {f"q{i:03d}": 1.0 for i in range(n)}}
    current = {"lookup success@1": _summary(cur_pq["lookup success@1"])}
    baseline = {"metrics": {"lookup success@1": _summary(base_pq["lookup success@1"])},
                "per_query": base_pq}
    gate = er.compare_to_baseline(current, cur_pq, baseline)
    m = gate["metrics"]["lookup success@1"]
    assert gate["tripped"] == []
    assert m["verdict"] == "drop-within-noise"
    assert "ADVISORY" in m["note"] and "cannot trip" in m["note"]


# ------------------------------------------------------ end-to-end (mock)

def test_end_to_end_mock_subset(tmp_path):
    manifest = _manifest()
    if manifest is None:
        pytest.skip("eval corpus sources absent")
    db = tmp_path / "eval.sqlite"
    built = bec.build_index(db, "mock", manifest, subset=12)
    assert built["counts"]["files"] >= 12
    assert built["report"]["files"]["failed"] == 0

    fixture_root = Path(manifest["fixture"]["root"])
    ingested = {r[0] for r in _paths(db)}
    queries = [q for q in er.eligible_queries(_queries())
               if all(er._resolve(e["path"], fixture_root) in ingested
                      for e in q.get("expect", []))]
    assert len(queries) >= 5, "subset must keep at least a handful of queries"
    rows = er.run_eval(db, "mock", queries, fixture_root)
    current = er.summarize(rows)
    assert 0.0 <= current["success@5"]["value"] <= 1.0
    assert current["success@5"]["n"] == len([r for r in rows
                                             if not r["metrics"]["noise"]])
    # deterministic: a second identical run byte-matches every metric
    rows2 = er.run_eval(db, "mock", queries, fixture_root)
    assert er.summarize(rows2) == current


def _paths(db):
    from goldmine.store import Store

    store = Store(db)
    try:
        return store.db.execute("SELECT path FROM files").fetchall()
    finally:
        store.close()


# ------------------------------------------------------------- fold audit (F3)

def _row(qid, tag, expect, hits):
    q = {"id": qid, "q": "x", "tag": tag, "expect": expect}
    return {"query": q,
            "metrics": {"noise": False, "n_hits": len(hits), "success5": 0.0,
                        "success1": 0.0, "rr": 0.0, "ndcg": None},
            "hit_paths": [h["path"] for h in hits],
            "hits": hits}


def test_fold_audit_flags_expected_path_only_in_the_folded_region():
    # the expected file IS hit — but only as a weaker match (< 0.5x top)
    rows = [_row("t1", "semantic",
                 [{"path": "/a/b.md", "grade": 2}],
                 [{"path": "/a/top.md", "score": 0.9, "duplicate_of": None},
                  {"path": "/a/b.md", "score": 0.3, "duplicate_of": None}])]
    audit = er.fold_audit(rows, FX)
    assert audit["fold_hidden"] == 1
    assert audit["audited"] == 1
    assert audit["hidden"][0]["id"] == "t1"


def test_fold_audit_duplicate_fold_hides_a_high_scoring_hit():
    # score above the 0.5x line, but folded as a near-duplicate of rank 1
    rows = [_row("t2", "semantic",
                 [{"path": "/a/dup.md", "grade": 3}],
                 [{"path": "/a/orig.md", "score": 0.8, "duplicate_of": None},
                  {"path": "/a/dup.md", "score": 0.8, "duplicate_of": 1}])]
    assert er.fold_audit(rows, FX)["fold_hidden"] == 1


def test_fold_audit_clear_when_expected_match_is_visible():
    # expected path visible unfolded -> not hidden
    rows = [_row("t3", "semantic",
                 [{"path": "/a/b.md", "grade": 2}],
                 [{"path": "/a/b.md", "score": 0.9, "duplicate_of": None},
                  {"path": "/a/weak.md", "score": 0.2, "duplicate_of": None}])]
    audit = er.fold_audit(rows, FX)
    assert audit["fold_hidden"] == 0
    # expected path absent entirely -> a MISS, not a fold-hidden
    rows = [_row("t4", "semantic",
                 [{"path": "/a/gone.md", "grade": 2}],
                 [{"path": "/a/top.md", "score": 0.9, "duplicate_of": None}])]
    audit = er.fold_audit(rows, FX)
    assert audit["fold_hidden"] == 0 and audit["audited"] == 1
    # noise queries are not audited; a hit-less row is not audited either
    noise = {"id": "t5", "q": "x", "tag": "noise", "expect": []}
    rows = [{"query": noise, "metrics": {"noise": True, "n_hits": 0},
             "hit_paths": [], "hits": []}]
    assert er.fold_audit(rows, FX) == {"audited": 0, "fold_hidden": 0, "hidden": []}


def test_fold_audit_path_fallback_without_hit_records():
    """Old rows (no per-hit records) degrade honestly: audited from hit_paths,
    never a crash."""
    q = {"id": "t6", "q": "x", "tag": "semantic",
         "expect": [{"path": "/a/b.md", "grade": 2}]}
    rows = [{"query": q, "metrics": {"noise": False, "n_hits": 1},
             "hit_paths": ["/a/b.md"]}]
    audit = er.fold_audit(rows, FX)
    assert audit["audited"] == 0  # no fold data -> not counted as hidden
    assert audit["fold_hidden"] == 0


def test_fold_audit_line_in_rendered_metrics():
    rows = [_row("t1", "semantic",
                 [{"path": "/a/b.md", "grade": 2}],
                 [{"path": "/a/top.md", "score": 0.9, "duplicate_of": None},
                  {"path": "/a/b.md", "score": 0.3, "duplicate_of": None}])]
    current = {name: {"value": None, "n": 0} for name in
               ("success@5", "success@1", "MRR", "nDCG@10", "lookup success@1",
                "zero-hit honesty")}
    audit = er.fold_audit(rows, FX)
    out = er.render(rows, current, {}, split="all", backend="mock",
                    fold_audit_report=audit)
    assert "fold-audit: 1 of 1" in out
    assert "advisory, not gated" in out
    # --no-fold-audit silences the line entirely
    out = er.render(rows, current, {}, split="all", backend="mock",
                    fold_audit_report=None)
    assert "fold-audit" not in out


def test_fold_audit_rides_the_gate_output_advisory():
    audit = {"audited": 3, "fold_hidden": 1, "hidden": [{"id": "q1"}]}
    gate = {"metrics": {name: {"baseline": None, "current": None, "delta": None,
                               "verdict": "n/a", "note": ""}
                        for name in er.TRACKED_METRICS},
            "tripped": [], "fold_audit": audit}
    out = er.render_gate(gate)
    assert "fold-audit: 1 of 3" in out
    assert "GATE TRIPPED" not in out  # the audit never trips the gate
