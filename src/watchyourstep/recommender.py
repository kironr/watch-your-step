"""Rank candidate videos against a viewer's history using Claude.

Editable surfaces for hillclimbing:
  prompts/system.md          system prompt
  prompts/user_template.md   per-request message ({profile}, {candidates}, {k})
  config.toml                model, max_tokens, extra API params (e.g. effort)
  src/watchyourstep/profile.py       how history is summarized
"""
from __future__ import annotations

import json
import random
import re
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .profile import build_profile
from .takeout import WatchEvent

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Candidate:
    video_id: str
    title: str
    channel: str
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(d["video_id"], d["title"], d.get("channel", ""), d.get("description", ""))


@dataclass
class RankResult:
    ranking: list[str]                      # video_ids, best first, always a full permutation
    reasons: dict[str, str] = field(default_factory=dict)
    parse_ok: bool = True
    error: str | None = None
    stop_reason: str | None = None
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    request: dict = field(default_factory=dict)
    raw_text: str = ""


def load_config(path: Path | None = None) -> dict:
    return tomllib.loads((path or ROOT / "config.toml").read_text(encoding="utf-8"))


def _format_candidates(cands: list[Candidate]) -> str:
    out = []
    for c in cands:
        line = f"[{c.video_id}] {c.title} — {c.channel}"
        if c.description:
            line += f"\n    {c.description[:300]}"
        out.append(line)
    return "\n".join(out)


def _parse_ranking(text: str, valid_ids: list[str]) -> tuple[list[str], dict[str, str], bool]:
    """Extract {"ranking":[{"id":..,"reason":..}]} from the reply; repair to a full permutation."""
    m = re.search(r"\{.*\}", text, re.S)
    ranked, reasons, ok = [], {}, False
    if m:
        try:
            data = json.loads(m.group(0))
            for item in data.get("ranking", []):
                vid = item.get("id") if isinstance(item, dict) else item
                if vid in valid_ids and vid not in ranked:
                    ranked.append(vid)
                    if isinstance(item, dict) and item.get("reason"):
                        reasons[vid] = item["reason"]
            ok = len(ranked) == len(valid_ids)
        except json.JSONDecodeError:
            pass
    ranked += [v for v in valid_ids if v not in ranked]  # unranked go last, in given order
    return ranked, reasons, ok


class Recommender:
    def __init__(self, config: dict | None = None, backend: str | None = None):
        self.cfg = config or load_config()
        self.backend = backend or self.cfg.get("backend", "claude")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=self.cfg.get("max_retries", 4))
        return self._client

    def rank(self, history: list[WatchEvent], candidates: list[Candidate],
             liked_ids: set[str] | None = None, k: int = 10, seed: int | None = None) -> RankResult:
        ids = [c.video_id for c in candidates]
        if self.backend == "random":
            r = ids[:]
            random.Random(seed).shuffle(r)
            return RankResult(r)
        if self.backend == "novelty":  # shortcut detector: unfamiliar channels first
            counts = Counter(e.channel for e in history)
            by_id = {c.video_id: c for c in candidates}
            return RankResult(sorted(ids, key=lambda v: counts[by_id[v].channel]))
        if self.backend == "channel_freq":
            counts = Counter(e.channel for e in history)
            return RankResult(sorted(ids, key=lambda v: -counts[next(c.channel for c in candidates if c.video_id == v)]))
        return self._rank_claude(history, candidates, liked_ids, k)

    def _rank_claude(self, history, candidates, liked_ids, k) -> RankResult:
        p = self.cfg.get("profile", {})
        profile = build_profile(history, liked_ids, **p)
        if self.cfg.get("use_interests", True):
            from .discover import read_interests
            note, _ = read_interests()
            if note:
                profile = "## In the viewer's own words\n" + note + "\n\n" + profile
        system = (ROOT / "prompts/system.md").read_text(encoding="utf-8")
        user = (ROOT / "prompts/user_template.md").read_text(encoding="utf-8").format(
            profile=profile, candidates=_format_candidates(candidates), k=k, n=len(candidates))
        req = dict(model=self.cfg["model"], max_tokens=self.cfg.get("max_tokens", 4000),
                   system=system, messages=[{"role": "user", "content": user}],
                   **self.cfg.get("extra_params", {}))
        t0 = time.time()
        try:
            resp = self.client.messages.create(**req)
        except Exception as e:  # plumbing failure, not model failure
            return RankResult([c.video_id for c in candidates], parse_ok=False,
                              error=f"{type(e).__name__}: {e}", request=req, latency_s=time.time() - t0)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        ranking, reasons, ok = _parse_ranking(text, [c.video_id for c in candidates])
        return RankResult(ranking, reasons, ok, None, resp.stop_reason,
                          {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
                          time.time() - t0, req, text)
