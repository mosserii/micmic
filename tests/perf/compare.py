#!/usr/bin/env python3
"""Per-repeat p50 of one stage, per arm, from a bench --ab run, and whether the two
arms' ranges overlap. A delta counts only when they do not.

    .venv/bin/python3 tests/perf/compare.py tests/perf/runs/<run>.json [stage ...] [--only case ...]
"""
import json
import sys

from bench import q


def main() -> int:
    args = sys.argv[2:]
    only = []
    if "--only" in args:
        only = args[args.index("--only") + 1:]
        args = args[:args.index("--only")]
    d = json.load(open(sys.argv[1]))
    stages = args or ["wall_ms", "understand", "jev_extra", "gemini", "other_ms"]
    rows = [r for r in d["rows"] if not only or any(o in r["case"] for o in only)]
    if only:
        print(f"cases matching {only}: {len({(r['case'], r['turn']) for r in rows})} turns")
    reps = sorted({r["repeat"] for r in rows})
    for st in stages:
        per = {arm: [round(q([r[st] for r in rows if r["arm"] == arm and r["repeat"] == k], .5))
                     for k in reps] for arm in ("A", "B")}
        a, b = per["A"], per["B"]
        verdict = ("B faster, ranges apart" if max(b) < min(a) else
                   "B slower, ranges apart" if min(b) > max(a) else "ranges overlap: noise")
        print(f"{st:11s} A {a} (spread {max(a)-min(a)})  B {b} (spread {max(b)-min(b)})  "
              f"median of repeats {sorted(a)[len(a)//2]} -> {sorted(b)[len(b)//2]}  {verdict}")
        # The same turn, A against B, run seconds apart: the drift between runs cancels.
        pa = {(r["case"], r["turn"], r["repeat"]): r[st] for r in rows if r["arm"] == "A"}
        pairs = [(r["repeat"], r[st] - pa[(r["case"], r["turn"], r["repeat"])]) for r in rows
                 if r["arm"] == "B" and (r["case"], r["turn"], r["repeat"]) in pa]
        per_rep = [round(q([dv for k2, dv in pairs if k2 == k], .5)) for k in reps]
        neg = sum(1 for _, dv in pairs if dv < 0)
        pos = sum(1 for _, dv in pairs if dv > 0)
        pv = verdict_p = ("all repeats agree: B faster" if max(per_rep) < 0 else
                          "all repeats agree: B slower" if min(per_rep) > 0 else "repeats disagree")
        print(f"{'':11s} paired B-A median per repeat {per_rep}  ({pv}); "
              f"pairs B<A {neg}, B>A {pos}, of {len(pairs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
