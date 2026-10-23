"""One joint exposure, fixed at-most-once arm starts, irreversible crash state."""
from datetime import datetime, timezone
from pathlib import Path

from answer_eval.protocol import EvaluationError, canonical_json, canonical_sha256, fields, sha
from product_eval.ledger import _atomic_json, _lock, _read, authorize_holdout


def _append(root, state, payload):
    record = {"schema": 1, "sequence": state["count"], "previous_sha256": state["head_sha256"], "payload": payload}
    record["sha256"] = canonical_sha256(record)
    import os

    with (root / "holdout_exposure.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    _atomic_json(root / "state.json", {"schema": 1, "anchor_sha256": state["anchor_sha256"], "head_sha256": record["sha256"], "count": state["count"] + 1})
    return record


class JointAuthorization:
    def __init__(self, frozen, order):
        self.frozen, self.order = frozen, order
        self.root = Path(frozen["ledger"]["path"]).resolve()
        authorization = frozen["authorization"]
        # The existing strict ledger consumes a prior0 exposure before entering
        # ANY arm. Crashes after this line are spent, even with zero final rows.
        self.exposure = authorize_holdout(self.root, authorization, identity=frozen["bundle_identity"],
            candidate_code_sha256=authorization["candidate_code_sha256"], parameters_sha256=authorization["parameters_sha256"],
            dependency_lock_sha256=authorization["dependency_lock_sha256"])
        self.digest = canonical_sha256(frozen)
        self.current = None
        self.started = False

    def select(self, arm):
        if arm not in self.order:
            raise EvaluationError("undeclared joint arm")
        self.current, self.started = arm, False

    def __call__(self, ledger, freeze, *, identity, candidate_code_sha256, parameters_sha256, dependency_lock_sha256, recheck=False):
        if (self.current is None or self.started or recheck or Path(ledger).resolve() != self.root
                or freeze != self.frozen["authorization"] or identity != self.frozen["bundle_identity"]
                or candidate_code_sha256 != freeze["candidate_code_sha256"]
                or dependency_lock_sha256 != freeze["dependency_lock_sha256"]
                or parameters_sha256 != self.frozen["arms"][self.current]["parameters_sha256"]):
            raise EvaluationError("joint arm authorization differs from the frozen actual request")
        with _lock(self.root):
            state, records = _read(self.root)
            started = [r["payload"]["arm"] for r in records if r["payload"].get("event") == "joint_arm_started"]
            completed = [r["payload"]["arm"] for r in records if r["payload"].get("event") == "joint_arm_completed"]
            if (records[1] != self.exposure or started != completed or self.order[:len(started)] != started
                    or len(started) >= len(self.order) or self.order[len(started)] != self.current
                    or any(r["payload"].get("event") not in {"initialized", "holdout_exposed", "joint_arm_started", "joint_arm_completed"} for r in records)):
                raise EvaluationError("joint audit cannot restart, repeat or replace a spent arm")
            _append(self.root, state, {"event": "joint_arm_started", "arm": self.current, "at": datetime.now(timezone.utc).isoformat(),
                "joint_freeze_sha256": self.digest, "exposure_sha256": self.exposure["sha256"], "parameters_sha256": parameters_sha256})
        self.started = True
        return self.exposure

    def complete(self, report_sha256, usage_sha256):
        if not self.started:
            raise EvaluationError("joint arm cannot complete without its pre-query start event")
        with _lock(self.root):
            state, records = _read(self.root)
            last = records[-1]["payload"]
            if last.get("event") != "joint_arm_started" or last.get("arm") != self.current or last.get("joint_freeze_sha256") != self.digest:
                raise EvaluationError("joint arm completion does not follow its unique start")
            _append(self.root, state, {"event": "joint_arm_completed", "arm": self.current, "at": datetime.now(timezone.utc).isoformat(),
                "joint_freeze_sha256": self.digest, "report_sha256": report_sha256, "usage_sha256": usage_sha256})

    def finish(self):
        with _lock(self.root):
            state, records = _read(self.root)
            completed = [r["payload"].get("arm") for r in records if r["payload"].get("event") == "joint_arm_completed"]
            if completed != self.order or records[-1]["payload"].get("event") != "joint_arm_completed":
                raise EvaluationError("joint audit is incomplete; no partial denominator acceptance")
            record = _append(self.root, state, {"event": "joint_audit_completed", "at": datetime.now(timezone.utc).isoformat(),
                "joint_freeze_sha256": self.digest, "arms": self.order})
        return record


def verify_chain(frozen, order, audit):
    """Read only: no recheck, new exposure, reset or inferred skipped arm."""
    root = Path(frozen["ledger"]["path"])
    state, records = _read(root)
    if len(records) != 3 + 2 * len(order) or state["anchor_sha256"] != frozen["ledger"]["anchor_sha256"]:
        raise EvaluationError("joint ledger is incomplete/reset or has extra/repeated exposures")
    fields(records[0]["payload"], {"event", "nonce", "created_at"}, "joint ledger initialization")
    if records[0]["payload"]["event"] != "initialized":
        raise EvaluationError("joint ledger initialization event differs")
    sha(records[0]["payload"]["nonce"], "joint ledger initialization nonce")
    authorization, exposure = frozen["authorization"], records[1]
    payload = exposure["payload"]
    expected = {"event": "holdout_exposed", "audited_at": payload.get("audited_at"),
        "freeze_sha256": canonical_sha256(authorization), "candidate_code_sha256": authorization["candidate_code_sha256"],
        "parameters_sha256": authorization["parameters_sha256"], "dependency_lock_sha256": authorization["dependency_lock_sha256"],
        "bundle_identity": frozen["bundle_identity"], "purpose": "final_joint_release_audit", "result_scope": "aggregate_and_per_query", "prior_exposures": 0}
    if payload != expected or audit["exposure"] != exposure:
        raise EvaluationError("joint audit lacks its actual prior0 pre-query exposure")
    digest = canonical_sha256(frozen)
    for index, arm in enumerate(order):
        start, complete = (record["payload"] for record in records[2 + 2 * index:4 + 2 * index])
        if start != {"event": "joint_arm_started", "arm": arm, "at": start.get("at"), "joint_freeze_sha256": digest,
                     "exposure_sha256": exposure["sha256"], "parameters_sha256": frozen["arms"][arm]["parameters_sha256"]}:
            raise EvaluationError("joint arm start was reordered/repeated or changed parameters")
        refs = audit["arms"][arm]
        if complete != {"event": "joint_arm_completed", "arm": arm, "at": complete.get("at"), "joint_freeze_sha256": digest,
                        "report_sha256": refs["report"]["sha256"], "usage_sha256": refs["usage"]["sha256"]}:
            raise EvaluationError("joint arm raw artifacts differ from the irreversible completed-arm ledger")
    final = records[-1]["payload"]
    if final != {"event": "joint_audit_completed", "at": final.get("at"), "joint_freeze_sha256": digest, "arms": order}:
        raise EvaluationError("joint final event differs from the fixed complete registry")
    from answer_eval.protocol import timestamp

    times = [frozen["frozen_at"], payload["audited_at"], *[r["payload"]["at"] for r in records[2:]]]
    stamps = [timestamp(value, "joint event timestamp") for value in times]
    if stamps != sorted(stamps):
        raise EvaluationError("joint exposure/start/completion timestamps are not causal")
    return {"anchor_sha256": state["anchor_sha256"], "head_sha256": state["head_sha256"], "prior_exposures": 0,
            "arms": order, "exposure_at": payload["audited_at"], "completed_at": final["at"]}
