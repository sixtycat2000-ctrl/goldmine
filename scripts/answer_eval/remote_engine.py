"""Reviewed remote-engine adapter for answer-eval/v3 (extension: remote-openai-chat/v1).

Executes frozen answer/judge prompt bytes against an OpenAI-compatible chat
completions endpoint, one sequential call per immutable job descriptor, and
writes the raw HTTP completion family the existing v3 readers verify. The API
key is read from the environment, matched by SHA256 fingerprint only, and is
never logged, printed or persisted. See
reports/execution/P0-03/answer-engine-extension-001/SPEC.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from acceptance_lib.evidence import now, write_json
from .protocol import (PROTOCOL, EvaluationError, canonical_json, canonical_sha256,
                       fields, nonempty, parse_json, sha, timestamp)

ENGINE_KIND = "remote-openai-chat/v1"
ENGINE_FIELDS = {"schema", "kind", "base_url", "model_id", "api_key_sha256",
                 "settings", "request_timeout_seconds"}
SETTING_FIELDS = {"temperature", "top_p", "stream"}
RECEIPT_KIND = "answer-eval.http-run/v1"
RECEIPT_FIELDS = {"schema", "kind", "run_id", "check_id", "method", "url", "engine",
                  "started_at", "finished_at", "duration_ms", "status", "timed_out",
                  "error", "finish_reason", "response_model", "request", "http_request",
                  "response", "stdout", "usage", "usage_values"}
MAX_RECORD_BYTES = 1024 * 1024
KEY_ENV = "GLM_KEY"
JOB_CHECK = "answer.job"


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def validate_engine(value, label: str = "remote engine") -> dict:
    fields(value, ENGINE_FIELDS, label)
    if type(value["schema"]) is not int or value["schema"] != 1 or value["kind"] != ENGINE_KIND:
        raise EvaluationError(f"{label}: require schema1 {ENGINE_KIND}")
    base = value["base_url"]
    if not isinstance(base, str) or not base.startswith("https://") or base != base.rstrip("/") or " " in base:
        raise EvaluationError(f"{label}.base_url: require an https URL without trailing slash")
    nonempty(value["model_id"], label + ".model_id")
    sha(value["api_key_sha256"], label + ".api_key_sha256")
    settings = value["settings"]
    fields(settings, SETTING_FIELDS, label + ".settings")
    for name, low, high in (("temperature", 0.0, 2.0), ("top_p", 0.0, 1.0)):
        number = settings[name]
        if type(number) not in (int, float) or isinstance(number, bool) or not low <= number <= high:
            raise EvaluationError(f"{label}.settings.{name}: require a number in {low}..{high}")
    if settings["stream"] is not False:
        raise EvaluationError(f"{label}.settings.stream must be false")
    timeout = value["request_timeout_seconds"]
    if type(timeout) not in (int, float) or isinstance(timeout, bool) or not 0 < timeout <= 3600:
        raise EvaluationError(f"{label}.request_timeout_seconds: require a positive bound")
    return value


def load_engine(path: Path) -> dict:
    return validate_engine(parse_json(Path(path).read_text(encoding="utf-8"), str(path)))


def require_api_key(engine: dict) -> str:
    key = os.environ.get(KEY_ENV)
    if not key:
        raise EvaluationError(f"{KEY_ENV} is not set; the remote engine key is environment-only", status="MISSING")
    if key_fingerprint(key) != engine["api_key_sha256"]:
        raise EvaluationError(f"{KEY_ENV} fingerprint does not match the registered remote engine identity")
    return key


def engine_binding(engine: dict) -> dict:
    """The existing schema3 models.<role> value under the remote extension."""
    return {"model_id": engine["model_id"], "model_sha256": canonical_sha256(engine)}


def chat_url(engine: dict) -> str:
    return engine["base_url"] + "/chat/completions"


def render_request(engine: dict, prompt_text: str) -> dict:
    settings = engine["settings"]
    return {"model": engine["model_id"], "stream": False,
            "messages": [{"role": "user", "content": prompt_text}],
            "temperature": settings["temperature"], "top_p": settings["top_p"]}


@dataclass(frozen=True)
class HttpExchange:
    """One transport result; the only network touchpoint (tests replace it)."""
    status: int
    response_bytes: bytes
    error: str | None
    timed_out: bool


def http_exchange(url: str, request_bytes: bytes, key: str, timeout: float) -> HttpExchange:
    request = urllib.request.Request(url, data=request_bytes, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpExchange(response.status, response.read(), None, False)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        return HttpExchange(exc.code, body, f"HTTP {exc.code} {exc.reason}", False)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return HttpExchange(0, b"", f"URLError: {reason}", isinstance(reason, TimeoutError))
    except TimeoutError:
        return HttpExchange(0, b"", "timeout", True)


@dataclass(frozen=True)
class Completion:
    run_id: str
    started_at: str
    finished_at: str
    duration_ms: float
    status: int
    request_bytes: bytes
    response_bytes: bytes
    content: str
    finish_reason: str
    response_model: str
    usage: dict


def _failure(message: str, *, kind: str = "ENGINE_CALL_FAILED") -> dict:
    return {"status": "FAILED", "failure": kind, "error": message, "release_eligible": False}


def _bounded_bytes(data: bytes, label: str) -> bytes:
    if len(data) > MAX_RECORD_BYTES:
        raise EvaluationError(f"{label} exceeds the 1MiB completion bound ({len(data)} bytes)")
    return data


def execute_prompt(engine: dict, key: str, prompt_text: str) -> tuple[Completion | None, dict]:
    """One deterministic call; no retry. Missing native usage is always a failure."""
    url = chat_url(engine)
    request_bytes = canonical_json(render_request(engine, prompt_text)).encode("utf-8")
    if len(request_bytes) > MAX_RECORD_BYTES:
        return None, _failure(f"rendered HTTP request exceeds 1MiB ({len(request_bytes)} bytes)",
                              kind="REQUEST_TOO_LARGE")
    timeout = float(engine["request_timeout_seconds"])
    started, wall = now(), time.perf_counter()
    exchange = http_exchange(url, request_bytes, key, timeout)
    duration_ms = (time.perf_counter() - wall) * 1000.0
    finished = now()
    run_id = str(uuid.uuid4())
    if exchange.timed_out:
        return None, {**_failure(f"request exceeded the registered {timeout}s wall bound", kind="TIMED_OUT"),
                      "run_id": run_id, "started_at": started, "finished_at": finished,
                      "duration_ms": duration_ms, "status": exchange.status,
                      "request_bytes": request_bytes, "response_bytes": exchange.response_bytes}
    if exchange.error is not None or exchange.status != 200:
        return None, {**_failure(exchange.error or f"HTTP status {exchange.status}", kind="HTTP_ERROR"),
                      "run_id": run_id, "started_at": started, "finished_at": finished,
                      "duration_ms": duration_ms, "status": exchange.status,
                      "request_bytes": request_bytes, "response_bytes": exchange.response_bytes}
    try:
        body = json.loads(exchange.response_bytes.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        return None, {**_failure(f"native response is not JSON: {exc}", kind="INVALID_RESPONSE"),
                      "run_id": run_id, "started_at": started, "finished_at": finished,
                      "duration_ms": duration_ms, "status": exchange.status,
                      "request_bytes": request_bytes, "response_bytes": exchange.response_bytes}
    try:
        choice = body["choices"][0]
        content = choice["message"]["content"]
        usage = body["usage"]
        prompt_tokens, completion_tokens = usage["prompt_tokens"], usage["completion_tokens"]
        finish_reason, response_model = body["choices"][0]["finish_reason"], body.get("model", "")
    except (KeyError, IndexError, TypeError) as exc:
        return None, {**_failure(f"native response lacks required fields: {exc}", kind="INVALID_RESPONSE"),
                      "run_id": run_id, "started_at": started, "finished_at": finished,
                      "duration_ms": duration_ms, "status": exchange.status,
                      "request_bytes": request_bytes, "response_bytes": exchange.response_bytes}
    if not isinstance(content, str) or not content.strip():
        return None, {**_failure("native response content is empty", kind="INVALID_RESPONSE"),
                      "run_id": run_id, "started_at": started, "finished_at": finished,
                      "duration_ms": duration_ms, "status": exchange.status,
                      "request_bytes": request_bytes, "response_bytes": exchange.response_bytes}
    if (type(prompt_tokens) is not int or type(completion_tokens) is not int
            or prompt_tokens < 0 or completion_tokens < 0):
        return None, {**_failure("native usage counters are missing or non-integer; never recorded as zero",
                                 kind="MISSING_USAGE"),
                      "run_id": run_id, "started_at": started, "finished_at": finished,
                      "duration_ms": duration_ms, "status": exchange.status,
                      "request_bytes": request_bytes, "response_bytes": exchange.response_bytes}
    try:
        _bounded_bytes(exchange.response_bytes, "native response")
    except EvaluationError as exc:
        return None, {**_failure(str(exc), kind="RESPONSE_TOO_LARGE"),
                      "run_id": run_id, "started_at": started, "finished_at": finished,
                      "duration_ms": duration_ms, "status": exchange.status,
                      "request_bytes": request_bytes, "response_bytes": exchange.response_bytes}
    return Completion(run_id, started, finished, duration_ms, exchange.status, request_bytes,
                      exchange.response_bytes, content, str(finish_reason), str(response_model),
                      {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}), {}


def _artifact(folder: Path, name: str, data: bytes) -> dict:
    _bounded_bytes(data, name)
    (folder / name).write_bytes(data)
    return {"path": name, "sha256": hashlib.sha256(data).hexdigest()}


def write_raw_completion(folder: Path, job: dict, engine: dict, prompt_bytes: bytes,
                         completion: Completion) -> dict:
    """Write the fixed http-run raw family; the folder must be fresh."""
    if folder.exists():
        raise EvaluationError("raw completion folders are never overwritten")
    folder.mkdir(parents=True)
    stdout_bytes = completion.content.encode("utf-8")
    artifacts = {
        "request": _artifact(folder, "request.txt", prompt_bytes),
        "http_request": _artifact(folder, "http_request.json", completion.request_bytes),
        "response": _artifact(folder, "response.json", completion.response_bytes),
        "stdout": _artifact(folder, "stdout.log", stdout_bytes),
        "usage": _artifact(folder, "usage.json",
                           (canonical_json(completion.usage) + "\n").encode("utf-8")),
    }
    receipt = {"schema": 1, "kind": RECEIPT_KIND, "run_id": completion.run_id, "check_id": JOB_CHECK,
               "method": "POST", "url": chat_url(engine), "engine": engine,
               "started_at": completion.started_at, "finished_at": completion.finished_at,
               "duration_ms": completion.duration_ms, "status": completion.status,
               "timed_out": False, "error": None, "finish_reason": completion.finish_reason,
               "response_model": completion.response_model, "usage_values": completion.usage}
    receipt.update({key: artifacts[key] for key in ("request", "http_request", "response", "stdout", "usage")})
    _bounded_bytes(canonical_json(receipt).encode("utf-8"), "receipt.json")
    write_json(folder / "receipt.json", receipt)
    manifest = {"schema": 1, "kind": RECEIPT_KIND, "run_id": completion.run_id, "check_id": JOB_CHECK,
                "suite_id": PROTOCOL, "identity": {"job": job, "engine": engine},
                "receipt": {"path": "receipt.json",
                            "sha256": hashlib.sha256((folder / "receipt.json").read_bytes()).hexdigest()}}
    write_json(folder / "manifest.json", manifest)
    return manifest


def write_failure_record(folder: Path, job: dict, engine: dict, failure: dict) -> Path:
    """Keep failed attempts outside the canonical raw completion set."""
    for name in ("request_bytes", "response_bytes"):
        data = failure.get(name)
        if isinstance(data, (bytes, bytearray)):
            del failure[name]
            if 0 < len(data) <= MAX_RECORD_BYTES:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / name.replace("_bytes", ".bin")).write_bytes(data)
            failure[name + "_sha256"] = hashlib.sha256(data).hexdigest()
            failure[name + "_bytes_length"] = len(data)
    failure.update(schema=1, kind="answer-eval.http-failure/v1", job_id=job["job_id"],
                   engine=engine, url=chat_url(engine), release_eligible=False)
    folder.mkdir(parents=True, exist_ok=True)
    sequence = len(list(folder.glob("*.failure.json"))) + 1
    path = folder / f"{sequence:03d}.failure.json"
    write_json(path, failure)
    return path


def judge_output(content: str, question: dict) -> dict:
    """Strictly normalize the native judge content to the frozen v3 grade payload."""
    try:
        result = parse_json(content, "judge response")
    except EvaluationError as exc:
        raise EvaluationError(f"judge response is not strict JSON: {exc}") from exc
    if not isinstance(result, dict) or set(result) != {"score", "refused", "fabricated_fact", "fact_results"}:
        raise EvaluationError("judge response must contain exactly score/refused/fabricated_fact/fact_results")
    score = result["score"]
    if type(score) is not int or score not in (0, 1, 2):
        raise EvaluationError("judge score must be integer 0/1/2")
    if type(result["refused"]) is not bool or type(result["fabricated_fact"]) is not bool:
        raise EvaluationError("judge refused/fabricated_fact must be booleans")
    facts = result["fact_results"]
    if not isinstance(facts, list):
        raise EvaluationError("judge fact_results must be a list")
    known = {fact["fact_id"] for fact in question["required_facts"]}
    seen = []
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {"fact_id", "supported", "evidence_ids"}:
            raise EvaluationError("judge fact_result must contain exactly fact_id/supported/evidence_ids")
        if type(fact["supported"]) is not bool or not isinstance(fact["evidence_ids"], list):
            raise EvaluationError("judge fact_result requires supported bool and evidence_ids list")
        if (any(not isinstance(x, str) for x in fact["evidence_ids"])
                or len(set(fact["evidence_ids"])) != len(fact["evidence_ids"])
                or fact["supported"] and not fact["evidence_ids"]):
            raise EvaluationError("judge fact_result evidence_ids must be unique and nonempty when supported")
        seen.append(fact["fact_id"])
    if len(set(seen)) != len(seen) or set(seen) != known:
        raise EvaluationError("judge fact_results must cover every required fact exactly once")
    return result


def verify_http_manifest(folder: Path, job: dict) -> tuple[dict, dict, Path]:
    """Read back one http-run raw family and enforce the extension's receipt layout."""
    from acceptance_lib.evidence import verify_artifact
    manifest = parse_json((folder / "manifest.json").read_text(encoding="utf-8"), "raw manifest")
    if (set(manifest) != {"schema", "kind", "run_id", "check_id", "suite_id", "identity", "receipt"}
            or manifest["schema"] != 1 or manifest["kind"] != RECEIPT_KIND
            or manifest["check_id"] != JOB_CHECK or manifest["suite_id"] != PROTOCOL):
        raise EvaluationError("raw completion must use the fixed http-run manifest layout")
    engine = job["runtime"]["engine"]
    validate_engine(engine, "job runtime engine")
    if manifest["identity"] != {"job": job, "engine": engine}:
        raise EvaluationError("raw completion job/engine identity mismatch")
    receipt = parse_json((verify_artifact(folder, manifest["receipt"])).read_text(encoding="utf-8"), "raw receipt")
    if set(receipt) != RECEIPT_FIELDS or receipt["schema"] != 1 or receipt["kind"] != RECEIPT_KIND:
        raise EvaluationError("raw completion receipt fields do not match the extension layout")
    if receipt["run_id"] != manifest["run_id"] or receipt["check_id"] != JOB_CHECK:
        raise EvaluationError("raw receipt identity mismatch")
    if receipt["engine"] != engine or receipt["method"] != "POST" or receipt["url"] != chat_url(engine):
        raise EvaluationError("raw receipt engine/endpoint differs from the registered identity")
    if receipt["status"] != 200 or receipt["timed_out"] or receipt["error"] is not None:
        raise EvaluationError("raw completion records a failed engine call")
    started, finished = timestamp(receipt["started_at"], "receipt.started_at"), timestamp(receipt["finished_at"], "receipt.finished_at")
    if finished < started or not receipt["duration_ms"] > 0 or not receipt["finish_reason"].strip():
        raise EvaluationError("raw receipt timing/finish_reason invalid")
    prompt = verify_artifact(folder, receipt["request"])
    if hashlib.sha256(prompt.read_bytes()).hexdigest() != job["request"]["sha256"]:
        raise EvaluationError("raw engine receipt lacks the exact frozen request bytes")
    http_request = parse_json(verify_artifact(folder, receipt["http_request"]).read_text(encoding="utf-8"), "http request")
    if http_request != render_request(engine, prompt.read_text(encoding="utf-8")):
        raise EvaluationError("stored HTTP request differs from the frozen engine rendering")
    response = parse_json(verify_artifact(folder, receipt["response"]).read_text(encoding="utf-8"), "native response")
    usage = parse_json(verify_artifact(folder, receipt["usage"]).read_text(encoding="utf-8"), "engine usage")
    fields(usage, {"input_tokens", "output_tokens"}, "actual engine usage")
    if any(type(v) is not int or v < 0 for v in usage.values()):
        raise EvaluationError("actual usage requires nonnegative integer counters, no invented unknown=0")
    try:
        native = response["usage"]
        if (usage["input_tokens"] != native["prompt_tokens"] or usage["output_tokens"] != native["completion_tokens"]):
            raise KeyError("usage mismatch")
    except (KeyError, TypeError) as exc:
        raise EvaluationError("recorded usage differs from the native response counters") from exc
    stdout = verify_artifact(folder, receipt["stdout"])
    content = response["choices"][0]["message"]["content"]
    if not isinstance(content, str) or stdout.read_bytes() != content.encode("utf-8"):
        raise EvaluationError("stored stdout differs from the native response content")
    return manifest, receipt, stdout
