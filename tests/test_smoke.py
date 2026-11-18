"""Offline tests: parsing, grading, and the Claude code path with a stubbed client."""
import json
from types import SimpleNamespace

from evals.grader import grade
from watchyourstep.recommender import Candidate, Recommender, _parse_ranking, load_config
from watchyourstep.takeout import WatchEvent


def test_grade_perfect_and_worst():
    assert grade(["a", "b", "c", "d"], ["a", "b"])["ndcg@10"] == 1.0
    assert grade(["c", "d", "a", "b"], ["a", "b"])["hit@1"] == 0.0


def test_parse_repairs_partial_and_junk():
    ids = ["x", "y", "z"]
    r, reasons, ok = _parse_ranking('sure! {"ranking":[{"id":"z","reason":"r"},{"id":"bogus"}]}', ids)
    assert r == ["z", "x", "y"] and not ok and reasons == {"z": "r"}
    assert _parse_ranking("no json", ids)[0] == ids


class StubClient:
    def __init__(self):
        self.messages = self
        self.last = None

    def create(self, **req):
        self.last = req
        ids = [l.split("]")[0][1:] for l in req["messages"][0]["content"].splitlines() if l.startswith("[")]
        body = json.dumps({"ranking": [{"id": i, "reason": "fits"} for i in reversed(ids)]})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def test_claude_path_with_stub():
    rec = Recommender(load_config(), backend="claude")
    rec._client = StubClient()
    hist = [WatchEvent("v" * 11, "Hand-cut dovetails", "Plane & Chisel", "2026-01-01T00:00:00Z")]
    cands = [Candidate("a" * 11, "Lathe basics", "Shop Notes"), Candidate("b" * 11, "Crypto 100x", "MoonBag")]
    res = rec.rank(hist, cands)
    assert res.parse_ok and res.ranking == ["b" * 11, "a" * 11]
    assert "Hand-cut dovetails" in rec._client.last["messages"][0]["content"]
    assert rec._client.last["model"] == load_config()["model"]


def test_discover_end_to_end(tmp_path, monkeypatch):
    """discover: searches -> drop watched/Shorts/live -> Claude ranks -> HTML page."""
    import watchyourstep.discover as d
    import watchyourstep.youtube as yt

    note, queries = d.read_interests()
    assert "Claude" in note and "## Searches" not in note and len(queries) >= 10

    vids = {"aaaaaaaaaaa": 1800, "bbbbbbbbbbb": 45, "ccccccccccc": 900, "ddddddddddd": 3600, "eeeeeeeeeee": 600}

    def fake_api(endpoint, **kw):
        if endpoint == "search":
            return {"items": [{"id": {"videoId": v}, "snippet": {"title": f"{kw['q']} {v[:2]}", "channelTitle": "Ch",
                     "description": "d", "publishedAt": "2026-01-01T00:00:00Z"}} for v in vids]}
        ids = kw["id"].split(",")
        if "contentDetails" in kw["part"]:
            return {"items": [{"id": i, "contentDetails": {"duration": f"PT{vids[i]}S"}} for i in ids]}
        return {"items": [{"id": i, **({"liveStreamingDetails": {}} if i == "ddddddddddd" else {}),
                           "snippet": {"liveBroadcastContent": "none"}} for i in ids]}
    monkeypatch.setattr(yt, "api_get", fake_api)
    monkeypatch.setattr(d, "api_get", fake_api)
    monkeypatch.setattr(d, "SEARCH_CACHE", tmp_path / "search_cache.json")
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    monkeypatch.setattr(yt, "DURATIONS_PATH", tmp_path / "dur.json")
    monkeypatch.setattr(yt, "LIVE_PATH", tmp_path / "live.json")
    monkeypatch.setattr(d, "fetch_durations", lambda ids, progress=False: yt.fetch_durations(ids, tmp_path / "dur.json", progress))
    monkeypatch.setattr(d, "fetch_live_flags", lambda ids, progress=False: yt.fetch_live_flags(ids, tmp_path / "live.json", progress))

    got = d.gather(queries[:2], watched={"eeeeeeeeeee"})
    ids = {v["video_id"] for v in got}
    assert ids == {"aaaaaaaaaaa", "ccccccccccc"}  # Short, live stream and watched video dropped

    rec = Recommender(load_config(), backend="claude")
    rec._client = StubClient()
    hist = [WatchEvent("v" * 11, "Claude Code subagents deep dive", "Some Channel", "2026-01-01T00:00:00Z")]
    res = rec.rank(hist, d.to_candidates(got))
    assert res.parse_ok
    assert "In the viewer's own words" in rec._client.last["messages"][0]["content"]
    page = tmp_path / "p.html"
    d.write_page(page, [next(v for v in got if v["video_id"] == i) for i in res.ranking], res.reasons)
    assert "i.ytimg.com" in page.read_text(encoding="utf-8")


def test_youtube_retries_429(monkeypatch):
    import io, urllib.error
    import watchyourstep.youtube as yt
    calls = []

    def fake_urlopen(url, timeout=30):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        return io.BytesIO(b'{"items": []}')
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(yt.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(yt.time, "sleep", lambda s: None)
    assert yt.api_get("search", q="x") == {"items": []} and len(calls) == 3


def test_search_cache_only_retries_failed_topics(tmp_path, monkeypatch):
    import watchyourstep.discover as d
    monkeypatch.setattr(d, "SEARCH_CACHE", tmp_path / "c.json")
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    monkeypatch.setattr(d, "fetch_durations", lambda ids, progress=False: {i: 600 for i in ids})
    monkeypatch.setattr(d, "fetch_live_flags", lambda ids, progress=False: {})
    searched, b_tries = [], []

    def fake_search(q, n, days):
        searched.append(q)
        if q == "b":
            b_tries.append(1)
        if q == "b" and len(b_tries) == 1:
            raise RuntimeError("HTTP Error 429: Too Many Requests")
        return [{"video_id": (q * 11)[:11], "title": q, "channel": "c", "description": "",
                 "published": "", "query": q}]
    monkeypatch.setattr(d, "search", fake_search)
    first = d.gather(["a", "b", "c"], set())
    assert {v["query"] for v in first} == {"a", "c"}
    searched.clear()
    second = d.gather(["a", "b", "c"], set())
    assert searched == ["b"] and {v["query"] for v in second} == {"a", "b", "c"}


def test_daily_search_cap_stops_quickly(tmp_path, monkeypatch):
    import io, urllib.error
    import watchyourstep.youtube as yt
    import watchyourstep.discover as d
    calls = []

    def always_429(url, timeout=30):
        calls.append(url)
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b'{"error":{}}'))
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(yt.urllib.request, "urlopen", always_429)
    monkeypatch.setattr(yt.time, "sleep", lambda s: None)
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    monkeypatch.setattr(d, "SEARCH_CACHE", tmp_path / "c.json")
    monkeypatch.setattr(d, "fetch_durations", lambda ids, progress=False: {})
    monkeypatch.setattr(d, "fetch_live_flags", lambda ids, progress=False: {})
    d.gather(["a", "b", "c", "d"], set())
    assert len(calls) == 3  # one topic: first try + 2 retries, then it stops instead of trying b, c, d
