"""Programmatic grader: the output is a ranking and the answers are known, so no LLM judge.

Deterministic by construction (the post's grader-consistency check passes trivially).
"""
from __future__ import annotations

import math

PRIMARY = "ndcg@10"   # smoother than recall@3, so lower run-to-run variance


def grade(ranking: list[str], positives: list[str]) -> dict[str, float]:
    pos = set(positives)
    ranks = [i for i, v in enumerate(ranking) if v in pos]  # 0-based

    def ndcg(k):
        dcg = sum(1 / math.log2(r + 2) for r in ranks if r < k)
        ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(pos))))
        return dcg / ideal if ideal else 0.0

    return {
        "ndcg@10": ndcg(10),
        "recall@3": sum(r < 3 for r in ranks) / len(pos) if pos else 0.0,
        "hit@1": float(bool(ranks) and ranks[0] == 0),
        "mrr": 1 / (ranks[0] + 1) if ranks else 0.0,
    }
