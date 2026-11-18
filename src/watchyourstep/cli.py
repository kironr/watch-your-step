"""wys (Watch Your Step) — rank candidate YouTube videos against your Takeout history.

  wys profile --history watch-history.json [--likes "Liked videos.csv"]
  wys discover --history watch-history.json [--likes ...] [--top 15] [--days 365]
  wys rank candidates.json --history watch-history.json [--likes ...] [--top 10] [--json]

candidates.json: a JSON list (or JSONL) of {"video_id", "title", "channel", "description"?}.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .profile import build_profile
from .recommender import Candidate, Recommender, load_config
from .takeout import load_liked_ids, load_watch_history
from .youtube import is_deliberate, load_durations, load_live


def _load_candidates(path: str) -> list[Candidate]:
    text = Path(path).read_text(encoding="utf-8").strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(l) for l in text.splitlines() if l.strip()]
    return [Candidate.from_dict(r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wys")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("profile", "rank", "discover"):
        p = sub.add_parser(name)
        p.add_argument("--history", required=True, help="Takeout watch-history.json")
        p.add_argument("--likes", help="Takeout 'Liked videos.csv'")
        p.add_argument("--include-shorts", action="store_true",
                       help="keep Shorts and live streams in your history (default: drop them if data/ caches exist)")
        if name == "discover":
            p.add_argument("--top", type=int, default=15)
            p.add_argument("--per-query", type=int, default=15, help="results per search")
            p.add_argument("--days", type=int, help="only videos published in the last N days")
            p.add_argument("--max-queries", type=int, help="only run the first N searches (saves quota)")
            p.add_argument("--per-topic", type=int, default=2, help="max picks from any one search")
            p.add_argument("--cache-days", type=float, default=3,
                           help="reuse a topic's search results for this many days (0 = always search)")
            p.add_argument("--reuse", action="store_true",
                           help="re-rank the last search results instead of searching again (no YouTube quota)")
            p.add_argument("--model")
        if name == "rank":
            p.add_argument("candidates")
            p.add_argument("--top", type=int, default=10)
            p.add_argument("--backend", choices=["claude", "channel_freq", "novelty", "random"])
            p.add_argument("--model")
            p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    history = load_watch_history(a.history)
    durations = load_durations()
    if durations and not a.include_shorts:
        # Drop known Shorts; keep videos whose length was never looked up.
        live = load_live()
        history = [e for e in history if e.video_id not in durations or is_deliberate(e.video_id, durations, live)]
    liked = load_liked_ids(a.likes)
    cfg = load_config()

    if a.cmd == "profile":
        print(build_profile(history, liked, **cfg.get("profile", {})))
        return 0

    if a.model:
        cfg["model"] = a.model

    if a.cmd == "discover":
        from .discover import gather, read_interests, to_candidates, write_page, ROOT as _R
        _, queries = read_interests()
        if not queries:
            print("No searches found under '## Searches' in interests.md", file=sys.stderr)
            return 1
        queries = queries[: a.max_queries] if a.max_queries else queries
        saved = _R / "data" / "discover_candidates.json"
        if a.reuse:
            if not saved.exists():
                print("Nothing to reuse yet: run once without --reuse.", file=sys.stderr)
                return 1
            videos = json.loads(saved.read_text(encoding="utf-8"))
            print(f"Re-ranking {len(videos)} saved search results (no YouTube quota used)...")
        else:
            print(f"Searching YouTube for {len(queries)} topics...")
            watched = {e.video_id for e in load_watch_history(a.history)}
            videos = gather(queries, watched, a.per_query, a.days, cache_days=a.cache_days)
            saved.parent.mkdir(exist_ok=True)
            saved.write_text(json.dumps(videos), encoding="utf-8")
        if not videos:
            print("Nothing found.", file=sys.stderr)
            return 1
        # 60 candidates x a reason each needs more room than a 10-video slate.
        cfg["max_tokens"] = max(cfg.get("max_tokens", 4000), 16000)
        res = Recommender(cfg, "claude").rank(history, to_candidates(videos), liked, k=a.top)
        if res.error:
            print(f"error: {res.error}", file=sys.stderr)
            return 1
        (_R / "data").mkdir(exist_ok=True)
        (_R / "data" / "discover_raw.txt").write_text(res.raw_text, encoding="utf-8")
        if not res.parse_ok:
            why = "the reply was cut off at max_tokens" if res.stop_reason == "max_tokens" else "reply wasn't valid JSON"
            print(f"error: Claude's ranking couldn't be read ({why}). Raw reply saved to data/discover_raw.txt",
                  file=sys.stderr)
            return 1
        by_id = {v["video_id"]: v for v in videos}
        # Spread picks across interests: at most --per-topic videos from any one search.
        picks, per_q = [], {}
        for vid in res.ranking:
            q = by_id[vid]["query"]
            if per_q.get(q, 0) < a.per_topic:
                picks.append(by_id[vid])
                per_q[q] = per_q.get(q, 0) + 1
            if len(picks) == a.top:
                break
        for i, v in enumerate(picks, 1):
            print(f"{i:>2}. {v['title']} — {v['channel']} ({v['minutes']} min)\n    https://youtu.be/{v['video_id']}")
            if v["video_id"] in res.reasons:
                print(f"    {res.reasons[v['video_id']]}")
        page = _R / "data" / "discover.html"
        write_page(page, picks, res.reasons)
        print(f"\nPage with thumbnails: {page}")
        return 0

    cands = _load_candidates(a.candidates)
    res = Recommender(cfg, a.backend).rank(history, cands, liked, k=a.top)
    if res.error:
        print(f"error: {res.error}", file=sys.stderr)
        return 1
    if not res.parse_ok:
        print("warning: Claude's ranking couldn't be read; showing candidates in their original order.",
              file=sys.stderr)
    by_id = {c.video_id: c for c in cands}
    top = res.ranking[: a.top]
    if a.json:
        print(json.dumps([{**by_id[v].__dict__, "rank": i + 1, "reason": res.reasons.get(v, "")}
                          for i, v in enumerate(top)], indent=2))
    else:
        for i, v in enumerate(top, 1):
            c = by_id[v]
            print(f"{i:>2}. {c.title} — {c.channel}\n    https://youtu.be/{v}")
            if v in res.reasons:
                print(f"    {res.reasons[v]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
