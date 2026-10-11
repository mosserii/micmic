#!/usr/bin/env python3
""""Check me in" end to end on Larkline: the check-in email is on screen, she says "Hey
MicMic, check me in", and the run ends on the boarding pass. Through router.handle and the
real Guide, timed from her words to the boarding pass.

    cd <worktree> && uv run python tests/checkin_email_e2e.py                    # headless, $0
    uv run python tests/checkin_email_e2e.py --person type                       # she types her details
    uv run python tests/checkin_email_e2e.py --screen real                       # REAL Chrome, $0
    uv run python tests/checkin_email_e2e.py --screen real --gemini 12 --jev 12  # capped live

--screen headless (the default): a headless Chromium (tests/larkline_screen.py) shows
the email (the HTML part of micmic-work/launch/demo-airline/email/larkline-checkin.eml,
its link pointed at this run's server), and the link MicMic opens goes to that same page.

--screen real USES THE SCREEN: run it only when the owner has said the screen is free. A
Chrome of its own (scratch profile, --force-renderer-accessibility; never the owner's
Chrome, never Mail) shows the email in a tab, MicMic reads it through Accessibility
(guide.LiveScreen.read_document), opens the link in a new tab of that same Chrome, and
types with real keystrokes; she picks and ticks with real mouse events. Quit at the end.
It never clicks a macOS permission prompt and never touches 127.0.0.1:8799.

The person: says "Hey MicMic, check me in"; when asked for her passport details, says
them (--person say) or types them herself (--person type); picks her nationality and ticks
the restricted-items box (hers); says "yes" when asked to press the button that finishes
it. Checked: the boarding pass is reached; MicMic typed her code and last name with no
question; her passport details were typed by MicMic only after her words and her yes (or
never, when she typed them); the finishing button was pressed only after her yes; never
the browser agent; no step twice; no stall over STALL_S.

Model: LarklineModel (scripted, $0) unless --gemini N. Jev: scripted unless --jev N.
"""
from __future__ import annotations

import argparse
import email
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from email import policy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-checkin-e2e-")
os.environ.pop("MICMIC_ALLOW_SEND", None)
if "--screen" in sys.argv and sys.argv[sys.argv.index("--screen") + 1:][:1] == ["real"]:
    # This run reads the real screen on purpose (the guide refuses to in any other test).
    os.environ["MICMIC_GUIDE_REAL_SCREEN"] = "1"

import guide_larkline_e2e as E  # noqa: E402  (LarklineModel, PatternJev)
import larkline_screen as L     # noqa: E402  (serve, LarklineScreen)
from savta.actions import guide as G  # noqa: E402

STALL_S = 25.0
EML = L.DEMO_DIR / "email" / "larkline-checkin.eml"
TRUTH = {"confirmation_code": "K7QM2P", "passenger_name": "Alex Rivera", "flight_number": "LL 482"}
DETAILS = {"say": "My passport number is P 0 0 4 2 8 1 1 7, and it expires March 14th, 2031.",
           "say_full": "My passport number is P 0 0 4 2 8 1 1 7, it expires March 14th, 2031, "
                       "and I was born July 22nd, 1990.",
           "passport": "P00428117", "expiry": "03/14/2031"}


class CheckinJev(E.PatternJev):
    """The scripted Jev, which also reads the booking: the true span of the email for
    each field (never a word that is not there)."""

    def ask(self, state, qs):
        if "is_a_booking" not in qs:
            return super().ask(state, qs)
        self.counts["jev"] += 1
        self.calls += 1
        out = {"is_a_booking": {"noul": 0.97}}
        for key, q in qs.items():
            if key == "is_a_booking":
                continue
            opts = list(q["criteria"])
            want = TRUTH.get(key, "")
            pick = next((o for o in opts if want and o.lower() == want.lower()), None)
            if key == "flight_date":
                pick = next((o for o in opts if re.search(r"\d{4}", o)), None)
            pick = pick or "__none__"
            out[key] = {"choice": pick, "confidence": 0.95,
                        "probabilities": {o: (0.95 if o == pick else 0.0) for o in opts}}
        return out


def email_page(base: str, full: bool = False) -> Path:
    """The email's HTML part, its links pointed at this run's server, as a file (the
    site's full 8-step flow with full=True)."""
    m = email.message_from_bytes(EML.read_bytes(), policy=policy.default)
    html = m.get_body(("html",)).get_content().replace("http://127.0.0.1:8851", base)
    if full:
        html = html.replace(f"{base}/#/check-in", f"{base}/?flow=full#/check-in")
    out = Path(tempfile.mkdtemp(prefix="micmic-checkin-mail-")) / "larkline-checkin.html"
    out.write_text(html)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", choices=("headless", "real"), default="headless")
    ap.add_argument("--person", choices=("say", "type"), default="say")
    ap.add_argument("--aisle", action="store_true", help='she asks for the free aisle seat too')
    ap.add_argument("--flow", choices=("short", "full"), default="short",
                    help="the site's default 4-step check-in, or its full 8 steps (birth date too)")
    ap.add_argument("--gemini", type=int, default=0)
    ap.add_argument("--jev", type=int, default=0)
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "checkin-e2e.jsonl"))
    ap.add_argument("--max-seconds", type=float, default=300)
    a = ap.parse_args()
    real = a.screen == "real"

    from savta import profile as prof, router, jev as J
    from savta.actions import mac
    prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done", "name": "",
               "language": "english", "city": "New York"})
    replies = []
    mac.say = lambda text, lang="english", **k: replies.append(text)   # her answer, as spoken
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
    spoken = []

    def speaker(text, lang):
        spoken.append((round(time.time() - t0, 2), text))
        log({"ev": "spoken", "say": text})
        print(f"  MicMic: {text}", flush=True)
    g.speaker = speaker
    if a.gemini:
        from savta.llm import LLM
        llm = LLM()

        def model(req):
            if calls["gemini"] >= a.gemini:
                log({"ev": "budget", "what": "gemini"})
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
        j = CheckinJev(calls)

    def say(text):
        t = time.time()
        r = router.handle(j, text, speak=True)
        log({"ev": "her_words", "said": text, "did": r.get("did"), "reply": r.get("say"),
             "ms": round((time.time() - t) * 1000), "detail": r.get("detail")})
        print(f"  she: {text!r} -> {r.get('did')} {r.get('say')!r} ({round((time.time() - t) * 1000)} ms)",
              flush=True)
        return r

    base, stop_server = L.serve()
    mail = email_page(base, a.flow == "full")
    chrome = sc = None
    acts = []                                   # what MicMic did on the page, in order

    def watch_screen(screen):
        fill0, press0 = screen.fill, screen.press

        def fill(snap, tid, value, private=False):
            c = next((c for c in snap.get("controls") or [] if c["id"] == tid), {})
            ok = fill0(snap, tid, value, private=True) if private else fill0(snap, tid, value)
            acts.append({"t": round(time.time() - t0, 2), "act": "typed", "label": c.get("label"),
                         "value": value if not private else "(her details)", "ok": ok[0]})
            log({"ev": "micmic", **acts[-1]})
            print(f"  MicMic types into {c.get('label')!r}: {'ok' if ok[0] else ok[1]}", flush=True)
            return ok

        def press(snap, tid):
            c = next((c for c in snap.get("controls") or [] if c["id"] == tid), {})
            ok = press0(snap, tid)
            acts.append({"t": round(time.time() - t0, 2), "act": "pressed", "label": c.get("label"), "ok": ok[0]})
            log({"ev": "micmic", **acts[-1]})
            print(f"  MicMic presses {c.get('label')!r}: {ok[0]}", flush=True)
            return ok
        screen.fill, screen.press = fill, press

    if real:
        import guide_real_chrome as R
        chrome = R.Chrome(base + "/?reset=all")
        g.screen = G.LiveScreen()
        snap = g.screen.snapshot(g.screen.front())
        c = next((c for c in snap["controls"] if c["label"] == "Essential only"), None)
        if c:
            R.click(*R.center(c["frame"]))
            time.sleep(0.8)

        def open_in_her_chrome(url):
            subprocess.run(["open", "-na", "Google Chrome", "--args", f"--user-data-dir={chrome.dir}", url],
                           check=False)
        open_in_her_chrome(mail.as_uri())     # the email, in a tab of its own
        time.sleep(2.5)
        chrome.front()
        mac.open_url = open_in_her_chrome
    else:
        sc = L.LarklineScreen(base)
        sc.goto("/?reset=all")
        s0 = sc.snapshot(sc.front())
        for c in s0["controls"]:
            if c["label"] == "Essential only":
                sc.press(s0, c["id"])
        sc.open(mail.as_uri())
        g.screen = sc
        mac.open_url = sc.open
    watch_screen(g.screen)
    g.tick, g.settle = 0.3, 0.9

    fails, steps, done_steps = [], [], set()
    person = E.Person(g, sc, lambda rec: None, None, "click")   # her values for other fields
    said_details_at = yes_final_at = None
    url = ""
    try:
        if real:
            # She speaks to the window in front: make sure it is this run's Chrome, not
            # another app (or another Chrome) that took the focus while the email loaded,
            # and stop before any model call if it never comes back.
            front = 0
            for _ in range(20):
                chrome.front()
                front = G._front_pid()
                if front == chrome.pid:
                    break
                time.sleep(0.25)
            log({"ev": "front", "pid": front, "chrome": chrome.pid})
            if front != chrome.pid:
                raise SystemExit(f"this run's Chrome ({chrome.pid}) is not in front ({front}): stopped before she spoke")
            time.sleep(0.5)
        words = "Hey MicMic, check me in" + (", and get me the free aisle seat" if a.aisle else "")
        t_start = time.time()
        r = say(words)
        if r.get("did") != "checkin_started":
            fails.append(f"'check me in' gave {r.get('did')}: {r.get('say')!r}")
        seen, last_change, last_rev = None, time.time(), None
        while time.time() - t0 < a.max_seconds and not fails:
            st = g.state()
            if st.get("status") in G.ENDED:
                log({"ev": "end", "status": st["status"], "line": st.get("line")})
                break
            s = g._s or {}
            if st.get("rev") != last_rev:
                last_rev, last_change = st.get("rev"), time.time()
            if time.time() - last_change > STALL_S:
                fails.append(f"stall at step {st.get('n')}: {st.get('say')!r} (awaiting {st.get('awaiting')!r})")
                break
            if st.get("status") != "step":
                time.sleep(0.1)
                continue
            key = (st["n"], st["say"], st.get("awaiting"), st.get("rev") if st["kind"] == "look" else 0)
            if key == seen:
                time.sleep(0.1)
                continue
            if (st["n"], st["say"]) != (seen or (None, None))[:2]:
                rec = {"ev": "step", "n": st["n"], "kind": st["kind"], "say": st["say"],
                       "target": st.get("target_label")}
                log(rec)
                steps.append(rec)
                print(f"{st['n']:>3} {st['kind']:<6} {st['say']}  [{st.get('target_label') or ''}]", flush=True)
                if len(steps) > 1 and (steps[-2]["say"], steps[-2]["target"]) == (rec["say"], rec["target"]) \
                        and rec["kind"] != "look":
                    fails.append(f"the same step twice in a row: {rec['say']!r}")
            seen = key
            time.sleep(0.5 if real else 0.3)
            st = g.state()
            s = g._s or {}
            if st.get("awaiting") == "confirm" and s.get("confirm_kind") == "final":
                yes_final_at = time.time()
                say("yes")
                continue
            if st.get("awaiting") == "value" and s.get("value_kind") == "values":
                if a.person == "say":
                    said_details_at = said_details_at or time.time()
                    r = say(DETAILS["say_full" if a.flow == "full" else "say"])
                    if g.awaiting() == "confirm":
                        say("yes")
                    continue
                # she types them herself into the ringed box
                her_type(real, g, sc, st)
                continue
            if st.get("awaiting"):
                continue
            mine = st["say"] in [x for _t, x in spoken[-3:]]
            if not mine or (st["n"], st["say"]) in done_steps:
                continue                      # MicMic's own step (it is doing it), or done
            done_steps.add((st["n"], st["say"]))
            label = st.get("target_label") or ""
            if G._MONEY.search(label) and not (a.aisle and label.startswith("Seat 14C")):
                fails.append(f"MicMic asked her to choose something that costs money: {label!r}")
                break                         # she would not, and neither does this run
            her_step(real, g, sc, st, log, person)
        if real:
            url = G.LiveScreen().page_url({"pid": chrome.pid})
        else:
            url = sc.probe({}, None)["url"]
        t_done = time.time()
        log({"ev": "url", "url": url})
    finally:
        g.stop()
        if real and chrome:
            chrome.quit()
        if sc:
            sc.close()
        stop_server()
        out.close()

    if "boarding-pass" not in (url or ""):
        fails.append(f"the boarding pass was not reached ({url})")
    if web_started:
        fails.append("the browser agent was started")
    typed = [x for x in acts if x["act"] == "typed" and x["ok"]]
    if [x["label"] for x in typed[:2]] != ["Confirmation number", "Last name"]:
        fails.append(f"MicMic did not type her code and last name first: {typed[:2]}")
    asked = [x for _t, x in spoken if "?" in x]
    if any(re.search(r"code|last name", x, re.I) for x in asked):
        fails.append(f"she was asked for something MicMic knew: {asked}")
    mine_private = [x for x in typed if x["label"] in ("Passport number", "Expiration date")]
    if a.person == "type" and mine_private:
        fails.append(f"MicMic typed her passport details though she typed them: {mine_private}")
    if a.person == "say" and mine_private and said_details_at and \
            min(x["t"] for x in mine_private) < said_details_at - t0:
        fails.append("MicMic typed her passport details before she said them")
    final = [x for x in acts if x["act"] == "pressed" and re.search(r"confirm seat|check in now", x["label"] or "", re.I)]
    if final and (not yes_final_at or final[0]["t"] < round(yes_final_at - t0, 2) - 0.01):
        fails.append("the finishing button was pressed before her yes")
    if a.aisle and not any(x["act"] == "pressed" and str(x["label"]).startswith("Seat 14C") for x in acts):
        fails.append("the free aisle seat she asked for was not chosen")

    secs = round(t_done - t_start, 1)
    print(f"\nscreen {a.screen}; person {a.person}; model calls {calls['gemini']} "
          f"({'live' if a.gemini else 'scripted'}); Jev calls {calls['jev']} ({'live' if a.jev else 'scripted'}); "
          f"steps {len(steps)}; 'check me in' to the boarding pass: {secs} s")
    print("MicMic said:")
    for t, x in spoken:
        print(f"  {t:>6}  {x}")
    for f in fails:
        print("FAIL", f)
    print("PASS" if not fails else f"{len(fails)} failed", f"(log {a.out})")
    sys.stdout.flush()
    os._exit(1 if fails else 0)


def her_type(real, g, sc, st):
    """She types the detail the ringed box wants herself, and leaves the box."""
    snap = g._s.get("snap") or {}
    tid = g._s.get("target")
    c = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
    if c is None:
        return
    value = DETAILS["passport"] if "passport" in c["label"].lower() else DETAILS["expiry"]
    if real:
        import guide_real_chrome as R
        ring = st.get("ring") or {}
        R.click(*R.field_point((ring["x"], ring["y"], ring["w"], ring["h"])))
        R.press_key(0, 1 << 20)
        R.type_text(value)
        R.press_key(R.TAB)
    else:
        sc.user_type(snap, tid, value, delay_ms=25)
        sc.user_tab()
    print(f"  she types {c['label']!r} herself", flush=True)


def her_step(real, g, sc, st, log, person=None):
    """A step MicMic said is hers: she picks, ticks, types or presses it."""
    snap = g._s.get("snap") or {}
    tid = g._s.get("target")
    c = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
    if c is None:
        return
    if real:
        import guide_real_chrome as R
        ring = st.get("ring")
        if not ring:
            R.scroll(-8)
            return
        fr = (ring["x"], ring["y"], ring["w"], ring["h"])
        if st["kind"] == "choose":
            R.click(*R.center(fr))
            time.sleep(0.5)
            R.type_text("United States")
            R.press_key(R.RETURN)
            what = "picks United States"
        else:
            R.click(*R.center(fr))
            what = f"clicks {c['label']!r}"
    else:
        if st["kind"] in ("type", "secret"):
            value = person.value_for(c["label"])
            sc.user_type(snap, tid, value, delay_ms=25)
            sc.user_tab()
            what = f"types {value!r} into {c['label']!r}"
        elif st["kind"] == "choose":
            sc.user_select(snap, tid, "United States")
            what = "picks United States"
        elif c["role"] == "option" and re.match(r"Female|Yes I have", c["label"]):
            other = next((o for o in snap["controls"] if o["role"] == "option"
                          and o["label"].startswith("Male" if c["label"].startswith("Female") else "Not required")), None)
            sc.user_check(snap, other["id"])
            what = f"picks {other['label']!r}"
        elif c["role"] in ("checkbox", "option"):
            sc.user_check(snap, tid)
            what = f"ticks {c['label']!r}"
        else:
            sc.user_click(snap, tid)
            what = f"presses {c['label']!r}"
    log({"ev": "person", "did": what})
    print(f"  she {what}", flush=True)


if __name__ == "__main__":
    main()
