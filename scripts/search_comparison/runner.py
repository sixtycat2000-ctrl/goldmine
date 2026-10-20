"""Explicit diagnostic lexical comparison over one frozen text-common view."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys

from answer_eval.protocol import EvaluationError, canonical_sha256, sha256_file
from .adapters import parse_goldmine, parse_qmd, parse_recoll
from .manifest import load_comparison, repo_path
from .metrics import score_files, summarize
from .process import run_process


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _freeze_goldmine(root, destination):
    files = [*sorted((root / "goldmine").rglob("*.py")), root / "pyproject.toml"]
    identities = {}
    for source in files:
        relative = source.relative_to(root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_bytes()
        identities[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
        target.write_bytes(raw)
    if any(sha256_file(root / name) != sha for name, sha in identities.items()):
        raise EvaluationError("goldmine source changed while freezing; start a new comparison")
    return {"source_sha256": canonical_sha256(identities), "files": identities,
            "python": platform.python_version(), "python_executable": sys.executable}


def _environment(root, output, manifest):
    base = {key: value for key, value in os.environ.items() if not key.startswith(("GOLDMINE_", "RECOLL_", "QMD_", "XDG_"))}
    result = {}
    for name in manifest["systems"]:
        isolated = output / "environment" / name
        isolated.mkdir(parents=True)
        env = {**base, "XDG_CONFIG_HOME": str(isolated / "config"),
               "XDG_CACHE_HOME": str(isolated / "cache"), "XDG_DATA_HOME": str(isolated / "data")}
        if name == "qmd":
            for key, value in manifest["systems"][name]["environment"].items():
                env[key] = str(repo_path(root, value))
        elif name == "recoll":
            system = manifest["systems"][name]
            env.update(RECOLL_CONFDIR=str(repo_path(root, system["config_dir"])),
                       RECOLL_DATADIR=str(repo_path(root, system["data_dir"])))
        else:
            env.update(GOLDMINE_HOME=str(isolated / "home"), PYTHONPATH=str(output / "goldmine-source"))
        result[name] = env
    return result


def run_comparison(manifest_path, *, root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists():
        raise EvaluationError("comparison output must be a new directory")
    manifest, bundle, mapping = load_comparison(manifest_path, root)
    output.mkdir(parents=True)
    work = output / "work"
    work.mkdir()
    source = _freeze_goldmine(root, output / "goldmine-source")
    envs = _environment(root, output, manifest)
    host = {"uname": platform.uname()._asdict(), "python": platform.python_version(), "cpu_count": os.cpu_count()}
    corpus = repo_path(root, manifest["view"]["corpus_root"])
    db = output / "goldmine-lexical.sqlite"
    config = output / "goldmine.toml"
    config.write_text("[core]\nhome = " + json.dumps(str(output / "environment/goldmine/home")) + '\n[backend]\ndefault = "mock"\n', encoding="utf-8")
    goldmine_common = ["--db", str(db), "--config", str(config), "--backend", "mock", "--json"]
    prepare = run_process([sys.executable, "-m", "goldmine", "ingest", *goldmine_common,
                           "--lexical-only", str(corpus)], cwd=work, env=envs["goldmine"], raw_prefix=output / "raw/goldmine-prepare")
    _write(output / "goldmine-prepare.json", prepare)
    goldmine_available = prepare.get("exit_code") == 0
    selected = set(bundle.splits["dev"]) if bundle.splits else {question["id"] for question in bundle.queries}
    questions = [question for question in bundle.queries if question["id"] in selected]
    runs = {name: [] for name in manifest["systems"]}
    qmd, recoll = manifest["systems"]["qmd"], manifest["systems"]["recoll"]
    parsers = {"goldmine": parse_goldmine, "qmd": parse_qmd, "recoll": parse_recoll}
    for question in questions:
        # Query IDs never become paths; generated ordinals select raw filenames.
        number = len(runs["goldmine"]) + 1
        argv = {
            "goldmine": [sys.executable, "-m", "goldmine", "search", *goldmine_common,
                         "--lexical", "--scorer", "lexical", "--mode", "fast", "--rerank", "off", "--no-serve", "--limit", "10", "--", question["q"]],
            "qmd": [str(repo_path(root, qmd["binary"])), "--index", qmd["index"], "search", "--format", "json", "-n", "10", "-c", qmd["collection"], "--", question["q"]],
            "recoll": [str(repo_path(root, recoll["binary"])), "-c", str(repo_path(root, recoll["config_dir"])), "-F", "url", "-n", "10", "--", question["q"]],
        }
        for name in runs:
            row = {"id": question["id"], "q": question["q"], "primary_class": question["primary_class"],
                   "source_group": question["source_group"], "status": "MISSING", "metrics": {}, "hits": []}
            prefix = output / "raw" / f"{name}-{number:03d}"
            if name == "goldmine" and not goldmine_available:
                row["error"] = "isolated lexical index preparation failed; raw preparation retained"
            else:
                process = run_process(argv[name], cwd=work, env=envs[name], raw_prefix=prefix)
                row["process"] = process
                if process.get("exit_code") != 0 or process["timed_out"]:
                    row["error"] = "query process failed or timed out; raw stdout/stderr retained"
                else:
                    try:
                        parsed = parsers[name](prefix.with_suffix(".stdout").read_text(encoding="utf-8"), mapping)
                        row.update(parsed)
                        row["metrics"] = score_files(question, [hit["path"] for hit in row["hits"]])
                        row["status"] = "OK"
                    except (EvaluationError, UnicodeError, ValueError, OSError) as error:
                        row["error"] = "parser/mapping failure: " + str(error)
            runs[name].append(row)
    if any({row["id"] for row in rows} != selected for rows in runs.values()):
        raise EvaluationError("systems did not retain identical question denominators")
    raw = {path.relative_to(output).as_posix(): sha256_file(path) for path in sorted((output / "raw").iterdir())}
    complete = all(all(row["status"] == "OK" for row in rows) for rows in runs.values())
    report = {"schema": 1, "status": "DIAGNOSTIC" if complete else "MISSING", "diagnostic_only": True, "quality_evidence": False,
              "view": "text-common", "split": "dev" if bundle.splits else "draft_dev_seed",
              "manifest_sha256": sha256_file(Path(manifest_path)), "bundle_identity": bundle.identity,
              "goldmine_source": source, "configuration": manifest,
              "host": host, "isolation": {"cwd": str(work), "environments": {name: {key: value for key, value in env.items()
                      if key.startswith(("XDG_", "RECOLL_", "GOLDMINE_")) or key == "PYTHONPATH"} for name, env in envs.items()}},
              "n_queries": len(questions), "questions": questions, "fixed_full_suite_target": 420,
              "position_metrics": "NOT_COMPARABLE: no common verified locations in these CLI outputs",
              "performance": "NOT_MEASURED: per-command wall times are raw execution diagnostics only",
              "missing": bundle.missing + manifest["missing"], "raw_sha256": raw,
              "systems": {name: {"mode": "lexical", "rows": rows, "summary": summarize(rows)} for name, rows in runs.items()}}
    _write(output / "report.json", report)
    recompute_report(report, raw_root=output, mapping=mapping)
    return report


def recompute_report(report, *, raw_root=None, mapping=None):
    """Tables derive from retained per-query mapped hits, not hand-edited sums."""
    configuration = report["configuration"]
    if raw_root is not None:
        raw_root = Path(raw_root).resolve()
        for name, digest in report["raw_sha256"].items():
            path = (raw_root / name).resolve()
            if not path.is_relative_to(raw_root / "raw") or sha256_file(path) != digest:
                raise EvaluationError("comparison raw output path/checksum mismatch")
        if mapping is None:
            raise EvaluationError("raw comparison recomputation requires the frozen URI mapping")
    questions = {question["id"]: question for question in report["questions"]}
    if len(questions) != report["n_queries"]:
        raise EvaluationError("comparison question labels are incomplete/duplicated")
    expected = None
    for name, system in report["systems"].items():
        rows = system["rows"]
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)) or len(ids) != report["n_queries"] or (expected is not None and set(ids) != expected):
            raise EvaluationError("comparison query denominator changed")
        expected = set(ids)
        for row in rows:
            question = questions.get(row["id"])
            if question is None or any(row[key] != question[key] for key in ("q", "primary_class", "source_group")):
                raise EvaluationError("comparison row differs from saved frozen question labels")
            if row["status"] == "OK":
                if raw_root is not None:
                    filename = row["process"]["stdout"]
                    path = raw_root / "raw" / filename
                    if Path(filename).name != filename or "raw/" + filename not in report["raw_sha256"]:
                        raise EvaluationError("row stdout is outside the frozen raw output set")
                    parser = {"goldmine": parse_goldmine, "qmd": parse_qmd, "recoll": parse_recoll}[name]
                    actual = parser(path.read_text(encoding="utf-8"), mapping)
                    if any(row.get(key) != value for key, value in actual.items()):
                        raise EvaluationError("comparison mapped ranks differ from raw CLI output")
                if row["metrics"] != score_files(question, [hit["path"] for hit in row["hits"]]):
                    raise EvaluationError("comparison metrics differ from mapped file ranks")
            elif row["status"] != "MISSING" or row["hits"] or row["metrics"]:
                raise EvaluationError("failed comparison rows must retain their empty/error status")
        if system["summary"] != summarize(rows):
            raise EvaluationError("comparison summary differs from retained per-query rows")
    if not configuration["diagnostic_only"] or report["quality_evidence"]:
        raise EvaluationError("diagnostic comparison cannot be relabeled as quality evidence")
    return {name: system["summary"] for name, system in report["systems"].items()}
