"""Build eval cases from your own Takeout history (time-travel evaluation).

Each case freezes your history at a cutoff time t and asks the recommender to rank a
slate of candidates. Positives are videos you actually went on to watch for the first
time in (t, t + horizon]. Negatives come from a distractor pool of videos you never
watched (see fetch_distractors.py). Ground truth is written to a separate file so the
recommender never sees it.

  python -m evals.build_cases --history path/to/watch-history.json \
      --likes "path/to/Liked videos.csv" --distractors evals/data/distractors.jsonl

Writes evals/cases/cases.jsonl, evals/cases/answers.jsonl, evals/cases/split.json.
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from datetime import timedelta
from pathlib import Path

from watchyourstep.takeout import load_liked_ids, load_watch_history, parse_time
from watchyourstep.youtube import SHORTS_MAX_SECONDS, is_deliberate, load_durations, load_live

OUT = Path(__file__).parent / "cases"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--likes")
    ap.add_argument("--distractors", default=str(Path(__file__).parent / "data/distractors.jsonl"))
    ap.add_argument("--n-cases", type=int, default=40)
    ap.add_argument("--positives", type=int, default=2, help="positives per slate")
    ap.add_argument("--slate", type=int, default=10, help="candidates per slate")
    ap.add_argument("--horizon-days", type=int, default=7)
    ap.add_argument("--history-days", type=int, default=180, help="history kept per case")
    ap.add_argument("--span-days", type=int, default=365, help="how far back cutoffs may go")
    ap.add_argument("--unfamiliar-frac", type=float, default=None,
                    help="share of decoys from channels you hadn't watched yet (default: match your real watches)")
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--include-shorts", action="store_true",
                    help="keep Shorts (default: long-form only; needs evals.fetch_durations)")
    ap.add_argument("--include-live", action="store_true", help="keep live streams and their replays")
    ap.add_argument("--min-seconds", type=int, default=SHORTS_MAX_SECONDS,
                    help="videos this long or shorter count as Shorts")
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)

    all_events = load_watch_history(a.history)
    liked = load_liked_ids(a.likes)
    ever_watched = {e.video_id for e in all_events}  # any view, Short or not, disqualifies a decoy
    if a.include_shorts:
        events, keep = all_events, (lambda vid: True)
    else:
        durations = load_durations()
        if not durations:
            raise SystemExit("No video lengths cached. Run: python -m evals.fetch_durations --history ...  "
                             "(or pass --include-shorts)")
        live = {} if a.include_live else load_live()
        if not a.include_live and not live:
            print("! No live-stream flags cached (run evals.fetch_durations again); streams are kept.")
        keep = lambda vid: is_deliberate(vid, durations, live, a.min_seconds)
        events = [e for e in all_events if keep(e.video_id)]
        what = "Long-form" + ("" if a.include_live else ", no live streams")
        print(f"{what}: kept {len(events)} of {len(all_events)} watch events")
    first_seen = {}
    for e in events:
        first_seen.setdefault(e.video_id, e)

    pool = [json.loads(l) for l in Path(a.distractors).read_text(encoding="utf-8").splitlines() if l.strip()]
    pool = [d for d in pool if d["video_id"] not in ever_watched and keep(d["video_id"])]
    print(f"Decoy pool: {len(pool)} usable videos")
    if len(pool) < a.slate:
        raise SystemExit(f"Distractor pool too small ({len(pool)}). Run evals.fetch_distractors first.")

    horizon = timedelta(days=a.horizon_days)
    end = events[-1].dt
    # Non-overlapping cutoffs every `horizon`, newest first, so cases are independent.
    cutoffs, t = [], end - horizon
    while t > end - timedelta(days=a.span_days) and t > events[0].dt + timedelta(days=30):
        cutoffs.append(t)
        t -= horizon

    # Pass 1: pick the real next watch(es) for every cutoff.
    slates = []
    for t in cutoffs:
        hist = [e for e in events if t - timedelta(days=a.history_days) <= e.dt <= t]
        if len(hist) < 20:
            continue
        prior_channels = Counter(e.channel for e in events if e.dt <= t)
        novel = [e for e in first_seen.values() if t < e.dt <= t + horizon]
        if len(novel) < a.positives:
            continue
        rng.shuffle(novel)
        novel.sort(key=lambda e: e.video_id not in liked)  # liked first (stable)
        slates.append((t, hist, prior_channels, novel[: a.positives]))

    # Channel familiarity must not give the answer away. Decoys come from unfamiliar
    # channels at the same rate as the real watches do, so "pick the new channel" (or
    # "pick the favourite channel") is no better than chance.
    all_pos = [(e, pc) for _, _, pc, pos in slates for e in pos]
    p_unfam = a.unfamiliar_frac if a.unfamiliar_frac is not None else (
        sum(pc[e.channel] == 0 for e, pc in all_pos) / max(1, len(all_pos)))
    print(f"Real watches from unfamiliar channels: {p_unfam:.0%}. Decoys matched to that rate.")

    # Pass 2: sample decoys for each slate.
    cases, answers, short_fam, short_unf = [], [], 0, 0
    for t, hist, prior_channels, pos in slates:
        # Distractors must not be from the future relative to the slate.
        valid = [d for d in pool if not d.get("published") or parse_time(d["published"]) <= t + horizon]
        fam = [d for d in valid if prior_channels[d.get("channel", "")] > 0]
        unf = [d for d in valid if prior_channels[d.get("channel", "")] == 0]
        n_neg = a.slate - len(pos)
        n_unf = round(n_neg * p_unfam)
        if len(unf) < n_unf:
            short_unf += 1
        if len(fam) < n_neg - n_unf:
            short_fam += 1
        n_unf = min(n_unf, len(unf))
        n_fam = min(n_neg - n_unf, len(fam))
        n_unf = min(len(unf), n_neg - n_fam)
        negs = rng.sample(unf, n_unf) + rng.sample(fam, n_fam)
        if len(negs) < n_neg:
            continue

        # Titles + channels only: Takeout has no descriptions for positives, so showing
        # descriptions on distractors would leak which candidates are the answers.
        cands = [{"video_id": e.video_id, "title": e.title, "channel": e.channel} for e in pos]
        cands += [{"video_id": d["video_id"], "title": d["title"], "channel": d.get("channel", "")} for d in negs]
        rng.shuffle(cands)

        tags = []
        if any(prior_channels[e.channel] == 0 for e in pos):
            tags.append("positive_from_new_channel")   # no channel prior to lean on
        if n_fam:
            tags.append("familiar_channel_distractors")  # decoys from channels you already watch
        if any(e.video_id in liked for e in pos):
            tags.append("liked_positive")

        cid = f"c{t:%Y%m%d}"
        cases.append({"case_id": cid, "cutoff": t.isoformat(), "tags": tags,
                      "history": [e.to_dict() for e in hist],
                      "liked_ids": sorted(liked & {e.video_id for e in hist}),
                      "candidates": cands})
        answers.append({"case_id": cid, "positives": [e.video_id for e in pos],
                        "positive_channels_seen_before": {e.video_id: prior_channels[e.channel] for e in pos}})
    if short_unf:
        print(f"! {short_unf} slate(s) had too few unfamiliar-channel decoys. "
              "Add search decoys: python -m evals.fetch_distractors --auto-queries 15 ...")
    if short_fam:
        print(f"! {short_fam} slate(s) had too few familiar-channel decoys.")

    if len(cases) > a.n_cases:
        keep = set(rng.sample([c["case_id"] for c in cases], a.n_cases))
        cases = [c for c in cases if c["case_id"] in keep]
        answers = [x for x in answers if x["case_id"] in keep]

    ids = [c["case_id"] for c in cases]
    rng.shuffle(ids)
    n_test = max(1, round(len(ids) * a.test_frac))
    split = {"train": sorted(ids[n_test:]), "test": sorted(ids[:n_test]), "seed": a.seed}

    OUT.mkdir(exist_ok=True)
    (OUT / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    (OUT / "answers.jsonl").write_text("".join(json.dumps(x) + "\n" for x in answers), encoding="utf-8")
    (OUT / "split.json").write_text(json.dumps(split, indent=2), encoding="utf-8")
    tag_counts = Counter(t for c in cases for t in c["tags"])
    print(f"{len(cases)} cases ({len(split['train'])} train / {len(split['test'])} test). Tags: {dict(tag_counts)}")
    print("Review them: python -m evals.review")


if __name__ == "__main__":
    main()
