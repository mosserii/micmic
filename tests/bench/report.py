#!/usr/bin/env python3
"""Summarise a MicMic Bench run: per-category pass rate, spread across repeats,
latency (p50/p95 per turn) and cost, plus every failing case.

    uv run python tests/bench/report.py --run-id v1-20261001

Writes <run>/summary.md and <run>/failures.json next to results.jsonl. Reads only the
run directory; makes no calls.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))
os.environ.setdefault("MICMIC_STATE_DIR", tempfile.mkdtemp(prefix="micmic-report-"))
import grade as G  # noqa: E402  (pure; savta only for latinize)

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "bench-runs"
JEV_PER_M_IN = 0.042            # savta/jev.py cost_usd
# Gemini list price is not in the repo; flash-lite class rates, stated as assumed.
GEM_PER_M_IN, GEM_PER_M_OUT = 0.10, 0.40
ORDER = ["messaging", "multi_turn", "safety", "ambiguity", "calendar_reminders", "media",
         "apps", "screen", "knowledge", "live_info", "web_tasks", "prefs_memory", "guide",
         "chitchat", "do_nothing"]


def pct(xs, q):
    if not xs:
        return 0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, round(q * (len(xs) - 1))))
    return xs[k]


def table(rows, repeats, title):
    """rows: results for one slice. Pass rate per repeat, mean, min-max spread."""
    by_cat = collections.defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    out = [f"### {title}", "",
           "| category | n | pass (mean) | per repeat | spread | p50 ms | p95 ms | Gemini ms p50 |",
           "|---|---|---|---|---|---|---|---|"]
    cats = [c for c in ORDER if c in by_cat] + sorted(set(by_cat) - set(ORDER))
    allr = []
    for cat in cats + ["ALL"]:
        rs = rows if cat == "ALL" else by_cat[cat]
        per = []
        for rep in range(1, repeats + 1):
            x = [r for r in rs if r["repeat"] == rep]
            if x:
                per.append(100.0 * sum(r["pass"] for r in x) / len(x))
        n = len({r["id"] for r in rs})
        walls = [t["timing"]["wall_ms"] for r in rs for t in r["turns"]]
        gms = [t["timing"]["gem_ms"] for r in rs for t in r["turns"] if t["timing"]["gem_ms"]]
        mean = statistics.mean(per) if per else 0
        spread = (max(per) - min(per)) if per else 0
        line = (f"| {'**ALL**' if cat == 'ALL' else cat} | {n} | {mean:.1f}% | "
                f"{' / '.join(f'{p:.0f}' for p in per)} | {spread:.1f} pts | "
                f"{pct(walls, .5):.0f} | {pct(walls, .95):.0f} | {pct(gms, .5) if gms else '-'} |")
        out.append(line)
        allr.append((cat, mean, spread))
    return "\n".join(out), allr


def load_cases() -> dict:
    cases = {}
    for name in ("cases.jsonl", "hard_cases.jsonl"):
        for line in (HERE / name).read_text().splitlines():
            if line.strip():
                c = json.loads(line)
                if name == "hard_cases.jsonl" and not c["id"].startswith("hard"):
                    c["id"] = "hard-" + c["id"]
                cases[c["id"]] = c
    return cases


def regrade(rows: list, cases: dict) -> tuple[list, dict]:
    """Grade every stored turn again against the expectations in the repo now. The
    outputs are the run's own; only the yardstick moves, and only when a case was wrong."""
    stats = collections.Counter()
    for r in rows:
        c = cases.get(r["id"])
        if c is None:
            stats["case_missing"] += 1
            continue
        ok_all = True
        for i, t in enumerate(r["turns"]):
            exp = (c["turns"][i].get("expect") if i < len(c["turns"]) else None) or {}
            if t["did"] in ("EXCEPTION", "TIMEOUT", "RUNNER_ERROR"):
                fails = [f for f in t["fails"] if f.startswith(t["did"])] or t["fails"]
            else:
                stored = t.get("judge")
                fails, extra = G.grade(exp, t["did"], t["say"], t["detail"], t.get("asked_back"),
                                       t["effects"], t["sends"], t["timing"]["wall_ms"],
                                       t.get("pending_after"), stored)
                if exp.get("rubric") and not stored and not fails:
                    stats["rubric_not_judged"] += 1
            if fails != t["fails"]:
                stats["turns_changed"] += 1
            t["fails_live"] = t["fails"]
            t["fails"] = fails
            ok_all = ok_all and not fails
        if ok_all != r["pass"]:
            stats["fail_to_pass" if ok_all else "pass_to_fail"] += 1
        r["pass_live"], r["pass"] = r["pass"], ok_all
    return rows, dict(stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--live-grades", action="store_true",
                    help="report the grades made during the run, not a regrade")
    a = ap.parse_args()
    d = Path(a.out) / a.run_id
    meta = json.loads((d / "meta.json").read_text())
    rows = [json.loads(l) for l in (d / "results.jsonl").read_text().splitlines() if l.strip()]
    regraded = {}
    if not a.live_grades:
        rows, regraded = regrade(rows, load_cases())
    repeats = max(r["repeat"] for r in rows)
    spent = meta.get("spent", {})
    jev_cost = spent.get("jev_in_tok", 0) * JEV_PER_M_IN / 1e6
    gem_cost = (spent.get("gem_in_tok", 0) * GEM_PER_M_IN + spent.get("gem_out_tok", 0) * GEM_PER_M_OUT) / 1e6
    turns = [t for r in rows for t in r["turns"]]
    jev_n = sum(t["timing"]["jev_n"] for t in turns)

    md = [f"# MicMic Bench v1: run {a.run_id}", "",
          f"- Question set: **{meta['question_set']}**, n = {meta['n_cases']} cases "
          f"({', '.join(meta['sources'])}), {len(turns) // repeats} turns per repeat",
          f"- Repeats: {repeats} (complete: {len(rows)} case runs)",
          f"- Commit: `{meta['commit']}` (main, savta 1.2.2); runner `tests/bench/run_bench.py` on feat/bench",
          f"- Models: Jev `{meta['jev_model']}` ({meta.get('jev_mode')}), Gemini `{meta['gemini_model']}` "
          f"({meta.get('llm_mode')}), judge `{meta['judge_model']}`",
          f"- Client `native`, activation `push` (do_nothing cases: `wake`), cancel window "
          f"{meta['cancel_window_s']:.0f}s (never fires), gates on, all side effects walled",
          f"- Started {meta['started']}, finished {meta.get('finished', '(running)')}"
          + (f", STOPPED: {meta['stopped']}" if meta.get("stopped") else ""),
          f"- Calls: Jev {spent.get('jev_live')} live; Gemini {spent.get('gem_live')} live "
          f"(of which judge {spent.get('judge_live')}), {spent.get('gem_memo')} replayed from the "
          f"repeat-1 memo; osascript escaped: {meta.get('osa_escaped', '?')}",
          f"- Cost: Jev ${jev_cost:.4f} (input tokens x $0.042/M); Gemini ~${gem_cost:.4f} "
          f"(ASSUMED $0.10/M in, $0.40/M out); per case run: {jev_n / max(1, len(rows)):.2f} Jev calls",
          f"- Grading: {'as graded live' if a.live_grades else 'regraded offline against the expectations in the repo now'}"
          + (f" ({regraded})" if regraded else ""),
          ""]
    t_all, _ = table(rows, repeats, "All cases (bench + hard)")
    t_b, _ = table([r for r in rows if r.get("source") == "cases.jsonl"], repeats, "Bench set (cases.jsonl)")
    t_h, _ = table([r for r in rows if r.get("source") != "cases.jsonl"], repeats,
                   "Hard cases from real assistant failures (hard_cases.jsonl)")
    md += [t_all, "", t_b, "", t_h, ""]
    by_lang = collections.defaultdict(list)
    for r in rows:
        by_lang[r["lang"]].append(r)
    md.append("### By language\n\n| lang | case runs | pass |\n|---|---|---|")
    for lg, rs in sorted(by_lang.items()):
        md.append(f"| {lg} | {len(rs)} | {100 * sum(r['pass'] for r in rs) / len(rs):.1f}% |")
    md.append("")
    # Latency by component, turns that made a live call.
    srv = [t["timing"]["server_ms"] or t["timing"]["wall_ms"] for t in turns]
    jms = [t["timing"]["jev_ms"] for t in turns if t["timing"]["jev_n"]]
    gms = [t["timing"]["gem_ms"] for t in turns if t["timing"]["gem_n"] or t["timing"]["gem_memo"]]
    md += ["### Latency per turn (ms)", "", "| part | n | p50 | p95 | max |", "|---|---|---|---|---|",
           f"| server (handle) | {len(srv)} | {pct(srv, .5)} | {pct(srv, .95)} | {max(srv)} |",
           f"| Jev (sum of calls in the turn) | {len(jms)} | {pct(jms, .5)} | {pct(jms, .95)} | {max(jms or [0])} |",
           f"| Gemini (turns that used it) | {len(gms)} | {pct(gms, .5)} | {pct(gms, .95)} | {max(gms or [0])} |",
           ""]

    # Per case across repeats.
    by_id = collections.defaultdict(list)
    for r in rows:
        by_id[r["id"]].append(r)
    fails = []
    for cid, rs in by_id.items():
        n_pass = sum(r["pass"] for r in rs)
        if n_pass == len(rs):
            continue
        rs = sorted(rs, key=lambda r: r["repeat"])
        fails.append({"id": cid, "category": rs[0]["category"], "impact": rs[0]["impact"],
                      "lang": rs[0]["lang"], "source": rs[0].get("source"),
                      "passed": n_pass, "runs": len(rs),
                      "turns": [{"say_in": t["say_in"],
                                 "per_repeat": [{"did": r["turns"][i]["did"] if i < len(r["turns"]) else None,
                                                 "say": r["turns"][i]["say"][:220] if i < len(r["turns"]) else None,
                                                 "fails": r["turns"][i]["fails"] if i < len(r["turns"]) else None,
                                                 "sends": r["turns"][i]["sends"] if i < len(r["turns"]) else None,
                                                 "effects": r["turns"][i]["effects"] if i < len(r["turns"]) else None,
                                                 "ms": r["turns"][i]["timing"]["wall_ms"] if i < len(r["turns"]) else None}
                                                for r in rs]}
                                for i, t in enumerate(rs[0]["turns"])]})
    (d / "failures.json").write_text(json.dumps(fails, ensure_ascii=False, indent=1))
    md += [f"### Failing cases: {len(fails)} of {len(by_id)} failed at least once "
           f"({sum(1 for f in fails if f['passed'] == 0)} in every repeat, "
           f"{sum(1 for f in fails if f['passed'] > 0)} flaky)", ""]
    imp_rank = {"wrong_send": 0, "wrong_action": 1, "needless_ask": 2, "slow": 3, "wording": 4}
    for f in sorted(fails, key=lambda f: (imp_rank.get(f["impact"], 5), f["passed"], f["id"])):
        first, where = "", f["turns"][0]["say_in"]
        for t in f["turns"]:
            hit = next((x for pr in t["per_repeat"] for x in (pr["fails"] or [])), "")
            if hit:
                first, where = hit, t["say_in"]
                break
        md.append(f"- `{f['id']}` [{f['impact']}] {f['passed']}/{f['runs']} pass: "
                  f"\"{where}\" -> {first[:160]}")
    (d / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md[:60]))
    print(f"\nwrote {d / 'summary.md'} and {d / 'failures.json'}")


if __name__ == "__main__":
    main()
