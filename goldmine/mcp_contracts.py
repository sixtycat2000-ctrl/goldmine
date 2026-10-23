"""Read-only MCP schemas and projections; no optional SDK imports."""
from __future__ import annotations

from dataclasses import asdict
import json

from .contracts import ApplicationError, SearchRequest
from .search import VALID_KINDS, _date_to_epoch, parse_filters


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def integer(low, high, default):
    return {"type": "integer", "minimum": low, "maximum": high, "default": default}


STRINGS = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 8192}, "maxItems": 20}
FILTERS = obj({"kinds": {**STRINGS, "items": {"enum": sorted(VALID_KINDS)}},
               "under": STRINGS, "site": STRINGS, "phrases": STRINGS,
               "after": {"type": "string", "maxLength": 64},
               "before": {"type": "string", "maxLength": 64}})
SCHEMAS = {
    "goldmine_search": obj({"query": {"type": "string", "minLength": 1, "maxLength": 8192},
        "limit": integer(1, 50, 10), "mode": {"enum": ["fast", "quality"], "default": "fast"},
        "rerank": {"enum": ["off", "local"], "default": "off"},
        "multi_query": {"type": "boolean", "default": False},
        "pack": {"type": "boolean", "default": False}, "budget": integer(512, 32768, 4000),
        "context": integer(0, 3, 1), "why": {"type": "boolean", "default": False},
        "filters": FILTERS}, ["query"]),
    "goldmine_get": obj({"citation_id": {"type": "string", "minLength": 1, "maxLength": 128},
                          "budget": integer(512, 8000, 2000)}, ["citation_id"]),
    "goldmine_context": obj({"citation_ids": {"type": "array", "minItems": 1, "maxItems": 10,
                  "items": {"type": "string", "minLength": 1, "maxLength": 128}},
                  "neighbors": integer(0, 3, 1), "budget": integer(512, 32768, 4000)}, ["citation_ids"]),
    "goldmine_status": obj({}),
}
DESCRIPTIONS = {
    "goldmine_search": "Search only this explicitly selected local index; return stored evidence and opaque citations.",
    "goldmine_get": "Read indexed evidence using an opaque citation returned by search; no source-file access.",
    "goldmine_context": "Build a budgeted context from stored citations and nearby indexed segments.",
    "goldmine_status": "Inspect this index's identity, counts and supported retrieval capabilities.",
}


def search_request(arguments: dict, *, timeout_s: float) -> SearchRequest:
    query = arguments["query"]
    try:
        valid = query.strip() and len(query.encode("utf-8")) <= 8192
    except UnicodeError:
        valid = False
    if not valid:
        raise ApplicationError("invalid_params", "query must be nonempty UTF-8 of at most 8192 bytes", exit_code=2)
    clean, filters = parse_filters(query)
    explicit = arguments.get("filters") or {}
    for field in ("kinds", "under", "site", "phrases"):
        value = explicit.get(field)
        if value:
            if field != "phrases" and getattr(filters, field):
                raise ApplicationError("invalid_params", f"{field} cannot appear both inline and in filters", exit_code=2)
            if field == "kinds":
                filters.kinds = set(value)
            else:
                getattr(filters, field).extend(value)
    for field in ("after", "before"):
        if field in explicit:
            stamp = _date_to_epoch(explicit[field])
            if stamp is None:
                raise ApplicationError("invalid_params", f"invalid {field} date", exit_code=2)
            if getattr(filters, field) is not None:
                raise ApplicationError("invalid_params", f"{field} cannot appear both inline and in filters", exit_code=2)
            setattr(filters, field, stamp)
    if filters.after is not None and filters.before is not None and filters.after > filters.before:
        raise ApplicationError("invalid_params", "after must not be later than before", exit_code=2)
    return SearchRequest(query=clean, filters=filters, require_citations=True,
        limit=arguments.get("limit", 10), context=arguments.get("context", 1),
        pack=arguments.get("pack", False), budget=arguments.get("budget", 4000),
        why=arguments.get("why", False), mode=arguments.get("mode", "fast"),
        rerank=arguments.get("rerank", "off"), multi_query=arguments.get("multi_query", False),
        timeout_s=timeout_s)


def project_search(result, *, pack: bool) -> dict:
    meta = {"settings": result.settings, "via": result.via,
            "fallback_reason": result.fallback_reason, "stale": result.stale}
    if pack:
        pm = result.pack_meta or {}
        meta.update({"used_tokens": pm["used_tokens"], "token_mode": pm["token_mode"],
                     "tokenizer": pm["tokenizer"],
                     "records": len(pm["records"]), "skipped": pm["skipped_over_budget"] + pm.get("skipped_by_record_limit", 0)})
        if len(json.dumps(meta, ensure_ascii=False).encode("utf-8")) > 2048:
            raise ApplicationError("invalid_index", "pack metadata exceeds 2KiB")
        return {"context_text": result.pack_text, "metadata": meta}
    citations = {item.segment_id: asdict(item) for item in result.citations}
    hits = []
    for hit in result.hits:
        entry = asdict(hit)
        entry.pop("excerpt_marked", None)
        entry["citation"] = citations.get(hit.segment_id)
        entry["supporting_citations"] = [asdict(c) for c in result.supporting_citations.get(hit.segment_id, [])]
        context = result.contexts.get(hit.rank)
        if context is not None:
            entry["context"] = [{**part, "citation": citations.get(part["segment_id"])} for part in context.parts]
            if context.note:
                entry["context_note"] = context.note
        hits.append(entry)
    return {"hits": hits, "metadata": meta}


def project_context(result) -> dict:
    return {"context_text": result.context_text, "metadata": {
        "used_tokens": result.used_tokens, "token_mode": result.token_mode,
        "tokenizer": result.tokenizer, "skipped": result.skipped,
        "truncated": result.truncated, "evidence_type": result.evidence_type,
        "file_status": result.file_status,
    }}
