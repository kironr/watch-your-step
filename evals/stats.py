from __future__ import annotations

import random
import statistics


def mean_ci(xs: list[float], iters: int = 5000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float, float]:
    """Mean with a percentile bootstrap confidence interval."""
    if not xs:
        return (float("nan"),) * 3
    rng = random.Random(seed)
    n = len(xs)
    boots = sorted(statistics.fmean(rng.choices(xs, k=n)) for _ in range(iters))
    return statistics.fmean(xs), boots[int(iters * alpha / 2)], boots[int(iters * (1 - alpha / 2)) - 1]
