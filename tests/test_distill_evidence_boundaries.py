"""Independent tiny mechanical counterexamples; never real distill quality evidence."""
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from acceptance_lib.evidence import InvalidEvidence, artifact, capture_command, sha256, write_json
from acceptance_lib import distill_evidence, owned_process
from distill_eval import preparation
from distill_eval.identity import evaluator_identity
from distill_eval.scoring import excerpt_audit
from goldmine.citations import read_evidence
from goldmine.distill import Card, CItem, render_json
from goldmine.store import FileRec, SegmentRec, Store
from test_distill_preparation import preparation_fixture

TREE = Path(preparation.__file__).resolve().parents[2]
TEXT = "A literal source statement with a stable sentence and location."


def _stored(tmp_path):
    store = Store(tmp_path / "tiny.sqlite")
    path = str(tmp_path / "genuine.txt")
    fid = store.upsert_file(FileRec(path, kind="doc", sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
                                    state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec("chunk", "text", loc=2, loc_end=3,
                                           excerpt=TEXT, text_for_fts=TEXT, original_fts=TEXT),
                                  SegmentRec("chunk", "text", loc=4, loc_end=5,
                                             excerpt=TEXT, text_for_fts=TEXT, original_fts=TEXT)])
    sids = [r[0] for r in store.db.execute("SELECT id FROM segments WHERE file=? AND kind='chunk' ORDER BY id", (fid,))]
    members = [CItem(sid, fid, np.zeros(768), TEXT, path, "doc", loc, end)
               for sid, loc, end in zip(sids, (2, 4), (3, 5))]
    return store, members


def _card(member, status, members=None):
    excerpt = {"text": TEXT, "path": member.path, "kind": member.kind, "loc": member.loc,
               "loc_end": member.loc_end, "role": "primary"}
    return Card("source", "terms", ["source"], [excerpt], members or [member], 1,
                status=status, topic_id=7)


@pytest.mark.parametrize("status", ["active", "header"])
def test_authentic_actual_store_citation_control(tmp_path, status):
    store, members = _stored(tmp_path)
    with closing(store):
        card = _card(members[0], status)
        actual = read_evidence(store, members[0].sid)
        assert actual.text == TEXT and actual.citation.path == members[0].path
        report = excerpt_audit(store, [card])
        assert report["checked"] == report["passed"] == 1
        assert report["fixed_card_review"][0]["verdict"] == "PASS"


@pytest.mark.parametrize("status", ["active", "header"])
@pytest.mark.parametrize("field,value", [("path", "/counterfeit/source.txt"), ("kind", "pdf"),
                                         ("loc", 999), ("loc_end", 999), ("fid", 999)])
def test_coordinated_member_excerpt_lie_is_rejected(tmp_path, status, field, value):
    store, members = _stored(tmp_path)
    with closing(store):
        member = copy.copy(members[0])
        setattr(member, field, value)
        card = _card(member, status)
        # A real opaque citation still points to the genuine stored metadata.
        actual = read_evidence(store, member.sid)
        assert actual.text == TEXT
        assert getattr(actual.citation, "file_id" if field == "fid" else field) != value
        emitted = render_json([card], {})["cards"][0]
        if field != "fid":
            assert emitted["excerpts"][0][field] == value and emitted["citations"][0][field] == value
        report = excerpt_audit(store, [card])
        assert report["passed"] < report["checked"] or report["fixed_card_review"][0]["verdict"] == "FAIL", report


@pytest.mark.parametrize("status", ["active", "header"])
def test_nonexcerpt_emitted_member_citation_is_rejected(tmp_path, status):
    store, members = _stored(tmp_path)
    with closing(store):
        members[1].path = "/counterfeit/nonexcerpt-source.txt"
        card = _card(members[0], status, members)
        assert render_json([card], {})["cards"][0]["citations"][1]["path"] == members[1].path
        assert read_evidence(store, members[1].sid).citation.path != members[1].path
        report = excerpt_audit(store, [card])
        assert report["passed"] < report["checked"] or report["fixed_card_review"][0]["verdict"] == "FAIL", report


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_before_popen_handle_handoff_cleans_owned_group(tmp_path, monkeypatch, signum):
    actual_popen = owned_process.subprocess.Popen
    children = []
    def interrupted_handoff(*args, **kwargs):
        proc = actual_popen(*args, **kwargs)
        # Inject at the owned target's handoff only. A legitimate Darwin
        # cleanup census also uses Popen, but does not create a new session.
        if kwargs.get("start_new_session") is True:
            children.append(proc)
            os.kill(os.getpid(), signum)
        return proc
    monkeypatch.setattr(owned_process.subprocess, "Popen", interrupted_handoff)
    run = tmp_path / "interrupted-handoff"
    try:
        capture_command(run, "mechanical.independent-handoff", [sys.executable, "-c", "import time; time.sleep(30)"],
                        cwd=tmp_path, timeout=1, owned_process_group=True)
        receipt = json.loads((run / "receipt.json").read_text())
        assert receipt["exit_code"] == 128 + signum and not receipt["timed_out"]
        assert len(children) == 1
        assert children[0].returncode is not None, "owned child leaked before Popen handle assignment"
    finally:
        # Baseline intentionally fails. Kill the exact newly owned group before
        # reaping its leader; never signal a reaped/reusable PGID.
        for proc in children:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=1)


def test_assets_list_fails_as_invalid_evidence(preparation_fixture):
    path, document, labels = preparation_fixture
    document["assets"] = ["python", "model", "server", "ocr"]
    write_json(path, document)
    with pytest.raises(InvalidEvidence):
        preparation.validate_preparation(path, path.parent / "base.sqlite", labels,
                                         path.parent / "corpus", path.parent / "incremental.sqlite")


def _raw_run(out, identity, argv, report):
    out.mkdir()
    stdout, stderr = out / "stdout", out / "stderr"
    write_json(stdout, report)
    stderr.write_text("SYNTHETIC MALFORMED-INPUT MECHANICS ONLY; no quality command executed.\n")
    receipt = {"schema": 1, "run_id": "mechanical-independent", "check_id": "release.distill", "argv": argv,
               "cwd": str(out), "env": {}, "started_at": "2026-10-21T01:00:00+00:00",
               "finished_at": "2026-10-21T01:00:00+00:00", "exit_code": 0, "timed_out": False,
               "stdout": artifact(out, stdout), "stderr": artifact(out, stderr), "artifacts": {}}
    write_json(out / "receipt.json", receipt)
    write_json(out / "manifest.json", {"schema": 1, "kind": "goldmine.command-run", "run_id": receipt["run_id"],
                                      "check_id": "release.distill", "suite_id": "distill.real.v1", "identity": identity,
                                      "receipt": artifact(out, out / "receipt.json")})
    return out / "manifest.json"


def test_manifest_identity_list_fails_as_invalid_evidence(tmp_path):
    manifest = _raw_run(tmp_path / "raw", [], [sys.executable, "mechanical-invalid-script"], {})
    with pytest.raises(InvalidEvidence):
        distill_evidence.check(SimpleNamespace(file=lambda *args, **kwargs: manifest), {})


def test_report_fingerprint_list_fails_as_invalid_evidence(tmp_path, preparation_fixture):
    path, document, labels = preparation_fixture
    out = path.parent
    proof = preparation.validate_preparation(path, out / "base.sqlite", labels, out / "corpus", out / "incremental.sqlite")
    argv = [proof["python_path"], str(TREE / "scripts/eval_distill.py"), "--report-real", str(out / "base.sqlite"), "--json",
            "--labels", str(labels), "--corpus-root", str(out / "corpus"), "--incremental-db", str(out / "incremental.sqlite"),
            "--preparation", str(path)]
    manifest = _raw_run(tmp_path / "raw", {"source_files": evaluator_identity(TREE),
                        "tool_sha256": sha256(TREE / "scripts/eval_distill.py"), "preparation": proof}, argv,
                        {"schema": 2, "kind": "goldmine.distill-real", "evidence_mode": "real", "fingerprint": []})
    model = Path(document["assets"]["model"]["path"])
    inputs = SimpleNamespace(file=lambda key, **kwargs: model if key == "models.embedding" else manifest)
    with pytest.raises(InvalidEvidence):
        distill_evidence.check(inputs, {})
