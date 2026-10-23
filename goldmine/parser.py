"""Argparse wiring (v2 F1: extracted from cli.py).

build_parser + the subparser builders, _JsonAwareParser (--json usage errors),
BACKEND_CHOICES and the `models`->`model` argv rewrite. Everything here is
parse-time: no stores, no backends, no exit codes beyond argparse's own.
Re-imported by goldmine.cli — that module stays the import surface
(tests pin `from goldmine.cli import main, build_parser`).
"""

from __future__ import annotations

import argparse
import json

from . import __version__
from .errors import UserFacingError




# --------------------------------------------------------------------- parser


#: The canonical backend ids (v2 review F14): validated by argparse on EVERY
#: command that accepts --backend — an unknown value is a clean usage error
#: (exit 2), never a raw traceback, and never silently ignored. Kept as a
#: static triple on purpose: a build whose llama module fails to import must
#: still PARSE --backend llama (and report unavailability honestly, exit 4),
#: not refuse it as a usage error.
BACKEND_CHOICES = ("llama", "mock")

class _JsonAwareParser(argparse.ArgumentParser):
    """argparse parser that honors --json for its OWN usage errors (F4): the
    standard usage+error still goes to stderr with exit 2, and a --json run
    additionally gets the universal error object on stdout — a script never
    sees an empty stdout on a bad invocation. main() pins the argv being
    parsed on the class (subparsers are separate instances of it)."""

    _argv: list[str] = []

    def error(self, message):  # noqa: D401 - argparse signature
        if "--json" in type(self)._argv:
            print(json.dumps(UserFacingError(
                kind="usage_error",
                message=message,
                why=message,
                fixes=["goldmine --help", "goldmine help <command>"],
                doctor=False,
            ).to_json(), indent=2, ensure_ascii=False))
        super().error(message)

def _global_options() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    for args, kwargs in (
        (("--index",), dict(metavar="NAME", help="named index under <home>")),
        (("--db",), dict(metavar="PATH", help="explicit index file (overrides --index)")),
        (("--backend",), dict(metavar="NAME", choices=list(BACKEND_CHOICES),
                              help=" | ".join(BACKEND_CHOICES))),
        (("--config",), dict(metavar="PATH", help="config.toml location")),
        (("-v", "--verbose"), dict(action="count", help="INFO (-v) / DEBUG (-vv) to stderr")),
        (("-q", "--quiet"), dict(action="store_true", help="errors only")),
        (("--log-file",), dict(metavar="PATH", help="also append full-level logs here")),
        (("--json",), dict(action="store_true", help="machine-readable stdout")),
        (("--no-color",), dict(action="store_true", help="disable ANSI (auto off when piped)")),
    ):
        kwargs["default"] = argparse.SUPPRESS
        p.add_argument(*args, **kwargs)
    return p

def build_parser() -> argparse.ArgumentParser:
    common = _global_options()
    parser = _JsonAwareParser(
        prog="goldmine",
        description="Ingest anything, search anything — local-first macOS CLI.",
        parents=[common],
    )
    parser.add_argument(
        "--version", action="version", version=f"goldmine {__version__}"
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    p_ingest = sub.add_parser(
        "ingest", parents=[common], help="index files and URLs"
    )
    p_ingest.add_argument("roots", nargs="+", metavar="PATH/URL",
                          help="files, folders, or http(s) URLs to index")
    p_ingest.add_argument("--code", action="store_true",
                          help="include code/data files (default off)")
    p_ingest.add_argument("--dry-run", action="store_true",
                          help="classify only; no DB writes, no backend")
    p_ingest.add_argument("--lexical-only", action="store_true",
                          help="ingest without embeddings (FTS-only index)")
    p_ingest.add_argument("--reindex", action="store_true",
                          help="clear segments + fingerprint and re-extract everything")
    p_ingest.add_argument("--refresh-thin", action="store_true",
                          help="re-extract files with thin/scanned PDF pages "
                               "(OCR upgrade) even when unchanged")
    p_ingest.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                          help="repeatable glob (relative to any root, or absolute)")
    p_ingest.add_argument("--include-datasets", action="store_true",
                          help="do not apply dataset-folder detection")
    p_ingest.add_argument("--max-file-mb", type=float, metavar="N", help="default 200")
    p_ingest.add_argument("--max-pages", type=int, metavar="N", help="PDF page cap, default 5000")
    p_ingest.add_argument("--max-chunks", type=int, metavar="N", help="per-file chunk cap, default 5000")
    p_ingest.add_argument("--audio-max-minutes", type=float, metavar="N", help="default 120")
    p_ingest.add_argument("--max-depth", type=int, metavar="N", help="crawler depth, default 2")
    p_ingest.add_argument("--crawl-max-pages", type=int, metavar="N", help="default 50")
    p_ingest.add_argument("--crawl-max-mb", type=float, metavar="N", help="default 5")
    p_ingest.add_argument("--crawl-no-robots", action="store_true",
                          help="ignore robots.txt (default: respect it)")
    p_ingest.add_argument("--politeness", type=float, metavar="SECONDS", help="default 1.0")
    p_ingest.add_argument("--workers", type=int, metavar="N", help="extraction pool, default 2")
    p_ingest.add_argument("--batch", type=int, metavar="N", help="embedding batch, default 16")
    p_ingest.add_argument("--encoding", metavar="CHARSET",
                          help="text decode override for this run (D5b)")

    p_search = sub.add_parser(
        "search", parents=[common], help="hybrid search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "query filters (inline, anywhere, case-insensitive prefixes):\n"
            "  kind:pdf|doc|code|image|audio|video|webpage   repeatable kinds via |\n"
            "  under:PATH      file paths under a prefix (~ expanded)\n"
            "  after:DATE / before:DATE   YYYY-MM-DD or 7d/2w/3m; the file's\n"
            "                  content date when known, else its mtime\n"
            "  site:HOST       web pages from one host\n"
            '  "exact phrase"  quoted = exact phrase match\n'
            "Unknown word: prefixes are NOT filters — they stay literal search\n"
            "terms. A filter that matches nothing yields zero hits, honestly."
        ),
    )
    p_search.add_argument("query", nargs="+", metavar="QUERY",
                          help="search words; inline filters (kind:, under:, "
                               "after:, before:, site:) parse out — see the "
                               "epilog below")
    p_search.add_argument("--limit", type=int, metavar="N",
                          help="max hits (default from config: 20; 0 is a "
                               "valid answer)")
    p_search.add_argument("--scorer", metavar="NAME",
                          choices=["hybrid", "lexical", "semantic", "rrf"],
                          help="hybrid | lexical | semantic | rrf "
                               "(default from config)")
    p_search.add_argument("--kinds", metavar="KINDS",
                          help="comma list: doc,pdf,code,image,audio,video,webpage")
    p_search.add_argument("--z-gate", type=float, metavar="X",
                          help="z-score gate for vector candidates "
                               "(default from config)")
    p_search.add_argument("--lexical", action="store_true",
                          help="force lexical-only AND suppress the fingerprint refusal")
    p_search.add_argument("--excerpt-chars", type=int, metavar="N", default=240,
                          help="excerpt width in chars (default 240; 0 disables "
                               "excerpts)")
    p_search.add_argument("--why", action="store_true",
                          help="per-hit score decomposition (human line; `why` "
                               "object in --json)")
    p_search.add_argument("--raw", action="store_true",
                          help="--json: keep «» query-term markers (field "
                               "excerpt_marked); the excerpt field stays clean")
    # v2 item 19 (F3): the interactive layer (fzf widget, scripts/shell/)
    p_search.add_argument("--format", choices=["plain", "fzf"], metavar="FMT",
                          help="plain (default) | fzf: one line per hit "
                               "path<TAB>kind<TAB>loc<TAB>excerpt<TAB>score")
    # v2.1 R2/R1 (docs/v2.1-pack-eval.md §1.2/§2.1): neighbor expansion and
    # the answer-material pack. One context knob, clamped at 3; the pack
    # defaults to 1 neighbor layer.
    p_search.add_argument("--context", type=int, metavar="N",
                          help="neighbor segments each side (0-3, clamped at "
                               "3; default 0 — 1 in --pack mode)")
    p_search.add_argument("--pack", action="store_true",
                          help="emit an answer-material pack instead of the "
                               "hit list: verbatim records + sources footer, "
                               "budgeted for pasting straight to an LLM")
    p_search.add_argument("--budget", type=int, metavar="N",
                          help="pack token budget (default 4000, clamped to "
                               "512-32768; needs --pack)")
    # v2.1 R8 (D1/§6): bypass the serve daemon for THIS search (cold spawn)
    p_search.add_argument("--no-serve", dest="no_serve", action="store_true",
                          help="skip the serve daemon for this search (cold "
                               "llama-server spawn, exactly the pre-serve path)")
    p_search.add_argument("--mode", choices=["fast", "quality"], default="fast",
                          help="fast by default; quality enables explicit optional experiments")
    p_search.add_argument("--rerank", choices=["off", "local"], default="off",
                          help="optional local reranker; requires --mode quality")
    p_search.add_argument("--multi-query", action="store_true",
                          help="bounded deterministic compound search; requires --mode quality")

    # --- v2.1 R8 (D6): the warm-search daemon — `serve` (start, foreground),
    # `serve status`, `serve stop` ---
    sub.add_parser("mcp", parents=[common],
                   help="read-only stdio MCP; explicitly choose --db PATH or --index NAME")

    p_serve = sub.add_parser(
        "serve", parents=[common],
        help="warm-search daemon (opt-in): resident embedding model, sub-300ms "
             "searches; yields under memory pressure",
        description=(
            "Start the serve daemon: it keeps ONE llama-server resident (spawned "
            "lazily on the first search) so semantic searches skip the ~1s model "
            "load. Under memory pressure the model steps aside and stays aside "
            "until the system recovers; searches never fail because of serve — "
            "worst case they are cold. Single instance (flock); Ctrl-C or "
            "`goldmine serve stop` exits cleanly. Searches use it automatically "
            "when it runs; --no-serve opts a single search out."
        ),
    )
    p_serve.add_argument("--linger", type=float, metavar="S", default=None,
                         help="idle seconds before exit (default from config: "
                              "300; only searches refresh the timer, status/"
                              "stop do not)")
    serve_verbs = p_serve.add_subparsers(dest="serve_verb", metavar="<verb>")
    serve_verb_common = _global_options()
    serve_verbs.add_parser(
        "status", parents=[serve_verb_common],
        help="daemon status (human, or --json envelope)",
    )
    serve_verbs.add_parser(
        "stop", parents=[serve_verb_common],
        help="stop the daemon (shutdown op; verified SIGTERM fallback)",
    )

    # --- v2.1 R9 (D4): the folder auto-follow daemon — `watch` (start,
    # foreground), `watch stop` (serve-block shape) ---
    p_watch = sub.add_parser(
        "watch", parents=[common],
        help="folder auto-follow: dropped files become searchable within "
             "minutes; yields to battery/thermal/memory pressure",
        description=(
            "Watch folders and keep the index fresh: each change is detected "
            "by a cheap scan and ingested by the ordinary pipeline (cold "
            "backend per pass, per-file atomic commits — a crash never "
            "re-does finished work). Scope is explicit ONLY: [init] roots by "
            "default, --roots to override for this session (never written to "
            "config). Holds: pauses under thermal load, batches <= 50 files "
            "on battery, defers under memory pressure. Single instance "
            "(flock); Ctrl-C or `goldmine watch stop` exits cleanly; "
            "`--forever` disables the idle exit for a launchd deployment."
        ),
    )
    p_watch.add_argument("--roots", nargs="+", metavar="PATH",
                         help="folders to watch for THIS session (replaces "
                              "[init] roots; never written to config)")
    p_watch.add_argument("--code", action="store_true",
                         help="include code/data files (same default as ingest)")
    p_watch.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                         help="repeatable glob, as in ingest")
    p_watch.add_argument("--include-datasets", action="store_true",
                         help="do not apply dataset-folder detection")
    p_watch.add_argument("--lexical-only", action="store_true",
                         help="index text without embeddings (no model spawn; "
                              "also immune to the memory-pressure defer)")
    p_watch.add_argument("--interval-fast", type=float, metavar="S", default=None,
                         help="detection floor after a change/at start "
                              "(default from config: 5)")
    p_watch.add_argument("--interval-slow", type=float, metavar="S", default=None,
                         help="quiet-corpus cadence cap (default from config: "
                              "120; decay x2 per quiet scan)")
    p_watch.add_argument("--settle", type=float, metavar="S", default=None,
                         help="debounce before an ingest pass (default from "
                              "config: 2)")
    p_watch.add_argument("--linger", type=float, metavar="S", default=None,
                         help="quiet seconds before exit 0 (default from "
                              "config: 300)")
    p_watch.add_argument("--forever", action="store_true",
                         help="disable the idle exit (launchd deployment)")
    watch_verbs = p_watch.add_subparsers(dest="watch_verb", metavar="<verb>")
    watch_verb_common = _global_options()
    watch_verbs.add_parser(
        "stop", parents=[watch_verb_common],
        help="stop the daemon (verified SIGTERM from the heartbeat receipt)",
    )

    p_status = sub.add_parser("status", parents=[common], help="index health")
    p_status.add_argument("--failures", type=int, metavar="N", default=10,
                          help="how many failure/metadata rows to list "
                               "(0 shows none)")

    # --- v2 item 4 (A10): provenance ---
    p_version = sub.add_parser(
        "version", parents=[common],
        help="version (+ --full: build provenance, models, backends)",
    )
    p_version.add_argument(
        "--full", action="store_true",
        help="everything: git describe, llama-server build/source, model pin "
             "(sidecar sha, no recompute), schema support, backends, home",
    )

    # --- v2 item 5 (B1·B2): model manager (singular `model`, C1); the plural
    # `models` alias is a pure argv rewrite (see _rewrite_models_alias) so it
    # never even shows in --help ---
    _add_model_commands(sub, "model")

    # --- agent B's commands: subparsers exist, handlers are honest stubs ---
    p_distill = sub.add_parser(
        "distill", parents=[common],
        help="distill golden cards (v2: calibrated tau, admission guard)",
        description=(
            "Cluster the index's vectors into topic cards. v2 calibrates the "
            "tau threshold from the corpus itself (Otsu over sampled pairwise "
            "cosines, percentile fallback — real corpora are expected to run "
            "the percentile path); a leader pass with an average-link "
            "admission guard replaces the v1 mega-cluster behavior."
        ),
    )
    p_distill.add_argument("--preview", action="store_true",
                           help="dry run: corpus size, calibrated tau (+ raw + "
                                "source + fallback reason), pairwise quartiles, "
                                "size histogram, largest dropped clusters — "
                                "writes NOTHING")
    p_distill.add_argument("--threshold", type=float, metavar="X",
                           help="manual tau override (tau_source='manual'); "
                                "unset = CALIBRATED from the corpus (v2 "
                                "change: no more fixed 0.70 default)")
    p_distill.add_argument("--min-members", type=int, metavar="N",
                           help="smallest cluster kept (default 3; 0 keeps "
                                "singletons)")
    p_distill.add_argument("--max-chunks", type=int, metavar="N",
                           help="refuse — never truncate — when the corpus "
                                "exceeds N vectorized chunks (default 50000)")
    p_distill.add_argument("--kinds", metavar="KINDS",
                           help="comma list: doc,pdf,code,image,audio,video,webpage")
    p_distill.add_argument("--under", action="append", metavar="PATH",
                           help="restrict the corpus to files under PATH "
                                "(repeatable; file mtime/mtime floor compose)")
    p_distill.add_argument("--after", metavar="DATE",
                           help="only files with mtime >= DATE (YYYY-MM-DD, "
                                "file mtime, NOT content date)")
    p_distill.add_argument("--as", metavar="NAME", dest="as_name",
                           help="save scope + params as a named view, then run it")
    p_distill.add_argument("--rerun", metavar="NAME", dest="rerun",
                           help="run a saved view (scope + params reapplied; "
                                "incremental when eligible)")
    p_distill.add_argument("--refresh", action="store_true",
                           help="forget incremental state for this view and "
                                "force a full recompute (history kept)")
    p_distill.add_argument("--limit-cards", type=int, metavar="N",
                           help="how many cards to show/write (default 20; "
                                "0 shows none — every card still saves)")
    p_distill.add_argument("--out", metavar="FILE",
                           help="write markdown to FILE, or to a DIR / "
                                "extension-less path (writes cards.md + "
                                "cards.json)")

    p_doctor = sub.add_parser("doctor", parents=[common], help="environment checks")
    p_doctor.add_argument(
        "--deep", action="store_true",
        help="also run index integrity/FK checks, staleness summary, cards and "
             "backup health (slower on big indexes)",
    )
    p_backup = sub.add_parser(
        "backup", parents=[common], help="snapshot the index to a single sqlite file",
        description=(
            "Backup the index with VACUUM INTO: a consistent single-file copy "
            "(WAL content included) taken while the index stays usable. The "
            "copy is verified (opens + PRAGMA integrity_check) before it "
            "counts. Default output: <home>/backups/<index>-<timestamp>.sqlite, "
            "older backups pruned beyond --keep. The goldmine home lives under "
            "~/Library, which Time Machine covers by default — these backups "
            "are a second, restorable copy (ASR/OCR transcripts are "
            "irreplaceable derived data), not your only safety net."
        ),
    )
    p_backup.add_argument("--out", metavar="PATH",
                          help="explicit backup file (disables default-dir pruning)")
    p_backup.add_argument("--keep", type=int, metavar="N", default=5,
                          help="keep the newest N backups in <home>/backups (default 5)")
    p_backends = sub.add_parser("backends", parents=[common], help="list backends")
    p_backends.add_argument("--probe", action="store_true",
                            help="live-test each backend (spawns llama-server "
                                 "and embeds one text)")
    p_reindex = sub.add_parser(
        "reindex", parents=[common], help="rebuild stored derived indexes without extraction",
    )
    p_reindex.add_argument("component", choices=["lexical", "vectors"],
                           help="rebuild stored CJK/lookup indexes or derived vector cache; no extraction")
    vector_mode = p_reindex.add_mutually_exclusive_group()
    vector_mode.add_argument("--enable-cache", action="store_true",
                            help="vectors only: explicitly enable the built cache after measurement")
    vector_mode.add_argument("--disable-cache", action="store_true",
                            help="vectors only: disable existing cache without rebuilding")
    p_reset = sub.add_parser("reset", parents=[common], help="delete or rebuild the index")
    p_reset.add_argument("--yes", action="store_true",
                         help="delete without confirmation (required when "
                              "stdin is not a TTY)")
    p_reset.add_argument("--reindex", action="store_true",
                         help="clear segments + cards and re-ingest the stored "
                              "roots instead of deleting")

    # --- v2 item 18 (F2'): the opener sliver (03 §5.3) ---
    p_open = sub.add_parser(
        "open", parents=[common],
        help="open a hit or file at its moment (audio seek, editor line)",
        description=(
            "Open a file at the moment that matters: audio/video at a second "
            "(QuickTime/IINA jump when that's the default player), code at a "
            "line ($EDITOR +LINE), everything else with the default app. "
            "Every degrade prints what happened and ends at plain `open` — "
            "exit stays 0. Use --hit/--query to open a search result without "
            "shell-quoting paths."
        ),
    )
    p_open.add_argument("--hit", type=int, metavar="N",
                        help="open hit N (1-based) of --query — re-runs the "
                             "search deterministically")
    p_open.add_argument("--query", nargs="+", metavar="QUERY",
                        help="the search to re-run (required with --hit)")
    p_open.add_argument("--path", metavar="PATH",
                        help="open this file directly")
    p_open.add_argument("--loc", type=float, metavar="N",
                        help="seconds (audio/video) or line (code) to jump to")
    p_open.add_argument("--kind", metavar="KIND",
                        help="override the kind guess (doc,pdf,code,image,"
                             "audio,video,webpage)")
    p_open.add_argument("--dry-run", action="store_true",
                        help="print what would run; execute nothing")

    # --- v2 item 20 (F4): the init state machine (03 §5.1) ---
    p_init = sub.add_parser(
        "init", parents=[common],
        help="guided setup: env, model, folders, first ingest (resumable)",
        description=(
            "Guided first run: environment checks, embedding model, folder "
            "choice, optional speech/OCR setup, the first ingest, and a demo "
            "search. Resumable — Ctrl-C and re-run to continue where you "
            "left off (--restart starts over). goldmine only searches "
            "folders you add — never your whole Mac."
        ),
    )
    p_init.add_argument("--yes", action="store_true",
                        help="non-interactive: create and use ~/Documents/goldmine "
                             "(never sweeps existing folders), install the model "
                             "if missing, skip media")
    p_init.add_argument("--root", dest="roots", action="append", default=[],
                        metavar="PATH",
                        help="index this folder (repeatable). Explicit --root "
                             "folders are your own choice — --yes uses them "
                             "instead of creating the dedicated folder")
    p_init.add_argument("--restart", action="store_true",
                        help="discard saved init progress and start over")
    p_init.add_argument("--model-path", metavar="PATH",
                        help="adopt this local .gguf as the embedding model "
                             "(sha256-verified, no copy)")
    p_init.add_argument("--media", action="store_true",
                        help="enable speech transcription + screenshot OCR "
                             "without prompting (installs whisper, 574 MB)")
    p_init.add_argument("--skip-demo", action="store_true",
                        help="skip the end-of-init demo search")

    # --- v2 item 21 (F7/F8): topic guides (03 §5.8) ---
    p_help = sub.add_parser(
        "help", parents=[common],
        help="topic guides (getting-started, search-syntax, troubleshoot, …)",
        description="Print one topic guide, or list the topics.",
    )
    p_help.add_argument("topic", nargs="?", metavar="TOPIC",
                        help="a topic name (run `goldmine help` to list them)")

    return parser

def _add_model_commands(sub, name: str) -> None:
    """The `goldmine model` group (05 §4.3)."""
    kwargs: dict = {
        "parents": [_global_options()],
        "help": "pinned model manager: install/list/verify/use/adopt/check",
        "description": (
            "Install and verify manifest-pinned models into <home>/models "
            "(resumable downloads, sha256-verified), adopt existing files, bind "
            "the default embedding model, and run the live arch probe. "
            "`goldmine model list` shows every pinned key."
        ),
    }
    p_model = sub.add_parser(name, **kwargs)
    # required=True (S3): a bare `goldmine model` is an argparse usage error —
    # which in --json mode emits the error object on stdout via
    # _JsonAwareParser — never the old hand-rolled usage line with empty stdout
    verbs = p_model.add_subparsers(dest="model_verb", metavar="<verb>",
                                   required=True)
    # each verb re-carries the global options so `model list --json` (options
    # AFTER the verb) parses like every other command
    verb_common = _global_options()

    p_install = verbs.add_parser(
        "install", parents=[verb_common],
        help="download a pinned model (or --from PATH to register a local file)",
    )
    p_install.add_argument("name", metavar="KEY",
                           help="a pinned manifest key (`goldmine model list` "
                                "shows them)")
    p_install.add_argument("--from", dest="from_path", metavar="PATH",
                           help="register this existing file by path (no copy)")
    p_install.add_argument("--as", dest="alias", metavar="ALIAS",
                           help="record an alias name in the registry")

    verbs.add_parser("list", parents=[verb_common],
                     help="every pinned model and its install state")

    p_verify = verbs.add_parser(
        "verify", parents=[verb_common],
        help="recompute bytes+sha256 vs the pin",
    )
    p_verify.add_argument("name", metavar="KEY", nargs="?",
                          help="omit to verify every resolvable entry")

    p_use = verbs.add_parser(
        "use", parents=[verb_common],
        help="set the default embedding model (writes config llama.model)",
    )
    p_use.add_argument("name", metavar="KEY",
                       help="a pinned embedding-model key")

    p_adopt = verbs.add_parser(
        "adopt", parents=[verb_common],
        help="register an existing local file under a manifest key (sha-verified)",
    )
    p_adopt.add_argument("path", metavar="PATH",
                         help="the local model file (.gguf) to register")
    p_adopt.add_argument("name", metavar="KEY",
                         help="the manifest key to register it under")

    verbs.add_parser(
        "check", parents=[verb_common],
        help="live arch probe: spawn llama-server, embed 'ping', "
             "assert dim (catches the brew-b11146 trap)",
    )

def _rewrite_models_alias(argv: list[str]) -> list[str]:
    """C1: `goldmine models …` (02's spelling) is a pure argv rewrite to
    `goldmine model …` — truly hidden (never registers a parser, never shows
    in --help), with the verb map `models add X` == `model install X`.
    Only the FIRST non-flag token can be the command; global flags that take a
    value are skipped so their value is never mistaken for the command."""
    value_flags = {"--index", "--db", "--config", "--log-file"}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in value_flags:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        if token == "models":
            rest = argv[i + 1:]
            if rest and rest[0] == "add":
                rest = ["install"] + rest[1:]
            return argv[:i] + ["model"] + rest
        break  # first positional is some other command
    return argv
