#!/usr/bin/env python3
"""native/bar.py, run for real: a real NSPanel, a real WKWebView, the real page.

    native/.venv/bin/python3 \\
        tests/bar/test_bar_native.py

That interpreter is the app's own (PyObjC with WebKit), used read-only; this file puts
THIS worktree's native/ first on sys.path so the bar under test is the one here. The
page comes from this worktree's server on 8802 (started if nothing is there), never
from 8799.

It opens exactly one window of someone else's: a TextEdit it launches itself, as its
own new instance, on a scratch file. It makes that the frontmost app, then shows,
updates, resizes and hides the bar and checks after every step that TextEdit is STILL
the frontmost app. At the end it quits that TextEdit instance, by pid, and nothing
else. Nothing here clicks, types into or closes any other app.

Snapshots come from WKWebView.takeSnapshot (screencapture has no Screen Recording
permission on this machine and returns only wallpaper). CSS blur() does not composite
in those snapshots, so the blurred colour field looks hard-edged there; on screen it
is soft.
"""
from __future__ import annotations

import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "native"))

import AppKit  # noqa: E402
import Foundation  # noqa: E402
import WebKit  # noqa: E402

import bar as barmod  # noqa: E402
from bar import MicMicBar, _bar_frame, _screen_for_point  # noqa: E402
# The listener imports both windows into one process. Objective-C class names are
# global, and a second "_Bridge" here once made the panel fail to build at launch
# ("overriding existing Objective-C class"), which a bar-only import never shows.
import panel as _panelmod  # noqa: E402,F401

PORT = 8802
SERVER = f"http://127.0.0.1:{PORT}"
assert PORT != 8799, "never the live server"
SHOTS = Path(os.environ.get("BAR_SHOTS", tempfile.mkdtemp(prefix="bar-native-shots-")))
SHOTS.mkdir(parents=True, exist_ok=True)
PASSED, FAILED = [], []
# Jev and Gemini cost money: the server here is a proxy client pointed at a closed local
# port, so it never reads the developer's keys from .env.local, never registers a device
# with the cloud, and any model call fails at once, for free. Nothing here needs one.
OFFLINE = {"MICMIC_MODE": "proxy", "MICMIC_PROXY_URL": "http://127.0.0.1:9",
           "MICMIC_PROXY_TOKEN": "offline-test"}


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + (f"   ({detail})" if ok and detail else "")
          + ("" if ok else f"   <- {detail}"))


# ---------------------------------------------------------------- plumbing
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


def next_frame(bar, timeout=5.0):
    """Resolves once the page has produced two animation frames: proof it is painting."""
    box = {}

    def done(result, error):
        box["done"] = True
        box["e"] = error
    bar.web.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(
        "await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); return 1;",
        {}, None, WebKit.WKContentWorld.pageWorld(), done)
    wait_for(lambda: box.get("done"), timeout, step=0.001)
    return box.get("done", False) and box.get("e") is None


def snapshot(bar, path):
    box = {}

    def done(img, err):
        if img is not None:
            rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
            rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}) \
               .writeToFile_atomically_(str(path), True)
        box["done"] = True
    bar.web.takeSnapshotWithConfiguration_completionHandler_(None, done)
    wait_for(lambda: box.get("done"), 5)
    return Path(path).exists()


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_server():
    if port_open(PORT):
        return None
    env = dict(os.environ, MICMIC_PORT=str(PORT),
               MICMIC_STATE_DIR=tempfile.mkdtemp(prefix="bar-native-state-"), **OFFLINE)
    env.pop("MICMIC_ALLOW_SEND", None)
    env.pop("MICMIC_ALLOW_CALL", None)
    proc = subprocess.Popen([str(ROOT / ".venv/bin/python3"), "-m", "savta.server"], cwd=ROOT,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if port_open(PORT):
            return proc
        time.sleep(0.1)
    proc.kill()
    raise SystemExit("server on 8802 did not start")


def frontmost_pid():
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()) if app is not None else -1


def lsappinfo_front_pid():
    """A second opinion from Launch Services, outside this process entirely."""
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    out = subprocess.run(["lsappinfo", "info", "-only", "pid", asn],
                         capture_output=True, text=True).stdout
    try:
        return int(out.strip().split("=")[-1])
    except ValueError:
        return -1


def fresh_bar():
    b = MicMicBar(SERVER)
    ok = wait_for(lambda: b._ready, 10)
    pump(barmod.WARM_SECONDS + 0.2)   # let the warm pass finish
    return b, ok


def set_ui(bar, lang="en", dark=False):
    name = AppKit.NSAppearanceNameDarkAqua if dark else AppKit.NSAppearanceNameAqua
    bar.panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name))
    js(bar, f"applyLanguage('{lang}', {{remember:false}}); 1")
    pump(0.1)


# ---------------------------------------------------------------- tests
def test_pure_placement():
    R = Foundation.NSMakeRect

    class S:
        def __init__(self, f):
            self._f = f

        def frame(self):
            return self._f
    main, left, above = S(R(0, 0, 1440, 900)), S(R(-1920, -100, 1920, 1080)), S(R(0, 900, 2560, 1440))
    screens = [main, left, above]
    P = Foundation.NSMakePoint
    check("pointer on the main screen picks it", _screen_for_point(P(700, 400), screens) is main)
    check("pointer on a screen to the left (negative x) picks it",
          _screen_for_point(P(-500, 300), screens) is left)
    check("pointer on a screen above picks it", _screen_for_point(P(1200, 2000), screens) is above)
    check("pointer ON the top edge (menu bar) still counts as that screen",
          _screen_for_point(P(700, 900), [main]) is main)
    check("pointer nowhere returns None (caller falls back to main)",
          _screen_for_point(P(99999, 99999), screens) is None)
    f = _bar_frame(R(0, 0, 1440, 875), 64)
    check("640 wide, centred", f.size.width == 640 and f.origin.x == 400, f)
    top = f.origin.y + f.size.height
    check("top edge a fixed share below the menu bar", top == 875 - round(875 * barmod.TOP_FRACTION), top)
    f2 = _bar_frame(R(-1920, -100, 1920, 1055), 100)
    check("centred on a screen at negative coordinates",
          f2.origin.x + f2.size.width / 2 == -960 and f2.size.height == 100, f2)
    f3 = _bar_frame(R(0, 0, 600, 700), 64)
    check("narrow screen: narrower bar with side margins", f3.size.width == 600 - 32 and f3.origin.x == 16, f3)


def test_load_and_replay():
    b = MicMicBar(SERVER)
    # Called before the page exists: must be replayed, not dropped.
    b.set_state("thinking", "")
    b.set_heard("what time is it in tokyo")
    b.set_result("It is 9:40 in the evening in Tokyo.", None)
    ok = wait_for(lambda: b._ready, 10)
    check("page loads", ok)
    pump(0.3)
    got = js(b, "[document.getElementById('heard').textContent, document.getElementById('reply').textContent,"
                " document.getElementById('undo').hidden].join('|')")
    check("state sent before the page loaded is replayed once it has",
          got == "what time is it in tokyo|It is 9:40 in the evening in Tokyo.|true", got)
    b.close()
    pump(0.2)


def test_warm_latency(b):
    set_ui(b, "en")
    warm, occl = [], []
    for gap in [0.3] * 12 + [4.0] * 3:
        b.hide()
        wait_for(lambda: not b.panel.isVisible(), 2)
        pump(gap)
        t0 = time.perf_counter()
        b.set_state("listening", "")
        b.show()
        call = time.perf_counter() - t0
        wait_for(lambda: b.panel.occlusionState() & AppKit.NSWindowOcclusionStateVisible, 2, step=0.001)
        t_occ = time.perf_counter() - t0
        painted = next_frame(b)
        t_frame = time.perf_counter() - t0
        if not painted:
            check("page painted after show", False, gap)
        warm.append(t_frame * 1000)
        occl.append(t_occ * 1000)
        if gap == 0.3 and len(warm) == 1:
            check("show() itself returns immediately", call < 0.02, f"{call * 1000:.1f}ms")
    warm_sorted = sorted(warm)
    p50 = statistics.median(warm)
    p95 = warm_sorted[int(round(0.95 * (len(warm) - 1)))]
    check("warm show -> window on screen (occlusion visible), p50",
          statistics.median(occl) < 50, f"p50 {statistics.median(occl):.1f}ms, max {max(occl):.1f}ms, n={len(occl)}")
    check("warm show -> page painted two frames, p50 under 60ms", p50 < 60,
          f"p50 {p50:.1f}ms, p95 {p95:.1f}ms, max {max(warm):.1f}ms, n={len(warm)} "
          f"(12 after 0.3s hidden, 3 after 4s hidden)")
    b.hide()
    wait_for(lambda: not b.panel.isVisible(), 2)
    # Contrast: a bar shown the moment it is built, before it could warm up.
    t0 = time.perf_counter()
    cold = MicMicBar(SERVER)
    cold.set_state("listening", "")
    cold.show()
    wait_for(lambda: cold._ready, 10, step=0.001)
    next_frame(cold)
    cold_ms = (time.perf_counter() - t0) * 1000
    check("for contrast, a cold bar (built and shown at once) takes far longer",
          cold_ms > p50, f"cold {cold_ms:.0f}ms vs warm p50 {p50:.1f}ms")
    cold.close()
    pump(0.3)
    return p50, p95


def test_placement(b):
    b.set_state("listening", "")
    b.show()
    pump(0.1)
    screen = _screen_for_point(AppKit.NSEvent.mouseLocation(), AppKit.NSScreen.screens()) \
        or AppKit.NSScreen.mainScreen()
    vis = screen.visibleFrame()
    f = b.panel.frame()
    mid = f.origin.x + f.size.width / 2
    check("on the pointer's screen, horizontally centred",
          abs(mid - (vis.origin.x + vis.size.width / 2)) <= 1, (f, vis))
    check("near the top of that screen", abs((f.origin.y + f.size.height)
          - (vis.origin.y + vis.size.height - round(vis.size.height * barmod.TOP_FRACTION))) <= 1)
    check("window level is status level, above floating windows (the panel, PiP)",
          b.panel.level() == AppKit.NSStatusWindowLevel, b.panel.level())
    cb = b.panel.collectionBehavior()
    check("joins all Spaces and full-screen spaces, never in window cycling",
          cb & AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
          and cb & AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
          and cb & AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    check("non-activating panel", b.panel.styleMask() & AppKit.NSWindowStyleMaskNonactivatingPanel)
    b.hide()
    wait_for(lambda: not b.panel.isVisible(), 2)


def test_resize(b):
    b.set_state("listening", "")
    b.show()
    pump(0.2)
    f0 = b.panel.frame()
    top0 = f0.origin.y + f0.size.height
    check("slim to begin with (64px)", f0.size.height == 64, f0.size.height)
    b._on_message("height:150")
    f = b.panel.frame()
    check("height message resizes the window", f.size.height == 150, f.size.height)
    check("it grows downwards, top edge fixed", f.origin.y + f.size.height == top0)
    b._on_message("height:9999")
    check("capped at MAX_H", b.panel.frame().size.height == barmod.MAX_H)
    b._on_message("height:12")
    check("never smaller than MIN_H", b.panel.frame().size.height == barmod.MIN_H)
    b._on_message("height:abc")
    b._on_message("nonsense")
    check("garbage messages are ignored", b.panel.frame().size.height == barmod.MIN_H)
    # And the real round trip: the page measures itself and native follows.
    b.set_heard("what's on my calendar tomorrow")
    b.set_result("Two things: the dentist at nine, then lunch with Noa at one in the new "
                 "place on Dizengoff. Then nothing until the evening.", "")
    wait_for(lambda: b.panel.frame().size.height > 64, 3)
    page_h = js(b, "Math.ceil(document.querySelector('.stage').getBoundingClientRect().height)")
    f = b.panel.frame()
    check("the page's own height message sized the window to its content",
          f.size.height == page_h and f.origin.y + f.size.height == top0, (f.size.height, page_h))
    snapshot(b, SHOTS / "native-en-light-result.png")
    b.set_state("listening", "")
    wait_for(lambda: b.panel.frame().size.height == 64, 3)
    check("the next turn shrinks it back", b.panel.frame().size.height == 64)
    b.hide()
    wait_for(lambda: not b.panel.isVisible(), 2)


def test_fade(b):
    # A short result fades on its own after RESULT_MIN seconds.
    b.set_state("listening", "")
    b.show()
    b.set_heard("mute")
    t0 = time.monotonic()
    b.set_result("Muted.", None)
    gone = wait_for(lambda: not b.panel.isVisible(), 10)
    took = time.monotonic() - t0
    check("a result fades out on its own", gone, f"{took:.2f}s")
    check("...after the reading time, not before",
          barmod.RESULT_MIN - 0.05 <= took <= barmod.RESULT_MIN + barmod.FADE_OUT + 0.5, f"{took:.2f}s")
    check("fading out resets the page for the next show",
          js(b, "document.getElementById('reply').textContent + document.getElementById('heard').textContent") == "")

    # Hovering holds it. The pointer is simulated (moving her real pointer is not
    # this test's business); everything else, the timer and the fade, is real.
    inside = {"p": Foundation.NSMakePoint(-1, -1)}
    b._pointer = lambda: inside["p"]
    b.set_state("listening", "")
    b.show()
    f = b.panel.frame()
    b.set_result("Muted.", None)
    inside["p"] = Foundation.NSMakePoint(f.origin.x + 50, f.origin.y + 20)   # she moves onto it
    pump(barmod.RESULT_MIN + 2.0)
    check("pointer over the bar holds it past its time", b.panel.isVisible() and b.is_visible())
    inside["p"] = Foundation.NSMakePoint(f.origin.x - 300, f.origin.y - 300)  # and away again
    t0 = time.monotonic()
    gone = wait_for(lambda: not b.panel.isVisible(), 5)
    took = time.monotonic() - t0
    check("leaving it lets it fade after the grace period", gone
          and took <= barmod.HOVER_GRACE + barmod.FADE_OUT + 0.4, f"{took:.2f}s")

    # A pointer that was already parked where the bar appeared is not a hover.
    parked = Foundation.NSMakePoint(f.origin.x + 100, f.origin.y + 30)
    b._pointer = lambda: parked
    b.set_state("listening", "")
    b.show()
    t0 = time.monotonic()
    b.set_result("Muted.", None)
    gone = wait_for(lambda: not b.panel.isVisible(), 10)
    check("a parked pointer the bar appeared under does not hold it forever", gone,
          f"{time.monotonic() - t0:.2f}s")
    b._pointer = AppKit.NSEvent.mouseLocation

    # The real pointer, wherever she left it: the geometry test uses the real value.
    b.set_state("listening", "")
    b.show()
    real = AppKit.NSEvent.mouseLocation()
    b.panel.setFrameOrigin_(Foundation.NSMakePoint(real.x - 100, real.y - 30))
    check("real pointer position is read and hit-tested against the real frame",
          b._pointer_inside(b._pointer_xy()))
    b.hide()
    wait_for(lambda: not b.panel.isVisible(), 2)

    # "touch" (she pressed Undo) pushes the fade back.
    b.set_state("listening", "")
    b.show()
    b.set_result("Muted.", "")
    before = b._fade_at
    pump(max(0.0, before - time.monotonic() - 1.0))    # a second before it would go
    b._on_message("touch")
    check("a touch in the bar extends its time", b._fade_at >= before + barmod.TOUCH_HOLD - 1.2,
          f"{b._fade_at - before:.2f}s later")
    pump(1.5)
    check("...so it is still up after its original deadline", b.panel.isVisible())
    b.hide()
    wait_for(lambda: not b.panel.isVisible(), 2)

    # Errors fade too; idle hides quickly; listening does not fade on its own.
    b.set_state("error", "The server is not answering")
    b.show()
    t0 = time.monotonic()
    gone = wait_for(lambda: not b.panel.isVisible(), barmod.ERROR_HOLD + 3)
    check("an error fades after ERROR_HOLD", gone and time.monotonic() - t0 >= barmod.ERROR_HOLD - 0.05,
          f"{time.monotonic() - t0:.2f}s")
    b.set_state("listening", "")
    b.show()
    pump(3.0)
    check("listening stays up (the listener closes it)", b.panel.isVisible())
    t0 = time.monotonic()
    b.set_state("idle", "")
    gone = wait_for(lambda: not b.panel.isVisible(), 3)
    check("set_state('idle') hides it promptly", gone, f"{time.monotonic() - t0:.2f}s")

    # show() in the middle of a fade-out wins.
    b.set_state("listening", "")
    b.show()
    b.hide()
    b.set_state("listening", "")
    b.show()
    pump(0.8)
    check("show() during a fade-out keeps it up", b.panel.isVisible() and b.panel.alphaValue() == 1.0,
          b.panel.alphaValue())
    b.hide()
    wait_for(lambda: not b.panel.isVisible(), 2)


def test_esc(b):
    b.set_state("listening", "")
    b.show()
    check("Esc monitor installed while visible", b._esc_monitor is not None)
    a_key = AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        AppKit.NSEventTypeKeyDown, Foundation.NSMakePoint(0, 0), 0, 0, 0, None, "a", "a", False, 0)
    esc = AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        AppKit.NSEventTypeKeyDown, Foundation.NSMakePoint(0, 0), 0, 0, 0, None, "\x1b", "\x1b", False, 53)
    b._on_global_key(a_key)
    pump(0.4)
    check("another key in her app leaves the bar alone", b.panel.isVisible())
    b._on_global_key(esc)
    gone = wait_for(lambda: not b.panel.isVisible(), 2)
    check("Esc (global monitor path, bar not key) dismisses", gone)
    check("Esc monitor removed once hidden", b._esc_monitor is None)


def test_focus_and_page_esc(b):
    """The one that matters most. Returns (ok, detail)."""
    probe = Path(tempfile.mkdtemp(prefix="bar-focus-")) / "focus-probe.txt"
    probe.write_text("MicMic bar focus probe. This window was opened by a test and closes itself.\n")
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
    try:
        check("launched our own TextEdit instance", pid is not None, pid)
        if pid is None:
            return
        front = wait_for(lambda: frontmost_pid() == pid, 15)
        check("TextEdit is frontmost before the bar appears", front,
              f"frontmost pid {frontmost_pid()}, TextEdit {pid}")
        me = AppKit.NSRunningApplication.currentApplication()

        def still(step):
            pump(0.25)
            ws, ls = frontmost_pid(), lsappinfo_front_pid()
            ok = ws == pid and ls == pid and not me.isActive() and not b.panel.isKeyWindow()
            check(f"focus kept: {step}", ok,
                  f"NSWorkspace {ws}, lsappinfo {ls}, TextEdit {pid}, bar key={b.panel.isKeyWindow()}, "
                  f"test app active={me.isActive()}")
        b.set_state("listening", "")
        b.show()
        still("show()")
        for i in range(200):
            b.set_heard("summarize this document for me " + str(i))
        still("200 live-word updates")
        b.set_state("thinking", "")
        still("thinking")
        b._on_message("height:180")
        still("resize")
        b.set_result("It is a one-line note that says it was opened by a test.", "")
        still("result with Undo")
        js(b, "document.getElementById('undo').click(); 1")
        wait_for(lambda: js(b, "document.getElementById('undo').hidden") is True, 5)
        still("Undo pressed (script click)")
        b.hide()
        wait_for(lambda: not b.panel.isVisible(), 2)
        still("hide()")
        b.set_state("listening", "")
        b.show()
        still("show() again")

        # She clicks INTO the bar (the only way it may become key) and presses Esc there.
        # Clicking cannot be simulated without moving her real pointer, so the effect of
        # the click is reproduced directly: the panel becomes key. Being key in a
        # non-activating panel must still leave TextEdit the frontmost app.
        b.panel.makeKeyWindow()
        pump(0.3)
        ws = frontmost_pid()
        check("clicked into (key), the app she was in is STILL frontmost",
              ws == pid and not me.isActive(), f"frontmost {ws}, TextEdit {pid}, active={me.isActive()}")
        b.panel.makeFirstResponder_(b.web)
        esc = AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
            AppKit.NSEventTypeKeyDown, Foundation.NSMakePoint(0, 0), 0,
            AppKit.NSProcessInfo.processInfo().systemUptime(), b.panel.windowNumber(), None,
            "\x1b", "\x1b", False, 53)
        b.panel.sendEvent_(esc)
        gone = wait_for(lambda: not b.panel.isVisible(), 3)
        check("Esc inside the bar reaches the page, which posts dismiss, which hides it", gone)
        pump(0.3)
        check("after it hides, TextEdit is frontmost", frontmost_pid() == pid)
    finally:
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


def test_threads_and_undo(b):
    b.set_state("listening", "")
    b.show()
    evals = []
    real_eval = b._eval
    b._eval = lambda s: (evals.append(s), real_eval(s))

    def speech_queue():
        for i in range(500):
            b.set_heard("book a table for two at eight " + str(i))
            time.sleep(0.001)
        b.set_result("Booked for 8pm at Taizu.", "")
    th = threading.Thread(target=speech_queue)
    th.start()
    while th.is_alive():
        pump(0.01)
    pump(0.3)
    heard_evals = [e for e in evals if "barHeard" in e]
    reply = js(b, "document.getElementById('reply').textContent")
    heard = js(b, "document.getElementById('heard').textContent")
    check("500 set_heard calls from a background thread are coalesced",
          0 < len(heard_evals) < 250, f"{len(heard_evals)} page updates for 500 calls")
    check("the result from that thread is shown, not wiped by late words",
          reply == "Booked for 8pm at Taizu." and heard == "book a table for two at eight 499",
          (heard, reply))
    b._eval = real_eval
    # Undo through the real page and the real server, with nothing done in this
    # process: the server answers undone:false with its own sentence, and the page
    # must show that sentence, never "Undone".
    js(b, "document.getElementById('undo').click(); 1")
    wait_for(lambda: js(b, "document.getElementById('undo').hidden") is True, 5)
    pump(0.2)
    got = js(b, "document.getElementById('reply').textContent")
    check("Undo with nothing to undo shows the server's sentence, never 'Undone'",
          bool(got) and got != "Undone", got)
    b.hide()
    wait_for(lambda: not b.panel.isVisible(), 2)


def test_snapshots(b):
    paths = []
    b.set_state("listening", "")
    b.show()
    for lang, dark in (("en", False), ("en", True), ("he", False), ("he", True), ("ar", True), ("ru", False)):
        set_ui(b, lang, dark)
        tag = f"{lang}-{'dark' if dark else 'light'}"
        heard = {"en": "turn off the lights in the living room and play something calm",
                 "he": "תכבי את האור בסלון ותשימי משהו רגוע",
                 "ar": "أطفئ الأضواء في غرفة المعيشة وشغّل شيئاً هادئاً",
                 "ru": "выключи свет в гостиной и включи что-нибудь спокойное"}[lang]
        say = {"en": "Lights are off. Playing Nils Frahm, Says, on the living room speaker.",
               "he": "כיביתי את האור. מנגנת את Nils Frahm ברמקול בסלון.",
               "ar": "أطفأت الأضواء. أشغّل Nils Frahm على مكبر الصوت في غرفة المعيشة.",
               "ru": "Свет выключен. Включаю Nils Frahm на колонке в гостиной."}[lang]
        b.set_state("listening", "")
        pump(0.3)
        p = SHOTS / f"native-{tag}-1-listening.png"
        snapshot(b, p); paths.append(p)
        b.set_heard(heard)
        b.set_state("thinking", "")
        pump(0.3)
        p = SHOTS / f"native-{tag}-2-thinking.png"
        snapshot(b, p); paths.append(p)
        b.set_result(say, "")
        pump(0.45)
        p = SHOTS / f"native-{tag}-3-result.png"
        snapshot(b, p); paths.append(p)
        if lang == "en" and not dark:
            b.set_state("error", "The server is not answering")
            pump(0.3)
            p = SHOTS / f"native-{tag}-4-error.png"
            snapshot(b, p); paths.append(p)
    check("snapshots written", all(p.exists() for p in paths), len(paths))
    b.hide()
    wait_for(lambda: not b.panel.isVisible(), 2)
    set_ui(b, "en", False)
    b.panel.setAppearance_(None)
    return paths


def test_static_no_activation():
    src = (ROOT / "native" / "bar.py").read_text()
    code = "\n".join(line.split("#")[0] for line in src.splitlines()
                     if not line.strip().startswith(("#", '"', "'")))
    bad = [w for w in ("activateIgnoringOtherApps_", "makeKeyAndOrderFront_", ".activate(",
                       "makeKeyWindow", "makeMainWindow", "activateWithOptions_") if w in code]
    check("bar.py contains no call that activates the app or makes the bar key", not bad, bad)


def main():
    proc = ensure_server()
    app = AppKit.NSApplication.sharedApplication()
    # The listener is an LSUIElement app; an accessory policy is the same thing here.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    app.finishLaunching()
    try:
        print("\n-- static"); test_static_no_activation()
        print("\n-- placement (pure)"); test_pure_placement()
        print("\n-- load and replay"); test_load_and_replay()
        b, ok = fresh_bar()
        check("bar built and warm", ok)
        print("\n-- warm latency"); test_warm_latency(b)
        print("\n-- placement (real screen)"); test_placement(b)
        print("\n-- resize"); test_resize(b)
        print("\n-- fade and hover"); test_fade(b)
        print("\n-- esc"); test_esc(b)
        print("\n-- threads and undo"); test_threads_and_undo(b)
        print("\n-- focus (TextEdit frontmost)"); test_focus_and_page_esc(b)
        print("\n-- snapshots"); paths = test_snapshots(b)
        for p in paths:
            print("   ", p)
        b.close()
        pump(0.2)
    finally:
        if proc is not None:
            proc.terminate()
    print(f"\nsnapshots: {SHOTS}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
