#!/usr/bin/env python3
"""Bake the concept-map layout v2 (layered): four horizontal depth bands
(L3 goldmine 机制 at top → L0 基础概念 at bottom), nodes placed by cluster
column within their band, one-time overlap relaxation. Deterministic seed.
Reads site/content/concepts.json (v2 layered schema) and writes
site/content/graph.baked.json for the map runtime."""
import json, math, random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "site/content/concepts.json"
DST = ROOT / "site/content/graph.baked.json"

WORLD_W, WORLD_H = 1680.0, 1320.0
BANDS = {  # layer -> (y_top, y_bottom)
    3: (70, 400),
    2: (450, 700),
    1: (750, 1000),
    0: (1050, 1250),
}
CLUSTER_ORDER = ["search", "models", "media", "identity", "eval", "systems", "process"]
CLUSTER_TOKENS = {"search": "A", "models": "B", "identity": "C", "eval": "D",
                  "media": "E", "systems": "F", "process": "G"}
MIN_GAP = 30.0
RELAX_ITER = 40

def main():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    rng = random.Random(20261025)
    nodes_in = data["nodes"]
    clusters_meta = {c["id"]: c for c in data["clusters"]}
    layers_meta = data.get("layers", [])

    # cluster columns across the width
    col_x = {}
    for i, cid in enumerate(CLUSTER_ORDER):
        col_x[cid] = 140 + (WORLD_W - 280) * i / (len(CLUSTER_ORDER) - 1)

    positions = {}
    by_band = {}
    for n in nodes_in:
        by_band.setdefault(n["layer"], []).append(n)
    for layer, members in by_band.items():
        top, bot = BANDS[layer]
        # even x-spread across the whole band (density balance); cluster
        # identity stays in the node COLOR, not the column
        rows = 2 if len(members) <= 18 else 3
        ordered = sorted(members, key=lambda n: (CLUSTER_ORDER.index(n["cluster"]), n["id"]))
        # interleave rows so neighbors alternate vertically
        for j, n in enumerate(ordered):
            row = j % rows
            frac = (j + 0.5) / len(ordered)
            x = 130 + (WORLD_W - 260) * frac + rng.uniform(-16, 16)
            rowFrac = (row + 0.5) / rows
            y = top + (bot - top) * rowFrac + rng.uniform(-16, 16)
            positions[n["id"]] = (x, y)

    # overlap relaxation within the same band (chebyshev-ish euclidean push)
    ids = [n["id"] for n in nodes_in]
    layer_of = {n["id"]: n["layer"] for n in nodes_in}
    for _ in range(RELAX_ITER):
        moved = 0
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                ia, ib = ids[a], ids[b]
                if layer_of[ia] != layer_of[ib]:
                    continue
                ax, ay = positions[ia]; bx, by = positions[ib]
                dx, dy = bx - ax, by - ay
                d = math.hypot(dx, dy)
                if d < MIN_GAP and d > 0.001:
                    push = (MIN_GAP - d) / 2
                    ux, uy = dx / d, dy / d
                    positions[ia] = (ax - ux * push, ay - uy * push)
                    positions[ib] = (bx + ux * push, by + uy * push)
                    moved += 1
        if not moved:
            break

    # clamp into band + world margins
    for nid, (x, y) in positions.items():
        top, bot = BANDS[layer_of[nid]]
        positions[nid] = (max(70, min(WORLD_W - 70, x)),
                          max(top + 22, min(bot - 22, y)))

    def ref_of(r):
        if isinstance(r, dict):
            return {"path": r.get("path", ""), "lines": r.get("lines", ""),
                    "note": r.get("note_zh") or r.get("note") or ""}
        path, _, note = r.partition("—")
        return {"path": path.strip(), "lines": "", "note": note.strip()}

    nodes_out = []
    for n in nodes_in:
        x, y = positions[n["id"]]
        nodes_out.append({
            "id": n["id"], "cluster": n["cluster"], "layer": n["layer"],
            "keystone": False,
            "title_zh": n["term_zh"], "title_en": n["term_en"],
            "one_zh": n.get("one_liner_zh", ""), "one_en": n.get("one_liner_en", ""),
            "def_zh": n.get("professional_zh", ""), "def_en": n.get("professional_en", ""),
            "plain_zh": n.get("plain_zh", ""), "plain_en": n.get("plain_en", ""),
            "example_zh": n.get("example_zh", ""), "example_en": n.get("example_en", ""),
            "analogy_zh": n.get("analogy_zh", ""), "analogy_en": n.get("analogy_en", ""),
            "refs": [ref_of(r) for r in n.get("code_refs", [])],
            "deep": [{"to": d["to"], "rel_zh": d.get("why_zh", ""), "rel_en": d.get("why_en", "")}
                     for d in n.get("dig_deeper", [])],
            "x": round(x, 1), "y": round(y, 1),
        })

    out = {
        "meta": {"version": 2, "world": [WORLD_W, WORLD_H],
                 "bands": [{"layer": l["n"], "y0": BANDS[l["n"]][0], "y1": BANDS[l["n"]][1],
                            "label_zh": l["label_zh"], "label_en": l["label_en"]}
                           for l in sorted(layers_meta, key=lambda x: -x["n"])],
                 "clusters": [{"id": cid, "token": CLUSTER_TOKENS[cid],
                               "label_zh": clusters_meta[cid]["label_zh"],
                               "label_en": clusters_meta[cid]["label_en"]}
                              for cid in CLUSTER_ORDER if cid in clusters_meta]},
        "nodes": nodes_out,
    }
    DST.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    have = {n["id"] for n in nodes_out}
    bad = [d["to"] for n in nodes_out for d in n["deep"] if d["to"] not in have]
    per_layer = {}
    for n in nodes_out:
        per_layer[n["layer"]] = per_layer.get(n["layer"], 0) + 1
    print(f"baked {len(nodes_out)} nodes per-layer {sorted(per_layer.items(), reverse=True)}, "
          f"{sum(len(n['deep']) for n in nodes_out)} edges; bad targets: {bad or 'none'}")

if __name__ == "__main__":
    main()
