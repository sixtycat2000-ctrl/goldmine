"""Read back stored segments and align only text actually sent in pack bodies."""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import math
from pathlib import Path

from answer_eval.protocol import EvaluationError
from .alignment import asr_word_spans, exact_character_spans
from .bundle import normalized
from .results import record_body_present


def relative_source(path, corpus_root, bundle):
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(Path(corpus_root).resolve()).as_posix()
    except ValueError as exc:
        raise EvaluationError("search returned a file outside the explicit product corpus") from exc
    if relative not in bundle.sources:
        raise EvaluationError("search returned an unregistered corpus file")
    return relative


def read_segment_spans(store, segment_id, source_path, actual_path, bundle):
    row = store.db.execute("SELECT s.kind,s.loc,s.loc_end,f.path,f.info,f.source,f.kind FROM segments s JOIN files f ON s.file=f.id WHERE s.id=?", (segment_id,)).fetchone()
    if row is None or Path(row[3]).resolve() != Path(actual_path).resolve():
        raise EvaluationError("citation segment/source mismatch")
    source = bundle.sources[source_path]
    spans = []
    if row[0] == "name":
        payload = {"path": row[3], "basename": Path(row[3]).name, "info": json.loads(row[4] or "{}"),
                   "source": row[5], "kind": row[6]}
        for definition in source["views"]:
            view = bundle.views[(source_path, definition["id"])]
            if view["evidence_type"] != "name_metadata":
                continue
            value = payload
            for field in view["storage_field"].split("."):
                value = value.get(field) if isinstance(value, dict) else None
            text = normalized(str(value)) if value is not None else None
            if text == view["text"]:
                spans.append({"path": source_path, "view_id": view["id"], "evidence_type": "name_metadata",
                              "location": None, "start_char": 0, "end_char": len(text), "text": text,
                              "citation_id": f"eval-segment:{segment_id}", "readback_verified": True})
        return spans
    body = store.segment_text(segment_id)
    if not body:
        return spans
    text = normalized(body)
    for definition in source["views"]:
        view = bundle.views[(source_path, definition["id"])]
        location = view["location"]
        if view["evidence_type"] == "name_metadata" or location is None or row[1] is None:
            continue
        start, end = row[1], row[2] if row[2] is not None else row[1]
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end)):
            raise EvaluationError("stored citation has nonnumeric/nonfinite locations")
        if location["unit"] in ("page", "line"):
            if not float(start).is_integer() or not float(end).is_integer():
                raise EvaluationError("stored page/line citation has fractional coordinates")
            start, end = int(start), int(end)
        if location["unit"] == "page" and not start == end == location["start"] == location["end"]:
            continue
        if end < location["start"] or start > location["end"]:
            continue
        view_text = view["text"]
        lower, upper = 0, len(view_text)
        if location["unit"] == "line":
            lines = view_text.splitlines(keepends=True)
            lower = sum(len(x) for x in lines[:max(0, int(start - location["start"]))])
            upper = sum(len(x) for x in lines[:min(len(lines), int(end - location["start"] + 1))])
        cursor = lower
        exact = False
        while (offset := view_text.find(text, cursor, upper)) >= 0:
            exact = True
            spans.append({"path": source_path, "view_id": view["id"], "evidence_type": view["evidence_type"],
                          "location": {"unit": location["unit"], "start": start, "end": end},
                          "start_char": offset, "end_char": offset + len(text), "text": text,
                          "citation_id": f"eval-segment:{segment_id}", "readback_verified": True})
            cursor = offset + max(1, len(text))
        if not exact:
            # An unrelated OCR typo/header must not erase an otherwise exact
            # complete fact. ASR has its separately versioned word proof; all
            # other evidence retains literal characters and a 32-char minimum.
            align = asr_word_spans if view["evidence_type"] == "asr_text" else exact_character_spans
            for matched in align(view_text[lower:upper], text):
                spans.append({"path": source_path, "view_id": view["id"], "evidence_type": view["evidence_type"],
                              "location": {"unit": location["unit"], "start": start, "end": end},
                              **matched, "start_char": lower + matched["start_char"],
                              "end_char": lower + matched["end_char"],
                              "citation_id": f"eval-segment:{segment_id}", "readback_verified": True})
    return spans


def emitted_spans(store, meta, pack_text, corpus_root, bundle):
    result = []
    for record in meta["records"]:
        body = normalized(record["text"])
        context = normalized(pack_text)
        if not record_body_present(record, context):
            raise EvaluationError("pack sidecar claims text absent from the emitted context_text")
        path = relative_source(record["path"], corpus_root, bundle)
        parts = record["segments"] or ([{"segment_id": record["segment_id"]}] if record["segment_id"] is not None else [])
        for part in parts:
            for span in read_segment_spans(store, part["segment_id"], path, record["path"], bundle):
                if "word_alignment" in span:
                    # Re-align against this ONE actual material record. The
                    # observed witness must also occur literally in read-back;
                    # text from another stored part cannot impersonate it.
                    readback = span["word_alignment"]["observed_text"]
                    for matched in asr_word_spans(span["text"], body):
                        if matched["word_alignment"]["observed_text"] in readback:
                            result.append({**span, **matched,
                                           "start_char": span["start_char"] + matched["start_char"],
                                           "end_char": span["start_char"] + matched["end_char"]})
                    continue
                # Seam trimming can remove a prefix. Retain only actual matching
                # source text, never the complete untrimmed stored segment.
                matcher = SequenceMatcher(None, span["text"], body, autojunk=False)
                for match in matcher.get_matching_blocks():
                    if match.size:
                        result.append({**span, "start_char": span["start_char"] + match.a,
                                       "end_char": span["start_char"] + match.a + match.size,
                                       "text": span["text"][match.a:match.a + match.size]})
    return result


def index_capability_missing(store, bundle, corpus_root, *, original_reference=None):
    missing = []
    unsupported = set()
    if original_reference is not None:
        from answer_eval.protocol import fields
        from .reference_support import verify_original_support
        fields(original_reference,{"production_root","production"},"original reference context")
        receipt = verify_original_support(store,bundle,corpus_root,original_reference["production_root"],original_reference["production"])
        unsupported = {row["path"] for row in receipt["unsupported"]}
    for path, source in bundle.sources.items():
        if path in unsupported:
            continue
        actual = str((Path(corpus_root) / path).resolve())
        row = store.db.execute("SELECT id,sha256,state,info,extractor_version FROM files WHERE path=?", (actual,)).fetchone()
        if row is None or row[1] != source["asset"]["sha256"] or row[2] != "done":
            missing.append(f"{path}: indexed source missing/stale/incomplete")
            continue
        info = json.loads(row[3] or "{}")
        bodies = store.db.execute("SELECT s.kind,coalesce(f.original,f.body) FROM segments s JOIN fts f ON f.rowid=s.id WHERE s.file=? AND s.kind!='name'", (row[0],)).fetchall()
        needed = {need for q in bundle.queries if any(e["path"] == path for e in q["expect"]) for need in q["needs"]}
        if "asr" in needed and (not str(row[4] or "").startswith("asr-") or not any(kind == "audio" and text.strip() for kind, text in bodies)):
            missing.append(f"{path}: actual ASR content segments missing")
        if "ocr" in needed and (not any(text.strip() for _, text in bodies) or not (info.get("ocr") or info.get("ocr_chars") or info.get("ocr_pages"))):
            missing.append(f"{path}: actual OCR content missing")
        if "exif" in needed:
            for q in bundle.queries:
                for e in q["expect"]:
                    if e["path"] == path and e["evidence_type"] == "name_metadata":
                        value = {"path": actual, "basename": Path(actual).name, "info": info}
                        for key in e["storage_field"].split("."):
                            value = value.get(key) if isinstance(value, dict) else None
                        if value is None:
                            missing.append(f"{path}: metadata field {e['storage_field']} missing")
    return missing
