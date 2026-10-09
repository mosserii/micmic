#!/usr/bin/env python3
"""Guide mode on forms (savta/actions/guide.py), with a fake screen, a fake model and a
fake voice. No window, no Accessibility, no Jev, no Gemini.

    cd <worktree> && uv run python tests/test_guide_forms.py

From the Larkline check-in rehearsal (2026-10-08): a text field was a click step
("Click the Confirmation number field"), the click into it was taken for the step being
done, the same step came back three times and then nothing for 45 s, and "Do it for me"
was refused on every field. What is held here: a field step says what to enter and is a
field step whatever the model called it; it moves on when the field's value changed and
she left it or stopped typing, never on the click into it, and never says the same
field again; a click with nothing typed waits quietly, then one nudge; the next fields
on the page come without a model call; "Do it for me" types a value from her profile,
reads back one she said before typing it, asks for one it does not know, never invents
one, never types into a password, card, PIN or one-time-code field, and never presses
a button that finishes it; in the router, what she says to a field step is the value
or the yes, in one Jev request, and "help me <task>" on a page with a form is a guide.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The guide suite's fakes and helpers (it sets the scratch state dir and the offline
# model endpoint on import).
import test_guide as TG  # noqa: E402
from test_guide import FakeJev, FakeModel, check, finished, until  # noqa: E402

from savta.actions import guide as G  # noqa: E402
from savta.actions import mac  # noqa: E402

WIN = TG.WIN


# ---------------------------------------------------------------- the fake form
class FormScreen(TG.FakeScreen):
    """The Larkline check-in, as the guide reads it: fields with hints, a list, boxes
    and buttons. Values change when she types (type_in) or MicMic fills one (fill)."""

    def __init__(self, browser=True):
        super().__init__(browser)
        self.page = "checkin"
        self.fills: list = []
        self.checked: dict = {}
        self.pages = {
            "checkin": [(1, "field", "Confirmation number", (300, 200, 400, 40), "6 letters and numbers"),
                        (2, "field", "Last name", (300, 260, 400, 40), ""),
                        (3, "button", "Find my trip", (300, 320, 200, 40), "")],
            "docs": [(1, "menu", "Nationality", (300, 150, 300, 36), ""),
                     (2, "field", "Passport number", (300, 200, 300, 36), "6 to 9 letters and numbers, no spaces."),
                     (3, "field", "Month", (300, 250, 80, 36), "MM"),
                     (4, "field", "Day", (390, 250, 80, 36), "DD"),
                     (5, "field", "Year", (480, 250, 100, 36), "YYYY"),
                     (6, "field", "City", (300, 300, 300, 36), ""),
                     (7, "checkbox", "I confirm these details are correct", (300, 350, 400, 30), ""),
                     (8, "button", "Save and continue", (300, 400, 200, 40), "")],
            "review": [(1, "button", "Check in now", (300, 300, 200, 40), ""),
                       (2, "button", "Continue to payment", (300, 360, 200, 40), "")],
            "pay": [(1, "field", "Card number", (300, 200, 300, 36), ""),
                    (2, "field", "Security code", (300, 250, 100, 36), "3 digits on the back"),
                    (3, "field", "Password", (300, 300, 300, 36), ""),
                    (4, "field", "PIN", (300, 350, 100, 36), ""),
                    (5, "field", "Enter the one-time code we sent you", (300, 400, 200, 36), ""),
                    (6, "button", "Pay now", (300, 450, 200, 40), "")],
            "buttons": [(1, "button", "Book", (300, 200, 200, 40), ""),
                        (2, "link", "Flight status", (300, 260, 200, 40), "")],
        }

    def snapshot(self, front):
        self.snaps += 1
        controls, refs = [], {}
        for cid, role, label, (x, y, w, h), hint in self.pages[self.page]:
            secure = bool(re.search(r"password|card number|security code|\bpin\b|one-time|passport",
                                    label, re.I))
            value = self.values.get((self.page, cid), "")
            controls.append({"id": cid, "role": role, "ax_role": "", "label": label,
                             "value": "" if secure else value, "hint": hint,
                             "checked": self.checked.get((self.page, cid)) if role == "checkbox" else None,
                             "frame": (x, y + self.offset, w, h), "secure": secure,
                             "typed": role == "field", "pick": role == "menu",
                             **({"filled": bool(value)} if secure and role == "field" else {})})
            refs[cid] = (self.page, cid)
        raw = {cid: self.values.get((self.page, cid)) for cid, role, *_ in self.pages[self.page]
               if role in ("field", "menu")}
        return {"app": front["app"], "pid": 42, "window": self.page, "url": f"https://lark/{self.page}",
                "title": self.page, "controls": controls, "refs": refs, "raw": raw,
                "text": "Find your trip | Where do I find my confirmation number?",
                "win": WIN, "secure_focus": self.secure_focus, "browser": self.browser,
                "error": None}

    def probe(self, snap, tid):
        self.probes += 1
        ref = (snap.get("refs") or {}).get(tid)
        alive = ref is not None and ref[0] == self.page and tid not in self.gone
        frame, state = None, None
        if alive:
            for cid, _r, _l, (x, y, w, h), _h in self.pages[self.page]:
                if cid == tid:
                    frame = (x, y + self.offset, w, h)
            v = self.values.get((self.page, tid))
            if (self.page, tid) in self.checked:
                v = int(self.checked[(self.page, tid)])
            state = (v, None, None)
        return {"pid": 42, "title": self.page, "url": f"https://lark/{self.page}",
                "struct": [self.struct], "alive": alive if ref else None, "state": state,
                "focused": self.focused == tid, "frame": frame, "win": WIN}

    def fill(self, snap, tid, value):
        self.fills.append((tid, value))
        ctl = next(c for c in snap["controls"] if c["id"] == tid)
        if ctl["secure"] or not ctl["typed"]:
            return False, "refused"
        self.values[(self.page, tid)] = value
        return True, "ok"

    # what she does herself
    def type_in(self, tid, text, step=0.02):
        self.focused = tid
        time.sleep(step)
        for i in range(1, len(text) + 1):
            self.values[(self.page, tid)] = text[:i]
            time.sleep(step)


def field(say, target, kind="type", name="", ask="", value="", profile="", then=None, why="So we can find you."):
    return {"done": False, "say": say, "target": target, "kind": kind, "why": why,
            "goal": "Check in for the flight", "finish": "", "field": name, "ask": ask,
            "value": value, "profile": profile, "then": then or []}


def click(say, target, why="Because."):
    return field(say, target, kind="click", why=why)


CONF_LINE = "Type your 6-character confirmation code. It's in your booking email."


def new_guide(screen, model):
    g = TG.new_guide(screen, model)
    g.field_idle, g.nudge_after = 0.25, 10.0
    return g


def set_profile(name):
    from savta import profile as prof
    prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done", "name": name,
               "language": "english", "city": "Haifa"})


# ---------------------------------------------------------------- the step request
def t_request_carries_field_details():
    sc = FormScreen()
    snap = sc.snapshot(sc.front())
    text = G.build_request("check in", "english", [], snap, "start",
                           said=["guide me to check in, my code is K7QM2P"])
    check("a field goes with its hint, so the step can say what to enter",
          '1 field "Confirmation number" (empty) hint "6 letters and numbers"' in text, text)
    check("what she said while guided goes with the step call (a code said earlier)",
          'What they said while being guided' in text and "K7QM2P" in text, text)
    sc.page = "docs"
    sc.checked[("docs", 7)] = False
    text = G.build_request("check in", "english", [], sc.snapshot(sc.front()), "start")
    check("a box says whether it is ticked", '"I confirm these details are correct" [not ticked]' in text, text)
    check("a passport field is a secret field: no value, but its hint still helps",
          '2 field "Passport number" [secret field] (empty) hint "6 to 9 letters' in text, text)
    check("the model is told to say WHAT to enter in a field, never only to click it",
          "say WHAT to enter" in G.SYSTEM and "never only to click or type in the field" in G.SYSTEM)
    check("and never to make a value up", "Never guess a value, never make one up" in G.SYSTEM)


# ---------------------------------------------------------------- the field step
def t_field_step_says_what_to_enter():
    sc = FormScreen()
    # The rehearsal's model called the field a click: the control decides.
    m = FakeModel([field(CONF_LINE, 1, kind="click", name="your confirmation code",
                         ask="What's your confirmation code?")])
    g = new_guide(sc, m)
    st = g.start("guide me to check in for my flight", "english", wait=True)
    check("a field step's words say what to enter and where it comes from",
          st["said"] == CONF_LINE and st["say"] == CONF_LINE, st)
    check("a text field is a type step whatever the model called it", st["kind"] == "type", st["kind"])
    check("the card knows what goes there", st["field"] == "your confirmation code", st)
    check("Do it for me is offered on an ordinary field", st["can_do"] is True, st)
    g.stop()


def t_click_into_field_waits_quietly_then_one_nudge():
    sc = FormScreen()
    m = FakeModel([field(CONF_LINE, 1, name="your confirmation code"), click("unused", 3)])
    g = new_guide(sc, m)
    g.nudge_after = 0.6
    g.start("check in", "english", speak=True, wait=True)
    sc.focused = 1                    # she clicks into the field and types nothing
    time.sleep(0.4)
    check("a click into the field is not the step being done: no model call, nothing said",
          len(m.requests) == 1 and g.said == [] and g.state()["n"] == 1, (len(m.requests), g.said))
    ok = until(lambda: len(g.said) == 1, 2.0)
    time.sleep(0.5)
    check("after a while, one gentle nudge, not the step again",
          ok and len(g.said) == 1 and g.said[0][0].startswith("Take your time")
          and "tell me" in g.said[0][0] and CONF_LINE not in g.said[0][0], g.said)
    check("and still no model call", len(m.requests) == 1, len(m.requests))
    g.stop()


def t_advance_on_value_change():
    sc = FormScreen()
    m = FakeModel([field(CONF_LINE, 1, name="your confirmation code"),
                   field("Type your last name, as on your booking.", 2, name="your last name")])
    g = new_guide(sc, m)
    g.start("check in", "english", speak=True, wait=True)
    sc.type_in(1, "K7QM2P")
    sc.focused = 2                    # she tabs to the next field
    ok = until(lambda: g.state()["n"] == 2 and g.state()["status"] == "step")
    check("leaving the field she filled moves on to the next step", ok, g.state())
    check("the model was told she filled in the field",
          "filled in the field" in m.requests[-1]["reason"], m.requests[-1]["reason"])
    check("the next step is spoken once", g.said == [("Type your last name, as on your booking.", "english")],
          g.said)
    # Typed and waiting, still in the field: it moves on after a short pause.
    m.script = [click("Click Find my trip below the fields.", 3)]
    sc.type_in(2, "Rivera")
    ok = until(lambda: g.state()["n"] == 3, 3.0)
    check("typing and then waiting (still in the field) moves on too", ok, g.state())
    check("one model call per field, none while she typed", len(m.requests) == 3, len(m.requests))
    g.stop()


def t_no_repeated_step():
    sc = FormScreen()
    m = FakeModel([field(CONF_LINE, 1, name="your confirmation code"),
                   # the rehearsal: the same field again, worded a little differently
                   field("Type the confirmation code in the field on the right.", 1),
                   field("Type the confirmation code in the middle field.", 1)])
    g = new_guide(sc, m)
    g.start("check in", "english", speak=True, wait=True)
    sc.struct = 9                     # the page shifts under her click
    sc.focused = 1
    time.sleep(0.3)
    sc.type_in(1, "K7QM")
    until(lambda: len(m.requests) >= 2, 2.0)
    time.sleep(0.3)
    st = g.state()
    check("the same field never comes back as a new step: same number, same words, nothing said",
          st["n"] == 1 and st["say"] == CONF_LINE and g.said == [], (st["n"], st["say"], g.said))
    check("and it is still a type step on that field", st["kind"] == "type" and st["status"] == "step", st)
    g.stop()


def t_planned_fields_without_model_calls():
    sc = FormScreen()
    sc.page = "docs"
    then = [{"say": "Enter the month your passport expires, as two digits.", "target": 3, "kind": "type",
             "field": "expiry month"},
            {"say": "Now the day.", "target": 4, "kind": "type", "field": "expiry day"},
            {"say": "Now the year, all four digits.", "target": 5, "kind": "type", "field": "expiry year"},
            {"say": "Click Save and continue.", "target": 8, "kind": "click"}]   # a button: not planned
    m = FakeModel([field("Enter your passport number exactly as printed on the photo page.", 2,
                         name="your passport number", then=then),
                   field("Type the city where you will stay.", 6, name="the city")])
    g = new_guide(sc, m)
    st = g.start("check in", "english", speak=True, wait=True)
    check("the passport number is a secret step: hers alone, never typed for her",
          st["kind"] == "secret" and st["can_do"] is False, st)
    sc.type_in(2, "P00428117")        # its words are never read, only that it changed
    sc.focused = 3
    ok = until(lambda: g.state()["n"] == 2)
    check("the next field the model planned comes without another model call",
          ok and len(m.requests) == 1 and g.state()["say"].startswith("Enter the month"), g.state())
    sc.values[("docs", 4)] = "14"     # she already filled the day
    sc.type_in(3, "03")
    sc.focused = 5
    ok = until(lambda: g.state()["n"] == 3)
    check("a planned field she already filled is skipped",
          ok and g.state()["say"].startswith("Now the year") and len(m.requests) == 1, g.state())
    sc.type_in(5, "2031")
    sc.focused = None
    ok = until(lambda: len(m.requests) == 2 and g.state()["n"] == 4)
    check("after the plan, and never for a button in it, the model is asked again",
          ok and g.state()["say"] == "Type the city where you will stay.", g.state())
    check("each planned step was spoken once, in order",
          [s for s, _ in g.said] == ["Enter the month your passport expires, as two digits.",
                                     "Now the year, all four digits.", "Type the city where you will stay."],
          g.said)
    g.stop()

    # A new page drops the plan.
    sc = FormScreen()
    sc.page = "docs"
    m = FakeModel([field("Enter your passport number.", 2, then=then[:1]), click("Click Find my trip.", 3)])
    g = new_guide(sc, m)
    g.start("check in", "english", wait=True)
    sc.page = "checkin"
    ok = until(lambda: len(m.requests) == 2)
    check("a new page drops the planned fields and asks the model", ok and g.state()["n"] == 2, g.state())
    g.stop()


def t_choose_step():
    sc = FormScreen()
    sc.page = "docs"
    sc.values[("docs", 1)] = "Select a country"
    m = FakeModel([field("Pick your nationality from the list at the top.", 1, kind="click",
                         name="your nationality"),
                   field("Enter your passport number.", 2)])
    g = new_guide(sc, m)
    st = g.start("check in", "english", speak=True, wait=True)
    check("a list is a choose step", st["kind"] == "choose", st["kind"])
    r = g.do_it(speak=False)
    check("Do it for me on a list says to pick it, and picks nothing",
          not r["ok"] and "pick this one from the list" in r["said"] and sc.fills == [], r)
    sc.values[("docs", 1)] = "United States"
    ok = until(lambda: g.state()["n"] == 2)
    check("picking an answer moves on", ok, g.state())
    g.stop()


def t_other_option_in_the_set():
    sc = FormScreen()
    sc.page = "docs"
    sc.pages["docs"] += [(9, "option", "Female", (300, 450, 100, 30), ""),
                         (10, "option", "Male", (420, 450, 100, 30), ""),
                         (11, "option", "Unspecified (X)", (540, 450, 120, 30), "")]
    m = FakeModel([click("Choose the sex shown on your passport.", 9), field("Type the city.", 6)])
    g = new_guide(sc, m)
    st = g.start("check in", "english", speak=True, wait=True)
    check("an option of hers is never picked for her", st["can_do"] is False
          and g.do_it(speak=False)["said"].startswith("This choice is yours"), st)
    time.sleep(0.1)
    sc.checked[("docs", 10)] = True   # she picks Male, not the ringed Female
    ok = until(lambda: g.state()["n"] == 2)
    check("picking another option of the same set than the ringed one is the step done", ok, g.state())
    g.stop()


def t_field_group_named():
    snap = FormScreen().snapshot({"app": "Google Chrome"})
    snap["controls"][0]["group"] = "Expiration date"
    text = G.build_request("x", "english", [], snap, "start")
    check("a field's group (a fieldset's legend) goes with it, to tell Month from Month",
          '"Confirmation number" (empty) hint "6 letters and numbers" in "Expiration date"' in text, text)


# ---------------------------------------------------------------- do it for me
def t_do_types_profile_value():
    set_profile("Alex Rivera")
    sc = FormScreen()
    m = FakeModel([field("Type your last name.", 2, name="your last name", profile="last_name"),
                   click("Click Find my trip.", 3)])
    g = new_guide(sc, m)
    g.start("check in", "english", wait=True)
    r = g.do_it(speak=False)
    check("Do it for me types her own last name from her profile, at once",
          r["ok"] and sc.fills == [(2, "Rivera")] and r["said"] == "Typed it in.", (r, sc.fills))
    ok = until(lambda: g.state()["n"] == 2)
    check("and the next step comes without waiting for her", ok, g.state())
    check("nothing was pressed while typing", sc.presses == [], sc.presses)
    g.stop()
    set_profile("Test")
    sc = FormScreen()
    g = new_guide(sc, FakeModel([field("Type your last name.", 2, profile="last_name",
                                       ask="What's your last name?")]))
    g.start("check in", "english", wait=True)
    r = g.do_it(speak=False)
    check("a one-word profile name is never used as a last name: it asks",
          r["said"] == "What's your last name?" and sc.fills == [], r)
    g.stop()


def t_do_reads_back_a_value_she_said():
    sc = FormScreen()
    m = FakeModel([field(CONF_LINE, 1, name="your confirmation code", value="K7QM2P",
                         ask="What's your confirmation code?"),
                   field("Type your last name.", 2)])
    g = new_guide(sc, m)
    g.start("guide me to check in, my confirmation code is K 7 Q M 2 P", "english",
            speak=True, wait=True)
    r = g.do_it()
    check("a value she said is read back before it is typed, a code letter by letter",
          not r["ok"] and r["said"] == "I'll type K, 7, Q, M, 2, P. Is that right?" and sc.fills == [], r)
    check("the card shows it as it will be typed, and waits for her yes",
          r["asking"] == "I'll type K7QM2P. Is that right?" and r["awaiting"] == "confirm", r)
    check("the guide says the read-back itself when the card asked",
          until(lambda: ("I'll type K, 7, Q, M, 2, P. Is that right?", "english") in g.said), g.said)
    r = g.do_it(confirm=True)
    check("her yes types it", r["ok"] and sc.fills == [(1, "K7QM2P")], (r, sc.fills))
    ok = until(lambda: g.state()["n"] == 2)
    check("and the guide moves on", ok, g.state())
    g.stop()


def t_never_invents_a_value():
    sc = FormScreen()
    m = FakeModel([field(CONF_LINE, 1, name="your confirmation code", value="ABC123",
                         ask="What's your confirmation code?")])
    g = new_guide(sc, m)
    g.start("guide me to check in", "english", wait=True)
    check("a value she never said is dropped", g._s["field"]["value"] == "", g._s["field"])
    r = g.do_it(speak=False)
    check("so Do it for me asks for it instead of typing a made-up one",
          r["said"] == "What's your confirmation code?" and r["awaiting"] == "value" and sc.fills == [], r)
    g.stop()


def t_do_asks_then_types_what_she_says():
    sc = FormScreen()
    m = FakeModel([field(CONF_LINE, 1, name="your confirmation code", ask="What's your confirmation code?"),
                   field("Type your last name.", 2)])
    g = new_guide(sc, m)
    g.start("check in", "english", wait=True)
    r = g.do_it(speak=False)
    check("with no value known, MicMic asks for it", r["said"] == "What's your confirmation code?"
          and g.awaiting() == "value", r)
    r = g.give("it's k seven q m two p", speak=False)
    check("a spelled code is joined and read back", r["said"] == "I'll type K, 7, Q, M, 2, P. Is that right?"
          and g.awaiting() == "confirm", r)
    r = g.answer(False, heard="no", speak=False)
    check("no asks again, typing nothing", r["said"] == "Okay. What should it be?" and sc.fills == []
          and g.awaiting() == "value", r)
    r = g.give("K7QM2B", speak=False)
    r = g.answer(False, heard="no, it's K7QM2P", speak=False)
    check("a no with the right value in it reads that one back",
          r["said"] == "I'll type K, 7, Q, M, 2, P. Is that right?", r)
    r = g.answer(True, speak=False)
    check("yes types exactly that", r["ok"] and sc.fills == [(1, "K7QM2P")], (r, sc.fills))
    g.stop()


def t_do_refuses_secret_fields():
    sc = FormScreen()
    sc.page = "pay"
    for tid, label in ((1, "card number"), (2, "security code"), (3, "password"), (4, "PIN"),
                       (5, "one-time code")):
        m = FakeModel([field(f"Type your {label}.", tid, kind="type")])
        g = new_guide(sc, m)
        st = g.start("pay", "english", wait=True)
        r = g.do_it(speak=False)
        check(f"a {label} field is hers alone: never offered, never typed, with the reason said",
              st["kind"] == "secret" and st["can_do"] is False and not r["ok"]
              and r["said"].startswith("I never type passwords, card numbers or security codes")
              and sc.fills == [], (st["kind"], st["can_do"], r.get("said")))
        check(f"and what she says to a {label} field is not taken as a value",
              g.give("1234", speak=False) is None, label)
        g.stop()
    sc = FormScreen()
    sc.page = "docs"
    g = new_guide(sc, FakeModel([field("Enter your passport number.", 2)]))
    g.start("check in", "english", wait=True)
    r = g.do_it(speak=False)
    check("a passport number stays hers, said as private (not as a password)",
          r["said"].startswith("Passport and ID numbers stay private") and sc.fills == [], r)
    g.stop()
    # Whatever the screen said, the screen's own fill refuses a secret field too.
    sc = FormScreen()
    sc.page = "pay"
    snap = sc.snapshot(sc.front())
    check("the screen's fill itself refuses a secret field", sc.fill(snap, 1, "4242")[0] is False)


def t_confirmation_gate_intact():
    sc = FormScreen()
    sc.page = "review"
    for tid, label in ((1, "Check in now"), (2, "Continue to payment")):
        g = new_guide(sc, FakeModel([click(f"Click {label} at the bottom.", tid)]))
        st = g.start("check in", "english", wait=True)
        r = g.do_it(speak=False)
        check(f"{label!r} is never pressed for her, and she hears why",
              st["can_do"] is False and not r["ok"] and r["said"].startswith("This one finishes it")
              and sc.presses == [], (st["can_do"], r.get("said"), sc.presses))
        g.stop()
    sc = FormScreen()
    sc.page = "docs"
    g = new_guide(sc, FakeModel([click("Tick the box to confirm your details.", 7)]))
    st = g.start("check in", "english", wait=True)
    check("'I confirm these details are correct' is her word: never ticked for her",
          st["can_do"] is False and not g.do_it(speak=False)["ok"] and sc.presses == [], st)
    g.stop()
    sc = FormScreen()
    sc.page = "pay"
    g = new_guide(sc, FakeModel([click("Click Pay now.", 6)]))
    st = g.start("pay", "english", wait=True)
    check("nor is Pay now", st["can_do"] is False and sc.presses == [], st)
    g.stop()
    # Typing never presses anything and never sends a Return.
    sc = FormScreen()
    g = new_guide(sc, FakeModel([field(CONF_LINE, 1), click("x", 3)]))
    g.start("check in, my code is K7QM2P", "english", wait=True)
    g.give("K7QM2P\n", speak=False)
    g.answer(True, speak=False)
    check("filling a field presses no button and types no Return",
          sc.presses == [] and sc.fills == [(1, "K7QM2P")], (sc.presses, sc.fills))
    g.stop()


# ---------------------------------------------------------------- her words
def t_spoken_values():
    cases = {
        "my confirmation code is K7QM2P": "K7QM2P", "K 7 Q M 2 P": "K7QM2P",
        "it's k seven q m two p": "K7QM2P", "kilo seven quebec mike two papa": "K7QM2P",
        "my last name is Rivera please": "Rivera", "Rua das Flores 28": "Rua das Flores 28",
        "1200 195": "1200 195", "P zero zero four two eight one one seven": "P00428117",
        "הקוד הוא K7QM2P": "K7QM2P", "мой код это K 7 Q M 2 P": "K7QM2P",
        "الرقم هو K7QM2P": "K7QM2P", "Lisbon": "Lisbon",
    }
    bad = {k: (G.spoken_value(k), v) for k, v in cases.items() if G.spoken_value(k) != v}
    check("what she says is the value, without the words around it", not bad, bad)
    check("a code is read back letter by letter, words are not",
          G._spell("K7QM2P") == "K, 7, Q, M, 2, P" and G._spell("Rivera") == "Rivera")
    check("a value counts as said only when it is in her words",
          G._said_it("K7QM2P", ["my code is K 7 Q M 2 P"]) and not G._said_it("K7QM2B", ["my code is K7QM2P"]))


def t_four_languages():
    for lang, word in (("hebrew", "אקליד"), ("arabic", "رح أكتب"), ("russian", "Введу")):
        sc = FormScreen()
        g = new_guide(sc, FakeModel([field("x", 1, value="K7QM2P")]))
        g.start("K7QM2P", lang, wait=True)
        r = g.do_it(speak=False)
        check(f"the read-back is in {lang}", word in r["said"] and "K, 7, Q" in r["said"], r["said"])
        g.stop()
    dash = re.compile("[—–]")
    keys = ("ask_value", "confirm_value", "typed", "type_failed", "say_again", "what_instead",
            "do_secret", "do_private", "do_final", "choose_self", "nudge_type", "nudge_tell",
            "nudge_choose", "noted")
    check("every new line exists in all four languages",
          all(set(G.SAY[k]) == {"hebrew", "arabic", "russian", "english"} for k in keys))
    check("and none has an em or en dash", not [k for k in keys for v in G.SAY[k].values() if dash.search(v)])


# ---------------------------------------------------------------- the router
def t_router_field_turns():
    from savta import router
    set_profile("Test")
    router.due_briefing = lambda: False
    router.get_contacts = lambda *a, **k: ["Dana Levi"]
    mac.screen_locked = lambda: False
    said = []
    mac.say = lambda text, lang="english": said.append(text)
    sc = FormScreen()
    m = FakeModel([field(CONF_LINE, 1, name="your confirmation code", ask="What's your confirmation code?"),
                   field("Type your last name.", 2, name="your last name", ask="What's your last name?"),
                   click("Click Find my trip.", 3)])
    router.GUIDE.screen, router.GUIDE.model = sc, m
    router.GUIDE.speaker = lambda text, lang: said.append(text)
    router.GUIDE.tick, router.GUIDE.settle = 0.01, 0.04
    router.GUIDE.field_idle = 0.25
    j = FakeJev()
    base = {"intent": ("do_online", 0.8), "is_complete": 0.95, "language": ("english", 0.99)}
    j.over = {**base, "guidance": ("walk_me_through", 0.9)}
    r = router.handle(j, "guide me to check in for my flight", speak=False)
    check("the guide starts on the field with what to enter", r["did"] == "guide_step" and r["say"] == CONF_LINE, r)

    j.over = {**base, "guide_command": ("gives_value", 0.9)}
    c0 = j.calls
    r = router.handle(j, "my confirmation code is K 7 Q M 2 P", speak=False)
    check("she says the code: read back for her yes, in the same single Jev request",
          r["did"] == "guide_gives_value" and r["say"] == "I'll type K, 7, Q, M, 2, P. Is that right?"
          and j.calls == c0 + 1 and sc.fills == [], (r["did"], r["say"], j.calls - c0))
    check("Jev is told what MicMic just asked", j.asked[-1][0]["guide_in_progress"].get("micmic_just_asked") is None
          and "guide_command" in j.asked[-1][1], j.asked[-1][0]["guide_in_progress"])
    j.over = {**base, "guide_command": ("next", 0.8)}     # "yes" can read as "got it"
    r = router.handle(j, "yes", speak=False)
    check("Jev saw the question it is a yes to",
          "Is that right?" in j.asked[-1][0]["guide_in_progress"].get("micmic_just_asked", ""),
          j.asked[-1][0]["guide_in_progress"])
    check("her yes types it", r["did"] == "guide_yes" and sc.fills == [(1, "K7QM2P")] and r["say"] == "Typed it in.",
          (r["did"], r["say"], sc.fills))
    ok = until(lambda: router.GUIDE.state()["n"] == 2)
    check("and the guide moves to the next field", ok, router.GUIDE.state())

    j.over = {**base, "guide_command": ("do_it", 0.9)}
    r = router.handle(j, "can you type it for me", speak=False)
    check("'type it for me' with no value known asks for it",
          r["did"] == "guide_do_it" and r["say"] == "What's your last name?", (r["did"], r["say"]))
    j.over = {**base, "intent": ("help", 0.95)}
    r = router.handle(j, "what can you do", speak=False)
    check("a request Jev is sure is its own is answered as one, even while MicMic waits",
          r["did"] == "helped" and router.GUIDE.awaiting() == "value", (r["did"], router.GUIDE.awaiting()))
    j.over = {**base}                  # a bare name: Jev reads no command at all
    r = router.handle(j, "Rivera", speak=False)
    check("the answer to MicMic's question is taken as the value even with no command read",
          r["did"] == "guide_gives_value" and r["say"] == "I'll type Rivera. Is that right?", (r["did"], r["say"]))
    j.over = {**base, "guide_command": ("yes", 0.9)}
    r = router.handle(j, "yes", speak=False)
    check("and typed on her yes", sc.fills[-1] == (2, "Rivera"), sc.fills)
    until(lambda: router.GUIDE.state()["n"] == 3)

    # A detail said before its field: kept for the step call.
    j.over = {**base, "guide_command": ("gives_value", 0.9)}
    r = router.handle(j, "my passport number is P00428117", speak=False)
    check("a passport number said aloud is never passed to the step call",
          not any("P00428117" in x for x in router.GUIDE._s["said"]), router.GUIDE._s["said"])
    r = router.handle(j, "the city is Lisbon", speak=False)
    check("a detail said while the step is a button is kept for its field, and acknowledged",
          r["did"] == "guide_noted" and "the city is Lisbon" in router.GUIDE._s["said"], (r["did"], r["say"]))
    j.over = {**base, "guide_command": ("stop", 0.9)}
    r = router.handle(j, "stop", speak=False)
    check("stop still stops", r["did"] == "guide_stopped" and not router.GUIDE.active(), r["did"])


def t_router_help_me_on_a_page():
    from savta import router
    set_profile("Test")
    launched = []
    saved = router._launch_web
    router._launch_web = lambda *a, **k: launched.append(a[1]) or {"web_run": "x"}
    try:
        sc = FormScreen()
        m = FakeModel([field(CONF_LINE, 1, name="your confirmation code")])
        router.GUIDE.screen, router.GUIDE.model = sc, m
        j = FakeJev()
        j.over = {"intent": ("do_online", 0.9), "is_complete": 0.95, "language": ("english", 0.99)}
        r = router.handle(j, "help me check in for my flight to Lisbon", speak=False)
        check("'help me check in' with the check-in page open is a guide, in her own window",
              r["did"] == "guide_step" and r["say"] == CONF_LINE and not launched, (r["did"], launched))
        check("still one Jev request", j.calls == 1, j.calls)
        router.GUIDE.stop()

        # The installed 1.2.5, 2026-10-09 16:57:19 (trace): "Help me check in for my
        # flight to Lisbon." with Chrome in front on the Larkline home page went to the
        # browser agent (jev_n 3: understand, the task span, the start site), because
        # the old check needed a form read off the page and read none. Here the page
        # cannot be read at all (as without Accessibility): it is still a guide.
        class TracePage(FormScreen):
            def page_url(self, front):
                return "http://127.0.0.1:8851/"

            def snapshot(self, front):
                return {"error": "no_access", "controls": []}
        router.GUIDE.screen, router.GUIDE.model = TracePage(), FakeModel([field(CONF_LINE, 1)])
        j = FakeJev()
        j.over = {"intent": ("do_online", 1.0), "is_complete": 0.95, "language": ("english", 0.99)}
        r = router.handle(j, "Help me check in for my flight to Lisbon.", speak=False)
        check("the exact trace case: a web page in front, nothing read off it, is a guide, never "
              "the browser agent", str(r["did"]).startswith("guide") and not launched and j.calls == 1,
              (r["did"], launched, j.calls))
        check("and with nothing readable the guide says how to let MicMic see the screen",
              "Accessibility" in r["say"], r["say"])
        router.GUIDE.stop()

        router.GUIDE.screen, router.GUIDE.model = sc, FakeModel([field(CONF_LINE, 1)])
        sc.page = "buttons"            # a page with no form: still her page
        r = router.handle(j, "help me check in for my flight to Lisbon", speak=False)
        check("any web page in front is a guide, form or not", r["did"] == "guide_step" and not launched,
              (r["did"], launched))
        router.GUIDE.stop()

        class NewTab(FormScreen):
            def page_url(self, front):
                return "chrome://newtab/"
        router.GUIDE.screen = NewTab()
        r = router.handle(j, "help me check in for my flight to Lisbon", speak=False)
        check("a new tab is not a page: the web task goes on as before, and the trace says why",
              r["did"] == "web_working" and launched
              and (r.get("detail") or {}).get("front_page", {}).get("why") == "blank page",
              (r["did"], r.get("detail")))
        router.GUIDE.screen = sc
        sc.page = "checkin"
        n = len(launched)
        r = router.handle(j, "check me in for my flight to Lisbon", speak=False)
        check("without 'help me', a web task is the browser's as before",
              r["did"] == "web_working" and len(launched) == n + 1, r["did"])
        sc.browser = False
        r = router.handle(j, "help me check in for my flight", speak=False)
        check("and with no browser in front (Notes) it is the browser's too, with the reason kept",
              r["did"] == "web_working"
              and (r.get("detail") or {}).get("front_page", {}).get("why") == "not a browser",
              (r["did"], r.get("detail")))
    finally:
        router._launch_web = saved
        router.GUIDE.stop()
    check("a test process never reads the real screen for it",
          G.Guide().page_in_front()["why"] == "test")
    check("browsers are known by name or bundle id",
          G._is_browser("Google Chrome") and G._is_browser("Google Chrome for Testing")
          and G._is_browser("", "com.google.Chrome") and G._is_browser("Safari")
          and not G._is_browser("Notes", "com.apple.Notes"))


def t_guide_is_one_request():
    """A guide's calls carry X-MicMic-Session: guide:<id>, so the proxy meters the guide
    once (proxy/tests/test_guide_session.py); calls outside a guide carry nothing."""
    import http.server
    import json as _json
    import threading
    from savta import router, jev as J
    from savta.llm import LLM
    sc = FormScreen()
    m = FakeModel([field(CONF_LINE, 1)])
    g = new_guide(sc, m)
    g.start("check in", "english", wait=True)
    sid = g.state()["id"]
    check("the step call names its guide session",
          m.requests[0].get("session") == f"guide:{sid}"
          and re.fullmatch(r"guide:[0-9a-f]{8,32}", m.requests[0]["session"]), m.requests[0].get("session"))
    check("and the guide says it while it runs, not after",
          g.session_tag() == f"guide:{sid}" and (g.stop() or True) and g.session_tag() == "")

    # The headers really go out, through the proxy, on Gemini and on Jev.
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append((self.path, self.headers.get("X-MicMic-Session")))
            out = (b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}]}' if "gemini" in self.path
                   else _json.dumps({"answers": {}, "usage": {}}).encode())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        llm = LLM()
        llm._proxy = (False, "127.0.0.1", port, "", "offline-test")
        llm.session = "guide:0a1b2c3d4e"
        llm._post_once({"contents": []})
        llm.session = ""
        llm._post_once({"contents": []})
        j = J.Jev()
        j._https, j._host, j._port, j._path = False, "127.0.0.1", port, "/v1/jev"
        j.session = "guide:0a1b2c3d4e"
        j.ask({"utterance": "yes"}, {"x": {"type": "noul"}})
        j.session = ""
        j.ask({"utterance": "what time is it"}, {"x": {"type": "noul"}})
    finally:
        srv.shutdown()
    check("Gemini through the proxy carries the guide's session, and only then",
          [h for p, h in seen if "gemini" in p] == ["guide:0a1b2c3d4e", None], seen)
    check("Jev through the proxy too", [h for p, h in seen if "jev" in p] == ["guide:0a1b2c3d4e", None], seen)

    # The router tags her turns while a guide runs, and only for that turn.
    set_profile("Test")
    router.GUIDE.screen = FormScreen()
    router.GUIDE.model = FakeModel([field(CONF_LINE, 1, ask="What's your confirmation code?")])
    fj = FakeJev()
    fj.session = ""
    sessions = []
    real_ask = fj.ask
    fj.ask = lambda state, qs: (sessions.append(fj.session), real_ask(state, qs))[1]
    base = {"intent": ("do_online", 0.9), "is_complete": 0.95, "language": ("english", 0.99)}
    fj.over = {**base, "guidance": ("walk_me_through", 0.9)}
    router.handle(fj, "guide me to check in", speak=False)
    tag = router.GUIDE.session_tag()
    fj.over = {**base, "guide_command": ("do_it", 0.9)}
    router.handle(fj, "type it for me", speak=False)
    router.GUIDE.stop()
    fj.over = dict(base)
    router.handle(fj, "help me check in for my flight", speak=False)
    check("the turn that starts a guide is metered as usual, her turns inside it carry the "
          "guide, and nothing after it does",
          sessions[0] == "" and sessions[1] == tag and tag and sessions[-1] == "" and fj.session == "",
          (sessions, tag))
    router.GUIDE.stop()


def t_page_card_buttons():
    html = (TG.ROOT / "savta" / "web" / "index.html").read_text()
    check("the card has Yes and No for a read-back, sent as do {confirm}",
          'id="gNoB"' in html and "{confirm:false}" in html and "{confirm:true}" in html)
    check("and shows what MicMic asked in place of the step", "asking ? st.asking" in html)
    for lang in ("he_f", "en", "ar", "ru"):
        block = html.split(f"  {lang}:{{", 1)[1].split("\n  }", 1)[0]
        check(f"Yes and No exist in {lang}", "gYes:" in block and "gNo:" in block)


def main():
    tests = [t_request_carries_field_details, t_field_step_says_what_to_enter,
             t_click_into_field_waits_quietly_then_one_nudge, t_advance_on_value_change,
             t_no_repeated_step, t_planned_fields_without_model_calls, t_choose_step,
             t_other_option_in_the_set, t_field_group_named,
             t_do_types_profile_value, t_do_reads_back_a_value_she_said, t_never_invents_a_value,
             t_do_asks_then_types_what_she_says, t_do_refuses_secret_fields,
             t_confirmation_gate_intact, t_spoken_values, t_four_languages,
             t_router_field_turns, t_router_help_me_on_a_page, t_guide_is_one_request,
             t_page_card_buttons]
    for t in tests:
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran to the end", False, repr(e)[:300])
    print(f"\n{len(TG.PASSED)} passed, {len(TG.FAILED)} failed")
    for f in TG.FAILED:
        print("  FAILED:", f)
    sys.stdout.flush()
    os._exit(1 if TG.FAILED else 0)


if __name__ == "__main__":
    main()
