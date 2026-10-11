#!/usr/bin/env python3
"""Guide mode end to end in REAL Chrome, through the real Accessibility path and real
keystrokes: the Larkline check-in from the home page to the boarding pass.

    cd <worktree> && uv run python tests/guide_real_chrome.py --person click
    uv run python tests/guide_real_chrome.py --person do            # she says "do it", "yes"
    uv run python tests/guide_real_chrome.py --person do --gemini 20 --jev 40   # capped live

USES THE SCREEN. Run it only when the owner has said the screen is free. It opens a
Chrome of its own (a scratch profile, --force-renderer-accessibility, never the owner's
Chrome), serves the Larkline demo on a free port, and quits that Chrome at the end.
It never clicks a macOS permission prompt and never touches 127.0.0.1:8799.

Unlike tests/guide_larkline_e2e.py (headless Chromium, Playwright fills the fields),
here the guide reads the window with guide.LiveScreen (AX), MicMic types with
LiveScreen.fill (AXValue, then keystrokes), and the person clicks and types with real
mouse and keyboard events. The rehearsal of 2026-10-11 failed exactly where the
headless run could not look: "yes" to a read-back went to the browser agent, typing
into a real <input> failed, a checkbox at the window's edge had no ring, and an
optional field stalled the travelers page.

Model: LarklineModel (the scripted stand-in, $0) unless --gemini N. Jev: PatternJev
(scripted, $0) unless --jev N. Prints every step; PASS needs the boarding pass, no
step twice in a row, no stall over STALL_S, and no browser agent.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-real-chrome-")
# This run reads the real screen on purpose (guide.Guide.page_in_front refuses to in any
# other test process).
os.environ["MICMIC_GUIDE_REAL_SCREEN"] = "1"
os.environ.pop("MICMIC_ALLOW_SEND", None)

import guide_larkline_e2e as E  # noqa: E402  (LarklineModel, PatternJev, VALUES, DATES)
import larkline_screen as L     # noqa: E402  (serve)
from savta.actions import guide as G  # noqa: E402

STALL_S = 25.0


# ---------------------------------------------------------------- her Chrome
class Chrome:
    """A Chrome of its own: scratch profile, AX tree built up front, quit at the end."""

    def __init__(self, url: str):
        self.dir = Path(tempfile.mkdtemp(prefix="micmic-real-chrome-profile-"))
        subprocess.run(["open", "-na", "Google Chrome", "--args", f"--user-data-dir={self.dir}",
                        "--force-renderer-accessibility", "--no-first-run",
                        "--no-default-browser-check", "--disable-features=ChromeWhatsNewUI",
                        "--window-size=1280,860", "--window-position=80,40", url], check=True)
        self.pid = 0
        end = time.time() + 20
        while time.time() < end and not self.pid:
            p = subprocess.run(["pgrep", "-f", "--", f"--user-data-dir={self.dir}"],
                               capture_output=True, text=True).stdout.split()
            mains = [int(x) for x in p if "--type=" not in _cmd(int(x))]
            self.pid = mains[0] if mains else 0
            time.sleep(0.3)
        if not self.pid:
            raise SystemExit("Chrome did not start")
        time.sleep(3.0)
        self.front()
        # One click on the window's title bar makes it the key window: the first click
        # into a page that is not key yet is taken for that, and a field stays unfocused.
        click(700, 52)
        time.sleep(0.3)

    def front(self):
        G._bring_to_front(self.pid)

    def quit(self):
        subprocess.run(["kill", str(self.pid)], capture_output=True)
        time.sleep(1.5)
        shutil.rmtree(self.dir, ignore_errors=True)


def _cmd(pid: int) -> str:
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                          text=True).stdout


# ---------------------------------------------------------------- her hands
def click(x: float, y: float):
    G._click(x, y)
    time.sleep(0.25)


def press_key(code: int, flags: int = 0):
    import Quartz
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


def type_text(text: str):
    import Quartz
    for ch in text:
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, 1, ch)
            Quartz.CGEventSetFlags(ev, 0)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.04)


def scroll(lines: int = -8, at=(700, 500)):
    import Quartz
    G._click  # noqa: B018 (same module the clicks come from)
    move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, at, 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, lines)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    time.sleep(0.6)


TAB, RETURN = 48, 36


def center(fr):
    return fr[0] + fr[2] / 2, fr[1] + fr[3] / 2


def field_point(fr):
    """Where a person clicks a text box: near its left end. Larkline's check-in card lays
    a group over the right half of the confirmation box (the frame's centre is not the box)."""
    return fr[0] + min(fr[2] / 2, 40), fr[1] + fr[3] / 2


class Person:
    """Does each step with real mouse and keys, or asks MicMic by voice (router)."""

    def __init__(self, guide, chrome, log, say=None, mode="click"):
        self.g, self.chrome, self.log, self.say, self.mode = guide, chrome, log, say, mode
        self.dates_seen = {"Month": 0, "Day": 0, "Year": 0}
        self.pressed_final = False

    def value_for(self, label, spoken=False):
        return E.Person.value_for(self, label, spoken)

    def note(self, text):
        self.log({"ev": "person", "did": text})

    def act(self, st):
        snap = self.g._s.get("snap") or {}
        tid = self.g._s.get("target")
        ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
        kind, label = st.get("kind"), (ctl or {}).get("label", "")
        ring = st.get("ring")
        time.sleep(0.6)
        self.chrome.front()
        if kind in ("look", "wait") or ctl is None or not ring:
            scroll(-8)
            if self.mode == "do" and self.say:
                self.say("next")
            else:
                self.g.next_step()
            return self.note("she scrolls and goes on")
        fr = (ring["x"], ring["y"], ring["w"], ring["h"])
        if self.mode == "do" and self.say and st.get("can_do"):
            if kind == "type":
                self.say("type it for me")
                if self.g.awaiting() == "value":
                    self.say(self.value_for(label, spoken=True))
                if self.g.awaiting() == "confirm":
                    self.say("yes")
                return self.note(f"she asked MicMic to type {label!r}")
            if re.search(r"check in now|confirm seat|pay", label, re.I):
                self.pressed_final = True
            self.say("do it for me")
            return self.note(f"she asked MicMic to press {label!r}")
        if kind in ("type", "secret"):
            value = self.value_for(label)
            click(*field_point(fr))
            press_key(0, 1 << 20)            # Cmd-A, then her words
            type_text(value)
            got = self.g.screen.probe(snap, tid)
            if label.lower() != "city":
                press_key(TAB)
            return self.note(f"she types {value!r} (the field reads {got.get('state')!r}, "
                             f"focused {got.get('focused')})")
        if kind == "choose":
            click(*center(fr))
            time.sleep(0.5)
            type_text("United States")
            press_key(RETURN)                # inside the open list: picks, never submits
            return self.note("she picks United States")
        if ctl["role"] == "option":
            want = {"Female": "Male", "Yes I have": "Not required"}
            other = next((w for k, w in want.items() if label.startswith(k)), None)
            if other:
                o = next((c for c in snap["controls"] if c["role"] == "option"
                          and c["label"].startswith(other)), None)
                if o:
                    click(*center(o["frame"]))
                    return self.note(f"she picks {other!r} (not the ringed option)")
        if re.search(r"check in now|confirm seat|pay", label, re.I):
            self.pressed_final = True
            r = self.g.do_it(speak=False)
            self.note(f"Do it for me on {label!r}: {r.get('said')!r}")
        click(*center(fr))
        self.note(f"she clicks {label!r}")


# ---------------------------------------------------------------- the run
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--person", choices=("click", "do"), default="click")
    ap.add_argument("--gemini", type=int, default=0)
    ap.add_argument("--jev", type=int, default=0)
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "real-chrome.jsonl"))
    ap.add_argument("--max-seconds", type=float, default=600)
    a = ap.parse_args()

    from savta import profile as prof, router, jev as J
    from savta.actions import mac
    prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done", "name": "",
               "language": "english", "city": "New York"})
    mac.say = lambda text, lang="english": None
    mac.screen_locked = lambda: False
    router.due_briefing = lambda: False
    router.get_contacts = lambda *aa, **kk: ["Dana Levi"]
    web_started = []

    def no_web(*aa, **kk):
        web_started.append(aa[1:2])
        raise SystemExit("the browser agent was started")
    router._launch_web = no_web

    out = open(a.out, "w")
    t0 = time.time()

    def log(rec):
        rec["t"] = round(time.time() - t0, 2)
        out.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        out.flush()

    calls = {"gemini": 0, "jev": 0}
    g = router.GUIDE
    g.screen = G.LiveScreen()
    g.speaker = lambda text, lang: log({"ev": "spoken", "say": text})
    if a.gemini:
        from savta.llm import LLM
        llm = LLM()

        def model(req):
            if calls["gemini"] >= a.gemini:
                return None
            calls["gemini"] += 1
            t = time.time()
            res = G.gemini_step(llm, req)
            log({"ev": "gemini", "n": calls["gemini"], "ms": round((time.time() - t) * 1000), "raw": res})
            return res
    else:
        scripted = E.LarklineModel()

        def model(req):
            calls["gemini"] += 1
            log({"ev": "request", "n": calls["gemini"], "prompt": req["prompt"]})
            return scripted(req)
    g.model = model

    if a.jev:
        real_ask = J.Jev._ask

        def capped(self, *aa, **kk):
            if calls["jev"] >= a.jev:
                raise SystemExit(f"Jev budget of {a.jev} used up")
            calls["jev"] += 1
            return real_ask(self, *aa, **kk)
        J.Jev._ask = capped
        j = J.Jev()
    else:
        j = E.PatternJev(calls)

    def say(text):
        t = time.time()
        r = router.handle(j, text, speak=False)
        log({"ev": "her_words", "said": text, "did": r.get("did"), "reply": r.get("say"),
             "ms": round((time.time() - t) * 1000)})
        print(f"     she: {text!r} -> {r.get('did')} {r.get('say')!r}"[:160], flush=True)
        return r

    base, stop_server = L.serve()
    chrome = Chrome(base + "/")
    fails, steps = [], []
    try:
        # The cookie banner, once, as she would.
        snap = g.screen.snapshot(g.screen.front())
        c = next((c for c in snap["controls"] if c["label"] == "Essential only"), None)
        if c:
            click(*center(c["frame"]))
            time.sleep(0.8)
        person = Person(g, chrome, log, say, a.person)
        g.tick, g.settle = 0.3, 0.9
        r = say("Help me check in for my flight to Lisbon.")
        if not str(r.get("did", "")).startswith("guide"):
            fails.append(f"routing gave {r.get('did')}, not the guide")
        seen, last_key, last_t = None, None, time.time()
        while time.time() - t0 < a.max_seconds and not fails:
            st = g.state()
            if st.get("status") in G.ENDED:
                log({"ev": "end", "status": st["status"], "line": st.get("line")})
                break
            key = (st.get("n"), st.get("say"), st.get("rev") if st.get("kind") == "look" else 0)
            if st.get("status") != "step" or st.get("awaiting") or key == seen:
                if time.time() - last_t > STALL_S and st.get("status") == "step":
                    fails.append(f"stall at step {st.get('n')}: {st.get('say')!r}")
                time.sleep(0.2)
                continue
            rec = {"ev": "step", "n": st["n"], "kind": st["kind"], "can_do": st["can_do"],
                   "say": st["say"], "target": st.get("target_label"), "ring": st.get("ring"),
                   "wait_s": round(time.time() - last_t, 2)}
            log(rec)
            steps.append(rec)
            print(f"{st['n']:>3} {st['kind']:<6} do={str(st['can_do']):<5} {rec['wait_s']:>5}s  "
                  f"{st['say']}  [{st.get('target_label') or ''}]"[:200], flush=True)
            if (st["say"], st.get("target_label")) == last_key and st["kind"] != "look":
                fails.append(f"the same step twice in a row: {st['say']!r}")
            if st["kind"] in ("type", "choose", "click") and not st.get("ring"):
                fails.append(f"step {st['n']} has no ring: {st['say']!r}")
            last_key, seen = (st["say"], st.get("target_label")), key
            try:
                person.act(st)
            except SystemExit as e:
                fails.append(str(e))
            last_t = time.time()
        url = G.LiveScreen().page_url({"pid": chrome.pid})
        log({"ev": "url", "url": url})
    finally:
        g.stop()
        chrome.quit()
        stop_server()
        out.close()
    if "boarding-pass" not in (url or ""):
        fails.append(f"the boarding pass was not reached ({url})")
    if web_started:
        fails.append("the browser agent was started")
    print(f"\nperson {a.person}; model calls {calls['gemini']} ({'live' if a.gemini else 'scripted'}); "
          f"Jev calls {calls['jev']} ({'live' if a.jev else 'scripted'}); steps {len(steps)}; "
          f"{round(time.time() - t0)} s")
    for f in fails:
        print("FAIL", f)
    print("PASS" if not fails else f"{len(fails)} failed", f"(log {a.out})")
    sys.stdout.flush()
    os._exit(1 if fails else 0)


if __name__ == "__main__":
    main()
