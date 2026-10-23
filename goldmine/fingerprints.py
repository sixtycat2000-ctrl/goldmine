"""Fingerprint identity + the refusal gate (verdict 2 CRITICAL paragraph).

Never mix vectors across embedding fingerprints. The fingerprint covers: backend
family, model id, model sha256, dim, prompt scheme, pooling, normalization, and
the preprocessing (chunk/extract semantics) version. The llama.cpp *build* string
is provenance recorded in `meta.llama_build` — deliberately NOT part of the
fingerprint (D2 refinement: brew upgrades must not force reindexes).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from .backends.base import EmbeddingBackend

__all__ = [
    "PREPROCESSING_VERSION",
    "FingerprintMismatch",
    "FingerprintReport",
    "EXTRACTOR_VERSIONS",
    "build",
    "canonical",
    "fingerprint_id",
    "check",
    "embed_identity_id",
    "embed_identity",
    "embed_id_from_fingerprint",
    "live_embed_identity",
    "live_embed_id",
    "legacy_prompts_doc",
    "live_embed_id",
    "current_extractors",
    "staleness_report",
]

PREPROCESSING_VERSION = "pipeline.v1"


class FingerprintMismatch(RuntimeError):
    """Stored index fingerprint differs from the live one. CLI maps to exit 3."""


@dataclass
class FingerprintReport:
    ok: bool
    diff_fields: list[str] = field(default_factory=list)
    message: str = ""
    remediation: str = ""


def build(backend: "EmbeddingBackend", preprocessing: str = PREPROCESSING_VERSION) -> dict:
    """Full fingerprint JSON for a live backend.

    The eight canonical fields are joined by any EXTRA keys the backend's
    fingerprint_fields() returns (e.g. adapter-specific `family` /
    `task_type` mappings). Extra fields flow into the canonical JSON and hence
    the id — backends that record none are unaffected.
    """
    fields = dict(backend.fingerprint_fields())
    fp = {
        "backend": backend.backend_id,
        "model_id": str(fields.get("model_id", "")),
        "model_sha256": str(fields.get("model_sha256", "")),
        "dim": int(fields.get("dim", backend.dim)),
        "prompts": str(fields.get("prompts", "")),
        "pooling": str(fields.get("pooling", "")),
        "normalization": str(fields.get("normalization", "")),
        "preprocessing": preprocessing,
    }
    for key in sorted(fields):
        if key not in fp:
            fp[key] = fields[key]
    return fp


def canonical(fp: dict) -> bytes:
    """Sorted keys, no whitespace — the identity is byte-stable across key order."""
    return json.dumps(fp, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def fingerprint_id(fp: dict) -> str:
    import hashlib

    return hashlib.sha256(canonical(fp)).hexdigest()[:16]


def check(stored_id: str, stored_json: str, live: dict) -> FingerprintReport:
    """Compare the index's stored fingerprint against the live one.

    ok=True when the ids match. Otherwise diff_fields names every differing key
    and the message carries both remediations.
    """
    live_id = fingerprint_id(live)
    if stored_id == live_id:
        return FingerprintReport(ok=True)

    try:
        stored = json.loads(stored_json) if stored_json else {}
    except json.JSONDecodeError:
        stored = None

    if not isinstance(stored, dict):
        diff = ["(stored fingerprint unparsable)"]
    else:
        diff = sorted(
            key
            for key in set(stored) | set(live)
            if stored.get(key) != live.get(key)
        )
    stored_brief = ", ".join(f"{k}: stored={stored.get(k)!r} live={live.get(k)!r}" for k in diff) if isinstance(stored, dict) else ""
    return FingerprintReport(
        ok=False,
        diff_fields=diff,
        message=(
            "index was built with a different embedding fingerprint "
            f"(differs in: {', '.join(diff)}). {stored_brief}"
        ).rstrip(),
        remediation=(
            "rebuild vectors with `goldmine ingest --reindex`, or rebuild everything "
            "with `goldmine reset --reindex`; alternatively search keyword-only "
            "with `goldmine search --lexical`"
        ),
    )


def gate(store_meta, live: dict) -> None:
    """Refusal gate for every path that touches vectors.

    `store_meta` is a callable key -> str | None (Store.meta). Raises
    FingerprintMismatch when the index holds a different fingerprint; a missing
    fingerprint (fresh index) passes — ingest will record it.
    """
    import logging

    stored_id = store_meta("fingerprint_id")
    if stored_id is None:
        return
    stored_json = store_meta("fingerprint_json") or ""
    report = check(stored_id, stored_json, live)
    if not report.ok:
        logging.getLogger("goldmine.fingerprints").error(report.message)
        raise FingerprintMismatch(f"{report.message} {report.remediation}")


# ------------------------------------------------------------- v2 embed identity
#
# The v1 fingerprint bundles four independent axes into one id (the embedding
# identity, the chunker, the extraction semantics, the tool set). v2 splits the
# EMBED identity out (v2 05 §4.2, dimension 1): what makes vectors comparable.
#
# B2 BINDING RULE (normative, REDESIGN §3 C2 / 05 §4.1): the legacy v1
# fingerprint's combined `prompts` string (backends/base.py PROMPT_SCHEME,
# format "v1:q='...' d='...'") carried the DOCUMENT prompt in its `d='...'`
# slot. A fresh v2 index computes prompts_doc from the backend's own split
# constant (`prompts_doc_template`) — byte-identical to that slot's content
# for llama/mock — and the query prompt (`q='...'`) plus `preprocessing` are
# EXCLUDED from the embed id. Therefore a migrated index's backfilled embed
# ids EQUAL fresh-index ids for the same model constants: the migration is
# zero-re-embed and reports zero (embed-)stale. NEVER copy the old combined
# fingerprint_id into files.embed_id. (Live derivation never PARSES the
# legacy scheme — see live_embed_identity; parsing is migrations-only.)

#: Fields of the v1 fingerprint that are NOT part of the embed identity.
_NON_EMBED_FIELDS = frozenset(
    {"backend", "model_id", "model_sha256", "dim", "prompts", "pooling",
     "normalization", "preprocessing"}
)


def legacy_prompts_doc(fingerprint_json) -> str:
    """The document-prompt template, parsed VERBATIM from the legacy combined
    PROMPT_SCHEME's ``d='...'`` part (B2 binding rule).

    Accepts the stored ``meta.fingerprint_json`` string (or an already-parsed
    fingerprint dict, or the bare scheme string). Returns exactly what a fresh
    v2 index derives from the split constants — e.g. "title: {} | text: {}" —
    so ids computed over it match across migrated and fresh indexes. Raises
    ValueError when no ``d='...'`` slot can be parsed (the v1 gate guarantees
    one exists for any real index).
    """
    import re

    fp = fingerprint_json
    if isinstance(fp, bytes):
        fp = fp.decode("utf-8", "replace")
    if isinstance(fp, str):
        stripped = fp.strip()
        if stripped.startswith("{"):
            try:
                fp = json.loads(stripped)
            except json.JSONDecodeError:
                fp = {}
        else:
            fp = {"prompts": stripped}
    if not isinstance(fp, dict):
        raise ValueError(f"cannot parse legacy fingerprint: {fingerprint_json!r}")
    scheme = str(fp.get("prompts", ""))
    # greedy to the LAST quote: the doc slot terminates the scheme
    match = re.search(r"d='(.*)'", scheme, re.DOTALL)
    if not match:
        raise ValueError(f"no d='…' slot in legacy prompt scheme: {scheme!r}")
    return match.group(1)


def embed_identity_id(
    model_sha: str | None,
    dim: int,
    pooling: str | None,
    normalization: str | None,
    prompts_doc: str,
    media_preproc: str | None = None,
    *,
    backend: str | None = None,
    model_id: str | None = None,
    extras: dict | None = None,
) -> str:
    """sha256[:16] over the embed identity — the CURRENT embedding identity id,
    EXCLUDING preprocessing and the query prompt (B2 binding rule).

    Canonical fields: model_sha256, dim, prompts_doc, pooling, normalization,
    plus media_preproc (media preprocessing, e.g. the mock's "bytes.v1") and —
    when known — backend, model_id, and any backend extras (05 §4.2). All
    callers that have a full fingerprint dict should prefer
    `embed_id_from_fingerprint`, which fills the optional keyword fields
    consistently; both paths produce the same id for the same constants.
    """
    ident = {
        "model_sha256": str(model_sha or ""),
        "dim": int(dim),
        "pooling": str(pooling or ""),
        "normalization": str(normalization or ""),
        "prompts_doc": str(prompts_doc),
    }
    if media_preproc is not None:
        ident["media_preproc"] = str(media_preproc)
    if backend is not None:
        ident["backend"] = str(backend)
    if model_id is not None:
        ident["model_id"] = str(model_id)
    for key in sorted(extras or {}):
        if key not in ident:
            ident[key] = extras[key]
    import hashlib

    return hashlib.sha256(canonical(ident)).hexdigest()[:16]


def embed_identity(fp: dict) -> dict:
    """The embed-identity dict from a full (v1-shaped) fingerprint dict:
    preprocessing and the combined `prompts` string are dropped, `prompts_doc`
    is parsed per the B2 binding rule, backend extras flow through.

    STORED-fingerprint path only (migrations / status / doctor reading
    meta.fingerprint_json): the live path is `live_embed_identity()`, which
    reads the backend's own split constants and never parses the legacy
    scheme (a legacy prompts value may have no d-slot to parse)."""
    ident = {
        "backend": str(fp.get("backend", "")),
        "model_id": str(fp.get("model_id", "")),
        "model_sha256": str(fp.get("model_sha256", "")),
        "dim": int(fp.get("dim", 0)),
        "prompts_doc": legacy_prompts_doc(fp),
        "pooling": str(fp.get("pooling", "")),
        "normalization": str(fp.get("normalization", "")),
    }
    if fp.get("media") is not None:
        ident["media_preproc"] = str(fp["media"])
    for key in fp:
        if key not in _NON_EMBED_FIELDS and key != "media" and key not in ident:
            ident[key] = fp[key]
    return ident


def _embed_identity_id(ident: dict) -> str:
    """The id over one embed-identity dict (the shared tail of both derivation
    paths — stored fingerprint and live backend — so they can never drift)."""
    return embed_identity_id(
        ident["model_sha256"],
        ident["dim"],
        ident["pooling"],
        ident["normalization"],
        ident["prompts_doc"],
        media_preproc=ident.get("media_preproc"),
        backend=ident["backend"],
        model_id=ident["model_id"],
        extras={k: v for k, v in ident.items()
                if k not in {"backend", "model_id", "model_sha256", "dim",
                             "prompts_doc", "pooling", "normalization",
                             "media_preproc"}},
    )


def embed_id_from_fingerprint(fp: dict) -> str:
    """The embed id for a STORED fingerprint dict (meta.fingerprint_json
    parsed) — migration backfill, status/doctor bookkeeping. For a LIVE
    backend always use `live_embed_id()` (see live_embed_identity)."""
    return _embed_identity_id(embed_identity(fp))


def live_embed_identity(backend: "EmbeddingBackend") -> dict:
    """The embed-identity dict from a STARTED backend's OWN structured fields
    (v2 review F1): prompts_doc comes from `prompts_doc_template` — the split
    doc-prompt constant each backend declares — NEVER from parsing the
    combined legacy scheme (whose d='…' slot a backend's prompts value may
    deliberately lack; parsing it raised ValueError and killed every ingest).
    Field-for-field identical to `embed_identity()` on the same backend's
    fingerprint (same keys, same extras flow), so live and migrated
    derivations share one shape; `legacy_prompts_doc()` remains for stored v1
    fingerprints only."""
    fields = dict(backend.fingerprint_fields())
    ident = {
        "backend": str(backend.backend_id),
        "model_id": str(fields.get("model_id", "")),
        "model_sha256": str(fields.get("model_sha256", "")),
        "dim": int(fields.get("dim", backend.dim)),
        "prompts_doc": str(backend.prompts_doc_template),
        "pooling": str(fields.get("pooling", "")),
        "normalization": str(fields.get("normalization", "")),
    }
    if fields.get("media") is not None:
        ident["media_preproc"] = str(fields["media"])
    for key in sorted(fields):
        if key not in _NON_EMBED_FIELDS and key != "media" and key not in ident:
            ident[key] = fields[key]
    return ident


def live_embed_id(backend: "EmbeddingBackend") -> str:
    """The live embed identity id from a STARTED backend's structured fields
    (fields for llama are only fully known after startup/ping)."""
    return _embed_identity_id(live_embed_identity(backend))


# ---------------------------------------------------- extractor version registry
#
# Single source (v2 05 §4.2 dimension 2): extractor family -> the version
# string this build writes. Values are byte-compatible with the literals the
# extractors/pipeline already write, so existing rows never go spuriously
# stale. Tool-versioned families (pdftotext@…, textutil@…) are dynamic —
# `current_extractors()` adds the live tool versions to the static set.

EXTRACTOR_VERSIONS: dict[str, str] = {
    "txt": "txt-stdlib.v1",
    "md": "md-stdlib.v1",
    "docx": "docx-stdlib.v1",
    "html": "html-stdlib.v1",
    "code": "code-stdlib.v1",
    "url": "url-stdlib.v1",
    # 02-ingestion F1: eml/epub were missing here, so every plain ingest
    # re-extracted AND re-embedded all .eml/.epub rows forever (their stored
    # version sat outside the "current" set). The strings match the extractors'
    # EML_TOOL_VERSION / EPUB_TOOL_VERSION verbatim — no bump: existing rows
    # become current on the next plain ingest.
    "eml": "eml-stdlib.v2",
    "epub": "epub-stdlib.v2",
    # v2 media (B4/B5): sips thumbnails + EXIF + screenshot OCR replaced the
    # raw-bytes image path — image-bytes.v1 rows are stale by design so plain
    # ingest re-does them (G10). The -ocr variant marks files whose screenshot
    # OCR text landed (info.ocr_chars); both are current simultaneously.
    "image": "image-sips.v2",
    "image+ocr": "image-sips-ocr.v2",
    "media": "media-ffmpeg.v1",
    # v2 ASR (item 6, 02 §5.1): whisper-cli + VAD transcript extraction —
    # audio/video files whose soundtrack became text segments. v2 bump (02 F8,
    # 2026-10-16): the transcript merge cap switched to est-tokens, so v1 rows
    # re-transcribe on the next plain ingest (G10 semantics).
    "asr": "asr-whisper.v2",
}


def current_extractors() -> set[str]:
    """Every extractor version string this build can write: the static registry
    plus the tool-versioned families (pdftotext@<live>, textutil@<live>,
    including the '…@unknown' fallbacks the extractors record when the tool's
    version cannot be probed), plus the OCR-upgraded PDF variant (02 §5.2
    Scope B: pages rendered via pdftoppm + Vision OCR get
    'pdftotext+visionocr@<ver>'). Staleness/doctor/status all compare against
    this set."""
    from .deadline import RequestTimeout

    versions = set(EXTRACTOR_VERSIONS.values())
    try:
        from .ingest.extract_pdf import pdftotext_version

        versions.add(f"pdftotext@{pdftotext_version() or 'unknown'}")
        versions.add(f"pdftotext+visionocr@{pdftotext_version() or 'unknown'}")
    except RequestTimeout:
        raise
    except Exception:  # pragma: no cover - tool probing must never break status
        pass
    try:
        from .ingest.extract_text import textutil_version

        if textutil_version():
            versions.add(f"textutil@{textutil_version()}")
    except RequestTimeout:
        raise
    except Exception:  # pragma: no cover
        pass
    return versions


# --------------------------------------------------------------- staleness (v2)
#
# Per-file identity drift as a QUERYABLE FACT (05 §4.2) — never a refusal
# (A2: only an incompatible embed identity refuses, via the existing gate
# above; exit-3 semantics are unchanged). Staleness scopes, honestly narrow:
#   embed     — files with vec-bearing segments whose files.embed_id is NULL
#               or differs from the live id (vectorless files cannot be
#               embed-stale)
#   chunker   — capability='content_indexed' files whose chunker_version is
#               NULL or differs from the current CHUNKER_VERSION
#   extractor — capability='content_indexed' files whose extractor_version is
#               NULL or outside the current registry (same scope as the v1
#               drift notice)


def staleness_report(
    store,
    live_embed: str | None = None,
    *,
    chunker_version: str | None = None,
    extractor_versions: set[str] | None = None,
) -> dict:
    """Counts of per-file staleness by dimension. `store` is a Store (or a bare
    sqlite3.Connection — anything with `.db` or executing directly). When
    `live_embed` is None the embed dimension counts only NULL embed ids (the
    caller could not establish a live identity)."""
    from .chunker import CHUNKER_VERSION

    db = getattr(store, "db", store)
    if chunker_version is None:
        chunker_version = CHUNKER_VERSION
    if extractor_versions is None:
        extractor_versions = current_extractors()

    vec_files = db.execute(
        "SELECT COUNT(DISTINCT file) FROM segments WHERE vec IS NOT NULL"
    ).fetchone()[0]
    files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    content_files = db.execute(
        "SELECT COUNT(*) FROM files WHERE capability = 'content_indexed'"
    ).fetchone()[0]

    embed_stale = 0
    if vec_files:
        if live_embed:
            embed_stale = db.execute(
                "SELECT COUNT(*) FROM files WHERE id IN "
                "(SELECT DISTINCT file FROM segments WHERE vec IS NOT NULL) "
                "AND (embed_id IS NULL OR embed_id != ?)",
                (live_embed,),
            ).fetchone()[0]
        else:
            embed_stale = db.execute(
                "SELECT COUNT(*) FROM files WHERE id IN "
                "(SELECT DISTINCT file FROM segments WHERE vec IS NOT NULL) "
                "AND embed_id IS NULL"
            ).fetchone()[0]

    chunker_stale = db.execute(
        "SELECT COUNT(*) FROM files WHERE capability = 'content_indexed' "
        "AND (chunker_version IS NULL OR chunker_version != ?)",
        (chunker_version,),
    ).fetchone()[0]

    placeholders = ",".join("?" for _ in extractor_versions)
    extractor_stale = db.execute(
        "SELECT COUNT(*) FROM files WHERE capability = 'content_indexed' AND "
        f"(extractor_version IS NULL OR extractor_version NOT IN ({placeholders}))",
        tuple(sorted(extractor_versions)),
    ).fetchone()[0]

    embed_clause = (
        "(embed_id IS NULL OR embed_id != ?)" if live_embed else "embed_id IS NULL"
    )
    params = [chunker_version, *sorted(extractor_versions)]
    if live_embed:
        params.append(live_embed)
    stale_files = db.execute(
        "SELECT COUNT(*) FROM files WHERE ("
        "  (capability = 'content_indexed' AND "
        "    (chunker_version IS NULL OR chunker_version != ?))"
        f"  OR (capability = 'content_indexed' AND "
        f"    (extractor_version IS NULL OR extractor_version NOT IN ({placeholders})))"
        "  OR (id IN (SELECT DISTINCT file FROM segments WHERE vec IS NOT NULL) "
        f"    AND {embed_clause})"
        ")",
        params,
    ).fetchone()[0]

    # F6: HOW LONG has this been stale — the oldest indexed_at among each
    # dimension's stale rows ("since <date>" in status; None = nothing stale
    # in that dimension). Same predicates as the counts above, so the two
    # can never disagree.
    def _oldest(where: str, where_params: tuple) -> float | None:
        row = db.execute(
            f"SELECT MIN(indexed_at) FROM files WHERE {where}", where_params
        ).fetchone()
        return row[0] if row else None

    oldest_stale = {
        "embed": _oldest(
            "id IN (SELECT DISTINCT file FROM segments WHERE vec IS NOT NULL) "
            f"AND {embed_clause}",
            (live_embed,) if live_embed else (),
        ),
        "chunker": _oldest(
            "capability = 'content_indexed' AND "
            "(chunker_version IS NULL OR chunker_version != ?)",
            (chunker_version,),
        ),
        "extractor": _oldest(
            "capability = 'content_indexed' AND "
            f"(extractor_version IS NULL OR extractor_version NOT IN ({placeholders}))",
            tuple(sorted(extractor_versions)),
        ),
    }
    oldest_any = min(
        (v for v in oldest_stale.values() if v is not None), default=None
    )

    return {
        "files": files,
        "content_files": content_files,
        "vec_files": vec_files,
        "stale_files": stale_files,
        "embed_stale": embed_stale,
        "chunker_stale": chunker_stale,
        "extractor_stale": extractor_stale,
        "oldest_stale_indexed_at": oldest_any,
        "oldest_stale": oldest_stale,
        "current": {
            "chunker_version": chunker_version,
            "embed_id": live_embed,
            "extractors": sorted(extractor_versions),
        },
    }
