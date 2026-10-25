"""Optional local rank-head model; independent of the embedding fingerprint.

One instance owns one localhost child. Requests never install assets. The caller
closes it on success, failure and cancellation; no idle resident model is kept.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from .deadline import check_deadline, deadline_sleep, remaining_timeout
from .http_transport import request as http_request
from .model_manifest import DEFAULT_RERANKER_KEY, by_key


class RerankError(RuntimeError):
    pass


def from_config(cfg):
    from .backends.llama import REPO_MODELS, _in_repo_checkout, resolve_server_path

    entry = by_key(DEFAULT_RERANKER_KEY)
    server, _ = resolve_server_path(cfg.llama)
    directory = Path(cfg.models.dir).expanduser() if cfg.models.dir else Path(cfg.home).expanduser() / "models"
    model = directory / entry.name
    if not model.is_file() and _in_repo_checkout():
        model = REPO_MODELS / entry.subdir / entry.name
    return LocalReranker(server, model)


def validate_scores(payload, count):
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != count:
        raise RerankError("incomplete rerank results")
    scores = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RerankError("invalid rerank result")
        index, score = row.get("index"), row.get("relevance_score")
        if (type(index) is not int or not 0 <= index < count or index in scores
                or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
            raise RerankError("invalid rerank index or confidence")
        scores[index] = float(score)
    return [scores[i] for i in range(count)]


class LocalReranker:
    def __init__(self, server: Path | None, model: Path):
        self.server, self.model = server, Path(model)
        self._proc = None
        self._log = None
        self._port = None

    def _post(self, endpoint, payload):
        check_deadline()
        try:
            response = http_request(
                f"http://127.0.0.1:{self._port}/{endpoint}",
                data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                headers={"Content-Type": "application/json"}, timeout_s=10,
            )
            result = json.loads(response.body)
        except (OSError, ValueError) as exc:
            check_deadline()
            raise RerankError(f"reranker request failed: {type(exc).__name__}") from exc
        check_deadline()
        return result

    def startup(self):
        check_deadline()
        if self._proc is not None and self._proc.poll() is None:
            return
        entry = by_key(DEFAULT_RERANKER_KEY)
        if self.server is None or not Path(self.server).is_file() or not self.model.is_file():
            raise RerankError(f"local reranker is missing; install {entry.key} explicitly")
        from .util import sha256_file

        if self.model.stat().st_size != entry.bytes or sha256_file(self.model) != entry.sha256:
            raise RerankError("reranker model size/SHA256 differs from the pinned manifest")
        check_deadline()
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            self._port = reserved.getsockname()[1]
        self._log = tempfile.TemporaryFile()
        try:
            self._proc = subprocess.Popen([
                str(self.server), "-m", str(self.model), "--embedding", "--reranking", "--pooling", "rank",
                "--host", "127.0.0.1", "--port", str(self._port), "-c", "2048", "-np", "1", "--no-webui",
            ], stdout=self._log, stderr=self._log, stdin=subprocess.DEVNULL)
            until = time.monotonic() + remaining_timeout(30)
            while time.monotonic() < until:
                check_deadline()
                if self._proc.poll() is not None:
                    raise RerankError(f"reranker exited with code {self._proc.returncode}")
                try:
                    response = http_request(f"http://127.0.0.1:{self._port}/health", timeout_s=.25, max_bytes=2000)
                    if response.status == 200:
                        return
                except OSError:
                    check_deadline()
                deadline_sleep(.05)
            raise RerankError("reranker startup timed out")
        except BaseException:
            self.close()
            raise

    def _bounded_text(self, text, maximum):
        # Clip source UTF-8 only at verified tokenizer-piece boundaries. Never
        # reconstruct model-produced text or estimate a supposedly exact cap.
        payload = self._post("tokenize", {"content": text, "add_special": False, "with_pieces": True})
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        if not isinstance(tokens, list):
            raise RerankError("reranker tokenizer unavailable")
        pieces = []
        for item in tokens:
            piece = item.get("piece") if isinstance(item, dict) else None
            if isinstance(piece, str):
                pieces.append(piece.encode("utf-8"))
            elif isinstance(piece, list) and all(type(b) is int and 0 <= b <= 255 for b in piece):
                pieces.append(bytes(piece))
            else:
                raise RerankError("invalid reranker token piece")
        if b"".join(pieces) != text.encode("utf-8"):
            raise RerankError("reranker pieces do not round-trip to source")
        clipped = b"".join(pieces[:maximum]).decode("utf-8", "ignore")
        # Removing a partial Unicode character can change final tokenization.
        count = self._post("tokenize", {"content": clipped, "add_special": False}).get("tokens")
        if not isinstance(count, list) or len(count) > maximum:
            raise RerankError("reranker clipped input exceeds token cap")
        return clipped, len(count)

    def rank(self, query: str, documents: list[str]) -> tuple[list[float], dict]:
        if not 1 <= len(documents) <= 20:
            raise RerankError("reranker accepts 1..20 admitted segments")
        self.startup()
        bounded_query, query_tokens = self._bounded_text(query, 256)
        bounded = [self._bounded_text(text, 384) for text in documents]
        payload = self._post("v1/rerank", {"query": bounded_query,
            "documents": [text for text, _ in bounded], "top_n": len(documents)})
        return validate_scores(payload, len(documents)), {
            "model": DEFAULT_RERANKER_KEY, "query_tokens": query_tokens,
            "document_tokens": [count for _, count in bounded],
            "query_truncated": bounded_query != query,
            "documents_truncated": [text != source for (text, _), source in zip(bounded, documents)],
        }

    def close(self):
        proc, self._proc = self._proc, None
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=.25)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
        finally:
            if self._log is not None:
                self._log.close()
                self._log = None
