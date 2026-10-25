"""goldmine open — open a file at a moment/line (v2 item 18 / F2', 03 §5.3).

v2.0 scope (review C9): plain `open`, the QuickTime/IINA jump (DigUp-proven,
Moments.swift:172-217 — this is what makes ASR timestamps clickable), and the
$EDITOR path for text. The 23-editor table + URL schemes defer to v2.1.

Fallback chain (uniform, 03 §5.3): jump mechanism -> plain `open` -> printed
instruction. Every degrade names what happened on stderr; nothing fails the
command; exit stays 0. A jump we can't make is stated, never pretended.

Mechanics are split plan/execute so tests can assert exact argvs without
spawning anything (`--dry-run` exposes the same plan to users, like DigUp's
`Opener.dryRun`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

__all__ = [
    "OpenPlan",
    "clock_fmt",
    "default_app_bundle",
    "editor_command",
    "kind_for_path",
    "plan_open",
    "run_plan",
]

QUICKTIME_BUNDLE = "com.apple.QuickTimePlayerX"
IINA_BUNDLE = "com.colliderli.iina"

#: editors known to accept git's `+LINE` convention (03 §5.3 doc row; the
#: task's vi/vim/nano/emacs plus nvim/less/mg from the same table)
EDITOR_LINE_HINT = {"vi", "vim", "nvim", "nano", "emacs", "less", "mg"}

#: the DigUp QuickTime port (Moments.swift:174-193), verbatim script. Time
#: goes over as whole milliseconds (a decimal point depends on locale).
QUICKTIME_SCRIPT = """
on run argv
    set theFile to POSIX file (item 1 of argv)
    set theTime to (item 2 of argv as integer) / 1000
    tell application "QuickTime Player"
        activate
        set theDocument to open theFile
        set current time of theDocument to theTime
        try
            set miniaturized of (first window whose document is theDocument) to false
        end try
        play theDocument
        return current time of theDocument
    end tell
end run
""".strip()

OSASCRIPT_TIMEOUT_S = 15.0  # -1712 (no answer) must not wedge the CLI


def clock_fmt(seconds: float) -> str:
    """0:06-style copy for instruction lines (h:mm:ss past an hour)."""
    total = max(0, int(round(float(seconds))))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


@dataclass
class OpenPlan:
    """What open will do; `argv` empty means 'nothing to run' (dry-run probe)."""

    path: str
    kind: str
    loc: float | None = None
    how: str = "open"          # quicktime | iina | open | editor | none
    jumped: bool = False       # a moment/line jump was attempted
    argv: list[str] = field(default_factory=list)
    timeout: float | None = None
    note: str = ""             # degrade instruction (stderr) or exit-line extra

    def result_line(self) -> str:
        """The one stdout line: what opened + how (03 §5.3 'the open line
        says which happened')."""
        at = ""
        if self.how == "quicktime":
            return (f"opened {self.path} ({self.kind}) in QuickTime Player "
                    f"at {clock_fmt(self.loc or 0)}")
        if self.how == "iina":
            return (f"opened {self.path} ({self.kind}) in IINA "
                    f"at {clock_fmt(self.loc or 0)}")
        if self.how == "editor":
            editor = editor_command(self.argv[0]) if self.argv else "editor"
            if self.jumped:
                line = self.argv[1] if len(self.argv) > 2 else ""
                return f"opened {self.path} ({self.kind}) in {editor} at line {line.lstrip('+')}"
            return f"opened {self.path} ({self.kind}) in {editor}"
        if self.note and not self.jumped and self.note.startswith("jump to"):
            return f"opened {self.path} ({self.kind}) at start — {self.note}"
        if self.note:  # e.g. the pdf page-jump honesty line
            return f"opened {self.path} ({self.kind}) — {self.note}"
        return f"opened {self.path} ({self.kind})"


def kind_for_path(path: str) -> str:
    """Kind from the extension (rules' extension sets — read-only reuse)."""
    from .ingest import rules

    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    if not ext and str(path).startswith(("http://", "https://")):
        return "webpage"
    if ext in rules.PDF_EXTS:
        return "pdf"
    if ext in rules.IMAGE_EXTS:
        return "image"
    if ext in rules.AUDIO_EXTS:
        return "audio"
    if ext in rules.VIDEO_EXTS:
        return "video"
    if ext in rules.CODE_EXTS:
        return "code"
    if ext in rules.DOC_EXTS:
        return "doc"
    return "doc"  # honest default: `open` handles anything


def default_app_bundle(path: str) -> str | None:
    """Bundle id of the app that would open `path` (mdls, stdlib subprocess).

    None when mdls can't say (unindexed tmp file, mdls missing) — callers
    degrade to plain open, never guess."""
    try:
        proc = subprocess.run(
            ["/usr/bin/mdls", "-raw", "-name", "kMDItemCFBundleIdentifier", str(path)],
            capture_output=True, timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out or out == "(null)":
        return None
    return out


def editor_command(argv0: str) -> str:
    """Basename of the editor command (so /usr/bin/vim still matches vi)."""
    return os.path.basename(argv0.strip()) or argv0.strip()


def plan_open(
    path: str,
    kind: str | None = None,
    loc: float | None = None,
    *,
    probe_default_app: bool = True,
) -> OpenPlan:
    """Build the open plan (no side effects). Never raises on odd input:
    a missing loc/kind degrades to plain open, which handles any file."""
    kind = kind or kind_for_path(path)
    plan = OpenPlan(path=str(path), kind=kind, loc=loc)

    if kind in ("audio", "video") and loc is not None:
        note_jump = f"jump to {clock_fmt(loc)}"
        bundle = default_app_bundle(path) if probe_default_app else None
        if bundle == QUICKTIME_BUNDLE:
            ms = int(round(float(loc) * 1000))
            plan.how = "quicktime"
            plan.jumped = True
            plan.argv = ["/usr/bin/osascript", "-e", QUICKTIME_SCRIPT, str(path), str(ms)]
            plan.timeout = OSASCRIPT_TIMEOUT_S
            plan.note = note_jump  # used only when the jump degrades
            return plan
        if bundle == IINA_BUNDLE:
            url = "iina://open?url=" + quote("file://" + str(path), safe=":/")
            plan.how = "iina"
            plan.jumped = True
            plan.argv = ["open", f"{url}&mpv_start={float(loc):.1f}"]
            plan.note = note_jump
            return plan
        # unknown/other default player: plain open + an honest instruction
        plan.how = "open"
        plan.note = note_jump
        plan.argv = ["open", str(path)]
        return plan

    if kind == "code" or (kind == "doc" and os.environ.get("EDITOR")):
        editor = os.environ.get("EDITOR") or "vi"
        argv = [editor, str(path)]
        if loc is not None and editor_command(editor) in EDITOR_LINE_HINT:
            argv = [editor, f"+{int(loc)}", str(path)]
            plan.jumped = True
        plan.how = "editor"
        plan.argv = argv
        return plan

    if kind == "pdf" and loc is not None:
        plan.how = "open"
        plan.argv = ["open", str(path)]
        plan.note = "opened at file start (page jump not supported)"
        return plan

    plan.argv = ["open", str(path)]
    return plan


def run_plan(plan: OpenPlan, *, dry_run: bool = False) -> OpenPlan:
    """Execute a plan; ALWAYS degrades to plain open, never raises.

    QuickTime failure (-1743 not allowed, -1712 no answer, timeout, anything
    else) -> plain `open` + one stderr line naming it (Moments.swift:196-217).
    A missing editor degrades the same way. Exit code stays with the caller
    (0) — the degrade is the product behaving honestly, not a failure.
    """
    if dry_run or not plan.argv:
        return plan
    try:
        if plan.how == "quicktime":
            proc = subprocess.run(
                plan.argv, capture_output=True, timeout=plan.timeout,
                stdin=subprocess.DEVNULL,
            )
            if proc.returncode == 0:
                return plan
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()
            return _degrade_to_open(plan, tail)
        proc = subprocess.run(plan.argv, stdin=subprocess.DEVNULL)
        if proc.returncode == 0:
            return plan
        if plan.how == "open":
            plan.note = plan.note or f"open failed (exit {proc.returncode})"
            return plan  # nothing to fall back TO — open was the last resort
        return _degrade_to_open(plan, f"exit {proc.returncode}")
    except subprocess.TimeoutExpired:
        return _degrade_to_open(plan, "timed out")
    except FileNotFoundError:
        return _degrade_to_open(plan, f"{plan.argv[0]}: not found")
    except OSError as exc:
        return _degrade_to_open(plan, str(exc))


def _degrade_to_open(plan: OpenPlan, reason: str) -> OpenPlan:
    """Uniform degrade: plain open + one stderr line naming what happened
    (03 §5.3). The stdout result line then states the outcome plainly."""
    fallback = OpenPlan(
        path=plan.path, kind=plan.kind, loc=plan.loc, how="open",
        argv=["open", plan.path],
    )
    if plan.jumped and plan.note:  # we owed the user a moment
        fallback.note = plan.note  # "jump to 0:06"
        detail = (
            f"{plan.how} didn't go to the moment ({reason or 'failed'}) — "
            f"opened at start; {plan.note}"
        )
    else:
        detail = f"{plan.how} failed ({reason or 'failed'}) — opened with the default app"
    print(f"goldmine open: {detail}", file=sys.stderr)
    return fallback
