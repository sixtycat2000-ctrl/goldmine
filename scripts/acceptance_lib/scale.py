"""Source-bound full capacity adapter; old vector-only reports are incomplete."""
from pathlib import Path

from .evidence import InvalidEvidence


def check(root: Path, inputs, targets):
    from scale_bench.validation import validate
    from .performance import _reference_source

    try:
        path = inputs.file("evidence.scale", manifest=True)
        result = validate(path, current_project=root, expected_reference=_reference_source(root),
                          targets=targets["scale"], require_all=True)
        return result["status"], result
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError) as exc:
        return "MISSING", {"reason": str(exc), "required": "scale.full.v2: all12 native-schema paired cases; "
            "10 unrounded phase samples and30 actual cold/warm search+JSON-pack+human-pack calls per arm; "
            "raw streams, timing/RSS, immutable SQLite/readback and current candidate source"}
