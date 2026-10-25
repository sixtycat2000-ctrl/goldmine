"""Read-only evidence operations shared by command and protocol adapters.

Each call owns its Store and temporary backend. A single read transaction spans
ranking, text, citations and packaging; a concurrent writer cannot mix generations.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from typing import Callable

from . import fingerprints
from .backends.base import EmbeddingBackend
from .citations import index_uuid, read_evidence, resolve_citation
from .contracts import (
    ApplicationError, BudgetTooSmall, ContextRequest, ContextResult, IndexStatus,
    SearchRequest, SearchResult, SegmentContext, StoredEvidence,
)
from .deadline import RequestTimeout, check_deadline, remaining_timeout, request_timeout
from .lexical import LEXICAL_VERSION
from .pack import (
    BudgetTooSmall as PackBudgetTooSmall,
    NeighborIndex, PackSettings, build_pack_from_hits, tokens_of,
)
from .search import Hit, Searcher
from .anchor_admission import guard_composed_hits
from .store import Store

BackendFactory = Callable[[], EmbeddingBackend | None]


class Application:
    def __init__(self, db_path: Path | str, backend_factory: BackendFactory | None = None,
                 *, reranker_factory: Callable | None = None, admission_parameters=None):
        self.db_path = Path(db_path)
        self.backend_factory = backend_factory
        self.reranker_factory = reranker_factory
        self.admission_parameters = admission_parameters

    @contextmanager
    def _snapshot(self, timeout_s: float):
        try:
            with request_timeout(timeout_s):
                if not self.db_path.is_file():
                    raise ApplicationError("no_index", f"index does not exist: {self.db_path}", exit_code=4)
                try:
                    with closing(Store(self.db_path, ro=True, timeout_s=remaining_timeout(60))) as store:
                        version = store.db.execute("PRAGMA user_version").fetchone()[0]
                        if version < 4:
                            raise ApplicationError(
                                "missing_capability", "this read-only API needs schema v4 or newer; "
                                "upgrade the index explicitly", exit_code=4,
                            )
                        with store.read_snapshot():
                            yield store
                except sqlite3.Error as exc:
                    check_deadline()
                    raise ApplicationError("invalid_index", f"cannot read index: {exc}") from exc
        except RequestTimeout as exc:
            raise ApplicationError("timeout", str(exc)) from exc

    @contextmanager
    def _backend(self, *, enabled: bool):
        check_deadline()
        backend = self.backend_factory() if enabled and self.backend_factory is not None else None
        with closing(backend) if backend is not None else nullcontext():
            check_deadline()
            yield backend

    def _visual_capability(self) -> bool:
        """Static media-capability probe (no spawn): helper + mmproj resolved
        on this install. Feeds status' visual_search flag together with the
        stored fingerprint's media extras (digup-plan-002 T14)."""
        try:
            with self._backend(enabled=True) as backend:
                return backend is not None and backend.capability().image
        except Exception:
            return False

    def search(self, request: SearchRequest) -> SearchResult:
        self._validate_search_mode(request)
        timeout_s = request.timeout_s if request.timeout_s is not None else (10.0 if request.mode == "quality" else 5.0)
        with self._snapshot(timeout_s) as store:
            uuid = index_uuid(store, required=request.require_citations)
            with self._backend(enabled=not request.lexical_only) as backend:
                scorer = "lexical" if request.lexical_only else request.scorer
                if scorer != "lexical" and backend is None:
                    raise ApplicationError("missing_capability", "semantic search needs a configured backend", exit_code=4)
                if not request.lexical_only and backend is not None:
                    fingerprints.gate(store.meta, fingerprints.build(backend))
                check_deadline()
                hits, quality, fallback = self._retrieve(store, backend, request, scorer)
                check_deadline()
                citations = [
                    read_evidence(store, hit.segment_id, uuid=uuid).citation
                    for hit in hits if uuid is not None and hit.segment_id is not None
                ]
                supporting_citations = {
                    hit.segment_id: [read_evidence(store, sid, uuid=uuid).citation
                                     for sid in hit.supporting_segment_ids]
                    for hit in hits if uuid is not None and hit.supporting_segment_ids
                }
                contexts = {}
                if request.context:
                    neighbor = NeighborIndex(store)
                    cited_ids = {citation.segment_id for citation in citations}
                    for hit in hits:
                        parts, note = neighbor.parts_for(hit, request.context)
                        contexts[hit.rank] = SegmentContext(parts, note)
                        if uuid is not None:
                            for part in parts:
                                sid = part["segment_id"]
                                if sid not in cited_ids:
                                    citations.append(read_evidence(store, sid, uuid=uuid).citation)
                                    cited_ids.add(sid)
                pack_text, pack_meta = None, None
                if request.pack:
                    pack_text, pack_meta = self._pack(store, backend, request, hits)
                return SearchResult(
                    hits=hits, citations=citations, contexts=contexts,
                    pack_text=pack_text, pack_meta=pack_meta,
                    settings={
                        "scorer": scorer, "mode": request.mode, "rerank": request.rerank,
                        "multi_query": request.multi_query, "z_gate": request.z_gate,
                        "limit": request.limit, "context": request.context,
                        "budget": request.budget, "why": request.why,
                        "lexical_upgrade_required": store.meta("lexical_version") != LEXICAL_VERSION,
                        **quality,
                    },
                    via=getattr(backend, "via", "cold"),
                    fallback_reason=fallback or getattr(backend, "fallback_reason", None),
                    stale=request.lexical_only and store.meta("fingerprint_id") is not None,
                    supporting_citations=supporting_citations,
                )

    @staticmethod
    def _validate_search_mode(request: SearchRequest) -> None:
        if request.mode not in ("fast", "quality") or request.rerank not in ("off", "local"):
            raise ApplicationError("invalid_params", "invalid search mode or reranker", exit_code=2)
        if request.lexical_only and request.rerank != "off":
            raise ApplicationError("invalid_params", "lexical-only search cannot use a reranker", exit_code=2)
        if request.mode == "fast" and (request.rerank != "off" or request.multi_query):
            raise ApplicationError("invalid_params", "reranking and multi-query need quality mode", exit_code=2)

    def _retrieve(self, store, backend, request, scorer):
        # rank-window-001: the window arbitration rank head is armed only for
        # pack-building requests (answer evidence assembly). Bare listing
        # searches — including the latency bench — keep the ranker off, and
        # their file scores never depended on window choice anyway.
        ranker = None
        if request.pack and self.reranker_factory is not None:
            try:
                # The factory product's lifetime is policy-managed by its
                # provider (a shared lease reaps on idle; a raw LocalReranker
                # is owned by the provider) — never closed here.
                ranker = self.reranker_factory()
            except Exception:
                ranker = None
        return self._retrieve_with_ranker(store, backend, request, scorer, ranker)

    def _retrieve_with_ranker(self, store, backend, request, scorer, ranker):
        searcher = Searcher(store, None if request.lexical_only else backend,
                            admission_parameters=self.admission_parameters, ranker=ranker)
        decisions = []
        # A2 (DigUp Engine.swift:749-760): count of file rows dropped because
        # the file vanished since the last ingest run; surfaces in settings.
        stats: dict = {}
        kwargs = dict(limit=request.limit, scorer=scorer, z_gate=request.z_gate,
                      obligation_sink=decisions,
                      kinds=request.kinds, filters=request.filters,
                      snippet_width=request.snippet_width, why=request.why,
                      stats=stats)
        if request.mode == "fast":
            hits = searcher.search(request.query, **kwargs)
            meta = ({"source_capabilities": decisions[0][1].source_capabilities} if decisions else {})
            if decisions and decisions[0][1].constrained:
                meta["anchor_contract"] = decisions[0][1].diagnostics()
            if stats.get("_deleted_ids"):
                meta["deleted_filtered"] = len(stats["_deleted_ids"])
            return hits, meta, None
        from .quality import merge_queries, rerank_candidates, select_results

        metadata, fallback = {"quality_validation": "unvalidated_experiment"}, None
        if request.multi_query:
            hits, metadata["query_plan"] = merge_queries(searcher, request, kwargs)
            if metadata["query_plan"]["subquery_failures"]:
                fallback = "one_or_more_subqueries_failed"
        else:
            hits = searcher.search(request.query, **{**kwargs, "limit": 20 if request.limit > 0 else 0})
        if stats.get("_deleted_ids"):
            metadata["deleted_filtered"] = len(stats["_deleted_ids"])
        original = next((decision for text, decision in decisions if text == request.query), None)
        if original is not None:
            metadata["source_capabilities"] = original.source_capabilities
        if original is not None:
            hits = guard_composed_hits(store, original, hits)
            if original.constrained:
                metadata["anchor_contract"] = original.diagnostics()
        if request.rerank == "local":
            hits, metadata["reranker"], reason = rerank_candidates(
                store, request.query, hits, self.reranker_factory, why=request.why,
            )
            fallback = reason or fallback
        selected = select_results(hits, limit=request.limit,
                                  balance_topics=request.pack and request.multi_query)
        if original is not None:
            selected = guard_composed_hits(store, original, selected, final=True)
            if original.constrained and not selected and hits:
                metadata["anchor_contract"]["reason"] = "anchor_not_in_selected_limit"
        return selected, metadata, fallback

    @staticmethod
    def _pack(store, backend, request: SearchRequest, hits: list[Hit]):
        settings = PackSettings(
            query=request.query, scorer="lexical" if request.lexical_only else request.scorer,
            z_gate=request.z_gate, limit=request.limit, context=request.context,
            budget=request.budget, kinds=request.kinds, filters=request.filters,
            why=request.why,
        )
        citation_cache = {}

        def citation_id_of(segment_id: int) -> str:
            if segment_id not in citation_cache:
                citation_cache[segment_id] = read_evidence(store, segment_id).citation.citation_id
            return citation_cache[segment_id]

        try:
            result = build_pack_from_hits(
                store, backend, settings, hits,
                citation_id_of=citation_id_of if (request.require_citations or index_uuid(store, required=False)) else None,
            )
        except PackBudgetTooSmall as exc:
            raise BudgetTooSmall(str(exc)) from exc
        check_deadline()
        result[1]["tokenizer"] = (Application._tokenizer(backend) if result[1]["token_mode"] == "tokenize"
                                  else "goldmine.char-estimate/v1")
        return result

    def get(self, citation_id: str, *, budget: int = 2000, timeout_s: float = 5.0) -> ContextResult:
        if not 512 <= budget <= 8000:
            raise ApplicationError("invalid_params", "get budget must be 512..8000", exit_code=2)
        with self._snapshot(timeout_s) as store:
            evidence = resolve_citation(store, citation_id)
            with self._backend(enabled=True) as backend:
                if evidence.citation.evidence_type != "content":
                    return self._metadata_result(evidence, backend, budget)
                return self._context_result(store, backend, [evidence], neighbors=0, budget=budget)

    def context(self, request: ContextRequest) -> ContextResult:
        if not 1 <= len(request.citation_ids) <= 10 or not 0 <= request.neighbors <= 3:
            raise ApplicationError("invalid_params", "context needs 1..10 citations and 0..3 neighbors", exit_code=2)
        if not 512 <= request.budget <= 32768:
            raise ApplicationError("invalid_params", "context budget must be 512..32768", exit_code=2)
        with self._snapshot(request.timeout_s if request.timeout_s is not None else 5.0) as store:
            evidence = [resolve_citation(store, cid) for cid in dict.fromkeys(request.citation_ids)]
            with self._backend(enabled=True) as backend:
                if len(evidence) == 1 and evidence[0].citation.evidence_type != "content":
                    return self._metadata_result(evidence[0], backend, request.budget)
                return self._context_result(
                    store, backend, evidence, neighbors=request.neighbors, budget=request.budget,
                )

    def _context_result(self, store, backend, evidence, *, neighbors: int, budget: int) -> ContextResult:
        hits = [self._evidence_hit(e, rank) for rank, e in enumerate(evidence, 1)]
        request = SearchRequest(
            query="", scorer="lexical", context=neighbors, budget=budget,
            limit=len(hits), require_citations=True,
        )
        text, meta = self._pack(store, backend, request, hits)
        if not meta["records"] and evidence:
            raise BudgetTooSmall("budget cannot fit a complete cited segment")
        included = {r["segment_id"] for r in meta["records"]}
        included.update(part["segment_id"] for r in meta["records"] for part in r["segments"])
        included.update(other["segment_id"] for r in meta["records"] for other in r["also_hits"]
                        if other.get("segment_id") is not None)
        return ContextResult(
            context_text=text, citations=[e.citation for e in evidence if e.citation.segment_id in included],
            used_tokens=meta["used_tokens"], token_mode=meta["token_mode"],
            tokenizer=meta["tokenizer"], skipped=meta["skipped_over_budget"],
            evidence_type=evidence[0].citation.evidence_type if len(evidence) == 1 else None,
            file_status=evidence[0].file_status if len(evidence) == 1 else None,
        )

    @staticmethod
    def _evidence_hit(evidence: StoredEvidence, rank: int) -> Hit:
        c = evidence.citation
        return Hit(
            rank=rank, score=0.0, z=None, cosine=None, coverage=0.0, kind=c.kind,
            path=c.path, loc=c.loc, loc_end=c.loc_end, segment=c.segment_kind,
            excerpt=None, file_status=evidence.file_status, scorer="lexical",
            segment_id=c.segment_id,
        )

    def _metadata_result(self, evidence: StoredEvidence, backend, budget: int) -> ContextResult:
        c = evidence.citation
        # These are stored source fields, explicitly labelled as metadata. They
        # are never presented as a recovered document body or image caption.
        payload = {"path": c.path, "source": c.source, "kind": c.kind, "info": evidence.metadata}
        text = (
            f"# goldmine stored {c.evidence_type}\n# citation: {c.citation_id}\n"
            "# no quotable document body\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        )
        used, mode = tokens_of(text, backend)
        check_deadline()
        if used > budget:
            raise BudgetTooSmall()
        return ContextResult(
            context_text=text, citations=[c], used_tokens=used, token_mode=mode,
            tokenizer=self._tokenizer(backend) if mode == "tokenize" else "goldmine.char-estimate/v1", evidence_type=c.evidence_type,
            file_status=evidence.file_status,
        )

    @staticmethod
    def _tokenizer(backend) -> str:
        if backend is None:
            return "goldmine.char-estimate/v1"
        fields = backend.fingerprint_fields()
        return f"{backend.backend_id}:{fields.get('model_id', 'unknown')}:{fields.get('model_sha256', 'unknown')}"

    def status(self, *, timeout_s: float = 5.0, include_details: bool = False,
               failure_limit: int = 10) -> IndexStatus:
        from .evidence_status import stored_evidence_status

        with self._snapshot(timeout_s) as store:
            tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_schema")}
            uuid = index_uuid(store, required=False)
            raw_fp = store.meta("fingerprint_json")
            fp = json.loads(raw_fp) if raw_fp else None
            live_embed = None
            if fp:
                try:
                    live_embed = fingerprints.embed_id_from_fingerprint(fp)
                except (ValueError, TypeError):
                    pass
            details = {}
            if include_details:
                try:
                    roots = json.loads(store.meta("roots_json") or "[]")
                except ValueError:
                    roots = []
                details = {
                    "schema_version": store.db.execute("PRAGMA user_version").fetchone()[0], "fingerprint": fp,
                    "roots": [str(item) for item in roots] if isinstance(roots, list) else [],
                    "llama_build": store.meta("llama_build"),
                    "cards_rebuild_note": store.meta("cards_v2_rebuild_note") if store.cards_empty() else None,
                    "failures": [{"path": f.path, "detail": f.detail} for f in store.failures(failure_limit)],
                    "pending": [f.path for f in store.pending()],
                    "metadata_reasons": [{"reason": reason, "count": count}
                                         for reason, count in store.reasons("metadata_only")],
                }
            return IndexStatus(
                schema_version=store.db.execute("PRAGMA user_version").fetchone()[0],
                index_uuid=uuid, counts=store.counts(), lexical_version=store.meta("lexical_version"),
                fingerprint_id=store.meta("fingerprint_id"),
                capabilities={
                    "opaque_citations": uuid is not None, "cjk_lexical": "fts_cjk" in tables,
                    "exact_lookup": "lookup_terms" in tables, "stored_text": "fts" in tables,
                    # digup-plan-002: visual search is live exactly when the
                    # media capability is — helper+mmproj resolvable on THIS
                    # install (static check, no spawn) and the index was built
                    # with the media extras. The stored fingerprint carries
                    # media fields iff vectors of that family exist.
                    "visual_search": bool(
                        fp is not None and fp.get("media")
                        and self._visual_capability()
                    ),
                },
                staleness=fingerprints.staleness_report(store, live_embed), details=details,
                evidence=stored_evidence_status(store, limit=100 if include_details else 0),
            )
