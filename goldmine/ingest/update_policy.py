"""One update decision for ordinary ingest and explicit repair planning."""
from __future__ import annotations

import os
from pathlib import Path
import shutil

from ..index_state import missing_text_segments, update_reasons
from . import media


def thin_marker(rec):
    return bool(rec.detail and "page needs OCR" in rec.detail) or bool((rec.info or {}).get("thin_text"))


def needs_ocr_redo(rec):
    info = rec.info or {}
    if rec.kind == "pdf":
        states = info.get("ocr_page_states", {})
        return any(state in ("unavailable", "error") for state in states.values()) if states else thin_marker(rec)
    if rec.kind != "image" or not info.get("screenshot"):
        return False
    if info.get("ocr_status") in ("unavailable", "error"):
        return True
    if info.get("ocr_status") in ("complete", "no_text", "low_confidence"):
        return False
    return info.get("ocr") == "unavailable" or ("ocr" not in info and "ocr_chars" not in info)


def static_ocr_ready(cfg):
    if not cfg.ocr.enabled:
        return False
    explicit = os.environ.get("GOLDMINE_OCR_HELPER")
    path = Path(explicit).expanduser() if explicit else Path(cfg.home).expanduser() / "bin" / "goldmine-ocr"
    return path.is_file() and os.access(path, os.X_OK)


def reasons_for_update(store, rec, cfg, backend, *, extractors=None, refresh_thin=False,
                       ocr_ready=None, asr_ready=None):
    from .asr import resolve_tools

    embed_id = None
    if backend is not None:
        from ..fingerprints import live_embed_id

        embed_id = live_embed_id(backend)
    reasons = update_reasons(store, rec, embed_id=embed_id, extractors=extractors)
    info = rec.info or {}
    cap = backend.capability() if backend is not None else None
    if rec.kind == "image":
        if info.get("exif_version") != media.EXIF_METADATA_VERSION:
            reasons.append("exif_version_changed")
        if info.get("exif_status") not in {"complete", "absent", "unsupported", "unavailable"}:
            reasons.append("exif_retry")
        if info.get("exif_reader") is None and media.exif_reader(Path(rec.path)) is not None:
            reasons.append("exif_reader_available")
    if refresh_thin and thin_marker(rec):
        reasons.append("refresh_thin")
    if needs_ocr_redo(rec) and (ocr_ready or (lambda: static_ocr_ready(cfg)))():
        from .extract_pdf import pdftoppm_path

        if rec.kind != "pdf" or pdftoppm_path() is not None:
            reasons.append("ocr_retry")
    if cap is None:
        return list(dict.fromkeys(reasons))
    if (rec.kind in {"audio", "video"} and info.get("asr_status") in {"unavailable", "error"}
            and cap.text and str(cfg.ingest.asr) != "off"
            and (asr_ready or (lambda: resolve_tools(cfg) is not None))()):
        reasons.append("asr_retry")
    if cap.text and missing_text_segments(store, rec, image_capable=cap.image,
                                          ocr_text_vector=cfg.ingest.ocr_text_vector):
        reasons.append("missing_vectors")
    if (rec.kind in {"audio", "video"}
            and (info.get("audio_routing") == "none:lexical_only"
                 or (cap.audio and info.get("audio_routing") is None and info.get("asr_segments") is None))):
        reasons.append("media_upgrade")
    if rec.kind == "image" and cap.image and rec.capability == "metadata_only" and rec.detail == "backend lacks image embedding":
        reasons.append("media_upgrade")
    if rec.kind == "video" and cap.image:
        frames = info.get("frames")
        if frames in (None, "none:no_image_capability"):
            reasons.append("media_upgrade")
        elif isinstance(frames, int) and info.get("frame_timestamps") != media.FRAME_TIMESTAMPS_VERSION:
            reasons.append("frame_timestamps_changed")
        elif isinstance(frames, str) and (frames.startswith("error:") or frames == "none:ffmpeg_missing") and media.ffmpeg_path() is not None:
            reasons.append("frames_retry")
    if cap.audio and info.get("audio_routing") in {"none:ffprobe_missing", "none:ffmpeg_missing"}:
        tool = media.ffprobe_path() if info["audio_routing"] == "none:ffprobe_missing" else media.ffmpeg_path()
        if tool is not None:
            reasons.append("media_upgrade")
    if rec.capability == "metadata_only" and rec.detail:
        from .extract_pdf import pdftotext_path

        if ((rec.kind == "doc" and "textutil" in rec.detail and shutil.which("textutil"))
                or (rec.kind == "pdf" and "pdftotext" in rec.detail and pdftotext_path())):
            reasons.append("extractor_available")
    return list(dict.fromkeys(reasons))
