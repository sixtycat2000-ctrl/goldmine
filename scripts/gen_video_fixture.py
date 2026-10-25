#!/usr/bin/env python3
"""Generate the deterministic keyframe fixture video (video-frames-001).

tests/fixtures_data/media/ref-video.mp4: four STATIC 5 s scenes (red / blue /
green / white, hard cuts at 5/10/15 s) at 320x180 — below the 1024 budget, so
frames are never upscaled — plus a 440 Hz sine soundtrack. Sampling at the
frozen grid (start 0.5, step 3) yields 7 points; the aHash dedup keeps one
per scene (4 kept, spans 0.5→6.5 / 6.5→12.5 / 12.5→15.5 / 15.5→20.0) and
merges the 3 within-scene duplicates — exactly the shape the tests pin.

Byte determinism is a property of THIS script on one ffmpeg build (fixed
encoder settings, single thread); the checked-in file is the fixture and its
sha256 is pinned below + asserted by tests/test_video_frames.py, so a drifted
regeneration can never slip in unnoticed.

Usage:
  python3 scripts/gen_video_fixture.py            # generate + verify sha
  python3 scripts/gen_video_fixture.py --write    # regenerate + re-pin (慎用)
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures_data" / "media" / "ref-video.mp4"

#: Pinned at first generation (2026-10-25, ffmpeg 9.0.2, arm64).
SHA256 = "32baa650f7331c0ffe74e783eef1c4988be10f7c132c5893165609d2017c112c"

#: Four STATIC, spatially distinct patterns (uniform colors would all hash
#: to 0 — any two flat frames dedup as identical, which is real aHash
#: behavior, not a fixture property). Pairwise 8×8 aHash distances of these
#: four: 23-38 bits, far above the 4-bit dedup threshold (probed 2026-10-25).
SCENES = (
    "smptebars=s={size}:d=5:r={rate}",
    "rgbtestsrc=s={size}:d=5:r={rate}",
    "color=c=navy:s={size}:d=5:r={rate},drawbox=x=20:y=20:w=130:h=60:"
    "c=yellow:t=fill,drawbox=x=170:y=100:w=130:h=60:c=red:t=fill",
    "color=c=black:s={size}:d=5:r={rate},drawgrid=w=80:h=90:t=8:c=white",
)
SIZE = "320x180"
RATE = "12.5"
DURATION = 20.0


def ffmpeg_path() -> str:
    tool = shutil.which("ffmpeg")
    if tool is None:
        sys.exit("ffmpeg not found (brew install ffmpeg)")
    return tool


def build(dst: Path) -> None:
    argv: list[str] = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]
    for scene in SCENES:
        argv += ["-f", "lavfi", "-i", scene.format(size=SIZE, rate=RATE)]
    argv += [
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION:g}",
        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
        "-map", "[v]", "-map", "4:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-threads", "1",
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        str(dst),
    ]
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(argv, capture_output=True, timeout=120)
    if result.returncode != 0:
        sys.exit(f"ffmpeg failed rc={result.returncode}: "
                 f"{result.stderr.decode()[:400]}")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                        help="regenerate the fixture and re-pin SHA256 here")
    args = parser.parse_args()

    if args.write:
        build(OUT)
        digest = sha256_of(OUT)
        text = Path(__file__).read_text(encoding="utf-8")
        updated = text.replace(f'SHA256 = "{SHA256}"', f'SHA256 = "{digest}"')
        if updated == text:
            sys.exit("SHA256 pin not found in this script — edit manually")
        Path(__file__).write_text(updated, encoding="utf-8")
        print(f"generated + pinned: {OUT} sha256={digest}")
        return

    if not OUT.is_file():
        sys.exit(f"fixture missing: {OUT} (run with --write on a ffmpeg host)")
    digest = sha256_of(OUT)
    if digest != SHA256:
        sys.exit(f"fixture sha drift: {digest} != pinned {SHA256}")
    print(f"ok: {OUT.name} sha256={digest} size={OUT.stat().st_size}")


if __name__ == "__main__":
    main()
