"""Exact pointer prefers a code definition row over an earlier mention (R2).

Preregistered in reports/execution/P0-05/exact-definition-site-001. Owner
sets never change: the dedup key per (anchor, file, field, match_type) is
the same; only which row of an already-bound code file is kept.
"""
from __future__ import annotations

import pytest

from goldmine.anchor_resolution import definition_site
from goldmine.search import Searcher
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "defsite.sqlite")
    yield store
    store.close()


HEADER = '"""Base16, Base32, Base64 encodings."""\n\n__all__ = [\n    "b64encode", "b64decode",\n    "BOM_UTF32_BE", "header_encode",\n]\n'
DEFINITION = "\n\ndef b64encode(s, altchars=None):\n    \"\"\"Encode the bytes-like object s using Base64.\"\"\"\n    return binascii.b2a_base64(s)\n"
ASSIGNMENT = "\n# UTF-32, big endian\nBOM_UTF32_BE = b'\\x00\\x00\\xfe\\xff'\nheader_encode = decode\n"
USAGE = "\n\ndef encode_all(payload):\n    return b64encode(payload) + BOM_UTF32_BE\n"


def add(store, path, kind, *texts):
    fid = store.upsert_file(FileRec(path=path, state="done", capability="content_indexed", kind=kind))
    segs = [SegmentRec(kind="name", modality="none", text_for_fts="explicit folder")]
    for n, text in enumerate(texts, 1):
        segs.append(SegmentRec(kind="chunk", modality="none", loc=n * 10, loc_end=n * 10 + 5, text_for_fts=text))
    store.replace_segments(fid, segs)
    return fid


def hits(store, query):
    return Searcher(store, None).search(query, scorer="lexical", why=True)


def test_definition_site_grammar():
    assert definition_site("def b64encode(s):\n    pass", "base64.b64encode")
    assert definition_site("class TLSVersion(_IntEnum):\n", "ssl.TLSVersion")
    assert definition_site("async def fetch(x):", "fetch")
    assert definition_site("BOM_UTF32_BE = b'..'", "BOM_UTF32_BE")
    assert definition_site("    header_encode: Callable = decode", "header_encode")
    assert definition_site("const MAX_RETRIES = 3;", "MAX_RETRIES")
    assert definition_site("#define BUF_SIZE 4096", "BUF_SIZE")
    assert definition_site("fn parse_args() -> Args {", "parse_args")
    # mentions and comparisons are not definitions
    assert not definition_site("    'b64encode', 'b64decode',", "b64encode")
    assert not definition_site("if mode == BOM_UTF32_BE:", "BOM_UTF32_BE")
    assert not definition_site("    return b64encode(payload)", "b64encode")
    assert not definition_site("def b64encode_legacy(s):", "b64encode")
    assert not definition_site("", "x") and not definition_site("def x():", "")


@pytest.mark.parametrize("query,expected_loc", [
    ("base64.b64encode", 20),   # qualified components: definition row
    ("b64encode", 20),          # raw identifier: definition row
    ("BOM_UTF32_BE", 30),       # assignment row
    ("header_encode", 30),      # alias assignment row (base64mime: body_decode = decode)
])
def test_code_exact_pointer_lands_on_the_definition_row(index, query, expected_loc):
    add(index, "/explicit/base64.py", "code", HEADER, DEFINITION, ASSIGNMENT, USAGE)
    found = hits(index, query)
    assert found and found[0].path == "/explicit/base64.py"
    assert found[0].why["exact_source"] == "body"
    assert found[0].loc == expected_loc
    assert query.rsplit(".", 1)[-1] in index.segment_text(found[0].segment_id)


def test_prose_files_keep_the_earliest_mention(index):
    add(index, "/explicit/notes.md", "doc", HEADER, DEFINITION, ASSIGNMENT)
    found = hits(index, "b64encode")
    assert found and found[0].why["exact_source"] == "body" and found[0].loc == 10


def test_owner_set_and_ranking_are_unchanged_by_the_tiebreak(index):
    add(index, "/explicit/base64.py", "code", HEADER, DEFINITION, ASSIGNMENT, USAGE)
    add(index, "/explicit/client.py", "code", "from base64 import b64encode\n\ntoken = b64encode(b'x')\n")
    add(index, "/explicit/other.py", "code", "def unrelated():\n    return 1\n")
    found = hits(index, "b64encode")
    assert {h.path for h in found} == {"/explicit/base64.py", "/explicit/client.py"}
    assert all(h.admission_reason == "verified_exact" for h in found)
    by_path = {h.path: h for h in found}
    assert by_path["/explicit/base64.py"].loc == 20       # definition row
    assert by_path["/explicit/client.py"].loc == 10       # single body row: unchanged


def test_without_a_definition_row_the_earliest_mention_still_wins(index):
    add(index, "/explicit/uses.py", "code", "# see b64encode\n", "x = call(b64encode)\n", "y = b64encode\n")
    found = hits(index, "b64encode")
    # 'y = b64encode' is an assignment TO the name, not OF it: y is defined, b64encode is not
    assert found and found[0].loc == 10
