"""Remote engine bridge tests: mocked transport only, no network, no real key.

The GLM_SMOKE=1 real smoke is executed out-of-band (see
reports/execution/P0-03/answer-engine-extension-001/smoke-001/), never here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import answer_eval.remote_engine as remote_engine
import execute_answer_jobs as coordinator
from acceptance_lib.evidence import read_json, write_json
from answer_eval.completion_material import ANSWER_FIELDS, checked_answer, check_complete_records
from answer_eval.preparation import ARMS, load_preparation
from answer_eval.protocol import EvaluationError, canonical_json, canonical_sha256, load_run
from answer_eval.remote_engine import (ENGINE_KIND, chat_url, engine_binding, judge_output,
                                       key_fingerprint, load_engine, render_request,
                                       require_api_key, validate_engine,
                                       verify_http_manifest)
from answer_preparation_fixtures import fixture_plan, materials, production

TEST_KEY = "unit-test-key-material"


def make_engine(**overrides):
    engine = {"schema": 1, "kind": ENGINE_KIND, "base_url": "https://api.example.test/api/paas/v4",
              "model_id": "GLM-5.3-Flash", "api_key_sha256": key_fingerprint(TEST_KEY),
              "settings": {"temperature": 0.0, "top_p": 1.0, "stream": False},
              "request_timeout_seconds": 300}
    engine.update(overrides)
    return engine


@pytest.fixture(autouse=True)
def engine_key(monkeypatch):
    monkeypatch.setenv("GLM_KEY", TEST_KEY)
    monkeypatch.delenv("GLM_SMOKE", raising=False)


class Transport:
    """In-process fake chat-completions endpoint; records every call."""

    def __init__(self):
        self.calls = []
        self.responses = []  # per-call overrides

    def __call__(self, url, request_bytes, key, timeout):
        self.calls.append((url, request_bytes, key, timeout))
        if self.responses:
            return self.responses.pop(0)
        payload = json.loads(request_bytes)
        content = payload["messages"][0]["content"]
        facts = None
        for line in content.splitlines():
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if isinstance(candidate, dict) and "required_facts" in candidate:
                facts = candidate
        if facts is not None:  # judge prompt: echo a protocol-valid strict verdict
            content = canonical_json({"score": 0, "refused": False, "fabricated_fact": False,
                                      "fact_results": [{"fact_id": f["fact_id"], "supported": False,
                                                        "evidence_ids": []} for f in facts["required_facts"]]})
        else:
            content = "Synthetic mocked grounded answer."
        body = {"id": "chatcmpl-unit", "object": "chat.completion", "model": "glm-5.3-flash",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
        return remote_engine.HttpExchange(200, json.dumps(body).encode("utf-8"), None, False)


@pytest.fixture
def transport(monkeypatch):
    fake = Transport()
    monkeypatch.setattr(remote_engine, "http_exchange", fake)
    return fake


def test_request_rendering_and_binding_are_frozen():
    engine = make_engine()
    assert render_request(engine, "PROMPT") == {"model": "GLM-5.3-Flash", "stream": False,
        "messages": [{"role": "user", "content": "PROMPT"}], "temperature": 0.0, "top_p": 1.0}
    assert chat_url(engine) == engine["base_url"] + "/chat/completions"
    assert engine_binding(engine) == {"model_id": engine["model_id"],
                                      "model_sha256": canonical_sha256(engine)}


@pytest.mark.parametrize("edit, match", [
    (lambda e: e.update(kind="remote-openai-chat/v2"), "remote-openai-chat/v1"),
    (lambda e: e.update(extra=1), "unknown"),
    (lambda e: e.update(base_url="http://insecure.test/"), "https URL"),
    (lambda e: e.update(api_key_sha256="not-a-sha"), "SHA256"),
    (lambda e: e["settings"].update(stream=True), "stream"),
    (lambda e: e["settings"].update(temperature=2.5), "temperature"),
    (lambda e: e.update(request_timeout_seconds=0), "positive"),
])
def test_engine_registration_rejects_invalid_forms(edit, match):
    engine = make_engine()
    edit(engine)
    with pytest.raises(EvaluationError, match=match):
        validate_engine(engine)


def test_key_fingerprint_binds_without_persisting_the_key(monkeypatch, tmp_path):
    engine = make_engine()
    assert key_fingerprint(TEST_KEY) != TEST_KEY and len(key_fingerprint(TEST_KEY)) == 64
    assert require_api_key(engine) == TEST_KEY
    path = tmp_path / "engine.json"
    write_json(path, engine)
    assert load_engine(path) == engine
    monkeypatch.setenv("GLM_KEY", "another-key")
    with pytest.raises(EvaluationError, match="fingerprint does not match"):
        require_api_key(engine)
    monkeypatch.delenv("GLM_KEY")
    with pytest.raises(EvaluationError, match="GLM_KEY"):
        require_api_key(engine)
    assert TEST_KEY not in path.read_text(encoding="utf-8")


def execute_smoke_answer(tmp_path, engine):
    run, prepared, question, prep = coordinator.build_smoke_layout(tmp_path / "smoke", engine)
    job = read_json(run / "jobs" / f"{coordinator.SMOKE_QID}-a1.json")
    result = coordinator.execute_job(prep, run, prepared, job, engine, TEST_KEY)
    return run, prepared, question, prep, job, result


def test_answer_job_records_native_usage_and_sidecar(tmp_path, transport):
    engine = make_engine()
    run, prepared, question, prep, job, result = execute_smoke_answer(tmp_path, engine)
    assert result["status"] == "JOB_EXECUTED" and result["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert result["http"]["status"] == 200 and result["http"]["finish_reason"] == "stop"
    raw = run / "raw" / "answers_a1" / coordinator.SMOKE_QID
    manifest, receipt, stdout = verify_http_manifest(raw, job)
    assert result["execution_id"] == manifest["run_id"] == receipt["run_id"]
    assert read_json(raw / "usage.json") == {"input_tokens": 11, "output_tokens": 7}
    prompt = (run / "requests_a1" / (coordinator.SMOKE_QID + ".txt")).read_text(encoding="utf-8")
    assert json.loads((raw / "http_request.json").read_text(encoding="utf-8")) == render_request(engine, prompt)
    assert stdout.read_bytes() == (run / "answers_a1" / (coordinator.SMOKE_QID + ".md")).read_bytes()
    sidecar = read_json(run / "answers_a1" / (coordinator.SMOKE_QID + ".json"))
    assert set(sidecar) == ANSWER_FIELDS
    assert sidecar["execution_id"] == manifest["run_id"] and sidecar["completed_at"] == receipt["finished_at"]
    assert sidecar["answerer_model"] == engine_binding(engine) and sidecar["answerer_settings"] == engine["settings"]
    checked_answer(prep, run, prepared, coordinator.SMOKE_QID, "a1")  # frozen reader accepts it
    for path in sorted(run.rglob("*")):  # no key material ever reached disk
        if path.is_file():
            assert TEST_KEY not in path.read_text(encoding="utf-8", errors="ignore")


def answer_only_body(content="text", **usage):
    body = {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = usage
    return json.dumps(body).encode("utf-8")


@pytest.mark.parametrize("exchange, failure", [
    (remote_engine.HttpExchange(200, answer_only_body(), None, False), "MISSING_USAGE"),
    (remote_engine.HttpExchange(200, answer_only_body(prompt_tokens=5, completion_tokens=None), None, False), "MISSING_USAGE"),
    (remote_engine.HttpExchange(500, b"server exploded", "HTTP 500", False), "HTTP_ERROR"),
    (remote_engine.HttpExchange(0, b"", "timeout", True), "TIMED_OUT"),
    (remote_engine.HttpExchange(200, b"<html>not json</html>", None, False), "INVALID_RESPONSE"),
])
def test_failures_are_recorded_once_without_sidecars(tmp_path, monkeypatch, exchange, failure):
    engine = make_engine()
    calls = []

    def one_shot(url, request_bytes, key, timeout):
        calls.append(url)
        return exchange

    monkeypatch.setattr(remote_engine, "http_exchange", one_shot)
    run, prepared, question, prep, job, result = execute_smoke_answer(tmp_path, engine)
    assert result["status"] == "FAILED" and result["failure"] == failure
    assert len(calls) == 1 and result["retries"] == 0
    qid = coordinator.SMOKE_QID
    assert not (run / "answers_a1" / (qid + ".md")).exists()
    assert not (run / "answers_a1" / (qid + ".json")).exists()
    assert not (run / "raw" / "answers_a1" / qid).exists()
    record = read_json(Path(result["failure_record"]["path"]))
    assert record["kind"] == "answer-eval.http-failure/v1" and record["job_id"] == job["job_id"]


def test_judge_output_normalization_is_strict():
    question = {"id": "q", "class": "single", "q": "?", "source_group": "g", "expect": [], "required_facts":
                [{"fact_id": "f0", "description": "d", "sufficient_evidence": [["e0"]]}]}
    good = {"score": 1, "refused": False, "fabricated_fact": True,
            "fact_results": [{"fact_id": "f0", "supported": False, "evidence_ids": []}]}
    assert judge_output(canonical_json(good), question) == good
    for bad in ("```json\n" + canonical_json(good) + "\n```",            # fenced
                canonical_json(dict(good, score="1")),                    # wrong type
                canonical_json(dict(good, fact_results=[])),              # incomplete facts
                canonical_json(dict(good, extra=1))):                     # extra key
        with pytest.raises(EvaluationError):
            judge_output(bad, question)


def remote_plan(root, engine):
    """Fixture plan whose runtime registers the remote engine identity."""
    from answer_eval.protocol import sha256_file
    path = fixture_plan(root)
    plan = read_json(path)
    binding = engine_binding(engine)
    for role in ("answerer", "judge"):
        plan["common"]["models"][role] = dict(binding)
        plan["common"]["settings"][role] = dict(engine["settings"])
        plan["common"]["runtime"][role] = {"engine": engine}
    prereg_path = root / "preregistration.json"
    prereg = read_json(prereg_path)
    prereg["models"] = plan["common"]["models"]
    prereg["settings"] = plan["common"]["settings"]
    write_json(prereg_path, prereg)
    plan["common"]["preregistration"] = {"path": str(prereg_path.resolve()), "sha256": sha256_file(prereg_path)}
    write_json(path, plan)
    return path


@pytest.mark.parametrize("edit, match", [
    (lambda p: p["common"]["models"]["answerer"].update(model_sha256="0" * 64), "model binding"),
    (lambda p: p["common"]["settings"]["answerer"].update(temperature=1.5), "frozen remote engine settings"),
    (lambda p: p["common"]["runtime"]["answerer"].update(model_file={"path": "/nope", "sha256": "0" * 64}), "unknown"),
    (lambda p: p["common"]["runtime"]["answerer"].update(engine=make_engine(model_id="GLM-9-Flash")), "model binding"),
])
def test_remote_runtime_preparation_binding(tmp_path, edit, match):
    from answer_eval.protocol import sha256_file
    engine = make_engine()
    path = remote_plan(tmp_path / "inputs", engine)
    assert load_preparation(path, require_ready=True).common["runtime"]["answerer"] == {"engine": engine}
    plan = read_json(path)
    edit(plan)
    prereg_path = tmp_path / "inputs" / "preregistration.json"
    prereg = read_json(prereg_path)
    prereg["models"] = plan["common"]["models"]
    prereg["settings"] = plan["common"]["settings"]
    write_json(prereg_path, prereg)
    plan["common"]["preregistration"]["sha256"] = sha256_file(prereg_path)
    write_json(path, plan)
    with pytest.raises(EvaluationError, match=match):
        load_preparation(path, require_ready=True)


def test_local_runtime_plan_still_loads(tmp_path):
    """The additive remote branch leaves the frozen local-file form untouched."""
    prep = load_preparation(fixture_plan(tmp_path / "inputs"), require_ready=True)
    assert set(prep.common["runtime"]["answerer"]) == {"engine", "model_file"}


@pytest.fixture
def remote_study(tmp_path, monkeypatch):
    import answer_eval.job_material as jobs
    import product_eval.runner
    monkeypatch.setattr(jobs, "implementation", lambda root: {})
    monkeypatch.setattr(product_eval.runner, "production_identity",
                        lambda root, reference_run=False: production())
    engine = make_engine()
    plan = remote_plan(tmp_path / "inputs", engine)
    prep = load_preparation(plan, require_ready=True)
    root = tmp_path / "evaluator"
    reference = materials(prep, "reference", tmp_path / "reference", root)
    candidate = materials(prep, "candidate", tmp_path / "candidate", root)
    study = tmp_path / "study"
    jobs.prepare_jobs(plan, reference, candidate, study, root=root)
    return study, engine


def test_full_synthetic_study_executes_and_validates(tmp_path, transport, remote_study):
    """480 mocked answers + 960 mocked judgments produce load_run-valid v3 runs."""
    study, engine = remote_study
    prep = load_preparation(study / "plan.json", require_ready=True)
    for arm in ARMS:
        for qid in prep.questions:
            for a in ("a1", "a2"):
                result = coordinator.execute_question(study, arm, qid, a, engine)
                assert result["status"] == "QUESTION_EXECUTED", result
                assert result["genuine_completions"] == 3
        finalized = coordinator.finalize_run(study, arm)
        assert finalized["status"] == "RUN_FINALIZED" and finalized["records"] == 480
    assert len(transport.calls) == 1440
    ids = set()
    for arm in ARMS:
        run = Path(read_json(study / "study.json")["runs"][arm]["path"])
        loaded = load_run(run)  # frozen schema3 validation of every sidecar family
        assert len(loaded.execution_ids) == 480
        ids |= loaded.execution_ids
    assert len(ids) == 1440
    report = check_complete_records(study)
    assert report["status"] == "MISSING"  # execution review stays external by design
    assert (report["record_consistency"], report["answer_records"],
            report["judge_records"], report["distinct_execution_ids"]) == ("COMPLETE_DISK_RECORDS", 480, 960, 1440)


def test_execute_question_never_overwrites(tmp_path, transport, remote_study):
    study, engine = remote_study
    prep = load_preparation(study / "plan.json", require_ready=True)
    qid = next(iter(prep.questions))
    assert coordinator.execute_question(study, ARMS[0], qid, "a1", engine)["status"] == "QUESTION_EXECUTED"
    with pytest.raises(EvaluationError, match="never overwrit"):
        coordinator.execute_question(study, ARMS[0], qid, "a1", engine)


def test_smoke_gate_blocks_real_calls_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("GLM_SMOKE", raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("network must not be touched without GLM_SMOKE=1")

    monkeypatch.setattr(remote_engine, "http_exchange", no_network)
    result = coordinator.run_smoke(tmp_path / "smoke", make_engine())
    assert result == {"status": "SMOKE_DISABLED", "error": "set GLM_SMOKE=1 to allow real remote calls",
                      "network_calls": 0, "release_eligible": False}
    assert not (tmp_path / "smoke").exists()
