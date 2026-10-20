"""Honest evidence labels derived from stored segments and extractor provenance."""

from __future__ import annotations


def segment_evidence_types(kind, modality, text, file_kind, extractor_version=None, info=None, loc=None):
    if kind == "name":
        return "name_metadata", "name_metadata"
    if text is None:
        media = "image_vector" if modality == "image" else "audio_vector" if modality == "audio" else None
        return "vector_only", media
    if kind == "ocr" or (file_kind == "pdf" and loc in (info or {}).get("ocr_text_pages", (info or {}).get("ocr_pages", []))):
        return "content", "ocr_text"
    if file_kind in ("audio", "video") and (extractor_version or "").startswith("asr-"):
        return "content", "asr_text"
    return "content", None


def file_evidence_types(db, fid, *, kind, extractor_version=None, info=None):
    labels = set()
    for segment_kind, modality, loc, text in db.execute(
        "SELECT s.kind,s.modality,s.loc,coalesce(f.original,f.body) "
        "FROM segments s LEFT JOIN fts f ON f.rowid=s.id WHERE s.file=?", (fid,),
    ):
        broad, media = segment_evidence_types(segment_kind, modality, text, kind, extractor_version, info, loc)
        labels.add(media or broad)
    return sorted(labels)
