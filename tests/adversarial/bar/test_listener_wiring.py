#!/usr/bin/env python3
"""Adversarial cases for native/listener.py's Listener <-> bar wiring (display_mode,
view_attention/view_heard/view_result/view_idle/view_hide, set_status's glyph filter).

    native/.venv/bin/python3 \\
        tests/adversarial/bar/test_listener_wiring.py

Requires the native/.venv interpreter (listener.py imports AVFoundation/Speech, which
are only installed there); this file puts THIS worktree's native/ first on sys.path,
exactly like tests/bar/test_bar_native.py does for bar.py.

Listener() itself does no AppKit/AVFoundation work in __init__ (see the class body:
it only sets plain attributes), so it is built directly, with no NSApplication and no
run loop. Its own bar/panel are replaced with fakes that record every call, so what is
under test is the WIRING (which method calls which, with what arguments, under what
guard), not bar.py or panel.py themselves (those have their own suites).

`on_main()` normally queues its block on the main run loop via
NSOperationQueue.mainQueue().addOperationWithBlock_, which nothing here is pumping, so
without a run loop the block would sit queued and never run. It is monkeypatched to
run its argument immediately and synchronously, in-process, which is call-order
faithful for every method under test here (none of them yields between "decide to call
on_main" and the call itself). Where infeasible, it is reported inline, not silently
skipped.

Nothing here opens the microphone, touches Accessibility, or reaches the network:
watchdog()'s own network/engine/speech side effects are stubbed out by hand for the one
test that runs it for real (test_armed_window_expiry_via_watchdog).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "native"))

import listener as listenermod  # noqa: E402
from listener import Listener, L  # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


# Every on_main() call in listener.py is call-order faithful when run synchronously:
# nothing here is asynchronous work that needs a real run loop, only "run this on the
# main thread instead of a speech callback's thread" — and this test IS the main
# thread throughout.
listenermod.on_main = lambda fn: fn()


class FakeBar:
    def __init__(self):
        self.calls: list[tuple] = []
        self._visible = False

    def set_state(self, state, text=""):
        self.calls.append(("set_state", state, text))

    def set_heard(self, text):
        self.calls.append(("set_heard", text))

    def set_result(self, say, undo_label):
        self.calls.append(("set_result", say, undo_label))

    def show(self):
        self.calls.append(("show",))
        self._visible = True

    def hide(self):
        self.calls.append(("hide",))
        self._visible = False

    def is_visible(self):
        return self._visible

    def names(self):
        return [c[0] for c in self.calls]


class FakePanel:
    def __init__(self):
        self.calls: list[tuple] = []

    def show(self, activate=True):
        self.calls.append(("show", activate))

    def set_state(self, state, text=""):
        self.calls.append(("set_state", state, text))

    def set_heard(self, text):
        self.calls.append(("set_heard", text))

    def set_reply(self, text):
        self.calls.append(("set_reply", text))


def fresh_listener(bar=None, panel=None, cfg=None):
    lst = Listener()
    lst.bar = bar
    lst.panel = panel
    lst.cfg = cfg if cfg is not None else {}
    lst.locale = "en-US"
    return lst


# ---------------------------------------------------------------- full turn
def test_full_turn_result_never_clobbered():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)

    lst.view_attention()
    check("attention: bar told to listen", bar.calls[:2] == [("set_state", "listening", ""), ("show",)],
          bar.calls)

    for w in ("turn", "turn the", "turn the volume", "turn the volume down"):
        lst.view_heard(w)
    check("live words: every partial reaches the bar, in order",
          [c for c in bar.calls if c[0] == "set_heard"] ==
          [("set_heard", w) for w in ("turn", "turn the", "turn the volume", "turn the volume down")],
          bar.calls)

    before_thinking = len(bar.calls)
    lst.set_status(L("thinking", lst.locale), "◐")   # "◐" == "◐"
    check("thinking (bar already visible) reaches the bar",
          bar.calls[before_thinking:] == [("set_state", "thinking", L("thinking", lst.locale))],
          bar.calls[before_thinking:])

    lst.view_result("Turned it down.", "Undo")
    check("the result reaches the bar", bar.calls[-1] == ("set_result", "Turned it down.", "Undo"),
          bar.calls[-1])
    after_result = len(bar.calls)

    # "◉" follows every answer; must never reach the bar (it would show as an
    # unlabelled state and, worse, is not in GLYPH_STATE's whitelist for the bar at
    # all — see native/listener.py:839-846).
    lst.set_status(L("listening", lst.locale), "◉")   # "◉"
    check("'listening ◉' status after the result does not touch the bar at all",
          len(bar.calls) == after_result, bar.calls[after_result:])

    # "◼" (a send countdown) must not wipe the result either.
    lst.set_status(L("countdown", lst.locale, s=6), "◼")  # "◼"
    check("'countdown ◼' status after the result does not touch the bar at all",
          len(bar.calls) == after_result, bar.calls[after_result:])

    check("net effect: set_result is still the LAST mutating call the bar received",
          bar.calls[-1] == ("set_result", "Turned it down.", "Undo"), bar.calls[-1])


def test_thinking_before_shown_does_not_reach_a_hidden_bar():
    """"◐" is only mirrored to the bar it is already visible (native/listener.py:842:
    `glyph == "⚠" or self.bar.is_visible()`); an unusual order (thinking status set
    before view_attention ever showed the bar) must not pop it up early or silently."""
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.set_status(L("thinking", lst.locale), "◐")
    check("thinking status with the bar not yet visible reaches neither set_state nor show",
          bar.calls == [], bar.calls)


def test_empty_answer_goes_idle():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.view_attention()
    lst.view_result("", None)
    check("an empty answer (ignored / not for us) sends the bar to idle, not a blank result",
          bar.calls[-1] == ("set_state", "idle", ""), bar.calls[-1])


def test_stop_word_hides():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.view_attention()
    lst.view_hide()
    check("view_hide() (a stop word mid-answer) hides the bar", bar.calls[-1] == ("hide",))


def test_error_shows_even_if_bar_was_hidden():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    check("bar starts hidden", not bar.is_visible())
    lst.set_status("The server is not answering", "⚠")   # "⚠"
    check("an error reaches the bar even though it was never shown",
          ("set_state", "error", "The server is not answering") in bar.calls, bar.calls)
    check("an error also SHOWS the bar (native/listener.py:844-845)",
          bar.calls[-1] == ("show",), bar.calls)


def test_display_none_gets_nothing():
    bar, panel = FakeBar(), FakePanel()
    lst = fresh_listener(bar=bar, panel=panel, cfg={"display": "none"})
    lst.view_attention()
    lst.view_heard("hello")
    lst.view_result("Done.", None)
    check("display=none: the bar sees nothing from attention/heard/result",
          bar.calls == [], bar.calls)
    check("display=none: the panel sees nothing either", panel.calls == [], panel.calls)


def test_view_idle_and_view_hide_bypass_display_mode():
    """OBSERVATION, not a crash: view_attention/view_heard/view_result all gate on
    display_mode() (native/listener.py:790-814), but view_idle() and view_hide()
    (:816-822) touch self.bar unconditionally whenever it is not None, with no
    display_mode() check at all. In the ordinary case this is harmless (in "none"
    mode the bar was never shown, so idle/hide land on an already-idle, already-
    hidden bar) -- but it means the display=="none" contract is enforced in three
    places out of five, not centrally in one, so a FUTURE view_* method (or a
    display setting changed to "none" mid-turn while the bar is still up from
    before the change) is one easy-to-miss `if self.bar is not None` away from
    quietly popping the bar open again despite "none" being selected. Flagged for
    the integrator to judge intent; not counted as a failure here."""
    bar = FakeBar()
    lst = fresh_listener(bar=bar, cfg={"display": "none"})
    lst.view_idle()
    lst.view_hide()
    # Now gated like the other view_* calls: "none" means the bar is never touched.
    check("view_idle()/view_hide() leave the bar alone when display='none'",
          bar.calls == [], bar.calls)


def test_display_panel_shows_without_activating():
    panel = FakePanel()
    lst = fresh_listener(bar=None, panel=panel, cfg={"display": "panel"})
    lst.view_attention()
    check("display=panel: the panel is shown with activate=False (must not steal focus)",
          panel.calls == [("show", False)], panel.calls)


def test_display_bar_falls_back_to_panel_when_no_bar_exists():
    """"never leave her with nothing" (native/listener.py:786-788): if bar mode is
    requested/default but no MicMicBar was built (e.g. bar.py failed to import),
    display_mode() must fall back to panel rather than silently showing nothing."""
    panel = FakePanel()
    lst = fresh_listener(bar=None, panel=panel, cfg={"display": "bar"})
    check("display_mode() falls back to panel when self.bar is None",
          lst.display_mode() == "panel", lst.display_mode())
    lst.view_attention()
    check("...and view_attention() actually reaches the panel in that fallback",
          panel.calls == [("show", False)], panel.calls)


def test_display_bar_default_when_unset():
    bar = FakeBar()
    lst = fresh_listener(bar=bar, cfg={})
    check("no 'display' key at all defaults to 'bar'", lst.display_mode() == "bar")


def test_view_heard_ignores_empty_text():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.view_heard("")
    check("view_heard('') does not call the bar at all (native/listener.py:805: `if ... and text`)",
          bar.calls == [], bar.calls)


# ---------------------------------------------------------------- watchdog: armed-window expiry
def test_armed_window_expiry_via_watchdog():
    """Runs the REAL watchdog() loop in a background thread, with every dangerous
    side effect (network health checks, settings refresh, Accessibility probing,
    audio-engine recovery, starting a real recognition task) stubbed to a no-op, so
    only the stale-arm -> view_idle() path (native/listener.py:1596-1609) is real."""
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.running = True
    lst.paused = False
    lst.muted_until = 0.0
    lst.task = object()                 # not None, so `missing` is False
    lst.task_started = time.time()      # not stale by TASK_MAX
    lst.transcript = ""                 # nothing spoken
    lst.armed_until = time.time() - 1.0   # already expired
    lst.countdown_until = 0.0
    lst.last_health_check = time.time()
    lst.last_hotkey_check = time.time()
    lst.last_buffers_at = time.time()
    lst.last_debug = time.time()

    # Defence in depth: even if a timing edge lets one of the guarded blocks above
    # run anyway, none of these may do real work in a test process.
    listenermod.check_server_health = lambda: True
    lst.refresh_settings = lambda: None
    lst.update_hotkey_status = lambda: None
    lst.start_task = lambda: None
    lst.consider = lambda final=False: None
    lst.recover_engine = lambda why: None
    listenermod.play_sound = lambda path: None
    listenermod.server_duck = lambda on: None

    th = threading.Thread(target=lst.watchdog, daemon=True)
    th.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and ("set_state", "idle", "") not in bar.calls:
        time.sleep(0.05)
    lst.running = False
    th.join(timeout=2.0)

    check("an expired armed window sends the bar to idle via view_idle()",
          ("set_state", "idle", "") in bar.calls, bar.calls)
    check("armed_until is cleared so the window is not reported stale forever",
          lst.armed_until == 0.0, lst.armed_until)
    check("the watchdog thread actually stopped when asked", not th.is_alive())


def main():
    for t in (test_full_turn_result_never_clobbered,
              test_thinking_before_shown_does_not_reach_a_hidden_bar,
              test_empty_answer_goes_idle,
              test_stop_word_hides,
              test_error_shows_even_if_bar_was_hidden,
              test_display_none_gets_nothing,
              test_view_idle_and_view_hide_bypass_display_mode,
              test_display_panel_shows_without_activating,
              test_display_bar_falls_back_to_panel_when_no_bar_exists,
              test_display_bar_default_when_unset,
              test_view_heard_ignores_empty_text,
              test_armed_window_expiry_via_watchdog):
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(f"{t.__name__} ran to the end", False, repr(e)[:400])
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
