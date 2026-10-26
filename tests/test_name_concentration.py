"""search.v8 name-concentration (name-concentration-001): a file whose NAME
surface carries >= 3 distinct latin query units admits via name_quorum and
takes a flat NAME_HINT bonus in place of the kind hint. CJK units never count;
the matcher is column-filtered FTS on the runtime unit patterns.
"""
from __future__ import annotations

import pytest

from goldmine.admission import AdmissionParameters, decide_admission
from goldmine.search import NAME_HINT, NAME_UNIT_FLOOR, Searcher
from goldmine.store import FileRec, SegmentRec, Store
from test_search_offline import FixedBackend, evec


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "name.sqlite")
    yield store
    store.close()


def named(store, name, *, body=None):
    path = f"/corpus/photos/{name}"
    fid = store.upsert_file(FileRec(path=path, kind="image", state="done", capability="content_indexed"))
    segs = [SegmentRec(kind="name", modality="none", text_for_fts="corpus photos")]
    if body is not None:
        segs.append(SegmentRec(kind="chunk", modality="text", loc=1, loc_end=9, text_for_fts=body))
    store.replace_segments(fid, segs)
    return fid


def all_files(store):
    return {row[0] for row in store.db.execute("SELECT id FROM files WHERE state='done'")}


# ------------------------------------------------------------- admission

def test_decide_admission_name_paths_and_precedence():
    assert decide_admission(scorer="hybrid", name_hit=True) == "name_quorum"
    assert decide_admission(scorer="hybrid", name_hit=True, z=4.0, z_gate=3.0) == "name_quorum_standout"
    # exact outranks the name path
    assert decide_admission(scorer="hybrid", name_hit=True, exact=True) == "verified_exact"
    # coverage quorum still works without a name hit
    assert decide_admission(scorer="hybrid", coverage=0.6) == "lexical_quorum"
    # ablations never take the name path
    assert decide_admission(scorer="lexical", name_hit=True) is None
    assert decide_admission(scorer="semantic", name_hit=True) is None


# ----------------------------------------------------- matcher semantics

def test_three_latin_name_units_admit_and_outrank(index):
    photo = named(index, "ship_felicità_kremlin_3551.jpg")
    rival = named(index, "other.txt", body="ship fleet sea harbor naval")
    hits = Searcher(index, None).search(
        "ship kremlin 3551 photograph", scorer="hybrid", why=True)
    assert hits and hits[0].path.endswith("ship_felicità_kremlin_3551.jpg")
    assert hits[0].admission_reason in ("name_quorum", "name_quorum_standout")


def test_two_units_do_not_admit(index):
    # two name hits of five units: coverage 0.4 < quorum and 2 < floor
    named(index, "ship_kremlin.jpg")
    hits = Searcher(index, None).search(
        "ship kremlin photograph harbor original", scorer="hybrid")
    assert hits == []


def test_cjk_units_never_count_toward_the_floor(index):
    # four CJK units would clear a naive token floor; the latin-only matcher
    # must not admit (and CJK coverage is not the quorum's unit set either).
    named(index, "vue_组件_说明.md")
    # five CJK units of which two are name-side: CJK coverage 2/5 < quorum
    hits = Searcher(index, None).search("组件 说明 文档 配置 参考", scorer="hybrid")
    assert hits == []


def test_porter_stemming_is_the_runtime_matcher(index):
    # 'felicità' stems to 'felicit' — it must NOT count toward the floor
    # (query unit) even though the name token is 'felicità'.
    named(index, "ship_felicità_kremlin_3551.jpg")
    hits = Searcher(index, None).search(
        "ship felicità kremlin photograph original", scorer="hybrid")
    # ship + felicità(no stem match) + kremlin = 2 < 3, coverage 2/5 < quorum
    assert hits == []


def test_hint_replaces_kind_hint_not_stacks(index):
    # an image file (kind hint would fire) with >=3 name units gets exactly
    # NAME_HINT, not NAME_HINT + 1.0.
    photo = named(index, "alpha_beta_gamma.jpg")
    hits = Searcher(index, None).search("alpha beta gamma", scorer="hybrid", why=True)
    assert hits and hits[0].path.endswith("alpha_beta_gamma.jpg")
    # score = w_c*cov^1.5 + NAME_HINT (kind hint suppressed); with cov=1.0
    # and lookup weights off (no digits): 2.0*1.0 + 2.5 = 4.5
    assert hits[0].score == pytest.approx(2.0 * 1.0 + NAME_HINT)


def test_floor_constant_is_three():
    assert NAME_UNIT_FLOOR == 3 and NAME_HINT == 2.5
