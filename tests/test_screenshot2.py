#!/usr/bin/env python3
"""Second screenshot-feature regression suite: interactive region capture ("send
this" with nothing selected), the "I selected it" continuation, a calendar date
with no time, and prefs "always ask" reaching a screen-sourced send.

    cd /Users/zohar/jev/savta && python3 tests/test_screenshot2.py

Same shape and the same zero-cost design as tests/test_screenshot.py (read that
file's docstring for why): router.understand() is replaced by a controlled
stand-in built from _u(), and the Jev object is a _ScriptedJev that never
reaches the network. Unlike test_screenshot.py, a few scenarios here exercise
the REAL router._resume() (the calendar time-of-day question, in particular),
so _ScriptedJev is extended with a couple of controllable answers rather than
bypassing _resume() outright - the thing actually being tested is that branch's
own logic. This suite makes 0 real Jev calls under any MICMIC_REPLAY setting.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if os.environ.get("MICMIC_ALLOW_SEND", "").lower() in ("1", "true", "yes"):
    print("REFUSING TO RUN: MICMIC_ALLOW_SEND is set in this shell.\n"
          "This suite arms real sends and relies on the dry-run gate to stop them.")
    sys.exit(2)
os.environ.pop("MICMIC_ALLOW_SEND", None)

_STATE = tempfile.mkdtemp(prefix="micmic-test-screenshot2-state-")
os.environ["MICMIC_STATE_DIR"] = _STATE
atexit.register(lambda: shutil.rmtree(_STATE, ignore_errors=True))

from savta import router                       # noqa: E402
from savta import profile as prof              # noqa: E402
from savta import prefs                        # noqa: E402
from savta.actions import mac                  # noqa: E402
from savta.actions import screen               # noqa: E402
from savta.actions import contacts as book     # noqa: E402

import replay                                  # noqa: E402
replay.install()

router.due_briefing = lambda: False

prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done",
           "name": "Test", "language": "hebrew", "speech_lang": "he-IL", "city": "חיפה"})

# ---------------------------------------------------------------- walls
import subprocess as _sp_mod              # noqa: E402

LAUNCHED: list[list] = []
_REAL_RUN = _sp_mod.run
_BLOCK_CMDS = ("open", "osascript", "screencapture", "sips", "lsappinfo", "mdfind", "pmset")


def _blocked_run(cmd, *a, **kw):
    if isinstance(cmd, (list, tuple)) and cmd and cmd[0] in _BLOCK_CMDS:
        LAUNCHED.append(list(cmd))
        class _R:
            returncode, stdout, stderr = 0, "[test] blocked", ""
        return _R()
    return _REAL_RUN(cmd, *a, **kw)


_sp_mod.run = _blocked_run
mac.subprocess.run = _blocked_run
screen.subprocess.run = _blocked_run

OSA: list[str] = []


def _blocked_osa(script, timeout=15.0):
    OSA.append(script)
    return False, "[test] osascript blocked"


mac._osa = _blocked_osa

SENT: list[tuple] = []
SENT_ATTACH: list[tuple] = []
WA: list[tuple] = []
WA_OPENED: list[str] = []
SAID: list[tuple] = []
CLIP: list[str] = []
EVENTS: list[tuple] = []                  # (title, date, start, end, location, all_day)


def _stub_send_message(n, t, attachment=None):
    SENT.append((n, t))
    if attachment:
        SENT_ATTACH.append((n, attachment))
    return True, "[test stub] imessage"


def _stub_add_event(title, date, start, end="", location="", all_day=False):
    EVENTS.append((title, date, start, end, location, all_day))
    return True, "MicMic"


mac.send_message = _stub_send_message
mac.whatsapp = lambda n, t: (WA.append((n, t)), (True, "[test stub] whatsapp"))[1]
mac.whatsapp_open_chat = lambda n: (WA_OPENED.append(n), (True, "[test stub] opened chat"))[1]
mac.copy_file_to_clipboard = lambda p: (CLIP.append(p), True)[1]
mac.say = lambda t, l="english": SAID.append((l, t))
mac.screen_locked = lambda: False
mac.add_event = _stub_add_event

FIXED_BOOK = [{"name": "Zohar Levin", "phone": "+972500000001",
              "waid": "972500000001@s.whatsapp.net", "source": "fixture"}]
book.all_contacts = lambda force=False, wait=None: list(FIXED_BOOK)
router.get_contacts = lambda utterance="", wait=None: ["Zohar Levin"]

# ---------------------------------------------------------------- harness
PASSED = 0
FAILED: list[tuple[str, str]] = []


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


def short(v, n=200):
    s = repr(v)
    return s if len(s) <= n else s[:n] + "..."


def reset_state():
    router._cancel_pending()
    router.AWAITING = None
    router.MEM = router.Memory()
    router._LAST_SHOT.clear()
    prefs.set_confirm(False)
    for r in (SENT, SENT_ATTACH, WA, WA_OPENED, SAID, CLIP, OSA, LAUNCHED, EVENTS):
        r.clear()


# ---- the same fake understand() answer tests/test_micmic.py's scripted_turns and
# tests/test_screenshot.py build, kept in step with brain.py's current schema.
_U_KEYS = ("intent_confidence", "wants_full_length", "names_title", "contact_confidence",
          "contact_named", "has_message_content", "money_involved", "sounds_coached",
          "control_confidence", "wants_recent", "asking_for_notes", "is_complete",
          "noise", "refers_back", "is_compound", "needs_knowledge", "about_weather",
          "about_clock", "setting_emergency_contact", "inside_an_app",
          "speaker_gender_confidence", "rejects_last", "describes_instead",
          "refers_to_screen", "distress", "emergency", "wants_undo", "amends_message",
          "live_kind_confidence")


def _u(intent="message", **kw) -> dict:
    u = {k: 0.0 for k in _U_KEYS}
    u.update({"raw": {}, "spans": {}, "intent": intent, "intent_probs": {intent: 0.9},
              "media_kind": "not_applicable", "contact": "nobody",
              "control_action": "not_applicable", "player_action": "not_applicable",
              "channel": "imessage", "file_kind": "any", "when_minutes": "none",
              "language": "english", "speaker_gender": "unrevealed",
              "screen_task": "not_applicable", "weather_day": "today",
              "write_in": "not_applicable", "message_app": "unchanged",
              "live_kind": "not_live", "standing": {},
              "intent_confidence": 0.95, "is_complete": 0.95, "contact_confidence": 0.9})
    u.update(kw)
    return u


class _ScriptedJev:
    """Like test_screenshot.py's own, plus two controllable answers this file
    needs for scenarios that exercise the real router._resume() rather than
    bypassing it: "is this an answer" defaults to yes (every scripted follow-up
    here really is one), and the calendar time-of-day choice is settable."""

    def __init__(self):
        self.calls, self.cost_usd, self.busy_ms = 0, 0.0, 0.0
        self.event_time_answer = ("not_said", 0.9)

    def ask(self, state, qs):
        self.calls += 1
        out = {}
        for k, q in qs.items():
            if k == "is_answer":
                out[k] = {"noul": 0.9}
            elif k == "agreed":
                out[k] = {"noul": 0.9}
            elif k == "event_time":
                pick, conf = self.event_time_answer
                out[k] = {"choice": pick, "confidence": conf, "probabilities": {pick: conf}}
            elif q["type"] == "choice":
                opts = list(q["criteria"])
                pick = "nobody" if "nobody" in opts else opts[0]
                out[k] = {"choice": pick, "confidence": 0.9, "probabilities": {pick: 0.9}}
            elif q["type"] == "noul":
                out[k] = {"noul": 0.0}
            else:
                out[k] = {"score": 0.0}
        return out


_j = _ScriptedJev()


def fake_u(**overrides) -> dict:
    u = _u(intent="screen", channel="not_applicable", screen_task="not_applicable")
    u.update(overrides)
    return u


_REAL_UNDERSTAND = router.understand


def with_understand(u: dict):
    class _Ctx:
        def __enter__(self):
            router.understand = lambda *a, **k: u
            return self

        def __exit__(self, *exc):
            router.understand = _REAL_UNDERSTAND
            return False
    return _Ctx()


def with_same_person(confidence: float = 1.0):
    real = router._same_person

    class _Ctx:
        def __enter__(self):
            router._same_person = lambda j, utterance, contact: confidence
            return self

        def __exit__(self, *exc):
            router._same_person = real
            return False
    return _Ctx()


def with_frontmost(app: str, pid: int = 4242):
    real = screen.frontmost

    class _Ctx:
        def __enter__(self):
            screen.frontmost = lambda: {"app": app, "pid": pid, "bundle_id": "", "window": app}
            return self

        def __exit__(self, *exc):
            screen.frontmost = real
            return False
    return _Ctx()


def with_screen_context(ctx: dict):
    """router._screen_context() (Accessibility text/selection/page) stood in, for
    the same reason take_screenshot/frontmost are: no real screen to read here."""
    real = router._screen_context

    class _Ctx:
        def __enter__(self):
            router._screen_context = lambda max_chars=6000: dict(ctx)
            return self

        def __exit__(self, *exc):
            router._screen_context = real
            return False
    return _Ctx()


def with_event_from_screen(ev: dict | None):
    real = router._event_from_screen

    class _Ctx:
        def __enter__(self):
            router._event_from_screen = lambda ctx, lang: dict(ev) if ev else None
            return self

        def __exit__(self, *exc):
            router._event_from_screen = real
            return False
    return _Ctx()


class gates_on:
    def __enter__(self):
        self.prev = mac.SEND_FOR_REAL
        mac.SEND_FOR_REAL = True
        return self

    def __exit__(self, *exc):
        mac.SEND_FOR_REAL = self.prev
        return False


READABLE_CTX = {"permissions": {"accessibility": True, "screen_recording": True},
                "frontmost": {"app": "Safari", "pid": 4242, "bundle_id": "", "window": "Safari"},
                "selected": "", "focused": {"role": "", "value": "", "secure": False},
                "visible": {"text": "", "truncated": False}, "page": None, "has_image": False}


def readable_ctx(**over) -> dict:
    d = {**READABLE_CTX}
    d.update(over)
    return d


# ==================================================================== calendar
def t_calendar_date_no_time_then_timed():
    """1. A date with no time asks "what time, or the whole day", and a real time
    answer produces a normal timed event, still behind the usual confirmation."""
    reset_state()
    ev = {"title": "Pyramids of Giza", "date": "2026-11-28", "start": "", "end": "",
         "location": ""}
    u = fake_u(intent="screen", screen_task="add_to_calendar")
    with with_understand(u), with_frontmost("Safari"), \
         with_screen_context(readable_ctx()), with_event_from_screen(ev):
        r = router.handle(_j, "please put the concert on screen in the calendar", speak=False)
    check("a date with no time asks what time, rather than refusing the event",
          r["did"] == "need_event_time", short(r))
    check("it offers the whole day as an alternative",
          "whole day" in (r.get("say") or "").lower(), repr(r.get("say")))
    aw = router.AWAITING or {}
    check("the partial event (date, no time) is held for the follow-up",
          aw.get("event", {}).get("date") == "2026-11-28" and not aw["event"].get("start"),
          short(aw))

    _j.event_time_answer = ("20:30", 0.95)
    r2 = router.handle(_j, "eight thirty in the evening", speak=False)
    check("a real time moves on to the normal confirm-before-adding step",
          r2["did"] == "confirm_calendar", short(r2))
    check("the confirmation names the time she gave, not the whole day",
          "20:30" in (r2.get("say") or ""), repr(r2.get("say")))

    # "yesno" asks its own dedicated question (_ScriptedJev's "agreed" default is yes).
    r3 = router.handle(_j, "yes", speak=False)
    check("she is asked to confirm, then it is actually added, with the time she gave",
          r3["did"] == "calendar_added" and EVENTS
          and EVENTS[-1][1:3] == ("2026-11-28", "20:30")
          and EVENTS[-1][5] is False,
          f"did={r3['did']!r} events={EVENTS}")


def t_calendar_date_no_time_then_all_day():
    """2. The same date-with-no-time event, answered "all day": an all-day event,
    still behind the same confirmation."""
    reset_state()
    ev = {"title": "Pyramids of Giza", "date": "2026-11-28", "start": "", "end": "",
         "location": ""}
    u = fake_u(intent="screen", screen_task="add_to_calendar")
    with with_understand(u), with_frontmost("Safari"), \
         with_screen_context(readable_ctx()), with_event_from_screen(ev):
        router.handle(_j, "please put the concert on screen in the calendar", speak=False)
    _j.event_time_answer = ("all_day", 0.95)
    r2 = router.handle(_j, "the whole day", speak=False)
    check("'the whole day' moves on to an all-day confirmation",
          r2["did"] == "confirm_calendar" and "whole day" in (r2.get("say") or "").lower(),
          short(r2))
    r3 = router.handle(_j, "yes", speak=False)
    check("it is added as an all-day event",
          r3["did"] == "calendar_added" and EVENTS and EVENTS[-1][5] is True,
          f"did={r3['did']!r} events={EVENTS}")


def t_calendar_unclear_time_asks_again():
    """3. An answer that names no time and no "whole day" asks again, rather than
    guessing midnight."""
    reset_state()
    ev = {"title": "Pyramids of Giza", "date": "2026-11-28", "start": "", "end": "",
         "location": ""}
    u = fake_u(intent="screen", screen_task="add_to_calendar")
    with with_understand(u), with_frontmost("Safari"), \
         with_screen_context(readable_ctx()), with_event_from_screen(ev):
        router.handle(_j, "please put the concert on screen in the calendar", speak=False)
    _j.event_time_answer = ("not_said", 0.9)
    r2 = router.handle(_j, "hmm not sure", speak=False)
    check("an unclear answer asks again instead of guessing",
          r2["did"] == "need_event_time" and not EVENTS, short(r2))


def t_calendar_routes_without_the_word_screen():
    """4. "add to the calendar concert in giza" names no "screen" or "this", so
    refers_to_screen never fires - but Jev's own screen_task ("add_to_calendar",
    asked on every turn) is exactly as strong a signal that this is about the
    screen, and now rescues it out of chitchat."""
    reset_state()
    ev = {"title": "Concert in Giza", "date": "2026-11-28", "start": "20:00", "end": "",
         "location": "Giza"}
    u = fake_u(intent="chitchat", screen_task="add_to_calendar", refers_to_screen=0.05)
    with with_understand(u), with_frontmost("Safari"), \
         with_screen_context(readable_ctx()), with_event_from_screen(ev):
        r = router.handle(_j, "please add to the calendar concert in giza", speak=False)
    check("intent chitchat + screen_task add_to_calendar still reaches the calendar path",
          r["did"] == "confirm_calendar", short(r))


def t_calendar_full_event_unaffected():
    """5. A screen event that already has a time is unaffected: straight to the
    usual confirmation, no new question in between."""
    reset_state()
    ev = {"title": "Dentist", "date": "2026-10-05", "start": "10:00", "end": "10:30",
         "location": ""}
    u = fake_u(intent="screen", screen_task="add_to_calendar")
    with with_understand(u), with_frontmost("Safari"), \
         with_screen_context(readable_ctx()), with_event_from_screen(ev):
        r = router.handle(_j, "add this to my calendar", speak=False)
    check("a fully-timed event is not asked about a time at all",
          r["did"] == "confirm_calendar" and "10:00" in (r.get("say") or ""), short(r))


# ==================================================================== interactive capture
def t_interactive_capture_sends():
    """6. "send this to Zohar Levin" with nothing selected and no page open: the
    crosshair starts right away (never "select and disappear"), and a completed
    drag sends it with the normal read-back and countdown."""
    reset_state()
    router.CANCEL_WINDOW = 0.3
    with gates_on():
        started = threading.Event()
        fake_png = os.path.join(_STATE, "region.png")

        def fake_capture_region(on_start=None):
            if on_start:
                on_start(object())
            started.set()
            return {"ok": True, "path": fake_png}

        real_capture = screen.capture_region
        screen.capture_region = fake_capture_region
        try:
            u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0,
                      channel="not_applicable", refers_to_screen=0.9)
            with with_understand(u), with_same_person(), with_frontmost("Safari"), \
                 with_screen_context(readable_ctx()):
                r = router.handle(_j, "send this to Zohar Levin", speak=False)
            check("it answers at once with the drag instruction, not a bail-out",
                  r["did"] == "screen_select_wait" and "drag" in (r.get("say") or "").lower(),
                  short(r))
            check("nothing is sent yet", not SENT and not SENT_ATTACH, f"{SENT} {SENT_ATTACH}")
            assert started.wait(2.0), "capture never started"
            deadline = time.time() + 2.0
            while router.PENDING is None and time.time() < deadline:
                time.sleep(0.02)
            check("the completed drag arms the send, attached",
                  router.PENDING is not None and router.PENDING.get("attachment") == fake_png,
                  short(router.PENDING))
            time.sleep(0.6)
            check("it is delivered with the picked region attached",
                  SENT_ATTACH == [("Zohar Levin", fake_png)], f"attach={SENT_ATTACH}")
            check("she was told it was sending, with a description, not a raw placeholder",
                  any("Zohar" in t and "{what}" not in t for _, t in SAID), short(SAID))
        finally:
            screen.capture_region = real_capture
            router.CANCEL_WINDOW = 6.0


def t_interactive_capture_cancelled():
    """7. Esc during the drag: "Alright, nothing sent." - never silence, never a
    claim that something went out."""
    reset_state()
    with gates_on():
        def fake_capture_region(on_start=None):
            if on_start:
                on_start(object())
            return {"ok": False, "why": "cancelled"}

        real_capture = screen.capture_region
        screen.capture_region = fake_capture_region
        try:
            u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0,
                      refers_to_screen=0.9)
            with with_understand(u), with_same_person(), with_frontmost("Safari"), \
                 with_screen_context(readable_ctx()):
                router.handle(_j, "send this to Zohar Levin", speak=False)
            deadline = time.time() + 2.0
            while not SAID and time.time() < deadline:
                time.sleep(0.02)
            check("cancelling says nothing was sent, in so many words",
                  any("nothing sent" in t.lower() for _, t in SAID), short(SAID))
            check("nothing was armed or delivered", router.PENDING is None and not SENT,
                  f"pending={router.PENDING} sent={SENT}")
        finally:
            screen.capture_region = real_capture


def t_i_selected_it_continues_the_send():
    """8. While the crosshair is still up, she highlights text herself and says
    "I selected it": that text is sent immediately, on the usual countdown, and
    the crosshair is told to close rather than left running unattended."""
    reset_state()
    router.CANCEL_WINDOW = 0.3
    with gates_on():
        gate = threading.Event()
        terminated = []

        class _FakeProc:
            def terminate(self):
                terminated.append(True)
                gate.set()

        def fake_capture_region(on_start=None):
            if on_start:
                on_start(_FakeProc())
            gate.wait(2.0)              # blocks "until" terminate() is called
            return {"ok": False, "why": "cancelled"}

        real_capture = screen.capture_region
        screen.capture_region = fake_capture_region
        try:
            u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0,
                      refers_to_screen=0.9)
            with with_understand(u), with_same_person(), with_frontmost("Safari"), \
                 with_screen_context(readable_ctx()):
                r = router.handle(_j, "send this to Zohar Levin", speak=False)
            check("armed the interactive capture first", r["did"] == "screen_select_wait", short(r))

            with with_screen_context(readable_ctx(selected="a paragraph she highlighted")):
                r2 = router.handle(_j, "I selected it", speak=False)
            check("her own selection sends immediately, without waiting for the drag",
                  r2["did"] == "sending", short(r2))
            check("the crosshair is told to close",
                  terminated == [True], f"terminated={terminated}")
            time.sleep(0.6)
            check("the selected text goes out, not an attachment",
                  ("Zohar Levin", "a paragraph she highlighted") in SENT, f"sent={SENT}")
            check("AWAITING no longer holds the capture", router.AWAITING is None, short(router.AWAITING))
        finally:
            screen.capture_region = real_capture
            router.CANCEL_WINDOW = 6.0


def t_i_selected_it_nothing_selected_yet():
    """9. She says "I selected it" but nothing is actually selected: told to keep
    dragging (or select something), not silently dropped."""
    reset_state()
    with gates_on():
        gate = threading.Event()

        def fake_capture_region(on_start=None):
            if on_start:
                on_start(object())
            gate.wait(2.0)
            return {"ok": False, "why": "cancelled"}

        real_capture = screen.capture_region
        screen.capture_region = fake_capture_region
        try:
            u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0,
                      refers_to_screen=0.9)
            with with_understand(u), with_same_person(), with_frontmost("Safari"), \
                 with_screen_context(readable_ctx()):
                router.handle(_j, "send this to Zohar Levin", speak=False)
            with with_screen_context(readable_ctx(selected="")):
                r2 = router.handle(_j, "I selected it", speak=False)
            check("still nothing selected: she is asked again, not dropped",
                  r2["did"] == "screen_select_wait", short(r2))
            check("the capture is still pending", (router.AWAITING or {}).get("need")
                  == "screen_select_pending", short(router.AWAITING))
        finally:
            gate.set()
            screen.capture_region = real_capture


# ==================================================================== prefs always-ask
def t_always_ask_reaches_screenshot_send():
    """10. prefs.always_confirm() ("ask me before sending any message") now
    reaches a screenshot send too, not only a spoken message: it asks first,
    describing what is being sent rather than quoting nonexistent words."""
    reset_state()
    prefs.set_confirm(True)
    try:
        with gates_on():
            fake_png = os.path.join(_STATE, "screenshot.png")

            def fake_capture(pid=None):
                return {"ok": True, "path": fake_png}

            real_capture = screen.take_screenshot
            screen.take_screenshot = fake_capture
            try:
                u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0)
                with with_understand(u), with_same_person(), with_frontmost("Safari"):
                    r = router.handle(_j, "send the screenshot to Zohar Levin", speak=False)
                check("she is asked first, not sent on a countdown",
                      r["did"] == "confirm_send" and not SENT, short(r))
                check("the question describes it as a screenshot, no quoted body text",
                      "screenshot" in (r.get("say") or "").lower(), repr(r.get("say")))
                r2 = router.handle(_j, "yes", speak=False)
                check("a clear yes arms it, attached",
                      r2["did"] == "sending" and router.PENDING is not None
                      and router.PENDING.get("attachment") == fake_png, short(r2))
            finally:
                screen.take_screenshot = real_capture
    finally:
        prefs.set_confirm(False)


# ==================================================================== remembered screenshot
def t_recent_screenshot_reused_without_the_word():
    """11. "take a screenshot", then a few seconds later "send it to Dana" with
    no fresh mention of "screenshot": the one just taken is reused."""
    reset_state()
    router.CANCEL_WINDOW = 0.3
    with gates_on():
        fake_png = os.path.join(_STATE, "remember.png")
        open(fake_png, "wb").close()   # _recent_screenshot() checks the file is really there

        def fake_capture(pid=None):
            return {"ok": True, "path": fake_png}

        real_capture = screen.take_screenshot
        screen.take_screenshot = fake_capture
        try:
            u1 = fake_u(intent="screen", screen_task="not_applicable")
            with with_understand(u1), with_frontmost("Safari"):
                r1 = router.handle(_j, "take a screenshot", speak=False)
            check("the screenshot is saved first", r1["did"] == "screenshot_saved", short(r1))

            u2 = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0,
                       refers_to_screen=0.9)
            with with_understand(u2), with_same_person(), with_frontmost("Safari"), \
                 with_screen_context(readable_ctx()):
                r2 = router.handle(_j, "send it to Zohar Levin", speak=False)
            check("the just-taken screenshot is reused as the attachment",
                  r2["did"] == "sending", short(r2))
            time.sleep(0.6)
            check("it is delivered", SENT_ATTACH == [("Zohar Levin", fake_png)],
                  f"attach={SENT_ATTACH}")
        finally:
            screen.take_screenshot = real_capture
            router.CANCEL_WINDOW = 6.0


def t_recent_screenshot_expires():
    """12. Past the TTL, an old screenshot is no longer offered - she is asked to
    select something instead (which now means the interactive capture starts)."""
    reset_state()
    with gates_on():
        router._remember_shot("/tmp/does-not-matter.png", "", False)
        router._LAST_SHOT["at"] = time.time() - router.LAST_SHOT_TTL - 1

        def never_capture(on_start=None):
            raise AssertionError("should not be reached in this check")

        real_capture = screen.capture_region
        screen.capture_region = lambda on_start=None: {"ok": False, "why": "cancelled"}
        try:
            u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0,
                      refers_to_screen=0.9)
            with with_understand(u), with_same_person(), with_frontmost("Safari"), \
                 with_screen_context(readable_ctx()):
                r = router.handle(_j, "send it to Zohar Levin", speak=False)
            check("an expired screenshot is not reused; it falls through to the crosshair",
                  r["did"] == "screen_select_wait", short(r))
        finally:
            screen.capture_region = real_capture


TESTS = [
    ("1. calendar: date with no time, then a real time", t_calendar_date_no_time_then_timed),
    ("2. calendar: date with no time, then all day", t_calendar_date_no_time_then_all_day),
    ("3. calendar: an unclear time answer asks again", t_calendar_unclear_time_asks_again),
    ("4. calendar: routed without the word 'screen'", t_calendar_routes_without_the_word_screen),
    ("5. calendar: a fully-timed event is unaffected", t_calendar_full_event_unaffected),
    ("6. interactive capture: drag and it sends", t_interactive_capture_sends),
    ("7. interactive capture: Esc says nothing sent", t_interactive_capture_cancelled),
    ("8. 'I selected it' continues the send", t_i_selected_it_continues_the_send),
    ("9. 'I selected it' with nothing selected yet", t_i_selected_it_nothing_selected_yet),
    ("10. prefs always-ask reaches a screenshot send", t_always_ask_reaches_screenshot_send),
    ("11. a recent screenshot is reused without the word", t_recent_screenshot_reused_without_the_word),
    ("12. an expired screenshot is not reused", t_recent_screenshot_expires),
]


def main() -> int:
    for name, fn in TESTS:
        print(name)
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback
            FAILED.append((name, "raised an exception"))
            print(f"  FAIL  {name} raised an exception\n" +
                 "".join(f"          {l}\n" for l in traceback.format_exc().splitlines()))
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    print(f"replayed {replay.counts['replayed']}, live {replay.counts['live']} "
         f"(MICMIC_REPLAY={replay.MODE})")
    print(f"never sent for real: {len(OSA)} osascript calls escaped the stub (must be 0)")
    if FAILED:
        print("\nFailures:")
        for name, detail in FAILED:
            print(f"  - {name}\n      {detail}" if detail else f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
