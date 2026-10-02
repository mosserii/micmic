#!/usr/bin/env python3
"""The follow-up layer, offline: a follow-up changes the last thing MicMic did, in the
same place, instead of starting over (savta/followup.py).

    cd <checkout> && uv run python tests/test_followups.py

Each kind is walked the same way: set it, change it, change it again, then a new
request forgets it. Plus the safety half: never an event MicMic did not create, never
a player that is not playing, and a request with nothing recent or no modifier word is
sent to Jev exactly as before.

Nothing leaves the machine. The stub wall is tests/test_micmic.py, loaded the way
tests/perf/bench.py loads it (its main() never runs). On top of it: osascript is a fake
that plays Calendar (events by uid), Music and Spotify (a player state) and Chrome (tabs
by address, with and without JavaScript from Apple Events); Gemini is a fake that
writes a marked rewrite; Jev is scripted. No screencapture, no real osascript, nothing
reaches 127.0.0.1:8799.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-follow-state-")
for _k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL"):
    os.environ.pop(_k, None)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "perf"))
import bench                                   # noqa: E402

tm = bench.load_wall()
router, mac = tm.router, tm.mac
from savta import followup as fu               # noqa: E402

PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail: str = "") -> bool:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return bool(ok)


# ---------------------------------------------------------------- fake osascript
SCRIPTS: list[str] = []
CAL: dict[str, dict] = {}          # uid -> event, as Calendar would hold it
REMS: dict[str, dict] = {}         # id -> reminder, as Reminders would hold it
PLAYER = {"Music": "playing", "Spotify": "not_running"}
TABS: list[str] = []               # addresses of the tabs open in Chrome
VIDEO = {"paused": False}
JS_OK = {"on": True}               # Chrome's "Allow JavaScript from Apple Events"
PRESSED: list[str] = []            # keys pressed, with the tab they were pressed in


def _date(script: str, var: str) -> tuple[str, str]:
    g = {k: int(re.search(rf"set {k} of {var} to (\d+)", script).group(1))
         for k in ("year", "month", "hours", "minutes")}
    day = int(re.findall(rf"set day of {var} to (\d+)", script)[-1])
    return f"{g['year']:04d}-{g['month']:02d}-{day:02d}", f"{g['hours']:02d}:{g['minutes']:02d}"


def fake_osa(script: str, timeout: float = 4.0):
    SCRIPTS.append(script)
    if 'tell application "Reminders"' in script:
        # Reminders as it would hold them: rows by id, with a due day and maybe a time.
        dated = "set year of d" in script
        date, at = _date(script, "d") if dated else ("", "")
        allday = "allday due date" in script
        if "make new reminder" in script:
            rid = f"x-apple-reminder://REM-{len(REMS) + 1}"
            name = re.search(r'name:"([^"]*)"', script).group(1)
            REMS[rid] = {"name": name, "date": date, "at": "" if allday else at}
            return True, rid
        m = re.search(r'whose id is "([^"]*)"', script)
        if m:
            row = REMS.get(m.group(1))
            if row is None:
                return True, "missing"
            row.update(date=date, at="" if allday else at)
            return True, "ok"
        return True, ""
    if "make new event" in script:
        uid = f"UID-{len(CAL) + 1}"
        date, start = _date(script, "s")
        end = _date(script, "e")[1] if "set year of e" in script else ""
        title = re.search(r'summary:"([^"]*)"', script).group(1)
        CAL[uid] = {"title": title, "date": date, "start": start, "end": end,
                    "all_day": "allday event:true" in script, "calendar": "Home"}
        return True, f"Home\n{uid}"
    if "whose uid is" in script:
        uid = re.search(r'whose uid is "([^"]*)"', script).group(1)
        cal = re.search(r'calendar whose name is "([^"]*)"', script).group(1)
        ev = CAL.get(uid)
        if not ev or ev["calendar"] != cal:
            return True, "missing"
        date, start = _date(script, "s")
        if "set allday event of ev to true" in script:
            ev.update(date=date, start="", end="", all_day=True)
        else:
            ev.update(date=date, start=start, end=_date(script, "e")[1], all_day=False)
        return True, "ok"
    m = re.search(r'tell application "(Music|Spotify)" to return \(player state', script)
    if m:
        return True, PLAYER[m.group(1)]
    m = re.search(r'if application "(Music|Spotify)" is running then\ntell application', script)
    if m:
        return True, ""
    if 'application "Google Chrome"' in script and "contains" in script:
        frag = re.search(r'URL of t contains "([^"]*)"', script).group(1)
        tab = next((t for t in TABS if frag in t), None)
        if tab is None:
            return True, "missing"
        if "execute t javascript" in script and JS_OK["on"]:
            body = re.search(r'execute t javascript "(.*)"\)', script).group(1)
            if "if(v.paused)return 'already';v.pause()" in body:
                if VIDEO["paused"]:
                    return True, "already"
                VIDEO["paused"] = True
            elif "if(!v.paused)return 'already';v.play()" in body:
                if not VIDEO["paused"]:
                    return True, "already"
                VIDEO["paused"] = False
            return True, "done"
        k = re.search(r'System Events" to (keystroke "[^"]*"|key code \d+)( using \w+ down)?',
                      script)
        if not k:
            return True, "unsupported"
        PRESSED.append(f"{tab}:{k.group(1)}{k.group(2) or ''}")
        return True, "keys"
    return True, ""


# ---------------------------------------------------------------- fake Gemini
class FakeLLM:
    """Writes a visible, marked answer so a test can tell which prompt was sent."""
    available = True

    def __init__(self):
        self.calls, self.busy_ms, self.last_ms, self.last_error = 0, 0.0, 0.0, None
        self.prompts: list[tuple[str, str]] = []
        self.event_json = ""

    def generate_content(self, contents, timeout=30.0, system="", generation_config=None):
        self.calls += 1
        prompt = contents[0]["parts"][-1]["text"]
        self.prompts.append((system, prompt))
        if "extract calendar events" in system:
            text = self.event_json
        elif prompt.startswith("Say this again much shorter"):
            text = "[short] " + prompt.split("\n\n", 1)[1][:30]
        elif prompt.startswith("Say this again in"):
            lang = re.match(r"Say this again in (\w+)", prompt).group(1)
            text = f"[{lang}] " + prompt.split("\n\n", 1)[1][:30]
        else:
            text = "A recipe for shakshuka with eggs and tomatoes, ready in twenty minutes."
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    def split_steps(self, *a, **k):
        return None

    def answer(self, *a, **k):
        self.calls += 1
        return "[answer]"

    def chat(self, *a, **k):
        self.calls += 1
        return "[chat]"

    def text(self, *a, **k):
        self.calls += 1
        return "[text]"


# ---------------------------------------------------------------- scripted Jev
QUIET = ("not_applicable", "unspecified", "none", "nobody", "__none__", "any", "today",
         "unrevealed", "unchanged", "other")


class ScriptedJev:
    """Answers every question with its quiet default, except what a test scripts.
    A scripted string answers a choice, a number a noul, a dict is the raw answer."""
    mode = "replay"

    def __init__(self):
        self.calls, self.cost_usd, self.busy_ms = 0, 0.0, 0.0
        self.asked: list[dict] = []
        self.states: list[dict] = []
        self.say: dict = {}

    def ask(self, state, qs):
        self.calls += 1
        self.asked.append(qs)
        self.states.append(state)
        out = {}
        for k, q in qs.items():
            t = q.get("type")
            if t == "choice":
                keys = list(q["criteria"])
                sel = next((c for c in QUIET if c in keys), keys[0])
                out[k] = {"choice": sel, "confidence": 0.9,
                          "probabilities": {c: (0.9 if c == sel else 0.1 / max(1, len(keys) - 1))
                                            for c in keys}}
            elif t == "score":
                out[k] = {"score": 0.0}
            else:
                out[k] = {"noul": 0.95 if k == "is_complete" else 0.0}
        for k, v in self.say.items():
            if k in qs:
                out[k] = (v if isinstance(v, dict)
                          else {"choice": v, "confidence": 0.9, "probabilities": {v: 0.9}}
                          if isinstance(v, str) else {"noul": v})
        return out

    def warmup(self, *a, **k):
        pass


LAST_J: list[ScriptedJev] = []


def turn(utterance, *, intent="chitchat", follow=None, lang=None, extra=None):
    j = ScriptedJev()
    j.say = {"intent": intent}
    if follow:
        j.say[fu.QUESTION] = follow
    if lang:
        j.say["language"] = lang
    j.say.update(extra or {})
    LAST_J[:] = [j]
    return router.handle(j, utterance, speak=False, client="native", activation="push"), j


def yes(utterance="yes"):
    return turn(utterance, extra={"is_answer": 0.95, "agreed": 0.95})


def no(utterance="no"):
    return turn(utterance, extra={"is_answer": 0.95, "agreed": 0.05})


def asked_follow(j: ScriptedJev) -> bool:
    return bool(j.asked) and fu.QUESTION in j.asked[0]


def _offered(j: ScriptedJev, prefix: str) -> bool:
    """The follow-up question of the turn's first call offered a change to `prefix`."""
    crit = ((j.asked[0] if j.asked else {}).get(fu.QUESTION) or {}).get("criteria") or {}
    return any(k.startswith(prefix) for k in crit)


def told(j: ScriptedJev) -> bool:
    return bool(j.states) and "last_thing_micmic_did" in j.states[0]


LLM = FakeLLM()


def fresh():
    tm.reset_state()
    for x in (SCRIPTS, PRESSED, tm.OPENED, tm.LAUNCHED, tm.APPS, tm.SENT, tm.WA,
              tm.CLOSED, tm.VOLUME):
        x.clear()
    CAL.clear()
    REMS.clear()
    TABS.clear()
    PLAYER.update(Music="playing", Spotify="not_running")
    VIDEO["paused"] = False
    JS_OK["on"] = True
    mac._osa = fake_osa
    router.LLM_CLIENT = LLM
    LLM.prompts.clear()
    mac.cancel_timers()
    router.prefs._update(lambda d: d.update(music_app=""))


def _later(days: int) -> dt.date:
    return dt.date.today() + dt.timedelta(days=days)


def _weekday_key(days: int) -> str:
    return f"event on {_later(days).strftime('%A %-d %B')}"


# ================================================================== units
def t_units():
    print("\nt_units")
    for s in ("make it 10 minutes instead", "move it to 6pm", "cancel it", "what was it again?",
              "move it to Friday", "make it an hour", "at 7 instead", "add Dana to it",
              "pause", "skip", "back 10 seconds", "louder", "next episode", "full screen",
              "shorter", "in Hebrew", "send that to Dana", "and at 8?",
              "תעשי את זה עשר דקות במקום", "תזיזי את זה לשש", "תבטלי את זה", "מה זה היה?",
              "תעצרי", "תקצרי", "בעברית", "תשלחי את זה לדנה", "ובשבע?",
              "خليها بعد عشر دقايق", "وقفي", "باختصار أقصر", "перенеси на пятницу", "пауза",
              "короче", "а в семь?"):
        check(f"modifier word found: {s!r}", fu.says_modifier(s))
    for s in ("what's the weather", "play Shakira", "call Dana", "מה השעה",
              "תשימי שיר של זוהר ארגוב", "كم الساعة", "какая погода"):
        check(f"no modifier word: {s!r}", not fu.says_modifier(s))

    now = time.time()
    t = {"kind": "timer", "at": now, "id": "1", "text": "tea", "due": now + 600}
    check("nothing in play: no question even with a modifier word",
          fu.question(fu.in_play(None, None, now), "make it 10 minutes instead") is None)
    check("in play, no modifier word: no question",
          fu.question(fu.in_play(t, None, now), "what's the weather") is None)
    old = {**t, "at": now - fu.FOLLOW_TTL - 1}
    check("a last action older than FOLLOW_TTL is not in play", fu.in_play(old, None, now) == [])
    q = fu.question(fu.in_play(t, {"player": "youtube", "title": "x", "at": now}, now),
                    "make it 10 minutes instead")
    opts = list(q["questions"][fu.QUESTION]["criteria"])
    check("'none' is the first option (a replay that never saw it defaults to none)",
          opts[0] == "none", opts[:2])
    check("a reminder and what is playing are both offered, reminder first",
          "reminder at 18:00" in opts and "pause it" in opts
          and opts.index("reminder at 18:00") < opts.index("pause it"))
    check("under Jev's 255-option limit", len(opts) < 250, len(opts))
    ev = {"kind": "event", "at": now, "uid": "U", "date": _later(1).isoformat(),
          "start": "10:00", "end": "11:00", "title": "Dentist"}
    q = fu.question([ev], "move it to Friday")
    check("event options: days, times, lengths, invite, remove, under the limit",
          all(k in q["options"] for k in ("event starts at 19:00", "event lasts 90 minutes",
                                          "invite someone to the event", "remove the event"))
          and len(q["options"]) < 250, len(q["options"]))
    check("what the event already is is not offered (its day, its start, its length)",
          not any(k in q["options"] for k in (_weekday_key(1), "event starts at 10:00",
                                              "event lasts 60 minutes")))
    # "At 7" split between 07:00 and 19:00 is still clearly a new time: fields are summed.
    u = {"raw": {fu.QUESTION: {"choice": "none", "confidence": 0.35, "probabilities": {
        "none": 0.35, "event starts at 07:00": 0.25, "event starts at 19:00": 0.40}}}}
    ch = fu.read(u, q)
    check("fields are summed: 0.25 + 0.40 beats none 0.35, and 19:00 is the value",
          ch and ch["field"] == "at" and ch["value"] == "19:00" and ch["conf"] == 0.65, ch)
    u["raw"][fu.QUESTION]["probabilities"] = {"none": 0.6, "event starts at 19:00": 0.4}
    check("below the gate: a new request", fu.read(u, q) is None)
    check("event moved an hour later keeps its length",
          fu.event_change(ev, "later", 60) == {"date": ev["date"], "start": "11:00",
                                               "end": "12:00", "all_day": False})
    check("made an hour long from its start", fu.event_change(
        {**ev, "end": "10:30"}, "length", 60)["end"] == "11:00")
    check("the same time again changes nothing", fu.event_change(ev, "at", "10:00") is None)
    check("minutes until 18:00 is positive and under a day",
          0 < fu.minutes_until("18:00") <= 24 * 60)
    check("no em dash in any follow-up line",
          not any("—" in v for block in fu.LINES.values() for v in block.values()))


# ================================================================== the gate
def t_byte_identical():
    """No recent action, or no modifier word: understand() is asked exactly what it was."""
    print("\nt_byte_identical")
    fresh()
    _, j0 = turn("what's the weather", intent="look_up")
    base_q, base_s = sorted(j0.asked[0]), {k: v for k, v in j0.states[0].items()
                                           if k != "right_now"}
    turn("remind me in 5 minutes to take the pills", intent="timer",
         extra={"when_minutes": "5"})
    check("the reminder is the last action", (router.MEM.last_action or {}).get("kind") == "timer")
    _, j1 = turn("what's the weather", intent="look_up")
    check("a recent action but no modifier word: same questions",
          sorted(j1.asked[0]) == base_q and not asked_follow(j1))
    check("and the same state, byte for byte (the clock aside)",
          {k: v for k, v in j1.states[0].items() if k != "right_now"} == base_s)
    fresh()
    _, j2 = turn("make it 10 minutes instead", intent="timer")
    check("a modifier word but nothing recent: no question, nothing told",
          not asked_follow(j2) and not told(j2))


# ================================================================== reminders
def t_timer():
    print("\nt_timer")
    fresh()
    r, _ = turn("remind me in 5 minutes to take the pills", intent="timer",
                extra={"when_minutes": "5"})
    first = router.MEM.last_action["id"]
    check("set: a reminder in 5 minutes", r["did"] == "timer_set" and len(mac.pending_timers()) == 1)

    r, j = turn("make it 10 minutes instead", follow="reminder in 10 minutes from now")
    check("the follow-up question rode in understand(), and what was done was told",
          asked_follow(j) and told(j)
          and j.states[0]["last_thing_micmic_did"]["reminder_about"])
    check("one round trip", j.calls == 1, j.calls)
    pend = mac.pending_timers()
    check("modify: the same reminder, now in 10 minutes, and only one pending",
          r["did"] == "timer_set" and len(pend) == 1 and 9.5 <= pend[0]["in_minutes"] <= 10.1
          and r["detail"]["moved"], (r["did"], pend))
    check("the old one was stopped, not left running", mac.timer_row(first) is None)
    check("said in her words", r["say"] == "Alright, I will remind you in 10 minutes instead.",
          r["say"])
    check("with an Undo", bool(r.get("undo")))

    r, j = turn("move it to 6pm", follow="reminder at 18:00")
    pend = mac.pending_timers()
    want = fu.minutes_until("18:00")
    check("modify again: at 18:00, still one pending",
          r["did"] == "timer_set" and len(pend) == 1
          and abs(pend[0]["in_minutes"] - want) < 1.0, (r["did"], pend, want))
    check("and says the clock time", "18:00" in r["say"], r["say"])

    r, _ = turn("what was it again?", follow="say what the reminder is")
    check("what was it again: the words and when",
          r["did"] == "timer_recall" and "take the pills" in r["say"], r["say"])

    r, _ = turn("10 minutes later", follow="reminder 10 minutes later")
    pend = mac.pending_timers()
    check("10 minutes later: moved by ten from where it was",
          r["did"] == "timer_set" and len(pend) == 1
          and abs(pend[0]["in_minutes"] - (want + 10)) < 1.0, pend)

    r, _ = turn("cancel it", follow="cancel the reminder")
    check("cancel: the pending reminder stopped, no delete anywhere",
          r["did"] == "timer_cancelled" and mac.pending_timers() == [], mac.pending_timers())
    r, _ = turn("cancel it", follow="cancel the reminder")
    check("cancel again: says it has gone, stops nothing else", r["did"] == "timer_gone")

    # Hebrew, and a new request resets.
    fresh()
    turn("תזכירי לי בעוד 5 דקות לכבות את התנור", intent="timer", lang="hebrew",
         extra={"when_minutes": "5"})
    r, _ = turn("תעשי את זה עשר דקות במקום", follow="reminder in 10 minutes from now",
                lang="hebrew")
    check("HE: moved to 10 minutes, said in Hebrew",
          r["did"] == "timer_set" and r["say"] == "בסדר, אזכיר לך בעוד 10 דקות.", r["say"])
    r, _ = turn("מה השעה", intent="look_up", lang="hebrew")
    # The spoken answer to "מה השעה" is now the last action (t_spoken_answers), so the
    # check is that the reminder is no longer what a change would be about.
    check("HE: a new request forgets the reminder as the thing to change",
          (router.MEM.last_action or {}).get("kind") != "timer", router.MEM.last_action)
    r, j = turn("תבטלי את זה", intent="stop", lang="hebrew")
    check("HE: after it, 'cancel it' is not asked about the reminder",
          not _offered(j, "reminder") and len(mac.pending_timers()) == 1)
    mac.cancel_timers()


# ================================================================== calendar
EVENT = '{"title": "Dentist", "date": "%s", "start": "10:00", "end": "", "location": ""}'


def _add_dentist(lang=None):
    LLM.event_json = EVENT % _later(1).isoformat()
    r, _ = turn("add this to my calendar", intent="screen",
                extra={"screen_task": "add_to_calendar"}, lang=lang)
    assert r["did"] == "confirm_calendar", r
    return yes("yes" if not lang else "כן")[0]


def t_calendar():
    print("\nt_calendar")
    fresh()
    CAL["UID-OTHER"] = {"title": "Her own lunch", "date": _later(2).isoformat(),
                        "start": "13:00", "end": "14:00", "all_day": False, "calendar": "Home"}
    other = dict(CAL["UID-OTHER"])
    r = _add_dentist()
    uid = (router.MEM.last_action or {}).get("uid")
    check("set: added, and its uid and calendar remembered",
          r["did"] == "calendar_added" and uid == "UID-2"
          and router.MEM.last_action["calendar"] == "Home", router.MEM.last_action)

    friday = next(d for d in range(1, 8) if _later(d).weekday() == 4)
    SCRIPTS.clear()
    r, j = turn("move it to Friday", follow=_weekday_key(friday))
    check("modify: asks first, as adding did, and changes nothing yet",
          r["did"] == "confirm_event_change" and r.get("asked_back")
          and CAL[uid]["date"] == _later(1).isoformat() and not SCRIPTS, r.get("say"))
    check("the question names the new day and keeps the time",
          "Dentist" in r["say"] and "Friday" in r["say"] and "10:00" in r["say"], r["say"])
    r, _ = yes()
    check("yes: moved to Friday, same time, same length",
          r["did"] == "event_changed" and CAL[uid]["date"] == _later(friday).isoformat()
          and CAL[uid]["start"] == "10:00" and CAL[uid]["end"] == "11:00", CAL[uid])

    r, _ = turn("make it an hour and a half", follow="event lasts 90 minutes")
    r, _ = yes()
    check("modify again: 90 minutes from its start",
          r["did"] == "event_changed" and CAL[uid]["end"] == "11:30", CAL[uid])

    # "at 7 instead", split between 07:00 and 19:00.
    split = {"choice": "event starts at 19:00", "confidence": 0.45, "probabilities": {
        "none": 0.2, "event starts at 07:00": 0.35, "event starts at 19:00": 0.45}}
    r, _ = turn("at 7 instead", follow=split, extra={"is_complete": 0.2})
    check("'at 7 instead' is short but complete: asked about, not 'say it again'",
          r["did"] == "confirm_event_change", r["did"])
    r, _ = no()
    check("no: left as it was", r["did"] == "event_kept" and CAL[uid]["start"] == "10:00")

    r, _ = turn("what was it again?", follow="say what the event is")
    check("what was it: title and time", r["did"] == "event_recall"
          and "Dentist" in r["say"] and "10:00" in r["say"], r["say"])
    r, _ = turn("cancel it", follow="remove the event")
    check("remove: never deleted, and she is told where to do it",
          r["did"] == "event_not_removed" and uid in CAL and "Calendar" in r["say"], r["say"])
    r, _ = turn("add Dana to it", follow="invite someone to the event",
                extra={"contact": "Dana", "contact_is_named": 0.95})
    check("invite: says invitations are not possible, offers a message, sends nothing yet",
          r["did"] == "confirm_send" and "invitations" in r["say"] and not tm.SENT
          and router.AWAITING and router.AWAITING["need"] == "confirm_send", r["say"])
    router.AWAITING = None

    check("her own event was never touched",
          CAL["UID-OTHER"] == other and not any("UID-OTHER" in s for s in SCRIPTS))

    # Wrong-target safety.
    router.MEM.last_action = {**router.MEM.last_action, "uid": "", "at": time.time()}
    SCRIPTS.clear()
    r, _ = turn("move it to the day after", follow=_weekday_key(friday + 1))
    check("no uid on record: nothing is asked and no script runs",
          r["did"] == "event_missing" and not SCRIPTS and router.AWAITING is None)
    router.MEM.last_action = {**router.MEM.last_action, "uid": "UID-GONE", "at": time.time()}
    r, _ = turn("move it to the day after", follow=_weekday_key(friday + 1))
    check("(asked first)", r["did"] == "confirm_event_change", r["did"])
    r, _ = yes()
    check("the event is gone from Calendar: says so, changes nothing",
          r["did"] == "event_missing" and CAL["UID-OTHER"] == other, r["did"])

    # Hebrew, and a new request resets.
    fresh()
    _add_dentist(lang="hebrew")
    r, _ = turn("תזיזי את זה לשש בערב", follow="event starts at 18:00", lang="hebrew")
    check("HE: asks in Hebrew", r["did"] == "confirm_event_change"
          and "לשנות את Dentist" in r["say"] and "18:00" in r["say"], r["say"])
    r, _ = yes("כן")
    check("HE: moved to 18:00-19:00", r["did"] == "event_changed"
          and CAL["UID-1"]["start"] == "18:00" and CAL["UID-1"]["end"] == "19:00", CAL)
    turn("מה מזג האוויר", intent="look_up", lang="hebrew")
    r, j = turn("תזיזי את זה ליום שישי", intent="chitchat", lang="hebrew")
    check("HE: after a new request, 'move it' is not about the event any more",
          not _offered(j, "event") and r["did"] != "confirm_event_change")


# ================================================================== players
def _play_music():
    tm.reset_state()
    router.MEM.played("Shakira", "song_or_music", False,
                      {"id": "1", "title": "Waka Waka", "artist": "Shakira",
                       "player": "apple_music"}, "english")


def _play_youtube(vid="v0"):
    tm.reset_state()
    router.MEM.played("Umm Kulthum", "song_or_music", False,
                      {"id": vid, "title": "Enta Omri", "player": "youtube"}, "english")
    TABS.append(f"https://www.youtube.com/watch?v={vid}")


def t_players():
    print("\nt_players")
    fresh()
    _play_music()
    SCRIPTS.clear()
    r, j = turn("pause", follow="pause it")
    check("Music: the question rode along", asked_follow(j))
    check("Music: asked its state, then paused it, never launching it",
          r["did"] == "stopped" and any("player state" in s for s in SCRIPTS)
          and any(s.startswith('if application "Music" is running') and "\npause\n" in s
                  for s in SCRIPTS), SCRIPTS)
    PLAYER["Music"] = "paused"
    r, _ = turn("resume", follow="carry on playing")
    check("Music: resumed", r["did"] == "resume" and 'tell application "Music"\nplay' in SCRIPTS[-1])
    PLAYER["Music"] = "playing"
    r, _ = turn("back 10 seconds", follow="back 10 seconds")
    check("Music: back 10 seconds", "player position - 10" in SCRIPTS[-1], SCRIPTS[-1])
    r, _ = turn("skip", follow="the previous song or video")
    check("Music: previous track", "previous track" in SCRIPTS[-1])
    VOL = list(tm.VOLUME)
    r, _ = turn("louder", follow="louder")
    check("Music: louder goes to the volume, with its Undo",
          r["did"] == "louder" and bool(r.get("undo")), (r["did"], tm.VOLUME, VOL))
    r, _ = turn("full screen", follow="full screen")
    check("Music: full screen is honestly not possible", r["did"] == "cannot_control"
          and "Apple Music" in r["say"], r["say"])
    r, _ = turn("what was that again?", follow="say what is playing")
    check("Music: what is playing", r["say"] == "This is Waka Waka.", r["say"])

    # Never a player that is not playing.
    PLAYER["Music"] = "not_running"
    SCRIPTS.clear()
    r, _ = turn("pause", follow="pause it")
    check("Music not running: says so, sends it nothing",
          r["did"] == "nothing_playing" and not any("\npause\n" in s for s in SCRIPTS), SCRIPTS)
    PLAYER["Music"] = "paused"
    SCRIPTS.clear()
    r, _ = turn("pause", follow="pause it")
    check("Music already paused: pausing again toggles nothing",
          r["did"] == "stopped" and not any("\npause\n" in s for s in SCRIPTS))

    # YouTube in a tab, with JavaScript allowed.
    fresh()
    _play_youtube()
    TABS.append("https://mail.example.com/inbox")
    r, j = turn("pause", follow="pause it")
    check("YouTube: paused in its own tab (JavaScript), nothing pressed",
          r["did"] == "stopped" and VIDEO["paused"] and not PRESSED, (r["did"], PRESSED))
    r, _ = turn("pause", follow="pause it")
    check("YouTube: paused again does not toggle it back on",
          VIDEO["paused"] and "already" in r["say"].lower(), r["say"])
    r, _ = turn("carry on", follow="carry on playing")
    check("YouTube: resumed", r["did"] == "resume" and not VIDEO["paused"])
    # Without JavaScript: the site's own keys, in that tab only.
    JS_OK["on"] = False
    r, _ = turn("back 10 seconds", follow="back 10 seconds")
    check("YouTube, no JavaScript: 'j' pressed in the YouTube tab",
          r["did"] == "back_10" and PRESSED == ["https://www.youtube.com/watch?v=v0:keystroke \"j\""],
          PRESSED)
    r, _ = turn("full screen", follow="full screen")
    check("YouTube: full screen is 'f' in that tab", PRESSED[-1].endswith('keystroke "f"'), PRESSED)
    r, _ = turn("next episode", follow="the next episode")
    check("YouTube: next episode is shift-N", PRESSED[-1].endswith('keystroke "n" using shift down'),
          PRESSED)
    check("never a key in her mail tab", not any("mail.example" in p for p in PRESSED))
    # The tab is gone: nothing is pressed anywhere.
    TABS.clear()
    PRESSED.clear()
    r, _ = turn("pause", follow="pause it")
    check("YouTube tab closed: says so, presses nothing",
          r["did"] == "nothing_playing" and not PRESSED, (r["did"], PRESSED))
    # Next over YouTube: another of her own search, as "another one" does.
    TABS.append("https://www.youtube.com/watch?v=v0")
    tm.OPENED.clear()
    r, _ = turn("next", follow="the next song or video",
                extra={"best": "1", "any_good": 0.9})
    check("YouTube 'next': the next result of her search, not a key press",
          r["did"] == "playing" and tm.OPENED and "v0" not in tm.OPENED[-1], (r["did"], tm.OPENED))

    # "שיר יותר שמח בבקשה" holds יותר, so the question rides along, and on the one live
    # run it read as "next". What she says she wants instead must still win: a search
    # for the happier kind, not the same search again (test_micmic, 2026-09-27).
    fresh()
    _play_youtube()
    searched = []
    real_search = tm.yt.search
    tm.yt.search = lambda q_, n=18: (searched.append(q_), real_search(q_, n))[1]
    try:
        r, _ = turn("שיר יותר שמח בבקשה", intent="music", lang="hebrew",
                    follow="the next song or video",
                    extra={"describes_instead": 0.9, "rejects_last": 0.5,
                           "span": "שמח", "exists": 0.9, "best": "1", "any_good": 0.9})
    finally:
        tm.yt.search = real_search
    check("'a happier song' read as next: still searches for the happier kind",
          r["did"] == "playing" and searched and all("שמח" in q for q in searched),
          (r["did"], searched))

    # Netflix: found by its host; no key for the next episode there.
    fresh()
    tm.reset_state()
    router.MEM.played("Friends", "tv_or_series", True,
                      {"id": "netflix:Friends", "title": "Friends", "player": "netflix",
                       "open_only": True}, "english")
    TABS.append("https://www.netflix.com/watch/80057281")
    JS_OK["on"] = False
    r, _ = turn("pause", follow="pause it")
    check("Netflix: space in the Netflix tab",
          r["did"] == "stopped" and PRESSED == ["https://www.netflix.com/watch/80057281:key code 49"],
          PRESSED)
    r, _ = turn("next episode", follow="the next episode")
    check("Netflix: next episode is honestly not possible from here",
          r["did"] == "cannot_control" and "Netflix" in r["say"], r["say"])

    # Hebrew; and the plain player intent now drives the tab too.
    fresh()
    _play_youtube("v3")
    r, _ = turn("תעצרי", follow="pause it", lang="hebrew")
    check("HE: paused, said in Hebrew", r["did"] == "stopped" and VIDEO["paused"], r)
    r, j = turn("keep going please", intent="player", extra={"player_action": "resume"})
    check("no modifier word: no question, and player_action still resumes the tab",
          not asked_follow(j) and r["did"] == "resume" and not VIDEO["paused"], r["did"])

    # She started Spotify herself; MicMic put nothing on.
    fresh()
    PLAYER.update(Music="not_running", Spotify="playing")
    SCRIPTS.clear()
    r, j = turn("pause the music", intent="player", extra={"player_action": "pause"})
    check("her own Spotify: 'pause' pauses Spotify",
          r["did"] == "stopped" and r["detail"]["player"] == "spotify"
          and any('tell application "Spotify"\npause' in s for s in SCRIPTS), SCRIPTS)
    PLAYER.update(Spotify="not_running")
    SCRIPTS.clear()
    r, _ = turn("pause the music", intent="player", extra={"player_action": "pause"})
    check("nothing playing anywhere: nothing controlled",
          r["did"] == "nothing_playing" and not any("\npause" in s for s in SCRIPTS))

    # The owner's case still goes where it went: a new artist is a new request.
    fresh()
    _play_music()
    r, j = turn("change to Bad Bunny", intent="music",
                extra={"span_subject": "Bad Bunny", "span_subject_exists": 0.95})
    check("'change to Bad Bunny' asks the question, Jev says none, the music path runs",
          asked_follow(j) and r["did"] in ("playing", "opened_in_app", "not_found")
          and r["detail"].get("player") == "apple_music", (r["did"], r.get("detail")))


# ================================================================== screen answers
def t_answers():
    print("\nt_answers")
    fresh()
    LLM.prompts.clear()
    r, _ = turn("what's on my screen", intent="screen", extra={"screen_task": "describe"})
    check("set: described, remembered", r["did"] == "described_screen"
          and (router.MEM.last_action or {}).get("kind") == "answer")
    n0 = LLM.calls
    r, j = turn("shorter", follow="say it shorter")
    check("shorter: one Gemini call, one Jev call",
          r["did"] == "rewrote_answer" and LLM.calls == n0 + 1 and j.calls == 1, (LLM.calls, j.calls))
    check("rewrote what she heard, not the screen",
          r["say"].startswith("[short] A recipe") and "Say this again much shorter"
          in LLM.prompts[-1][1], r["say"])
    r, _ = turn("in Hebrew", follow="say it in Hebrew")
    check("modify again, in Hebrew: the shorter one, and spoken as Hebrew",
          r["say"].startswith("[Hebrew] [short]") and r["lang"] == "hebrew", (r["say"], r["lang"]))
    r, _ = turn("say it again", follow="say it again")
    check("again: the last version, no model call", r["say"].startswith("[Hebrew]")
          and LLM.calls == n0 + 2)
    r, _ = turn("send that to Dana", follow="send it to someone",
                extra={"contact": "Dana", "contact_is_named": 0.95})
    check("send that to Dana: asks first, quotes it, sends nothing yet",
          r["did"] == "confirm_send" and "Dana" in r["say"] and "[Hebrew]" in r["say"]
          and not tm.SENT, r["say"])
    router.AWAITING = None
    r, _ = turn("send it to someone", follow="send it to someone")
    check("send with no name: asks who, keeping the words",
          r["did"] == "need_who" and router.AWAITING["body"].startswith("[Hebrew]"))
    router.AWAITING = None

    fresh()
    r, _ = turn("תסכמי את מה שיש על המסך", intent="screen",
                extra={"screen_task": "summarize"}, lang="hebrew")
    r, _ = turn("תקצרי", follow="say it shorter", lang="hebrew")
    check("HE: shorter, in Hebrew", r["did"] == "rewrote_answer"
          and "in Hebrew" in LLM.prompts[-1][1], LLM.prompts[-1][1][:60])
    turn("play Shakira", intent="music",
         extra={"span_subject": "Shakira", "span_subject_exists": 0.95})
    r, j = turn("תקצרי", intent="chitchat", lang="hebrew")
    check("HE: after a new request, 'shorter' no longer rewrites the screen answer",
          r["did"] != "rewrote_answer"
          and (router.MEM.last_action or {}).get("task") != "summarize", router.MEM.last_action)


def t_spoken_answers():
    """Bench v1 mt-010: "who was albert einstein", then "shorter" -> "I can make it
    louder or quieter". Only screen answers were a last action; every spoken answer
    (a fact, small talk, the weather, a live result) is one now."""
    print("\nt_spoken_answers")
    fresh()
    r, _ = turn("who was albert einstein", intent="look_up", extra={"needs_knowledge": 0.95})
    check("a knowledge answer is a last action (kind answer, with what she heard)",
          r["did"] == "answered" and (router.MEM.last_action or {}).get("kind") == "answer"
          and router.MEM.last_action.get("text") == "[answer]", (r["did"], router.MEM.last_action))
    n0 = LLM.calls
    r, j = turn("shorter", follow="say it shorter")
    check("shorter after a knowledge answer: asked as a follow-up and rewritten",
          asked_follow(j) and r["did"] == "rewrote_answer"
          and r["say"].startswith("[short] [answer]") and LLM.calls == n0 + 1, (r["did"], r["say"]))
    check("its system prompt is not about a screen",
          "screen" not in LLM.prompts[-1][0].lower(), LLM.prompts[-1][0][:80])
    r, _ = turn("say it again", follow="say it again")
    check("say it again: the last version, no model call",
          r["did"] == "again" and r["say"].startswith("[short]") and LLM.calls == n0 + 1, r)

    fresh()
    r, _ = turn("how are you today", intent="chitchat")
    check("a chat reply is a last action too",
          r["did"] == "chatted" and (router.MEM.last_action or {}).get("kind") == "answer", r["did"])
    r, _ = turn("in Hebrew", follow="say it in Hebrew")
    check("in Hebrew after small talk: rewritten and spoken as Hebrew",
          r["did"] == "rewrote_answer" and r["lang"] == "hebrew"
          and r["say"].startswith("[Hebrew] [chat]"), (r["did"], r["lang"], r["say"]))

    fresh()
    _, j0 = turn("what's the weather", intent="look_up")
    base_q = sorted(j0.asked[0])
    turn("who was albert einstein", intent="look_up", extra={"needs_knowledge": 0.95})
    _, j1 = turn("what's the weather", intent="look_up")
    check("after an answer, a request with no modifier word is asked exactly as before",
          sorted(j1.asked[0]) == base_q and not asked_follow(j1) and not told(j1))
    r, _ = turn("play Shakira", intent="music",
                extra={"span_subject": "Shakira", "span_subject_exists": 0.95})
    check("a new request forgets the answer", (router.MEM.last_action or {}).get("kind") != "answer",
          router.MEM.last_action)


# ================================================================== messages
# The owner's session (1.2.0; the person and the page are stand-ins). A flights page in
# Chrome, "send this to <her>": the page's link went out on iMessage. "oh i meant on
# whatsapp" was asked what the message should say; "send her this page" and "send this
# page to <her>" were each asked again as a repeated instruction; "just a link to this
# page on whatsapp" was then queued as the message, word for word.
FLIGHTS_URL = "https://flights.example.org/search?from=TLV&to=JFK&date=2026-10-04"
FLIGHTS = {**bench.SCREEN,
           "frontmost": {"app": "Google Chrome", "pid": 1, "bundle_id": "com.google.Chrome",
                         "window": "Flights"},
           "visible": {"text": "Tel Aviv to New York, Sunday 4 October. Nonstop from $612. "
                               "Book with the airline or with an agent.", "truncated": False},
           "page": {"url": FLIGHTS_URL, "title": "Flights from Tel Aviv to New York"}}
LITERAL = "just a link to this page on whatsapp"
# Jev's readings of her first two sentences, from the owner's trace.
SEND_THIS = dict(contact="Dana", contact_is_named=0.92, refers_back=0.38,
                 message_has_content=0.15, refers_to_screen=0.9, same_person=0.95)
MEANT_WA = dict(contact="Dana", contact_is_named=0.9, refers_back=0.86,
                message_has_content=0.18, channel="whatsapp")
ASK_DANA = dict(contact="Dana", contact_is_named=0.95, message_has_content=0.1,
                same_person=0.95)


class on_flights:
    """The flights page on screen, sends switched on (every send is a recorder), and a
    countdown only the test runs out (router._fire_pending)."""

    def __enter__(self):
        self.prev = (router._screen_context, router.CANCEL_WINDOW)
        router._screen_context = lambda max_chars=6000: json.loads(json.dumps(FLIGHTS))
        router.CANCEL_WINDOW = 30.0
        self.gates = tm.gates_on()
        self.gates.__enter__()
        return self

    def __exit__(self, *exc):
        router._cancel_pending()
        router.AWAITING = None
        router._screen_context, router.CANCEL_WINDOW = self.prev
        self.gates.__exit__(*exc)
        router.prefs._update(lambda d: d.update(confirm_send=""))
        return False


def answer(utterance, **extra):
    """Her answer to "what should it say?", read as an answer (the owner's were)."""
    return turn(utterance, intent="message", extra={"is_answer": 0.95, **extra})


def link_sent_to_dana():
    r, _ = turn("send this to Dana", intent="message", extra=SEND_THIS)
    router._fire_pending()
    return r


def pend() -> dict:
    return dict(router.PENDING or {})


def fold_keys(j: ScriptedJev) -> set:
    """The questions _resume asked, when it was the first call of the turn."""
    return set(j.asked[0]) if j.asked else set()


def t_message_owner_session():
    print("\nt_message_owner_session")
    fresh()
    with on_flights():
        r, _ = turn("send this to Dana", intent="message", extra=SEND_THIS)
        d = r.get("detail") or {}
        check("1. 'send this to Dana' on the flights page: its link, on a countdown",
              r["did"] == "sending" and d.get("from_screen") == "send_link"
              and pend().get("text") == FLIGHTS_URL, (r["did"], d))
        router._fire_pending()
        check("1. and it went, on iMessage", tm.SENT == [("Dana", FLIGHTS_URL)] and not tm.WA,
              (tm.SENT, tm.WA))
        check("1. the send is the last action, as a message from the screen",
              (router.MEM.last_action or {}).get("kind") == "message"
              and router.MEM.last_action.get("from_screen") == "send_link", router.MEM.last_action)

        r, j = turn("oh i meant on whatsapp", intent="message", extra=MEANT_WA,
                    follow="send it again on WhatsApp")
        p = pend()
        check("2. 'oh i meant on whatsapp': the same link, the same person, on WhatsApp",
              r["did"] == "sending" and p.get("to") == "Dana" and p.get("channel") == "whatsapp"
              and p.get("text") == FLIGHTS_URL, (r["did"], r.get("say"), p))
        check("2. decided in the one understand() call, which was told what was sent",
              asked_follow(j) and told(j) and j.calls == 1, j.calls)
        check("2. said plainly, read back, on a countdown",
              "same link" in (r.get("say") or "") and "WhatsApp" in r["say"]
              and "Dana" in r["say"] and "Say no" in r["say"]
              and (r.get("detail") or {}).get("countdown"), r.get("say"))
        check("2. never claims to recall or undo the iMessage",
              not re.search(r"recall|unsen|took it back|delet|instead of", r.get("say") or "", re.I),
              r.get("say"))
        router._fire_pending()
        check("2. it went on WhatsApp, and the iMessage is left as it was",
              [t for _, t in tm.WA] == [FLIGHTS_URL] and len(tm.SENT) == 1, (tm.SENT, tm.WA))

        # 8 s later (her countdown had run out), and 12 s after that.
        r, _ = turn("send her this page", intent="message",
                    extra=dict(contact="Dana", contact_is_named=0.6, refers_back=0.9,
                               refers_to_screen=0.8, message_has_content=0.2,
                               channel="imessage", is_answer=0.95, restates_request=0.95))
        p = pend()
        check("3. 'send her this page': the page on screen again, on the app she just asked for",
              r["did"] == "sending" and (r.get("detail") or {}).get("from_screen") == "send_link"
              and p.get("channel") == "whatsapp" and p.get("text") == FLIGHTS_URL,
              (r["did"], r.get("say"), p))
        router._fire_pending()
        r, _ = turn(LITERAL, intent="message",
                    extra=dict(contact_is_named=0.1, refers_back=0.7, refers_to_screen=0.8,
                               message_has_content=0.4, channel="whatsapp",
                               is_answer=0.95, restates_request=0.1))
        p = pend()
        check("4. 'just a link to this page on whatsapp': the link on WhatsApp, not those words",
              r["did"] == "sending" and p.get("text") == FLIGHTS_URL
              and p.get("channel") == "whatsapp" and p.get("to") == "Dana",
              (r["did"], r.get("say"), p))
        router._fire_pending()
        check("nothing she said was ever sent as the message",
              all(t == FLIGHTS_URL for _, t in tm.SENT + tm.WA) and len(tm.WA) == 3,
              (tm.SENT, tm.WA))


def t_message_need_what():
    """Asked "what should it say?", an answer can be an instruction about the content
    rather than the content: the screen, the same again, or only the app."""
    print("\nt_message_need_what")
    with on_flights():
        # The state the owner was in: the correction not recognised as one.
        fresh()
        link_sent_to_dana()
        r, _ = turn("oh i meant on whatsapp", intent="message", extra=MEANT_WA, follow="none")
        check("a correction Jev does not recognise still asks what, on WhatsApp",
              r["did"] == "need_what" and (router.AWAITING or {}).get("channel") == "whatsapp",
              (r["did"], router.AWAITING))
        r, j = answer("send her this page", restates_request=0.95, answer_is="screen")
        p = pend()
        check("3. 'send her this page' answering it: the page's link, on WhatsApp",
              r["did"] == "sending" and (r.get("detail") or {}).get("from_screen") == "send_link"
              and p.get("channel") == "whatsapp" and p.get("text") == FLIGHTS_URL,
              (r["did"], r.get("say"), p))
        check("3. one Jev call, the answer's own", j.calls == 1, j.calls)

        fresh()
        link_sent_to_dana()
        turn("oh i meant on whatsapp", intent="message", extra=MEANT_WA, follow="none")
        r, _ = answer(LITERAL, restates_request=0.1, answer_is="screen")
        p = pend()
        check("4. 'just a link to this page on whatsapp' answering it: the link, never the words",
              r["did"] == "sending" and p.get("text") == FLIGHTS_URL
              and p.get("channel") == "whatsapp", (r["did"], r.get("say"), p))

        for said, kind in (("the link", "screen"), ("send the page", "screen"),
                           ("the same", "same")):
            fresh()
            link_sent_to_dana()
            turn("send a message to Dana", intent="message", extra=ASK_DANA)
            r, _ = answer(said, answer_is=kind)
            check(f"{said!r}: the page's link, not the words",
                  r["did"] == "sending" and pend().get("text") == FLIGHTS_URL
                  and pend().get("to") == "Dana", (r["did"], r.get("say"), pend()))

        fresh()
        turn("send a message to Dana", intent="message", extra=ASK_DANA)
        r, _ = answer("on whatsapp", answer_is="app_only")
        aw = router.AWAITING or {}
        check("'on whatsapp': the app changes, and it still asks what to say",
              r["did"] == "need_what" and aw.get("need") == "what"
              and aw.get("channel") == "whatsapp" and not router.PENDING
              and "WhatsApp" in (r.get("say") or ""), (r["did"], r.get("say"), aw))
        r, _ = answer("I'm running late")
        check("then her words go, on WhatsApp",
              r["did"] == "sending" and pend().get("text") == "I'm running late"
              and pend().get("channel") == "whatsapp", (r["did"], pend()))

        fresh()
        turn("send a message to Dana", intent="message", extra=ASK_DANA)
        r, _ = answer("tell her I'm running late", answer_is="words",
                      message_words="I m running late", message_words_exists=0.95)
        check("'tell her I'm running late': sends 'I'm running late'",
              r["did"] == "sending" and pend().get("text") == "I'm running late",
              (r["did"], r.get("say"), pend()))

        fresh()
        turn("send a message to Dana", intent="message", extra=ASK_DANA)
        r, _ = answer("tell her I'm coming on whatsapp", answer_is="words",
                      message_words="I m coming", message_words_exists=0.95)
        check("'tell her I'm coming on whatsapp': 'I'm coming', on WhatsApp",
              r["did"] == "sending" and pend().get("text") == "I'm coming"
              and pend().get("channel") == "whatsapp", (r["did"], pend()))

        fresh()
        turn("send a message to Dana", intent="message", extra=ASK_DANA)
        r, j = answer("I'm running late")
        check("genuine words are the message, as before",
              r["did"] == "sending" and pend().get("text") == "I'm running late", pend())
        check("and the answer's question is exactly what it was",
              fold_keys(j) == {"is_answer", "contact", "restates_request"}, fold_keys(j))

        fresh()
        turn("send a message to Dana", intent="message", extra=ASK_DANA)
        long_words = "I sent you the link to the hotel yesterday, can you check it tonight"
        r, j = answer(long_words, answer_is="words")
        check("genuine words that mention a link are sent whole, never cut to a part",
              r["did"] == "sending" and pend().get("text") == long_words
              and fu.ANSWER in fold_keys(j) and fu.WORDS_SPAN not in fold_keys(j),
              (pend(), fold_keys(j)))

        # Hebrew.
        fresh()
        link_sent_to_dana()
        turn("תשלחי הודעה לדנה", intent="message", extra=ASK_DANA, lang="hebrew")
        r, _ = answer("תשלחי לה את הדף הזה", answer_is="screen", restates_request=0.9)
        check("HE: 'תשלחי לה את הדף הזה': the page's link, said in Hebrew",
              r["did"] == "sending" and pend().get("text") == FLIGHTS_URL
              and r.get("lang") == "hebrew", (r["did"], r.get("say"), pend()))
        fresh()
        turn("תשלחי הודעה לדנה", intent="message", extra=ASK_DANA, lang="hebrew")
        r, _ = answer("בוואטסאפ", answer_is="app_only")
        check("HE: 'בוואטסאפ': the app changes and it asks again, in Hebrew",
              r["did"] == "need_what" and (router.AWAITING or {}).get("channel") == "whatsapp"
              and "וואטסאפ" in (r.get("say") or ""), (r["did"], r.get("say")))
        fresh()
        turn("תשלחי הודעה לדנה", intent="message", extra=ASK_DANA, lang="hebrew")
        r, _ = answer("תגידי לה שאני כבר בדרך הביתה", answer_is="words",
                      message_words="שאני כבר בדרך הביתה", message_words_exists=0.95)
        check("HE: 'תגידי לה שאני כבר בדרך הביתה': the words, without the instruction",
              r["did"] == "sending" and pend().get("text") == "שאני כבר בדרך הביתה", pend())


def t_message_follow_ups():
    """Right after a message: another app, another person too, instead, what was it."""
    print("\nt_message_follow_ups")
    gal = dict(contact="Gal Ben Ami", contact_is_named=0.95, same_person=0.95)
    with on_flights():
        fresh()
        link_sent_to_dana()
        r, _ = turn("send it to Gal too", intent="message", extra=gal,
                    follow="send it to another person too")
        p = pend()
        check("'send it to Gal too': the same link to Gal",
              r["did"] == "sending" and p.get("to") == "Gal Ben Ami"
              and p.get("text") == FLIGHTS_URL and "same link" in (r.get("say") or ""),
              (r["did"], r.get("say"), p))
        router._fire_pending()
        check("and Dana's is not sent again", tm.SENT == [("Dana", FLIGHTS_URL),
                                                          ("Gal Ben Ami", FLIGHTS_URL)], tm.SENT)

        fresh()
        turn("send this to Dana", intent="message", extra=SEND_THIS)       # still counting
        turn("send it to Gal too", intent="message", extra=gal,
             follow="send it to another person too")
        router._fire_pending()
        check("'too' while Dana's counts down: both go, Dana's is never dropped",
              sorted(n for n, _ in tm.SENT) == ["Dana", "Gal Ben Ami"], tm.SENT)

        fresh()
        turn("send this to Dana", intent="message", extra=SEND_THIS)
        r, _ = turn("oh I meant Gal", intent="message", extra=gal,
                    follow="send it to another person instead")
        router._fire_pending()
        check("'I meant Gal' while it counts down: it goes to Gal only",
              r["did"] == "sending" and tm.SENT == [("Gal Ben Ami", FLIGHTS_URL)], tm.SENT)

        fresh()
        turn("send this to Dana", intent="message", extra=SEND_THIS)
        r, _ = turn("no wait, send it on WhatsApp", intent="message", extra=dict(
            MEANT_WA, stop_it=0.40, says_what_instead=0.85), follow="send it again on WhatsApp")
        router._fire_pending()
        check("'no wait, send it on WhatsApp' during the countdown: only the WhatsApp one goes",
              r["did"] == "sending" and not tm.SENT and [t for _, t in tm.WA] == [FLIGHTS_URL],
              (r["did"], tm.SENT, tm.WA))

        fresh()
        link_sent_to_dana()
        r, _ = turn("what did I send?", intent="look_up", follow="say what was sent")
        check("'what did I send?': read back, nothing sent",
              r["did"] == "message_recall" and "link" in (r.get("say") or "")
              and "Dana" in r["say"] and not router.PENDING and len(tm.SENT) == 1, r.get("say"))

        fresh()
        link_sent_to_dana()
        r, _ = turn("send it by email", intent="message", follow="send it by an app MicMic cannot use")
        check("'send it by email': says which apps it can use, arms nothing",
              r["did"] == "cannot_send_there" and "WhatsApp" in (r.get("say") or "")
              and not router.PENDING, (r["did"], r.get("say")))

        fresh()
        link_sent_to_dana()
        r, _ = turn("התכוונתי בוואטסאפ", intent="message", lang="hebrew",
                    extra=dict(MEANT_WA), follow="send it again on WhatsApp")
        check("HE: 'התכוונתי בוואטסאפ': the same link on WhatsApp, said in Hebrew",
              r["did"] == "sending" and pend().get("channel") == "whatsapp"
              and pend().get("text") == FLIGHTS_URL and "וואטסאפ" in (r.get("say") or ""),
              (r["did"], r.get("say")))

        fresh()
        link_sent_to_dana()
        router.prefs._update(lambda d: d.update(confirm_send="always"))
        r, _ = turn("oh i meant on whatsapp", intent="message", extra=MEANT_WA,
                    follow="send it again on WhatsApp")
        aw = router.AWAITING or {}
        check("'ask me before sending' is kept: it asks first",
              r["did"] == "confirm_send" and aw.get("channel") == "whatsapp"
              and aw.get("from_screen") == "send_link" and not router.PENDING, (r["did"], aw))
        router.prefs._update(lambda d: d.update(confirm_send=""))
        router.AWAITING = None

        fresh()
        link_sent_to_dana()
        mac.SEND_FOR_REAL = False
        try:
            r, _ = turn("oh i meant on whatsapp", intent="message", extra=MEANT_WA,
                        follow="send it again on WhatsApp")
        finally:
            mac.SEND_FOR_REAL = True
        check("with sending switched off it says so and arms nothing",
              r["did"] == "send_disabled" and not router.PENDING, r["did"])


def t_message_gate():
    """Only her words about a message she just had sent ask anything new."""
    print("\nt_message_gate")
    for s in ("oh i meant on whatsapp", "send it on Telegram", "by text instead",
              "send it to Gal too", "what did I send?", "send it by email", "I meant Gal",
              "התכוונתי בוואטסאפ", "תשלחי את זה גם לגל", "מה שלחתי?",
              "ابعتيه عالواتساب", "отправь в WhatsApp", "что я отправила?"):
        check(f"message words found: {s!r}", fu.says_message_change(s))
    for s in ("what's the weather", "play Bad Bunny", "send Dana hi", "tell Dana I'm late",
              "מה השעה", "كم الساعة", "какая погода"):
        check(f"no message words: {s!r}", not fu.says_message_change(s))
    for s in (LITERAL, "send her this page", "the link", "the same", "on whatsapp",
              "tell her I'm coming", "תגידי לה שאני בדרך", "את הקישור", "בוואטסאפ",
              "the screenshot"):
        check(f"an answer that may point at the content: {s!r}", fu.points_at_content(s))
    for s in ("I'm running late", "happy birthday", "שאני מאחרת", "כן, מחר בשמונה"):
        check(f"plain words for the message: {s!r}", not fu.points_at_content(s))

    fresh()
    _, j0 = turn("what's the weather", intent="look_up")
    base = sorted(j0.asked[0])
    with on_flights():
        link_sent_to_dana()
        _, j = turn("what's the weather", intent="look_up")
        check("after a send, a request with no message words is asked exactly as before",
              sorted(j.asked[0]) == base and not told(j))
        fresh()
        turn("send a message to Dana that I will be late", intent="message",
             extra=dict(ASK_DANA, message_has_content=0.9, span="I will be late", exists=0.95))
        router._fire_pending()
        check("a message of her own words is the last action too",
              (router.MEM.last_action or {}).get("kind") == "message"
              and tm.SENT == [("Dana", "I will be late")], (router.MEM.last_action, tm.SENT))
        _, j = turn("send it on WhatsApp", intent="message", extra=dict(channel="whatsapp"))
        u_q = next(q for q in j.asked if "intent" in q)
        check("her own words to another app stay with the draft: no new question",
              "amends_message" in u_q and fu.QUESTION not in u_q, sorted(u_q)[:5])
        fresh()
        turn("send a message to Dana that I will be late", intent="message",
             extra=dict(ASK_DANA, message_has_content=0.9, span="I will be late", exists=0.95))
        router._fire_pending()
        _, j = turn("send it to Gal too", intent="message",
                    extra=dict(contact="Gal Ben Ami", contact_is_named=0.95))
        u_q = next(q for q in j.asked if "intent" in q)
        opts = list((u_q.get(fu.QUESTION) or {}).get("criteria") or {})
        check("'too' after her own words is asked, without offering another app",
              "send it to another person too" in opts
              and not any("again on" in o for o in opts), opts)
    check("no em dash in any new line",
          not any("—" in v for block in fu.LINES.values() for v in block.values()))


# ================================================================== new chat
def t_new_chat_resets():
    print("\nt_new_chat_resets")
    fresh()
    turn("remind me in 5 minutes to call", intent="timer", extra={"when_minutes": "5"})
    old = (router.MEM.last_action or {}).get("id")
    router.new_conversation()
    check("a new chat forgets the last action", router.MEM.last_action is None)
    r, j = turn("make it 10 minutes instead", intent="timer")
    # Read fresh, with the intent a timer, it sets one of its own (the amount comes from
    # her words since fix/time); it is never taken as a change to the old reminder.
    check("and the next turn is not asked about it", not asked_follow(j)
          and (router.MEM.last_action or {}).get("id") != old)
    mac.cancel_timers()


def main():
    t_units()
    t_byte_identical()
    t_timer()
    t_calendar()
    t_players()
    t_answers()
    t_spoken_answers()
    for t in (t_message_owner_session, t_message_need_what, t_message_follow_ups,
              t_message_gate):
        try:
            t()
        except Exception as e:  # noqa: BLE001  (on a checkout without the fix)
            check(f"{t.__name__} ran to the end", False, repr(e))
    t_new_chat_resets()
    mac._osa = tm._blocked_osa
    real_osa = [c for c in tm.LAUNCHED if c and c[0] in ("osascript", "screencapture")]
    check("0 osascript or screencapture escapes (every script went to the fake)",
          not tm.OSA and not real_osa, f"{len(tm.OSA)} + {len(real_osa)}")
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAILED: {n}  {d}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
