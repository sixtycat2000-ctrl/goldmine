"""Backend registry — the single construction point (ARCHITECTURE §4).

llama/foresight modules are agent-B deliverables (build passes 4/7); they are
imported lazily so this build works fully without them and reports their absence
honestly instead of crashing.
"""

from __future__ import annotations

import importlib
import logging

from ..config import Config
from .base import (
    Capability,
    EmbeddingBackend,
    EmbeddingError,
    NotSupported,
)

logger = logging.getLogger("goldmine.backends")

__all__ = [
    "Capability",
    "EmbeddingBackend",
    "EmbeddingError",
    "NotSupported",
    "BackendUnavailable",
    "BACKEND_IDS",
    "create_backend",
    "probe_all",
]


class BackendUnavailable(RuntimeError):
    """The backend exists as a concept but cannot run in this environment/build."""

    def __init__(self, message: str, fix_hint: str | None = None):
        super().__init__(message)
        self.fix_hint = fix_hint


def _load_class(backend_id: str) -> type[EmbeddingBackend] | None:
    """Lazily import a backend module; None when it is not in this build."""
    try:
        module = importlib.import_module(f"goldmine.backends.{backend_id}")
    except ImportError:
        return None
    cls = getattr(module, "BACKEND_CLASS", None)
    if cls is None:
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, EmbeddingBackend)
                and getattr(attr, "backend_id", None) == backend_id
                and attr.__module__ == module.__name__
            ):
                cls = attr
                break
    return cls  # type: ignore[return-value]


def _registry() -> dict[str, type[EmbeddingBackend]]:
    reg: dict[str, type[EmbeddingBackend]] = {"mock": None}  # type: ignore[dict-item]
    reg["mock"] = _load_class("mock")  # type: ignore[assignment]
    for backend_id in ("llama", "foresight"):
        cls = _load_class(backend_id)
        if cls is not None:
            reg[backend_id] = cls  # type: ignore[assignment]
    return {k: v for k, v in reg.items() if v is not None}


def BACKEND_IDS() -> list[str]:  # noqa: N802 - MODULE_MAP names the dict BACKENDS
    return sorted(_registry())


def create_backend(cfg: Config, name: str | None = None) -> EmbeddingBackend:
    """Construct a backend by name (default: cfg.backend). May raise
    BackendUnavailable (llama/foresight not in this build) or ValueError (unknown)."""
    backend_name = name or cfg.backend
    registry = _registry()
    if backend_name not in registry:
        if backend_name in ("llama", "foresight"):
            raise BackendUnavailable(
                f"backend '{backend_name}' is not available in this build",
                fix_hint="use --backend mock, or --lexical-only for a keyword-only index",
            )
        raise ValueError(
            f"unknown backend '{backend_name}' (known: {', '.join(sorted(registry))})"
        )
    cls = registry[backend_name]
    if backend_name == "llama":
        return cls(cfg.llama)  # type: ignore[misc]
    if backend_name == "foresight":
        return cls(cfg.foresight)  # type: ignore[misc]
    return cls()


def probe_all(cfg: Config, live: bool = False) -> dict[str, dict]:
    """Availability table for `goldmine backends` / `doctor` / `status`.

    `live` (a real embed round trip) is left to the backends themselves; in this
    build probe_all only reports static availability.
    """
    out: dict[str, dict] = {}
    for backend_id in ("llama", "foresight", "mock"):
        try:
            backend = create_backend(cfg, backend_id)
        except BackendUnavailable as exc:
            out[backend_id] = {
                "id": backend_id,
                "available": False,
                "detail": str(exc),
                "fix_hint": exc.fix_hint,
            }
            continue
        except ValueError as exc:
            out[backend_id] = {"id": backend_id, "available": False, "detail": str(exc)}
            continue
        try:
            info = backend.describe()
        except Exception as exc:  # describe must never crash status
            info = {"id": backend_id, "available": False, "detail": f"describe failed: {exc}"}
        out[backend_id] = info
    return out
