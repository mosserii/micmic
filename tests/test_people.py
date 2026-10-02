#!/usr/bin/env python3
"""The right person, and nothing unsafe on a countdown. MicMic Bench v1 (2026-10-01,
BENCH_REPORT.md themes 1-2, Siri moments 1-4 and 9), scripted:

    cd <checkout> && uv run python tests/test_people.py

  1. "text dina that i'm outside" with no Dina in the book counted down to Dana Cohen.
  2. "call david" with two Davids dialled David Katz.
  3. A contact with no number was counted down to, or "I could not make the call".
  4. "text dana ..." overrode her saved WhatsApp choice for Dana.
  5. "call 911" called the emergency contact; "send my location to Tom, I'm in
     trouble" called her too; "you're useless" and a scam text were "You sound upset".
  6. A card number went out on the plain 6 s countdown.
  7. "never mind" at "what should it say?" was read back as the message; "no wait to
     noa" during a countdown sometimes only cancelled.
  8. "transfer 500 shekels" and "buy ... and pay with my card" started browser tasks.

Nothing leaves the machine: the stub wall is tests/test_micmic.py (loaded the way
tests/perf/bench.py loads it), Jev is scripted per sentence, Gemini is a fake, the
browser agent and FaceTime are recorders. No osascript, nothing reaches 127.0.0.1:8799.
Every name is made up.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-people-state-")
for _k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL"):
    os.environ.pop(_k, None)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "perf"))
import bench                                   # noqa: E402

tm = bench.load_wall()
router, mac = tm.router, tm.mac
from savta import followup as fu               # noqa: E402
from savta import prefs                        # noqa: E402
from savta import profile as prof              # noqa: E402
from savta import people                       # noqa: E402
from savta.actions import contacts as book     # noqa: E402

PASSED = 0
FAILED: list[tuple[str, str]] = []


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
# Made up. Two Davids; Nora has no number at all; no Dina, Michael or Gail.
BOOK_ROWS = [("Mom", "Mom", "+15550000001"), ("Dad", "Dad", "+15550000002"),
             ("Dana Cohen", "Dana", "+15550000003"), ("Gal Ben Ami", "Gal", "+15550000004"),
             ("Noa Levi", "Noa", "+15550000005"), ("David Katz", "David", "+15550000006"),
             ("David Stern", "David", "+15550000007"), ("Tom Weiss", "Tom", "+15550000009"),
             ("Nora Bloom", "Nora", ""), ("רותי כהן", "רותי", "+15550000012")]
BOOK = []
for _n, _f, _p in BOOK_ROWS:
    _r = {"name": _n, "first": _f, "phone": _p,
          "waid": (_p.lstrip("+") + "@s.whatsapp.net") if _p else "", "source": "fixture"}
    _r["keys"] = book.keys_for(_n) | book.keys_for(_f)
    BOOK.append(_r)
book.all_contacts = lambda force=False, wait=None: list(BOOK)
book.recent_chats = lambda limit=30: [dict(r) for r in BOOK[:limit]]
book._CACHE.update(at=time.time() * 10, rows=list(BOOK))


# ---------------------------------------------------------------- fakes
class FakeLLM:
    available = True

    def __init__(self):
        self.calls, self.busy_ms, self.last_ms, self.last_error = 0, 0.0, 0.0, None
        self.splits: dict[str, list[str]] = {}
        self.split_asked: list[str] = []

    def split_steps(self, utterance, language="english"):
        self.calls += 1
        self.split_asked.append(utterance)
        return self.splits.get(utterance)

    def answer(self, *a, **k):
        self.calls += 1
        return "[answer]"

    chat = text = answer


QUIET = ("not_applicable", "unspecified", "none", "nobody", "__none__", "any", "today",
         "unrevealed", "unchanged", "other", "not_said", "neither")


class ScriptedJev:
    """Quiet defaults, plus what each sentence is scripted to answer. `span` names the
    words a span question picks."""
    mode = "replay"

    def __init__(self, by: dict[str, dict]):
        self.by = by
        self.calls, self.cost_usd, self.busy_ms = 0, 0.0, 0.0
        self.asked: list[dict] = []

    def ask(self, state, qs):
        self.calls += 1
        self.asked.append(qs)
        said = next((state[k] for k in ("utterance", "her_answer", "she_said")
                     if isinstance(state.get(k), str)), "")
        script = self.by.get(said, {})
        out = {}
        for k, q in qs.items():
            t = q.get("type")
            if t == "choice":
                keys = list(q["criteria"])
                sel = next((c for c in QUIET if c in keys), keys[0])
                if k in ("span", "span_subject") or k.startswith("span_"):
                    sel = "__none__" if "__none__" in keys else sel
                out[k] = {"choice": sel, "confidence": 0.9,
                          "probabilities": {c: (0.9 if c == sel else 0.1 / max(1, len(keys) - 1))
                                            for c in keys}}
            elif t == "score":
                out[k] = {"score": 0.0}
            else:
                out[k] = {"noul": 0.95 if k == "is_complete" else 0.0}
        if "span" in qs and script.get("span"):
            out["span"] = {"choice": script["span"], "confidence": 0.9,
                           "probabilities": {script["span"]: 0.9}}
            out["exists"] = {"noul": 0.95}
        for k, v in script.items():
            if k in qs and k != "span":
                out[k] = (v if isinstance(v, dict)
                          else {"choice": v, "confidence": 0.9, "probabilities": {v: 0.9}}
                          if isinstance(v, str) else {"noul": v})
            if k == "distress" and k in qs:
                out[k] = {"score": v}
        return out

    def warmup(self, *a, **k):
        pass

    def asked_for(self, key: str) -> int:
        return sum(1 for q in self.asked if key in q)


LLM = FakeLLM()
WEB_TASKS: list[tuple] = []


def fake_web_run(j, llm, goal, start_url, **k):
    WEB_TASKS.append((goal, start_url))
    return {"did": "done", "steps": [], "url": start_url, "title": ""}


router.web.run = fake_web_run
router.web.where_to_start = lambda j, task: ("https://duckduckgo.com/html/?q=x", "search")


def fresh():
    tm.reset_state()
    router.MEM = router.Memory()
    router._HISTORY.clear()
    router._JUST_SENT.clear()
    router.LAST_EMERGENCY = None
    router.LLM_CLIENT = LLM
    LLM.splits.clear()
    LLM.split_asked.clear()
    WEB_TASKS.clear()
    prefs._update(lambda d: d.update(message_app={}, confirm_send=""))
    p = prof.load()
    p.update(emergency_contact="Dana Cohen", pinned=[])
    prof.save(p)
    router.CANCEL_WINDOW = 30.0


def say(j: ScriptedJev, utterance: str) -> dict:
    return router.handle(j, utterance, speak=False, client="native", activation="push")


def msg(contact: str, lang: str = "english", **kw) -> dict:
    return {**dict(intent="message", contact=contact, contact_is_named=0.95,
                   message_has_content=0.9, language=lang, same_person=0.9), **kw}


def pending_to() -> str | None:
    return (router.PENDING or {}).get("to")


def awaiting() -> dict:
    return router.AWAITING or {}


# ---------------------------------------------------------------- 1. a name not in the book
def t_name_not_in_book():
    print("\nt_name_not_in_book")
    for lang, utt, body in (("english", "text dina that i'm outside", "i'm outside"),
                            ("hebrew", "תשלחי לדינה שאני מאחרת", "שאני מאחרת")):
        fresh()
        # Jev picks the nearest row and even says it is the same person: the bench saw
        # same_person clear its 0.40 gate for Dina against Dana Cohen.
        j = ScriptedJev({utt: msg("Dana Cohen", lang, span=body)})
        with tm.gates_on():
            r = say(j, utt)
        s = r.get("say") or ""
        check(f"{lang}: nothing armed or read back for a yes",
              r["did"] == "need_who" and router.PENDING is None
              and awaiting().get("need") == "who", (r["did"], s, router.PENDING))
        want = ("I don't have Dina in your contacts. Did you mean Dana Cohen?"
                if lang == "english" else "דינה")
        check(f"{lang}: she hears the name is not in her book, and who it sounds like",
              want in s and ("Dana" in s or "דנה" in s), s)
        check(f"{lang}: the code decided: no same-person round trip", j.asked_for("same_person") == 0,
              j.asked_for("same_person"))
    # A yes is the answer: then the read-back and countdown, to Dana, with her words.
    fresh()
    utt = "text dina that i'm outside"
    by = {utt: msg("Dana Cohen", span="i'm outside"),
          "yes": dict(is_answer=0.95, yes_no="yes"),
          "no": dict(is_answer=0.95, yes_no="no")}
    with tm.gates_on():
        say(ScriptedJev(by), utt)
        r = say(ScriptedJev(by), "yes")
        check("'yes': the message goes to Dana Cohen, words kept",
              r["did"] in ("sending", "confirm_send") and (pending_to() == "Dana Cohen"
                                                          or awaiting().get("contact") == "Dana Cohen")
              and "outside" in str((router.PENDING or {}).get("text") or awaiting().get("body")),
              (r["did"], r.get("say"), router.PENDING, awaiting()))
        router._cancel_pending()
    fresh()
    with tm.gates_on():
        say(ScriptedJev(by), utt)
        r = say(ScriptedJev(by), "no")
        check("'no': asked who, nothing armed", r["did"] == "need_who" and router.PENDING is None,
              (r["did"], r.get("say")))
    # Unknown with no likeness at all: she is told the name is not in her book.
    fresh()
    utt = "send a message to michael saying running late"
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Noa Levi", same_person=0.1, span="running late")}), utt)
    check("'michael': not in her contacts, said so, asked who",
          r["did"] == "need_who" and "I don't have Michael" in (r.get("say") or "")
          and router.PENDING is None, (r["did"], r.get("say")))
    fresh()
    with tm.gates_on():
        r = say(ScriptedJev({utt: dict(intent="message", contact="nobody", contact_is_named=0.95,
                                       message_has_content=0.9)}), utt)
    check("'michael' when Jev itself finds nobody: still told it is not in her contacts",
          r["did"] == "need_who" and "I don't have Michael" in (r.get("say") or ""),
          (r["did"], r.get("say")))
    # The near-misses that work today keep working, with no extra question.
    for utt, who in (("text dana co hen that im running late", "Dana Cohen"),
                     ("text gail ben ami that the meeting moved", "Gal Ben Ami"),
                     ("תשלחי לדנה שאני בדרך הביתה", "Dana Cohen")):
        fresh()
        j = ScriptedJev({utt: msg(who, "hebrew" if "ת" in utt else "english",
                                  span="the meeting moved" if "gail" in utt else "im running late")})
        with tm.gates_on():
            r = say(j, utt)
        check(f"{utt!r}: counts down to {who}", r["did"] in ("sending", "confirm_send")
              and (pending_to() == who or awaiting().get("contact") == who), (r["did"], r.get("say")))
        router._cancel_pending()
    check("units", people.check("text dina hi", "Dana Cohen")["match"] == "alike"
          and people.check("תשלחי לדנה", "Dana Cohen")["match"] == "exact"
          and people.check("תשלחי לדינה", "Dana Cohen")["match"] == "alike"
          and people.check("text my sister", "Noa Levi")["match"] == "none"
          and people.check("text mum that im home", "Mom")["match"] == "exact"
          and people.check("text my mother", "Mom")["match"] == "exact"
          and people.check("text mom", "Mom")["match"] == "exact"
          and people.check("hi", "Zed Unknown")["match"] == "unknown")


# ---------------------------------------------------------------- 2. two Davids
def t_two_davids():
    print("\nt_two_davids")
    for lang, utt, ans in (("english", "call david", "stern"), ("hebrew", "תתקשרי לדויד", "שטרן")):
        fresh()
        by = {utt: dict(intent="call", contact="David Katz", contact_is_named=0.95,
                        language=lang),
              ans: dict(is_answer=0.95, contact="David Stern")}
        with tm.gates_on():
            r = say(ScriptedJev(by), utt)
            s = r.get("say") or ""
            check(f"{lang}: '{utt}' asks which David, nobody is called",
                  r["did"] == "need_which" and not tm.CALLED and "Katz" in s and "Stern" in s
                  and ("או" in s if lang == "hebrew" else " or " in s), (r["did"], s, tm.CALLED))
            r = say(ScriptedJev(by), ans)
            check(f"{lang}: her answer places the call to David Stern",
                  r["did"] == "calling" and tm.CALLED and tm.CALLED[-1][0] == "David Stern",
                  (r["did"], r.get("say"), tm.CALLED))
    fresh()
    utt = "text david that dinner is at eight"
    by = {utt: msg("David Katz", span="dinner is at eight"),
          "katz": dict(is_answer=0.95, contact="David Katz")}
    with tm.gates_on():
        r = say(ScriptedJev(by), utt)
        check("a message to 'david' asks which, before any countdown",
              r["did"] == "need_which" and router.PENDING is None, (r["did"], r.get("say")))
        r = say(ScriptedJev(by), "katz")
        check("'katz': then to David Katz, with her words",
              pending_to() == "David Katz" and "eight" in (router.PENDING or {}).get("text", ""),
              (r["did"], router.PENDING))
        router._cancel_pending()
    fresh()
    utt = "call david stern"
    with tm.gates_on():
        r = say(ScriptedJev({utt: dict(intent="call", contact="David Stern",
                                       contact_is_named=0.95)}), utt)
    check("'call david stern' is not asked about", r["did"] == "calling"
          and tm.CALLED[-1][0] == "David Stern", (r["did"], tm.CALLED))
    fresh()
    utt = "call dina"
    with tm.gates_on():
        r = say(ScriptedJev({utt: dict(intent="call", contact="Dana Cohen",
                                       contact_is_named=0.95, same_person=0.9)}), utt)
    check("a call to a name that only sounds like Dana's asks, and dials nobody",
          r["did"] == "need_who" and not tm.CALLED and "Dina" in (r.get("say") or ""),
          (r["did"], r.get("say"), tm.CALLED))


# ---------------------------------------------------------------- 3. no number
def t_no_number():
    print("\nt_no_number")
    fresh()
    utt = "text nora bloom happy birthday"
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Nora Bloom", span="happy birthday")}), utt)
    check("a message to a contact with no number: said plainly, nothing armed or asked",
          r["did"] == "cannot_send_there" and router.PENDING is None and not awaiting()
          and "no phone number" in (r.get("say") or ""), (r["did"], r.get("say"), awaiting()))
    fresh()
    utt = "call nora bloom"
    with tm.gates_on():
        r = say(ScriptedJev({utt: dict(intent="call", contact="Nora Bloom",
                                       contact_is_named=0.95)}), utt)
    check("a call: 'has no phone number', and FaceTime is never opened",
          r["did"] == "call_failed" and "no phone number" in (r.get("say") or "")
          and not tm.CALLED, (r["did"], r.get("say"), tm.CALLED))
    # The other app is offered when the book has one: a WhatsApp-only contact asked for
    # by iMessage (not in the bench book: a row with an email and no number).
    fresh()
    BOOK.append({"name": "Ella Rosen", "first": "Ella", "phone": "", "waid": "",
                 "email": "ella@example.test", "source": "fixture",
                 "keys": book.keys_for("Ella Rosen")})
    try:
        utt = "send a whatsapp to ella that the keys are under the mat"
        with tm.gates_on():
            r = say(ScriptedJev({utt: msg("Ella Rosen", channel="whatsapp",
                                          span="the keys are under the mat")}), utt)
        check("WhatsApp unreachable, a text is: offered, and only on a yes",
              r["did"] == "confirm_send" and awaiting().get("channel") == "imessage"
              and router.PENDING is None, (r["did"], r.get("say"), awaiting()))
    finally:
        BOOK.pop()


# ---------------------------------------------------------------- 4. text keeps WhatsApp
def t_text_keeps_whatsapp():
    print("\nt_text_keeps_whatsapp")
    for utt in ("text dana say im here", "text tom say running late"):
        fresh()
        who = "Dana Cohen" if "dana" in utt else "Tom Weiss"
        prefs.set_app("whatsapp", who)
        with tm.gates_on():
            r = say(ScriptedJev({utt: msg(who, channel="imessage",
                                          span=utt.split("say ", 1)[1])}), utt)
        ch = (router.PENDING or {}).get("channel") or awaiting().get("channel")
        check(f"{utt!r}: her saved WhatsApp choice holds, no 'as you asked' note",
              ch == "whatsapp" and "text messages" not in (r.get("say") or ""),
              (r["did"], ch, r.get("say")))
        router._cancel_pending()
    fresh()
    prefs.set_app("whatsapp", "Dana Cohen")
    utt = "send dana a text message that im here"
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Dana Cohen", channel="imessage", span="im here")}), utt)
    ch = (router.PENDING or {}).get("channel") or awaiting().get("channel")
    check("'a text message' is her choice this time", ch == "imessage", (r["did"], ch))
    router._cancel_pending()


# ---------------------------------------------------------------- 5. emergency
def t_emergency():
    print("\nt_emergency")
    for utt, num in (("call 911", "911"), ("תתקשרי ל-101", "101"), ("call 112 now", "112")):
        fresh()
        with tm.gates_on():
            r = say(ScriptedJev({utt: dict(intent="call", emergency=0.95, distress=1.9,
                                           language="hebrew" if "ת" in utt else "english")}),
                    utt)
        check(f"{utt!r}: nobody is called, and she hears the number to dial",
              r["did"] == "emergency_number" and not tm.CALLED and num in (r.get("say") or ""),
              (r["did"], r.get("say"), tm.CALLED))
    check("no em dash in the line", not any("—" in v for v in
                                            people.LINES["emergency_number"].values()))
    fresh()
    utt = "send my location to tom weiss right now, im in trouble"
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Tom Weiss", emergency=0.8, distress=1.8,
                                      span="im in trouble")}), utt)
    check("a message she asked for in trouble goes to Tom, nobody else is called",
          not tm.CALLED and (pending_to() == "Tom Weiss" or awaiting().get("contact") == "Tom Weiss"),
          (r["did"], r.get("say"), tm.CALLED))
    router._cancel_pending()
    fresh()
    utt = "send my location to tom weiss right now, im in trouble"
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Tom Weiss", emergency=0.8, distress=1.8,
                                      message_has_content=0.1)}), utt)
    check("in trouble with no words given: her own words go to Tom, not 'what should it say?'",
          not tm.CALLED and r["did"] in ("sending", "confirm_send")
          and (pending_to() == "Tom Weiss" or awaiting().get("contact") == "Tom Weiss"),
          (r["did"], r.get("say"), tm.CALLED))
    router._cancel_pending()
    fresh()
    utt = "text dad the man from the bank said i must send him 5000 shekels today"
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Dad", distress=1.8, money_involved=0.95,
                                      sounds_coached=0.9,
                                      span="the man from the bank said i must send him 5000 shekels today")}),
                utt)
    check("a scam text to Dad is drafted with the money line and extra time, not 'you sound upset'",
          r["did"] in ("sending", "confirm_send") and (r.get("detail") or {}).get("extra_time")
          and r["did"] != "offered_help", (r["did"], r.get("say")))
    router._cancel_pending()
    fresh()
    utt = "you're useless"
    j = ScriptedJev({utt: dict(intent="chitchat", distress=1.9, upset_with_micmic=0.9)})
    with tm.gates_on():
        r = say(j, utt)
    check("'you're useless' is not distress: no call offered",
          r["did"] != "offered_help" and not awaiting(), (r["did"], r.get("say")))
    check("the question rode in the one understanding request", j.asked_for("upset_with_micmic") == 1
          and j.calls == 1, (j.asked_for("upset_with_micmic"), j.calls))
    fresh()
    utt = "i am so scared and alone"
    j = ScriptedJev({utt: dict(intent="chitchat", distress=1.9)})
    with tm.gates_on():
        r = say(j, utt)
    check("real distress with nobody named: the call is offered",
          r["did"] == "offered_help" and awaiting().get("contact") == "Dana Cohen", (r["did"], r.get("say")))
    check("and nothing new was asked for it", j.asked_for("upset_with_micmic") == 0)
    check("units: a number alone is not a call",
          people.emergency_number("set the volume to 100") == ""
          and people.emergency_number("transfer 100 shekels") == "")


# ---------------------------------------------------------------- 5b. sad, with a task
def t_distress_with_a_task():
    """TAGS_DESIGN.md side finding (2026-10-02): sad or stressed requests with a clear
    task and nobody named crossed the 1.55 distress gate (measured 1.31 to 1.88 for sad,
    up to 2.00 for stressed) and were answered "You sound upset. Shall I call ...?"
    instead of being done. Each one must do exactly what its calm twin does."""
    print("\nt_distress_with_a_task")
    cases = (
        ("حزينة كتير اليوم، شغليلي فيروز", 1.85,
         dict(intent="music", language="arabic", media_kind="song_or_music",
              span_subject="فيروز", span_subject_exists=0.95)),
        ("i am so sad today, play fairuz", 1.85,
         dict(intent="music", media_kind="song_or_music",
              span_subject="fairuz", span_subject_exists=0.95)),
        ("oh no I can't find the tickets", 1.87, dict(intent="find_file")),
        ("find me a flight now, quick, i am so stressed", 1.95,
         dict(intent="do_online", span="a flight")),
        ("i am so sad, remind me to call mom", 1.8,
         dict(intent="timer", contact="Mom", contact_is_named=0.9)),
        ("what's the weather, i feel awful today", 1.7, dict(intent="look_up")),
    )
    for utt, dist, script in cases:
        fresh()
        with tm.gates_on():
            calm = say(ScriptedJev({utt: dict(script, distress=0.0)}), utt)
        router._cancel_pending()
        fresh()
        j = ScriptedJev({utt: dict(script, distress=dist)})
        with tm.gates_on():
            r = say(j, utt)
        check(f"{utt!r} at distress {dist}: the task is done, not 'shall I call'",
              r["did"] != "offered_help" and r["did"] == calm["did"]
              and awaiting().get("question") != "shall I call someone",
              (r["did"], calm["did"], r.get("say")))
        s, cs = r.get("say") or "", calm.get("say") or ""
        check(f"{utt!r}: her answer leads, word for word as the calm one",
              s.startswith(cs) if cs else True, (s, cs))
        check(f"{utt!r}: nothing extra asked for it", j.calls == 1 or j.calls == calm.get("jev_calls"),
              (j.calls, calm.get("jev_calls")))
        router._cancel_pending()
    # Very high distress: one short caring line AFTER the task, never a question.
    fresh()
    utt = "i am so sad today, play fairuz"
    with tm.gates_on():
        calm = say(ScriptedJev({utt: dict(intent="music", media_kind="song_or_music",
                                          span_subject="fairuz", span_subject_exists=0.95,
                                          distress=0.0)}), utt)
        router._cancel_pending()
        fresh()
        r = say(ScriptedJev({utt: dict(intent="music", media_kind="song_or_music",
                                       span_subject="fairuz", span_subject_exists=0.95,
                                       distress=1.95)}), utt)
    s, cs = r.get("say") or "", calm.get("say") or ""
    check("very high distress: the task, then one caring line that asks nothing",
          r["did"] == calm["did"] and s.startswith(cs) and len(s) > len(cs)
          and "?" not in s[len(cs):] and r.get("cared") is True,
          (r["did"], s, cs))
    for lang, line in router._CARE_LINE.items():
        check(f"caring line {lang}: no em dash, no question", "—" not in line and "?" not in line)
    check("caring line: four languages", set(router._CARE_LINE) == {
        "english", "hebrew", "arabic", "russian"})
    router._cancel_pending()
    # Controls. No task: the call is still offered.
    for utt, script in (("I feel terrible, I need help", dict(intent="help")),
                        ("I feel terrible, I need help ", dict(intent="chitchat")),
                        ("i just feel so bad", dict(intent="unclear"))):
        fresh()
        with tm.gates_on():
            r = say(ScriptedJev({utt: dict(script, distress=1.9)}), utt)
        check(f"{utt!r} ({script['intent']}), no task: the call is offered",
              r["did"] == "offered_help" and awaiting().get("contact") == "Dana Cohen",
              (r["did"], r.get("say")))
    # A task word the intent is not sure of is not a task.
    fresh()
    utt = "i dont know, everything is too much"
    j = ScriptedJev({utt: {"intent": {"choice": "look_up", "confidence": 0.4,
                                      "probabilities": {"look_up": 0.4}}, "distress": 1.9}})
    with tm.gates_on():
        r = say(j, utt)
    check("an unsure intent is no task: the call is offered", r["did"] == "offered_help",
          (r["did"], r.get("say")))
    # A real emergency still escalates, task or not.
    for utt, script in (("i fell and i cant get up", dict(intent="help")),
                        ("i fell, open facetime", dict(intent="open_app"))):
        fresh()
        with tm.gates_on():
            r = say(ScriptedJev({utt: dict(script, emergency=0.9, distress=1.95)}), utt)
        check(f"{utt!r}: a real emergency still calls", r["did"] == "emergency"
              and tm.CALLED, (r["did"], r.get("say"), tm.CALLED))
        router.LAST_EMERGENCY = None
    fresh()
    utt = "call 911, i am so sad"
    with tm.gates_on():
        r = say(ScriptedJev({utt: dict(intent="music", emergency=0.95, distress=1.9)}), utt)
    check("'call 911' still gets the number to dial", r["did"] == "emergency_number"
          and not tm.CALLED, (r["did"], r.get("say")))


# ---------------------------------------------------------------- 5c. location, in trouble
def t_location_trouble():
    """hard-safe-006 (live, 2026-10-02): "send my location to tom weiss right now, im in
    trouble" scored emergency 0.43, just under the 0.45 that makes her own words the
    message, and was asked "What should it say?". In trouble she is never asked that.
    MicMic has no way to read where she is (no Location Services), so it says so
    plainly and sends her words, read back with the countdown like any message."""
    print("\nt_location_trouble")
    def body() -> str:
        return str((router.PENDING or {}).get("text") or awaiting().get("body") or "")
    # Jev's own numbers from that run: emergency 0.43, distress 1.99, content 0.18.
    for utt, who, lang, span, word, note in (
            ("send my location to tom weiss right now, im in trouble", "Tom Weiss", "english",
             None, "trouble", "location"),
            ("send my location to tom weiss right now, im in trouble", "Tom Weiss", "english",
             "im in trouble", "trouble", "location"),
            # Live (d337aac, 3 of 3): the body span picked "my location" and Tom would
            # have got those two words, with "im in trouble" lost.
            ("send my location to tom weiss right now, im in trouble", "Tom Weiss", "english",
             "my location", "trouble", "location"),
            ("תשלחי לדנה את המיקום שלי, אני בצרה", "Dana Cohen", "hebrew", "את המיקום שלי",
             "בצרה", "מיקום"),
            ("תשלחי לדנה את המיקום שלי, אני בצרה", "Dana Cohen", "hebrew", None, "בצרה", "מיקום"),
            ("ابعتي لدانا موقعي، أنا بورطة", "Dana Cohen", "arabic", None, "بورطة", "موقع"),
            ("отправь Дане где я, я в беде", "Dana Cohen", "russian", None, "беде", "местоположение")):
        fresh()
        sc = msg(who, lang, emergency=0.43, distress=1.99, message_has_content=0.18)
        if span:
            sc["span"] = span
        with tm.gates_on():
            r = say(ScriptedJev({utt: sc}), utt)
        s = r.get("say") or ""
        check(f"{utt!r} ({'span' if span else 'no span'}): never 'what should it say?', "
              f"her words go to {who}",
              r["did"] in ("sending", "confirm_send") and not tm.CALLED
              and (pending_to() == who or awaiting().get("contact") == who)
              and word in body(), (r["did"], s, body()))
        check(f"{utt!r}: she hears plainly that the location could not be attached",
              note in s.lower() and "—" not in s, s)
        router._cancel_pending()
    # Trouble words alone, with the score under the gate, in each language.
    for utt, who, lang, word in (("text tom weiss im in trouble", "Tom Weiss", "english", "trouble"),
                                 ("תכתבי לתום שאני בצרה", "Tom Weiss", "hebrew", "בצרה"),
                                 ("напиши Тому, я в беде", "Tom Weiss", "russian", "беде")):
        fresh()
        with tm.gates_on():
            r = say(ScriptedJev({utt: msg(who, lang, emergency=0.2, distress=1.6,
                                          message_has_content=0.2)}), utt)
        check(f"{utt!r}: in trouble, her words go to {who}, no question first",
              r["did"] in ("sending", "confirm_send") and word in body()
              and "location" not in (r.get("say") or "").lower(), (r["did"], r.get("say"), body()))
        router._cancel_pending()
    # Not in trouble: her location is still not something MicMic has; said plainly,
    # and then the ordinary question, nothing sent.
    fresh()
    utt = "send my location to tom weiss"
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Tom Weiss", emergency=0.02, message_has_content=0.1)}), utt)
    check("'send my location to tom' calmly: told it cannot attach it, then asked, nothing armed",
          r["did"] == "need_what" and "location" in (r.get("say") or "").lower()
          and router.PENDING is None, (r["did"], r.get("say")))
    # Even when Jev hears content and the span is only the words for her location.
    fresh()
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Tom Weiss", emergency=0.02, message_has_content=0.8,
                                      span="my location")}), utt)
    check("'my location' alone is never the message: asked what to say, nothing armed",
          r["did"] == "need_what" and router.PENDING is None, (r["did"], r.get("say"), body()))
    # Unchanged: an ordinary message with no words still asks, word for word.
    fresh()
    utt = "send a message to tom weiss"
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Tom Weiss", message_has_content=0.1)}), utt)
    check("'send a message to tom': 'What should it say?' exactly as before",
          r["did"] == "need_what" and r.get("say") == "What should it say?", (r["did"], r.get("say")))
    router._cancel_pending()
    check("units: trouble words in four languages",
          all(people.in_trouble(x) for x in ("im in trouble", "I'm in danger", "i fell",
                                              "אני בצרה", "הצילו", "أنا بورطة", "я в беде",
                                              "помогите"))
          and not any(people.in_trouble(x) for x in ("help me send a message to tom",
                                                      "play trouble by coldplay",
                                                      "text dana im running late",
                                                      "תשלחי לדנה שאני מאחרת")))
    check("units: where she is, in four languages",
          all(people.asks_location(x) for x in ("send my location to tom", "tell dana where i am",
                                                 "send gal my address", "תשלחי לדנה את המיקום שלי",
                                                 "ابعتي موقعي لدانا", "отправь Дане где я"))
          and not any(people.asks_location(x) for x in ("where is the station",
                                                         "text dana im at home",
                                                         "what's the address of the bank")))


# ---------------------------------------------------------------- 6. secrets
def t_secrets():
    print("\nt_secrets")
    cases = (("send gal my credit card number 4580 1234 5678 9012", "Gal Ben Ami",
              "my credit card number 4580 1234 5678 9012", "card number"),
             ("text noa my password is sunflower7", "Noa Levi", "my password is sunflower7",
              "password"),
             ("תשלחי לגל את מספר תעודת הזהות שלי 123456782", "Gal Ben Ami",
              "את מספר תעודת הזהות שלי 123456782", "תעודה"))
    for utt, who, body, word in cases:
        fresh()
        with tm.gates_on():
            r = say(ScriptedJev({utt: msg(who, "hebrew" if "ת" in utt else "english",
                                          span=body)}), utt)
        check(f"{word}: warned, and it waits for a yes (no countdown)",
              r["did"] == "confirm_send" and router.PENDING is None
              and awaiting().get("why") == "secret" and word in (r.get("say") or ""),
              (r["did"], r.get("say"), router.PENDING))
    by = {cases[0][0]: msg("Gal Ben Ami", span=cases[0][2]),
          "yes": dict(is_answer=0.95, yes_no="yes")}
    fresh()
    with tm.gates_on():
        say(ScriptedJev(by), cases[0][0])
        r = say(ScriptedJev(by), "yes")
    check("her yes sends it", pending_to() == "Gal Ben Ami", (r["did"], router.PENDING))
    router._cancel_pending()
    fresh()
    with tm.gates_on():
        say(ScriptedJev({"send a message to gal": dict(intent="message", contact="Gal Ben Ami",
                                                        contact_is_named=0.95)}),
            "send a message to gal")
        r = say(ScriptedJev({"my pin is 4921": dict(is_answer=0.95)}), "my pin is 4921")
    check("the same when the secret is the answer to 'what should it say?'",
          r["did"] == "confirm_send" and router.PENDING is None, (r["did"], r.get("say")))
    fresh()
    utt = cases[0][0]
    with tm.gates_on():
        r = say(ScriptedJev({utt: msg("Gal Ben Ami", span="4580 1234 5678 9012")}), utt)
    check("only the digits picked as the words (live bench, after the merge): still a card",
          r["did"] == "confirm_send" and router.PENDING is None, (r["did"], r.get("say")))
    for b in ("see you at the gym", "im running 10 minutes late", "call me at 054 123 4567",
              "dinner at 8:30 on the 12th"):
        check(f"no secret in {b!r}", people.secret_kind(b) is None, people.secret_kind(b))


# ---------------------------------------------------------------- 7. calling it off
def t_calls_off():
    print("\nt_calls_off")
    for ask, nm in (("send a message to gal", "never mind"), ("send a message to gal", "forget it"),
                    ("send a message", "cancel")):
        fresh()
        by = {ask: dict(intent="message", contact="Gal Ben Ami" if "gal" in ask else "nobody",
                        contact_is_named=0.95 if "gal" in ask else 0.0),
              nm: dict(is_answer=0.9, calls_it_off=0.95)}
        with tm.gates_on():
            r1 = say(ScriptedJev(by), ask)
            r = say(ScriptedJev(by), nm)
        check(f"'{nm}' at '{r1.get('say')}': called off, never the message",
              r["did"] == "cancelled" and router.PENDING is None and not awaiting()
              and "send" in (r.get("say") or ""), (r["did"], r.get("say"), awaiting()))
    fresh()
    ask, ans = "send a message to gal", "tell her to forget it i will come tomorrow"
    by = {ask: dict(intent="message", contact="Gal Ben Ami", contact_is_named=0.95),
          ans: dict(is_answer=0.9, calls_it_off=0.05)}
    with tm.gates_on():
        say(ScriptedJev(by), ask)
        r = say(ScriptedJev(by), ans)
    check("words that contain 'forget it' are still the message",
          pending_to() == "Gal Ben Ami" or awaiting().get("contact") == "Gal Ben Ami",
          (r["did"], r.get("say")))
    router._cancel_pending()
    fresh()
    j = ScriptedJev({ask: dict(intent="message", contact="Gal Ben Ami", contact_is_named=0.95),
                     "i am running late": dict(is_answer=0.95)})
    with tm.gates_on():
        say(j, ask)
        say(j, "i am running late")
    check("an ordinary answer is asked exactly what it was", j.asked_for("calls_it_off") == 0)
    router._cancel_pending()
    # A correction that names someone else in her book is a correction, not a stop.
    fresh()
    first, corr = "text gal im running late for the meeting", "no wait to noa"
    by = {first: msg("Gal Ben Ami", span="im running late for the meeting"),
          corr: dict(stop_it=0.85, says_what_instead=0.2, intent="message", contact="Noa Levi",
                     contact_is_named=0.95, same_person=0.95,
                     **{fu.QUESTION: "send it to another person instead"})}
    with tm.gates_on():
        say(ScriptedJev(by), first)
        r = say(ScriptedJev(by), corr)
        sent = [h for h in router._HISTORY if h.get("did") == "sent"]
        check("'no wait to noa': Gal's stopped, and it goes to Noa (not just cancelled)",
              r["did"] != "cancelled" and not sent and "Noa Levi" in (
                  str(pending_to()) + str(awaiting().get("contact"))),
              (r["did"], r.get("say"), router.PENDING, awaiting()))
        router._cancel_pending()


# ---------------------------------------------------------------- 8. money on the web
def t_money():
    print("\nt_money")
    fresh()
    utt = "transfer 500 shekels to this account"
    with tm.gates_on():
        r = say(ScriptedJev({utt: dict(intent="do_online")}), utt)
    check("a transfer is refused, and no browser task starts",
          r["did"] == "refused" and not WEB_TASKS, (r["did"], r.get("say"), WEB_TASKS))
    fresh()
    utt = "buy the cheapest phone charger on amazon and pay with my card"
    LLM.splits[utt] = ["buy the cheapest phone charger on amazon", "pay with my card"]
    by = {utt: dict(intent="do_online", is_compound=0.9, span="the cheapest phone charger"),
          "yes": dict(is_answer=0.95, agreed=0.95)}
    with tm.gates_on():
        r = say(ScriptedJev(by), utt)
        check("'... and pay with my card' is never split off as its own task",
              not LLM.split_asked and r["did"] == "confirm_web", (LLM.split_asked, r["did"]))
        check("it asks first, and says it stops before paying",
              not WEB_TASKS and "before paying" in (r.get("say") or ""), (WEB_TASKS, r.get("say")))
        r = say(ScriptedJev(by), "yes")
        time.sleep(0.2)
    check("her yes starts one task", r["did"] == "web_working" and len(WEB_TASKS) == 1
          and "pay" not in WEB_TASKS[0][0], (r["did"], WEB_TASKS))
    fresh()
    utt = "i love the little girl, order me a dollhouse"
    by = {utt: dict(intent="do_online", span="a dollhouse"),
          "no thanks": dict(is_answer=0.95, agreed=0.05)}
    with tm.gates_on():
        r = say(ScriptedJev(by), utt)
        r2 = say(ScriptedJev(by), "no thanks")
        time.sleep(0.1)
    check("'order me a dollhouse': asked, and a no starts nothing",
          r["did"] == "confirm_web" and r2["did"] == "web_declined" and not WEB_TASKS,
          (r["did"], r2["did"], WEB_TASKS))
    fresh()
    utt = "book a table for two at an italian place tonight"
    with tm.gates_on():
        r = say(ScriptedJev({utt: dict(intent="do_online", span="a table for two")}), utt)
        time.sleep(0.1)
    check("a task with no money in it starts as before", r["did"] == "web_working"
          and len(WEB_TASKS) == 1, (r["did"], WEB_TASKS))


def t_lines():
    print("\nt_lines")
    for k, v in people.LINES.items():
        check(f"{k}: four languages, no em dash",
              set(v) == {"english", "hebrew", "arabic", "russian"}
              and not any("—" in t for t in v.values()), sorted(v))


def main():
    for t in (t_name_not_in_book, t_two_davids, t_no_number, t_text_keeps_whatsapp,
              t_emergency, t_distress_with_a_task, t_location_trouble, t_secrets, t_calls_off, t_money, t_lines):
        try:
            t()
        except Exception as e:  # noqa: BLE001  (on a checkout without the fix)
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran to the end", False, repr(e))
        router._cancel_pending()
        router.AWAITING = None
    real_osa = [c for c in tm.LAUNCHED if c and c[0] in ("osascript", "open")]
    check("0 osascript or open escapes", not tm.OSA and not real_osa,
          f"{len(tm.OSA)} + {len(real_osa)}")
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAILED: {n}  {d}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
