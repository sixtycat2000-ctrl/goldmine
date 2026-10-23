"""Tier-1 unit tests: config resolution order (flag > env > file > default)."""

from __future__ import annotations

from pathlib import Path

from goldmine.config import (
    Config,
    default_config_path,
    index_path,
    load,
    resolve_home,
    set_config_init_roots,
    set_config_keys,
)


def test_defaults(tmp_home):
    cfg = load(config_path=tmp_home / "no-such.toml", env={})
    assert cfg.backend == "llama"
    assert cfg.ingest.max_file_mb == 200
    assert cfg.ingest.max_chunks == 5000
    assert cfg.search.z_gate == 3.0  # v2.1 single qualification rule
    assert cfg.search.scorer == "hybrid"
    assert cfg.distill.threshold is None  # v2 R6: unset means CALIBRATED
    assert cfg.llama.batch == 16
    assert cfg.llama.ctx == 2048


def test_config_file_parsed(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(
        """
[backend]
default = "mock"

[ingest]
max_file_mb = 42
politeness = 0.25

[search]
limit = 7
scorer = "rrf"

[distill]
threshold = 0.8
min_members = 5
""".strip(),
        encoding="utf-8",
    )
    cfg = load(config_path=toml, env={})
    assert cfg.backend == "mock"
    assert cfg.ingest.max_file_mb == 42
    assert cfg.ingest.politeness == 0.25
    assert cfg.search.limit == 7
    assert cfg.search.scorer == "rrf"
    assert cfg.distill.threshold == 0.8
    assert cfg.distill.min_members == 5
    # untouched defaults survive
    assert cfg.ingest.workers == 2
    assert cfg.llama.request_timeout == 60.0


def test_env_overrides_file(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text('[backend]\ndefault = "mock"\n', encoding="utf-8")
    cfg = load(config_path=toml, env={"GOLDMINE_BACKEND": "llama"})
    assert cfg.backend == "llama"  # env beats file

    cfg = load(
        config_path=toml,
        env={"GOLDMINE_HOME": "/data/gm", "GOLDMINE_MODEL": "/models/m.gguf"},
    )
    assert cfg.home == Path("/data/gm")
    assert cfg.llama.model == Path("/models/m.gguf")


def test_flag_applied_after_load(tmp_home):
    """Flags are applied on top by the CLI: flag > env > file > default."""
    cfg = load(config_path=None, env={"GOLDMINE_BACKEND": "mock"})
    cfg.backend = "llama"  # simulating --backend llama
    assert cfg.backend == "llama"


def test_unknown_keys_warn_not_crash(tmp_path, caplog):
    toml = tmp_path / "config.toml"
    toml.write_text(
        '[backend]\ndefault = "mock"\nbogus = true\n\n[nope]\nkey = 1\n',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="goldmine.config"):
        cfg = load(config_path=toml, env={})
    assert cfg.backend == "mock"
    assert any("bogus" in r.message for r in caplog.records)
    assert any("[nope]" in r.message for r in caplog.records)


def test_broken_toml_warns_and_uses_defaults(tmp_path, caplog):
    toml = tmp_path / "config.toml"
    toml.write_text("[backend\ndefault =", encoding="utf-8")
    with caplog.at_level("WARNING", logger="goldmine.config"):
        cfg = load(config_path=toml, env={})
    assert cfg.backend == "llama"


def test_goldmine_config_env(tmp_path):
    toml = tmp_path / "elsewhere.toml"
    toml.write_text("[search]\nlimit = 3\n", encoding="utf-8")
    cfg = load(config_path=None, env={"GOLDMINE_CONFIG": str(toml)})
    assert cfg.search.limit == 3


def test_resolve_home_creates(tmp_home):
    cfg = Config(home=tmp_home / "data" / "goldmine")
    out = resolve_home(cfg)
    assert out.is_dir()
    assert index_path(cfg, "default") == out / "default.sqlite"


def test_default_config_path_is_xdg():
    assert str(default_config_path()).endswith("goldmine/config.toml")
    assert "~" not in str(default_config_path())  # expanded


# ------------------------------------------------- v2 items 4/5: model plumbing


def test_model_configured_flag(tmp_path, tmp_home):
    # neither file nor env: the built-in default is NOT "configured"
    cfg = load(config_path=tmp_path / "none.toml", env={})
    assert cfg.llama.model_configured is False
    # file
    toml = tmp_path / "config.toml"
    toml.write_text('[llama]\nmodel = "/data/mymodel.gguf"\n')
    cfg = load(config_path=toml, env={})
    assert cfg.llama.model_configured is True
    assert cfg.llama.model == Path("/data/mymodel.gguf")
    # env wins over file
    cfg = load(config_path=toml, env={"GOLDMINE_MODEL": "/env/model.gguf"})
    assert cfg.llama.model == Path("/env/model.gguf")
    assert cfg.llama.model_configured is True


def test_models_dir_and_llama_home_propagation(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text('[models]\ndir = "/data/models"\n')
    cfg = load(config_path=toml, env={})
    assert cfg.models.dir == Path("/data/models")
    cfg = load(config_path=toml, env={"GOLDMINE_MODELS": "/env/models"})
    assert cfg.models.dir == Path("/env/models")  # env > file
    # the llama sub-config carries the resolved home (05 §4.3/§4.7 fallbacks)
    cfg = load(config_path=tmp_path / "none.toml", env={
        "GOLDMINE_HOME": "/env/gmhome",
    })
    assert cfg.llama.home == Path("/env/gmhome")


# --------------------- v2 item 20: [init] roots record + set_config_keys writer


def test_set_config_init_roots_round_trip(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text('[search]\nlimit = 3\n')
    set_config_init_roots(toml, ["/tmp/a b", "~/x"])
    cfg = load(config_path=toml, env={})
    assert cfg.init.roots == ["/tmp/a b", "~/x"]
    assert cfg.search.limit == 3  # untouched section survives
    # a rewrite REPLACES the record (never appends duplicates)
    set_config_init_roots(toml, ["/tmp/c"])
    cfg = load(config_path=toml, env={})
    assert cfg.init.roots == ["/tmp/c"]
    # house rule: .bak before first modification
    assert (tmp_path / "config.toml.bak").exists()


def test_set_config_keys_creates_section_and_typed_values(tmp_path):
    toml = tmp_path / "c2.toml"
    toml.write_text('[ocr]\nenabled = false\n')
    set_config_keys(toml, "ingest", {"asr": "auto", "workers": 4})
    cfg = load(config_path=toml, env={})
    assert cfg.ingest.asr == "auto"
    assert cfg.ingest.workers == 4
    assert cfg.ocr.enabled is False  # sibling section untouched
    text = toml.read_text()
    assert text.splitlines()[0] == "[ocr]"  # no reordering


def test_init_section_parsed_with_defaults(tmp_path):
    toml = tmp_path / "c3.toml"
    toml.write_text('[init]\nroots = ["/a", "/b"]\n')
    cfg = load(config_path=toml, env={})
    assert cfg.init.roots == ["/a", "/b"]
    cfg = load(config_path=tmp_path / "none.toml", env={})
    assert cfg.init.roots == []


def test_init_section_rejects_non_string_roots(tmp_path, caplog):
    toml = tmp_path / "c4.toml"
    toml.write_text('[init]\nroots = [1, 2]\n')
    cfg = load(config_path=toml, env={})
    assert cfg.init.roots == []
