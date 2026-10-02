#!/usr/bin/env python3
"""A correction during the send countdown replaces the send, never adds a second one.

    cd <checkout> && uv run python tests/test_send_safety.py

The owner's session on 1.2.0 (2026-09-28), names replaced:
  1. "take a screenshot and send to Dana": speech misheard the name and it matched Dina.
     It was split in two ("take a screenshot" + "send the screenshot to Dina"), and the
     reply was "Who should I send it to? Sending Dina a screenshot of your screen...":
     it asked who AND already sent.
  2. 5 s later, still inside the countdown: "to Dana not Dina". The open "who?" took it
     as its answer and armed Dana's; arming a new send fired the one it displaced, so
     Dina's went at once. The wrong person got the screenshot.
  3. "please only send on whatsapp from now on not on messages": Dana's was re-sent on
     WhatsApp after it had already gone on iMessage.
What must happen instead: exactly one send, to Dana on WhatsApp.

Nothing leaves the machine. The stub wall is tests/test_micmic.py, loaded the way
tests/perf/bench.py loads it; the screenshot is a fake path (no screencapture), Gemini
is a fake whose only job is the split the owner's session got, and Jev is scripted per
sentence. No osascript, nothing reaches 127.0.0.1:8799.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-sendsafety-state-")
for _k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL"):
    os.environ.pop(_k, None)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "perf"))
import bench                                   # noqa: E402

tm = bench.load_wall()
router, mac = tm.router, tm.mac
from savta import followup as fu               # noqa: E402
from savta import prefs                        # noqa: E402

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


# ---------------------------------------------------------------- fakes
SHOTS: list[str] = []
SHOT_DIR = Path(tempfile.mkdtemp(prefix="micmic-sendsafety-shots-"))


def fake_capture(utterance: str) -> dict:
    """An empty file where screencapture would have written the picture."""
    SHOTS.append(str(SHOT_DIR / f"shot-{len(SHOTS) + 1}.png"))
    Path(SHOTS[-1]).write_bytes(b"")
    return {"ok": True, "path": SHOTS[-1], "why": "", "window": False, "app": "Safari"}


router._capture_screenshot = fake_capture


class FakeLLM:
    """Only the split. Every other writing task is a marked placeholder."""
    available = True

    def __init__(self):
        self.calls, self.busy_ms, self.last_ms, self.last_error = 0, 0.0, 0.0, None
        self.splits: dict[str, list[str]] = {}

    def split_steps(self, utterance, language="english"):
        self.calls += 1
        return self.splits.get(utterance)

    def answer(self, *a, **k):
        self.calls += 1
        return "[answer]"

    chat = text = answer


QUIET = ("not_applicable", "unspecified", "none", "nobody", "__none__", "any", "today",
         "unrevealed", "unchanged", "other", "not_said")


class ScriptedJev:
    """Quiet defaults, plus what each sentence is scripted to answer. The sentence is
    whichever of the request's fields carries her words."""
    mode = "replay"

    def __init__(self, by: dict[str, dict]):
        self.by = by
        self.calls, self.cost_usd, self.busy_ms = 0, 0.0, 0.0
        self.asked: list[dict] = []
        self.states: list[dict] = []

    def ask(self, state, qs):
        self.calls += 1
        self.asked.append(qs)
        self.states.append(state)
        said = next((state[k] for k in ("utterance", "her_answer", "she_said")
                     if isinstance(state.get(k), str)), "")
        script = self.by.get(said, {})
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
        for k, v in script.items():
            if k in qs:
                out[k] = (v if isinstance(v, dict)
                          else {"choice": v, "confidence": 0.9, "probabilities": {v: 0.9}}
                          if isinstance(v, str) else {"noul": v})
        return out

    def warmup(self, *a, **k):
        pass


LLM = FakeLLM()


def fresh():
    tm.reset_state()
    router.MEM = router.Memory()
    router._HISTORY.clear()
    router._JUST_SENT.clear()
    router._LAST_SHOT.clear()
    router.LLM_CLIENT = LLM
    LLM.splits.clear()
    SHOTS.clear()
    prefs._update(lambda d: d.update(message_app={}, confirm_send=""))
    router.CANCEL_WINDOW = 30.0          # only the test runs a countdown out


def say(j: ScriptedJev, utterance: str) -> dict:
    return router.handle(j, utterance, speak=False, client="native", activation="push")


def sends() -> list[tuple]:
    """Every send that went out, as (to, how): /api/sent's history, which is what the
    owner's session was checked against."""
    return [(h.get("to"), "whatsapp" if h.get("channel") == "whatsapp" else "imessage")
            for h in router._HISTORY if h.get("did") == "sent"]


def contact_pick(name: str, p: float = 0.9, second: tuple[str, float] | None = None) -> dict:
    probs = {name: p}
    if second:
        probs[second[0]] = second[1]
    return {"choice": name, "confidence": p, "probabilities": probs}


# ---------------------------------------------------------------- the owner's session
def owner_script(lang: str) -> tuple[list[str], dict]:
    """(her three sentences, Jev's readings of each), from the owner's trace."""
    if lang == "hebrew":
        t1, s1, s2 = ("תצלמי מסך ותשלחי לדנה", "תצלמי מסך", "תשלחי את צילום המסך לדינה")
        t2, t3 = "לדנה לא לדינה", "מעכשיו תשלחי רק בוואטסאפ, לא בהודעות"
    else:
        t1, s1, s2 = ("take a screenshot and send to Dana", "take a screenshot",
                      "send the screenshot to Dina")
        t2, t3 = ("to Dana not Dina",
                  "please only send on whatsapp from now on not on messages")
    LLM.splits[t1] = [s1, s2]
    by = {
        # Misheard: the whole sentence matched Dina. Compound, as it was split.
        t1: dict(intent="message", is_compound=0.9, contact="Dina",
                 contact_is_named=0.96, message_has_content=0.1, screen_task="send",
                 language=lang, same_person=0.9),
        s1: dict(intent="screen", screen_task="send", contact_is_named=0.06,
                 language=lang),
        s2: dict(intent="message", contact="Dina", contact_is_named=0.96,
                 refers_back=0.43, message_has_content=0.08, language=lang,
                 same_person=0.9),
        # The cancel question read it as a change, not a stop (the owner's did not stop).
        t2: dict(stop_it=0.3, says_what_instead=0.8, is_answer=0.95, contact="Dana",
                 contact_is_named=0.95, intent="message", refers_back=0.3,
                 same_person=0.95, language=lang,
                 **{fu.QUESTION: "send it to another person instead"}),
        t3: dict(stop_it=0.1, says_what_instead=0.7, intent="message", contact="Dana",
                 contact_is_named=0.62, refers_back=0.82, message_has_content=0.18,
                 channel="whatsapp", language=lang, pref_kind="message_app",
                 pref_app="whatsapp",
                 **{fu.QUESTION: "send it again on WhatsApp"}),
    }
    return [t1, t2, t3], by


def t_owner_session(lang: str):
    print(f"\nt_owner_session ({lang})")
    fresh()
    (t1, t2, t3), by = owner_script(lang)
    with tm.gates_on():
        r1 = say(ScriptedJev(by), t1)
        s1 = r1.get("say") or ""
        check(f"{lang} 1. one action: a read-back and countdown, never 'who should I send it to?'",
              r1["did"] in ("sending", "need_which") and router.AWAITING is None
              or r1["did"] == "need_which",
              (r1["did"], s1, router.AWAITING and router.AWAITING.get("need")))
        check(f"{lang} 1. it does not both ask who and send",
              not (router.AWAITING and router.PENDING), (router.AWAITING, router.PENDING))
        check(f"{lang} 1. one screenshot taken", len(SHOTS) == 1, SHOTS)

        r2 = say(ScriptedJev(by), t2)
        p = dict(router.PENDING or {})
        check(f"{lang} 2. '{t2}': Dina's is stopped and nothing has gone yet",
              sends() == [], sends())
        check(f"{lang} 2. Dana's is the one counting down",
              r2["did"] == "sending" and p.get("to") == "Dana", (r2["did"], r2.get("say"), p))
        check(f"{lang} 2. she hears that Dina's was stopped",
              "Dina" in (r2.get("say") or "") or "דינה" in (r2.get("say") or ""),
              r2.get("say"))

        r3 = say(ScriptedJev(by), t3)
        p = dict(router.PENDING or {})
        check(f"{lang} 3. the countdown moves to WhatsApp, to Dana",
              r3["did"] == "sending" and p.get("to") == "Dana"
              and p.get("channel") == "whatsapp", (r3["did"], r3.get("say"), p))
        check(f"{lang} 3. 'from now on' is kept as her standing choice",
              prefs.load()["message_app"].get("*") == "whatsapp", prefs.load()["message_app"])
        check(f"{lang} 3. and she is told so once",
              sum((r3.get("say") or "").count(w) for w in ("From now on", "מעכשיו")) == 1,
              r3.get("say"))
        router._fire_pending()
    check(f"{lang} every send accounted for: exactly one, to Dana on WhatsApp",
          sends() == [("Dana", "whatsapp")] and tm.WA_OPENED == ["Dana"]
          and not tm.SENT and not tm.WA, (sends(), tm.SENT, tm.WA, tm.WA_OPENED))


def t_countdown_over(lang: str):
    """The same session, but Dana's countdown ran out before "only on WhatsApp": it went,
    and it is re-sent on WhatsApp, said plainly (fix/send-followup's behaviour)."""
    print(f"\nt_countdown_over ({lang})")
    fresh()
    (t1, t2, t3), by = owner_script(lang)
    with tm.gates_on():
        say(ScriptedJev(by), t1)
        say(ScriptedJev(by), t2)
        router._fire_pending()
        r3 = say(ScriptedJev(by), t3)
        s3 = r3.get("say") or ""
        check(f"{lang} after it went: the same screenshot again, on WhatsApp, said plainly",
              r3["did"] == "sending" and (router.PENDING or {}).get("channel") == "whatsapp"
              and ("same screenshot" in s3 or "שוב" in s3), s3)
        check(f"{lang} and the standing choice is saved", 
              prefs.load()["message_app"].get("*") == "whatsapp")
        router._fire_pending()
    check(f"{lang} Dina never got it; Dana on iMessage, then on WhatsApp",
          sends() == [("Dana", "imessage"), ("Dana", "whatsapp")], sends())


def t_open_question_answered_during_countdown():
    """The owner's exact state on 1.2.0: a "who?" left open by one half of a split and
    the other half's send counting down. Her correction answers the question: it must
    replace the send, never fire it."""
    print("\nt_open_question_answered_during_countdown")
    fresh()
    with tm.gates_on():
        router._arm_send("Dina", "", "english", attachment="/nonexistent/shot-2.png",
                         from_screen="send_screenshot", shot_what="your screen")
        router.AWAITING = {"at": router.time.time(), "need": "who",
                           "question": "who should the message go to", "channel": "imessage",
                           "lang": "english", "body": None, "cut_long": False,
                           "from_screen": "send_screenshot", "contact": None,
                           "intent": "message", "chan_said": False,
                           "attachment": "/nonexistent/shot-1.png", "shot_what": "your screen"}
        r = say(ScriptedJev({"to Dana not Dina": dict(
            stop_it=0.3, says_what_instead=0.8, is_answer=0.95, contact="Dana",
            same_person=0.95)}),
            "to Dana not Dina")
        check("taken as the answer, and Dina's is stopped, not sent",
              r["did"] == "sending" and sends() == [] and (router.PENDING or {}).get("to") == "Dana",
              (r["did"], sends(), router.PENDING))
        check("she hears it", "I stopped the one to Dina." in (r.get("say") or ""), r.get("say"))
        router._fire_pending()
    check("exactly one send, to Dana", sends() == [("Dana", "imessage")], sends())


def t_split_never_asks_and_sends():
    """A split the screenshot words do not catch still never asks who in the same reply
    that sends: the question is closed as answered."""
    print("\nt_split_never_asks_and_sends")
    fresh()
    t1, s1, s2 = ("snap my screen and send it to Dina", "take a screenshot",
                  "send it to Dina")
    LLM.splits[t1] = [s1, s2]
    by = {t1: dict(intent="message", is_compound=0.9, contact="Dina", contact_is_named=0.9),
          s1: dict(intent="screen", screen_task="send"),
          s2: dict(intent="message", contact="Dina", contact_is_named=0.96, same_person=0.9,
                   refers_back=0.7, refers_to_screen=0.9)}
    # No page open, so "it" is the screenshot just taken (router._screen_content).
    prev = router._screen_context
    router._screen_context = lambda max_chars=6000: {**bench.SCREEN, "page": {}}
    with tm.gates_on():
        try:
            r = say(ScriptedJev(by), t1)
        finally:
            router._screen_context = prev
        check("one line: sending, no 'who should I send it to?'",
              r["did"] == "did_several" and "Who should" not in (r.get("say") or "")
              and "could not" not in (r.get("say") or "") and router.AWAITING is None
              and (router.PENDING or {}).get("to") == "Dina"
              and (router.PENDING or {}).get("attachment"), (r.get("say"), router.AWAITING,
                                                             router.PENDING))
    # The split's own words hold the screenshot: only that step is done, once.
    fresh()
    LLM.splits[t1] = [s1, "send the screenshot to Dina"]
    by["send the screenshot to Dina"] = dict(by[s2])
    with tm.gates_on():
        r = say(ScriptedJev(by), t1)
        check("a split to 'take a screenshot' + 'send the screenshot to Dina' is one send",
              r["did"] == "sending" and len(SHOTS) == 1 and router.AWAITING is None
              and (router.PENDING or {}).get("to") == "Dina", (r["did"], r.get("say"), SHOTS))


def t_sound_alike_asks_first(lang: str):
    """A weak or split match between two people is asked, before any countdown."""
    print(f"\nt_sound_alike_asks_first ({lang})")
    fresh()
    utt, ans = (("תצלמי מסך ותשלחי לדליה", "לדנה") if lang == "hebrew"
                else ("take a screenshot and send to dena", "Dana"))
    split = {"choice": "Dana", "confidence": 0.44,
             "probabilities": {"Dana": 0.44, "nobody": 0.33, "Dina": 0.21}}
    by = {utt: dict(intent="screen", is_compound=0.92, contact=split, contact_is_named=0.85,
                    language=lang),
          ans: dict(is_answer=0.95, contact="Dana", same_person=0.95)}
    LLM.splits[utt] = ["take a screenshot", "send the screenshot to Dana"]
    with tm.gates_on():
        r = say(ScriptedJev(by), utt)
        check(f"{lang} 'dena': asked 'Dana or Dina?', nothing counting down",
              r["did"] == "need_which" and router.PENDING is None
              and "Dana" in (r.get("say") or "") and "Dina" in r["say"]
              and ("או" in r["say"] if lang == "hebrew" else " or " in r["say"]),
              (r["did"], r.get("say"), router.PENDING))
        check(f"{lang} the screenshot is kept for the answer",
              (router.AWAITING or {}).get("attachment") == SHOTS[-1] and len(SHOTS) == 1,
              router.AWAITING)
        r = say(ScriptedJev(by), ans)
        check(f"{lang} 'Dana': then one read-back and countdown, to Dana",
              r["did"] == "sending" and (router.PENDING or {}).get("to") == "Dana"
              and (router.PENDING or {}).get("attachment") == SHOTS[-1], (r["did"], router.PENDING))
        router._fire_pending()
    check(f"{lang} one send, to Dana", sends() == [("Dana", "imessage")], sends())

    fresh()
    sure = {"choice": "Dana", "confidence": 1.0, "probabilities": {"Dana": 1.0, "Dina": 0.0}}
    u2 = "take a screenshot and send to Dana"
    with tm.gates_on():
        r = say(ScriptedJev({u2: dict(intent="screen", contact=sure, contact_is_named=0.97,
                                      same_person=0.95)}), u2)
    check(f"{lang} a clear name is not asked about", r["did"] == "sending", r["did"])
    check("units: the gate", router._sound_alike({"contact_named": 0.95, "raw": {"contact": {
        "probabilities": {"Dana": 0.98, "Dina": 0.02}}}}) is None
        and router._sound_alike({"contact_named": 0.85, "raw": {"contact": {
            "probabilities": {"Dana": 0.44, "nobody": 0.33, "Dina": 0.21}}}}) == ("Dana", "Dina"))


def t_both_asked_for():
    """The same thing to two people goes to both only when she asked for both."""
    print("\nt_both_asked_for")
    gal = dict(intent="message", contact="Gal", contact_is_named=0.95, same_person=0.95,
               message_has_content=0.1)
    # "Send the screenshot to Dana and Gal", split in two: both go.
    fresh()
    t1 = "take a screenshot and send it to Dana and to Gal"
    LLM.splits[t1] = ["take a screenshot", "send the screenshot to Dana",
                      "send the screenshot to Gal"]
    by = {t1: dict(intent="message", is_compound=0.9),
          LLM.splits[t1][1]: dict(gal, contact="Dana"), LLM.splits[t1][2]: gal}
    with tm.gates_on():
        say(ScriptedJev(by), t1)
        router._fire_pending()
    check("'to Dana and to Gal': both go", sorted(sends()) == [("Dana", "imessage"),
                                                              ("Gal", "imessage")], sends())
    check("units: one action unless more follows the send",
          router._one_shot_send("take a screenshot and send to Dana")
          and router._one_shot_send("תצלמי מסך ותשלחי לדנה")
          and not router._one_shot_send(t1)
          and not router._one_shot_send("תצלמי מסך ותשלחי לדנה ולגל")
          and not router._one_shot_send("send Dana a message and play music"))

    # "To Gal too" while Dana's counts down: both go.
    fresh()
    with tm.gates_on():
        say(ScriptedJev({"send the screenshot to Dana": dict(gal, contact="Dana")}),
            "send the screenshot to Dana")
        say(ScriptedJev({"to Gal too": dict(gal, **{fu.QUESTION: "send it to another person too"})}),
            "to Gal too")
        router._fire_pending()
    check("'to Gal too' during the countdown: both go",
          sorted(sends()) == [("Dana", "imessage"), ("Gal", "imessage")], sends())

    # Her own words, "tell Gal the same" while Dana's counts down: both go, as before.
    fresh()
    with tm.gates_on():
        router._arm_send("Dana", "I will be late", "english")
        router._BOTH.on = True
        router._arm_send("Gal", "I will be late", "english")
        router._BOTH.on = False
        router._fire_pending()
    check("the same words to Gal when she asked for both: both go",
          sorted(sends()) == [("Dana", "imessage"), ("Gal", "imessage")], sends())

    # The net: the same words to another person seconds later, not asked for both.
    fresh()
    with tm.gates_on():
        router._arm_send("Dana", "I will be late", "english")
        router._fire_pending()
        router.PENDING = {"kind": "send", "to": "Gal", "text": "I will be late",
                          "lang": "english", "channel": "imessage", "attachment": None}
        router._fire_pending()
        aw = router.AWAITING or {}
        check("the same words to Gal seconds after Dana's: held and asked, not sent",
              sends() == [("Dana", "imessage")] and aw.get("need") == "confirm_send"
              and aw.get("contact") == "Gal" and aw.get("also"), (sends(), aw))
        check("she hears why", "just went to Dana" in (router._LAST_SPOKEN.get("say") or ""),
              router._LAST_SPOKEN.get("say"))
        say(ScriptedJev({"yes": dict(is_answer=0.95, agreed=0.95, answer="yes")}), "yes")
        router._fire_pending()
    check("and a yes sends it", sends() == [("Dana", "imessage"), ("Gal", "imessage")], sends())


def t_stopped_and_corrected():
    """The cancel question reads "no, to Dana not Dina" as a stop that says how instead:
    Dina's is stopped by that, and the correction is still carried out."""
    print("\nt_stopped_and_corrected")
    fresh()
    by = {"send the screenshot to Dina": dict(intent="message", contact="Dina",
                                               contact_is_named=0.96, same_person=0.9),
          "no, to Dana not Dina": dict(stop_it=0.8, says_what_instead=0.9, intent="message",
                                       contact="Dana", contact_is_named=0.95,
                                       same_person=0.95,
                                       **{fu.QUESTION: "send it to another person instead"})}
    with tm.gates_on():
        say(ScriptedJev(by), "send the screenshot to Dina")
        r = say(ScriptedJev(by), "no, to Dana not Dina")
        check("Dina's stopped, Dana's counting down, and she hears both",
              r["did"] == "sending" and (router.PENDING or {}).get("to") == "Dana"
              and "I stopped the one to Dina." in (r.get("say") or "")
              and "instead" in r["say"], (r["did"], r.get("say")))
        router._fire_pending()
    check("one send, to Dana", sends() == [("Dana", "imessage")], sends())


def t_correction_needs_a_name():
    """"Not to Dina" with no one Jev can put a name to: Dina's stops, and she is asked."""
    print("\nt_correction_needs_a_name")
    fresh()
    by = {"send the screenshot to Dina": dict(intent="message", contact="Dina",
                                               contact_is_named=0.96, same_person=0.9),
          "no, not Dina": dict(stop_it=0.3, says_what_instead=0.8, intent="message",
                               contact="Dina", contact_is_named=0.9,
                               **{fu.QUESTION: "send it to another person instead"})}
    with tm.gates_on():
        say(ScriptedJev(by), "send the screenshot to Dina")
        r = say(ScriptedJev(by), "no, not Dina")
        check("Dina's stopped and she is asked who", r["did"] == "need_who"
              and router.PENDING is None and sends() == [], (r["did"], router.PENDING, sends()))


def t_own_words_move_app():
    """"From now on only WhatsApp" while her own words count down on iMessage: saved,
    and that message goes by WhatsApp (after her yes), never by both."""
    print("\nt_own_words_move_app")
    fresh()
    utt = "from now on only whatsapp"
    with tm.gates_on():
        router._arm_send("Dana", "I will be late", "english")
        r = say(ScriptedJev({utt: dict(stop_it=0.1, intent="message", pref_kind="message_app",
                                       pref_app="whatsapp")}), utt)
        check("saved, and the iMessage one is not left counting down",
              prefs.load()["message_app"].get("*") == "whatsapp"
              and (router.PENDING is None or router.PENDING.get("channel") == "whatsapp"),
              (r["did"], r.get("say"), router.PENDING))
        say(ScriptedJev({"yes": dict(is_answer=0.95, agreed=0.95, answer="yes")}), "yes")
        router._fire_pending()
    check("one send, on WhatsApp", sends() == [("Dana", "whatsapp")], sends())


def t_gate():
    """A bare correction is asked about only while a message from her screen counts down."""
    print("\nt_gate")
    for s_ in ("to Dana not Dina", "no, Dana", "not Dina", "לדנה לא לדינה", "لدانا مش لدينا",
               "Дане, не Дине"):
        check(f"correction words: {s_!r}", fu.corrects_message(s_))
    for s_ in ("what's the weather", "play Bad Bunny", "send Dana hi", "notes", "nothing",
               "מה השעה", "לאט לאט"):
        check(f"no correction words: {s_!r}", not fu.corrects_message(s_))
    item = {"kind": "message", "to": "Dina", "channel": "imessage",
            "from_screen": "send_screenshot", "at": router.time.time()}
    check("asked while counting down", fu.question([{**item, "counting": True}], "not Dina"))
    check("not asked once it went", fu.question([item], "not Dina") is None)
    check("never for her own words (the draft has them)",
          fu.question([{**item, "own_words": True, "counting": True}], "not Dina") is None)
    a = {"from_screen": "send_screenshot", "attachment": "/x/1.png", "text": ""}
    check("two screenshots are the same thing",
          router._same_content(a, {"attachment": "/x/2.png", "text": ""}))
    check("different words are not", not router._same_content({"text": "hi"}, {"text": "bye"}))
    check("a link is not a screenshot",
          not router._same_content(a, {"from_screen": "send_link", "text": "https://x"}))
    check("no em dash in any new line", not any(
        "\u2014" in v for k in ("msg_instead", "stopped_to", "stopped_on", "msg_twice",
                                "which_one") for v in fu.LINES[k].values()))


def t_words_keep_their_apostrophes():
    """The message is a span of her own words: "I'm running late" went out as
    "I m running late" because every apostrophe was stripped before the spans."""
    from savta.brain import span_candidates
    c = span_candidates("send Dana a WhatsApp that I'm running late, then play some jazz")
    check("a contraction stays whole in the words a message is picked from",
          "I'm running late" in c and "I m running late" not in " | ".join(c), c[:4])
    c = span_candidates("tell Gal it’s Dana’s turn")
    check("a curly apostrophe stays too", "it’s Dana’s turn" in c, c[:3])
    c = span_candidates("tell Gal 'see you soon'")
    check("quote marks around words are still dropped", "see you soon" in c, c[:3])
    c = span_candidates("תשלח לדנה שאני קונה צ'יפס")
    check("a Hebrew geresh stays", "שאני קונה צ'יפס" in c, c[:3])


def main():
    for t, args in ((t_owner_session, ("english",)), (t_owner_session, ("hebrew",)),
                    (t_countdown_over, ("english",)), (t_countdown_over, ("hebrew",)),
                    (t_open_question_answered_during_countdown, ()),
                    (t_split_never_asks_and_sends, ()),
                    (t_sound_alike_asks_first, ("english",)),
                    (t_sound_alike_asks_first, ("hebrew",)),
                    (t_both_asked_for, ()), (t_stopped_and_corrected, ()),
                    (t_correction_needs_a_name, ()),
                    (t_own_words_move_app, ()), (t_gate, ()),
                    (t_words_keep_their_apostrophes, ())):
        try:
            t(*args)
        except Exception as e:  # noqa: BLE001  (on a checkout without the fix)
            import traceback
            traceback.print_exc()
            check(f"{t.__name__}{args} ran to the end", False, repr(e))
        router._cancel_pending()
        router.AWAITING = None
    real_osa = [c for c in tm.LAUNCHED if c and c[0] in ("osascript", "screencapture")]
    check("0 osascript or screencapture escapes", not tm.OSA and not real_osa,
          f"{len(tm.OSA)} + {len(real_osa)}")
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAILED: {n}  {d}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
