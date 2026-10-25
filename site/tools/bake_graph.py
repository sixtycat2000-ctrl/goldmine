#!/usr/bin/env python3
"""Bake the concept-map layout v3 (drill-down).

Seven cluster "dig sites" sit on the surface row; each site's L3 nodes fill a
grid beneath its header; every node's children (adjacent-lower-layer edges,
falling back to nearest-lower descendants for skip-generation reachability)
hang in rows below it, one 380px transition per layer crossed.

Deterministic: fixed seeds, stable sort order, no wall-clock input. Reads
site/content/concepts.json (v2 layered schema) and writes
site/content/graph.baked.json with parent/reveal/keystone for the runtime.
"""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "site/content/concepts.json"
DST = ROOT / "site/content/graph.baked.json"

CLUSTER_ORDER = ["search", "models", "media", "identity", "eval", "systems", "process"]
CLUSTER_TOKENS = {"search": "A", "models": "B", "identity": "C", "eval": "D",
                  "media": "E", "systems": "F", "process": "G"}
KEYSTONES = {  # one or two per site, visible before entering
    "search": ["admission-gate", "hybrid-fusion"],
    "models": ["media-sidecar"],
    "media": ["budget-routing"],
    "identity": ["four-layer-identity", "fingerprint-gate"],
    "eval": ["frozen-suite", "quality-gates"],
    "systems": ["watch-serve", "walk-gates"],
    "process": ["honesty-contract", "three-state"],
}
ROOM_W, TILE_Y, GRID_Y0 = 620.0, 96.0, 250.0
GRID_COLS, CELL_W, CELL_H = 3, 176.0, 128.0
TRANSITION = 380.0          # vertical px per layer crossed
SIB_W = 156.0               # horizontal px between siblings in a row
SIB_PER_ROW = 4
MIN_GAP, RELAX_ITER = 118.0, 90


def main():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    nodes_in = data["nodes"]
    layers_meta = data.get("layers", [])
    clusters_meta = {c["id"]: c for c in data["clusters"]}
    by_id = {n["id"]: n for n in nodes_in}

    # ---- reveal sets: adjacent children first, nearest-lower fallback ----
    children = {n["id"]: [] for n in nodes_in}
    for n in nodes_in:
        for e in n.get("dig_deeper", []):
            t = by_id.get(e["to"])
            if t is None or t["id"] == n["id"]:
                continue
            if t["layer"] == n["layer"] - 1:
                children[n["id"]].append(t["id"])
    # each kid joins the eligible inker with the fewest kids so far, so new
    # L3 nodes aren't left childless by first-inker-wins
    node_index = {n["id"]: i for i, n in enumerate(nodes_in)}
    inkers_of = {}
    for pid in sorted(children, key=lambda p: node_index[p]):
        for kid in children[pid]:
            inkers_of.setdefault(kid, []).append(pid)
    parent, load = {}, {}
    for kid, cands in inkers_of.items():
        best = min(cands, key=lambda p: (load.get(p, 0), node_index[p]))
        parent[kid] = best
        load[best] = load.get(best, 0) + 1

    def chain_ok(tid, avoid, cap=8):
        """True if walking parent pointers from tid terminates without a cycle
        and never touches avoid."""
        cur, seen, steps = tid, set(), 0
        while cur is not None:
            if cur == avoid or cur in seen or steps > cap:
                return False
            seen.add(cur)
            cur = parent.get(cur)
            steps += 1
        return True

    # skip-generation pass: parentless nodes anchor to their nearest
    # higher-layer inker (who digs to them), descending layer, only when the
    # candidate's chain terminates — keeps the parent graph a forest at L3
    inkers = {}
    for n in nodes_in:
        for e in n.get("dig_deeper", []):
            t = by_id.get(e["to"])
            if t and t["id"] != n["id"] and t["layer"] < n["layer"]:
                inkers.setdefault(t["id"], []).append(n)
    parentless = [n for n in nodes_in if n["id"] not in parent and n["layer"] < 3]
    for n in sorted(parentless, key=lambda x: (-x["layer"], x["id"])):
        cands = [p for p in inkers.get(n["id"], []) if chain_ok(p["id"], n["id"])]
        cands.sort(key=lambda p: (p["layer"], p["id"]))  # nearest layer above first
        if cands:
            parent[n["id"]] = cands[0]["id"]

    # final sweep: anything still unparented (or cyclic) anchors to its
    # cluster's first keystone L3 — guarantees every node is drill-reachable
    ks_of = {}
    for cid, ks in KEYSTONES.items():
        ks_of[cid] = [k for k in ks if k in by_id] or \
                     [m["id"] for m in sorted(nodes_in, key=lambda x: x["id"])
                      if m["cluster"] == cid and m["layer"] == 3]
    for n in nodes_in:
        if n["layer"] == 3:
            continue
        p = parent.get(n["id"])
        if p is None or not chain_ok(p, n["id"]):
            parent[n["id"]] = ks_of[n["cluster"]][0]
    tree_children = {}
    for kid, pid in parent.items():
        tree_children.setdefault(pid, []).append(kid)

    # ---- positions -------------------------------------------------------
    positions = {}
    # cluster tiles
    tile_x = {}
    for i, cid in enumerate(CLUSTER_ORDER):
        tile_x[cid] = 140 + ROOM_W * i + ROOM_W / 2
        positions["tile:" + cid] = (tile_x[cid], TILE_Y)
    # L3 room grids
    room_nodes = {}
    for cid in CLUSTER_ORDER:
        members = [n for n in nodes_in if n["cluster"] == cid and n["layer"] == 3]
        members.sort(key=lambda n: n["id"])
        room_nodes[cid] = members
        for j, n in enumerate(members):
            col, row = j % GRID_COLS, j // GRID_COLS
            x = tile_x[cid] - (GRID_COLS * CELL_W) / 2 + CELL_W * (col + 0.5)
            y = GRID_Y0 + CELL_H * row + CELL_H / 2
            positions[n["id"]] = (x, y)

    # child rows, depth-first down each parent
    for pid in sorted(tree_children, key=lambda p: (-by_id[p]["layer"], p)):
        kids = sorted(tree_children[pid], key=lambda k: (by_id[k]["layer"], k))
        px, py = positions[pid]
        for j, kid in enumerate(kids):
            row, col = j // SIB_PER_ROW, j % SIB_PER_ROW
            span = min(len(kids) - row * SIB_PER_ROW, SIB_PER_ROW)
            x = px + (col - (span - 1) / 2) * SIB_W
            y = py + TRANSITION * (by_id[pid]["layer"] - by_id[kid]["layer"]) + row * CELL_H
            positions[kid] = (x, y)

    # ---- overlap relaxation within same layer ----------------------------
    ids = [n["id"] for n in nodes_in]
    layer_of = {n["id"]: n["layer"] for n in nodes_in}
    for _ in range(RELAX_ITER):
        moved = 0
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                ia, ib = ids[a], ids[b]
                if layer_of[ia] != layer_of[ib]:
                    continue
                ax, ay = positions[ia]
                bx, by = positions[ib]
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

    # ---- world bounds ----------------------------------------------------
    max_layer_by_room = {}
    for n in nodes_in:
        cid = n["cluster"]
        depth_y = positions[n["id"]][1]
        max_layer_by_room[cid] = max(max_layer_by_room.get(cid, 0), depth_y)
    WORLD_W = 140 + ROOM_W * len(CLUSTER_ORDER) + 140
    WORLD_H = max(max_layer_by_room.values()) + 170
    for nid, (x, y) in positions.items():
        positions[nid] = (max(80, min(WORLD_W - 80, x)), max(40, min(WORLD_H - 60, y)))

    def ref_of(r):
        if isinstance(r, dict):
            return {"path": r.get("path", ""), "lines": r.get("lines", ""),
                    "note": r.get("note_zh") or r.get("note") or ""}
        path, _, note = r.partition("—")
        return {"path": path.strip(), "lines": "", "note": note.strip()}

    keystone_flat = {k for ks in KEYSTONES.values() for k in ks}
    nodes_out = []
    for n in nodes_in:
        x, y = positions[n["id"]]
        kids = tree_children.get(n["id"], [])
        nodes_out.append({
            "id": n["id"], "cluster": n["cluster"], "layer": n["layer"],
            "keystone": n["id"] in keystone_flat,
            "parent": parent.get(n["id"]),
            "reveal": kids,
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
        "meta": {
            "version": 3, "world": [round(WORLD_W, 1), round(WORLD_H, 1)],
            "room_w": ROOM_W, "tile_y": TILE_Y,
            "layers": [{"n": l["n"], "label_zh": l["label_zh"], "label_en": l["label_en"],
                        "blurb_zh": l.get("blurb_zh", ""), "blurb_en": l.get("blurb_en", "")}
                       for l in sorted(layers_meta, key=lambda x: -x["n"])],
            "clusters": [{"id": cid, "token": CLUSTER_TOKENS[cid],
                          "label_zh": clusters_meta[cid]["label_zh"],
                          "label_en": clusters_meta[cid]["label_en"],
                          "blurb_zh": clusters_meta[cid].get("blurb_zh", ""),
                          "blurb_en": clusters_meta[cid].get("blurb_en", "")}
                         for cid in CLUSTER_ORDER if cid in clusters_meta],
        },
        "nodes": nodes_out,
    }
    DST.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    have = {n["id"] for n in nodes_out}
    bad = [d["to"] for n in nodes_out for d in n["deep"] if d["to"] not in have]
    orphan = [n["id"] for n in nodes_out if n["layer"] < 3 and not n["parent"]]
    reach = set()
    stack = [n["id"] for n in nodes_out if n["layer"] == 3]
    while stack:
        cur = stack.pop()
        if cur in reach:
            continue
        reach.add(cur)
        nxt = by_id[cur]
        stack.extend(tree_children.get(cur, []))
        stack.extend(d["to"] for d in nxt.get("dig_deeper", []) if d["to"] in by_id)
    unreachable = [n["id"] for n in nodes_out if n["id"] not in reach]
    per_layer = {}
    for n in nodes_out:
        per_layer[n["layer"]] = per_layer.get(n["layer"], 0) + 1
    print(f"baked {len(nodes_out)} nodes per-layer {sorted(per_layer.items(), reverse=True)}; "
          f"world {WORLD_W:.0f}x{WORLD_H:.0f}; bad targets: {bad or 'none'}; "
          f"orphan lower nodes: {orphan or 'none'}; unreachable: {unreachable or 'none'}")


if __name__ == "__main__":
    main()
