"""v2 items 15/16: search output excellence + query filters.

Item 15 (D4·D5·D7): «»-marked term-density snippets (render-time, from the
segment's fts original/body), near-dup collapse, weaker-matches fold, --why
decomposition, and the single z-gate qualification rule (noise queries ->
zero hits; v2.1: gate 3.0, no separate floor).
Item 16 (D6): kind:/under:/after:/before:/site: + quoted phrases, applied
pre-fusion at the _allowed_files seam; unknown prefixes stay literal.

Fusion math itself is pinned by tests/test_search_offline.py (unmodified).
"""

from __future__ import annotations

import datetime
import json
import math

import numpy as np
import pytest

from goldmine.cli import render_hits
from goldmine.search import Hit, Searcher, parse_filters
from goldmine.store import FileRec, SegmentRec, Store
from goldmine.util import excerpt as make_excerpt


def evec(i: int) -> np.ndarray:
    v = np.zeros(768, dtype=np.float32)
    v[i] = 1.0
    return v


def cos_vec(c: float, dim: int) -> np.ndarray:
    """Unit vector at cosine exactly `c` to evec(0), tilted into `dim`."""
    v = c * evec(0) + math.sqrt(max(0.0, 1.0 - c * c)) * evec(dim)
    return v.astype(np.float32)


class FixedBackend:
    """Searcher only needs embed_texts; a duck-typed stand-in keeps these
    tests independent of the conftest backend registry."""

    def __init__(self, vec: np.ndarray):
        self.vec = np.asarray(vec, dtype=np.float32)

    def embed_texts(self, texts):
        return np.stack([self.vec for _ in texts])


def add_file(
    store: Store,
    path: str,
    texts: list[str],
    vecs: list[np.ndarray | None],
    kind: str = "doc",
    capability: str = "content_indexed",
    modality: str = "text",
    source: str = "file",
    mtime: float | None = None,
    content_date: float | None = None,
    excerpts: list[str] | None = None,
) -> int:
    fid = store.upsert_file(
        FileRec(path=path, kind=kind, size=10, state="pending",
                source=source, mtime=mtime)
    )
    store.mark_file(fid, state="done", capability=capability, detail=None,
                    info=None, extractor_version="txt-stdlib.v1",
                    content_date=content_date)
    segs = [SegmentRec(kind="name", modality="none", excerpt=path.split("/")[-1],
                       text_for_fts=f"folder words {path}")]
    for i, (text, vec) in enumerate(zip(texts, vecs)):
        segs.append(
            SegmentRec(
                kind="chunk", modality=modality if vec is not None else "none",
                loc=i + 1, loc_end=i + 1,
                excerpt=(excerpts[i] if excerpts else text),
                text_for_fts=text, vec=vec,
            )
        )
    store.replace_segments(fid, segs)
    return fid


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.sqlite")
    yield s
    s.close()


# ---------------------------------------------------------------- item 15.1


def test_snippet_window_marks_cluster_not_chunk_start(store):
    """The «» snippet comes from the segment's FULL text (fts original/body)
    with a term-density window; the STORED excerpt is untouched."""
    body = ("filler sentence one. " * 20
            + "the needle sits deep in this chunk body and the needle repeats. "
            + "filler sentence two. " * 20)
    add_file(store, "/docs/deep.txt", [body], [evec(0)],
             excerpts=["chunk-start filler words"])
    clean, flt = parse_filters("needle")
    hits = Searcher(store, FixedBackend(evec(0))).search(clean, filters=flt)
    assert hits and hits[0].path == "/docs/deep.txt"
    h = hits[0]
    assert h.excerpt == "chunk-start filler words"      # stored excerpt unchanged
    assert h.excerpt_marked is not None
    assert h.excerpt_marked.count("«needle»") == 2
    assert "sits" in h.excerpt_marked                    # window covers the cluster


def test_snippet_cjk_marks_without_whitespace(store):
    lead = "开头部分的填充内容。" * 30
    body = lead + "关键词出现在这里。" + "后续中文内容。" * 5
    add_file(store, "/docs/cjk.txt", [body], [evec(0)])
    for i in range(25):
        add_file(store, f"/f/f{i}.txt", ["filler text here"], [evec(1)])
    clean, flt = parse_filters('"关键词"')  # CJK reaches search as a quoted phrase
    hits = Searcher(store, FixedBackend(evec(0))).search(clean, filters=flt)
    assert hits, "CJK phrase + semantic arm must qualify the file"
    marked = hits[0].excerpt_marked
    assert marked is not None and "«关键词»" in marked
    assert marked.startswith("…")            # anchored at the match, not text start
    assert "出现在这里" in marked


def test_snippet_missing_body_falls_back_to_stored_excerpt(store):
    """A segment with a vector but no fts row (pure image) keeps its stored
    excerpt — no crash, no fabricated snippet."""
    fid = store.upsert_file(FileRec(path="/img/pic2.png", kind="image", size=10,
                                    state="pending"))
    store.mark_file(fid, state="done", capability="content_indexed", detail=None,
                    info=None, extractor_version="img.v1")
    store.replace_segments(fid, [
        SegmentRec(kind="name", modality="none", excerpt="pic2",
                   text_for_fts="folder words /img/pic2.png"),
        SegmentRec(kind="image", modality="image", loc=1, excerpt="stored visual",
                   text_for_fts=None, vec=evec(0)),
    ])
    for i in range(25):
        add_file(store, f"/f/f{i}.txt", ["filler text here"], [evec(1)])
    hits = Searcher(store, FixedBackend(evec(0))).search("qqq zzq")  # no lexical overlap
    by_path = {h.path: h for h in hits}
    h = by_path["/img/pic2.png"]
    assert h.segment_id is not None
    assert h.excerpt_marked is None                   # no body -> no marked snippet
    assert h.excerpt == "stored visual"               # stored excerpt kept


def test_snippet_disabled_by_width_none(store):
    body = "a needle in this body " * 40
    add_file(store, "/docs/plain.txt", [body], [evec(0)])
    hits = Searcher(store, FixedBackend(evec(0))).search(
        "needle", snippet_width=None
    )
    assert hits and hits[0].excerpt_marked is None


def test_snippet_prefix_last_marks_whole_word(store):
    """While-typing: FTS matched kubernetes via the prefix star; the mark
    extends through the whole word (DigUp inflects parity)."""
    add_file(store, "/docs/k8s.txt", ["the kubernetes cluster setup guide"], [evec(0)])
    hits = Searcher(store, FixedBackend(evec(0))).search("kubernet")
    assert hits and hits[0].excerpt_marked == "the «kubernetes» cluster setup guide"


# ---------------------------------------------------------------- item 15.2


def test_near_dup_sha_fold_identical_text(store):
    add_file(store, "/a/one.txt", ["identical body text here"], [None])
    add_file(store, "/b/two.txt", ["identical body text here"], [None])
    hits = Searcher(store, None).search("identical body", scorer="lexical")
    assert len(hits) == 2
    assert hits[1].duplicate_of == hits[0].rank
    assert hits[0].duplicate_of is None


def test_near_dup_vector_fold_identical_vectors(store):
    """Different text, same vector (cos 1.0 >= 0.97): folds under the
    higher-ranked hit."""
    add_file(store, "/a/one.txt", ["first wording of the note"], [evec(0)])
    add_file(store, "/b/two.txt", ["second wording of the note"], [evec(0)])
    for i in range(25):
        add_file(store, f"/f/f{i}.txt", ["unrelated content"], [evec(i % 20 + 1)])
    hits = Searcher(store, FixedBackend(evec(0))).search("qqq zzq")  # semantic only
    assert len(hits) == 2
    assert hits[0].path == "/a/one.txt"  # tie broken by file id
    assert hits[1].duplicate_of == hits[0].rank


def test_near_dup_below_threshold_not_folded(store):
    add_file(store, "/a/one.txt", ["first wording of the note"], [evec(0)])
    add_file(store, "/b/two.txt", ["second wording of the note"], [evec(2)])
    hits = Searcher(store, FixedBackend(evec(0))).search("qqq zzq")
    assert all(h.duplicate_of is None for h in hits)


def test_weaker_fold_human_only(store):
    """Hits below 0.5x top fold into a trailing line in HUMAN output only;
    --json keeps every hit (scripts need the data)."""
    add_file(store, "/w/full.txt", ["alpha beta gamma delta here"], [evec(1)])
    add_file(store, "/w/three.txt", ["alpha beta gamma elsewhere"], [evec(2)])
    add_file(store, "/w/two.txt", ["alpha beta only"], [evec(3)])
    hits = Searcher(store, FixedBackend(evec(30))).search("alpha beta gamma delta")
    assert [round(h.score, 3) for h in hits] == [2.0, 1.299, 0.707]
    human = render_hits(hits, json_mode=False)
    assert "1. score 2.00" in human and "2. score 1.30" in human
    assert "3. score" not in human                      # folded away
    assert human.endswith("… +1 weaker matches")
    loaded = json.loads(render_hits(hits, json_mode=True))
    assert [h["rank"] for h in loaded] == [1, 2, 3]     # json keeps everything


def test_no_weaker_fold_when_all_strong(store):
    add_file(store, "/w/a.txt", ["alpha beta gamma delta here"], [evec(1)])
    add_file(store, "/w/b.txt", ["alpha beta gamma elsewhere"], [evec(2)])
    hits = Searcher(store, FixedBackend(evec(30))).search("alpha beta gamma delta")
    human = render_hits(hits, json_mode=False)
    assert "weaker matches" not in human


def test_duplicate_line_in_human_output(store):
    add_file(store, "/a/one.txt", ["identical body text here"], [None])
    add_file(store, "/b/two.txt", ["identical body text here"], [None])
    add_file(store, "/c/three.txt", ["different words entirely"], [None])
    hits = Searcher(store, None).search("identical body", scorer="lexical", limit=10)
    human = render_hits(hits, json_mode=False)
    assert "+1 duplicate" in human
    loaded = json.loads(render_hits(hits, json_mode=True))
    assert any(h.get("duplicate_of") is not None for h in loaded)


# ---------------------------------------------------------------- item 15.3


def test_why_json_shape(store):
    add_file(store, "/docs/why.txt", ["needle unique sentence"], [evec(0)])
    for i in range(25):
        add_file(store, f"/f/f{i}.txt", ["filler text here"], [evec(1)])
    hits = Searcher(store, FixedBackend(evec(0))).search("needle", why=True)
    w = hits[0].why
    assert w is not None
    assert w["w_m"] == 1.0 and w["w_c"] == 2.0
    # v2.1: the z gate is the single qualification rule (default 3.0); the
    # v2.0 separate standout floor ("confidence_z") is gone from the why dict
    assert w["z_gate"] == 3.0 and "confidence_z" not in w
    assert w["lookup"] is False
    assert w["z"] == pytest.approx(5.0, abs=1e-6)
    assert w["bm25_rank"] == 1
    assert set(w["score_parts"]) == {"z", "cov"}
    assert sum(w["score_parts"].values()) == pytest.approx(hits[0].score, rel=1e-6)
    # additive: without why=True the decomposition is not attached
    plain = Searcher(store, FixedBackend(evec(0))).search("needle")
    assert plain[0].why is None


def test_why_segment_margin_zero_when_spread(store):
    """A file hit spread across identical segments: margin best-vs-next = 0."""
    add_file(store, "/docs/two.txt", ["needle unique sentence",
                                      "needle second copy"], [evec(0), evec(0)])
    for i in range(25):
        add_file(store, f"/f/f{i}.txt", ["filler text here"], [evec(1)])
    hits = Searcher(store, FixedBackend(evec(0))).search("needle", why=True)
    assert hits[0].why["segment_margin_z"] == pytest.approx(0.0, abs=1e-9)


def test_why_human_line_and_json_flag(store, capsys):
    from goldmine.cli import main

    add_file(store, "/docs/why.txt", ["the tolerations section explains"], [None])
    store.close()
    argv = ["--backend", "mock", "--db", str(store.path), "search",
            "--scorer", "lexical", "tolerations"]
    rc = main([*argv, "--json"])
    assert rc == 0
    plain = json.loads(capsys.readouterr().out)
    assert "why" not in plain[0]  # additive: no --why, no why key

    rc = main([*argv, "--why"])
    assert rc == 0
    human = capsys.readouterr().out
    assert "why:" in human and "cov^1.5" in human

    rc = main([*argv, "--why", "--json"])
    assert rc == 0
    hit = json.loads(capsys.readouterr().out)[0]
    assert hit["why"]["coverage"] == pytest.approx(1.0)


def test_render_why_line_components():
    hit = Hit(
        rank=1, score=3.41, z=3.12, cosine=0.9, coverage=0.5, kind="doc",
        path="/n/k8s.md", loc=120, loc_end=128, segment="chunk",
        excerpt="x", file_status="content_indexed", scorer="hybrid",
        bm25_rank=2,
        why={"w_m": 1.0, "w_c": 2.0, "z_gate": 3.0,
             "lookup": True, "z": 3.12, "cosine": 0.9, "coverage": 0.5,
             "bm25_rank": 2, "kind_hint": ["doc"], "kind_boost": 1.0,
             "score_parts": {"z": 3.12, "cov": 0.29, "kind": 1.0}, "score": 3.41,
             "segment_margin_z": 1.22},
    )
    line = render_hits([hit], json_mode=False, why=True)
    assert "why: 1·z 3.12 + 2·cov^1.5 0.50 + kind doc 1.0 = 3.41" in line
    assert "bm25 #2" in line and "lookup weights" in line
    assert "seg-margin 1.22σ" in line
    loaded = json.loads(render_hits([hit], json_mode=True, why=True))
    assert loaded[0]["why"]["bm25_rank"] == 2
    assert loaded[0]["excerpt"] == "x"  # excerpt key untouched by --why


# ------------------------------------------- single z-gate rule (v2.1, 15.4)


def _graded_noise_corpus(store: Store) -> None:
    """26 files whose cosine to the query vector is graded so the top z lands
    in [2, 2.5): per-query standardization WOULD hand back 3 hits for a pure
    noise query (the W2-A finding) — the gate must not."""
    for i, c in enumerate((1.0, 0.95, 0.9, 0.85, 0.8)):
        add_file(store, f"/n/g{i}.txt", [f"gibberish text number {i}"],
                 [cos_vec(c, i + 1)])
    for i in range(21):
        add_file(store, f"/f/filler{i}.txt", ["unrelated corpus words"],
                 [evec(100 + i)])


def test_z_gate_refuses_midband_candidates_on_noise(store):
    _graded_noise_corpus(store)
    searcher = Searcher(store, FixedBackend(evec(0)))
    # the pure-semantic ablation keeps the z gate as its whole contract; pin
    # it explicitly at 2.0 to show the 2σ band the standardization produces
    # (the expert A/B tightens the default to 3.0, so 2.0 is now opt-in)
    sem = searcher.search("zzqwk flurbat", scorer="semantic", z_gate=2.0)
    assert len(sem) == 3 and all(h.z >= 2.0 for h in sem)
    # hybrid at the v2.1 default gate 3.0 refuses the whole [2, 2.5) band —
    # no lexical anchor arm anymore -> honest zero hits (A/B: s@5 0.747 ->
    # 0.781, honesty 0.4 -> 0.6, zero per-tag regressions)
    hits = searcher.search("zzqwk flurbat", scorer="hybrid")
    assert hits == []


def test_z_gate_keeps_standouts(store):
    """The gate's whole point: z >= 3.0 with zero coverage still qualifies."""
    add_file(store, "/t/target.txt", ["needle unique sentence"], [evec(0)])
    for i in range(25):
        add_file(store, f"/f/f{i}.txt", ["filler text here"], [evec(1)])
    hits = Searcher(store, FixedBackend(evec(0))).search("qqq zzq")  # no lexical match
    assert len(hits) == 1 and hits[0].path == "/t/target.txt"


def test_coverage_quorum_half_anchors(store):
    """Coverage >= 0.5 quorum admits a zero-z candidate (arm untouched by the
    v2.1 gate raise); a WEAKER anchor no longer qualifies — the v2.0
    'any coverage' anchor arm is gone with the single rule."""
    _graded_noise_corpus(store)
    # 2 of the 3 query terms -> coverage 2/3 >= 0.5: qualifies on the quorum
    add_file(store, "/n/anchored.txt", ["gibberish text zzqwk flurbat"],
             [cos_vec(0.95, 9)])
    # 1 of 3 terms -> coverage 1/3 < 0.5, z < 3 gate: refused (the v2.0
    # floor would have admitted it via cov > 0)
    add_file(store, "/n/weak.txt", ["plain filler zzqwk"], [cos_vec(0.95, 11)])
    hits = Searcher(store, FixedBackend(evec(0))).search("zzqwk flurbat qworble")
    assert [h.path for h in hits] == ["/n/anchored.txt"]


# ---------------------------------------------------------------- item 16


def test_parse_filters_grammar():
    clean, flt = parse_filters('kind:PDF|doc needle under:~/docs "exact phrase"')
    assert clean == "needle"
    assert flt.kinds == {"pdf", "doc"}
    assert flt.under[0].endswith("/docs")  # ~ expanded
    assert flt.phrases == ["exact phrase"]
    assert flt.after is None and flt.before is None
    assert bool(flt) is True


def test_parse_filters_dates_relative_and_absolute():
    clean, flt = parse_filters("needle after:2026-01-02 before:3w")
    assert clean == "needle"
    assert flt.after == datetime.datetime(2026, 1, 2).timestamp()
    assert flt.before is not None and flt.before > flt.after
    clean, flt = parse_filters("needle after:7d")
    assert flt.after == pytest.approx(
        (datetime.datetime.now() - datetime.timedelta(days=7))
        .replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )


def test_parse_filters_unknown_prefix_and_bad_values_stay_literal():
    for query in ("foo:bar needle", "kind:bogus needle", "needle after:yesterday",
                  "under: needle", "kind: needle"):
        clean, flt = parse_filters(query)
        assert not flt, query
        assert "needle" in clean or query.startswith(("under:", "kind:"))


def test_parse_filters_case_insensitive_prefixes():
    clean, flt = parse_filters("KIND:Pdf needle SITE:Example.com")
    assert flt.kinds == {"pdf"}
    assert flt.site == ["example.com"]
    assert clean == "needle"


def test_filter_kinds_applied_at_seam(store):
    add_file(store, "/docs/note.txt", ["needle unique sentence"], [None])
    add_file(store, "/docs/paper.pdf", ["needle unique sentence"], [None],
             kind="pdf")
    s = Searcher(store, None)
    clean, flt = parse_filters("needle kind:doc")
    hits = s.search(clean, scorer="lexical", filters=flt)
    assert [h.kind for h in hits] == ["doc"]
    clean, flt = parse_filters("needle kind:pdf|doc")
    assert sorted(h.kind for h in s.search(clean, scorer="lexical", filters=flt)) == [
        "doc", "pdf"]
    clean, flt = parse_filters("needle KIND:PDF")
    assert [h.kind for h in s.search(clean, scorer="lexical", filters=flt)] == ["pdf"]


def test_filter_kinds_intersect_flag_kinds(store):
    add_file(store, "/docs/note.txt", ["needle unique sentence"], [None])
    add_file(store, "/docs/paper.pdf", ["needle unique sentence"], [None], kind="pdf")
    clean, flt = parse_filters("needle kind:pdf")
    assert Searcher(store, None).search(
        clean, scorer="lexical", filters=flt, kinds={"doc"}) == []


def test_filter_under_prefix(store):
    add_file(store, "/docs/sub/needle.txt", ["needle unique sentence"], [None])
    add_file(store, "/docs-other/needle.txt", ["needle unique sentence"], [None])
    add_file(store, "/other/needle.txt", ["needle unique sentence"], [None])
    clean, flt = parse_filters("needle under:/docs")
    hits = Searcher(store, None).search(clean, scorer="lexical", filters=flt)
    assert [h.path for h in hits] == ["/docs/sub/needle.txt"]


def test_filter_under_tilde_expansion(store, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "docs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    add_file(store, str(home / "docs" / "needle.txt"), ["needle unique sentence"],
             [None])
    add_file(store, "/var/needle.txt", ["needle unique sentence"], [None])
    clean, flt = parse_filters("needle under:~/docs")
    hits = Searcher(store, None).search(clean, scorer="lexical", filters=flt)
    assert [h.path for h in hits] == [str(home / "docs" / "needle.txt")]


def _epoch(y, m, d):
    return datetime.datetime(y, m, d).timestamp()


def test_filter_dates_coalesce_content_date_then_mtime(store):
    add_file(store, "/d/a.txt", ["needle unique sentence"], [None],
             mtime=_epoch(2020, 1, 1), content_date=_epoch(2026, 1, 15))
    add_file(store, "/d/b.txt", ["needle unique sentence"], [None],
             mtime=_epoch(2026, 2, 1))
    add_file(store, "/d/c.txt", ["needle unique sentence"], [None],
             mtime=_epoch(2026, 2, 1), content_date=_epoch(2020, 6, 1))
    s = Searcher(store, None)
    clean, flt = parse_filters("needle after:2026-01-01")
    # content_date wins over mtime: a (2026 content, 2020 mtime) passes,
    # c (2020 content) does not — even though its mtime is 2026
    assert sorted(h.path for h in s.search(clean, scorer="lexical", filters=flt)) == [
        "/d/a.txt", "/d/b.txt"]
    clean, flt = parse_filters("needle after:2026-01-20")
    assert [h.path for h in s.search(clean, scorer="lexical", filters=flt)] == [
        "/d/b.txt"]
    clean, flt = parse_filters("needle before:2021-01-01")
    assert [h.path for h in s.search(clean, scorer="lexical", filters=flt)] == [
        "/d/c.txt"]


def test_filter_dates_url_fetched_at(store):
    add_file(store, "https://a.example/x", ["needle unique sentence"], [None],
             source="url")
    with store.write_transaction():
        store.db.execute(
            "UPDATE files SET info = ? WHERE path = 'https://a.example/x'",
            (json.dumps({"fetched_at": _epoch(2026, 3, 1)}),))
    add_file(store, "https://b.example/y", ["needle unique sentence"], [None],
             source="url", mtime=_epoch(2020, 1, 1))
    s = Searcher(store, None)
    clean, flt = parse_filters("needle after:2026-01-01")
    hits = s.search(clean, scorer="lexical", filters=flt)
    assert [h.path for h in hits] == ["https://a.example/x"]


def test_filter_site_host(store):
    add_file(store, "https://example.com/page", ["needle unique sentence"], [None],
             source="url")
    add_file(store, "https://other.com/page", ["needle unique sentence"], [None],
             source="url")
    add_file(store, "/docs/local.txt", ["needle unique sentence"], [None])
    clean, flt = parse_filters("needle site:example.com")
    hits = Searcher(store, None).search(clean, scorer="lexical", filters=flt)
    assert [h.path for h in hits] == ["https://example.com/page"]


def test_filter_quoted_phrase(store):
    add_file(store, "/p/adjacent.txt", ["the cluster setup guide here"], [None])
    add_file(store, "/p/separated.txt", ["cluster and the setup docs"], [None])
    s = Searcher(store, None)
    clean, flt = parse_filters('"cluster setup"')
    assert clean == "" and flt.phrases == ["cluster setup"]
    hits = s.search(clean, scorer="lexical", filters=flt)
    assert [h.path for h in hits] == ["/p/adjacent.txt"]


def test_filter_phrase_and_terms_combine(store):
    add_file(store, "/p/both.txt", ["needles and cluster setup steps"], [None])
    add_file(store, "/p/loose.txt", ["needles but no phrase here"], [None])
    clean, flt = parse_filters('needles "cluster setup"')
    assert clean == "needles"
    hits = Searcher(store, None).search(clean, scorer="lexical", filters=flt)
    # the phrase is an FTS pattern, not a hard filter: the bare term still
    # anchors loose.txt, but phrase+term coverage ranks the phrase file first
    assert [h.path for h in hits] == ["/p/both.txt", "/p/loose.txt"]
    assert hits[0].coverage == pytest.approx(1.0)
    assert hits[1].coverage == pytest.approx(0.5)


def test_filter_zero_candidates_clean_zero_hit(store):
    add_file(store, "/docs/needle.txt", ["needle unique sentence"], [None])
    clean, flt = parse_filters("needle under:/nonexistent")
    hits = Searcher(store, None).search(clean, scorer="lexical", filters=flt)
    assert hits == []
    clean, flt = parse_filters("needle kind:audio")
    assert Searcher(store, None).search(clean, scorer="lexical", filters=flt) == []


def test_json_filters_applied_echo(store, capsys):
    from goldmine.cli import main

    add_file(store, "/docs/needle.txt", ["needle unique sentence"], [None])
    store.close()
    rc = main(["--backend", "mock", "--db", str(store.path), "search",
               "--scorer", "lexical", "needle", "kind:doc", "under:/docs", "--json"])
    assert rc == 0
    hits = json.loads(capsys.readouterr().out)
    assert hits and hits[0]["filters_applied"] == {
        "kind": ["doc"], "under": ["/docs"]}
    # no filters parsed -> the key is absent (json shape stays additive)
    rc = main(["--backend", "mock", "--db", str(store.path), "search",
               "--scorer", "lexical", "needle", "--json"])
    hits = json.loads(capsys.readouterr().out)
    assert rc == 0 and all("filters_applied" not in h for h in hits)


def test_cli_zero_candidate_filter_honest_zero_hit(store, tmp_path, capsys):
    from goldmine.cli import main

    add_file(store, "/docs/needle.txt", ["needle unique sentence"], [None])
    store.close()
    rc = main(["--backend", "mock", "--db", str(store.path), "search",
               "--scorer", "lexical", "needle", "under:/nonexistent"])
    assert rc == 0
    assert "nothing found for 'needle under:/nonexistent'" in capsys.readouterr().out


def test_cli_snippet_render_human_and_raw_json(store, capsys):
    from goldmine.cli import main

    body = "the tolerations section sits deep in this document body"
    add_file(store, "/docs/tol.txt", [body], [None])
    store.close()
    rc = main(["--backend", "mock", "--db", str(store.path), "search",
               "--scorer", "lexical", "tolerations"])
    assert rc == 0
    human = capsys.readouterr().out
    assert "«tolerations»" in human

    rc = main(["--backend", "mock", "--db", str(store.path), "search",
               "--scorer", "lexical", "tolerations", "--json", "--raw"])
    hit = json.loads(capsys.readouterr().out)[0]
    assert rc == 0
    assert "«tolerations»" in hit["excerpt_marked"]
    assert "«" not in hit["excerpt"] and hit["excerpt"] == body  # stored clean

    rc = main(["--backend", "mock", "--db", str(store.path), "search",
               "--scorer", "lexical", "tolerations", "--json"])
    hit = json.loads(capsys.readouterr().out)[0]
    assert rc == 0 and "excerpt_marked" not in hit


def test_render_hits_stored_excerpt_trim_still_applies():
    """Without a marked snippet, the stored excerpt keeps the P3-13 trim."""
    hit = Hit(
        rank=1, score=1.0, z=None, cosine=None, coverage=1.0, kind="doc",
        path="/n/a.txt", loc=1, loc_end=1, segment="chunk",
        excerpt="x" * 400, file_status="content_indexed", scorer="lexical",
    )
    out = render_hits([hit], json_mode=False, excerpt_chars=240)
    assert make_excerpt("x" * 400, 240) in out
    out = render_hits([hit], json_mode=False, excerpt_chars=0)
    assert '   "' not in out
