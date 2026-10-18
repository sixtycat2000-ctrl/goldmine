"""Universal user-facing error shape (v2 item 17 / F1, docs/v2/03 §5.7).

Every user-facing failure prints, in this order, on stderr:

    <what happened, one line, lower-case, no stack>
    why: <root cause as data — path, exit code, missing thing>
    fix: <exact copy-pasteable command>            <- >= 1 line, mechanical
    more: goldmine doctor                          <- when the cause may be broader

Rules (03 §5.7): `fix:` is a real command (never "install the tool"); when two
remedies exist both print (`fix:` then `or:`); `--json` mode gets the same
fields as structured keys on stdout — ``{"error": {kind, message, why, fix,
doctor}}`` (stable keys, `fix` an array of strings) — so scripts can react;
exit codes are unchanged (SPEC §1.1) and live with the caller.

The no-fix-less-errors guarantee is structural: render_error() and
UserFacingError both REFUSE to build a failure without at least one non-empty
fix line (ValueError at construction), so a fix-less error cannot ship.

The documented exception lives outside these builders: main()'s catch-all
(`_unhandled_error` in cli.py) prints a deliberately fix-less partial shape
(error + more + report) for truly unexpected crashes — there is no honest
mechanical fix for a bug. Everything user-diagnosable goes through here.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

__all__ = ["UserFacingError", "render_error_lines", "render_error"]

DOCTOR_COMMAND = "goldmine doctor"


@dataclass
class UserFacingError(Exception):
    """A failure that renders in the universal 4-line shape (03 §5.7).

    Raise from any command handler; main() renders it and returns
    `exit_code`. `fixes` must contain at least one non-empty, exact,
    copy-pasteable command — enforced here.
    """

    kind: str                      # machine-stable snake_case id (--json "kind")
    message: str                   # WHAT happened, one line, no stack
    why: str = ""                  # root cause as data (path/exit code/missing thing)
    fixes: list[str] = field(default_factory=list)
    doctor: bool = True            # whether "more: goldmine doctor" applies
    exit_code: int = 1             # SPEC §1.1; callers override (4, 3, 2, 5...)

    def __post_init__(self) -> None:
        fixes = [f.strip() for f in self.fixes if f and f.strip()]
        if not fixes:
            raise ValueError(
                f"fix-less error forbidden (03 §5.7): kind={self.kind!r} "
                "needs at least one copy-pasteable fix command"
            )
        self.fixes = fixes
        super().__init__(self.message)

    def to_json(self) -> dict:
        """The stable --json error object: {"error": {...}} (SPEC §1.2 data
        channel). Keys are frozen: kind, message, why, fix, doctor."""
        return {
            "error": {
                "kind": self.kind,
                "message": self.message,
                "why": self.why,
                "fix": list(self.fixes),
                "doctor": bool(self.doctor),
            }
        }


def render_error_lines(*, kind: str, message: str, why: str,
                       fixes: list[str], doctor: bool) -> list[str]:
    """The 4-line human shape as lines (no trailing newline)."""
    fixes = [f.strip() for f in fixes if f and f.strip()]
    if not fixes:
        raise ValueError(
            f"fix-less error forbidden (03 §5.7): kind={kind!r} "
            "needs at least one copy-pasteable fix command"
        )
    lines = [message]
    if why:
        lines.append(f"why: {why}")
    lines.append(f"fix: {fixes[0]}")
    for extra in fixes[1:]:
        lines.append(f"or: {extra}")
    if doctor:
        lines.append(f"more: {DOCTOR_COMMAND}")
    return lines


def render_error(*, kind: str, message: str, why: str, fixes: list[str],
                 doctor: bool = True, json_mode: bool = False,
                 stream=None) -> None:
    """Print one failure in the universal shape.

    Human form always goes to stderr (diagnostics channel, SPEC §1.2). In
    --json mode the structured error object additionally goes to stdout so
    scripts can react; exit codes stay with the caller.
    """
    lines = render_error_lines(kind=kind, message=message, why=why,
                               fixes=fixes, doctor=doctor)
    out = stream if stream is not None else sys.stderr
    print("\n".join(lines), file=out)
    if json_mode:
        payload = UserFacingError(
            kind=kind, message=message, why=why, fixes=fixes, doctor=doctor,
        ).to_json()
        print(json.dumps(payload, indent=2, ensure_ascii=False))
