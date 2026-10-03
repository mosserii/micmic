#!/usr/bin/env python3
"""savta/consent.py: MicMic's own line before an Automation or Screen Recording prompt
(PERMISSION_FALLBACKS.md P0-17, P0-18), and the panes the page may open (P0-19).

    uv run python tests/test_consent.py

No server, no network, no osascript, no macOS dialog: the listener's line is a fake
explainer here (tests/test_consent_lines.py drives the real one with a fake NSAlert),
and the Screen Recording preflight is stubbed. Zero live calls.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="consent-test-")
os.environ["MICMIC_DRY_OPEN"] = "1"
os.environ.setdefault("MICMIC_CLOUD_VOICE", "off")
os.environ.setdefault("MICMIC_PROXY_URL", "http://127.0.0.1:9")

from savta import consent  # noqa: E402
from savta import onboarding_api as ob  # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


class Explainer:
    """The listener's line, faked: answers from a script, records every target shown."""

    def __init__(self, answers=None, delay=0.0):
        self.shown: list[str] = []
        self.answers = dict(answers or {})
        self.delay = delay

    def __call__(self, target):
        self.shown.append(target)
        if self.delay:
            time.sleep(self.delay)
        return self.answers.get(target, True)


def fresh(answers=None, delay=0.0):
    consent._reset_for_tests()
    ex = Explainer(answers, delay)
    consent.register(ex)
    return ex


# ---------------------------------------------------------------- Automation lines
def test_unregistered_goes_ahead_without_a_line():
    consent._reset_for_tests()
    check("no listener in this process: explain() lets her request go ahead",
          consent.explain("Messages") is True)
    check("and records nothing, so a listener registered later still shows its line",
          consent.answer("Messages") is None)


def test_one_line_per_target_per_run():
    ex = fresh()
    check("first Apple Event to Messages: the line, and her Continue lets it go",
          consent.explain("Messages") is True and ex.shown == ["Messages"], ex.shown)
    check("the second time this run: no second line", consent.explain("Messages") is True
          and ex.shown == ["Messages"], ex.shown)
    check("bundle id and AppleScript name are the same target",
          consent.explain("com.apple.MobileSMS") is True and ex.shown == ["Messages"], ex.shown)
    check("another target gets its own line", consent.explain("Calendar") is True
          and ex.shown == ["Messages", "Calendar"], ex.shown)


def test_not_now_holds_for_the_run_without_a_second_alert():
    ex = fresh({"Notes": False})
    check("Not now: explain() says no, nothing is sent", consent.explain("Notes") is False)
    check("asked again this run: still no, and no second alert (the fallback answers)",
          consent.explain("notes") is False and ex.shown == ["Notes"], ex.shown)
    check("answer() reads it without prompting", consent.answer("Notes") is False)
    consent.forget("Notes")
    ex.answers["Notes"] = True
    check("she asks again explicitly (forget): the line once more, and her yes",
          consent.explain("Notes") is True and ex.shown == ["Notes", "Notes"], ex.shown)


def test_no_answer_in_time_is_not_a_yes():
    ex = fresh({"Reminders": None})
    check("no answer in time: nothing goes out this turn", consent.explain("Reminders") is False)
    check("and nothing is recorded, so the next turn may show it again",
          consent.answer("Reminders") is None)
    consent.remember("Reminders", True)
    check("a late Continue (remember) stands for the run without another line",
          consent.explain("Reminders") is True and ex.shown == ["Reminders"], ex.shown)


def test_a_broken_line_never_costs_her_the_request():
    consent._reset_for_tests()

    def broken(target):
        raise RuntimeError("no NSApp")
    consent.register(broken)
    check("an alert that cannot show: the request goes ahead (macOS still asks)",
          consent.explain("Music") is True)


def test_two_turns_racing_see_one_line():
    ex = fresh(delay=0.2)
    got: list = []
    ts = [threading.Thread(target=lambda: got.append(consent.explain("Google Chrome")))
          for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(3)
    check("three callers at once: one line, all three get her answer",
          ex.shown == ["Google Chrome"] and got == [True, True, True], (ex.shown, got))


def test_targets_and_aliases():
    check("the ten Automation targets of the audit",
          set(consent.TARGETS) == {"Messages", "Notes", "Reminders", "Calendar", "Music",
                                   "Spotify", "Google Chrome", "System Events", "FaceTime",
                                   "Contacts"}, consent.TARGETS)
    check("every alias lands on a known target",
          all(v in consent.TARGETS for v in consent._ALIASES.values()))
    check("canonical: iCal is Calendar, com.spotify.client is Spotify, chrome is Chrome",
          (consent.canonical("iCal"), consent.canonical("com.spotify.client"),
           consent.canonical("chrome")) == ("Calendar", "Spotify", "Google Chrome"))
    check("an unknown app keeps its own name (the generic line names it)",
          consent.canonical("Pages") == "Pages")
    check("an empty target asks nothing", consent.explain("") is True)


# ---------------------------------------------------------------- Screen Recording
class ScreenAsker:
    def __init__(self, answer=True):
        self.calls: list[bool] = []
        self.answer = answer

    def __call__(self, first_time):
        self.calls.append(first_time)
        return self.answer


def _screen(granted=False, answer=True, marker=False):
    consent._reset_for_tests()
    consent._screen_granted = lambda: granted
    m = consent._marker()
    if marker:
        m.write_text("1")
    elif m.exists():
        m.rename(m.with_suffix(".old"))        # no delete path, even in a test
    a = ScreenAsker(answer)
    consent.register_screen(a)
    return a


def test_screen_recording_ask():
    a = _screen(granted=True)
    check("already on: granted, and no line", consent.ask_screen_recording() == "granted"
          and a.calls == [], a.calls)
    a = _screen()
    check("never asked on this Mac: the line, macOS asks, MicMic must restart",
          consent.ask_screen_recording() == "asked" and a.calls == [True], a.calls)
    check("the Mac remembers it was asked (macOS will not show its dialog again)",
          consent._marker().exists())
    check("asked again this run: no second line", consent.ask_screen_recording() == "asked_before"
          and a.calls == [True], a.calls)
    a = _screen(marker=True)
    check("asked on an earlier run: the line knows macOS will not ask (first_time False)",
          consent.ask_screen_recording() == "asked" and a.calls == [False], a.calls)
    a = _screen(answer=False)
    check("Not now: declined", consent.ask_screen_recording() == "declined" and a.calls == [True])
    check("and it holds for the run, with no second line",
          consent.ask_screen_recording() == "declined" and a.calls == [True], a.calls)
    check("nothing marked as asked after a Not now", not consent._marker().exists())
    consent.forget_screen()
    a.answer = True
    check("she asks again explicitly: the line once more",
          consent.ask_screen_recording() == "asked" and a.calls == [True, True], a.calls)
    a = _screen(answer=None)
    check("no answer in time: declined for this turn, not remembered",
          consent.ask_screen_recording() == "declined" and consent._screen_state[0] == "")
    consent._reset_for_tests()
    consent._screen_granted = lambda: False
    check("no listener in this process: unavailable (the caller takes the no-permission path)",
          consent.ask_screen_recording() == "unavailable")


def test_late_screen_answers():
    a = _screen(answer=None)
    check("no answer in time: declined, nothing recorded",
          consent.ask_screen_recording() == "declined" and consent._screen_state[0] == ""
          and not consent._marker().exists())
    consent.note_screen_asked()              # her Continue arrives late: macOS was asked
    check("a late Continue is recorded: the marker and asked",
          consent._marker().exists() and consent._screen_state[0] == "asked",
          consent._screen_state)
    check("the next ask makes no first-time claim: asked_before, no line",
          consent.ask_screen_recording() == "asked_before" and a.calls == [True], a.calls)
    a = _screen(answer=None)
    consent.ask_screen_recording()
    consent.note_screen_declined()           # her Not now arrives late
    check("a late Not now holds for the run: declined, no second line",
          consent.ask_screen_recording() == "declined" and a.calls == [True]
          and not consent._marker().exists(), a.calls)
    a = _screen()
    consent.ask_screen_recording()
    consent.note_screen_declined()
    check("a stray late Not now never undoes a Continue already recorded",
          consent._screen_state[0] == "asked", consent._screen_state)


def test_screen_preflight_never_prompts():
    src = (ROOT / "savta" / "consent.py").read_text()
    check("consent.py never reaches CoreGraphics itself (only the listener asks, after its line)",
          "Quartz" not in src and "ctypes" not in src and "CDLL" not in src)


# ---------------------------------------------------------------- panes (P0-19)
def test_panes():
    want = {"screen_recording": "Privacy_ScreenCapture", "full_disk": "Privacy_AllFiles",
            "automation": "Privacy_Automation", "accessibility": "Privacy_Accessibility",
            "microphone": "Privacy_Microphone", "speech": "Privacy_SpeechRecognition"}
    check("PANES has screen_recording, full_disk, automation and the three onboarding panes",
          all(ob.PANES.get(k, "").endswith("?" + v) for k, v in want.items()), ob.PANES)
    check("every privacy pane stays a Privacy & Security URL",
          all(u.startswith("x-apple.systempreferences:com.apple.preference.security?Privacy_")
              for u in ob.PANES.values()))
    check("Login Items is its own pane",
          ob.OTHER_PANES.get("login_items")
          == "x-apple.systempreferences:com.apple.LoginItems-Settings.extension")
    for k in ("screen_recording", "login_items", "full_disk", "automation"):
        code, body = ob.open_pane(k)
        check(f"open_pane({k}) opens it (dry here)", code == 200 and body.get("opened") == k, body)
    code, body = ob.open_pane("x-apple.systempreferences:anything")
    check("a URL from the page is never opened", code == 400 and body["error"] == "unknown_pane")
    check("request() still asks only the three onboarding permissions",
          ob.KINDS == ("microphone", "speech", "accessibility")
          and ob.request("screen_recording")[0] == 400)


def test_no_em_dash_in_consent():
    src = (ROOT / "savta" / "consent.py").read_text()
    check("consent.py has no user-facing strings with an em dash", "—" not in src)


def test_no_delete_path():
    bad = ["os.remove", "os.unlink", ".unlink(", "rmtree", "rm -rf", "shutil.move"]
    hits = [b for b in bad for f in ("consent.py", "onboarding_api.py")
            if b in (ROOT / "savta" / f).read_text()]
    check("no delete path in consent.py or onboarding_api.py", not hits, hits)


def main():
    for t in (test_unregistered_goes_ahead_without_a_line,
              test_one_line_per_target_per_run,
              test_not_now_holds_for_the_run_without_a_second_alert,
              test_no_answer_in_time_is_not_a_yes,
              test_a_broken_line_never_costs_her_the_request,
              test_two_turns_racing_see_one_line,
              test_targets_and_aliases,
              test_screen_recording_ask,
              test_late_screen_answers,
              test_screen_preflight_never_prompts,
              test_panes,
              test_no_em_dash_in_consent,
              test_no_delete_path):
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(f"{t.__name__} ran to the end", False, repr(e)[:400])
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
