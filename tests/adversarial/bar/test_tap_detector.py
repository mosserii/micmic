#!/usr/bin/env python3
"""Adversarial cases for native/listener.py's TapDetector (the right-modifier-tap
push-to-talk hotkey).

    native/.venv/bin/python3 \\
        tests/adversarial/bar/test_tap_detector.py

That interpreter is required because listener.py imports AVFoundation/Speech, which
are only installed there; this file puts THIS worktree's native/ first on sys.path so
the class under test is the one here, exactly like tests/bar/test_bar_native.py does
for bar.py. TapDetector itself is pure data (no AppKit calls in its body), so no run
loop, no window, no real keyboard is needed to exercise it.

We do NOT register a real global hotkey monitor (that needs Accessibility trust and
would install a live keyboard hook against this machine); we drive the class's own
key_down()/flags_changed() state machine directly with synthetic (keyCode, flags, t)
triples, which is the only way listener.py's own author intended this to be tested
(see the class docstring: "so `--check` can drive it with synthetic key sequences").
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "native"))

import listener as listenermod  # noqa: E402
from listener import TapDetector  # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


import AppKit  # noqa: E402

OPTION = int(AppKit.NSEventModifierFlagOption)
COMMAND = int(AppKit.NSEventModifierFlagCommand)
SHIFT = int(AppKit.NSEventModifierFlagShift)
CONTROL = int(AppKit.NSEventModifierFlagControl)

# Standard ANSI-US virtual keycodes for the modifiers under test (from Apple's HID
# usage tables, the same numbers listener.py's own TAP_KEYS table uses).
LEFT_OPTION_CODE = 58
RIGHT_OPTION_CODE = 61
LEFT_SHIFT_CODE = 56
RIGHT_SHIFT_CODE = 60
LEFT_COMMAND_CODE = 55


def new_detector():
    return TapDetector(RIGHT_OPTION_CODE, OPTION)


# ---------------------------------------------------------------- basic contract
def test_lone_tap_fires():
    for n in range(3):
        det = new_detector()
        det.flags_changed(RIGHT_OPTION_CODE, OPTION, 100.0)      # pressed
        fired = det.flags_changed(RIGHT_OPTION_CODE, 0, 100.10)  # released, 100ms later
        check(f"[{n}] a lone right-Option tap fires", fired)


def test_held_too_long_does_not_fire():
    for n in range(3):
        det = new_detector()
        det.flags_changed(RIGHT_OPTION_CODE, OPTION, 100.0)
        fired = det.flags_changed(RIGHT_OPTION_CODE, 0, 100.0 + TapDetector.MAX_S + 0.20)
        check(f"[{n}] right-Option held past MAX_S ({TapDetector.MAX_S}s) does not fire",
              not fired)


def test_boundary_exact_max_s():
    # <= MAX_S is the documented rule (native/listener.py:461); exactly on the
    # boundary must fire, one tick over must not.
    det = new_detector()
    fired_at_boundary = det.flags_changed(RIGHT_OPTION_CODE, OPTION, 0.0)
    fired_at_boundary = det.flags_changed(RIGHT_OPTION_CODE, 0, TapDetector.MAX_S)
    check("exactly MAX_S (inclusive) still counts as a tap", fired_at_boundary)
    det2 = new_detector()
    det2.flags_changed(RIGHT_OPTION_CODE, OPTION, 0.0)
    fired_over = det2.flags_changed(RIGHT_OPTION_CODE, 0, TapDetector.MAX_S + 0.001)
    check("one millisecond past MAX_S does not", not fired_over)


def test_left_option_ignored():
    for n in range(3):
        det = new_detector()   # tracks RIGHT_OPTION_CODE only
        det.flags_changed(LEFT_OPTION_CODE, OPTION, 200.0)
        fired = det.flags_changed(LEFT_OPTION_CODE, 0, 200.10)
        check(f"[{n}] left Option (a different key code) never fires the right-Option detector",
              not fired)


def test_two_fast_taps_each_fire_independently():
    det = new_detector()
    r1 = []
    for i in range(3):
        t0 = 10.0 * (i + 1)
        det.flags_changed(RIGHT_OPTION_CODE, OPTION, t0)
        r1.append(det.flags_changed(RIGHT_OPTION_CODE, 0, t0 + 0.08))
    check("three fast, separate taps each fire on their own (no debounce lockout)",
          r1 == [True, True, True], r1)


def test_ordinary_key_while_held_cancels():
    """"cmd+c" etc: an ordinary key pressed WHILE the modifier is held must not let
    the eventual release read as a tap (native/listener.py:450-452, key_down())."""
    for n in range(3):
        det = new_detector()
        det.flags_changed(RIGHT_OPTION_CODE, OPTION, 300.0)   # option down
        det.key_down()                                        # some ordinary key, e.g. 'e'
        fired = det.flags_changed(RIGHT_OPTION_CODE, 0, 300.05)  # option released quickly after
        check(f"[{n}] right-Option used as a modifier for an ordinary key does not "
              f"register as a tap", not fired)


# ---------------------------------------------------------------- adversarial: chords
def test_chord_with_another_modifier_key_event():
    """Another modifier key's OWN flagsChanged event (its key code, not the tracked
    one) arriving while the tap key is held must cancel the tap — the class's own
    comment says so ("another modifier moved: not a lone tap", listener.py:456)."""
    for n in range(3):
        det = new_detector()
        det.flags_changed(RIGHT_OPTION_CODE, OPTION, 400.0)                  # option down
        det.flags_changed(LEFT_COMMAND_CODE, OPTION | COMMAND, 400.02)       # cmd goes down too
        fired = det.flags_changed(RIGHT_OPTION_CODE, COMMAND, 400.05)        # option released
        check(f"[{n}] Option+Cmd (Cmd's own flagsChanged event seen first) does not fire",
              not fired)


def test_modifier_already_held_before_the_tap():
    """ADVERSARIAL FINDING candidate: if another modifier (say, Shift) is already
    held down and its flagsChanged event was never routed to THIS detector (because
    the global monitor only calls flags_changed with events for keys of interest, or
    because Shift went down before the bar's monitor was installed), a subsequent
    right-Option press/release still reads as a tap even though Shift is still down —
    flags_changed() only tests `flags & self.flag`, ignoring every other modifier bit
    that may be set in the same event. Reproduced n=5."""
    results = []
    for n in range(5):
        det = new_detector()
        # Right-Option goes down while Shift's bit is ALREADY set in the flags word
        # (as it would be if she is mid Shift+Option-drag, or Shift went down before
        # this monitor attached — a global monitor sees only flagsChanged events for
        # keys pressed AFTER it is installed, not the modifiers already held).
        det.flags_changed(RIGHT_OPTION_CODE, OPTION | SHIFT, 500.0 + n)
        fired = det.flags_changed(RIGHT_OPTION_CODE, SHIFT, 500.05 + n)
        results.append(fired)
    check("Option tapped while Shift is (or was already) held still fires push-to-talk "
          "-- TapDetector.flags_changed() checks only `flags & self.flag` and never "
          "verifies no OTHER modifier bit is set, unlike _register_combo()'s own "
          "`relevant` mask check a few lines below it",
          not any(results), f"fired={results} (all True = bug reproduced 5/5)")


def test_chord_release_order_reversed():
    """The mirror of the case above: Cmd is held first, then Option is tapped and
    released, then Cmd is released. Because the intervening Cmd-down was seen as a
    flagsChanged event for Cmd's OWN key code (not Option's), the existing 'another
    modifier moved' guard fires for the PRESS. But if Cmd was already down before
    Option's press (so no flagsChanged for Cmd was seen by this detector in between),
    nothing cancels it -- same root cause as the Shift case above, exercised via a
    different modifier and press order."""
    results = []
    for n in range(5):
        det = new_detector()
        det.flags_changed(RIGHT_OPTION_CODE, COMMAND | OPTION, 600.0 + n)  # Cmd already down
        fired = det.flags_changed(RIGHT_OPTION_CODE, COMMAND, 600.05 + n)  # Option released, Cmd still down
        results.append(fired)
    check("Option tap fires even with Cmd continuously held through the whole tap",
          not any(results), f"fired={results} (all True = bug reproduced 5/5)")


def test_repeated_press_without_release_does_not_double_fire():
    """macOS can, in principle, redeliver a flagsChanged press event without an
    intervening release (a hardware glitch, or two apps' monitors racing). A second
    'pressed' report while already down must not itself fire, and must not corrupt
    down_at so badly that the EVENTUAL real release fires twice."""
    det = new_detector()
    det.flags_changed(RIGHT_OPTION_CODE, OPTION, 700.0)
    fired_1 = det.flags_changed(RIGHT_OPTION_CODE, OPTION, 700.01)   # redundant press
    fired_2 = det.flags_changed(RIGHT_OPTION_CODE, 0, 700.03)         # the real release
    check("a redundant 'still pressed' report never itself fires", not fired_1)
    check("the following real release still fires exactly once", fired_2)


def test_right_control_and_right_shift_taps_too():
    """The same class is reused for every entry in TAP_KEYS; prove it is not
    right-Option-specific by accident."""
    for spec in ("right-command", "right-control", "right-shift"):
        code, flag_name = listenermod.TAP_KEYS[spec]
        flag = int(getattr(AppKit, flag_name))
        det = TapDetector(code, flag)
        det.flags_changed(code, flag, 800.0)
        fired = det.flags_changed(code, 0, 800.05)
        check(f"{spec}: lone tap fires", fired)


def main():
    for t in (test_lone_tap_fires, test_held_too_long_does_not_fire,
              test_boundary_exact_max_s, test_left_option_ignored,
              test_two_fast_taps_each_fire_independently,
              test_ordinary_key_while_held_cancels,
              test_chord_with_another_modifier_key_event,
              test_modifier_already_held_before_the_tap,
              test_chord_release_order_reversed,
              test_repeated_press_without_release_does_not_double_fire,
              test_right_control_and_right_shift_taps_too):
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(f"{t.__name__} ran to the end", False, repr(e)[:300])
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
