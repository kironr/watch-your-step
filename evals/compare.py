"""Compare a candidate run against a baseline and apply the hillclimb decision rule.

  python -m evals.compare baseline try-1 [--min-delta 0.02] [--goal performance|cost]

Decision rule from the post:
  train and test both improve  -> KEEP
  train improves, test flat    -> REVERT (overfitting suspected)
  any regression               -> REVERT
For --goal cost: KEEP if cost per case drops and neither split regresses.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from .grader import PRIMARY
from .stats import mean_ci

RES = Path(__file__).parent / "results"


def per_case(label: str) -> dict[str, dict[str, float]]:
    rows = [json.loads(l) for l in (RES / label / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    acc = defaultdict(lambda: defaultdict(list))
    for r in rows:
        acc[r["split"]][r["case_id"]].append(r["scores"][PRIMARY])
    return {s: {c: statistics.fmean(v) for c, v in d.items()} for s, d in acc.items()}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("candidate")
    ap.add_argument("--min-delta", type=float, default=0.02,
                    help="smallest gain you'd act on; should exceed the noise floor from run.py --reps")
    ap.add_argument("--goal", choices=["performance", "cost"], default="performance")
    a = ap.parse_args(argv)

    base, cand = per_case(a.baseline), per_case(a.candidate)
    verdicts = {}
    for split in ("train", "test"):
        common = sorted(set(base.get(split, {})) & set(cand.get(split, {})))
        if not common:
            print(f"{split:>5}: no overlapping cases (run both labels on this split)")
            continue
        diffs = [cand[split][c] - base[split][c] for c in common]
        m, lo, hi = mean_ci(diffs)
        v = "improves" if m >= a.min_delta else "regresses" if m <= -a.min_delta else "flat"
        verdicts[split] = v
        print(f"{split:>5}: Δ{PRIMARY} = {m:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]  n={len(common)}  → {v}")

    sb = json.loads((RES / a.baseline / "summary.json").read_text(encoding="utf-8"))["splits"].get("all", {})
    sc = json.loads((RES / a.candidate / "summary.json").read_text(encoding="utf-8"))["splits"].get("all", {})
    cb, cc = sb.get("cost_per_case_usd"), sc.get("cost_per_case_usd")
    if cb is not None and cc is not None:
        print(f" cost: {cb*100:.2f}¢ → {cc*100:.2f}¢ per case")

    tr, te = verdicts.get("train"), verdicts.get("test")
    if "regresses" in verdicts.values():
        decision = "REVERT (regression)"
    elif a.goal == "cost":
        decision = "KEEP" if cb is not None and cc is not None and cc < cb else "REVERT (no cost gain or cost unknown)"
    elif tr == "improves" and te == "improves":
        decision = "KEEP"
    elif tr == "improves" and te in ("flat", None):
        decision = "REVERT (train-only gain: overfitting suspected)" if te else "UNDECIDED (run the test split)"
    else:
        decision = "REVERT (no gain beyond noise)"
    print(f"decision: {decision}")


if __name__ == "__main__":
    main()
