"""Configuration: defaults, config.toml (stdlib tomllib), env resolution.

Precedence (SPEC §1/§10): CLI flag > environment variable > config.toml > default.
`load()` applies env > file > default; the CLI applies flags on top of the loaded
Config dataclass. Unknown config keys WARN (never fail).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("goldmine.config")


@dataclass
class LlamaCfg:
    server: Path = Path("/opt/homebrew/opt/llama.cpp/bin/llama-server")
    model: Path = Path("~/git/goldmine/models/embeddinggemma-2-Q8_0.gguf")
    ctx: int = 2048
    batch: int = 16
    startup_timeout: float = 120.0
    request_timeout: float = 60.0
    # True when llama.server came from env/config (not the built-in default):
    # an explicitly configured binary is honored as-is (even the brew path —
    # doctor will then report the brew build's gemma-embedding2 limitation),
    # while the default falls through to home > vendor > brew at resolution time.
    server_configured: bool = False
    # True when llama.model came from env/config: an explicit model wins even
    # when missing; the built-in default falls through to <home>/models and the
    # repo models/ pin (backends/llama.py:resolve_model_path).
    model_configured: bool = False
    # Propagated goldmine home (set by load from Config.home; None for
    # standalone LlamaCfg instances — llama.py falls back to GOLDMINE_HOME env
    # then the platform default). Used for the <home>/bin/llama-server and
    # <home>/models resolution legs.
    home: Path | None = None


@dataclass
class ModelsCfg:
    # Optional models-dir override (env GOLDMINE_MODELS / [models] dir);
    # None = <home>/models (the model-manager canonical location).
    dir: Path | None = None


@dataclass
class ForesightCfg:
    socket: str = ""
    expect_version: str = ""


@dataclass
class IngestCfg:
    max_file_mb: int = 200
    max_pages: int = 5000
    max_chunks: int = 5000
    audio_max_minutes: float = 120.0
    workers: int = 2
    politeness: float = 1.0
    # v2 OCR policy (02 §5.2, A5/Q21): under a text-only backend (llama) a
    # screenshot has NO image vector, so the OCR text gets a text vector
    # instead of being FTS-only (DigUp's no-vector evidence assumed an image
    # vector existed). Flip to false for FTS-only everywhere.
    ocr_text_vector: bool = True
    # v2 ASR (item 6, 02 §5.1): auto = transcribe when whisper-cli + models
    # are present, else the v1 raw-window/metadata fallback; always = ASR
    # only (failures are honest failed/metadata rows); off = v1 path only.
    asr: str = "auto"
    asr_max_minutes: float = 120.0   # source-audio cap; over -> partial rows
    asr_language: str = "auto"       # -l value; 'auto' = detect per file
    asr_whisper: str = ""            # optional whisper-cli path override


@dataclass
class OcrCfg:
    """[ocr] — Vision OCR master switch (02 §5.8). Auto-off when the
    goldmine-ocr helper is absent; doctor reports the state."""
    enabled: bool = True


@dataclass
class SearchCfg:
    limit: int = 20
    z_gate: float = 3.0  # single qualification rule (v2.1 A/B; see search.py)
    scorer: str = "hybrid"


@dataclass
class InitCfg:
    """[init] — the scope-invariant record (v2 item 20): the roots the user
    explicitly chose during `goldmine init`. Written by init's folder step;
    NEVER populated by any default sweep — an empty list means "user has not
    chosen folders yet"."""

    roots: list[str] = field(default_factory=list)


@dataclass
class DistillCfg:
    # v2.1 R6 (D13): None = CALIBRATED — every run measures τ from the corpus
    # (distill.measure_tau). A value here (or --threshold X) acts as a manual
    # override (tau_source='manual'); the v1 default 0.70 was the bug.
    threshold: float | None = None
    min_members: int = 3
    max_chunks: int = 50000


@dataclass
class ServeCfg:
    """[serve] — the warm-search daemon (R8, D7 as amended by review F9).

    There is deliberately NO `auto` key: auto-start was CUT from this build
    (an untested dead path must not ship); running the daemon is the explicit
    opt-in `goldmine serve`. `socket` doubles as the GOLDMINE_SERVE env target
    (env wins over the file per the standard precedence)."""

    linger: int = 300       # idle seconds before the daemon exits (F7: only model-touching ops refresh)
    timeout_s: float = 10.0  # client per-request deadline (D2)
    socket: str = ""        # explicit socket path (env GOLDMINE_SERVE / [serve] socket)


@dataclass
class WatchCfg:
    """[watch] — the folder auto-follow daemon (R9, docs/v2.1-r9-watch.md §6.2
    as amended by review R-F1). Interval overrides only — the daemon's holds,
    scope and lifecycle are flags/runtime, never config."""

    interval_fast: int = 5        # detection floor after a change or at start
    interval_slow: int = 120      # quiet-corpus cadence (decay x2, capped here)
    settle_s: float = 2.0         # debounce before an ingest pass
    linger: int = 300             # quiet seconds before exit 0 (--forever disables)
    battery_max_batch: int = 50   # max changed files per delta pass on battery
    full_period_s: int = 21600    # idle self-heal sweep (deletions + same-mtime)
    min_pass_gap_s: float = 25.0  # R-F1a: floor between embed-bearing passes
    battery_pass_gap_s: float = 120.0  # R-F1c: minimum embed-pass gap on battery


@dataclass
class Config:
    home: Path = Path("~/Library/Application Support/goldmine")
    backend: str = "llama"
    llama: LlamaCfg = field(default_factory=LlamaCfg)
    foresight: ForesightCfg = field(default_factory=ForesightCfg)
    models: ModelsCfg = field(default_factory=ModelsCfg)
    ingest: IngestCfg = field(default_factory=IngestCfg)
    ocr: OcrCfg = field(default_factory=OcrCfg)
    search: SearchCfg = field(default_factory=SearchCfg)
    distill: DistillCfg = field(default_factory=DistillCfg)
    serve: ServeCfg = field(default_factory=ServeCfg)
    watch: WatchCfg = field(default_factory=WatchCfg)
    init: InitCfg = field(default_factory=InitCfg)


def default_config_path() -> Path:
    """XDG-style portable config location (SPEC §1)."""
    return Path("~/.config/goldmine/config.toml").expanduser()


def resolve_home(cfg: Config) -> Path:
    """Data directory: expanduser + mkdir -p. Idempotent."""
    home = Path(cfg.home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return home


def index_path(cfg: Config, name: str = "default") -> Path:
    return resolve_home(cfg) / f"{name}.sqlite"


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _apply_section(cfg: Config, section: str, data: dict[str, Any]) -> None:
    """Fold one parsed [section] table into cfg. Unknown keys WARN."""
    if section == "core":
        if "home" in data:
            cfg.home = Path(str(data["home"]))
        unknown(section, data, {"home"})
    elif section == "backend":
        if "default" in data:
            cfg.backend = str(data["default"])
        unknown(section, data, {"default"})
    elif section == "llama":
        if "server" in data:
            cfg.llama.server = Path(str(data["server"]))
            cfg.llama.server_configured = True
        if "model" in data:
            cfg.llama.model = Path(str(data["model"]))
            cfg.llama.model_configured = True
        if "ctx" in data and (v := _coerce_int(data["ctx"])) is not None:
            cfg.llama.ctx = v
        if "batch" in data and (v := _coerce_int(data["batch"])) is not None:
            cfg.llama.batch = v
        if "startup_timeout" in data and (v := _coerce_float(data["startup_timeout"])) is not None:
            cfg.llama.startup_timeout = v
        if "request_timeout" in data and (v := _coerce_float(data["request_timeout"])) is not None:
            cfg.llama.request_timeout = v
        unknown(section, data, {"server", "model", "ctx", "batch", "startup_timeout", "request_timeout"})
    elif section == "models":
        if "dir" in data:
            cfg.models.dir = Path(str(data["dir"]))
        unknown(section, data, {"dir"})
    elif section == "foresight":
        if "socket" in data:
            cfg.foresight.socket = str(data["socket"])
        if "expect_version" in data:
            cfg.foresight.expect_version = str(data["expect_version"])
        unknown(section, data, {"socket", "expect_version"})
    elif section == "ingest":
        for key in ("max_file_mb", "max_pages", "max_chunks", "workers"):
            if key in data and (v := _coerce_int(data[key])) is not None:
                setattr(cfg.ingest, key, v)
        for key in ("audio_max_minutes", "politeness"):
            if key in data and (v := _coerce_float(data[key])) is not None:
                setattr(cfg.ingest, key, v)
        if "ocr_text_vector" in data:
            cfg.ingest.ocr_text_vector = _coerce_bool(data["ocr_text_vector"], True)
        if "asr" in data:
            mode = str(data["asr"]).strip().lower()
            if mode in ("auto", "always", "off"):
                cfg.ingest.asr = mode
            else:
                logger.warning(
                    "config: [ingest] asr=%r invalid (auto|always|off) — ignored",
                    data["asr"],
                )
        if "asr_max_minutes" in data and (
            v := _coerce_float(data["asr_max_minutes"])
        ) is not None:
            cfg.ingest.asr_max_minutes = v
        if "asr_language" in data:
            cfg.ingest.asr_language = str(data["asr_language"])
        if "asr_whisper" in data:
            cfg.ingest.asr_whisper = str(data["asr_whisper"])
        unknown(section, data, set(IngestCfg.__dataclass_fields__))
    elif section == "ocr":
        if "enabled" in data:
            cfg.ocr.enabled = _coerce_bool(data["enabled"], True)
        unknown(section, data, set(OcrCfg.__dataclass_fields__))
    elif section == "search":
        if "limit" in data and (v := _coerce_int(data["limit"])) is not None:
            cfg.search.limit = v
        if "z_gate" in data and (v := _coerce_float(data["z_gate"])) is not None:
            cfg.search.z_gate = v
        if "scorer" in data:
            cfg.search.scorer = str(data["scorer"])
        unknown(section, data, set(SearchCfg.__dataclass_fields__))
    elif section == "distill":
        if "threshold" in data and (v := _coerce_float(data["threshold"])) is not None:
            cfg.distill.threshold = v
        for key in ("min_members", "max_chunks"):
            if key in data and (v := _coerce_int(data[key])) is not None:
                setattr(cfg.distill, key, v)
        unknown(section, data, set(DistillCfg.__dataclass_fields__))
    elif section == "serve":
        # R8 [serve] (D7): linger/timeout_s/socket. An `auto` key is UNKNOWN
        # here on purpose (F9 cut) and warns like any other unknown key.
        if "linger" in data and (v := _coerce_int(data["linger"])) is not None:
            cfg.serve.linger = v
        if "timeout_s" in data and (v := _coerce_float(data["timeout_s"])) is not None:
            cfg.serve.timeout_s = v
        if "socket" in data:
            cfg.serve.socket = str(data["socket"])
        unknown(section, data, set(ServeCfg.__dataclass_fields__))
    elif section == "watch":
        # R9 [watch] (§6.2 as amended by review R-F1): interval overrides only.
        for key in ("interval_fast", "interval_slow", "battery_max_batch",
                    "full_period_s"):
            if key in data and (v := _coerce_int(data[key])) is not None:
                setattr(cfg.watch, key, v)
        if "linger" in data and (v := _coerce_int(data["linger"])) is not None:
            cfg.watch.linger = v
        for key in ("settle_s", "min_pass_gap_s", "battery_pass_gap_s"):
            if key in data and (v := _coerce_float(data[key])) is not None:
                setattr(cfg.watch, key, v)
        unknown(section, data, set(WatchCfg.__dataclass_fields__))
    elif section == "init":
        if "roots" in data:
            vals = data["roots"]
            if isinstance(vals, list) and all(isinstance(v, str) for v in vals):
                cfg.init.roots = list(vals)
            else:
                logger.warning(
                    "config: [init] roots=%r invalid (list of strings) — ignored", vals
                )
        unknown(section, data, set(InitCfg.__dataclass_fields__))
    else:
        unknown(section, data, set())


def unknown(section: str, data: Mapping[str, Any], known: set[str]) -> None:
    for key in data:
        if key not in known:
            logger.warning("config: unknown key [%s] %s — ignored", section, key)


# --------------------------------------------------------------------- writer


def _toml_value(value: Any) -> str:
    """Serialize one Python value as the right-hand side of a TOML line.
    (Basic strings via json.dumps — JSON basic strings are TOML basic strings,
    the same trick model.set_config_llama_model uses.)"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if value is None:
        raise ValueError("config writer: None is not a TOML value")
    return str(value)  # int / float


def set_config_keys(config_path: Path, section: str, values: dict[str, Any]) -> None:
    """Write `key = value` lines into one [section] of config.toml, preserving
    every other line (comments, ordering, sibling sections). Creates the
    section at the end of the file when absent; inserts new keys right after
    the section header. A .bak copy is written before the first modification
    (the house rule for editing user config)."""
    quoted_section = f"[{section}]"
    text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    lines = text.splitlines()
    out: list[str] = []
    current = None
    section_seen = False
    pending = dict(values)
    section_insert_at: int | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current == section and pending:
                # flush keys we did not find in the existing section: right
                # after its last line
                for key, val in pending.items():
                    out.append(f"{key} = {_toml_value(val)}")
                pending.clear()
            current = stripped
            if current == quoted_section:
                section_seen = True
                section_insert_at = len(out) + 1  # right after the header
        elif (
            current == quoted_section
            and pending
            and stripped and not stripped.startswith("#") and "=" in stripped
        ):
            key = stripped.split("#")[0].split("=", 1)[0].strip()
            if key in pending:
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f"{indent}{key} = {_toml_value(pending.pop(key))}")
                continue
        out.append(line)
    if pending:
        if section_seen and section_insert_at is not None:
            fresh = [f"{k} = {_toml_value(v)}" for k, v in pending.items()]
            out[section_insert_at:section_insert_at] = fresh
        else:
            if out and out[-1].strip():
                out.append("")
            out.append(quoted_section)
            out.extend(f"{k} = {_toml_value(v)}" for k, v in pending.items())
    new_text = "\n".join(out) + "\n"
    if new_text != text:
        if config_path.is_file():
            shutil.copy2(config_path, config_path.with_name(config_path.name + ".bak"))
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_name(config_path.name + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, config_path)


def set_config_init_roots(config_path: Path, roots: list[str]) -> None:
    """The scope-invariant record (v2 item 20): persist the roots the user
    explicitly chose into [init] roots. Nothing else ever writes this key."""
    set_config_keys(config_path, "init", {"roots": list(roots)})


def load(config_path: Path | None = None, env: Mapping[str, str] = os.environ) -> Config:
    """Resolve configuration. env > config file > built-in defaults.

    Flags are applied by the CLI on the returned dataclass (flag > env > file).
    """
    cfg = Config()

    path = Path(config_path) if config_path is not None else None
    if path is None:
        env_cfg = env.get("GOLDMINE_CONFIG")
        path = Path(env_cfg) if env_cfg else default_config_path()

    if path.is_file():
        try:
            with open(path, "rb") as f:
                parsed = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            logger.warning("config: %s does not parse (%s) — using defaults", path, exc)
            parsed = {}
        for section, data in parsed.items():
            if not isinstance(data, dict):
                logger.warning("config: ignoring non-table %s", section)
                continue
            _apply_section(cfg, section, data)
    elif config_path is not None or env.get("GOLDMINE_CONFIG"):
        logger.warning("config: %s not found — using defaults", path)

    # Environment overrides (SPEC §11).
    if v := env.get("GOLDMINE_HOME"):
        cfg.home = Path(v)
    if v := env.get("GOLDMINE_BACKEND"):
        cfg.backend = v
    if v := env.get("GOLDMINE_LLAMA_SERVER"):
        cfg.llama.server = Path(v)
        cfg.llama.server_configured = True
    if v := env.get("GOLDMINE_MODEL"):
        cfg.llama.model = Path(v)
        cfg.llama.model_configured = True
    if v := env.get("GOLDMINE_MODELS"):
        cfg.models.dir = Path(v)
    if v := env.get("GOLDMINE_FORESIGHT_SOCKET"):
        cfg.foresight.socket = v
    # R8 serve (D7): the explicit-daemon-socket override; read ONLY here (the
    # cli.py:198-208-era rule that env reads belong to config.load — the env
    # block now lives at these lines, which is the F10 citation refresh)
    if v := env.get("GOLDMINE_SERVE"):
        cfg.serve.socket = v
    # v2 ASR (02 §5.1): whisper-cli override + mode knob
    if v := env.get("GOLDMINE_WHISPER"):
        cfg.ingest.asr_whisper = v
    if v := env.get("GOLDMINE_ASR"):
        if v.strip().lower() in ("auto", "always", "off"):
            cfg.ingest.asr = v.strip().lower()
    # v2 OCR (02 §5.8): master switch + the A5 policy toggle (eval A/B uses it)
    if "GOLDMINE_OCR" in env:
        cfg.ocr.enabled = _coerce_bool(env["GOLDMINE_OCR"], True)
    if "GOLDMINE_OCR_TEXT_VECTOR" in env:
        cfg.ingest.ocr_text_vector = _coerce_bool(
            env["GOLDMINE_OCR_TEXT_VECTOR"], True
        )
    # The llama sub-config needs the resolved home for the <home>/bin and
    # <home>/models fallback legs (05 §4.3/§4.7).
    cfg.llama.home = cfg.home
    return cfg
