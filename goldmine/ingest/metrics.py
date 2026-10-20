"""Optional pass-local ingestion measurements; no stdout or global observers."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager


class IngestMetrics:
    def __init__(self, enabled=True, backend=None):
        self.enabled = enabled
        self.backend = backend
        self._lock = threading.Lock()
        self._local = threading.local()
        self._times = {f"{stage}_ms": 0.0 for stage in ("scan", "hash", "extract", "embed", "commit")}
        self._calls = {name: 0 for name in ("embed_calls", "embed_inputs", "ocr_calls", "ocr_inputs", "asr_calls")}
        self._spawn_start = self._observed("model_spawn_count")
        self._http_start = self._observed("embedding_http_requests")

    def _observed(self, field):
        value = getattr(self.backend, field, None) if self.backend is not None else 0
        return value if type(value) is int and value >= 0 else None

    def call(self, name, count=1):
        if self.enabled:
            with self._lock:
                self._calls[name] += count

    @contextmanager
    def stage(self, name):
        if not self.enabled:
            yield
            return
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        frame = [time.perf_counter_ns(), 0]
        stack.append(frame)
        try:
            yield
        finally:
            elapsed = time.perf_counter_ns() - frame[0]
            stack.pop()
            if stack:
                stack[-1][1] += elapsed
            with self._lock:
                self._times[name + "_ms"] += max(0, elapsed - frame[1]) / 1_000_000

    def snapshot(self, *, files_skipped=0):
        if not self.enabled:
            return None
        spawn_end = self._observed("model_spawn_count")
        http_end = self._observed("embedding_http_requests")
        with self._lock:
            result = {**self._times, **self._calls}
        result.update(
            files_skipped=files_skipped,
            model_spawns=(spawn_end - self._spawn_start if spawn_end is not None and self._spawn_start is not None else None),
            embedding_http_requests=(http_end - self._http_start if http_end is not None and self._http_start is not None else None),
            timing_semantics="exclusive per thread; extract worker totals can overlap wall time; embed_ms includes identity checks/model startup; hash_ms counts source-file hashes",
            call_semantics="embed calls count content API calls; HTTP requests include startup probes/retries when observable; OCR calls count recognize batches; ASR calls count transcribe attempts",
            native_observability={
                "model_spawns": spawn_end is not None and self._spawn_start is not None,
                "embedding_http_requests": http_end is not None and self._http_start is not None,
                "scope": "this backend instance; external/shared native processes require tool-level observation",
            },
        )
        return result
