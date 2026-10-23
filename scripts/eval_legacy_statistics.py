"""Historical retrieval statistics, kept byte-semantically compatible.

New grouped evaluations use source-group resampling; these legacy per-query
statistics remain for comparison with the frozen v3 reports only.
"""
from __future__ import annotations

import math
import random

def _binom_tail_le(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p) — iterative pmf, no scipy."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    q = 1.0 - p
    pmf = q ** n
    total = pmf
    for i in range(1, k + 1):
        pmf *= (n - i + 1) / i * (p / q)
        total += pmf
    return min(total, 1.0)


def _solve_decreasing(f, target: float) -> float:
    """x in [0, 1] with decreasing f(x) == target (bisection, 1e-12 closed)."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def binom_ci(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Exact Clopper-Pearson CI for a binomial proportion (scipy-free):
    lower solves P(X <= k-1 | lo) = (1-conf)/2, upper solves
    P(X <= k | hi) = (1-conf)/2. Reference point (n=146, k=109):
    [0.668, 0.815]."""
    alpha = 1.0 - conf
    if n <= 0:
        return (0.0, 1.0)
    lo = 0.0 if k <= 0 else _solve_decreasing(
        lambda p: _binom_tail_le(k - 1, n, p), 1 - alpha / 2)
    hi = 1.0 if k >= n else _solve_decreasing(
        lambda p: _binom_tail_le(k, n, p), alpha / 2)
    return (lo, hi)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p on the discordant pair counts (b = pairs the
    current run gained, c = pairs it lost): p = 2 * P(Binom(b+c, 0.5) <=
    min(b, c)), clipped to 1. 7 same-direction flips out of 7 discordant
    pairs -> p = 0.0156 (one-sided 0.0078)."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2.0 * tail)


# ---------------------------------------------------------------- bootstrap

def bootstrap_ci(values: list[float], seed: int = 42,
                 reps: int = 2000) -> tuple[float, float]:
    """95% percentile CI of the mean (seeded — deterministic)."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(reps):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * reps)]
    hi = means[min(reps - 1, int(0.975 * reps))]
    return lo, hi
