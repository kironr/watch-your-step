"""wys discover: search YouTube for your interests, then let Claude rank the results for you.

Reads searches from interests.md, drops Shorts, live streams and anything you've already
watched, ranks the rest against your history + stated interests, and writes an HTML page.
Quota: about 100 units per search plus ~1 per 50 videos for the length/live checks.
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .recommender import ROOT, Candidate
from .youtube import SHORTS_MAX_SECONDS, DailyLimitReached, api_get, fetch_durations, fetch_live_flags

INTERESTS_PATH = ROOT / "interests.md"
SEARCH_CACHE = ROOT / "data" / "search_cache.json"


def read_interests(path: Path = INTERESTS_PATH) -> tuple[str, list[str]]:
    """Return (note for Claude, list of searches)."""
    if not path.exists():
        return "", []
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^## Searches\s*$", text, re.M)
    note, searches = (text[: m.start()], text[m.end():]) if m else (text, "")
    note = "\n".join(l for l in note.splitlines() if not l.startswith("Claude reads") and not l.startswith("`wys discover`")).strip()
    queries = [m.group(1).strip() for m in re.finditer(r"^\s*[-*]\s+(.+)$", searches, re.M)]
    return note, queries


def search(q: str, per_query: int, days: int | None) -> list[dict]:
    kw = dict(part="snippet", q=q, type="video", maxResults=min(per_query, 50), relevanceLanguage="en")
    if days:
        kw["publishedAfter"] = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = api_get("search", **kw)
    return [{"video_id": it["id"]["videoId"], "title": html.unescape(it["snippet"]["title"]),
             "channel": html.unescape(it["snippet"]["channelTitle"]),
             "description": html.unescape(it["snippet"].get("description", "")),
             "published": it["snippet"].get("publishedAt", ""), "query": q}
            for it in data.get("items", [])]


def gather(queries: list[str], watched: set[str], per_query: int = 15, days: int | None = None,
           max_candidates: int = 60, cache_days: float = 3) -> list[dict]:
    # Results per search are cached, so a rerun only searches topics that failed or went stale.
    cache = json.loads(SEARCH_CACHE.read_text(encoding="utf-8")) if SEARCH_CACHE.exists() else {}
    now = time.time()
    pool: dict[str, dict] = {}
    fresh = searched = failed = 0
    quota_gone = False
    for q in queries:
        key = f"{q}|{per_query}|{days or ''}"
        hit = cache.get(key)
        if hit and now - hit["at"] < cache_days * 86400:
            results = hit["items"]
            fresh += 1
        elif quota_gone:
            failed += 1
            continue
        else:
            try:
                results = search(q, per_query, days)
                cache[key] = {"at": now, "items": results}
                searched += 1
                time.sleep(3)  # searches have a per-minute cap; bursts trigger 429s
            except DailyLimitReached as e:
                failed += 1
                print(f"  {e}\n  Skipping the remaining searches; ranking what's already found.")
                quota_gone = True
                continue
            except Exception as e:
                failed += 1
                print(f"  search failed for {q!r}: {e}")
                continue
        for v in results:
            if v["video_id"] not in watched:
                pool.setdefault(v["video_id"], v)
    SEARCH_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SEARCH_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    print(f"  searches: {searched} new, {fresh} reused from the last {cache_days:g} days, {failed} failed")
    if failed:
        print("  Run the same command again later to fill in the failed topics; the others won't be re-searched.")
    ids = list(pool)
    dur = fetch_durations(ids, progress=False)
    live = fetch_live_flags([v for v in ids if (dur.get(v) or 0) > SHORTS_MAX_SECONDS], progress=False)
    keep = [v for v in pool.values()
            if (dur.get(v["video_id"]) or 0) > SHORTS_MAX_SECONDS and not live.get(v["video_id"])]
    for v in keep:
        v["minutes"] = round(dur[v["video_id"]] / 60)
    # Round-robin across queries so every interest is represented before trimming.
    by_q: dict[str, list[dict]] = {}
    for v in keep:
        by_q.setdefault(v["query"], []).append(v)
    out, i = [], 0
    while len(out) < max_candidates and any(i < len(l) for l in by_q.values()):
        for l in by_q.values():
            if i < len(l) and len(out) < max_candidates:
                out.append(l[i])
        i += 1
    print(f"  {len(pool)} found, {len(keep)} long-form and unwatched, ranking {len(out)}")
    return out


def to_candidates(videos: list[dict]) -> list[Candidate]:
    return [Candidate(v["video_id"], v["title"], v["channel"],
                      f"[{v['minutes']} min] {v['description']}") for v in videos]


def write_page(path: Path, picks: list[dict], reasons: dict[str, str]) -> None:
    rows = []
    for i, v in enumerate(picks, 1):
        vid = v["video_id"]
        rows.append(
            f'<a class="card" href="https://www.youtube.com/watch?v={vid}" target="_blank">'
            f'<img src="https://i.ytimg.com/vi/{vid}/mqdefault.jpg" alt="">'
            f'<div><div class="t">{i}. {html.escape(v["title"])}</div>'
            f'<div class="m">{html.escape(v["channel"])} · {v["minutes"]} min · {v["published"][:10]}</div>'
            f'<div class="r">{html.escape(reasons.get(vid, ""))}</div>'
            f'<div class="q">from search: {html.escape(v["query"])}</div></div></a>')
    css = ("body{font:15px/1.45 system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}"
           ".card{display:flex;gap:14px;padding:12px 0;border-bottom:1px solid #eee;color:inherit;text-decoration:none}"
           ".card:hover{background:#fafafa}img{width:240px;height:135px;object-fit:cover;border-radius:8px;flex:none}"
           ".t{font-weight:600}.m,.q{color:#666;font-size:13px}.r{margin-top:6px}")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<!doctype html><meta charset=utf-8><title>Watch Your Step picks</title><style>{css}</style>"
                    f"<h1>Watch Your Step: your picks</h1><p class=m>Generated {stamp}</p>" + "".join(rows),
                    encoding="utf-8")
