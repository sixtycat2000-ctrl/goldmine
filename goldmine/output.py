"""CLI output polish (v2 item 19 / F3, 03 §5.5): ingest ticker + ETA, per-root
media pre-flight line, `--format fzf` result serialization.

CLI-layer only: everything here is computed OUTSIDE the pipeline (a cheap
re-walk for the pre-flight counts, a live COUNT for vectors) so ingest
pipeline internals stay untouched. Non-TTY runs see none of this — the end
summary is the headless interface, unchanged.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ASR_REALTIME_FACTOR",
    "MEDIA_PROBE_CAP",
    "IngestTicker",
    "RootMedia",
    "asr_estimate_enabled",
    "fzf_lines",
    "fmt_clock",
    "fmt_hours",
    "render_root_media",
    "scan_root_media",
]

#: ASR wall-clock estimate divisor, MEASURED on the owner machine (02 §2 table:
#: 21.6 min audio in 3:01 wall → 7.2x realtime; "1 h ≈ 8.4 min"). The task
#: brief said "~15x"; the docs' measured constant is the number we ship.
ASR_REALTIME_FACTOR = 7.2

#: Upper bound on pre-run ffprobe duration probes (each is a subprocess; the
#: pre-flight must stay instant even on audio-heavy roots).
MEDIA_PROBE_CAP = 500

_PROGRESS_RE = re.compile(r"ingest: (\d+)/(\d+) files(?:, (\d+) failed)?")


def fmt_clock(seconds: float) -> str:
    """m:ss (h:mm:ss past an hour) — ticker elapsed/eta and open's jump copy."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_hours(seconds: float) -> str:
    """Compact duration for the pre-flight line: `3.2h` / `27m` / `40s`."""
    seconds = max(0.0, float(seconds))
    if seconds >= 5400:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 90:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


# ------------------------------------------------------------------- ticker


class IngestTicker:
    """Single-line ingest progress on a TTY stderr (03 §5.5, 2 s cadence).

    Draws ``[ingest 42/120 files · vectors 1,203 · 1 failed · 0:44 elapsed ·
    eta 0:44]`` carriage-returned in place. ANSI (\\r + clear-to-EOL) only
    when `ansi` is set (NO_COLOR / --no-color turn it off; padding keeps the
    line clean without EL). Vectors come from a live COUNT on the run's own
    Store (same connection the pipeline commits on, so the read is exact).
    The pipeline exposes file counts only, so ETA is files-linear
    (elapsed/done × remaining) — shown from file 5 on.
    """

    def __init__(
        self,
        total_files: int,
        *,
        store=None,
        stream=None,
        ansi: bool = True,
        interval: float = 2.0,
        clock: object = time.monotonic,
    ) -> None:
        import sys

        self.total = max(0, int(total_files))
        self.store = store
        self.stream = stream if stream is not None else sys.stderr
        self.ansi = ansi
        self.interval = float(interval)
        self.clock = clock
        self.done = 0
        self.failed = 0
        self.drawn = False
        self.started = clock()
        self._last_emit = float("-inf")
        self._last_len = 0

    # -- pipeline progress hook -------------------------------------------
    def update(self, message: str) -> None:
        m = _PROGRESS_RE.search(message or "")
        if not m:
            return
        self.done, total, failed = int(m.group(1)), int(m.group(2)), m.group(3)
        self.total = max(self.total, total)
        self.failed = int(failed or 0)
        now = self.clock()
        final = self.done >= self.total
        if not final and (now - self._last_emit) < self.interval:
            return
        self._last_emit = now
        self._draw(now)

    def finish(self) -> None:
        if self.drawn:
            self.stream.write("\n")
            self.stream.flush()

    # -- rendering ---------------------------------------------------------
    def _vectors(self) -> int | None:
        if self.store is None:
            return None
        try:
            return self.store.counts().vectors
        except Exception:  # a read must never kill the run
            return None

    def _draw(self, now: float) -> None:
        elapsed = max(0.0, now - self.started)
        if self.done >= 5 and self.done < self.total:
            eta = fmt_clock(elapsed / self.done * (self.total - self.done))
        else:
            eta = "…"
        parts = [f"ingest {self.done}/{self.total} files"]
        vectors = self._vectors()
        if vectors is not None:
            parts.append(f"vectors {vectors:,}")
        if self.failed:
            parts.append(f"{self.failed} failed")
        parts.append(f"{fmt_clock(elapsed)} elapsed")
        parts.append(f"eta {eta}")
        line = "[" + " · ".join(parts) + "]"
        if self.ansi:
            prefix = "" if not self.drawn else "\r"
            self.stream.write(prefix + "\x1b[K" + line)
        else:
            pad = max(0, self._last_len - len(line))
            prefix = "" if not self.drawn else "\r"
            self.stream.write(prefix + line + " " * pad)
        self._last_len = len(line)
        self.drawn = True
        self.stream.flush()


# ------------------------------------------------- per-root media pre-flight


@dataclass
class RootMedia:
    """Per-root file counts from the CLI-side walk (no DB, no pipeline)."""

    root: str
    files: int = 0
    bytes: int = 0
    audio: int = 0
    video: int = 0
    images: int = 0
    #: total audio+video seconds actually readable (ffprobe), None when no
    #: probe was possible (ffprobe missing / capped out / nothing readable)
    media_seconds: float | None = None
    probed: int = 0
    probe_capped: bool = False


def scan_root_media(
    roots: list[str],
    *,
    code: bool = False,
    exclude: list[str] | None = None,
    max_file_mb: float = 200,
    probe_durations: bool = True,
    duration_fn=None,
    max_probes: int = MEDIA_PROBE_CAP,
    audio_cap_s: float | None = None,
) -> list[RootMedia]:
    """Per-root media counts, computed from the same walk rules the pipeline
    uses (goldmine.ingest.walk — read-only import, pipeline untouched).
    `duration_fn` is injectable for tests; defaults to media.ffprobe_duration.
    `audio_cap_s` mirrors the pipeline's per-file ASR cap in the estimate."""
    # direct submodule import (goldmine.ingest.walk — read-only)
    from .ingest.media import ffprobe_duration
    from .ingest.walk import Candidate, walk_files

    if duration_fn is None:
        duration_fn = ffprobe_duration
    out: list[RootMedia] = []
    for root in roots:
        if str(root).startswith(("http://", "https://")):
            continue  # URLs have no walk; the crawl reports its own counts
        rm = RootMedia(root=str(root))
        seconds: list[float] = []
        probes = 0
        for item in walk_files(
            [Path(root)],
            code=code,
            exclude=list(exclude or []),
            max_file_bytes=int(max_file_mb) * (1 << 20),
        ):
            if not isinstance(item, Candidate):
                continue
            rm.files += 1
            rm.bytes += item.size
            if item.kind == "image":
                rm.images += 1
            elif item.kind in ("audio", "video"):
                rm.audio += item.kind == "audio"
                rm.video += item.kind == "video"
                if probe_durations and probes < max_probes:
                    probes += 1
                    d = duration_fn(item.path)
                    if d is not None:
                        if audio_cap_s is not None:
                            d = min(d, audio_cap_s)
                        seconds.append(float(d))
        rm.probed = probes
        rm.probe_capped = probes >= max_probes
        if probe_durations and seconds:
            rm.media_seconds = sum(seconds)
        out.append(rm)
    return out


def asr_estimate_enabled(cfg) -> bool:
    """Whether the ASR wall-clock estimate belongs in the pre-flight line:
    `ingest.asr != off` AND whisper + models actually resolve."""
    if str(getattr(cfg.ingest, "asr", "auto") or "auto") == "off":
        return False
    try:
        from .ingest.asr import resolve_tools

        return resolve_tools(cfg) is not None
    except Exception:
        return False


def render_root_media(medias: list[RootMedia], *, asr: bool = False) -> str:
    """The C5 pre-flight lines, one per root:
    ``root ~/X: 412 files (18 audio ≈ 3.2h → asr ≈ 27m, 26 images)…``"""
    lines = []
    for rm in medias:
        bits: list[str] = []
        hours = fmt_hours(rm.media_seconds) if rm.media_seconds else None
        asr_h = (
            fmt_hours(rm.media_seconds / ASR_REALTIME_FACTOR)
            if asr and rm.media_seconds
            else None
        )

        def media_bit(count: int, noun: str, lead: bool) -> str:
            # hours attach to the first-listed media kind (audio, else video)
            bit = f"{count} {noun}"
            if lead and hours:
                bit += f" ≈ {hours}"
            if lead and asr_h:
                bit += f" → asr ≈ {asr_h}"
            return bit

        if rm.audio:
            bits.append(media_bit(rm.audio, "audio", lead=True))
        if rm.video:
            bits.append(media_bit(rm.video, "video", lead=not rm.audio))
        if rm.images:
            bits.append(f"{rm.images} images")
        line = f"root {rm.root}: {rm.files} files"
        if bits:
            line += " (" + ", ".join(bits) + ")"
        if rm.probe_capped:
            line += f" (duration probed for {rm.probed} media files)"
        lines.append(line + "…")
    return "\n".join(lines)


# ------------------------------------------------------------ fzf serialization


def fzf_lines(hits, loc_fn=None) -> list[str]:
    """`search --format fzf` rows: ``path\\tkind\\tloc\\texcerpt\\tscore``.

    Tabs/newlines are stripped from every field so fzf's column split (and a
    naive `cut`) stay correct on adversarial content. Lives here — not in the
    render_hits region (owned by the search pass) — so the search command
    carries only a thin call."""
    def clean(value) -> str:
        return (
            str(value)
            .replace("\t", " ")
            .replace("\r", " ")
            .replace("\n", " ")
            .strip()
        )

    rows = []
    for h in hits:
        loc = ""
        if loc_fn is not None:
            try:
                loc = loc_fn(h.kind, h.loc, h.loc_end) or ""
            except Exception:
                loc = ""
        rows.append(
            "\t".join(
                [
                    clean(h.path),
                    clean(h.kind),
                    clean(loc),
                    clean(h.excerpt or ""),
                    f"{h.score:.4f}",
                ]
            )
        )
    return rows
