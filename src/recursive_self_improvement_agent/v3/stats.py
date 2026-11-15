"""Promotion statistics that stay valid under repeated, sequential selection.

* Paired betting e-process (testing by betting; Waudby-Smith & Ramdas, 2024) for
  H0: E[candidate - incumbent] <= 0 on bounded paired differences. By Ville's
  inequality, P(sup_t K_t >= 1/alpha) <= alpha under H0, so the selection set may be
  inspected after every batch and evaluation can stop early.
* Error spending across an unbounded number of recursive cycles:
  alpha_c = alpha * 6 / (pi^2 c^2), which sums to alpha.
* Holm-corrected one-sided exact sign tests for per-family regressions.
* Seeded paired bootstrap and Wilson intervals for reporting, plus a McNemar power
  calculation used to size the splits.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats as sps

MAX_BET = 0.5


def alpha_for_cycle(cycle: int, alpha: float = 0.05) -> float:
    if cycle < 1:
        raise ValueError("cycles are numbered from 1")
    return alpha * 6.0 / (math.pi**2 * cycle**2)


@dataclass(frozen=True)
class EProcess:
    wealth: float
    max_wealth: float
    n: int
    path: tuple[float, ...]

    def rejects(self, alpha: float) -> bool:
        return self.max_wealth >= 1.0 / alpha


def betting_eprocess(
    diffs: Sequence[float], prior_mean: float = 0.0, prior_var: float = 0.25
) -> EProcess:
    """Predictable-plug-in (aGRAPA-style) bets on differences in [-1, 1]."""
    wealth = 1.0
    best = 1.0
    path: list[float] = []
    mean_num, var_num, count = prior_mean, prior_var, 1.0
    for d in diffs:
        if not -1.0 <= d <= 1.0:
            raise ValueError("paired differences must lie in [-1, 1]")
        mu = mean_num / count
        var = var_num / count
        bet = min(max(mu / (var + mu * mu), 0.0), MAX_BET) if var + mu * mu > 0 else 0.0
        wealth *= 1.0 + bet * d
        best = max(best, wealth)
        path.append(wealth)
        mean_num += d
        count += 1.0
        var_num += (d - mean_num / count) ** 2
    return EProcess(wealth=wealth, max_wealth=best, n=len(path), path=tuple(path))


def sign_test_regression_p(wins: int, losses: int) -> float:
    """One-sided p-value that the candidate is worse (more losses than wins)."""
    if wins + losses == 0:
        return 1.0
    return float(sps.binomtest(losses, wins + losses, 0.5, alternative="greater").pvalue)


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for rank, (key, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - rank) * p))
        adjusted[key] = running
    return adjusted


def mcnemar_exact_p(wins: int, losses: int) -> float:
    if wins + losses == 0:
        return 1.0
    return float(sps.binomtest(wins, wins + losses, 0.5, alternative="two-sided").pvalue)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def paired_bootstrap(
    a: Sequence[float], b: Sequence[float], reps: int = 2000, seed: int = 0
) -> tuple[float, float, float]:
    """Mean of (a - b) with a 95% percentile interval."""
    x = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    if x.size == 0:
        return (0.0, 0.0, 0.0)
    rng = np.random.default_rng(seed)
    samples = rng.choice(x, size=(reps, x.size), replace=True).mean(axis=1)
    return (
        float(x.mean()),
        float(np.percentile(samples, 2.5)),
        float(np.percentile(samples, 97.5)),
    )


def ratio_bootstrap(
    cost: Sequence[float], success: Sequence[int], reps: int = 2000, seed: int = 0
) -> tuple[float | None, float | None, float | None]:
    """Cost per verified completion with a 95% percentile interval."""
    c = np.asarray(cost, dtype=float)
    s = np.asarray(success, dtype=float)
    if s.sum() == 0:
        return (None, None, None)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, c.size, size=(reps, c.size))
    num = c[idx].sum(axis=1)
    den = s[idx].sum(axis=1)
    ratios = num[den > 0] / den[den > 0]
    return (
        float(c.sum() / s.sum()),
        float(np.percentile(ratios, 2.5)),
        float(np.percentile(ratios, 97.5)),
    )


def mcnemar_sample_size(
    delta: float, discordance: float, alpha: float = 0.05, power: float = 0.8
) -> int:
    """Pairs needed to detect a paired accuracy difference `delta` (one-sided)."""
    if not 0 < delta < discordance <= 1:
        raise ValueError("require 0 < delta < discordance <= 1")
    za = sps.norm.ppf(1 - alpha)
    zb = sps.norm.ppf(power)
    n = (za * math.sqrt(discordance) + zb * math.sqrt(discordance - delta * delta)) ** 2 / (
        delta * delta
    )
    return math.ceil(n)
