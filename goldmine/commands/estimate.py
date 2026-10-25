"""`goldmine estimate <root>`: read-only first-ingest time estimate.

DigUp Estimator.swift port. The rate constants are DigUp's priors, measured
2026-10-07 on an M4 Pro (365-file testbed, 160 s whole index) — labeled as
such everywhere; goldmine's own calibration is pending. This command reads
METADATA ONLY: the walk rules (same skips the ingest uses), pdfinfo page
counts, ffprobe durations. It never opens an index, never embeds, never
writes anything (it cannot: no --db surface exists).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..errors import UserFacingError
from ..ingest import rules
from ..ingest.extract_pdf import pdf_info, pdfinfo_path
from ..ingest.media import ffprobe_duration, ffprobe_path
from ..ingest.walk import SkipNotice, walk_files

# --- DigUp Costs (Estimator.swift:5-16), verbatim ---------------------------
SECONDS_PER_IMAGE = 0.12           # 140 tokens, decode included
SECONDS_PER_SCREENSHOT = 0.32      # 280 tokens; its OCR (0.24 s) sets the pace
SECONDS_PER_PDF_PAGE = 0.09
SECONDS_PER_DOC_CHUNK = 0.095
SECONDS_PER_AUDIO_SECOND = 0.0089  # a 30 s window every 25 s (embed only)
SECONDS_PER_VIDEO_SECOND = 0.019   # a 70-token keyframe per shot + soundtrack
SECONDS_PER_CODE_STRETCH = 0.037   # 2026-10-09: 1245 stretches in 45 s
CODE_CHARS_PER_STRETCH = 1400
CODE_INDEX_BYTES_PER_CHAR = 2.4
CODE_INDEX_BYTES_PER_STRETCH = 1600.0

# --- goldmine mechanics mirrored from the ingest pipeline -------------------
DOC_CHUNK_BYTES = 2000      # DigUp's chunk size; goldmine's chunker targets
                            # ~450 tokens/chunk — same magnitude, estimate only
PDF_FIRST_PASS_PAGES = 30   # extract_pdf: first pass reads 30 pages …
PDF_STEP_PAGES = 50         # … then 50-page steps (long files finish last)
MAX_PDF_PAGES = 5000        # config default (config.py IngestCfg.max_pages)
MAX_DOC_CHUNKS = 5000       # config default (chunker.DEFAULT_MAX_CHUNKS)

# --- degraded-mode priors (tool missing) — stated loudly in the output ------
ASSUMED_PDF_PAGES = 10
ASSUMED_AUDIO_SECONDS = 60.0
ASSUMED_VIDEO_SECONDS = 30.0

RATES_PROVENANCE = (
    "rates are DigUp priors (M4 Pro, 2026-10-07) — goldmine calibration pending"
)


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m{s:02d}s"


def estimate_root(root: Path, *, code: bool) -> dict:
    """Walk + count + extrapolate. Pure read: walk_files stats and classifies;
    pdfinfo/ffprobe open only the files the walk admitted as candidates
    (dataless iCloud placeholders were already skipped before any open)."""
    counts: dict[str, int] = {}
    skips: dict[str, int] = {}
    pdf_pages_total = pdf_pages_first = 0
    pdf_files = 0
    doc_chunks = 0
    audio_seconds = video_seconds = 0.0
    screenshots = 0
    code_stretches = 0
    code_index_bytes = 0.0
    seconds = 0.0
    later_seconds = 0.0
    long_files = 0
    unreadable_pdfs = 0
    unreadable_media = 0

    for item in walk_files([root], code=code, exclude=[], max_file_bytes=2**30):
        if isinstance(item, SkipNotice):
            if not item.is_dir:  # folder notices are counted, not itemized
                skips[item.reason] = skips.get(item.reason, 0) + 1
            continue
        counts[item.kind] = counts.get(item.kind, 0) + 1
        if item.kind == "image":
            if rules.is_screenshot(str(item.path), item.path.name):
                screenshots += 1
                seconds += SECONDS_PER_SCREENSHOT
            else:
                seconds += SECONDS_PER_IMAGE
        elif item.kind == "pdf":
            pdf_files += 1
            raw = int(pdf_info(item.path).get("pages") or 0)
            if raw <= 0:
                unreadable_pdfs += 1
                raw = ASSUMED_PDF_PAGES
            total = max(1, min(raw, MAX_PDF_PAGES))
            first = min(total, PDF_FIRST_PASS_PAGES) if total > PDF_FIRST_PASS_PAGES else total
            pdf_pages_total += total
            pdf_pages_first += first
            seconds += first * SECONDS_PER_PDF_PAGE
            if total > first:
                long_files += 1
                later_seconds += (total - first) * SECONDS_PER_PDF_PAGE
        elif item.kind == "doc":
            chunks = max(1, min(MAX_DOC_CHUNKS, item.size // DOC_CHUNK_BYTES))
            doc_chunks += chunks
            seconds += chunks * SECONDS_PER_DOC_CHUNK
        elif item.kind == "audio":
            dur = ffprobe_duration(item.path)
            if dur is None:
                unreadable_media += 1
                dur = ASSUMED_AUDIO_SECONDS
            audio_seconds += dur
        elif item.kind == "video":
            dur = ffprobe_duration(item.path)
            if dur is None:
                unreadable_media += 1
                dur = ASSUMED_VIDEO_SECONDS
            video_seconds += dur
        elif item.kind == "code":
            characters = item.size // 8 if item.path.suffix.lower() == ".ipynb" else item.size
            stretches = max(1, (characters + CODE_CHARS_PER_STRETCH - 1) // CODE_CHARS_PER_STRETCH)
            code_stretches += stretches
            code_index_bytes += characters * CODE_INDEX_BYTES_PER_CHAR + stretches * CODE_INDEX_BYTES_PER_STRETCH

    # audio/video time from the accumulated durations (DigUp: seconds * rate)
    seconds += audio_seconds * SECONDS_PER_AUDIO_SECOND + video_seconds * SECONDS_PER_VIDEO_SECOND

    notes = [RATES_PROVENANCE,
             "nothing was read, embedded, or written — metadata only",
             "audio prior is embed-only; goldmine also transcribes (whisper), so audio time is optimistic"]
    degraded = []
    if pdf_files > 0 and pdfinfo_path() is None:
        degraded.append("pdf pages")
        notes.append(f"pdf pages unknown (pdfinfo missing) — assumed {ASSUMED_PDF_PAGES} pages/pdf")
    elif unreadable_pdfs:
        notes.append(f"{unreadable_pdfs} pdf(s) unreadable by pdfinfo — assumed {ASSUMED_PDF_PAGES} pages each")
    if ffprobe_path() is None:
        degraded.append("av durations")
        notes.append(f"av durations unknown (ffprobe missing) — assumed "
                     f"{ASSUMED_AUDIO_SECONDS:.0f}s audio / {ASSUMED_VIDEO_SECONDS:.0f}s video")
    elif unreadable_media:
        notes.append(f"{unreadable_media} audio/video file(s) unreadable by ffprobe — assumed "
                     f"{ASSUMED_AUDIO_SECONDS:.0f}s audio / {ASSUMED_VIDEO_SECONDS:.0f}s video")
    return {
        "root": str(root), "files": counts, "skipped": skips,
        "screenshot_guess": screenshots,
        "pdf_files": pdf_files, "pdf_pages_total": pdf_pages_total,
        "pdf_pages_first_pass": pdf_pages_first,
        "doc_chunks": doc_chunks,
        "audio_seconds": round(audio_seconds, 1), "video_seconds": round(video_seconds, 1),
        "code_stretches": code_stretches,
        "code_index_mb": round(code_index_bytes / 2**20, 1),
        "long_files": long_files,
        "seconds": round(seconds, 1), "later_seconds": round(later_seconds, 1),
        "total_seconds": round(seconds + later_seconds, 1),
        "degraded": degraded, "notes": notes,
    }


def cmd_estimate(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    if not root.exists():
        raise UserFacingError(
            kind="usage_error", message=f"estimate: no such path: {root}",
            why="estimate walks the folder you point it at",
            fixes=["check the path", "goldmine help estimate"], doctor=False, exit_code=2,
        )
    est = estimate_root(root, code=bool(getattr(args, "code", False)))
    if getattr(args, "json", False):
        import json

        print(json.dumps(est, ensure_ascii=False, indent=2))
        return 0
    files_total = sum(est["files"].values())
    skips_total = sum(est["skipped"].values())
    print(f"estimate for {est['root']} (read-only: nothing was read, embedded, or written)")
    print()
    print(f"files: {files_total} indexable" + (f", {skips_total} skipped" if skips_total else ""))
    for kind in ("doc", "pdf", "image", "audio", "video", "code"):
        n = est["files"].get(kind, 0)
        if not n:
            continue
        line = f"  {kind:<7}{n:>6}"
        if kind == "pdf":
            line += f"   {est['pdf_pages_total']} pages ({est['pdf_pages_first_pass']} in the first pass)"
        elif kind == "image" and est["screenshot_guess"]:
            line += f"   incl. {est['screenshot_guess']} screenshot-guessed (costlier)"
        elif kind == "doc":
            line += f"   ~{est['doc_chunks']} chunks"
        elif kind == "audio":
            line += f"   {_fmt_duration(est['audio_seconds'])} total"
        elif kind == "video":
            line += f"   {_fmt_duration(est['video_seconds'])} total"
        elif kind == "code":
            line += f"   ~{est['code_stretches']} stretches, ~{est['code_index_mb']} MB code index"
        print(line)
    total_min = est["total_seconds"] / 60.0
    first_min = est["seconds"] / 60.0
    later_min = est["later_seconds"] / 60.0
    line = f"first pass ≈ {first_min:.1f} min"
    if later_min > 0:
        line += f", {est['long_files']} long file(s) after ≈ {later_min:.1f} min"
    print()
    print(line + f"  →  total ≈ {max(total_min, 0.1):.1f} min on an M4 Pro-class machine")
    print(f"rates: {RATES_PROVENANCE}")
    for note in est["notes"][2:]:
        print(f"note: {note}")
    return 0
