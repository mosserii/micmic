#!/usr/bin/env python3
"""native/bar.py, run for real, for two adversarial cases the existing
tests/bar/test_bar_native.py suite does not cover: pressing Undo at the exact instant
the fade-out countdown starts, and focus safety through a REAL, in-process /api/undo
round trip (rather than that suite's 404, since /api/undo now exists in this tree).

    native/.venv/bin/python3 \\
        tests/adversarial/bar/test_native_fade_undo_focus.py

Puts THIS worktree's native/ first on sys.path (so bar.py under test is the one here)
and runs an in-process savta.server on MICMIC_PORT=8813, in its own private
MICMIC_STATE_DIR, with an undo armed through the frozen router._offer_undo()
mechanism -- never a subprocess, never the live 8799 server, never a real Mac
action (the reversing fn only appends to a local list).

It opens exactly one window of someone else's: a TextEdit it launches itself, on a
scratch file, exactly like tests/bar/test_bar_native.py's own focus test, and quits
only that instance at the end.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "native"))

import AppKit  # noqa: E402
import Foundation  # noqa: E402

import bar as barmod  # noqa: E402
from bar import MicMicBar  # noqa: E402

PORT = 8813
assert PORT not in (8799, 8802), "never the live server or the page-suite's port"
SHOTS = Path(os.environ.get("BAR_SHOTS", tempfile.mkdtemp(prefix="adv-native-shots-")))
SHOTS.mkdir(parents=True, exist_ok=True)
PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


# ---------------------------------------------------------------- in-process server
os.environ.pop("MICMIC_ALLOW_SEND", None)
os.environ.pop("MICMIC_ALLOW_CALL", None)
_STATE = tempfile.mkdtemp(prefix="adv-native-state-")
os.environ["MICMIC_STATE_DIR"] = _STATE
os.environ["MICMIC_PORT"] = str(PORT)
sys.path.insert(0, str(ROOT))
from savta import server as srv          # noqa: E402
from savta import router                 # noqa: E402

SERVER = f"http://127.0.0.1:{PORT}"


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server():
    assert not port_open(PORT), f"something is already on {PORT}"
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.H)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    for _ in range(50):
        if port_open(PORT):
            return httpd, th
        time.sleep(0.05)
    raise SystemExit(f"in-process server never came up on {PORT}")


def arm_undo(say="Reversed.", fn=None):
    calls = []

    def default_fn():
        calls.append(1)
        return True
    router._offer_undo("test_action", "english", fn or default_fn, {"english": say})
    return calls


# ---------------------------------------------------------------- plumbing (as test_bar_native.py)
def pump(seconds):
    Foundation.NSRunLoop.currentRunLoop().runUntilDate_(
        Foundation.NSDate.dateWithTimeIntervalSinceNow_(seconds))


def wait_for(pred, timeout=5.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        pump(step)
    return bool(pred())


def js(bar, code, timeout=5.0):
    box = {}

    def done(result, error):
        box["r"], box["e"], box["done"] = result, error, True
    bar.web.evaluateJavaScript_completionHandler_(code, done)
    wait_for(lambda: box.get("done"), timeout)
    return box.get("r")


def frontmost_pid():
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()) if app is not None else -1


def lsappinfo_front_pid():
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    out = subprocess.run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True).stdout
    try:
        return int(out.strip().split("=")[-1])
    except ValueError:
        return -1


def fresh_bar():
    b = MicMicBar(SERVER)
    ok = wait_for(lambda: b._ready, 10)
    pump(barmod.WARM_SECONDS + 0.2)
    return b, ok


# ---------------------------------------------------------------- 1. Undo exactly as fade starts
def test_undo_exactly_as_fade_starts():
    """Presses the real page's Undo button (via a synthetic script click, same
    technique tests/bar/test_bar_native.py uses) at t == RESULT_MIN - epsilon, the
    instant _tick() would otherwise decide to fade the bar out, and confirms: the
    "touch" message the page sends on click extends the hold (bar.py:553-555), the
    bar does not fade mid-undo, the real /api/undo round trip completes, and the
    bar THEN fades normally afterward rather than getting stuck up forever."""
    b, ok = fresh_bar()
    check("bar built and warm", ok)
    if not ok:
        b.close(); pump(0.2); return
    # Shrink the reading-time hold so the test does not need to wait ~4.5s for real,
    # while keeping the same relative timing relationship under test.
    orig_min, orig_max, orig_fade = barmod.RESULT_MIN, barmod.RESULT_MAX, barmod.FADE_OUT
    barmod.RESULT_MIN, barmod.RESULT_MAX, barmod.FADE_OUT = 0.6, 2.0, 0.15
    try:
        router._drop_undo()
        calls = arm_undo(say="Reversed exactly on time.")
        b.set_state("listening", "")
        b.show()
        b.set_result("Did the thing.", "")     # has_undo=True -> hold ~= RESULT_MIN + RESULT_UNDO_EXTRA
        fade_at = b._fade_at
        # Wait until just before the deadline, then click Undo right on top of it.
        wait_for(lambda: fade_at - time.monotonic() <= 0.05, timeout=10)
        still_up_before_click = b.panel.isVisible()
        js(b, "document.getElementById('undo').click(); 1")
        # The click posts "touch" synchronously in mousedown/click handling before the
        # fetch resolves; give the fetch a moment to complete against the real server.
        undone = wait_for(lambda: js(b, "document.getElementById('undo').hidden") is True, 5)
        reply = js(b, "document.getElementById('reply').textContent")
        check("bar was still visible right before the deadline (test set up correctly)",
              still_up_before_click)
        check("clicking Undo right at the fade deadline does not let the bar fade "
              "out from under the click ('touch' extends _fade_at, bar.py:553-555)",
              b.panel.isVisible())
        check("the real undo round trip completed and the reversing fn ran exactly once",
              undone and len(calls) == 1, (undone, calls))
        check("the page shows the real server's own 'say' text, not a canned line",
              reply == "Reversed exactly on time.", reply)
        # And it must still fade out eventually afterward -- "touch" is not "stay
        # forever", per TOUCH_HOLD.
        gone = wait_for(lambda: not b.panel.isVisible(), barmod.TOUCH_HOLD + 3)
        check("after Undo, the bar still fades out on its own afterward (not stuck up)",
              gone)
    finally:
        barmod.RESULT_MIN, barmod.RESULT_MAX, barmod.FADE_OUT = orig_min, orig_max, orig_fade
        b.close()
        pump(0.2)


# ---------------------------------------------------------------- 2. focus through a REAL undo
def test_focus_kept_through_real_undo_round_trip():
    """The existing suite's focus test undoes against a 404 (real 404 in that tree,
    now a genuine miss in this one). This repeats the focus-safety check with a REAL,
    successful /api/undo round trip -- the code path she will actually see in
    production, an async fetch() resolving while a completely different app is
    frontmost, is not something the existing 404-based test exercises at all."""
    probe = Path(tempfile.mkdtemp(prefix="adv-bar-focus-")) / "focus-probe.txt"
    probe.write_text("MicMic bar focus probe (adversarial). Closes itself.\n")
    before = {int(a.processIdentifier()) for a in
              AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.TextEdit")}
    subprocess.run(["open", "-n", "-a", "TextEdit", str(probe)], check=True)
    mine = {}

    def found():
        now = {int(a.processIdentifier()): a for a in
               AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.TextEdit")}
        new = [p for p in now if p not in before]
        if new:
            mine["pid"], mine["app"] = new[0], now[new[0]]
        return bool(new)
    wait_for(found, 15)
    pid = mine.get("pid")
    b = None
    try:
        check("launched our own TextEdit instance", pid is not None, pid)
        if pid is None:
            return
        wait_for(lambda: frontmost_pid() == pid, 15)
        me = AppKit.NSRunningApplication.currentApplication()
        b, ok = fresh_bar()
        check("bar built and warm", ok)

        router._drop_undo()
        calls = arm_undo(say="Really undone.")
        b.set_state("listening", "")
        b.show()
        b.set_result("Did the thing.", "")
        js(b, "document.getElementById('undo').click(); 1")
        undone = wait_for(lambda: js(b, "document.getElementById('undo').hidden") is True, 5)
        pump(0.3)
        ws, ls = frontmost_pid(), lsappinfo_front_pid()
        check("a real, successful /api/undo round trip completed", undone and len(calls) == 1,
              (undone, calls))
        check("TextEdit is STILL frontmost through a real async Undo fetch resolving",
              ws == pid and ls == pid and not me.isActive() and not b.panel.isKeyWindow(),
              f"NSWorkspace {ws}, lsappinfo {ls}, TextEdit {pid}, "
              f"bar key={b.panel.isKeyWindow()}, test app active={me.isActive()}")
    finally:
        if b is not None:
            b.hide()
            wait_for(lambda: not b.panel.isVisible(), 2)
            b.close()
            pump(0.2)
        if pid is not None:
            app = mine["app"]
            app.terminate()
            if not wait_for(lambda: app.isTerminated(), 8):
                app.forceTerminate()
            wait_for(lambda: app.isTerminated(), 5)
            check("closed only the TextEdit this test opened", app.isTerminated(), pid)
        try:
            probe.unlink()
        except OSError:
            pass


def main():
    httpd, th = start_server()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    app.finishLaunching()
    try:
        print("\n-- undo exactly as fade starts"); test_undo_exactly_as_fade_starts()
        print("\n-- focus through a real undo round trip"); test_focus_kept_through_real_undo_round_trip()
    finally:
        httpd.shutdown()
        th.join(timeout=3)
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
