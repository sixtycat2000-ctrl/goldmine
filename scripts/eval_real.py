#!/usr/bin/env python3
"""Real-corpus relevance eval + regression gate (v2 items D1/D2,
docs/v2/01-search-relevance.md §5.1, docs/v2/REDESIGN.md §1.1-1.2).

Runs the hand-labeled queries in evals/real.jsonl (+ the merged
evals/media_queries.jsonl set). The default legacy suite preserves the frozen
needs exclusions; --suite full-media includes every query and verifies actual
stored ASR/OCR/EXIF evidence before any inference against the eval index built by
scripts/build_eval_corpus.py.

Metrics (B12/A4 naming — success@5, NOT R@5):
  success@5 / success@1   a query is solved when any expect with grade >= 2
                          is found in the top k with its location constraint
                          satisfied (DigUp at() semantics: pdf pages contained,
                          code/doc line ranges overlapped)
  MRR                     reciprocal rank of the first qualifying hit
  nDCG@10                 graded (DCG = Σ relᵢ/log₂(i+1); IDCG from the
                          query's own ideal ordering); a same-file hit at the
                          wrong location scores grade-1 (the "same file other
                          location" grade), floor 0
  zero-hit honesty        noise queries (expect []) must return 0 hits;
                          violations are counted and listed

Per-tag breakdown prints n and marks tags below n >= 20 ADVISORY (B12: a
-0.02 on a 5-query tag is half a query — it can neither justify nor block).
lookup (n=16) and zero-hit honesty (n=5) are ADVISORY VERBATIM: their exact
95% CIs span the scale ([0.15, 0.85] at n=5) — they report deltas and can
never trip the gate.

Every run headline carries 95% CIs: exact binomial (Clopper-Pearson,
scipy-free) for the count metrics, percentile bootstrap for MRR/nDCG. At
n=109 the s@5 CI is ~±0.08 — read deltas smaller than that as noise.

The ONE mechanical accuracy gate: --gate — a tracked metric failing only
when its drop is BOTH beyond -0.02 absolute AND statistically significant:
s@5/s@1 via the exact McNemar test on paired flips (two-sided p < 0.05;
trips at 7 same-direction flips at n=109, where the paired bootstrap's
effective wire is ~10 flips), MRR/nDCG via the paired bootstrap 95% CI.
lookup/zero-hit are advisory and cannot trip. The 0.85/0.90-class numbers
are PUBLISHED TARGETS, printed, not gated.

Usage:
  python3 scripts/eval_real.py                          # mock, offline
  python3 scripts/eval_real.py --backend llama          # real embeddings
  python3 scripts/eval_real.py --backend llama --gate
  python3 scripts/eval_real.py --update-baseline --yes --backend llama
  python3 scripts/eval_real.py --split train            # tuning reads 70% only
  python3 scripts/eval_real.py --write-splits           # (re)commit splits.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))  # build_eval_corpus (baseline sha)

from goldmine.backends import create_backend  # noqa: E402
from goldmine.config import Config  # noqa: E402
from goldmine.search import Searcher  # noqa: E402
from goldmine.store import Store  # noqa: E402

EVALS_DIR = REPO_ROOT / "evals"
REAL_JSONL = EVALS_DIR / "real.jsonl"
MEDIA_JSONL = EVALS_DIR / "media_queries.jsonl"
MANIFEST_JSON = EVALS_DIR / "corpus_manifest.json"
SPLITS_JSON = EVALS_DIR / "splits.json"
BASELINE_JSON = EVALS_DIR / "baseline.json"

#: media-pipeline capabilities that have not landed yet — their queries are
#: skipped by default so the gate's corpus stays stable (C11/A4)
# video-eval-001: "frames" joins the gated set — the 8 keyframe-retrieval
# questions stay OUT of the frozen legacy 109 and IN full-media.
SKIPPED_NEEDS = frozenset({"asr", "ocr", "exif", "frames"})

#: per-tag deltas below this n are advisory only (B12/A4)
ADVISORY_N = 20

#: the regression rule: a drop beyond this, AND statistically significant,
#: fails (the -0.02 alone is fiction at this n — power analysis: detecting
#: -0.02 on a binary metric needs ~7,400 queries per arm unpaired)
GATE_DELTA = 0.02
BOOTSTRAP_REPS = 2000
SPLIT_SEED = 20261016
BOOTSTRAP_SEED = 42

#: s@5/s@1 are paired binary outcomes — the primary gate statistic is the
#: exact McNemar test on the discordant pairs (trips at 7 same-direction
#: flips at n=109; the paired bootstrap's effective wire there is ~10)
BINARY_METRICS = frozenset({"success@5", "success@1"})
MCNEMAR_ALPHA = 0.05

#: subsets that can never statistically gate at their current n (lookup n=16,
#: zero-hit n=5 — exact CIs span [0.15, 0.85]): reported verbatim, never trip
ADVISORY_METRICS = frozenset({"lookup success@1", "zero-hit honesty"})

TRACKED_METRICS = ("success@5", "success@1", "MRR", "nDCG@10",
                   "lookup success@1", "zero-hit honesty")

PUBLISHED_TARGETS = {  # §1.2: floors to establish, REPORTED not gated
    "success@5": 0.85, "success@1": 0.55, "MRR": 0.65,
    "lookup success@1": 0.90,
}


# ------------------------------------------------------------------ loading

def load_jsonl(path: Path) -> list[dict]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{n}: bad JSON ({exc})") from exc
    return out


def load_queries(evals_dir: Path = EVALS_DIR) -> list[dict]:
    """real.jsonl + the merged media set (ids must stay unique across both)."""
    queries = load_jsonl(evals_dir / "real.jsonl")
    if (evals_dir / "media_queries.jsonl").is_file():
        queries += load_jsonl(evals_dir / "media_queries.jsonl")
    seen: set[str] = set()
    for q in queries:
        if q["id"] in seen:
            raise SystemExit(f"duplicate query id {q['id']!r}")
        seen.add(q["id"])
    return queries


def should_skip(query: dict, include_needs: set[str] | None = None) -> bool:
    needs = set(query.get("needs") or [])
    blocked = needs & SKIPPED_NEEDS
    if not blocked:
        return False
    return not blocked.issubset(set(include_needs or []))


def eligible_queries(queries: list[dict], include_needs=None) -> list[dict]:
    return [q for q in queries if not should_skip(q, include_needs)]


def validate(queries: list[dict], manifest: dict, fixture_root: Path) -> list[str]:
    """Label validator: every expect path must exist in the manifest."""
    pinned = {e["path"] for e in manifest["files"]}
    fixture = {e["path"] for e in (manifest.get("fixture") or {}).get("files", [])}
    problems: list[str] = []
    for q in queries:
        qid = q.get("id", "?")
        if not q.get("q"):
            problems.append(f"{qid}: empty query text")
        tag = q.get("tag")
        if not tag:
            problems.append(f"{qid}: missing tag")
        for exp in q.get("expect", []):
            p = exp["path"]
            if p.startswith("/"):
                if p not in pinned:
                    problems.append(f"{qid}: expect path not in manifest: {p}")
            elif p in fixture:
                pass  # fixture-relative, pinned
            elif (fixture_root / p).is_file():
                problems.append(f"{qid}: fixture file not pinned: {p}")
            else:
                problems.append(f"{qid}: expect path unknown: {p}")
            g = exp.get("grade")
            if not isinstance(g, int) or not 0 <= g <= 3:
                problems.append(f"{qid}: bad grade {g!r}")
            if "lines" in exp and "pages" in exp:
                problems.append(f"{qid}: both lines and pages given")
        if tag == "noise" and q.get("expect"):
            problems.append(f"{qid}: noise query must have empty expect")
    return problems


# ------------------------------------------------------------------- splits

def write_splits(queries: list[dict], path: Path, seed: int = SPLIT_SEED) -> dict:
    """Seeded 70/30 split stratified by tag over the ELIGIBLE queries
    (needs:asr/ocr/exif excluded). Committed; tuning may only read train."""
    by_tag: dict[str, list[str]] = {}
    for q in eligible_queries(queries):
        by_tag.setdefault(q["tag"], []).append(q["id"])
    rng = random.Random(seed)
    train: list[str] = []
    holdout: list[str] = []
    for tag in sorted(by_tag):
        ids = sorted(by_tag[tag])
        rng.shuffle(ids)
        cut = round(len(ids) * 0.7)
        train.extend(ids[:cut])
        holdout.extend(ids[cut:])
    splits = {"seed": seed, "advisory_n": ADVISORY_N,
              "train": sorted(train), "holdout": sorted(holdout),
              "notes": ["tuning reads train only; holdout is read only by full runs",
                        "new queries enter the train/report pool; the frozen "
                        "holdout ids are never retuned against"]}
    path.write_text(json.dumps(splits, indent=1) + "\n", encoding="utf-8")
    return splits


def select_split(queries: list[dict], split: str, splits: dict | None) -> list[dict]:
    if split == "all":
        return queries
    if splits is None:
        raise SystemExit(f"--split {split} needs {SPLITS_JSON} (run --write-splits)")
    known = set(splits["train"]) | set(splits["holdout"])
    if split == "frozen":  # full run, but must not silently diverge from splits
        missing = {q["id"] for q in queries} - known
        if missing:
            raise SystemExit(
                f"splits.json is stale (unknown ids: {sorted(missing)[:5]}…) — "
                "rerun --write-splits deliberately and review labels"
            )
        return queries
    pool = set(splits["train" if split == "train" else "holdout"])
    return [q for q in queries if q["id"] in pool]


# ------------------------------------------------------------------ scoring

def _resolve(path: str, fixture_root: Path) -> str:
    """Canonical absolute path: the index stores resolved roots (walk.py
    resolves them), and on macOS /tmp is a symlink to /private/tmp — both
    sides must be canonical or every fixture label mismatches."""
    p = Path(path) if path.startswith("/") else fixture_root / path
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def _loc_satisfied(hit, exp: dict) -> bool:
    if "pages" in exp:
        a, b = exp["pages"]
        if hit.kind != "pdf" or hit.loc is None:
            return False
        end = hit.loc_end if hit.loc_end is not None else hit.loc
        return hit.loc <= b and end >= a  # containment (DigUp at())
    if "lines" in exp:
        a, b = exp["lines"]
        if hit.kind in ("pdf", "audio", "video") or hit.loc is None:
            return False
        end = hit.loc_end if hit.loc_end is not None else hit.loc
        return hit.loc <= b and end >= a  # overlap (DigUp at())
    if "loc" in exp:
        # video keyframe moment (video-eval-001): satisfied only by the SAME
        # kept frame — Hit.segment is the SEGMENT kind ("frame"; Hit.kind is
        # the file kind "video" — using it here scored 0/37 in construction),
        # and same-file audio windows must never fake a numeric containment.
        return (hit.segment == "frame" and hit.loc is not None
                and abs(hit.loc - exp["loc"]) < 0.5)
    return True


def _rel(hit_path: str, hit, exp: dict, fixture_root: Path) -> int:
    """Gain of a hit against one expectation (0 when it is not that file)."""
    if hit_path != _resolve(exp["path"], fixture_root):
        return 0
    grade = exp["grade"]
    if _loc_satisfied(hit, exp):
        return grade
    return max(grade - 1, 0)  # same file, other location


def score_query(query: dict, hits, fixture_root: Path) -> dict:
    """Per-query metric contributions from an ordered hit list (limit 10)."""
    noise = query["tag"] == "noise"
    paths = [h.path for h in hits]
    expects = query.get("expect", [])

    def qualifies(rank_i: int) -> bool:
        """Solved at this rank: a grade>=2 expect on this file with its
        location constraint satisfied (success@k / MRR credit rule)."""
        for exp in expects:
            if (exp["grade"] >= 2
                    and paths[rank_i] == _resolve(exp["path"], fixture_root)
                    and _loc_satisfied(hits[rank_i], exp)):
                return True
        return False

    success1 = bool(paths) and qualifies(0)
    success5 = any(qualifies(i) for i in range(min(5, len(paths))))
    rr = 0.0
    for i in range(len(paths)):
        if qualifies(i):
            rr = 1.0 / (i + 1)
            break
    # nDCG@10: linear gain, log2(i+1) discount (01 §5.1); same-file-wrong-loc
    # scores grade-1 via _rel
    dcg = 0.0
    for i in range(min(10, len(paths))):
        gain = max((_rel(paths[i], hits[i], exp, fixture_root) for exp in expects),
                   default=0)
        if gain > 0:
            dcg += gain / math.log2(i + 2)
    ideal = sorted((exp["grade"] for exp in expects), reverse=True)
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal[:10]) if g > 0)
    ndcg = (dcg / idcg) if idcg > 0 else None  # None = excluded (noise / all-0)

    return {
        "success1": 1.0 if success1 else 0.0,
        "success5": 1.0 if success5 else 0.0,
        "rr": rr,
        "ndcg": ndcg,
        "n_hits": len(hits),
        "noise": noise,
    }


def summarize(rows: list[dict]) -> dict:
    """rows: [{query, tag, lookup-tag?, metrics}] -> metric dict with n."""
    ranked = [r for r in rows if not r["metrics"]["noise"]]
    noise = [r for r in rows if r["metrics"]["noise"]]

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    out = {
        "success@5": {"value": mean([r["metrics"]["success5"] for r in ranked]),
                      "n": len(ranked)},
        "success@1": {"value": mean([r["metrics"]["success1"] for r in ranked]),
                      "n": len(ranked)},
        "MRR": {"value": mean([r["metrics"]["rr"] for r in ranked]),
                "n": len(ranked)},
        "nDCG@10": {"value": mean([r["metrics"]["ndcg"] for r in ranked]),
                    "n": sum(1 for r in ranked if r["metrics"]["ndcg"] is not None)},
    }
    lookup = [r for r in ranked if r["query"].get("tag") == "lookup"]
    out["lookup success@1"] = {
        "value": mean([r["metrics"]["success1"] for r in lookup]) if lookup else None,
        "n": len(lookup),
    }
    if noise:
        out["zero-hit honesty"] = {
            "value": mean([1.0 if r["metrics"]["n_hits"] == 0 else 0.0
                           for r in noise]),
            "n": len(noise),
        }
    else:
        out["zero-hit honesty"] = {"value": None, "n": 0}
    return out


def per_tag(rows: list[dict]) -> dict[str, dict]:
    tags: dict[str, dict] = {}
    for r in rows:
        entry = tags.setdefault(r["query"]["tag"],
                                {"success@5": [], "success@1": [], "MRR": [],
                                 "nDCG@10": [], "n": 0, "noise_n": 0,
                                 "zero_hit_violations": 0})
        m = r["metrics"]
        if m["noise"]:
            entry["noise_n"] += 1
            if m["n_hits"] > 0:
                entry["zero_hit_violations"] += 1
        else:
            entry["n"] += 1
            entry["success@5"].append(m["success5"])
            entry["success@1"].append(m["success1"])
            entry["MRR"].append(m["rr"])
            if m["ndcg"] is not None:
                entry["nDCG@10"].append(m["ndcg"])
    for entry in tags.values():
        for key in ("success@5", "success@1", "MRR", "nDCG@10"):
            vals = entry[key]
            entry[key] = (sum(vals) / len(vals)) if vals else None
    return tags


# ---------------------------------------------------------------- statistics

from eval_legacy_statistics import (  # compatibility exports for existing harness clients
    _binom_tail_le, _solve_decreasing, binom_ci, mcnemar_exact, bootstrap_ci,
)


def compare_to_baseline(current: dict, per_query: dict, baseline: dict) -> dict:
    """The one mechanical gate (B12: counts/uncertainty).

    A tracked metric trips the gate iff its delta < -0.02 AND the drop is
    statistically significant on PAIRED per-query data: s@5/s@1 via the exact
    McNemar test on discordant pairs (two-sided p < 0.05), MRR/nDCG via the
    paired bootstrap 95% CI. lookup/zero-hit honesty are ADVISORY: their
    subsets (n=16/n=5) are too small for any CI to gate, so they report
    deltas and can never trip. Returns {metric: {baseline, current, delta,
    n, ci, verdict, note}} plus an overall 'tripped' list under '__tripped__'.
    """
    report: dict = {"metrics": {}, "tripped": [], "gate_delta": GATE_DELTA}
    base_metrics = baseline.get("metrics", {})
    for name in TRACKED_METRICS:
        cur = current.get(name) or {}
        base = base_metrics.get(name) or {}
        cur_v, base_v = cur.get("value"), base.get("value")
        if cur_v is None or base_v is None:
            report["metrics"][name] = {
                "baseline": base_v, "current": cur_v, "delta": None,
                "n": cur.get("n"), "verdict": "n/a",
                "note": "not computable on this run/split",
            }
            continue
        delta = cur_v - base_v
        n = cur.get("n") or 0
        note_parts = [f"n={n}", f"1 query = {1.0 / n:.4f}" if n else "n=0"]
        # significance on paired per-query values
        base_pq = (baseline.get("per_query") or {}).get(name) or {}
        cur_pq = per_query.get(name) or {}
        common = sorted(set(base_pq) & set(cur_pq))
        if name in BINARY_METRICS and len(common) >= 2:
            gained = sum(1 for q in common if cur_pq[q] > 0 and base_pq[q] <= 0)
            dropped = sum(1 for q in common if cur_pq[q] <= 0 and base_pq[q] > 0)
            p = mcnemar_exact(gained, dropped)
            outside = p < MCNEMAR_ALPHA
            note_parts.append(
                f"exact McNemar on discordant pairs "
                f"(+{gained} gained / -{dropped} dropped): p={p:.4f}")
        elif len(common) >= 2:
            rng = random.Random(BOOTSTRAP_SEED)
            deltas = []
            for _ in range(BOOTSTRAP_REPS):
                acc = 0.0
                for _ in range(len(common)):
                    qid = common[rng.randrange(len(common))]
                    acc += cur_pq[qid] - base_pq[qid]
                deltas.append(acc / len(common))
            deltas.sort()
            ci = (deltas[int(0.025 * BOOTSTRAP_REPS)],
                  deltas[min(BOOTSTRAP_REPS - 1, int(0.975 * BOOTSTRAP_REPS))])
            outside = ci[1] < -GATE_DELTA
            note_parts.append(f"paired bootstrap 95% CI of delta "
                              f"[{ci[0]:+.4f}, {ci[1]:+.4f}]")
        else:
            outside = False
            note_parts.append("no paired per-query data — significance untestable")
        if delta < -GATE_DELTA and outside and name not in ADVISORY_METRICS:
            verdict = "REGRESSION"
            report["tripped"].append(name)
        elif delta < -GATE_DELTA:
            verdict = "drop-within-noise"  # reported, not failed (B12)
            if name in ADVISORY_METRICS:
                note_parts.append(
                    "ADVISORY: cannot trip the gate (subset n too small to "
                    "gate — exact CI spans the scale)")
            else:
                note_parts.append("drop could be single-query noise at this n")
        else:
            verdict = "ok"
        report["metrics"][name] = {
            "baseline": round(base_v, 4), "current": round(cur_v, 4),
            "delta": round(delta, 4), "n": n,
            "verdict": verdict, "note": "; ".join(note_parts),
        }
    return report


# --------------------------------------------------------------------- run

def run_eval(db: Path, backend_name: str, queries: list[dict],
             fixture_root: Path, *, scorer: str = "hybrid",
             verbose: bool = False) -> list[dict]:
    cfg = Config(home=str(db.parent / ".evalhome"))
    cfg.backend = backend_name
    store = Store(db, ro=True)
    backend = create_backend(cfg, backend_name)
    rows: list[dict] = []
    try:
        searcher = Searcher(store, backend)
        for q in queries:  # already sorted by id
            kinds = set(q["kinds"]) if q.get("kinds") else None
            with store.read_snapshot():
                hits = searcher.search(q["q"], limit=10, scorer=scorer, kinds=kinds)
            rows.append({"query": q, "metrics": score_query(q, hits, fixture_root),
                         "hit_paths": [h.path for h in hits],
                         "hits": [{"path": h.path, "score": h.score,
                                   "duplicate_of": h.duplicate_of} for h in hits]})
            if verbose:
                mark = "ok" if (rows[-1]["metrics"]["success5"] or
                                q["tag"] == "noise") else "MISS"
                print(f"  {q['id']} [{q['tag']}] {mark} "
                      f"top={hits[0].path.rsplit('/', 1)[-1] if hits else '-'}")
    finally:
        store.close()
        backend.close()
    return rows


# --------------------------------------------------------------- fold audit

#: a hit scoring below this fraction of the top hit is in the human UI's
#: weaker-matches fold (same 0.5x rule the renderers use)
FOLD_FRACTION = 0.5


def _hit_folded(hit: dict, top_score: float) -> bool:
    """The human fold rule: near-duplicates fold under their kept hit
    (search.py item 15.2a), weaker matches (score < 0.5x top) fold away."""
    if hit.get("duplicate_of") is not None:
        return True
    return bool(top_score > 0 and hit["score"] < FOLD_FRACTION * top_score)


def fold_audit(rows: list[dict], fixture_root: Path) -> dict:
    """F3 (fold audit): does the ranked list hide an expected match?

    For every non-noise query, an expected path that appears in the hits but
    ONLY inside the folded region (duplicate folds + score < 0.5x top) is
    `fold_hidden`: the metric credits it, a human scanning the unfolded view
    would never see it. Advisory by design — the count rides along with the
    printed metrics and the gate output, and gates nothing.
    """
    hidden: list[dict] = []
    audited = 0
    for r in rows:
        q = r["query"]
        if q["tag"] == "noise":
            continue
        hits = r.get("hits") or []
        if not hits:
            continue
        audited += 1
        top = hits[0]["score"] or 0.0
        expected = {_resolve(e["path"], fixture_root) for e in q.get("expect", [])}
        for path in sorted(expected):
            path_hits = [h for h in hits if h["path"] == path]
            if path_hits and all(_hit_folded(h, top) for h in path_hits):
                hidden.append({"id": q["id"], "tag": q["tag"], "path": path})
                break
    return {"audited": audited, "fold_hidden": len(hidden), "hidden": hidden}


def render_fold_audit(audit: dict) -> str:
    """The one summary line (advisory, never gating)."""
    return (
        f"fold-audit: {audit['fold_hidden']} of {audit['audited']} audited "
        "queries have an expected path ONLY in the folded region "
        f"(duplicate or <{FOLD_FRACTION:g}x top) — advisory, not gated"
    )


def headline_cis(rows: list[dict]) -> dict[str, str]:
    """95% CI per headline metric for EVERY run output (04-eval F2: the
    v2.0 headline printed value+n only — no run ever showed its uncertainty):
    exact Clopper-Pearson for the count metrics, percentile bootstrap of the
    per-query values for the mean-based ones (MRR/nDCG)."""
    ranked = [r for r in rows if not r["metrics"]["noise"]]

    def _ci(name: str, values: list[float], k: int | None) -> str:
        if not values:
            return "-"
        if k is not None:
            lo, hi = binom_ci(k, len(values))
        else:
            lo, hi = bootstrap_ci(values)
        return f"[{lo:.3f}, {hi:.3f}]"

    out = {}
    for name, key, count in (("success@5", "success5", True),
                             ("success@1", "success1", True)):
        vals = [r["metrics"][key] for r in ranked]
        out[name] = _ci(name, vals, round(sum(vals)) if vals else None)
    out["MRR"] = _ci("MRR", [r["metrics"]["rr"] for r in ranked], None)
    ndcg = [r["metrics"]["ndcg"] for r in ranked if r["metrics"]["ndcg"] is not None]
    out["nDCG@10"] = _ci("nDCG@10", ndcg, None)
    lookup = [r["metrics"]["success1"] for r in ranked
              if r["query"].get("tag") == "lookup"]
    out["lookup success@1"] = _ci("lookup", lookup,
                                  round(sum(lookup)) if lookup else None)
    noise = [1.0 if r["metrics"]["n_hits"] == 0 else 0.0
             for r in rows if r["metrics"]["noise"]]
    out["zero-hit honesty"] = _ci("zh", noise, round(sum(noise)) if noise else None)
    return out


def render(rows: list[dict], current: dict, tags: dict, *, split: str,
           backend: str, fold_audit_report: dict | None = None) -> str:
    cis = headline_cis(rows)
    lines = [
        f"eval_real: {len(rows)} queries (split {split}, backend {backend}, "
        f"hybrid scorer)",
    ]
    if backend == "mock":
        lines.append(
            "mock backend: mechanics-only (hash embeddings — not a quality "
            "signal; φ(mock,llama)≈0.009 per-query agreement)"
        )
    lines.append(
        f"{'metric':<18} {'value':>7} {'n':>4}  {'95% CI':>17}   "
        "published target (reported, not gated)"
    )
    for name in ("success@5", "success@1", "MRR", "nDCG@10", "lookup success@1"):
        m = current[name]
        target = PUBLISHED_TARGETS.get(name)
        tgt = f"{target:.2f}" if target else "-"
        val = f"{m['value']:.3f}" if m["value"] is not None else "n/a"
        lines.append(f"{name:<18} {val:>7} {m['n']:>4}  {cis.get(name, '-'):>17}"
                     f"   {tgt}")
    zh = current["zero-hit honesty"]
    val = f"{zh['value']:.3f}" if zh["value"] is not None else "n/a"
    lines.append(f"{'zero-hit honesty':<18} {val:>7} {zh['n']:>4}  "
                 f"{cis.get('zero-hit honesty', '-'):>17}   noise queries "
                 "returning 0 hits (target 1.000)")
    if fold_audit_report is not None:
        # F3: the fold audit rides with the printed metrics (advisory)
        lines.append(render_fold_audit(fold_audit_report))
    lines.append("")
    lines.append(f"per-tag (ADVISORY below n >= {ADVISORY_N}):")
    lines.append(f"  {'tag':<12} {'n':>3} {'s@5':>6} {'s@1':>6} {'MRR':>6} "
                 f"{'nDCG':>6}  noise/zero-hit")
    for tag in sorted(tags):
        t = tags[tag]
        adv = " (advisory)" if t["n"] < ADVISORY_N and t["n"] > 0 else ""
        def f(v):
            return f"{v:.3f}" if v is not None else "-"
        lines.append(
            f"  {tag:<12} {t['n']:>3} {f(t['success@5']):>6} {f(t['success@1']):>6} "
            f"{f(t['MRR']):>6} {f(t['nDCG@10']):>6}  "
            f"{t['noise_n']}/{t['zero_hit_violations']}{adv}"
        )
    # misses worth eyeballing (top-level only, terse)
    misses = [r for r in rows
              if not r["metrics"]["noise"] and not r["metrics"]["success5"]]
    if misses:
        lines.append("")
        lines.append(f"missed in top 5 ({len(misses)}):")
        for r in misses[:15]:
            top = r["hit_paths"][0].rsplit("/", 1)[-1] if r["hit_paths"] else "-"
            lines.append(f"  {r['query']['id']} [{r['query']['tag']}] "
                         f"{r['query']['q'][:60]!r} top={top}")
    viol = [r for r in rows if r["metrics"]["noise"] and r["metrics"]["n_hits"] > 0]
    if viol:
        lines.append("")
        lines.append(f"zero-hit violations ({len(viol)}):")
        for r in viol:
            lines.append(f"  {r['query']['id']} {r['query']['q']!r} -> "
                         f"{r['metrics']['n_hits']} hits")
    return "\n".join(lines)


def render_claims(current: dict, db: Path) -> str:
    """The three falsifiable v2.0 claims (REDESIGN §1.1), status where
    computable from this run."""
    store = Store(db, ro=True)
    try:
        failed = store.db.execute(
            "SELECT COUNT(*) FROM files WHERE state = 'failed'"
        ).fetchone()[0]
        embed_failed = store.db.execute(
            "SELECT COUNT(*) FROM files WHERE state = 'failed' "
            "AND detail LIKE '%embedding failed%'"
        ).fetchone()[0]
        counts = store.counts()
        vectorless = store.db.execute(
            "SELECT COUNT(*) FROM segments WHERE vec IS NULL AND kind != 'name'"
        ).fetchone()[0]
    finally:
        store.close()
    lines = [
        "",
        "v2.0 falsifiable claims (status where computable):",
        "  1. measured accuracy on the real corpus: printed above with per-tag n",
    ]
    for name, target in PUBLISHED_TARGETS.items():
        m = current.get(name) or {}
        v = m.get("value")
        if v is None:
            lines.append(f"     {name}: n/a (target {target})")
        else:
            state = "meets" if v >= target else "below"
            lines.append(f"     {name}: {v:.3f} vs published target {target} "
                         f"({state} — target is owned, not a gate)")
    lines.append(
        f"  2. zero file-level embed failures: {embed_failed} embed-failed files, "
        f"{failed} failed files total, {vectorless} vectorless non-name segments "
        f"of {counts.segments} (index-level evidence)"
    )
    lines.append(
        "  3. per-kind coverage floors: computed by `goldmine status --json` + "
        "the coverage fixture (02 §9), not by this harness"
    )
    return "\n".join(lines)


def render_gate(gate: dict) -> str:
    lines = [
        "",
        f"regression gate (the ONE accuracy gate: tracked metric "
        f"-{GATE_DELTA} vs baseline AND statistically significant — s@5/s@1: "
        f"exact McNemar p < {MCNEMAR_ALPHA} on paired flips; MRR/nDCG: "
        f"outside paired bootstrap 95% CI; lookup/zero-hit: advisory, "
        f"cannot trip — else exit 1):",
        f"  {'metric':<18} {'baseline':>8} {'current':>8} {'delta':>8}  verdict",
    ]
    for name in TRACKED_METRICS:
        m = gate["metrics"][name]
        d = f"{m['delta']:+.4f}" if m["delta"] is not None else "n/a"
        lines.append(
            f"  {name:<18} {m['baseline']!s:>8} {m['current']!s:>8} {d:>8}  "
            f"{m['verdict']}"
        )
        lines.append(f"      {m['note']}")
    if gate.get("fold_audit") is not None:
        # F3: the audit rides with the gate output — advisory, never a trip
        lines.append(f"  {render_fold_audit(gate['fold_audit'])}")
    if gate["tripped"]:
        lines.append(f"  GATE TRIPPED by: {', '.join(gate['tripped'])}")
    else:
        lines.append("  gate: green")
    return "\n".join(lines)


def per_query_maps(rows: list[dict]) -> dict[str, dict[str, float]]:
    """{metric: {qid: value}} for baseline recording + paired bootstrap."""
    out = {name: {} for name in TRACKED_METRICS}
    for r in rows:
        qid, m = r["query"]["id"], r["metrics"]
        if m["noise"]:
            out["zero-hit honesty"][qid] = 1.0 if m["n_hits"] == 0 else 0.0
            continue
        out["success@5"][qid] = m["success5"]
        out["success@1"][qid] = m["success1"]
        out["MRR"][qid] = m["rr"]
        if m["ndcg"] is not None:
            out["nDCG@10"][qid] = m["ndcg"]
        if r["query"].get("tag") == "lookup":
            out["lookup success@1"][qid] = m["success1"]
    return out


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(EVALS_DIR / "eval.sqlite"))
    parser.add_argument("--backend", default="mock", help="mock | llama")
    parser.add_argument("--suite", choices=["legacy", "full-media"], default="legacy",
                        help="legacy keeps the frozen 109-query denominator (128 minus 19 needs-gated); full-media requires all 128")
    parser.add_argument("--manifest", default=str(MANIFEST_JSON))
    parser.add_argument("--fixture-root", default=None)
    parser.add_argument("--split", default="all",
                        choices=["all", "frozen", "train", "holdout"],
                        help="train = the 70%% tuning pool only; holdout is read "
                             "only by full runs; frozen = all + splits sanity check")
    parser.add_argument("--include-needs", metavar="CAP", action="append", default=[],
                        help="run skipped media queries needing CAP "
                             "(asr|ocr|exif) anyway")
    parser.add_argument("--scorer", default="hybrid",
                        choices=["hybrid", "lexical", "semantic", "rrf"])
    parser.add_argument("--gate", action="store_true",
                        help="compare against evals/baseline.json: a tracked "
                             "metric trips (exit 1) only when its drop is "
                             "both beyond -0.02 AND significant — s@5/s@1 "
                             "exact McNemar p<0.05, MRR/nDCG paired bootstrap "
                             "95%% CI; lookup/zero-hit are advisory (cannot "
                             "trip). Also re-verifies the corpus on disk "
                             "(manifest hashes + fixture) before comparing")
    parser.add_argument(
        "--fold-audit", action=argparse.BooleanOptionalAction, default=True,
        help="report whether any expected path falls ONLY in the human fold "
             "region (duplicates + score < 0.5x top) — default ON, ADVISORY: "
             "counts and lists, gates nothing (--no-fold-audit silences)")
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--yes", action="store_true",
                        help="required with --update-baseline")
    parser.add_argument("--write-splits", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from eval_media_suite import SUITE_IDS, inspect_index
    suite_id = SUITE_IDS[args.suite]
    args.baseline = args.baseline or str(BASELINE_JSON if args.suite == "legacy"
                                         else EVALS_DIR / "media_full_baseline.json")
    if args.suite == "full-media" and args.split != "all":
        parser.error("full-media uses all 128 queries; --split must be all")

    manifest = json.loads(Path(args.manifest).read_text())
    fixture_root = Path(args.fixture_root or manifest["fixture"]["root"])

    queries = sorted(eligible_queries(load_queries(),
                                      include_needs=SKIPPED_NEEDS if args.suite == "full-media"
                                      else set(args.include_needs)),
                     key=lambda q: q["id"])

    problems = validate(queries, manifest, fixture_root)
    if problems:
        print(f"label validation FAILED ({len(problems)} problems):",
              file=sys.stderr)
        for p in problems[:25]:
            print(f"  {p}", file=sys.stderr)
        return 2

    if args.write_splits:
        all_q = load_queries()
        splits = write_splits(all_q, SPLITS_JSON)
        print(f"splits: {SPLITS_JSON} — train {len(splits['train'])} / "
              f"holdout {len(splits['holdout'])} (seed {splits['seed']})")
        return 0

    if args.validate_only:
        print(f"validate: {len(queries)} eligible queries, labels consistent "
              f"with {len(manifest['files'])} pinned files + "
              f"{len(manifest['fixture']['files'])} fixture files")
        return 0

    db = Path(args.db)
    if not db.is_file():
        print(f"no eval index at {db} — run scripts/build_eval_corpus.py first",
              file=sys.stderr)
        return 2

    preflight = inspect_index(db, queries, fixture_root)
    if args.suite == "full-media" and preflight["missing"]:
        result = {"suite_id": suite_id, "status": "MISSING", **preflight,
                  "repair": "Run scripts/build_eval_corpus.py --backend llama explicitly after preparing the missing source metadata/tools."}
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return 2

    splits = json.loads(SPLITS_JSON.read_text()) if SPLITS_JSON.is_file() else None
    queries = select_split(queries, args.split, splits)

    rows = run_eval(db, args.backend, queries, fixture_root, scorer=args.scorer,
                    verbose=args.verbose and not args.json)
    current = summarize(rows)
    tags = per_tag(rows)
    pq = per_query_maps(rows)
    audit = fold_audit(rows, fixture_root) if args.fold_audit else None

    if args.json:
        out = {"split": args.split, "backend": args.backend, "suite_id": suite_id,
               "preflight": preflight,
               "corpus_version": manifest["corpus_version"], "metrics": current,
               "per_tag": tags,
               **({"fold_audit": audit} if audit is not None else {}),
               "rows": [
                   {"id": r["query"]["id"], "tag": r["query"]["tag"],
                    **r["metrics"]} for r in rows]}
        print(json.dumps(out, indent=1, ensure_ascii=False))
    else:
        print(render(rows, current, tags, split=args.split, backend=args.backend,
                     fold_audit_report=audit))
        print(render_claims(current, db))

    if args.update_baseline:
        if not args.yes:
            print("--update-baseline requires --yes (baselines are deliberate)",
                  file=sys.stderr)
            return 2
        if args.split not in ("all", "frozen"):
            print("--update-baseline needs a full run (--split all|frozen)",
                  file=sys.stderr)
            return 2
        import build_eval_corpus as bec  # corpus sha helper (same directory)
        baseline = {
            "schema": 1,
            "suite_id": suite_id,
            "preflight": preflight,
            "backend": args.backend,
            "scorer": args.scorer,
            "date": _dt.datetime.now().strftime("%Y-%m-%d"),
            "corpus_version": manifest["corpus_version"],
            "corpus_sha": bec.corpus_sha(manifest),
            "split": args.split,
            "metrics": current,
            "per_query": pq,
            "gate": {"delta": GATE_DELTA, "bootstrap_reps": BOOTSTRAP_REPS,
                     "seed": BOOTSTRAP_SEED, "advisory_n": ADVISORY_N,
                     "mcnemar_alpha": MCNEMAR_ALPHA},
            "notes": [
                "the one accuracy gate: tracked metric -0.02 AND significant — "
                "s@5/s@1 exact McNemar p<0.05 on paired flips; MRR/nDCG paired "
                "bootstrap 95% CI",
                "lookup success@1 (n=16) and zero-hit honesty (n=5) are "
                "ADVISORY: they report deltas and cannot trip",
                "mock backend is mechanics-only (phi≈0.01 vs llama per-query "
                "agreement) — never read mock numbers as a quality signal",
                "search rule is the v2.1 single z-gate (3.0) + ANY-term lookup "
                "trigger; labels/corpus v2 (snapshot-pinned goldmine slice)",
            ],
        }
        Path(args.baseline).write_text(
            json.dumps(baseline, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"baseline written: {args.baseline} (backend {args.backend}, "
              f"corpus_sha {baseline['corpus_sha']})")

    if args.gate:
        if args.split not in ("all", "frozen"):
            print(f"--gate needs a full run (--split all|frozen), not "
                  f"{args.split!r} — subset metrics are not comparable to the "
                  "baseline", file=sys.stderr)
            return 2
        baseline_path = Path(args.baseline)
        if not baseline_path.is_file():
            print(f"no baseline at {baseline_path} — run --update-baseline --yes "
                  "first", file=sys.stderr)
            return 2
        baseline = json.loads(baseline_path.read_text())
        if baseline.get("suite_id", "legacy.v3") != suite_id:
            print("baseline suite identity differs; legacy and full-media cannot be compared", file=sys.stderr)
            return 2
        if baseline.get("corpus_version") != manifest["corpus_version"]:
            print(f"baseline corpus_version {baseline.get('corpus_version')} != "
                  f"manifest {manifest['corpus_version']} — old baselines are "
                  "invalidated loudly, not compared across corpora",
                  file=sys.stderr)
            return 2
        import build_eval_corpus as bec
        # verify DISK state, not the manifest's self-reported hashes (the old
        # sha check hashed the manifest itself — tautological): re-hash every
        # pinned file + the fixture, fail loudly on any drift
        drift = bec.verify_manifest(manifest)
        if drift:
            print(f"corpus drift on disk ({len(drift)}):", file=sys.stderr)
            for d in drift[:20]:
                print(f"  {d}", file=sys.stderr)
            print("the pinned corpus no longer matches its recorded hashes — "
                  "refresh the manifest deliberately (review labels), then "
                  "re-run --update-baseline --yes", file=sys.stderr)
            return 2
        live_sha = bec.corpus_sha(manifest)
        if baseline.get("corpus_sha") != live_sha:
            print(f"baseline corpus_sha {baseline.get('corpus_sha')} != live "
                  f"{live_sha} — the pinned corpus changed (refreshed manifest "
                  "or fixture); re-run --update-baseline --yes deliberately",
                  file=sys.stderr)
            return 2
        if baseline.get("backend") != args.backend:
            print(f"baseline backend {baseline.get('backend')!r} != "
                  f"{args.backend!r} — mock and llama numbers are not comparable",
                  file=sys.stderr)
            return 2
        gate = compare_to_baseline(current, pq, baseline)
        gate["fold_audit"] = audit  # F3: advisory, never part of the trip rule
        if not args.json:
            print(render_gate(gate))
        else:
            print(json.dumps({"gate": gate}, indent=1, ensure_ascii=False))
        if gate["tripped"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
