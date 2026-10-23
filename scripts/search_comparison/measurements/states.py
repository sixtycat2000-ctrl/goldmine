"""Fixed private state layouts/configs; no engine source or global cache writes."""
import json
from pathlib import Path

from answer_eval.protocol import EvaluationError
from search_comparison.engine_capture.settings import setup
from search_comparison.engine_capture.resources import configuration


def configure(plan, view, arm, cell, state, *, replay=False):
    owner = cell / state
    if not replay:
        owner.mkdir(parents=True)
    engine = arm.split(".")[0]
    values, env = setup(owner, engine, plan.engines[arm], view, replay=replay)
    values["semantic"] = arm in {"qmd.vsearch", "qmd.query"}
    if engine == "goldmine":
        values["embedding_sha256"] = plan.value["engines"][engine]["model_roles"]["embedding"]["sha256"]
    contract = configuration(engine, values, view)
    if engine == "qmd" and state.startswith("states/"):
        # A copied ready database needs its actual collection configuration,
        # rather than a fake collection-add over an already prepared database.
        path = Path(contract["path"])
        text = json.dumps(contract["value"], ensure_ascii=False, sort_keys=True) + "\n"
        if replay:
            if path.read_text() != text:
                raise EvaluationError("ready copied QMD state config changed")
        else:
            path.write_text(text)
    return {"state": state, "values": values, "env": env, "configuration": contract, "view": view}


def index(state, engine):
    return state["values"][{"goldmine": "goldmine_db", "qmd": "qmd_db", "recoll": "recoll_db"}[engine]]


def for_cell(ledger, plan, view, arm, state):
    cache = getattr(ledger, "state_configs", {})
    if state not in cache:
        cache[state] = configure(plan, view, arm, ledger.owner, state, replay=ledger.replay is not None)
        ledger.state_configs = cache
    elif cache[state]["view"]["corpus"] != view["corpus"] or cache[state]["view"]["name"] != view["name"]:
        raise EvaluationError("fixed state was rebound to a different physical corpus/view")
    cache[state]["view"] = view
    return cache[state]


def changed_configuration(ledger, state):
    """The full environment/config for each actual state is bound per call."""
    ledger.commands.env = state["env"]
    ledger.commands.context = {**ledger.commands.context, "configuration_contract": state["configuration"],
                               "state": state["state"], "working_corpus": state["view"]["corpus"]}
