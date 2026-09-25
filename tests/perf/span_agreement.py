#!/usr/bin/env python3
"""Does a span asked inside understand() pick the same words as its own pick_span call?

    cd <checkout> && .venv/bin/python3 tests/perf/span_agreement.py [--repeats 2]

Folding router.FOLDED_SPANS into understand() saves a round trip on every clock,
weather, music and film turn, but the question then sits beside thirty others and her
contacts, memory and likes instead of alone with the sentence. That must not change
what gets picked (and where it does, the table says which way was right). For each
sentence below this asks the span both ways, in the exact call shapes the router
uses, `repeats` times each, and reports agreement next to each way's agreement with
itself. Also times understand() with and without the spans,
interleaved. Real Jev calls, about $0.03 at 2 repeats; nothing else leaves the machine.
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-spans-state-")

import argparse          # noqa: E402
import json              # noqa: E402
import time              # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench             # noqa: E402

# (span name, sentence, what a person would take as right: a list of acceptable
# picks, None meaning "no span"). The expectations are for reading the table; the
# verdict is agreement between the two ways of asking.
CASES = [
    ("subject", "play some Umm Kulthum", ["Umm Kulthum"]),
    ("subject", "תשימי לי שיר של זוהר ארגוב", ["זוהר ארגוב"]),
    ("subject", "شغليلي أغنية لفيروز", ["لفيروز", "فيروز"]),
    ("subject", "включи песню Аллы Пугачёвой", ["Аллы Пугачёвой"]),
    ("subject", "I want to watch a Leonardo DiCaprio movie", ["Leonardo DiCaprio"]),
    ("subject", "תשימי לי את הסרט הסנדק", ["הסנדק"]),
    ("subject", "play the Beatles", ["Beatles", "the Beatles"]),
    ("subject", "שים לי את עומר אדם", ["עומר אדם"]),
    ("subject", "شغلي عمرو دياب", ["عمرو دياب"]),
    ("subject", "включи Высоцкого", ["Высоцкого"]),
    ("subject", "show me a documentary about whales", ["whales"]),
    ("subject", "play me some music", [None]),
    ("subject", "תשימי לי מוזיקה", [None]),
    ("subject", "put on some music please", [None]),
    ("subject", "play a song", [None]),
    ("subject", "תשימי שיר נחמד ביוטיוב", [None]),
    ("subject", "شغليلي موسيقى", [None]),
    ("subject", "включи музыку", [None]),
    ("subject", "I want to watch a movie", [None]),
    ("subject", "play something", [None]),
    ("clock_place", "what time is it", [None]),
    ("clock_place", "מה השעה", [None]),
    ("clock_place", "كم الساعة هلق", [None]),
    ("clock_place", "который час", [None]),
    ("clock_place", "what time is it in New York", ["New York"]),
    ("clock_place", "מה השעה בטוקיו", ["בטוקיו", "טוקיו"]),
    ("clock_place", "который час в Москве", ["в Москве", "Москве"]),
    ("clock_place", "كم الساعة بلندن", ["بلندن", "لندن"]),
    ("weather_place", "what's the weather like today", [None]),
    ("weather_place", "מה מזג האוויר מחר", [None]),
    ("weather_place", "كيف الطقس اليوم", [None]),
    ("weather_place", "какая сегодня погода", [None]),
    ("weather_place", "מה מזג האוויר בתל אביב", ["בתל אביב", "תל אביב"]),
    ("weather_place", "какая погода в Москве", ["в Москве", "Москве"]),
    ("weather_place", "كيف الطقس بحيفا", ["بحيفا", "حيفا"]),
    ("weather_place", "is it going to rain in London tomorrow", ["London"]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=2)
    a = ap.parse_args()
    tm = bench.load_wall()
    from savta import brain, memory as longterm
    from savta.jev import Jev
    router = tm.router
    j = Jev()
    j.warmup()
    tm.reset_state()
    # Exactly what router.handle() sends.
    folded_spec = {k: (router.SPANS[k], router.SPAN_EXISTS.get(k)) for k in router.FOLDED_SPANS}
    rows, t_plain, t_fold = [], [], []
    for name, text, ok in CASES:
        contacts = router.get_contacts(text)
        recent = json.dumps(router.MEM.snapshot(), ensure_ascii=False)
        folded, separate = [], []
        for r in range(a.repeats):
            # Interleaved, alternating which goes first.
            order = ("fold", "plain") if r % 2 == 0 else ("plain", "fold")
            for way in order:
                t0 = time.time()
                u = brain.understand(j, text, contacts, recent, playing="",
                                     likes=longterm.summary(),
                                     spans=folded_spec if way == "fold" else None)
                (t_fold if way == "fold" else t_plain).append((time.time() - t0) * 1000)
                if way == "fold":
                    folded.append(u["spans"][name][0])
            separate.append(brain.pick_span(j, text, router.SPANS[name])[0])
        agree = sum(f == s for f in folded for s in separate) / (len(folded) * len(separate))
        rows.append((name, text, folded, separate, agree,
                     all(f in ok for f in folded), all(s in ok for s in separate)))
        print(f"{name:13s} {text[:34]:34s} fold={folded} alone={separate} "
              f"agree={agree:.2f} right: fold {rows[-1][5]} alone {rows[-1][6]}", flush=True)
    n = len(rows)
    agree_all = sum(1 for r in rows if r[4] == 1.0)
    self_fold = sum(1 for r in rows if len(set(r[2])) == 1)
    self_sep = sum(1 for r in rows if len(set(r[3])) == 1)
    right_fold = sum(1 for r in rows if r[5])
    right_sep = sum(1 for r in rows if r[6])
    print(f"\n{n} sentences x {a.repeats} repeats")
    print(f"folded == alone on every repeat: {agree_all}/{n}")
    print(f"stable with itself: folded {self_fold}/{n}, alone {self_sep}/{n}")
    print(f"matches the expected span: folded {right_fold}/{n}, alone {right_sep}/{n}")
    print(f"understand ms: plain p50 {bench.q(t_plain, .5):.0f} p95 {bench.q(t_plain, .95):.0f}; "
          f"with spans p50 {bench.q(t_fold, .5):.0f} p95 {bench.q(t_fold, .95):.0f} "
          f"(n={len(t_plain)} each)")
    print(f"cost: jev ${j.cost_usd:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
