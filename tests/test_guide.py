#!/usr/bin/env python3
"""Guide mode (savta/actions/guide.py) and its router hooks, with a fake screen, a
fake model and a fake voice. No window, no Accessibility, no Jev, no Gemini.

    cd <worktree> && uv run python tests/test_guide.py

What is held here: one model call per step and none while she works; the ring only
ever points at a control the model was given, inside the window; a step is noticed
when the page, the window or the control changes, not while she types; no picture
goes out while a password field has the keyboard; "Do it for me" only for a plain
click on a web page; Done, back, why, stop; the caps; and in the router, that "walk
me through" starts it, a how-to question gets the chip, and nothing else does.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-guide-test-")
os.environ.pop("MICMIC_ALLOW_SEND", None)
# Any model call that slipped past the fakes fails at once, for free.
os.environ.update({"MICMIC_MODE": "proxy", "MICMIC_PROXY_URL": "http://127.0.0.1:9",
                   "MICMIC_PROXY_TOKEN": "offline-test"})

from savta.actions import guide as G  # noqa: E402
from savta.actions import mac  # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


def until(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- fakes
WIN = (100.0, 50.0, 1000.0, 800.0)


class FakeScreen:
    """A console with pages. Each page lists controls; press() and go() move on."""

    def __init__(self, browser=True):
        self.page = "home"
        self.browser = browser
        self.secure_focus = False
        self.focused = None
        self.values: dict = {}
        self.offset = 0.0           # scrolling moves every control
        self.struct = 3
        self.presses: list = []
        self.snaps = 0
        self.probes = 0
        self.shots = 0
        self.gone: set = set()
        self.pages = {
            "home": [(1, "link", "APIs & Services", (110, 200, 160, 28)),
                     (2, "button", "Create credentials", (600, 120, 170, 34)),
                     (3, "field", "Search", (400, 60, 300, 30))],
            "apis": [(1, "button", "Enable APIs and services", (300, 150, 220, 36)),
                     (2, "link", "Library", (110, 240, 120, 24))],
            "maps": [(1, "button", "Enable", (320, 300, 90, 36)),
                     (2, "field", "Password", (320, 360, 200, 30))],
        }

    def front(self):
        return {"app": "Google Chrome" if self.browser else "Notes", "pid": 42,
                "bundle_id": "", "window": "Console"}

    def snapshot(self, front):
        self.snaps += 1
        controls, refs = [], {}
        for cid, role, label, (x, y, w, h) in self.pages[self.page]:
            secure = label == "Password"
            controls.append({"id": cid, "role": role, "ax_role": "", "label": label,
                             "value": "" if secure else self.values.get(cid, ""),
                             "frame": (x, y + self.offset, w, h), "secure": secure,
                             "typed": role == "field"})
            refs[cid] = (self.page, cid)
        return {"app": front["app"], "pid": 42, "window": self.page, "url": f"https://c/{self.page}",
                "title": self.page, "controls": controls, "refs": refs, "text": "",
                "win": WIN, "secure_focus": self.secure_focus, "browser": self.browser,
                "error": None}

    def screenshot(self, pid):
        self.shots += 1
        return b"\x89PNG fake"

    def probe(self, snap, tid):
        self.probes += 1
        ref = (snap.get("refs") or {}).get(tid)
        alive = ref is not None and ref[0] == self.page and tid not in self.gone
        frame = None
        if alive:
            for cid, _r, _l, (x, y, w, h) in self.pages[self.page]:
                if cid == tid:
                    frame = (x, y + self.offset, w, h)
        return {"pid": 42, "title": self.page, "url": f"https://c/{self.page}",
                "struct": [self.struct], "alive": alive if ref else None,
                "state": (self.values.get(tid), None, None) if alive else None,
                "focused": self.focused == tid, "frame": frame, "win": WIN}

    def press(self, snap, tid):
        self.presses.append(tid)
        return True, "ok"

    def find(self, front, role, label):
        snap = self.snapshot(front)
        for c in snap["controls"]:
            if c["role"] == role and c["label"] == label:
                return snap, c["id"]
        return snap, None


class FakeModel:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list = []

    def __call__(self, req):
        self.requests.append(req)
        if not self.script:
            return None
        nxt = self.script.pop(0)
        return nxt(req) if callable(nxt) else nxt


def step(say, target=None, kind="click", why="Because.", goal="Enable the Maps API"):
    return {"done": False, "say": say, "target": target, "kind": kind, "why": why,
            "goal": goal, "finish": ""}


def finished(line="That's done: the Maps API is enabled."):
    return {"done": True, "say": "", "target": None, "kind": "click", "why": "",
            "goal": "Enable the Maps API", "finish": line}


def new_guide(screen, model):
    g = G.Guide()
    g.screen, g.model = screen, model
    g.said = []
    g.speaker = lambda text, lang: g.said.append((text, lang))
    g.tick, g.settle = 0.01, 0.04
    return g


# ---------------------------------------------------------------- the step request
def t_request_text():
    sc = FakeScreen()
    snap = sc.snapshot(sc.front())
    snap["controls"][2]["value"] = "maps"
    text = G.build_request("add the Maps API", "english", ["Click APIs & Services."], snap,
                           "the screen changed")
    check("the request names the goal, the steps so far and why it is asked",
          "Goal: add the Maps API" in text and "1. Click APIs & Services." in text
          and "the screen changed" in text, text[:200])
    check("each control goes as id, kind, label and where it is across and down the window",
          '1 link "APIs & Services" (9%,20%)' in text, text)
    check("a field says what it already holds", 'now says "maps"' in text, text)
    sc.page = "maps"
    snap = sc.snapshot(sc.front())
    text = G.build_request("x", "english", [], snap, "start")
    check("a password field is named as a secret field, never with a value",
          '2 field "Password" [secret field]' in text and "hunter2" not in text, text)


def t_parse_and_clean():
    check("parse_step reads JSON even with words around it",
          G.parse_step('here: {"done": false, "say": "Click Enable."} ok') ==
          {"done": False, "say": "Click Enable."})
    check("parse_step gives None for no JSON", G.parse_step("no json at all") is None)
    check("an em dash in a model sentence becomes a comma",
          G._clean_sentence("Click Enable — the blue button") == "Click Enable, the blue button")
    check("markdown is stripped from a model sentence",
          G._clean_sentence("**Click** `Enable`") == "Click Enable")


# ---------------------------------------------------------------- the session
def t_first_step_and_ring():
    sc = FakeScreen()
    m = FakeModel([step("Click APIs & Services in the menu on the left.", 1)])
    g = new_guide(sc, m)
    st = g.start("add the Maps API to my project", "english", speak=True, wait=True)
    check("the first step comes back to a caller that waits", st.get("said", "").startswith("Click APIs"),
          st)
    check("it is step 1 and the guide is active", st["n"] == 1 and st["active"] and st["status"] == "step", st)
    check("the ring is the control's own frame, in screen coordinates",
          st["ring"] == {"x": 110.0, "y": 200.0, "w": 160.0, "h": 28.0}, st["ring"])
    check("the goal is shown in the model's short words", st["goal_label"] == "Enable the Maps API", st)
    check("a caller that waits says the step itself: the guide does not also speak it",
          g.said == [], g.said)
    check("one model call for the step, with the picture", len(m.requests) == 1
          and m.requests[0]["image"] == b"\x89PNG fake", len(m.requests))
    check("Do it for me is offered for a plain click on a web page", st["can_do"] is True, st)
    # waiting: many ticks, no model calls
    time.sleep(0.3)
    check("no model call while she works (only cheap probes)",
          len(m.requests) == 1 and sc.probes > 5, (len(m.requests), sc.probes))
    g.stop()


def t_invalid_target_no_ring():
    sc = FakeScreen()
    m = FakeModel([step("Click the blue button.", 99)])
    g = new_guide(sc, m)
    st = g.start("x", "english", wait=True)
    check("an id the model was not given means no ring, never a guessed one",
          st["ring"] is None and st["say"] == "Click the blue button.", st)
    check("and no Do it for me without a target", st["can_do"] is False)
    g.stop()
    m = FakeModel([step("Look at the top.", "abc")])
    g = new_guide(FakeScreen(), m)
    st = g.start("x", "english", wait=True)
    check("a target that is not a number means no ring", st["ring"] is None, st)
    g.stop()


def t_offscreen_no_ring():
    sc = FakeScreen()
    sc.pages["home"][0] = (1, "link", "APIs & Services", (110, 1200, 160, 28))
    m = FakeModel([step("Click APIs & Services.", 1)])
    g = new_guide(sc, m)
    st = g.start("x", "english", wait=True)
    check("a control scrolled out of the window gets no ring", st["ring"] is None, st)
    g.stop()


def t_auto_advance_on_page_change():
    sc = FakeScreen()
    m = FakeModel([step("Click APIs & Services in the menu on the left.", 1),
                   step("Click Enable APIs and services at the top.", 1),
                   finished()])
    g = new_guide(sc, m)
    g.start("add the Maps API", "english", speak=True, wait=True)
    sc.page = "apis"                       # she clicked: a new page
    ok = until(lambda: g.state()["n"] == 2 and g.state()["status"] == "step")
    st = g.state()
    check("a new page is noticed and the next step worked out by itself", ok, st)
    check("the step is spoken when it came by itself", g.said and g.said[-1][0].startswith("Click Enable"),
          g.said)
    check("the model was told the steps so far and that the screen changed",
          m.requests[1]["history"] == ["Click APIs & Services in the menu on the left."]
          and "screen changed" in m.requests[1]["reason"], m.requests[1]["history"])
    check("exactly two model calls for two steps", len(m.requests) == 2, len(m.requests))
    sc.page = "maps"
    ok = until(lambda: g.state()["status"] == "done")
    st = g.state()
    check("when the model says the goal is reached, it finishes with its sentence",
          ok and st["line"] == "That's done: the Maps API is enabled.", st)
    check("finishing takes the ring away and ends the guide", st["ring"] is None and not st["active"], st)
    check("the finish line is spoken", until(lambda: any("That's done" in s for s, _ in g.said)), g.said)


def t_scroll_moves_ring_not_step():
    sc = FakeScreen()
    m = FakeModel([step("Click APIs & Services.", 1), step("unused")])
    g = new_guide(sc, m)
    g.start("x", "english", wait=True)
    sc.offset = -40.0
    ok = until(lambda: (g.state()["ring"] or {}).get("y") == 160.0)
    time.sleep(0.2)
    check("scrolling moves the ring with the control", ok, g.state()["ring"])
    check("and is not taken as the step being done", len(m.requests) == 1 and g.state()["n"] == 1,
          len(m.requests))
    g.stop()


def t_typing_does_not_advance():
    sc = FakeScreen()
    m = FakeModel([step("Type maps in the Search box at the top.", 3, kind="type"),
                   step("Click the first result.")])
    g = new_guide(sc, m)
    g.start("x", "english", wait=True)
    sc.focused = 3
    time.sleep(0.1)                   # the watcher's baseline has her in the field
    g._s["rebase"] = True
    time.sleep(0.1)
    sc.values[3] = "m"
    time.sleep(0.1)
    sc.values[3] = "map"
    sc.struct = 4                     # the suggestions drop down under the box
    time.sleep(0.3)
    check("typing in the field (and its suggestions) is not the step being done",
          len(m.requests) == 1, len(m.requests))
    sc.focused = None                 # she left the field
    ok = until(lambda: len(m.requests) == 2)
    check("leaving the field is", ok, len(m.requests))
    g.stop()


def t_done_button():
    sc = FakeScreen()
    m = FakeModel([step("Look for Library on the left.", None, kind="look"),
                   step("Click Library.", 2)])
    g = new_guide(sc, m)
    g.start("x", "english", wait=True)
    st = g.next_step(wait=True)
    check("Done asks for the next step at once, even with no change on screen",
          st["n"] == 2 and st.get("said") == "Click Library.", st)
    check("and says why it asked", "did the last step" in m.requests[1]["reason"], m.requests[1]["reason"])
    g.stop()


def t_same_step_not_counted_twice():
    sc = FakeScreen()
    m = FakeModel([step("Click APIs & Services.", 1), step("Click APIs & Services.", 1)])
    g = new_guide(sc, m)
    g.start("x", "english", speak=True, wait=True)
    g.next_step()
    until(lambda: len(m.requests) == 2 and g.state()["status"] == "step")
    time.sleep(0.05)
    st = g.state()
    check("the same step again keeps its number and is not said twice",
          st["n"] == 1 and not g.said, (st["n"], g.said))
    g.stop()


def t_secure_field_no_picture():
    sc = FakeScreen()
    sc.page = "maps"
    sc.secure_focus = True
    m = FakeModel([step("Type your password in the Password box.", 2, kind="click")])
    g = new_guide(sc, m)
    st = g.start("x", "english", wait=True)
    check("no picture goes to the model while a password field has the keyboard",
          m.requests[0]["image"] is None and sc.shots == 0, sc.shots)
    check("a step aimed at a secret field is a secret step, whatever the model said",
          st["kind"] == "secret", st["kind"])
    check("and is never done for her", st["can_do"] is False)
    check("the password field's contents never reach the request",
          "[secret field]" in m.requests[0]["prompt"], m.requests[0]["prompt"])
    g.stop()


def t_do_it_for_me():
    sc = FakeScreen()
    m = FakeModel([step("Click APIs & Services.", 1), step("next")])
    g = new_guide(sc, m)
    g.start("x", "english", wait=True)
    r = g.do_it()
    check("Do it for me presses the ringed control", r["ok"] and sc.presses == [1], r)
    g.stop()
    for label, why in (("Create credentials", "a credential"), ("Pay now", "a payment"),
                       ("Delete project", "a delete")):
        sc = FakeScreen()
        sc.pages["home"][1] = (2, "button", label, (600, 120, 170, 34))
        g = new_guide(sc, FakeModel([step(f"Click {label}.", 2)]))
        st = g.start("x", "english", wait=True)
        r = g.do_it()
        check(f"Do it for me is never offered for {why} ({label})",
              st["can_do"] is False and not r["ok"] and sc.presses == [], (st["can_do"], r))
        g.stop()
    sc = FakeScreen(browser=False)
    g = new_guide(sc, FakeModel([step("Click APIs & Services.", 1)]))
    st = g.start("x", "english", wait=True)
    check("nor outside a web browser", st["can_do"] is False, st)
    g.stop()


def t_back_why_repeat_stop():
    sc = FakeScreen()
    m = FakeModel([step("Click APIs & Services.", 1, why="That is where the APIs live."),
                   step("Click Enable APIs and services.", 1)])
    g = new_guide(sc, m)
    g.start("x", "english", wait=True)
    sc.page = "apis"
    until(lambda: g.state()["n"] == 2)
    sc.page = "home"                  # she went back in the browser herself
    time.sleep(0.02)
    calls = len(m.requests)
    st = g.back()
    check("go back gives the step before, without a model call",
          st.get("said") == "Click APIs & Services." and st["n"] == 1 and len(m.requests) == calls, st)
    check("and rings its control again when it is still there", st["ring"] is not None, st)
    st = g.explain()
    check("why says the step's reason, without a model call",
          st.get("said") == "That is where the APIs live." and len(m.requests) == calls, st)
    check("repeat says the step again", g.repeat().get("said") == "Click APIs & Services.")
    st = g.stop()
    check("stop ends it, takes the ring away and says so",
          st["status"] == "stopped" and st["ring"] is None and "stopped" in st["said"], st)
    time.sleep(0.1)
    sc.page = "maps"
    time.sleep(0.2)
    check("nothing more is asked of the model after stop", len(m.requests) == calls, len(m.requests))
    check("a command with no guide running says so",
          "not guiding" in g.next_step().get("said", ""), g.next_step())


def t_back_on_first_step():
    g = new_guide(FakeScreen(), FakeModel([step("Click APIs & Services.", 1)]))
    g.start("x", "english", wait=True)
    check("back on the first step says there is nothing to go back to",
          "first step" in g.back().get("said", ""))
    g.stop()


def t_stop_during_thinking_discards():
    sc = FakeScreen()
    hold = {"go": False}

    def slow(req):
        until(lambda: hold["go"], 2)
        return step("Late step.", 1)
    m = FakeModel([slow])
    g = new_guide(sc, m)
    g.start("x", "english")
    until(lambda: len(m.requests) == 1)
    g.stop()
    hold["go"] = True
    time.sleep(0.1)
    st = g.state()
    check("a step that arrives after stop is dropped, not shown", st["status"] == "stopped"
          and st["say"] == "" and st["ring"] is None, st)


def t_caps_and_failures():
    sc = FakeScreen()
    m = FakeModel([step(f"Step {i}.", 1) for i in range(40)])
    g = new_guide(sc, m)
    g.start("x", "english", wait=True)
    for _ in range(G.MAX_STEPS + 2):
        g.next_step(wait=True)
    st = g.state()
    check(f"no more than {G.MAX_STEPS} steps", st["status"] == "failed" and st["n"] == G.MAX_STEPS
          and len(m.requests) == G.MAX_STEPS, (st["status"], st["n"], len(m.requests)))
    g = new_guide(FakeScreen(), FakeModel([None]))
    st = g.start("x", "hebrew", wait=True)
    check("no answer from the model ends it with a line in her language",
          st["status"] == "failed" and "להדריך" in st["said"], st)

    class Locked(FakeScreen):
        def snapshot(self, front):
            return {"error": "no_access", "controls": []}
    st = new_guide(Locked(), FakeModel([step("x")])).start("x", "english", wait=True)
    check("no Accessibility says how to turn it on", "Accessibility" in st["said"], st)

    class Nothing(FakeScreen):
        def front(self):
            return None
    st = new_guide(Nothing(), FakeModel([step("x")])).start("x", "english", wait=True)
    check("no window to guide in says so", "open window" in st["said"], st)


def t_offer():
    g = new_guide(FakeScreen(), FakeModel([step("Click APIs & Services.", 1)]))
    o = g.offer("how do I add an API key here?", "hebrew")
    check("an offer carries the chip's label in her language", o["label"] == "תדריכי אותי", o)
    check("the state shows the live offer to the page", g.state()["offer"]["id"] == o["id"])
    check("a wrong offer id is refused", g.take_offer("nope") is None)
    got = g.take_offer(o["id"])
    check("the right one gives the question back, once", got["goal"].startswith("how do I")
          and g.take_offer(o["id"]) is None)
    g.offer("q", "english")
    g._offer["at"] -= G.OFFER_TTL + 1
    check("an old offer has lapsed", g.state()["offer"] is None and g.take_offer() is None)


def t_state_is_public_only():
    g = new_guide(FakeScreen(), FakeModel([step("Click APIs & Services.", 1)]))
    st = g.start("x", "english", wait=True)
    blob = json.dumps(g.state())
    check("state() carries no refs, no snapshot and no screen text",
          "refs" not in blob and "controls" not in blob and "history" not in blob, blob[:200])
    g.stop()


# ---------------------------------------------------------------- the router
class FakeJev:
    """Answers every question with its first option or no, except the overrides."""

    def __init__(self):
        self.over = {}
        self.calls = 0
        self.input_tokens = 0
        self.busy_ms = 0.0
        self.cost_usd = 0.0
        self.asked = []

    def ask(self, state, qs):
        self.calls += 1
        self.asked.append((state, qs))
        out = {}
        for k, q in qs.items():
            if k in self.over:
                v = self.over[k]
                if q["type"] == "choice":
                    keys = list(q["criteria"])
                    pick, conf = v if isinstance(v, tuple) else (v, 0.9)
                    out[k] = {"choice": pick, "confidence": conf,
                              "probabilities": {x: (conf if x == pick else 0.0) for x in keys}}
                elif q["type"] == "score":
                    out[k] = {"score": v}
                else:
                    out[k] = {"noul": v}
                continue
            if q["type"] == "choice":
                keys = list(q["criteria"])
                out[k] = {"choice": keys[0], "confidence": 1.0,
                          "probabilities": {x: (1.0 if i == 0 else 0.0) for i, x in enumerate(keys)}}
            elif q["type"] == "score":
                out[k] = {"score": 0.0}
            else:
                out[k] = {"noul": 0.0}
        return out


def t_router():
    from savta import router, profile as prof
    prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done", "name": "Test",
               "language": "english", "city": "Haifa"})
    router.due_briefing = lambda: False
    router.get_contacts = lambda *a, **k: ["Dana Levi"]
    mac.screen_locked = lambda: False
    said = []
    mac.say = lambda text, lang="english": said.append(text)
    sc = FakeScreen()
    m = FakeModel([step("Click APIs & Services in the menu on the left.", 1),
                   step("Click Enable APIs and services.", 1), step("x", 1)])
    router.GUIDE.screen, router.GUIDE.model = sc, m
    router.GUIDE.speaker = lambda text, lang: said.append(text)
    router.GUIDE.tick, router.GUIDE.settle = 0.01, 0.04
    router.LLM_CLIENT.answer = lambda *a, **k: "Open Credentials, then choose Create credentials."
    j = FakeJev()
    base = {"intent": ("help", 0.9), "is_complete": 0.95, "language": ("english", 0.99)}

    # an ordinary request: no chip, no guide
    j.over = dict(base)
    r = router.handle(j, "what can you do", speak=False)
    check("an ordinary request gets no Guide me chip", "guide_offer" not in r and r["did"] == "helped", r)
    check("the guidance question rides in the same single request",
          j.calls == 1 and "guidance" in j.asked[-1][1], j.calls)
    check("the guide's commands are not asked when no guide runs",
          "guide_command" not in j.asked[-1][1])

    # a how-to about the screen: answered as usual, plus the chip
    j.over = {**base, "guidance": ("how_to_here", 0.8)}
    r = router.handle(j, "how do I add an API key here?", speak=False)
    check("a how-to question about the screen is answered as a question (not with MicMic's "
          "feature list) and gets the chip",
          r["did"] == "answered" and r["say"].startswith("Open Credentials")
          and (r.get("guide_offer") or {}).get("label") == "Guide me", r)
    check("the chip costs no extra call", j.calls == 2, j.calls)
    j.over = {**base, "guidance": ("how_to_here", 0.3)}
    r = router.handle(j, "how do I do it", speak=False)
    check("below the gate there is no chip", "guide_offer" not in r, r.get("guide_offer"))

    # "guide me" right after the chip: guides toward the question that got it
    j.over = {**base, "guidance": ("how_to_here", 0.8)}
    router.handle(j, "how do I add an API key here?", speak=False)
    j.over = {**base, "guidance": ("walk_me_through", 0.9)}
    r = router.handle(j, "guide me", speak=True)
    check("saying guide me after the chip starts the guide with the first step",
          r["did"] == "guide_step" and r["say"].startswith("Click APIs"), r)
    check("its goal is the question that got the chip",
          m.requests[0]["goal"] == "how do I add an API key here?", m.requests[0]["goal"])
    check("the reply carries the guide's state for the card",
          (r.get("guide") or {}).get("n") == 1 and r["guide"]["ring"] is not None, r.get("guide"))
    check("the first step is spoken once, by the reply", said.count(r["say"]) == 1, said)

    # while guiding: the commands ride along, and act
    j.over = {**base, "guide_command": ("why", 0.9)}
    r = router.handle(j, "why?", speak=False)
    check("while guiding, the guide's commands ride in the same request",
          "guide_command" in j.asked[-1][1] and "guide_in_progress" in j.asked[-1][0], j.asked[-1][0].keys())
    check("why answers with the step's reason", r["did"] == "guide_why" and r["say"] == "Because.", r)
    j.over = {**base, "guide_command": ("next", 0.9)}
    r = router.handle(j, "done", speak=False)
    check("next says the next step", r["did"] == "guide_step" and r["say"].startswith("Click Enable"), r)
    j.over = {**base, "guide_command": ("next", 0.3)}
    r = router.handle(j, "what can you do", speak=False)
    check("a weak command is an ordinary request, and the guide carries on",
          r["did"] == "helped" and router.GUIDE.active(), r["did"])
    check("no chip while a guide runs", "guide_offer" not in r)
    j.over = {**base, "guide_command": ("stop", 0.9)}
    r = router.handle(j, "stop", speak=False)
    check("stop ends the guide", r["did"] == "guide_stopped" and not router.GUIDE.active(), r)

    # explicit, from nothing
    m.script = [step("Click APIs & Services in the menu on the left.", 1)]
    j.over = {**base, "intent": ("do_online", 0.8), "guidance": ("walk_me_through", 0.85)}
    r = router.handle(j, "walk me through adding the Maps API", speak=False)
    check("walk me through starts guiding even when the intent reads as do_online",
          r["did"] == "guide_step" and m.requests[-1]["goal"] == "walk me through adding the Maps API", r)
    router.GUIDE.stop()
    # Bench gd-002, measured live: "can you guide me through ..." reads as help (0.93)
    # with guidance "none" at only 0.32-0.41, and got the feature list 3/3.
    m.script = [step("Open System Settings and click Bluetooth.", 1)]
    j.over = {**base, "intent": ("help", 0.93), "guidance": ("none", 0.35)}
    r = router.handle(j, "can you guide me through connecting my bluetooth headphones",
                      speak=False)
    check("'can you guide me through X' starts the guide for X, not the feature list",
          r["did"] == "guide_step" and m.requests[-1]["goal"].endswith("bluetooth headphones"), r)
    router.GUIDE.stop()
    m.script = [step("Open System Settings and click Bluetooth.", 1)]
    j.over = {**base, "intent": ("help", 0.98), "guidance": ("none", 0.39)}
    r = router.handle(j, "תדריכי אותי איך לחבר אוזניות בלוטות'", speak=False)
    check("the same in Hebrew", r["did"] == "guide_step", r)
    router.GUIDE.stop()
    # Off the computer (measured: guidance "none" 0.94-1.00): the words are not enough.
    n0 = len(m.requests)
    j.over = {**base, "intent": ("help", 0.5), "guidance": ("none", 0.94)}
    r = router.handle(j, "can you guide me through baking bread", speak=False)
    check("guiding through something away from the computer is not a guide",
          not str(r["did"]).startswith("guide") and len(m.requests) == n0, r["did"])
    j.over = {**base, "intent": ("message", 0.9), "guidance": ("walk_me_through", 0.9)}
    n0 = len(m.requests)
    r = router.handle(j, "send Dana a message", speak=False)
    check("a message is never taken for a guide", not str(r["did"]).startswith("guide")
          and len(m.requests) == n0, r["did"])


# ---------------------------------------------------------------- the native ring
def t_overlay_follower():
    sys.path.insert(0, str(ROOT / "native"))
    try:
        import guide_overlay as O
    except Exception as e:  # noqa: BLE001
        return check("native/guide_overlay.py imports", False, repr(e))
    check("an accessibility frame becomes an AppKit frame (y flipped on the main screen)",
          O.to_cocoa({"x": 110, "y": 200, "w": 160, "h": 28}, 900) == (110.0, 672.0, 160.0, 28.0))

    class Ov:
        def __init__(self):
            self.calls = []

        def show(self, ring):
            self.calls.append(("show", ring))

        def hide(self):
            self.calls.append(("hide",))

    class Bar:
        def __init__(self):
            self.visible, self.shown, self.edges, self._edge = False, 0, [], "top"

        def is_visible(self):
            return self.visible

        def show(self):
            self.shown += 1
            self.visible = True

        def set_edge(self, e):
            self.edges.append(e)
            self._edge = e

        def frame(self):
            return None

    ov, bar = Ov(), Bar()
    f = O.GuideFollower("http://x", ov, bar=bar, display=lambda: "bar")
    ring = {"x": 110, "y": 200, "w": 160, "h": 28}
    f.apply({"active": True, "status": "thinking", "id": "a", "n": 0, "ring": None})
    check("a guide starting brings the strip up (display: bar)", bar.shown == 1, bar.shown)
    check("no ring while it is thinking", ov.calls == [], ov.calls)
    f.apply({"active": True, "status": "step", "id": "a", "n": 1, "ring": ring})
    check("a step with a frame rings it", ov.calls == [("show", ring)], ov.calls)
    f.apply({"active": True, "status": "step", "id": "a", "n": 1, "ring": ring})
    check("the same ring is not redrawn every poll", len(ov.calls) == 1, ov.calls)
    bar.visible = False
    f.apply({"active": True, "status": "step", "id": "a", "n": 1, "ring": ring})
    check("a strip she hid is not pushed back up on the same step", bar.shown == 1, bar.shown)
    f.apply({"active": True, "status": "step", "id": "a", "n": 2, "ring": None})
    check("a step with no frame takes the ring away (never a wrong ring)", ov.calls[-1] == ("hide",), ov.calls)
    check("and a new step brings the strip back into view", bar.shown == 2, bar.shown)
    f.apply({"active": False, "status": "done", "id": "a", "n": 2, "ring": ring})
    check("an ended guide never shows a ring", ov.calls[-1] == ("hide",) and len(ov.calls) == 2, ov.calls)
    f.apply(None)
    check("the server being away changes nothing", len(ov.calls) == 2)

    bar2 = Bar()
    f2 = O.GuideFollower("http://x", Ov(), bar=bar2, display=lambda: "none")
    f2.apply({"active": True, "status": "step", "id": "b", "n": 1, "ring": ring})
    check("with nothing chosen to appear, the strip stays away (the ring still shows)",
          bar2.shown == 0 and f2.overlay.calls, (bar2.shown, f2.overlay.calls))

    class Panel:
        def __init__(self):
            self.shows = []

        def show(self, activate=True):
            self.shows.append(activate)
    pn = Panel()
    f3 = O.GuideFollower("http://x", Ov(), panel=pn, display=lambda: "panel")
    f3.apply({"active": True, "status": "step", "id": "c", "n": 1, "ring": ring})
    check("display: panel shows the window without activating MicMic", pn.shows == [False], pn.shows)

    # bar.py needs WebKit, which only native/.venv has.
    code = ("import sys; sys.path.insert(0, 'native'); import bar as B, Foundation;"
            "v = Foundation.NSMakeRect(0, 0, 1440, 875);"
            "t, b = B._bar_frame(v, 120), B._bar_frame(v, 120, 'bottom');"
            "print(round(t.origin.y + t.size.height), round(b.origin.y))")
    py = ROOT / "native" / ".venv" / "bin" / "python3"
    if not py.exists():
        return check("native/.venv exists for the strip's placement check", False, str(py))
    import subprocess
    out = subprocess.run([str(py), "-c", code], cwd=ROOT, capture_output=True, text=True).stdout.split()
    check("the strip can sit at the bottom as far up as it sits down from the top",
          out == ["770", "105"], out)


# ---------------------------------------------------------------- copy
def t_copy():
    dash = re.compile("[—–]")
    bad = [f"{k}/{lang}" for k, t in G.SAY.items() for lang, v in t.items() if dash.search(v)]
    check("no em or en dash in anything the guide says", not bad, bad)
    check("every guide line exists in all four languages",
          all(set(t) == {"hebrew", "arabic", "russian", "english"} for t in G.SAY.values()))
    html = (ROOT / "savta" / "web" / "index.html").read_text()
    keys = ("guideMe", "gStep", "gDone", "gDoIt", "gStop", "gWhy", "gLooking", "gFinished",
            "gClose", "gSecret", "gStopped")
    for lang in ("he_f", "en", "ar", "ru"):
        block = html.split(f"  {lang}:{{", 1)[1].split("\n  }", 1)[0] if f"  {lang}:{{" in html else ""
        missing = [k for k in keys if f"{k}:" not in block]
        check(f"the guide card's words exist in {lang}", not missing, missing)
    strings = re.findall(r'\bg[A-Z]\w*:"([^"]*)"|guideMe:"([^"]*)"', html)
    flat = [a or b for a, b in strings]
    check("no em or en dash in the guide card's words", not any(dash.search(x) for x in flat),
          [x for x in flat if dash.search(x)])


def main():
    tests = [t_request_text, t_parse_and_clean, t_first_step_and_ring, t_invalid_target_no_ring,
             t_offscreen_no_ring, t_auto_advance_on_page_change, t_scroll_moves_ring_not_step,
             t_typing_does_not_advance, t_done_button, t_same_step_not_counted_twice,
             t_secure_field_no_picture, t_do_it_for_me, t_back_why_repeat_stop, t_back_on_first_step,
             t_stop_during_thinking_discards, t_caps_and_failures, t_offer, t_state_is_public_only,
             t_router, t_overlay_follower, t_copy]
    for t in tests:
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran to the end", False, repr(e)[:300])
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print("  FAILED:", f)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
