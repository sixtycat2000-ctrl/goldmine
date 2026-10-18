"""CLI: the 13 command bodies, the error funnel, exit codes, logging setup.

Exit codes (SPEC §1.1): 0 ok · 1 runtime error · 2 usage · 3 fingerprint mismatch ·
4 prerequisite missing · 5 partial success · 130 interrupted.
stdout carries data only; progress/diagnostics go to stderr (SPEC §1.2).

F1 split: argparse wiring lives in parser.py, human/JSON rendering in
render.py; their public names are re-imported below so goldmine.cli stays the
import surface (tests pin `from goldmine.cli import main, build_parser,
render_hits, _crawl_bounds, _rewrite_models_alias`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import __version__, fingerprints
from .backends import BackendUnavailable, create_backend, probe_all
from .backends.base import BackendStartupError, EmbeddingBackend, EmbeddingError
from .config import Config, default_config_path, index_path, load, resolve_home
from .errors import UserFacingError, render_error
from .fingerprints import FingerprintMismatch, build as fp_build, check as fp_check
from .ingest import IngestPipeline
# F1 split: the argparse wiring lives in parser.py; these names stay importable
# from goldmine.cli — the pinned test surface (test_cli/test_model import
# build_parser and _rewrite_models_alias from here) and main() needs the class.
from .parser import (  # noqa: F401
    BACKEND_CHOICES,
    _JsonAwareParser,
    _rewrite_models_alias,
    build_parser,
)
# F1 split: the renderers live in render.py; these names stay importable from
# goldmine.cli (the pinned test surface: test_search_v2_output imports
# render_hits here; init.py lazily imports both) and _render_loc feeds fzf.
from .render import _render_loc, render_hits, render_ingest_summary  # noqa: F401
from .search import Searcher, parse_filters
from .store import Store
from .util import human_size

logger = logging.getLogger("goldmine.cli")

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_FINGERPRINT = 3
EXIT_PREREQ = 4
EXIT_PARTIAL = 5
EXIT_INTERRUPTED = 130

STALE_SCORER_LABEL = "lexical (stale-vectors)"

#: extractor versions this build can write (drift notices compare against
#: these). Single source: the v2 registry in fingerprints.py (values are
#: byte-compatible with every existing row, so nothing goes spuriously stale).
CURRENT_EXTRACTORS = set(fingerprints.EXTRACTOR_VERSIONS.values())


# ------------------------------------------------------------------ utilities


def _g(args: argparse.Namespace, name: str, default=None):
    """Global/flag value: argparse SUPPRESS means absent unless given."""
    return getattr(args, name, default)


def setup_logging(args: argparse.Namespace) -> None:
    quiet = bool(_g(args, "quiet", False))
    verbose = _g(args, "verbose", 0) or 0
    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        env = os.environ.get("GOLDMINE_LOG_LEVEL", "").upper()
        level = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
                 "WARNING": logging.WARNING, "ERROR": logging.ERROR}.get(
            env, logging.WARNING
        )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger("goldmine")
    root.setLevel(logging.DEBUG)
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(level)
    stream.setFormatter(formatter)
    root.handlers[:] = [stream]
    log_file = _g(args, "log_file")
    if log_file:
        handler = logging.FileHandler(log_file)
        handler.setLevel(logging.DEBUG)  # full-level logs to the file
        handler.setFormatter(formatter)
        root.addHandler(handler)


def _load_config(args: argparse.Namespace) -> Config:
    config_flag = _g(args, "config")
    return load(config_path=Path(config_flag) if config_flag else None)


def _db_path(args: argparse.Namespace, cfg: Config) -> Path:
    db = _g(args, "db")
    if db:
        return Path(db)
    return index_path(cfg, _g(args, "index", "default") or "default")


def _make_backend(cfg: Config, name: str | None) -> EmbeddingBackend | None:
    return create_backend(cfg, name)


def _emit_json(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _envelope(command: str, **fields) -> dict:
    """The standard machine envelope (D5/F11): every informational --json
    payload opens with the frozen {"schema": "goldmine/1", "command": ...}
    keys — version, backup, backends, open, init share one builder so the
    contract cannot drift per command."""
    return {"schema": "goldmine/1", "command": command, **fields}


# --------------------------------------- universal error shape (v2 item 17)
#
# Every user-facing failure prints the 03 §5.7 4-line shape (WHAT / why: /
# fix: / more: goldmine doctor) via errors.render_error(); in --json mode the
# stable {"error": {kind, message, why, fix, doctor}} object goes to stdout.
# UserFacingError REFUSES to build without a fix — a fix-less error cannot
# ship. Exit codes are unchanged (SPEC §1.1); exit-code choices stay with the
# caller, rendering stays here.


def _emit_error(exc: UserFacingError, json_mode: bool = False) -> None:
    # F13: no logger.error beside the shape — the 4-line shape IS the message;
    # logging it too duplicated every failure on stderr.
    render_error(
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


# ------------------------------------------------------------------- ingest


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


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    dry_run = bool(_g(args, "dry_run", False))
    json_mode = bool(_g(args, "json", False))

    store = None
    if not dry_run:
        store = Store(_db_path(args, cfg))

    backend: EmbeddingBackend | None = None
    lexical_only = bool(_g(args, "lexical_only", False))
    backend_name = _g(args, "backend")
    if not lexical_only:
        try:
            backend = _make_backend(cfg, backend_name)
        except BackendUnavailable as exc:
            # v2 item 17: universal shape; --lexical-only stays the second,
            # always-available remedy (F13: no logger.error beside the shape)
            _emit_error(_err_backend(
                exc,
                message="ingest refused: no embedding backend available",
                fixes=["goldmine model check", "goldmine ingest --lexical-only"],
                with_hint=True,
            ), json_mode=json_mode)
            return EXIT_PREREQ

    # flag > env > file for the cap knobs the pipeline reads off cfg
    if _g(args, "max_file_mb") is not None:
        cfg.ingest.max_file_mb = int(_g(args, "max_file_mb"))
    if _g(args, "max_pages") is not None:
        cfg.ingest.max_pages = _g(args, "max_pages")
    if _g(args, "max_chunks") is not None:
        cfg.ingest.max_chunks = _g(args, "max_chunks")
    if _g(args, "audio_max_minutes") is not None:
        cfg.ingest.audio_max_minutes = _g(args, "audio_max_minutes")
    if _g(args, "politeness") is not None:
        cfg.ingest.politeness = _g(args, "politeness")
    if _g(args, "workers") is not None:
        cfg.ingest.workers = _g(args, "workers")
    if _g(args, "batch") is not None:
        cfg.llama.batch = _g(args, "batch")

    # v2 item 19 (F3): TTY-only progress. Non-TTY runs keep the exact v1
    # behavior (end summary only) — headless/SSH is honest without a ticker.
    ticker = None
    if (
        not dry_run
        and not json_mode
        and not bool(_g(args, "quiet", False))
        and sys.stderr.isatty()
    ):
        from .output import IngestTicker, asr_estimate_enabled, render_root_media, scan_root_media

        file_roots = [r for r in args.roots
                      if not str(r).startswith(("http://", "https://"))]
        medias = scan_root_media(
            file_roots,
            code=bool(_g(args, "code", False)),
            exclude=list(_g(args, "exclude", []) or []),
            max_file_mb=cfg.ingest.max_file_mb,
            audio_cap_s=float(cfg.ingest.audio_max_minutes) * 60.0,
        )
        pre = render_root_media(medias, asr=asr_estimate_enabled(cfg))
        if pre:
            print(pre, file=sys.stderr)
        from .util import ansi_enabled

        ticker = IngestTicker(
            sum(m.files for m in medias),
            store=store,
            ansi=ansi_enabled(bool(_g(args, "no_color", False))),
        )

    def progress(message: str) -> None:
        logger.info("%s", message)
        if ticker is not None:
            ticker.update(message)

    pipeline = IngestPipeline(cfg, store, backend, lexical_only=lexical_only)
    crawl_bounds = _crawl_bounds(args, cfg)
    try:
        report = pipeline.run(
            list(args.roots),
            code=bool(_g(args, "code", False)),
            exclude=list(_g(args, "exclude", []) or []),
            dry_run=dry_run,
            reindex=bool(_g(args, "reindex", False)),
            refresh_thin=bool(_g(args, "refresh_thin", False)),
            crawl_bounds=crawl_bounds,
            encoding_override=_g(args, "encoding"),
            include_datasets=bool(_g(args, "include_datasets", False)),
            progress=progress,
        )
    finally:
        if ticker is not None:
            ticker.finish()
        if store is not None:
            store.close()

    print(render_ingest_summary(report, json_mode=json_mode))
    if report.cancelled:
        return EXIT_INTERRUPTED
    if report.failed >= 1:
        # v2 item 17: per-file failures (corrupt docx, crawl failures, …) get
        # the universal shape on stderr; the summary rows above stay the data
        _emit_ingest_partial(report, json_mode)
        return EXIT_PARTIAL
    return EXIT_OK


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
def _parse_kinds(arg: str | None) -> set[str] | None:
    """--kinds (search and distill take the same spelling, D6): comma list
    -> a set of kind tokens; None when unset/empty."""
    return {k.strip() for k in arg.split(",") if k.strip()} if arg else None


# ------------------------------------------------------------------- search


def cmd_search(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    db = _db_path(args, cfg)
    json_mode = bool(_g(args, "json", False))
    refused = _gate_no_index(db, json_mode=json_mode)
    if refused is not None:
        return refused
    store = Store(db)
    try:
        query = " ".join(args.query)
        scorer = _g(args, "scorer") or cfg.search.scorer
        # P3-13: `--limit 0` is a valid answer (zero hits), never the default.
        limit_arg = _g(args, "limit")
        limit = cfg.search.limit if limit_arg is None else limit_arg
        z_gate = _g(args, "z_gate")
        if z_gate is None:
            z_gate = cfg.search.z_gate
        lexical_flag = bool(_g(args, "lexical", False))
        kinds = _parse_kinds(_g(args, "kinds"))

        backend: EmbeddingBackend | None = None
        semantic_requested = scorer in ("hybrid", "semantic", "rrf") and not lexical_flag
        if semantic_requested:
            try:
                backend = _make_backend(cfg, _g(args, "backend"))
            except BackendUnavailable as exc:
                logger.error("search: %s", exc)
                _emit_error(_err_backend(
                    exc,
                    message="search: semantic scoring needs a backend",
                    fixes=[
                        "goldmine search <query> --scorer lexical",
                        "goldmine model check",
                    ],
                ), json_mode=json_mode)
                return EXIT_PREREQ
        else:
            # lexical ablation: FTS needs no embeddings; build a backend only to
            # run the fingerprint gate, and degrade honestly when unavailable
            try:
                backend = _make_backend(cfg, _g(args, "backend"))
            except BackendUnavailable:
                backend = None
        # fingerprint gate (exit 3) — suppressed by --lexical (SPEC §3)
        if not lexical_flag:
            refused = _fingerprint_refusal(store, backend, command="search",
                                           json_mode=json_mode)
            if refused is not None:
                return refused

        stale = lexical_flag and store.meta("fingerprint_id") is not None
        # D6 filters parse out of the query string here (§5.7): kind:/under:/
        # after:/before:/site: tokens + quoted phrases; unknown prefixes stay
        # literal text. Zero-candidate filters -> the standard zero-hit output.
        clean_query, filters = parse_filters(query)
        # P3-13: `--excerpt-chars 0` disables excerpts (argparse default 240
        # applies only when the flag is absent — never override an explicit 0).
        # It caps the «» snippet width too; 0 disables the decoration.
        excerpt_arg = _g(args, "excerpt_chars", 240)
        excerpt_chars = 240 if excerpt_arg is None else excerpt_arg
        try:
            hits = Searcher(store, None if lexical_flag else backend).search(
                clean_query,
                limit=limit,
                scorer="lexical" if lexical_flag else scorer,
                z_gate=z_gate,
                kinds=kinds,
                filters=filters,
                snippet_width=excerpt_chars or None,
                why=bool(_g(args, "why", False)),
            )
        finally:
            # a spawned llama-server MUST NOT outlive the command (V2: kill
            # only our children — but always kill them)
            if backend is not None:
                backend.close()
        if stale:
            for hit in hits:
                hit.scorer = STALE_SCORER_LABEL
        # v2 item 19 (F3): the fzf interactive layer — one TSV line per hit;
        # the serializer lives in output.py (this region stays thin)
        if _g(args, "format") == "fzf":
            from .output import fzf_lines

            rows = fzf_lines(hits, loc_fn=_render_loc)
            if rows:
                print("\n".join(rows))
            return EXIT_OK
        rendered = render_hits(hits, json_mode=json_mode,
                               excerpt_chars=excerpt_chars,
                               why=bool(_g(args, "why", False)),
                               raw=bool(_g(args, "raw", False)),
                               filters=filters)
        if rendered:  # zero hits render as "" — don't print a blank line
            print(rendered)
        if not hits and not json_mode:
            # T1: the honest zero carries its next step (help search-syntax
            # promises it); the hint is diagnostics, so it goes to stderr —
            # stdout stays data-only (SPEC §1.2)
            print(f"nothing found for {query!r}")
            print("try: fewer/shorter words · drop kind:/under: filters · "
                  "goldmine help search-syntax", file=sys.stderr)
        return EXIT_OK
    finally:
        store.close()
# ------------------------------------------------------------------- status


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


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    db = _db_path(args, cfg)
    refused = _gate_no_index(db, json_mode=bool(_g(args, "json", False)),
                             data_home=str(resolve_home(cfg)))
    if refused is not None:
        # status keeps its data-home line (03 §5.7 row 1) — as the `why:`
        return refused
    store = Store(db)
    try:
        counts = store.counts()
        fp_id = store.meta("fingerprint_id")
        fp_json = store.meta("fingerprint_json")
        fp = json.loads(fp_json) if fp_json else None
        # P3-13 convention (F7): an explicit 0 is a valid answer (zero rows),
        # never the default — `or 10` silently turned `--failures 0` into 10
        failures_arg = _g(args, "failures")
        failures = store.failures(10 if failures_arg is None else failures_arg)
        pending = store.pending()
        backends = probe_all(cfg)
        current_extractors = fingerprints.current_extractors()
        drift = store.db.execute(
            "SELECT COUNT(*) FROM files WHERE capability = 'content_indexed' "
            "AND (extractor_version IS NULL OR extractor_version NOT IN "
            f"({','.join('?' for _ in current_extractors)}))",
            tuple(sorted(current_extractors)),
        ).fetchone()[0]

        notices: list[dict] = []
        llama_info = backends.get("llama") or {}
        stored_build = store.meta("llama_build")
        if (
            stored_build
            and llama_info.get("build")
            and stored_build != llama_info["build"]
        ):
            notices.append({
                "code": "build_changed",
                "message": (
                    f"llama.cpp build changed since last ingest (recorded: "
                    f"{stored_build}, installed: {llama_info['build']}) — "
                    "reindex recommended"
                ),
            })
        if drift:
            notices.append({
                "code": "extractor_drift",
                "message": f"{drift} content-indexed files were extracted by a "
                           "different extractor version — `goldmine ingest --reindex` refreshes",
            })
        # v2 staleness (05 §4.2, journey spec C10): per-file identity drift as a
        # visible fact, never a refusal. The live embed id is derived from the
        # index's recorded fingerprint (the gate keeps stored == live at ingest
        # time); per-file drift against it is what re-running ingest fixes.
        live_embed = None
        if fp_json:
            try:
                live_embed = fingerprints.embed_id_from_fingerprint(fp)
            except (ValueError, TypeError):
                live_embed = None
        staleness = fingerprints.staleness_report(
            store, live_embed, extractor_versions=current_extractors
        )
        stale_hint = _staleness_hint(staleness, store)
        roots = _index_roots(store)
        json_mode = bool(_g(args, "json", False))
        if json_mode:
            _emit_json({
                "index": str(db),
                "index_bytes": db.stat().st_size,
                "schema_version": store.meta("schema_version"),
                "roots": roots,
                "fingerprint": {
                    "id": fp_id,
                    "backend": (fp or {}).get("backend"),
                    "model_id": (fp or {}).get("model_id"),
                    "dim": (fp or {}).get("dim"),
                },
                "backends": backends,
                "counts": {
                    "files": counts.files,
                    "segments": counts.segments,
                    "vectors": counts.vectors,
                    "bytes": counts.bytes,
                    "content_indexed": counts.totals.get("content_indexed", 0),
                    "metadata_only": counts.totals.get("metadata_only", 0),
                    "unsupported": counts.totals.get("unsupported", 0),
                    "failed": counts.totals.get("failed", 0),
                    "pending": counts.states.get("pending", 0),
                },
                "by_kind": counts.by_kind,
                "staleness": {**staleness, "rerun_hint": stale_hint},
                "failures": [
                    {"path": f.path, "detail": f.detail} for f in failures
                ],
                "pending": [f.path for f in pending],
                "metadata_reasons": [
                    {"reason": r, "count": n} for r, n in store.reasons("metadata_only")
                ],
                "notices": notices,
            })
            return EXIT_OK

        lines = [
            f"index: {db} ({human_size(db.stat().st_size)})",
            f"schema v{store.meta('schema_version') or Store.SCHEMA_VERSION}"
            + (f" · fingerprint {fp_id}" if fp_id else " · no fingerprint (keyword-only)")
            + (
                f" ({(fp or {}).get('backend')}/{(fp or {}).get('model_id')}"
                f"/{(fp or {}).get('dim')})"
                if fp
                else ""
            ),
        ]
        # scope invariant (v2 item 20): the indexed roots are visible state
        if roots:
            shown = ", ".join(roots[:3]) + (f" (+{len(roots) - 3} more)" if len(roots) > 3 else "")
            lines.append(f"roots: {shown}")
        for backend_id, info in backends.items():
            if info.get("available"):
                lines.append(
                    f"backend {backend_id}: available"
                    + (f" (dim {info.get('dim')})" if info.get("dim") else "")
                )
            else:
                lines.append(
                    f"backend {backend_id}: unavailable — {info.get('detail', 'unknown')}"
                )
        lines.append(
            f"files: {counts.files} · segments: {counts.segments} · "
            f"vectors: {counts.vectors} · bytes: {human_size(counts.bytes)}"
        )
        stale_line = (
            f"stale: {staleness['stale_files']} of {staleness['files']} files "
            f"(embed {staleness['embed_stale']} · chunker {staleness['chunker_stale']} "
            f"· extractor {staleness['extractor_stale']})"
        )
        oldest = staleness.get("oldest_stale_indexed_at")
        if oldest:
            # F6: how long has this been stale (oldest stale row's index date)
            stale_line += (
                f" — since {time.strftime('%Y-%m-%d', time.localtime(oldest))}"
            )
        lines.append(stale_line)
        if stale_hint:
            lines.append(f"  {stale_hint}")
        lines.append("capability grid (kind × capability):")
        for kind in sorted(counts.by_kind):
            grid = counts.by_kind[kind]
            cells = " · ".join(f"{cap} {n}" for cap, n in sorted(grid.items()))
            lines.append(f"  {kind:<8} {cells}")
        if pending:
            lines.append(f"pending: {len(pending)}")
            for rec in pending[:5]:
                lines.append(f"  {rec.path}")
        if failures:
            lines.append(f"failed: {len(failures)} shown")
            for rec in failures:
                lines.append(f"  {rec.path}: {rec.detail}")
        for reason, n in store.reasons("metadata_only"):
            lines.append(f"metadata-only: {reason} × {n}")
        for notice in notices:
            lines.append(f"notice: [{notice['code']}] {notice['message']}")
        print("\n".join(lines))
        return EXIT_OK
    finally:
        store.close()


# ------------------------------------------------- v2 item 4: version --full


def _git_describe() -> str | None:
    """`git describe --tags --always --dirty` at the repo root; None outside a
    checkout (installed copies) or when git is unusable (e.g. no commits)."""
    import subprocess

    from .backends.llama import REPO_ROOT

    if not (REPO_ROOT / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "describe", "--tags", "--always", "--dirty"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def _sidecar_sha256(model: Path) -> tuple[str | None, str | None]:
    """(sha256, source) read from the `<model>.sha256` sidecar — NO recompute
    (version --full must stay cheap; `model verify` does the real hashing)."""
    sidecar = model.with_name(model.name + ".sha256")
    try:
        cached = json.loads(sidecar.read_text())
        return str(cached["sha256"]), "sidecar"
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None, None


def _version_full(cfg: Config) -> dict:
    from . import migrations
    from .backends.llama import (
        home_server_path,
        probe_version,
        resolve_model_path,
        resolve_server_path,
    )

    server, source = resolve_server_path(cfg.llama)
    model = resolve_model_path(cfg.llama)
    sha, sha_source = _sidecar_sha256(model) if model.is_file() else (None, None)
    backends = probe_all(cfg)
    return {
        "git": _git_describe(),
        "llama_server": {
            "path": str(server) if server else None,
            "source": source,
            "build": probe_version(server) if server else None,
        },
        "model": {
            "path": str(model),
            "id": model.stem,
            "sha256": sha,
            "sha256_source": sha_source,
            "present": model.is_file(),
        },
        "schema_version_supported": migrations.TARGET,
        "backends": {
            backend_id: bool(info.get("available"))
            for backend_id, info in sorted(backends.items())
        },
        "home": str(resolve_home(cfg)),
        "home_server_path": str(home_server_path(cfg)),
    }


def cmd_version(args: argparse.Namespace) -> int:
    import platform

    cfg = _load_config(args)
    info: dict = {
        "goldmine": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    if _g(args, "full", False):
        info.update(_version_full(cfg))
    if _g(args, "json", False):
        # F11: the standard envelope (init/open style)
        _emit_json(_envelope("version", **info))
        return EXIT_OK
    lines = [f"goldmine {info['goldmine']} (python {info['python']}, {info['platform']})"]
    if _g(args, "full", False):
        lines.append(f"git: {info.get('git') or 'not a git checkout'}")
        server = info["llama_server"]
        lines.append(
            f"llama-server: {server['path'] or 'not found'} "
            f"(source: {server['source']}, build: {server['build'] or '?'})"
        )
        model = info["model"]
        if model["sha256"]:
            lines.append(
                f"model: {model['id']} ({model['path']}) "
                f"sha256 {model['sha256'][:16]}… ({model['sha256_source']})"
            )
        else:
            lines.append(
                f"model: {model['id']} ({model['path']}) — no sidecar sha"
                if model["present"] else
                f"model: {model['id']} — file missing at {model['path']}"
            )
        lines.append(f"schema: supports up to v{info['schema_version_supported']}")
        backends = ", ".join(
            f"{bid} {'available' if ok else 'unavailable'}"
            for bid, ok in info["backends"].items()
        )
        lines.append(f"backends: {backends}")
        lines.append(f"home: {info['home']}")
    print("\n".join(lines))
    return EXIT_OK


# --------------------------------------------------- v2 item 5: model manager
def cmd_model(args: argparse.Namespace) -> int:
    from . import model as model_mod

    cfg = _load_config(args)
    verb = _g(args, "model_verb")  # never None: argparse requires a verb (S3)
    json_mode = bool(_g(args, "json", False))
    manager = model_mod.ModelManager(cfg)
    try:
        if verb == "list":
            rows = manager.listing()
            print(model_mod.render_listing(rows, json_mode))
            return EXIT_OK
        if verb == "install":
            from_path = _g(args, "from_path")
            path = manager.install(
                _g(args, "name"),
                from_path=Path(from_path) if from_path else None,
                alias=_g(args, "alias"),
            )
            if json_mode:
                _emit_json({"installed": _g(args, "name"), "path": str(path)})
            else:
                print(str(path))
            return EXIT_OK
        if verb == "verify":
            results = manager.verify(_g(args, "name"))
            print(model_mod.render_verify(results, json_mode))
            failed = [r for r in results if r.get("bytes_ok") is False or r.get("sha_ok") is False]
            return EXIT_RUNTIME if failed else EXIT_OK
        if verb == "use":
            path = manager.use(_g(args, "name"))
            if json_mode:
                _emit_json({"use": _g(args, "name"), "path": str(path)})
            else:
                print(str(path))
            return EXIT_OK
        if verb == "adopt":
            path = manager.adopt(Path(_g(args, "path")).expanduser(), _g(args, "name"))
            if json_mode:
                _emit_json({"adopted": _g(args, "name"), "path": str(path)})
            else:
                print(str(path))
            return EXIT_OK
        if verb == "check":
            findings = manager.check()
            print(model_mod.render_check(findings, json_mode))
            return EXIT_OK if findings.get("ok") else EXIT_PREREQ
        # unreachable: argparse (required + choices) only passes the six verbs
        # above (S3/W2 — the old dead `unknown verb` branch is gone)
        raise model_mod.ModelError(f"unknown verb {verb!r}", usage=True)
    except model_mod.ModelError as exc:
        # v2 item 17: universal shape ("model missing" retrofit row)
        usage = bool(getattr(exc, "usage", False))
        _emit_error(UserFacingError(
            kind="model_error",
            message=f"model {verb}: {exc}",
            why=str(exc),
            fixes=[exc.fix_hint] if exc.fix_hint else ["goldmine model list"],
            doctor=not usage,  # caller errors don't need a doctor row
            exit_code=EXIT_USAGE if usage else EXIT_RUNTIME,
        ), json_mode=json_mode)
        return EXIT_USAGE if usage else EXIT_RUNTIME


# ----------------------------------------------- agent-B commands (passes 5-6)


def cmd_distill(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    db = _db_path(args, cfg)
    json_mode = bool(_g(args, "json", False))
    refused = _gate_no_index(db, json_mode=json_mode)
    if refused is not None:
        return refused
    store = Store(db)
    try:
        from . import distill
        from .distill import DistillRefused, build_cards

        # Fingerprint gate (SPEC §4: exit 3), same identity check as search:
        # cards cluster stored vectors, so distilling across embedding spaces
        # would mix models. A keyword-only index (no fingerprint) has nothing
        # to compare and passes.
        stored_id = store.meta("fingerprint_id")
        if stored_id is not None:
            try:
                gate_backend = _make_backend(cfg, _g(args, "backend"))
            except BackendUnavailable as exc:
                _emit_error(_err_backend(
                    exc,
                    message="distill refused: cannot verify the index "
                            "fingerprint without a backend",
                    fixes=["goldmine model check"],
                ), json_mode=json_mode)
                return EXIT_PREREQ
            try:
                refused = _fingerprint_refusal(store, gate_backend,
                                               command="distill",
                                               json_mode=json_mode)
            finally:
                gate_backend.close()
            if refused is not None:
                return refused

        vectors = store.counts().vectors
        if vectors < 2:
            _emit_error(UserFacingError(
                kind="insufficient_content",
                message=f"distill needs at least 2 vectorized chunks (index has {vectors})",
                why=f"{vectors} vectorized chunks in {db}",
                fixes=["goldmine ingest <roots>"],
                doctor=False,
                exit_code=EXIT_RUNTIME,
            ), json_mode=json_mode)
            return EXIT_RUNTIME

        threshold = _g(args, "threshold")
        if threshold is None:
            threshold = cfg.distill.threshold
        # P3-13 convention (F7): an explicit 0 is a real answer for every knob
        # (`--min-members 0` keeps singleton clusters, `--max-chunks 0` refuses
        # any corpus, `--limit-cards 0` shows none) — `or` fell back to the
        # default and silently broke all three
        min_members = _g(args, "min_members")
        if min_members is None:
            min_members = cfg.distill.min_members
        max_chunks = _g(args, "max_chunks")
        if max_chunks is None:
            max_chunks = cfg.distill.max_chunks
        limit_cards = _g(args, "limit_cards")
        if limit_cards is None:
            limit_cards = 20
        kinds = _parse_kinds(_g(args, "kinds"))
        try:
            cards, stats = build_cards(
                store, threshold=float(threshold), min_members=int(min_members),
                max_chunks=int(max_chunks), kinds=kinds,
            )
        except DistillRefused as exc:
            _emit_error(UserFacingError(
                kind="distill_refused",
                message=f"distill refused: {exc}",
                why=str(exc),
                fixes=["goldmine distill --max-chunks <N>"],
                doctor=False,
                exit_code=EXIT_RUNTIME,
            ), json_mode=json_mode)
            return EXIT_RUNTIME
        store.save_cards(cards)

        out_arg = _g(args, "out")
        if json_mode:
            print(json.dumps(
                distill.render_json(cards[:limit_cards], stats),
                indent=2, ensure_ascii=False,
            ))
            return EXIT_OK

        header = distill.header_line(stats)
        markdown = distill.render_markdown(cards[:limit_cards], header)
        if out_arg:
            out = Path(out_arg).expanduser()
            # dir mode (write cards.md + cards.json): existing dir, trailing
            # slash, or an extension-less path (SPEC's `--out FILE` markdown
            # mode stays for real file paths)
            dir_mode = out.is_dir() or str(out_arg).endswith("/") or not out.suffix
            if dir_mode:
                out.mkdir(parents=True, exist_ok=True)
                md_path = out / "cards.md"
                json_path = out / "cards.json"
                md_path.write_text(markdown, encoding="utf-8")
                json_path.write_text(
                    json.dumps(distill.render_json(cards, stats), indent=2,
                               ensure_ascii=False),
                    encoding="utf-8",
                )
                print(
                    f"wrote {md_path} and {json_path} "
                    f"({stats['clusters_kept']} cards); full output above",
                    file=sys.stderr,
                )
                print(markdown)  # SPEC: default output is markdown on stdout
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(markdown, encoding="utf-8")
                logger.info("distill: wrote %s", out)
        else:
            print(markdown)
        return EXIT_OK
    finally:
        store.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    cfg = _load_config(args)
    db = _db_path(args, cfg)
    # W1: the doctor parser defines only --deep; the old dead `--probe` read
    # is gone (the live probe flag belongs to `backends --probe`)
    live = bool(os.environ.get("GOLDMINE_LIVE"))
    deep = bool(_g(args, "deep", False))
    results = doctor.run_checks(cfg, db, probe=live, deep=deep)
    json_mode = bool(_g(args, "json", False))
    summary = doctor.summary(results)  # W10: computed once, not three times
    if json_mode:
        _emit_json({
            "checks": [
                {
                    "name": r.name, "status": r.status,
                    "detail": r.detail, "fix_hint": r.fix_hint,
                }
                for r in results
            ],
            "summary": summary,
        })
    else:
        for r in results:
            marker = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[r.status]
            print(f"{marker} {r.name}: {r.detail}")
            if r.fix_hint:
                print(f"       fix: {r.fix_hint}")
        print(
            f"doctor: {summary['counts']['ok']} ok, {summary['counts']['warn']} warn, "
            f"{summary['counts']['fail']} fail"
        )
    return EXIT_PREREQ if not summary["ok"] else EXIT_OK


# ------------------------------------------------------------------- backup


def cmd_backup(args: argparse.Namespace) -> int:
    """goldmine backup (05 §4.8): VACUUM INTO a verified single-file copy.
    Restore = stop anything using the index, copy the file back."""
    from . import migrations

    cfg = _load_config(args)
    db = _db_path(args, cfg)
    if not db.exists():
        _emit_error(
            UserFacingError(
                kind="no_index",
                message=f"no index at {db} — nothing to back up",
                why=f"no index file at {db}",
                fixes=["goldmine ingest <roots>"],
                doctor=False,
                exit_code=EXIT_PREREQ,
            ),
            json_mode=bool(_g(args, "json", False)),
        )
        return EXIT_PREREQ
    out_arg = _g(args, "out")
    keep = _g(args, "keep", 5)
    keep = 5 if keep is None else keep
    try:
        info = migrations.create_backup(
            db,
            out=Path(out_arg).expanduser() if out_arg else None,
            keep=keep,
            home=resolve_home(cfg),
        )
    except (RuntimeError, OSError) as exc:
        # (F13: the shape below is the message — no duplicate logger line)
        _emit_error(UserFacingError(
            kind="backup_failed",
            message="backup failed",
            why=str(exc),
            fixes=["goldmine backup --out <path>", "goldmine doctor --deep"],
            doctor=True,
            exit_code=EXIT_RUNTIME,
        ), json_mode=bool(_g(args, "json", False)))
        return EXIT_RUNTIME
    if bool(_g(args, "json", False)):
        # F11: the standard envelope (init/open style)
        _emit_json(_envelope("backup", **info))
    else:
        print(
            f"backup: {info['path']} ({human_size(info['bytes'])}) — "
            f"integrity {info['integrity']}"
        )
        pruned = info.get("pruned") or []
        if pruned:
            print(f"pruned {len(pruned)} older backup(s) beyond keep {keep}")
    return EXIT_OK


def _probe_backend_live(cfg: Config, backend_id: str) -> dict:
    """--probe: a live embed round trip per backend. Spawns llama-server ONLY
    here (explicit user intent); failures mark the backend unavailable."""
    try:
        backend = create_backend(cfg, backend_id)
    except BackendUnavailable as exc:
        return {"available": False, "detail": str(exc)}
    except ValueError as exc:
        return {"available": False, "detail": str(exc)}
    try:
        backend.startup()
        vectors = backend.embed_texts(["goldmine backends --probe"])
        info = {
            "available": True,
            "dim": int(vectors.shape[1]),
            "probe": "embedded 1 text ok",
        }
        return info
    except Exception as exc:  # startup/embed failure = honest unavailability
        return {
            "available": False,
            "detail": f"probe failed: {type(exc).__name__}: {exc}",
            "error_tail": getattr(exc, "tail", "")[:2000],
        }
    finally:
        backend.close()


def cmd_backends(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    probe = bool(_g(args, "probe", False))
    infos = probe_all(cfg)
    if probe:
        for backend_id in sorted(infos):
            infos[backend_id].update(_probe_backend_live(cfg, backend_id))

    json_mode = bool(_g(args, "json", False))
    if json_mode:
        # F11: the standard envelope (init/open style)
        _emit_json(_envelope("backends",
                             backends=[infos[k] for k in sorted(infos)]))
        return EXIT_OK
    for backend_id in sorted(infos):
        info = infos[backend_id]
        cap = info.get("capability") or {}
        cap_str = (
            f"text {'yes' if cap.get('text') else 'no'} · "
            f"image {'yes' if cap.get('image') else 'no'} · "
            f"audio {'yes' if cap.get('audio') else 'no'}"
        )
        line = (
            f"{backend_id:<10} {'available' if info.get('available') else 'unavailable'}"
            + (f"  dim {info['dim']}" if info.get("dim") else "")
            + f"  {cap_str}"
        )
        if info.get("server"):
            line += f"  [{info['server']}]"
        if info.get("socket"):
            line += f"  [{info['socket']}]"
        if info.get("probe"):
            line += f"  probe: {info['probe']}"
        print(line)
        if not info.get("available") and info.get("detail"):
            print(f"           {info['detail']}")
        if not info.get("available") and info.get("fix_hint"):
            print(f"           fix: {info['fix_hint']}")
    return EXIT_OK


def _index_files(db: Path) -> list[Path]:
    return [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]


def cmd_reset(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    db = _db_path(args, cfg)
    yes = bool(_g(args, "yes", False))
    reindex = bool(_g(args, "reindex", False))
    json_mode = bool(_g(args, "json", False))

    if reindex:
        if not db.exists():
            # S1 (v2 item 17 sweep): the missing-index refusal gets the
            # universal shape — exit 4 like every other no-index path, with
            # the error object on stdout in --json mode
            _emit_error(_err_no_index(db), json_mode=json_mode)
            return EXIT_PREREQ
        store = Store(db)
        try:
            roots = _index_roots(store)
            if not roots:
                # S2: same shape — the remedies are the point of the message
                _emit_error(UserFacingError(
                    kind="reset_no_roots",
                    message="reset --reindex needs stored roots "
                            "(no `roots_json` in meta)",
                    why=f"the index at {db} has no recorded ingest roots",
                    fixes=["goldmine ingest <roots>", "goldmine reset --yes"],
                    doctor=False,
                    exit_code=EXIT_USAGE,
                ), json_mode=json_mode)
                return EXIT_USAGE
            with store.db:
                store.db.execute("DELETE FROM fts")
                store.db.execute("DELETE FROM card_chunks")
                store.db.execute("DELETE FROM cards")
                store.db.execute("DELETE FROM segments")
            store.set_meta("fingerprint_id", None)
            store.set_meta("fingerprint_json", None)
        finally:
            store.close()
        logger.info("reset: cleared vectors + cards; re-ingesting %d root(s)", len(roots))
        return _run_reindex_ingest(
            cfg, db, roots,
            backend_name=_g(args, "backend"),
            json_mode=json_mode,
        )

    targets = [p for p in _index_files(db) if p.exists()]
    if not targets:
        print(f"nothing to delete (no index at {db})")
        return EXIT_OK
    if not yes:
        size = sum(p.stat().st_size for p in targets)
        print(
            f"would delete {len(targets)} file(s): "
            + ", ".join(str(p) for p in targets)
            + f" ({human_size(size)})"
        )
        if sys.stdin.isatty():
            answer = input(f"delete index at {db}? [y/N] ")
            if answer.strip().lower() in ("y", "yes"):
                yes = True
        else:
            # v2 item 17: the "already exemplary" reset refusal promoted to
            # the universal shape (still exit 2, still nothing deleted); the
            # error object reaches stdout in --json mode too
            _emit_error(UserFacingError(
                kind="reset_needs_confirmation",
                message="reset refused: deleting the index needs confirmation",
                why="non-interactive session (stdin is not a TTY)",
                fixes=[f"goldmine reset --yes --db {db}"],
                doctor=False,
                exit_code=EXIT_USAGE,
            ), json_mode=json_mode)
            return EXIT_USAGE
    if yes:
        for path in targets:
            path.unlink()
            logger.info("reset: deleted %s", path)
        print(f"deleted {len(targets)} file(s) — index at {db} is gone")
    return EXIT_OK


def _run_reindex_ingest(cfg: Config, db: Path, roots: list[str], *,
                        backend_name: str | None, json_mode: bool) -> int:
    """reset --reindex step 2: fresh ingest over the stored roots."""
    from .ingest import IngestPipeline

    backend = None
    try:
        backend = _make_backend(cfg, backend_name)
    except BackendUnavailable as exc:
        _emit_error(_err_backend(
            exc,
            message="reset --reindex: no backend — the index is now vectorless",
            fixes=["goldmine model check", "goldmine ingest --lexical-only"],
        ), json_mode=json_mode)
        return EXIT_PREREQ
    store = Store(db)
    try:
        pipeline = IngestPipeline(cfg, store, backend)
        report = pipeline.run(list(roots), reindex=True)
        print(render_ingest_summary(report, json_mode=json_mode))
        if report.cancelled:
            return EXIT_INTERRUPTED
        if report.failed >= 1:
            # S5: same honesty as `ingest` — exit 5 with the ingest_partial
            # shape, not a bare exit code
            _emit_ingest_partial(report, json_mode)
            return EXIT_PARTIAL
        return EXIT_OK
    finally:
        store.close()
        backend.close()


# ------------------------------------------------- opener (v2 item 18 / F2')


def _open_resolve_hit(args: argparse.Namespace, cfg: Config, n: int, query: str):
    """Re-run the search deterministically and return (path, kind, loc) of hit
    N — avoids shell-quoting paths in widgets/scripts (03 §5.6). Degrades to
    lexical ranking when no backend is available, stated on stderr."""
    from .search import Searcher

    db = _db_path(args, cfg)
    if not db.exists():
        raise _err_no_index(db)
    store = Store(db)
    backend: EmbeddingBackend | None = None
    try:
        scorer = cfg.search.scorer
        try:
            backend = _make_backend(cfg, _g(args, "backend"))
        except BackendUnavailable:
            scorer = "lexical"
            print(
                "goldmine open: no backend — ranked lexically "
                "(hit order may differ from a semantic search)",
                file=sys.stderr,
            )
        # W3: the old dead `--limit` read is gone — `open` defines no such
        # flag; search deep enough for hit N, nothing more
        limit = max(n, cfg.search.limit)
        hits = Searcher(store, backend).search(query, limit=limit, scorer=scorer)
        hit = next((h for h in hits if h.rank == n), None)
        if hit is None:
            raise UserFacingError(
                kind="hit_out_of_range",
                message=f"no hit {n} (search returned {len(hits)} hit(s) for {query!r})",
                why=f"requested rank {n}, got {len(hits)} hits",
                fixes=["goldmine search <query> --limit 50", "goldmine open --path <path>"],
                doctor=False,
                exit_code=EXIT_USAGE,
            )
        return hit.path, hit.kind, hit.loc
    finally:
        if backend is not None:
            backend.close()
        store.close()


def cmd_open(args: argparse.Namespace) -> int:
    """`goldmine open` (03 §5.3): open a hit or file at its moment. Jump
    mechanism -> plain open -> printed instruction; nothing fails the
    command after a target is resolved (exit stays 0)."""
    import shlex

    from .opener import kind_for_path, plan_open, run_plan

    cfg = _load_config(args)
    json_mode = bool(_g(args, "json", False))
    dry_run = bool(_g(args, "dry_run", False))
    hit_n = _g(args, "hit")
    query_parts = _g(args, "query")
    path = _g(args, "path")
    kind = _g(args, "kind")
    loc = _g(args, "loc")

    try:
        if hit_n is not None or query_parts:
            if hit_n is None or not query_parts:
                raise UserFacingError(
                    kind="usage",
                    message="open: --hit N and --query go together",
                    why="--hit re-runs a search; it needs the query text",
                    fixes=["goldmine open --hit 1 --query <terms>"],
                    doctor=False,
                    exit_code=EXIT_USAGE,
                )
            target_path, hit_kind, hit_loc = _open_resolve_hit(
                args, cfg, hit_n, " ".join(query_parts)
            )
            target = path or target_path
            target_kind = kind or hit_kind
            target_loc = loc if loc is not None else hit_loc
        elif path:
            target = str(Path(path).expanduser())
            target_kind = kind
            target_loc = loc
        else:
            raise UserFacingError(
                kind="usage",
                message="open: nothing to open",
                why="give --path, or --hit N with --query",
                fixes=["goldmine open --path <path>", "goldmine open --hit 1 --query <terms>"],
                doctor=False,
                exit_code=EXIT_USAGE,
            )
        if kind is None:
            target_kind = target_kind or kind_for_path(target)
        if not Path(target).exists() and not str(target).startswith(("http://", "https://")):
            raise UserFacingError(
                kind="path_not_found",
                message=f"no such file: {target}",
                why="the file is gone (moved or deleted since it was indexed?)",
                fixes=["goldmine search <terms>"],
                doctor=False,
                exit_code=EXIT_RUNTIME,
            )
    except UserFacingError as exc:
        _emit_error(exc, json_mode=json_mode)
        return exc.exit_code

    plan = plan_open(target, target_kind, target_loc)
    plan = run_plan(plan, dry_run=dry_run)
    if dry_run and plan.argv:
        print(f"would run: {shlex.join(plan.argv)}", file=sys.stderr)
    if json_mode:
        _emit_json(_envelope(
            "open",
            path=plan.path,
            kind=plan.kind,
            loc=target_loc,
            how=plan.how,
            jumped=plan.jumped,
            dry_run=dry_run,
            note=plan.note or None,
        ))
    else:
        print(plan.result_line())
    return EXIT_OK


# ------------------------------------------------- init (v2 item 20 / F4)


def cmd_init(args: argparse.Namespace) -> int:
    """`goldmine init` (03 §5.1): the resumable onboarding state machine;
    the engine lives in init.py, this stays wiring."""
    from .init import run_init

    cfg = _load_config(args)
    config_flag = _g(args, "config")
    env_config = os.environ.get("GOLDMINE_CONFIG")
    if config_flag:
        config_path = Path(config_flag)
    elif env_config:
        config_path = Path(env_config)
    elif os.environ.get("GOLDMINE_HOME"):
        # F12 (split-brain fix): a sandboxed GOLDMINE_HOME keeps ALL of init's
        # persisted files inside the sandbox — the [init] roots record belongs
        # next to init_state.json (both under resolve_home), never in the real
        # ~/.config. Without GOLDMINE_HOME today's XDG default is preserved.
        config_path = resolve_home(cfg) / "config.toml"
    else:
        config_path = default_config_path()
    return run_init(
        cfg,
        db=_db_path(args, cfg),
        config_path=config_path,
        yes=bool(_g(args, "yes", False)),
        media_flag=bool(_g(args, "media", False)),
        model_path=_g(args, "model_path"),
        roots_arg=list(_g(args, "roots", []) or []),
        restart=bool(_g(args, "restart", False)),
        skip_demo=bool(_g(args, "skip_demo", False)),
        backend_name=_g(args, "backend"),
        json_mode=bool(_g(args, "json", False)),
        quiet=bool(_g(args, "quiet", False)),
        no_color=bool(_g(args, "no_color", False)),
    )


# ------------------------------------------------- help (v2 item 21 / F7)


def cmd_help(args: argparse.Namespace) -> int:
    from . import help as help_mod

    topic = _g(args, "topic")
    if topic is None:
        print(help_mod.render_topic_list())
        return EXIT_OK
    text = help_mod.TOPICS.get(topic)
    if text is None:
        # S4 (v2 item 17 sweep): the refusal gets the universal shape — the
        # topic list rides in `why:`, `fix: goldmine help` lists them, and in
        # --json mode the error object reaches stdout (never empty stdout)
        _emit_error(UserFacingError(
            kind="unknown_help_topic",
            message=f"unknown help topic: {topic!r}",
            why="known topics: " + ", ".join(help_mod.TOPICS),
            fixes=["goldmine help"],
            doctor=False,
            exit_code=EXIT_USAGE,
        ), json_mode=bool(_g(args, "json", False)))
        return EXIT_USAGE
    print(text)
    return EXIT_OK


# --------------------------------------------------------------------- main


_DISPATCH = {
    "ingest": cmd_ingest,
    "search": cmd_search,
    "status": cmd_status,
    "version": cmd_version,
    "model": cmd_model,
    "distill": cmd_distill,
    "doctor": cmd_doctor,
    "backup": cmd_backup,
    "backends": cmd_backends,
    "reset": cmd_reset,
    "open": cmd_open,
    "init": cmd_init,
    "help": cmd_help,
}


def _unhandled_error(exc: Exception, json_mode: bool) -> int:
    """The catch-all tail (v2 item 17 + F4c): type+message line, doctor
    pointer and report recipe on stderr; the error object on stdout in --json
    mode. `logger.exception` stays — it is the ONLY stack-trace record."""
    logger.exception("unhandled error: %s", exc)
    print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("more: goldmine doctor", file=sys.stderr)
    print("report: goldmine status --json > goldmine-bug.txt", file=sys.stderr)
    if json_mode:
        print(json.dumps(UserFacingError(
            kind="internal_error",
            message=f"{type(exc).__name__}: {exc}",
            why="unhandled error — the traceback is in the log (-v/--log-file)",
            fixes=[
                "goldmine -v <command>   # re-run verbose for the traceback",
                "goldmine status --json > goldmine-bug.txt",
            ],
            doctor=True,
        ).to_json(), indent=2, ensure_ascii=False))
    return EXIT_RUNTIME


def main(argv: list[str] | None = None) -> int:
    """Entry point; never raises, always returns an int (MODULE_MAP contract)."""
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    argv = _rewrite_models_alias(list(argv))
    _JsonAwareParser._argv = argv  # F4: --json usage errors read this
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage/errors
        return int(exc.code or 0)
    setup_logging(args)
    if args.cmd is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    json_mode = bool(_g(args, "json", False))
    try:
        return _DISPATCH[args.cmd](args)
    except FingerprintMismatch as exc:
        # v2 item 17: the fingerprint refusal in the universal shape. The
        # message carries message+remediation (from fingerprints.gate); the
        # mechanical escapes mirror _err_fingerprint's fixes. (F13: no
        # logger.error beside the shape — the shape IS the message.)
        _emit_error(UserFacingError(
            kind="fingerprint_mismatch",
            message="refused: the index was built with a different embedding fingerprint",
            why=str(exc),
            fixes=["goldmine reset --reindex", "goldmine search <query> --lexical"],
            doctor=True,
            exit_code=EXIT_FINGERPRINT,
        ), json_mode=json_mode)
        return EXIT_FINGERPRINT
    except BackendUnavailable as exc:
        _emit_error(_err_backend(
            exc,
            message="refused: no embedding backend available",
            fixes=["goldmine model check", "goldmine backends"],
            with_hint=True,
        ), json_mode=json_mode)
        return EXIT_PREREQ
    except BackendStartupError as exc:
        # startup failure is exit 2 per ARCHITECTURE §8 ("llama-server fails to
        # start -> command fails (exit 2)"), distinct from a mid-ingest crash
        # (exit 1) and from per-file failures (exit 5 partial). DEVIATION vs
        # SPEC §1.1 (2 = usage error) resolved in ARCHITECTURE's favor by the
        # build scope — kept as-is in v2 (03 §5.7: "the exit-2-vs-1 deviation
        # gets revisited in SPEC", not silently changed here).
        tail = (getattr(exc, "tail", "") or "").strip()
        why = tail.splitlines()[-1] if tail else str(exc)
        _emit_error(UserFacingError(
            kind="backend_startup_failed",
            message="backend failed to start",
            why=why,
            fixes=["goldmine model check", "goldmine backends --probe"],
            doctor=True,
            exit_code=EXIT_USAGE,
        ), json_mode=json_mode)
        return EXIT_USAGE
    except EmbeddingError as exc:
        # include the server's own error line (the 2670-token class, 04 §6.2);
        # v2.0's token guard removes the common cause
        tail = (getattr(exc, "tail", "") or "").strip()
        why = str(exc)
        if tail:
            first_line = tail.splitlines()[-1].strip()
            if first_line and first_line not in why:
                why = f"{why} — server said: {first_line}"
        _emit_error(UserFacingError(
            kind="embedding_failed",
            message="embedding backend failed mid-run",
            why=why,
            fixes=["goldmine ingest --lexical-only", "goldmine model check"],
            doctor=True,
            exit_code=EXIT_RUNTIME,
        ), json_mode=json_mode)
        return EXIT_RUNTIME
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except RuntimeError as exc:
        # index newer than this build (migrations/store refuse on open) —
        # a specific, mechanical remedy exists, so it gets the real shape
        if "newer than this build" in str(exc):
            _emit_error(UserFacingError(
                kind="index_schema_newer",
                message="index schema is newer than this build supports",
                why=str(exc),
                fixes=["pip install --upgrade goldmine"],
                doctor=False,
                exit_code=EXIT_RUNTIME,
            ), json_mode=json_mode)
            return EXIT_RUNTIME
        return _unhandled_error(exc, json_mode)  # main() never raises
    except sqlite3.DatabaseError as exc:
        # F5: a corrupt / non-SQLite --db file is a diagnosed failure with the
        # universal shape (kind index_corrupt) — never a raw traceback. Any
        # OTHER DatabaseError (locked db, logic error) falls into the shared
        # catch-all tail — main() still never raises.
        if not _is_corruption_error(str(exc)):
            return _unhandled_error(exc, json_mode)
        cfg = _load_config(args)
        _emit_error(
            _err_index_corrupt(_db_path(args, cfg), exc, cfg),
            json_mode=json_mode,
        )
        return EXIT_RUNTIME
    except Exception as exc:
        # v2 item 17: the catch-all keeps its type+message line and gains the
        # doctor pointer + a report recipe (03 §5.7 last row). F4c: in --json
        # mode the error object ALWAYS reaches stdout — even this path.
        return _unhandled_error(exc, json_mode)


if __name__ == "__main__":
    raise SystemExit(main())
