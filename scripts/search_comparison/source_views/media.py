"""Full-image OCR and full-input ASR; never use annotation passages as text."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import math
import wave

from answer_eval.protocol import EvaluationError, parse_json, sha256_file
from .coverage import unit
from .sources import read_bounded


def ocr_text(image, commands, name):
    payload = json.dumps({"id": "0", "path": str(image)}, ensure_ascii=False).encode("utf-8") + b"\n"
    stdin = commands.input_file(name + ".jsonl", payload)
    raw, _, generated = commands.call("image.ocr", name, input=image, stdin_file=stdin)
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1 or generated:
        raise EvaluationError("one full image must have one OCR record and no extra output", status="MISSING")
    result = parse_json(lines[0], "Vision OCR")
    if not isinstance(result, dict) or result.get("id") != "0":
        raise EvaluationError("OCR response input identity differs", status="MISSING")
    if "error" in result:
        raise EvaluationError("OCR image error: " + str(result["error"]), status="MISSING")
    if (set(result) != {"id", "path", "texts", "ms"} or result["path"] != str(image)
            or not isinstance(result["texts"], list) or type(result["ms"]) not in (int, float)
            or not math.isfinite(result["ms"]) or result["ms"] < 0):
        raise EvaluationError("OCR full-image result schema differs", status="MISSING")
    for block in result["texts"]:
        if (not isinstance(block, dict) or set(block) != {"text", "confidence", "box"}
                or not isinstance(block["text"], str) or type(block["confidence"]) not in (int, float)
                or not math.isfinite(block["confidence"]) or not 0 <= block["confidence"] <= 1
                or not isinstance(block["box"], list) or len(block["box"]) != 4
                or any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1 for n in block["box"])):
            raise EvaluationError("OCR block is malformed", status="MISSING")
    # Retain every actual block. No question selection or confidence filtering.
    return "\n".join(block["text"] for block in result["texts"]), result["texts"]


def convert_audio(asset, commands, limits):
    raw, _, generated = commands.call("audio.probe", "audio-probe", input=asset)
    try:
        duration = Decimal(raw.strip()) * 1000
        if not duration.is_finite() or duration <= 0:
            raise InvalidOperation
        expected = int(duration.to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise EvaluationError("ffprobe did not establish the full clip duration", status="MISSING") from error
    if generated or expected > limits["max_duration_ms"]:
        raise EvaluationError("clip is unbounded/over declared limit; no prefix cap is allowed", status="MISSING")
    details = {"full_input": True, "duration_tolerance_ms": 2}
    try:
        _, decoded, generated = commands.call("audio.decode", "audio-decode", input=asset)
        if set(generated) != {"audio.wav"}:
            raise EvaluationError("full decoder did not return the expected single WAV", status="MISSING")
        wav = decoded / "audio.wav"
        with wave.open(str(wav), "rb") as stream:
            if (stream.getnchannels(), stream.getframerate(), stream.getsampwidth()) != (1, 16000, 2):
                raise EvaluationError("decoded full clip has the wrong fixed audio format", status="MISSING")
            details["decoded_duration_ms"] = stream.getnframes() * 1000 / stream.getframerate()
        if abs(details["decoded_duration_ms"] - expected) > 2:
            raise EvaluationError("full decoded waveform differs from independently probed clip duration", status="MISSING")
        details["decoded_sha256"] = sha256_file(wav)
        _, asr_dir, generated = commands.call("audio.asr", "audio-asr", wav=wav)
        if set(generated) != {"output.json"}:
            raise EvaluationError("whisper full-input JSON missing or unexpected sidecar returned", status="MISSING")
        payload = parse_json(read_bounded(asr_dir / "output.json", limits["max_output_bytes"]).decode("utf-8"), "whisper full JSON")
        rows = payload.get("transcription") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise EvaluationError("whisper did not return a complete transcription list", status="MISSING")
        segments, previous = [], -1
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("text"), str) or not isinstance(row.get("offsets"), dict):
                raise EvaluationError("whisper transcript entry is malformed", status="MISSING")
            start, end = row["offsets"].get("from"), row["offsets"].get("to")
            if (type(start) not in (int, float) or type(end) not in (int, float) or not math.isfinite(start)
                    or not math.isfinite(end) or not 0 <= start <= end <= expected + 2 or start < previous):
                raise EvaluationError("whisper segment is outside the full clip or reordered", status="MISSING")
            segments.append({"start_ms": start, "end_ms": end, "text": row["text"]})
            previous = start
        details.update(segments=segments, raw_json_sha256=sha256_file(asr_dir / "output.json"),
                       coverage_scope="whole-input invocation, not a guarantee every spoken word was recognized")
        return expected, [unit("millisecond", 0, expected, "\n".join(r["text"] for r in rows), "whispercpp-full-no-vad/v1", details=details)]
    except (EvaluationError, OSError, ValueError, wave.Error) as error:
        return expected, [unit("millisecond", 0, expected, "", "full-audio-conversion-failed", error=str(error), details=details)]
