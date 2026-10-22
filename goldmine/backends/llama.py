"""LlamaServerBackend — managed llama-server subprocess + OpenAI-compatible HTTP.

Lifecycle (ARCHITECTURE §7.2, binding scope for build pass 4; v2 resolution
order per 05 §4.7 — the installed-copy fix):
  resolve binary (env GOLDMINE_LLAMA_SERVER > config llama.server >
  <goldmine home>/bin/llama-server > vendor/llama.cpp/build/bin/llama-server
  [repo checkout only] > brew prefix, with the brew arch warning) ->
  resolve model (env GOLDMINE_MODEL > config llama.model when configured >
  legacy default when present > <home>/models pin > repo models/ pin) ->
  verify model file + sha256 (sidecar cache) -> probe-then-start on an ephemeral
  127.0.0.1 port (a healthy foreign server on the port is NEVER adopted; we pick
  another port) -> poll GET /health until startup_timeout -> one probe embed to
  enforce dim == 768 -> POST /v1/embeddings in cfg.batch batches with
  per-request deadlines and retry-once on transient failures -> shutdown =
  SIGTERM, 5 s, SIGKILL. Only processes WE spawned are ever killed.

Runtime fact (recorded 2026-10-12): the brew llama-server (0.5.0, build 11146)
CANNOT load this model — the GGUF arch `gemma-embedding2` is unknown to that
build. The project uses a locally built llama-server (vendor/llama.cpp, tag
b11461). `describe()` and doctor report this honestly.

Inputs arrive prompt-framed (pipeline applies `title: … | text: …`, search
applies `task: search result | query: …`); this backend never adds prompts.
Output vectors are L2-normalized defensively (`normalization: l2_post` in the
fingerprint — a no-op when the server already normalized with --pooling mean).
"""

from __future__ import annotations

import collections
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
from pathlib import Path

import numpy as np

from ..deadline import (
    RequestTimeout, check_deadline, current_deadline, deadline_lock,
    deadline_sleep, remaining_timeout,
)
from ..http_transport import request as http_request
from .base import (
    PROMPT_SCHEME,
    BackendStartupError,
    Capability,
    EmbeddingBackend,
    EmbeddingError,
    NotSupported,
)

logger = logging.getLogger("goldmine.backends.llama")

BACKEND_CLASS: type["LlamaServerBackend"]  # registry hook (backends/__init__)

VEC_DIM = 768

#: Fixed llama-server parallel-slot count (04 §6.2: beyond 2 slots buys no
#: measured throughput — embedding is GPU-compute-bound — and costs KV memory).
SERVER_SLOTS = 2

#: Repo root (two levels above this module: goldmine/backends/llama.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_SERVER = REPO_ROOT / "vendor" / "llama.cpp" / "build" / "bin" / "llama-server"
BREW_SERVER = Path("/opt/homebrew/opt/llama.cpp/bin/llama-server")
#: Repo models dir (dev-checkout leg of the model resolution order).
REPO_MODELS = REPO_ROOT / "models"
#: The pinned llama.cpp tag the vendor/release builds use (05 §4.3).
PINNED_LLAMA_TAG = "b11461"

#: Fallback goldmine home when the LlamaCfg carries none (standalone configs);
#: real runs always carry cfg.home from config.load (env GOLDMINE_HOME aware).
DEFAULT_GOLDMINE_HOME = Path("~/Library/Application Support/goldmine")

#: sha256 of the pinned text model (models/SHA256SUMS); enforced when the
#: configured model carries the pinned basename.
PINNED_MODEL_BASENAME = "embeddinggemma-2-Q8_0.gguf"
PINNED_MODEL_SHA256 = "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135"

BREW_CANNOT_LOAD_NOTE = (
    "brew llama.cpp 0.5.0 (build 11146) cannot load EmbeddingGemma 2: GGUF arch "
    "'gemma-embedding2' is unknown to that build; use the release asset "
    "(<home>/bin/llama-server, scripts/build_llama_tar.sh) or the vendor build "
    "(vendor/llama.cpp, tag b11461) via llama.server/GOLDMINE_LLAMA_SERVER"
)

#: transient network conditions for retry-once (never 4xx)
_TRANSIENT = (
    urllib.error.URLError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
    OSError,
)


def _goldmine_home(cfg) -> Path:
    home = getattr(cfg, "home", None)
    if home is None:
        env = os.environ.get("GOLDMINE_HOME")
        home = Path(env) if env else DEFAULT_GOLDMINE_HOME
    return Path(home).expanduser()


def home_server_path(cfg) -> Path:
    """`<goldmine home>/bin/llama-server` — the release-asset install location
    (05 §4.3/§4.7). Between config and vendor in the resolution order so an
    installed copy works without a checkout."""
    return _goldmine_home(cfg) / "bin" / "llama-server"


def _in_repo_checkout() -> bool:
    """The repo-relative vendor path is only a candidate when running from a
    checkout (pyproject.toml at REPO_ROOT) — for pip-installed copies REPO_ROOT
    is site-packages and the vendor tree cannot exist there (05 §4.7)."""
    return (REPO_ROOT / "pyproject.toml").is_file()


def resolve_server_path(cfg) -> tuple[Path | None, str]:
    """Server binary resolution order (binding scope, v2 05 §4.7): env
    GOLDMINE_LLAMA_SERVER / config `llama.server` win when explicitly
    configured (cfg.server_configured, tracked by config.load), then
    `<home>/bin/llama-server` (release asset install), then the repo-relative
    vendor build (checkout only), then the brew prefix path. Returns
    (path | None, source in {"env/config", "home", "vendor", "brew",
    "explicit", "none"}).

    P3-14: an explicitly configured path that does NOT exist resolves to
    (None, "explicit") — the home/vendor/brew fallback applies ONLY when
    nothing was configured, never as a silent repair of a bad explicit path.
    """
    configured = Path(cfg.server).expanduser()
    if getattr(cfg, "server_configured", False):
        if configured.is_file():
            return configured, "env/config"
        return None, "explicit"
    candidates = (
        ("home", home_server_path(cfg)),
        ("vendor", VENDOR_SERVER if _in_repo_checkout() else None),
        ("brew", BREW_SERVER),
    )
    for source, path in candidates:
        if path is not None and path.is_file():
            return path, source
    return None, "none"


def default_model_candidates(cfg) -> list[Path]:
    """Default-model resolution legs beyond env/config (which config.load
    folds into cfg.model + model_configured): the legacy built-in default when
    present (dev checkout — keeps every historical configuration working),
    then the model-manager locations `<home>/models/<pinned>` and the repo
    `models/` copy (task order: home before repo)."""
    from ..model_manifest import DEFAULT_EMBEDDING_KEY, by_key

    entry = by_key(DEFAULT_EMBEDDING_KEY)
    if entry is None:  # pragma: no cover - the manifest always pins it
        return []
    home_copy = _goldmine_home(cfg) / "models" / entry.name
    repo_copy = REPO_MODELS / entry.name
    return [home_copy, repo_copy]


def resolve_model_path(cfg) -> Path:
    """Model resolution (v2 task order): env GOLDMINE_MODEL > config
    llama.model (model_configured) > legacy default when present >
    <home>/models pinned copy > repo models/ pinned copy. Explicit wins even
    when missing (same philosophy as the server path); the last resort is the
    configured value itself so error messages name the user-visible path."""
    configured = Path(cfg.model).expanduser()
    if getattr(cfg, "model_configured", False):
        return configured
    if configured.is_file():
        return configured
    for candidate in default_model_candidates(cfg):
        if candidate.is_file():
            return candidate
    return configured


def probe_version(binary: Path) -> str | None:
    """`llama-server --version` (3 s timeout); None when it fails.

    The real llama-server prints the version banner to STDERR (ggml logging),
    stdout is the fallback; the `version:` line wins when present."""
    # Capture quirk (brew 0.5.0 b11146, observed 2026-10-12): with stdout and
    # stderr on SEPARATE pipes the process hangs; a single merged capture
    # exits instantly. Merge, then parse.
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=remaining_timeout(3.0),
        )
    except RequestTimeout:
        raise
    except (OSError, subprocess.TimeoutExpired):
        check_deadline()
        return None
    check_deadline()
    lines = [
        line.strip()
        for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    for line in lines:
        if line.startswith("version:"):
            return line
    return lines[0] if lines else None


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


class LlamaServerBackend(EmbeddingBackend):
    backend_id = "llama"
    fingerprint_without_startup = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.server_path: Path | None = None
        self.server_source = "none"
        self.build_string: str | None = None
        self._proc: subprocess.Popen | None = None
        self._port: int | None = None
        self._ring: collections.deque[str] = collections.deque(maxlen=200)
        self._started = False
        self._start_lock = threading.Lock()
        self._sha256_cache: str | None = None
        self.model_spawn_count = 0
        self.embedding_http_requests = 0

    # ------------------------------------------------------------- identity

    @property
    def dim(self) -> int:
        return VEC_DIM

    def capability(self) -> Capability:
        # /v1/embeddings is TEXT-ONLY in llama.cpp (mmproj exists but images and
        # audio are not embeddable through this endpoint) — honest capability.
        return Capability(text=True, image=False, audio=False)

    def model_path(self) -> Path:
        return resolve_model_path(self.cfg)

    def model_sha256(self) -> str:
        """sha256 of the GGUF with a `<model>.sha256` sidecar cache; recomputed
        when size or mtime changed (MODULE_MAP contract)."""
        check_deadline()
        model = self.model_path()
        if self._sha256_cache is not None:
            return self._sha256_cache
        sidecar = model.with_name(model.name + ".sha256")
        try:
            st = model.stat()
            cached = json.loads(sidecar.read_text())
            if cached.get("size") == st.st_size and cached.get("mtime_ns") == st.st_mtime_ns:
                self._sha256_cache = str(cached["sha256"])
                return self._sha256_cache
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        from ..util import sha256_file

        digest = sha256_file(model)
        try:
            sidecar.write_text(json.dumps({
                "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest,
            }))
        except OSError:
            pass
        self._sha256_cache = digest
        return digest

    def fingerprint_fields(self) -> dict:
        model = self.model_path()
        sha = self.model_sha256() if model.is_file() else "missing"
        return {
            "model_id": model.stem,
            "model_sha256": sha,
            "dim": VEC_DIM,
            "prompts": PROMPT_SCHEME,
            "pooling": "server:mean",  # spawned with --pooling mean
            "normalization": "l2_post",  # client re-normalizes defensively
        }

    def max_input_tokens(self) -> int | None:
        return int(self.cfg.ctx)

    # ------------------------------------------------------------ lifecycle

    def stderr_tail(self) -> str:
        """Last ~4 KB of combined child output (ring buffer) for error reports."""
        return "".join(list(self._ring))[-4096:]

    def _verify_model(self) -> None:
        model = self.model_path()
        if not model.is_file():
            raise BackendStartupError(
                f"model file not found: {model}",
                "set llama.model / GOLDMINE_MODEL, or `goldmine model install "
                "embeddinggemma-2-Q8_0` into <home>/models (repo default: "
                "models/embeddinggemma-2-Q8_0.gguf)",
            )
        sha = self.model_sha256()
        if model.name == PINNED_MODEL_BASENAME and sha != PINNED_MODEL_SHA256:
            raise BackendStartupError(
                f"model {model.name} sha256 mismatch: expected {PINNED_MODEL_SHA256}, "
                f"got {sha} — the pinned GGUF changed; rebuild the index knowingly "
                "or restore the pinned file (models/SHA256SUMS)",
            )

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _port_has_foreign_server(self, port: int) -> bool:
        """Probe-then-start: a healthy llama-server already on this port means we
        pick another port (never adopt, never kill processes we did not spawn)."""
        try:
            return http_request(f"http://127.0.0.1:{port}/health", timeout_s=0.5, max_bytes=2000).status == 200
        except RequestTimeout:
            raise
        except OSError:
            check_deadline()
            return False

    def _drain(self, stream) -> None:
        try:
            for line in iter(stream.readline, b""):
                self._ring.append(line.decode("utf-8", "replace")[:2000])
        except (OSError, ValueError):
            pass  # closed underneath us during shutdown
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _spawn(self, port: int) -> subprocess.Popen:
        check_deadline()
        argv = [
            str(self.server_path),
            "--embeddings", "--pooling", "mean",
            "-m", str(self.model_path()),
            # 04 §6.2 measured: -np beyond 2 buys nothing (np 1/2/4/8/16 all
            # within ±5% — embedding is GPU-compute-bound) and only costs KV
            # memory. Fixed 2 slots; cfg.batch stays the CLIENT-side batch
            # (latency granularity), never mapped onto -np.
            "-np", str(SERVER_SLOTS),
            # b11461 treats -c as the TOTAL KV cache across -np parallel
            # slots, so the total must be slots*ctx for every slot to see the
            # configured per-request ctx (2048/16 once left 256/request and
            # 400-rejected ~450-token chunks).
            "-c", str(int(self.cfg.ctx) * SERVER_SLOTS),
            # n_batch is the physical compute batch; a single embedding input
            # cannot span ubatches (mean pooling needs the whole sequence), so
            # n_ubatch must cover the largest input. The server clamps
            # n_batch down to n_ubatch, and slots then serialize — correct,
            # just serial.
            "-b", str(int(self.cfg.ctx) * SERVER_SLOTS),
            "-ub", str(int(self.cfg.ctx)),
            "--host", "127.0.0.1", "--port", str(port),
        ]
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        self.model_spawn_count += 1
        return proc

    def _wait_healthy(self, proc: subprocess.Popen, port: int, deadline_s: float) -> bool:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            check_deadline()
            if proc.poll() is not None:
                return False  # child exited: surfaced by the caller
            try:
                response = http_request(url, timeout_s=max(0.001, min(1.0, deadline - time.monotonic())), max_bytes=2000)
                if response.status == 200:
                    return True
            except RequestTimeout:
                raise
            except OSError:
                check_deadline()
            deadline_sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return False

    def startup(self) -> None:
        """Resolve -> verify -> spawn -> /health -> dim probe. Idempotent.
        Raises BackendStartupError (never starts half-way). Lock-guarded:
        count_tokens/token_pieces run on extraction worker threads while the
        main thread embeds (D3), so concurrent first-starts must not
        double-spawn."""
        check_deadline()
        if self._started:
            return
        with deadline_lock(self._start_lock):
            try:
                return self._startup_locked()
            except BaseException:
                self.close()
                raise

    def _startup_locked(self) -> None:
        check_deadline()
        if self._started:
            return
        self._ring.clear()

        server, source = resolve_server_path(self.cfg)
        if server is None:
            if source == "explicit":
                # P3-14: a configured path that does not exist is an error with
                # a fix hint — never a silent home/vendor/brew fallback.
                raise BackendStartupError(
                    f"configured llama-server not found: {self.cfg.server}",
                    "fix llama.server / GOLDMINE_LLAMA_SERVER, or unset it to "
                    "use the home/vendor/brew fallback",
                )
            raise BackendStartupError(
                "llama-server binary not found (looked at "
                f"{self.cfg.server}, {home_server_path(self.cfg)}, "
                f"{VENDOR_SERVER}, {BREW_SERVER})",
                "install the prebuilt release asset into <home>/bin/llama-server "
                "(scripts/build_llama_tar.sh builds it locally), or set "
                "llama.server / GOLDMINE_LLAMA_SERVER",
            )
        self.server_path, self.server_source = server, source
        if source == "brew" or (source == "env/config" and _is_brew_path(server)):
            logger.warning("%s", BREW_CANNOT_LOAD_NOTE)
        self.build_string = probe_version(server)
        if self.build_string is None:
            raise BackendStartupError(
                f"{server} exists but `--version` failed — not a usable llama-server",
            )
        self._verify_model()

        last_error = "unknown startup failure"
        for attempt in range(1, 4):  # port-bind race mitigation: 3 attempts
            check_deadline()
            port = self._free_port()
            while self._port_has_foreign_server(port):  # never adopt a foreign server
                logger.info("llama: port %d already serves a healthy server; picking another", port)
                port = self._free_port()
            check_deadline()
            proc = self._spawn(port)
            self._proc, self._port = proc, port
            threads = [
                threading.Thread(target=self._drain, args=(stream,), daemon=True)
                for stream in (proc.stdout, proc.stderr)
            ]
            for thread in threads:
                thread.start()
            if self._wait_healthy(proc, port, float(self.cfg.startup_timeout)):
                try:
                    probe = self._post_embeddings(["goldmine startup dim probe"])
                except EmbeddingError as exc:
                    self._kill()
                    last_error = f"startup probe embed failed: {exc}"
                    continue
                if probe.shape != (1, VEC_DIM):
                    self._kill()
                    last_error = (
                        f"model returned {probe.shape[1]}-dim vectors, expected {VEC_DIM}"
                    )
                    continue
                self._started = True
                logger.info(
                    "llama: server ready on 127.0.0.1:%d (%s, %s)",
                    port, self.build_string, source,
                )
                return
            # failed attempt: reap the child, capture why, retry on a new port
            exited = proc.poll() is not None
            self._kill()
            last_error = (
                f"server exited during startup (rc={proc.returncode})"
                if exited
                else f"not healthy after {self.cfg.startup_timeout:g}s"
            )
            logger.warning("llama: startup attempt %d failed: %s", attempt, last_error)
        raise BackendStartupError(
            f"llama-server failed to start after 3 attempts: {last_error}",
        )

    def _kill(self) -> None:
        """SIGTERM -> 5 s -> SIGKILL. Only OUR Popen child, never pkill."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                deadline = current_deadline()
                grace = 5.0 if deadline is None else max(0.0, min(0.25, deadline.expires_at - time.monotonic()))
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5.0 if current_deadline() is None else 1.0)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass

    def close(self) -> None:
        self._started = False
        self._kill()

    # ------------------------------------------------------------ embedding

    def _post_tokenize(self, content: str, *, add_special: bool,
                       with_pieces: bool):
        """One POST /tokenize (single-content endpoint, serial localhost HTTP
        — ~0.2-1 ms/request measured class). Returns the parsed `tokens` list
        (ids, or {"id","piece"} objects when with_pieces). Raises
        EmbeddingError/_TransientEmbeddingError like the embeddings path."""
        check_deadline()
        assert self._port is not None
        body = json.dumps({
            "content": content,
            "add_special": add_special,
            "with_pieces": with_pieces,
        }).encode("utf-8")
        try:
            response = http_request(
                f"http://127.0.0.1:{self._port}/tokenize", data=body,
                headers={"Content-Type": "application/json"},
                timeout_s=min(float(self.cfg.request_timeout), 30.0),
            )
            payload = json.loads(response.body)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            if exc.code in (500, 502, 503, 504):
                raise _TransientEmbeddingError(f"HTTP {exc.code}: {detail.strip()}")
            raise EmbeddingError(f"tokenize HTTP {exc.code}", detail.strip())
        except RequestTimeout:
            raise
        except _TRANSIENT as exc:
            check_deadline()
            raise _TransientEmbeddingError(f"connection error: {exc}") from exc
        except ValueError as exc:
            raise EmbeddingError("invalid tokenize HTTP response", str(exc)) from exc
        check_deadline()
        tokens = payload.get("tokens")
        if not isinstance(tokens, list):
            raise EmbeddingError(f"unexpected tokenize payload shape: {payload!r}"[:400])
        return tokens

    def count_tokens(self, texts: list[str]) -> list[int] | None:
        """Exact counts via POST /tokenize with add_special=true (the embeddings
        endpoint tokenizes inputs with specials — verified against the vendored
        b11461 handler). None on any failure -> estimator fallback (C6)."""
        if not texts:
            return []
        try:
            self.startup()
            return [
                len(self._retry(self._post_tokenize, t, add_special=True,
                                with_pieces=False))
                for t in texts
            ]
        except RequestTimeout:
            raise
        except (EmbeddingError, OSError, ValueError) as exc:
            check_deadline()
            logger.debug("llama: count_tokens unavailable (%s) — estimating", exc)
            return None

    def token_pieces(self, text: str) -> "list[bytes] | None":
        """Token pieces via POST /tokenize with_pieces=true, add_special=false
        (content-only: pieces must concatenate back to the text for the
        chunker's offset alignment). Byte-array pieces (byte-fallback tokens
        the server emits as [b0, b1, ...] when not valid utf-8) become raw
        bytes. None on any failure -> estimator fallback."""
        try:
            self.startup()
            tokens = self._retry(self._post_tokenize, text, add_special=False,
                                 with_pieces=True)
        except RequestTimeout:
            raise
        except (EmbeddingError, OSError, ValueError) as exc:
            check_deadline()
            logger.debug("llama: token_pieces unavailable (%s) — estimating", exc)
            return None
        pieces: list[bytes] = []
        for item in tokens:
            if not isinstance(item, dict):
                return None
            piece = item.get("piece")
            if isinstance(piece, str):
                pieces.append(piece.encode("utf-8"))
            elif isinstance(piece, list):
                try:
                    pieces.append(bytes(int(b) & 0xFF for b in piece))
                except (TypeError, ValueError):
                    return None
            else:
                return None
        return pieces

    def _retry(self, fn, *args, **kwargs):
        """Retry-once wrapper for cheap tokenize calls (mirrors _embed_batch)."""
        check_deadline()
        try:
            return fn(*args, **kwargs)
        except _TransientEmbeddingError as first:
            check_deadline()
            if self._proc is not None and self._proc.poll() is not None:
                raise EmbeddingError("llama-server died mid-run", self.stderr_tail()) from first
            logger.warning("llama: transient tokenize failure (%s) — retrying once", first)
            try:
                return fn(*args, **kwargs)
            except _TransientEmbeddingError as second:
                raise EmbeddingError(
                    f"tokenize failed after one retry: {second}", self.stderr_tail()
                ) from second

    def _post_embeddings(self, texts: list[str]) -> np.ndarray:
        """One POST /v1/embeddings; returns (len(texts), dim) float32 (raw)."""
        check_deadline()
        assert self._port is not None
        body = json.dumps({"input": list(texts)}).encode("utf-8")
        try:
            self.embedding_http_requests += 1
            response = http_request(
                f"http://127.0.0.1:{self._port}/v1/embeddings", data=body,
                headers={"Content-Type": "application/json"}, timeout_s=float(self.cfg.request_timeout),
            )
            payload = json.loads(response.body)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            if exc.code in (500, 502, 503, 504):
                raise _TransientEmbeddingError(f"HTTP {exc.code}: {detail.strip()}")
            raise EmbeddingError(f"embeddings HTTP {exc.code}", detail.strip())
        except RequestTimeout:
            raise
        except _TRANSIENT as exc:
            check_deadline()
            raise _TransientEmbeddingError(f"connection error: {exc}") from exc
        except ValueError as exc:
            raise EmbeddingError("invalid embeddings HTTP response", str(exc)) from exc
        check_deadline()
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError(
                f"unexpected embeddings payload shape ({len(data or [])} rows "
                f"for {len(texts)} inputs)",
                self.stderr_tail(),
            )
        rows = sorted(data, key=lambda item: int(item.get("index", 0)))
        matrix = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
        if matrix.shape != (len(texts), VEC_DIM):
            raise EmbeddingError(
                f"embeddings returned shape {matrix.shape}, expected "
                f"({len(texts)}, {VEC_DIM})",
                self.stderr_tail(),
            )
        return matrix

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        """One batch with retry-once on transient connection/server errors;
        a server that died mid-run is a hard EmbeddingError (SIGINT path kills
        the child, so a dead child after cancellation must not respawn)."""
        check_deadline()
        assert self._proc is not None
        try:
            return self._post_embeddings(texts)
        except _TransientEmbeddingError as first:
            check_deadline()
            if self._proc.poll() is not None:
                raise EmbeddingError(
                    "llama-server died mid-run",
                    self.stderr_tail(),
                ) from first
            logger.warning("llama: transient failure (%s) — retrying once", first)
            try:
                return self._post_embeddings(texts)
            except _TransientEmbeddingError as second:
                raise EmbeddingError(
                    f"embeddings failed after one retry: {second}",
                    self.stderr_tail(),
                ) from second

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        check_deadline()
        if not texts:
            return np.zeros((0, VEC_DIM), dtype=np.float32)
        self.startup()  # idempotent; raises BackendStartupError when unusable
        batch = max(1, int(self.cfg.batch))
        out = np.zeros((len(texts), VEC_DIM), dtype=np.float32)
        for start in range(0, len(texts), batch):
            check_deadline()
            part = self._embed_batch(list(texts[start:start + batch]))
            out[start:start + part.shape[0]] = part
        return _l2_normalize(out)  # defensive; record 'l2_post' in fingerprint

    def embed_image(self, path: Path) -> np.ndarray:
        raise NotSupported(
            "llama /v1/embeddings is text-only — images are metadata-only under "
            "this backend (use --backend mock for image vectors)"
        )

    def embed_audio_wav(self, wav: bytes) -> np.ndarray:
        raise NotSupported(
            "llama /v1/embeddings is text-only — audio is metadata-only under "
            "this backend (use --backend mock for audio vectors)"
        )

    # -------------------------------------------------------------- report

    def describe(self) -> dict:
        server, source = resolve_server_path(self.cfg)
        model = self.model_path()
        info: dict = {
            "id": self.backend_id,
            "dim": VEC_DIM,
            "capability": {"text": True, "image": False, "audio": False},
            "server": str(server) if server else None,
            "server_source": source,
            "model": str(model),
            "note": BREW_CANNOT_LOAD_NOTE,
        }
        available = server is not None and model.is_file()
        if server is None:
            info["fix_hint"] = (
                "fix llama.server / GOLDMINE_LLAMA_SERVER (the configured path "
                "does not exist), or unset it to use the home/vendor/brew "
                "fallback"
                if source == "explicit"
                else "install the release asset into <home>/bin/llama-server "
                "(scripts/build_llama_tar.sh), or set llama.server / "
                "GOLDMINE_LLAMA_SERVER"
            )
        if server is not None:
            self.build_string = probe_version(server)
            info["build"] = self.build_string
        if model.is_file():
            try:
                info["model_sha256"] = self.model_sha256()
            except OSError as exc:
                info["model_sha256"] = None
                info["detail"] = f"model unreadable: {exc}"
                available = False
        else:
            info["fix_hint"] = (
                "model file missing — `goldmine model install "
                "embeddinggemma-2-Q8_0` (or see models/SHA256SUMS in the repo)"
            )
        info["available"] = available
        return info


def _is_brew_path(path: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path("/opt/homebrew"))
    except (OSError, ValueError):
        return False


class _TransientEmbeddingError(EmbeddingError):
    """Internal marker: worth one retry (connection refused/reset, 5xx)."""
