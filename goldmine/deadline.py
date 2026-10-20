"""One request budget, inherited by synchronous SQLite and backend calls.

No worker is abandoned on timeout. Each blocking operation uses the remaining
time, and retries check the same deadline before starting more work. ContextVars
keep concurrent callers isolated without mutating a backend's shared config.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

_cancellation_check: ContextVar[Callable[[], None] | None] = ContextVar(
    "goldmine_cancellation_check", default=None,
)


@contextmanager
def request_cancellation(checker: Callable[[], None]):
    """Let an adapter supply cooperative cancellation without a core dependency."""
    token = _cancellation_check.set(checker)
    try:
        check_deadline()
        yield
    finally:
        _cancellation_check.reset(token)


def _check_cancellation() -> None:
    checker = _cancellation_check.get()
    if checker is not None:
        checker()


class RequestTimeout(TimeoutError):
    """The end-to-end request budget expired; never a fallback signal."""


@dataclass(frozen=True)
class Deadline:
    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> "Deadline":
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("request timeout must be finite and nonnegative")
        return cls(time.monotonic() + seconds)

    def remaining(self, limit: float | None = None) -> float:
        _check_cancellation()
        seconds = self.expires_at - time.monotonic()
        if seconds <= 0:
            raise RequestTimeout("request deadline exceeded")
        return seconds if limit is None else min(seconds, limit)


_active: ContextVar[Deadline | None] = ContextVar("goldmine_deadline", default=None)


def current_deadline() -> Deadline | None:
    return _active.get()


def check_deadline() -> None:
    deadline = current_deadline()
    if deadline is not None:
        deadline.remaining()
    else:
        _check_cancellation()


def remaining_timeout(limit: float) -> float:
    deadline = current_deadline()
    if deadline is not None:
        return deadline.remaining(limit)
    _check_cancellation()
    return limit


@contextmanager
def request_timeout(seconds: float | None = None, *, deadline: Deadline | None = None):
    """Use the earliest of a caller's budget, an explicit and a parent deadline."""
    candidates = [d for d in (current_deadline(), deadline) if d is not None]
    if seconds is not None:
        candidates.append(Deadline.after(seconds))
    effective = min(candidates, key=lambda d: d.expires_at) if candidates else None
    token = _active.set(effective)
    try:
        check_deadline()
        yield effective
        check_deadline()
    finally:
        _active.reset(token)


@contextmanager
def deadline_lock(lock):
    """A synchronous lock wait consumes the same request budget."""
    deadline = current_deadline()
    acquired = lock.acquire() if deadline is None else lock.acquire(timeout=deadline.remaining())
    if not acquired:
        raise RequestTimeout("request deadline exceeded while waiting for a backend")
    try:
        check_deadline()
        yield
    finally:
        lock.release()


def deadline_sleep(seconds: float) -> None:
    time.sleep(remaining_timeout(seconds))
    check_deadline()
