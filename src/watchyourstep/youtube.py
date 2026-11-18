"""Minimal YouTube Data API v3 helpers (set YOUTUBE_API_KEY)."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.googleapis.com/youtube/v3/"
ROOT = Path(__file__).resolve().parents[2]
DURATIONS_PATH = ROOT / "data" / "durations.json"
LIVE_PATH = ROOT / "data" / "live.json"
SHORTS_MAX_SECONDS = 180  # Shorts can be up to 3 minutes


class DailyLimitReached(RuntimeError):
    """YouTube's per-day cap is used up (e.g. 100 searches/day); retrying today won't help."""


def api_get(endpoint: str, retries: int = 2, **params) -> dict:
    """GET a YouTube API endpoint. Retries short rate limits and server errors; stops at daily caps."""
    if "YOUTUBE_API_KEY" not in os.environ:
        raise SystemExit("Set YOUTUBE_API_KEY (Google Cloud console > YouTube Data API v3 > API key).")
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            daily = any(k in body for k in ("quotaExceeded", "dailyLimitExceeded", "per day", "perDay"))
            if e.code == 403 or daily or (e.code == 429 and attempt == retries):
                raise DailyLimitReached(
                    f"YouTube refused the {endpoint} request ({e.code}). The daily limit is probably used up "
                    "(searches are capped at 100 per day); it resets at midnight Pacific time.") from e
            if e.code in (429, 500, 502, 503):
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = int(ra) if ra and str(ra).isdigit() else 20 * (attempt + 1)
                print(f"  YouTube said {e.code}; waiting {wait}s and retrying...")
                time.sleep(wait)
                continue
            raise


def parse_iso_duration(s: str) -> int:
    """'PT1H2M3S' -> 3723. Live/premiere placeholders ('P0D') -> 0."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def load_durations(path: Path = DURATIONS_PATH) -> dict[str, int | None]:
    """video_id -> seconds (None = video unavailable/deleted)."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def fetch_durations(ids, path: Path = DURATIONS_PATH, progress=True) -> dict[str, int | None]:
    """Look up durations for ids not already cached; 1 quota unit per 50 videos."""
    cache = load_durations(path)
    todo = sorted({i for i in ids if i not in cache})
    path.parent.mkdir(parents=True, exist_ok=True)
    for n, start in enumerate(range(0, len(todo), 50)):
        batch = todo[start:start + 50]
        data = api_get("videos", part="contentDetails", id=",".join(batch), maxResults=50)
        found = {it["id"]: parse_iso_duration(it["contentDetails"].get("duration", "")) for it in data.get("items", [])}
        for vid in batch:
            cache[vid] = found.get(vid)  # None if deleted/private
        if n % 20 == 0:
            path.write_text(json.dumps(cache), encoding="utf-8")
            if progress:
                print(f"  {min(start + 50, len(todo))}/{len(todo)} looked up")
    path.write_text(json.dumps(cache), encoding="utf-8")
    return cache


def is_long_form(video_id: str, durations: dict, min_seconds: int = SHORTS_MAX_SECONDS) -> bool:
    d = durations.get(video_id)
    return bool(d) and d > min_seconds


def load_live(path: Path = LIVE_PATH) -> dict[str, bool]:
    """video_id -> True if it was a live broadcast (or its replay)."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def fetch_live_flags(ids, path: Path = LIVE_PATH, progress=True) -> dict[str, bool]:
    """Mark videos that were live streams/premieres; 1 quota unit per 50 videos."""
    cache = load_live(path)
    todo = sorted({i for i in ids if i not in cache})
    path.parent.mkdir(parents=True, exist_ok=True)
    for n, start in enumerate(range(0, len(todo), 50)):
        batch = todo[start:start + 50]
        data = api_get("videos", part="liveStreamingDetails,snippet", id=",".join(batch), maxResults=50)
        live = {it["id"] for it in data.get("items", [])
                if "liveStreamingDetails" in it or it.get("snippet", {}).get("liveBroadcastContent") in ("live", "upcoming")}
        for vid in batch:
            cache[vid] = vid in live
        if n % 20 == 0:
            path.write_text(json.dumps(cache), encoding="utf-8")
            if progress:
                print(f"  {min(start + 50, len(todo))}/{len(todo)} checked for live")
    path.write_text(json.dumps(cache), encoding="utf-8")
    return cache


def is_deliberate(video_id: str, durations: dict, live: dict, min_seconds: int = SHORTS_MAX_SECONDS) -> bool:
    """Long-form and not a live stream/replay: the kind of view that reflects a choice."""
    return is_long_form(video_id, durations, min_seconds) and not live.get(video_id, False)
