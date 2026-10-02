#!/usr/bin/env python3
"""Questions about what is on her screen, keys pressed in a named app, and messages
read out: the fixes from MicMic Bench v1 (2026-10-01) and the demo check.

    cd <worktree> && uv run python tests/test_screen_apps.py

  * "What time does my flight leave?" with the booking open was answered "I do not
    have access to your personal travel information" (scr-010/011/014), and "how
    many eggs does it need" over a recipe saying 4 said "2 eggs" (hard-multi-008).
    A question the word gate lets through carries one more Jev question in the same
    request (brain.SCREEN_ASK_QUESTIONS); a yes reads the screen, the model is given
    its text, and code checks every number it says against that text.
  * "In the calculator press 5 times 3" answered "fifteen" (chat) instead of pressing,
    and another phrasing pressed stray keys and said only "Alright". The program she
    named wins, the keys are exactly 5 x 3 =, and the display is read back. Driven
    against a fake Accessibility tree of a calculator, never the real one.
  * Read-out messages said "by Saturday.. Mom: ...".

Zero live calls: understand() is stood in, Jev is scripted, Gemini is a stub, and no
osascript, screencapture or Accessibility call runs for real. Never reaches 8799.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if os.environ.get("MICMIC_ALLOW_SEND", "").lower() in ("1", "true", "yes"):
    print("REFUSING TO RUN: MICMIC_ALLOW_SEND is set in this shell.")
    sys.exit(2)
os.environ.pop("MICMIC_ALLOW_SEND", None)
_STATE = tempfile.mkdtemp(prefix="micmic-test-screen-apps-")
os.environ["MICMIC_STATE_DIR"] = _STATE
atexit.register(lambda: shutil.rmtree(_STATE, ignore_errors=True))
# Any model call that slipped past the stubs fails at once, for free.
os.environ.update({"MICMIC_MODE": "proxy", "MICMIC_PROXY_URL": "http://127.0.0.1:9",
                   "MICMIC_PROXY_TOKEN": "offline-test"})

from savta import router                       # noqa: E402
from savta import brain                        # noqa: E402
from savta import profile as prof              # noqa: E402
from savta.actions import mac                  # noqa: E402
from savta.actions import apps                 # noqa: E402
from savta.actions import screen               # noqa: E402
from savta.actions import contacts as book     # noqa: E402
from savta.actions import targets as tg        # noqa: E402

import replay                                  # noqa: E402
replay.install()

router.due_briefing = lambda: False
prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done", "name": "Sam",
           "language": "english", "speech_lang": "en-US", "city": "Haifa"})

# ---------------------------------------------------------------- walls
import subprocess as _sp_mod              # noqa: E402

LAUNCHED: list[list] = []
OSA: list[str] = []


def _blocked_run(cmd, *a, **kw):
    LAUNCHED.append(list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)])

    class _R:
        returncode, stdout, stderr = 0, "", ""
    return _R()


_sp_mod.run = _blocked_run
mac.subprocess.run = _blocked_run
screen.subprocess.run = _blocked_run
mac._osa = lambda script, timeout=15.0: (OSA.append(script), (False, "[test] blocked"))[1]
mac.say = lambda t, l="english": None
mac.screen_locked = lambda: False
mac.installed_apps = lambda limit=200: ["Calculator", "Safari", "Mail", "Notes", "Slack"]
tg._APPS_CACHE.update(at=0.0, names=[])
book.all_contacts = lambda force=False, wait=None: []
router.get_contacts = lambda utterance="", wait=None, **_: ["Dana Cohen"]
# Accessibility is never reached for real: every test that needs it brings a fake.
apps._ax = lambda: None
screen.context = lambda max_chars=6000: None

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


def short(v, n=240):
    s = repr(v)
    return s if len(s) <= n else s[:n] + "..."


_U_KEYS = ("intent_confidence", "wants_full_length", "names_title", "contact_confidence",
           "contact_named", "has_message_content", "money_involved", "sounds_coached",
           "control_confidence", "wants_recent", "asking_for_notes", "is_complete",
           "noise", "refers_back", "is_compound", "needs_knowledge", "about_weather",
           "about_clock", "setting_emergency_contact", "inside_an_app",
           "speaker_gender_confidence", "rejects_last", "describes_instead",
           "refers_to_screen", "distress", "emergency", "wants_undo", "amends_message",
           "live_kind_confidence", "screen_question", "named_app_confidence",
           "named_app_only_look", "guidance_confidence")


def _u(intent="look_up", **kw) -> dict:
    u = {k: 0.0 for k in _U_KEYS}
    u.update({"raw": {}, "spans": {}, "intent": intent, "intent_probs": {intent: 0.9},
              "media_kind": "not_applicable", "contact": "nobody",
              "control_action": "not_applicable", "player_action": "not_applicable",
              "channel": "imessage", "file_kind": "any", "when_minutes": "none",
              "language": "english", "speaker_gender": "unrevealed",
              "screen_task": "not_applicable", "weather_day": "today",
              "write_in": "not_applicable", "message_app": "unchanged",
              "live_kind": "not_live", "standing": {}, "named_app": "none",
              "guidance": "none", "guide_command": "none",
              "intent_confidence": 0.95, "is_complete": 0.95, "contact_confidence": 0.9})
    u.update(kw)
    return u


class _ScriptedJev:
    """Never reaches the network: the first option or no, for anything asked."""

    def __init__(self):
        self.calls, self.cost_usd, self.busy_ms, self.input_tokens = 0, 0.0, 0.0, 0
        self.asked: list = []

    def ask(self, state, qs):
        self.calls += 1
        self.asked.append((state, qs))
        out = {}
        for k, q in qs.items():
            if q["type"] == "choice":
                opts = list(q["criteria"])
                pick = "nobody" if "nobody" in opts else opts[0]
                out[k] = {"choice": pick, "confidence": 0.9, "probabilities": {pick: 0.9}}
            elif q["type"] == "noul":
                out[k] = {"noul": 0.0}
            else:
                out[k] = {"score": 0.0}
        return out


class understood:
    """router.understand stood in; records the keyword arguments it was given."""

    def __init__(self, u: dict):
        self.u, self.kw = u, []

    def __enter__(self):
        self.real = router.understand
        router.understand = lambda *a, **k: (self.kw.append(k), self.u)[1]
        return self

    def __exit__(self, *exc):
        router.understand = self.real
        return False


class model:
    """Gemini stood in: records every prompt, answers with `reply`."""

    def __init__(self, reply: str | None):
        self.reply, self.prompts = reply, []

    def __enter__(self):
        c = router.LLM_CLIENT
        self.saved = (c.key, c.generate_content, c.chat)
        c.key = "offline-test"

        def gen(contents, *a, **k):
            self.prompts.append((k.get("system", ""), contents[0]["parts"][-1]["text"]))
            if self.reply is None:
                return None
            return {"candidates": [{"content": {"parts": [{"text": self.reply}]}}]}
        c.generate_content = gen
        c.chat = lambda *a, **k: "chat reply"
        return self

    def __exit__(self, *exc):
        c = router.LLM_CLIENT
        c.key, c.generate_content, c.chat = self.saved
        return False


class screen_shows:
    """router._screen_context stood in, counting reads."""

    def __init__(self, ctx: dict | None):
        self.ctx, self.reads = ctx, 0

    def __enter__(self):
        self.real = router._screen_context

        def ctx_fn(max_chars=6000):
            self.reads += 1
            return json.loads(json.dumps(self.ctx)) if self.ctx else self.ctx
        router._screen_context = ctx_fn
        return self

    def __exit__(self, *exc):
        router._screen_context = self.real
        return False


def _ctx(app, title, text, url=""):
    return {"permissions": {"accessibility": True, "screen_recording": False},
            "frontmost": {"app": app, "pid": 4102, "bundle_id": "", "window": title},
            "selected": "", "focused": {"role": "", "value": "", "secure": False},
            "visible": {"text": text, "truncated": False},
            "page": {"url": url, "title": title} if url else {}, "has_image": False}


FLIGHT = _ctx("Google Chrome", "Booking confirmed",
              "Booking confirmed. El Al LY 027, Tel Aviv TLV to New York JFK, Tue 14 Oct, "
              "departs 00:45, arrives 06:10. Booking code QX7RTA. Passenger: Sam Cohen. "
              "Seat 32A.", "https://example-airline.test/booking")
RECIPE = _ctx("Safari", "Easy shakshuka",
              "Easy shakshuka. Ingredients: 4 eggs, 2 tomatoes, 1 red pepper, 1 onion. "
              "Steps: 1. Chop the onion. 2. Fry 5 minutes. 3. Make 4 wells and crack in "
              "the eggs. 4. Cover and cook 6 minutes.", "https://example-recipes.test/s")


def reset():
    router._cancel_pending()
    router.AWAITING = None
    router.MEM = router.Memory()


# ==================================================================== 1. screen questions
def t_gate_words():
    for s in ("what time does my flight leave", "how many eggs do i need",
              "what's my booking code", "whats my flight", "how many eggs does it need",
              "which seat do i have", "מתי הטיסה שלי", "כמה ביצים צריך", "كم بيضة لازم",
              "во сколько мой рейс"):
        check(f"the word gate lets through: {s}", screen.may_ask_about_screen(s))
    for s in ("play some jazz", "what time is it", "call dana", "send this to dana",
              "tell me a joke", "what is the weather", "open the calculator",
              "who is the president of france", "תשימי מוזיקה"):
        check(f"the word gate keeps out: {s}", not screen.may_ask_about_screen(s))


def t_question_rides_only_when_gated():
    """The new question is in the one understand() request only behind the word gate;
    every other request is what it was."""
    j = _ScriptedJev()
    brain.understand(j, "play some jazz", ["Dana Cohen"])
    plain = j.asked[-1]
    check("an ordinary request carries no screen_question",
          "screen_question" not in plain[1], sorted(plain[1])[:5])
    brain.understand(j, "play some jazz", ["Dana Cohen"], screen_ask=False)
    check("screen_ask=False sends exactly the same request",
          json.dumps(j.asked[-1][1], sort_keys=True) == json.dumps(plain[1], sort_keys=True)
          and {k: v for k, v in j.asked[-1][0].items() if k != "right_now"}
          == {k: v for k, v in plain[0].items() if k != "right_now"})
    u = brain.understand(j, "what time does my flight leave", ["Dana Cohen"], screen_ask=True)
    check("behind the gate it rides in the same single request",
          "screen_question" in j.asked[-1][1] and j.calls == 3, j.calls)
    check("and comes back as a number", isinstance(u["screen_question"], float))
    check("the screen itself is never sent to Jev",
          "Booking" not in json.dumps(j.asked[-1][0]), "")

    reset()
    with understood(_u("music", intent_confidence=0.95)) as und, screen_shows(None) as sc, \
            model("x"):
        router.handle(_ScriptedJev(), "play some jazz please", speak=False)
    check("the router passes nothing new for an ordinary request",
          "screen_ask" not in und.kw[-1] and sc.reads == 0, short(und.kw[-1].keys()))


def t_flight_time_answered_from_screen():
    """scr-010: grounded in the booking on her screen."""
    reset()
    u = _u("look_up", intent_confidence=1.0, screen_question=0.84, needs_knowledge=0.43)
    with understood(u) as und, screen_shows(FLIGHT) as sc, \
            model("Your flight LY 027 leaves at 00:45 on Tuesday 14 October.") as m:
        r = router.handle(_ScriptedJev(), "what time does my flight leave", speak=False)
    check("the question rode in understand()", und.kw[-1].get("screen_ask") is True,
          short(und.kw[-1].keys()))
    check("answered from the screen", r["did"] == "answered" and "00:45" in r["say"]
          and r["detail"].get("from_screen"), short(r))
    check("the screen was read once", sc.reads == 1, sc.reads)
    check("the model was given the screen text, quoted", m.prompts and
          "departs 00:45" in m.prompts[-1][1] and "----- screen text -----" in m.prompts[-1][1],
          short(m.prompts))
    check("and told to copy numbers as written", "exactly as the screen writes it" in m.prompts[-1][1])
    check("nothing was opened in the browser", not any("open" in c[:1] for c in LAUNCHED), LAUNCHED)
    check("the answer is kept for a follow-up", (router.MEM.last_action or {}).get("kind") == "answer")


def t_pointing_at_the_screen_stays_a_screen_read():
    """Bench hard-scr-001: "what does this email say" is the screen read it always
    was (described_screen), not a grounded answer to a question."""
    reset()
    u = _u("screen", intent_confidence=0.95, screen_question=0.9, refers_to_screen=0.95,
           screen_task="describe")
    email = _ctx("Mail", "Dinner on Friday", "From: Dana Cohen. Dinner at Rosa's on Friday at 8pm?")
    with understood(u), screen_shows(email), model("Dana asks about dinner at Rosa's on Friday at 8pm.") as m:
        r = router.handle(_ScriptedJev(), "what does this email say", speak=False)
    check("pointing at the screen is still read by the screen branch",
          r["did"] == "described_screen" and not any("screen text -----" in p[1] for p in m.prompts),
          short(r))


def t_booking_code():
    reset()
    u = _u("look_up", intent_confidence=0.88, screen_question=0.82)
    with understood(u), screen_shows(FLIGHT), model("Your booking code is QX7RTA."):
        r = router.handle(_ScriptedJev(), "what's my booking code", speak=False)
    check("scr-014: the booking code comes off the screen",
          r["did"] == "answered" and "QX7RTA" in r["say"], short(r))


def t_invented_number_refused():
    """hard-multi-008: the recipe says 4 eggs; an answer of 2 is never passed on."""
    reset()
    u = _u("look_up", intent_confidence=0.8, screen_question=0.83)
    with understood(u), screen_shows(RECIPE), model("That recipe needs 2 eggs."):
        r = router.handle(_ScriptedJev(), "how many eggs does it need", speak=False)
    check("an invented number is not said", "2" not in r["say"] and r["did"] == "not_on_screen",
          short(r))
    check("she is told it cannot be seen", "cannot see" in r["say"].lower(), r["say"])
    reset()
    with understood(u), screen_shows(RECIPE), model("You need four eggs."):
        r = router.handle(_ScriptedJev(), "how many eggs do i need", speak=False)
    check("the right number, even as a word, is said", r["did"] == "answered"
          and "four" in r["say"], short(r))


def t_not_on_screen_falls_through_or_says_so():
    reset()
    u = _u("chitchat", intent_confidence=0.9, screen_question=0.4)
    with understood(u), screen_shows(FLIGHT), model("NOT_ON_SCREEN"):
        r = router.handle(_ScriptedJev(), "how many eggs do i need", speak=False)
    check("not on the screen and not sure: answered the ordinary way",
          r["did"] == "chatted" and not (r.get("detail") or {}).get("from_screen"), short(r))
    reset()
    u = _u("look_up", intent_confidence=0.9, screen_question=0.85)
    with understood(u), screen_shows(RECIPE), model("NOT_ON_SCREEN"):
        r = router.handle(_ScriptedJev(), "what time does my flight leave", speak=False)
    check("sure it is about her own thing, and it is not there: says so",
          r["did"] == "not_on_screen", short(r))


def t_below_gate_and_blank_screen():
    reset()
    u = _u("chitchat", intent_confidence=0.9, screen_question=0.1)
    with understood(u), screen_shows(RECIPE), model("You need 4 eggs.") as m:
        r = router.handle(_ScriptedJev(), "how many eggs are in a dozen", speak=False)
    check("below the gate the screen is not asked", r["did"] == "chatted"
          and not any("screen text" in p[1] for p in m.prompts), short(r))
    reset()
    u = _u("chitchat", intent_confidence=0.9, screen_question=0.6)
    with understood(u), screen_shows(_ctx("Google Chrome", "New Tab", "")), model("x") as m:
        r = router.handle(_ScriptedJev(), "how many eggs do i need", speak=False)
    check("a blank screen costs no model call for it", r["did"] == "chatted"
          and not any("screen text" in p[1] for p in m.prompts), short(r))


def t_numbers_check():
    R = RECIPE["visible"]["text"]
    F = FLIGHT["visible"]["text"]
    for ans, txt, want in (("That recipe needs 2 eggs.", R, False), ("You need 4 eggs.", R, True),
                           ("Cook it for 10 minutes.", R, False), ("Cook it 6 minutes.", R, True),
                           ("It leaves at 12:45 am.", F, False), ("Seat 32A.", F, True),
                           ("It leaves at 00:45.", F, True)):
        check(f"numbers check: {ans!r} -> {want}", screen.numbers_on_screen(ans, txt) == want)


# ==================================================================== 4. keys in a named app
class _El:
    def __init__(self, role, **attrs):
        self.attrs = {"AXRole": role, **attrs}


class FakeCalculator:
    """A calculator's accessibility tree: a display and labelled buttons. Pressing a
    button changes the display the way the real one does."""

    LABELS = {"0": "0", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7",
              "8": "8", "9": "9", "Add": "+", "Subtract": "-", "Multiply": "×",
              "Divide": "÷", "Equals": "=", "Decimal": ".", "All Clear": "AC"}

    def __init__(self):
        self.running = False
        self.presses: list[str] = []
        self.display = _El("AXStaticText", AXValue="0", AXDescription="main display")
        self.buttons = [_El("AXButton", AXDescription=d) for d in self.LABELS]
        group = _El("AXGroup", AXChildren=self.buttons)
        self.window = _El("AXWindow", AXTitle="Calculator", AXChildren=[self.display, group])
        self.root = _El("AXApplication", AXMainWindow=self.window, AXChildren=[self.window])
        self.acc, self.op, self.entry, self.fresh = None, None, "0", True

    def _value(self):
        return float(self.entry)

    def _apply(self):
        if self.op is None or self.acc is None:
            return self._value()
        a, b = self.acc, self._value()
        return {"+": a + b, "-": a - b, "×": a * b, "÷": a / b if b else float("nan")}[self.op]

    def press(self, label):
        k = self.LABELS[label]
        self.presses.append(k)
        if k.isdigit() or k == ".":
            self.entry = (k if k != "." else "0.") if self.fresh else self.entry + k
            self.fresh = False
        elif k in "+-×÷":
            self.acc = self._apply() if not self.fresh else (self.acc if self.acc is not None
                                                             else self._value())
            self.op, self.fresh = k, True
            self.entry = self._fmt(self.acc)
        elif k == "=":
            res = self._apply()
            self.acc, self.op, self.fresh = None, None, True
            self.entry = self._fmt(res)
        elif k == "AC":
            self.acc, self.op, self.entry, self.fresh = None, None, "0", True
        self.display.attrs["AXValue"] = self.entry

    @staticmethod
    def _fmt(x):
        return str(int(x)) if float(x).is_integer() else str(x)

    def ax(self):
        def get(el, name, _):
            if name in el.attrs:
                return 0, el.attrs[name]
            return -25212, None

        def press(el, action):
            self.press(el.attrs["AXDescription"])
            return 0
        return {"app": lambda pid: self.root, "get": get, "press": press,
                "set": lambda el, n, v: -25200, "trusted": lambda: True, "timeout": None}


class calculator:
    def __enter__(self):
        self.calc = FakeCalculator()
        self.saved = (apps._ax, mac.running_apps, mac.open_app, apps.SETTLE_MS)
        apps._ax = self.calc.ax
        apps.SETTLE_MS = 0
        mac.running_apps = lambda: ([{"name": "Calculator", "pids": [5150]}]
                                    if self.calc.running else []) + [{"name": "Safari", "pids": [7]}]

        def open_app(name):
            LAUNCHED.append(["open", "-a", name])
            if name == "Calculator":
                self.calc.running = True
            return True, name
        mac.open_app = open_app
        return self.calc

    def __exit__(self, *exc):
        apps._ax, mac.running_apps, mac.open_app, apps.SETTLE_MS = self.saved
        return False


def t_key_sequence():
    for said, want in (("in the calculator press 5 times 3", ["5", "×", "3", "="]),
                       ("in the calculator, press the buttons 5, times, 3, equals",
                        ["5", "×", "3", "="]),
                       ("In the calculator, press five", ["5"]),
                       ("press 12.5 plus 3", ["1", "2", ".", "5", "+", "3", "="]),
                       ("במחשבון תלחצי 5 כפול 3", ["5", "×", "3", "="]),
                       ("нажми 7 плюс 8", ["7", "+", "8", "="]),
                       ("press the blue button", None)):
        got = apps.key_sequence(tg.without_place(said, "app:Calculator"), "Calculator")
        check(f"keys of {said!r}: {want}", got == want, got)


def t_calculator_presses_exact_keys():
    """Demo check: "in the calculator press 5 times 3" chatted "That gives fifteen";
    with Calculator closed. Named app wins: it is opened, exactly 5 x 3 = is pressed,
    and what the display shows is what she hears."""
    reset()
    LAUNCHED.clear()
    u = _u("chitchat", intent_confidence=0.6, inside_an_app=0.5,
           named_app="app:Calculator", named_app_confidence=0.97)
    with calculator() as calc, understood(u), model("That gives fifteen."):
        r = router.handle(_ScriptedJev(), "in the calculator press 5 times 3", speak=False)
    check("not answered as a sum: the keys were pressed", r["did"] == "app_done", short(r))
    check("Calculator was opened first", ["open", "-a", "Calculator"] in LAUNCHED, LAUNCHED)
    check("exactly 5 x 3 = was pressed, nothing stray", calc.presses == ["5", "×", "3", "="],
          calc.presses)
    check("she hears what the display shows", r["say"] == "Calculator shows 15.", r["say"])
    check("no em dash in what she hears", "—" not in r["say"])

    reset()
    u = _u("open_app", intent_confidence=0.56, inside_an_app=0.98,
           named_app="app:Calculator", named_app_confidence=0.98)
    with calculator() as calc, understood(u), model(None):
        calc.running = True
        r = router.handle(_ScriptedJev(), "in the calculator, press the buttons 5, times, 3, "
                          "equals", speak=False)
    check("the spelled-out phrasing presses the same four keys",
          calc.presses == ["5", "×", "3", "="] and r["say"] == "Calculator shows 15.",
          f"{calc.presses} {r['say']!r}")


def t_calculator_missing_key_stops():
    reset()
    u = _u("open_app", intent_confidence=0.6, inside_an_app=0.98,
           named_app="app:Calculator", named_app_confidence=0.98)
    with calculator() as calc, understood(u), model(None):
        calc.running = True
        calc.buttons[:] = [b for b in calc.buttons if b.attrs["AXDescription"] != "Multiply"]
        r = router.handle(_ScriptedJev(), "in the calculator press 5 times 3", speak=False)
    check("a key it cannot find stops it, and she is told which",
          r["did"] == "app_blocked" and "times" in r["say"] and calc.presses == ["5"],
          f"{calc.presses} {short(r)}")


def t_calculator_hebrew():
    reset()
    u = _u("chitchat", intent_confidence=0.6, inside_an_app=0.4, language="hebrew",
           named_app="app:Calculator", named_app_confidence=0.9)
    with calculator() as calc, understood(u), model(None):
        calc.running = True
        r = router.handle(_ScriptedJev(), "במחשבון תלחצי 5 כפול 3", speak=False)
    check("in Hebrew too, and the reply is in Hebrew",
          calc.presses == ["5", "×", "3", "="] and "15" in r["say"]
          and any("֐" <= c <= "׿" for c in r["say"]), f"{calc.presses} {r['say']!r}")


# ==================================================================== 5. messages read out
def t_read_messages_single_stops():
    reset()
    real = book.unread_summary
    book.unread_summary = lambda limit=8: [
        {"who": "Dana Cohen", "text": "Did you see Gal's email? We need a yes on the flights "
                                      "by Saturday.", "at": time.time()},
        {"who": "Mom", "text": "Call me when you land on Friday, love you", "at": time.time()},
        {"who": "Gal", "text": "Are we still on?", "at": time.time()}]
    try:
        with understood(_u("read_msgs", intent_confidence=0.95)), model(None):
            r = router.handle(_ScriptedJev(), "what did I miss on WhatsApp today", speak=False)
    finally:
        book.unread_summary = real
    check("read out", r["did"] == "read_messages", short(r))
    check("no double full stop", ".." not in r["say"] and "?." not in r["say"], r["say"])
    check("each message still ends once", r["say"].endswith("Are we still on?")
          and "love you. Gal" in r["say"], r["say"])


TESTS = [
    ("1a. the word gate", t_gate_words),
    ("1b. the question rides only behind the gate", t_question_rides_only_when_gated),
    ("1c. flight time from the screen", t_flight_time_answered_from_screen),
    ("1d. booking code from the screen", t_booking_code),
    ("1d2. pointing at the screen stays a screen read", t_pointing_at_the_screen_stays_a_screen_read),
    ("1e. an invented number is refused", t_invented_number_refused),
    ("1f. not on the screen", t_not_on_screen_falls_through_or_says_so),
    ("1g. below the gate, blank screen", t_below_gate_and_blank_screen),
    ("1h. the numbers check", t_numbers_check),
    ("4a. keys from her words", t_key_sequence),
    ("4b. calculator presses exactly 5 x 3 =", t_calculator_presses_exact_keys),
    ("4c. a missing key stops it", t_calculator_missing_key_stops),
    ("4d. calculator in Hebrew", t_calculator_hebrew),
    ("5. read-out messages end once", t_read_messages_single_stops),
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
    print(f"osascript calls escaped the stub: {len(OSA)} (must be 0)")
    if OSA:
        FAILED.append(("osascript escaped", str(len(OSA))))
    if FAILED:
        print("\nFailures:")
        for name, detail in FAILED:
            print(f"  - {name}\n      {detail}" if detail else f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
