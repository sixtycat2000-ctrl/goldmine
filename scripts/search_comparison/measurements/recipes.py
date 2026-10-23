"""Only reviewed installed CLI operations; never a caller-provided command hook."""
from answer_eval.protocol import EvaluationError
from search_comparison.engine_capture.recipes import compile_recipe
from .selection import ARMS

SEMANTIC_QMD = {"qmd.vsearch", "qmd.query"}


def ordinary_update(arm, values):
    if arm not in ARMS:
        raise EvaluationError("unregistered incremental arm")
    engine = arm.split(".")[0]
    if engine == "goldmine":
        return [compile_recipe("goldmine.prepare", values)]
    if engine == "recoll":
        return [compile_recipe("recoll.prepare", values)]
    if engine == "qmd":
        needed = ("node", "qmd_launcher", "qmd_index")
        if any(k not in values or not isinstance(values[k], str) or not values[k] or "\x00" in values[k] for k in needed):
            raise EvaluationError("unresolved fixed QMD update role")
        commands = [[values["node"], values["qmd_launcher"], "--index", values["qmd_index"], "update"]]
        if arm in SEMANTIC_QMD:
            commands.append(compile_recipe("qmd.embed", values))
        elif arm != "qmd.search":
            raise EvaluationError("unregistered QMD update arm")
        return commands
    raise EvaluationError("unregistered incremental engine")


def query(arm, values, text):
    if arm not in ARMS:
        raise EvaluationError("unregistered measurement query arm")
    if not isinstance(text, str) or not text or "\x00" in text:
        raise EvaluationError("one literal nonempty query required")
    return compile_recipe(arm, {**values, "query": text})


def operations(cohorts):
    """Fixed typed operations for future execution, not a free command graph."""
    if len(cohorts["base112"]) != 112 or len(cohorts["add10"]) != 10:
        raise EvaluationError("fixed112+10 cohort required")
    return [
        {"kind": "materialize_base", "paths": cohorts["base112"], "expected_count": 112},
        {"kind": "ordinary_prepare", "stage": "base112"},
        {"kind": "readonly_audit_and_closed_snapshot", "stage": "base112", "expected_count": 112},
        {"kind": "add_unchanged_sources", "paths": cohorts["add10"], "expected_count": 122},
        {"kind": "ordinary_update", "stage": "incremental122"},
        {"kind": "readonly_audit_and_closed_snapshot", "stage": "incremental122", "expected_count": 122},
        {"kind": "independent_fresh_prepare_same_physical_corpus", "stage": "fresh122"},
        {"kind": "readonly_audit_and_closed_snapshot", "stage": "fresh122", "expected_count": 122},
        {"kind": "compare_logical_signatures", "left": "incremental122", "right": "fresh122"},
        {"kind": "separate_lifecycle_copy_prepare", "expected_count": 122},
        {"kind": "remove_owned_source", "path": cohorts["remove"], "expected_count": 121},
        {"kind": "ordinary_update", "stage": "removed121"},
        {"kind": "readonly_audit_and_closed_snapshot", "stage": "removed121", "expected_count": 121},
        {"kind": "restore_original_and_replace_owned_source", "restore": cohorts["remove"],
         "target": cohorts["replace_target"], "donor": cohorts["replace_donor"], "expected_count": 122},
        {"kind": "ordinary_update", "stage": "replaced122"},
        {"kind": "readonly_audit_and_closed_snapshot", "stage": "replaced122", "expected_count": 122},
        {"kind": "independent_fresh_prepare_same_physical_corpus", "stage": "replacement_fresh122"},
        {"kind": "readonly_audit_and_closed_snapshot", "stage": "replacement_fresh122", "expected_count": 122},
        {"kind": "compare_logical_signatures", "left": "replaced122", "right": "replacement_fresh122"},
    ]
