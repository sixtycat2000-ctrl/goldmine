"""Filesystem walker: os.scandir recursion, exclusions, symlink policy, prune list.

Deterministic (sorted), single-threaded on purpose. Directory symlinks are not
followed; a visited (device, inode) set is the belt-and-braces cycle guard (V5c).
Hidden directories are not descended into; hidden FILES are still classified so
credential files like `.env` surface with their real reason.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import rules
from ..store import Store

logger = logging.getLogger("goldmine.walk")

TS_READ_BYTES = 600  # DigUp: read 600 bytes to sniff MPEG-TS


@dataclass
class Candidate:
    path: Path
    kind: str
    size: int
    mtime: float
    #: P1-3: POSIX identity (same dev+ino+size+mtime at a new path = a MOVE,
    #: not a delete+re-embed — DigUp IndexStore.swift:261-269).
    dev: int | None = None
    ino: int | None = None


@dataclass
class SkipNotice:
    path: str
    reason: str
    is_dir: bool = False   # folder-level notices are counted, not recorded as files
    #: DigUp A1-18: a folder the walk could not read is UNAVAILABLE, not empty.
    #: Callers treat these paths as deletion-exempt prefixes (rows under them
    #: are kept), so a TCC prompt or an ejected drive never reads as "deleted".
    unreachable: bool = False


#: F8 (v2 item 21): a stat/scandir refused with EPERM/EACCES is macOS privacy
#: (TCC), not an empty folder — "unreadable ≠ empty" is the DigUp lesson. The
#: notice is a folder-level SkipNotice with this exact reason so callers
#: (ingest summary, init) can name the fix instead of silently walking nothing.
TCC_REASON = (
    "protected by macOS (TCC) — grant Full Disk Access to your terminal, "
    "or index a copy elsewhere"
)


def _matches_exclude(rel: str, absolute: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(absolute, pat) for pat in patterns)


def walk_files(
    roots: list[Path],
    *,
    code: bool,
    exclude: list[str],
    max_file_bytes: int,
) -> Iterator[Candidate | SkipNotice]:
    """Yield Candidates (indexable files) and SkipNotices (with reasons), sorted.

    The caller records file-level SkipNotices as capability=unsupported rows;
    folder-level notices (is_dir=True) are only counted.

    Named `walk_files` (was `walk`): the module is `goldmine.ingest.walk`, and
    re-exporting a function of the same name at package level shadowed the
    module — `from goldmine.ingest import walk` yielded the FUNCTION (02 F12).
    The module name is importable again; the function keeps one public name.
    """
    visited: set[tuple[int, int]] = set()
    for root in sorted(roots, key=str):
        # P2-11: absolutize at the walk boundary — entry.path derives from the
        # root, so a relative root used to store CWD-dependent paths (churn and
        # duplicate rows when the same dir was re-ingested by absolute path).
        # Migration: indexes built before this fix may hold relative rows;
        # `goldmine ingest --reindex` rewrites them.
        root = Path(root).expanduser().resolve()
        try:
            root_stat = root.stat()
        except PermissionError:
            # F8: EPERM/EACCES on the root itself — TCC-protected, never
            # "empty". Surfaced, not swallowed.
            yield SkipNotice(str(root), TCC_REASON, is_dir=True, unreachable=True)
            continue
        except OSError as exc:
            # A1-18: the root passed the caller's start-of-run stat (F10) but
            # vanished before the walk reached it — unavailable, not empty.
            logger.warning("walk: cannot stat %s (%s)", root, exc)
            yield SkipNotice(str(root), f"cannot stat: {exc}", is_dir=True,
                             unreachable=True)
            continue
        if (root_stat.st_dev, root_stat.st_ino) in visited:
            continue
        visited.add((root_stat.st_dev, root_stat.st_ino))
        if stat.S_ISREG(root_stat.st_mode):
            yield from _handle_file(root, code=code, max_file_bytes=max_file_bytes)
            continue
        if not root.is_dir():
            # 02 F2: a FIFO/socket/device as the root itself — scandir would
            # only log a warning and silently index nothing; refuse with the
            # machine reason instead (reading it would block a worker).
            yield SkipNotice(str(root), "not a regular file")
            continue
        yield from _walk_dir(
            root, rel="", code=code, exclude=exclude, max_file_bytes=max_file_bytes,
            visited=visited,
        )


def _handle_file(path: Path, *, code: bool, max_file_bytes: int):
    """A root that is itself a file: classify + stat, same rules as directory walks."""
    name = path.name
    decision = _decide_file(path, name, code=code)
    if decision.skip_reason or decision.kind is None:
        yield SkipNotice(str(path), decision.skip_reason or "not an indexed kind")
        return
    try:
        st = path.stat()
    except OSError as exc:
        yield SkipNotice(str(path), f"stat failed: {exc}")
        return
    if not stat.S_ISREG(st.st_mode):
        # 02 F2: a FIFO/socket/device as the root itself — reading it would
        # block a worker forever (named pipe named *.txt hung whole ingests).
        yield SkipNotice(str(path), "not a regular file")
        return
    if st.st_size > max_file_bytes:
        yield SkipNotice(str(path), f"over size cap ({st.st_size} > {max_file_bytes} bytes)")
        return
    yield Candidate(
        path=path, kind=decision.kind, size=st.st_size, mtime=st.st_mtime,
        dev=st.st_dev, ino=st.st_ino,
    )


def _walk_dir(
    directory: Path,
    *,
    rel: str,
    code: bool,
    exclude: list[str],
    max_file_bytes: int,
    visited: set[tuple[int, int]],
) -> Iterator[Candidate | SkipNotice]:
    try:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
    except PermissionError:
        # F8: unreadable directory (TCC) — a notice with the fix, not silence
        yield SkipNotice(str(directory), TCC_REASON, is_dir=True, unreachable=True)
        return
    except OSError as exc:
        # A1-18 (DigUp Indexer.swift:118-120): a directory that cannot be
        # listed (drive ejected mid-walk, IO error) must not read as empty —
        # the notice marks the subtree unreachable so callers keep its rows.
        logger.warning("walk: cannot list %s (%s)", directory, exc)
        yield SkipNotice(str(directory), f"cannot list: {exc}", is_dir=True,
                         unreachable=True)
        return
    for entry in entries:
        name = entry.name
        abs_path = entry.path
        rel_path = f"{rel}/{name}" if rel else name

        if entry.is_symlink() and entry.is_dir(follow_symlinks=True):
            continue  # symlinked directory: never followed (V5c symlink policy)

        if name.startswith("."):
            if entry.is_dir(follow_symlinks=False):
                continue  # hidden directory: not descended into
            # hidden file: fall through, classified like anything else

        if entry.is_dir(follow_symlinks=False):
            try:
                st_dir = entry.stat(follow_symlinks=False)
            except PermissionError:
                yield SkipNotice(abs_path, TCC_REASON, is_dir=True, unreachable=True)
                continue
            except OSError as exc:
                logger.warning("walk: cannot stat %s (%s)", abs_path, exc)
                yield SkipNotice(abs_path, f"cannot stat: {exc}", is_dir=True,
                                 unreachable=True)
                continue
            if (st_dir.st_dev, st_dir.st_ino) in visited:
                continue
            visited.add((st_dir.st_dev, st_dir.st_ino))
            reason = rules.folder_skip(name)
            if reason is not None:
                yield SkipNotice(abs_path, reason, is_dir=True)
                continue
            if _matches_exclude(rel_path, abs_path, exclude):
                yield SkipNotice(abs_path, "excluded folder", is_dir=True)
                continue
            yield from _walk_dir(
                Path(abs_path), rel=rel_path, code=code, exclude=exclude,
                max_file_bytes=max_file_bytes, visited=visited,
            )
            continue

        # A file (dir-symlinks never reach here; file symlinks follow to content)
        if _matches_exclude(rel_path, abs_path, exclude):
            yield SkipNotice(abs_path, "excluded")
            continue
        try:
            st = entry.stat(follow_symlinks=True)
        except OSError as exc:
            logger.warning("walk: cannot stat %s (%s)", abs_path, exc)
            continue
        if not stat.S_ISREG(st.st_mode):
            # 02 F2: a named pipe (or socket/device) with a text-ish extension
            # classified as a doc and BLOCKED read_bytes() forever — one hung
            # worker stalled the whole run. Special files are skipped with the
            # honest reason; regular files (incl. followed file symlinks) are
            # unaffected.
            yield SkipNotice(abs_path, "not a regular file")
            continue
        if st.st_size > max_file_bytes:
            from ..util import human_size

            yield SkipNotice(abs_path, f"size cap (>{human_size(max_file_bytes)})")
            continue
        decision = _decide_file(Path(abs_path), name, code=code)
        if decision.kind is not None:
            yield Candidate(
                path=Path(abs_path), kind=decision.kind, size=st.st_size,
                mtime=st.st_mtime, dev=st.st_dev, ino=st.st_ino,
            )
        else:
            reason = decision.skip_reason or "unknown type"
            yield SkipNotice(abs_path, reason)


def _decide_file(path: Path, name: str, *, code: bool) -> rules.Decision:
    """Skip-reason gate first (secrets/minified/license), then extension classify."""
    if rules.is_secret(name):
        return rules.Decision(kind=None, skip_reason="key or password file")
    if rules.looks_minified(name):
        return rules.Decision(kind=None, skip_reason="minified")
    if rules.is_license(name):
        return rules.Decision(kind=None, skip_reason="license")
    ext = path.suffix.lstrip(".").lower()
    head: bytes | None = None
    if ext in rules.TS_EXTS:
        head = _read_head(path)
    return rules.classify(name, ext, code=code, head=head)


def _read_head(path: Path, n: int = TS_READ_BYTES) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


@dataclass(frozen=True)
class Deletions:
    """compute_deletions result: ids to prune + the A1-18 guard accounting.

    `kept_unreachable` counts rows kept because their root (or a subtree the
    walk could not read) was UNAVAILABLE at deletion time — surfaced, never
    silently skipped; `unavailable_roots` lists (root, reason) pairs.
    """

    gone: list[int]
    kept_unreachable: int = 0
    unavailable_roots: list[tuple[str, str]] = field(default_factory=list)


def compute_deletions(
    store: Store, roots: list[str], *, unreachable: list[str] | None = None
) -> Deletions:
    """Files under the CURRENT roots whose path vanished from disk
    (ARCHITECTURE §2.1 deletions).

    DEVIATION note (P1-2): earlier builds also pruned everything under NO
    current root, so ingesting root B deleted a previously ingested root A.
    A run only speaks for the roots it was given; removing a root from the
    rotation is `goldmine reset` territory. URL rows are never pruned here
    (a page missing from disk is meaningless for them).

    A1-18 guard (DigUp Indexer.swift:118-120, IndexService.swift:22-23): a
    root that vanished between the run's start-of-run stat (F10) and THIS
    point — or a subtree the walk reported unreachable (TCC, cannot-list) —
    is unavailable, NOT empty. Its rows are kept and counted, so an ejected
    drive never reads as "all deleted". Availability is re-checked here, not
    once at run start.
    """
    from ..store import _split_roots, _under_roots

    file_roots, url_roots = _split_roots(roots)
    unavailable_roots: list[tuple[str, str]] = []
    for r in file_roots:
        try:
            os.stat(r)
        except OSError as exc:
            unavailable_roots.append((r, str(exc)))
    # Prefix match with a real separator so /a/b never matches /a/bcd.
    keep_prefixes = sorted(
        {p if p.endswith(os.sep) else p + os.sep for p in (unreachable or [])}
    )

    def _kept_prefix(path: str) -> bool:
        return any(path.startswith(p) for p in keep_prefixes)

    out: list[int] = []
    kept = 0
    root_set = {root for root, _ in unavailable_roots}
    for fid, path, source in store.db.execute(
        "SELECT id, path, source FROM files ORDER BY id"
    ).fetchall():
        if source != "file":
            continue
        if not _under_roots(path, source, file_roots, url_roots):
            continue
        if _kept_prefix(path):
            kept += 1
            continue
        if any(_under_roots(path, source, [r], []) for r in root_set):
            kept += 1
            continue
        if not os.path.exists(path):
            out.append(fid)
    return Deletions(
        gone=out, kept_unreachable=kept, unavailable_roots=unavailable_roots
    )


def detect_moves(
    store: Store, roots: list[str], candidates: list[Candidate]
) -> list[tuple[int, str, str]]:
    """P1-3 (DigUp Indexer.swift:108-112): rows under the current roots whose
    stored path vanished, matched against THIS run's candidates by POSIX
    identity — same (dev, ino, size) with mtime within 1 ms means the file
    MOVED: return (fid, old_path, new_path) for store.move_file, keeping
    segments/vectors instead of delete + full re-embed ("Screenshot tools
    move screenshots into project folders all the time"). Rows with NULL
    dev/ino (URL rows, pre-v7 rows) are never matched; a vanished row
    without a matching candidate stays a deletion, as before."""
    from ..store import _split_roots, _under_roots

    file_roots, url_roots = _split_roots(roots)
    by_identity: dict[tuple[int, int], list[Candidate]] = {}
    for cand in candidates:
        if cand.dev is not None and cand.ino is not None:
            by_identity.setdefault((cand.dev, cand.ino), []).append(cand)

    moves: list[tuple[int, str, str]] = []
    for fid, path, dev, ino, size, mtime in store.db.execute(
        "SELECT id, path, dev, ino, size, mtime FROM files "
        "WHERE source = 'file' AND dev IS NOT NULL AND ino IS NOT NULL "
        "ORDER BY id"
    ).fetchall():
        if not _under_roots(path, "file", file_roots, url_roots):
            continue
        if os.path.exists(path):
            continue
        for cand in by_identity.get((dev, ino), ()):
            if cand.path == path:
                continue
            if cand.size != size:
                continue
            if mtime is None or abs(cand.mtime - mtime) > 0.001:
                continue
            moves.append((fid, path, str(cand.path)))
            break
    return moves
