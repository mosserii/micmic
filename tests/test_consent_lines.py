#!/usr/bin/env python3
"""native/listener.py's just-in-time lines (PERMISSION_FALLBACKS.md L4): the Automation
line per target (P0-17), the Screen Recording ask (P0-18), the contacts line naming
both macOS dialogs when WhatsApp is installed (P0-20), and the quiet bar line after
"Not now" on the microphone or speech line (P2-1). Also savta/consent.py driving the
REAL listener methods end to end.

    native/.venv/bin/python3 tests/test_consent_lines.py

The native venv (listener.py imports AppKit/AVFoundation/Speech). Nothing here shows an
NSAlert (ask_alert is a fake), asks macOS for anything (request_screen_capture,
ask_microphone and open_settings_pane are fakes), reads this Mac's TCC answers or apps
(mic_status, speech_status, whatsapp_installed are fakes), or reaches a server (every
URL points at a closed port; never 8799). Zero live calls.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native"))
sys.path.insert(1, str(ROOT))
os.environ["MICMIC_ASR_NO_DOWNLOAD"] = "1"
os.environ["MICMIC_ASR_DIR"] = tempfile.mkdtemp(prefix="consent-lines-asr-")
os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="consent-lines-state-")
os.environ["MICMIC_CLOUD_VOICE"] = "off"
os.environ["MICMIC_PROXY_URL"] = "http://127.0.0.1:9"

import listener as lm  # noqa: E402
from listener import L, Listener  # noqa: E402
from savta import consent  # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


# ---------------------------------------------------------------- fakes
lm.on_main = lambda fn: fn()
_DEAD = "http://127.0.0.1:9"
lm.SERVER = _DEAD
lm.UTTERANCE_URL = _DEAD + "/api/utterance"
lm.CONFIG_URL = _DEAD + "/api/config?contacts=1"
lm.LOGIN_ITEM_URL = _DEAD + "/api/login_item_status"
lm.post_turn_open = lambda ts, kind: None
lm.post_turn_closed = lambda ts, why: {}
lm.post_login_item_status = lambda status: None
lm.get_config = lambda: {}
lm.fetch_permission_request = lambda: ""
lm.RECOVERY_POLL = 0.05

TCC = {"mic": 3, "speech": 3}
ASKED: list[str] = []
WHATSAPP = {"here": False}
lm.mic_status = lambda: TCC["mic"]
lm.speech_status = lambda: TCC["speech"]
lm.ask_microphone = lambda done: ASKED.append("microphone")   # never answered
lm.ask_speech = lambda done: ASKED.append("speech")
lm.request_accessibility = lambda: ASKED.append("accessibility") or False
lm.open_settings_pane = lambda k: ASKED.append("pane:" + k)
lm.request_screen_capture = lambda: ASKED.append("screen_capture") or False
lm.whatsapp_installed = lambda: WHATSAPP["here"]


# Focus: which app was in front before the line, and whether it is back. Never this
# Mac's real frontmost app (that would activate the terminal running the test).
class FakeApp:
    def __init__(self, name):
        self.name, self.active = name, False

    def __repr__(self):
        return f"FakeApp({self.name})"


FOCUS = {"front": None, "comes_back": True, "given": []}
lm.frontmost_app = lambda: FOCUS["front"]


def _give_back(app):
    FOCUS["given"].append(app)
    if app is not None and FOCUS["comes_back"]:
        app.active = True


lm.give_focus_back = _give_back
lm.is_front = lambda app: bool(app is not None and app.active)

# The alert: what it showed, and what "she" answers. answer True = first button,
# False = Not now, None = she never answers (the alert stays up).
ALERTS: list[dict] = []
ANSWER = {"next": True, "delay": 0.0}


def fake_alert(message, ok, cancel, on_ok, after=None):
    a = {"message": message, "ok": ok, "cancel": cancel, "on_ok": on_ok, "after": after}
    ALERTS.append(a)
    ans, delay = ANSWER["next"], ANSWER["delay"]
    if ans is None:
        return

    def answer():
        if delay:
            time.sleep(delay)
        if ans:
            on_ok()
        if after:
            after()
    if delay:
        threading.Thread(target=answer, daemon=True).start()
    else:
        answer()


lm.ask_alert = fake_alert


class FakeBar:
    def __init__(self):
        self.calls: list[tuple] = []
        self._visible = False

    def set_state(self, state, text=""):
        self.calls.append(("set_state", state, text))

    def set_result(self, say, undo_label):
        self.calls.append(("set_result", say, undo_label))

    def set_hint(self, spec):
        self.calls.append(("set_hint", spec))

    def set_heard(self, text):
        self.calls.append(("set_heard", text))

    def show(self):
        self.calls.append(("show",))
        self._visible = True

    def hide(self):
        self._visible = False

    def is_visible(self):
        return self._visible


class FakePanel:
    def __init__(self):
        self.calls: list[tuple] = []

    def show(self, activate=True):
        self.calls.append(("show", activate))

    def set_state(self, state, text=""):
        self.calls.append(("set_state", state, text))

    def set_reply(self, text):
        self.calls.append(("set_reply", text))


def make(lang=None, display="bar"):
    lst = Listener()
    lst.bar, lst.panel = FakeBar(), FakePanel()
    lst.cfg = {"display": display}
    if lang:
        lst.cfg["language_hint"] = lang
    lst.locale = "en-US"
    lst.begin = lambda: None
    lst.register_hotkey = lambda force=False: None
    return lst


def reset(answer=True, delay=0.0):
    ALERTS.clear()
    ASKED.clear()
    FOCUS.update(front=None, comes_back=True, given=[])
    ANSWER.update(next=answer, delay=delay)
    consent._reset_for_tests()


NEW_KEYS = ("jit_mic_quiet", "jit_speech_quiet", "jit_contacts_two", "jit_auto_other",
            "jit_screen", "jit_screen_off", "jit_screen_restart") + tuple(lm.AUTOMATION_LINES.values())


# ---------------------------------------------------------------- the lines
def test_lines_in_every_language():
    bad = [(lang, k) for lang in ("he", "en", "ar", "ru")
           for k in NEW_KEYS + ("jit_mic", "jit_speech", "jit_mic_off", "jit_speech_off")
           if not lm.LANG[lang].get(k) or "—" in lm.LANG[lang][k]]
    check("every new line exists in he/en/ar/ru, with no em dash", not bad, bad)
    check("the listener has a line for each of consent.TARGETS",
          set(lm.AUTOMATION_LINES) == set(consent.TARGETS),
          set(consent.TARGETS) ^ set(lm.AUTOMATION_LINES))
    check("the generic line names the app in every language",
          all("Pages" in L("jit_auto_other", lang, app="Pages") for lang in ("he", "en", "ar", "ru")))
    check("each Automation line says macOS asks next (en)",
          all(L(k, "en").endswith("macOS will ask you next.") for k in lm.AUTOMATION_LINES.values()))
    check("the mic and speech lines mention typing (P2-1)",
          "type" in L("jit_mic", "en") and "type" in L("jit_speech", "en")
          and "type" in L("jit_mic_quiet", "en") and "type" in L("jit_speech_quiet", "en"))
    check("the restart line is the audit's line",
          L("jit_screen_restart", "en") == "MicMic will need to restart after you turn it on.")
    check("the contacts line for two dialogs says once or twice",
          "once or twice" in L("jit_contacts_two", "en"))
    check("the Messages line is the audit's line",
          L("jit_auto_messages", "en") == "To send it, MicMic uses the Messages app. macOS will ask you next.")
    check("the Calendar line is the audit's line",
          L("jit_auto_calendar", "en") == "To add it, MicMic uses Calendar. macOS will ask you next.")


# ---------------------------------------------------------------- P0-17
def test_explain_automation_continue_and_not_now():
    reset(True)
    lst = make()
    got = lst.explain_automation("Messages")
    check("Continue: True", got is True, got)
    check("MicMic's Messages line, Continue and Not now",
          len(ALERTS) == 1 and ALERTS[0]["message"] == L("jit_auto_messages", "en")
          and (ALERTS[0]["ok"], ALERTS[0]["cancel"]) == (L("continue", "en"), L("not_now", "en")),
          ALERTS)
    check("the line itself asks macOS for nothing", ASKED == [], ASKED)
    reset(False)
    check("Not now: False", lst.explain_automation("Calendar") is False)
    check("nothing is left marked as on screen", not lst._consent_up, lst._consent_up)


def test_explain_automation_language_and_unknown_target():
    reset(True)
    lst = make(lang="he-IL")
    lst.explain_automation("Reminders")
    check("her language: the Hebrew Reminders line",
          ALERTS and ALERTS[-1]["message"] == lm.LANG["he"]["jit_auto_reminders"], ALERTS[-1:])
    lst = make()
    lst.explain_automation("Pages")
    check("an app with no line of its own: the generic line, which names it",
          ALERTS[-1]["message"] == L("jit_auto_other", "en", app="Pages"), ALERTS[-1:])


def test_explain_automation_no_answer_then_late_continue():
    reset(None)
    prev = lm.CONSENT_WAIT
    lm.CONSENT_WAIT = 0.2
    try:
        lst = make()
        got = lst.explain_automation("Notes")
        check("no answer in time: None (the turn moves on, nothing sent)", got is None, got)
        again = lst.explain_automation("Notes")
        check("pressed again while the alert is still up: no second alert",
              again is None and len(ALERTS) == 1, (again, len(ALERTS)))
        ALERTS[0]["on_ok"]()                 # she clicks Continue, late
        ALERTS[0]["after"]()
        check("her late Continue is kept for the next time (consent.remember)",
              consent.answer("Notes") is True, consent.answer("Notes"))
        check("and the alert is no longer marked as up", "Notes" not in lst._consent_up)
    finally:
        lm.CONSENT_WAIT = prev


def test_consent_drives_the_real_listener():
    reset(True)
    lst = make()
    consent.register(lst.explain_automation)
    check("consent.explain (bundle id) shows the listener's Messages line",
          consent.explain("com.apple.MobileSMS") is True
          and [a["message"] for a in ALERTS] == [L("jit_auto_messages", "en")], ALERTS)
    check("the second Apple Event to Messages: no second line",
          consent.explain("Messages") is True and len(ALERTS) == 1, len(ALERTS))
    ANSWER["next"] = False
    check("System Events, Not now: False", consent.explain("System Events") is False)
    check("asked again this run: False with no new alert (one line per target per run)",
          consent.explain("System Events") is False and len(ALERTS) == 2, len(ALERTS))
    # Two turns at once, she answers after a moment: one alert, both get the yes.
    ANSWER.update(next=True, delay=0.15)
    got: list = []
    ts = [threading.Thread(target=lambda: got.append(consent.explain("FaceTime"))) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(3)
    check("two callers at once: one FaceTime alert, both True",
          got == [True, True] and sum(a["message"] == L("jit_auto_facetime", "en") for a in ALERTS) == 1,
          (got, [a["message"] for a in ALERTS]))


# ---------------------------------------------------------------- P0-18
def _screen_reset(answer=True, marker=False, display="bar"):
    reset(answer)
    lst = make(display=display)
    consent._screen_granted = lambda: False
    m = consent._marker()
    if marker:
        m.write_text("1")
    elif m.exists():
        m.rename(m.with_suffix(".old"))       # no delete path, even in a test
    consent.register_screen(lst.ask_screen_recording)
    return lst


def test_screen_recording_first_time():
    lst = _screen_reset(True)
    got = consent.ask_screen_recording()
    check("first time: asked", got == "asked", got)
    check("MicMic's line first, Continue / Not now",
          [a["message"] for a in ALERTS] == [L("jit_screen", "en")]
          and ALERTS[0]["ok"] == L("continue", "en"), ALERTS)
    check("then macOS's request, exactly once, and no pane over its dialog",
          ASKED == ["screen_capture"], ASKED)
    check("then the restart line on the bar, not a second modal",
          ("set_result", L("jit_screen_restart", "en"), None) in lst.bar.calls
          and len(ALERTS) == 1, lst.bar.calls)
    got = consent.ask_screen_recording()
    check("asked again this run: asked_before, no alert, no request",
          got == "asked_before" and len(ALERTS) == 1 and ASKED == ["screen_capture"], (got, ASKED))


def test_screen_recording_asked_on_an_earlier_run():
    _screen_reset(True, marker=True)
    got = consent.ask_screen_recording()
    check("asked on an earlier run: the line says turn MicMic on there, with Open Settings",
          got == "asked" and ALERTS and ALERTS[0]["message"] == L("jit_screen_off", "en")
          and ALERTS[0]["ok"] == L("open_settings", "en"), (got, ALERTS))
    check("its yes opens the Screen Recording pane", "pane:screen_recording" in ASKED, ASKED)


def test_screen_recording_not_now():
    lst = _screen_reset(False)
    got = consent.ask_screen_recording()
    check("Not now: declined, and macOS is never asked",
          got == "declined" and ASKED == [], (got, ASKED))
    check("no restart line after a Not now",
          not any(c[0] == "set_result" for c in lst.bar.calls), lst.bar.calls)
    check("and no second alert this run", consent.ask_screen_recording() == "declined"
          and len(ALERTS) == 1, len(ALERTS))


def test_screen_recording_in_her_language_and_panel_mode():
    lst = _screen_reset(True, display="panel")
    lst.cfg["language_hint"] = "ru-RU"
    consent.ask_screen_recording()
    check("Russian line first", ALERTS and ALERTS[0]["message"] == lm.LANG["ru"]["jit_screen"])
    check("panel mode: the window shows without taking focus, the status says restart",
          ("show", False) in lst.panel.calls and lst.status_text == lm.LANG["ru"]["jit_screen_restart"],
          (lst.panel.calls, lst.status_text))


def test_screen_recording_no_answer_then_late_continue():
    lst = _screen_reset(None)
    prev = lm.CONSENT_WAIT
    lm.CONSENT_WAIT = 0.2
    try:
        got = consent.ask_screen_recording()
        check("no answer in time: declined for this turn, macOS not asked, nothing recorded",
              got == "declined" and ASKED == [] and consent._screen_state[0] == ""
              and not consent._marker().exists(), (got, ASKED, consent._screen_state))
        got = consent.ask_screen_recording()
        check("asked again while the line is still up: no second alert over it",
              got == "declined" and len(ALERTS) == 1, (got, len(ALERTS)))
        check("the listener itself returns None at once while it is up",
              lst.ask_screen_recording(True) is None and len(ALERTS) == 1, len(ALERTS))
        ALERTS[0]["on_ok"]()                 # she clicks Continue, late
        ALERTS[0]["after"]()
        check("her late Continue asked macOS, once", ASKED == ["screen_capture"], ASKED)
        check("and it is recorded: the marker and asked for this run",
              consent._marker().exists() and consent._screen_state[0] == "asked",
              consent._screen_state)
        check("the restart line shows after the late Continue too",
              ("set_result", L("jit_screen_restart", "en"), None) in lst.bar.calls, lst.bar.calls)
        check("the line is no longer marked as up", lst._screen_up is False)
        got = consent.ask_screen_recording()
        check("the next screenshot request: asked_before, no first-time line, no false 'asked'",
              got == "asked_before" and len(ALERTS) == 1 and ASKED == ["screen_capture"],
              (got, len(ALERTS), ASKED))
    finally:
        lm.CONSENT_WAIT = prev


def test_screen_recording_no_answer_then_late_not_now():
    lst = _screen_reset(None)
    prev = lm.CONSENT_WAIT
    lm.CONSENT_WAIT = 0.2
    try:
        consent.ask_screen_recording()
        ALERTS[0]["after"]()                 # she clicks Not now, late
        check("a late Not now holds for the run",
              consent._screen_state[0] == "declined" and not consent._marker().exists(),
              consent._screen_state)
        check("and is never nagged: declined, no second alert, macOS never asked",
              consent.ask_screen_recording() == "declined" and len(ALERTS) == 1 and ASKED == [],
              (len(ALERTS), ASKED))
        check("no restart line after a Not now",
              not any(c[0] == "set_result" for c in lst.bar.calls), lst.bar.calls)
    finally:
        lm.CONSENT_WAIT = prev


def test_screen_recording_line_up_flag_cleared_on_answer():
    lst = _screen_reset(True)
    consent.ask_screen_recording()
    check("answered in time: the line is not marked as up", lst._screen_up is False)
    lst2 = make()
    lm_prev = lm.ask_alert

    def boom(*a, **k):
        raise RuntimeError("no alert")
    lm.ask_alert = boom
    try:
        try:
            lst2.ask_screen_recording(True)
        except RuntimeError:
            pass
        check("an alert that failed to show leaves nothing marked as up", lst2._screen_up is False)
    finally:
        lm.ask_alert = lm_prev


# ---------------------------------------------------------------- focus after a line
def test_automation_line_hands_focus_back():
    reset(True)
    her = FakeApp("TextEdit")
    FOCUS["front"] = her
    lst = make()
    got = lst.explain_automation("System Events")
    check("Continue: True", got is True, got)
    check("on close, focus goes back to the app she was in",
          FOCUS["given"] == [her], FOCUS["given"])
    check("and the caller is told it is in front before any keystroke",
          lst.last_focus_back is True, lst.last_focus_back)

    reset(False)
    her = FakeApp("Mail")
    FOCUS["front"] = her
    check("Not now: False", lst.explain_automation("Notes") is False)
    check("Not now hands focus back too", FOCUS["given"] == [her], FOCUS["given"])

    reset(True)
    FOCUS["front"] = None                    # MicMic itself was in front
    lst.explain_automation("Messages")
    check("MicMic was in front: nothing to hand back", FOCUS["given"] == [None]
          and lst.last_focus_back is None, (FOCUS["given"], lst.last_focus_back))


def test_automation_focus_not_back_is_reported_not_assumed():
    reset(True)
    her = FakeApp("Safari")
    FOCUS.update(front=her, comes_back=False)
    prev = lm.FOCUS_WAIT
    lm.FOCUS_WAIT = 0.15
    try:
        lst = make()
        t0 = time.time()
        got = lst.explain_automation("System Events")
        took = time.time() - t0
        check("her app never came back: still True (her Continue), but reported as not in front",
              got is True and lst.last_focus_back is False, (got, lst.last_focus_back))
        check("the wait for focus is bounded by FOCUS_WAIT", took < 1.0, took)
    finally:
        lm.FOCUS_WAIT = prev


def test_automation_late_answer_does_not_steal_focus():
    reset(None)
    her = FakeApp("Pages")
    FOCUS["front"] = her
    prev = lm.CONSENT_WAIT
    lm.CONSENT_WAIT = 0.2
    try:
        lst = make()
        check("no answer in time: None", lst.explain_automation("System Events") is None)
        ALERTS[0]["after"]()                 # she clicks Not now, late
        check("a late answer moves no app to the front (the turn moved on)",
              FOCUS["given"] == [], FOCUS["given"])
        check("a late Not now stands for the run (consent.remember False)",
              consent.answer("System Events") is False, consent.answer("System Events"))
    finally:
        lm.CONSENT_WAIT = prev


def test_automation_answer_at_the_timeout_is_never_lost():
    # Her click lands right around CONSENT_WAIT: whichever way the race goes, either the
    # waiting turn gets the answer or consent remembers it. Never neither.
    lost = []
    prev = lm.CONSENT_WAIT
    try:
        for i in range(30):
            reset(True, delay=0.05)
            lm.CONSENT_WAIT = 0.05
            lst = make()
            got = lst.explain_automation("Reminders")
            time.sleep(0.1)
            if not (got is True or consent.answer("Reminders") is True):
                lost.append((i, got, consent.answer("Reminders")))
        check("30 races at the timeout: the Continue always lands somewhere", not lost, lost)
    finally:
        lm.CONSENT_WAIT = prev


# ---------------------------------------------------------------- P0-20
def test_contacts_line_names_both_dialogs_when_whatsapp_is_here():
    for here, key in ((True, "jit_contacts_two"), (False, "jit_contacts")):
        reset(True)
        WHATSAPP["here"] = here
        lst = make()
        got = lst.confirm_contacts()
        check(f"WhatsApp {'installed' if here else 'not installed'}: the {key} line",
              got is True and [a["message"] for a in ALERTS] == [L(key, "en")], ALERTS)
    check("her Continue on the contacts line covers the Contacts app's Automation prompt",
          consent.answer("Contacts") is True and consent.explain("Contacts") is True
          and len(ALERTS) == 1, len(ALERTS))
    reset(False)
    WHATSAPP["here"] = True
    check("Not now: False, and Contacts is not marked allowed",
          make().confirm_contacts() is False and consent.answer("Contacts") is None)
    WHATSAPP["here"] = False


# ---------------------------------------------------------------- P2-1
def test_not_now_on_the_mic_line_gives_a_quiet_bar_line():
    reset(False)
    TCC.update(mic=0, speech=3)
    lst = make()
    lst.ask_just_in_time()
    check("first press: MicMic's microphone line", len(ALERTS) == 1
          and ALERTS[0]["message"] == L("jit_mic", "en"), ALERTS)
    lst._jit_at = 0.0                        # a minute later, past JIT_REPEAT
    lst.ask_just_in_time()
    check("pressed again after Not now: no modal", len(ALERTS) == 1, len(ALERTS))
    check("a quiet bar line instead, that mentions typing",
          ("set_result", L("jit_mic_quiet", "en"), None) in lst.bar.calls
          and ("show",) in lst.bar.calls, lst.bar.calls)
    check("and the menu's status line says it", lst.status_text == L("jit_mic_quiet", "en"),
          lst.status_text)
    check("nothing asked of macOS", ASKED == [], ASKED)
    lst._jit_at = 0.0
    lst._jit_quiet["microphone"] = time.time() - lm.JIT_QUIET_FOR - 1
    lst.ask_just_in_time()
    check("after 10 minutes: the line once more", len(ALERTS) == 2, len(ALERTS))
    TCC.update(mic=3, speech=3)
    time.sleep(0.15)


def test_yes_on_the_mic_line_is_not_quieted():
    reset(None)
    TCC.update(mic=0, speech=3)
    lst = make()
    lst.ask_just_in_time()
    ALERTS[0]["on_ok"]()                     # she clicks Allow microphone
    check("her yes asks macOS for the microphone", ASKED == ["microphone"], ASKED)
    check("and takes back the Not now", "microphone" not in lst._jit_quiet, lst._jit_quiet)
    lst._jit_at = 0.0
    lst.ask_just_in_time()
    check("still not answered by macOS: a later press explains again, no quiet line",
          len(ALERTS) == 2 and not any(c[0] == "set_result" for c in lst.bar.calls),
          (len(ALERTS), lst.bar.calls))
    TCC.update(mic=3, speech=3)
    time.sleep(0.15)


def test_speech_not_now_quiet_line_in_hebrew_panel():
    reset(False)
    TCC.update(mic=3, speech=0)
    lst = make(display="panel")
    lst.locale = "he-IL"
    lst.ask_just_in_time()
    lst._jit_at = 0.0
    lst.ask_just_in_time()
    check("speech, Not now, then a press: the Hebrew quiet line, the window without focus",
          len(ALERTS) == 1 and lst.status_text == lm.LANG["he"]["jit_speech_quiet"]
          and ("show", False) in lst.panel.calls, (len(ALERTS), lst.status_text, lst.panel.calls))
    TCC.update(mic=3, speech=3)
    time.sleep(0.15)


def test_registration_in_the_bundle():
    src = (ROOT / "native" / "listener.py").read_text()
    start = src.index("\ndef ensure_server")
    body = src[start:src.index("\ndef ", start + 10)]
    check("the bundle registers the Automation line and the Screen Recording ask",
          "_consent.register(LISTENER.explain_automation)" in body
          and "_consent.register_screen(LISTENER.ask_screen_recording)" in body)
    check("nothing at launch calls the Screen Recording request",
          "request_screen_capture" not in src[src.index("def applicationDidFinishLaunching_"):
                                              src.index("def showFromOtherLaunch_")])
    owners = []
    i = src.find("request_screen_capture()")
    while i != -1:
        if src[i - 4:i] != "def ":
            d = src.rfind("    def ", 0, i)
            owners.append(src[d + 8:src.index("(", d)])
        i = src.find("request_screen_capture()", i + 1)
    outer = src.rfind("    def ask_screen_recording", 0, src.rfind("request_screen_capture()"))
    check("request_screen_capture is called only from ask_screen_recording's yes",
          owners == ["yes"] and outer != -1, owners)


def main():
    for t in (test_lines_in_every_language,
              test_explain_automation_continue_and_not_now,
              test_explain_automation_language_and_unknown_target,
              test_explain_automation_no_answer_then_late_continue,
              test_consent_drives_the_real_listener,
              test_screen_recording_first_time,
              test_screen_recording_asked_on_an_earlier_run,
              test_screen_recording_not_now,
              test_screen_recording_in_her_language_and_panel_mode,
              test_screen_recording_no_answer_then_late_continue,
              test_screen_recording_no_answer_then_late_not_now,
              test_screen_recording_line_up_flag_cleared_on_answer,
              test_automation_line_hands_focus_back,
              test_automation_focus_not_back_is_reported_not_assumed,
              test_automation_late_answer_does_not_steal_focus,
              test_automation_answer_at_the_timeout_is_never_lost,
              test_contacts_line_names_both_dialogs_when_whatsapp_is_here,
              test_not_now_on_the_mic_line_gives_a_quiet_bar_line,
              test_yes_on_the_mic_line_is_not_quieted,
              test_speech_not_now_quiet_line_in_hebrew_panel,
              test_registration_in_the_bundle):
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(f"{t.__name__} ran to the end", False, repr(e)[:400])
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
