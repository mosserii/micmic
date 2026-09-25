#!/usr/bin/env python3
"""A self-contained PyObjC harness for savta.actions.screen.

    python3 tests/screen_harness/harness.py

Every window this opens belongs to THIS process: it creates its own NSApplication,
builds its own NSWindow(s) with its own controls, and activates itself to the front
so screen.py's frontmost()-based functions read data this file put there on purpose
— never the user's real apps. Every window and every app it launches (Calculator,
for one scenario) is closed again before the process exits.

Why this needs an NSApplication at all, and not just a bare AXUIElement test: a
plain PyObjC process that never calls -[NSApplication finishLaunching] never
registers an accessibility server for itself, so AXUIElementCreateApplication(own
pid) comes back with kAXErrorNotImplemented (-25208) for every attribute — a
window can exist and be on screen and still be invisible to Accessibility. That
cost real time to find; it is why every scenario below pumps the run loop through
`app.finishLaunching()` and `activateWithOptions_` before reading anything.

No pytest. Plain asserts, one printed line per check, non-zero exit on failure.

Recorded incident: an early run of this suite had a real iMessage notification steal
focus mid-scenario, and visible_text() faithfully read the real Messages app's real
conversation content into this suite's own output — nothing this file's own windows
put there. activate_self() and safe_read() below both exist because of that one run;
see activate_self()'s docstring for the mechanism and main()'s RealAppInFront handler
for what happens if it is ever tripped again (the whole run aborts, on the spot).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

if os.environ.get("MICMIC_ALLOW_SEND", "").lower() in ("1", "true", "yes") or \
   os.environ.get("MICMIC_ALLOW_CALL", "").lower() in ("1", "true", "yes"):
    print("REFUSING TO RUN: this harness only reads, but it shares a process with "
          "savta.actions and must not run with a real send/call gate open.")
    sys.exit(2)

from AppKit import (  # noqa: E402
    NSApplication, NSWindow, NSTextField, NSSecureTextField, NSTextView,
    NSScrollView, NSButton, NSBackingStoreBuffered, NSMakeRect, NSMakeRange,
    NSRunningApplication, NSApplicationActivateIgnoringOtherApps,
)
import Foundation  # noqa: E402

from savta.actions import screen, apps, mac  # noqa: E402

PASSED = 0
FAILED: list[tuple[str, str]] = []
_KEEPALIVE: list = []          # holds every window/control so PyObjC never frees one
                                 # out from under Accessibility mid-scenario


def check(name: str, ok, detail: str = "") -> bool:
    global PASSED
    ok = bool(ok)
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------- app plumbing

APP = NSApplication.sharedApplication()
APP.setActivationPolicy_(0)          # regular, so it can be the frontmost app
APP.finishLaunching()                # registers the accessibility server — see
                                      # the module docstring for why this is required


def pump(seconds: float = 0.4) -> None:
    """Run the app's event loop for a short burst so a window we just ordered
    front, or a selection we just set, has actually taken effect before we read it
    back through Accessibility."""
    end = time.time() + seconds
    while time.time() < end:
        ev = APP.nextEventMatchingMask_untilDate_inMode_dequeue_(
            0xFFFFFFFF, Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.02),
            "kCFRunLoopDefaultMode", True)
        if ev is not None:
            APP.sendEvent_(ev)
        APP.updateWindows()


class RealAppInFront(RuntimeError):
    """Raised when activation could not put THIS process in front, which means
    whatever screen.py would read next is a real app, not one of this harness's own
    windows. Every scenario that calls screen.py must refuse to proceed rather than
    silently read it — see the module docstring incident note for why this exists."""


def activate_self() -> None:
    """Bring this process to the front and PROVE it landed, rather than trust that
    activateWithOptions_ succeeded.

    Found the hard way: on a live, logged-in Mac, an iMessage notification arriving
    during the pump() below can steal focus in the same instant this harness thinks
    it just activated itself. One run of this suite did exactly that — Messages
    became frontmost mid-scenario, and visible_text() faithfully read its real,
    private conversation content into this suite's own output. Nothing here may
    call into screen.py again until frontmost() itself confirms this process's own
    pid is the one in front; if it never lands, the scenario aborts instead of
    reading whatever real app is there.
    """
    my_pid = os.getpid()
    for _ in range(30):
        # Measured: a bare process-level activate (no window to raise) sometimes
        # never sticks in this environment, especially right after another app
        # quits — but re-ordering an actual window front on every retry, alongside
        # the process activation, reliably wins. If this scenario has no window of
        # its own yet, there is nothing to raise here; the caller is responsible for
        # having one open before it needs the harness to be frontmost.
        if _KEEPALIVE:
            try:
                _KEEPALIVE[-1].makeKeyAndOrderFront_(None)
            except Exception:  # noqa: BLE001
                pass
        NSRunningApplication.currentApplication().activateWithOptions_(
            NSApplicationActivateIgnoringOtherApps)
        pump(0.2)
        if screen.frontmost().get("pid") == my_pid:
            return
    raise RealAppInFront(
        "could not bring this harness process to the front after repeated "
        "attempts — refusing to let any scenario read whatever real app is there")


def safe_read(fn, *args, **kwargs):
    """Call a content-bearing screen.* function, but only trust the result if this
    harness process was still the frontmost app immediately before AND immediately
    after the call. Belt-and-braces alongside activate_self()'s own check: that one
    guards against losing focus before a scenario starts, this one guards against
    losing it mid-call. Raises RealAppInFront rather than hand back content read
    from whatever real app won the race.
    """
    my_pid = os.getpid()
    if screen.frontmost().get("pid") != my_pid:
        raise RealAppInFront(f"{fn.__name__}() was about to run without this "
                             f"harness in front — refusing to call it")
    result = fn(*args, **kwargs)
    if screen.frontmost().get("pid") != my_pid:
        raise RealAppInFront(f"a real app took focus during {fn.__name__}() — "
                             f"discarding whatever it returned")
    return result


def make_window(title: str, w: int = 500, h: int = 320) -> NSWindow:
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(80, 120, w, h), 15, NSBackingStoreBuffered, False)
    win.setTitle_(title)
    _KEEPALIVE.append(win)
    return win


def static_label(frame, text: str) -> NSTextField:
    tf = NSTextField.alloc().initWithFrame_(NSMakeRect(*frame))
    tf.setStringValue_(text)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    return tf


def close_all_windows() -> None:
    for w in list(_KEEPALIVE):
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass
    _KEEPALIVE.clear()
    pump(0.2)


def run_in_thread(fn):
    """Call fn() on a background Python thread with no run loop of its own — the
    same shape the real server's request thread has — and report whether it raised."""
    box: dict = {}

    def target():
        try:
            box["result"] = fn()
        except Exception:  # noqa: BLE001
            box["error"] = traceback.format_exc(limit=6)

    t = threading.Thread(target=target)
    t.start()
    t.join(timeout=10)
    return box


# ================================================================== scenarios

def t_permissions_and_no_run_loop_hang():
    p = screen.permissions()
    check("accessibility is granted in this dev environment",
          p["accessibility"] is True, p)
    check("screen_recording is a plain bool (granted or not, either is fine here)",
          isinstance(p["screen_recording"], bool), p)


def t_plain_and_rtl_and_emoji_text():
    win = make_window("Text scenario")
    cv = win.contentView()
    rows = [
        ("Hello world, this is plain English text.", 260),
        ("שלום עולם, זהו טקסט בעברית.", 230),
        ("Mixed text: hello שלום world مرحبا 123", 200),
        ("مرحبا بالعالم، هذا نص عربي.", 170),
        ("Emoji test 🎉🚀😀 done", 140),
    ]
    for text, y in rows:
        cv.addSubview_(static_label((20, y, 440, 24), text))
    win.makeKeyAndOrderFront_(None)
    activate_self()

    fm = screen.frontmost()
    check("frontmost() finds this harness process while its window is up",
          fm["app"] and fm["pid"] == os.getpid(), fm)
    check("frontmost() reports this window's title", fm["window"] == "Text scenario", fm)

    vis = safe_read(screen.visible_text)
    check("visible_text() is not truncated for a handful of labels",
          vis["truncated"] is False, vis)
    for text, _ in rows:
        check(f"visible_text() contains: {text!r}", text in vis["text"], vis["text"][:300])
    check("visible_text() app/window match frontmost()",
          vis["app"] == fm["app"] and vis["window"] == fm["window"])

    close_all_windows()


def t_secure_field_never_leaks():
    SECRET = "SuperSecret123!"
    win = make_window("Secure field scenario")
    cv = win.contentView()
    cv.addSubview_(static_label((20, 260, 300, 20), "Account password:"))
    sec = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(20, 230, 300, 24))
    sec.setStringValue_(SECRET)
    cv.addSubview_(sec)
    win.makeKeyAndOrderFront_(None)
    activate_self()

    def leaked(obj) -> bool:
        return SECRET in repr(obj)

    # Not focused, nothing selected yet.
    vis = safe_read(screen.visible_text)
    check("the secret is absent from visible_text() before the field is even focused",
          not leaked(vis), vis)

    # Focus it, then select all of it in its field editor — the strongest case: a
    # secure field that is both focused AND has a selection.
    win.makeFirstResponder_(sec)
    pump(0.2)
    editor = win.fieldEditor_forObject_(True, sec)
    if editor is not None:
        editor.setSelectedRange_(NSMakeRange(0, len(SECRET)))
    pump(0.2)

    foc = safe_read(screen.focused_text)
    check("a focused secure field reports secure=True", foc["secure"] is True, foc)
    check("a focused secure field's value is never returned", foc["value"] == "", foc)
    check("the secret never appears anywhere in focused_text()'s output", not leaked(foc), foc)

    sel = safe_read(screen.selected_text)
    check("selected_text() on a secure field is empty even with a real selection",
          sel == "", repr(sel))
    check("the secret never appears in selected_text()'s output", not leaked(sel), repr(sel))

    vis = safe_read(screen.visible_text)
    check("the secret never appears in visible_text() while the field is focused "
          "and selected", not leaked(vis), vis)

    ctx = safe_read(screen.context)
    check("the secret never appears anywhere in context()'s full output", not leaked(ctx), ctx)

    close_all_windows()


def t_forbidden_label_field_withheld():
    win = make_window("Forbidden label scenario")
    cv = win.contentView()
    card = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 260, 300, 24))
    card.setStringValue_("4111111111111111")
    card.setAccessibilityLabel_("Card number")
    cv.addSubview_(card)
    heb = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 220, 300, 24))
    heb.setStringValue_("hunter2")
    heb.setAccessibilityLabel_("סיסמה")
    cv.addSubview_(heb)
    ok_field = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 180, 300, 24))
    ok_field.setStringValue_("Zohar")
    ok_field.setAccessibilityLabel_("First name")
    cv.addSubview_(ok_field)
    win.makeKeyAndOrderFront_(None)
    activate_self()

    vis = safe_read(screen.visible_text)
    check("a field labelled 'Card number' never appears in visible_text()",
          "Card number" not in vis["text"] and "4111111111111111" not in vis["text"], vis)
    check("a field labelled 'סיסמה' never appears in visible_text()",
          "סיסמה" not in vis["text"] and "hunter2" not in vis["text"], vis)
    check("an ordinary 'First name' field is NOT withheld — the guard is not "
          "swallowing everything", "Zohar" in vis["text"], vis["text"][:300])

    win.makeFirstResponder_(card)
    pump(0.2)
    foc = safe_read(screen.focused_text)
    check("a 'Card number' field is withheld even though its role/subrole is "
          "ordinary — the label match alone must trigger it",
          foc["secure"] is True and foc["value"] == "", foc)

    close_all_windows()


def t_selection_in_a_text_view():
    win = make_window("Selection scenario")
    cv = win.contentView()
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 40, 440, 200))
    tv = NSTextView.alloc().initWithFrame_(scroll.bounds())
    content = "Selectable content for the test, and then some more text after it."
    tv.setString_(content)
    tv.setEditable_(True)
    scroll.setDocumentView_(tv)
    cv.addSubview_(scroll)
    win.makeKeyAndOrderFront_(None)
    activate_self()

    win.makeFirstResponder_(tv)
    pump(0.2)
    want = "Selectable content"
    tv.setSelectedRange_(NSMakeRange(0, len(want)))
    pump(0.2)
    sel = safe_read(screen.selected_text)
    check("selected_text() returns exactly the selected substring", sel == want, repr(sel))

    tv.setSelectedRange_(NSMakeRange(0, 0))
    pump(0.2)
    sel_none = safe_read(screen.selected_text)
    check("selected_text() is empty when nothing is selected", sel_none == "", repr(sel_none))

    close_all_windows()


def t_thousands_of_elements_bounded():
    win = make_window("Big window scenario", w=600, h=400)
    cv = win.contentView()
    N = 5000
    for i in range(N):
        # Position does not matter to Accessibility; only that each is a real
        # subview with a real AXStaticText role.
        tf = static_label((0, 0, 40, 14), f"item {i}")
        cv.addSubview_(tf)
    win.makeKeyAndOrderFront_(None)
    activate_self()

    t0 = time.time()
    vis = safe_read(screen.visible_text, max_chars=6000, max_elements=800, budget_s=1.5)
    elapsed = time.time() - t0
    check(f"visible_text() over {N} elements returns within budget "
          f"({elapsed:.2f}s, budget 1.5s + overhead)", elapsed < 4.0, elapsed)
    check("visible_text() honestly reports truncation over a huge tree",
          vis["truncated"] is True, vis)
    check("visible_text() respects the element cap",
          vis["elements"] <= 800 + 1, vis["elements"])
    check("visible_text() respects the character cap",
          len(vis["text"]) <= 6000, len(vis["text"]))

    close_all_windows()


def t_no_window_open():
    # This process is still the app in front (activate_self() re-asserts that even
    # with no window to show — activation is a process-level, not per-window,
    # concept) but has zero visible windows: the same shape as "the frontmost app
    # has no windows".
    activate_self()

    vis = safe_read(screen.visible_text)
    check("visible_text() with no window returns an empty, non-crashing result",
          vis == {"app": vis["app"], "window": "", "text": "", "truncated": False,
                  "elements": 0})
    sel = safe_read(screen.selected_text)
    check("selected_text() with no window returns ''", sel == "")
    foc = safe_read(screen.focused_text)
    check("focused_text() with no window returns the empty shape",
          foc == {"role": "", "value": "", "secure": False}, foc)
    page = safe_read(screen.browser_page)
    check("browser_page() with no window returns None (this harness is not a "
          "browser anyway)", page is None)


def t_screenshot_without_grant():
    granted = screen.permissions()["screen_recording"]
    png = screen.screenshot_png()
    if granted:
        check("screenshot_png() returned bytes because Screen Recording IS granted "
              "in this environment (not the case this test targets, but still must "
              "not crash)", isinstance(png, bytes))
    else:
        check("screenshot_png() returns None cleanly without Screen Recording "
              "access, and never raises", png is None)


def t_launched_after_process_started():
    """Prove the stale-list bug is gone: launch an app AFTER this harness process
    started, and confirm both screen.frontmost() and apps._pid_for() see it live."""
    # Keep a window of our own open through this whole scenario. activate_self()
    # needs one to raise once Calculator quits and hands focus to whatever else was
    # running on this machine — see its docstring for why a bare process-level
    # activate is not always enough to reclaim focus back from a real app.
    make_window("Calculator scenario", w=200, h=100)
    activate_self()

    before = apps._pid_for("Calculator")
    check("Calculator is not already running before this scenario starts "
          "(otherwise this test would not prove anything)", before is None,
          f"pid={before}")

    subprocess.run(["open", "-a", "Calculator"], check=False)
    calc_pid = None
    deadline = time.time() + 8
    while time.time() < deadline:
        calc_pid = apps._pid_for("Calculator")
        if calc_pid:
            break
        time.sleep(0.2)
    check("apps._pid_for('Calculator') finds it live, no restart of this process "
          "needed", calc_pid is not None, f"pid={calc_pid}")

    deadline = time.time() + 5
    fm = {}
    while time.time() < deadline:
        fm = screen.frontmost()
        if fm.get("app", "").lower() == "calculator":
            break
        time.sleep(0.2)
    check("screen.frontmost() reports Calculator as frontmost after `open -a` "
          "activated it", fm.get("app", "").lower() == "calculator", fm)
    if calc_pid:
        check("screen.frontmost()'s pid matches apps._pid_for()'s pid",
              fm.get("pid") == calc_pid, (fm.get("pid"), calc_pid))

    # Quit ONLY this Calculator, by its own pid — never by name, never anything else.
    if calc_pid:
        ok, msg = mac.quit_app([calc_pid])
        check("Calculator (this one pid only) was asked to quit", ok, msg)
        deadline = time.time() + 5
        gone = False
        while time.time() < deadline:
            if apps._pid_for("Calculator") is None:
                gone = True
                break
            time.sleep(0.2)
        check("Calculator is gone after quitting", gone)

    activate_self()  # bring the harness back to the front for whatever runs next


def t_background_thread_and_main_thread_agree():
    win = make_window("Thread scenario")
    cv = win.contentView()
    cv.addSubview_(static_label((20, 200, 300, 24), "Thread-checked text"))
    win.makeKeyAndOrderFront_(None)
    activate_self()

    def gather():
        return {
            "permissions": screen.permissions(),
            "frontmost": screen.frontmost(),
            "selected": safe_read(screen.selected_text),
            "focused": safe_read(screen.focused_text),
            "visible": safe_read(screen.visible_text),
            "page": safe_read(screen.browser_page),
            "context": safe_read(screen.context),
        }

    main_result = gather()
    check("every function runs on the main thread without raising",
          "Thread-checked text" in main_result["visible"]["text"], main_result["visible"])

    box = run_in_thread(gather)
    check("every function also runs on a background thread with no run loop, "
          "without raising", "error" not in box, box.get("error", ""))
    if "result" in box:
        bg = box["result"]
        check("the background thread sees the same frontmost app as the main thread",
              bg["frontmost"]["pid"] == main_result["frontmost"]["pid"], bg["frontmost"])
        check("the background thread reads the same visible text as the main thread",
              bg["visible"]["text"] == main_result["visible"]["text"])

    close_all_windows()


def t_context_latency():
    win = make_window("Latency scenario")
    cv = win.contentView()
    for i in range(40):
        cv.addSubview_(static_label((20, 20 + i * 12, 400, 12), f"line {i} of sample text"))
    win.makeKeyAndOrderFront_(None)
    activate_self()

    samples = []
    for _ in range(15):
        t0 = time.time()
        safe_read(screen.context)
        samples.append(time.time() - t0)
    samples.sort()
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95) if int(len(samples) * 0.95) < len(samples)
                  else -1]
    print(f"  context() latency over {len(samples)} calls: "
          f"p50={p50*1000:.0f}ms p95={p95*1000:.0f}ms max={max(samples)*1000:.0f}ms")
    check("context() stays well under a second per call on this harness window",
          p95 < 1.0, p95)

    close_all_windows()


TESTS = [
    ("1. permissions and no run-loop hang", t_permissions_and_no_run_loop_hang),
    ("2. plain English, Hebrew, mixed RTL, Arabic and emoji", t_plain_and_rtl_and_emoji_text),
    ("3. a secure field never leaks, focused or selected", t_secure_field_never_leaks),
    ("4. a forbidden-label field is withheld even though it is not secure",
     t_forbidden_label_field_withheld),
    ("5. selection in an NSTextView, and none", t_selection_in_a_text_view),
    ("6. thousands of elements stay within budget", t_thousands_of_elements_bounded),
    ("7. no window open at all", t_no_window_open),
    ("8. screenshot_png() without Screen Recording", t_screenshot_without_grant),
    ("9. an app launched after this process started", t_launched_after_process_started),
    ("10. background thread agrees with the main thread", t_background_thread_and_main_thread_agree),
    ("11. context() latency", t_context_latency),
]


def main() -> int:
    print(f"screen.py harness   {time.strftime('%Y-%m-%d %H:%M:%S')}   pid={os.getpid()}\n")
    t_all = time.time()
    for title, fn in TESTS:
        print(title)
        try:
            fn()
        except RealAppInFront as e:
            # A real app won a focus race mid-scenario. Stop the whole run right
            # here rather than let a later scenario read whatever is now in front —
            # this is exactly the incident the module docstring records, and the
            # correct response is to abort loudly, not to keep going and hope.
            close_all_windows()
            print(f"  ABORT {title}: {e}")
            print()
            print("-" * 78)
            print("HARNESS ABORTED: a real application briefly held focus instead of "
                  "this harness's own window. No further scenarios were run, so "
                  "nothing past this point could have read real app content. See "
                  "activate_self()'s docstring for the incident this guards against.")
            print(f"{PASSED} passed before the abort, 0 failed, run incomplete")
            return 3
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc(limit=8)
            FAILED.append((title, "raised: " + tb))
            print(f"  FAIL  {title} raised an exception")
            print("          " + tb.replace("\n", "\n          "))
        finally:
            close_all_windows()
        print()

    dt = time.time() - t_all
    print("-" * 78)
    if FAILED:
        print(f"{len(FAILED)} FAILURE(S):")
        for name, detail in FAILED:
            print(f"  - {name}\n      {detail}")
    print(f"{PASSED} passed, {len(FAILED)} failed   {dt:.1f}s")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = main()
    sys.exit(code)
