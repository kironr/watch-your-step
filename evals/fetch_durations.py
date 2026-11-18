"""Look up video lengths and live-stream flags so Shorts and streams can be excluded
(cached in data/durations.json and data/live.json).

  python -m evals.fetch_durations --history watch-history.json

Covers every video in your history plus the distractor pool. Safe to re-run: only
new IDs are looked up. Uses roughly 1 quota unit per 50 videos.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from watchyourstep.takeout import load_watch_history
from watchyourstep.youtube import DURATIONS_PATH, SHORTS_MAX_SECONDS, fetch_durations, fetch_live_flags


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--distractors", default=str(Path(__file__).parent / "data/distractors.jsonl"))
    a = ap.parse_args(argv)

    events = load_watch_history(a.history)
    ids = {e.video_id for e in events}
    if Path(a.distractors).exists():
        ids |= {json.loads(l)["video_id"] for l in Path(a.distractors).read_text(encoding="utf-8").splitlines() if l.strip()}
    print(f"{len(ids)} unique videos")
    dur = fetch_durations(ids)
    # Live checks only for long-form videos (Shorts are dropped anyway): saves quota.
    live = fetch_live_flags({v for v in ids if (dur.get(v) or 0) > SHORTS_MAX_SECONDS})

    def kind(v):
        d = dur.get(v)
        if d is None:
            return "unavailable"
        if d == 0 or live.get(v):
            return "live stream/replay"
        return "short" if d <= SHORTS_MAX_SECONDS else "long-form"
    c = Counter(kind(e.video_id) for e in events)
    print(f"Your watch events: {dict(c)}")
    print(f"Saved to {DURATIONS_PATH}")


if __name__ == "__main__":
    main()
