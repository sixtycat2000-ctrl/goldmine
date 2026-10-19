#!/usr/bin/env python3
"""Per-kind coverage KPI report (v2 B8, 02 §9) over a goldmine index.

Prints the per-kind x capability table plus the fine-grained (kind.ext)
content-indexed percentage, and optionally gates the numbers against the
02 §9 floors. `--fixture` builds+ingests the deterministic coverage fixture
(mock multimodal backend, --code on) and reports on that fresh index.

Floors (02 §9 v2 targets, restated here as the gate's source of truth):
  txt/md/html/docx/rtf/doc/odt/eml/epub .... 100 % content-indexed
  pdf-text ................................ 100 % content-indexed
  code (--code) ............................ 100 % content-indexed
  webpage .................................. 100 % content-indexed
  image ........ 100 % MULTIMODAL backends only; llama: metadata-only expected
  audio ........ >= 98 % MULTIMODAL only; llama: metadata-only expected
  video ........ >= 95 % MULTIMODAL only (soundtrack); llama: metadata-only
  pdf-scanned .. >= 95 % — ADVISORY until OCR lands (needs:ocr)
  screenshot OCR text on 100 % of screenshots — ADVISORY until OCR lands
  secrets/minified/archives ................. 0 % by design, reasons recorded
  (audio/video floors additionally require ffprobe; the checks are skipped
   with a printed reason when the tool is absent)

Backend awareness: the report reads meta.fingerprint_json to learn which
backend built the index, so the same gate works for mock/foresight (media
floors enforced) and llama (media expected honestly metadata-only).

Usage:
  coverage_report.py DB [--json] [--gate]
  coverage_report.py --fixture [--fixture-dir DIR] [--json] [--gate]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from goldmine.store import Store  # noqa: E402

#: fine-bucket floors: fraction of scanned files that must be content_indexed
TEXT_FLOOR = 1.0
AUDIO_FLOOR = 0.98
VIDEO_FLOOR = 0.95
DOC_EXTS = {
    "txt": "txt", "text": "txt", "md": "md", "markdown": "md", "html": "html",
    "htm": "html", "docx": "docx", "rtf": "rtf", "doc": "doc", "odt": "odt",
    "eml": "eml", "epub": "epub",
}


def _ext(path: str) -> str:
    name = Path(path).name.lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


#: by-design-skip reasons: unsupported rows that must NOT count against a
#: kind's floor (02 §9: secrets/minified/archives are 0 % by design)
NOISE_DETAIL_MARKERS = ("minified", "license", "key or password", "archive")


def _is_noise(capability: str, detail: str) -> bool:
    return capability == "unsupported" and any(
        marker in (detail or "") for marker in NOISE_DETAIL_MARKERS
    )


def collect(db_path: Path) -> dict:
    """Read one index -> per-kind/fine tables + backend facts."""
    store = Store(db_path)
    try:
        rows = store.db.execute(
            "SELECT path, kind, capability, coalesce(detail,''), info, state "
            "FROM files"
        ).fetchall()
        fp_raw = store.meta("fingerprint_json")
        backend = None
        multimodal = False
        if fp_raw:
            try:
                fp = json.loads(fp_raw)
                backend = fp.get("backend")
                multimodal = "media" in fp or backend == "foresight"
            except json.JSONDecodeError:
                pass
    finally:
        store.close()

    per_kind: dict[str, dict[str, int]] = {}
    fine: dict[str, dict[str, int]] = {}
    for path, kind, capability, detail, info_raw, state in rows:
        capability = capability or "pending"
        per_kind.setdefault(kind, {}).setdefault(capability, 0)
        per_kind[kind][capability] += 1

        bucket = kind
        if kind == "doc":
            bucket = f"doc:{DOC_EXTS.get(_ext(path), _ext(path) or 'none')}"
        elif kind == "pdf":
            bucket = (
                "pdf-scanned"
                if capability == "metadata_only" and "OCR" in detail
                else "pdf-text"
            )
        elif kind == "image":
            try:
                screenshot = bool(json.loads(info_raw or "{}").get("screenshot"))
            except json.JSONDecodeError:
                screenshot = False
            bucket = "image:screenshot" if screenshot else "image:photo"
        fine.setdefault(bucket, {})
        if _is_noise(capability, detail):
            # by-design skip: never counts against the kind's floor
            fine[bucket]["noise-excluded"] = fine[bucket].get("noise-excluded", 0) + 1
        else:
            fine[bucket][capability] = fine[bucket].get(capability, 0) + 1
    return {
        "db": str(db_path),
        "backend": backend,
        "multimodal": multimodal,
        "per_kind": per_kind,
        "fine": fine,
        "ffprobe": shutil.which("ffprobe") is not None,
    }


def _ci_pct(counts: dict[str, int]) -> float:
    total = sum(n for c, n in counts.items() if c != "noise-excluded")
    return counts.get("content_indexed", 0) / total if total else 0.0


def evaluate(report: dict) -> list[dict]:
    """Floor checks -> [{name, status: ok|fail|advisory|skipped, detail}]."""
    fine = report["fine"]
    checks: list[dict] = []
    multimodal = report["multimodal"]
    ffprobe = report["ffprobe"]

    def text_bucket(name: str, floor: float = TEXT_FLOOR) -> None:
        if name not in fine:
            checks.append({"name": name, "status": "skipped",
                           "detail": "no files of this kind in the index"})
            return
        pct = _ci_pct(fine[name])
        checks.append({
            "name": name, "status": "ok" if pct >= floor else "fail",
            "detail": f"{pct:.0%} content-indexed (floor {floor:.0%}, "
                      f"n={sum(fine[name].values())})",
        })

    for bucket in ("doc:txt", "doc:md", "doc:html", "doc:docx", "doc:eml",
                   "doc:epub", "pdf-text", "code"):
        text_bucket(bucket)

    # media: floors only under a multimodal backend; under llama the honest
    # expectation is metadata-only (02 §9 / task brief)
    for bucket, floor in (("image:photo", 1.0), ("image:screenshot", 1.0),
                          ("audio", AUDIO_FLOOR), ("video", VIDEO_FLOOR)):
        if bucket not in fine:
            checks.append({"name": bucket, "status": "skipped",
                           "detail": "no files of this kind in the index"})
            continue
        counts = fine[bucket]
        pct = _ci_pct(counts)
        if multimodal:
            if bucket in ("audio", "video") and not ffprobe:
                checks.append({"name": bucket, "status": "skipped",
                               "detail": f"{pct:.0%} content-indexed but ffprobe "
                                         "absent — media floors need it"})
                continue
            checks.append({
                "name": bucket, "status": "ok" if pct >= floor else "fail",
                "detail": f"{pct:.0%} content-indexed (floor {floor:.0%}, "
                          f"n={sum(counts.values())})",
            })
        else:
            md_only = counts.get("metadata_only", 0) / sum(counts.values())
            checks.append({
                "name": bucket,
                "status": "ok" if md_only >= AUDIO_FLOOR else "fail",
                "detail": f"text-only backend: {md_only:.0%} metadata-only as "
                          "expected (content floor N/A under "
                          f"{report['backend']})",
            })

    # scanned PDFs + screenshot OCR: advisory until the OCR item (B4) lands
    if "pdf-scanned" in fine:
        pct = _ci_pct(fine["pdf-scanned"])
        checks.append({
            "name": "pdf-scanned", "status": "advisory",
            "detail": f"{pct:.0%} content-indexed (floor >=95% activates with "
                      "OCR, needs:ocr; today honest metadata-only)",
        })
    # noise must stay 0 % by design — archives carry the honest reason
    archive_unsupported = 0
    archive_indexed = 0
    for row in _archive_rows(report):
        if row[0] == "unsupported" and "archive" in (row[1] or ""):
            archive_unsupported += 1
        elif row[0] == "content_indexed":
            archive_indexed += 1
    if archive_unsupported or archive_indexed:
        checks.append({
            "name": "archives-zero-by-design",
            "status": "ok" if archive_indexed == 0 and archive_unsupported > 0 else "fail",
            "detail": f"{archive_indexed} indexed / {archive_unsupported} "
                      "unsupported with the archive reason",
        })
    return checks


def _archive_rows(report: dict):
    # F12 hygiene: synthetic reports (unit fixtures with a junk "db" name like
    # "x") have no index behind them — opening a Store would CREATE a stray
    # CWD-relative sqlite file on every test run. No file -> no archive rows,
    # which is exactly what an empty real index yields for this check.
    db = Path(report["db"])
    if not db.is_file():
        return []
    store = Store(report["db"])
    try:
        return store.db.execute(
            "SELECT capability, coalesce(detail,'') FROM files "
            "WHERE lower(path) LIKE '%.zip' OR lower(path) LIKE '%.tar.gz' "
            "OR lower(path) LIKE '%.7z' OR lower(path) LIKE '%.rar'"
        ).fetchall()
    finally:
        store.close()


def render_text(report: dict, checks: list[dict]) -> str:
    lines = [
        f"coverage: {report['db']} "
        f"(backend {report['backend']}, "
        f"{'multimodal' if report['multimodal'] else 'text-only'}, "
        f"ffprobe {'yes' if report['ffprobe'] else 'no'})",
        "",
        f"{'kind':<20} {'capability breakdown':<44} ci%",
        "-" * 72,
    ]
    for kind in sorted(report["per_kind"]):
        counts = report["per_kind"][kind]
        breakdown = ", ".join(f"{c} {n}" for c, n in sorted(counts.items()))
        lines.append(f"{kind:<20} {breakdown:<44} {_ci_pct(counts):>5.0%}")
    lines.append("")
    lines.append(f"{'bucket':<20} {'status':<9} detail")
    lines.append("-" * 72)
    for check in checks:
        lines.append(
            f"{check['name']:<20} {check['status']:<9} {check['detail']}"
        )
    return "\n".join(lines)


def _read_version_file(fixture_dir: Path) -> dict | None:
    """The stored fixture_version.json, or None (missing/unparsable = stale)."""
    try:
        return json.loads((fixture_dir / "fixture_version.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def ensure_fresh_fixture(fixture_dir: Path, *, stale: str = "rebuild") -> dict:
    """F9: build the fixture if absent; when present, verify its stored
    fixture_version.json (version constant + generator content hash + file
    count) against THIS generator. A stale tree auto-rebuilds — or, with
    stale='fail' (--no-rebuild), refuses loudly, so a coverage report is
    never computed over files the current generator no longer writes.
    Returns the fresh manifest."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import make_coverage_fixture as mcf

    manifest = mcf.build(fixture_dir)
    expected = mcf.fixture_version_record(manifest)
    stored = _read_version_file(fixture_dir)
    if stored == expected:
        return manifest
    if stored is None:
        detail = "fixture_version.json missing"
    else:
        detail = (
            f"version {stored.get('version')!r} != {expected['version']!r}, "
            f"generator drifted: "
            f"{stored.get('generator_sha256') != expected['generator_sha256']}, "
            f"file count {stored.get('files')!r} != {expected['files']!r}"
        )
    if stale == "fail":
        print(
            f"error: coverage fixture at {fixture_dir} is stale ({detail}) — "
            "rebuild it (drop --no-rebuild) or investigate",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"fixture stale ({detail}) — rebuilding", file=sys.stderr)
    return mcf.build(fixture_dir, force=True)


def run_fixture(fixture_dir: Path, *, stale: str = "rebuild") -> Path:
    """Build the fixture if needed (rebuilding a stale one, F9) and ingest it
    into a fresh index OUTSIDE the fixture tree (the index/home files must
    not be ingested themselves)."""
    manifest = ensure_fresh_fixture(fixture_dir, stale=stale)
    root = Path(manifest["root"])
    side = root.parent / f"{root.name}.kpi"

    from goldmine.backends.mock import MockBackend
    from goldmine.config import Config
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store

    side.mkdir(parents=True, exist_ok=True)
    db_path = side / "coverage.sqlite"
    for stale in side.glob("coverage.sqlite*"):
        stale.unlink()
    cfg = Config(home=str(side / "gmhome"))
    store = Store(db_path)
    backend = MockBackend(dim=768, multimodal=True)
    try:
        pipeline = IngestPipeline(cfg, store, backend)
        report = pipeline.run([str(root)], code=True)
    finally:
        store.close()
        backend.close()
    if report.failed:
        print(f"WARNING: {report.failed} fixture files failed: {report.failures}")
    return db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="goldmine per-kind coverage KPI report (02 §9)",
        epilog=__doc__.split("Floors", 1)[1].join(("Floors", "")),
    )
    parser.add_argument("target", nargs="?", default=None,
                        help="index db path, or the literal --fixture")
    parser.add_argument("--fixture", action="store_true",
                        help="build + ingest the coverage fixture, then report")
    parser.add_argument("--fixture-dir", default="/tmp/gm_covfix")
    parser.add_argument("--no-rebuild", action="store_true",
                        help="F9: refuse (exit 2) when the fixture tree is "
                             "stale instead of rebuilding it automatically")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--gate", action="store_true",
                        help="exit 1 when any enforced floor fails")
    args = parser.parse_args(argv)

    if args.fixture or args.target == "--fixture":
        db_path = run_fixture(Path(args.fixture_dir).expanduser(),
                              stale="fail" if args.no_rebuild else "rebuild")
    elif args.target:
        db_path = Path(args.target).expanduser()
        if not db_path.is_file():
            print(f"error: no index at {db_path}")
            return 2
    else:
        parser.error("give an index db path or --fixture")
        return 2

    report = collect(db_path)
    checks = evaluate(report)
    payload = {**report, "checks": checks}

    if args.json:
        print(json.dumps(payload, indent=1, sort_keys=True))
    else:
        print(render_text(report, checks))

    if args.gate:
        failed = [c for c in checks if c["status"] == "fail"]
        if failed:
            print(
                f"\nGATE FAILED: {len(failed)} floor(s) violated: "
                + ", ".join(c["name"] for c in failed),
                file=sys.stderr,
            )
            return 1
        print("\ngate: PASS (advisory rows are not enforced)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
