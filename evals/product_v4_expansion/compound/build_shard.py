"""Compile source-first, manually annotated cross-file compound questions."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import unicodedata


ROOT = Path(__file__).resolve().parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def normalized(text):
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def reference(path):
    return {"path": path, "sha256": sha((ROOT / path).read_bytes())}


def compile_shard():
    acquisition = json.loads((ROOT / "acquisition.json").read_text(encoding="utf-8"))
    annotations = json.loads((ROOT / "annotations.json").read_text(encoding="utf-8"))
    sources, queries = [], []
    for repo in acquisition["repos"]:
        if repo["id"] not in annotations:
            continue
        group = "upstream:" + repo["id"]
        paths = [f"compound/{repo['id']}/{item['original_path']}" for item in repo["sources"]]
        pair = []
        for index, item in enumerate(repo["sources"]):
            raw = (ROOT / item["asset"]).read_bytes()
            assert sha(raw) == item["sha256"]
            text = normalized(raw.decode("utf-8"))
            view_path = f"views/compound/{repo['id']}/{item['original_path']}.txt"
            out = ROOT / view_path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            source = {
                "path": paths[index], "asset": reference(item["asset"]),
                "kind": "code" if Path(item["original_path"]).suffix == ".py" else "doc",
                "source_group": group, "parents": [p for p in paths if p != paths[index]],
                "provenance": {
                    "type": "real", "origin": {"kind": "git", "repository": repo["repository"],
                        "revision": repo["revision"], "path": item["original_path"]},
                    "license": "BSD-3-Clause" if repo["id"] in ("httpx", "pip-tools", "flask") else "MIT",
                    "license_file": reference(repo["license"]["asset"]),
                    "generation": "No generated source. Complete original upstream file bytes preserved at the pinned commit; "
                                  "independent oracle view applies NFC/LF only. Questions and fact spans were authored after "
                                  "reading the actual paired source text, without models, retrieval, or success-based selection.",
                },
                "views": [{"id": "original-lines", **reference(view_path), "evidence_type": "content_text",
                    "location": {"unit": "line", "start": 1, "end": len(text.splitlines())},
                    "storage_field": None, "normalization": "nfc-lf-v1"}],
            }
            sources.append(source)
            pair.append((source, text))
        for number, annotation in enumerate(annotations[repo["id"]], 1):
            assert len(annotation["facts"]) == 2 and {f["source"] for f in annotation["facts"]} == {0, 1}
            expected, facts = [], []
            for fact_number, fact in enumerate(annotation["facts"], 1):
                source, text = pair[fact["source"]]
                span = normalized(fact["span"])
                assert text.count(span) == 1, (repo["id"], number, fact_number, "span missing/ambiguous", span)
                start = text.index(span)
                expected.append({
                    "evidence_id": f"e{fact_number}", "path": source["path"], "kind": source["kind"], "grade": 3,
                    "evidence_type": "content_text", "view_id": "original-lines",
                    "location": {"unit": "line", "start": 1 + text[:start].count("\n"),
                                 "end": 1 + text[:start + len(span) - 1].count("\n")},
                    "span": {"start_char": start, "end_char": start + len(span), "text": span,
                             "sha256": sha(span.encode("utf-8"))},
                })
                facts.append({"fact_id": f"f{fact_number}", "description": fact["description"],
                              "sufficient_evidence": [[f"e{fact_number}"]]})
            queries.append({
                "id": f"compound-real-{repo['id']}-{number:02d}", "primary_class": "compound",
                "tags": ["real", "cross_file"], "q": annotation["q"], "expect": expected,
                "required_facts": facts, "needs": [], "source_group": group,
            })
    shard = {"sources": sources, "queries": queries}
    (ROOT / "shard.json").write_text(json.dumps(shard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    usage = Counter(e["path"] for q in queries for e in q["expect"])
    assert not usage or max(usage.values()) <= 8
    print(f"Compiled {len(queries)} questions, {len(sources)} sources, {len({s['source_group'] for s in sources})} groups.")


if __name__ == "__main__":
    compile_shard()
