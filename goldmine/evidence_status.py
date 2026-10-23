"""Stored media evidence states, without probing tools or reading source files."""
from collections import Counter
import json

from .deadline import check_deadline


def stored_evidence_status(store, *, limit=100):
    states, types, shown = Counter(), Counter(), []
    total = 0
    for path, kind, capability, raw in store.db.execute(
            "SELECT path,kind,capability,info FROM files WHERE kind IN ('pdf','image','audio','video') ORDER BY path"):
        check_deadline()
        info = json.loads(raw) if raw else {}
        info = info if isinstance(info, dict) else {}
        evidence = info.get("evidence_types") or []
        evidence = evidence if isinstance(evidence, list) else []
        types.update(str(item) for item in set(evidence))
        ocr, asr = info.get("ocr_status"), info.get("asr_status")
        states[f"{kind}:ocr:{ocr or 'unrecorded'}"] += 1 if kind in ("pdf", "image") else 0
        states[f"{kind}:asr:{asr or 'unrecorded'}"] += 1 if kind in ("audio", "video") else 0
        total += 1
        if len(shown) < limit:
            shown.append({"path": path, "kind": kind, "capability": capability,
                          "evidence_types": evidence, "ocr_status": ocr, "asr_status": asr,
                          "ocr_page_states": info.get("ocr_page_states"),
                          "status_recorded": bool(evidence or ocr or asr)})
    return {"media_files": total, "evidence_type_counts": dict(sorted(types.items())),
            "state_counts": {key: value for key, value in sorted(states.items()) if value},
            "files": shown, "files_truncated": max(0, total - len(shown))}
