"""Deterministic whole-source grouping; no retrieval scores are inputs."""

from __future__ import annotations

import random
import re
from collections import Counter, defaultdict

from answer_eval.protocol import EvaluationError, canonical_sha256, fields
from .schema import CLASSES, MEDIA, NOISE


def source_groups(sources: dict, queries: list[dict], views: dict) -> dict[str, str]:
    parent = {s["source_group"]: s["source_group"] for s in sources.values()}

    def find(x):
        while x != parent[x]:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    hashes = {}
    shingles = {}
    for path, s in sorted(sources.items()):
        group = s["source_group"]
        for ancestor in s["parents"]:
            if ancestor not in sources:
                raise EvaluationError("source parent is absent from manifest")
            union(group, sources[ancestor]["source_group"])
        identities = [s["asset"]["sha256"]] + [v["sha256"] for v in s["views"]]
        for identity in identities:
            if identity in hashes:
                union(group, hashes[identity])
            else:
                hashes[identity] = group
        # Detect near copies even if a curator omitted the provenance edge.
        text = "\n".join(views[(path, v["id"])]["text"] for v in s["views"] if v["evidence_type"] != "name_metadata")
        tokens = re.findall(r"\w+", text.casefold())
        grams = {tuple(tokens[i:i + 5]) for i in range(max(0, len(tokens) - 4))}
        if len(grams) >= 30:
            for old_group, old_grams in shingles.items():
                if len(grams & old_grams) / len(grams | old_grams) >= 0.90:
                    union(group, old_group)
            shingles[group] = grams
    for q in queries:
        groups = [sources[e["path"]]["source_group"] for e in q["expect"]]
        for group in groups[1:]:
            union(groups[0], group)
    return {path: find(s["source_group"]) for path, s in sources.items()}


def _vector(q):
    values = {q["primary_class"]: 1}
    if q["primary_class"] == "media-text":
        for subtype in MEDIA:
            if subtype in q["tags"]:
                values["media:" + subtype] = 1
    if q["primary_class"] == "noise":
        for subtype in NOISE:
            if subtype in q["tags"]:
                values["noise:" + subtype] = 1
    return values


def make_splits(queries: list[dict], *, seed: int = 20261021) -> dict:
    grouped = defaultdict(list)
    for q in queries:
        grouped[q["source_group"]].append(q)
    vectors = {g: sum((Counter(_vector(q)) for q in qs), Counter()) for g, qs in grouped.items()}
    total = sum(vectors.values(), Counter())
    if any(total[c] < 50 for c in CLASSES) or any(total["media:" + m] < 16 for m in MEDIA):
        raise EvaluationError("not enough independent labeled data to make complete dev/holdout folds", status="MISSING")
    rng = random.Random(seed)
    keys = sorted(vectors)
    targets = {k: n / 2 for k, n in total.items()}
    best = None
    for _ in range(3000):
        order = keys[:]
        rng.shuffle(order)
        order.sort(key=lambda g: -sum(vectors[g].values()))
        selected, counts = set(), Counter()
        for group in order:
            candidate = counts + vectors[group]
            before = sum((counts[k] - targets[k]) ** 2 for k in targets)
            after = sum((candidate[k] - targets[k]) ** 2 for k in targets)
            if after < before:
                selected.add(group)
                counts = candidate
        valid = all(25 <= counts[c] <= total[c] - 25 for c in CLASSES)
        valid &= all(8 <= counts["media:" + m] <= total["media:" + m] - 8 for m in MEDIA)
        if valid:
            loss = sum((counts[k] - targets[k]) ** 2 for k in targets)
            choice = (loss, tuple(sorted(selected)))
            if best is None or choice < best:
                best = choice
                if loss == 0:
                    break
    if best is None:
        raise EvaluationError("no valid grouped stratified split; add independent sources, do not split linked groups", status="MISSING")
    holdout_groups = set(best[1])
    return {"schema": 1, "seed": seed, "queries_sha256": canonical_sha256(queries),
            "dev": sorted(q["id"] for q in queries if q["source_group"] not in holdout_groups),
            "holdout": sorted(q["id"] for q in queries if q["source_group"] in holdout_groups)}


def validate_splits(split, queries: list[dict]):
    fields(split, {"schema", "seed", "queries_sha256", "dev", "holdout"}, "splits")
    if type(split["schema"]) is not int or split["schema"] != 1 or split["seed"] != 20261021:
        raise EvaluationError("splits schema/seed mismatch")
    if split["queries_sha256"] != canonical_sha256(queries):
        raise EvaluationError("splits are stale for these questions")
    lookup = {q["id"]: q for q in queries}
    for fold in ("dev", "holdout"):
        ids = split[fold]
        if (not isinstance(ids, list) or any(not isinstance(q, str) for q in ids)
                or len(ids) != len(set(ids)) or not set(ids) <= lookup.keys()):
            raise EvaluationError("invalid/duplicate split question IDs")
    if set(split["dev"]) & set(split["holdout"]) or set(split["dev"]) | set(split["holdout"]) != lookup.keys():
        raise EvaluationError("split must partition every question exactly once")
    group_sets = [{lookup[q]["source_group"] for q in split[f]} for f in ("dev", "holdout")]
    if group_sets[0] & group_sets[1]:
        raise EvaluationError("source group leaked across folds")
    for fold in ("dev", "holdout"):
        counts = Counter(lookup[q]["primary_class"] for q in split[fold])
        media = Counter(t for qid in split[fold] if lookup[qid]["primary_class"] == "media-text" for t in lookup[qid]["tags"] if t in MEDIA)
        if any(counts[c] < 25 for c in CLASSES) or any(media[m] < 8 for m in MEDIA):
            raise EvaluationError("fold category/media minimums not satisfied", status="MISSING")
