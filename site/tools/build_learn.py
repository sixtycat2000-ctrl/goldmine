#!/usr/bin/env python3
"""Build site/learn.html: inline the baked graph JSON and render the
no-JS static glossary fallback (all nodes, three layers, refs)."""
import html, json
from pathlib import Path

SITE = Path(__file__).resolve().parents[1]
graph = json.loads((SITE / "content/graph.baked.json").read_text(encoding="utf-8"))
tpl = (SITE / "learn.template.html").read_text(encoding="utf-8")

def esc(s):
    return html.escape(str(s or ""), quote=True)

# static fallback: groups by LAYER (v3), deepest last
layers_meta = {b["n"]: b for b in graph["meta"].get("layers", [])}
groups_html = []
for lay in sorted(layers_meta, reverse=True):
    lm = layers_meta[lay]
    members = [n for n in graph["nodes"] if n["layer"] == lay]
    cl = {"token": "L%d" % lay, "label_zh": lm["label_zh"], "label_en": lm["label_en"]}
    cards = []
    for n in members:
        cards.append(
            '<div class="fcard"><h4>{zh}</h4><p class="en">{en}</p>'
            '<p><span class="lbl">定义</span> {d}</p>'
            '<p><span class="lbl">大白话</span> {p}</p>'
            '<p><span class="lbl">实例</span> {e}</p>'
            '{refs}</div>'.format(
                zh=esc(n["title_zh"]), en=esc(n["title_en"]),
                d=esc(n.get("def_zh") or "—"), p=esc(n.get("plain_zh") or "—"),
                e=esc(n.get("example_zh") or "—"),
                refs=("<ul class='refs'>" + "".join(
                    "<li><code>%s</code></li>" % esc(r["path"]) for r in n.get("refs", [])[:2]
                ) + "</ul>") if n.get("refs") else "",
            ))
    groups_html.append(
        '<details class="fgroup" open><summary>{lbl}</summary>{cards}</details>'.format(
            lbl=esc(cl["token"] + " · " + cl["label_zh"] + " / " + cl["label_en"]),
            cards="".join(cards)))

fallback = ('<div class="banner" hidden style="font:12px var(--mono);color:var(--gold);'
            'border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-bottom:16px">'
            '地图数据损坏 — 已降级为目录视图 / map data invalid — list view</div>'
            + "".join(groups_html))

graph_json = json.dumps(graph, ensure_ascii=False, separators=(",", ":"))
out = tpl.replace("{{GRAPH_JSON}}", graph_json)
out = out.replace('<div class="fallback" hidden></div>',
                  '<div class="fallback" data-fb>' + fallback + "</div>")
(SITE / "learn.html").write_text(out, encoding="utf-8")
kb = (len(out) + len(graph_json)) / 1024
print(f"learn.html built: {len(graph['nodes'])} nodes inlined, "
      f"{len(out)/1024:.0f} KB total")
