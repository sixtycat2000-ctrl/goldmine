"""Explicit candidate arms reuse the application pipeline inside one RO snapshot."""
from answer_eval.protocol import EvaluationError, fields


def validate_parameters(parameters, *, reference_run):
    fields(parameters, {"scorer", "z_gate", "limit", "context", "budget"}, "search parameters",
           {"mode", "rerank", "multi_query", "admission"})
    mode, rerank = parameters.get("mode", "fast"), parameters.get("rerank", "off")
    multi = parameters.get("multi_query", False)
    if (mode not in ("fast", "quality") or rerank not in ("off", "local") or type(multi) is not bool
            or (mode == "fast" and (rerank != "off" or multi))):
        raise EvaluationError("invalid quality-mode parameter combination")
    admission = parameters.get("admission", {})
    fields(admission, set(), "admission parameters", {"coverage_quorum", "semantic_segment_margin"})
    if reference_run and (mode != "fast" or rerank != "off" or multi or admission):
        raise EvaluationError("original reference cannot run candidate-only quality/admission arms")


def retrieve(store, backend, cfg, query, filters, parameters):
    from goldmine.admission import AdmissionParameters
    from goldmine.application import Application
    from goldmine.contracts import SearchRequest
    from goldmine.deadline import request_timeout

    admission = AdmissionParameters(**parameters.get("admission", {}))
    request = SearchRequest(query=query, filters=filters, require_citations=True, pack=True,
        **{key: value for key, value in parameters.items() if key != "admission"})
    def reranker():
        from goldmine.rerank import from_config

        return from_config(cfg)
    app = Application(store.path, reranker_factory=reranker, admission_parameters=admission)
    with request_timeout(10 if request.mode == "quality" else 5):
        hits, settings, fallback = app._retrieve(store, backend, request, request.scorer)
        text, meta = app._pack(store, backend, request, hits)
    meta["quality_settings"], meta["fallback_reason"] = settings, fallback
    return hits, text, meta
