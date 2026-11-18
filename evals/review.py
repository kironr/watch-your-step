"""Generate evals/cases/review.html so you can eyeball every case before running.

  python -m evals.review
"""
import json
from pathlib import Path

from .report import write_review

HERE = Path(__file__).parent / "cases"


def main():
    cases = [json.loads(l) for l in (HERE / "cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    answers = {a["case_id"]: a for a in map(json.loads, (HERE / "answers.jsonl").read_text(encoding="utf-8").splitlines())}
    split = json.loads((HERE / "split.json").read_text(encoding="utf-8"))
    write_review(HERE / "review.html", cases, answers, split)
    print(f"wrote {HERE / 'review.html'}")


if __name__ == "__main__":
    main()
