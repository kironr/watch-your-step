"""Generate a synthetic Takeout export + distractor pool so the pipeline runs end-to-end
without your real data or a YouTube API key.

  python scripts/make_demo_data.py            # writes demo/ and evals/data/distractors.jsonl
"""
import csv
import json
import random
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

rng = random.Random(1)
ROOT = Path(__file__).resolve().parents[1]

TOPICS = {  # topic -> (channels, title templates); first 4 topics are the viewer's interests
    "woodworking": (["Plane & Chisel", "Garage Joinery", "Shop Notes"],
                    ["Building a {} from scrap oak", "Hand-cut {} joints, no jigs", "Restoring a 1940s {}"]),
    "rust": (["Ferris Talks", "Borrow Checked", "Systems Hour"],
             ["Writing a {} in Rust", "Async Rust: {} explained", "Zero-copy {} parsing"]),
    "chess": (["Endgame Lab", "Opening Theory Daily"],
              ["The {} gambit, refuted?", "Every {} endgame you must know", "Speedrun to 2000: {}"]),
    "cooking": (["Weeknight Wok", "Bread Theory"],
                ["The only {} recipe you need", "{} three ways", "Sourdough {} troubleshooting"]),
    "crypto": (["MoonBag", "Chart Wizards"], ["{} coin to 100x?", "Why {} will crash"]),
    "gaming": (["RageQuit", "Pixel Plays"], ["{} speedrun world record", "I played {} for 100 hours"]),
    "celebs": (["Hollywood Tea"], ["{} drama EXPLAINED", "What happened to {}?"]),
}
NOUNS = ["bookshelf", "dovetail", "workbench", "parser", "scheduler", "key-value store", "Sicilian",
         "rook", "London", "dumpling", "focaccia", "ramen", "lathe", "allocator", "Caro-Kann", "chili oil"]
INTERESTS = {"woodworking": 4, "rust": 3, "chess": 2, "cooking": 2}


def vid():
    return "".join(rng.choices(string.ascii_letters + string.digits + "-_", k=11))


def make_video(topic, channel=None):
    chans, tmpls = TOPICS[topic]
    return {"video_id": vid(), "title": rng.choice(tmpls).format(rng.choice(NOUNS)),
            "channel": channel or rng.choice(chans), "topic": topic}


def main():
    end = datetime(2026, 11, 1, tzinfo=timezone.utc)
    start = end - timedelta(days=420)
    history, liked = [], []
    new_channel_pool = {"woodworking": ["Timber Frame Tom"], "rust": ["Unsafe Blocks"],
                        "chess": ["Blitz Brain"], "cooking": ["Knife Skills"]}
    t = start
    while t < end:
        t += timedelta(hours=rng.uniform(2, 14))
        topic = rng.choices(list(INTERESTS), weights=INTERESTS.values())[0]
        # After the midpoint, the viewer starts following a few new channels.
        ch = rng.choice(new_channel_pool[topic]) if t > start + timedelta(days=200) and rng.random() < 0.2 else None
        v = make_video(topic, ch)
        history.append((t, v))
        if rng.random() < 0.15:
            liked.append(v["video_id"])
        if rng.random() < 0.05 and history:  # rewatch
            history.append((t + timedelta(minutes=30), rng.choice(history)[1]))
    if rng.random() >= 0:  # sprinkle a few off-taste autoplays
        for _ in range(20):
            history.append((start + timedelta(days=rng.uniform(0, 420)), make_video("gaming")))
    history.sort(key=lambda x: x[0])

    def item(t, v):
        cid = "UC" + str(abs(hash(v["channel"])) % 10**20).zfill(22)
        return {"header": "YouTube", "title": f"Watched {v['title']}",
                "titleUrl": f"https://www.youtube.com/watch?v={v['video_id']}",
                "subtitles": [{"name": v["channel"], "url": f"https://www.youtube.com/channel/{cid}"}],
                "time": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "products": ["YouTube"]}

    out = ROOT / "demo/Takeout/YouTube and YouTube Music"
    (out / "history").mkdir(parents=True, exist_ok=True)
    (out / "playlists").mkdir(parents=True, exist_ok=True)
    (out / "history/watch-history.json").write_text(json.dumps([item(t, v) for t, v in reversed(history)], indent=1), encoding="utf-8")
    with open(out / "playlists/Liked videos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Video ID", "Playlist Video Creation Timestamp"])
        for v in liked:
            w.writerow([v, end.isoformat()])

    # Distractors: same-channel uploads you skipped (hard) + off-taste topics (easy).
    pool = []
    for topic in TOPICS:
        for _ in range(60 if topic in INTERESTS else 40):
            v = make_video(topic)
            v["published"] = (start + timedelta(days=rng.uniform(0, 420))).strftime("%Y-%m-%dT%H:%M:%SZ")
            v["description"] = f"A video about {topic}."
            pool.append(v)
    (ROOT / "evals/data").mkdir(parents=True, exist_ok=True)
    (ROOT / "evals/data/distractors.jsonl").write_text("".join(json.dumps(v) + "\n" for v in pool), encoding="utf-8")
    print(f"{len(history)} watch events, {len(liked)} likes, {len(pool)} distractors → demo/ and evals/data/")


if __name__ == "__main__":
    main()
