"""Reuse acceptance capture and its opt-in owned process group; no second runner."""
from __future__ import annotations

import os
from pathlib import Path

from acceptance_lib.evidence import capture_command, read_command_run
from answer_eval.protocol import EvaluationError, canonical_sha256, sha256_file
from .recipes import compile_recipe, SUITE


def _owned_path(path, owner):
    """Reject aliases before an executable can follow an output/input ancestor."""
    path, owner = Path(path), Path(owner)
    if not path.is_absolute() or not path.is_relative_to(owner):
        raise EvaluationError("converter I/O leaves marked ownership root")
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise EvaluationError("converter I/O contains a symlink component")
        if component == owner:
            return path
    raise EvaluationError("converter I/O leaves marked ownership root")


class Commands:
    def __init__(self, owner, case, resources, identity, *, timeout, max_output, replay=None):
        self.owner, self.case = Path(owner), Path(case)
        self.resources, self.identity = resources, identity
        self.timeout, self.max_output = timeout, max_output
        self.saved, self.rows = replay, []
        self.env = {"PATH": os.pathsep.join(sorted({str(Path(p).parent) for p in resources.values()} | {"/usr/bin", "/bin"})),
                    "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "TMPDIR": str(self.case / "tmp"), "XDG_CACHE_HOME": str(self.case / "cache"),
                    "XDG_CONFIG_HOME": str(self.case / "config"), "XDG_DATA_HOME": str(self.case / "data"),
                    "GOLDMINE_OCR_AUTOCOMPILE": "0"}
        if replay is None:
            for name in ("tmp", "cache", "config", "data", "inputs"):
                (self.case / name).mkdir()

    def input_file(self, name, raw):
        path = _owned_path(self.case / "inputs" / name, self.owner)
        if path.parent != self.case / "inputs":
            raise EvaluationError("unsafe controlled converter input path")
        if self.saved is None:
            with path.open("xb") as stream:
                stream.write(raw)
        elif not path.is_file() or path.read_bytes() != raw:
            raise EvaluationError("controlled converter stdin differs from exact expected bytes")
        return path

    def call(self, recipe, name, **values):
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
            raise EvaluationError("unsafe fixed command ordinal")
        expected = {"pdf.probe": {"input"}, "pdf.text": {"input", "page"}, "pdf.raster": {"input", "page"},
                    "image.ocr": {"input", "stdin_file"}, "audio.probe": {"input"},
                    "audio.decode": {"input"}, "audio.asr": {"wav"}}
        if recipe not in expected or set(values) != expected[recipe]:
            raise EvaluationError("fixed converter arguments cannot override tools/models/owned output tokens")
        run_dir = self.case / "commands" / name
        bridge = Path(__file__).with_name("_stdin_exec.py")
        local = {**self.resources, "stdin_bridge": str(bridge), "prefix": str(run_dir / "output"),
                 "wav": str(run_dir / "audio.wav"), **{k: str(v) for k, v in values.items()}}
        for key in ("prefix", "wav", "stdin_file", "input"):
            if key in local:
                _owned_path(local[key], self.owner)
        if "page" in local and (not local["page"].isdigit() or int(local["page"]) < 1):
            raise EvaluationError("converter page must be a positive decimal ordinal")
        argv = compile_recipe(recipe, local)
        input_keys = ("wav",) if recipe == "audio.asr" else ("input", "stdin_file")
        identity = {**self.identity, "recipe": recipe, "ordinal": name, "environment": self.env,
                    "inputs": {k: sha256_file(Path(local[k])) for k in input_keys if k in local}}
        check_id = "comparison.convert." + recipe
        if self.saved is None:
            capture_command(run_dir, check_id, argv, cwd=self.case, env=self.env,
                            identity=identity, suite_id=SUITE, timeout=self.timeout, owned_process_group=True)
        manifest, receipt, stdout = read_command_run(run_dir / "manifest.json", check_id=check_id, suite_id=SUITE)
        visible_env = {"GOLDMINE_OCR_AUTOCOMPILE": "0", "PYTHONNOUSERSITE": "1"}
        if (manifest["identity"] != identity or receipt["argv"] != argv or receipt["cwd"] != str(self.case.resolve())
                or receipt["env"] != visible_env):
            raise EvaluationError("converter raw command source/config/argv identity changed")
        generated = {}
        for file in sorted(run_dir.iterdir()):
            if file.name in ("manifest.json", "receipt.json", "stdout.log", "stderr.log"):
                continue
            if file.is_symlink() or not file.is_file() or file.stat().st_size > self.max_output:
                raise EvaluationError("unexpected or oversized converter artifact", status="MISSING")
            generated[file.name] = sha256_file(file)
        row = {"recipe": recipe, "ordinal": name, "command": {"path": (run_dir / "manifest.json").relative_to(self.owner).as_posix(),
                "sha256": sha256_file(run_dir / "manifest.json")}, "generated": generated}
        self.rows.append(row)
        if self.saved is not None and (len(self.rows) > len(self.saved) or self.saved[len(self.rows)-1] != row):
            raise EvaluationError("converter command order/artifacts differ from captured evidence")
        if receipt["exit_code"] != 0 or receipt["timed_out"]:
            raise EvaluationError("converter failed/timed out; raw attempt retained", status="MISSING")
        if stdout.stat().st_size > self.max_output or (run_dir / receipt["stderr"]["path"]).stat().st_size > self.max_output:
            raise EvaluationError("converter raw stream exceeds fixed bound", status="MISSING")
        return stdout.read_text(encoding="utf-8"), run_dir, generated

    def finish(self):
        if self.saved is not None and self.rows != self.saved:
            raise EvaluationError("missing/extra converter attempts in replay")
        return self.rows
