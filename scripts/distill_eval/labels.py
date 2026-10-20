"""Reject undersized, unlicensed or result-derived topic labels before evaluation."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from acceptance_lib.evidence import InvalidEvidence, external_ref, read_json, verify_artifact


def load_labels(path: Path, corpus_root: Path) -> tuple[dict, dict]:
    data = read_json(path)
    required = {"schema", "suite_id", "state", "evidence_mode", "frozen_at", "labeling_protocol",
                "reviewers", "product_manifest", "topics", "documents"}
    if not isinstance(data, dict) or set(data) != required:
        raise InvalidEvidence("real labels need the exact documented schema1 fields")
    if (data["schema"] != 1 or data["suite_id"] != "distill.real.v1" or data["state"] != "FROZEN" or
            data["evidence_mode"] != "real" or data["labeling_protocol"] != "independent-before-clustering"):
        raise InvalidEvidence("require frozen real labels, independently assigned before clustering")
    if not isinstance(data["reviewers"], list) or not data["reviewers"] or any(not isinstance(v, str) or not v.strip() for v in data["reviewers"]):
        raise InvalidEvidence("independent label reviewers must be identified")
    try:
        frozen = datetime.fromisoformat(data["frozen_at"])
        if frozen.utcoffset() is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError) as exc:
        raise InvalidEvidence("frozen_at needs an ISO timestamp with timezone") from exc
    topics = data["topics"]
    if not isinstance(topics, list) or len(topics) < 8 or len(set(topics)) != len(topics) or any(not isinstance(t, str) or not t for t in topics):
        raise InvalidEvidence("need at least8 distinct predeclared topic IDs")
    manifest_path = external_ref(path.parent, data["product_manifest"], manifest=True)
    manifest = read_json(manifest_path)
    if manifest.get("state") != "FROZEN" or manifest.get("evidence_mode") != "real":
        raise InvalidEvidence("product source manifest must itself be frozen and real")
    sources = {source["path"]: source for source in manifest.get("sources", [])}
    if len(sources) != len(manifest.get("sources", [])):
        raise InvalidEvidence("duplicate product source path")
    documents = data["documents"]
    if not isinstance(documents, list):
        raise InvalidEvidence("documents must be a frozen label list")
    by_path, topic_groups, seen = {}, defaultdict(set), set()
    roles = set()
    for row in documents:
        if not isinstance(row, dict) or set(row) != {"path", "topics", "role", "split", "case_tags"}:
            raise InvalidEvidence("each label needs path/topics/role/split/case_tags only")
        name = row["path"]
        if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts or name in seen:
            raise InvalidEvidence("label path is duplicated or not a portable relative source path")
        seen.add(name)
        if row["split"] not in ("base", "incremental") or row["role"] not in ("topic", "cross_topic", "noise"):
            raise InvalidEvidence("unknown distill role/split")
        assigned = row["topics"]
        if not isinstance(assigned, list) or len(set(assigned)) != len(assigned) or set(assigned) - set(topics):
            raise InvalidEvidence("unknown or repeated topic ID")
        if (row["role"] == "topic" and len(assigned) != 1 or row["role"] == "noise" and assigned or
                row["role"] == "cross_topic" and len(assigned) < 2):
            raise InvalidEvidence("topic/cross_topic/noise roles need one/multiple/no topic labels")
        if not isinstance(row["case_tags"], list) or any(not isinstance(tag, str) for tag in row["case_tags"]):
            raise InvalidEvidence("case_tags must be explicit strings, never inferred from clusters")
        source = sources.get(name)
        if not source or source.get("provenance", {}).get("type") != "real":
            raise InvalidEvidence(f"label has no real product source: {name}")
        verify_artifact(manifest_path.parent, source["asset"])
        provenance = source["provenance"]
        if not provenance.get("license"):
            raise InvalidEvidence(f"source license missing: {name}")
        verify_artifact(manifest_path.parent, provenance["license_file"])
        target = (corpus_root / name).resolve()
        if not target.is_relative_to(corpus_root.resolve()):
            raise InvalidEvidence("indexed source path escapes corpus root")
        by_path[str(target)] = {**row, "source_group": source.get("source_group"), "asset_sha256": source["asset"]["sha256"]}
        if row["split"] == "base":
            roles.add(row["role"])
            if row["role"] == "topic":
                if not source.get("source_group"):
                    raise InvalidEvidence("topic document lacks independent source group")
                topic_groups[assigned[0]].add(source["source_group"])
    base = [r for r in by_path.values() if r["split"] == "base"]
    additions = [r for r in by_path.values() if r["split"] == "incremental"]
    if len(base) < 100 or len(additions) != 10:
        raise InvalidEvidence("real corpus needs at least100 base documents and exactly10 incremental additions")
    if any(len(topic_groups[topic]) < 8 for topic in topics):
        raise InvalidEvidence("every frozen topic needs at least8 independent base source groups")
    if not {"noise", "cross_topic"}.issubset(roles):
        raise InvalidEvidence("base corpus needs explicitly labelled noise and cross-topic documents")
    tags = {tag for row in base for tag in row["case_tags"]}
    if len({tag for tag in tags if tag.startswith("language:")}) < 2 or not {"paraphrase", "duplicate"}.issubset(tags):
        raise InvalidEvidence("real labels must include multilingual/paraphrase/duplicate cases")
    return data, by_path
