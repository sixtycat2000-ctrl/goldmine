"""Nested qualified reconstruction: module.Class.attr needs every component
present as a stored surface (R6a).

Preregistered in reports/execution/P0-05/qualified-nested-001. A fabricated
tail or a fabricated middle qualifier binds nowhere, so the C-INT clause
still refuses; only real enum/class member chains gain an owner.
"""
from __future__ import annotations

import pytest

from goldmine.search import Searcher
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "nested.sqlite")
    yield store
    store.close()


def add_module(store, path, body):
    fid = store.upsert_file(FileRec(path=path, kind="code", state="done", capability="content_indexed"))
    store.replace_segments(fid, [
        SegmentRec(kind="name", modality="none", text_for_fts="owned folder"),
        SegmentRec(kind="chunk", modality="none", loc=156, loc_end=203, text_for_fts=body),
    ])
    return fid


@pytest.fixture
def ssl_index(index):
    add_module(index, "/owned/ssl.py",
               "@_simple_enum(_IntEnum)\nclass TLSVersion(_IntEnum):\n"
               "    MINIMUM_SUPPORTED = _ssl.PROTO_MINIMUM_SUPPORTED\n"
               "    TLSv1_2 = _ssl.PROTO_TLSv1_2\n"
               "    TLSv1_3 = _ssl.PROTO_TLSv1_3\n"
               "    def wrap_socket(self, sock):\n        return sock\n")
    return index


def hits(store, query):
    return Searcher(store, None).search(query, scorer="lexical", why=True)


def test_nested_member_resolves_and_ranks_the_definition_row(ssl_index):
    found = hits(ssl_index, "ssl.TLSVersion.TLSv1_2")
    assert found and found[0].why["exact_match_type"] == "qualified_components"
    assert "TLSv1_2 = _ssl.PROTO_TLSv1_2" in ssl_index.segment_text(found[0].segment_id)
    assert found[0].admission_reason == "verified_exact"


@pytest.mark.parametrize("tail", ["TLSv1_9", "TLSv1_2b", "wrap"])
def test_fabricated_tail_still_refuses(ssl_index, tail):
    assert hits(ssl_index, f"ssl.TLSVersion.{tail}") == []


@pytest.mark.parametrize("middle", ["TLSVerzion", "SSLContext"])
def test_fabricated_middle_qualifier_refuses(ssl_index, middle):
    assert hits(ssl_index, f"ssl.{middle}.TLSv1_2") == []


def test_two_component_behaviour_is_unchanged(ssl_index):
    add_module(ssl_index, "/owned/wirefmt.py", "def encode_field(value): pass")
    found = hits(ssl_index, "wirefmt.encode_field")
    assert found and found[0].why["exact_match_type"] == "qualified_components"
    # the module itself without the symbol still refuses
    assert hits(ssl_index, "wirefmt.encode_field_v2") == []


def test_package_init_route_supports_nested_members(index):
    add_module(index, "/owned/httpx/__init__.py", "class _Client(BaseClient):\n    _transport = None\n")
    found = hits(index, "httpx._Client._transport")
    assert found and found[0].why["exact_match_type"] == "qualified_components"


def test_bare_colon_class_header_alone_is_a_conservative_miss(index):
    # body_surfaces keeps the colon of a bare `class Foo:` header as part of the
    # lexeme (the C1 colon-word shape), so a qualifier that appears ONLY there
    # does not verify. Real modules reference the class elsewhere; this pins
    # the strict side of the rule rather than loosening surface matching.
    add_module(index, "/owned/lone.py", "class Only:\n    member = 1\n")
    assert hits(index, "lone.Only.member") == []


def test_literal_binding_outranks_reconstruction(ssl_index):
    add_module(ssl_index, "/owned/notes.py", "ssl.TLSVersion.TLSv1_2 mentioned verbatim")
    found = hits(ssl_index, "ssl.TLSVersion.TLSv1_2")
    assert found and found[0].path == "/owned/notes.py" and found[0].why["exact_source"] == "body"
