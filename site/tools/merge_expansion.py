#!/usr/bin/env python3
"""Merge expansion-20261026 parts into site/content/concepts.json.

Idempotent: nodes whose id already exists are skipped (original content is
never rewritten). extra_edges are appended to the source node's dig_deeper
unless an edge to the same target already exists. Validates:
  - every node has the full v2 field set
  - all dig_deeper targets (existing + merged) resolve to real node ids
Exit nonzero on any validation failure; prints a merge summary.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "site/content/concepts.json"
PARTS = sorted((ROOT / "site/content/expansion-20261026").glob("part*.json"))

REQUIRED = {"id", "cluster", "layer", "term_zh", "term_en", "one_liner_zh",
            "one_liner_en", "professional_zh", "professional_en", "plain_zh",
            "plain_en", "example_zh", "example_en", "code_refs", "analogy_zh",
            "analogy_en", "dig_deeper"}


def fail(msg):
    print(f"VALIDATION FAIL: {msg}")
    sys.exit(1)


def main():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    nodes = data["nodes"]
    ids = {n["id"] for n in nodes}
    clusters = {c["id"] for c in data["clusters"]}
    added_nodes, added_edges, skipped = 0, 0, 0

    # pass 1: nodes
    for part in PARTS:
        pdata = json.loads(part.read_text(encoding="utf-8"))
        for n in pdata.get("nodes", []):
            missing = REQUIRED - set(n)
            if missing:
                fail(f"{part.name}: {n.get('id')} missing {sorted(missing)}")
            if n["cluster"] not in clusters:
                fail(f"{part.name}: {n['id']} unknown cluster {n['cluster']}")
            if n["layer"] not in (0, 1, 2, 3):
                fail(f"{part.name}: {n['id']} bad layer {n['layer']}")
            if n["id"] in ids:
                skipped += 1
                continue
            nodes.append(n)
            ids.add(n["id"])
            added_nodes += 1

    # pass 2: extra edges
    for part in PARTS:
        pdata = json.loads(part.read_text(encoding="utf-8"))
        for e in pdata.get("extra_edges", []):
            src = next((x for x in nodes if x["id"] == e["from"]), None)
            if src is None:
                fail(f"{part.name}: extra_edge from unknown node {e['from']}")
            if e["to"] not in ids:
                fail(f"{part.name}: extra_edge to unknown node {e['to']}")
            if any(d["to"] == e["to"] for d in src["dig_deeper"]):
                continue
            src["dig_deeper"].append({"to": e["to"], "why_zh": e["why_zh"],
                                      "why_en": e["why_en"]})
            added_edges += 1

    # pass 3: all edge targets resolve
    for n in nodes:
        for d in n["dig_deeper"]:
            if d["to"] not in ids:
                fail(f"node {n['id']} digs into unknown id {d['to']}")
        if len(n["dig_deeper"]) > 8:
            fail(f"node {n['id']} has {len(n['dig_deeper'])} edges (cap 8)")

    SRC.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    print(f"merged: +{added_nodes} nodes, +{added_edges} edges, "
          f"{skipped} already present; total {len(nodes)} nodes")


if __name__ == "__main__":
    main()
