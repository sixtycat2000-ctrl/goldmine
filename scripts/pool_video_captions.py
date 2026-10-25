#!/usr/bin/env python3
"""video-eval-001 construction step 2: caption pooling.

For every captioned keyframe, run the REAL search stack (llama backend,
hybrid scorer, limit 10 — exactly what eval_real uses) against the pool
index and record the rank of the TARGET frame segment (same file, kind
'frame', |loc - expect| < 0.5s). rank <= 5 -> usable caption.

Usage: python3 scripts/pool_video_captions.py [--db /tmp/pool.sqlite]
Writes reports/execution/video-eval-001/pooling.jsonl (every caption, hit
or miss) and prints the pool summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from goldmine.backends import create_backend  # noqa: E402
from goldmine.config import Config  # noqa: E402
from goldmine.search import Searcher  # noqa: E402
from goldmine.store import Store  # noqa: E402

CLIP_FILES = {
    "bbb": REPO_ROOT / "evals" / "video_corpus" / "bbb-excerpt.mp4",
    "ed": REPO_ROOT / "evals" / "video_corpus" / "ed-excerpt.mp4",
    "ed2": REPO_ROOT / "evals" / "video_corpus" / "ed2-excerpt.mp4",
    "sintel": REPO_ROOT / "evals" / "video_corpus" / "sintel-excerpt.mp4",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/tmp/pool.sqlite")
    parser.add_argument("--out", default=str(
        REPO_ROOT / "reports/execution/video-eval-001/pooling.jsonl"))
    args = parser.parse_args()

    ledger = []
    ledger_path = REPO_ROOT / "reports/execution/video-eval-001/captions.jsonl"
    for line in ledger_path.read_text().splitlines():
        if line.startswith("//"):
            continue
        ledger.append(json.loads(line))

    cfg = Config(home=str(Path(args.db).parent / ".evalhome"))
    cfg.backend = "llama"
    store = Store(Path(args.db))
    backend = create_backend(cfg, "llama")
    searcher = Searcher(store, backend)
    rows = []
    try:
        for entry in ledger:
            target = str(CLIP_FILES[entry["clip"]])
            hits = searcher.search(entry["caption"], limit=10, scorer="hybrid")
            rank = None
            top5 = []
            for i, h in enumerate(hits):
                top5.append({"path": h.path.rsplit("/", 1)[-1],
                             "kind": h.kind, "segment": h.segment,
                             "loc": h.loc})
                # Hit.kind is the FILE kind ("video"); the segment kind lives
                # in Hit.segment — a frame hit carries the primary segment's
                # loc (hits are per-file: select_evidence picks the segment).
                if (rank is None and h.path == target
                        and h.segment == "frame"
                        and h.loc is not None
                        and abs(h.loc - entry["loc"]) < 0.5):
                    rank = i + 1
            rows.append({
                "clip": entry["clip"], "loc": entry["loc"],
                "caption": entry["caption"],
                "target_rank": rank,
                "top3": top5[:3],
            })
            print(f"{entry['clip']:6s} {entry['loc']:5.1f}s "
                  f"rank={rank if rank else '-':>3} {entry['caption'][:60]}")
    finally:
        backend.close()
        store.close()

    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    pool = [r for r in rows if r["target_rank"] and r["target_rank"] <= 5]
    print(f"\npool: {len(pool)}/{len(rows)} captions rank<=5")
    for clip in CLIP_FILES:
        n = len([r for r in pool if r["clip"] == clip])
        print(f"  {clip}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
