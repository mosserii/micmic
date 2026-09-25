#!/usr/bin/env python3
"""Round-trip ratchet: how many network round trips each bench turn makes, offline.

    cd <checkout> && .venv/bin/python3 tests/perf/perf_check.py            # check
    .venv/bin/python3 tests/perf/perf_check.py --update                    # lower ceilings

Wall-clock time is what she feels, but it is noisy: the same turn swings by hundreds
of milliseconds between runs, far too much to fail a change on. What sets the wall
time is the round trips, each one a few hundred milliseconds that nothing local can
hide, and those can be counted exactly. So this replays every turn of
tests/perf/bench.py against the answers the bench recorded (tests/perf/replay.json),
with a fake Jev and a fake Gemini that each take a fixed 100 ms, and counts:

    jev      Jev round trips in the turn
    gemini   Gemini round trips in the turn
    depth    round trips on the critical path: two calls in parallel count once
    store    reads of WhatsApp's message store (each a copy of the whole store)

Each is checked against tests/perf/budget.json. A count above its ceiling fails; a
count below it is reported, and --update ratchets the ceiling down to it. Ceilings
never go up from here: raising one is a deliberate hand edit with a reason.

Costs nothing and sends nothing: no Jev, no Gemini, no network. Exit 0 pass, 1 over
budget, 2 the replay no longer matches the code (a turn took a different route, or
asked a question the recording never saw): re-record with bench.py, then check again.
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-perfcheck-state-")
for _k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL"):
    os.environ.pop(_k, None)

import argparse          # noqa: E402
import copy              # noqa: E402
import json              # noqa: E402
import threading         # noqa: E402
import time              # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench             # noqa: E402

BUDGET = HERE / "budget.json"
FAKE_S = 0.10
METRICS = ("jev", "gemini", "depth", "store")


def _default(q: dict):
    """A stand-in answer for a question the recording never saw: the first option,
    or "no". Enough to let the turn finish so the extra round trip is counted."""
    t = q.get("type")
    if t == "choice":
        keys = list((q.get("criteria") or {"x": None}).keys())
        return {"choice": keys[0], "confidence": 1.0,
                "probabilities": {k: (1.0 if i == 0 else 0.0) for i, k in enumerate(keys)}}
    if t == "score":
        return {"score": 0.0}
    return {"noul": 0.0}


class Replay:
    def __init__(self, answers: dict):
        self.src = answers
        self.left: dict = {}
        self.turn = ""
        self.calls: list[tuple[str, str, float, float]] = []
        self.misses: list[str] = []
        self.lock = threading.Lock()

    def start(self, turn: str):
        self.turn = turn
        self.left = {k: copy.deepcopy(v) for k, v in self.src.items()
                     if k.startswith(turn + "|")}
        self.calls, self.misses = [], []

    def jev(self, caller: str, questions: dict) -> dict:
        t0 = time.time()
        time.sleep(FAKE_S)
        key = f"{self.turn}|jev|{caller}|{','.join(sorted(questions))}"
        with self.lock:
            got = self.left.get(key) or []
            ans = got.pop(0) if got else None
            if ans is None:
                # Same caller, recorded with fewer questions: a question was added to an
                # existing call. Keep the recorded answers, default the new ones.
                for k, v in self.left.items():
                    parts = k.split("|")
                    if (len(parts) == 4 and parts[1] == "jev" and parts[2] == caller and v
                            and set(parts[3].split(",")) <= set(questions)):
                        ans = v.pop(0)
                        break
            if ans is None:
                self.misses.append(f"jev:{caller}")
                ans = {}
            ans = {**{k: _default(q) for k, q in questions.items() if k not in ans}, **ans}
            self.calls.append(("jev", caller, t0, time.time()))
        return ans

    def gemini(self, caller: str) -> dict:
        t0 = time.time()
        time.sleep(FAKE_S)
        key = f"{self.turn}|gemini|{caller}"
        with self.lock:
            got = self.left.get(key) or []
            d = got.pop(0) if got else None
            if d is None:
                self.misses.append(f"gemini:{caller}")
                d = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
            self.calls.append(("gemini", caller, t0, time.time()))
        return d


class FakeJev:
    mode = "replay"

    def __init__(self, rp: Replay):
        self.rp = rp
        self.calls = 0
        self.input_tokens = 0
        self.last_ms = 0.0
        self.key = None

    def ask(self, state, questions):
        self.calls += 1
        return self.rp.jev(sys._getframe(1).f_code.co_name, questions)

    def warmup(self, *a, **k):
        pass

    @property
    def cost_usd(self) -> float:
        return 0.0


def measure(tm, rp: Replay) -> dict:
    from savta.llm import LLM
    router = tm.router

    def post(llm, payload, model=None, timeout=None):
        f = sys._getframe(1)
        caller = f.f_code.co_name
        if caller in ("text", "generate_content"):
            caller = f.f_back.f_code.co_name
        llm.calls += 1
        return rp.gemini(caller)

    LLM._post = post
    if not router.LLM_CLIENT.available:
        router.LLM_CLIENT.key = "replay-only"          # never used: _post is replaced
    # Reads of WhatsApp's message store (a copy of 620 MB each on the owner's Mac),
    # counted through the real recent_chats() with the read itself replaced by the
    # suite's fixture rows: nothing is copied, only counted.
    from savta.actions import contacts as book
    store_reads = [0]

    def fake_read(limit: int) -> list[dict]:
        store_reads[0] += 1
        return [dict(r) for r in tm.FIXED_BOOK[:limit]]
    book._read_recent = fake_read
    book._RECENT.update(at=0.0, rows=None)
    book.recent_chats = tm.REAL_RECENT_CHATS
    j = FakeJev(rp)
    out = {}
    for case, turns in bench.QUESTIONS:
        tm.reset_state()
        router.LAST_EMERGENCY = None
        p = tm.prof.load()
        p["last_briefed"] = time.strftime("%Y-%m-%d")
        tm.prof.save(p)
        recent_log: list[str] = []
        for ti, text in enumerate(turns):
            key = f"{case}#{ti}"
            rp.start(key)
            reads0 = store_reads[0]
            res = router.handle(j, text, " | ".join(recent_log[-2:]), speak=False,
                                client="native", activation="push")
            spans = [(a, b) for _, _, a, b in rp.calls]
            want = (rp.src.get(f"{key}|did") or [None])[0]
            # A recorded call nobody asked for again: the route changed under the
            # recording even if it ends in the same place.
            unused = [k.split("|", 1)[1] for k, v in rp.left.items()
                      if v and not k.endswith("|did")]
            out[key] = {"jev": sum(1 for c in rp.calls if c[0] == "jev"),
                        "gemini": sum(1 for c in rp.calls if c[0] == "gemini"),
                        "depth": round(bench.union_ms(spans) / (FAKE_S * 1000)),
                        "store": store_reads[0] - reads0,
                        "did": res.get("did"), "recorded_did": want,
                        "misses": list(rp.misses) + [f"unused {u_}" for u_ in unused]}
            if res.get("did") not in ("ignored", "waiting"):
                recent_log.append(f"{text} -> {res.get('did')}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="write current counts as ceilings where they are lower (or new)")
    a = ap.parse_args()
    if not bench.REPLAY.exists():
        print(f"no recording at {bench.REPLAY}: run tests/perf/bench.py first")
        return 2
    rec = json.loads(bench.REPLAY.read_text())
    tm = bench.load_wall()
    got = measure(tm, Replay(rec["answers"]))
    budget = json.loads(BUDGET.read_text()) if BUDGET.exists() else {}

    stale, over, under = [], [], []
    print(f"{'turn':18s} {'jev':>4s} {'gem':>4s} {'depth':>5s} {'store':>5s}   "
          f"ceiling (jev/gem/depth/store)   did")
    for k, v in got.items():
        b = budget.get(k)
        cap = "/".join(str(b.get(m, "-")) for m in METRICS) if b else "none"
        flag = ""
        if v["did"] != v["recorded_did"] or v["misses"]:
            stale.append(k)
            flag = f"  STALE (recorded {v['recorded_did']}, misses {v['misses']})"
        elif b:
            for m in METRICS:
                if v[m] > b.get(m, v[m]):
                    over.append(f"{k} {m} {v[m]} > {b[m]}")
                    flag += f"  OVER {m}"
                elif v[m] < b.get(m, v[m]):
                    under.append(k)
        print(f"{k:18s} {v['jev']:4d} {v['gemini']:4d} {v['depth']:5d} {v['store']:5d}   "
              f"{cap:29s}   {v['did']}{flag}")
    tot = {m: sum(v[m] for v in got.values()) for m in METRICS}
    print(f"\ntotal over {len(got)} turns: {tot['jev']} jev, {tot['gemini']} gemini, "
          f"critical-path depth {tot['depth']}, message-store reads {tot['store']}")

    if a.update and not stale:
        new = {}
        for k, v in got.items():
            b = budget.get(k)
            new[k] = ({m: min(v[m], b.get(m, v[m])) for m in METRICS} if b
                      else {m: v[m] for m in METRICS})
        BUDGET.write_text(json.dumps(new, indent=1) + "\n")
        print(f"wrote {BUDGET.name}")
    if stale:
        print(f"\nSTALE: {len(stale)} turn(s) no longer match the recording: {stale}\n"
              f"re-record with tests/perf/bench.py, then run this again")
        return 2
    if over:
        print("\nOVER BUDGET:\n  " + "\n  ".join(over))
        return 1
    if under and not a.update:
        print(f"\n{len(set(under))} turn(s) now under their ceiling: run with --update "
              f"to ratchet it down")
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
