"""Source-bound query admission; scores never stand in for missing obligations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import unicodedata

from .anchor_resolution import Binding, Resolver, VerificationBound, literal_occurs
from .deadline import check_deadline
from .query_obligations import ANCHOR_VERSION, Anchor, Clause, QueryPlan, plan_query


@dataclass
class AnchorDecision:
    plan: QueryPlan
    owners: tuple[frozenset[int], ...] = ()
    bindings: tuple[Binding, ...] = ()
    witnesses: dict[tuple[int, int], tuple] = field(default_factory=dict)
    exact: dict = field(default_factory=dict)
    reason: str | None = None
    inspected_rows: int = 0
    queries: int = 0
    source_capabilities: dict = field(default_factory=dict)

    @property
    def constrained(self):
        return bool(self.owners) or self.reason is not None

    @property
    def allowed(self):
        return set().union(*self.owners) if self.owners else set()

    def represented(self, file_ids):
        return not self.reason and all(file_ids & set(owners) for owners in self.owners)

    def diagnostics(self):
        return {"algorithm_version": ANCHOR_VERSION, "reason": self.reason,
                "anchors": len(self.plan.anchors), "clauses": len(self.owners),
                "bindings": len(self.bindings), "inspected_source_rows": self.inspected_rows,
                "source_queries": self.queries,
                "witnesses": sum(len(w) for w in self.witnesses.values()),
                "semantic_remainder": "not_implemented", "source_capabilities": self.source_capabilities}

    def why(self, file_id):
        clauses = [i for i, owners in enumerate(self.owners) if file_id in owners]
        return {"algorithm_version": ANCHOR_VERSION, "clauses": clauses,
                "bindings": [{**asdict(b), "clause_ids": [c.id for c in self.plan.clauses
                               if b.anchor_id in c.anchor_ids and file_id in self.owners[c.id]]}
                             for b in self.bindings if b.file_id == file_id],
                "remainder_witnesses": [
                    {"clause_id": clause, "kind": kind, "unit": unit, "segment_id": sid}
                    for (clause, fid), values in self.witnesses.items() if fid == file_id
                    for kind, unit, sid in values],
                "proof_scope": "stored lexical candidate support, not emitted fact or entailment"}


def _finish(decision, resolver):
    decision.inspected_rows = len(resolver.attempted)
    decision.queries = resolver.queries
    decision.source_capabilities = resolver.profile.diagnostics()
    return decision


def _best_exact(bindings):
    output = {}
    for b in sorted(bindings, key=lambda b: (b.level, {"raw": 0, "alias": 1, "qualified_components": 2}[b.match_type],
                                            b.segment_id, b.field)):
        output.setdefault(b.file_id, b.exact())
    return output


def _pure_metadata(plan, resolver):
    if plan.pure_text is None or not plan.pure_text.strip():
        return []
    return resolver.lookup(_whole_anchor(plan), whole_metadata=True)


def _whole_anchor(plan):
    text = plan.pure_text
    raw = text.strip()
    start = len(text) - len(text.lstrip())
    component = 1 if plan.phrases else 0
    return Anchor(0, component, start, start + len(raw), raw,
                  "path" if "/" in raw or "\\" in raw else "identity")


def _owners(plan, resolver, terms_of):
    by_anchor = {}
    bindings, cache = [], {}
    for anchor in plan.anchors:
        check_deadline()
        key = (unicodedata.normalize("NFKC", anchor.raw), anchor.kind)
        if key not in cache:
            cache[key] = resolver.bind(anchor)
        values = [replace(b, anchor_id=anchor.id, query_term=anchor.raw) for b in cache[key]]
        by_anchor[anchor.id] = {b.file_id for b in values}
        bindings.extend(values)
        if not values:
            return AnchorDecision(plan, reason="anchor_unresolved")
    owners, witnesses = [], {}
    for clause in plan.clauses:
        eligible = (set.intersection(*(by_anchor[a] for a in clause.anchor_ids))
                    if clause.anchor_ids else set(resolver.allowed))
        # Anchorless phrases use bounded global body recall, not every file.
        if not clause.anchor_ids and clause.phrases:
            eligible &= {r.file for r in resolver.literal_rows(clause.phrases[0])}
        if not eligible:
            reason = "anchor_scope_ambiguous" if len(clause.anchor_ids) > 1 else "anchor_unresolved"
            return AnchorDecision(plan, reason=reason)
        accepted = set()
        for fid in sorted(eligible):
            proof = resolver.remainder(fid, clause.remainder, clause.phrases, terms_of)
            if proof is not None:
                accepted.add(fid)
                witnesses[clause.id, fid] = proof
        if not accepted:
            return AnchorDecision(plan, reason="remainder_unsupported")
        owners.append(frozenset(accepted))
    bindings = tuple(b for b in bindings if any(b.file_id in owners[c.id] and b.anchor_id in c.anchor_ids
                                               for c in plan.clauses))
    return AnchorDecision(plan, tuple(owners), bindings, witnesses, _best_exact(bindings))


def resolve_obligations(store, query, phrases, allowed, terms_of, *, inspected=()):
    """One snapshot/call; bounds return unresolved, integrity/deadline errors propagate."""
    plan = plan_query(query, phrases)
    resolver = Resolver(store, allowed)
    try:
        # A real whole filename/path/title precedes heuristic scope splitting.
        pure = _pure_metadata(plan, resolver)
        if pure:
            resolver.read(inspected)
            # Preserve body owners too when the same complete query is stored.
            whole = _whole_anchor(plan)
            plan = QueryPlan((whole,), (Clause(0, (0,), ""),), plan.phrases, plan.pure_text)
            bindings = tuple(dict.fromkeys([*pure, *resolver.bind(whole, body_only=True)]))
            decision = AnchorDecision(plan, (frozenset(b.file_id for b in bindings),), bindings,
                                      exact=_best_exact(bindings))
        elif not plan.anchors:
            if not phrases:
                decision = AnchorDecision(plan)  # unchanged ordinary admission
            else:
                # No identity owner: enforce real quoted literals while keeping
                # ordinary unquoted-word quorum/z admission (no new all-word veto).
                plan = QueryPlan((), (Clause(0, (), "", tuple(phrases)),), tuple(phrases), plan.pure_text)
                resolver.read(inspected)
                decision = _owners(plan, resolver, terms_of)
        elif plan.reason:
            decision = AnchorDecision(plan, reason=plan.reason)
        else:
            resolver.read(inspected)
            decision = _owners(plan, resolver, terms_of)
    except VerificationBound:
        decision = AnchorDecision(plan, reason="anchor_verification_bound")
    return _finish(decision, resolver)


def guard_composed_hits(store, decision, hits, *, final=False):
    """Original obligations survive optional query fusion/reranking/limit."""
    if not decision.constrained:
        return hits
    if decision.reason or not hits:
        return []
    ids = [hit.segment_id for hit in hits if hit.segment_id is not None]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    files = dict(store.db.execute("SELECT id,file FROM segments WHERE id IN (" + marks + ")", ids))
    output = [hit for hit in hits if files.get(hit.segment_id) in decision.allowed]
    if final and not decision.represented({files[hit.segment_id] for hit in output}):
        return []
    return output
