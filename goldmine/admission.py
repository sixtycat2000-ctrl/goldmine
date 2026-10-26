"""Pure file admission, separate from recall, score fusion and evidence display.

Defaults reproduce the registered control arm. Alternative parameters are
explicit evaluation inputs; the unfinished product suite cannot select them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SEARCH_BEHAVIOR_VERSION = "search.v8-name-concentration"


@dataclass(frozen=True)
class AdmissionParameters:
    coverage_quorum: float = 0.5
    semantic_segment_margin: float | None = None

    def __post_init__(self):
        if not math.isfinite(self.coverage_quorum) or not 0 <= self.coverage_quorum <= 1:
            raise ValueError("coverage quorum must be finite and within 0..1")
        margin = self.semantic_segment_margin
        if margin is not None and (not math.isfinite(margin) or margin < 0):
            raise ValueError("semantic segment margin must be finite and nonnegative")


def decide_admission(*, scorer, z=None, coverage=0.0, lexical_match=False, exact=False,
                     z_gate=3.0, parameters=None, segment_margin=None, textless=False,
                     name_hit=False):
    """Return a machine reason or None; a highest cosine alone proves nothing.

    Lexical and RRF ablations retain their existing any-FTS-match rule. Hybrid
    uses the file coverage quorum; semantic uses only its explicit z gate.
    Exact anchors have already verified the remaining query upstream. A hybrid
    file that shares no query word at all (zero coverage and no FTS rank) is
    not standing out ABOUT the query, however high its z (search.v7 witness).

    search.v8 (name-concentration-001): a hybrid file whose NAME surface
    carries at least three distinct query units is "called for by name" — it
    admits via name_quorum (z-qualified: name_quorum_standout) even when the
    scattered-body coverage quorum is not met. Reason precedence:
    exact > name_quorum(_standout) > lexical_quorum > semantic_standout.
    """
    params = parameters or AdmissionParameters()
    if exact and scorer != "semantic":
        return "verified_exact"
    if scorer in ("lexical", "rrf") and lexical_match:
        return "lexical_quorum"
    if scorer == "hybrid" and name_hit:
        semantic = z is not None and math.isfinite(z) and z >= z_gate
        return "name_quorum_standout" if semantic else "name_quorum"
    if scorer == "hybrid" and math.isfinite(coverage) and coverage >= params.coverage_quorum:
        return "lexical_quorum"
    semantic = scorer in ("hybrid", "semantic", "rrf") and z is not None and math.isfinite(z) and z >= z_gate
    if (semantic and scorer == "hybrid" and not textless and not lexical_match
            and not (math.isfinite(coverage) and coverage > 0)):
        semantic = False
    if semantic and params.semantic_segment_margin is not None and segment_margin is not None:
        semantic = math.isfinite(segment_margin) and segment_margin >= params.semantic_segment_margin
    return "semantic_standout" if semantic else None


def admit_files(*, scorer, z_by_file, coverage, word_rank, exact, rejected_exact,
                z_gate, parameters=None, segment_margins=None, textless=None,
                name_hits=None):
    candidates = set(z_by_file) | set(coverage) | set(word_rank) | set(exact)
    name_hits = name_hits or set()
    if scorer == "hybrid":
        # name-named files are candidates in their own right
        candidates |= name_hits
    out = {}
    for fid in sorted(candidates - set(rejected_exact)):
        reason = decide_admission(
            scorer=scorer, z=z_by_file.get(fid), coverage=coverage.get(fid, 0.0),
            lexical_match=fid in word_rank, exact=fid in exact, z_gate=z_gate,
            parameters=parameters, segment_margin=(segment_margins or {}).get(fid),
            textless=fid in (textless or ()),
            name_hit=fid in name_hits,
        )
        if reason:
            out[fid] = reason
    return out
