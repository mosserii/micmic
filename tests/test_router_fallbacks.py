#!/usr/bin/env python3
"""Router honesty without permissions (PERMISSION_FALLBACKS.md section 4, lane L2, and
the router handoffs of lanes L3 mac actions, L4 consent lines, L5 page button and L6
screenshots in micmic-work/qa/ROUTER_HANDOFFS.md):

    cd <checkout> && uv run python tests/test_router_fallbacks.py

  P0-7  "read my messages" with no access: never "No new messages"; WhatsApp opened,
        the Full Disk Access line, "Want me to open it?" and its yes opens the pane.
  P0-8  calls: _call_contact honours SEND_FOR_REAL and CALL_FOR_REAL (call_off);
        facetime_confirm is "FaceTime is ready to call Dana. Press Call.", never
        "calling"; the emergency loop stops at the first sheet, no redial latch.
  P0-9  control replies follow the result: brightness, close, pause (Music, Spotify,
        a video tab, a player she started herself).
  P0-10 a dated reminder Reminders would not take is MicMic's spoken reminder at 9:00
        that day, said honestly; no date: kept in MicMic, said so.
  P0-11 a screenshot without Screen Recording: MicMic's line and the one ask, else the
        Screenshot app; "send this screen" via her own Control-Shift-Command-4 and the
        clipboard, then the chat to paste into. Never "Saved", never "turn MicMic on"
        before MicMic is in the list.
  P1-2  Messages without Automation: clipboard, Messages opened, paste and Return.
  P1-3  Calendar without Automation: Calendar's own sheet (.ics).
  P1-4  "stop" over a video closes MicMic's own tab (or pauses it), never Cmd-W.
  P1-5  Accessibility-only features say why, with detail.fix.pane for the page button.
  P1-6  screen questions without Accessibility: Chrome's page, or what she copied.
  P2-3  "what did I write down" says where notes went.
  Also: detail.fix = {"pane": ...} on every reply that asks for a permission.

Zero live calls: Jev is scripted (tests/test_followups.py), Gemini is its fake, the
stub wall is tests/test_micmic.py. No osascript, no `open`, nothing reaches
127.0.0.1:8799, no page is fetched from the network. Every name is made up.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

os.environ["MICMIC_DRY_OPEN"] = "1"            # the pane opener never opens anything
os.environ.pop("MICMIC_FAKE_SCREEN_RECORDING", None)
os.environ.pop("MICMIC_FAKE_AUTOMATION", None)
os.environ["MICMIC_FAKE_PASTE_ACCESS"] = "free"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_followups as tf                    # noqa: E402  (loads the stub wall)

router, mac, tm = tf.router, tf.mac, tf.tm
from savta import consent                      # noqa: E402
from savta import profile as prof              # noqa: E402
from savta.actions import contacts as book     # noqa: E402
from savta.actions import music as am          # noqa: E402
from savta.actions import screen               # noqa: E402
from savta.actions import apps as apps_mod     # noqa: E402

os.environ["MICMIC_SHOTS_DIR"] = os.path.join(os.environ["MICMIC_STATE_DIR"], "shots")

PASSED = 0
FAILED: list[tuple[str, str]] = []
LANGS = ("english", "hebrew", "arabic", "russian")


def check(name: str, ok, detail="") -> bool:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, str(detail)))
        print(f"  FAIL  {name}\n          {detail}")
    return bool(ok)


# ---------------------------------------------------------------- her address book
BOOK_ROWS = [("Dana Cohen", "Dana", "+15550000003"), ("Gal Ben Ami", "Gal", "+15550000004"),
             ("Noa Levi", "Noa", "+15550000005"), ("Nora Bloom", "Nora", "")]
BOOK = []
for _n, _f, _p in BOOK_ROWS:
    _r = {"name": _n, "first": _f, "phone": _p,
          "waid": (_p.lstrip("+") + "@s.whatsapp.net") if _p else "", "source": "fixture"}
    _r["keys"] = book.keys_for(_n) | book.keys_for(_f)
    BOOK.append(_r)
book.all_contacts = lambda force=False, wait=None: list(BOOK)
book.recent_chats = lambda limit=30: [dict(r) for r in BOOK[:limit]]
book._CACHE.update(at=time.time() * 10, rows=list(BOOK))

CALLING = ("calling", "i am calling", "מתקשרת", "عم بتصل", "звоню")


def says_calling(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in CALLING)


def det(r: dict) -> dict:
    return r.get("detail") if isinstance(r.get("detail"), dict) else {}


def pane(r: dict) -> str | None:
    return (det(r).get("fix") or {}).get("pane") if isinstance(det(r).get("fix"), dict) else None


class patched:
    """Set attributes for one block, put them back after."""

    def __init__(self, obj, **kw):
        self.obj, self.kw, self.old = obj, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.obj, k)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.obj, k, v)
        return False


class env:
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def fresh():
    tf.fresh()
    for x in (tm.SAID, tm.CALLED, tm.TABS_CLOSED, tm.BRIGHT, tm.NOTES, tm.REMINDERS,
              tm.WA_OPENED, tm.SENT_ATTACH):
        x.clear()
    router.LAST_EMERGENCY = None
    router.cancel_emergency()
    router._HISTORY.clear()
    router._CLIP_HINT.clear()
    consent._reset_for_tests()
    mac._GRANTED.clear()
    mac._DENIED_AT.clear()
    p = prof.load()
    p.update(emergency_contact="Dana Cohen", pinned=["Gal Ben Ami", "Noa Levi"])
    prof.save(p)


def wait_for(cond, secs: float = 3.0) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


def launched(prefix: list[str]) -> list[list]:
    return [c for c in tm.LAUNCHED if c[:len(prefix)] == prefix]


# ================================================================== P0-8 calls
def t_calls():
    print("\nt_calls (P0-8)")
    fresh()
    for lang in LANGS:
        did, said, d = router._call_contact("Dana Cohen", lang)
        check(f"{lang}: calls switched off: call_off, nothing dialled",
              did == "call_disabled" and said == router._sp(lang, "call_off")
              and not tm.CALLED, (did, said, tm.CALLED))
    real_ft = mac.facetime
    mac.facetime = tm.REAL_FACETIME
    try:
        for lang in LANGS:
            fresh()
            with tm.gates_on():
                did, said, d = router._call_contact("Dana Cohen", lang)
            check(f"{lang}: facetime_confirm is 'FaceTime is ready', never 'calling'",
                  did == "call_ready" and said == router._fb(
                      "call_ready", lang, who=router.for_speech("calling", "Dana Cohen", lang))
                  and not says_calling(said)
                  and str(d.get("result", "")).startswith("facetime_confirm"), (did, said, d))
        with tm.gates_on():
            did, said, _ = router._call_contact("Dana Cohen", "english")
        check("the English line, exactly",
              said == "FaceTime is ready to call Dana Cohen. Press Call.", said)

        # A whole turn: "call dana".
        fresh()
        with tm.gates_on():
            r, _ = tf.turn("call dana", intent="call",
                           extra={"contact": "Dana Cohen", "contact_is_named": 0.95,
                                  "same_person": 0.95})
        check("'call dana' with calls on: FaceTime ready, one sheet",
              r["did"] == "call_ready" and len(launched(["open"])) == 1
              and not says_calling(r.get("say")), (r["did"], r.get("say"), tm.LAUNCHED))
        fresh()
        r, _ = tf.turn("call dana", intent="call",
                       extra={"contact": "Dana Cohen", "contact_is_named": 0.95,
                              "same_person": 0.95})
        check("'call dana' with calls off: call_off, nothing opened (T8)",
              r["did"] == "call_disabled" and not launched(["open"])
              and r.get("say") == router._sp("english", "call_off"),
              (r["did"], r.get("say"), tm.LAUNCHED))

        # The emergency countdown runs out: one FaceTime sheet, no do-not-redial latch.
        fresh()
        with tm.gates_on():
            router._arm_emergency("Dana Cohen", "english", "x", False)
            pend_id = router.EMERGENCY_PENDING["id"]
            out = router._fire_emergency(pend_id, say_it=False)
        sheets = [c for c in tm.LAUNCHED if str(c[-1]).startswith("facetime")]
        check("emergency: the loop stops at the first FaceTime sheet",
              out and out["did"] == "call_ready" and len(sheets) == 1, (out, sheets))
        check("emergency: no do-not-redial latch for a call that was only made ready",
              router.LAST_EMERGENCY is None, router.LAST_EMERGENCY)
        check("emergency: it says FaceTime is ready, never 'I am calling'",
              out and "FaceTime is ready" in out["say"] and not says_calling(out["say"]),
              out and out.get("say"))
    finally:
        mac.facetime = real_ft


# ================================================================== P0-9 pause
def t_pause():
    print("\nt_pause (P0-9)")
    asked: list = []

    def state(app, ask=True):
        asked.append((app, ask))
        return "playing"

    refuse = lambda act: (False, "pause_failed:automation_denied:Music")  # noqa: E731
    for lang in LANGS:
        fresh()
        router.MEM.last_played = {"player": "apple_music", "title": "Song", "artist": "Band"}
        with patched(am, state=state, control=refuse):
            did, said, d = router._control_player("pause", lang)
        check(f"{lang}: Music would not pause: pause_failed, never 'I paused the music'",
              did == "pause_failed" and said != am.line("stopped", lang)
              and said == am.line("pause_failed", lang, app="Apple Music"), (did, said))
    fresh()
    router.MEM.last_played = {"player": "spotify", "title": "Song", "artist": "Band"}
    with patched(am, state=state,
                 spotify_control=lambda act: (False, "pause_failed:automation_declined:Spotify")):
        did, said, d = router._in_app_control("stop", "english")
    check("Spotify 'stop' refused: pause_failed with the space bar line",
          did == "pause_failed" and "space bar" in said and "Spotify" in said, (did, said))
    fresh()
    router.MEM.last_played = {"player": "apple_music", "title": "Song", "artist": "Band"}
    with patched(am, state=state, control=lambda act: (True, "")):
        did, said, d = router._control_player("pause", "english")
    check("Music took the pause: 'I paused the music.'",
          did == "stopped" and said == am.line("stopped", "english"), (did, said))
    with patched(am, state=state, control=lambda act: (False, "automation_denied:Music")):
        did, said, d = router._control_player("next", "english")
    check("'next' refused: never 'Alright'", did == "control_failed"
          and said != router._sp("english", "ok"), (did, said))
    with patched(am, control=lambda act: (False, "automation_denied:Music")):
        did, said, d = router._in_app_control("restart", "english")
    check("'start it again' refused: never 'Alright'", did == "control_failed"
          and said != router._sp("english", "ok"), (did, said))

    # A player she started herself, MicMic put nothing on (T16, T17).
    fresh()
    asked.clear()
    with patched(am, state=state, control=refuse):
        r, _ = tf.turn("pause", intent="player", extra={"player_action": "pause"})
    check("her own player: its state read with ask=False (no Automation prompt)",
          asked and all(a is False for _, a in asked), asked)
    check("her own player, pause refused: never 'I paused the music'",
          r["did"] == "pause_failed" and "space bar" in (r.get("say") or ""),
          (r["did"], r.get("say")))

    # A video in a tab MicMic opened: Chrome or System Events not allowed.
    fresh()
    router.MEM.last_played = {"player": "youtube", "id": "vid123", "title": "A film"}

    def tab_failed(frag, action, site="other"):
        mac.LAST_TAB_FAILURE[0] = "automation_denied:Google Chrome"
        return "failed"

    with patched(mac, tab_player=tab_failed):
        did, said, d = router._control_player("pause", "english")
    check("a video that would not pause: 'Press K in the video', nothing muted",
          did == "pause_failed" and "Press K" in said
          and d.get("why") == "automation_denied:Google Chrome", (did, said, d))
    check("and MicMic does not believe it is paused",
          not (router.MEM.last_played or {}).get("paused"), router.MEM.last_played)


# ================================================================== P1-4 stop a video
def t_stop_video():
    print("\nt_stop_video (P1-4)")
    for player, frag in (("youtube", "vid123"), ("netflix", "netflix.com")):
        fresh()
        router.MEM.last_played = {"player": player, "id": "vid123", "title": "A film"}
        r, _ = tf.turn("stop", intent="stop")
        check(f"{player}: 'stop' closes MicMic's own tab, never Cmd-W to the front app",
              r["did"] == "stopped" and tm.TABS_CLOSED == [frag] and not tm.CLOSED
              and router.MEM.last_played is None,
              (r["did"], tm.TABS_CLOSED, tm.CLOSED))
    fresh()
    router.MEM.last_played = {"player": "youtube", "id": "vid123", "title": "A film"}
    with patched(mac, close_tab_with=lambda frag: False,
                 tab_player=lambda frag, action, site="other": "keys"):
        r, _ = tf.turn("stop", intent="stop")
    check("Chrome would not close it: paused in its own tab, still no Cmd-W",
          r["did"] == "stopped" and det(r).get("paused") and not tm.CLOSED
          and (router.MEM.last_played or {}).get("paused"), (r["did"], det(r), tm.CLOSED))
    fresh()
    router.MEM.last_played = {"player": "youtube", "id": "vid123", "title": "A film"}
    with patched(mac, close_tab_with=lambda frag: False,
                 tab_player=lambda frag, action, site="other": "failed"):
        r, _ = tf.turn("stop", intent="player", extra={"player_action": "stop"})
    check("neither worked: she hears how, never 'Alright', no Cmd-W",
          r["did"] == "pause_failed" and "Press K" in (r.get("say") or "") and not tm.CLOSED,
          (r["did"], r.get("say"), tm.CLOSED))
    fresh()
    router.MEM.last_played = {"player": "youtube", "id": "vid123", "title": "A film"}
    with patched(mac, close_tab_with=lambda frag: False,
                 tab_player=lambda frag, action, site="other": "missing"):
        r, _ = tf.turn("stop", intent="stop")
    check("the tab is gone already: said so, nothing pressed anywhere",
          r["did"] == "nothing_playing" and not tm.CLOSED, (r["did"], r.get("say")))


# ================================================================== P0-9 brightness, close
def t_control():
    print("\nt_control (P0-9)")
    opened: list = []
    for lang in LANGS:
        fresh()
        opened.clear()
        with patched(mac, brightness=lambda d: (False, "automation_denied:System Events"),
                     open_display_settings=lambda: (opened.append(1),
                                                    (True, "opened_display_settings"))[1]):
            r, _ = tf.turn("make the screen brighter", intent="control", lang=lang,
                           extra={"control_action": "brighter"})
        check(f"{lang}: brightness that did not change: never 'Alright', Displays opened",
              r["did"] == "bright_failed" and opened
              and (r.get("say") or "").strip(" .") != router._sp(lang, "ok").strip(" .")
              and "undo" not in r, (r["did"], r.get("say")))
    fresh()
    with patched(mac, brightness=lambda d: (False, "no_displayservices; keys_failed"),
                 open_display_settings=lambda: (False, "open_failed")):
        r, _ = tf.turn("make the screen darker", intent="control",
                       extra={"control_action": "darker"})
    check("Displays would not open either: where to change it, in words",
          r["did"] == "bright_failed" and "System Settings" in (r.get("say") or ""),
          (r["did"], r.get("say")))
    fresh()
    with patched(mac, close_front_window=lambda: (False, "automation_not_asked:System Events")):
        r, _ = tf.turn("close this", intent="control", extra={"control_action": "close_this"})
    check("close with nothing pressed: 'Press Command-W', never 'Alright'",
          r["did"] == "close_failed" and "Command-W" in (r.get("say") or ""),
          (r["did"], r.get("say")))
    fresh()
    r, _ = tf.turn("make the screen darker", intent="control",
                   extra={"control_action": "darker"})
    check("brightness that worked still says Alright, with its undo",
          r["did"] == "darker" and tm.BRIGHT, (r["did"], tm.BRIGHT))

    # The 2026-10-07 shoot: "turn on Night Shift" (and its how-to) read as darker and
    # the screen was dimmed. MicMic cannot switch Night Shift: it says so, opens
    # Displays, and the brightness is never touched, whatever Jev's intent was.
    for said, intent, extra, feature in (
            ("turn on Night Shift", "control", {"control_action": "darker"}, "night_shift"),
            ("switch night shift off", "stop", {}, "night_shift"),
            ("Night Shift on please", "control", {"control_action": "brighter"}, "night_shift"),
            ("turn on True Tone", "control", {"control_action": "brighter"}, "true_tone"),
            ("open night shift", "open_app", {}, "night_shift")):
        for lang in LANGS:
            fresh()
            opened.clear()
            bright: list = []
            with patched(mac, brightness=lambda d: (bright.append(d), (True, "level:40"))[1],
                         open_display_settings=lambda: (opened.append(1),
                                                        (True, "opened_display_settings"))[1]):
                r, _ = tf.turn(said, intent=intent, lang=lang, extra=extra)
            check(f"{lang}: {said!r} ({intent}): never the brightness, said it can't, "
                  f"Displays opened",
                  r["did"] == "cannot_control" and not bright and not tm.BRIGHT and opened
                  and det(r).get("asked_for") == feature
                  and "—" not in (r.get("say") or "")
                  and (r.get("say") or "").strip(" .") != router._sp(lang, "ok").strip(" ."),
                  (r["did"], r.get("say"), bright))
    fresh()
    bright = []
    with patched(mac, brightness=lambda d: (bright.append(d), (True, "level:40"))[1],
                 open_display_settings=lambda: (False, "open_failed")):
        r, _ = tf.turn("turn on Night Shift", intent="control",
                       extra={"control_action": "darker"})
    check("Displays would not open: where Night Shift is, in words",
          r["did"] == "cannot_control" and "System Settings" in (r.get("say") or "")
          and "Night Shift" in (r.get("say") or "") and not bright,
          (r["did"], r.get("say"), bright))
    fresh()
    bright = []
    with patched(mac, brightness=lambda d: (bright.append(d), (True, "level:40"))[1],
                 open_display_settings=lambda: (opened.append(1), (True, "x"))[1]):
        r, _ = tf.turn("turn on dark mode", intent="control",
                       extra={"control_action": "darker"})
    check("dark mode is not the brightness either: where it is, in words",
          r["did"] == "cannot_control" and "Appearance" in (r.get("say") or "")
          and not bright, (r["did"], r.get("say"), bright))
    fresh()
    r, _ = tf.turn("I'm working the night shift tonight", intent="chitchat")
    check("a night shift at work is small talk, not a display setting",
          r["did"] != "cannot_control", (r["did"], r.get("say")))


# ================================================================== P0-10, P0-13, P2-3
def t_reminders_and_notes():
    print("\nt_reminders_and_notes (P0-10, P2-3)")
    real_add = mac.add_reminder
    mac.add_reminder = tm.REAL_ADD_REMINDER
    try:
        with env(MICMIC_FAKE_AUTOMATION="Reminders=denied,*=granted"):
            fresh()
            r, _ = tf.turn("remind me tomorrow to call the bank", intent="timer",
                           extra={"time_request": "reminder", "time_day": "tomorrow",
                                  "time_day_exists": 0.95, "time_about": "to call the bank",
                                  "time_about_exists": 0.95})
            tmrw = dt.date.today() + dt.timedelta(days=1)
            due = [t for t in mac.pending_timers()]
            nine = dt.datetime.combine(tmrw, dt.time(9, 0))
            check("Reminders denied, a day: MicMic's own spoken reminder at 9:00 that day",
                  r["did"] == "timer_set" and len(due) == 1
                  and abs(mac.timer_row(due[0]["id"])["at"] - nine.timestamp()) < 2
                  if due else False, (r["did"], r.get("say"), due))
            say = r.get("say") or ""
            check("said honestly: Reminders not reachable, and when MicMic will say it",
                  "Reminders" in say and "9:00" in say and "added a reminder" not in say, say)
            check("the words are kept in MicMic's notes file too",
                  any("call the bank" in n for n in mac.read_local_notes(5)),
                  mac.read_local_notes(5))
            check("'kept_locally' is never kept as a reminder's id",
                  (router.MEM.last_action or {}).get("kind") != "reminder",
                  router.MEM.last_action)

            fresh()
            r, _ = tf.turn("add a reminder to buy milk", intent="note",
                           extra={"time_request": "reminder", "time_about": "buy milk",
                                  "time_about_exists": 0.95})
            check("Reminders denied, no day: kept in MicMic, said so, never 'I added'",
                  r["did"] == "noted" and "MicMic" in (r.get("say") or "")
                  and "added" not in (r.get("say") or "").lower(), (r["did"], r.get("say")))
        for lang in LANGS:
            fresh()
            now = dt.datetime(2026, 10, 3, 12, 0)
            out = router._reminder_kept("call the bank", now.date(), "to call the bank", lang,
                                        now, lambda d, s, **kw: {"did": d, "say": s, **kw}, {})
            check(f"{lang}: 9:00 already gone that day: kept in MicMic, said so",
                  out["did"] == "noted" and out["say"] == router._fb("reminder_kept_note", lang),
                  out)
    finally:
        mac.add_reminder = real_add

    fresh()
    with patched(mac, make_note=lambda t, title="": (True, "kept_locally (automation_denied:Notes)")):
        r, _ = tf.turn("write down buy eggs", intent="note", extra={"span": "buy eggs"})
    check("Notes denied: 'I kept it for you in MicMic'", r["did"] == "noted"
          and "kept it for you in MicMic" in (r.get("say") or ""), (r["did"], r.get("say")))
    fresh()
    with patched(mac, read_local_notes=lambda limit=5: []):
        r, _ = tf.turn("what did i write down", intent="note",
                       extra={"asking_for_notes": 0.95})
    check("'what did I write down' with nothing in MicMic says where notes went (P2-3)",
          r["did"] == "no_notes" and "Notes app" in (r.get("say") or ""),
          (r["did"], r.get("say")))


# ================================================================== P1-3 calendar
def t_calendar():
    print("\nt_calendar (P1-3)")
    ev = {"title": "Lunch with Dana", "date": (dt.date.today() + dt.timedelta(days=2)).isoformat(),
          "start": "13:00", "end": "", "location": ""}
    with env(MICMIC_FAKE_AUTOMATION="Calendar=denied,*=granted"):
        fresh()
        router.AWAITING = {"at": time.time(), "need": "confirm_calendar",
                           "question": "shall I add this event to your calendar",
                           "lang": "english", "event": dict(ev), "body": None, "contact": None}
        r, _ = tf.yes()
        ics = [c for c in launched(["open", "-a", "Calendar"])]
        check("Calendar denied: Calendar's own sheet opened on an .ics",
              r["did"] == "calendar_ready" and ics and str(ics[-1][-1]).endswith(".ics"),
              (r["did"], tm.LAUNCHED))
        check("the line: 'Calendar is open with the event. Click OK to add it.'",
              r.get("say") == "Calendar is open with the event. Click OK to add it.",
              r.get("say"))
        check("never 'I have added', and nothing remembered to move later",
              "added" not in (r.get("say") or "") and not tf.CAL
              and (router.MEM.last_action or {}).get("kind") != "event",
              (r.get("say"), tf.CAL, router.MEM.last_action))
        fresh()
        router.AWAITING = {"at": time.time(), "need": "confirm_calendar",
                           "question": "shall I add this event to your calendar",
                           "lang": "english", "event": dict(ev), "body": None, "contact": None}
        with patched(mac, add_event_ics=lambda *a, **k: (False, "ics_failed open")):
            r, _ = tf.yes()
        check("the sheet would not open: the failure line with the Automation pane",
              r["did"] == "calendar_failed" and pane(r) == "automation", (r["did"], det(r)))


# ================================================================== P1-2 Messages, WhatsApp
def _fire(to: str, channel: str = "imessage", text: str = "running late") -> str:
    router.PENDING = {"to": to, "text": text, "lang": "english", "channel": channel,
                      "at": time.time(), "id": "t1"}
    router._fire_pending()
    return tm.SAID[-1][1] if tm.SAID else ""


def t_messages():
    print("\nt_messages (P1-2)")
    denied = lambda n, t, attachment=None: (False, "automation_denied:Messages")  # noqa: E731
    fresh()
    with patched(mac, send_message=denied):
        said = _fire("Dana Cohen")
    check("Messages denied, a number: 'Messages is open on Dana Cohen. Paste ...'",
          said == "Messages is open on Dana Cohen. Paste with Command-V and press Return.", said)
    check("never 'Tell me again and I will try once more', never 'Sent'",
          "try once more" not in said and not said.startswith("Sent"), said)
    check("history: left ready, not sent", router._HISTORY[-1]["did"] == "left_ready",
          router._HISTORY[-1])
    fresh()
    with patched(mac, send_message=denied):
        said = _fire("Nora Bloom")
    check("no number: 'Start a message to Nora Bloom, paste ...'",
          said.startswith("Messages is open. Start a message to Nora Bloom"), said)
    fresh()
    got = []
    with patched(mac, send_message=denied,
                 message_ready=lambda w, t="", attachment=None: (got.append((w, t)),
                                                                  (False, "ready_open"))[1]):
        said = _fire("Dana Cohen")
    check("the clipboard refused: she is told to type it",
          "Type your message there" in said and got and got[0][0] == "+15550000003",
          (said, got))
    fresh()
    with patched(mac, whatsapp=lambda n, t: (tm.WA.append((n, t)), (False, "ready"))[1]):
        said = _fire("Dana Cohen", channel="whatsapp")
    check("WhatsApp on a number left ready: 'Press Return to send it' (L3 handoff 6)",
          said == "Your message to Dana Cohen is ready in WhatsApp. Press Return to send it.",
          said)
    # She said Not now to MicMic's Messages line, then asks for a text message again:
    # the line may show once more (L4 handoff C).
    fresh()
    consent.remember("Messages", False)
    tf.turn("send dana a text message saying hi", intent="message",
            extra={"contact": "Dana Cohen", "contact_is_named": 0.95, "same_person": 0.95,
                   "channel": "imessage", "message_has_content": 0.9})
    check("naming Messages again after Not now forgets that answer",
          consent.answer("Messages") is None, consent.answer("Messages"))
    fresh()
    consent.remember("Messages", False)
    tf.turn("send dana hi", intent="message",
            extra={"contact": "Dana Cohen", "contact_is_named": 0.95, "same_person": 0.95,
                   "message_has_content": 0.9})
    check("an ordinary send does not undo her Not now", consent.answer("Messages") is False,
          consent.answer("Messages"))
    router._cancel_pending()


# ================================================================== P0-7 read my messages
def t_read_messages():
    print("\nt_read_messages (P0-7)")
    for access, did, needle in (("denied", "messages_unreadable", "Full Disk Access"),
                                ("not_asked", "cannot_do", "have not looked"),
                                ("absent", "cannot_do", "WhatsApp is not on this Mac")):
        fresh()
        with patched(book, unread_summary=lambda limit=8: [],
                     messages_access=lambda a=access: a):
            r, _ = tf.turn("read my messages", intent="read_msgs")
        check(f"{access}: never 'No new messages'",
              r["did"] == did and "No new messages" not in (r.get("say") or "")
              and needle in (r.get("say") or ""), (r["did"], r.get("say")))
        if access == "denied":
            check("denied: WhatsApp opened for her, Full Disk Access pane for the button",
                  "WhatsApp" in tm.APPS and pane(r) == "full_disk", (tm.APPS, det(r)))
            check("denied: 'Want me to open it?' is a question left open",
                  r.get("asked_back") and (router.AWAITING or {}).get("need") == "open_pane",
                  router.AWAITING)
            r2, _ = tf.yes()
            check("her yes opens the Full Disk Access pane",
                  r2["did"] == "opened_settings" and det(r2).get("pane") == "full_disk"
                  and det(r2).get("opened") is True, (r2["did"], det(r2)))
            with patched(book, unread_summary=lambda limit=8: [],
                         messages_access=lambda: "denied"):
                tf.turn("read my messages", intent="read_msgs")
            r3, _ = tf.no()
            check("her no leaves it, and is not asked again",
                  r3["did"] == "pane_declined" and router.AWAITING is None, (r3["did"],
                                                                             router.AWAITING))
    fresh()
    with patched(book, unread_summary=lambda limit=8: [], messages_access=lambda: "ok"):
        r, _ = tf.turn("read my messages", intent="read_msgs")
    check("readable and empty: 'No new messages.' is the truth then",
          r["did"] == "no_messages", (r["did"], r.get("say")))
    # The access read itself never looks inside before MicMic may.
    with patched(book, may_touch=lambda: False):
        check("messages_access: not allowed to look yet is 'not_asked'",
              book.messages_access() == "not_asked")


class no_read_yet:
    """A Mac where no read of her book has worked yet (no contacts_ok), with the real
    messages_access: WhatsApp's app and folder are fakes in the test's own state dir
    (an app, and a folder without the message store, so macOS's refusal reads as
    "denied"). `explain` is MicMic's line (the listener's confirm_contacts)."""

    def __init__(self, explain=None, tried=True):
        root = Path(os.environ["MICMIC_STATE_DIR"]) / "fake-whatsapp"
        (root / "WhatsApp.app").mkdir(parents=True, exist_ok=True)
        (root / "group").mkdir(parents=True, exist_ok=True)
        self.kw = dict(read_before=lambda source="": False,
                       WA_APPS=(root / "WhatsApp.app",), WA_DIR=root / "group")
        if not tried:
            self.kw["never_tried"] = lambda: True
        self.explain = explain
        self.p = patched(book, **self.kw)

    def __enter__(self):
        self.p.__enter__()
        book._first_ok[0] = None
        book._permit_until[0] = 0.0
        book.set_explainer(self.explain)
        return self

    def __exit__(self, *exc):
        book.set_explainer(None)
        book._first_ok[0] = None
        book._permit_until[0] = 0.0
        return self.p.__exit__(*exc)


def t_read_messages_first_line():
    """The reviewer's case: "קראי לי את ההודעות" on a Mac where no contacts read had
    worked heard "since you said not now" with MicMic's line never shown, forever."""
    print("\nt_read_messages_first_line (P0-7: the line before the read, in every language)")
    shown: list = []

    def line(answer):
        def fn():
            shown.append(answer)
            return answer
        return fn

    for lang, words in (("hebrew", "קראי לי את ההודעות"), ("arabic", "اقرئي لي الرسائل")):
        fresh()
        shown.clear()
        with no_read_yet(explain=line(False)):
            check(f"{lang}: nothing may look yet", not book.may_touch())
            r, _ = tf.turn(words, intent="read_msgs", lang=lang)
            check(f"{lang}: MicMic's line was shown before anything was read",
                  shown == [False], shown)
            want = router.degender(router._FALLBACK_SAY["msgs_not_asked"][lang], "feminine")
            check(f"{lang}: her Not now, said as hers, nothing read, never 'no messages'",
                  r["did"] == "cannot_do" and det(r).get("declined") is True
                  and r.get("say") == want
                  and det(r).get("messages") == "not_asked" and not book.may_touch(),
                  (r["did"], r.get("say"), det(r)))
            # She asks again: the line once more (never the same reply forever), and
            # her Continue lets the read go on to the honest Full Disk Access ask.
            book.set_explainer(line(True))
            r2, _ = tf.turn(words, intent="read_msgs", lang=lang)
            check(f"{lang}: asked again, the line shows again", shown == [False, True], shown)
            check(f"{lang}: after her Continue, the real access read says why (denied)",
                  r2["did"] == "messages_unreadable" and det(r2).get("messages") == "denied"
                  and pane(r2) == "full_disk" and "לא עכשיו" not in (r2.get("say") or ""),
                  (r2["did"], r2.get("say"), det(r2)))
        router.AWAITING = None

    # No line could be shown (a test, a separate-process source build): the read goes
    # on as her request asked, and the reply never claims a Not now.
    fresh()
    with no_read_yet(explain=None):
        r, _ = tf.turn("קראי לי את ההודעות", intent="read_msgs", lang="hebrew")
    check("hebrew, no line to show: no 'not now' claim, the access read decides",
          r["did"] == "messages_unreadable" and det(r).get("messages") == "denied"
          and "לא עכשיו" not in (r.get("say") or ""), (r["did"], r.get("say"), det(r)))
    router.AWAITING = None

    # MicMic may still not look, and she never said Not now: the neutral line.
    for lang in LANGS:
        fresh()
        with no_read_yet(), patched(book, may_read_first=lambda: False):
            r, _ = tf.turn({"english": "read my messages", "hebrew": "קראי לי את ההודעות",
                            "arabic": "اقرئي لي الرسائل",
                            "russian": "прочитай мои сообщения"}[lang],
                           intent="read_msgs", lang=lang)
        neutral = router.degender(router._FALLBACK_SAY["msgs_not_looked"][lang], "feminine")
        check(f"{lang}: never said Not now, never told she did",
              r["did"] == "cannot_do" and det(r).get("declined") is False
              and r.get("say") == neutral, (r["did"], r.get("say"), det(r)))

    # A Mac where no read was ever tried: her Continue starts the just-in-time read
    # and the turn waits for macOS's answer, as for a request about a person.
    fresh()
    waited: list = []
    with no_read_yet(explain=line(True), tried=False), \
            patched(book, first_read=lambda wait=0: waited.append(wait) or []):
        tf.turn("اقرئي لي الرسائل", intent="read_msgs", lang="arabic")
    check("never tried: the first read waits for macOS's answer inside the turn",
          len(waited) == 1 and waited[0] >= 3.0, waited)
    router.AWAITING = None

    # A read has worked before: no line, nothing extra.
    fresh()
    shown.clear()
    book.set_explainer(line(False))
    try:
        with patched(book, read_before=lambda source="": True,
                     unread_summary=lambda limit=8: [], messages_access=lambda: "ok"):
            r, _ = tf.turn("קראי לי את ההודעות", intent="read_msgs", lang="hebrew")
    finally:
        book.set_explainer(None)
        book._first_ok[0] = None
    check("a read has worked before: no line, 'no messages' is the truth",
          not shown and r["did"] == "no_messages", (shown, r["did"]))


# ================================================================== P0-11 screenshots
def t_screenshots():
    print("\nt_screenshots (P0-11)")
    no_grant = lambda pid=None: {"ok": False, "why": "screen_recording"}  # noqa: E731
    asked: list = []

    def asker(answer):
        def fn(first_time):
            asked.append(first_time)
            return answer
        return fn

    shot_u = {"screen_task": "screenshot"}
    # Never asked: MicMic's line and macOS's request, once; the restart said; tool open.
    with env(MICMIC_FAKE_SCREEN_RECORDING="not_asked"), patched(screen, take_screenshot=no_grant):
        fresh()
        asked.clear()
        consent.register_screen(asker(True))
        r, _ = tf.turn("take a screenshot", intent="screen", extra=shot_u)
        say = r.get("say") or ""
        check("never asked: MicMic's line and the one ask (T18)", asked == [True], asked)
        check("the ask is noted, so it is never a 'first time' again",
              screen.screen_recording_asked(), screen.screen_recording_asked())
        check("the Screenshot app is open, and 'restart' is said; never 'Saved'",
              r["did"] == "screenshot_no_permission" and "Screenshot tool is open" in say
              and "restart" in say and "Saved" not in say
              and launched(["open", "-a", "Screenshot"]), (say, tm.LAUNCHED))
        check("the Screen Recording pane for the page's button",
              pane(r) == "screen_recording", det(r))
        # Not now: no nag, no pane.
        fresh()
        asked.clear()
        consent.register_screen(asker(False))
        r, _ = tf.turn("take a screenshot", intent="screen", extra=shot_u)
        check("Not now: the tool, and no 'turn MicMic on', no pane (never nag)",
              len(asked) == 1 and "turn MicMic on" not in (r.get("say") or "")
              and pane(r) is None, (asked, r.get("say"), det(r)))
        r, _ = tf.turn("take a screenshot", intent="screen", extra=shot_u)
        check("asked again plainly after Not now: no second line this run",
              len(asked) == 1 and r["did"] == "screenshot_no_permission", asked)
        r, _ = tf.turn("take the screenshot yourself", intent="screen", extra=shot_u)
        check("'take it yourself' after Not now: MicMic's line once more (L4 handoff C)",
              len(asked) == 2, asked)
    with env(MICMIC_FAKE_SCREEN_RECORDING="denied"), patched(screen, take_screenshot=no_grant):
        fresh()
        asked.clear()
        consent.register_screen(asker(True))
        r, _ = tf.turn("take a screenshot", intent="screen", extra=shot_u)
        check("denied: no second macOS ask; turn it on and reopen, with the pane",
              not asked and "Screen Recording" in (r.get("say") or "")
              and "reopen" in (r.get("say") or "") and pane(r) == "screen_recording",
              (asked, r.get("say"), det(r)))
        fresh()
        with patched(screen, open_screenshot_app=lambda: (False, "open_failed")):
            r, _ = tf.turn("take a screenshot", intent="screen", extra=shot_u)
        check("the tool would not open: macOS's own keys, never a claim",
              "Shift-Command-3" in (r.get("say") or "") and "Saved" not in (r.get("say") or ""),
              r.get("say"))

        # "send a screenshot to dana" (T19): her keys, the clipboard, then the chat.
        fresh()
        waited: list = []

        def wait(since, timeout=0, poll=0.3, cancel=None):
            waited.append((since, cancel() if cancel else None))
            return True, "on_clipboard"

        with patched(screen, clipboard_count=lambda: 41, wait_for_clipboard_image=wait), \
                tm.gates_on():
            r, _ = tf.turn("send a screenshot to dana", intent="message",
                           extra={"contact": "Dana Cohen", "contact_is_named": 0.95,
                                  "same_person": 0.95, "message_has_content": 0.0})
            say = r.get("say") or ""
            check("send without the grant: the keys to press, nothing sent (T19)",
                  r["did"] == "screen_clip_wait" and "Control-Shift-Command-4" in say
                  and not tm.SENT and router.PENDING is None, (r["did"], say, tm.SENT))
            check("the clipboard counter was read before she heard the keys",
                  wait_for(lambda: bool(waited)) and waited[0][0] == 41, waited)
            got = wait_for(lambda: any("Paste with Command-V" in s for _, s in tm.SAID))
            check("then the chat opened on Dana, and the paste line, never 'Sent'",
                  got and not any(s.startswith("Sent") for _, s in tm.SAID)
                  and not tm.SENT, tm.SAID)
        fresh()
        with patched(screen, clipboard_count=lambda: 41,
                     wait_for_clipboard_image=lambda *a, **k: (False, "not_an_image")), \
                tm.gates_on():
            r, _ = tf.turn("send a screenshot to dana", intent="message",
                           extra={"contact": "Dana Cohen", "contact_is_named": 0.95,
                                  "same_person": 0.95, "message_has_content": 0.0})
            got = wait_for(lambda: any("not a picture" in s for _, s in tm.SAID))
            check("she copied something else: said so, no chat opened",
                  got and not tm.SENT, tm.SAID)


def t_screenshot_who_first():
    """Without Screen Recording, "send a screenshot to dina" opened Messages on Dana
    Cohen and told her to press Return: the clipboard path skipped the "did you mean"
    and "which one" checks the granted path runs."""
    print("\nt_screenshot_who_first (P0-11: the person is settled before the clipboard)")
    no_grant = lambda pid=None: {"ok": False, "why": "screen_recording"}  # noqa: E731
    waited: list = []

    def wait(since, timeout=0, poll=0.3, cancel=None):
        waited.append(since)
        return True, "on_clipboard"

    def opened_on(name: str) -> bool:
        return any(name in s for _, s in tm.SAID if "Paste with Command-V" in s)

    shot = {"screen_task": "screenshot"}
    with env(MICMIC_FAKE_SCREEN_RECORDING="denied"), patched(screen, take_screenshot=no_grant), \
            patched(screen, clipboard_count=lambda: 41, wait_for_clipboard_image=wait), \
            tm.gates_on():
        # "dina" only sounds like Dana Cohen: asked first, nothing opened or watched.
        fresh()
        waited.clear()
        r, _ = tf.turn("send a screenshot to dina", intent="message",
                       extra={"contact": "Dana Cohen", "contact_is_named": 0.95,
                              "same_person": 0.95, "message_has_content": 0.0, **shot})
        check("dina: 'Did you mean Dana Cohen?' first, as with the grant",
              r["did"] == "need_who" and "Did you mean Dana Cohen" in (r.get("say") or ""),
              (r["did"], r.get("say")))
        time.sleep(0.2)
        check("dina: no clipboard watch, no chat opened, no keys to press yet",
              not waited and not tm.SAID and not tm.SENT
              and "Control-Shift-Command-4" not in (r.get("say") or ""),
              (waited, tm.SAID, r.get("say")))
        check("dina: the question remembers the screenshot it is for",
              (router.AWAITING or {}).get("shot_clip") == "denied", router.AWAITING)
        r2, _ = tf.turn("yes", extra={"is_answer": 0.95, "yes_no": "yes"})
        check("her yes: now the keys to press, for Dana Cohen",
              r2["did"] == "screen_clip_wait" and det(r2).get("to") == "Dana Cohen"
              and "Control-Shift-Command-4" in (r2.get("say") or "")
              and pane(r2) == "screen_recording", (r2["did"], r2.get("say"), det(r2)))
        got = wait_for(lambda: opened_on("Dana"))
        check("then the chat on Dana Cohen to paste into, never 'Sent'",
              got and waited == [41] and not tm.SENT
              and not any(s.startswith("Sent") for _, s in tm.SAID), (tm.SAID, waited))

        # Her no: nothing opened, asked who instead.
        fresh()
        waited.clear()
        tf.turn("send a screenshot to dina", intent="message",
                extra={"contact": "Dana Cohen", "contact_is_named": 0.95,
                       "same_person": 0.95, "message_has_content": 0.0, **shot})
        r3, _ = tf.turn("no", extra={"is_answer": 0.95, "yes_no": "no"})
        time.sleep(0.2)
        check("her no: nobody's chat opened, nothing watched",
              r3["did"] != "screen_clip_wait" and not waited and not tm.SAID,
              (r3["did"], waited, tm.SAID))
        router.AWAITING = None

        # Two people with the first name she said: "which one?", then the one she names.
        row = {"name": "Dana Levi", "first": "Dana", "phone": "+15550000013",
               "waid": "15550000013@s.whatsapp.net", "source": "fixture"}
        row["keys"] = book.keys_for(row["name"]) | book.keys_for(row["first"])
        BOOK.append(row)
        try:
            fresh()
            waited.clear()
            r, _ = tf.turn("send a screenshot to dana", intent="message",
                           extra={"contact": "Dana Cohen", "contact_is_named": 0.95,
                                  "same_person": 0.95, "message_has_content": 0.0, **shot})
            time.sleep(0.2)
            check("two Danas: 'which one?' first, no chat opened on either",
                  r["did"] == "need_which" and not waited and not tm.SAID
                  and set(det(r).get("between") or []) == {"Dana Cohen", "Dana Levi"},
                  (r["did"], r.get("say"), det(r), tm.SAID))
            r2, _ = tf.turn("Dana Levi", extra={"is_answer": 0.95, "contact": "Dana Levi"})
            check("she names Dana Levi: the keys, for Dana Levi",
                  r2["did"] == "screen_clip_wait" and det(r2).get("to") == "Dana Levi",
                  (r2["did"], r2.get("say"), det(r2)))
            check("and the chat opened is Dana Levi's",
                  wait_for(lambda: opened_on("Dana Levi")) and not tm.SENT, tm.SAID)
        finally:
            BOOK.remove(row)
        router.AWAITING = None

        # Named plainly: straight to the keys, as before (no extra question).
        fresh()
        r, _ = tf.turn("send a screenshot to dana", intent="message",
                       extra={"contact": "Dana Cohen", "contact_is_named": 0.95,
                              "same_person": 0.95, "message_has_content": 0.0, **shot})
        check("dana, one Dana: the keys at once",
              r["did"] == "screen_clip_wait" and det(r).get("to") == "Dana Cohen",
              (r["did"], r.get("say")))
        wait_for(lambda: opened_on("Dana"))
        router.AWAITING = None


# ================================================================== P1-5, P1-6 the screen
def t_accessibility():
    print("\nt_accessibility (P1-5, P1-6)")
    blocked_ctx = {"permissions": {"accessibility": False, "screen_recording": False},
                   "frontmost": {"app": "Notes", "pid": 1, "bundle_id": "com.apple.Notes",
                                 "window": ""},
                   "selected": "", "focused": {}, "visible": {"text": ""}, "page": None,
                   "has_image": False}
    untrusted = {"trusted": lambda: False}

    # In-app control without Accessibility: the reason and the button (T20).
    fresh()
    with patched(apps_mod, available=lambda: (False, "System Settings > ..."),
                 _ax=lambda: untrusted, running_apps=lambda: ["Calculator"]):
        r, _ = tf.turn("press 5 times 3 in calculator", intent="unclear",
                       extra={"inside_an_app": 0.95})
    check("in-app control: the reason (Accessibility) and the pane (T20)",
          r["did"] == "cannot_reach_app" and "Accessibility" in (r.get("say") or "")
          and pane(r) == "accessibility", (r["did"], r.get("say"), det(r)))

    # The screen, Notes in front, no Accessibility: blocked, with the button and the
    # clipboard offer; she copies, asks again, and it is read from the clipboard.
    fresh()
    count = {"n": 10}
    with patched(router, _screen_context=lambda max_chars=6000: dict(blocked_ctx),
                 _clipboard_text=lambda: "Flight XY123 leaves at 10:40 from gate B7"), \
            patched(screen, clipboard_count=lambda: count["n"]):
        r, _ = tf.turn("what is on my screen", intent="screen",
                       extra={"screen_task": "describe"})
        check("blocked: the Accessibility line, the pane, and 'copy it and ask again'",
              r["did"] == "screen_unavailable" and pane(r) == "accessibility"
              and "Command-C" in (r.get("say") or ""), (r["did"], r.get("say"), det(r)))
        r, _ = tf.turn("what is on my screen", intent="screen",
                       extra={"screen_task": "describe"})
        check("nothing copied since: still blocked", r["did"] == "screen_unavailable",
              r["did"])
        count["n"] = 11
        n0 = len(tf.LLM.prompts)
        r, _ = tf.turn("what is on my screen", intent="screen",
                       extra={"screen_task": "describe"})
        check("she copied the text: the answer comes from the clipboard (P1-6)",
              r["did"] == "described_screen" and len(tf.LLM.prompts) == n0 + 1
              and "10:40" in tf.LLM.prompts[-1][1], (r["did"], tf.LLM.prompts[-1:]))
    # The clipboard would put up macOS's paste alert: no offer, no read.
    fresh()
    with env(MICMIC_FAKE_PASTE_ACCESS="ask"), \
            patched(router, _screen_context=lambda max_chars=6000: dict(blocked_ctx)):
        r, _ = tf.turn("what is on my screen", intent="screen",
                       extra={"screen_task": "describe"})
    check("paste would ask: no 'copy it' offer (no unannounced macOS alert)",
          "Command-C" not in (r.get("say") or "") and pane(r) == "accessibility",
          r.get("say"))

    # Chrome in front, no Accessibility: Chrome's own page, fetched by MicMic (T21).
    fresh()
    chrome_ctx = {**blocked_ctx, "frontmost": {"app": "Google Chrome", "pid": 2,
                                               "bundle_id": "com.google.Chrome", "window": ""}}
    told: list = []

    def tell(app, script, timeout=4.0, launch=True, ask=True):
        told.append((app, launch))
        return True, "https://example.com/trip\nMy trip"

    fetched: list = []
    with patched(router, _screen_context=lambda max_chars=6000: dict(chrome_ctx),
                 _page_text=lambda url, max_chars=6000: (fetched.append(url),
                                                         "Your train leaves at 08:15")[1]), \
            patched(mac, _tell=tell):
        n0 = len(tf.LLM.prompts)
        r, _ = tf.turn("what is on my screen", intent="screen",
                       extra={"screen_task": "describe"})
    check("Chrome: the page from Chrome's own AppleScript, never launching Chrome",
          told == [("Google Chrome", False)] and fetched == ["https://example.com/trip"],
          (told, fetched))
    check("Chrome: answered from that page, not 'I cannot see your screen' (T21)",
          r["did"] == "described_screen" and "08:15" in tf.LLM.prompts[-1][1]
          and "example.com" in tf.LLM.prompts[-1][1], (r["did"], tf.LLM.prompts[n0:]))

    # The page fetcher itself: http(s) only, never this Mac, tags stripped.
    check("_page_text refuses a page on this Mac", router._page_text(
        "http://127.0.0.1:8799/") == "" and router._page_text("file:///etc/hosts") == "")

    class _Resp:
        headers = None

        def __init__(self, body):
            import email.message
            self.body = body
            self.headers = email.message.Message()
            self.headers["Content-Type"] = "text/html; charset=utf-8"

        def read(self, n=-1):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import urllib.request as _ur
    html = (b"<html><head><title>x</title><script>var a=1</script></head><body>"
            b"<h1>Trip</h1><p>Leaves &amp; arrives at 08:15</p><style>p{}</style></body></html>")
    with patched(_ur, urlopen=lambda req, timeout=0: _Resp(html)):
        txt = router._page_text("https://example.com/trip")
    check("_page_text keeps the words, drops scripts and styles",
          "Trip" in txt and "Leaves & arrives at 08:15" in txt and "var a" not in txt
          and "p{}" not in txt, txt)

    # The 2026-10-07 shoot, 6 of 6: "what's this article about?" over a Wikipedia
    # article in Chrome, Accessibility ON, was summarized as "you have Chrome open
    # with ...": Chrome showed only its tabs and toolbar (~230 characters). With no
    # page text in the read, the page Chrome names is fetched and summarized.
    toolbar = ("Olive - Wikipedia | New Tab | Back | Forward | Reload | "
               "en.wikipedia.org/wiki/Olive | Bookmark this tab | Extensions | "
               "Google Chrome | Side panel | Customize and control Google Chrome")
    article = ("The olive, botanical name Olea europaea, is a species of small tree in "
               "the family Oleaceae, found traditionally in the Mediterranean Basin. ") * 12
    chrome_ax = {"permissions": {"accessibility": True, "screen_recording": False},
                 "frontmost": {"app": "Google Chrome", "pid": 2,
                               "bundle_id": "com.google.Chrome", "window": "Olive - Wikipedia"},
                 "selected": "", "focused": {}, "page": None, "has_image": False,
                 "visible": {"text": toolbar, "document": "", "truncated": False}}
    for task, said, did in (("summarize", "what's this article about", "summarized_screen"),
                            ("translate", "translate this article to Hebrew",
                             "translated_screen")):
        fresh()
        told.clear()
        fetched.clear()
        with patched(router, _screen_context=lambda max_chars=6000: dict(chrome_ax),
                     _page_text=lambda url, max_chars=6000: (fetched.append(url), article)[1]), \
                patched(mac, _tell=lambda app, script, timeout=4.0, launch=True, ask=True: (
                    told.append((app, launch)),
                    (True, "https://en.wikipedia.org/wiki/Olive\nOlive - Wikipedia"))[1]):
            n0 = len(tf.LLM.prompts)
            r, _ = tf.turn(said, intent="screen", extra={"screen_task": task})
        prompt = tf.LLM.prompts[-1][1] if len(tf.LLM.prompts) > n0 else ""
        check(f"{task}: Chrome's page is fetched when the read holds only its toolbar",
              told == [("Google Chrome", False)]
              and fetched == ["https://en.wikipedia.org/wiki/Olive"], (told, fetched))
        check(f"{task}: the article goes to the model, not the toolbar",
              r["did"] == did and "Olea europaea" in prompt and "Bookmark this tab" not in prompt,
              (r["did"], prompt[:300]))
    # A read that already holds the page, or another app in front: nothing extra.
    for ctx_x, why in (({**chrome_ax, "visible": {"text": toolbar + " " + article,
                                                  "document": article, "truncated": False}},
                        "Chrome with its page already read"),
                       ({**chrome_ax, "frontmost": {"app": "Notes", "pid": 3,
                                                    "bundle_id": "com.apple.Notes",
                                                    "window": ""}}, "Notes in front"),
                       ({**chrome_ax, "selected": "Olives are picked in autumn."},
                        "selected text")):
        fresh()
        told.clear()
        fetched.clear()
        with patched(router, _screen_context=lambda max_chars=6000, c=ctx_x: dict(c),
                     _page_text=lambda url, max_chars=6000: (fetched.append(url), article)[1]), \
                patched(mac, _tell=lambda app, script, timeout=4.0, launch=True, ask=True: (
                    told.append((app, launch)), (True, "https://x.org/a\nA"))[1]):
            r, _ = tf.turn("what's this article about", intent="screen",
                           extra={"screen_task": "summarize"})
        check(f"{why}: no Apple Event to Chrome and no fetch",
              not told and not fetched and r["did"] == "summarized_screen",
              (told, fetched, r["did"]))
    # A Wikipedia article is read through Wikipedia's API: its text, not its menus.
    asked: list = []
    api = json.dumps({"query": {"pages": {"1": {"extract": "The olive is a tree.\n\nIt grows."}}}})

    def wiki_open(req, timeout=0):
        asked.append(getattr(req, "full_url", req))
        return _Resp(api.encode())
    with patched(_ur, urlopen=wiki_open):
        txt = router._page_text("https://en.wikipedia.org/wiki/Olive")
    check("a Wikipedia article: the article's own text, from Wikipedia's API",
          txt == "The olive is a tree.\nIt grows." and asked
          and "api.php" in asked[0] and "titles=Olive" in asked[0], (txt, asked))
    main_html = (b"<html><body><nav>" + b"Menu item " * 300 + b"</nav><main><p>"
                 + b"The real story. " * 200 + b"</p></main></body></html>")
    with patched(_ur, urlopen=lambda req, timeout=0: _Resp(main_html)):
        txt = router._page_text("https://example.com/story")
    check("a page that marks its <main>: that, not its menus",
          txt.startswith("The real story.") and "Menu item" not in txt, txt[:80])

    # Guide mode blocked by Accessibility carries the pane (L5).
    with patched(router, _ax_missing=lambda: True):
        check("a guide that failed for want of Accessibility carries the pane",
              router._guide_fix({"status": "failed"}) == {"fix": {"pane": "accessibility"}})
        check("a guide step that went fine carries none",
              router._guide_fix({"status": "step"}) == {})


# ================================================================== the book's pane
def t_book_pane():
    print("\nt_book_pane (fix on every reply that asks for a permission)")
    for why, want in (("whatsapp", "full_disk"), ("automation", "automation")):
        fresh()
        with patched(book, unreadable=lambda w=why: w,
                     all_contacts=lambda force=False, wait=None: []), \
                patched(router, get_contacts=lambda *a, **k: []):
            r, _ = tf.turn("call tamar", intent="call",
                           extra={"contact": "nobody", "contact_is_named": 0.95})
        check(f"{why}: the book line carries the {want} pane",
              r["did"] == "need_who" and pane(r) == want, (r["did"], r.get("say"), det(r)))


# ================================================================== the lines
def t_lines():
    print("\nt_lines")
    import re
    for key, block in router._FALLBACK_SAY.items():
        check(f"{key}: all four languages", all(block.get(l) for l in LANGS), list(block))
        for lang, text in block.items():
            check(f"{key}/{lang}: no em dash", "—" not in text, text)
            fields = set(re.findall(r"\{(\w+)\}", text))
            check(f"{key}/{lang}: only known fields", fields <= {"who", "at_he", "at_ar"},
                  fields)
            got = router.degender(text, "feminine")
            check(f"{key}/{lang}: the gender marks expand",
                  "|" not in got and not router._GENDERED.search(got), got)
    from savta import people
    block = people.LINES["wa_ready_number"]
    check("wa_ready_number: all four languages", all(block.get(l) for l in LANGS))


def main():
    for t in (t_calls, t_pause, t_stop_video, t_control, t_reminders_and_notes, t_calendar,
              t_messages, t_read_messages, t_read_messages_first_line, t_screenshots,
              t_screenshot_who_first, t_accessibility, t_book_pane,
              t_lines):
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran to the end", False, repr(e))
        router._cancel_pending()
        router.AWAITING = None
        consent._reset_for_tests()
    real_osa = [c for c in tm.LAUNCHED if c and c[0] == "osascript"]
    check("0 osascript escapes", not real_osa and not tm.OSA, (real_osa, tm.OSA))
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAILED: {n}  {d}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
