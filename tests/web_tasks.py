"""Real multi-step tasks on real websites, run several times each.

Not part of the regression suite: it needs the network, it takes minutes, and real
sites change under it. Run it by hand to see how reliable the browser agent actually is:

    .venv/bin/python3 tests/web_tasks.py            # 3 runs of each task
    .venv/bin/python3 tests/web_tasks.py 1          # one run of each, quicker
    .venv/bin/python3 tests/web_tasks.py 3 flight   # just one task

Nothing is ever bought. Card, CVV, password and identity fields are refused in plain
Python inside act(), and so is the final purchase button, so a wrong model pick cannot
spend money. The checkout task exists to prove exactly that: it fills a basket, walks
to the order form, and must stop.

One run does not tell you anything — these sites are genuinely non-deterministic. Read
the N/3 line, not a single result.
"""
from __future__ import annotations
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from savta.jev import Jev
from savta.llm import LLM
from savta.actions import web

# Every task says "do not book / do not buy" out loud, because that is exactly how the
# owner would phrase it, and the refusal must not depend on her having said it.
TASKS = {
    "flight": ("Find flights from Tel Aviv to London leaving next Sunday for one adult "
               "and show the results. Do not book.",
               "https://www.google.com/travel/flights", 30),
    "hotel": ("Find a hotel in Rome for two nights starting next Friday for two adults "
              "and show the list of hotels. Do not book.",
              "https://www.booking.com", 30),
    "tshirt": ("Find a plain navy blue t-shirt for men in size medium and open its "
               "page. Do not buy anything.",
               "https://www.marksandspencer.com", 24),
    "checkout": ("Put a phone in the basket and get to the order form, ready for me to "
                 "pay. Do NOT pay and do NOT enter card details.",
                 "https://www.demoblaze.com", 24),
}

GOOD = {"done", "partly_done", "needs_payment"}


def main() -> int:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    only = sys.argv[2:] or list(TASKS)
    j = Jev(); j.warmup()
    llm = LLM()
    print(f"browser agent, {repeats} run(s) each   {time.strftime('%Y-%m-%d %H:%M')}\n")
    worst = 0
    for name in only:
        if name not in TASKS:
            print(f"  no such task: {name}"); continue
        goal, url, steps = TASKS[name]
        ok, times, spent = 0, [], []
        for i in range(repeats):
            before = j.cost_usd
            t0 = time.time()
            marks = []
            try:
                r = web.run(j, llm, goal=goal, start_url=url, headless=True,
                            max_steps=steps, on_step=lambda m: marks.append(m))
            except Exception as e:  # noqa: BLE001
                print(f"  {name} {i+1}: crashed — {e!r}"[:110]); continue
            dt = time.time() - t0
            times.append(dt); spent.append(j.cost_usd - before)
            good = r.get("did") in GOOD
            ok += good
            note = f"  ({r['note']})" if r.get("note") else ""
            print(f"  {name:<9} {i+1}: {'ok  ' if good else 'FAIL'} "
                  f"did={str(r.get('did')):<14} page={str(r.get('page_is')):<10} "
                  f"{dt:4.0f}s  [{r.get('ended')}]{note}")
            if name == "checkout" and r.get("did") == "needs_payment":
                print(f"             stopped at: {r.get('waiting_on')!r}")
            if not good:
                # r["steps"] is the complete record; `marks` only holds the steps that
                # happen to report progress, which hid whole categories of behaviour.
                for m in (r.get("steps") or [])[:16]:
                    print(f"             {m[:84]}")
            time.sleep(6)          # do not hammer a real shop
        if times:
            worst = max(worst, repeats - ok)
            print(f"  => {name}: {ok}/{repeats}   {min(times):.0f}-{max(times):.0f}s   "
                  f"${sum(spent):.4f}\n")
    return 1 if worst else 0


if __name__ == "__main__":
    raise SystemExit(main())
