"""Bind hand-authored PDF/screenshot labels to independently checked source views.

This compiler never invokes goldmine, retrieval, embedding, ASR, or OCR. It uses
only already-frozen source bytes and manually authored annotations.
"""
from pathlib import Path
import hashlib
import json
import re

HERE = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ref(relative):
    return {"path": relative, "sha256": sha((HERE / relative).read_bytes())}


def load(name):
    return json.loads((HERE / "provenance" / name).read_text())


def question(qid, query, source, view, start, end, tag):
    text = (HERE / view["path"]).read_text()
    assert 0 <= start < end <= len(text)
    location = dict(view["location"])
    if location["unit"] == "line":
        location["start"] = 1 + text[:start].count("\n")
        location["end"] = 1 + text[:end - 1].count("\n")
    piece = text[start:end]
    evidence = {"evidence_id": "e0", "path": source["path"], "kind": source["kind"], "grade": 3,
                "evidence_type": view["evidence_type"], "location": location,
                "span": {"start_char": start, "end_char": end, "text": piece, "sha256": sha(piece.encode())},
                "view_id": view["id"]}
    return {"id": qid, "primary_class": "media-text" if tag == "ocr" else "pdf-page",
            "tags": [tag, "real"], "q": query, "expect": [evidence],
            "required_facts": [{"fact_id": "f0", "description": "The cited source states: " + " ".join(piece.split()),
                                "sufficient_evidence": [["e0"]]}],
            "needs": ["ocr"] if view["evidence_type"] == "ocr_text" else [],
            "source_group": source["source_group"]}


def literal_range(text, quote):
    # Curators supply readable whitespace-collapsed quotes. Preserve the actual
    # source bytes/character offsets; this does not rewrite the source view.
    pattern = r"\s+".join(re.escape(word) for word in quote.split())
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError(f"require one literal source anchor, found {len(matches)}: {quote!r}")
    return matches[0].span()


def main():
    pages = load("pdf-pages.json")
    text_labels = load("pdf_text_questions.json")
    scan_labels = load("pdf_scan_annotations.json")
    shots = load("screenshot_annotations.json")
    sources, queries = [], []
    source_by_id = {}
    for record in pages:
        name = record["id"]
        scanned = record["kind"] == "real_scan_repackage"
        license_name = "historical-patents-public-domain.txt" if scanned else ("NIST-publications.txt" if name.startswith("nist-") else name + "-LICENSE.txt")
        license_title = "Public-domain original historical patent publication; faithful raster digitization" if scanned else ("NIST Technical Series public-service worldwide reprint/derivative permission; attribution retained" if name.startswith("nist-") else {"pytest": "MIT", "sphinx": "BSD-2-Clause", "pillow": "MIT-CMU"}[name])
        views = []
        for i in range(1, len(record["original_page_numbers"]) + 1):
            if scanned and not any(row["document"] == name and row["original_page"] == record["original_page_numbers"][i - 1] for row in scan_labels):
                continue  # Unverified OCR on unlabelled pages is retained as raw acquisition, not gold truth.
            relative = f"views/root/pdf/{name}-p{i:03d}.txt"
            views.append({"id": f"p{i:03d}", **ref(relative),
                          "evidence_type": "ocr_text" if scanned else "content_text",
                          "location": {"unit": "page", "start": i, "end": i},
                          "storage_field": None, "normalization": "nfc-lf-v1"})
        source = {"path": "root/pdf/" + Path(record["asset"]).name,
                  "asset": ref(record["asset"]), "kind": "pdf", "source_group": "upstream:pytest" if name == "pytest" else "pdf:" + name,
                  "parents": [], "provenance": {"type": "real", "origin": {"kind": "url", "url": record["original"]["url"], "revision": "sha256:" + record["original"]["sha256"]},
                  "license": license_title, "license_file": ref("licenses/root/" + license_name),
                  "generation": record["generation"] + " Original SHA256=" + record["original"]["sha256"] + "; asset-to-original page map=" + json.dumps(record["original_page_numbers"]) + (". Republished courtesy of the National Institute of Standards and Technology. Full original attribution in licenses/root/ATTRIBUTION.md." if name.startswith("nist-") else "")}, "views": views}
        sources.append(source)
        source_by_id[name] = source
        if not scanned:
            for k, (original_page, query, quote) in enumerate(text_labels[name], 1):
                view = views[record["original_page_numbers"].index(original_page)]
                text = (HERE / view["path"]).read_text()
                start, end = literal_range(text, quote)
                queries.append(question(f"v4x-pdf-{name}-{k:02d}", query, source, view, start, end, "text_pdf"))
    counts = {}
    for row in scan_labels:
        name = row["document"]
        source = source_by_id[name]
        record = next(r for r in pages if r["id"] == name)
        page = record["original_page_numbers"].index(row["original_page"]) + 1
        view = next(v for v in source["views"] if v["location"]["start"] == page)
        text = (HERE / view["path"]).read_text()
        assert text[row["start_char"]:row["end_char"]] == row["text"]
        counts[name] = counts.get(name, 0) + 1
        queries.append(question(f"v4x-pdf-{name}-{counts[name]:02d}", row["question"], source, view,
                                row["start_char"], row["end_char"], "scanned_pdf"))
    for row in shots:
        text = (HERE / row["view"]).read_text()
        assert text[row["start_char"]:row["end_char"]] == row["text"]
        view = {"id": "ocr", **ref(row["view"]), "evidence_type": "ocr_text",
                "location": {"unit": "line", "start": 1, "end": len(text.splitlines())},
                "storage_field": None, "normalization": "nfc-lf-v1"}
        source = {"path": "root/screenshots/" + Path(row["asset"]).name, "asset": ref(row["asset"]),
                  "kind": "screenshot", "source_group": "upstream:" + row["project"], "parents": [],
                  "provenance": {"type": "real", "origin": {"kind": "url", "url": row["url"], "revision": "capture-sha256:" + ref(row["asset"])["sha256"]},
                                 "license": row["license"], "license_file": ref(row["license_file"]),
                                 "generation": "Unmodified browser viewport screenshot of the publisher's real documentation. Question/truth chosen from the DOM before standalone OCR. DOM capture and source-verification notes retained under provenance/. Source text is not rendered from generated questions. No ranking was run during curation."},
                  "views": [view]}
        sources.append(source)
        queries.append(question("v4x-ocr-" + row["id"], row["question"], source, view,
                                row["start_char"], row["end_char"], "ocr"))
    assert len(sources) == 32 and len(queries) == 80
    (HERE / "shard.json").write_text(json.dumps({"sources": sources, "queries": queries}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"sources": len(sources), "queries": len(queries), "state": "DRAFT_PENDING_INDEPENDENT_AUDIT"}))


if __name__ == "__main__":
    main()
