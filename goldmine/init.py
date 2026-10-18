"""`goldmine init` — the resumable onboarding state machine (v2 item 20 / F4,
docs/v2/REDESIGN §5 row 20, 03 §5.1).

Eight states, each idempotent, each saved on completion:

    1 env      python/numpy/tools (doctor's probes, quiet)
    2 model    detect / install / adopt the embedding model (or skip -> lexical)
    3 probe    `model check` live arch probe (non-fatal warn on failure)
    4 folders  THE SCOPE STEP — explicit folder choice only (see below)
    5 media    ASR + OCR offer (`--media` = yes without prompting)
    6 ingest   first ingest over the chosen roots (standard ticker)
    7 demo     one real search for the corpus's most common word
    8 done     summary + next commands

Progress lives in ``<home>/init_state.json`` (``{"step": N, "state": <name>,
"choices": …, "updated_at": …}``); Ctrl-C (exit 130) or a crash resumes at the
saved state. ``--restart`` wipes it.

SCOPE INVARIANT (product-owner requirement): goldmine must never index anything
the user did not explicitly choose. There is no default sweep of
~/Documents/~/Downloads/~/Desktop. The folder step offers to CREATE a dedicated
folder (``~/Documents/goldmine/``) as the zero-thought option, or takes paths
the user types one by one; ``--yes`` creates the dedicated folder and never
sweeps existing directories silently; explicit ``--root PATH`` flags are the
user's own choice and the only roots ``--yes`` will index besides the dedicated
folder. Every chosen root is echoed with its file/media estimate before ingest
starts, ``[init] roots`` in config.toml records the choice, and ``status``
shows the indexed roots.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    Config,
    resolve_home,
    set_config_init_roots,
    set_config_keys,
)
from .errors import UserFacingError, render_error
from .model_manifest import DEFAULT_EMBEDDING_KEY

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_PREREQ = 4
EXIT_PARTIAL = 5
EXIT_INTERRUPTED = 130

#: The scope invariant, verbatim — printed by the folder step and at done, and
#: stated in README + USER_GUIDE. Do not reword casually; tests match it.
INVARIANT = "goldmine only searches folders you add — never your whole Mac."

#: The zero-thought folder option (created on request, empty, then offered).
DEDICATED_DIR = Path("~/Documents/goldmine")

#: Manifest keys the media step installs (whisper + its VAD — ASR needs both,
#: the VAD pin is under 1 MB).
WHISPER_KEY = "whisper-large-v3-turbo-q5_0"
VAD_KEY = "silero-vad-v5.1.2"

#: Folder-step guards: warn + confirm above these (journey spec C10: a
#: 100 GB-class root gets a confirm/estimate guard; here, at init).
GUARD_FILES = 10_000
GUARD_MEDIA_HOURS = 2.0
GUARD_100GB_FILES = 100_000

#: TCC-guarded locations for the PRE-warning (03 §5.1: warn before the walk).
TCC_FULL_DISK = ("Library",)  # fails outright without Full Disk Access
TCC_ASKABLE = ("Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures")

STATES = ("env", "model", "probe", "folders", "media", "ingest", "demo", "done")

_DEMO_STOPWORDS = {
    "that", "this", "with", "from", "have", "what", "when", "were", "they",
    "will", "would", "there", "their", "about", "which", "these", "those",
    "other", "because", "through", "should", "where", "while", "being",
    "been", "into", "also", "than", "then", "them", "some", "very", "over",
    "under", "more", "most", "such", "only", "each", "like", "just", "made",
    "make", "used", "using", "file", "files", "name", "names", "text",
    "page", "pages", "content", "http", "https", "www", "com",
}


# ------------------------------------------------------------------ state file


def state_path(cfg: Config) -> Path:
    return resolve_home(cfg) / "init_state.json"


def load_state(cfg: Config) -> dict:
    try:
        state = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict) or not isinstance(state.get("choices"), dict):
        return {}
    return state


def save_state(cfg: Config, step: int, choices: dict) -> None:
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "step": step,
                "state": STATES[step - 1] if 1 <= step <= len(STATES) else "?",
                "choices": choices,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def wipe_state(cfg: Config) -> None:
    state_path(cfg).unlink(missing_ok=True)


# ----------------------------------------------------------------- run context


@dataclass
class _Ctx:
    cfg: Config
    db: Path
    config_path: Path
    yes: bool = False
    media_flag: bool = False
    model_path: Path | None = None
    roots_arg: list[str] = field(default_factory=list)
    skip_demo: bool = False
    backend_name: str | None = None
    json_mode: bool = False
    quiet: bool = False
    no_color: bool = False
    choices: dict = field(default_factory=dict)
    partial: bool = False
    resumed: bool = False
    #: prompts: None = builtins.input (resolved at CALL time so tests can
    #: monkeypatch builtins.input); tests may also pass a callable directly.
    input_fn: object | None = None

    @property
    def backend_id(self) -> str:
        return self.backend_name or self.cfg.backend

    def say(self, text: str) -> None:
        """Narration: stdout is init's product; in --json mode narration moves
        to stderr so stdout stays a single machine payload."""
        stream = sys.stderr if self.json_mode else sys.stdout
        print(text, file=stream)

    def ask(self, prompt: str) -> str:
        import builtins

        # print the prompt ourselves: `input()` echoes only on a real TTY,
        # and scripted/piped stdin would swallow it (prompts are narration)
        self.say(prompt)
        fn = self.input_fn if self.input_fn is not None else builtins.input
        return str(fn("")).strip()

    def confirm(self, prompt: str, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        answer = self.ask(f"{prompt} {suffix}").lower()
        if not answer:
            return default
        return answer in ("y", "yes")


# --------------------------------------------------------------------- the run


def run_init(
    cfg: Config,
    *,
    db: Path,
    config_path: Path,
    yes: bool = False,
    media_flag: bool = False,
    model_path: Path | None = None,
    roots_arg: list[str] | None = None,
    restart: bool = False,
    skip_demo: bool = False,
    backend_name: str | None = None,
    json_mode: bool = False,
    quiet: bool = False,
    no_color: bool = False,
) -> int:
    """Run (or resume) the init state machine; returns the exit code."""
    ctx = _Ctx(
        cfg=cfg, db=db, config_path=config_path, yes=yes, media_flag=media_flag,
        model_path=Path(model_path).expanduser() if model_path else None,
        roots_arg=[str(r) for r in (roots_arg or [])],
        skip_demo=skip_demo, backend_name=backend_name, json_mode=json_mode,
        quiet=quiet, no_color=no_color,
    )

    if restart:
        wipe_state(cfg)
    saved = load_state(cfg)
    step = 1
    if saved and isinstance(saved.get("step"), int) and 1 < saved["step"] <= len(STATES):
        step = saved["step"]
        ctx.choices = dict(saved["choices"])
        ctx.resumed = True
        ctx.say(f"resuming init at step {step}/8 ({STATES[step - 1]}) — `goldmine init --restart` starts over")

    handlers = {
        "env": _state_env,
        "model": _state_model,
        "probe": _state_probe,
        "folders": _state_folders,
        "media": _state_media,
        "ingest": _state_ingest,
        "demo": _state_demo,
        "done": _state_done,
    }
    try:
        while step <= len(STATES):
            handlers[STATES[step - 1]](ctx)
            step += 1
            if step <= len(STATES):
                save_state(cfg, step, ctx.choices)
    except (KeyboardInterrupt, EOFError) as exc:
        save_state(cfg, min(step, len(STATES)), ctx.choices)
        reason = (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "input ended"
        )
        ctx.say(
            f"init: {reason} — progress saved. Re-run `goldmine init` to resume "
            f"at step {min(step, len(STATES))}/8 "
            f"({STATES[min(step, len(STATES)) - 1]}), "
            "or `goldmine init --restart` to start over"
        )
        return EXIT_INTERRUPTED
    except UserFacingError as exc:
        render_error(
            kind=exc.kind, message=exc.message, why=exc.why, fixes=exc.fixes,
            doctor=exc.doctor, json_mode=json_mode,
        )
        return exc.exit_code

    wipe_state(cfg)
    if json_mode:
        print(json.dumps(_json_summary(ctx), indent=2, ensure_ascii=False))
    return EXIT_PARTIAL if ctx.partial else EXIT_OK


def _json_summary(ctx: _Ctx) -> dict:
    from .cli import _envelope  # lazy: cli imports init lazily too

    roots = [str(r) for r in ctx.choices.get("roots", [])]
    media = {
        "asr": _asr_ready(ctx.cfg),
        "ocr": _ocr_ready(ctx.cfg),
    }
    return _envelope(
        "init",
        ok=True,
        index=str(ctx.db),
        roots=roots,
        model=ctx.choices.get("model"),
        media=media,
        resumed=ctx.resumed,
    )


# ---------------------------------------------------------------- step 1: env


def _state_env(ctx: _Ctx) -> None:
    """Doctor's probes, quiet: python + numpy are fatal-if-missing; optional
    tools are warn-only with fix lines (a missing pdftotext downgrades
    capability, it never breaks init)."""
    from . import doctor

    ctx.say("step 1/8 · environment")
    while True:
        checks = [doctor.check_python(), doctor.check_numpy()]
        tools = [_tool_row(name, hint) for name, hint in _OPTIONAL_TOOLS]
        fatal = [c for c in checks if c.status == "fail"]
        for c in checks + tools:
            marker = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[c.status]
            line = f"  {marker} {c.name}: {c.detail}"
            if c.fix_hint:
                line += f"\n         fix: {c.fix_hint}"
            ctx.say(line)
        if not fatal:
            return
        if ctx.yes:
            raise UserFacingError(
                kind="init_env_missing",
                message="init: environment checks failed",
                why="; ".join(f"{c.name}: {c.detail}" for c in fatal),
                fixes=[c.fix_hint for c in fatal if c.fix_hint] or ["goldmine doctor"],
                doctor=True,
                exit_code=EXIT_PREREQ,
            )
        answer = ctx.ask(
            "fix the above, then press Enter to re-check (or 's' to continue anyway)"
        )
        if answer.lower() == "s":
            return


#: name -> (present check argv, fix hint); presence-only, warn when missing
_OPTIONAL_TOOLS = [
    ("pdftotext", "brew install poppler (PDF text extraction)"),
    ("ffmpeg", "brew install ffmpeg (audio/video metadata + ASR transcode)"),
]


def _tool_row(name: str, fix_hint: str):
    from .doctor import CheckResult, STATUS_OK, STATUS_WARN

    import shutil

    if shutil.which(name) is None:
        return CheckResult(name, STATUS_WARN, "not found (optional)", fix_hint)
    return CheckResult(name, STATUS_OK, "found")


# --------------------------------------------------------------- step 2: model


def _state_model(ctx: _Ctx) -> None:
    from .model import ModelError, ModelManager

    ctx.say("step 2/8 · embedding model")
    if ctx.backend_id in ("mock", "foresight"):
        ctx.choices["model"] = ctx.backend_id
        ctx.say(f"  backend {ctx.backend_id} — no embedding model needed")
        return
    manager = ModelManager(ctx.cfg, err=print_to_stderr)
    if ctx.model_path is not None:
        try:
            path = manager.adopt(ctx.model_path, DEFAULT_EMBEDDING_KEY)
        except ModelError as exc:
            raise UserFacingError(
                kind="init_model_adopt",
                message=f"init: cannot adopt {ctx.model_path}",
                why=str(exc),
                fixes=[
                    f"goldmine model adopt <file> {DEFAULT_EMBEDDING_KEY}",
                    "goldmine model list",
                ],
                doctor=False,
                exit_code=EXIT_RUNTIME,
            ) from exc
        ctx.choices["model"] = DEFAULT_EMBEDDING_KEY
        ctx.say(f"  adopted {path}")
        return

    path, source = manager.resolve(DEFAULT_EMBEDDING_KEY)
    if path is not None:
        ctx.choices["model"] = DEFAULT_EMBEDDING_KEY
        ctx.say(f"  found {DEFAULT_EMBEDDING_KEY} ({source}: {path})")
        return

    size_mb = _embed_size_mb()
    if ctx.yes:
        _install_embed_model(ctx, manager, size_mb)
        return
    ctx.say(
        f"  the embedding model {DEFAULT_EMBEDDING_KEY} is not installed "
        f"(~{size_mb} MB download, sha256-verified against a pinned manifest)."
    )
    while True:
        answer = ctx.ask(
            "  [i] install it now · [a] adopt a local .gguf file · "
            "[s] skip (keyword-only index) — choose [i/a/s]"
        ).lower()
        if answer == "i":
            _install_embed_model(ctx, manager, size_mb)
            return
        if answer == "a":
            raw = ctx.ask("  path to the .gguf file")
            if not raw:
                ctx.say("  no path given")
                continue
            try:
                path = manager.adopt(Path(raw).expanduser(), DEFAULT_EMBEDDING_KEY)
            except ModelError as exc:
                ctx.say(f"  cannot adopt that file: {exc}")
                continue
            ctx.choices["model"] = DEFAULT_EMBEDDING_KEY
            ctx.say(f"  adopted {path}")
            return
        if answer == "s":
            ctx.choices["model"] = "lexical"
            ctx.say(
                "  skipped — the index will be keyword-only (no semantic "
                "search). Install later with: goldmine model install "
                f"{DEFAULT_EMBEDDING_KEY}"
            )
            return
        ctx.say("  answer i, a, or s")


def _embed_size_mb() -> int:
    from .model_manifest import by_key

    entry = by_key(DEFAULT_EMBEDDING_KEY)
    return round(entry.bytes / (1 << 20)) if entry else 310


def _install_embed_model(ctx: _Ctx, manager, size_mb: int) -> None:
    from .model import ModelError

    ctx.say(f"  installing {DEFAULT_EMBEDDING_KEY} (~{size_mb} MB, resumable, "
            "sha256-verified — progress lines follow on stderr)…")
    try:
        path = manager.install(DEFAULT_EMBEDDING_KEY)
    except ModelError as exc:
        raise UserFacingError(
            kind="init_model_install",
            message=f"init: could not install {DEFAULT_EMBEDDING_KEY}",
            why=str(exc),
            fixes=[
                f"goldmine model install {DEFAULT_EMBEDDING_KEY}",
                f"goldmine init --model-path <file.gguf>",
            ],
            doctor=True,
            exit_code=EXIT_PREREQ,
        ) from exc
    ctx.choices["model"] = DEFAULT_EMBEDDING_KEY
    ctx.say(f"  installed {path}")


def print_to_stderr(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------- step 3: probe


def _state_probe(ctx: _Ctx) -> None:
    ctx.say("step 3/8 · live model probe")
    if ctx.choices.get("model") in ("lexical", "mock", "foresight", None):
        ctx.say("  skipped — nothing to probe (no local embedding model in play)")
        return
    from .model import ModelManager

    manager = ModelManager(ctx.cfg, err=print_to_stderr)
    try:
        findings = manager.check()
    except Exception as exc:  # the probe is a taste, never a gate here
        ctx.say(f"  warn: probe could not run ({exc}) — fix: goldmine model check")
        return
    if findings.get("ok"):
        ctx.say(f"  live embed ok — {findings.get('live_embed')}")
        return
    ctx.say(f"  warn: {findings.get('live_embed')}")
    if findings.get("brew_trap"):
        ctx.say(f"  {findings['brew_trap']}")
    ctx.say("  non-fatal — first ingest will name the same problem with its fix; "
            "check now with: goldmine model check")


# -------------------------------------------------------------- step 4: folders


def _state_folders(ctx: _Ctx) -> None:
    ctx.say("step 4/8 · folders")
    ctx.say(f"  {INVARIANT}")

    if ctx.roots_arg:
        roots = []
        for raw in ctx.roots_arg:
            path = _validated_root(ctx, raw)
            if path is not None:
                _check_root_guards(ctx, path, yes_refuse=True)
                roots.append(str(path))
        if not roots:
            raise UserFacingError(
                kind="init_no_roots",
                message="init: none of the --root paths could be used",
                why=", ".join(ctx.roots_arg),
                fixes=["goldmine init   # interactive folder choice"],
                doctor=False,
                exit_code=EXIT_PREREQ,
            )
        _finish_folders(ctx, roots)
        return
    if ctx.yes:
        dedicated = DEDICATED_DIR.expanduser()
        dedicated.mkdir(parents=True, exist_ok=True)
        _finish_folders(ctx, [str(dedicated)])
        return

    ctx.say("  (a) create and use ~/Documents/goldmine/  (recommended — empty, yours)")
    ctx.say("  (b) add folders one at a time")
    roots: list[str] = []
    while True:
        answer = ctx.ask("  choose [a/b]").lower()
        if answer == "a":
            dedicated = DEDICATED_DIR.expanduser()
            dedicated.mkdir(parents=True, exist_ok=True)
            roots = [str(dedicated)]
            break
        if answer == "b":
            roots = _ask_folders(ctx)
            if roots:
                break
            ctx.say("  add at least one folder (empty selection is not a choice)")
            continue
        ctx.say("  answer a or b")

    _finish_folders(ctx, roots)


def _ask_folders(ctx: _Ctx) -> list[str]:
    roots: list[str] = []
    while True:
        raw = ctx.ask("  folder to add (empty when done)")
        if not raw:
            return roots
        path = _validated_root(ctx, raw)
        if path is None:
            continue
        note = _tcc_note(path)
        if note:
            ctx.say(f"  note: {note}")
        try:
            os.scandir(path).close()  # readability probe (DigUp: unreadable ≠ empty)
        except PermissionError:
            ctx.say(
                f"  can't read {path} (macOS privacy settings?) — not added. "
                "Grant Full Disk Access to your terminal, or pick a copy elsewhere."
            )
            continue
        except OSError:
            pass  # exists() passed; let ingest surface anything stranger
        if any(str(path) == r for r in roots):
            ctx.say("  already added")
            continue
        if not _check_root_guards(ctx, path, yes_refuse=False):
            continue  # declined — back to the prompt, nothing added
        roots.append(str(path))


def _validated_root(ctx: _Ctx, raw: str) -> Path | None:
    path = Path(raw).expanduser()
    if not path.exists():
        ctx.say(f"  no such folder: {path}")
        return None
    if not path.is_dir():
        ctx.say(f"  not a folder: {path} (files are added via `goldmine ingest <file>`)")
        return None
    return path


def _tcc_note(path: Path) -> str | None:
    """Pre-warning for macOS-protected locations (03 §5.1 folders step)."""
    home = Path("~").expanduser()
    try:
        rel = path.resolve().relative_to(home)
    except ValueError:
        return None
    top = rel.parts[0] if rel.parts else ""
    if top in TCC_FULL_DISK:
        return (
            f"{path} is under ~/Library, which is protected by macOS — indexing "
            "it will fail without Full Disk Access for your terminal. Prefer a "
            "folder outside ~/Library, or grant access first."
        )
    if top in TCC_ASKABLE:
        return (
            f"{path} is a macOS-protected location — the system may ask "
            "permission to read it (click Allow); if denied, goldmine reports "
            "the folder unreadable instead of indexing nothing silently."
        )
    return None


def _check_root_guards(ctx: _Ctx, path: Path, *, yes_refuse: bool) -> bool:
    """Estimate one root and enforce the journey-spec guards: >10k files or
    >2h audio/video warn + confirm; a 100 GB-class root confirms (default NO)
    interactively and is REFUSED under --yes (never sweep big roots silently).
    Returns True when the root may be added; False when the user declined
    (interactive only — a decline never aborts init, it just doesn't add)."""
    from .output import RootMedia, scan_root_media

    medias = scan_root_media(
        [str(path)],
        max_file_mb=ctx.cfg.ingest.max_file_mb,
        audio_cap_s=float(ctx.cfg.ingest.audio_max_minutes) * 60.0,
    )
    rm = medias[0] if medias else RootMedia(root=str(path))
    hours = (rm.media_seconds or 0.0) / 3600.0
    if rm.files >= GUARD_100GB_FILES:
        reason = (
            f"this looks like a 100 GB-class corpus (≈{rm.files:,} files in {path})"
        )
        if yes_refuse and ctx.yes:
            raise UserFacingError(
                kind="init_root_too_big",
                message="init: refusing to index a 100 GB-class root without confirmation",
                why=reason,
                fixes=[
                    "goldmine init   # interactive — review and confirm the big root",
                    "goldmine init --yes --root <smaller-folder>",
                ],
                doctor=False,
                exit_code=EXIT_PREREQ,
            )
        return ctx.confirm(f"  {reason} — index it anyway?", default=False)
    if rm.files > GUARD_FILES:
        line = f"  {path}: {rm.files:,} files (more than {GUARD_FILES:,}) — a long first ingest"
    elif hours > GUARD_MEDIA_HOURS:
        line = f"  {path}: ≈{hours:.1f}h of audio/video (more than {GUARD_MEDIA_HOURS:g}h) — transcription takes time"
    else:
        return True
    ctx.say(line)
    if ctx.yes:
        ctx.say("  (--yes: proceeding with the explicitly given root)")
        return True
    return ctx.confirm("  add it anyway?", default=False)


def _finish_folders(ctx: _Ctx, roots: list[str]) -> None:
    ctx.choices["roots"] = roots
    set_config_init_roots(ctx.config_path, roots)
    ctx.cfg.init.roots = list(roots)
    ctx.say("  searching:")
    for r in roots:
        ctx.say(f"    {r}")
    ctx.say(f"  (recorded in {ctx.config_path} — only these folders are ever indexed)")


# --------------------------------------------------------------- step 5: media


def _state_media(ctx: _Ctx) -> None:
    from .model import ModelError, ModelManager
    from .ocr import ensure_helper

    ctx.say("step 5/8 · media (speech transcription + screenshot OCR)")
    if ctx.yes and not ctx.media_flag:
        ctx.say(
            "  skipped (--yes stays lean) — enable later with: goldmine init --media"
        )
        _say_media_skip_note(ctx)
        return
    if not ctx.media_flag:
        if not ctx.confirm(
            "  enable speech transcription (whisper, +574 MB download) and "
            "screenshot/scan reading (OCR helper compile)?",
            default=False,
        ):
            ctx.choices["media"] = False
            if not _say_media_skip_note(ctx):
                ctx.say(
                    "  skipped — audio/screenshots stay metadata-only. Enable later "
                    "with `goldmine init --media` (or `goldmine model install "
                    f"{WHISPER_KEY}`)."
                )
            return
    manager = ModelManager(ctx.cfg, err=print_to_stderr)
    for key in (WHISPER_KEY, VAD_KEY):
        path, source = manager.resolve(key)
        if path is not None:
            ctx.say(f"  {key}: already installed ({source})")
            continue
        try:
            path = manager.install(key)
        except ModelError as exc:
            ctx.say(
                f"  warn: {key} install failed ({exc}) — ASR stays off; retry "
                f"later with: goldmine model install {key}"
            )
    set_config_keys(ctx.config_path, "ingest", {"asr": "auto"})
    ctx.cfg.ingest.asr = "auto"
    set_config_keys(ctx.config_path, "ocr", {"enabled": True})
    ctx.cfg.ocr.enabled = True
    helper, note = ensure_helper(ctx.cfg)
    if helper is not None:
        ctx.say(f"  OCR helper ready at {helper}" + (f" ({note})" if note else ""))
    else:
        ctx.say(
            f"  note: {note} — screenshots stay metadata-only until the helper "
            "exists (goldmine doctor names the fix)"
        )
    ctx.choices["media"] = True
    ctx.say("  speech transcription on (whisper-large-v3-turbo) · screenshot OCR "
            + ("on" if helper is not None else "off"))


# -------------------------------------------------------------- step 6: ingest


def _state_ingest(ctx: _Ctx) -> None:
    ctx.say("step 6/8 · first ingest")
    roots: list[str] = ctx.choices.get("roots") or []
    if not roots:
        raise UserFacingError(
            kind="init_no_roots",
            message="init: no folders chosen — nothing to ingest",
            why="step 4 never completed",
            fixes=["goldmine init"],
            doctor=False,
            exit_code=EXIT_PREREQ,
        )
    from .output import asr_estimate_enabled, render_root_media, scan_root_media

    file_roots = [r for r in roots if not r.startswith(("http://", "https://"))]
    medias = scan_root_media(
        file_roots,
        max_file_mb=ctx.cfg.ingest.max_file_mb,
        audio_cap_s=float(ctx.cfg.ingest.audio_max_minutes) * 60.0,
    )
    pre = render_root_media(medias, asr=asr_estimate_enabled(ctx.cfg))
    if pre:
        for line in pre.splitlines():
            ctx.say(f"  {line}")
    total_files = sum(m.files for m in medias)
    if total_files == 0:
        ctx.say(
            "  the chosen folder(s) are empty — drop files in and run: "
            f"goldmine ingest {' '.join(roots)}"
        )
        ctx.choices["ingest_done"] = True
        return

    from .backends import BackendUnavailable, create_backend
    from .ingest import IngestPipeline
    from .store import Store

    lexical_only = ctx.choices.get("model") == "lexical"
    backend = None
    if not lexical_only:
        try:
            backend = create_backend(ctx.cfg, ctx.backend_name)
        except BackendUnavailable as exc:
            from .cli import _err_backend  # lazy: cli imports init lazily too

            raise _err_backend(
                exc,
                kind="init_backend_unavailable",
                message="init: no embedding backend available for the first ingest",
                fixes=[
                    "goldmine model check",
                    f"goldmine ingest {' '.join(roots)} --lexical-only",
                ],
            ) from exc

    store = Store(ctx.db)
    ticker = None
    try:
        if not ctx.json_mode and not ctx.quiet and sys.stderr.isatty():
            from .output import IngestTicker
            from .util import ansi_enabled

            ticker = IngestTicker(
                total_files, store=store,
                ansi=ansi_enabled(ctx.no_color),
            )

        def progress(message: str) -> None:
            if ticker is not None:
                ticker.update(message)

        pipeline = IngestPipeline(ctx.cfg, store, backend, lexical_only=lexical_only)
        try:
            report = pipeline.run(list(roots), progress=progress)
        finally:
            if ticker is not None:
                ticker.finish()
        from .render import render_ingest_summary

        for line in render_ingest_summary(report, json_mode=False).splitlines():
            ctx.say(f"  {line}")
        if report.cancelled:
            raise KeyboardInterrupt()
        if report.failed >= 1:
            ctx.partial = True
    finally:
        if backend is not None:
            backend.close()
        store.close()
    ctx.choices["ingest_done"] = True


# ---------------------------------------------------------------- step 7: demo


def _state_demo(ctx: _Ctx) -> None:
    ctx.say("step 7/8 · demo")
    if ctx.skip_demo:
        ctx.say("  skipped (--skip-demo)")
        return
    from .store import Store

    store = Store(ctx.db)
    backend = None
    try:
        vectors = store.counts().vectors
        if vectors < 1:
            ctx.say("  no vectors in the index yet — demo skipped")
            return
        term = pick_demo_term(store)
        if not term:
            ctx.say("  could not pick a demo word — demo skipped")
            return
        backend = _demo_backend(ctx)
        from .search import Searcher

        hits = Searcher(store, backend).search(term, limit=1)
        if not hits:
            ctx.say(f'  nothing found for "{term}" (honest zero — ingest more folders)')
            return
        from .render import render_hits

        ctx.say(f'  search "{term}" →')
        for line in render_hits(hits, False).splitlines():
            ctx.say(f"    {line}")
    except Exception as exc:  # the demo is a taste, never a gate
        ctx.say(f"  demo skipped ({type(exc).__name__}: {exc})")
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        store.close()


def _demo_backend(ctx: _Ctx):
    if ctx.choices.get("model") == "lexical":
        return None
    from .backends import create_backend

    return create_backend(ctx.cfg, ctx.backend_name)


def pick_demo_term(store) -> str | None:
    """The demo query: the most common single word in the corpus (TF-IDF-ish —
    the highest document frequency over content segments), computed from the
    stored body text so path/filename words (which ride in the name rows)
    never win. Bounded sample, deterministic tie-break. None when the corpus
    is empty or opaque."""
    import re

    try:
        rows = store.db.execute(
            "SELECT f.body FROM fts f JOIN segments s ON s.id = f.rowid "
            "WHERE s.kind != 'name' LIMIT 2000"
        ).fetchall()
    except Exception:
        return None
    df: dict[str, int] = {}
    for (body,) in rows:
        words = set(re.findall(r"[a-z][a-z']{3,}", (body or "")[:4000].lower()))
        for word in words:
            if word not in _DEMO_STOPWORDS:
                df[word] = df.get(word, 0) + 1
    if not df:
        return None
    return sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# ---------------------------------------------------------------- step 8: done


def _state_done(ctx: _Ctx) -> None:
    roots = ctx.choices.get("roots") or []
    ctx.say("init complete — 3 commands from zero to a result.")
    ctx.say(f"  index:   {ctx.db}")
    ctx.say("  roots:   " + (" ".join(roots) if roots else "(none — ingest when ready)"))
    ctx.say(f"  model:   {_model_line(ctx)}")
    ctx.say(f"  media:   {_media_line(ctx)}")
    ctx.say(f"  {INVARIANT}")
    ctx.say("next:")
    ctx.say("  goldmine search <words>   find things (filters: goldmine help search-syntax)")
    ctx.say("  goldmine status           what's indexed and what isn't")
    ctx.say("  goldmine open --help      open a hit at its moment (audio seek, editor line)")
    ctx.say("  goldmine help             all topic guides")


def _model_line(ctx: _Ctx) -> str:
    choice = ctx.choices.get("model")
    if choice == "lexical":
        return (
            "keyword-only (no embedding model yet — goldmine model install "
            f"{DEFAULT_EMBEDDING_KEY} adds semantic search)"
        )
    if choice in ("mock", "foresight"):
        return f"backend {choice} (offline/test backend — no model file)"
    from .model import ModelManager

    try:
        path, source = ModelManager(ctx.cfg).resolve(DEFAULT_EMBEDDING_KEY)
    except Exception:
        path, source = None, None
    if path is not None:
        return f"{DEFAULT_EMBEDDING_KEY} ({source}: {path})"
    return f"{DEFAULT_EMBEDDING_KEY} (not resolved)"


def _asr_ready(cfg: Config) -> bool:
    from .output import asr_estimate_enabled

    return bool(asr_estimate_enabled(cfg))


def _say_media_skip_note(ctx: _Ctx) -> bool:
    """F7 (media honesty): a SKIPPED media install does not turn speech
    transcription off — `ingest.asr` defaults to auto, so when whisper already
    resolves on this machine audio files will still be transcribed. Say so,
    and name the off switch; False when the honest note does not apply (no
    whisper -> the old 'stays metadata-only' copy is accurate)."""
    if not _asr_ready(ctx.cfg):
        return False
    ctx.say(
        "  media install skipped — note: speech transcription will still run "
        "on audio files if whisper is available on this machine; disable with "
        "config ingest.asr=off"
    )
    return True


def _ocr_ready(cfg: Config) -> bool:
    from .ocr import helper_path

    return helper_path(cfg) is not None


def _media_line(ctx: _Ctx) -> str:
    bits = [
        "speech transcription " + ("on (whisper)" if _asr_ready(ctx.cfg) else "off (models not installed)"),
        "screenshot OCR " + ("on" if _ocr_ready(ctx.cfg) else "off (helper not installed)"),
    ]
    return " · ".join(bits)
