#!/usr/bin/env python3
"""The browser agent waits for her at a human check or a sign-in, then carries on.

    uv run python tests/test_web_resume.py

Offline and free: no browser, no network, no Jev, no Gemini. web.drive() runs its real
step loop on a scripted page, and Jev is a script that raises on any question it was
not told to expect, so "no model calls while waiting" is checked, not assumed. The
router half runs router.handle() with understanding scripted and web.run() pointed at
the same scripted page.

The owner's case (2026-09-27): a domain search on a registrar hit a "verify you are
human" page, MicMic said "that part is yours", he solved it in the window, and nothing
happened, because the task had already ended.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Nothing here may land in her real state (see tests/test_micmic.py, wall two).
os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-web-resume-")
os.environ.pop("MICMIC_ALLOW_SEND", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from savta.actions import web      # noqa: E402

PASSED, FAILED = [], []


def check(name: str, ok, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(("  ok    " if ok else "  FAIL  ") + name + ("" if ok else f"   <- {detail}"))


# ------------------------------------------------------------------ the fakes
def _links(n: int, start: int = 100) -> list[dict]:
    return [{"id": start + i, "tag": "a", "kind": "click", "type": "", "name": "",
             "label": f"Link {i}", "value": "", "inModal": False} for i in range(n)]


PAGES = {
    "home": {"url": "https://registrar.example/", "title": "Find a domain",
             "text": "Find your perfect domain name. Search.",
             "elements": [{"id": 2, "tag": "button", "kind": "click", "type": "",
                           "name": "", "label": "Search", "value": "",
                           "inModal": False}] + _links(8)},
    # A real challenge page has almost nothing on it, which also exercises the thin-page
    # waits that come first.
    "captcha": {"url": "https://registrar.example/challenge", "title": "Just a moment...",
                "text": "Verify you are human by completing the action below.",
                "elements": [{"id": 50, "tag": "a", "kind": "click", "type": "",
                              "name": "", "label": "Privacy", "value": "",
                              "inModal": False}],
                "frames": ["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x"]},
    "results": {"url": "https://registrar.example/search?q=micmic",
                "title": "micmic.app is available",
                "text": "Domain results for micmic: micmic.app is available, micmic.io",
                "elements": _links(10, 200)},
    "signin": {"url": "https://registrar.example/login", "title": "Sign in",
               "text": "Sign in to continue.",
               "elements": [{"id": 60, "tag": "input", "kind": "type", "type": "email",
                             "name": "username  username", "label": "Email",
                             "value": "", "inModal": False},
                            {"id": 61, "tag": "input", "kind": "type", "type": "password",
                             "name": "password  current-password", "label": "Password",
                             "value": "", "inModal": False},
                            {"id": 62, "tag": "button", "kind": "click", "type": "",
                             "name": "", "label": "Sign in", "value": "",
                             "inModal": False}] + _links(8, 300)},
    "checkout": {"url": "https://registrar.example/checkout", "title": "Checkout",
                 "text": "Your basket: micmic.app. Order total 12.99",
                 "elements": [{"id": 70, "tag": "button", "kind": "click", "type": "",
                               "name": "", "label": "Place order", "value": "",
                               "inModal": False}] + _links(8, 400)},
}

# What Jev "says" on each page. The challenge page is deliberately absent: asking the
# model anything there is a failure.
DECIDE = {
    "home":     {"op": "click", "target": "2", "page_is": "form", "goal_met": 0.1},
    "results":  {"op": "done", "page_is": "results", "goal_met": 0.9},
    "signin":   {"op": "blocked", "page_is": "wrong", "goal_met": 0.1, "needs_her": 0.9},
    "checkout": {"op": "click", "target": "70", "page_is": "checkout", "goal_met": 0.5},
}


def _choice(x: str, c: float = 0.9) -> dict:
    return {"choice": x, "confidence": c, "probabilities": {x: c}}


class FakeJev:
    def __init__(self, page=None, stop_score: float = 0.9):
        self.page, self.stop_score = page, stop_score
        self.calls = 0
        self.cost_usd = 0.0
        self.busy_ms = 0.0
        self.asked_on: list[str] = []

    def ask(self, state, questions: dict) -> dict:
        self.calls += 1
        if "ends_at_checkout" in questions:
            return {"ends_at_checkout": {"noul": 0.1}}
        if "stop_it" in questions:
            return {"stop_it": {"noul": self.stop_score}}
        if "operation" in questions:
            name = self.page.state
            self.asked_on.append(name)
            if web.waiting():
                raise AssertionError("asked Jev while waiting for her")
            if name not in DECIDE:
                raise AssertionError(f"asked Jev on the {name!r} page")
            d = DECIDE[name]
            a = {"operation": _choice(d["op"]), "goal_met": {"noul": d["goal_met"]},
                 "needs_her": {"noul": d.get("needs_her", 0.05)},
                 "page_is": _choice(d["page_is"])}
            if "target" in d:
                a[f"{d['op']}_target"] = _choice(d["target"])
            return a
        raise AssertionError(f"unexpected Jev question: {sorted(questions)}")


class FakeNode:
    def __init__(self, page, el):
        self.page, self.el = page, el

    def evaluate(self, js: str):
        if "el.click()" in js:
            self.page.pressed(self.el)
        return True           # hittable, focused

    def scroll_into_view_if_needed(self, timeout=0):
        pass

    def click(self, timeout=0):
        self.page.pressed(self.el)

    def input_value(self):
        return self.el.get("value", "")


class _Keys:
    def press(self, key):
        pass

    def type(self, text, delay=0):
        pass


class _Mouse:
    def wheel(self, dx, dy):
        pass


class FakePage:
    """A site that follows a script. `after` maps a pressed element id to the page it
    leads to; `her` is what she does while the agent waits: after `solve_after` polls
    the page becomes `solved_to` (or, with `stop_after`, the router is told to stop)."""

    def __init__(self, state: str, after: dict, solve_after=None, solved_to=None,
                 stop_after=None):
        self.state = state
        self.after = after
        self.solve_after, self.solved_to, self.stop_after = solve_after, solved_to, stop_after
        self.polls = 0
        self.waiting_seen: set = set()
        self.presses: list[str] = []
        self.keyboard, self.mouse = _Keys(), _Mouse()

    @property
    def url(self):
        return PAGES[self.state]["url"]

    def title(self):
        return PAGES[self.state]["title"]

    def evaluate(self, js: str):
        p = PAGES[self.state]
        return {"url": p["url"], "title": p["title"], "modal": False, "text": p["text"],
                "elements": [dict(e) for e in p["elements"]],
                "frames": list(p.get("frames", [])), "solved": p.get("solved", False)}

    def query_selector(self, sel: str):
        want = sel.split('"')[1]
        for e in PAGES[self.state]["elements"]:
            if str(e["id"]) == want:
                return FakeNode(self, e)
        return None

    def pressed(self, el):
        self.presses.append(el["label"])
        if el["id"] in self.after:
            self.state = self.after[el["id"]]

    def wait_for_timeout(self, ms):
        time.sleep(0.001)
        kind = web.waiting()
        if not kind:
            return
        self.waiting_seen.add(kind)
        self.polls += 1
        if self.stop_after is not None and self.polls >= self.stop_after:
            web.stop_waiting()
        if self.solve_after is not None and self.polls >= self.solve_after:
            self.state = self.solved_to

    def wait_for_load_state(self, *a, **k):
        pass


def _drive(page, j, **kw) -> tuple[dict, dict]:
    seen = {"handoff": [], "resume": [], "jev_at_handoff": None, "jev_at_resume": None}

    def on_handoff(kind):
        seen["handoff"].append(kind)
        seen["jev_at_handoff"] = j.calls

    def on_resume(kind):
        seen["resume"].append(kind)
        seen["jev_at_resume"] = j.calls
    kw.setdefault("on_handoff", on_handoff)
    r = web.drive(j, None, "find me a nice domain for my app called mic mic", page,
                  page.url, on_resume=on_resume, **kw)
    return r, seen


# ------------------------------------------------------------------ web.drive
def t_check_clears_and_the_task_goes_on():
    print("a human check appears, she solves it, the same task carries on and finishes")
    page = FakePage("home", after={2: "captcha"}, solve_after=4, solved_to="results")
    j = FakeJev(page)
    t0 = time.time()
    r, seen = _drive(page, j)
    check("handed to her once, as a human check", seen["handoff"] == ["human_check"],
          str(seen["handoff"]))
    check("the browser said it was waiting while she worked",
          page.waiting_seen == {"human_check"}, str(page.waiting_seen))
    check("it carried on once the check was gone", seen["resume"] == ["human_check"],
          str(seen["resume"]))
    check("no Jev call while it waited",
          seen["jev_at_handoff"] == seen["jev_at_resume"],
          f"{seen['jev_at_handoff']} -> {seen['jev_at_resume']}")
    check("Jev was never asked about the challenge page", "captcha" not in j.asked_on,
          str(j.asked_on))
    check("and the task finished on the results", r["did"] == "done"
          and r["url"] == PAGES["results"]["url"], f"did={r['did']} url={r['url']}")
    check("the log says it waited and carried on",
          "waiting for her: human check" in r["steps"]
          and "she finished it, carrying on" in r["steps"], str(r["steps"]))
    en = web.shown_steps(r["steps"], "english")
    he = web.shown_steps(r["steps"], "hebrew")
    check("she sees 'Waiting for you: solve the check in the browser'",
          "Waiting for you: solve the check in the browser" in en, str(en))
    check("and 'Thanks, carrying on'", "Thanks, carrying on" in en, str(en))
    check("in Hebrew too", "מחכה לך: הבדיקה בדפדפן" in he and "תודה, ממשיכה" in he, str(he))
    check("no em dash in what she sees", not any("—" in x for x in en + he))
    check("nothing is left waiting afterwards", web.waiting() is None)
    check("fast with a scripted page", time.time() - t0 < 5, f"{time.time() - t0:.1f}s")


def t_check_never_clears():
    print("a human check that is never solved ends politely after the wait")
    page = FakePage("home", after={2: "captcha"})
    j = FakeJev(page)
    t0 = time.time()
    r, seen = _drive(page, j, handoff_wait=0.3)
    check("it waited, then stopped", r["did"] == "needs_her" and r.get("waited_out")
          and r["ended"] == "waited for her, then stopped", str(r))
    check("and never claimed to carry on", seen["resume"] == [])
    check("the reason is still the human check",
          r["why"] == "the site asked for a human check", r.get("why"))
    check("within the wait it was given", time.time() - t0 < 3, f"{time.time() - t0:.1f}s")
    check("nothing is left waiting afterwards", web.waiting() is None)


def t_she_says_stop_while_it_waits():
    print("she says stop while it waits: it stops")
    page = FakePage("home", after={2: "captcha"}, stop_after=3)
    j = FakeJev(page)
    t0 = time.time()
    r, seen = _drive(page, j)
    check("stopped", r["did"] == "stopped"
          and r["ended"] == "she said stop while it waited", str(r))
    check("without waiting out the three minutes", time.time() - t0 < 3,
          f"{time.time() - t0:.1f}s")
    check("and did not carry on", seen["resume"] == [] and "results" not in j.asked_on)
    check("nothing is left waiting afterwards", web.waiting() is None)
    page = FakePage("home", after={2: "results"})
    ev = threading.Event()
    ev.set()
    r, _ = _drive(page, FakeJev(page), stop=ev)
    check("a stop while it is working (not waiting) ends it at the next step",
          r["did"] == "stopped" and page.presses == [], str(r))


def t_sign_in_is_waited_for_too():
    print("a sign-in wall: she signs in, it carries on")
    page = FakePage("home", after={2: "signin"}, solve_after=3, solved_to="results")
    j = FakeJev(page)
    r, seen = _drive(page, j)
    check("handed to her as a sign-in", seen["handoff"] == ["sign_in"], str(seen))
    check("carried on and finished", seen["resume"] == ["sign_in"] and r["did"] == "done",
          f"did={r['did']}")
    check("the agent never typed into the password field",
          "Password" not in page.presses, str(page.presses))
    en = web.shown_steps(r["steps"], "english")
    check("she sees 'Waiting for you: sign in in the browser'",
          "Waiting for you: sign in in the browser" in en, str(en))


def t_payment_is_never_resumed():
    print("the final pay button stays hers: no wait, no press")
    page = FakePage("home", after={2: "checkout", 70: "results"})
    j = FakeJev(page)
    r, seen = _drive(page, j)
    check("stopped in front of the purchase", r["did"] == "needs_payment", r["did"])
    check("the button was never pressed", "Place order" not in page.presses,
          str(page.presses))
    check("and there was no wait to carry on past it", seen["handoff"] == [], str(seen))


def t_nobody_watching_keeps_the_old_ending():
    print("with nobody to hand it to (a headless run), a check ends the run as before")
    page = FakePage("home", after={2: "captcha"})
    j = FakeJev(page)
    r, _ = _drive(page, j, on_handoff=None)
    check("needs her, with no wait", r["did"] == "needs_her" and not r.get("waited_out")
          and page.polls == 0, str(r))


def t_detection():
    print("what counts as a human check, and as one that is finished")
    base = {"title": "Shop", "text": "Products", "elements": [], "frames": []}
    check("an ordinary page is not a check", not web.human_check(base))
    check("'verify you are human' is", web.human_check({**base, "text": "Please verify you are human"}))
    check("Hebrew 'I am not a robot' is", web.human_check({**base, "text": "אני לא רובוט"}))
    check("a visible challenge frame is", web.human_check(
        {**base, "frames": ["https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"]}))
    check("an invisible reCAPTCHA badge is not", not web.human_check(
        {**base, "frames": ["https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible"]}))
    check("a check whose answer field is filled in is finished", not web.human_check(
        {**base, "text": "captcha", "solved": True}))
    check("a password box is a sign-in", web.is_secret({"type": "password"}))
    check("a one-time code is a sign-in", web.is_secret(
        {"type": "text", "name": "otp one-time-code", "label": "Code"}))
    check("a card number is not a sign-in (never waited past)", not web.is_secret(
        {"type": "text", "name": "cardnumber cc-number", "label": "Card number"}))
    check("stop_waiting with nothing waiting says so", web.stop_waiting() is False)


# ------------------------------------------------------------------ router
def t_router_answers_at_once_then_carries_on():
    print("router: the turn answers while it waits, then says how it went")
    from savta import router
    from savta.actions import mac
    said: list[str] = []
    pages: list[FakePage] = []

    def fake_run(j, llm, task, start, **kw):
        page = FakePage("home", after={2: "captcha"}, solve_after=30, solved_to="results")
        pages.append(page)
        kw.pop("headless", None)
        return web.drive(j, llm, task, page, start, **kw)

    with _scripted_router(fake_run, said) as j:
        t0 = time.time()
        r = router.handle(j, "find me a nice domain on the registrar for my app called mic mic")
        dt = time.time() - t0
        check("the turn answers at once", r["did"] == "web_working" and dt < 5
              and r["say"] == "Working on it in the browser.",
              f"{r.get('did')} {r.get('say')} {dt:.1f}s")
        _until(lambda: web.waiting() is not None)
        st = router.web_status()
        check("she is told what she has to do", any(
            "Waiting for you: solve the check in the browser" in x for x in said), str(said))
        check("no em dash in anything said", not any("—" in x for x in said))
        check("/api/web_status says it is waiting", st["waiting"] == "human_check"
              and "Waiting for you" in st["say"], str(st))
        check("and carries the run the reply named",
              st["id"] == (r.get("detail") or {}).get("web_run"), str(st))
        pages[0].solve_after = pages[0].polls + 1           # she solves it now
        _until(lambda: (router.web_status().get("finished") or {}).get("id") == st["id"])
        fin = router.web_status()["finished"] or {}
        check("it said 'Thanks, carrying on'", "Thanks, carrying on." in said, str(said))
        check("then how it ended", fin.get("did") == "web_done"
              and fin.get("say") == "Found it. It is on the screen."
              and said[-1] == "Found it. It is on the screen.", f"{fin} {said}")
        check("in that order", said.index("Thanks, carrying on.")
              < said.index("Found it. It is on the screen."), str(said))


def t_router_stop_while_waiting():
    print("router: 'stop' while it waits stops the browser task")
    from savta import router
    said: list[str] = []

    def fake_run(j, llm, task, start, **kw):
        kw.pop("headless", None)
        return web.drive(j, llm, task, FakePage("home", after={2: "captcha"}), start, **kw)

    with _scripted_router(fake_run, said) as j:
        r = router.handle(j, "find me a nice domain on the registrar for my app called mic mic")
        run_id = (r.get("detail") or {}).get("web_run")
        _until(lambda: web.waiting() is not None)
        check("waiting first", r["did"] == "web_working" and web.waiting() == "human_check",
              r.get("did"))
        t0 = time.time()
        s = router.handle(j, "stop")
        check("'stop' is answered as stopping it", s["did"] == "web_stopped"
              and s["say"] == "Okay, I stopped.", str(s.get("did")) + " " + str(s.get("say")))
        _until(lambda: (router.web_status().get("finished") or {}).get("id") == run_id)
        fin = router.web_status()["finished"] or {}
        check("the task ended as stopped, quickly", fin.get("did") == "web_stopped"
              and time.time() - t0 < 5, str(fin))
        check("and said nothing more after 'Okay, I stopped.'",
              said[-1] == "Okay, I stopped." and not fin.get("say"), str(said))
        check("nothing is left waiting", web.waiting() is None)

    print("router: Hebrew 'stop' is answered in Hebrew")
    with _scripted_router(lambda j, llm, task, start, **kw: web.drive(
            j, llm, task, FakePage("home", after={2: "captcha"}), start,
            **{k: v for k, v in kw.items() if k != "headless"}), said, lang="hebrew") as j:
        r = router.handle(j, "תמצאי לי דומיין יפה לאפליקציה שלי")
        check("'working on it' in Hebrew", r["say"] == "עובדת על זה בדפדפן.", r.get("say"))
        _until(lambda: web.waiting() is not None)
        s = router.handle(j, "עצרי")
        check("עצרתי", s["did"] == "web_stopped" and s["say"] == "בסדר, עצרתי.", s.get("say"))
        _until(lambda: web.waiting() is None)


def t_router_long_run_without_a_check():
    print("router: a long run with no check answers at once, and the bar can follow it")
    from savta import router
    said: list[str] = []
    release = threading.Event()
    reached = threading.Event()

    def slow_run(j, llm, task, start, on_step=None, **kw):
        # Stands in for a 75 s run: it logs steps, then holds until the test lets go.
        on_step("consent: Reject all")
        on_step("click Search")
        reached.set()
        release.wait(10)
        on_step("click micmic.app")
        return {"did": "done", "steps": ["consent: Reject all", "click Search",
                                         "click micmic.app"],
                "url": "https://registrar.example/r", "title": "micmic.app", "why": ""}

    with _scripted_router(slow_run, said) as j:
        t0 = time.time()
        r = router.handle(j, "find me a nice domain on the registrar for my app called mic mic")
        dt = time.time() - t0
        check("the turn answers long before the run ends (no 45 s listener timeout)",
              r["did"] == "web_working" and dt < 2, f"{r.get('did')} {dt:.2f}s")
        check("its reply names the run for the bar to follow",
              (r.get("detail") or {}).get("web_run") == router.web_status()["id"])
        reached.wait(5)
        st = router.web_status()
        check("while it runs, /api/web_status has its latest step in her words",
              st["running"] and not st["waiting"] and st["progress"] == "Pressed Search"
              and not st["finished"], str(st))
        check("and nothing has been said about the ending yet",
              "Found it. It is on the screen." not in said, str(said))
        release.set()
        _until(lambda: bool(router.web_status().get("finished")))
        st = router.web_status()
        check("when it ends, the ending is there and said",
              not st["running"] and (st["finished"] or {}).get("say") == "Found it. It is on the screen."
              and said[-1] == "Found it. It is on the screen.", f"{st} {said}")


class _scripted_router:
    """understand() scripted to do_online, the web start fixed, Jev a script."""

    def __init__(self, fake_run, said: list, lang: str = "english"):
        self.fake_run, self.said, self.lang = fake_run, said, lang

    def __enter__(self):
        from savta import router
        from savta.actions import mac
        self.prev = (router.understand, router.get_contacts, router.due_briefing,
                     router.pick_span, web.run, web.where_to_start, mac.say)
        lang = self.lang
        # A finished first run with a made-up name, in this test's own state dir, so
        # handle() goes straight to the request instead of the onboarding branch.
        from savta import profile as prof
        prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done",
                   "name": "Test", "language": lang})

        def understand(j, utt, contacts, recent="", playing="", likes=None, spans=None,
                       draft=None):
            return _u("do_online", language=lang)
        router.understand = understand
        router.get_contacts = lambda *a, **k: []
        router.due_briefing = lambda: False
        router.pick_span = lambda j, utt, q: (utt, 0.9)
        web.run = self.fake_run
        web.where_to_start = lambda j, task: ("https://registrar.example/", "anything")
        mac.say = lambda text, lang="english", *a, **k: self.said.append(text)
        self.j = FakeJev()
        # The page is created inside run(); Jev reads whichever page is live.
        real_run = self.fake_run
        j = self.j

        def run_and_track(j_, llm, task, start, **kw):
            orig_drive = web.drive

            def drive(j2, llm2, goal, page, start_url, **kw2):
                j.page = page
                return orig_drive(j2, llm2, goal, page, start_url, **kw2)
            web.drive = drive
            try:
                return real_run(j_, llm, task, start, **kw)
            finally:
                web.drive = orig_drive
        web.run = run_and_track
        return self.j

    def __exit__(self, *exc):
        from savta import router
        from savta.actions import mac
        router._web_stop()
        _until(lambda: not router._web_running())
        (router.understand, router.get_contacts, router.due_briefing,
         router.pick_span, web.run, web.where_to_start, mac.say) = self.prev
        return False


_U_KEYS = ("intent_confidence", "wants_full_length", "names_title", "contact_confidence",
           "contact_named", "has_message_content", "money_involved", "sounds_coached",
           "control_confidence", "wants_recent", "asking_for_notes", "is_complete",
           "noise", "refers_back", "is_compound", "needs_knowledge", "about_weather",
           "about_clock", "setting_emergency_contact", "inside_an_app",
           "speaker_gender_confidence", "rejects_last", "describes_instead",
           "refers_to_screen", "distress", "emergency", "wants_undo", "amends_message")


def _u(intent: str, **kw) -> dict:
    """The same shape tests/test_micmic.py's _u() builds."""
    u = {k: 0.0 for k in _U_KEYS}
    u.update({"raw": {}, "spans": {}, "intent": intent, "intent_probs": {intent: 0.9},
              "media_kind": "not_applicable", "contact": "nobody",
              "control_action": "not_applicable", "player_action": "not_applicable",
              "channel": "imessage", "file_kind": "any", "when_minutes": "none",
              "language": "english", "speaker_gender": "unrevealed",
              "screen_task": "not_applicable", "weather_day": "today",
              "write_in": "not_applicable", "message_app": "unchanged",
              "intent_confidence": 0.95, "is_complete": 0.95, "contact_confidence": 0.9})
    u.update(kw)
    return u


def _until(cond, limit: float = 5.0) -> None:
    t0 = time.time()
    while not cond() and time.time() - t0 < limit:
        time.sleep(0.01)


def main() -> int:
    tests = [t_detection, t_check_clears_and_the_task_goes_on, t_check_never_clears,
             t_she_says_stop_while_it_waits, t_sign_in_is_waited_for_too,
             t_payment_is_never_resumed, t_nobody_watching_keeps_the_old_ending,
             t_router_answers_at_once_then_carries_on, t_router_stop_while_waiting,
             t_router_long_run_without_a_check]
    only = sys.argv[1:]
    for t in tests:
        if only and not any(o in t.__name__ for o in only):
            continue
        try:
            t()
        except Exception:  # noqa: BLE001
            import traceback
            FAILED.append(t.__name__)
            print(f"  FAIL  {t.__name__} raised\n{traceback.format_exc()}")
        print()
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
