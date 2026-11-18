# Watch Your Step: notes for Claude Code

Personal YouTube recommender. `src/watchyourstep/` ranks candidate videos against a viewer's
Takeout history using Claude. `evals/` is a time-travel eval: freeze history at a cutoff,
rank a slate, score against what the viewer actually watched next.

## Running

```
pip install -e .
python -m evals.run --label <name> --reps 3      # all cases; reports train + test separately
python -m evals.compare <baseline> <candidate>   # applies the keep/revert rule
python -m pytest -q tests
```

## Hillclimbing (`/claude-api hillclimb`)

**Default goal:** raise test `ndcg@10`. Once that plateaus, switch to cost: same score on a
cheaper model or lower effort (fill in `[pricing]` in config.toml first).

**Editable surfaces, cheapest first:**
1. `prompts/system.md`, `prompts/user_template.md`
2. `config.toml`: `model`, `max_tokens`, `[extra_params]`, `[profile]`
3. `src/watchyourstep/profile.py`: how history is summarized (harness code)

**Not editable:** `evals/` (cases, answers, grader, runner, split) and `tests/`. If a case or
the grader looks wrong, stop and report it. Don't patch around it.

**Rules (overfitting safeguards):**
- Read transcripts from `evals/results/<label>/transcripts/train/` only. Never open
  `transcripts/test/` or `evals/cases/answers.jsonl`.
- Never paste case content into prompts or code: no video titles, channel names or IDs
  from the eval. Patches must state general rules about taste inference.
- The recommender must never read anything under `evals/`. Answers stay in the runner process.
- One root-cause patch per round. Rewrite a whole section rather than tweaking one word.
- Before the first round, run the baseline with `--reps 3`. If `noise_floor_sd_between_reps`
  is larger than the gain you would act on, add reps or cases before optimizing.
- After 2–3 flat rounds, stop editing. Bucket every remaining train failure by cause
  (ambiguous slate, autoplay positive, distractor the viewer would obviously like, real
  model miss) and report the buckets.
- Finish on the version that scored best on test. If the gain is within the CI, say so
  and recommend against merging.

## Known biases in the eval
- Decoys are matched to the rate at which real watches come from unfamiliar channels, so
  channel familiarity carries no signal. Check: `--backend novelty` and `--backend channel_freq`
  should both score about the same as `--backend random`. If either clearly beats random, the eval
  has a shortcut; fix the cases, don't exploit it.
- Long-form only by default. Shorts (≤180 s, from `data/durations.json`) are dropped from
  history, answers and decoys, because a Short being "watched" is mostly feed autoplay.
- Positives are videos that were *watched*, which includes autoplay. Review cases with
  `python -m evals.review` and drop doubtful ones.
- Candidates show only title + channel, because Takeout has no descriptions for
  positives. Showing descriptions on distractors only would leak the answer.
