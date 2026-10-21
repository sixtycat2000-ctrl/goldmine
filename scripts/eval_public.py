#!/usr/bin/env python3
"""R5 public benchmark mini-suite (BEIR mini) — the "someone else's exam".

Downloads, ingests and scores three small BEIR test splits (scifact, nfcorpus,
arguana) through the UNTOUCHED production stack (IngestPipeline .md route +
Searcher), and prints the neutral comparison table: ours-BM25 vs ours-hybrid
vs published BM25 references. Spec: docs/v2.1-r5-public.md (§0b review
amendments F1-F9 absorbed).

Discipline copied, not coupled:
  downloader  = goldmine/model.py:357-445 verbatim shape (.partial + HTTP Range
                resume, 200-restart, short-read retry, 2/5/15/30 s backoff,
                404/410 fail-fast, size+sha256 verify, atomic os.replace, disk
                precheck) — ModelFile/ModelManager are HF-shaped (D4).
  scorer      = scripts/eval_real.py machinery reused unchanged (summarize /
                per_query_maps / headline_cis / compare_to_baseline); the nDCG
                block is the formula VERIFIED trec_eval-identical (D7).

Adapter rules (D8/D9 + review F1/F2/F5):
  - queries = queries.jsonl ∩ qrels/test.tsv qids
  - fetch-deep-then-cut: retrieve limit=20 → drop hits whose docid == qid →
    cut to 10 → score (the arguana self-doc is byte-identical to its query =
    near-certain rank-1 on both legs; a cut-then-drop would bias 69% of the
    suite). Searcher fusion is file-keyed, so hit.path→docid is 1:1 and the
    drop removes at most one hit — the adapter asserts that invariant.
  - success@5/success@1/MRR threshold = the dataset's max qrel grade
  - dangling qrels stay in IDCG (trec_eval parity)
  - empty-title docs skip the '# ' header line (title slot stays the slug)

Usage:
  python3 scripts/eval_public.py --download                 # fetch+verify, build corpora+indexes (mock)
  python3 scripts/eval_public.py --backend llama            # real numbers: embed + search, both arms, all datasets
  python3 scripts/eval_public.py --dataset scifact --arm lexical
  python3 scripts/eval_public.py --offline --table          # cache-only
  python3 scripts/eval_public.py --update-baseline --yes --backend llama
  python3 scripts/eval_public.py --gate --backend llama     # advisory v1 (never a release gate)

stdout stays data-only (table / --json); all progress goes to stderr.
Dataset bytes, derived corpora and indexes live ONLY under the cache root
(NEVER committed — nfcorpus/arguana carry NC licenses, review F4).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))  # eval_real (the house scorer)

# The formal exporter selects an isolated production checkout BEFORE importing
# goldmine. The historical aggregate/advisory interface below stays compatible.
if __name__ == "__main__" and "--raw" in sys.argv[1:]:
    from public_eval.runner import main as raw_main

    raise SystemExit(raw_main(sys.argv[1:]))

from goldmine.backends import create_backend  # noqa: E402
from goldmine.backends.base import PROMPT_SCHEME  # noqa: E402
from goldmine.chunker import CHUNKER_VERSION  # noqa: E402
from goldmine.config import Config, load  # noqa: E402
from goldmine.ingest import IngestPipeline  # noqa: E402
from goldmine.model import (  # noqa: E402  (download discipline reuse, D4)
    CHUNK_BYTES,
    DISK_SLACK_BYTES,
    RETRY_BACKOFF_S,
    sha256_and_size,
)
from goldmine.search import Searcher  # noqa: E402
from goldmine.store import Store  # noqa: E402

import eval_real as er  # noqa: E402  (house scorer machinery, reused unchanged)

EVALS_DIR = REPO_ROOT / "evals"
PUBLIC_MANIFEST_JSON = EVALS_DIR / "public_manifest.json"
PUBLIC_BASELINE_JSON = EVALS_DIR / "public_baseline.json"

#: retrieval is DEEP (F1): the self-doc drop happens before the cut to 10
FETCH_LIMIT = 20
TOP_K = 10

#: the .md corpus-dir subdir name (D5/D6 cache layout)
CORPUS_MD_DIR = "corpus_md"
ZIP_DIR = "beir"  # under the data root: <root>/beir/<name>.zip, <root>/beir/<name>/

DOCID_SAFE_RE = r"[A-Za-z0-9_-]{1,240}"


class PublicError(RuntimeError):
    """A public-suite failure with a fix hint. `exit_code` 2 = caller error
    (cache/identity/usage), 1 is reserved for a tripped gate."""

    def __init__(self, message: str, fix_hint: str | None = None):
        super().__init__(message)
        self.fix_hint = fix_hint


def _err(message: str) -> None:
    print(message, file=sys.stderr)


# ------------------------------------------------------------------ loading

def load_manifest(path: Path = PUBLIC_MANIFEST_JSON) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or manifest.get("suite") != "beir-mini":
        raise PublicError(f"{path}: not a beir-mini public_manifest (schema {manifest.get('schema')})")
    for name, entry in manifest["datasets"].items():
        for field in ("url", "bytes", "sha256", "license", "n_docs",
                      "n_queries_test", "grade_range", "published_bm25"):
            if field not in entry:
                raise PublicError(f"{path}: dataset {name!r} lacks {field!r}")
    return manifest


def data_root() -> Path:
    """Cache root for zips + derived corpora/indexes (D4/GOLDMINE_PUBLIC_DATA).
    NEVER under the repo — nfcorpus/arguana are NC-licensed (F4)."""
    env = os.environ.get("GOLDMINE_PUBLIC_DATA")
    if env:
        root = Path(env).expanduser()
    else:
        root = resolve_root_default()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_root_default() -> Path:
    from goldmine.config import resolve_home

    return resolve_home(load()) / "datasets"


def dataset_dir(root: Path, name: str) -> Path:
    return root / ZIP_DIR / name


def zip_path(root: Path, name: str) -> Path:
    return root / ZIP_DIR / f"{name}.zip"


# ------------------------------------------------------- BEIR file parsing

def _read_jsonl(path: Path) -> list[dict]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise PublicError(f"{path}:{n}: bad JSON ({exc})") from exc
    return out


def load_corpus(unzipped: Path) -> list[dict]:
    """BEIR corpus.jsonl rows ({_id,title,text}), sorted by _id (determinism, §4)."""
    path = _find_one(unzipped, "corpus.jsonl")
    rows = _read_jsonl(path)
    for n, r in enumerate(rows, 1):
        if not r.get("_id") or not isinstance(r.get("text"), str):
            raise PublicError(f"{path}:{n}: row lacks _id/text")
    return sorted(rows, key=lambda r: r["_id"])


def load_queries(unzipped: Path) -> dict[str, str]:
    path = _find_one(unzipped, "queries.jsonl")
    return {r["_id"]: r["text"] for r in _read_jsonl(path) if r.get("_id")}


def load_qrels(unzipped: Path) -> dict[str, dict[str, int]]:
    """qrels/test.tsv → {qid: {docid: grade}}. Header-tolerant (BEIR ships a
    `query-id\\tcorpus-id\\tscore` 3-column header — LIVE-RUN CAUGHT: the raise
    used to precede the header check); dangling docids STAY (D9c). Both data
    shapes are accepted: 4-col (qid, 0, docid, grade) and 3-col."""
    path = _find_one(unzipped, "test.tsv", sub="qrels")
    qrels: dict[str, dict[str, int]] = {}
    first = True
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) not in (3, 4):
            raise PublicError(f"{path}:{n}: expected 3-4 TSV fields, got {len(parts)}")
        try:
            g = int(parts[-1])
        except ValueError:
            if first:  # header row (query-id/corpus-id/score)
                continue
            raise PublicError(f"{path}:{n}: non-integer qrel grade "
                              f"{parts[-1]!r}") from None
        finally:
            first = False
        if len(parts) == 4:
            qid, docid = parts[0], parts[2]  # qid \t 0 \t docid \t grade
        else:
            qid, docid = parts[0], parts[1]
        qrels.setdefault(qid, {})[docid] = g
    return qrels


def _find_one(unzipped: Path, filename: str, sub: str | None = None) -> Path:
    """Locate a BEIR file inside the unzipped tree (layout may be nested)."""
    rel = f"{sub}/{filename}" if sub else filename
    exact = unzipped / rel
    if exact.is_file():
        return exact
    hits = [p for p in sorted(unzipped.rglob(filename)) if p.is_file()]
    if len(hits) == 1:
        return hits[0]
    raise PublicError(
        f"{unzipped}: cannot locate {rel} ({len(hits)} candidates) — "
        "the zip does not match the BEIR distribution shape")


# --------------------------------------------------------------- downloader
# model.py:357-445 discipline verbatim-shape; ModelManager is HF-shaped and
# writes a models registry, so the ~80 lines are COPIED, not subclassed (D4).

class Fetcher:
    """Pinned-zip fetcher: .partial + Range resume + size+sha verify + atomic
    rename. Injected open_url/sleep seams, same as ModelManager (model.py:123)."""

    def __init__(self, root: Path, *, err=_err, sleep=time.sleep,
                 open_url=urllib.request.urlopen):
        self.root = root
        self.err = err
        self._sleep = sleep
        self._open_url = open_url

    def ensure_zip(self, name: str, entry: dict, *, allow_download: bool,
                   force_refresh: bool) -> Path:
        """Verified zip path. Cache is invalidated BY sha, never patched
        silently (§3.2): a mismatched zip is a loud error unless an explicit
        --download re-fetches it."""
        dest = zip_path(self.root, name)
        if dest.is_file():
            sha, size = sha256_and_size(dest)
            if size == entry["bytes"] and sha == entry["sha256"]:
                return dest
            if not (force_refresh and allow_download):
                raise PublicError(
                    f"{name}: cached zip {dest} does not match the pin "
                    f"(bytes {size} != {entry['bytes']} or sha256 mismatch)",
                    "re-run with --download to re-fetch deliberately "
                    "(the pin is the contract; the cache is never patched)",
                )
            self.err(f"[download] {name}: cached zip mismatched the pin — re-fetching")
        if not allow_download:
            raise PublicError(
                f"{name}: no verified zip in the cache ({dest})",
                "run python3 scripts/eval_public.py --download (drop --offline)",
            )
        self._disk_precheck(name, entry)
        partial = dest.with_name(dest.name + ".partial")
        self._download(name, entry, dest, partial)
        self.err(f"[verifying] {name}: size+sha256 against the pin")
        sha, size = sha256_and_size(partial)
        if size != entry["bytes"] or sha != entry["sha256"]:
            detail = (f"downloaded {size} bytes (expected {entry['bytes']})"
                      if size != entry["bytes"]
                      else f"sha256 {sha} != pinned {entry['sha256']}")
            partial.unlink(missing_ok=True)
            raise PublicError(
                f"{name} failed verification: {detail}",
                "the pin may be dead or the mirror corrupted the transfer — "
                "retry; if it persists the manifest needs a deliberate edit (F7)",
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, dest)  # final name only after verify (DigUp rule)
        self.err(f"[done] {name}: {dest} ({entry['bytes']:,} bytes, pin verified)")
        return dest

    def _disk_precheck(self, name: str, entry: dict) -> None:
        dest_dir = self.root / ZIP_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        needed = entry["bytes"] + DISK_SLACK_BYTES
        try:
            free = shutil.disk_usage(dest_dir).free
        except OSError as exc:
            raise PublicError(f"cannot stat {dest_dir} for the disk precheck: {exc}")
        if free < needed:
            missing = needed - free
            raise PublicError(
                f"not enough disk space for {name}: need {entry['bytes']:,} bytes "
                f"+ {DISK_SLACK_BYTES // (1 << 20)} MB slack, only {free:,} free on "
                f"{dest_dir} (missing {missing:,} bytes)",
                "free space or set GOLDMINE_PUBLIC_DATA elsewhere",
            )

    def _download(self, name: str, entry: dict, dest: Path, partial: Path) -> None:
        """Resumable transfer (model.py _download shape): Range: bytes=<n>- from
        the .partial size; a server answering 200 after a Range restarts at 0;
        404/410 fail fast (a dead pin never heals); 429/5xx backoff 2/5/15/30 s."""
        url = entry["url"]
        self.err(f"[pending] {name}: {url}")
        attempt = 0
        while True:
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                resp = self._open_url(
                    urllib.request.Request(url, headers=headers), timeout=60.0
                )
                with resp:
                    status = getattr(resp, "status", 200)
                    if offset and status != 206:
                        offset = 0  # server ignored the Range: restart from zero
                    total = entry["bytes"]
                    mode = "ab" if offset else "wb"
                    size = offset
                    partial.parent.mkdir(parents=True, exist_ok=True)
                    with open(partial, mode) as out:
                        while True:
                            block = resp.read(CHUNK_BYTES)
                            if not block:
                                break
                            out.write(block)
                            size += len(block)
                    if size != total and size < total:
                        raise urllib.error.URLError(
                            f"short read: {size} of {total} bytes")
                if attempt:
                    self.err(f"[downloading] {name}: resumed/complete after retry {attempt}")
                return
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    # dead-pin class: burning the backoff on a 404 measured
                    # 52 s of pointless retries (model.py:402-413 rationale)
                    raise PublicError(
                        f"{name} is not available at {url} — HTTP {exc.code}: "
                        "the manifest pin is dead or the file was removed",
                        "verify upstream; recovery is a deliberate manifest edit "
                        "with re-measured bytes+sha (F7) — no retry attempted",
                    ) from exc
                if attempt < len(RETRY_BACKOFF_S):
                    wait = RETRY_BACKOFF_S[attempt]
                    attempt += 1
                    self.err(f"[downloading] {name}: transfer failed (HTTP {exc.code}: "
                             f"{exc.reason}) — retry {attempt}/{len(RETRY_BACKOFF_S)} "
                             f"in {wait:g}s (resuming from the .partial)")
                    self._sleep(wait)
                    continue
                raise PublicError(
                    f"download of {name} failed after {attempt} attempts: "
                    f"HTTP {exc.code}: {exc.reason}",
                    "check the network and re-run — the .partial file resumes",
                ) from exc
            except (urllib.error.URLError, OSError) as exc:
                if attempt < len(RETRY_BACKOFF_S):
                    wait = RETRY_BACKOFF_S[attempt]
                    attempt += 1
                    self.err(f"[downloading] {name}: transfer failed ({exc}) — "
                             f"retry {attempt}/{len(RETRY_BACKOFF_S)} in {wait:g}s "
                             "(resuming from the .partial)")
                    self._sleep(wait)
                    continue
                raise PublicError(
                    f"download of {name} failed after {attempt} attempts: {exc}",
                    "check the network and re-run — the .partial file resumes",
                ) from exc


def unzip_dataset(name: str, entry: dict, root: Path) -> Path:
    """Extract the verified zip into <root>/beir/<name>/ (skipped when the
    corpus is already there; the zip sha was verified by the caller)."""
    dest = dataset_dir(root, name)
    if (dest / "corpus.jsonl").is_file():
        return dest
    zp = zip_path(root, name)
    sha, size = sha256_and_size(zp)
    if size != entry["bytes"] or sha != entry["sha256"]:
        raise PublicError(
            f"{name}: zip on disk drifted from the pin — refusing to extract",
            "re-run with --download (cache invalidated BY sha, never patched)",
        )
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zp) as zf:
        zf.extractall(dest)  # CPython zipfile sanitizes absolute/.. members
    if not _corpus_present(dest):
        raise PublicError(
            f"{name}: zip extracted but no corpus.jsonl found — "
            "distribution shape changed (deliberate manifest edit required, F7)")
    return dest


def _corpus_present(dest: Path) -> bool:
    try:
        return _find_one(dest, "corpus.jsonl").is_file()
    except PublicError:
        return False


# ----------------------------------------------------------------- prepare

def docid_to_filename(docid: str) -> str:
    """<docid>.md; ids in this trio are [A-Za-z0-9-], anything else is
    percent-escaped rather than improvised (§4). quote/unquote roundtrips."""
    if re.fullmatch(DOCID_SAFE_RE, docid):
        return f"{docid}.md"
    return f"{quote(docid, safe='')}.md"


def docid_from_path(path: str) -> str:
    """hit.path → docid (inverse of docid_to_filename)."""
    return unquote(Path(path).stem)


def doc_md_bytes(doc: dict) -> bytes:
    """Deterministic byte format (D5): `# {title}\\n\\n{text}\\n` — the header
    feeds extract_markdown's title slot (framing `title: {real title} |
    text: {chunk}` on every chunk) AND chunk-1 text + FTS. F5: an empty title
    SKIPS the header line entirely (the title slot stays the docid slug)."""
    title = (doc.get("title") or "").strip()
    text = doc["text"]
    if title:
        return f"# {title}\n\n{text}\n".encode("utf-8")
    return f"{text}\n".encode("utf-8")


def write_corpus_md(corpus: list[dict], out_dir: Path) -> list[str]:
    """One <docid>.md per doc, docs sorted by _id (done by load_corpus).
    Returns the sorted docid list."""
    out_dir.mkdir(parents=True, exist_ok=True)
    docids = []
    for doc in corpus:
        docid = doc["_id"]
        docids.append(docid)
        (out_dir / docid_to_filename(docid)).write_bytes(doc_md_bytes(doc))
    return docids


def prepare_dataset(name: str, entry: dict, root: Path, *, backend_name: str,
                    rebuild: bool = False, verbose: bool = False) -> dict:
    """Unzip → corpus .md files → per-dataset ingest (D5/D6). Derived artifacts
    are keyed by (zip sha, backend) in <name>/prepared.json; a backend switch
    forces --rebuild (F9: the fingerprint gate refuses mixing vectors)."""
    ds_dir = dataset_dir(root, name)
    unzipped = unzip_dataset(name, entry, root)
    marker_path = ds_dir / "prepared.json"
    marker = {}
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text())
        except (OSError, ValueError):
            marker = {}
    db = ds_dir / "index.sqlite"
    fresh = (marker.get("zip_sha256") == entry["sha256"]
             and marker.get("backend") == backend_name
             and db.is_file() and not rebuild)
    if fresh:
        return {"db": db, "n_docs": marker.get("n_docs"), "rebuilt": False,
                "ingest_s": None, "report": marker}
    if (marker.get("backend") and marker["backend"] != backend_name
            and not rebuild and db.is_file()):
        raise PublicError(
            f"{name}: index was built with backend {marker['backend']!r}, "
            f"this run uses {backend_name!r} — the fingerprint gate refuses "
            "mixing vectors across embed identities",
            "re-run with --rebuild to re-embed under the current backend (F9)",
        )

    started = time.monotonic()
    corpus = load_corpus(unzipped)
    if len(corpus) != entry["n_docs"]:
        raise PublicError(
            f"{name}: corpus has {len(corpus)} docs, manifest pins "
            f"{entry['n_docs']} — pin and bytes disagree (deliberate edit required)")
    corpus_md = ds_dir / CORPUS_MD_DIR
    if corpus_md.exists():
        shutil.rmtree(corpus_md)  # derived: regenerated deterministically
    write_corpus_md(corpus, corpus_md)

    if db.exists():
        db.unlink()
    for suffix in ("-shm", "-wal"):
        stale = ds_dir / f"index.sqlite{suffix}"
        if stale.exists():
            stale.unlink()
    cfg = Config(home=str(ds_dir / ".evalhome"))
    cfg.backend = backend_name
    store = Store(db)
    backend = create_backend(cfg, backend_name)

    def _progress(msg: str) -> None:
        _err(f"ingest[{name}]: {msg}")

    try:
        report = IngestPipeline(cfg, store, backend).run(
            [str(corpus_md)], code=True,
            progress=_progress if verbose else None)
        fingerprint_id = store.meta("fingerprint_id")
    finally:
        store.close()
        backend.close()
    content = report.content_indexed
    if report.failed:
        raise PublicError(
            f"{name}: {report.failed} ingest failure(s) "
            f"(e.g. {report.failures[:1]}) — the corpus_md route must be clean")
    marker = {
        "zip_sha256": entry["sha256"],
        "backend": backend_name,
        "n_docs": len(corpus),
        "content_indexed": content,
        "fingerprint_id": fingerprint_id,
        "chunker_version": CHUNKER_VERSION,
    }
    marker_path.write_text(json.dumps(marker, indent=1) + "\n", encoding="utf-8")
    return {"db": db, "n_docs": len(corpus), "rebuilt": True,
            "ingest_s": time.monotonic() - started, "report": marker}


# ------------------------------------------------------------------ scoring

def apply_fetch_deep_cut(hits, docids: list[str], qid: str,
                         k: int = TOP_K) -> list:
    """F1: fetch-deep-then-cut. `hits` is the limit=20 retrieval; drop the
    (at most one — file-keyed fusion) hit whose docid == qid, THEN cut to k.
    A cut-then-drop would bias every self-doc query to a 9-hit window."""
    kept = [h for h, d in zip(hits, docids) if d != qid]
    return kept[:k]


def score_query_public(docids: list[str], grades: dict[str, int],
                       max_grade: int) -> dict:
    """Per-query metrics vs qrels (D7/D8/D9). nDCG block is the
    trec_eval-identical formula (eval_real.py:291-299): linear gain (= qrel
    grade), discount log2(rank+1), IDCG = top-10 of the query's own qrel
    grades sorted desc — DANGLING qrels included, so impossible positives cap
    nDCG below 1.0 exactly as trec_eval reports them. success/MRR threshold =
    the dataset's max grade."""
    gains = [grades.get(d, 0) for d in docids]
    first = next((g for g in gains if g == max_grade), None)
    rr = 0.0
    if first is not None:
        rr = 1.0 / (gains.index(first) + 1)
    success1 = 1.0 if gains and gains[0] == max_grade else 0.0
    success5 = 1.0 if any(g == max_grade for g in gains[:5]) else 0.0
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains[:TOP_K]) if g > 0)
    ideal = sorted(grades.values(), reverse=True)[:TOP_K]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal) if g > 0)
    ndcg = (dcg / idcg) if idcg > 0 else None
    return {
        "success1": success1,
        "success5": success5,
        "rr": rr,
        "ndcg": ndcg,
        "n_hits": len(docids),
        "noise": False,  # BEIR ships no noise queries — zero-hit honesty n/a
    }


def run_dataset_arms(store: Store, backend, name: str, entry: dict,
                     unzipped: Path, arms: list[str],
                     verbose: bool = False) -> dict[str, list[dict]]:
    """Both arms over the SAME index (D10), warm loop: the backend (llama
    server) is created once per RUN by the caller and shared across datasets
    (review F3); load_vectors caches per Store."""
    queries = load_queries(unzipped)
    qrels = load_qrels(unzipped)
    max_grade = entry["grade_range"][1]
    qids = sorted(set(qrels) & set(queries))  # D9a: the test split
    missing_text = sorted(set(qrels) - set(queries))
    if missing_text:
        raise PublicError(
            f"{name}: {len(missing_text)} qrel qids have no queries.jsonl row "
            f"(e.g. {missing_text[:3]}) — distribution shape changed")
    searcher = Searcher(store, backend)
    rows_by_arm: dict[str, list[dict]] = {}
    for arm in arms:
        rows: list[dict] = []
        for qid in qids:
            hits = searcher.search(queries[qid], limit=FETCH_LIMIT, scorer=arm)
            docids = [docid_from_path(h.path) for h in hits]
            if len(docids) != len(set(docids)):  # F2 invariant: 1:1 path→docid
                dupes = sorted({d for d in docids if docids.count(d) > 1})
                raise PublicError(
                    f"{name}/{arm}/{qid}: one-hit-per-doc invariant violated "
                    f"({len(docids)} hits, duplicate docids {dupes[:3]}) — "
                    "search semantics changed; the F1 drop rule is unsound now")
            kept = apply_fetch_deep_cut(hits, docids, qid)
            kept_ids = [docid_from_path(h.path) for h in kept]
            rows.append({
                "query": {"id": qid, "tag": f"public/{name}"},
                "fetched_ranked_docids": docids,
                "post_cut_ranked_docids": kept_ids,
                "query_text_sha256": hashlib.sha256(queries[qid].encode("utf-8")).hexdigest(),
                "metrics": score_query_public(kept_ids, qrels[qid], max_grade),
            })
            if verbose:
                top = kept_ids[0] if kept_ids else "-"
                _err(f"  {name}/{arm} {qid} top={top} "
                     f"ndcg={rows[-1]['metrics']['ndcg']}")
        rows_by_arm[arm] = rows
    return {"rows_by_arm": rows_by_arm, "n_queries": len(qids),
            "n_corpus": entry["n_docs"]}


# --------------------------------------------------- statistics (safe CI)

def _binom_tail_le_lp(k: int, n: int, p: float) -> float:
    """P(X <= k), X ~ Binomial(n, p) — the eval_real recurrence run in LOG
    space. LIVE-RUN CAUGHT: eval_real._binom_tail_le underflows at large n
    (q**n -> 0.0; arguana n=1406 k=854 produced the degenerate CI
    [0.411, 0.411]); the n=146 house reference is unaffected. eval_real.py is
    outside R5 scope, so the public suite owns this safe variant and its test
    pins the n=146 reference point to it."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    q = 1.0 - p
    log_ratio = math.log(p / q)
    log_pmf = n * math.log(q)
    total = 0.0
    for i in range(k + 1):
        total += math.exp(log_pmf)
        next_log = math.log((n - i) / (i + 1)) + log_ratio
        if log_pmf < -80.0 and next_log < 0.0:
            break  # past the mode AND below float noise: tail negligible
        log_pmf += next_log
    return min(total, 1.0)


def binom_ci_lp(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Exact Clopper-Pearson CI, underflow-safe at any n (see above)."""
    alpha = 1.0 - conf
    if n <= 0:
        return (0.0, 1.0)
    lo = 0.0 if k <= 0 else er._solve_decreasing(
        lambda p: _binom_tail_le_lp(k - 1, n, p), 1 - alpha / 2)
    hi = 1.0 if k >= n else er._solve_decreasing(
        lambda p: _binom_tail_le_lp(k, n, p), alpha / 2)
    return (lo, hi)


def headline_cis_public(rows: list[dict]) -> dict[str, str]:
    """er.headline_cis with the underflow-safe count CI (same shapes)."""
    ranked = [r for r in rows if not r["metrics"]["noise"]]

    def _ci(name: str, values: list[float], k: int | None) -> str:
        if not values:
            return "-"
        if k is not None:
            lo, hi = binom_ci_lp(k, len(values))
        else:
            lo, hi = er.bootstrap_ci(values)
        return f"[{lo:.3f}, {hi:.3f}]"

    out = {}
    for name, key in (("success@5", "success5"), ("success@1", "success1")):
        vals = [r["metrics"][key] for r in ranked]
        out[name] = _ci(name, vals, round(sum(vals)) if vals else None)
    out["MRR"] = _ci("MRR", [r["metrics"]["rr"] for r in ranked], None)
    ndcg = [r["metrics"]["ndcg"] for r in ranked if r["metrics"]["ndcg"] is not None]
    out["nDCG@10"] = _ci("nDCG@10", ndcg, None)
    return out


# ------------------------------------------------------------------- table

def _fmt_metric(m: dict, cis: dict, name: str) -> str:
    v = m.get("value")
    ci = cis.get(name, "-")
    val = f"{v:.4f}" if v is not None else " n/a"
    return f"{val} {ci}"


def best_published(entry: dict) -> tuple[float, str]:
    refs = entry["published_bm25"]
    labeled = [(v, k) for k, v in refs.items() if v is not None]
    v, k = max(labeled)
    return v, k


def render_table(results: dict[str, dict], manifest: dict, *,
                 backend: str, gate_note: bool = False) -> str:
    """The neutral comparison table (D10 + F2/F6 divergence footnotes)."""
    lines = [f"public BEIR mini-suite — backend {backend} — "
             f"{_dt.date.today().isoformat()}"]
    if backend == "mock":
        lines.append("mock backend: mechanics-only (hash embeddings — not a "
                     "quality signal; read the published column, not these numbers)")
    header = (f"{'dataset':<10} {'arm':<24} {'n':>5} "
              f"{'nDCG@10':>22} {'success@5':>22} {'success@1':>22} {'MRR':>22}")
    lines += [header, "-" * len(header)]
    for name, res in results.items():
        entry = manifest["datasets"][name]
        for arm, data in res["arms"].items():
            m = data["metrics"]
            cis = data["cis"]
            label = "BM25 (ours, lexical)" if arm == "lexical" else f"{arm} (ours)"
            lines.append(
                f"{name:<10} {label:<24} {res['n_queries']:>5} "
                f"{_fmt_metric(m['nDCG@10'], cis, 'nDCG@10'):>22} "
                f"{_fmt_metric(m['success@5'], cis, 'success@5'):>22} "
                f"{_fmt_metric(m['success@1'], cis, 'success@1'):>22} "
                f"{_fmt_metric(m['MRR'], cis, 'MRR'):>22}")
        refs = entry["published_bm25"]
        ref_cells = []
        for key, tag in (("beir_paper", "paper"), ("pyserini_2cr_flat", "2CR flat"),
                         ("pyserini_2cr_mf", "2CR MF"), ("anserini_repro", "anserini")):
            if refs.get(key) is not None:
                ref_cells.append(f"{tag} {refs[key]:.4f}" if key != "anserini_repro"
                                 else f"{tag} ≈{refs[key]:.4f}")
        pub = "nDCG " + " · ".join(ref_cells)
        lines.append(f"{name:<10} {'BM25 (published)':<24} {'-':>5} {pub:>22}")
        best, best_key = best_published(entry)
        ours = res["arms"].get("hybrid", {}).get("metrics", {}).get("nDCG@10", {})
        if ours.get("value") is not None:
            delta = ours["value"] - best
            lines.append(f"{name:<10} {'Δ hybrid − best pub':<24} {'':>5} "
                         f"{delta:>+22.4f}   (best published: {best_key})")
        lines.append("")
    lines.append("divergence footnotes (read deltas WITH these):")
    lines.append("  [1] fetch-deep-then-cut (review F1): retrieval limit=20 → qid==docid")
    lines.append("      hit dropped → cut to 10 → scored. The arguana self-doc is byte-")
    lines.append("      identical to its query (near-certain rank-1 on both legs); a")
    lines.append("      cut-then-drop would bias 69% of that suite.")
    lines.append("  [2] one-hit-per-doc invariant (F2): fusion is file-keyed, hit.path→docid")
    lines.append("      is 1:1. Chunk-fragmented BM25 — docs exceeding one chunk")
    lines.append("      (index-measured, chunk.v2-tok): scifact 13.0% / nfcorpus 17.4% /")
    lines.append("      arguana 21.9% — published BM25 ranks whole docs; and our")
    lines.append("      title-in-header framing differs from published baselines'")
    lines.append("      title+text concatenation.")
    lines.append("  [3] name-segment slugs (F6): our index carries name segments over")
    lines.append("      docid slugs (arguana slugs carry topical words); published BM25")
    lines.append("      has no such field — mild apples-oranges on the lexical arm only.")
    lines.append("  [4] our z-gate may return fewer than 10 hits (zero-hit honesty is part")
    lines.append("      of what the exam measures); BEIR BM25 always returns 10 ranked hits.")
    lines.append("  [5] published numbers are cross-implementation CONTEXT, never a gate")
    lines.append("      (D10); the controlled delta is lexical vs hybrid on the same index.")
    return "\n".join(lines)


# ----------------------------------------------------------- baseline/gate

def build_baseline(results: dict[str, dict], manifest: dict, root: Path, *,
                   backend: str, arms: list[str], datasets: list[str]) -> dict:
    """evals/public_baseline.json (D11, schema modeled on eval_real's).
    provenance carries the llama-server build string (review F8)."""
    provenance: dict = {"llama_build": None, "llama_server": None}
    if backend == "llama":
        try:
            from goldmine.backends.llama import probe_version, resolve_server_path

            server, source = resolve_server_path(load().llama)
            provenance = {"llama_build": probe_version(server) if server else None,
                          "llama_server": f"{server} ({source})" if server else None}
        except Exception as exc:  # provenance is never load-bearing (D2)
            provenance = {"llama_build": None, "llama_server": None,
                          "error": str(exc)}
    out: dict = {
        "schema": 1,
        "suite": "beir-mini",
        "backend": backend,
        "arms": arms,
        "date": _dt.date.today().isoformat(),
        "provenance": provenance,
        "identities": {
            "chunker_version": CHUNKER_VERSION,
            "prompt_scheme": PROMPT_SCHEME,
            "zip_sha256": {n: manifest["datasets"][n]["sha256"] for n in datasets},
            "manifest_sha256": sha256_and_size(PUBLIC_MANIFEST_JSON)[0],
        },
        "datasets": {},
        "gate": {"delta": er.GATE_DELTA, "bootstrap_reps": er.BOOTSTRAP_REPS,
                 "seed": er.BOOTSTRAP_SEED, "mcnemar_alpha": er.MCNEMAR_ALPHA},
        "notes": [
            "advisory v1 (D11): never wired to cron/CI, can never block the "
            "product gates (eval_real --gate + answer gate stay THE release gates)",
            "metrics per arm reuse the eval_real machinery unchanged; success "
            "threshold = the dataset's max qrel grade (D8)",
            "fetch-deep-then-cut per review F1; identities/sha mismatch exits 2",
        ],
    }
    for name, res in results.items():
        out["datasets"][name] = {
            "zip_sha256": manifest["datasets"][name]["sha256"],
            "n_queries": res["n_queries"],
            "arms": {
                arm: {"metrics": data["metrics"],
                      "per_query": er.per_query_maps(data["rows"])}
                for arm, data in res["arms"].items()
            },
        }
    return out


def run_gate(results: dict[str, dict], baseline: dict, manifest: dict,
             root: Path, *, backend: str, datasets: list[str]) -> tuple[str, list[str]]:
    """Advisory gate (D11): identity/sha mismatch → exit-2 error; per
    dataset+arm compare_to_baseline with the house rule; returns (report,
    tripped)."""
    problems: list[str] = []
    if baseline.get("backend") != backend:
        raise PublicError(
            f"baseline backend {baseline.get('backend')!r} != {backend!r} — "
            "mock and llama numbers are not comparable")
    ident = baseline.get("identities") or {}
    for key, now in (("chunker_version", CHUNKER_VERSION),
                     ("prompt_scheme", PROMPT_SCHEME),
                     ("manifest_sha256", sha256_and_size(PUBLIC_MANIFEST_JSON)[0])):
        if ident.get(key) != now:
            problems.append(f"identity {key}: baseline {ident.get(key)!r} != live {now!r}")
    for name in datasets:
        if name not in baseline.get("datasets", {}):
            problems.append(f"dataset {name}: absent from baseline")
            continue
        # verify DISK state (B7-style): re-hash the zip actually on disk
        zp = zip_path(root, name)
        if not zp.is_file():
            problems.append(f"dataset {name}: zip missing from cache")
            continue
        sha, size = sha256_and_size(zp)
        pin = manifest["datasets"][name]
        if size != pin["bytes"] or sha != pin["sha256"]:
            problems.append(f"dataset {name}: cached zip no longer matches its pin")
        elif baseline["datasets"][name].get("zip_sha256") != sha:
            problems.append(f"dataset {name}: baseline zip sha != live sha")
    if problems:
        raise PublicError(
            "baseline identities drifted — never compared across identities: "
            + "; ".join(problems),
            "re-run --update-baseline --yes deliberately")
    report: dict = {"datasets": {}}
    tripped: list[str] = []
    for name in datasets:
        entry_base = baseline["datasets"][name]
        cur = results.get(name)
        if cur is None:
            continue
        report["datasets"][name] = {}
        for arm in cur["arms"]:
            base_arm = (entry_base.get("arms") or {}).get(arm)
            if not base_arm:
                report["datasets"][name][arm] = {"verdict": "n/a",
                                                 "note": "no baseline arm"}
                continue
            gate = er.compare_to_baseline(
                cur["arms"][arm]["metrics"],
                er.per_query_maps(cur["arms"][arm]["rows"]),
                base_arm)
            report["datasets"][name][arm] = gate
            for metric in gate["tripped"]:
                tripped.append(f"{name}/{arm}/{metric}")
    lines = ["advisory public gate (v1, D11): house rule per dataset+arm — "
             "a tracked metric fails only on -0.02 AND significance; NEVER a "
             "release gate."]
    for name, arms in report["datasets"].items():
        for arm, gate in arms.items():
            if gate.get("verdict") == "n/a":
                lines.append(f"  {name}/{arm}: {gate['note']}")
                continue
            for metric, m in gate["metrics"].items():
                if m.get("delta") is None:
                    continue
                lines.append(f"  {name}/{arm:<7} {metric:<12} "
                             f"{m['baseline']:>7} -> {m['current']:>7} "
                             f"({m['delta']:+.4f})  {m['verdict']}")
    if tripped:
        lines.append(f"  GATE TRIPPED by: {', '.join(tripped)} (advisory v1 — "
                     "report it; it cannot block a release)")
    else:
        lines.append("  gate: green (advisory)")
    return "\n".join(lines), tripped


# --------------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", action="append", default=None,
                        metavar="NAME",
                        help="limit scope (repeatable; default: all three)")
    parser.add_argument("--arm", action="append", default=None,
                        choices=["hybrid", "lexical", "semantic", "rrf"],
                        help="arms to run (repeatable; default: both)")
    parser.add_argument("--backend", default="mock", choices=["mock", "llama"])
    parser.add_argument("--download", action="store_true",
                        help="fetch+verify missing zips, build corpora+indexes "
                             "and stop (no scoring); also heals a sha-mismatched "
                             "cached zip deliberately")
    parser.add_argument("--offline", action="store_true",
                        help="cache-only: never touch the network")
    parser.add_argument("--rebuild", action="store_true",
                        help="regenerate corpus_md + index even when fresh "
                             "(required after a backend switch, F9)")
    parser.add_argument("--table", action="store_true",
                        help="print the comparison table (the default output)")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--gate", action="store_true",
                        help="advisory regression gate vs "
                             "evals/public_baseline.json (v1: never blocks)")
    parser.add_argument("--yes", action="store_true",
                        help="required with --update-baseline")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return _run(args)
    except PublicError as exc:
        _err(f"error: {exc}")
        if exc.fix_hint:
            _err(f"fix: {exc.fix_hint}")
        return 2


def _run(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    all_names = list(manifest["datasets"])
    datasets = args.dataset or all_names
    unknown = [d for d in datasets if d not in manifest["datasets"]]
    if unknown:
        raise PublicError(f"unknown dataset(s) {unknown} — known: {all_names}")
    arms = args.arm or ["hybrid", "lexical"]
    root = data_root()
    fetcher = Fetcher(root)
    allow_download = not args.offline

    # ---- acquire + prepare
    prep: dict[str, dict] = {}
    for name in datasets:
        entry = manifest["datasets"][name]
        fetcher.ensure_zip(name, entry, allow_download=allow_download,
                           force_refresh=args.download)
        prep[name] = prepare_dataset(name, entry, root, backend_name=args.backend,
                                     rebuild=args.rebuild, verbose=args.verbose)
        note = ("rebuilt" if prep[name]["rebuilt"] else "cached")
        secs = prep[name]["ingest_s"]
        wall = f" in {secs:.0f}s" if secs else ""
        _err(f"[prepare] {name}: index {note}{wall}")

    if args.download:
        return 0  # fetch+verify+build only (§8 usage)

    # ---- run (one backend spawn for the whole run — review F3)
    run_cfg = Config(home=str(root / ZIP_DIR / ".runhome"))
    run_cfg.backend = args.backend
    backend = create_backend(run_cfg, args.backend)
    results: dict[str, dict] = {}
    try:
        for name in datasets:
            entry = manifest["datasets"][name]
            unzipped = dataset_dir(root, name)
            started = time.monotonic()
            store = Store(prep[name]["db"])
            try:
                out = run_dataset_arms(store, backend, name, entry, unzipped,
                                       arms, verbose=args.verbose)
            finally:
                store.close()
            arms_out = {}
            for arm, rows in out["rows_by_arm"].items():
                arms_out[arm] = {
                    "metrics": er.summarize(rows),
                    "cis": headline_cis_public(rows),
                    "rows": rows,
                }
            results[name] = {
                "n_queries": out["n_queries"],
                "n_corpus": out["n_corpus"],
                "wall_search_s": time.monotonic() - started,
                "arms": arms_out,
            }
            _err(f"[run] {name}: {out['n_queries']} queries × {len(arms)} arm(s) "
                 f"in {results[name]['wall_search_s']:.1f}s")
    finally:
        backend.close()

    if args.json:
        print(json.dumps({
            "suite": "beir-mini", "backend": args.backend,
            "advisory_only": True,
            "formal_export": "Use --raw with explicit prepared cache, production root, model and server.",
            "datasets": {
                name: {
                    "n_queries": res["n_queries"],
                    "wall_search_s": round(res["wall_search_s"], 1),
                    "arms": {arm: {"metrics": data["metrics"],
                                   "cis": data["cis"], "rows": data["rows"]}
                             for arm, data in res["arms"].items()},
                } for name, res in results.items()},
        }, indent=1, ensure_ascii=False))
    else:
        print(render_table(results, manifest, backend=args.backend))

    if args.update_baseline:
        if not args.yes:
            _err("--update-baseline requires --yes (baselines are deliberate)")
            return 2
        if datasets != all_names or sorted(arms) != ["hybrid", "lexical"]:
            _err("--update-baseline needs the full suite (all datasets, both "
                 "arms) — a partial run must not clobber the other rows")
            return 2
        baseline = build_baseline(results, manifest, root, backend=args.backend,
                                  arms=arms, datasets=datasets)
        PUBLIC_BASELINE_JSON.write_text(
            json.dumps(baseline, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8")
        _err(f"baseline written: {PUBLIC_BASELINE_JSON}")

    if args.gate:
        if not PUBLIC_BASELINE_JSON.is_file():
            _err(f"no baseline at {PUBLIC_BASELINE_JSON} — run "
                 "--update-baseline --yes first")
            return 2
        baseline = json.loads(PUBLIC_BASELINE_JSON.read_text())
        gate_text, tripped = run_gate(results, baseline, manifest, root,
                                      backend=args.backend, datasets=datasets)
        if args.json:
            print(json.dumps({"gate": gate_text, "tripped": tripped},
                             indent=1, ensure_ascii=False))
        else:
            print(gate_text)
        if tripped:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
