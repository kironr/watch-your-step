"""Fetch a pool of plausible-but-unwatched videos to use as decoys.

Sources (YouTube Data API v3, set YOUTUBE_API_KEY):
  1. Uploads from your most-watched long-form channels (1 quota unit per 50 videos):
     right creator, but a video you chose not to watch.
  2. Searches on your topics (100 units each, and at most 100 searches per day): same interests, creators you
     don't follow. Needed so channel familiarity can't give the answer away.
     --queries topics.txt  your own list, one query per line, or
     --auto-queries 15     Claude writes queries from your profile (saved to
                           evals/data/queries.txt so you can edit and reuse them).

  python -m evals.fetch_distractors --history watch-history.json --auto-queries 15

New videos are merged into the existing pool. Then run evals.fetch_durations again so
Shorts and live streams among them get filtered.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

from watchyourstep.profile import build_profile
from watchyourstep.recommender import Recommender, load_config
from watchyourstep.takeout import load_watch_history
from watchyourstep.youtube import DailyLimitReached, api_get, is_deliberate, load_durations, load_live

HERE = Path(__file__).parent


def channel_uploads(channel_id: str, pages: int) -> list[dict]:
    out, token = [], None
    for _ in range(pages):
        kw = {"part": "snippet", "playlistId": "UU" + channel_id[2:], "maxResults": 50}
        if token:
            kw["pageToken"] = token
        data = api_get("playlistItems", **kw)
        for it in data.get("items", []):
            s = it["snippet"]
            out.append({"video_id": s["resourceId"]["videoId"], "title": s["title"],
                        "channel": s.get("videoOwnerChannelTitle") or s.get("channelTitle", ""),
                        "description": s.get("description", "")[:500],
                        "published": s.get("publishedAt"), "source": "channel_uploads"})
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def search(q: str) -> list[dict]:
    """One search per topic (searches are capped at 100/day). Shorts are filtered later."""
    data = api_get("search", part="snippet", q=q, type="video", maxResults=50, relevanceLanguage="en")
    return [{"video_id": it["id"]["videoId"], "title": it["snippet"]["title"],
             "channel": it["snippet"]["channelTitle"], "description": it["snippet"].get("description", ""),
             "published": it["snippet"].get("publishedAt"), "source": f"search:{q}"}
            for it in data.get("items", [])]


def auto_queries(events, n: int) -> list[str]:
    """Ask Claude for YouTube searches that match this viewer's long-form interests."""
    cfg = load_config()
    profile = build_profile(events, window_days=3650, top_channels=40, recent_titles=120)
    rec = Recommender(cfg)
    resp = rec.client.messages.create(
        model=cfg["model"], max_tokens=1500,
        messages=[{"role": "user", "content":
                   f"Here is a summary of someone's YouTube viewing history.\n\n{profile}\n\n"
                   f"Write {n} YouTube search queries that would surface long-form videos on the "
                   "topics and formats this person watches, but mostly from creators other than the "
                   "channels listed. Cover all of their main interests in proportion. 2 to 6 words "
                   'each. Reply with JSON only: {"queries": ["...", ...]}'}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    return [q.strip() for q in json.loads(m.group(0))["queries"] if q.strip()][:n] if m else []


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--channels", type=int, default=30, help="top N channels to pull uploads from")
    ap.add_argument("--pages", type=int, default=4, help="pages of 50 uploads per channel")
    ap.add_argument("--skip-channels", action="store_true", help="only run searches")
    ap.add_argument("--queries", help="file with one search query per line")
    ap.add_argument("--auto-queries", type=int, default=0, help="have Claude write N queries from your history")
    ap.add_argument("--out", default=str(HERE / "data/distractors.jsonl"))
    a = ap.parse_args(argv)

    events = load_watch_history(a.history)
    durations, live = load_durations(), load_live()
    if durations:  # use deliberate long-form watches, so Shorts-heavy channels don't dominate
        events = [e for e in events if is_deliberate(e.video_id, durations, live)]
        print(f"Using {len(events)} long-form watches")

    out = Path(a.out)
    pool: dict[str, dict] = {}
    if out.exists():
        for l in out.read_text(encoding="utf-8").splitlines():
            if l.strip():
                v = json.loads(l)
                pool[v["video_id"]] = v
    before = len(pool)

    if not a.skip_channels:
        counts = Counter(e.channel_id for e in events if e.channel_id)
        for cid, _ in counts.most_common(a.channels):
            try:
                for v in channel_uploads(cid, a.pages):
                    pool.setdefault(v["video_id"], v)
            except Exception as e:
                print(f"skip {cid}: {e}")

    queries = []
    if a.queries:
        queries += [q.strip() for q in Path(a.queries).read_text(encoding="utf-8").splitlines() if q.strip()]
    if a.auto_queries:
        gen = auto_queries(events, a.auto_queries)
        (HERE / "data").mkdir(exist_ok=True)
        (HERE / "data/queries.txt").write_text("\n".join(gen) + "\n", encoding="utf-8")
        print(f"Claude wrote {len(gen)} queries (saved to evals/data/queries.txt):")
        for q in gen:
            print(f"  - {q}")
        queries += gen
    for i, q in enumerate(queries):
        try:
            for v in search(q):
                pool.setdefault(v["video_id"], v)
            time.sleep(3)
        except DailyLimitReached as e:
            print(f"{e}\nStopped after {i} of {len(queries)} searches. Rerun tomorrow with "
                  f"--queries evals/data/queries.txt --skip-channels to add the rest.")
            break
        except Exception as e:
            print(f"search failed for {q!r}: {e}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(v) + "\n" for v in pool.values()), encoding="utf-8")
    n_search = sum(v["source"].startswith("search") for v in pool.values())
    print(f"pool: {len(pool)} videos ({len(pool) - before} new; {n_search} from searches) -> {out}")


if __name__ == "__main__":
    main()
