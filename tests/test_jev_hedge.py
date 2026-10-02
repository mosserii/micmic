#!/usr/bin/env python3
"""The hedged Jev request, offline (savta/jev.py).

    cd <checkout> && uv run python tests/test_jev_hedge.py

Bench v1 (2026-10-01): 8 turns stalled about 13 s on one Jev call, the 12 s socket
timeout and then a retry; the worst turn took 26.7 s. A call not answered by about
Jev's p95 now sends ONE duplicate, byte for byte the same, and the first answer wins.

Nothing leaves the machine: a fake Jev on a free local port (never 8799) answers, and
can be told to stall the first copy of a request. No key is real.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-hedge-state-")
os.environ["TYPESAFE_API_KEY"] = "test-key-not-real"
os.environ.pop("MICMIC_MODE", None)
os.environ.pop("MICMIC_JEV_HEDGE_S", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from savta import jev as jevmod      # noqa: E402
from savta.jev import Jev            # noqa: E402

PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail: str = "") -> bool:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, str(detail)))
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return bool(ok)


ANSWER = {"answers": {"intent": {"choice": "music", "confidence": 0.9,
                                 "probabilities": {"music": 0.9, "call": 0.1}}},
          "usage": {"input_tokens": 100}}


class FakeJev:
    """A local Jev. `stall`: how long each request waits before answering, by its order
    of arrival (request 1 stalls stall[0] seconds, ...); missing entries answer at once."""

    def __init__(self):
        self.bodies: list[bytes] = []
        self.stall: list[float] = []
        self.status: list[int] = []
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                with fake.lock:
                    i = len(fake.bodies)
                    fake.bodies.append(body)
                    wait = fake.stall[i] if i < len(fake.stall) else 0.0
                    code = fake.status[i] if i < len(fake.status) else 200
                time.sleep(wait)
                out = json.dumps(ANSWER if code == 200 else {"error": "busy"}).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        assert self.port != 8799
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def reset(self, stall=(), status=()):
        with self.lock:
            self.bodies, self.stall, self.status = [], list(stall), list(status)


FAKE = FakeJev()


def client(hedge_s: float | None = 0.3, cap: float = 5.0, timeout: float = 4.0) -> Jev:
    j = Jev(timeout=timeout)
    j._https, j._host, j._port, j._path = False, "127.0.0.1", FAKE.port, "/v1/systemone"
    j.hedge_s = hedge_s
    j.total_cap = cap
    return j


QS = {"intent": {"type": "choice", "criteria": {"music": "m", "call": "c"}}}
STATE = {"she_said": "play something"}


def t_stalled_call_is_hedged():
    print("\nt_stalled_call_is_hedged")
    FAKE.reset(stall=[3.0])
    j = client(hedge_s=0.3)
    t0 = time.time()
    ans = j.ask(STATE, QS)
    took = time.time() - t0
    check("answered by the duplicate, well before the stalled one", took < 1.5, f"{took:.2f}s")
    check("the answer is the real answer", ans.get("intent", {}).get("choice") == "music", ans)
    check("two requests went out", len(FAKE.bodies) == 2, len(FAKE.bodies))
    check("byte for byte the same request (so replay keys and Jev see one question)",
          len(FAKE.bodies) == 2 and FAKE.bodies[0] == FAKE.bodies[1])
    check("one hedge counted", j.hedges == 1 and j.hedge_wins == 1, (j.hedges, j.hedge_wins))
    check("one round trip counted (calls), not two", j.calls == 1, j.calls)


def t_fast_call_is_not_hedged():
    print("\nt_fast_call_is_not_hedged")
    FAKE.reset()
    j = client(hedge_s=0.3)
    j.ask(STATE, QS)
    time.sleep(0.5)
    check("a fast answer sends nothing more", len(FAKE.bodies) == 1, len(FAKE.bodies))
    check("no hedge counted", j.hedges == 0, j.hedges)


def t_total_is_capped():
    print("\nt_total_is_capped")
    FAKE.reset(stall=[10.0, 10.0, 10.0])
    j = client(hedge_s=0.3, cap=1.2, timeout=10.0)
    t0 = time.time()
    err = None
    try:
        j.ask(STATE, QS)
    except RuntimeError as e:
        err = e
    took = time.time() - t0
    check("both copies stalled: it gives up at the cap, not after 2 x 12 s", err is not None
          and took < 1.8, f"{took:.2f}s {err!r}")
    check("never more than one duplicate (no retry storm on a stall)", len(FAKE.bodies) == 2,
          len(FAKE.bodies))


def t_hedge_off():
    print("\nt_hedge_off")
    FAKE.reset(stall=[0.8])
    j = client(hedge_s=0)
    j.ask(STATE, QS)
    check("hedge_s=0 turns it off: one request", len(FAKE.bodies) == 1, len(FAKE.bodies))
    os.environ["MICMIC_JEV_HEDGE_S"] = "0"
    try:
        j2 = Jev()
        check("MICMIC_JEV_HEDGE_S=0 turns it off for a new client", j2.hedge_delay() is None,
              j2.hedge_delay())
    finally:
        os.environ.pop("MICMIC_JEV_HEDGE_S", None)


def t_busy_retry_still_works():
    print("\nt_busy_retry_still_works")
    FAKE.reset(status=[529])
    j = client(hedge_s=0.3)
    ans = j.ask(STATE, QS)
    check("a busy answer is retried as before", ans.get("intent", {}).get("choice") == "music"
          and len(FAKE.bodies) == 2, len(FAKE.bodies))


def t_adaptive_delay():
    print("\nt_adaptive_delay")
    j = Jev()
    check("default delay before any sample is about p95 of Bench v1 (2.0 s)",
          j.hedge_delay() == jevmod.HEDGE_DEFAULT_S, j.hedge_delay())
    check("own key", j.mode == "own_key", j.mode)
    for _ in range(100):
        j._lat.append(400.0)
    check("fast recent calls: never below the own-key floor",
          j.hedge_delay() == jevmod.HEDGE_OWN_MIN_S, j.hedge_delay())
    j._lat.clear()
    for i in range(100):
        j._lat.append(9000.0 if i % 5 == 0 else 500.0)
    check("slow recent calls: never above the ceiling", j.hedge_delay() == jevmod.HEDGE_MAX_S,
          j.hedge_delay())
    j._lat.clear()
    for i in range(100):
        j._lat.append(1800.0 if i >= 88 else 400.0)
    check("in between, own key: the recent p90", abs(j.hedge_delay() - 1.8) < 1e-6,
          j.hedge_delay())
    j._lat.clear()
    for i in range(100):
        j._lat.append(1300.0 if i >= 88 else 400.0)
    check("own key: a 1.3 s p90 sends the duplicate at 1.3 s", abs(j.hedge_delay() - 1.3) < 1e-6,
          j.hedge_delay())
    # Through the proxy a duplicate is metered against her allowance: p95, floor 1.5 s.
    j.mode = "proxy"
    check("proxy: the same calls wait for the p95 floor", j.hedge_delay() == jevmod.HEDGE_MIN_S,
          j.hedge_delay())
    j._lat.clear()
    for i in range(100):
        j._lat.append(1800.0 if i >= 94 else 400.0)
    check("proxy, in between: the recent p95", abs(j.hedge_delay() - 1.8) < 1e-6,
          j.hedge_delay())


def t_replay_ignores_the_duplicate():
    """tests/replay.py patches Jev._ask; the duplicate is below it, so a recording holds
    the question once and a replay never sees a second copy."""
    print("\nt_replay_ignores_the_duplicate")
    import replay
    rec = Path(tempfile.mkdtemp(prefix="micmic-hedge-rec-")) / "rec.jsonl"
    real = Jev._ask
    replay.RECORDING = rec
    replay.MODE = "record"
    replay._store.clear()
    replay._written.clear()
    replay._loaded = False
    replay._install_jev()
    try:
        FAKE.reset(stall=[3.0])
        j = client(hedge_s=0.3)
        j.ask(STATE, QS)
        lines = rec.read_text().splitlines() if rec.exists() else []
        check("record mode: a hedged call is recorded once", len(lines) == 1, len(lines))
        check("and two requests reached the wire", len(FAKE.bodies) == 2, len(FAKE.bodies))
        replay.MODE = "replay"
        replay._loaded = False
        replay._store.clear()
        FAKE.reset()
        j2 = client(hedge_s=0.3)
        ans = j2.ask(STATE, QS)
        check("replay: the recorded answer, nothing on the wire",
              ans.get("intent", {}).get("choice") == "music" and not FAKE.bodies,
              len(FAKE.bodies))
    finally:
        Jev._ask = real


def main():
    t_stalled_call_is_hedged()
    t_fast_call_is_not_hedged()
    t_total_is_capped()
    t_hedge_off()
    t_busy_retry_still_works()
    t_adaptive_delay()
    t_replay_ignores_the_duplicate()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAILED: {n}  {d}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
