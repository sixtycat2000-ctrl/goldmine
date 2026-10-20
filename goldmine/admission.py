"""Pure file admission, separate from recall, score fusion and evidence display.

Defaults reproduce the registered control arm. Alternative parameters are
explicit evaluation inputs; the unfinished product suite cannot select them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SEARCH_BEHAVIOR_VERSION = "search.v3-evidence-control"


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
                     z_gate=3.0, parameters=None, segment_margin=None):
    """Return a machine reason or None; a highest cosine alone proves nothing.

    Lexical and RRF ablations retain their existing any-FTS-match rule. Hybrid
    uses the file coverage quorum; semantic uses only its explicit z gate.
    Exact anchors have already verified the remaining query upstream.
    """
    params = parameters or AdmissionParameters()
    if exact and scorer != "semantic":
        return "verified_exact"
    if scorer in ("lexical", "rrf") and lexical_match:
        return "lexical_quorum"
    if scorer == "hybrid" and math.isfinite(coverage) and coverage >= params.coverage_quorum:
        return "lexical_quorum"
    semantic = scorer in ("hybrid", "semantic", "rrf") and z is not None and math.isfinite(z) and z >= z_gate
    if semantic and params.semantic_segment_margin is not None and segment_margin is not None:
        semantic = math.isfinite(segment_margin) and segment_margin >= params.semantic_segment_margin
    return "semantic_standout" if semantic else None


def admit_files(*, scorer, z_by_file, coverage, word_rank, exact, rejected_exact,
                z_gate, parameters=None, segment_margins=None):
    candidates = set(z_by_file) | set(coverage) | set(word_rank) | set(exact)
    out = {}
    for fid in sorted(candidates - set(rejected_exact)):
        reason = decide_admission(
            scorer=scorer, z=z_by_file.get(fid), coverage=coverage.get(fid, 0.0),
            lexical_match=fid in word_rank, exact=fid in exact, z_gate=z_gate,
            parameters=parameters, segment_margin=(segment_margins or {}).get(fid),
        )
        if reason:
            out[fid] = reason
    return out
