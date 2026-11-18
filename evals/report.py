"""Static HTML pages: run results and the pre-run case review."""
from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path

CSS = """body{font:14px/1.45 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}
table{border-collapse:collapse;width:100%;margin:1rem 0}td,th{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}
.pos{background:#e7f6ea}.warn{background:#fff4d6;padding:6px 10px;border-radius:4px;margin:4px 0}
.tag{display:inline-block;background:#eef;border-radius:3px;padding:0 5px;margin-right:4px;font-size:12px}
details{margin:.4rem 0}summary{cursor:pointer}code{font-size:12px}"""


def _e(x) -> str:
    return html.escape(str(x))


def write_report(out: Path, summary: dict, rows: list[dict], cases: dict) -> None:
    prim = summary["primary"]
    h = [f"<!doctype html><meta charset=utf-8><title>Eval: {_e(out.name)}</title><style>{CSS}</style>",
         f"<h1>{_e(out.name)}</h1><p>backend <b>{_e(summary['backend'])}</b>, model <b>{_e(summary['model'])}</b>, "
         f"primary metric <b>{prim}</b></p><table><tr><th>split</th><th>cases</th><th>{prim}</th><th>95% CI</th>"
         "<th>recall@3</th><th>hit@1</th><th>noise (sd between reps)</th><th>errors / parse / trunc</th>"
         "<th>tokens in / out</th><th>cost</th></tr>"]
    for sp, s in summary["splits"].items():
        nf = s["noise_floor_sd_between_reps"]
        h.append(f"<tr><td>{sp}</td><td>{s['n_cases']}</td><td><b>{s[prim]:.3f}</b></td>"
                 f"<td>[{s['ci95'][0]:.3f}, {s['ci95'][1]:.3f}]</td><td>{s['metrics']['recall@3']:.3f}</td>"
                 f"<td>{s['metrics']['hit@1']:.3f}</td><td>{'—' if nf is None else f'{nf:.3f}'}</td>"
                 f"<td>{s['api_errors']} / {s['parse_failures']} / {s['truncated']}</td>"
                 f"<td>{s['tokens']['input']:,} / {s['tokens']['output']:,}</td>"
                 f"<td>{'—' if s.get('cost_per_case_usd') is None else '$%.2f' % (s['cost_per_case_usd'] * s['n_runs'])}</td></tr>")
    h.append("</table>")
    for sp, s in summary["splits"].items():
        h += [f"<div class=warn>[{sp}] {_e(w)}</div>" for w in s["warnings"]]
    if "all" in summary["splits"] and summary["splits"]["all"]["by_tag"]:
        h.append("<h3>By tag</h3><table>" + "".join(
            f"<tr><td>{_e(t)}</td><td>{v:.3f}</td></tr>" for t, v in summary["splits"]["all"]["by_tag"].items()) + "</table>")

    by_case = defaultdict(list)
    for r in rows:
        by_case[r["case_id"]].append(r)
    h.append("<h2>Per case</h2>")
    for cid, rs in sorted(by_case.items(), key=lambda kv: sum(r["scores"][prim] for r in kv[1]) / len(kv[1])):
        c = cases[cid]
        mean = sum(r["scores"][prim] for r in rs) / len(rs)
        title = {x["video_id"]: f"{x['title']} — {x['channel']}" for x in c["candidates"]}
        r0 = rs[0]
        rows_html = "".join(
            f"<tr class={'pos' if v in r0['positives'] else ''}><td>{i+1}</td><td>{_e(title[v])}</td>"
            f"<td>{_e(r0['reasons'].get(v, ''))}</td></tr>" for i, v in enumerate(r0["ranking"]))
        tags = "".join(f"<span class=tag>{_e(t)}</span>" for t in c["tags"])
        h.append(f"<details><summary><b>{mean:.2f}</b> {cid} [{rs[0]['split']}] {tags} "
                 f"reps: {', '.join(f'{r['scores'][prim]:.2f}' for r in rs)}</summary>"
                 f"<p>rep 0 ranking (green = actually watched)</p><table>{rows_html}</table></details>")
    (out / "report.html").write_text("\n".join(h), encoding="utf-8")


def write_review(path: Path, cases: list[dict], answers: dict, split: dict) -> None:
    which = {c: s for s in ("train", "test") for c in split[s]}
    h = [f"<!doctype html><meta charset=utf-8><title>Eval case review</title><style>{CSS}</style>",
         f"<h1>Eval case review ({len(cases)} cases)</h1>",
         "<p>Check each slate before trusting the eval. Green rows are the videos you really watched next. "
         "Ask: could a human who knew your history pick them? If a positive was an accidental autoplay, "
         "or a distractor is something you'd obviously watch, drop the case: add its id "
         "(e.g. <code>c20261109</code>) on its own line in <code>evals/cases/excluded.txt</code>. "
         "The runner skips those.</p>"]
    for c in cases:
        pos = set(answers[c["case_id"]]["positives"])
        tags = "".join(f"<span class=tag>{_e(t)}</span>" for t in c["tags"])
        rows = "".join(f"<tr class={'pos' if x['video_id'] in pos else ''}><td>{_e(x['title'])}</td>"
                       f"<td>{_e(x['channel'])}</td></tr>" for x in c["candidates"])
        recent = "".join(f"<li>{_e(e['title'])} — {_e(e['channel'])}</li>" for e in c["history"][-15:][::-1])
        h.append(f"<details><summary>{c['case_id']} [{which[c['case_id']]}] cutoff {c['cutoff'][:10]} {tags}</summary>"
                 f"<table>{rows}</table><p>Last 15 watched before cutoff:</p><ul>{recent}</ul></details>")
    path.write_text("\n".join(h), encoding="utf-8")
