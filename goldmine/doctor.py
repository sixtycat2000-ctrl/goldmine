"""doctor: honest environment checks with fix hints (SPEC §6, V5f).

Every check yields OK / WARN / FAIL + detail + fix_hint. Doctor NEVER fixes
anything and never starts the llama-server except under GOLDMINE_LIVE=1 (the
explicit live-load test). Exit contract: 0 when no FAIL, 4 when any FAIL.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, resolve_home
from .ingest import extract_pdf, media
from .util import human_size

STATUS_OK, STATUS_WARN, STATUS_FAIL = "ok", "warn", "fail"


@dataclass
class CheckResult:
    name: str
    status: str                    # ok | warn | fail
    detail: str = ""
    fix_hint: str | None = None


def _tool_check(name: str, argv: list[str], missing_hint: str) -> CheckResult:
    tool = shutil.which(argv[0])
    if tool is None:
        return CheckResult(name, STATUS_WARN, f"{argv[0]} not found", missing_hint)
    from .util import run_tool

    result = run_tool(argv, timeout_s=10.0)
    banner = (result.stderr or result.stdout).strip().splitlines()
    version = banner[0].strip() if banner else f"rc={result.rc}"
    return CheckResult(name, STATUS_OK, version)


def check_python() -> CheckResult:
    version = sys.version_info
    if version >= (3, 12):
        return CheckResult(
            "python", STATUS_OK, f"{platform.python_version()} ({sys.executable})"
        )
    return CheckResult(
        "python", STATUS_FAIL, f"{platform.python_version()} < 3.12",
        "pyenv install 3.12",
    )


def check_numpy() -> CheckResult:
    try:
        import numpy
    except ImportError:
        return CheckResult(
            "numpy", STATUS_FAIL, "not importable",
            "pip install numpy (>= 1.26) — declared dependency",
        )
    return CheckResult("numpy", STATUS_OK, numpy.__version__)


def check_llama(cfg: Config) -> CheckResult:
    from .backends.llama import (
        BREW_CANNOT_LOAD_NOTE,
        PINNED_MODEL_BASENAME,
        PINNED_MODEL_SHA256,
        home_server_path,
        probe_version,
        resolve_model_path,
        resolve_server_path,
    )
    from .util import sha256_file

    server, source = resolve_server_path(cfg.llama)
    if server is None:
        if source == "explicit":
            # P3-14: an explicitly configured but missing binary is its own
            # failure mode — never silently fall back to home/vendor/brew.
            return CheckResult(
                "backend llama", STATUS_FAIL,
                f"configured llama-server not found: {cfg.llama.server}",
                "fix llama.server / GOLDMINE_LLAMA_SERVER, or unset it to use "
                "the home/vendor/brew fallback",
            )
        return CheckResult(
            "backend llama", STATUS_FAIL,
            f"llama-server binary not found (looked at {cfg.llama.server}, "
            f"{home_server_path(cfg.llama)}, vendor/llama.cpp/build/bin, "
            "/opt/homebrew/opt/llama.cpp/bin)",
            "install the release asset into <home>/bin/llama-server "
            "(scripts/build_llama_tar.sh) or set llama.server / "
            "GOLDMINE_LLAMA_SERVER",
        )
    detail = f"server: {server} ({source})"
    from .backends.llama import _is_brew_path

    if source == "brew" or _is_brew_path(server):
        return CheckResult(
            "backend llama", STATUS_FAIL, f"{detail}\n{BREW_CANNOT_LOAD_NOTE}",
            "use the release asset (<home>/bin/llama-server, "
            "scripts/build_llama_tar.sh) or the vendor build (tag b11461) — "
            "the brew build cannot load gemma-embedding2",
        )
    version = probe_version(server)
    if version is None:
        return CheckResult(
            "backend llama", STATUS_FAIL,
            f"{detail} — `--version` failed (not a usable llama-server)",
        )

    model = resolve_model_path(cfg.llama)
    if not model.is_file():
        return CheckResult(
            "backend llama", STATUS_FAIL,
            f"{detail}; version {version}; model missing: {model}",
            "see models/SHA256SUMS in the repo (goldmine/models/)",
        )
    digest = sha256_file(model)
    hash_note = f"sha256 {digest[:16]}…"
    if model.name == PINNED_MODEL_BASENAME and digest != PINNED_MODEL_SHA256:
        return CheckResult(
            "backend llama", STATUS_FAIL,
            f"{detail}; version {version}; model {model.name} HASH MISMATCH "
            f"(expected {PINNED_MODEL_SHA256[:16]}…, got {digest[:16]}…)",
            "restore the pinned GGUF (models/SHA256SUMS) or knowingly rebuild "
            "the index",
        )
    sums = _verify_sha256sums(model.parent)
    extra = ""
    if sums is not None:
        bad = [name for name, ok, _why in sums if not ok]
        extra = (
            f"; SHA256SUMS: {len(sums) - len(bad)}/{len(sums)} verified"
            if not bad
            else f"; SHA256SUMS mismatch: {', '.join(bad)}"
        )
    status = STATUS_OK
    return CheckResult(
        "backend llama", status,
        f"{detail}; version {version}; model {model.name} ok ({hash_note}){extra}; "
        "media: text only (no image/audio embedding)",
    )


def _verify_sha256sums(models_dir: Path) -> list[tuple[str, bool, str]] | None:
    """Verify models/SHA256SUMS entries (name hash pairs). None when absent."""
    sums = models_dir / "SHA256SUMS"
    if not sums.is_file():
        return None
    from .util import sha256_file

    results: list[tuple[str, bool, str]] = []
    for line in sums.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        expected, _, name = line.partition(" ")
        name = name.strip().lstrip("*")
        path = models_dir / name
        if not path.is_file():
            results.append((name, False, "missing"))
            continue
        results.append(
            (name, sha256_file(path) == expected, "hash" )
        )
    return results


def check_server_install(cfg: Config) -> CheckResult:
    """The install-truth row (v2 item 4/A10): where WOULD a llama-server come
    from? An installed copy (pip/pipx/uv — no repo checkout) with neither a
    `<home>/bin` release asset nor a vendor build is the packaging bug class
    the row exists to name, with the release-asset + script fix."""
    from .backends.llama import (
        REPO_ROOT,
        home_server_path,
        resolve_server_path,
    )

    server, source = resolve_server_path(cfg.llama)
    if server is not None:
        return CheckResult(
            "llama-server install", STATUS_OK, f"{server} (source: {source})"
        )
    if source == "explicit":
        return CheckResult(
            "llama-server install", STATUS_FAIL,
            f"configured llama-server not found: {cfg.llama.server}",
            "fix llama.server / GOLDMINE_LLAMA_SERVER, or unset it",
        )
    in_checkout = (REPO_ROOT / ".git").exists()
    if in_checkout:
        return CheckResult(
            "llama-server install", STATUS_WARN,
            "no llama-server found (no <home>/bin asset, no vendor build, no "
            "brew install)",
            "scripts/build_llama_tar.sh builds the pinned llama.cpp (tag "
            "b11461) into vendor/llama.cpp and dist/",
        )
    return CheckResult(
        "llama-server install", STATUS_FAIL,
        "goldmine is an installed copy and no llama-server is available "
        f"(looked at {home_server_path(cfg.llama)}, repo vendor, brew)",
        "download the llama-server release asset into "
        f"{home_server_path(cfg.llama)} (goldmine model check verifies it), or "
        "build locally: scripts/build_llama_tar.sh",
    )


def check_models_embedding(cfg: Config) -> CheckResult:
    """Model-manager row 1: default embedding model resolved + pin verified.
    Uses the sidecar when valid (cheap); recomputes only when stale/absent."""
    import json as _json

    from .backends.llama import resolve_model_path
    from .model_manifest import by_key

    model = resolve_model_path(cfg.llama)
    if not model.is_file():
        return CheckResult(
            "models: embedding", STATUS_FAIL,
            f"default embedding model not found: {model}",
            "goldmine model install embeddinggemma-2-Q8_0 (downloads into "
            "<home>/models), or set llama.model / GOLDMINE_MODEL",
        )
    entry = by_key(model.stem)
    sha_note = "unverified (no manifest pin for this file)"
    ok = None
    if entry is not None:
        from .util import sha256_file

        st = model.stat()
        try:
            cached = _json.loads(
                model.with_name(model.name + ".sha256").read_text()
            )
            if cached.get("size") == st.st_size and cached.get("mtime_ns") == st.st_mtime_ns:
                sha = str(cached["sha256"])
            else:
                sha = sha256_file(model)
        except (OSError, ValueError, KeyError, _json.JSONDecodeError):
            sha = sha256_file(model)
        ok = sha == entry.sha256 and st.st_size == entry.bytes
        sha_note = (
            f"sha256 {sha[:16]}… matches the pin"
            if ok else
            f"PIN MISMATCH (expected {entry.sha256[:16]}…, {entry.bytes:,} bytes)"
        )
    return CheckResult("models: embedding", STATUS_OK if ok is not False else STATUS_FAIL,
                       f"{model.name} at {model} — {sha_note}")


def check_models_media(cfg: Config) -> CheckResult:
    """Model-manager row 2: whisper + VAD models present or honestly noted
    absent (ASR lands with v2 item B3; the row makes the state visible now)."""
    from .model import ModelManager

    manager = ModelManager(cfg)
    missing = []
    found = []
    for entry in manager.manifest:
        if entry.role not in ("asr", "vad"):
            continue
        path, source = manager.resolve(entry.key)
        if path is None:
            missing.append(entry.key)
        else:
            found.append(f"{entry.key} ({source})")
    if missing:
        return CheckResult(
            "models: asr/vad", STATUS_WARN,
            f"not installed: {', '.join(missing)}"
            + (f"; present: {', '.join(found)}" if found else ""),
            "goldmine model install " + " ; ".join(missing)
            + " — ASR (speech-to-text) stays unavailable until then",
        )
    return CheckResult("models: asr/vad", STATUS_OK, ", ".join(found))


def check_foresight(cfg: Config) -> CheckResult:
    from .backends.foresight import ForesightBackend, grpcio_available

    if not grpcio_available():
        return CheckResult(
            "backend foresight", STATUS_WARN,
            "grpcio not installed — the experimental foresight backend is "
            "unavailable (this is fine: llama/mock cover text)",
            "pip install --user grpcio",
        )
    backend = ForesightBackend(cfg.foresight)
    socket_path = backend.socket_path()
    if not socket_path.exists():
        return CheckResult(
            "backend foresight", STATUS_WARN,
            f"socket {socket_path} does not exist (helper not running) — "
            "optional backend, unavailability is data",
            "start the Foresight app; the helper owns the socket",
        )
    try:
        version = backend.ping()
    except Exception as exc:
        return CheckResult(
            "backend foresight", STATUS_FAIL,
            f"Ping failed on {socket_path}: {exc}",
        )
    finally:
        backend.close()
    expected = cfg.foresight.expect_version
    if expected and version != expected:
        return CheckResult(
            "backend foresight", STATUS_WARN,
            f"helper version {version} != pinned {expected} "
            f"({socket_path})",
            "update foresight.expect_version or rebuild the index after a "
            "helper upgrade",
        )
    return CheckResult(
        "backend foresight", STATUS_OK,
        f"helper {version} on {socket_path} (experimental; text+image+audio)",
    )


def check_mock() -> CheckResult:
    from .backends.mock import MockBackend

    backend = MockBackend(dim=768)
    vec = backend.embed_texts(["doctor self-test"])
    norm = float((vec[0] @ vec[0]) ** 0.5)
    cap = backend.capability()
    media_note = (
        "text+image+audio" if cap.image and cap.audio
        else "text only (fake media via multimodal=True)"
    )
    return CheckResult(
        "backend mock", STATUS_OK,
        f"in-process, dim 768 (norm {norm:.3f}), media: {media_note} — "
        "always available",
    )


def check_live_llama(cfg: Config) -> CheckResult:
    """GOLDMINE_LIVE=1 only: spawn + embed 'ping' -> expect 768."""
    from .backends.llama import LlamaServerBackend
    from .backends.base import BackendStartupError, EmbeddingError

    backend = LlamaServerBackend(cfg.llama)
    try:
        backend.startup()
        vectors = backend.embed_texts(["ping"])
        dim = int(vectors.shape[1])
        backend.close()
    except (BackendStartupError, EmbeddingError) as exc:
        backend.close()
        return CheckResult(
            "backend llama live", STATUS_FAIL, f"live load test failed: {exc}",
        )
    return CheckResult(
        "backend llama live", STATUS_OK,
        f"spawned + embedded 'ping' -> dim {dim} on 127.0.0.1 (closed cleanly)",
    )


def check_index(cfg: Config, db: Path | None) -> CheckResult:
    if db is None or not Path(db).exists():
        return CheckResult(
            "index", STATUS_WARN,
            f"no index at {db}",
            "run `goldmine ingest <path>` to create one",
        )
    from .store import Store

    store = Store(db)
    try:
        schema = store.meta("schema_version")
        fingerprint = store.meta("fingerprint_id")
        counts = store.counts()
        orphans = store.db.execute(
            "SELECT COUNT(*) FROM segments s LEFT JOIN files f ON f.id = s.file "
            "WHERE f.id IS NULL"
        ).fetchone()[0]
        # fts orphans: rows whose segment is gone. FTS5 has no FK cascade, so a
        # crash between the two deletes (or any pre-fix build) leaves them behind.
        fts_orphans = store.db.execute(
            "SELECT COUNT(*) FROM fts WHERE rowid NOT IN (SELECT id FROM segments)"
        ).fetchone()[0]
        detail = (
            f"schema v{schema or '?'}, fingerprint "
            f"{fingerprint or 'none (keyword-only)'}, files {counts.files}, "
            f"vectors {counts.vectors}, segments {counts.segments}"
        )
        if orphans or fts_orphans:
            parts = []
            if orphans:
                parts.append(f"{orphans} orphan segment row(s)")
            if fts_orphans:
                parts.append(f"{fts_orphans} orphan fts row(s)")
            return CheckResult(
                "index", STATUS_FAIL, f"{detail}; {'; '.join(parts)}",
                "report this — a crash left index rows without their owner",
            )
        if schema and int(schema) != store.SCHEMA_VERSION:
            return CheckResult(
                "index", STATUS_WARN,
                f"schema v{schema}, this build has v{store.SCHEMA_VERSION}",
            )
        if fingerprint:
            from .backends.mock import MockBackend
            from . import fingerprints

            # honest: without knowing which backend built the index we can only
            # verify presence + shape, not identity (live gate does that)
            fp_json = store.meta("fingerprint_json") or ""
            parsed = json.loads(fp_json) if fp_json else {}
            detail += f" ({parsed.get('backend')}/{parsed.get('model_id')}/dim {parsed.get('dim')})"
        return CheckResult("index", STATUS_OK, detail)
    finally:
        store.close()


# ------------------------------------------------------- deep checks (--deep)
#
# doctor --deep (05 §4.8): integrity + FK over the index file, a staleness
# summary row, cards-vs-index fingerprint consistency, and backup dir health.
# Plain sqlite3 connections only — deep checks never mutate the index.


def _open_ro(db: Path):
    """Short-lived connection for read-only checks (no DDL, no Store open).

    Goes through the package's one connection factory in ro mode (storage
    critique 03 F6): a mode=ro URI that cannot create or mutate the file — the
    old plain connect here was read-WRITE despite the name, and would have
    created an empty database had it ever been pointed at a missing path."""
    from .store import connect_index

    return connect_index(db, ro=True)


def check_integrity_deep(db: Path) -> CheckResult:
    con = _open_ro(db)
    try:
        finding = con.execute("PRAGMA integrity_check").fetchone()
        integrity = finding[0] if finding else "no result"
        fk_violations = con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()
    size_mb = Path(db).stat().st_size / (1 << 20)
    if integrity != "ok":
        return CheckResult(
            "index integrity", STATUS_FAIL,
            f"integrity_check: {integrity} ({size_mb:.1f} MB file)",
            "restore a `goldmine backup` copy — this file is corrupt",
        )
    if fk_violations:
        return CheckResult(
            "index integrity", STATUS_FAIL,
            f"integrity ok but {len(fk_violations)} foreign-key violation(s): "
            f"{fk_violations[:3]}",
            "restore a `goldmine backup` copy, or report this",
        )
    return CheckResult(
        "index integrity", STATUS_OK,
        f"integrity_check ok, foreign_key_check clean ({size_mb:.1f} MB file)",
    )


def check_staleness_deep(db: Path) -> CheckResult:
    """The staleness summary row: per-file identity drift, never a refusal."""
    from . import fingerprints

    con = _open_ro(db)
    try:
        live_embed = None
        row = con.execute(
            "SELECT value FROM meta WHERE key = 'fingerprint_json'"
        ).fetchone()
        if row:
            try:
                live_embed = fingerprints.embed_id_from_fingerprint(json.loads(row[0]))
            except (ValueError, TypeError, json.JSONDecodeError):
                live_embed = None
        report = fingerprints.staleness_report(con, live_embed)
    finally:
        con.close()
    detail = (
        f"{report['stale_files']} of {report['files']} files stale "
        f"(embed {report['embed_stale']} · chunker {report['chunker_stale']} · "
        f"extractor {report['extractor_stale']})"
    )
    if report["stale_files"]:
        return CheckResult(
            "staleness", STATUS_WARN, detail,
            "re-run `goldmine ingest <roots>` to refresh the stale files",
        )
    return CheckResult("staleness", STATUS_OK, detail)


def check_cards_fingerprint(db: Path) -> CheckResult:
    """Cards pin the fingerprint they were distilled under; a mismatch against
    the index fingerprint means the card generation predates the current
    vectors (v1 already kept one generation — this makes the drift visible)."""
    con = _open_ro(db)
    try:
        note_row = con.execute(
            "SELECT value FROM meta WHERE key = 'cards_v2_rebuild_note'"
        ).fetchone()
        active_cards = con.execute(
            "SELECT COUNT(*) FROM cards WHERE status = 'active'"
        ).fetchone()[0]
        n_cards, card_fp = con.execute(
            "SELECT COUNT(*), MIN(fingerprint_id) FROM cards"
        ).fetchone()
        row = con.execute(
            "SELECT value FROM meta WHERE key = 'fingerprint_id'"
        ).fetchone()
        index_fp = row[0] if row else None
        distinct_fps = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT fingerprint_id FROM cards"
            ).fetchall()
        ]
    finally:
        con.close()
    # F11 (R6 §3.3): the rebuild announcement's warn line while the note
    # exists AND no active cards are present
    if note_row and note_row[0] and active_cards == 0:
        return CheckResult(
            "cards fingerprint", STATUS_WARN,
            note_row[0],
            "run `goldmine distill` to build v2 cards",
        )
    if not n_cards:
        return CheckResult(
            "cards fingerprint", STATUS_OK,
            "no cards (run `goldmine distill` to build golden cards)",
        )
    if index_fp is None:
        return CheckResult(
            "cards fingerprint", STATUS_WARN,
            f"{n_cards} cards but the index has no fingerprint",
        )
    if distinct_fps != [index_fp]:
        return CheckResult(
            "cards fingerprint", STATUS_WARN,
            f"{n_cards} cards were distilled under "
            f"{', '.join(distinct_fps)} != index {index_fp}",
            "re-run `goldmine distill` to rebuild cards for the current vectors",
        )
    return CheckResult(
        "cards fingerprint", STATUS_OK,
        f"{n_cards} cards match the index fingerprint {card_fp}",
    )


def check_backup_health(cfg: Config, db: Path) -> CheckResult:
    """Backup dir health: existence and age of the newest backup of THIS index."""
    from . import migrations

    home = resolve_home(cfg)
    newest = migrations.last_backup(Path(db), home)
    if newest is None:
        return CheckResult(
            "backups", STATUS_WARN,
            f"no backup of {Path(db).name} under {migrations.backup_dir_for(home)}",
            "run `goldmine backup` (ASR/OCR transcripts are irreplaceable "
            "derived data)",
        )
    path, mtime = newest
    age_days = (time.time() - mtime) / 86400.0
    detail = (
        f"last backup {path.name} "
        f"({human_size(path.stat().st_size)}, {age_days:.1f} days ago)"
    )
    if age_days > 7.0:
        return CheckResult(
            "backups", STATUS_WARN, detail,
            "run `goldmine backup` — backups are aging",
        )
    return CheckResult("backups", STATUS_OK, detail)


def check_config(cfg: Config) -> CheckResult:
    """Env vars / config values must at least parse to known values."""
    known = {"llama", "foresight", "mock"}
    backend = cfg.backend
    if backend not in known:
        return CheckResult(
            "config", STATUS_FAIL,
            f"GOLDMINE_BACKEND/backend.default = {backend!r} is not one of "
            f"{sorted(known)}",
            "set --backend llama|foresight|mock",
        )
    return CheckResult(
        "config", STATUS_OK,
        f"default backend {backend}, home {cfg.home}",
    )


def check_disk(cfg: Config) -> CheckResult:
    home = resolve_home(cfg)
    try:
        usage = shutil.disk_usage(home)
    except OSError as exc:
        return CheckResult("disk", STATUS_WARN, f"cannot stat {home}: {exc}")
    free_gb = usage.free / (1 << 30)
    if free_gb < 1.0:
        return CheckResult(
            "disk", STATUS_FAIL,
            f"{free_gb:.1f} GB free on {home}",
            "free space — ingest and embeddings need headroom",
        )
    if free_gb < 5.0:
        return CheckResult(
            "disk", STATUS_WARN, f"{free_gb:.1f} GB free on {home}",
        )
    return CheckResult("disk", STATUS_OK, f"{free_gb:.1f} GB free on {home}")


def run_checks(
    cfg: Config, db: Path | None, probe: bool = False, deep: bool = False
) -> list[CheckResult]:
    """The registry (MODULE_MAP): python, numpy, backends, tools, index, disk.
    `deep` adds the 05 §4.8 deep checks (integrity/FK, staleness, cards,
    backups) — only meaningful when the index exists."""
    results: list[CheckResult] = [
        check_python(),
        check_numpy(),
        check_llama(cfg),
        check_server_install(cfg),
        check_models_embedding(cfg),
        check_models_media(cfg),
        check_foresight(cfg),
        check_mock(),
        _tool_check(
            "pdftotext", [extract_pdf.pdftotext_path() or "pdftotext", "-v"],
            "brew install poppler (PDF text extraction)",
        ),
        CheckResult(*_textutil_check()),
        _tool_check(
            "ffmpeg", [media.ffmpeg_path() or "ffmpeg", "-version"],
            "brew install ffmpeg (audio/video metadata + windows)",
        ),
        _tool_check(
            "ffprobe", [media.ffprobe_path() or "ffprobe", "-version"],
            "brew install ffmpeg",
        ),
        _whisper_check(),
        check_ocr(cfg),
        check_clt(),
        check_index(cfg, db),
        check_serve_daemon(cfg),
        check_config(cfg),
        check_disk(cfg),
    ]
    if probe or os.environ.get("GOLDMINE_LIVE") == "1":
        results.append(check_live_llama(cfg))
    if deep and db is not None and Path(db).exists():
        results.append(check_integrity_deep(Path(db)))
        results.append(check_staleness_deep(Path(db)))
        results.append(check_cards_fingerprint(Path(db)))
        results.append(check_backup_health(cfg, Path(db)))
    elif deep:
        results.append(CheckResult(
            "index integrity", STATUS_WARN,
            "skipped — no index to check",
            "run `goldmine ingest <path>` to create one",
        ))
    return results


def _textutil_check() -> tuple[str, str, str]:
    from .ingest.extract_text import textutil_path, textutil_version

    if textutil_path() is None:
        return (
            "textutil", STATUS_WARN, "not found (rtf/doc/odt degrade to metadata-only)",
            "macOS ships textutil; non-macOS is fine without it",
        )
    return ("textutil", STATUS_OK, textutil_version())


def _whisper_check() -> CheckResult:
    tool = shutil.which("whisper-cli")
    if tool is None:
        return CheckResult(
            "whisper", STATUS_WARN, "not found (stretch ASR hook unavailable)",
            # copy-pasteable bar (03 §5.7): both commands run as-is
            "brew install whisper-cpp && goldmine model install "
            "whisper-large-v3-turbo-q5_0",
        )
    return CheckResult("whisper", STATUS_OK, tool)


def check_ocr(cfg: Config) -> CheckResult:
    """OCR row (v2 B4/02 §5.2): helper presence + the `goldmine-ocr 1`
    handshake. A missing/stale binary means OCR OFF — never a crash; fix
    paths are the prebuilt asset (user path) or CLT compile (dev fallback)."""
    from . import ocr

    if not cfg.ocr.enabled:
        return CheckResult(
            "ocr", STATUS_WARN, "disabled (ocr.enabled = false)",
            "set [ocr] enabled = true to read screenshots/scans",
        )
    helper = ocr.helper_path(cfg)
    if helper is None:
        return CheckResult(
            "ocr", STATUS_WARN,
            "goldmine-ocr not installed — screenshots and scanned PDFs stay "
            "unindexed text",
            "install the prebuilt goldmine-ocr release asset into "
            f"{resolve_home(cfg) / 'bin'} (no dev tools needed), or compile "
            "with scripts/build_ocr_helper.sh (needs Command Line Tools)",
        )
    if not os.access(helper, os.X_OK):
        return CheckResult(
            "ocr", STATUS_WARN, f"{helper} is not executable — OCR off",
            f"chmod +x {helper} or reinstall the helper",
        )
    banner = ocr.handshake(cfg)
    if banner is None:
        return CheckResult(
            "ocr", STATUS_WARN,
            f"{helper} failed the --version handshake (stale binary? "
            "macOS majors can break it) — OCR off",
            "rebuild with scripts/build_ocr_helper.sh (or reinstall the "
            "release asset)",
        )
    note = (
        "screenshot + scanned-PDF text extraction ready"
        if banner.strip() == ocr.HELPER_VERSION else
        f"unexpected handshake {banner!r} (expected {ocr.HELPER_VERSION!r})"
    )
    status = STATUS_OK if banner.strip() == ocr.HELPER_VERSION else STATUS_WARN
    return CheckResult("ocr", status, f"{helper} ({note})")


def check_clt() -> CheckResult:
    """Command Line Tools row (B13): swiftc is ONLY needed to compile
    goldmine-ocr from source — the prebuilt release asset needs no dev tools."""
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        return CheckResult(
            "clt (swiftc)", STATUS_WARN,
            "swiftc not found — only needed to BUILD goldmine-ocr from "
            "source; the release asset needs no dev tools",
            "xcode-select --install",
        )
    return CheckResult("clt (swiftc)", STATUS_OK, swiftc)


def check_serve_daemon(cfg: Config) -> CheckResult:
    """R8 serve row (D6 + review F4) — lives in goldmine.serve so the daemon's
    rendering and the doctor row can never drift apart. Doctor never fixes:
    the orphan receipt-sweep runs in `serve start`, not here."""
    from .serve import check_serve_daemon as _check

    return _check(cfg)


def summary(results: list[CheckResult]) -> dict:
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return {"ok": counts["fail"] == 0, "counts": counts}
