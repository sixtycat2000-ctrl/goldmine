#!/usr/bin/env python3
"""Explicit local reranker capability probe, separate from retrieval A/B quality.

Never downloads a model. Verifies its bytes and tests the pinned server's actual
tokenize/rerank endpoints, including reversed document order. Reaps only its child.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def sha256(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def post(port, endpoint, value, timeout):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/{endpoint}",
        json.dumps(value).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def probe(server, model, expected_sha, out):
    if out.exists() and any(out.iterdir()):
        raise ValueError("--out must be absent or empty")
    if sha256(model) != expected_sha:
        raise ValueError("model SHA256 mismatch; server was not started")
    out.mkdir(parents=True, exist_ok=True)
    version = subprocess.run([str(server), "--version"], capture_output=True, timeout=10)
    if version.returncode:
        raise ValueError("server --version failed")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    argv = [str(server), "-m", str(model), "--embedding", "--reranking", "--pooling", "rank",
            "--host", "127.0.0.1", "--port", str(port), "-c", "2048", "-np", "1", "--no-webui"]
    report = {"schema": 1, "kind": "reranker-capability/v1", "argv": argv,
              "model_sha256": expected_sha, "server_sha256": sha256(server),
              "server_version": (version.stdout + version.stderr).decode("utf-8", "replace"),
              "quality_status": "NOT_MEASURED", "checks": [], "status": "FAIL"}
    proc = None
    try:
        with (out / "server.log").open("wb") as log:
            proc = subprocess.Popen(argv, stdout=log, stderr=log)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise ValueError(f"reranker server exited with {proc.returncode}")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=.3) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(.05)
            else:
                raise ValueError("reranker startup timeout")
            canaries = [
                ("What is the capital of France?", "Paris is the capital of France.", "Copper gaskets should be inspected on Tuesdays."),
                ("How do I sort a Python list in place?", "Use list.sort() to sort a Python list in place.", "The Moon orbits the Earth."),
                ("中国的首都是哪里？", "北京是中国的首都。", "酸面包需要定期喂养酵母。"),
            ]
            for query, relevant, distractor in canaries:
                pair = []
                for documents in ([relevant, distractor], [distractor, relevant]):
                    counts = [len(post(port, "tokenize", {"content": doc}, 5)["tokens"]) for doc in documents]
                    if any(count > 384 for count in counts):
                        raise ValueError("probe document exceeded 384 reranker tokens")
                    started = time.monotonic()
                    raw = post(port, "v1/rerank", {"query": query, "documents": documents, "top_n": 2}, 10)
                    items = raw.get("results")
                    if not isinstance(items, list) or len(items) != 2:
                        raise ValueError("reranker returned incomplete results")
                    scores = {item["index"]: item["relevance_score"] for item in items}
                    if set(scores) != {0, 1} or any(type(v) not in (int, float) or not math.isfinite(v) for v in scores.values()):
                        raise ValueError("reranker returned invalid indices or scores")
                    target = documents.index(relevant)
                    ok = scores[target] > scores[1 - target] and scores[target] > .5
                    pair.append({"query": query, "documents": documents, "token_counts": counts,
                                 "raw_response": raw, "elapsed_ms": (time.monotonic() - started) * 1000,
                                 "expected_index": target, "passed": ok})
                report["checks"].extend(pair)
            report["status"] = "PASS" if all(row["passed"] for row in report["checks"]) else "FAIL"
    except (OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
        report["error"] = str(exc)
    finally:
        if proc is not None:
            report["owned_pid"] = proc.pid
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            report["child_reaped"] = proc.poll() is not None
        report["artifacts"] = {"server.log": sha256(out / "server.log")} if (out / "server.log").is_file() else {}
        (out / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = probe(args.server.resolve(), args.model.resolve(), args.sha256, args.out)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"status": result["status"], "checks": len(result["checks"]),
                      "quality_status": result["quality_status"], "child_reaped": result.get("child_reaped")}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
