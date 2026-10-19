"""Synthetic BEIR-shaped mini fixture for the R5 public-suite tests.

ZERO real BEIR bytes (nfcorpus/arguana carry NC licenses — manifest licenses_note,
review F4): the corpus text below is original filler prose, built in-test via
zipfile into tmp dirs. The SHAPE mirrors the canonical BEIR zips:

  <name>/corpus.jsonl    {"_id","title","text"} rows
  <name>/queries.jsonl   {"_id","text"} rows (a SUPERSET of the test split)
  <name>/qrels/test.tsv  qid \t 0 \t docid \t grade

Quirks exercised (spec §9 + review F1/F5):
  - `self-doc-1`: query id == corpus id, text byte-identical (the arguana
    counter-argument self-inclusion; its qrel positive is a DIFFERENT doc)
  - `doc-beta`: EMPTY title (69% of arguana — the .md route skips the header)
  - `q-dangling` → `missing-doc-x`: qrel positive absent from the corpus
  - `weird/id 1`: docid needing percent-escape in the .md filename
  - `q1` (doc) vs `q1`/`q10` (queries): qid-docid namespace safety
  - `q-graded`: graded qrels (grade 2 + grade 1) on the graded variant
  - `q-noqrel`: queries.jsonl row with NO qrel row (filtered out, D9a)

The graded variant (default) carries a grade-2 qrel; the binary variant is
all grade 1 (scifact/arguana-like). `stats()` recomputes the counts the
manifest-schema assertions rely on.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

FIXTURE_NAME = "minicorp"

#: (docid, title, text) — original filler prose, no BEIR bytes.
DOCS: list[tuple[str, str, str]] = [
    ("doc-alpha",
     "Alpha Ferromagnetic Core Memory Arrays",
     "Core memory stored bits in magnetic rings. Each ferrite ring held one "
     "bit by the direction of its magnetization. Wiring threads passed through "
     "every ring so the address lines could flip the polarity during a write."),
    ("doc-beta",
     "",
     "Sourdough fermentation depends on a symbiotic culture of wild yeast and "
     "lactic acid bacteria. The starter doubles in volume during bulk ferment, "
     "and the acid profile shapes both flavor and the final crumb structure."),
    ("doc-gamma",
     "Gamma Ridge Regression Notes",
     "Ridge regression shrinks coefficient estimates by penalizing their "
     "squared size. The penalty trades a little bias for a large variance "
     "reduction, which often lowers prediction error on collinear features."),
    ("self-doc-1",
     "Self Document One",
     "The vacuum tube calculator consumed kilowatts and failed weekly. "
     "Reliability engineering began when maintenance logs became data and "
     "failure rates were modeled instead of merely suffered."),
    ("weird/id 1",
     "Escaped Identifier Document",
     "Percussion welding joins dissimilar metals by a sudden arc discharge. "
     "The energy budget is measured in microseconds, which limits the heat "
     "affected zone to a fraction of a millimetre."),
    ("grade-two-doc",
     "Grade Two Document On Adaptive Filters",
     "Adaptive equalizers trained on a pilot sequence converge faster than "
     "blind schemes. The least mean squares update is cheap per sample but "
     "converges slowly when the eigenvalue spread of the input is wide."),
    ("doc-echo",
     "Echo Delta Echo Counterpoint",
     "Counterpoint rules forbid parallel fifths because they collapse the "
     "independence of the two voices. Species counterpoint exercises add one "
     "voice at a time against a cantus firmus."),
    ("q1",
     "Queue One Doc On Mail Queues",
     "A mail queue retries deferred deliveries with exponential backoff. "
     "Bounces are classified by status codes so that permanent failures leave "
     "the queue immediately while transient ones age out slowly."),
]

#: queries.jsonl rows (id, text). q-noqrel has NO qrel row (D9a filter).
QUERIES: list[tuple[str, str]] = [
    ("self-doc-1",
     "The vacuum tube calculator consumed kilowatts and failed weekly. "
     "Reliability engineering began when maintenance logs became data and "
     "failure rates were modeled instead of merely suffered."),
    ("q-plain",
     "How does a sourdough starter ferment and shape crumb structure?"),
    ("q-graded",
     "Why do adaptive equalizers converge slowly with wide eigenvalue spread?"),
    ("q-dangling",
     "Nobody could retrieve the lost manuscript of numismatics."),
    ("q-escape",
     "Describe percussion welding of dissimilar metals."),
    ("q1",
     "Mail queue backoff and bounce classification"),
    ("q10",
     "Parallel fifths collapse voice independence in counterpoint."),
    ("q-noqrel",
     "A query that has no qrels row at all and must be filtered."),
]

#: qid -> {docid: grade} — the graded variant (nfcorpus-like).
QRELS_GRADED: dict[str, dict[str, int]] = {
    "self-doc-1": {"doc-echo": 1},
    "q-plain": {"doc-beta": 1},
    "q-graded": {"grade-two-doc": 2, "doc-gamma": 1},
    "q-dangling": {"missing-doc-x": 1},  # dangling: not in the corpus (D9c)
    "q-escape": {"weird/id 1": 1},
    "q1": {"doc-alpha": 1},
    "q10": {"doc-echo": 1},
}

#: the binary variant (scifact/arguana-like): every grade flattened to 1.
QRELS_BINARY: dict[str, dict[str, int]] = {
    qid: {d: 1 for d in grades} for qid, grades in QRELS_GRADED.items()
}


def _corpus_jsonl() -> bytes:
    lines = [json.dumps({"_id": d, "title": t, "text": x}, ensure_ascii=False)
             for d, t, x in DOCS]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _queries_jsonl() -> bytes:
    lines = [json.dumps({"_id": q, "text": x}, ensure_ascii=False)
             for q, x in QUERIES]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _qrels_tsv(qrels: dict[str, dict[str, int]]) -> bytes:
    rows = [f"{qid}\t0\t{docid}\t{grade}"
            for qid, grades in sorted(qrels.items())
            for docid, grade in sorted(grades.items())]
    return ("\n".join(rows) + "\n").encode("utf-8")


def write_tree(dest_dir: Path, *, name: str = FIXTURE_NAME,
               graded: bool = True) -> Path:
    """Write the unzipped BEIR-shaped tree <dest_dir>/<name>/{...}; returns it."""
    tree = dest_dir / name
    (tree / "qrels").mkdir(parents=True, exist_ok=True)
    (tree / "corpus.jsonl").write_bytes(_corpus_jsonl())
    (tree / "queries.jsonl").write_bytes(_queries_jsonl())
    (tree / "qrels" / "test.tsv").write_bytes(
        _qrels_tsv(QRELS_GRADED if graded else QRELS_BINARY))
    return tree


def build_mini_zip(dest_zip: Path, *, name: str = FIXTURE_NAME,
                   graded: bool = True) -> Path:
    """Zip the BEIR-shaped tree exactly like the canonical distributions
    (one top-level <name>/ folder). Deterministic-enough for tests."""
    import tempfile

    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tree = write_tree(Path(td), name=name, graded=graded)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(tree.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(tree.parent).as_posix())
    return dest_zip


def stats(graded: bool = True) -> dict:
    """The counts a manifest-schema assertion relies on (recomputed, never
    quoted from memory)."""
    qrels = QRELS_GRADED if graded else QRELS_BINARY
    grades = sorted({g for gr in qrels.values() for g in gr.values()})
    return {
        "n_docs": len(DOCS),
        "n_queries_jsonl": len(QUERIES),
        "n_queries_test": len(qrels),
        "n_qrels_rows": sum(len(g) for g in qrels.values()),
        "grade_range": [min(grades), max(grades)],
        "dangling": {"missing-doc-x"},
        "empty_title_docs": {d for d, t, _ in DOCS if not t.strip()},
    }
