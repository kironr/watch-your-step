"""Run the recommender over eval cases and score it.

  python -m evals.run --label baseline --reps 3              # all cases, 3 replicates
  python -m evals.run --label try-1 --split train            # hillclimb round
  python -m evals.run --label smoke --backend channel_freq   # no API calls

Writes evals/results/<label>/:
  results.jsonl                    one line per case x replicate
  transcripts/{train,test}/*.json  full request, reply, ranking and scores
  summary.json, report.html        means with 95% CIs per split, plus diagnostics
"""
from __future__ import annotations

import argparse
import json
import statistics
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from watchyourstep.recommender import Candidate, Recommender, load_config
from watchyourstep.takeout import WatchEvent

from .grader import PRIMARY, grade
from .report import write_report
from .stats import mean_ci

HERE = Path(__file__).parent


def load_cases():
    cases = [json.loads(l) for l in (HERE / "cases/cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    answers = {a["case_id"]: a for a in map(json.loads, (HERE / "cases/answers.jsonl").read_text(encoding="utf-8").splitlines())}
    split = json.loads((HERE / "cases/split.json").read_text(encoding="utf-8"))
    which = {cid: s for s in ("train", "test") for cid in split[s]}
    excl_file = HERE / "cases/excluded.txt"
    if excl_file.exists():
        excluded = {l.split("#")[0].strip() for l in excl_file.read_text(encoding="utf-8").splitlines()} - {""}
        cases = [c for c in cases if c["case_id"] not in excluded]
        print(f"Skipping {len(excluded)} case(s) listed in evals/cases/excluded.txt")
    return cases, answers, which


def run_one(rec: Recommender, case: dict, positives: list[str], rep: int) -> dict:
    hist = [WatchEvent(**e) for e in case["history"]]
    cands = [Candidate.from_dict(c) for c in case["candidates"]]
    # The recommender gets history + candidates only. Answers stay in this process.
    res = rec.rank(hist, cands, set(case["liked_ids"]), seed=zlib.crc32(f"{case['case_id']}:{rep}".encode()))
    return {"case_id": case["case_id"], "rep": rep, "tags": case["tags"],
            "scores": grade(res.ranking, positives), "ranking": res.ranking, "positives": positives,
            "reasons": res.reasons, "parse_ok": res.parse_ok, "error": res.error,
            "stop_reason": res.stop_reason, "usage": res.usage, "latency_s": round(res.latency_s, 2),
            "request": res.request, "raw_text": res.raw_text}


def summarize(rows: list[dict], cfg: dict) -> dict:
    out = {"model": cfg.get("model"), "backend": cfg.get("backend"), "primary": PRIMARY, "splits": {}}
    price = cfg.get("pricing", {}).get(cfg.get("model"), {})
    for split in ("train", "test", "all"):
        rs = [r for r in rows if split == "all" or r["split"] == split]
        if not rs:
            continue
        per_case = defaultdict(list)
        for r in rs:
            per_case[r["case_id"]].append(r["scores"][PRIMARY])
        case_means = [statistics.fmean(v) for v in per_case.values()]
        m, lo, hi = mean_ci(case_means)
        reps = sorted({r["rep"] for r in rs})
        rep_means = [statistics.fmean(r["scores"][PRIMARY] for r in rs if r["rep"] == k) for k in reps]
        s = {"n_cases": len(per_case), "n_runs": len(rs), PRIMARY: m, "ci95": [lo, hi],
             "metrics": {k: statistics.fmean(r["scores"][k] for r in rs) for k in rs[0]["scores"]},
             # Diagnostics (section 2 of the post)
             "noise_floor_sd_between_reps": statistics.stdev(rep_means) if len(rep_means) > 1 else None,
             "mean_within_case_sd": statistics.fmean(statistics.pstdev(v) for v in per_case.values()) if len(reps) > 1 else None,
             "api_errors": sum(bool(r["error"]) for r in rs),
             "parse_failures": sum(not r["parse_ok"] and not r["error"] for r in rs),
             "truncated": sum(r["stop_reason"] == "max_tokens" for r in rs),
             "never_solved": sorted(c for c, v in per_case.items() if max(v) == 0),
             "by_tag": {}}
        tags = {t for r in rs for t in r["tags"]}
        for t in sorted(tags):
            s["by_tag"][t] = statistics.fmean(r["scores"][PRIMARY] for r in rs if t in r["tags"])
        tin = sum(r["usage"].get("input_tokens", 0) for r in rs)
        tout = sum(r["usage"].get("output_tokens", 0) for r in rs)
        s["tokens"] = {"input": tin, "output": tout}
        if price.get("input") or price.get("output"):
            s["cost_per_case_usd"] = (tin * price.get("input", 0) + tout * price.get("output", 0)) / 1e6 / len(rs)
        s["mean_latency_s"] = statistics.fmean(r["latency_s"] for r in rs)
        warn = []
        if m >= 0.95:
            warn.append("Headroom: baseline ≥95%. Make cases harder, or optimize cost/latency instead.")
        if s["api_errors"] or s["truncated"]:
            warn.append("Plumbing: API errors or truncated replies. Fix before reading scores.")
        if s["never_solved"]:
            warn.append(f"{len(s['never_solved'])} case(s) scored 0 on every run: check for ambiguous or broken cases.")
        s["warnings"] = warn
        out["splits"][split] = s
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--split", choices=["train", "test", "all"], default="all")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--backend", choices=["claude", "channel_freq", "novelty", "random"])
    ap.add_argument("--model")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, help="only the first N cases (smoke test)")
    ap.add_argument("--with-interests", action="store_true",
                    help="include interests.md in the profile (written today, so it can leak later tastes into past cases)")
    a = ap.parse_args(argv)

    cfg = load_config()
    if a.model:
        cfg["model"] = a.model
    if a.backend:
        cfg["backend"] = a.backend
    cfg["use_interests"] = a.with_interests
    rec = Recommender(cfg)
    cases, answers, which = load_cases()
    cases = [c for c in cases if a.split == "all" or which[c["case_id"]] == a.split][: a.limit]

    out = HERE / "results" / a.label
    for s in ("train", "test"):
        (out / "transcripts" / s).mkdir(parents=True, exist_ok=True)
    jobs = [(c, r) for c in cases for r in range(a.reps)]
    with ThreadPoolExecutor(a.workers) as ex:
        rows = list(ex.map(lambda j: run_one(rec, j[0], answers[j[0]["case_id"]]["positives"], j[1]), jobs))

    for r in rows:
        r["split"] = which[r["case_id"]]
        (out / "transcripts" / r["split"] / f"{r['case_id']}_r{r['rep']}.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
    (out / "results.jsonl").write_text("".join(
        json.dumps({k: v for k, v in r.items() if k not in ("request", "raw_text")}) + "\n" for r in rows), encoding="utf-8")
    summary = summarize(rows, cfg)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_report(out, summary, rows, {c["case_id"]: c for c in cases})

    for split, s in summary["splits"].items():
        print(f"{split:>5}: {PRIMARY}={s[PRIMARY]:.3f}  95% CI [{s['ci95'][0]:.3f}, {s['ci95'][1]:.3f}]  "
              f"n={s['n_cases']}  errors={s['api_errors']} parse_fail={s['parse_failures']}")
        tok = s["tokens"]
        cost = s.get("cost_per_case_usd")
        cost_txt = (f"  cost ${cost * s['n_runs']:.2f} ({cost * 100:.2f}¢/call)" if cost is not None
                    else ("  (add [pricing] to config.toml for cost)" if tok["input"] else ""))
        if tok["input"] or tok["output"]:
            print(f"       tokens in {tok['input']:,} / out {tok['output']:,}{cost_txt}")
        for w in s["warnings"]:
            print(f"       ! {w}")
    print(f"report: {out / 'report.html'}")


if __name__ == "__main__":
    main()
