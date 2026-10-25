"""Ingest command adapters."""
from __future__ import annotations
import argparse
import sys
from ..backends import BackendUnavailable
from ..backends.base import EmbeddingBackend
from ..render import render_ingest_summary
from ..store import Store
from ..command_support import (EXIT_INTERRUPTED, EXIT_OK, EXIT_PARTIAL, EXIT_PREREQ, _crawl_bounds, _db_path, _emit_error, _emit_ingest_partial, _err_backend, _g, _load_config, _make_backend, runtime)

import logging
logger = logging.getLogger("goldmine.cli")


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
        from ..output import IngestTicker, asr_estimate_enabled, render_root_media, scan_root_media

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
        from ..util import ansi_enabled

        ticker = IngestTicker(
            sum(m.files for m in medias),
            store=store,
            ansi=ansi_enabled(bool(_g(args, "no_color", False))),
        )

    def progress(message: str) -> None:
        logger.info("%s", message)
        if ticker is not None:
            ticker.update(message)

    pipeline = runtime().pipeline_type(cfg, store, backend, lexical_only=lexical_only)
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
