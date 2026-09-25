#!/usr/bin/env python3
"""Adversarial cases against the real HTTP server for the frozen /api/undo contract
({"undone": bool, "did": str, "say": str}) and the "display" setting
(/api/settings, /api/config).

    .venv/bin/python3 \\
        tests/adversarial/bar/test_undo_and_display_server.py

Runs savta.server's HTTP handler IN-PROCESS on MICMIC_PORT=8813, in its own private
MICMIC_STATE_DIR, exactly the way tests/test_micmic.py runs the router in-process —
never as a subprocess of the real `python3 -m savta.server`, so there is no window in
which an unstubbed savta.actions.mac could reach the real machine. MICMIC_ALLOW_SEND
and MICMIC_ALLOW_CALL are asserted unset before anything is imported.

Scope note: exercising the full pipeline that ordinarily arms an undo (a spoken
command -> router.handle() -> brain.py -> a real Jev/LLM call -> an action -> its own
_offer_undo() call) would spend real API calls against a real key for no benefit to
this lane. Instead this arms undos directly through savta.router's own frozen
mechanism (_offer_undo/undo_last/UNDO_WINDOW_S — the exact objects named in the brief)
and drives the real /api/undo endpoint against them over real HTTP. That is the
contract the bar and native/listener.py's `undo` field are written against; the LLM
step in between is out of this lane's scope and is exercised by tests/test_micmic.py.
"""
from __future__ import annotations

import json
import os
import socket
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------- wall one (as test_micmic.py)
if os.environ.get("MICMIC_ALLOW_SEND", "").lower() in ("1", "true", "yes"):
    print("REFUSING TO RUN: MICMIC_ALLOW_SEND is set."); sys.exit(2)
if os.environ.get("MICMIC_ALLOW_CALL", "").lower() in ("1", "true", "yes"):
    print("REFUSING TO RUN: MICMIC_ALLOW_CALL is set."); sys.exit(2)
os.environ.pop("MICMIC_ALLOW_SEND", None)
os.environ.pop("MICMIC_ALLOW_CALL", None)

PORT = 8813
assert PORT not in (8799,), "never the live server's port"
_STATE = tempfile.mkdtemp(prefix="adv-undo-state-")
os.environ["MICMIC_STATE_DIR"] = _STATE
os.environ["MICMIC_PORT"] = str(PORT)
sys.path.insert(0, str(ROOT))

from savta import server as srv         # noqa: E402  (reads MICMIC_PORT at import time)
from savta import router                # noqa: E402  (the frozen undo mechanism under test)

BASE = f"http://127.0.0.1:{PORT}"
PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server():
    assert not _port_open(PORT), f"something is already listening on {PORT}"
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.H)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    for _ in range(50):
        if _port_open(PORT):
            return httpd, th
        time.sleep(0.05)
    raise SystemExit(f"in-process server never came up on {PORT}")


def post(path, body=b"", headers=None, timeout=5):
    req = urllib.request.Request(BASE + path, data=body, method="POST",
                                 headers=headers or {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def get(path, timeout=5):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def undo_now():
    return post("/api/undo")


def arm_undo(did="test_action", lang="english", ok=True, say="Done, test undo."):
    """Arms exactly one undo through the real, frozen mechanism, with a fake `fn`
    that never touches the machine."""
    calls = []

    def fn():
        calls.append(1)
        return ok

    router._offer_undo(did, lang, fn, {"english": say, "hebrew": say})
    return calls


# ---------------------------------------------------------------- undo contract
def test_undo_with_nothing_armed():
    router._drop_undo()
    status, body = undo_now()
    check("HTTP 200 with nothing armed", status == 200, status)
    check("contract shape: undone is bool, did and say are str",
          isinstance(body.get("undone"), bool) and isinstance(body.get("did"), str)
          and isinstance(body.get("say"), str), body)
    check("undone is False with nothing armed", body["undone"] is False, body)
    check("did explains there was nothing", body["did"] == "nothing_to_undo", body)


def test_undo_after_an_offered_action():
    router._drop_undo()
    calls = arm_undo(did="volume", say="I put the volume back.")
    status, body = undo_now()
    check("HTTP 200 after a real offer", status == 200, status)
    check("undone True, the reversing fn called exactly once",
          body["undone"] is True and len(calls) == 1, (body, calls))
    check("did names what was undone", body["did"] == "undid_volume", body)
    check("say is the language-specific done line", body["say"] == "I put the volume back.", body)


def test_undo_twice_in_a_row():
    router._drop_undo()
    arm_undo(say="Once only.")
    s1, b1 = undo_now()
    s2, b2 = undo_now()
    check("first undo succeeds", b1["undone"] is True, b1)
    check("second undo (nothing left) says so, not a repeat of the first",
          b2["undone"] is False and b2["did"] == "nothing_to_undo" and b2["say"] != b1["say"],
          (b1, b2))


def test_undo_after_window_expires():
    router._drop_undo()
    arm_undo(say="Too late now.")
    with router._UNDO_LOCK:
        router._UNDO["at"] -= (router.UNDO_WINDOW_S + 10)   # backdate past the window
    status, body = undo_now()
    check("an expired undo is refused, not silently performed",
          body["undone"] is False and body["did"] == "undo_expired", body)


def test_undo_fn_that_fails():
    router._drop_undo()
    router._offer_undo("closed_app", "english", lambda: False, {"english": "reopened"})
    status, body = undo_now()
    check("a reversing fn that returns False is reported as a real failure, not success",
          body["undone"] is False and body["did"] == "undo_failed", body)


def test_undo_fn_that_raises():
    """undo_last() wraps u['fn']() in try/except (savta/router.py:183-186); an action
    whose reverse itself throws must still answer the frozen contract, never a 500."""
    router._drop_undo()

    def boom():
        raise RuntimeError("mac refused")
    router._offer_undo("timer_set", "english", boom, {"english": "cancelled"})
    status, body = undo_now()
    check("a reversing fn that raises still gets a clean HTTP 200 with the contract shape",
          status == 200 and body["undone"] is False and body["did"] == "undo_failed", (status, body))


def test_undo_malformed_body_is_irrelevant():
    """The server never parses /api/undo's body (savta/server.py:282-288: it drains
    and discards it) -- undo takes no parameters. Garbage in the body must not crash
    the handler or change the outcome versus an empty body."""
    router._drop_undo()
    arm_undo(say="Body should not matter.")
    status, body = post("/api/undo", body=b"not json at all {{{", headers={"Content-Type": "text/plain"})
    check("garbage POST body does not crash /api/undo", status == 200, status)
    check("...and the real undo still happens (body is ignored, not required)",
          body["undone"] is True and body["say"] == "Body should not matter.", body)


def test_undo_slow_lying_content_length():
    """A client can claim a Content-Length larger than what it actually sends.
    BaseHTTPRequestHandler's do_POST -> self._drain() does rfile.read(n), which
    blocks until n bytes arrive or the socket closes. This measures how long the
    server's own request thread hangs when a caller lies -- worth knowing even if
    ThreadingHTTPServer means other connections are unaffected."""
    router._drop_undo()
    arm_undo(say="Should not be reachable while the socket is open.")
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    try:
        s.sendall(b"POST /api/undo HTTP/1.1\r\nHost: x\r\nContent-Length: 5000000\r\n"
                  b"Content-Type: text/plain\r\n\r\n" + b"x" * 1024)   # far short of 5MB, then silence
        t0 = time.monotonic()
        s.settimeout(3.0)
        got_within_3s = True
        try:
            data = s.recv(4096)
            got_within_3s = bool(data)
        except socket.timeout:
            got_within_3s = False
        took = time.monotonic() - t0
        # Fixed: a claimed body over MAX_BODY is refused at once and the connection
        # closed with no answer, instead of holding a thread until the client leaves.
        check("a Content-Length over the server's cap is refused at once, connection closed",
              not got_within_3s and took < 1.5, f"responded={got_within_3s} closed within {took:.2f}s")
    finally:
        s.close()
    # Draining that hung read: give the now-closed socket's server-side thread a
    # moment to hit EOF and finish the request it was blocked on (see the next test
    # for what that finishing actually does to global undo state).
    time.sleep(1.0)


def test_undo_dropped_connection_still_consumes_the_armed_undo():
    """ADVERSARIAL FINDING: a client that gives up on /api/undo (closes its socket
    while the server is still blocked reading a lying Content-Length body, exactly
    the shape of test_undo_slow_lying_content_length above, and exactly what the
    bar's own fetch() would do on a timeout/AbortController) does NOT cancel the
    server's request. rfile.read(n) returns a short read at EOF instead of raising,
    so do_POST() carries on, reaches router.undo_last(), and really performs the
    undo -- the result is only ever written to a socket nobody is reading from
    (BrokenPipeError, swallowed by BaseHTTPServer). The bar shows her a network
    error ("I couldn't reach the computer, so nothing was undone" -- COPY.undoNet
    in savta/web/index.html) while the action was, in fact, reversed. This is the
    undo-contract's version of the "sending" countdown's own well-handled double-
    fire risk; here it is NOT guarded. Reproduced n=3."""
    reps = []
    for n in range(3):
        router._drop_undo()
        arm_undo(say=f"silently consumed {n}")
        s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        # Under the size cap, so this is the pure short-read case.
        s.sendall(b"POST /api/undo HTTP/1.1\r\nHost: x\r\nContent-Length: 5000\r\n"
                  b"Content-Type: text/plain\r\n\r\n" + b"x" * 64)
        s.close()                          # she (or her browser) gave up
        time.sleep(1.0)                    # let the server's blocked read hit EOF
        _, body = undo_now()               # a SECOND, honest undo attempt afterwards
        reps.append(body["did"])
    # Fixed in savta/server.py _read_body(): a short read is a dropped request and
    # does nothing, so the honest attempt afterwards still finds the action to undo.
    check("a dropped /api/undo does nothing; the next honest undo still undoes it. 3/3 reps",
          reps == ["undid_test_action"] * 3, reps)


def test_undo_concurrent_double_post():
    """Two POSTs racing for the same armed undo: the take is `with _UNDO_LOCK: u,
    _UNDO = _UNDO, None` (savta/router.py:174-175), so exactly one may see it."""
    n_reps = 5
    exactly_one = []
    for rep in range(n_reps):
        router._drop_undo()
        arm_undo(say=f"race {rep}")
        results = [None, None]

        def go(i):
            results[i] = undo_now()
        t1 = threading.Thread(target=go, args=(0,))
        t2 = threading.Thread(target=go, args=(1,))
        t1.start(); t2.start()
        t1.join(); t2.join()
        undone_count = sum(1 for _, b in results if b["undone"] is True)
        exactly_one.append(undone_count == 1)
    check(f"exactly one of two concurrent /api/undo POSTs gets undone:true, "
          f"{n_reps}/{n_reps} reps", all(exactly_one), exactly_one)


# ---------------------------------------------------------------- display setting
def test_display_settings_validation_unit():
    from savta.server import _validate_settings, DISPLAYS
    check("DISPLAYS is exactly panel/bar/none", set(DISPLAYS) == {"panel", "bar", "none"}, DISPLAYS)
    cases = [
        ("panel", True), ("bar", True), ("none", True),
        ("PANEL", False),          # case-sensitive
        ("", False),
        (None, False),             # str(None) == "None", not a real value
        ("bar; rm", False),
        (123, False),
        (["bar"], False),
        (True, False),             # str(True) == "True"
    ]
    for value, want_ok in cases:
        ok, bad = _validate_settings({"display": value})
        got_ok = "display" in ok and not bad
        check(f"display={value!r} -> {'accepted' if want_ok else 'rejected'}",
              got_ok == want_ok, f"ok={ok} bad={bad}")


def test_display_settings_over_http():
    router._drop_undo()
    # A known-good baseline first.
    status, body = post("/api/settings", json.dumps({"display": "bar"}).encode())
    check("valid display=bar is saved", body.get("saved") == ["display"], body)
    _, cfg = get("/api/config")
    check("GET /api/config reflects it", cfg.get("display") == "bar", cfg)

    for bad_value in ("PANEL", "", None, "bar; rm -rf /", 123, "nonsense"):
        status, body = post("/api/settings", json.dumps({"display": bad_value}).encode())
        check(f"invalid display={bad_value!r} over HTTP: not saved, reported rejected",
              body.get("saved") == [] and "display" in body.get("rejected", []), body)
        _, cfg = get("/api/config")
        check(f"...and /api/config still shows the last GOOD value ('bar'), not {bad_value!r}",
              cfg.get("display") == "bar", cfg)

    # And a real change still works after a run of rejections.
    status, body = post("/api/settings", json.dumps({"display": "none"}).encode())
    check("display=none accepted after a run of bad values", body.get("saved") == ["display"], body)
    _, cfg = get("/api/config")
    check("...and takes effect", cfg.get("display") == "none", cfg)
    post("/api/settings", json.dumps({"display": "bar"}).encode())   # leave it at the default


def test_display_settings_malformed_json_body():
    router._drop_undo()
    status, body = post("/api/settings", body=b"{not json", headers={"Content-Type": "application/json"})
    check("malformed JSON body to /api/settings does not crash the server (HTTP 200, nothing saved)",
          status == 200 and body.get("saved") == [], (status, body))
    _, cfg = get("/api/config")
    check("...and the display setting is unaffected", cfg.get("display") == "bar", cfg)


def main():
    httpd, th = start_server()
    try:
        for t in (test_undo_with_nothing_armed, test_undo_after_an_offered_action,
                  test_undo_twice_in_a_row, test_undo_after_window_expires,
                  test_undo_fn_that_fails, test_undo_fn_that_raises,
                  test_undo_malformed_body_is_irrelevant, test_undo_slow_lying_content_length,
                  test_undo_dropped_connection_still_consumes_the_armed_undo,
                  test_undo_concurrent_double_post, test_display_settings_validation_unit,
                  test_display_settings_over_http, test_display_settings_malformed_json_body):
            print(f"\n-- {t.__name__}")
            try:
                t()
            except Exception as e:  # noqa: BLE001
                check(f"{t.__name__} ran to the end", False, repr(e)[:400])
    finally:
        httpd.shutdown()
        th.join(timeout=3)
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
