"""Fixed product.v4 denominators and deterministic audit sampling."""

from collections import Counter
import random

from .schema import CLASSES, MEDIA, NOISE


def coverage_missing(queries, sources):
    missing = []
    counts = Counter(q["primary_class"] for q in queries)
    if len(queries) != 420:
        missing.append(f"unique questions: {len(queries)}/420; fixed answerable/noise target 360/60")
    for primary in CLASSES:
        subset = [q for q in queries if q["primary_class"] == primary]
        if counts[primary] != 60:
            missing.append(f"{primary}: {counts[primary]}/60")
        if primary != "noise" and len({q["source_group"] for q in subset}) < 12:
            missing.append(f"{primary}: independent source groups {len({q['source_group'] for q in subset})}/12")
    def at_least(primary, tags, need, label):
        n = sum(q["primary_class"] == primary and bool(set(q["tags"]) & set(tags)) for q in queries)
        if n < need:
            missing.append(f"{label}: {n}/{need}")
    for tag in ("code", "docs"):
        at_least("semantic", [tag], 15, "semantic " + tag)
    for tag, n in (("zh_only", 30), ("mixed", 15), ("ja", 5), ("ko", 5)):
        at_least("cjk", [tag], n, "cjk " + tag)
    for tags in (("filename", "path"), ("mixed_identifier",), ("punctuated_identifier",)):
        at_least("lookup", tags, 15, "lookup " + "/".join(tags))
    at_least("pdf-page", ["text_pdf"], 30, "text PDF")
    at_least("pdf-page", ["scanned_pdf", "mixed_pdf"], 20, "scanned/mixed PDF")
    for tag in MEDIA:
        at_least("media-text", [tag], 20, "media " + tag)
    for tag in NOISE:
        at_least("noise", [tag], 20, "noise " + tag)
    at_least("compound", ["cross_file"], 30, "cross-file compound")
    usage = Counter(path for q in queries for path in {e["path"] for e in q["expect"]})
    for path, n in sorted(usage.items()):
        if n > 8:
            missing.append(f"source {path} carries {n} questions, maximum 8")
    for primary in CLASSES[:-1]:
        subset = [q for q in queries if q["primary_class"] == primary]
        if subset and not any(any(sources[e["path"]]["provenance"]["type"] == "real" for e in q["expect"]) for q in subset):
            missing.append(f"{primary}: only synthetic canaries; real source curation required")
    return missing


def audit_sample_ids(queries, seed=20261021):
    rng = random.Random(seed)
    selected = []
    for primary in CLASSES:
        ids = sorted(q["id"] for q in queries if q["primary_class"] == primary)
        selected.extend(rng.sample(ids, min(8, len(ids))))
    return sorted(selected)
