#!/usr/bin/env python3
"""Compile a frozen dev-only measurement packet; never execute an engine."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from answer_eval.protocol import EvaluationError
from search_comparison.engine_capture.plan import load_plan
from search_comparison.measurements.packet import compile_packet
from search_comparison.source_views.run import owned_output, write_new


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--plan-sha256", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--parent", required=True, type=Path)
    a=p.parse_args(argv)
    try:
        plan=load_plan(a.plan,a.plan_sha256)
        owner=owned_output(a.out,a.parent,plan.bundle)
        packet=compile_packet(plan)
        protected=[plan.path,Path(plan.projection["reference"]["path"]).parent.parent]
        for engine in plan.value["engines"].values():
            if engine:
                for k in ("installation","python_environment","models","dependencies"):
                    if k in engine: protected.append(Path(engine[k]["path"]))
                protected.extend(Path(t["path"]) for t in engine["tools"].values())
                for role in engine.get("model_roles",{}).values():
                    model=role.get("resource",role)
                    protected.append(Path(model["path"]).parent)
        protected.extend(Path(v["corpus"]) for v in plan.views.values())
        if any(owner.is_relative_to(x) or x.is_relative_to(owner) for x in protected):
            raise EvaluationError("packet output overlaps protected engine/source/projection inputs")
        owner.mkdir()
        write_new(owner/"execution-packet.json",packet)
        print(json.dumps({"status":"EXECUTION_PACKET_ONLY","executor_status":packet["executor"]["status"],"denominators":packet["denominators"],"quality_evidence":False,"release_eligible":False}))
        return 0
    except (EvaluationError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({"status":"MISSING","error":str(exc),"release_eligible":False}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())
