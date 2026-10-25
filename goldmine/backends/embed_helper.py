"""MediaEmbedder — the resident goldmine-embed helper sidecar (digup-plan-002).

Owns the multimodal C helper the same way LlamaServerBackend owns llama-server:
resolve (env/config explicit > <home>/bin > repo vendor build) → spawn → wait
the `ready` event → serve line-delimited JSON requests → kill ONLY our child.
Media vectors NEVER carry a prompt (D7); the helper does the resize, the mtmd
encode and the mean-pool + L2 (design helper-design-v1.md §2 + amendments-001
B3/B4, M1/M2, M11).

Fingerprint relevance: the parent backend emits the media extras (media
preproc string, mmproj sha, budgets, routing) exactly when `resolvable()` is
true, so a text-only install stays fingerprint-byte-identical to pre-multimodal
builds (the mock's discipline, mock.py:31-34).
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import os
import select
import signal
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from ..deadline import (
    RequestTimeout, check_deadline, current_deadline, deadline_sleep,
    remaining_timeout,
)
from ..ingest.media import LEGAL_BUDGETS
from .base import BackendStartupError, EmbeddingError

logger = logging.getLogger("goldmine.backends.embed_helper")

VEC_DIM = 768

#: The helper's self-identification (matches --version / ready.helper).
HELPER_HANDSHAKE = "goldmine-embed 1"
HELPER_PROTO = 1

#: One protocol line may not exceed this (a 30 s WAV is ~1.3 MB base64; the
#: bound refuses corruption, and the helper discards oversize lines alive).
MAX_LINE_BYTES = 16 << 20

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_HELPER = (
    REPO_ROOT / "vendor" / "llama.cpp" / "build-static" / "bin" / "goldmine-embed"
)

#: sha256 of the pinned mmproj (model_manifest mmproj-embeddinggemma-2-Q8_0);
#: enforced when the resolved file carries the pinned basename.
PINNED_MMPROJ_BASENAME = "mmproj-embeddinggemma-2-Q8_0.gguf"
PINNED_MMPROJ_SHA256 = (
    "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f"
)

_MMPROJ_NAME = "mmproj-embeddinggemma-2-Q8_0"


def _goldmine_home(cfg) -> Path:
    home = getattr(cfg, "home", None)
    if home is None:
        env = os.environ.get("GOLDMINE_HOME")
        home = Path(env) if env else Path(
            "~/Library/Application Support/goldmine"
        )
    return Path(home).expanduser()


def _in_repo_checkout() -> bool:
    return (REPO_ROOT / "pyproject.toml").is_file()


def home_helper_path(cfg) -> Path:
    """`<goldmine home>/bin/goldmine-embed` — the release-asset install leg."""
    return _goldmine_home(cfg) / "bin" / "goldmine-embed"


def resolve_helper_path(cfg) -> tuple[Path | None, str]:
    """Helper binary resolution (mirrors resolve_server_path, llama.py): an
    explicitly configured path wins even when missing (no silent fallback);
    otherwise home > repo vendor build-static. No brew leg exists."""
    if getattr(cfg, "embed_helper_configured", False):
        configured = Path(cfg.embed_helper).expanduser()
        if configured.is_file():
            return configured, "env/config"
        return None, "explicit"
    candidates = (
        ("home", home_helper_path(cfg)),
        ("vendor", VENDOR_HELPER if _in_repo_checkout() else None),
    )
    for source, path in candidates:
        if path is not None and path.is_file():
            return path, source
    return None, "none"


def resolve_mmproj_path(cfg) -> Path:
    """mmproj resolution (mirrors resolve_model_path): explicit wins even when
    missing; the last resort names the user-visible path for error messages."""
    if getattr(cfg, "mmproj_configured", False):
        return Path(cfg.mmproj).expanduser()
    for candidate in (
        _goldmine_home(cfg) / "models" / f"{_MMPROJ_NAME}.gguf",
        REPO_ROOT / "models" / f"{_MMPROJ_NAME}.gguf",
    ):
        if candidate.is_file():
            return candidate
    return Path(cfg.mmproj).expanduser()


def _sha256_cached(path: Path, cache: dict) -> str:
    """sha256 with a `<file>.sha256` sidecar (size+mtime guarded), shared
    scheme with llama.model_sha256 so fingerprinting stays cheap."""
    if cache.get("path") == path:
        return cache["sha256"]
    sidecar = path.with_name(path.name + ".sha256")
    try:
        import json as _json

        st = path.stat()
        cached = _json.loads(sidecar.read_text())
        if cached.get("size") == st.st_size and cached.get("mtime_ns") == st.st_mtime_ns:
            cache.update(path=path, sha256=str(cached["sha256"]))
            return cache["sha256"]
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    from ..util import sha256_file

    digest = sha256_file(path)
    try:
        st = path.stat()
        sidecar.write_text(json.dumps({
            "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest,
        }))
    except OSError:
        pass
    cache.update(path=path, sha256=digest)
    return digest


class MediaEmbedder:
    """Lazy sidecar: nothing is spawned until the first media embed. All
    request I/O is single-threaded by contract (commit embeds media serially);
    only the pipe drains run on daemon threads (llama.py _drain pattern)."""

    def __init__(self, cfg, model_path):
        self.cfg = cfg
        self._model_path = Path(model_path)
        self._proc: subprocess.Popen | None = None
        self._started = False
        self._start_lock = threading.Lock()
        self._ring: collections.deque[str] = collections.deque(maxlen=200)
        self._caps: dict = {}
        self._next_id = 0
        self._sha_cache: dict = {}
        self.load_seconds: float | None = None

    # ------------------------------------------------------------- resolve

    def resolvable(self) -> bool:
        """Static (no spawn): helper AND mmproj AND text model all resolve."""
        helper, _ = resolve_helper_path(self.cfg)
        return (
            helper is not None
            and resolve_mmproj_path(self.cfg).is_file()
            and self._model_path.is_file()
        )

    def capability(self) -> tuple[bool, bool]:
        """(image, audio) — True/True while resolvable (the pinned mmproj
        carries both encoders, measured vision=1 audio=1); refined by the
        ready event after startup, never by a spawn here."""
        if not self.resolvable():
            return False, False
        if self._caps:
            return bool(self._caps.get("vision")), bool(self._caps.get("audio"))
        return True, True

    def mmproj_sha256(self) -> str:
        path = resolve_mmproj_path(self.cfg)
        if not path.is_file():
            return "missing"
        return _sha256_cached(path, self._sha_cache)

    def _verify_mmproj(self) -> None:
        path = resolve_mmproj_path(self.cfg)
        if not path.is_file():
            raise BackendStartupError(
                f"mmproj file not found: {path}",
                "set llama.mmproj / GOLDMINE_MMPROJ, or `goldmine model install "
                "mmproj-embeddinggemma-2-Q8_0` into <home>/models "
                "(repo default: models/mmproj-embeddinggemma-2-Q8_0.gguf)",
            )
        if path.name == PINNED_MMPROJ_BASENAME:
            sha = self.mmproj_sha256()
            if sha != PINNED_MMPROJ_SHA256:
                raise BackendStartupError(
                    f"mmproj {path.name} sha256 mismatch: expected "
                    f"{PINNED_MMPROJ_SHA256}, got {sha} — restore the pinned "
                    "file (models/SHA256SUMS) or rebuild knowingly",
                )

    # ------------------------------------------------------------ lifecycle

    def stderr_tail(self) -> str:
        return "".join(list(self._ring))[-4096:]

    def _drain(self, stream, ring: bool = False) -> None:
        try:
            for line in iter(stream.readline, b""):
                if ring:
                    self._ring.append(line.decode("utf-8", "replace")[:2000])
        except (OSError, ValueError):
            pass  # closed underneath us during shutdown
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def startup(self) -> None:
        """Spawn → ready event → caps cache. Idempotent; BackendStartupError
        on any failure (never half-started). A respawn after a mid-run death
        is exactly this path again (next request, never inside a failure)."""
        check_deadline()
        if self._started:
            return
        with self._start_lock:
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
        helper, source = resolve_helper_path(self.cfg)
        if helper is None:
            if source == "explicit":
                raise BackendStartupError(
                    f"configured goldmine-embed helper not found: "
                    f"{self.cfg.embed_helper}",
                    "fix llama.embed_helper / GOLDMINE_EMBED_HELPER, or unset "
                    "it to use the home/vendor fallback",
                )
            raise BackendStartupError(
                "goldmine-embed helper binary not found (looked at "
                f"{self.cfg.embed_helper}, {home_helper_path(self.cfg)}, "
                f"{VENDOR_HELPER})",
                "build it with scripts/build_embed_helper.sh (dev) or install "
                "the release asset into <home>/bin/goldmine-embed",
            )
        self._verify_mmproj()
        argv = [
            str(helper),
            "--model", str(self._model_path),
            "--mmproj", str(resolve_mmproj_path(self.cfg)),
            "--ctx", "2048",
        ]
        check_deadline()
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._proc = proc
        # ONLY stderr is drained by a thread (amendments M1): stdout is the
        # PROTOCOL channel and is read by the request loop (_read_line) — a
        # stdout drain thread would eat the responses (llama.py can drain both
        # because its pipes are pure logs over HTTP).
        stderr_drain = threading.Thread(
            target=self._drain, args=(proc.stderr, True), daemon=True
        )
        stderr_drain.start()
        ready = self._read_line(
            float(self.cfg.startup_timeout), what="ready event"
        )
        try:
            event = json.loads(ready)
        except ValueError as exc:
            raise BackendStartupError(
                f"helper ready line is not JSON: {ready[:200]!r}",
            ) from exc
        if (
            event.get("event") != "ready"
            or event.get("helper") != HELPER_HANDSHAKE
            or int(event.get("proto", -1)) != HELPER_PROTO
        ):
            raise BackendStartupError(
                f"unexpected helper ready line: {ready[:200]!r}",
                self.stderr_tail(),
            )
        if int(event.get("dim", -1)) != VEC_DIM:
            raise BackendStartupError(
                f"helper reports dim {event.get('dim')}, expected {VEC_DIM}",
            )
        self._caps = {
            "vision": bool(event.get("vision")),
            "audio": bool(event.get("audio")),
            "sample_rate": event.get("sample_rate"),
        }
        self.load_seconds = event.get("load_seconds")
        self._started = True
        logger.info(
            "embed-helper: ready (%s, load %ss, vision=%s audio=%s)",
            source, self.load_seconds,
            self._caps["vision"], self._caps["audio"],
        )

    def _kill(self) -> None:
        """SIGTERM → 5 s → SIGKILL. Only OUR Popen child, never pkill."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                deadline = current_deadline()
                grace = 5.0 if deadline is None else max(
                    0.0, min(0.25, deadline.expires_at - time.monotonic())
                )
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5.0 if current_deadline() is None else 1.0)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass

    def close(self) -> None:
        self._started = False
        self._caps = {}
        self._kill()

    # ------------------------------------------------------------ protocol

    def _read_line(self, timeout_s: float, *, what: str) -> str:
        """One \\n-terminated stdout line under a timeout; EOF or a dead child
        is a hard EmbeddingError (the caller decides which wording).

        Raw os.read on the fd — NEVER the BufferedReader: a buffered read(1)
        can drain the whole pipe into user space, after which level-triggered
        poll never reports POLLIN again (observed: ready line eaten by the
        BufferedReader, poll starves, startup times out)."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise EmbeddingError("goldmine-embed is not running")
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + timeout_s
        chunks: list[bytes] = []
        total = 0
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        try:
            while True:
                check_deadline()
                if proc.poll() is not None and not chunks:
                    raise EmbeddingError(
                        "goldmine-embed died mid-run",
                        self.stderr_tail(),
                    )
                left = deadline - time.monotonic()
                if left <= 0:
                    raise EmbeddingError(
                        f"helper {what} timed out after {timeout_s:g}s",
                        self.stderr_tail(),
                    )
                if not poller.poll(min(left, 0.5) * 1000):
                    continue
                try:
                    data = os.read(fd, 65536)
                except InterruptedError:  # pragma: no cover - retried by CPython
                    continue
                if not data:
                    raise EmbeddingError(
                        "goldmine-embed died mid-run",
                        self.stderr_tail(),
                    )
                newline = data.find(b"\n")
                if newline >= 0:
                    chunks.append(data[:newline])
                    return b"".join(chunks).decode("utf-8", "replace")
                chunks.append(data)
                total += len(data)
                if total > MAX_LINE_BYTES:
                    raise EmbeddingError(
                        f"helper {what} line exceeds {MAX_LINE_BYTES} bytes",
                    )
        finally:
            poller.unregister(fd)

    def _write_line(self, line: str) -> None:
        """Full write under the request deadline; EPIPE means the child is
        gone — surfaced as a hard error."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise EmbeddingError("goldmine-embed is not running")
        fd = proc.stdin.fileno()
        data = line.encode("utf-8") + b"\n"
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:  # pragma: no cover
                continue
            except (BrokenPipeError, OSError) as exc:
                raise EmbeddingError(
                    "goldmine-embed died mid-run", self.stderr_tail(),
                ) from exc
            view = view[written:]

    def _request(self, payload: dict) -> dict:
        """One round trip. No retry: a resident helper either answers or is
        broken; retrying would double a 5-10 s GPU encode (design §4.5)."""
        check_deadline()
        self.startup()
        self._next_id += 1
        payload = {"id": self._next_id, **payload}
        timeout_s = min(
            float(self.cfg.request_timeout),
            max(remaining_timeout(float(self.cfg.request_timeout)), 0.001),
        )
        self._write_line(json.dumps(payload, ensure_ascii=False))
        response = self._read_line(timeout_s, what=f"op {payload.get('op')!r}")
        try:
            parsed = json.loads(response)
        except ValueError as exc:
            raise EmbeddingError(
                f"helper response is not JSON: {response[:200]!r}",
                self.stderr_tail(),
            ) from exc
        if parsed.get("id") != payload["id"]:
            raise EmbeddingError(
                f"helper response id mismatch: {response[:200]!r}",
            )
        return parsed

    def ping(self) -> bool:
        return bool(self._request({"op": "ping"}).get("ok"))

    def caps(self) -> dict:
        return dict(self._request({"op": "caps"}))

    # ------------------------------------------------------------ embedding

    def embed_image(self, path: Path, budget: int) -> np.ndarray:
        if int(budget) not in LEGAL_BUDGETS:
            raise EmbeddingError(
                f"budget {budget} not in {list(LEGAL_BUDGETS)}",
            )
        if not self._caps.get("vision", True):
            raise EmbeddingError(
                "mmproj lacks vision support (ready event reported "
                "vision=false)",
            )
        raw = base64.b64encode(bytes(path)).decode("ascii")
        return self._vec_response(self._request({
            "op": "embed", "kind": "image", "budget": int(budget),
            "path_b64": raw,
        }))

    def embed_audio_wav(self, wav: bytes) -> np.ndarray:
        if not self._caps.get("audio", True):
            raise EmbeddingError(
                "mmproj lacks audio support (ready event reported "
                "audio=false)",
            )
        return self._vec_response(self._request({
            "op": "embed", "kind": "audio",
            "wav_b64": base64.b64encode(wav).decode("ascii"),
        }))

    @staticmethod
    def _vec_response(response: dict) -> np.ndarray:
        if not response.get("ok"):
            error = response.get("error") or {}
            raise EmbeddingError(
                f"helper error {error.get('code', 'unknown')}: "
                f"{str(error.get('message', ''))[:300]}",
            )
        try:
            raw = base64.b64decode(response["vec_b64"], validate=True)
        except (KeyError, ValueError) as exc:
            raise EmbeddingError(
                f"helper response missing/invalid vec_b64: {response!r}"[:400],
            ) from exc
        if len(raw) != VEC_DIM * 4:
            raise EmbeddingError(
                f"helper vector is {len(raw)} bytes, expected {VEC_DIM * 4}",
            )
        return np.frombuffer(raw, dtype="<f4").astype(np.float32).copy()
