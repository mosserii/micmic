#!/usr/bin/env python3
"""Guide mode end to end on the Larkline check-in, with no screen: the real Guide
(savta/actions/guide.py) reads a HEADLESS Chromium through tests/larkline_screen.py,
and a scripted person does each step the way the guide says it.

    cd <worktree> && uv run python tests/guide_larkline_e2e.py               # $0, scripted model
    uv run python tests/guide_larkline_e2e.py --gemini 10                    # live Gemini, capped
    uv run python tests/guide_larkline_e2e.py --router --jev 30 --gemini 4   # her words through
                                                                             # the router, live Jev

Scripted (the default) costs nothing: LarklineModel stands in for Gemini, reading only
what Gemini is sent (the request text: the controls, their values and hints, the page),
and writes the steps the way the step prompt asks for them. --gemini N uses the real
step call (gemini_step) for at most N calls, then stops. --router sends what she says
("help me check in for my flight to Lisbon", "my confirmation code is K 7 Q M 2 P",
"yes", "type my last name for me", "Rivera") through router.handle with a live Jev,
capped at --jev calls; anything that would start the browser agent fails the run.

The person: she types into a field (and tabs out, or just stops), picks from a list,
ticks a box, picks a different option from the one ringed, scrolls when told to look,
asks MicMic to type her confirmation code and her last name, and presses "Check in
now" herself (MicMic must refuse it). Checked: every step is new (never the same step
twice in a row), no step waits more than STALL_S, the boarding pass is reached (in the
scripted run), the finishing button is never pressed by MicMic, and secret fields are
never typed by MicMic. Writes every step to --out (JSONL) and prints a table.

Never the owner's screen, never 127.0.0.1:8799, never a visible browser.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-larkline-e2e-")
os.environ.pop("MICMIC_ALLOW_SEND", None)

import larkline_screen as L  # noqa: E402
from savta.actions import guide as G  # noqa: E402

STALL_S = 12.0
PERSON_PAUSE = 0.4

# ---------------------------------------------------------------- the scripted model
# One plan per page: what to fill, pick, tick or press, in order, and how the step says
# it. (role, label, nth occurrence of that label, kind, say, field, ask, profile)
PLAN = {
    "#/check-in": [
        ("field", "Confirmation number", 0, "type",
         "Type your 6-character confirmation code. It's in your booking email.",
         "your confirmation code", "What's your confirmation code?", ""),
        ("field", "Last name", 0, "type", "Type your last name, as it is on your booking.",
         "your last name", "What's your last name?", "last_name"),
        ("button", "Find my trip", 0, "click", "Click Find my trip, under the two boxes.", "", "", "")],
    "#/check-in/trip": [
        ("checkbox", "Check in Alex Rivera", 0, "click", "Tick the box next to Alex Rivera to check in.", "", "", ""),
        ("button", "Check in selected traveler", 0, "click", "Click Check in selected traveler at the bottom.", "", "", "")],
    "#/check-in/travelers": [
        ("button", "Continue", 0, "click", "Your details are already filled in. Click Continue at the bottom.", "", "", "")],
    "#/check-in/documents": [
        ("menu", "Nationality", 0, "choose", "Pick your nationality from the list.", "your nationality",
         "What's your nationality?", ""),
        ("menu", "Issuing country", 0, "choose", "Pick the country that issued your passport.",
         "the issuing country", "Which country issued your passport?", ""),
        ("field", "Passport number", 0, "secret",
         "Enter your passport number exactly as printed on the photo page.", "your passport number", "", ""),
        ("field", "Month", 0, "type", "Now the expiration date. Type the month, as two digits.",
         "the expiry month", "In which month does your passport expire?", ""),
        ("field", "Day", 0, "type", "Then the day it expires.", "the expiry day", "On which day?", ""),
        ("field", "Year", 0, "type", "And the year, all four digits.", "the expiry year", "Which year?", ""),
        ("field", "Month", 1, "type", "Now your date of birth. Type the month you were born.",
         "your birth month", "In which month were you born?", ""),
        ("field", "Day", 1, "type", "Then the day.", "your birth day", "On which day?", ""),
        ("field", "Year", 1, "type", "And the year you were born.", "your birth year", "Which year?", ""),
        ("option", "Female", 0, "click", "Choose the sex shown on your passport.", "", "", ""),
        ("option", "Yes I have a visa or permit", 0, "click",
         "Say whether you have a visa for Portugal. Not required is right for most visitors.", "", "", ""),
        ("field", "Street address or hotel name", 0, "type",
         "Type where you'll stay in Portugal. A hotel name is fine.", "where you'll stay",
         "Where will you stay in Lisbon?", ""),
        ("field", "City", 0, "type", "Type the city.", "the city", "Which city?", ""),
        ("field", "Postal code", 0, "type", "Type the postal code, four digits, a space, then three.",
         "the postal code", "What's the postal code?", ""),
        ("checkbox", "I confirm these details are correct", 0, "click",
         "Tick the box to confirm your details are right.", "", "", ""),
        ("button", "Save and continue", 0, "click", "Click Save and continue at the bottom.", "", "", "")],
    "#/check-in/safety": [
        ("checkbox", "I understand.", 0, "click",
         "Read the list, then tick the box to say your bags have none of these.", "", "", ""),
        ("button", "I understand, continue", 0, "click", "Click I understand, continue.", "", "", "")],
    "#/check-in/seats": [
        ("button", "Continue to bags", 0, "click", "Your seat is 31B. Click Continue to bags to keep it.", "", "", "")],
    "#/check-in/bags": [
        ("button", "Continue", 0, "click", "Carry-on is included. Click Continue to skip checked bags.", "", "", "")],
    "#/check-in/offers": [
        ("button", "No thanks", 0, "click", "Click No thanks to skip the upgrade.", "", "", ""),
        ("option", "No thanks", 0, "click", "Choose No thanks for trip protection.", "", "", ""),
        ("button", "Continue to review", 0, "click", "Click Continue to review.", "", "", "")],
    "#/check-in/review": [
        ("button", "Check in now", 0, "click", "Everything is ready. Click Check in now when you're happy.", "", "", "")],
}
_LINE = re.compile(r'^(\d+) (.+?) "(.*?)"(.*) \((\d+)%,(\d+)%\)$')


def parse_controls(prompt: str) -> list[dict]:
    """The controls exactly as the step request lists them to the model."""
    out = []
    for line in prompt.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        rest = m.group(4)
        v = re.search(r'now says "(.*?)"', rest)
        out.append({"id": int(m.group(1)), "role": m.group(2), "label": m.group(3),
                    "value": v.group(1) if v else "", "empty": "(empty)" in rest,
                    "secret": "[secret field]" in rest, "ticked": "[ticked]" in rest,
                    "filled": "[secret field] (filled)" in rest,
                    "y": int(m.group(6))})
    return out


class LarklineModel:
    """A stand-in for Gemini on Larkline: it sees what Gemini sees and nothing else."""

    def __init__(self):
        self.requests: list = []
        self.done_items: set = set()

    def __call__(self, req: dict) -> dict:
        self.requests.append(req)
        prompt = req["prompt"]
        url = re.search(r"Web page: .*\((.*?)\)$", prompt, re.M)
        route = "#/" + url.group(1).split("#/", 1)[1] if url and "#/" in url.group(1) else "#/"
        route = route.split("?")[0].rstrip("/")
        if "boarding-pass" in route:
            return {"done": True, "finish": "That's done: you're checked in, and your boarding pass is on screen.",
                    "say": "", "target": None, "kind": "click", "goal": "Check in for the Lisbon flight"}
        plan = PLAN["#/check-in"] if route in ("#/", "#/check-in") else PLAN.get(route)
        if not plan:
            return {"done": False, "say": "Wait a moment for the page to load.", "target": None,
                    "kind": "wait", "goal": "Check in for the Lisbon flight"}
        controls = parse_controls(prompt)
        todo = []
        for i, item in enumerate(plan):
            role, label, nth, kind = item[:4]
            ctl = self._find(controls, role, label, nth)
            if ctl is not None and self._filled(ctl, controls, role, item):
                continue
            if (route, i) in self.done_items and role in ("button",) and item[1] == "No thanks":
                continue
            todo.append((i, item, ctl))
        if not todo:
            return {"done": False, "say": "Scroll down a little.", "target": None, "kind": "look",
                    "goal": "Check in for the Lisbon flight"}
        # Gemini sees only what is in the window: what is above it was done (the steps so
        # far say so), what is below it is found by scrolling down.
        visible = [t for t in todo if t[2] is not None]
        if visible:
            todo = todo[todo.index(visible[0]):]
        else:
            seen = [k for k, it in enumerate(plan) if self._find(controls, it[0], it[1], it[2])]
            after = [t for t in todo if seen and t[0] > max(seen)]
            todo = after or todo
        i, item, ctl = todo[0]
        role, label, nth, kind, say, field, ask, profile = item
        if ctl is None:
            return {"done": False, "say": f"Scroll down to find {label.rstrip('.')}.", "target": None,
                    "kind": "look", "why": "It is further down the page.", "goal": "Check in for the Lisbon flight"}
        if role == "button" and label == "No thanks":
            self.done_items.add((route, i))
        then = []
        if kind in G.FIELD_KINDS:
            for j, nxt, nctl in todo[1:6]:
                if nctl is None or nxt[0] in ("button", "link"):
                    break
                then.append({"say": nxt[4], "target": nctl["id"], "kind": nxt[3], "field": nxt[5],
                             "ask": nxt[6], "value": self._said(req, nxt), "profile": nxt[7]})
        return {"done": False, "say": say, "target": ctl["id"], "kind": kind,
                "why": "The airline needs it to check you in.", "goal": "Check in for the Lisbon flight",
                "finish": "", "field": field, "ask": ask, "value": self._said(req, item),
                "profile": profile, "then": then}

    @staticmethod
    def _said(req, item) -> str:
        """Only what she said, copied from her words, as the prompt asks."""
        said = " ".join(req.get("said") or [])
        if item[1] == "Confirmation number":
            m = re.search(r"\b([A-Z0-9](?:\s?[A-Z0-9]){5})\b", said)
            return m.group(1).replace(" ", "") if m else ""
        return ""

    @staticmethod
    def _find(controls, role, label, nth):
        same = [c for c in controls if c["role"] == role
                and (c["label"] == label or (label.endswith(".") and c["label"].startswith(label))
                     or (role in ("option", "checkbox") and c["label"].startswith(label)))]
        return same[nth] if len(same) > nth else None

    @staticmethod
    def _filled(ctl, controls, role, item) -> bool:
        if role == "field":
            return bool(ctl["value"]) or ctl["filled"]
        if role == "menu":
            return bool(ctl["value"]) and ctl["value"] != "Select a country"
        if role == "checkbox":
            return ctl["ticked"]
        if role == "option":
            # a set of options is answered when any option beside it is ticked
            return any(c["ticked"] for c in controls if c["role"] == "option" and abs(c["y"] - ctl["y"]) <= 6)
        return False


# ---------------------------------------------------------------- the person
VALUES = [(r"confirmation", "K7QM2P"), (r"last name", "Rivera"), (r"passport number", "P00428117"),
          (r"street|hotel", "Rua das Flores 28"), (r"^city$", "Lisbon"), (r"postal", "1200 195")]
DATES = {"exp": ("03", "14", "2031"), "dob": ("07", "22", "1990")}


class Person:
    """Does each step the guide gives, the way she would, and notes what happened."""

    def __init__(self, guide, screen, log, router_say=None):
        self.g, self.sc, self.log = guide, screen, log
        self.router_say = router_say          # her words through the router, when set
        self.dates_seen = {"Month": 0, "Day": 0, "Year": 0}
        self.pressed_final = False

    def act(self, st: dict) -> None:
        snap = self.g._s.get("snap") or {}
        tid = self.g._s.get("target")
        ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
        kind, label = st.get("kind"), (ctl or {}).get("label", "")
        time.sleep(PERSON_PAUSE)
        if kind in ("look", "wait") or ctl is None:
            self.sc.user_scroll(450)
            self.note("she scrolls and says done")
            self.g.next_step()
            return
        if kind in ("type", "secret"):
            return self.fill(st, snap, tid, label)
        if kind == "choose":
            self.sc.user_select(snap, tid, "United States")
            return self.note("she picks United States")
        # a click: a box, an option, or a button
        if ctl["role"] == "option" and label.startswith("Female"):
            other = next((c for c in snap["controls"] if c["role"] == "option" and c["label"].startswith("Male")), None)
            self.sc.user_check(snap, other["id"])
            return self.note("she picks Male (not the ringed option)")
        if ctl["role"] == "option" and label.startswith("Yes I have"):
            other = next((c for c in snap["controls"] if c["role"] == "option" and c["label"].startswith("Not required")), None)
            self.sc.user_check(snap, other["id"])
            return self.note("she picks Not required")
        if ctl["role"] in ("checkbox", "option"):
            self.sc.user_check(snap, tid)
            return self.note(f"she ticks {label[:40]!r}" + ("" if not st.get("can_do") else " (Do it for me was offered)"))
        if st.get("can_do"):
            r = self.g.do_it(speak=False)
            return self.note(f"Do it for me pressed {label!r}: ok={r.get('ok')}")
        r = self.g.do_it(speak=False)
        self.note(f"Do it for me refused {label!r}: {r.get('said')!r}")
        if re.search(r"check in now|pay", label, re.I):
            self.pressed_final = True
        self.sc.press(snap, tid)
        self.note(f"she presses {label!r} herself")

    def fill(self, st, snap, tid, label):
        low = label.lower()
        if low in ("month", "day", "year"):
            which = "exp" if self.dates_seen[label] == 0 else "dob"
            self.dates_seen[label] += 1
            value = DATES[which][("month", "day", "year").index(low)]
        else:
            value = next((v for rx, v in VALUES if re.search(rx, low)), "x")
        if "confirmation" in low:
            # She asks MicMic to type it; it was said, so it is read back first.
            if self.router_say:
                self.router_say("my confirmation code is K 7 Q M 2 P")
                self.router_say("yes")
            else:
                r = self.g.do_it(speak=False)
                self.note(f"Do it for me: {r.get('said')!r}")
                if self.g.awaiting() == "value":
                    r = self.g.give("it's K 7 Q M 2 P", speak=False)
                    self.note(f"she says it: {r.get('said')!r}")
                r = self.g.answer(True, speak=False)
                self.note(f"she says yes: {r.get('said')!r}")
            return
        if "last name" in low:
            if self.router_say:
                self.router_say("type my last name for me")
                if self.g.awaiting() == "value":
                    self.router_say("Rivera")
                if self.g.awaiting() == "confirm":
                    self.router_say("yes")
            else:
                r = self.g.do_it(speak=False)
                self.note(f"Do it for me (profile): {r.get('said')!r}")
            return
        if st.get("kind") == "secret":
            r = self.g.do_it(speak=False)
            self.note(f"Do it for me refused: {r.get('said')!r}")
        self.sc.user_type(snap, tid, value, delay_ms=25)
        if low in ("city",):
            self.note(f"she types {value!r} and waits (no Tab)")
        else:
            self.sc.user_tab()
            self.note(f"she types {value!r} and tabs out")

    def note(self, text):
        self.log({"ev": "person", "did": text})


# ---------------------------------------------------------------- the run
def prewalk(sc) -> None:
    """The steps before the documents page, done by hand with no guide and no model."""
    def press(label, role=None, how="press", value=None):
        for _ in range(8):
            s = sc.snapshot(sc.front())
            c = next((c for c in s["controls"] if c["label"].startswith(label)
                      and (role is None or c["role"] == role)), None)
            if c:
                {"press": lambda: sc.press(s, c["id"]), "check": lambda: sc.user_check(s, c["id"]),
                 "type": lambda: sc.user_type(s, c["id"], value)}[how]()
                time.sleep(0.6)
                return
            sc.user_scroll(450)
        raise SystemExit(f"prewalk: {label!r} not found")
    sc.goto("/#/check-in")
    press("Confirmation number", "field", "type", "K7QM2P")
    press("Last name", "field", "type", "Rivera")
    press("Find my trip", "button")
    press("Check in Alex Rivera", "checkbox", "check")
    press("Check in selected traveler", "button")
    press("Continue", "button")
    sc.user_scroll(-5000)
    if "documents" not in sc.probe({}, None)["url"]:
        raise SystemExit("prewalk did not reach the documents step")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gemini", type=int, default=0, help="live Gemini step calls allowed (0 = scripted)")
    ap.add_argument("--router", action="store_true", help="her words go through router.handle")
    ap.add_argument("--jev", type=int, default=0, help="live Jev calls allowed with --router")
    ap.add_argument("--start", default="/#/check-in", help="where the page starts")
    ap.add_argument("--goal", default="help me check in for my flight to Lisbon")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "larkline-e2e.jsonl"))
    ap.add_argument("--max-seconds", type=float, default=420.0)
    ap.add_argument("--no-plan", action="store_true",
                    help="no planned fields: one model call per step, as before 2026-10-08")
    ap.add_argument("--prewalk", action="store_true",
                    help="do the check-in form, trip and travelers steps first (no model), "
                         "then guide from the documents step")
    a = ap.parse_args()
    if a.no_plan:
        G.MAX_QUEUE = 0

    from savta import profile as prof
    prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done",
               "name": "Alex" if a.router else "Alex Rivera", "language": "english", "city": "New York"})

    base, stop_server = L.serve()
    sc = L.LarklineScreen(base)
    sc.goto("/?reset=all")
    sc.goto(a.start)
    s0 = sc.snapshot(sc.front())
    for c in s0["controls"]:
        if c["label"] == "Essential only":
            sc.press(s0, c["id"])         # the cookie banner, once, before she starts
    time.sleep(0.4)
    if a.prewalk:
        prewalk(sc)

    out = open(a.out, "w")
    t0 = time.time()

    def log(rec):
        rec["t"] = round(time.time() - t0, 2)
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()

    calls = {"gemini": 0, "jev": 0}
    from savta import router
    g = router.GUIDE if a.router else G.Guide()
    g.screen = sc
    spoken = []
    g.speaker = lambda text, lang: (spoken.append(text), log({"ev": "spoken", "say": text}))

    if a.gemini:
        llm_holder = {}

        def live_model(req):
            if calls["gemini"] >= a.gemini:
                log({"ev": "budget", "what": "gemini", "used": calls["gemini"]})
                return None
            calls["gemini"] += 1
            from savta.llm import LLM
            llm = llm_holder.setdefault("llm", LLM())
            t = time.time()
            res = G.gemini_step(llm, req)
            log({"ev": "gemini", "n": calls["gemini"], "ms": round((time.time() - t) * 1000),
                 "said_to_model": req.get("said"), "raw": res})
            return res
        g.model = live_model
        model_name = "gemini (live)"
    else:
        scripted = LarklineModel()

        def scripted_model(req):
            calls["gemini"] += 1
            log({"ev": "request", "n": calls["gemini"], "prompt": req["prompt"]})
            return scripted(req)
        g.model = scripted_model
        model_name = "LarklineModel (scripted)"

    router_say = None
    if a.router:
        from savta import jev as J
        from savta.actions import mac
        mac.say = lambda text, lang="english": log({"ev": "mac_say", "say": text})
        mac.screen_locked = lambda: False
        router.due_briefing = lambda: False
        router.get_contacts = lambda *aa, **kk: ["Dana Levi"]

        def no_web(*aa, **kk):
            log({"ev": "FAIL", "why": "the browser agent was started", "task": str(aa[1:2])})
            raise SystemExit("the browser agent was started: routing chose the web task")
        router._launch_web = no_web
        real_ask = J.Jev._ask

        def capped_ask(self, *aa, **kk):
            if calls["jev"] >= a.jev:
                raise SystemExit(f"Jev budget of {a.jev} used up")
            calls["jev"] += 1
            return real_ask(self, *aa, **kk)
        J.Jev._ask = capped_ask
        j = J.Jev()

        def router_say(text):
            t = time.time()
            r = router.handle(j, text, speak=False)
            log({"ev": "her_words", "said": text, "did": r.get("did"), "reply": r.get("say"),
                 "ms": round((time.time() - t) * 1000), "jev_calls": calls["jev"]})
            return r

    person = Person(g, sc, log, router_say)
    g.tick, g.settle = 0.3, 0.9
    if a.router:
        r = router_say(a.goal)
        if not str(r.get("did", "")).startswith("guide"):
            log({"ev": "FAIL", "why": f"routing gave {r.get('did')}, not the guide"})
    else:
        g.start(a.goal, "english", speak=True, wait=True)

    seen_key, last_key, last_t, steps, fails = None, None, time.time(), [], []
    while time.time() - t0 < a.max_seconds:
        st = g.state()
        if st.get("status") in G.ENDED:
            log({"ev": "end", "status": st["status"], "line": st.get("line")})
            break
        key = (st.get("n"), st.get("say"), st.get("rev") if st.get("kind") == "look" else 0)
        if st.get("status") != "step" or st.get("awaiting") or key == seen_key:
            if time.time() - last_t > STALL_S and st.get("status") == "step":
                fails.append(f"stall at step {st.get('n')}: {st.get('say')!r}")
                log({"ev": "stall", "n": st.get("n"), "say": st.get("say")})
                g.next_step()
                last_t = time.time()
            time.sleep(0.1)
            continue
        wait_s = round(time.time() - last_t, 2)
        rec = {"ev": "step", "n": st["n"], "kind": st["kind"], "can_do": st["can_do"],
               "say": st["say"], "target": st.get("target_label"), "field": st.get("field"),
               "wait_s": wait_s, "model_calls": calls["gemini"]}
        log(rec)
        steps.append(rec)
        if last_key and (st["say"], st.get("target_label")) == last_key and st["kind"] != "look":
            fails.append(f"the same step twice in a row: {st['say']!r}")
        last_key = (st["say"], st.get("target_label"))
        seen_key = key
        try:
            person.act(st)
        except SystemExit as e:
            fails.append(str(e))
            break
        except Exception as e:  # noqa: BLE001
            log({"ev": "person_error", "err": repr(e)[:200]})
        last_t = time.time()
    final = g.state()
    url = sc.probe({}, None)["url"]
    if fails:
        end_snap = sc.snapshot(sc.front())
        log({"ev": "end_page", "url": url, "text": end_snap["text"][:1500]})
        print("page at the end:", end_snap["text"][:700])
    g.stop()
    out.close()

    print(f"\nmodel: {model_name}; gemini calls {calls['gemini']}; jev calls {calls['jev']}; "
          f"steps {len(steps)}; {round(time.time() - t0, 1)} s")
    print(f"{'n':>3} {'kind':<7} {'do':<5} {'wait':>5}  say   [target]")
    for r in steps:
        print(f"{r['n']:>3} {r['kind']:<7} {str(r['can_do']):<5} {r['wait_s']:>5}  {r['say']}   "
              f"[{r['target'] or ''}]")
    print(f"end: {final.get('status')} {final.get('line')!r}  url {url}")
    if not a.gemini and "boarding-pass" not in url:
        fails.append(f"the boarding pass was not reached (url {url})")
    if not person.pressed_final and not a.gemini and not a.router:
        fails.append("the finishing button was never reached")
    waits = sorted(r["wait_s"] for r in steps[1:]) or [0]
    print(f"wait between steps: p50 {waits[len(waits) // 2]} s, max {waits[-1]} s")
    for f in fails:
        print("FAIL", f)
    print("PASS" if not fails else f"{len(fails)} failed", f"(log {a.out})")
    sc.close()
    stop_server()
    sys.stdout.flush()
    os._exit(1 if fails else 0)


if __name__ == "__main__":
    main()
