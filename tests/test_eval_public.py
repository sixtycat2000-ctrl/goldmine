"""R5 public-suite offline tests (docs/v2.1-r5-public.md §9 + §0b amendments).

Everything here is OFFLINE: the BEIR-shaped fixture is synthetic (built
in-test via zipfile — zero real BEIR bytes, review F4) and every network path
runs through the Fetcher's injected open_url/sleep seams (model.py discipline).

Covers: manifest schema + B7 pin discipline; downloader resume/sha-mismatch/
404/backoff/disk-precheck; the .md adapter (byte format, F5 empty-title,
escaping, qrels parsing, query filtering, dangling qrels); the F1
fetch-deep-then-cut rule + qid-docid namespace safety; hand-computed nDCG
values (trec_eval convention); the eval_real machinery reuse; table +
baseline roundtrip + advisory-gate behavior; and a mock e2e over the mini
fixture that runs twice and must be identical.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import eval_public as ep  # noqa: E402
import eval_real as er  # noqa: E402
from goldmine.store import Store  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "mini_fixture", REPO / "evals" / "public" / "mini_fixture.py")
mf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mf)

NAME = mf.FIXTURE_NAME


# ------------------------------------------------------------------ helpers


@pytest.fixture(scope="session")
def mini_zip(tmp_path_factory) -> Path:
    return mf.build_mini_zip(tmp_path_factory.mktemp("mini") / f"{NAME}.zip",
                             graded=True)


@pytest.fixture(scope="session")
def mini_zip_binary(tmp_path_factory) -> Path:
    return mf.build_mini_zip(tmp_path_factory.mktemp("mini-bin") / f"{NAME}.zip",
                             graded=False)


def entry_for(zip_path: Path, *, graded: bool = True, **over) -> dict:
    st = mf.stats(graded)
    data = zip_path.read_bytes()
    entry = {
        "url": "https://fake.ukp/thakur/BEIR/datasets/minicorp.zip",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "license": "CC BY 4.0 (synthetic fixture — no real BEIR bytes)",
        "n_docs": st["n_docs"],
        "n_queries_test": st["n_queries_test"],
        "grade_range": st["grade_range"],
        "published_bm25": {"beir_paper": 0.500, "pyserini_2cr_flat": None},
    }
    entry.update(over)
    return entry


class FakeResp:
    """The seam object model.py's loop needs: .status + .read + context mgr."""

    def __init__(self, status: int = 200, body: bytes = b"",
                 record: list | None = None):
        self.status = status
        self._body = body
        self.record = record

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        if not self._body:
            return b""
        out, self._body = self._body[:n], self._body[n:]
        return out


class FakeNet:
    """Scripted open_url: each call pops a (status|exception, body) answer."""

    def __init__(self, script: list, record: list | None = None):
        self.script = script
        self.calls: list = record if record is not None else []
        self.sleeps: list[float] = []

    def open_url(self, request, timeout=None):
        self.calls.append(request)
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return FakeResp(status=status, body=body, record=self.calls)

    def sleep(self, s: float):
        self.sleeps.append(s)


def place_zip(root: Path, zip_path: Path) -> dict:
    """Pre-place a verified zip in the cache layout; returns its entry."""
    dest = ep.zip_path(root, NAME)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(zip_path.read_bytes())
    return entry_for(dest)


def run_with_stub_searcher(monkeypatch, ranking: dict[str, list[str]],
                           tree: Path, entry: dict, arms: list[str]) -> dict:
    """run_dataset_arms against a scripted ranking (controlled retrieval, so
    the metrics are hand-checkable); records every search() call."""
    calls: list[tuple] = []
    text_to_qid = {text: qid for qid, text in mf.QUERIES}

    class StubSearcher:
        def __init__(self, store, backend):
            pass

        def search(self, q, *, limit, scorer, **kw):
            calls.append((text_to_qid[q], limit, scorer))
            return [NS(path=f"/fake-corpus/{ep.docid_to_filename(d)}")
                    for d in ranking[text_to_qid[q]]]

    monkeypatch.setattr(ep, "Searcher", StubSearcher)
    out = ep.run_dataset_arms(store=None, backend=None, name=NAME, entry=entry,
                              unzipped=tree, arms=arms)
    out["calls"] = calls
    return out


#: scripted ranking for the stub (same list for both arms). The self-doc hit
#: ranks FIRST for self-doc-1 and q1 — the F1 drop must fire through the loop.
RANKING = {
    "self-doc-1": ["self-doc-1", "doc-echo", "doc-alpha"],
    "q-plain": ["doc-gamma", "doc-beta", "q1"],
    "q-graded": ["grade-two-doc", "doc-gamma", "doc-alpha"],
    "q-dangling": ["doc-alpha", "doc-beta"],
    "q-escape": ["weird/id 1", "doc-alpha"],
    "q1": ["q1", "doc-alpha", "doc-echo"],
    "q10": ["q1", "doc-echo", "doc-beta"],
}

#: 1/log2(3) — the rank-2 discount, appears throughout the hand values
L2 = 1.0 / math.log2(3)


# ------------------------------------------------ manifest (B7 + §3 schema)


def test_public_manifest_schema_and_licenses():
    manifest = ep.load_manifest()
    assert manifest["suite"] == "beir-mini" and manifest["schema"] == 1
    assert list(manifest["datasets"]) == ["scifact", "nfcorpus", "arguana"]
    for name, entry in manifest["datasets"].items():
        assert entry["url"].startswith(
            "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/")
        assert entry["n_docs"] > 0 and entry["n_queries_test"] > 0
        assert entry["grade_range"][0] >= 1
        assert entry["qrels_test"]["rows"] == sum(entry["qrels_test"]["grades"].values())
        assert entry["published_bm25"]["beir_paper"] is not None
        # review F4: every dataset carries a license; the NC pair names NC
        assert "license" in entry and entry["license"]
        if name in ("nfcorpus", "arguana"):
            assert "NC" in entry["license"]
    assert "unversioned" in manifest["provenance"]  # review F7
    assert "NC" in manifest["licenses_note"]
    assert "5,416,593" in manifest["excluded"]["climate-fever"]
    # review F2/F6 divergence footnotes are pinned as manifest notes
    notes = " ".join(manifest["notes"])
    assert "chunk-fragmented" in notes and "name-segment" in notes
    # quirks carry the F1/F5 rules
    assert any("fetch-deep-then-cut" in q for q in manifest["datasets"]["arguana"]["quirks"])
    assert any("69%" in q for q in manifest["datasets"]["arguana"]["quirks"])


def test_public_manifest_pins_are_the_verified_constants():
    """The reviewer re-verified these EXACT pins by downloading the real
    distributions (spec §2, 2026-10-19); a drift here is a deliberate edit."""
    manifest = ep.load_manifest()
    pins = {
        "scifact": (2_816_079,
                    "536e14446a0ba56ed1398ab1055f39fe852686ecad24a6306c80c490fa8e0165"),
        "nfcorpus": (2_448_432,
                     "efe5be03f8c5b86a5870102d0599d227c8c6e2484328e68c6522560385671b0b"),
        "arguana": (3_773_617,
                    "cfdf79adce27a401b3cd3ea267903134dbfab2c6afeb95d7fe5724a00bf7557b"),
    }
    for name, (n_bytes, sha) in pins.items():
        entry = manifest["datasets"][name]
        assert entry["bytes"] == n_bytes, name
        assert entry["sha256"] == sha, name
    assert sum(n for n, _ in pins.values()) == 9_038_128
    assert manifest["datasets"]["nfcorpus"]["grade_range"] == [1, 2]
    assert manifest["datasets"]["scifact"]["grade_range"] == [1, 1]
    assert manifest["datasets"]["arguana"]["n_queries_test"] == 1406


def test_public_manifest_vs_file_b7(mini_zip):
    """Hash mechanics use a portable fixture; acceptance assets owns real BEIR pins."""
    entry = entry_for(mini_zip)
    actual_sha, actual_size = ep.sha256_and_size(mini_zip)
    assert actual_sha == entry["sha256"]
    assert actual_size == entry["bytes"]
    mini_zip.write_bytes(mini_zip.read_bytes() + b"corruption")
    assert ep.sha256_and_size(mini_zip) != (entry["sha256"], entry["bytes"])


# --------------------------------------------------------------- downloader


def test_fetch_downloads_and_verifies(tmp_path, mini_zip):
    entry = entry_for(mini_zip)
    net = FakeNet([(200, mini_zip.read_bytes())])
    fetcher = ep.Fetcher(tmp_path, err=lambda m: None, sleep=net.sleep,
                         open_url=net.open_url)
    dest = fetcher.ensure_zip(NAME, entry, allow_download=True,
                              force_refresh=False)
    assert dest == ep.zip_path(tmp_path, NAME)
    assert dest.read_bytes() == mini_zip.read_bytes()
    assert net.sleeps == [] and len(net.calls) == 1
    assert not dest.with_name(dest.name + ".partial").exists()


def test_fetch_resume_range_honored(tmp_path, mini_zip):
    entry = entry_for(mini_zip)
    full = mini_zip.read_bytes()
    partial = ep.zip_path(tmp_path, NAME).with_name(NAME + ".zip.partial")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(full[:1000])
    net = FakeNet([(206, full[1000:])])
    fetcher = ep.Fetcher(tmp_path, err=lambda m: None, sleep=net.sleep,
                         open_url=net.open_url)
    dest = fetcher.ensure_zip(NAME, entry, allow_download=True,
                              force_refresh=False)
    assert dest.read_bytes() == full
    assert net.calls[0].headers.get("Range") == "bytes=1000-"


def test_fetch_200_after_range_restarts_from_zero(tmp_path, mini_zip):
    entry = entry_for(mini_zip)
    full = mini_zip.read_bytes()
    partial = ep.zip_path(tmp_path, NAME).with_name(NAME + ".zip.partial")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(full[:1000] + b"GARBAGE")
    # server ignores the Range and answers 200 with the FULL body
    net = FakeNet([(200, full)])
    fetcher = ep.Fetcher(tmp_path, err=lambda m: None, sleep=net.sleep,
                         open_url=net.open_url)
    dest = fetcher.ensure_zip(NAME, entry, allow_download=True,
                              force_refresh=False)
    assert dest.read_bytes() == full  # restarted, not appended


def test_fetch_sha_mismatch_deletes_partial_and_fails(tmp_path, mini_zip):
    entry = entry_for(mini_zip)
    n = len(mini_zip.read_bytes())
    bad = (b"corrupted!" * (n // 10 + 1))[:n]  # right size, wrong bytes
    net = FakeNet([(200, bad)])
    fetcher = ep.Fetcher(tmp_path, err=lambda m: None, sleep=net.sleep,
                         open_url=net.open_url)
    with pytest.raises(ep.PublicError, match="failed verification"):
        fetcher.ensure_zip(NAME, entry, allow_download=True, force_refresh=False)
    assert not ep.zip_path(tmp_path, NAME).exists()
    assert not ep.zip_path(tmp_path, NAME).with_name(NAME + ".zip.partial").exists()


def test_fetch_404_fails_fast_without_backoff(tmp_path, mini_zip):
    entry = entry_for(mini_zip)
    net = FakeNet([urllib.error.HTTPError("u", 404, "nope", {}, None)])
    fetcher = ep.Fetcher(tmp_path, err=lambda m: None, sleep=net.sleep,
                         open_url=net.open_url)
    with pytest.raises(ep.PublicError, match="pin is dead"):
        fetcher.ensure_zip(NAME, entry, allow_download=True, force_refresh=False)
    assert net.sleeps == [] and len(net.calls) == 1  # no retry burned


def test_fetch_backoff_on_500_then_success(tmp_path, mini_zip):
    entry = entry_for(mini_zip)
    net = FakeNet([urllib.error.HTTPError("u", 500, "boom", {}, None),
                   (200, mini_zip.read_bytes())])
    fetcher = ep.Fetcher(tmp_path, err=lambda m: None, sleep=net.sleep,
                         open_url=net.open_url)
    dest = fetcher.ensure_zip(NAME, entry, allow_download=True,
                              force_refresh=False)
    assert dest.is_file()
    assert net.sleeps == [ep.RETRY_BACKOFF_S[0]]  # 2 s first, model.py table


def test_fetch_disk_precheck_blocks_before_network(tmp_path, monkeypatch, mini_zip):
    entry = entry_for(mini_zip)
    monkeypatch.setattr(ep.shutil, "disk_usage",
                        lambda p: NS(total=1, used=1, free=0))
    net = FakeNet([])
    fetcher = ep.Fetcher(tmp_path, err=lambda m: None, sleep=net.sleep,
                         open_url=net.open_url)
    with pytest.raises(ep.PublicError, match="disk space"):
        fetcher.ensure_zip(NAME, entry, allow_download=True, force_refresh=False)
    assert net.calls == []  # refused before a single byte hit the network


def test_offline_missing_zip_never_touches_network(tmp_path, mini_zip):
    entry = entry_for(mini_zip)
    net = FakeNet([])
    fetcher = ep.Fetcher(tmp_path, err=lambda m: None, sleep=net.sleep,
                         open_url=net.open_url)
    with pytest.raises(ep.PublicError, match="no verified zip"):
        fetcher.ensure_zip(NAME, entry, allow_download=False,
                           force_refresh=False)
    assert net.calls == []


def test_cached_zip_verified_and_mismatch_never_patched(tmp_path, mini_zip):
    entry = place_zip(tmp_path, mini_zip)
    net = FakeNet([])
    fetcher = ep.Fetcher(tmp_path, err=lambda m: None, sleep=net.sleep,
                         open_url=net.open_url)
    # verified cache hit: no network at all
    dest = fetcher.ensure_zip(NAME, entry, allow_download=True,
                              force_refresh=False)
    assert dest.is_file() and net.calls == []
    # drifted cache + no explicit --download: loud error, cache NOT patched
    dest.write_bytes(dest.read_bytes() + b"drift")
    with pytest.raises(ep.PublicError, match="does not match the pin"):
        fetcher.ensure_zip(NAME, entry, allow_download=True,
                           force_refresh=False)
    assert net.calls == []


# ----------------------------------------------------------- md adapter (D5)


def test_doc_md_bytes_and_escaping():
    titled = {"_id": "d1", "title": "A Title", "text": "Body text"}
    assert ep.doc_md_bytes(titled) == b"# A Title\n\nBody text\n"
    # F5: empty title skips the header line entirely (title slot stays slug)
    empty = {"_id": "d2", "title": "", "text": "Body text"}
    assert ep.doc_md_bytes(empty) == b"Body text\n"
    assert ep.doc_md_bytes({"_id": "d3", "title": "  ", "text": "B"}) == b"B\n"
    assert ep.docid_to_filename("plain-id") == "plain-id.md"
    assert ep.docid_to_filename("weird/id 1") == "weird%2Fid%201.md"
    assert ep.docid_from_path("/x/weird%2Fid%201.md") == "weird/id 1"
    assert ep.docid_from_path("/x/plain-id.md") == "plain-id"


def test_write_corpus_md_sorted_and_complete(tmp_path):
    corpus = ep.load_corpus(mf.write_tree(tmp_path, graded=True))
    out = tmp_path / "corpus_md"
    docids = ep.write_corpus_md(corpus, out)
    assert docids == sorted(d for d, _, _ in mf.DOCS)
    files = sorted(p.name for p in out.glob("*.md"))
    assert files == sorted(ep.docid_to_filename(d) for d in docids)
    beta = out / "doc-beta.md"
    assert not beta.read_bytes().startswith(b"# ")  # F5 empty-title branch


def test_load_corpus_sorts_by_id(tmp_path):
    corpus = ep.load_corpus(mf.write_tree(tmp_path))
    ids = [d["_id"] for d in corpus]
    assert ids == sorted(ids)
    assert len(corpus) == mf.stats()["n_docs"]


def test_qrels_parsing_grade_range_and_header(tmp_path):
    tree = mf.write_tree(tmp_path, graded=True)
    qrels = ep.load_qrels(tree)
    st = mf.stats()
    assert qrels["q-graded"] == {"grade-two-doc": 2, "doc-gamma": 1}
    grades = sorted({g for gr in qrels.values() for g in gr.values()})
    assert [min(grades), max(grades)] == st["grade_range"] == [1, 2]
    assert sum(len(g) for g in qrels.values()) == st["n_qrels_rows"]
    # a header row (non-integer grade) is tolerated and skipped
    tsv = tree / "qrels" / "test.tsv"
    tsv.write_text("query-id\t0\tcorpus-id\tscore\n" + tsv.read_text())
    assert ep.load_qrels(tree) == qrels
    # LIVE-RUN CAUGHT: the real BEIR header is the 3-column shape, and
    # 3-column data rows (qid, docid, grade) parse with the docid in slot 1
    tree3 = mf.write_tree(tmp_path / "three", graded=True)
    body3 = "\n".join("\t".join([qid, docid, str(g)])
                      for qid, grades in sorted(qrels.items())
                      for docid, g in sorted(grades.items()))
    (tree3 / "qrels" / "test.tsv").write_text(
        "query-id\tcorpus-id\tscore\n" + body3 + "\n")
    assert ep.load_qrels(tree3) == qrels
    # binary variant flattens to grade 1
    tree_b = mf.write_tree(tmp_path / "bin", graded=False)
    grades_b = [g for gr in ep.load_qrels(tree_b).values() for g in gr.values()]
    assert set(grades_b) == {1}


def test_query_filtering_to_qrel_qids(tmp_path):
    tree = mf.write_tree(tmp_path)
    queries = ep.load_queries(tree)
    qrels = ep.load_qrels(tree)
    assert "q-noqrel" in queries  # present in queries.jsonl...
    assert "q-noqrel" not in qrels  # ...but filtered from scoring (D9a)
    assert len(set(qrels) & set(queries)) == mf.stats()["n_queries_test"]
    assert set(qrels) <= set(queries)  # no dangling query ids in the fixture


def test_dangling_qrel_stays_in_grades(tmp_path):
    tree = mf.write_tree(tmp_path)
    qrels = ep.load_qrels(tree)
    corpus_ids = {d["_id"] for d in ep.load_corpus(tree)}
    assert "missing-doc-x" in qrels["q-dangling"]
    assert "missing-doc-x" not in corpus_ids
    # it keeps its IDCG slot: a zero-gain hit list scores nDCG 0.0, NOT None
    m = ep.score_query_public(["doc-alpha"], qrels["q-dangling"], 2)
    assert m["ndcg"] == 0.0 and m["ndcg"] is not None
    # ...and no corpus doc carries that qrel, so ANY hit list gains 0 here
    m2 = ep.score_query_public(["doc-beta"], qrels["q-dangling"], 2)
    assert m2["ndcg"] == 0.0


# ---------------------------------------------- F1 fetch-deep-then-cut + ns


def test_fetch_deep_cut_drops_self_then_cuts():
    """F1 fixture assertion: self-doc at rank 1 + the relevant doc at RAW rank
    11 — fetch-deep-then-cut keeps it at rank 10; cut-then-drop would lose it
    AND hand the query a 9-hit window (the 69%-of-arguana bias class)."""
    hits = [NS(path=f"/c/doc-{i:02d}.md") for i in range(20)]
    hits[0] = NS(path="/c/self.md")
    hits[10] = NS(path="/c/relevant-doc.md")
    docids = [ep.docid_from_path(h.path) for h in hits]
    kept = ep.apply_fetch_deep_cut(hits, docids, "self")
    assert len(kept) == 10
    assert ep.docid_from_path(kept[-1].path) == "relevant-doc"
    assert all(ep.docid_from_path(h.path) != "self" for h in kept)
    # the biased alternative for contrast: cut first, drop second
    biased = [h for h in hits[:10]
              if ep.docid_from_path(h.path) != "self"]
    assert len(biased) == 9 and "relevant-doc" not in str([h.path for h in biased])


def test_qid_docid_namespace_safety():
    hits = [NS(path="/c/q1.md"), NS(path="/c/doc-echo.md")]
    docids = [ep.docid_from_path(h.path) for h in hits]
    # exact match drops; a longer qid sharing the prefix must NOT drop
    assert [ep.docid_from_path(h.path)
            for h in ep.apply_fetch_deep_cut(hits, docids, "q1")] == ["doc-echo"]
    assert len(ep.apply_fetch_deep_cut(hits, docids, "q10")) == 2
    assert len(ep.apply_fetch_deep_cut(hits, docids, "Q1")) == 2  # case-exact
    assert len(ep.apply_fetch_deep_cut(hits, docids, "q11")) == 2


def test_run_arms_fetches_deep_and_drops_self(monkeypatch, tmp_path, mini_zip):
    tree = mf.write_tree(tmp_path, graded=True)
    entry = entry_for(mini_zip, graded=True)
    out = run_with_stub_searcher(monkeypatch, RANKING, tree, entry,
                                 ["hybrid", "lexical"])
    # F1: the retrieval is DEEP (limit=20), for every query and both arms
    assert out["calls"], "stub searcher recorded nothing"
    assert all(limit == ep.FETCH_LIMIT for _q, limit, _s in out["calls"])
    assert {s for _q, _l, s in out["calls"]} == {"hybrid", "lexical"}
    # the drop fired through the loop: self-doc-1 keeps 2 hits, not 3
    rows = {r["query"]["id"]: r["metrics"]
            for arm in out["rows_by_arm"].values() for r in arm}
    assert rows["self-doc-1"]["n_hits"] == 2
    assert rows["q1"]["n_hits"] == 2  # doc q1 dropped for query q1
    assert rows["q10"]["n_hits"] == 3  # doc q1 kept for query q10 (namespace)


def test_hand_computed_metrics_through_the_loop(monkeypatch, tmp_path, mini_zip):
    """Controlled ranking -> exact metric values (trec_eval convention, D7)."""
    tree = mf.write_tree(tmp_path, graded=True)
    entry = entry_for(mini_zip, graded=True)  # grade_range [1, 2]
    out = run_with_stub_searcher(monkeypatch, RANKING, tree, entry, ["lexical"])
    rows = {r["query"]["id"]: r["metrics"] for r in out["rows_by_arm"]["lexical"]}
    g = rows["q-graded"]
    assert (g["success1"], g["success5"], g["rr"], g["ndcg"]) == (1.0, 1.0, 1.0, 1.0)
    p = rows["q-plain"]  # grade-1 positive vs max grade 2: no success credit
    assert (p["success1"], p["success5"], p["rr"]) == (0.0, 0.0, 0.0)
    assert p["ndcg"] == pytest.approx(L2)  # 1/log2(3), the only gain
    d = rows["q-dangling"]
    assert d["ndcg"] == 0.0 and not d["ndcg"] is None
    s = rows["self-doc-1"]  # its grade-1 positive gives ndcg 1.0, no success
    assert (s["success1"], s["rr"], s["ndcg"]) == (0.0, 0.0, 1.0)
    e = rows["q-escape"]  # grade-1 gain still feeds nDCG under max grade 2
    assert e["ndcg"] == 1.0 and e["success5"] == 0.0
    # aggregates over the 7 scored queries
    summary = er.summarize(out["rows_by_arm"]["lexical"])
    assert summary["success@5"]["n"] == 7
    # only q-graded carries a grade-2 positive (the max grade): success/MRR
    # credit is impossible for every other query in the graded variant
    assert summary["success@5"]["value"] == pytest.approx(1 / 7)
    assert summary["success@1"]["value"] == pytest.approx(1 / 7)
    assert summary["MRR"]["value"] == pytest.approx(1 / 7)
    # nDCG credits all grade-1 gains regardless of the threshold
    assert summary["nDCG@10"]["value"] == pytest.approx(
        (1 + L2 + 1 + 0 + 1 + 1 + L2) / 7)


def test_binary_variant_threshold_flips_success(monkeypatch, tmp_path, mini_zip_binary):
    tree = mf.write_tree(tmp_path / "b", graded=False)
    entry = entry_for(mini_zip_binary, graded=False)  # grade_range [1, 1]
    out = run_with_stub_searcher(monkeypatch, RANKING, tree, entry, ["lexical"])
    summary = er.summarize(out["rows_by_arm"]["lexical"])
    # every grade-1 positive now counts: 6 of 7 queries have one in top-5
    assert summary["success@5"]["value"] == pytest.approx(6 / 7)
    assert summary["success@1"]["value"] == pytest.approx(4 / 7)
    assert summary["MRR"]["value"] == pytest.approx(5 / 7)


# ------------------------------------------------- nDCG trec_eval convention


def test_hand_computed_ndcg_graded_trec_convention():
    """D7: linear gain (= grade), log2(rank+1) discount, IDCG = top-10 of the
    query's own grades. Hand value for grades {d1:2, d2:1}, hits [d2, d1]."""
    grades = {"d1": 2, "d2": 1}
    m = ep.score_query_public(["d2", "d1"], grades, 2)
    dcg = 1.0 / math.log2(2) + 2.0 / math.log2(3)   # g1 at rank 1, g2 at rank 2
    idcg = 2.0 / math.log2(2) + 1.0 / math.log2(3)  # ideal: grade 2 first
    assert m["ndcg"] == pytest.approx(dcg / idcg)   # ≈ 0.8597, NOT 1.0
    # ideal ordering: grade-2 doc at rank 1 scores 1.0
    m2 = ep.score_query_public(["d1", "d2"], grades, 2)
    assert m2["ndcg"] == pytest.approx(1.0)
    # a grade-2 hit beats two grade-1 hits in the ideal but not per-rank
    g3 = {"a": 2, "b": 1, "c": 1}
    m3 = ep.score_query_public(["b", "c"], g3, 2)
    idcg3 = 2.0 + 1.0 / math.log2(3) + 1.0 / math.log2(4)  # ideal [2,1,1]
    assert m3["ndcg"] == pytest.approx((1.0 + 1.0 / math.log2(3)) / idcg3)


def test_hand_computed_ndcg_dangling_caps_below_one():
    m = ep.score_query_public(["a"], {"a": 1, "ghost": 1}, 1)
    idcg = 1.0 + 1.0 / math.log2(3)
    assert m["ndcg"] == pytest.approx(1.0 / idcg)
    assert 0 < m["ndcg"] < 1.0


def test_success_threshold_is_max_grade():
    grades = {"low": 1, "top": 2}
    # a grade-1 hit at rank 1: success metrics demand the MAX grade (D8)
    m = ep.score_query_public(["low"], grades, 2)
    assert (m["success1"], m["success5"], m["rr"]) == (0.0, 0.0, 0.0)
    # graded nDCG still credits the hit — but the unreachable grade-2 ghost
    # occupies ideal mass (dangling parity): ndcg = 1 / (2 + 1/log2 3) ≈ 0.380
    assert m["ndcg"] == pytest.approx(1.0 / (2.0 + 1.0 / math.log2(3)))
    # the same list under a binary regime (max grade 1) succeeds
    m2 = ep.score_query_public(["low"], grades, 1)
    assert (m2["success1"], m2["success5"], m2["rr"]) == (1.0, 1.0, 1.0)


def test_ndcg_ideal_is_cut_at_ten():
    grades = {f"d{i}": 1 for i in range(11)}
    m = ep.score_query_public(["d0"], grades, 1)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(10))  # 10 slots, not 11
    assert m["ndcg"] == pytest.approx(1.0 / idcg)


def test_binom_ci_safe_variant_matches_house_reference():
    """LIVE-RUN CAUGHT (arguana n=1406): er._binom_tail_le underflows at
    large n and binom_ci(854, 1406) degenerates to [0.411, 0.411]. The public
    suite's log-space variant must (a) reproduce the house n=146 reference
    and (b) stay non-degenerate where the golden one breaks."""
    lo, hi = ep.binom_ci_lp(109, 146)
    assert abs(lo - 0.668) < 0.001 and abs(hi - 0.815) < 0.001
    assert (lo, hi) == pytest.approx(er.binom_ci(109, 146), abs=1e-9)
    # the degenerate case: k/n = 0.6074 -> CI ≈ ±1.96*sqrt(pq/n) = ±0.0256
    lo2, hi2 = ep.binom_ci_lp(854, 1406)
    assert lo2 < hi2
    assert abs(lo2 - 0.5818) < 0.003 and abs(hi2 - 0.6326) < 0.003
    # ...while the golden harness's own value IS degenerate (documented here
    # so the divergence is loud, not silent)
    assert er.binom_ci(854, 1406)[0] == er.binom_ci(854, 1406)[1]


def test_headline_cis_public_arguana_scale():
    rows = [{"query": {"id": f"q{i}", "tag": "public/x"},
             "metrics": {"success1": 1.0 if i < 366 else 0.0,
                         "success5": 1.0 if i < 854 else 0.0,
                         "rr": 0.5, "ndcg": 0.5, "n_hits": 10,
                         "noise": False}} for i in range(1406)]
    cis = ep.headline_cis_public(rows)
    assert cis["success@5"] == "[0.581, 0.633]"
    assert cis["success@1"].startswith("[0.23")
    assert cis["MRR"] == "[0.500, 0.500]"  # constant series: degenerate bootstrap CI


def test_empty_hits_stay_honest():
    m = ep.score_query_public([], {"a": 1}, 1)
    assert m == {"success1": 0.0, "success5": 0.0, "rr": 0.0, "ndcg": 0.0,
                 "n_hits": 0, "noise": False}


# ------------------------------------------------- eval_real machinery reuse


def test_rows_flow_through_eval_real_machinery(monkeypatch, tmp_path, mini_zip):
    tree = mf.write_tree(tmp_path)
    entry = entry_for(mini_zip)
    out = run_with_stub_searcher(monkeypatch, RANKING, tree, entry, ["lexical"])
    rows = out["rows_by_arm"]["lexical"]
    current = er.summarize(rows)
    assert set(current) == set(er.TRACKED_METRICS)
    assert current["lookup success@1"] == {"value": None, "n": 0}
    assert current["zero-hit honesty"]["n"] == 0
    pq = er.per_query_maps(rows)
    assert set(pq["nDCG@10"]) == {r["query"]["id"] for r in rows}
    cis = ep.headline_cis_public(rows)
    assert all(cis[k].startswith("[") for k in
               ("success@5", "success@1", "MRR", "nDCG@10"))
    # identical baseline stays green; a fully-flipped rerun trips (n=20
    # discordant pairs -> exact McNemar p << 0.05 AND delta -1.0)
    gate = er.compare_to_baseline(current, pq, {"metrics": current,
                                                "per_query": pq})
    assert gate["tripped"] == []

    def all_success(v: float) -> list[dict]:
        return [{"query": {"id": f"q{i:03d}", "tag": "public/x"},
                 "metrics": {"success1": v, "success5": v, "rr": v,
                             "ndcg": v, "n_hits": 10, "noise": False}}
                for i in range(20)]
    base = er.summarize(all_success(1.0))
    base_pq = er.per_query_maps(all_success(1.0))
    gate2 = er.compare_to_baseline(
        er.summarize(all_success(0.0)), er.per_query_maps(all_success(0.0)),
        {"metrics": base, "per_query": base_pq})
    assert "success@5" in gate2["tripped"]


def test_one_hit_per_doc_invariant_is_enforced(monkeypatch, tmp_path, mini_zip):
    tree = mf.write_tree(tmp_path)
    entry = entry_for(mini_zip)

    class DupeSearcher:
        def __init__(self, store, backend):
            pass

        def search(self, q, *, limit, scorer, **kw):
            return [NS(path="/c/doc-alpha.md"), NS(path="/c/doc-alpha.md")]

    monkeypatch.setattr(ep, "Searcher", DupeSearcher)
    with pytest.raises(ep.PublicError, match="invariant"):
        ep.run_dataset_arms(None, None, NAME, entry, tree, ["lexical"])


# ------------------------------------------------------------ table + gate


def _fake_results() -> dict:
    """A results dict shaped exactly like main()'s, from synthetic rows."""
    rows = []
    for i in range(20):
        ok = i < 12
        rows.append({"query": {"id": f"q{i:03d}", "tag": "public/scifact"},
                     "metrics": {"success1": 1.0 if ok else 0.0,
                                 "success5": 1.0 if i < 15 else 0.0,
                                 "rr": 1.0 if ok else 0.4,
                                 "ndcg": 0.9 if ok else 0.2,
                                 "n_hits": 10, "noise": False}})
    metrics = er.summarize(rows)
    return {"scifact": {"n_queries": 20, "n_corpus": 100,
                        "wall_search_s": 0.1,
                        "arms": {"hybrid": {"metrics": metrics,
                                            "cis": ep.headline_cis_public(rows),
                                            "rows": rows}}},
            "nfcorpus": {"n_queries": 5, "n_corpus": 50, "wall_search_s": 0.1,
                         "arms": {"lexical": {
                             "metrics": metrics,
                             "cis": ep.headline_cis_public(rows), "rows": rows}}},
            "arguana": {"n_queries": 5, "n_corpus": 50, "wall_search_s": 0.1,
                        "arms": {"hybrid": {
                            "metrics": metrics,
                            "cis": ep.headline_cis_public(rows), "rows": rows}}}}


def test_render_table_rows_refs_and_footnotes():
    manifest = ep.load_manifest()
    out = ep.render_table(_fake_results(), manifest, backend="llama")
    # one row per arm + published refs + delta
    assert "hybrid (ours)" in out and "BM25 (ours, lexical)" in out
    assert "BM25 (published)" in out
    assert "paper 0.6650" in out and "paper 0.3180" in out and "paper 0.3970" in out
    assert "2CR flat" in out and "≈0.3168" in out
    assert "Δ hybrid − best pub" in out
    # divergence footnotes (F1/F2/F6 + z-gate sparsity + context-not-gate)
    assert "fetch-deep-then-cut" in out and "69%" in out
    assert "one-hit-per-doc" in out
    assert "13.0%" in out and "17.4%" in out and "21.9%" in out
    assert "title-in-header" in out
    assert "name-segment" in out
    assert "z-gate" in out
    assert "never a gate" in out
    # mock banner appears for mock runs, not llama
    assert "mechanics-only" not in out
    out_mock = ep.render_table(_fake_results(), manifest, backend="mock")
    assert "mechanics-only" in out_mock


def test_baseline_roundtrip_gate_green_then_trips(tmp_path):
    """--update-baseline roundtrip + advisory gate (D11): an identical rerun
    is green; a collapsed success@5 trips (and the report says advisory)."""
    zp = mf.build_mini_zip(tmp_path / "beir" / f"{NAME}.zip")
    entry = entry_for(zp)  # real bytes+sha: the gate re-hashes the real zip
    manifest = {"datasets": {NAME: {"sha256": entry["sha256"],
                                    "bytes": entry["bytes"]}}}
    results = _fake_results()
    results = {NAME: results["scifact"]}
    baseline = ep.build_baseline(results, manifest, tmp_path, backend="mock",
                                 arms=["hybrid"], datasets=[NAME])
    assert baseline["schema"] == 1 and baseline["backend"] == "mock"
    assert baseline["identities"]["zip_sha256"][NAME] == entry["sha256"]
    bp = tmp_path / "public_baseline.json"
    bp.write_text(json.dumps(baseline))
    loaded = json.loads(bp.read_text())

    text, tripped = ep.run_gate(results, loaded, manifest, tmp_path,
                                backend="mock", datasets=[NAME])
    assert tripped == [] and "gate: green" in text

    # a real regression: success@5 collapses on the hybrid arm (20/20 flips)
    bad = json.loads(json.dumps(results))
    for r in bad[NAME]["arms"]["hybrid"]["rows"]:
        r["metrics"]["success5"] = 0.0
    bad[NAME]["arms"]["hybrid"]["metrics"] = er.summarize(
        bad[NAME]["arms"]["hybrid"]["rows"])
    text2, tripped2 = ep.run_gate(bad, loaded, manifest, tmp_path,
                                  backend="mock", datasets=[NAME])
    assert tripped2 == [f"{NAME}/hybrid/success@5"]
    assert "GATE TRIPPED" in text2 and "advisory" in text2


def _sha_side_effect(p):
    """sha256_and_size stand-in: zips map to their dataset pin char."""
    return (p.stem[0] * 64, 1)


def test_gate_identity_and_backend_mismatch_exit_2(tmp_path):
    manifest = {"datasets": {"scifact": {"sha256": "a" * 64}}}
    results = {"scifact": _fake_results()["scifact"]}
    baseline = ep.build_baseline(results, manifest, tmp_path, backend="mock",
                                 arms=["hybrid"], datasets=["scifact"])
    baseline["identities"]["chunker_version"] = "ancient"
    with pytest.raises(ep.PublicError, match="identities drifted"):
        ep.run_gate(results, baseline, manifest, tmp_path, backend="mock",
                    datasets=["scifact"])
    ok = ep.build_baseline(results, manifest, tmp_path, backend="mock",
                           arms=["hybrid"], datasets=["scifact"])
    with pytest.raises(ep.PublicError, match="not comparable"):
        ep.run_gate(results, ok, manifest, tmp_path, backend="llama",
                    datasets=["scifact"])


def test_baseline_provenance_and_identities_shape(tmp_path):
    manifest = ep.load_manifest()
    results = _fake_results()
    baseline = ep.build_baseline(results, manifest, tmp_path, backend="mock",
                                 arms=["hybrid", "lexical"],
                                 datasets=["scifact"])
    assert baseline["identities"]["prompt_scheme"].startswith("v1:")
    assert baseline["identities"]["manifest_sha256"] == ep.sha256_and_size(
        ep.PUBLIC_MANIFEST_JSON)[0]
    assert baseline["identities"]["zip_sha256"]["scifact"] == \
        manifest["datasets"]["scifact"]["sha256"]
    assert baseline["datasets"]["scifact"]["n_queries"] == 20
    assert "advisory v1" in " ".join(baseline["notes"])


# ---------------------------------------------------------------- e2e mock


def _prepare_and_run(root: Path, zip_path: Path, entry: dict) -> dict:
    prep = ep.prepare_dataset(NAME, entry, root, backend_name="mock")
    from goldmine.backends.mock import MockBackend

    store = Store(prep["db"])
    try:
        out = ep.run_dataset_arms(store, MockBackend(), NAME, entry,
                                  root / "beir" / NAME,
                                  ["hybrid", "lexical"])
    finally:
        store.close()
    out["prep"] = prep
    return out


def test_prepare_marker_and_backend_switch_refusal(tmp_path):
    zp = mf.build_mini_zip(tmp_path / "beir" / f"{NAME}.zip")
    entry = entry_for(zp)
    root = tmp_path
    prep = ep.prepare_dataset(NAME, entry, root, backend_name="mock")
    assert prep["rebuilt"] and prep["n_docs"] == mf.stats()["n_docs"]
    assert prep["report"]["content_indexed"] == mf.stats()["n_docs"]
    marker = json.loads((root / "beir" / NAME / "prepared.json").read_text())
    assert marker["backend"] == "mock" and marker["zip_sha256"] == entry["sha256"]
    # second call: fresh, no rebuild
    again = ep.prepare_dataset(NAME, entry, root, backend_name="mock")
    assert again["rebuilt"] is False
    # backend switch refuses loudly (F9) unless --rebuild
    with pytest.raises(ep.PublicError, match="refuses mixing vectors"):
        ep.prepare_dataset(NAME, entry, root, backend_name="llama")
    # corrupting the marker's zip sha forces an auto-rebuild (derived state)
    marker["zip_sha256"] = "0" * 64
    (root / "beir" / NAME / "prepared.json").write_text(json.dumps(marker))
    rebuilt = ep.prepare_dataset(NAME, entry, root, backend_name="mock")
    assert rebuilt["rebuilt"] is True


def test_e2e_mock_twice_identical(tmp_path, mini_zip):
    """§9.6: fixture → (pre-placed verified zip) → corpus_md → ingest mock →
    both arms → table; runs twice, identical metrics."""
    root = tmp_path
    zp = mf.build_mini_zip(root / "beir" / f"{NAME}.zip")
    entry = entry_for(zp)
    out1 = _prepare_and_run(root, zp, entry)
    out2 = _prepare_and_run(root, zp, entry)
    assert out1["n_queries"] == mf.stats()["n_queries_test"]
    m1 = {arm: er.summarize(rows) for arm, rows in out1["rows_by_arm"].items()}
    m2 = {arm: er.summarize(rows) for arm, rows in out2["rows_by_arm"].items()}
    assert m1 == m2  # determinism (the offline half of review F8)
    # the lexical leg retrieves: the sourdough query finds the empty-title doc
    lex = {r["query"]["id"]: r["metrics"]
           for r in out1["rows_by_arm"]["lexical"]}
    assert lex["q-plain"]["n_hits"] >= 1
    assert lex["q-escape"]["n_hits"] >= 1  # percent-escaped doc is findable
    # the escaped docid roundtrips through the F1 drop and scoring namespace
    table = ep.render_table(
        {NAME: {"n_queries": out1["n_queries"],
                "n_corpus": entry["n_docs"], "wall_search_s": 0.0,
                "arms": {arm: {"metrics": er.summarize(rows),
                               "cis": ep.headline_cis_public(rows)}
                         for arm, rows in out1["rows_by_arm"].items()}}},
        {"datasets": {NAME: entry}}, backend="mock")
    assert "minicorp" in table and "BM25 (ours, lexical)" in table
