"""Source-bound query admission; scores never stand in for missing obligations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import unicodedata

from .anchor_resolution import Binding, Resolver, VerificationBound, literal_occurs, remainder_units
from .deadline import check_deadline
from .lexical import cjk_runs
from .query_obligations import (ANCHOR_VERSION, QUOTE_PROVENANCE_MIN_RUN,
                                QUOTE_PROVENANCE_OWNER_CAP, Anchor, Clause,
                                QueryPlan, anchor_strength, clause_spans,
                                plan_query, quote_normalize)


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
    soft_clauses: tuple[tuple[int, str], ...] = ()
    soft_bindings: tuple[Binding, ...] = ()
    ambient_anchors: tuple[str, ...] = ()

    @property
    def constrained(self):
        return any(self.owners) or self.reason is not None

    @property
    def allowed(self):
        return set().union(*(o for o in self.owners if o)) if any(self.owners) else set()

    def represented(self, file_ids):
        return not self.reason and all(file_ids & set(owners) for owners in self.owners if owners)

    def diagnostics(self):
        return {"algorithm_version": ANCHOR_VERSION, "reason": self.reason,
                "anchors": len(self.plan.anchors), "clauses": len(self.owners),
                "bindings": len(self.bindings), "inspected_source_rows": self.inspected_rows,
                "source_queries": self.queries,
                "witnesses": sum(len(w) for w in self.witnesses.values()),
                "soft_clauses": [dict(clause_id=cid, reason=r) for cid, r in self.soft_clauses],
                "ambient_anchors": list(self.ambient_anchors),
                "semantic_remainder": "structural_quote_provenance",
                "source_capabilities": self.source_capabilities}

    def why(self, file_id):
        clauses = [i for i, owners in enumerate(self.owners) if owners and file_id in owners]
        return {"algorithm_version": ANCHOR_VERSION, "clauses": clauses,
                "bindings": [{**asdict(b), "clause_ids": [c.id for c in self.plan.clauses
                               if b.anchor_id in c.anchor_ids and file_id in self.owners[c.id]]}
                             for b in self.bindings if b.file_id == file_id],
                "remainder_witnesses": [
                    {"clause_id": clause, "kind": kind, "unit": unit, "segment_id": sid}
                    for (clause, fid), values in self.witnesses.items() if fid == file_id
                    for kind, unit, sid in values],
                "proof_scope": "stored lexical candidate support, not emitted fact or entailment"}

    def soft_why(self):
        """Soft clauses never constrain or promote; their bindings are the disclosure."""
        return {"algorithm_version": ANCHOR_VERSION,
                "soft_clauses": [dict(clause_id=cid, reason=r) for cid, r in self.soft_clauses],
                "obligatory_bindings": [asdict(b) for b in self.soft_bindings],
                "ambient_anchors": list(self.ambient_anchors),
                "proof_scope": "stored lexical candidate support, not emitted fact or entailment"}


def _finish(decision, resolver):
    decision.inspected_rows = len(resolver.attempted)
    decision.queries = resolver.queries
    decision.source_capabilities = resolver.profile.diagnostics()
    return decision


def _best_exact(bindings):
    order = {"raw": 0, "alias": 1, "qualified_components": 2, "path_suffix": 3,
             "metadata_token": 4}
    output = {}
    for b in sorted(bindings, key=lambda b: (b.level, order[b.match_type],
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


def clause_intent(clause, terms_of):
    """C-INT iff the remainder carries no query content (design §2)."""
    text = clause.remainder
    if clause.phrases or terms_of(text) or cjk_runs(text):
        return "C-NL"
    return "C-INT"


def _bind_anchor(anchor, resolver, cache):
    key = (unicodedata.normalize("NFKC", anchor.raw), anchor.kind)
    if key not in cache:
        cache[key] = resolver.bind(anchor)
    return [replace(b, anchor_id=anchor.id, query_term=anchor.raw) for b in cache[key]]


def _quote_provenance(plan, clause, query, spans, intent, accepted, by_anchor, resolver):
    """The quote-lookup clause test (quote-provenance-design-001 §2).

    Precondition order is pinned: C-NL intent, >=1 BOUND component-0
    obligatory anchor, non-empty accepted owners within the owner cap, and
    available clause spans — then every accepted owner's stored original
    text must contain the clause-bounded head or tail run verbatim
    (whitespace-run-normalized, case-sensitive, >= the structural floor).
    Only a demotion gate: returning False everywhere keeps today's behavior.
    """
    if intent != "C-NL" or not accepted or spans is None or len(spans) != len(plan.clauses):
        return False
    if not 1 <= len(accepted) <= QUOTE_PROVENANCE_OWNER_CAP:
        return False
    cstart, cend = spans[clause.id]
    obligatory = [a for a in plan.anchors
                  if a.id in clause.anchor_ids and a.component == 0
                  and anchor_strength(a) == "obligatory"]
    if not obligatory or not any(by_anchor.get(a.id) for a in obligatory):
        return False
    first_end = min(a.end for a in obligatory)
    last_start = max(a.start for a in obligatory)
    head = quote_normalize(query[cstart:first_end])
    tail = quote_normalize(query[last_start:cend])
    for fid in sorted(accepted):
        check_deadline()
        whole = resolver.quote_corpus(fid)
        if whole is None:
            return False
        if not ((len(head) >= QUOTE_PROVENANCE_MIN_RUN and head in whole)
                or (len(tail) >= QUOTE_PROVENANCE_MIN_RUN and tail in whole)):
            return False
    return True


def _owners(plan, query, resolver, terms_of, *, spans=None):
    """Intent-aware obligation resolution (anchor-intent design §C-2).

    C-INT clauses keep the exact-lookup contract. In C-NL clauses only
    obligatory anchors participate; ambient anchors are never bound.
    Hard-empty only when a clause's obligatory anchors are entirely absent;
    failing remainders or missing siblings demote the clause to soft
    (unconstrained, disclosed, never verified-exact).
    """
    intents = {c.id: clause_intent(c, terms_of) for c in plan.clauses}
    strengths = {a.id: anchor_strength(a) for a in plan.anchors}
    clause_of = {a.id: [c for c in plan.clauses if a.id in c.anchor_ids] for a in plan.anchors}
    # Quoted phrase components are hard existence obligations in every intent;
    # query anchors participate per clause intent and strength.
    participates = {a.id: (a.component != 0 or
                           bool(clause_of[a.id]) and
                           (all(intents[c.id] == "C-INT" for c in clause_of[a.id]) or
                            strengths[a.id] == "obligatory"))
                    for a in plan.anchors}
    by_anchor, bindings, cache = {}, [], {}
    for anchor in plan.anchors:
        check_deadline()
        if not participates[anchor.id]:
            continue
        values = _bind_anchor(anchor, resolver, cache)
        by_anchor[anchor.id] = {b.file_id for b in values}
        bindings.extend(values)
    ambient_raws = sorted({a.raw for a in plan.anchors
                           if not participates[a.id]
                           and any(intents[c.id] == "C-NL" for c in clause_of[a.id])})
    owners, witnesses = [], {}
    soft_clauses, soft_bindings = [], []
    for clause in plan.clauses:
        check_deadline()
        clause_anchors = [a for a in plan.anchors
                          if a.id in clause.anchor_ids and participates[a.id]]
        if intents[clause.id] == "C-NL" and not clause_anchors and not clause.phrases:
            soft_clauses.append((clause.id, "ambient_only"))
            owners.append(frozenset())
            continue
        if intents[clause.id] == "C-INT":
            for anchor in clause_anchors:
                if not by_anchor[anchor.id]:
                    return AnchorDecision(plan, reason="anchor_unresolved")
            eligible = (set.intersection(*(by_anchor[a] for a in clause.anchor_ids
                                           if a in by_anchor))
                        if clause.anchor_ids else set(resolver.allowed))
            # Anchorless phrases use bounded global body recall, not every file.
            if not clause.anchor_ids and clause.phrases:
                eligible &= {r.file for r in resolver.literal_rows(clause.phrases[0])}
            if not eligible:
                reason = "anchor_scope_ambiguous" if len(clause.anchor_ids) > 1 else "anchor_unresolved"
                return AnchorDecision(plan, reason=reason)
        else:
            bound = [a for a in clause_anchors if by_anchor.get(a.id)]
            phrase_files = {r.file for r in resolver.literal_rows(clause.phrases[0])} \
                if clause.phrases else None
            if not bound and (phrase_files is None or not phrase_files):
                return AnchorDecision(plan, reason="anchor_unresolved")
            eligible = set.intersection(*(by_anchor[a.id] for a in bound)) if bound else set()
            if phrase_files is not None:
                if not phrase_files:
                    eligible = set()
                else:
                    eligible = (eligible & phrase_files) if bound else phrase_files
            if not eligible:
                soft_clauses.append((clause.id, "anchor_sibling_absent"))
                soft_bindings.extend(b for b in bindings if b.anchor_id in
                                     {a.id for a in bound})
                owners.append(frozenset())
                continue
        accepted = set()
        # SC-01: the clause's pure remainder units are identical for every
        # eligible file; prepare them once per clause in registered order.
        units = remainder_units(clause.remainder, terms_of)
        # info-recall-001 bodyless exemption: a metadata-bound file whose only
        # segment is the name row has no body a remainder could witness — the
        # question is vacuous, the binding stands, and the exemption is
        # disclosed as its own witness kind. Scoped to metadata bindings;
        # phrases keep their body-literal requirement.
        metadata_bound = {b.file_id: b for b in bindings
                          if b.match_type == "metadata_token" and b.anchor_id in clause.anchor_ids}
        for fid in sorted(eligible):
            binding = metadata_bound.get(fid)
            if (binding is not None and not clause.phrases
                    and resolver.is_bodyless(fid)):
                accepted.add(fid)
                witnesses[clause.id, fid] = (
                    ("metadata_bodyless", binding.query_term, binding.segment_id),)
                continue
            proof = resolver.remainder(fid, clause.remainder, clause.phrases, terms_of, units=units)
            if proof is not None:
                accepted.add(fid)
                witnesses[clause.id, fid] = proof
        if not accepted:
            if intents[clause.id] == "C-INT":
                return AnchorDecision(plan, reason="remainder_unsupported")
            soft_clauses.append((clause.id, "remainder_soft"))
            soft_bindings.extend(b for b in bindings if b.anchor_id in clause.anchor_ids)
            owners.append(frozenset())
            continue
        if _quote_provenance(plan, clause, query, spans, intents[clause.id],
                             accepted, by_anchor, resolver):
            # The stored owners prove the clause quotes them: the anchors are
            # provenance markers of the passage, not retrieval discriminators.
            # Soft semantics only — unconstrained, disclosed, never verified.
            soft_clauses.append((clause.id, "quote_provenance"))
            soft_bindings.extend(b for b in bindings if b.anchor_id in clause.anchor_ids)
            for fid in accepted:
                witnesses.pop((clause.id, fid), None)
            owners.append(frozenset())
            continue
        owners.append(frozenset(accepted))
    bindings = tuple(b for b in bindings if any(owners[c.id] and b.file_id in owners[c.id]
                                               and b.anchor_id in c.anchor_ids
                                               for c in plan.clauses))
    return AnchorDecision(plan, tuple(owners), bindings, witnesses, _best_exact(bindings),
                          soft_clauses=tuple(soft_clauses), soft_bindings=tuple(soft_bindings),
                          ambient_anchors=tuple(ambient_raws))


def resolve_obligations(store, query, phrases, allowed, terms_of, *, inspected=()):
    """One snapshot/call; bounds return unresolved, integrity/deadline errors propagate."""
    plan = plan_query(query, phrases)
    resolver = Resolver(store, allowed)
    try:
        # A real whole filename/path/title precedes heuristic scope splitting.
        pure = _pure_metadata(plan, resolver)
        if pure:
            resolver.read(inspected, preread=True)
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
                resolver.read(inspected, preread=True)
                # Synthetic anchorless clause: no anchors, so the quote rule is
                # structurally out (spans deliberately not supplied).
                decision = _owners(plan, query, resolver, terms_of)
        elif plan.reason == "anchor_scope_ambiguous" and not phrases and \
                all(clause_intent(c, terms_of) == "C-NL" for c in plan.clauses):
            # Design §C-3/§2.3: prose structure never refuses by itself. This
            # covers both the >=2-semicolon single clause and the one-semicolon
            # split whose sibling clause is anchorless (no phrases involved);
            # phrases-plus-split (C1-33) and the compare grammar stay pinned.
            plan = replace(plan, reason=None)
            resolver.read(inspected, preread=True)
            decision = _owners(plan, query, resolver, terms_of, spans=clause_spans(query))
        elif plan.reason:
            decision = AnchorDecision(plan, reason=plan.reason)
        else:
            resolver.read(inspected, preread=True)
            decision = _owners(plan, query, resolver, terms_of, spans=clause_spans(query))
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
