"""Tier-2: v2 identity plumbing — the B2 binding-rule functions, per-file
identity writes at commit, the extractor registry, staleness_report, and the
`status` staleness section (human + --json)."""

from __future__ import annotations

import json

import pytest

from goldmine import fingerprints
from goldmine.backends.base import DOCUMENT_PROMPT, PROMPT_SCHEME, QUERY_PROMPT
from goldmine.backends.mock import MockBackend
from goldmine.chunker import CHUNKER_VERSION
from goldmine.config import load
from goldmine.ingest import IngestPipeline
from goldmine.store import Store

# --------------------------------------------- B2 binding-rule function units


def test_legacy_prompts_doc_is_the_document_prompt_verbatim():
    """The d='…' slot equals the split constants' doc template, byte for byte:
    what a fresh v2 index computes and what the migration parses must agree."""
    doc = fingerprints.legacy_prompts_doc(PROMPT_SCHEME)
    assert doc == "title: {} | text: {}"
    assert doc == DOCUMENT_PROMPT.replace("{title}", "{}").replace("{text}", "{}")


def test_legacy_prompts_doc_accepts_stored_fingerprint_json():
    fp = fingerprints.build(MockBackend(dim=768))
    stored = fingerprints.canonical(fp).decode()
    assert (
        fingerprints.legacy_prompts_doc(stored)
        == fingerprints.legacy_prompts_doc(fp)
        == fingerprints.legacy_prompts_doc(fp["prompts"])
    )


def test_legacy_prompts_doc_ignores_the_query_slot():
    """Only a DOC-prompt change alters stored vectors; a query-prompt change
    invalidates nothing (C2)."""
    scheme_q_changed = PROMPT_SCHEME.replace(
        QUERY_PROMPT.format(text="{}"), "task: other | q: {}"
    )
    assert (
        fingerprints.legacy_prompts_doc(scheme_q_changed)
        == fingerprints.legacy_prompts_doc(PROMPT_SCHEME)
    )
    scheme_d_changed = PROMPT_SCHEME.replace(
        DOCUMENT_PROMPT.replace("{title}", "{}").replace("{text}", "{}"),
        "titre: {} | texte: {}",
    )
    assert (
        fingerprints.legacy_prompts_doc(scheme_d_changed)
        != fingerprints.legacy_prompts_doc(PROMPT_SCHEME)
    )


def test_legacy_prompts_doc_refuses_unparsable_scheme():
    with pytest.raises(ValueError):
        fingerprints.legacy_prompts_doc("no slots here")
    with pytest.raises(ValueError):
        fingerprints.legacy_prompts_doc('{"prompts": ""}')


def test_embed_identity_id_each_dimension_changes_the_id():
    base = dict(
        model_sha="sha", dim=768, pooling="mean", normalization="l2",
        prompts_doc="title: {} | text: {}",
    )
    reference = fingerprints.embed_identity_id(**base)
    assert reference == fingerprints.embed_identity_id(**base)  # stable
    changed = dict(
        base, model_sha="sha2", dim=256, pooling="last", normalization="none",
        prompts_doc="other {}",
    )
    for key, value in changed.items():
        assert fingerprints.embed_identity_id(**{**base, key: value}) != reference, key
    # media preprocessing is part of the identity when present
    assert (
        fingerprints.embed_identity_id(**base, media_preproc="bytes.v1") != reference
    )


def test_embed_identity_id_optional_backend_fields_cover_the_full_identity():
    """05 §4.2: the embed identity also covers backend/model id + extras; both
    derivation paths (live backend, stored fingerprint) include them."""
    args = dict(
        model_sha="sha", dim=768, pooling="mean", normalization="l2",
        prompts_doc="d",
    )
    bare = fingerprints.embed_identity_id(**args)
    with_backend = fingerprints.embed_identity_id(
        **args, backend="llama", model_id="m", extras={"ubatch": 4}
    )
    assert bare != with_backend
    assert with_backend != fingerprints.embed_identity_id(
        **args, backend="mock", model_id="m", extras={"ubatch": 4}
    )


def test_embed_id_from_fingerprint_excludes_preprocessing():
    fp = fingerprints.build(MockBackend(dim=768))
    same_prep = dict(fp, preprocessing="pipeline.v2-tok")
    assert fingerprints.embed_id_from_fingerprint(fp) == fingerprints.embed_id_from_fingerprint(
        same_prep
    )
    assert fingerprints.embed_id_from_fingerprint(fp) != fingerprints.fingerprint_id(fp)


def test_live_embed_id_equals_fingerprint_derivation():
    for backend in (
        MockBackend(dim=768),
        MockBackend(seed=3, dim=768),
        MockBackend(dim=768, multimodal=True),
    ):
        assert fingerprints.live_embed_id(backend) == fingerprints.embed_id_from_fingerprint(
            fingerprints.build(backend)
        )


def test_prompts_doc_template_matches_the_legacy_d_slot():
    """B2 sync guard: the split constant every llama/mock-style backend
    declares must stay byte-identical to the legacy scheme's d='…' slot the
    migration parses — otherwise migrated and fresh ids would drift apart."""
    from goldmine.backends.base import EmbeddingBackend

    assert EmbeddingBackend.prompts_doc_template == "title: {} | text: {}"
    assert MockBackend(dim=768).prompts_doc_template == fingerprints.legacy_prompts_doc(
        PROMPT_SCHEME
    )


class _NoDSlotBackend(MockBackend):
    """A stub whose `prompts` fingerprint value has NO d='…' slot (a
    helper-style backend that frames text internally), so any live derivation
    that parses the legacy scheme raises ValueError (v2 review F1 — this
    class of backend used to kill every ingest)."""

    backend_id = "stub-no-d-slot"
    prompts_doc_template = "helper-internal (client sends framed text verbatim)"

    def fingerprint_fields(self) -> dict:
        fields = super().fingerprint_fields()
        fields["prompts"] = type(self).prompts_doc_template
        fields["family"] = "stub-gemma-embedding"
        return fields


def test_live_embed_id_survives_a_backend_without_a_d_slot():
    """F1: the live derivation reads `prompts_doc_template` — the split
    constant — and never parses the combined legacy scheme."""
    backend = _NoDSlotBackend(dim=768)
    with pytest.raises(ValueError):
        # sanity: the STORED-fingerprint parse genuinely refuses this scheme
        fingerprints.embed_id_from_fingerprint(fingerprints.build(backend))
    ident = fingerprints.live_embed_identity(backend)
    assert ident["prompts_doc"] == "helper-internal (client sends framed text verbatim)"
    assert ident["family"] == "stub-gemma-embedding"   # extras flow through
    live = fingerprints.live_embed_id(backend)
    assert live and len(live) == 16
    # the marker is part of the identity: changing it changes the id
    type(backend).prompts_doc_template = "helper-internal v2"
    try:
        assert fingerprints.live_embed_id(backend) != live
    finally:
        type(backend).prompts_doc_template = (
            "helper-internal (client sends framed text verbatim)"
        )


def test_ingest_with_a_no_d_slot_backend_records_the_marker_identity(tmp_path):
    """F1 end-to-end: a full pipeline ingest with the no-d-slot stub
    succeeds (it used to die in live_embed_id before any file was committed),
    and meta/files carry the marker-based embed identity."""
    db = tmp_path / "idx.sqlite"
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "notes.txt").write_text("scheduler eviction tolerations\n" * 3)
    backend = _NoDSlotBackend(dim=768)
    _run_ingest(db, root, backend=backend)
    store = Store(db)
    try:
        live = fingerprints.live_embed_id(backend)
        assert store.meta("embed_id") == live
        stored_identity = json.loads(store.meta("embed_identity_json"))
        assert stored_identity["prompts_doc"] == (
            "helper-internal (client sends framed text verbatim)"
        )
        rows = store.db.execute(
            "SELECT embed_id FROM files WHERE capability = 'content_indexed'"
        ).fetchall()
        assert rows and all(r[0] == live for r in rows)
        assert fingerprints.staleness_report(store, live)["stale_files"] == 0
    finally:
        store.close()


def test_media_extra_flows_into_the_embed_id():
    plain = MockBackend(dim=768)
    multi = MockBackend(dim=768, multimodal=True)
    assert fingerprints.live_embed_id(plain) != fingerprints.live_embed_id(multi)


# ------------------------------------------------------- extractor registry


def test_extractor_registry_values_match_written_rows():
    """Single source: the registry values are byte-compatible with the version
    strings the extractors/pipeline write, so nothing goes spuriously stale."""
    written = {
        "txt-stdlib.v1", "md-stdlib.v1", "docx-stdlib.v1", "html-stdlib.v1",
        "code-stdlib.v1", "ipynb-stdlib.v1", "url-stdlib.v1", "eml-stdlib.v2",
        "epub-stdlib.v2", "image-mm.v1", "image-mm-ocr.v1",
        "media-ffmpeg-mm.v2", "asr-whisper.v2",
        "asr-whisper.v2+media-ffmpeg-mm.v2",  # transcribed video + frames (v4)
    }
    assert set(fingerprints.EXTRACTOR_VERSIONS.values()) == written
    from goldmine import cli

    assert cli.CURRENT_EXTRACTORS <= set(fingerprints.EXTRACTOR_VERSIONS.values())


def test_current_extractors_includes_tool_families():
    current = fingerprints.current_extractors()
    assert "pdftotext@unknown" in current or any(
        v.startswith("pdftotext@") for v in current
    )
    # the static families never depend on the machine
    assert set(fingerprints.EXTRACTOR_VERSIONS.values()) <= current


# ------------------------------------------------- eml/epub staleness (02 F1)


def _make_eml(path, subject: str, body: str) -> None:
    path.write_text(
        f"From: Dana Wong <dana@example.org>\n"
        f"Subject: {subject}\n"
        f"Date: Mon, 12 Oct 2026 09:30:00 +0800\n"
        f"Content-Type: text/plain; charset=utf-8\n"
        f"\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _make_epub(path, title: str, chapter: str) -> None:
    import zipfile

    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="2.0" unique-identifier="id">'
        "<metadata/>"
        "<manifest>"
        '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest>"
        '<spine><itemref idref="c1"/></spine></package>'
    )
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("content.opf", opf)
        zf.writestr("c1.xhtml", f"<html><body><p>{chapter}</p></body></html>")


def test_eml_epub_unchanged_on_second_pass(tmp_path):
    """02-ingestion F1: eml/epub were missing from EXTRACTOR_VERSIONS, so the
    G10 staleness gate considered every .eml/.epub row extractor-stale and
    plain ingest re-extracted AND re-embedded them on EVERY run (the embed
    cost of an email-heavy corpus, paid forever). With the registry entries
    the second pass skips them like any other content file."""
    root = tmp_path / "corpus"
    root.mkdir()
    _make_eml(root / "note.eml", "Filter maintenance",
              "The canister filter needs a rinse every two weeks.")
    _make_epub(root / "book.epub", "Harbor Guide",
               "The lighthouse keeper logged the chart depth.")
    (root / "note.txt").write_text(
        "the tide gauge recorded 4.2 meters at dawn\n", encoding="utf-8"
    )
    db = tmp_path / "idx.sqlite"
    with MockBackend(dim=768) as backend:
        first = _run_ingest(db, root, backend=backend)
        assert first.failed == 0
        assert first.content_indexed == 3
        assert first.unchanged == 0
        second = _run_ingest(db, root, backend=backend)
    assert second.unchanged == 3, (
        "eml/epub rows must be current after the registry fix — "
        "re-doing them re-pays the full embed cost on every plain ingest"
    )
    assert second.content_indexed == 0
    store = Store(db)
    try:
        versions = {
            r[0]: r[1] for r in store.db.execute(
                "SELECT path, extractor_version FROM files"
            ).fetchall()
        }
        assert versions[str(root / "note.eml")] == "eml-stdlib.v2"
        assert versions[str(root / "book.epub")] == "epub-stdlib.v2"
        # nothing extractor-stale anywhere (the pre-fix doctor/status symptom)
        live = fingerprints.live_embed_id(MockBackend(dim=768))
        report = fingerprints.staleness_report(store, live)
        assert report["extractor_stale"] == 0
    finally:
        store.close()


# ------------------------------------------------- pipeline identity writes


def _run_ingest(db_path, root, backend=None, lexical_only=False):
    cfg = load()
    store = Store(db_path)
    try:
        pipeline = IngestPipeline(cfg, store, backend, lexical_only=lexical_only)
        return pipeline.run([str(root)])
    finally:
        store.close()


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "notes.txt").write_text(
        "scheduler eviction tolerations pressure\n" * 3, encoding="utf-8"
    )
    (root / "digits.txt").write_text("4815162342 9988776655\n", encoding="utf-8")
    (root / "secret.pem").write_text("-----BEGIN KEY-----\n", encoding="utf-8")
    return root


def test_ingest_writes_per_file_identity_at_commit(tmp_path, corpus):
    db = tmp_path / "idx.sqlite"
    backend = MockBackend(dim=768)
    _run_ingest(db, corpus, backend=backend)
    live = fingerprints.live_embed_id(backend)
    store = Store(db)
    try:
        rows = {
            r[0]: r[1:]
            for r in store.db.execute(
                "SELECT path, capability, chunker_version, embed_id FROM files"
            ).fetchall()
        }
        notes, digits, pem = (
            rows[str(corpus / "notes.txt")],
            rows[str(corpus / "digits.txt")],
            rows[str(corpus / "secret.pem")],
        )
        # content file with vectors: full identity
        assert notes[0] == "content_indexed"
        assert notes[1] == CHUNKER_VERSION
        assert notes[2] == live
        # digits-only text stays lexical (digits-only loses its vector) —
        # chunker stamped, embed_id honestly NULL
        assert digits[0] == "content_indexed"
        assert digits[1] == CHUNKER_VERSION
        assert digits[2] is None
        assert store.db.execute(
            "SELECT COUNT(*) FROM segments WHERE file = "
            "(SELECT id FROM files WHERE path = ?) AND vec IS NOT NULL",
            (str(corpus / "digits.txt"),),
        ).fetchone()[0] == 0
        # unsupported rows: chunker stamped, no embed identity
        assert pem[0] == "unsupported"
        assert pem[1] == CHUNKER_VERSION
        assert pem[2] is None
        # meta carries the live identity for status/doctor
        assert store.meta("embed_id") == live
        assert json.loads(store.meta("embed_identity_json"))["prompts_doc"] == (
            "title: {} | text: {}"
        )
        report = fingerprints.staleness_report(store, live)
        assert report["stale_files"] == 0
    finally:
        store.close()


def test_lexical_only_ingest_writes_no_embed_identity(tmp_path, corpus):
    db = tmp_path / "lex.sqlite"
    _run_ingest(db, corpus, lexical_only=True)
    store = Store(db)
    try:
        assert store.db.execute(
            "SELECT COUNT(*) FROM files WHERE embed_id IS NOT NULL"
        ).fetchone()[0] == 0
        assert store.meta("embed_id") is None
        assert store.db.execute(
            "SELECT COUNT(*) FROM files WHERE chunker_version IS NULL "
            "AND capability = 'content_indexed'"
        ).fetchone()[0] == 0
    finally:
        store.close()


# --------------------------------------------------------- staleness_report


def test_staleness_report_counts_each_dimension(tmp_path, corpus):
    db = tmp_path / "idx.sqlite"
    backend = MockBackend(dim=768)
    _run_ingest(db, corpus, backend=backend)
    live = fingerprints.live_embed_id(backend)
    store = Store(db)
    try:
        # tamper one row per dimension
        content = str(corpus / "notes.txt")
        with store.write_transaction():
            store.db.execute(
                "UPDATE files SET chunker_version = 'chunk.v0' WHERE path = ?", (content,)
            )
        with store.write_transaction():
            store.db.execute(
                "UPDATE files SET extractor_version = 'txt-stdlib.v0' WHERE path = ?",
                (content,),
            )
        digits = str(corpus / "digits.txt")
        with store.write_transaction():
            store.db.execute(
                "UPDATE files SET embed_id = 'deadbeef' WHERE path = ?", (digits,)
            )
        store.db.commit()
        # digits.txt has no vectors: a bogus embed_id on a vectorless file is
        # invisible by design — put the stale embed on the vec-bearing row
        with store.write_transaction():
            store.db.execute("UPDATE files SET embed_id = NULL WHERE path = ?", (content,))
        store.db.commit()

        report = fingerprints.staleness_report(store, live)
        assert report["chunker_stale"] == 1
        assert report["extractor_stale"] == 1
        assert report["embed_stale"] == 1  # NULL embed id on a vec-bearing file
        assert report["stale_files"] == 1  # same file, three reasons

        # NULL live identity: only missing embed ids count
        report_nolive = fingerprints.staleness_report(store, None)
        assert report_nolive["embed_stale"] == 1
        # a different live identity flags every vec-bearing file
        other = fingerprints.live_embed_id(MockBackend(seed=9, dim=768))
        assert fingerprints.staleness_report(store, other)["embed_stale"] == report["vec_files"]
    finally:
        store.close()


def test_staleness_report_oldest_stale_dates_per_dimension(tmp_path, corpus):
    """F6: MIN(indexed_at) over each dimension's stale rows — per-dimension
    independence, the overall minimum, and None when nothing is stale."""
    db = tmp_path / "idx.sqlite"
    backend = MockBackend(dim=768)
    _run_ingest(db, corpus, backend=backend)
    live = fingerprints.live_embed_id(backend)
    t_old, t_mid = 1750000000.0, 1760000000.0   # 2025-06 / 2025-10 epochs
    store = Store(db)
    try:
        # notes.txt goes stale in chunker + embed at t_old;
        # digits.txt goes stale in extractor only, at t_mid
        with store.write_transaction():
            store.db.execute(
                "UPDATE files SET chunker_version = 'chunk.v0', embed_id = NULL, "
                "indexed_at = ? WHERE path = ?", (t_old, str(corpus / "notes.txt")),
            )
        with store.write_transaction():
            store.db.execute(
                "UPDATE files SET extractor_version = 'txt-stdlib.v0', indexed_at = ? "
                "WHERE path = ?", (t_mid, str(corpus / "digits.txt")),
            )
        store.db.commit()

        report = fingerprints.staleness_report(store, live)
        assert report["oldest_stale"]["chunker"] == t_old
        assert report["oldest_stale"]["embed"] == t_old
        assert report["oldest_stale"]["extractor"] == t_mid
        assert report["oldest_stale_indexed_at"] == t_old

        # freshen everything: the dates honestly disappear
        with store.write_transaction():
            store.db.execute(
                "UPDATE files SET chunker_version = ?, embed_id = ?, "
                "extractor_version = 'txt-stdlib.v1'",
                (CHUNKER_VERSION, live),
            )
        store.db.commit()
        clean = fingerprints.staleness_report(store, live)
        assert clean["stale_files"] == 0
        assert clean["oldest_stale_indexed_at"] is None
        assert clean["oldest_stale"] == {
            "embed": None, "chunker": None, "extractor": None,
        }
    finally:
        store.close()


# --------------------------------------------------- status staleness section


def test_status_json_staleness_object(tmp_home, tmp_path, corpus, capsys):
    from goldmine.cli import main

    db = tmp_home / "gmhome" / "default.sqlite"
    assert main(["--backend", "mock", "--db", str(db), "ingest", str(corpus)]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    staleness = payload["staleness"]
    assert staleness["stale_files"] == 0
    assert set(staleness) >= {
        "files", "vec_files", "stale_files", "embed_stale", "chunker_stale",
        "extractor_stale", "current", "rerun_hint",
    }
    assert staleness["current"]["chunker_version"] == CHUNKER_VERSION


def test_status_human_staleness_and_rerun_hint(tmp_home, tmp_path, corpus, capsys):
    from goldmine.cli import main

    db = tmp_home / "gmhome" / "default.sqlite"
    assert main(["--backend", "mock", "--db", str(db), "ingest", str(corpus)]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "status"]) == 0
    out = capsys.readouterr().out
    assert "stale: 0 of" in out
    assert "re-run" not in out  # nothing stale -> no nagging

    # age the content files -> the hint names the recorded roots
    store = Store(db)
    try:
        t_old = 1750000000.0  # 2025-06-15 epoch — the "since" date source
        with store.write_transaction():
            store.db.execute(
                "UPDATE files SET chunker_version = 'chunk.v0', indexed_at = ? "
                "WHERE capability = 'content_indexed'",
                (t_old,),
            )
        store.db.commit()
    finally:
        store.close()
    capsys.readouterr()
    assert main(["--db", str(db), "status"]) == 0
    out = capsys.readouterr().out
    assert "stale: 2 of" in out  # notes.txt + digits.txt are both content_indexed
    assert f"re-run `goldmine ingest {corpus}` to refresh 2 stale files" in out
    # F6: the stale line gains "since <date>" (oldest stale row's index date)
    import time as _time

    assert f"since {_time.strftime('%Y-%m-%d', _time.localtime(t_old))}" in out

    # and --json staleness carries the same fields
    capsys.readouterr()
    assert main(["--db", str(db), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    staleness = payload["staleness"]
    assert staleness["oldest_stale_indexed_at"] == t_old
    assert staleness["oldest_stale"]["chunker"] == t_old
