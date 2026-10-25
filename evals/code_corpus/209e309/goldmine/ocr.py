"""OCR client — spawns the compiled `goldmine-ocr` Vision helper (v2 item B4,
docs/v2/02 §5.2).

The helper is a separate one-shot process (Vision caches ~100 MB of models in
its host — DigUp IndexService.swift:4-8); this module is the only thing that
talks to it. Contract:

- helper lives at ``<home>/bin/goldmine-ocr`` (env ``GOLDMINE_OCR_HELPER``
  overrides — how tests point at a stub);
- stdin batch mode: one JSON line ``{"id","path"}`` per image in, one JSON
  line ``{"id","path","texts":[{"text","confidence","box"}],"ms"}`` or
  ``{"id","error"}`` out; exit 0 even with per-image errors;
- ``--version`` prints ``goldmine-ocr 1`` (doctor handshake — a stale binary
  is "OCR off", never a crash);
- timeouts: a batch gets cold-start grace + 30 s per image, enforced here.

Confidence filtering (>= 0.5) and line concatenation happen in THIS module so
the policy stays in Python. Compile-on-demand (dev fallback, B13/A5: the user
path is the prebuilt release asset) is ``ensure_helper``: it compiles with
``swiftc -O`` when the helper is missing and CLT exists, and returns an honest
no-swiftc reason otherwise. Set ``GOLDMINE_OCR_AUTOCOMPILE=0`` to disable
autocompile (tests do).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, resolve_home
from .util import ToolError, run_tool

logger = logging.getLogger("goldmine.ocr")

__all__ = [
    "HELPER_NAME",
    "HELPER_VERSION",
    "MIN_CONFIDENCE",
    "OcrLine",
    "OcrPage",
    "OcrUnavailable",
    "helper_path",
    "helper_source",
    "ocr_available",
    "handshake",
    "compile_helper",
    "ensure_helper",
    "recognize",
    "recognize_one",
]

HELPER_NAME = "goldmine-ocr"
HELPER_VERSION = "goldmine-ocr 1"
HANDSHAKE_TIMEOUT_S = 5.0
DEFAULT_TIMEOUT_PER_IMAGE_S = 30.0
COLD_START_GRACE_S = 15.0      # measured cold start ~1.3 s; margin for model loads
COMPILE_TIMEOUT_S = 120.0
MIN_CONFIDENCE = 0.5           # lines below this are dropped at concatenation


class OcrUnavailable(RuntimeError):
    """The helper is missing/not executable, or a run could not be spawned."""


class OcrTimeout(RuntimeError):
    """The helper exceeded its per-image budget and was killed."""


@dataclass
class OcrLine:
    text: str
    confidence: float
    box: list[float] | None = None


@dataclass
class OcrPage:
    """One image's outcome. `error` set means the whole image failed."""

    lines: list[OcrLine] = field(default_factory=list)
    ms: float | None = None
    error: str | None = None

    def text(self, min_confidence: float = MIN_CONFIDENCE) -> str:
        """Recognized lines joined with \\n, low-confidence lines dropped."""
        return "\n".join(
            line.text for line in self.lines if line.confidence >= min_confidence
        )


# ------------------------------------------------------------------ discovery


def helper_path(cfg: Config) -> Path | None:
    """`<home>/bin/goldmine-ocr` when present (env override first). None = off."""
    env = os.environ.get("GOLDMINE_OCR_HELPER")
    if env:
        candidate = Path(env).expanduser()
        return candidate if candidate.is_file() else None
    candidate = resolve_home(cfg) / "bin" / HELPER_NAME
    return candidate if candidate.is_file() else None


def helper_source() -> Path | None:
    """tools/ocr/ocr_helper.swift next to the package (env override first)."""
    env = os.environ.get("GOLDMINE_OCR_SOURCE")
    candidates = [Path(env).expanduser()] if env else []
    candidates.append(Path(__file__).resolve().parents[1] / "tools" / "ocr" / "ocr_helper.swift")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def ocr_available(cfg: Config) -> bool:
    """Cheap capability probe for doctor/status: helper present + executable."""
    path = helper_path(cfg)
    return path is not None and os.access(path, os.X_OK)


def handshake(cfg: Config) -> str | None:
    """`goldmine-ocr --version` -> "goldmine-ocr 1", or None when unusable
    (missing, non-executable, crash, stale). Doctor treats None as OCR off."""
    path = helper_path(cfg)
    if path is None or not os.access(path, os.X_OK):
        return None
    try:
        result = run_tool([str(path), "--version"], timeout_s=HANDSHAKE_TIMEOUT_S)
    except (ToolError, OSError):
        return None
    if result.rc != 0 or result.timed_out:
        return None
    banner = result.stdout.strip()
    return banner or None


# ------------------------------------------------------------------- compile


def compile_helper(cfg: Config) -> Path | None:
    """Dev fallback (A5/B13): swiftc -O the helper into <home>/bin.
    Returns the binary path, or None with a logged reason — never raises."""
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        logger.info(
            "ocr: swiftc not found — install Command Line Tools "
            "(xcode-select --install) or use the prebuilt goldmine-ocr asset"
        )
        return None
    source = helper_source()
    if source is None:
        logger.info("ocr: helper source (tools/ocr/ocr_helper.swift) not found")
        return None
    dest = resolve_home(cfg) / "bin" / HELPER_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    logger.info("ocr: compiling %s -> %s (dev fallback, ~2 s)", source, dest)
    try:
        result = run_tool(
            [swiftc, "-O", str(source), "-o", str(tmp)], timeout_s=COMPILE_TIMEOUT_S
        )
    except ToolError as exc:
        logger.warning("ocr: compile failed: %s", exc)
        return None
    if result.rc != 0 or result.timed_out or not tmp.is_file():
        logger.warning(
            "ocr: compile failed rc=%s: %s", result.rc, result.stderr.strip()[:300]
        )
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    dest.chmod(0o755)
    logger.info("ocr: helper installed at %s", dest)
    return dest


def ensure_helper(cfg: Config, *, autocompile: bool = True) -> tuple[Path | None, str | None]:
    """(path, note): helper when usable. When missing, compiles on demand
    (config `ocr.enabled` gates the caller; `GOLDMINE_OCR_AUTOCOMPILE=0`
    disables autocompile — the offline test suite does). Never silent: every
    outcome is logged."""
    path = helper_path(cfg)
    if path is not None and os.access(path, os.X_OK):
        return path, None
    if not autocompile or os.environ.get("GOLDMINE_OCR_AUTOCOMPILE", "1") == "0":
        return None, "goldmine-ocr not installed (OCR off)"
    path = compile_helper(cfg)
    if path is None:
        return None, "goldmine-ocr unavailable: no prebuilt helper and no swiftc"
    return path, "compiled on demand"


# ------------------------------------------------------------------- runtime


def recognize(
    paths,
    cfg: Config,
    *,
    timeout_per_image: float = DEFAULT_TIMEOUT_PER_IMAGE_S,
    cold_start_s: float = COLD_START_GRACE_S,
) -> dict[str, OcrPage]:
    """Batch OCR. Returns id -> OcrPage (missing ids mean the helper died on
    that input without answering). Raises OcrUnavailable when the helper is
    missing and OcrTimeout when the batch exceeds its budget."""
    items = [(str(i), str(p)) for i, p in enumerate(paths)]
    if not items:
        return {}
    helper = helper_path(cfg)
    if helper is None or not os.access(helper, os.X_OK):
        raise OcrUnavailable("goldmine-ocr helper not found/executable")
    payload = "".join(
        json.dumps({"id": id_, "path": path}) + "\n" for id_, path in items
    ).encode("utf-8")
    budget = cold_start_s + timeout_per_image * len(items)
    try:
        proc = subprocess.run(
            [str(helper)], input=payload, capture_output=True, timeout=budget
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrTimeout(
            f"goldmine-ocr exceeded {budget:g}s for {len(items)} image(s)"
        ) from exc
    except OSError as exc:
        raise OcrUnavailable(f"cannot run {helper}: {exc}") from exc
    if proc.returncode != 0:
        raise OcrUnavailable(
            f"goldmine-ocr exited rc={proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace')[:300]}"
        )
    pages: dict[str, OcrPage] = {}
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("ocr: unparsable helper line: %.120s", line)
            continue
        id_ = str(record.get("id", ""))
        if not id_:
            continue
        if record.get("error"):
            pages[id_] = OcrPage(ms=record.get("ms"), error=str(record["error"]))
        else:
            pages[id_] = OcrPage(
                lines=[
                    OcrLine(
                        text=str(entry.get("text", "")),
                        confidence=float(entry.get("confidence", 0.0)),
                        box=entry.get("box"),
                    )
                    for entry in record.get("texts") or []
                ],
                ms=record.get("ms"),
            )
    return pages


def recognize_one(
    path, cfg: Config, *, timeout_per_image: float = DEFAULT_TIMEOUT_PER_IMAGE_S
) -> OcrPage:
    """Convenience: one image -> its OcrPage (never raises for image-level
    errors; OcrUnavailable/OcrTimeout still propagate)."""
    page = recognize([path], cfg, timeout_per_image=timeout_per_image).get("0")
    if page is None:
        return OcrPage(error="helper produced no answer")
    return page
