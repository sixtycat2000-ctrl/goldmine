"""Small conversion dispatch over original copied assets, never oracle bodies."""
from __future__ import annotations

from pathlib import Path

from answer_eval.protocol import EvaluationError
from .coverage import unit
from .media import convert_audio, ocr_text
from .pdf import convert_pdf
from .sources import MAX_SOURCE_BYTES, read_bounded


def jpeg_fields(asset):
    # This profile is JPEG-only. Do not invoke ImageIO or another reader fallback.
    from goldmine.ingest.exif import _jpeg_capture, parse_capture_time

    with asset.open("rb") as stream:
        if stream.read(2) != b"\xff\xd8":
            raise EvaluationError("JPEG metadata profile received an unsupported file", status="MISSING")
        fields = _jpeg_capture(stream)
    taken = parse_capture_time(fields["DateTimeOriginal"], fields["OffsetTimeOriginal"]) if fields else None
    return {"DateTimeOriginal": taken[0] if taken else None,
            "OffsetTimeOriginal": fields.get("OffsetTimeOriginal") if fields else None,
            "capture_status": "complete" if taken else "not_established", "reader": "jpeg-exif-original"}


def convert(source, asset: Path, commands, limits):
    kind = source["kind"]
    if kind in ("doc", "code"):
        raw = read_bounded(asset, MAX_SOURCE_BYTES)
        text = raw.decode("utf-8")  # Invalid UTF-8 is explicit failure, never replacement decoding.
        return len(raw), [unit("byte", 0, len(raw), text, "utf8-full-nfc-lf/v1")]
    if kind == "pdf":
        return convert_pdf(source, asset, commands, limits)
    if kind == "audio":
        return convert_audio(asset, commands, limits)
    if kind == "screenshot":
        try:
            text, blocks = ocr_text(asset, commands, "full-image-ocr")
            return 1, [unit("image", 0, 1, text, "vision-full/v1", details={"blocks": blocks, "full_input": True})]
        except (EvaluationError, OSError, ValueError) as error:
            return 1, [unit("image", 0, 1, "", "full-image-ocr-failed", error=str(error))]
    if kind == "image":
        metadata = jpeg_fields(asset)
        text = "Source: " + source["path"] + "\nDateTimeOriginal: " + (metadata["DateTimeOriginal"] or "[not-established]")
        if metadata["OffsetTimeOriginal"] is not None:
            text += "\nOffsetTimeOriginal: " + metadata["OffsetTimeOriginal"]
        return 1, [unit("metadata", 0, 1, text, "jpeg-original-metadata/v1", details=metadata)]
    raise EvaluationError("source kind has no frozen full-conversion profile", status="MISSING")
