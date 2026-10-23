"""Shared command configuration, errors, and explicit compatibility dependencies.

No command adapter is imported here. Context-local dependencies preserve the
historical CLI injection surface without mutable cross-command globals.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable
import argparse
import json
from pathlib import Path
from . import __version__, fingerprints
from .backends import BackendUnavailable, create_backend, probe_all
from .config import Config, default_config_path, index_path, load, resolve_home
from .errors import UserFacingError, render_error
from .fingerprints import FingerprintMismatch, build as fp_build, check as fp_check
from .ingest import IngestPipeline
from .store import Store

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_FINGERPRINT = 3
EXIT_PREREQ = 4
EXIT_PARTIAL = 5
EXIT_INTERRUPTED = 130
STALE_SCORER_LABEL = "lexical (stale-vectors)"
CURRENT_EXTRACTORS = set(fingerprints.EXTRACTOR_VERSIONS.values())

@dataclass(frozen=True)
class CommandRuntime:
    make_backend: Callable = create_backend
    pipeline_type: type = IngestPipeline
    render_error: Callable = render_error
    probe_backend: Callable | None = None


_RUNTIME = ContextVar("goldmine.command_runtime", default=CommandRuntime())


def runtime() -> CommandRuntime:
    return _RUNTIME.get()


@contextmanager
def command_runtime(value: CommandRuntime):
    token = _RUNTIME.set(value)
    try:
        yield
    finally:
        _RUNTIME.reset(token)


def _make_backend(cfg: Config, name: str | None):
    return runtime().make_backend(cfg, name)


def _g(args: argparse.Namespace, name: str, default=None):
    """Global/flag value: argparse SUPPRESS means absent unless given."""
    return getattr(args, name, default)


def _load_config(args: argparse.Namespace) -> Config:
    config_flag = _g(args, "config")
    return load(config_path=Path(config_flag) if config_flag else None)


def _db_path(args: argparse.Namespace, cfg: Config) -> Path:
    db = _g(args, "db")
    if db:
        return Path(db)
    return index_path(cfg, _g(args, "index", "default") or "default")


def _emit_json(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _envelope(command: str, **fields) -> dict:
    """The standard machine envelope (D5/F11): every informational --json
    payload opens with the frozen {"schema": "goldmine/1", "command": ...}
    keys — version, backup, backends, open, init share one builder so the
    contract cannot drift per command."""
    return {"schema": "goldmine/1", "command": command, **fields}


def _emit_error(exc: UserFacingError, json_mode: bool = False) -> None:
    # F13: no logger.error beside the shape — the 4-line shape IS the message;
    # logging it too duplicated every failure on stderr.
    runtime().render_error(
        kind=exc.kind, message=exc.message, why=exc.why, fixes=exc.fixes,
        doctor=exc.doctor, json_mode=json_mode,
    )


def _err_no_index(db: Path, *, data_home: str | None = None) -> UserFacingError:
    why = f"no index file at {db}"
    if data_home:
        why += f" (data home: {data_home})"
    # 03 §5.7 row 1: init now ships (v2 item 20), so the mechanical fix is
    # `goldmine init`; plain ingest stays the scriptable second remedy.
    return UserFacingError(
        kind="no_index",
        message=f"no index at {db} — ingest something first",
        why=why,
        fixes=["goldmine init", "goldmine ingest <roots>"],
        doctor=True,
        exit_code=EXIT_PREREQ,
    )


def _gate_no_index(db: Path, *, json_mode: bool,
                   data_home: str | None = None) -> int | None:
    """The shared no-index gate (D4): EXIT_PREREQ + the no_index shape when
    the index file is missing, None when it exists. (`open` raises the same
    error for its handler to catch; `backup` keeps its custom
    nothing-to-back-up shape on purpose.)"""
    if db.exists():
        return None
    _emit_error(_err_no_index(db, data_home=data_home), json_mode=json_mode)
    return EXIT_PREREQ


def _err_fingerprint(rep, *, command: str) -> UserFacingError:
    return UserFacingError(
        kind="fingerprint_mismatch",
        message=f"{command} refused: the index was built with a different embedding fingerprint",
        why=rep.message,
        # `goldmine reindex plan` (05 §4.2) is v2.1; today the mechanical
        # escapes are a full rebuild or keyword-only search
        fixes=["goldmine reset --reindex", "goldmine search <query> --lexical"],
        doctor=True,
        exit_code=EXIT_FINGERPRINT,
    )


def _fingerprint_refusal(store: Store, backend, *, command: str,
                         json_mode: bool) -> int | None:
    """The shared fingerprint gate (D3, SPEC §4 exit 3): compare the stored
    identity against the live backend and render the refusal when they
    diverge. Returns EXIT_FINGERPRINT, or None when the index may be used
    (keyword-only indexes — no stored fingerprint — and backend-less callers
    pass; callers handle their own --lexical suppression)."""
    stored_id = store.meta("fingerprint_id")
    if stored_id is None or backend is None:
        return None
    rep = fp_check(stored_id, store.meta("fingerprint_json") or "", fp_build(backend))
    if not rep.ok:
        _emit_error(_err_fingerprint(rep, command=command), json_mode=json_mode)
        return EXIT_FINGERPRINT
    return None


def _err_backend(exc: BackendUnavailable, *, message: str, fixes: list[str],
                 kind: str = "backend_unavailable",
                 with_hint: bool = False) -> UserFacingError:
    """One builder for every backend-unavailable refusal (D2): shape and exit
    are shared, message/fixes stay per-command. `with_hint` appends the
    backend's own fix_hint to why (only ingest does — the other sites keep
    their historical plain str(exc); 03 §5.7: `goldmine model check` FIRST,
    the arch failure masquerades as plain unavailability)."""
    why = str(exc)
    if with_hint and getattr(exc, "fix_hint", None):
        why += f" ({exc.fix_hint})"
    return UserFacingError(
        kind=kind,
        message=message,
        why=why,
        fixes=fixes,
        doctor=True,
        exit_code=EXIT_PREREQ,
    )


def _err_index_corrupt(db: Path, exc: Exception, cfg: Config) -> UserFacingError:
    """F5: a corrupt / non-SQLite index file gets the universal shape with the
    REAL mechanical remedies (verified against cli): the newest verified
    backup copied back over the file when one exists, else `goldmine reset
    --yes` (deletes the unreadable file; `reset --reindex` canNOT work — it
    opens the same unreadable file) and a plain ingest to rebuild."""
    from . import migrations

    fixes: list[str] = []
    try:
        backups = migrations.backups_of(db, resolve_home(cfg))
        if backups:
            fixes.append(f"cp '{backups[-1]}' '{db}'   # restore the newest backup")
    except Exception:  # home resolution must never mask the corrupt-db error
        pass
    fixes += ["goldmine reset --yes", "goldmine ingest <roots>"]
    return UserFacingError(
        kind="index_corrupt",
        message=f"index file unreadable: {db}",
        why=f"{exc}",
        fixes=fixes,
        doctor=True,
        exit_code=EXIT_RUNTIME,
    )


_CORRUPTION_MARKERS = (
    "not a database", "malformed", "encrypted", "corrupt", "unsupported file format",
)


def _is_corruption_error(text: str) -> bool:
    """sqlite3.DatabaseError also covers OperationalError ('database is
    locked', 'no such table') — only genuine file corruption gets the
    index_corrupt shape; everything else keeps its own path."""
    lowered = str(text).lower()
    return any(marker in lowered for marker in _CORRUPTION_MARKERS)


def _parse_kinds(arg: str | None) -> set[str] | None:
    """--kinds (search and distill take the same spelling, D6): comma list
    -> a set of kind tokens; None when unset/empty."""
    return {k.strip() for k in arg.split(",") if k.strip()} if arg else None


def _index_roots(store: Store) -> list[str]:
    """The roots this index was built from (scope invariant: `status` shows
    the indexed roots). From the pipeline's roots_json meta."""
    stored = store.meta("roots_json")
    try:
        roots = json.loads(stored) if stored else []
    except json.JSONDecodeError:
        return []
    return [str(r) for r in roots] if isinstance(roots, list) else []


def _staleness_hint(staleness: dict, store: Store | None = None) -> str:
    """The re-run hint (journey spec C10): 're-run `goldmine ingest <roots>` to
    refresh N stale files'. Roots come from the index's roots_json when the
    store is given (truncated to two); a hint only exists when files are
    actually stale — staleness never refuses anything."""
    n = staleness.get("stale_files", 0)
    if not n:
        return ""
    roots = _index_roots(store) if store is not None else []
    if roots:
        shown = " ".join(roots[:2]) + (f" (+{len(roots) - 2} more)" if len(roots) > 2 else "")
        return f"re-run `goldmine ingest {shown}` to refresh {n} stale files"
    return f"re-run `goldmine ingest <roots>` to refresh {n} stale files"


def _crawl_bounds(args: argparse.Namespace, cfg: Config):
    """CrawlBounds from the URL flags, or None when none was given.
    (P2-10: crawl_max_mb_page used to be defaulted from max_pages — copy-paste.)"""
    if not any(
        _g(args, name) is not None
        for name in ("max_depth", "crawl_max_pages", "crawl_max_mb", "crawl_no_robots")
    ):
        return None
    from .ingest.crawl import CrawlBounds

    return CrawlBounds(
        max_pages=_g(args, "crawl_max_pages") or CrawlBounds.max_pages,
        max_depth=_g(args, "max_depth") if _g(args, "max_depth") is not None else CrawlBounds.max_depth,
        max_mb_page=_g(args, "crawl_max_mb") or CrawlBounds.max_mb_page,
        politeness=cfg.ingest.politeness,
        respect_robots=not bool(_g(args, "crawl_no_robots", False)),
    )


def _emit_ingest_partial(report, json_mode: bool) -> None:
    """The exit-5 honesty block, shared by `ingest` and `reset --reindex`
    (S5): per-file failures get the universal shape on stderr while the
    summary rows above stay the data; --json keeps stdout to the summary (the
    failures already ride inside it)."""
    if json_mode or not report.failures:
        return
    first = report.failures[0]
    root_failures = [
        f for f in report.failures
        if str(f.get("detail", "")).startswith("root not accessible")
    ]
    if len(root_failures) == report.failed:
        message = (f"{report.failed} root(s) not accessible "
                   "(nothing was indexed from them — exit 5)")
    else:
        message = f"{report.failed} file(s) failed to extract " \
                  "(everything else was indexed — exit 5)"
    _emit_error(UserFacingError(
        kind="ingest_partial",
        message=message,
        why=f"{first['path']}: {first['detail']}"
            + (f" (+{report.failed - 1} more)" if report.failed > 1 else ""),
        fixes=["goldmine ingest <roots>"],
        doctor=False,
    ))
