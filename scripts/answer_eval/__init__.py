"""Disk-only answer evaluation v3; never starts a model or calls a service."""

from .protocol import EvaluationError, load_run
from .statistics import compute_score_variance, paired_group_bootstrap
from .workflow import compare_runs, validate_run, write_baseline

__all__ = [
    "EvaluationError", "load_run", "compute_score_variance",
    "paired_group_bootstrap", "compare_runs", "validate_run", "write_baseline",
]
