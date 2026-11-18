"""Turn watch history into a compact taste profile for the ranking prompt.

This file is an editable surface for hillclimbing (harness code). Keep it free of
anything eval-specific: it only ever sees history *before* the moment of recommendation.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from .takeout import WatchEvent


def build_profile(
    history: list[WatchEvent],
    liked_ids: set[str] | None = None,
    window_days: int = 180,
    top_channels: int = 25,
    recent_titles: int = 60,
) -> str:
    liked_ids = liked_ids or set()
    if not history:
        return "No viewing history available."
    end = history[-1].dt
    recent = [e for e in history if e.dt >= end - timedelta(days=window_days)] or history

    chan_counts = Counter(e.channel for e in recent)
    title_counts = Counter(e.video_id for e in recent)
    by_id = {e.video_id: e for e in recent}

    lines = [f"## Most-watched channels (last {window_days} days, watch count)"]
    lines += [f"- {c} ({n})" for c, n in chan_counts.most_common(top_channels)]

    rewatched = [by_id[v] for v, n in title_counts.most_common() if n > 1][:15]
    if rewatched:
        lines.append("\n## Rewatched (strong signal)")
        lines += [f"- {e.title} — {e.channel}" for e in rewatched]

    liked = [e for e in recent if e.video_id in liked_ids]
    if liked:
        lines.append("\n## Liked")
        seen = set()
        for e in reversed(liked):
            if e.video_id not in seen:
                seen.add(e.video_id)
                lines.append(f"- {e.title} — {e.channel}")
            if len(seen) >= 30:
                break

    lines.append("\n## Recently watched (newest first)")
    seen = set()
    for e in reversed(recent):
        if e.video_id in seen:
            continue
        seen.add(e.video_id)
        lines.append(f"- {e.title} — {e.channel}")
        if len(seen) >= recent_titles:
            break
    return "\n".join(lines)
