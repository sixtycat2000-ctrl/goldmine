"""Plan and execute targeted repairs from stored state and explicit scope."""
from __future__ import annotations

from pathlib import Path

from . import fingerprints
from .backends.base import EmbeddingError, document_prompt
from .index_state import (forgotten, missing_text_segments, publish_identity,
                          select_files, validate_identity)
from .util import pack_fp16


def plan_repairs(store, cfg, backend, targets=(), *, failed_only=False, missing_vectors=False):
    from .ingest.update_policy import reasons_for_update

    extractors = fingerprints.current_extractors()
    live = fingerprints.build(backend) if backend is not None else None
    cap = backend.capability() if backend is not None else None
    plan = []
    for rec in select_files(store, targets):
        if forgotten(store, rec.path) or (failed_only and rec.state != "failed"):
            continue
        reasons = reasons_for_update(store, rec, cfg, backend, extractors=extractors)
        if rec.state == "done" and rec.capability == "unsupported":
            continue
        if live is not None and store.meta("fingerprint_id") not in (None, fingerprints.fingerprint_id(live)):
            reasons.append("model_changed")
        text_segments = missing_text_segments(
            store, rec, image_capable=bool(cap and cap.image), ocr_text_vector=cfg.ingest.ocr_text_vector,
        ) if cap and cap.text else []
        if rec.source == "file":
            try:
                st = Path(rec.path).stat()
            except OSError:
                reasons.append("source_unavailable")
            else:
                if st.st_size != rec.size or st.st_mtime != rec.mtime:
                    reasons.append("source_changed")
        reasons = list(dict.fromkeys(reasons))
        if missing_vectors and not ({"missing_vectors", "media_upgrade"} & set(reasons)):
            continue
        if not reasons:
            continue
        action = "backfill" if reasons == ["missing_vectors"] else "reingest"
        if "source_unavailable" in reasons:
            action = "unavailable"
        if "model_changed" in reasons:
            action = "reindex_required"
        plan.append({"file_id": rec.id, "path": rec.path, "kind": rec.kind,
                     "reasons": reasons, "action": action,
                     "missing_text_segments": len(text_segments)})
    return plan


def execute_repairs(store, cfg, backend, plan, *, lexical_only=False):
    from .ingest import IngestPipeline
    from .ingest.crawl import CrawlBounds

    result = {"planned": len(plan), "vectors_added": 0, "files_backfilled": 0,
              "failures": [], "ingest": None}
    if any(item["action"] == "reindex_required" for item in plan):
        raise fingerprints.FingerprintMismatch("repair cannot mix models; use goldmine ingest --reindex on the recorded roots")
    if backend is not None and plan:
        validate_identity(store, fingerprints.build(backend), embed_id=fingerprints.live_embed_id(backend))
    live, identity, started = None, None, False
    reingest = [item["path"] for item in plan if item["action"] == "reingest"]
    for item in plan:
        if item["action"] == "unavailable":
            result["failures"].append({"path": item["path"], "detail": "source unavailable; restore or explicitly forget it"})
            continue
        if item["action"] != "backfill":
            continue
        rec = store.get_file(item["file_id"])
        if rec is None or backend is None:
            continue
        try:
            if not started:
                backend.startup()
                started = True
                live = fingerprints.build(backend)
                identity = fingerprints.live_embed_identity(backend)
                validate_identity(store, live, embed_id=fingerprints.live_embed_id(backend))
            cap = backend.capability()
            segments = missing_text_segments(store, rec, image_capable=cap.image,
                                             ocr_text_vector=cfg.ingest.ocr_text_vector)
            if not segments:
                continue
            title = (rec.info or {}).get("title") or (Path(rec.path).stem if rec.source == "file" else "page")
            prompts = [document_prompt(title, text) for _, text in segments]
            ceiling = backend.max_input_tokens()
            counts = backend.count_tokens(prompts) if ceiling and prompts else None
            if ceiling and any(count > ceiling for count in (counts or [max(1, len(p) // 4) for p in prompts])):
                reingest.append(rec.path)
                continue
            vectors = []
            batch = max(1, int(cfg.llama.batch))
            for start in range(0, len(prompts), batch):
                vectors.extend(backend.embed_texts(prompts[start:start + batch]))
            if len(vectors) != len(segments):
                raise EmbeddingError("backfill returned an incomplete embedding batch")
            with store.write_transaction():
                current = store.get_file(rec.id)
                if current is None or current.sha256 != rec.sha256 or forgotten(store, rec.path):
                    raise EmbeddingError("source index changed during backfill; retry the repair")
                publish_identity(store, live, identity=identity)
                for (sid, _), vector in zip(segments, vectors):
                    cursor = store.db.execute(
                        "UPDATE segments SET vec=?,modality='text' WHERE id=? AND file=? AND vec IS NULL",
                        (pack_fp16(vector), sid, rec.id))
                    if cursor.rowcount != 1:
                        raise EmbeddingError("indexed segment changed during backfill; retry the repair")
                store.db.execute("UPDATE files SET embed_id=? WHERE id=?", (fingerprints.live_embed_id(backend), rec.id))
                store._vec_generation += 1
            result["vectors_added"] += len(vectors)
            result["files_backfilled"] += 1
        except (EmbeddingError, OSError) as exc:
            result["failures"].append({"path": rec.path, "detail": str(exc)})
    if reingest:
        pipeline = IngestPipeline(cfg, store, backend, lexical_only=lexical_only)
        report = pipeline.run(list(dict.fromkeys(reingest)), code=True, reindex=True,
                              crawl_bounds=CrawlBounds(max_depth=0), update_roots=False)
        result["ingest"] = report.to_json()
        result["failures"].extend(report.failures)
        result["cancelled"] = report.cancelled
    return result
