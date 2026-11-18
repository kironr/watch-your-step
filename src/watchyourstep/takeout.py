"""Parse a Google Takeout YouTube export.

Expected files (paths are configurable):
  Takeout/YouTube and YouTube Music/history/watch-history.json
  Takeout/YouTube and YouTube Music/playlists/Liked videos.csv   (optional)

Export watch history as JSON (not HTML) when requesting the Takeout.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

VIDEO_ID_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")


@dataclass(frozen=True)
class WatchEvent:
    video_id: str
    title: str
    channel: str
    time: str  # ISO-8601 UTC
    channel_id: str = ""

    @property
    def dt(self) -> datetime:
        return parse_time(self.time)

    def to_dict(self) -> dict:
        return asdict(self)


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_watch_history(path: str | Path) -> list[WatchEvent]:
    """Return watch events sorted oldest-first, skipping ads, Music and removed videos."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    events: list[WatchEvent] = []
    for item in raw:
        if item.get("header") not in (None, "YouTube"):
            continue  # YouTube Music etc.
        if any("Ads" in d.get("name", "") for d in item.get("details", [])):
            continue  # "From Google Ads"
        url = item.get("titleUrl", "")
        m = VIDEO_ID_RE.search(url)
        subs = item.get("subtitles") or []
        title = item.get("title", "")
        if not m or not subs:
            continue  # removed/private video: no channel, title is just the URL
        if title.startswith("Watched "):
            title = title[len("Watched "):]
        ch_url = subs[0].get("url", "")
        ch_id = ch_url.rsplit("/channel/", 1)[-1] if "/channel/" in ch_url else ""
        events.append(WatchEvent(m.group(1), title.strip(), subs[0].get("name", "").strip(), item["time"], ch_id))
    events.sort(key=lambda e: e.time)
    return events


def load_liked_ids(path: str | Path | None) -> set[str]:
    """Read video IDs from Takeout's 'Liked videos.csv'. Tolerates old and new layouts."""
    if not path or not Path(path).exists():
        return set()
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    # Older exports have metadata rows before the real header; find the header row.
    start = next((i for i, l in enumerate(lines) if "Video ID" in l or "Video Id" in l), None)
    if start is None:
        return set()
    reader = csv.DictReader(lines[start:])
    key = next(k for k in reader.fieldnames or [] if k.strip().lower() == "video id")
    return {row[key].strip() for row in reader if row.get(key, "").strip()}
