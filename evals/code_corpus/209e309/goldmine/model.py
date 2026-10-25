"""Model manager (05 §4.3, REDESIGN §5 item 5): the `goldmine model` engine.

Owns the pinned-manifest lifecycle: resolve / install (resumable download) /
verify / use / adopt / check. State lives in two places only:

  <home>/models/<name>        installed files (final name = verified content)
  <home>/models.json          registry of adopted/aliased entries (path overrides)

Resolution order for a manifest entry's FILE (task order, 05 §4.3):
  per-key path override in models.json (adopt/install --from)
  > models dir (env GOLDMINE_MODELS / [models] dir config > <home>/models)
  > repo models/ (dev checkout, last).

The embedding DEFAULT used by the llama backend resolves (config.py feeds the
env/config legs): GOLDMINE_MODEL > config llama.model (model_configured) >
legacy default when present > <home>/models/<pinned name> > repo models/
(see backends/llama.py:resolve_model_path — kept in llama.py so the backend
stays importable without the manager).

Download discipline (DigUp ModelDownload.swift): `.partial` + HTTP Range
resume (re-sent across HF CDN redirects by urllib) → size check → streaming
sha256 → atomic rename into the final name ONLY when correct → sidecar write.
Disk-space precheck with +200 MB slack, error names the missing bytes. Progress
is the 4-state copy (03's ModelDownload states: pending / downloading /
verifying / done) printed to stderr as single lines — stdout stays data-only.

`check` is the live arch probe (05 §4.3): spawn llama-server exactly as the
backend does, embed "ping", assert dim > 0, and name the brew-b11146-class
trap explicitly. Tests gate it behind GOLDMINE_LIVE (or use the fake server).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .config import Config, resolve_home
from .model_manifest import (
    DEFAULT_EMBEDDING_KEY,
    MANIFEST,
    ModelFile,
    by_key as manifest_by_key,
    repo_relpath,
)

#: Repo models/ dir (monkeypatch seam for tests / non-checkout installs).
REPO_MODELS = Path(__file__).resolve().parents[1] / "models"

STATE_PENDING = "pending"
STATE_DOWNLOADING = "downloading"
STATE_VERIFYING = "verifying"
STATE_DONE = "done"

#: Retry backoff between download attempts (DigUp: 2/5/15/30 s).
RETRY_BACKOFF_S = (2.0, 5.0, 15.0, 30.0)
#: Download chunk size (streaming sha256 while writing).
CHUNK_BYTES = 1 << 20
#: Disk-space slack added to the transfer size on prechecks (DigUp: 200 MB).
DISK_SLACK_BYTES = 200 * 1024 * 1024

HUB_CACHE = Path("~/.cache/huggingface/hub").expanduser()


class ModelError(RuntimeError):
    """A model-manager command failed (bad key, missing file, bad pin).

    `usage=True` marks caller errors (unknown key) — the CLI maps those to
    exit 2 instead of 1."""

    def __init__(self, message: str, fix_hint: str | None = None,
                 usage: bool = False):
        super().__init__(message)
        self.fix_hint = fix_hint
        self.usage = usage


def _err_sink(message: str) -> None:
    print(message, file=sys.stderr)


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def write_sidecar(model: Path, sha256: str) -> None:
    """The backend-sidecar format (llama.py model_sha256 cache): size+mtime+sha."""
    st = model.stat()
    try:
        _atomic_write(
            model.with_name(model.name + ".sha256"),
            json.dumps({"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha256}),
        )
    except OSError:
        pass  # sidecar is an optimization, never a requirement


def sha256_and_size(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK_BYTES)
            if not block:
                break
            size += len(block)
            h.update(block)
    return h.hexdigest(), size


class ModelManager:
    def __init__(
        self,
        cfg: Config,
        manifest: list[ModelFile] | None = None,
        repo_models: Path | None = None,
        err: Callable[[str], None] = _err_sink,
        sleep: Callable[[float], None] = time.sleep,
        open_url: Callable[..., object] = urllib.request.urlopen,
    ):
        self.cfg = cfg
        self.home = resolve_home(cfg)
        self.manifest = manifest if manifest is not None else MANIFEST
        self.repo_models = repo_models if repo_models is not None else REPO_MODELS
        self.err = err
        self._sleep = sleep
        self._open_url = open_url

    # ------------------------------------------------------------- manifest

    def entry(self, key: str) -> ModelFile:
        for entry in self.manifest:
            if entry.key == key:
                return entry
        known = ", ".join(e.key for e in self.manifest)
        raise ModelError(
            f"unknown model {key!r} (known: {known})",
            "goldmine model list shows every pinned model",
            usage=True,
        )

    # ------------------------------------------------------------- locations

    def models_dir(self) -> Path:
        """env GOLDMINE_MODELS / [models] dir > <home>/models."""
        override = getattr(self.cfg.models, "dir", None) if self.cfg.models else None
        if override:
            return Path(override).expanduser()
        return self.home / "models"

    def state_path(self) -> Path:
        return self.home / "models.json"

    def load_state(self) -> dict:
        try:
            state = json.loads(self.state_path().read_text())
        except (OSError, ValueError):
            return {"version": 1, "models": {}}
        if not isinstance(state, dict) or not isinstance(state.get("models"), dict):
            return {"version": 1, "models": {}}
        return state

    def save_state(self, state: dict) -> None:
        _atomic_write(self.state_path(), json.dumps(state, indent=2, ensure_ascii=False))

    def install_dest(self, entry: ModelFile) -> Path:
        """Installed layout is flat under the models dir (05 §4.3)."""
        return self.models_dir() / entry.name

    # ------------------------------------------------------------ resolution

    def resolve(self, key: str) -> tuple[Path | None, str]:
        """(path, source) with source in {"state", "models-dir", "repo", "missing"}.
        Per-key path overrides (adopt / install --from) win over the models dir,
        which wins over the repo dev copy."""
        state = self.load_state()
        record = state["models"].get(key)
        if record and record.get("path"):
            path = Path(record["path"]).expanduser()
            if path.is_file():
                return path, "state"
        entry = self.entry(key)
        candidate = self.install_dest(entry)
        if candidate.is_file():
            return candidate, "models-dir"
        repo_copy = self.repo_models / repo_relpath(entry)
        if repo_copy.is_file():
            return repo_copy, "repo"
        return None, "missing"

    def listing(self) -> list[dict]:
        rows = []
        for entry in self.manifest:
            path, source = self.resolve(entry.key)
            rows.append({
                "key": entry.key,
                "role": entry.role,
                "repo": entry.repo,
                "revision": entry.revision,
                "name": entry.name,
                "bytes": entry.bytes,
                "sha256": entry.sha256,
                "dim": entry.dim,
                "installed": path is not None,
                "path": str(path) if path else None,
                "source": source if path else None,
            })
        return rows

    # ---------------------------------------------------------------- verify

    def verify(self, key: str | None = None) -> list[dict]:
        """Recompute bytes+sha256 of resolved entries against the manifest pin.
        key=None verifies every resolvable entry. B7 discipline: bytes AND sha
        are both checked — a bytes-only match with a wrong sha is a FAIL."""
        keys = [key] if key else [e.key for e in self.manifest]
        results = []
        for k in keys:
            entry = self.entry(k)
            path, source = self.resolve(k)
            if path is None:
                results.append({
                    "key": k, "path": None, "source": source,
                    "bytes_ok": None, "sha_ok": None,
                    "error": "not installed",
                })
                continue
            sha, size = sha256_and_size(path)
            results.append({
                "key": k, "path": str(path), "source": source,
                "bytes_ok": size == entry.bytes,
                "sha_ok": sha == entry.sha256,
            })
        return results

    # --------------------------------------------------------------- install

    def install(self, key: str, from_path: Path | None = None,
                alias: str | None = None) -> Path:
        """Bring a manifest entry to a resolvable state. Downloads into the
        models dir (resumable, verified, atomic rename); `--from PATH`
        registers an existing file by absolute path WITHOUT copying."""
        entry = self.entry(key)
        if from_path is not None:
            return self._register(key, from_path, alias)
        dest = self.install_dest(entry)
        if dest.is_file():
            sha, size = sha256_and_size(dest)
            if size == entry.bytes and sha == entry.sha256:
                self.err(f"[{STATE_DONE}] {key}: already installed at {dest}")
                self._record(key, str(dest), source="download", alias=alias)
                return dest
            raise ModelError(
                f"{dest} exists but does not match the pinned {key} "
                f"(bytes {size} != {entry.bytes} or sha mismatch)",
                f"move the file away and re-run goldmine model install {key}",
            )
        self._disk_precheck(entry)
        cached = self._hub_cache_copy(entry)
        partial = dest.with_name(dest.name + ".partial")
        if cached is not None:
            self._copy_verified(cached, partial, entry)
        else:
            self._download(entry, dest, partial)
        self.err(f"[{STATE_VERIFYING}] {key}: size+sha256 against the pin")
        sha, size = sha256_and_size(partial)
        if size != entry.bytes or sha != entry.sha256:
            detail = (
                f"downloaded {size} bytes (expected {entry.bytes})"
                if size != entry.bytes
                else f"sha256 {sha} != pinned {entry.sha256}"
            )
            partial.unlink(missing_ok=True)
            raise ModelError(
                f"{key} failed verification: {detail}",
                "the pin may be dead or the CDN corrupted the transfer — "
                "retry; if it persists, bump model_manifest.py",
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, dest)  # final name only after verify (DigUp rule)
        write_sidecar(dest, entry.sha256)
        self._record(key, str(dest), source="download", alias=alias)
        self.err(f"[{STATE_DONE}] {key}: {dest} ({entry.bytes:,} bytes, pin verified)")
        return dest

    def _disk_precheck(self, entry: ModelFile) -> None:
        dest_dir = self.models_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        needed = entry.bytes + DISK_SLACK_BYTES
        try:
            free = shutil.disk_usage(dest_dir).free
        except OSError as exc:
            raise ModelError(f"cannot stat {dest_dir} for the disk precheck: {exc}")
        if free < needed:
            missing = needed - free
            raise ModelError(
                f"not enough disk space for {entry.key}: need {entry.bytes:,} bytes "
                f"+ {DISK_SLACK_BYTES // (1 << 20)} MB slack, only {free:,} free on "
                f"{dest_dir} (missing {missing:,} bytes)",
                "free space or point [models] dir / GOLDMINE_MODELS elsewhere",
            )

    def _hub_cache_copy(self, entry: ModelFile) -> Path | None:
        """HF-hub shortcut (DigUp ModelFiles.swift:41-54): when the pinned
        snapshot is already in ~/.cache/huggingface/hub, copy + verify instead
        of downloading — ~30 lines, and this machine caches models there."""
        org, _, name = entry.repo.partition("/")
        snapshot = (
            HUB_CACHE / f"models--{org}--{name}" / "snapshots" / entry.revision / entry.name
        )
        return snapshot if snapshot.is_file() else None

    def _copy_verified(self, src: Path, partial: Path, entry: ModelFile) -> None:
        self.err(f"[{STATE_DOWNLOADING}] {entry.key}: copying pinned HF-hub cache "
                 f"snapshot {src}")
        partial.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        h = hashlib.sha256()
        with open(src, "rb") as fin, open(partial, "wb") as fout:
            while True:
                block = fin.read(CHUNK_BYTES)
                if not block:
                    break
                fout.write(block)
                h.update(block)
                size += len(block)
                self._progress(STATE_DOWNLOADING, entry, size)
        if size != entry.bytes or h.hexdigest() != entry.sha256:
            partial.unlink(missing_ok=True)
            raise ModelError(
                f"HF-hub cache copy of {entry.key} does not match the pin "
                f"(bytes {size} vs {entry.bytes}) — pin or cache is stale",
            )

    def _progress(self, state: str, entry: ModelFile, done: int,
                  started: float | None = None) -> None:
        now = time.monotonic()
        started = started if started is not None else now
        frac = done / entry.bytes if entry.bytes else 0.0
        elapsed = max(1e-6, now - started)
        rate = done / elapsed
        eta = (entry.bytes - done) / rate if rate > 1 else float("inf")
        self.err(
            f"[{state}] {entry.key}: {frac * 100:5.1f}% · {rate / (1 << 20):.1f} MB/s · "
            f"eta {eta:.0f}s"
        )

    def _download(self, entry: ModelFile, dest: Path, partial: Path) -> None:
        """Resumable download: Range: bytes=<n>- from the .partial size; a
        server that answers 200 (ignoring Range) restarts from zero."""
        url = entry.url()
        self.err(f"[{STATE_PENDING}] {entry.key}: {url} -> {dest}")
        attempt = 0
        while True:
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                resp = self._open_url(
                    urllib.request.Request(url, headers=headers), timeout=60.0
                )
                with resp:
                    status = getattr(resp, "status", 200)
                    if offset and status != 206:
                        # server ignored the Range (or fresh 200 after a
                        # redirect): restart the transfer from zero
                        offset = 0
                    total = entry.bytes
                    mode = "ab" if offset else "wb"
                    size = offset
                    started = time.monotonic()
                    next_line = started
                    partial.parent.mkdir(parents=True, exist_ok=True)
                    with open(partial, mode) as out:
                        while True:
                            block = resp.read(CHUNK_BYTES)
                            if not block:
                                break
                            out.write(block)
                            size += len(block)
                            now = time.monotonic()
                            if now - next_line >= 2.0:  # one line every ~2 s
                                self._progress(STATE_DOWNLOADING, entry, size, started)
                                next_line = now
                    if size != total and size < total:
                        raise urllib.error.URLError(
                            f"short read: {size} of {total} bytes"
                        )
                if attempt:
                    self.err(f"[{STATE_DOWNLOADING}] {entry.key}: resumed/complete "
                             f"after retry {attempt}")
                return
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    # A dead pin (or a removed revision) will never answer:
                    # burning the 2/5/15/30 s backoff on a 404 measured 52 s of
                    # pointless retries (storage critique 03 F9). Fail now,
                    # naming the manifest entry and the exact URL.
                    raise ModelError(
                        f"{entry.key} ({entry.name}) is not available at "
                        f"{url} — HTTP {exc.code}: the manifest pin is dead "
                        f"or the file was removed from {entry.repo}",
                        "check goldmine model list / bump model_manifest.py "
                        "to a live revision — no retry is attempted for 404/410",
                    ) from exc
                # 429/5xx (and other 4xx): transient — fall through to backoff
                if attempt < len(RETRY_BACKOFF_S):
                    wait = RETRY_BACKOFF_S[attempt]
                    attempt += 1
                    self.err(
                        f"[{STATE_DOWNLOADING}] {entry.key}: transfer failed "
                        f"(HTTP {exc.code}: {exc.reason}) — retry {attempt}/"
                        f"{len(RETRY_BACKOFF_S)} in {wait:g}s "
                        f"(resuming from the .partial)"
                    )
                    self._sleep(wait)
                    continue
                raise ModelError(
                    f"download of {entry.key} failed after {attempt} attempts: "
                    f"HTTP {exc.code}: {exc.reason}",
                    "check the network and re-run — the .partial file resumes",
                ) from exc
            except (urllib.error.URLError, OSError) as exc:
                if attempt < len(RETRY_BACKOFF_S):
                    wait = RETRY_BACKOFF_S[attempt]
                    attempt += 1
                    self.err(
                        f"[{STATE_DOWNLOADING}] {entry.key}: transfer failed "
                        f"({exc}) — retry {attempt}/{len(RETRY_BACKOFF_S)} in {wait:g}s "
                        f"(resuming from the .partial)"
                    )
                    self._sleep(wait)
                    continue
                raise ModelError(
                    f"download of {entry.key} failed after {attempt} attempts: {exc}",
                    "check the network and re-run — the .partial file resumes",
                ) from exc

    # ----------------------------------------------------------------- adopt

    def adopt(self, path: Path, key: str, alias: str | None = None) -> Path:
        """Register an existing local file under a manifest key after sha256
        (no copy). The pin must match when the key is a manifest entry."""
        return self._register(key, path, alias)

    def _register(self, key: str, path: Path, alias: str | None) -> Path:
        entry = self.entry(key)
        file = Path(path).expanduser()
        if not file.is_file():
            raise ModelError(f"no such file: {file}")
        sha, size = sha256_and_size(file)
        if size != entry.bytes or sha != entry.sha256:
            raise ModelError(
                f"{file} does not match the pinned {key} "
                f"(bytes {size} vs {entry.bytes}, sha256 {sha[:16]}… vs "
                f"{entry.sha256[:16]}…)",
                "adopt registers only pinned content; check the file or the pin",
            )
        path_abs = self._record(key, str(file.resolve()), source="adopt", alias=alias)
        self.err(
            f"[{STATE_DONE}] {key}: registered {file} by absolute path "
            f"(no copy; pin verified)"
        )
        return Path(path_abs)

    def _record(self, key: str, path: str, source: str,
                alias: str | None = None) -> str:
        state = self.load_state()
        record = state["models"].get(key, {})
        record.update({
            "path": path,
            "source": source,
            "registered_at": time.time(),
        })
        if alias:
            record["alias"] = alias
        state["models"][key] = record
        self.save_state(state)
        return path

    # ------------------------------------------------------------------- use

    def use(self, key: str, config_path: Path | None = None) -> Path:
        """Set the default embedding model in config.toml ([llama] model = path).
        Refuses to write anything it cannot resolve (05 §4.3: `use` writes
        nothing it cannot verify). Embedding-role models only."""
        entry = self.entry(key)
        if entry.role != "embedding":
            raise ModelError(
                f"{key} is role {entry.role!r}; `model use` binds an embedding model",
                f"use {DEFAULT_EMBEDDING_KEY} (or another role=embedding entry)",
            )
        path, source = self.resolve(key)
        if path is None:
            raise ModelError(
                f"model {key} is not installed anywhere (models dir "
                f"{self.models_dir()}, repo {self.repo_models})",
                f"goldmine model install {key} first",
            )
        from .config import default_config_path

        cfg_path = Path(config_path) if config_path else (
            Path(os.environ.get("GOLDMINE_CONFIG"))
            if os.environ.get("GOLDMINE_CONFIG")
            else default_config_path()
        )
        set_config_llama_model(cfg_path, str(path))
        self.err(f"[{STATE_DONE}] default embedding model -> {key} ({path}, {source})")
        return path

    # ----------------------------------------------------------------- check

    def check(self) -> dict:
        """Live arch probe (05 §4.3): resolve server+model, spawn llama-server
        exactly as the backend does, embed "ping", assert dim > 0. Catches the
        brew-b11146 class (build passes --version but cannot load the arch)."""
        from .backends.llama import (
            BREW_CANNOT_LOAD_NOTE,
            LlamaServerBackend,
            _is_brew_path,
            probe_version,
            resolve_model_path,
            resolve_server_path,
        )

        server, source = resolve_server_path(self.cfg.llama)
        findings: dict = {
            "server": str(server) if server else None,
            "server_source": source,
            "build_string": probe_version(server) if server else None,
            "brew_trap": None,
        }
        if server is None:
            findings["live_embed"] = (
                "skipped: no llama-server binary — looked at env/config, "
                f"{self.home / 'bin' / 'llama-server'}, repo vendor, brew"
            )
            findings["ok"] = False
            return findings
        if source == "brew" or _is_brew_path(server):
            findings["brew_trap"] = (
                "this build cannot load gemma-embedding2 — use the release asset "
                "(<home>/bin/llama-server) or the vendor build; " + BREW_CANNOT_LOAD_NOTE
            )
        model = resolve_model_path(self.cfg.llama)
        entry = manifest_by_key(DEFAULT_EMBEDDING_KEY)
        findings["model"] = str(model)
        findings["model_key"] = DEFAULT_EMBEDDING_KEY
        findings["model_sha_ok"] = None
        if model.is_file() and entry is not None and model.name == entry.name:
            sha, size = sha256_and_size(model)
            findings["model_sha_ok"] = sha == entry.sha256 and size == entry.bytes

        backend = LlamaServerBackend(self.cfg.llama)
        try:
            backend.startup()
            vectors = backend.embed_texts(["ping"])
            dim = int(vectors.shape[1])
            findings["live_embed"] = f"ok (embedded 'ping' -> dim {dim})"
            findings["dim"] = dim
            findings["ok"] = dim > 0
        except Exception as exc:  # startup/embed failure: the arch trap lands here
            tail = (getattr(exc, "tail", "") or "").strip()
            findings["live_embed"] = f"failed: {exc}" + (f"\n{tail}" if tail else "")
            findings["ok"] = False
            if findings["brew_trap"] is None and "arch" in str(exc).lower():
                findings["brew_trap"] = (
                    "this build cannot load gemma-embedding2 — use the release "
                    "asset/vendor build"
                )
        finally:
            backend.close()
        return findings


def set_config_llama_model(config_path: Path, value: str) -> None:
    """Write `model = "<value>"` into the [llama] section of config.toml,
    preserving every other line. Appends the section when absent. A one-line
    .bak copy is written next to the file before the first modification (the
    house rule for editing user config). Stdlib tomllib cannot dump, so this
    is a small line-oriented editor for exactly this one key."""
    quoted = json.dumps(str(value))  # JSON basic string == TOML basic string
    text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    lines = text.splitlines()
    out: list[str] = []
    section = None
    replaced = False
    llama_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            if section == "[llama]" and llama_start is None:
                llama_start = len(out)
        if (
            section == "[llama]"
            and not replaced
            and stripped.split("#")[0].strip().startswith("model")
            and "=" in stripped
        ):
            key = stripped.split("#")[0].split("=", 1)[0].strip()
            if key == "model":
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f"{indent}model = {quoted}")
                replaced = True
                continue
        out.append(line)
    if not replaced and llama_start is not None:
        out.insert(llama_start + 1, f"model = {quoted}")
        replaced = True
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.extend(["[llama]", f"model = {quoted}"])
    new_text = "\n".join(out) + "\n"
    if text != new_text:
        if config_path.is_file():
            shutil.copy2(config_path, config_path.with_name(config_path.name + ".bak"))
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(config_path, new_text)


def render_listing(rows: list[dict], json_mode: bool) -> str:
    if json_mode:
        # F11: the standard envelope (init/open style)
        return json.dumps(
            {"schema": "goldmine/1", "command": "model", "verb": "list",
             "models": rows},
            indent=2, ensure_ascii=False,
        )
    lines = []
    for row in rows:
        if row["installed"]:
            location = f"{row['source']}: {row['path']}"
        else:
            location = f"not installed ({row['name']}, {row['bytes']:,} bytes)"
        lines.append(
            f"{row['key']:<28} {row['role']:<9} "
            f"{'installed' if row['installed'] else 'missing':<9} {location}"
        )
    return "\n".join(lines)


def render_verify(results: list[dict], json_mode: bool) -> str:
    if json_mode:
        return json.dumps({"verify": results}, indent=2, ensure_ascii=False)
    lines = []
    for r in results:
        if r.get("error"):
            lines.append(f"{r['key']:<28} MISSING ({r['error']})")
            continue
        bytes_ok = "bytes ok" if r["bytes_ok"] else "BYTES MISMATCH"
        sha_ok = "sha ok" if r["sha_ok"] else "SHA MISMATCH"
        mark = "ok  " if (r["bytes_ok"] and r["sha_ok"]) else "FAIL"
        lines.append(f"{mark} {r['key']:<28} {bytes_ok}, {sha_ok} ({r['path']})")
    return "\n".join(lines)


def render_check(findings: dict, json_mode: bool) -> str:
    if json_mode:
        return json.dumps({"check": findings}, indent=2, ensure_ascii=False)
    lines = [
        f"server: {findings.get('server')} "
        f"(source: {findings.get('server_source')})",
        f"build: {findings.get('build_string')}",
        f"model: {findings.get('model')} "
        f"(key: {findings.get('model_key')}, sha_ok: {findings.get('model_sha_ok')})",
        f"live embed: {findings.get('live_embed')}",
    ]
    if findings.get("brew_trap"):
        lines.append(f"WARNING: {findings['brew_trap']}")
    lines.append("result: " + ("OK" if findings.get("ok") else "FAILED"))
    return "\n".join(lines)
