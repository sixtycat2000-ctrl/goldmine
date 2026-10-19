"""RemoteBackend — the `goldmine serve` client: line-JSON over a UDS (R8, D1/D2).

The daemon (goldmine/serve.py) owns ONE LlamaServerBackend; this module is the
thin client side of that seam. Contract highlights (spec docs/v2.1-r8-serve.md):

- Transport: AF_UNIX SOCK_STREAM, one JSON object per line, UTF-8, \\n-terminated.
  Every request `{"op": str, "id": str?, "pid": int?, ...params}`; every response
  `{"ok": true, "op": ..., "id": ..., **payload}` or
  `{"ok": false, ..., "error": {"kind", "message", "tail"}}`.
- `RemoteBackend` implements the EmbeddingBackend ABC so Searcher needs ZERO
  changes (search.py: 0 lines — the D1 invariant). `backend_id` stays "llama"
  (LOAD-BEARING: fingerprints.fp_build keys the fingerprint on it, so the
  fingerprint gate is byte-identical to a cold backend's). NOTE: this class is
  deliberately NOT registered in the backend registry — it is constructed only
  through connect_or_cold(), never via create_backend().
- Fingerprint parity: the handshake caches the daemon's fingerprint_fields (the
  daemon computes model_sha256 FRESH from file bytes — review F2 — and cold's
  fingerprint_fields uses the sidecar whose value equals the fresh hash of the
  same bytes), so fp_build(RemoteBackend) == fp_build(LlamaServerBackend) in the
  common case; pinned by test.
- Failure discipline (review F5): a serve failure must degrade latency, never
  lose the query. Mid-request failures retry ONCE on transient connection
  errors, then fall back to a cold LlamaServerBackend constructed here; a
  deterministic `model_not_resident` answer (guard cooldown) goes straight to
  cold. Exactly ONE stderr notice per fallback.
- L2 note: the daemon's backend normalizes rows with the same _l2_normalize as
  cold (a per-row op — batch boundaries cannot change a row), so THIS client
  must NOT normalize again; embed_texts returns the daemon rows verbatim. That
  bit-exactness is what the T8 byte-equality test (incl. the F10 cross-batch
  20-texts/batch-16 case) pins against cold.

F9 scope cuts honored here: `token_pieces()` returns None (op cut to G6 — the
chunker uses its estimator), and describe() is a stub pointing at
`goldmine serve status`.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import __version__
from ..config import Config, resolve_home
from .base import Capability, EmbeddingBackend, EmbeddingError, NotSupported
from .llama import VEC_DIM, LlamaServerBackend, resolve_model_path

logger = logging.getLogger("goldmine.backends.serve")

#: Wire protocol version (daemon refuses/handshake-fails on any mismatch).
PROTO = 1

#: Hard frame cap (D2): a longer line gets the CONNECTION CLOSED, no response.
MAX_LINE = 16 * 1024 * 1024

#: Embed caps (D2 as amended by F6): 64 texts AND 256 KiB per text. Count is
#: validated FIRST, so when both are violated the count error wins (F6).
EMBED_MAX_TEXTS = 64
EMBED_MAX_TEXT_BYTES = 256 * 1024

#: Client connect deadline for discovery probing (D2/§6: 0.5 s).
CONNECT_TIMEOUT_S = 0.5

#: Server closes a connection idle this long (D2); the client reconnects
#: transparently on the next request.
SERVER_IDLE_TIMEOUT_S = 30.0

#: macOS sun_path limit — validated before bind so a too-long tmp path fails
#: with an honest message instead of a raw OSError from the kernel.
SUN_PATH_MAX = 104

SOCKET_DIR_NAME = "goldmine-serve"
SOCKET_NAME = "serve.sock"


# ------------------------------------------------------------------ errors


class ServeUnusable(Exception):
    """A daemon WAS found (discovery/env) but the handshake failed — the
    client prints ONE stderr notice and cold-falls back. Absent daemon is a
    different path (silent, D1)."""


class ServeRequestError(Exception):
    """A request failed mid-flight (daemon `ok:false`, EOF, reset, deadline).

    `transient` marks connection-class failures worth the F5 retry-once;
    deterministic daemon answers (`bad_request`, `model_not_resident`) are not
    retried."""

    def __init__(self, message: str, *, kind: str = "connection",
                 tail: str = "", transient: bool = True):
        super().__init__(message)
        self.kind = kind
        self.tail = tail
        self.transient = transient


class LineTooLong(Exception):
    """The peer sent a line over MAX_LINE — close the connection (D2)."""


# ------------------------------------------------------------------- codec


def encode_line(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"


def send_line(sock: socket.socket, obj: dict) -> None:
    sock.sendall(encode_line(obj))


class LineReader:
    """Buffered line reader over a socket with the MAX_LINE cap (D2): reads
    chunks, splits on \\n; a line longer than the cap raises LineTooLong (the
    caller closes that connection)."""

    def __init__(self, sock: socket.socket, max_line: int = MAX_LINE):
        self._sock = sock
        self._max = max_line
        self._buf = bytearray()

    def readline(self) -> bytes | None:
        """One \\n-terminated line without the newline, or None at clean EOF."""
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl])
                del self._buf[: nl + 1]
                return line
            if len(self._buf) > self._max:
                raise LineTooLong(
                    f"line exceeds {self._max}-byte cap — closing connection"
                )
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                raise  # idle deadline: caller decides (server closes; client retries)
            if not chunk:
                return None  # EOF
            self._buf.extend(chunk)


def recv_response(sock: socket.socket, timeout_s: float) -> dict:
    """One response line as a dict. Raises ServeRequestError on EOF, reset,
    deadline, oversized line, or undecodable payload — all transient-class
    (the F5 retry-once then cold-fallback applies)."""
    sock.settimeout(timeout_s)
    try:
        reader = LineReader(sock)
        line = reader.readline()
        if line is None:
            raise ServeRequestError("daemon closed the connection (EOF)")
        if len(line) > MAX_LINE:
            raise ServeRequestError("response exceeds the line cap")
        payload = json.loads(line.decode("utf-8"))
    except socket.timeout as exc:
        raise ServeRequestError(
            f"daemon did not answer within {timeout_s:g}s"
        ) from exc
    except (OSError, LineTooLong) as exc:
        raise ServeRequestError(f"connection error: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServeRequestError(f"undecodable response: {exc}") from exc
    if not isinstance(payload, dict):
        raise ServeRequestError(f"response is not an object: {payload!r}"[:200])
    if not payload.get("ok"):
        _raise_ok_false(payload)
    return payload


def _raise_ok_false(payload: dict) -> None:
    """Turn an `ok:false` response into the raised ServeRequestError. The
    `transient` flag decides the F5 discipline: deterministic daemon answers
    (`bad_request` repeats identically; `model_not_resident` during a guard
    cooldown would just fail again) are NOT retried."""
    err = payload.get("error") or {}
    kind = str(err.get("kind") or "internal")
    raise ServeRequestError(
        str(err.get("message") or "daemon error"),
        kind=kind,
        tail=str(err.get("tail") or ""),
        transient=kind not in ("bad_request", "model_not_resident"),
    )


# -------------------------------------------------------------- discovery


@dataclass(frozen=True)
class Discovery:
    socket: str
    pid: int | None
    source: str  # "env" | "file"


def default_socket_dir() -> Path:
    """`$TMPDIR/goldmine-serve-<uid>/` — the per-uid 0700 directory (D5)."""
    return Path(os.environ.get("TMPDIR") or "/tmp") / f"{SOCKET_DIR_NAME}-{os.getuid()}"


def default_socket_path() -> Path:
    return default_socket_dir() / SOCKET_NAME


def socket_path_for(cfg: Config) -> Path:
    """Explicit env/config socket wins (GOLDMINE_SERVE via cfg.serve.socket);
    default is the per-uid TMPDIR path (D2)."""
    explicit = getattr(cfg.serve, "socket", "") if cfg is not None else ""
    return Path(explicit) if explicit else default_socket_path()


def discovery_path_for(cfg: Config) -> Path:
    return resolve_home(cfg) / "serve.json"


def read_discovery(cfg: Config) -> Discovery | None:
    """Discovery source order (D3): GOLDMINE_SERVE env (already folded into
    cfg.serve.socket by config.load) -> `<home>/serve.json`. Read-only: CLIENTS
    NEVER DELETE stale files — the next `serve start` sweeps them (D3)."""
    explicit = getattr(cfg.serve, "socket", "")
    if explicit:
        return Discovery(socket=explicit, pid=None, source="env")
    path = discovery_path_for(cfg)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("socket"):
        return None
    pid = data.get("pid")
    return Discovery(
        socket=str(data["socket"]),
        pid=int(pid) if isinstance(pid, int) else None,
        source="file",
    )


def pid_alive(pid: int) -> bool:
    """True when `pid` is a LIVE process. A ZOMBIE (an exited-but-unreaped
    child of the CALLER, the normal case when tests stop their own daemon
    subprocess) still answers kill(pid, 0) — it is asked separately via
    `ps -o stat=` (a 'Z' state means dead for every practical purpose)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    import subprocess

    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # cannot refine the answer; assume alive
    if proc.returncode != 0:
        return False  # ps says no such process
    stat = proc.stdout.decode("utf-8", "replace").strip()
    return not stat.startswith("Z")


def socket_dir_ok(sock_path: str | Path) -> bool:
    """D5 client side: the socket's parent dir must be 0700. A dir with the
    wrong mode -> treat the daemon as ABSENT (never connect). A MISSING dir is
    not this check's business (the connect itself fails -> the refused path)."""
    parent = Path(sock_path).parent
    try:
        return (os.stat(parent).st_mode & 0o777) == 0o700
    except OSError:
        return True  # missing dir: let the connect attempt decide the outcome


def connect_socket(path: str | Path, timeout_s: float) -> socket.socket:
    """Connect to a UDS with a hard deadline. Raises OSError on refusal/timeout."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        raise
    return sock


def one_shot_request(sock_path: str | Path, op: str, params: dict | None = None,
                     *, timeout_s: float = 5.0) -> dict:
    """Connect -> one request -> close. Used by `serve status`/`serve stop` and
    doctor; RemoteBackend keeps a persistent connection instead."""
    sock = connect_socket(sock_path, CONNECT_TIMEOUT_S)
    try:
        send_line(sock, {"op": op, "id": f"one-{op}", "pid": os.getpid(),
                         **(params or {})})
        return recv_response(sock, timeout_s)
    finally:
        sock.close()


# ---------------------------------------------------------- sidecar sha (D4)


def valid_sidecar_sha(model_path: str | Path) -> str | None:
    """The `<model>.sha256` sidecar sha when the sidecar is VALID for the file's
    current size+mtime (llama.py's cache validity rule), else None. Read-only:
    this never recomputes and never writes the sidecar (F2: the daemon owns the
    fresh hash; the residual — a stale-but-valid sidecar passing BOTH cold and
    the handshake — is a pre-existing llama.py property, documented not solved)."""
    import json as _json

    model = Path(model_path)
    sidecar = model.with_name(model.name + ".sha256")
    try:
        st = model.stat()
        cached = _json.loads(sidecar.read_text())
        if cached.get("size") == st.st_size and cached.get("mtime_ns") == st.st_mtime_ns:
            return str(cached["sha256"])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


# ------------------------------------------------------------- the backend


class RemoteBackend(EmbeddingBackend):
    """The serve client behind the ABC (D1 table). Constructed ONLY by
    connect_or_cold(); never registered in the backend registry.

    NOT overridden on purpose (parity with cold): `prompts_doc_template`
    inherits the base default (== llama's template), so the live embed identity
    matches a cold backend's exactly.
    """

    #: LOAD-BEARING constant (D1): fingerprints.fp_build keys on backend_id and
    #: the gate compares byte-identically with cold. This does NOT make the
    #: class registry-eligible — connect_or_cold() is the only constructor.
    backend_id = "llama"

    def __init__(self, cfg: Config, sock_path: str | Path,
                 *, notice_stream=None):
        self.cfg = cfg
        self._sock_path = str(sock_path)
        self._timeout = float(cfg.serve.timeout_s)
        self._notice_stream = notice_stream if notice_stream is not None else sys.stderr
        self._sock: socket.socket | None = None
        self._io_lock = threading.Lock()
        self._info: dict | None = None
        self._fp_fields: dict | None = None
        self._capability: Capability | None = None
        self._max_input: int | None = None
        self._daemon_pid: int | None = None
        self._cold: LlamaServerBackend | None = None
        self._notice_shown = False
        self.fallback_reason: str | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def via(self) -> str:
        """For the `via:` footer (D6): "serve", or "cold" once a mid-request
        failure pushed this backend onto the cold path (F5)."""
        return "cold" if self._cold is not None else "serve"

    def startup(self) -> None:
        """Connect + handshake (ping proto/version, info drift check). Idempotent;
        raises ServeUnusable when the daemon cannot be used — connect_or_cold
        turns that into the cold path + one notice (D1/§6)."""
        if self._sock is not None or self._cold is not None:
            return
        try:
            sock = connect_socket(self._sock_path, CONNECT_TIMEOUT_S)
        except OSError as exc:
            raise ServeUnusable(f"connect failed on {self._sock_path} ({exc})") from exc
        try:
            ping = self._roundtrip(sock, "ping", {})
            proto = ping.get("proto")
            if proto != PROTO:
                raise ServeUnusable(
                    f"protocol mismatch (daemon proto {proto} != client {PROTO})"
                )
            daemon_version = ping.get("goldmine_version")
            if daemon_version != __version__:
                raise ServeUnusable(
                    f"version mismatch (daemon {daemon_version} != client {__version__})"
                )
            self._daemon_pid = (
                int(ping["pid"]) if isinstance(ping.get("pid"), int) else None
            )
            info = self._roundtrip(sock, "info", {})
            drift = self._drift_reason(info)
            if drift:
                raise ServeUnusable(drift)
            self._cache_info(info)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        self._sock = sock

    def _cache_info(self, info: dict) -> None:
        self._info = info
        fields = info.get("fingerprint_fields")
        self._fp_fields = dict(fields) if isinstance(fields, dict) else None
        cap = info.get("capability") or {}
        self._capability = Capability(
            text=bool(cap.get("text")), image=bool(cap.get("image")),
            audio=bool(cap.get("audio")),
        )
        max_in = info.get("max_input_tokens")
        self._max_input = int(max_in) if isinstance(max_in, (int, float)) else None

    def _drift_reason(self, info: dict) -> str | None:
        """The D4 drift check: model PATH must match; the client's VALID sidecar
        sha must match the daemon's fresh hash (F2). Missing client sidecar ->
        no sha check here; the fingerprint gate remains the backstop."""
        local_model = str(resolve_model_path(self.cfg.llama))
        serve_model = info.get("model_path")
        if serve_model != local_model:
            return (
                f"model drift (serve model {serve_model} != local {local_model})"
                f" — using cold"
            )
        serve_sha = (info.get("fingerprint_fields") or {}).get("model_sha256")
        local_sha = valid_sidecar_sha(local_model)
        if local_sha is not None and serve_sha and serve_sha != local_sha:
            return (
                f"model drift (serve {str(serve_sha)[:8]} != local "
                f"{local_sha[:8]}) — using cold"
            )
        return None

    def close(self) -> None:
        """Socket ONLY — the client never signals the daemon (D1, pinned by
        test: daemon + llama-server survive a client close). After a cold
        fallback, the cold backend WE constructed this run is closed normally
        (kills its own child — house rule: only what we spawned)."""
        if self._cold is not None:
            self._cold.close()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def shutdown(self) -> None:  # pragma: no cover - trivial alias (D1)
        self.close()

    # ------------------------------------------------------------- plumbing

    def _notice(self, message: str) -> None:
        if not self._notice_shown:
            self._notice_shown = True
            print(f"goldmine serve: {message}", file=self._notice_stream)

    def _request(self, op: str, params: dict | None = None) -> dict:
        """One request on the persistent connection; transparent reconnect after
        the server's idle close; F8: carries the client pid on every request."""
        with self._io_lock:
            if self._sock is None:
                self._sock = connect_socket(self._sock_path, CONNECT_TIMEOUT_S)
            request = {"op": op, "pid": os.getpid(), **(params or {})}
            try:
                try:
                    send_line(self._sock, request)
                    return recv_response(self._sock, self._timeout)
                except (ServeRequestError, OSError) as first:
                    if not getattr(first, "transient", True):
                        raise
                    # transient: one fresh connection, one retry (F5 discipline)
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = connect_socket(self._sock_path, CONNECT_TIMEOUT_S)
                    send_line(self._sock, request)
                    return recv_response(self._sock, self._timeout)
            except (OSError, ServeRequestError) as exc:
                try:
                    if self._sock is not None:
                        self._sock.close()
                except OSError:
                    pass
                self._sock = None
                if isinstance(exc, ServeRequestError):
                    raise
                raise ServeRequestError(f"connection error: {exc}") from exc

    def _roundtrip(self, sock: socket.socket, op: str, params: dict) -> dict:
        """Handshake-only round trip on a caller-owned socket (no retry)."""
        send_line(sock, {"op": op, "pid": os.getpid(), **params})
        return recv_response(sock, self._timeout)

    def _fallback_to_cold(self, reason: str) -> LlamaServerBackend:
        """THE F5 guarantee: results still delivered, one notice, cold is exactly
        cmd_search's construction. The fingerprint gate already passed with the
        serve fields, and cold fields are byte-identical by construction (D1)."""
        if self._cold is None:
            self._cold = LlamaServerBackend(self.cfg.llama)
            self.fallback_reason = reason
            self._notice(f"{reason} — using cold")
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
        return self._cold

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        """One client-side batch: daemon embed op -> rows (already L2-normalized
        server-side; this side must not normalize again — see module docstring).
        F5: model_not_resident (guard) -> no retry, straight to cold; transient
        -> retry-once -> cold; any other mid-request failure -> cold."""
        try:
            payload = self._request("embed", {"texts": texts})
        except ServeRequestError as exc:
            if exc.transient:
                try:
                    payload = self._request("embed", {"texts": texts})
                except (ServeRequestError, OSError) as exc2:
                    return self._fallback_to_cold(str(exc2)).embed_texts(texts)
            else:
                return self._fallback_to_cold(str(exc)).embed_texts(texts)
        except OSError as exc:
            try:
                payload = self._request("embed", {"texts": texts})
            except (ServeRequestError, OSError) as exc2:
                return self._fallback_to_cold(str(exc2)).embed_texts(texts)
        vectors = payload.get("vectors")
        dim = payload.get("dim")
        if not isinstance(vectors, list) or len(vectors) != len(texts) or dim != VEC_DIM:
            return self._fallback_to_cold(
                f"malformed embed payload ({len(vectors or [])} rows)"
            ).embed_texts(texts)
        try:
            matrix = np.asarray(vectors, dtype=np.float32)
        except (TypeError, ValueError):
            return self._fallback_to_cold("malformed embed payload").embed_texts(texts)
        if matrix.shape != (len(texts), VEC_DIM):
            return self._fallback_to_cold("malformed embed payload").embed_texts(texts)
        return matrix

    # ------------------------------------------------------------ ABC members

    @property
    def dim(self) -> int:
        if self._cold is not None:
            return self._cold.dim
        return int((self._info or {}).get("dim") or VEC_DIM)

    def capability(self) -> Capability:
        """Computed locally from the handshake info — no round trip (D1)."""
        if self._cold is not None:
            return self._cold.capability()
        return self._capability or Capability(text=True, image=False, audio=False)

    def max_input_tokens(self) -> int | None:
        if self._cold is not None:
            return self._cold.max_input_tokens()
        return self._max_input

    def fingerprint_fields(self) -> dict:
        if self._cold is not None:
            return self._cold.fingerprint_fields()
        if self._fp_fields is None:
            raise EmbeddingError("serve handshake did not deliver fingerprint fields")
        return dict(self._fp_fields)

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if self._cold is not None:
            return self._cold.embed_texts(texts)
        if not texts:
            return np.zeros((0, VEC_DIM), dtype=np.float32)
        batch = max(1, int(self.cfg.llama.batch))
        out = np.zeros((len(texts), VEC_DIM), dtype=np.float32)
        for start in range(0, len(texts), batch):
            part = self._embed_batch(list(texts[start:start + batch]))
            out[start:start + part.shape[0]] = part
        return out  # rows already L2-normalized by the daemon's backend

    def count_tokens(self, texts: list[str]) -> list[int] | None:
        """Warm exact counts; ANY failure -> None (the caller's estimator
        fallback) — deliberately NOT a cold spawn (spec §11.2 default)."""
        if self._cold is not None:
            return self._cold.count_tokens(texts)
        if not texts:
            return []
        try:
            payload = self._request("count_tokens", {"texts": list(texts)})
            counts = payload.get("counts")
            if isinstance(counts, list) and len(counts) == len(texts):
                return [int(n) for n in counts]
        except (ServeRequestError, OSError, ValueError, TypeError) as exc:
            logger.debug("serve: count_tokens unavailable (%s) — estimating", exc)
        return None

    def token_pieces(self, text: str) -> "list[bytes] | None":
        """F9 scope cut: the token_pieces OP is not built in R8 (chunker-only
        caller; ingest stays cold — moves to G6). None = estimator fallback."""
        return None

    def embed_image(self, path: Path) -> np.ndarray:
        # llama.py messages VERBATIM (D1: the ABC surface fits unchanged)
        raise NotSupported(
            "llama /v1/embeddings is text-only — images are metadata-only under "
            "this backend (use --backend foresight or mock for image vectors)"
        )

    def embed_audio_wav(self, wav: bytes) -> np.ndarray:
        raise NotSupported(
            "llama /v1/embeddings is text-only — audio is metadata-only under "
            "this backend (use --backend foresight or mock for audio vectors)"
        )

    def describe(self) -> dict:
        """F9: serve flavor is a STUB in R8 — nothing wires it into
        status/backends; `goldmine serve status` is the real face."""
        return {
            "id": self.backend_id,
            "via": self.via,
            "socket": self._sock_path,
            "daemon_pid": self._daemon_pid,
            "detail": "serve backend (stub describe — use `goldmine serve status`)",
        }


# ----------------------------------------------------------- the seam (D1/§6)


def connect_or_cold(cfg: Config, backend_name: str | None = None, *,
                    no_serve: bool = False,
                    notice_stream=None) -> tuple[EmbeddingBackend, str]:
    """cmd_search's backend acquisition seam (the ONE deliberate cli change).

    Returns (backend, via) with via in {"serve", "cold"}. Order (§6):
    non-llama backend / --no-serve -> cold untouched; else discovery ->
    liveness -> handshake -> RemoteBackend; ANY failure -> cold construction
    (exactly today's create_backend path, same exceptions) + ONE stderr notice
    when a daemon was found but unusable — silent when simply absent (a dead-pid
    discovery file is the quiet "no daemon" case per T15).
    """
    effective = backend_name or cfg.backend
    if effective != "llama" or no_serve:
        from . import create_backend

        return create_backend(cfg, backend_name), "cold"

    found = read_discovery(cfg)
    if found is not None:
        if found.pid is not None and not pid_alive(found.pid):
            # stale discovery (daemon died): silent — this is "absent" (D1/T15)
            found = None
        elif not socket_dir_ok(found.socket):
            # D5: the socket dir lost its 0700 mode — treat the daemon as absent
            found = None
    if found is not None:
        try:
            backend = RemoteBackend(cfg, found.socket, notice_stream=notice_stream)
            backend.startup()
            return backend, "serve"
        except ServeUnusable as exc:
            stream = notice_stream if notice_stream is not None else sys.stderr
            print(f"goldmine serve: {exc}", file=stream)

    from . import create_backend

    return create_backend(cfg, backend_name), "cold"
