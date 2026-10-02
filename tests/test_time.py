#!/usr/bin/env python3
"""Timers, reminders, alarms and calendar events in her own words, offline.

    cd <checkout> && uv run python tests/test_time.py

The bench (2026-10-01, calendar_reminders 51.1%): "remind me in fifty minutes" said 45,
"set a timer for 25 minutes" asked "In how long?", "set an alarm for seven am" and
"remind me tomorrow at 9" had no path at all, "add lunch with Dana on Thursday at 1" had
no intent, and a weekday that had passed after midnight was dropped.

What is checked here: the parser (savta/timewords.py) in four languages; the time
question rides in the one understand() request only when her words hold a time word,
so every other request is asked exactly what it was; Jev picks the spans (which words
are the duration, the clock time, the day, what it is about) and code computes the
numbers; the right kind is set and said exactly; the follow-up layer moves the new
timer, alarm and reminder objects. Jev is scripted, Calendar and Reminders are the fake
osascript of tests/test_followups.py, nothing leaves the machine, nothing reaches
127.0.0.1:8799, and no real Calendar or Reminders is touched.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_followups as tf                    # noqa: E402  (loads the stub wall)

router, mac, fu = tf.router, tf.mac, tf.fu
# Reminders rows go through the real add_reminder into the fake osascript (tf.fake_osa
# plays Reminders by id), not the suite's recorder.
mac.add_reminder = tf.tm.REAL_ADD_REMINDER
try:
    from savta import timewords as tw          # noqa: E402
except ImportError as e:                       # on a checkout without the fix
    tw = None
    IMPORT_ERROR = repr(e)

PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail="") -> bool:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, str(detail)))
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return bool(ok)


Q = "time_request"


def spans(**kw) -> dict:
    """Scripted span picks: duration="fifty minutes" -> time_duration + its exists."""
    out = {}
    for k, v in kw.items():
        out[f"time_{k}"] = v
        out[f"time_{k}_exists"] = 0.95
    return out


def ask(utterance, kind, *, intent="unclear", lang=None, follow=None, **sp):
    extra = {Q: kind, **spans(**sp)}
    return tf.turn(utterance, intent=intent, lang=lang, follow=follow, extra=extra)


def due_of(r) -> dt.datetime | None:
    pend = [t for t in mac._TIMERS if t["at"] > time.time()]
    return dt.datetime.fromtimestamp(pend[-1]["at"]) if pend else None


def next_clock(h: int, m: int, now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now + dt.timedelta(seconds=30) else t + dt.timedelta(days=1)


def next_weekday(wd: int, today: dt.date | None = None) -> dt.date:
    today = today or dt.date.today()
    return today + dt.timedelta(days=(wd - today.weekday()) % 7 or 7)


# ================================================================== the parser
def t_parser():
    print("\nt_parser")
    if tw is None:
        check("savta.timewords exists", False, IMPORT_ERROR)
        return
    dur = {
        "fifty minutes": 50, "in fifty minutes": 50, "25 minutes": 25,
        "for 25 minutes": 25, "two hundred minutes": 200, "four minutes": 4,
        "an hour and a half": 90, "two and a half hours": 150, "half an hour": 30,
        "a quarter of an hour": 15, "three quarters of an hour": 45, "90 seconds": 1.5,
        "an hour": 60, "1.5 hours": 90, "2 hours and 15 minutes": 135,
        "twenty five minutes": 25, "twenty-five minutes": 25, "a couple of minutes": 2,
        "thirty minutes from now": 30, "a minute": 1,
        "בעוד חמישים דקות": 50, "שעה וחצי": 90, "רבע שעה": 15, "בעוד רבע שעה": 15,
        "חצי שעה": 30, "שעתיים": 120, "למאתיים דקות": 200, "לשלוש דקות": 3,
        "עשרים וחמש דקות": 25, "שעתיים וחצי": 150, "90 שניות": 1.5, "דקה": 1,
        "بعد خمسين دقيقة": 50, "ساعة ونص": 90, "ربع ساعة": 15, "ساعتين": 120,
        "خمسة وعشرين دقيقة": 25, "نص ساعة": 30,
        "через пятьдесят минут": 50, "полтора часа": 90, "полчаса": 30,
        "двадцать пять минут": 25, "два с половиной часа": 150, "на 25 минут": 25,
        "четверть часа": 15, "через час": 60,
    }
    for s, want in dur.items():
        got = tw.parse_duration(s)
        check(f"duration {s!r} = {want} min", got is not None and abs(got - want) < 1e-6, got)
    for s in ("minutes", "seven", "at 7", "tomorrow", "take the bread out", "fifteen people",
              "the 25", "דקות", "минут", ""):
        check(f"not a duration: {s!r}", tw.parse_duration(s) is None, tw.parse_duration(s))

    now = dt.datetime(2026, 10, 2, 15, 0)        # a Friday, 15:00
    clock = {  # (text, day text or "") -> HH:MM resolved from Friday 15:00
        ("6:30", ""): "18:30", ("at 6:30", ""): "18:30", ("seven am", ""): "07:00",
        ("at seven am", ""): "07:00", ("7 pm", ""): "19:00", ("noon", "tomorrow"): "12:00",
        ("at noon", ""): "12:00", ("midnight", ""): "00:00", ("9", "tomorrow"): "09:00",
        ("at 1", "on thursday"): "13:00", ("seven thirty", ""): "19:30",
        ("half past six", ""): "18:30", ("quarter to seven", ""): "18:45",
        ("in the evening", ""): "18:00", ("8pm", "friday"): "20:00",
        ("at nine am", "tomorrow"): "09:00", ("seven", "tomorrow morning"): "07:00",
        ("18:30", ""): "18:30", ("seven o'clock", ""): "19:00",
        ("לשבע בבוקר", ""): "07:00", ("בשש וחצי", ""): "18:30", ("ב-18:30", ""): "18:30",
        ("רבע לשבע", ""): "18:45", ("בצהריים", "מחר"): "12:00", ("בשמונה בערב", ""): "20:00",
        ("الساعة سبعة الصبح", ""): "07:00", ("سبعة ونص", ""): "19:30",
        ("в семь утра", ""): "07:00", ("в 6:30", ""): "18:30", ("в половине седьмого", ""): "18:30",
        ("в полдень", "завтра"): "12:00",
    }
    for (c, d), want in clock.items():
        ck = tw.parse_clock(c)
        dy = tw.parse_day(d) if d else None
        res = tw.resolve(None, ck, dy, now) if ck else None
        got = res and res.get("time")
        check(f"clock {c!r} {d!r} = {want}", got == want, (ck, dy, res))
    for s in ("fifty minutes", "tomorrow", "call mom", "25", "סבתא", ""):
        check(f"not a clock: {s!r}", tw.parse_clock(s) is None, tw.parse_clock(s))

    fri = dt.datetime(2026, 10, 2, 15, 0).date()
    days = {"tomorrow": fri + dt.timedelta(days=1), "today": fri, "on friday": fri,
            "on thursday": dt.date(2026, 10, 8), "next monday": dt.date(2026, 10, 5),
            "the day after tomorrow": dt.date(2026, 10, 4), "מחר": dt.date(2026, 10, 3),
            "ביום חמישי": dt.date(2026, 10, 8), "מחרתיים": dt.date(2026, 10, 4),
            "בשבת": dt.date(2026, 10, 3), "بكرا": dt.date(2026, 10, 3),
            "يوم الخميس": dt.date(2026, 10, 8), "завтра": dt.date(2026, 10, 3),
            "в четверг": dt.date(2026, 10, 8), "tomorrow morning": dt.date(2026, 10, 3),
            "october 14": dt.date(2026, 10, 14), "the 14th": dt.date(2026, 10, 14)}
    for s, want in days.items():
        dy = tw.parse_day(s)
        res = tw.resolve(None, None, dy, now) if dy else None
        check(f"day {s!r} = {want}", res and res.get("date") == want, (dy, res))
    # A weekday that has passed rolls to next week, never dropped: Friday 00:30, "Thursday".
    after_midnight = dt.datetime(2026, 10, 2, 0, 30)
    res = tw.resolve(None, tw.parse_clock("10"), tw.parse_day("thursday"), after_midnight)
    check("after midnight, Thursday is next week's", res["date"] == dt.date(2026, 10, 8), res)
    res = tw.resolve(None, tw.parse_clock("at 9"), tw.parse_day("friday"),
                     dt.datetime(2026, 10, 2, 10, 0))
    check("today's weekday with its time gone: next week", res["date"] == dt.date(2026, 10, 9),
          res)
    res = tw.resolve(None, tw.parse_clock("at 9"), tw.parse_day("friday"),
                     dt.datetime(2026, 10, 2, 7, 0))
    check("today's weekday with its time ahead: today", res["date"] == dt.date(2026, 10, 2), res)
    res = tw.resolve(None, tw.parse_clock("7"), None, dt.datetime(2026, 10, 2, 22, 0))
    check("alarm 'seven' at 22:00 is 07:00 tomorrow",
          res["date"] == dt.date(2026, 10, 3) and res["time"] == "07:00", res)

    # The pre-check: only time words open the question.
    for s in ("remind me in fifty minutes", "set a timer for 25 minutes", "wake me up at 7",
              "add lunch with dana on thursday at 1", "תזכירי לי מחר", "תשימי טיימר",
              "ذكريني بعد ساعة", "поставь будильник", "put dentist on my calendar",
              "remind me to text tom in ten minutes", "how much time is left on the timer"):
        check(f"time words: {s!r}", tw.mentions_time(s))
    for s in ("what's the weather", "play some music", "what time is it", "call dana",
              "מה השעה", "كم الساعة هلق", "который час", "tell me a joke",
              "send Miriam a message that I will be late", "open the calculator",
              "text dana i will be there at 8", "תשלחי לרותי שאני מגיעה בעוד חצי שעה"):
        check(f"no time words: {s!r}", not tw.mentions_time(s))
    check("no em dash in any time line",
          not any("—" in v for b in tw.LINES.values() for v in b.values()))
    check("every time line in English, Hebrew, Arabic and Russian",
          all(set(b) >= {"english", "hebrew", "arabic", "russian"} for b in tw.LINES.values()),
          [k for k, b in tw.LINES.items() if not set(b) >= {"english", "hebrew", "arabic",
                                                             "russian"}])


# ================================================================== durations
def t_durations():
    print("\nt_durations")
    tf.fresh()
    r, j = ask("remind me in fifty minutes to take the bread out", "reminder", intent="timer",
               duration="fifty minutes", about="to take the bread out")
    pend = mac.pending_timers()
    check("fifty minutes is 50, not 45", r["did"] == "timer_set" and len(pend) == 1
          and 49.5 <= pend[0]["in_minutes"] <= 50.1, (r["did"], r.get("say"), pend))
    check("said exactly what was set",
          r.get("say") == "I'll remind you in 50 minutes to take the bread out.", r.get("say"))
    check("one Jev round trip (what it is about rode in understand)", j.calls == 1, j.calls)
    check("the time question rode in understand()", Q in j.asked[0])

    tf.fresh()
    r, _ = ask("set a timer for 25 minutes", "timer", intent="timer", duration="25 minutes")
    check("a 25-minute timer, no 'In how long?'", r["did"] == "timer_set"
          and r.get("say") == "Timer for 25 minutes.", (r["did"], r.get("say")))
    check("the detail says it is a timer", (r.get("detail") or {}).get("kind") == "timer",
          r.get("detail"))

    tf.fresh()
    r, _ = ask("set a timer for two hundred minutes", "timer", intent="timer",
               duration="two hundred minutes")
    pend = mac.pending_timers()
    check("200 minutes stays 200 minutes, said in minutes",
          pend and 199.5 <= pend[0]["in_minutes"] <= 200.1 and "200 minutes" in r["say"]
          and "hour" not in r["say"], (pend, r.get("say")))

    tf.fresh()
    r, _ = ask("set a timer for an hour and a half", "timer", intent="timer",
               duration="an hour and a half")
    pend = mac.pending_timers()
    check("an hour and a half is 90 minutes", pend and 89.5 <= pend[0]["in_minutes"] <= 90.1
          and "hour" in r["say"], (pend, r.get("say")))

    tf.fresh()
    r, _ = ask("set a timer for 90 seconds", "timer", intent="timer", duration="90 seconds")
    pend = mac.pending_timers()
    check("90 seconds", pend and 1.4 <= pend[0]["in_minutes"] <= 1.6
          and r.get("say") == "Timer for 90 seconds.", (pend, r.get("say")))

    tf.fresh()
    r, _ = ask("תזכירי לי בעוד שעה וחצי לכבות את התנור", "reminder", intent="timer",
               lang="hebrew", duration="בעוד שעה וחצי", about="לכבות את התנור")
    pend = mac.pending_timers()
    check("HE: an hour and a half, said in Hebrew", r["did"] == "timer_set" and pend
          and 89.5 <= pend[0]["in_minutes"] <= 90.1 and "לכבות את התנור" in r["say"]
          and not any("a" <= ch <= "z" for ch in r["say"].lower()), (pend, r.get("say")))

    tf.fresh()
    r, _ = ask("תזכירי לי בעוד חמישים דקות להוציא את הלחם", "reminder", intent="timer",
               lang="hebrew", duration="בעוד חמישים דקות", about="להוציא את הלחם")
    check("HE: fifty minutes is 50", "50" in r.get("say", "") and mac.pending_timers()
          and 49.5 <= mac.pending_timers()[0]["in_minutes"] <= 50.1, r.get("say"))

    tf.fresh()
    r, _ = ask("ذكريني بعد ساعة ونص", "reminder", intent="timer", lang="arabic",
               duration="بعد ساعة ونص")
    check("AR: an hour and a half", mac.pending_timers()
          and 89.5 <= mac.pending_timers()[0]["in_minutes"] <= 90.1, r.get("say"))
    tf.fresh()
    r, _ = ask("поставь таймер на двадцать пять минут", "timer", intent="timer",
               lang="russian", duration="на двадцать пять минут")
    check("RU: 25 minutes, 'Таймер на 25 минут.'", r.get("say") == "Таймер на 25 минут.",
          r.get("say"))

    # Jev did not get to pick (the question said none) but the intent is a timer: the
    # number still comes from code, from her words, never from a closed list.
    tf.fresh()
    r, _ = tf.turn("remind me in fifty minutes to take the bread out", intent="timer",
                   extra={"when_minutes": "45"})
    pend = mac.pending_timers()
    check("fallback: still 50 from her words, the closed list is not read",
          pend and 49.5 <= pend[0]["in_minutes"] <= 50.1, (r.get("say"), pend))
    tf.fresh()
    r, _ = ask("set a timer", "timer", intent="timer")
    check("a timer with no amount asks for how long", r["did"] == "need_when"
          and r.get("say") == "For how long?", (r["did"], r.get("say")))
    mac.cancel_timers()


# ================================================================== clock times, days
def t_clock():
    print("\nt_clock")
    tf.fresh()
    r, j = ask("remind me tomorrow at 9 to call mom", "reminder", intent="timer",
               clock="at 9", day="tomorrow", about="to call mom")
    want = dt.datetime.combine(dt.date.today() + dt.timedelta(days=1), dt.time(9, 0))
    due = due_of(r)
    check("tomorrow at 9:00", r["did"] == "timer_set" and due
          and abs((due - want).total_seconds()) < 2, (r["did"], due))
    check("said: tomorrow at 9:00 to call Mom",
          (r.get("say") or "").lower() == "i'll remind you tomorrow at 9:00 to call mom.",
          r.get("say"))

    tf.fresh()
    r, _ = ask("remind me at 6:30 to call mom", "reminder", intent="timer", clock="at 6:30",
               about="to call mom")
    due = due_of(r)
    now = dt.datetime.now()
    want = min(next_clock(6, 30, now), next_clock(18, 30, now))
    check("6:30 with no am/pm: the next 6:30 or 18:30", due and abs((due - want).total_seconds()) < 2,
          (due, want))
    hhmm = f"{want.hour}:{want.minute:02d}"
    check("said with the clock time", hhmm in (r.get("say") or ""), r.get("say"))

    tf.fresh()
    r, _ = ask("remind me at noon to take my pills", "reminder", intent="timer",
               clock="at noon", about="to take my pills")
    due = due_of(r)
    check("at noon", due and due.hour == 12 and due.minute == 0, due)

    tf.fresh()
    r, _ = ask("remind me in the evening to water the plants", "reminder", intent="timer",
               clock="in the evening", about="to water the plants")
    due = due_of(r)
    check("in the evening: 18:00", due and due.hour == 18, due)

    tf.fresh()
    r, _ = ask("remind me on friday at 10 to call the bank", "reminder", intent="timer",
               clock="at 10", day="on friday", about="to call the bank")
    due = due_of(r)
    check("on Friday at 10:00", due and due.weekday() == 4 and due.hour == 10, due)
    mac.cancel_timers()


# ================================================================== alarms
def t_alarm():
    print("\nt_alarm")
    tf.fresh()
    r, _ = ask("set an alarm for seven am", "alarm", intent="timer", clock="seven am")
    due = due_of(r)
    check("an alarm at the next 07:00", r["did"] == "timer_set" and due
          and abs((due - next_clock(7, 0)).total_seconds()) < 2, (r.get("say"), due))
    check("said: an alarm, at 7:00, and honest that MicMic says it out loud",
          "Alarm" in r["say"] and "7:00" in r["say"] and "MicMic" in r["say"], r.get("say"))
    check("the detail says alarm", (r.get("detail") or {}).get("kind") == "alarm", r.get("detail"))
    r, j = tf.turn("make it ten minutes later", follow="reminder 10 minutes later",
                   extra={Q: "none"})
    due = due_of(r)
    check("follow-up: ten minutes later is 7:10, one alarm pending",
          r["did"] == "timer_set" and "7:10" in r["say"] and len(mac.pending_timers()) == 1
          and due and due.hour == 7 and due.minute == 10, (r.get("say"), due))
    r, _ = tf.turn("actually make it seven thirty", follow="reminder at 19:30")
    due = due_of(r)
    check("'seven thirty' after a 7am alarm is 7:30, not 19:30",
          due and due.hour == 7 and due.minute == 30 and "7:30" in r["say"], (r.get("say"), due))

    tf.fresh()
    r, _ = ask("תשימי שעון מעורר לשבע בבוקר", "alarm", intent="timer", lang="hebrew",
               clock="לשבע בבוקר")
    due = due_of(r)
    check("HE: alarm at 7:00, not a 480-minute timer", due and due.hour == 7 and due.minute == 0
          and "7:00" in r["say"], (r.get("say"), due))
    r, _ = tf.turn("תזיזי את זה עשר דקות קדימה", follow="reminder 10 minutes later",
                   lang="hebrew")
    check("HE: moved to 7:10", "7:10" in (r.get("say") or ""), r.get("say"))

    # Live, 2026-10-02 (repeat 2): "move it ten minutes forward" split 0.45 later / 0.48
    # earlier. A shift was plainly asked (0.93); neither half reached the gate, and the
    # time question then set a SECOND alarm in ten minutes. Later and earlier are one
    # field for the gate; the stronger direction is the value.
    tf.fresh()
    ask("תשימי שעון מעורר לשבע בבוקר", "alarm", intent="timer", lang="hebrew",
        clock="לשבע בבוקר")
    split = {"choice": "reminder 10 minutes earlier", "confidence": 0.48, "probabilities": {
        "none": 0.02, "reminder 10 minutes earlier": 0.48, "reminder 10 minutes later": 0.45,
        "reminder at 07:30": 0.02}}
    r, _ = tf.turn("תזיזי את זה עשר דקות קדימה", follow=split, lang="hebrew",
                   extra={Q: "alarm", **spans(duration="עשר דקות")})
    due = due_of(r)
    # Owner decision 2026-10-02: her "קדימה" is LATER, whatever the split, so this one
    # (earlier 0.48) is 7:10 now; it was 6:50 when the stronger half decided.
    check("a split shift is still a change to the alarm, never a second alarm",
          len(mac.pending_timers()) == 1 and due and due.hour == 7 and due.minute == 10
          and "7:10" in (r.get("say") or ""), (r.get("say"), mac.pending_timers()))

    tf.fresh()
    r, _ = ask("wake me up thirty minutes from now", "alarm", intent="timer",
               duration="thirty minutes from now")
    pend = mac.pending_timers()
    check("an alarm by an amount of time", pend and 29.5 <= pend[0]["in_minutes"] <= 30.1,
          (r.get("say"), pend))
    tf.fresh()
    r, _ = ask("set an alarm", "alarm", intent="timer")
    check("an alarm with no time asks for what time", r["did"] == "need_when"
          and r.get("say") == "For what time?", (r["did"], r.get("say")))
    mac.cancel_timers()


# ================================================================== forward / back
def _shift(n: int, which: str, conf: float = 0.97) -> dict:
    """The follow-up answer with the shift read as `which` ("later"/"earlier")."""
    key = f"reminder {n} minutes {which}"
    other = f"reminder {n} minutes {'earlier' if which == 'later' else 'later'}"
    return {"choice": key, "confidence": conf,
            "probabilities": {key: conf, other: round(1 - conf - 0.01, 2), "none": 0.01}}


def _alarm_at_seven(lang: str = "english"):
    tf.fresh()
    if lang == "hebrew":
        ask("תשימי שעון מעורר לשבע בבוקר", "alarm", intent="timer", lang="hebrew",
            clock="לשבע בבוקר")
    else:
        ask("set an alarm for seven am", "alarm", intent="timer", clock="seven am")


def t_direction_words():
    """Owner decision 2026-10-02 (bench hard-multi-hard-011): moving a reminder, alarm
    or timer "קדימה" (or "forward") is LATER, and "אחורה" is EARLIER, decided in code
    from her words whatever Jev's earlier/later split. Live, "תזיזי את זה עשר דקות
    קדימה" read earlier 0.97 and the alarm went to 6:50. Other phrasings are as before."""
    print("\nt_direction_words")
    cases = (
        ("hebrew", "תזיזי את זה עשר דקות קדימה", "earlier", (7, 10), "HE קדימה read earlier"),
        ("hebrew", "תזיזי עשר דקות קדימה", "earlier", (7, 10), "HE short קדימה"),
        ("english", "move it ten minutes forward", "earlier", (7, 10), "EN forward read earlier"),
        ("hebrew", "תזיזי את זה עשר דקות אחורה", "later", (6, 50), "HE אחורה read later"),
        ("hebrew", "תזיזי את זה עשר דקות קדימה", "later", (7, 10), "HE קדימה read later stays"),
        # Not touched: every other phrasing goes the way Jev read it.
        ("english", "push it back ten minutes", "later", (7, 10), "EN 'push it back' stays later"),
        ("english", "make it ten minutes earlier", "earlier", (6, 50), "EN 'earlier' stays"),
        ("hebrew", "תקדימי את זה בעשר דקות", "earlier", (6, 50), "HE תקדימי stays earlier"),
    )
    for lang, utt, jev_read, (h, m), name in cases:
        _alarm_at_seven(lang)
        r, _ = tf.turn(utt, follow=_shift(10, jev_read), lang=lang if lang == "hebrew" else None,
                       extra={Q: "none"})
        due = due_of(r)
        hhmm = f"{h}:{m:02d}"
        check(f"{name}: {hhmm}, one alarm",
              r["did"] == "timer_set" and len(mac.pending_timers()) == 1 and due
              and (due.hour, due.minute) == (h, m) and hhmm in (r.get("say") or ""),
              (r.get("did"), r.get("say"), due))
        if lang == "hebrew":
            check(f"{name}: said in Hebrew", r.get("lang") == "hebrew", r.get("lang"))
    # The words alone never move anything: with no shift read, nothing is flipped.
    u = {"raw": {fu.QUESTION: {"choice": "none", "confidence": 0.95,
                               "probabilities": {"none": 0.95}}}}
    fold = {"options": {"reminder 10 minutes earlier": ("timer", "earlier", -10, None),
                        "reminder 10 minutes later": ("timer", "later", 10, None)},
            "items": [{"kind": "timer"}]}
    check("a 'none' reading stays none, whatever her words", fu.read(u, fold, "קדימה") is None)
    ev = {"raw": {fu.QUESTION: {"choice": "event 30 minutes earlier", "confidence": 0.9,
                                "probabilities": {"event 30 minutes earlier": 0.9}}}}
    evf = {"options": {"event 30 minutes earlier": ("event", "earlier", -30, None),
                       "event 30 minutes later": ("event", "later", 30, None)},
           "items": [{"kind": "event"}]}
    got = fu.read(ev, evf, "move the meeting forward half an hour")
    check("a calendar event is not touched (the decision is for reminders, alarms, timers)",
          got and got["field"] == "earlier" and got["value"] == -30, got)
    mac.cancel_timers()


# ================================================================== reminders in Reminders
def t_reminder_rows():
    print("\nt_reminder_rows")
    tf.fresh()
    r, _ = ask("add a reminder to buy milk, eggs, and bread", "reminder", intent="note",
               about="buy milk eggs and bread")
    rows = list(tf.REMS.values())
    check("no time: a row in Reminders with every item", r["did"] == "noted" and len(rows) == 1
          and all(w in rows[0]["name"] for w in ("milk", "eggs", "bread"))
          and not rows[0]["date"], rows)
    check("said what was added", r.get("say") == "I added a reminder: buy milk eggs and bread.",
          r.get("say"))
    check("no spoken timer for it", mac.pending_timers() == [])

    tf.fresh()
    r, _ = ask("remind me to call noa levi tomorrow", "reminder", intent="timer",
               day="tomorrow", about="to call noa levi")
    rows = list(tf.REMS.values())
    tmrw = dt.date.today() + dt.timedelta(days=1)
    check("a day but no time: a row due that day", r["did"] == "noted" and len(rows) == 1
          and rows[0]["date"] == tmrw.isoformat(), (r.get("say"), rows))
    check("said: for tomorrow", "tomorrow" in (r.get("say") or ""), r.get("say"))
    check("it is the last action, with its id", (router.MEM.last_action or {}).get("kind")
          == "reminder" and (router.MEM.last_action or {}).get("id", "").startswith("x-apple"),
          router.MEM.last_action)
    after = tmrw + dt.timedelta(days=1)
    key = f"reminder on {after.strftime('%A %-d %B')}"
    r, j = tf.turn("actually make it the day after", follow=key)
    check("the follow-up was asked about that reminder", tf.asked_follow(j)
          and key in j.asked[0][fu.QUESTION]["criteria"], list(j.asked[0].get(fu.QUESTION, {})
                                                              .get("criteria", {}))[:6])
    rows = list(tf.REMS.values())
    check("follow-up: the same row moved, no second one", len(rows) == 1
          and rows[0]["date"] == after.isoformat(), rows)
    check("said the new day, not 'tomorrow'", "tomorrow" not in (r.get("say") or "")
          and after.strftime("%A") in (r.get("say") or ""), r.get("say"))
    r, _ = tf.turn("cancel it", follow="remove the reminder")
    check("no delete path: says so, the row stays", len(tf.REMS) == 1
          and r["did"] == "reminder_not_removed", (r["did"], r.get("say")))


# ================================================================== the calendar, spoken
def t_calendar():
    print("\nt_calendar")
    real_ctx = router._screen_context
    router._screen_context = lambda *a, **k: None        # no screen to read
    try:
        tf.fresh()
        r, _ = ask("add lunch with dana on thursday at 1", "calendar", intent="unclear",
                   about="lunch with dana", day="on thursday", clock="at 1")
        thu = next_weekday(3)
        if dt.date.today().weekday() == 3 and dt.datetime.now().hour < 13:
            thu = dt.date.today()
        ev = (r.get("detail") or {}).get("event") or {}
        check("asks first, with the day and 13:00", r["did"] == "confirm_calendar"
              and ev.get("date") == thu.isoformat() and ev.get("start") == "13:00"
              and "13:00" in r["say"] and "Thursday" in r["say"], (r.get("say"), ev))
        check("titled in her words", ev.get("title", "").lower() == "lunch with dana", ev)
        check("nothing added before the yes", not tf.CAL)
        r, _ = tf.yes()
        added = list(tf.CAL.values())
        check("yes: added Thursday 13:00", r["did"] == "calendar_added" and added
              and added[0]["date"] == thu.isoformat() and added[0]["start"] == "13:00", added)

        tf.fresh()
        r, _ = ask("put dentist on my calendar for thursday", "calendar", about="dentist",
                   day="for thursday")
        check("a day but no time: asks what time", r["did"] == "need_event_time"
              and "What time" in r["say"], (r["did"], r.get("say")))
        r, _ = tf.turn("at ten", extra={"is_answer": 0.95, "event_time": "10:00"})
        check("then the usual 'shall I add', Thursday 10:00", r["did"] == "confirm_calendar"
              and "10:00" in r["say"], (r["did"], r.get("say")))

        tf.fresh()
        r, _ = ask("add dentist appointment to my calendar", "calendar",
                   about="dentist appointment")
        check("no day, no time, no screen: asks when", r["did"] == "need_event_time"
              and "When" in r["say"] and r.get("asked_back"), (r["did"], r.get("say")))
        r, _ = tf.turn("thursday at 10", extra={"is_answer": 0.95})
        check("her answer has the day and time: 'shall I add', Thursday 10:00",
              r["did"] == "confirm_calendar" and "10:00" in r["say"]
              and "Thursday" in r["say"], (r["did"], r.get("say")))

        tf.fresh()
        r, _ = ask("תוסיפי תור לרופא שיניים ליומן", "calendar", lang="hebrew",
                   about="תור לרופא שיניים")
        check("HE: asks when, in Hebrew", r["did"] == "need_event_time"
              and not any("a" <= ch <= "z" for ch in r["say"].lower()), r.get("say"))

        # After midnight on a Friday, "Thursday" is next week's, not dropped.
        tf.fresh()
        real_now = tw._now
        tw._now = lambda: dt.datetime(2026, 10, 2, 0, 30).timestamp()
        try:
            r, _ = ask("add dentist on thursday at 10", "calendar", about="dentist",
                       day="on thursday", clock="at 10")
        finally:
            tw._now = real_now
        ev = (r.get("detail") or {}).get("event") or {}
        check("a passed weekday rolls to next week", r["did"] == "confirm_calendar"
              and ev.get("date") == "2026-10-08", (r.get("say"), ev))
    finally:
        router._screen_context = real_ctx


def t_screen_calendar_kept():
    print("\nt_screen_calendar_kept")
    tf.fresh()
    tf.LLM.event_json = tf.EVENT % tf._later(1).isoformat()
    r, _ = tf.turn("add this to my calendar", intent="screen",
                   extra={"screen_task": "add_to_calendar", Q: "calendar",
                          **spans(about="this")})
    check("'add this to my calendar' still reads the screen",
          r["did"] == "confirm_calendar" and "Dentist" in r["say"], (r["did"], r.get("say")))

    # The screen says "Thursday 10:00" and the run is past midnight into Friday: the
    # model resolved it to yesterday. That weekday is next week's, not dropped.
    today = dt.date.today()
    yday = today - dt.timedelta(days=1)
    ctx = {"permissions": {"accessibility": True}, "frontmost": {"app": "Safari"},
           "selected": "", "focused": {},
           "visible": {"text": f"Dentist appointment {yday.strftime('%A')} 10:00."},
           "page": {}}
    tf.LLM.event_json = ('{"title": "Dentist appointment", "date": "%s", "start": "10:00", '
                         '"end": "", "location": ""}' % yday.isoformat())
    ev = router._event_from_screen(ctx, "english")
    check("a weekday on screen that has passed rolls to next week",
          ev and ev["date"] == (yday + dt.timedelta(days=7)).isoformat(), ev)
    ctx["visible"]["text"] = f"Dentist appointment {yday.isoformat()} 10:00."
    ev = router._event_from_screen(ctx, "english")
    check("an explicit date in the past is still no event", ev is None, ev)


# ================================================================== time left, cancel
def t_time_left():
    print("\nt_time_left")
    tf.fresh()
    ask("set a timer for 15 minutes", "timer", intent="timer", duration="15 minutes")
    r, _ = ask("how much time is left on the timer", "time_left", intent="look_up")
    check("time left comes from the timer itself", r["did"] == "timer_recall"
          and "minute" in r["say"] and ("15" in r["say"] or "14" in r["say"]),
          (r["did"], r.get("say")))
    r, _ = ask("cancel the timer", "cancel", intent="stop")
    check("cancel the timer: stopped", r["did"] == "timer_cancelled"
          and mac.pending_timers() == [], (r["did"], r.get("say")))
    r, _ = ask("how much time is left on the timer", "time_left", intent="look_up")
    check("nothing running: says so", r["did"] == "timer_recall"
          and r["say"] == "There is no timer running.", r.get("say"))


# ================================================================== the gate
def t_gate():
    print("\nt_gate")
    tf.fresh()
    for s, intent in (("what's the weather", "look_up"), ("play some music", "music"),
                      ("what time is it", "look_up"), ("מה השעה", "look_up")):
        _, j = tf.turn(s, intent=intent)
        check(f"no time words, no time question: {s!r}",
              Q not in j.asked[0] and not any(k.startswith("time_") for k in j.asked[0]))
    # A message or call that only mentions a time goes out exactly as before: live,
    # "text david that dinner is at eight" (two Davids) went 2/3 -> 0/3 with the time
    # question riding beside the contact choice.
    for s_, it in (("text dana i'll be there at 8", "message"),
                   ("text gal i will be there in twenty minutes", "message"),
                   ("תשלחי לרותי שאני מגיעה בעוד חצי שעה", "message"),
                   ("send a message to dana and say the meeting is at three", "message")):
        tf.fresh()
        _, j = tf.turn(s_, intent=it)
        check(f"a message that mentions a time: no time question: {s_!r}",
              Q not in j.asked[0] and not any(k.startswith("time_") for k in j.asked[0]))
    tf.fresh()
    _, j = tf.turn("remind me to text tom weiss in ten minutes", intent="message")
    check("a reminder to text someone still asks it", Q in j.asked[0])
    tf.fresh()
    _, j = tf.turn("dana is coming at eight so we should start cooking", intent="chitchat")
    check("a time word opens the question", Q in j.asked[0])
    r, _ = tf.turn("text dana i'll be there at 8", intent="message",
                   extra={"contact": "Dana Cohen", "contact_is_named": 0.95,
                          "message_has_content": 0.95})
    check("and 'none' leaves a message a message", r["did"] not in ("timer_set", "noted",
                                                                    "confirm_calendar"),
          r["did"])
    tf.fresh()
    r, _ = ask("remind me to text tom weiss in ten minutes", "reminder", intent="message",
               duration="in ten minutes", about="to text tom weiss")
    check("a reminder to text someone is a reminder, nothing sent",
          r["did"] == "timer_set" and not tf.tm.SENT and not tf.tm.WA, (r["did"], tf.tm.SENT))
    mac.cancel_timers()


# ================================================================== a restart
def _restart():
    """MicMic quits: its threads die with it, the state file stays. Nothing is
    cancelled through mac (that would be her cancelling them)."""
    for t in list(mac._TIMERS):
        t["timer"].cancel()
    mac._TIMERS.clear()


def _file_rows() -> list[dict]:
    import json
    try:
        return json.loads(mac._timer_file().read_text()).get("timers", [])
    except Exception:  # noqa: BLE001
        return []


def t_persist():
    print("\nt_persist")
    import json
    tf.fresh()
    mac._timer_file().write_text(json.dumps({"version": 1, "timers": []}))
    ask("remind me tomorrow at 9 to call mom", "reminder", intent="timer",
        clock="at 9", day="tomorrow", about="to call mom")
    ask("set an alarm for seven am", "alarm", intent="timer", clock="seven am")
    ask("set a timer for 25 minutes", "timer", intent="timer", duration="25 minutes")
    rows = _file_rows()
    check("every spoken timer, reminder and alarm is in the state file",
          sorted(r.get("kind") for r in rows) == ["alarm", "reminder", "timer"], rows)
    check("with its due time, words, kind, id and language",
          all({"id", "at", "text", "kind", "language"} <= set(r) for r in rows), rows)
    ids = {t["id"]: t["at"] for t in mac._TIMERS}

    _restart()
    check("after the restart nothing is pending in memory", mac.pending_timers() == [])
    said_before = len(tf.tm.SAID)
    out = router.restore_timers(delay=0)
    pend = {t["id"]: t["at"] for t in mac._TIMERS}
    check("restored: the same three, same ids and due times", pend == ids, (pend, ids))
    check("nothing was said for ones still to come", len(tf.tm.SAID) == said_before
          and out == [], out)
    out2 = router.restore_timers(delay=0)
    check("restoring twice arms nothing twice", len(mac._TIMERS) == 3 and out2 == [],
          len(mac._TIMERS))

    # A follow-up and a cancel keep the file in step.
    rid = next(t["id"] for t in mac._TIMERS if t["kind"] == "timer")
    mac.cancel_timer(rid)
    check("a cancelled one leaves the file", rid not in {r["id"] for r in _file_rows()},
          _file_rows())

    # It goes off: once, and out of the file.
    tf.fresh()
    mac._timer_file().write_text(json.dumps({"version": 1, "timers": []}))
    row = mac.set_timer(0.002, "take the pills", "english")
    t0 = time.time()
    while row["id"] in {r["id"] for r in _file_rows()} and time.time() - t0 < 5:
        time.sleep(0.05)
    time.sleep(0.1)
    fired = [x for x in tf.tm.SAID if "take the pills" in x[1]]
    check("it went off once and left the file", len(fired) == 1
          and row["id"] not in {r["id"] for r in _file_rows()}, (fired, _file_rows()))
    check("a fire that finds its entry already claimed says nothing",
          mac._claim(row["id"]) is False)

    # While MicMic was off: due at 9:00 an hour ago.
    tf.fresh()
    _restart()
    import datetime as dtm
    nine = (dtm.datetime.now() - dtm.timedelta(minutes=5)).replace(second=0, microsecond=0)
    old = [{"id": "m1", "at": nine.timestamp(), "text": "call mom", "kind": "reminder",
            "language": "english", "lead": None},
           {"id": "m2", "at": nine.timestamp() - 60, "text": "", "kind": "alarm",
            "language": "hebrew", "lead": "שעון מעורר. השעה 7:00."},
           {"id": "f1", "at": time.time() + 3600, "text": "water the plants",
            "kind": "reminder", "language": "english", "lead": None}]
    mac._timer_file().write_text(json.dumps({"version": 1, "timers": old}))
    said_before = len(tf.tm.SAID)
    out = router.restore_timers(delay=0)
    t9 = f"{nine.hour}:{nine.minute:02d}"
    check("missed while off: said once, with the time and the words",
          any(x == f"While MicMic was off, you had a reminder at {t9}: call mom." for x in out),
          out)
    check("in her language", any("בזמן ש-MicMic היה כבוי" in x for x in out), out)
    check("and actually spoken", len(tf.tm.SAID) - said_before == len(out) == 2,
          tf.tm.SAID[said_before:])
    check("the missed ones left the file, the future one stayed and is armed",
          [r["id"] for r in _file_rows()] == ["f1"]
          and [t["id"] for t in mac._TIMERS] == ["f1"], (_file_rows(), mac._TIMERS))
    _restart()
    out = router.restore_timers(delay=0)
    check("a second launch never says them again", out == [], out)

    # A file that is not JSON: no crash, kept aside, and timers still work.
    tf.fresh()
    _restart()
    mac._timer_file().write_text("{not json")
    out = router.restore_timers(delay=0)
    check("a corrupt file: nothing restored, nothing said, no crash", out == [] and
          mac._TIMERS == [], out)
    check("its bytes are kept beside it", (mac._timer_file().parent /
          (mac._timer_file().name + ".corrupt")).read_text() == "{not json")
    mac.set_timer(30, "after the corrupt file", "english")
    check("and the next timer writes a good file",
          [r["text"] for r in _file_rows()] == ["after the corrupt file"], _file_rows())
    _restart()
    check("the file is written atomically (no partial file name left)",
          not list(mac._timer_file().parent.glob("timers.json.tmp*")))
    mac.cancel_timers()


def main():
    for t in (t_parser, t_durations, t_clock, t_alarm, t_direction_words, t_reminder_rows, t_calendar,
              t_screen_calendar_kept, t_time_left, t_gate, t_persist):
        try:
            t()
        except Exception as e:  # noqa: BLE001  (on a checkout without the fix)
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran to the end", False, repr(e))
    mac._osa = tf.tm._blocked_osa
    real = [c for c in tf.tm.LAUNCHED if c and c[0] in ("osascript", "screencapture")]
    check("0 osascript or screencapture escapes", not tf.tm.OSA and not real,
          f"{len(tf.tm.OSA)} + {len(real)}")
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAILED: {n}  {d[:200]}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
