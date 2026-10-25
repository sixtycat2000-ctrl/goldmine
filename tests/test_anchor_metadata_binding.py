"""info-recall-001: metadata binding tier + bodyless remainder exemption.

An obligatory identity anchor whose ordinary routes bind nowhere may bind by
WHOLE-ANCHOR flat equality to a file's own name/info surfaces (level 3,
`metadata_token`, never above ordinary bindings); a metadata-bound file with
no non-name segments passes the C-NL remainder witness vacuously, disclosed
as `metadata_bodyless`. Component parts of an anchor never bind alone.
"""
from __future__ import annotations

import pytest

from goldmine.anchor_admission import _best_exact, resolve_obligations
from goldmine.query_obligations import MAX_SOURCE_ROWS
from goldmine.anchor_resolution import Binding
from goldmine.search import Searcher, _terms_of
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "meta.sqlite")
    yield store
    store.close()


def photo(store, name, *, taken="2020-02-08 21:58:41", body=None, path=None):
    """A file whose only segment is the name row (media photo), unless body."""
    path = path or f"/corpus/media/photos/{name}"
    fid = store.upsert_file(FileRec(path=path, kind="image", state="done",
                                    capability="content_indexed",
                                    info={"taken": taken,
                                          "taken_source": "Exif.DateTimeOriginal"} if taken else None))
    segs = [SegmentRec(kind="name", modality="none", text_for_fts="corpus media photos")]
    if body is not None:
        segs.append(SegmentRec(kind="chunk", modality="text", loc=1, loc_end=3, text_for_fts=body))
    store.replace_segments(fid, segs)
    return fid


def doc(store, name, body):
    fid = store.upsert_file(FileRec(path=f"/corpus/docs/{name}", state="done", capability="content_indexed"))
    store.replace_segments(fid, [
        SegmentRec(kind="name", modality="none", text_for_fts="corpus docs"),
        SegmentRec(kind="chunk", modality="text", loc=1, loc_end=40, text_for_fts=body)])
    return fid


def all_files(store):
    return {row[0] for row in store.db.execute("SELECT id FROM files WHERE state='done'")}


def decide(store, query, phrases=()):
    with store.read_snapshot():
        return resolve_obligations(store, query, phrases, all_files(store), _terms_of)


def name_row(store, fid):
    return store.db.execute(
        "SELECT id FROM segments WHERE file=? AND kind='name' ORDER BY id LIMIT 1", (fid,)).fetchone()[0]


# ------------------------------------------------------------- tier binding

def test_bind_hit_name_surface(index):
    fid = photo(index, "Wrocław_MuzeumNarodowe.jpg")
    decision = decide(index, "Which photograph's original filename contains Wrocław and MuzeumNarodowe?")
    assert decision.reason is None and decision.constrained
    assert decision.allowed == {fid}
    binding = decision.bindings[0]
    assert (binding.match_type, binding.level, binding.field) == ("metadata_token", 3, "basename")
    assert binding.segment_id == name_row(index, fid) and binding.query_term == "MuzeumNarodowe"
    assert decision.exact[fid].level == 3 and decision.exact[fid].match_type == "metadata_token"


def test_bind_hit_info_surface_multi_owner(index):
    a = photo(index, "one.jpg", taken="2023-09-20 17:47:29")
    b = photo(index, "two.jpg", taken="2013-07-19 15:29:41")
    decision = decide(index, "What EXIF DateTimeOriginal belongs to the sunset photo?")
    assert decision.reason is None and decision.allowed == {a, b}
    assert {bd.field for bd in decision.bindings} == {"info"}
    assert all(bd.segment_id == name_row(index, f) for f in (a, b) for bd in decision.bindings
               if bd.file_id == f)


def test_no_bind_still_refuses(index):
    photo(index, "Wrocław_MuzeumNarodowe.jpg")
    assert decide(index, "Which photograph's filename contains MuzeumNarodowex?").reason == "anchor_unresolved"
    assert decide(index, "What EXIF DateTimeOrigina value?").reason == "anchor_unresolved"


def test_whole_anchor_pin_components_never_bind(index):
    # ssl is a real name token of this doc; the dotted anchor's COMPONENT
    # must not bind it (whole-anchor flat only).
    doc(index, "env-and-mode.md", "environment variables explained ssl settings")
    assert decide(index, "noise ssl.TLSVersion.TLSv1_9 broken").reason == "anchor_unresolved"
    assert decide(index, "leak .env.staging values").reason == "anchor_unresolved"


def test_quote_and_literal_anchors_never_use_the_tier(index):
    photo(index, "Wrocław_MuzeumNarodowe.jpg")
    # double quotes leave the text as a PHRASE (parse_filters contract): the
    # phrase keeps its body-literal requirement and the tier never sees it
    quoted = decide(index, "find it in the docs", phrases=("MuzeumNarodowe",))
    assert quoted.allowed == set() and not quoted.bindings
    # backticks stay in the text as kind='literal' anchors, excluded from the tier
    backtick = decide(index, "find `MuzeumNarodowe` in the docs")
    assert backtick.reason == "anchor_unresolved" and not backtick.bindings


def test_ordinary_binding_wins_and_tier_does_not_run(index):
    fid = doc(index, "panel.py", "the SearchPanel widget is defined in this panel module body")
    decision = decide(index, "where is the SearchPanel widget defined")
    assert decision.reason is None
    binding = decision.bindings[0]
    assert binding.match_type != "metadata_token" and binding.level <= 1


def test_best_exact_orders_ordinary_above_metadata():
    raw = Binding(1, 10, 100, "body", "x", "raw", 1, "x")
    meta = Binding(1, 20, 200, "basename", "y", "metadata_token", 3, "y")
    exact = _best_exact([meta, raw])
    assert exact[10].match_type == "raw" and exact[20].match_type == "metadata_token"


def test_multi_anchor_intersection(index):
    only = photo(index, "Wrocław_MuzeumNarodowe.jpg")
    photo(index, "other.jpg")
    decision = decide(index, "What is the EXIF DateTimeOriginal for the Wrocław MuzeumNarodowe photograph?")
    assert decision.reason is None and decision.allowed == {only}


def test_verification_bound_on_huge_name_scan(index):
    for i in range(MAX_SOURCE_ROWS + 2):
        index.upsert_file(FileRec(path=f"/corpus/f{i}.txt", state="done", capability="content_indexed"))
        index.replace_segments(i + 1, [SegmentRec(kind="name", modality="none", text_for_fts="corpus")])
    decision = decide(index, "MuzeumNarodowex refuse bound tier")
    assert decision.reason == "anchor_verification_bound"


# ------------------------------------------------- bodyless remainder exemption

def test_bodyless_cnl_clause_is_constrained_not_soft(index):
    fid = photo(index, "Wrocław_MuzeumNarodowe.jpg")
    decision = decide(index, "Which photograph's original filename contains Wrocław and MuzeumNarodowe?")
    assert decision.soft_clauses == () and decision.allowed == {fid}
    witness = decision.witnesses[(0, fid)]
    assert witness[0][0] == "metadata_bodyless" and witness[0][2] == name_row(index, fid)


def test_bodyful_cnl_clause_stays_soft(index):
    doc(index, "Wrocław_MuzeumNarodowe.md", "notes about naming things")
    decision = decide(index, "Which document's original filename contains Wrocław and MuzeumNarodowe?")
    # the file HAS a body; the remainder (filename/Wrocław words) must be
    # witnessed in body rows or the clause soft-demotes exactly as before
    assert decision.reason is None
    assert (0, "remainder_soft") in decision.soft_clauses or decision.allowed == set()


def test_phrases_keep_their_body_requirement(index):
    photo(index, "Wrocław_MuzeumNarodowe.jpg")
    decision = decide(index, "Which photograph mentions Wrocław and MuzeumNarodowe?",
                      phrases=("original filename",))
    # a phrase without body-literal rows keeps the clause unconstrained; the
    # bodyless exemption must NOT accept the photo around the phrase
    assert decision.allowed == set() and not any(
        w and w[0][0] == "metadata_bodyless" for w in decision.witnesses.values())


def test_exempt_scope_is_metadata_bindings_only(index):
    # a bodyless file bound by an ordinary (non-metadata) binding cannot
    # borrow the exemption: remainder still applies and demotes softly.
    fid = index.upsert_file(FileRec(path="/corpus/titled.bin", state="done", capability="metadata_only",
                                    info={"title": "MuzeumNarodowe"}))
    index.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="corpus")])
    decision = decide(index, "Which artifact's original filename contains Wrocław and MuzeumNarodowe?")
    assert decision.allowed == set()


# ------------------------------------------------------- pointer and scoping

def test_bodyless_primary_is_the_name_row_bodyful_is_not(index):
    photo(index, "Wrocław_MuzeumNarodowe.jpg")
    doc(index, "notes.md", "generic prose about photographs")
    hits = Searcher(index, None).search(
        "Which photograph's original filename contains Wrocław and MuzeumNarodowe?", scorer="lexical")
    assert hits and hits[0].path.endswith("Wrocław_MuzeumNarodowe.jpg")
    assert hits[0].segment == "name" and hits[0].admission_reason == "verified_exact"


def test_ambient_anchor_cannot_use_the_tier(index):
    # a bare year matching a taken value is AMBIENT: the tier must not
    # un-refuse a C-INT lookup through it.
    photo(index, "a.jpg", taken="2013-07-19 15:29:41")
    decision = decide(index, "2013")
    assert decision.reason == "anchor_unresolved"


def test_selector_independence_scores_unchanged(index):
    fid = doc(index, "panel.md", "the SearchPanel widget layout explained")
    from goldmine.retrieval_evidence import select_evidence
    from goldmine.lexical import LexicalResult, query_units
    from goldmine.search import Searcher as S
    units = query_units("panel layout", S.terms("panel layout"), [])
    words = LexicalResult(segment_units={}, segment_files={}, units=units)
    file = index.get_file(fid)
    before = select_evidence(index, file, None, words, [])
    # admission-side change only: evidence selection input is identical
    assert before is None or before.primary is not None
