"""Private fixed configs/environments; all actual child env keys enter identity."""
from __future__ import annotations

import json
import os
from pathlib import Path

from answer_eval.protocol import EvaluationError
from .recoll import sandbox_profile


def environment(owner, name, paths):
    # No credentials, proxies, Node options, model/cache or engine overrides are
    # inherited. HOME stays the human's actual HOME, never repurposed.
    env = {k: os.environ[k] for k in ("HOME",) if k in os.environ}
    env.update(LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8", PATH="/usr/bin:/bin",
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PIP_NO_INDEX="1",
               XDG_CONFIG_HOME=str(owner / "xdg/config"), XDG_CACHE_HOME=str(owner / "xdg/cache"), XDG_DATA_HOME=str(owner / "xdg/data"),
               TMPDIR=str(owner / "tmp"))
    if name == "goldmine":
        for role in ("ffmpeg", "ffprobe", "pdfinfo", "pdftotext", "pdftoppm", "sips"):
            if Path(paths[role]).name != role:
                raise EvaluationError("fixed Goldmine native PATH tool basename differs")
        env.update(GOLDMINE_HOME=str(owner / "home"), PYTHONPATH=paths["installation"],
                   GOLDMINE_OCR_HELPER=paths["ocr"], GOLDMINE_OCR_AUTOCOMPILE="0")
        env["PATH"] = ":".join(dict.fromkeys([str(Path(paths[r]).parent) for r in ("pdfinfo", "pdftotext", "pdftoppm", "ffmpeg", "ffprobe", "sips")] + ["/usr/bin", "/bin"]))
    elif name == "qmd":
        env.update(QMD_SOURCE_MODE="0", QMD_CONFIG_DIR=str(owner / "qmd-config"),
                   NODE_LLAMA_CPP_SKIP_DOWNLOAD="true", GGML_METAL_NO_RESIDENCY="1")
    else:
        env.update(RECOLL_CONFDIR=str(owner / "recoll-config"), RECOLL_DATADIR=paths["data_dir"])
        env["PATH"] = ":".join(dict.fromkeys([str(Path(paths["python"]).parent), paths["dependencies"],
                                                 str(Path(paths["recollq"]).parent), "/usr/bin", "/bin"]))
    return env


def setup(owner, name, paths, view, *, replay=False):
    owner = Path(owner)
    if not replay:
        for rel in ("cwd", "tmp", "home", "xdg/config", "xdg/cache", "xdg/data", "prepare", "queries", "audit"):
            (owner / rel).mkdir(parents=True, exist_ok=False)
    def text_file(path, text, *, mutable_after_prepare=False):
        if replay:
            if not mutable_after_prepare and path.read_bytes() != text.encode("utf-8"):
                raise EvaluationError("actual isolated configuration bytes changed")
        else:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(text)
    values = dict(paths, selected_corpus=view["corpus"], native_corpus=view["corpus"], common_corpus=view["corpus"])
    if name == "goldmine":
        cfg = owner / "goldmine.toml"
        # [search] request_timeout_s aligns the engine's internal client
        # deadline with the plan's preregistered 600s query wall (the product
        # default 10s/5s interactive deadline silently overrides it otherwise;
        # capture-completion-fix-001).
        text_file(cfg, '[core]\nhome = ' + json.dumps(str(owner / "home")) + '\n[backend]\ndefault = "llama"\n'
                       '[llama]\nserver = ' + json.dumps(paths["server"]) + '\nmodel = ' + json.dumps(paths["embedding"]) +
                       '\n[models]\ndir = ' + json.dumps(paths["models"]) + '\n[ingest]\nasr = "always"\nasr_whisper = ' + json.dumps(paths["whisper"]) +
                       '\nasr_language = "auto"\nasr_max_minutes = 120.0\n[ocr]\nenabled = true\n[search]\nrequest_timeout_s = 600.0\n')
        values.update(goldmine_db=str(owner / "index.sqlite"), goldmine_config=str(cfg))
    elif name == "qmd":
        cfg = owner / "qmd-config"
        if not replay:
            cfg.mkdir()
        # JSON quoted scalars are a strict subset of YAML scalars. No arbitrary
        # user YAML, hooks, update commands, alternative models or remote URLs.
        # All three pinned local model paths are written for every arm: QMD
        # 2.8.3 fills any omitted role with its documented hf: default URIs in
        # the stored config, which the frozen plan forbids as a fetch fallback
        # (quality-harness-fix-001). Presence does not change arm behavior:
        # search stays model-free BM25; vsearch uses embed+generate only.
        text = "models:\n" + "".join("  " + role + ": " + json.dumps(paths[role]) + "\n" for role in ("embed", "generate", "rerank") if role in paths)
        if not any(role in paths for role in ("embed", "generate", "rerank")):
            raise EvaluationError("QMD pinned local model roles are unresolved", status="MISSING")
        text_file(cfg / "comparison.yml", text, mutable_after_prepare=True)
        values.update(qmd_index="comparison", qmd_collection="sources", qmd_db=str(owner / "xdg/cache/qmd/comparison.sqlite"))
    else:
        cfg = owner / "recoll-config"
        if not replay:
            cfg.mkdir()
        # recoll.conf values are raw scalars: the installed 1.44.2 parser reads
        # them literally, so a JSON-quoted absolute path is treated as relative
        # and index creation fails on a quote-containing mkdir (see
        # recoll-conf-fix-001). Owned paths never contain newlines/quotes.
        text_file(cfg / "recoll.conf", "topdirs = " + view["corpus"] +
            "\ndbdir = " + str(owner / "recoll-db") + "\nloglevel = 2\nindexStoreDocText = 1\npreferStoredTextMimes = *\n")
        if view["name"] == "common":
            # Shared converted input is plain UTF8, not another Markdown -> HTML
            # conversion. Native view keeps the installed MIME/filter behavior.
            text_file(cfg / "mimemap", ".md = text/plain\n")
        profile = owner / "deny-source-read.sb"
        text_file(profile, sandbox_profile(view["corpus"]))
        values.update(recoll_config_dir=str(cfg), recoll_db=str(owner / "recoll-db"), sandbox_profile=str(profile))
    return values, environment(owner, name, paths)
