#!/usr/bin/env python3
"""The first-run onboarding: its endpoints, when it shows, and the page itself.

    .venv/bin/python3 tests/onboarding/test_onboarding.py

Each server this starts is this worktree's own, on port 8881, with a fresh
MICMIC_STATE_DIR, sends and calls disarmed, and MICMIC_DRY_OPEN=1 so pressing "Turn on"
never brings System Settings up in front of whoever is using the Mac. Never 8799.
Playwright's bundled Chromium, headless; one server and one browser at a time.

Free by default: the servers cannot reach Jev or Gemini (see OFFLINE below), and the
checks that judge a real answer are skipped. MICMIC_LIVE=1 runs those too, for about
a cent.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "savta" / "web"
PORT = 8881
BASE = f"http://127.0.0.1:{PORT}"
assert PORT != 8799, "never the live server"

PASSED, FAILED, SKIPPED = [], [], []
NOCOST = []           # /api/health of each server that ended with zero Jev calls
# Jev and Gemini cost money, so by default the servers here cannot reach them: they run
# as a proxy client pointed at a closed local port, which also skips reading the
# developer's keys from .env.local, and no device gets registered with the cloud. Every
# turn then answers "cannot reach my service", which is all most checks need. The few
# that judge a real answer run only with MICMIC_LIVE=1 and are listed as skipped
# otherwise, never counted as passed.
LIVE = os.environ.get("MICMIC_LIVE") == "1"
OFFLINE = {} if LIVE else {"MICMIC_MODE": "proxy", "MICMIC_PROXY_URL": "http://127.0.0.1:9",
                           "MICMIC_PROXY_TOKEN": "offline-test"}


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


def live_check(name, ok, detail=""):
    """A check of what Jev answered: real money, so only with MICMIC_LIVE=1."""
    if LIVE:
        return check(name, ok, detail)
    SKIPPED.append(name)
    print("SKIP " + name + "   (needs a live turn: MICMIC_LIVE=1)")


# ---------------------------------------------------------------- server
def _port_open():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


class Server:
    """A private server with its own state directory, stopped on exit."""

    def __init__(self, profile: dict | None = None, settings: dict | None = None):
        if _port_open():
            raise SystemExit(f"something is already listening on {PORT}; stop it first")
        self.state = Path(tempfile.mkdtemp(prefix="ob-test-state-"))
        # Always write one, even for "a new Mac": with no profile.json in the state
        # dir, the app copies in the checkout's own (legacy migration in paths.state),
        # and a developer's real, finished profile made every Mac look set up.
        (self.state / "profile.json").write_text(json.dumps(profile or {}))
        if settings is not None:
            (self.state / "settings.json").write_text(json.dumps(settings))

    def __enter__(self):
        env = dict(os.environ, MICMIC_PORT=str(PORT), MICMIC_STATE_DIR=str(self.state),
                   MICMIC_DRY_OPEN="1", **OFFLINE)
        env.pop("MICMIC_ALLOW_SEND", None)
        env.pop("MICMIC_ALLOW_CALL", None)
        self.proc = subprocess.Popen([str(ROOT / ".venv/bin/python3"), "-m", "savta.server"],
                                     cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(200):
            if _port_open():
                return self
            time.sleep(0.1)
        self.proc.kill()
        raise SystemExit(f"server on {PORT} did not start")

    def __exit__(self, *exc):
        # Every server here is keyless unless MICMIC_LIVE=1: its own count of paid Jev
        # calls says so before it goes.
        if not LIVE:
            try:
                h = json.loads(urllib.request.urlopen(BASE + "/api/health", timeout=5).read())
                ok = not h.get("calls") and not h.get("cost_usd")
            except Exception as e:  # noqa: BLE001
                h, ok = repr(e), False
            if not ok:
                check("this server made no paid model calls", False, h)
            else:
                NOCOST.append(h)
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def settings(self) -> dict:
        f = self.state / "settings.json"
        return json.loads(f.read_text()) if f.exists() else {}


def call(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# The page's messages to native land here instead of in WKWebView.
INIT = """
window.__msgs = [];
window.webkit = {messageHandlers: {
  micmic:    {postMessage: m => window.__msgs.push(String(m))},
  micmicbar: {postMessage: m => window.__msgs.push('bar:' + m)}}};
"""


def new_page(browser, lang="en", scheme="light", reduced="no-preference"):
    ctx = browser.new_context(viewport={"width": 380, "height": 560}, color_scheme=scheme,
                              reduced_motion=reduced)
    ctx.add_init_script(INIT + f"try{{localStorage.setItem('micmic.lang',{json.dumps(lang)})}}catch(_){{}}"
                        "try{sessionStorage.clear()}catch(_){}")
    page = ctx.new_page()
    page.__errors = []
    page.__requests = []
    page.on("pageerror", lambda e: page.__errors.append(str(e)))
    page.on("request", lambda r: page.__requests.append(r.url))
    return ctx, page


def settle(page, path="/?panel=1"):
    page.goto(BASE + path)
    page.wait_for_function("document.documentElement.lang.length === 2")
    page.wait_for_timeout(900)          # config, the loader, /api/onboarding, the sheet


def shown(page):
    return page.evaluate("!!document.querySelector('.ob') && document.body.classList.contains('onboarding')")


# ---------------------------------------------------------------- static
def strip_comments(src: str) -> str:
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # // comments, but not the // inside a URL
    return "\n".join(re.sub(r"(^|[^:\"'])//.*$", r"\1", line) for line in src.splitlines())


def test_static():
    for f in (WEB / "index.html", WEB / "onboarding/onboarding.js", WEB / "onboarding/onboarding.css"):
        code = strip_comments(f.read_text(encoding="utf-8"))
        check(f"{f.relative_to(ROOT)}: no em dash outside comments", "—" not in code,
              [l.strip()[:80] for l in code.splitlines() if "—" in l][:3])
    for f in (WEB / "onboarding/onboarding.js", WEB / "onboarding/onboarding.css"):
        check(f"{f.relative_to(ROOT)}: no em dash at all", "—" not in f.read_text(encoding="utf-8"))
        check(f"{f.relative_to(ROOT)}: no external assets",
              not re.search(r"https?://", strip_comments(f.read_text(encoding="utf-8"))))
    html = (WEB / "index.html").read_text(encoding="utf-8")
    keys = set(re.findall(r"\b(ob[0-9A-Z]\w*):", html))
    for table in ("he_f", "en", "ar", "ru"):
        m = re.search(rf"\n  {table}:\{{(.*?)\}},\n", html, flags=re.S)
        body = m.group(1) if m else ""
        missing = sorted(k for k in keys if f"{k}:" not in body)
        check(f"COPY.{table} has every onboarding string", m is not None and not missing, missing)
    js = (WEB / "onboarding/onboarding.js").read_text(encoding="utf-8")
    used = set(re.findall(r"""data-t="(ob\w+)"|t\("(ob\w+)"\)|"(ob[A-Z0-9]\w*)"|:"(ob[A-Z0-9]\w*)\"""", js))
    used = {x for tup in used for x in tup if x}
    check("every string the script uses exists in COPY", used <= keys, sorted(used - keys))
    check("the script adds no element with an id", not re.search(r"""\bid=["']""", js))
    css = (WEB / "onboarding/onboarding.css").read_text(encoding="utf-8")
    sels = [s.strip() for block in re.findall(r"(^|})\s*([^{}@]+)\{", strip_comments(css), flags=re.M)
            for s in block[1].split(",")]
    loose = [s for s in sels if s and not re.search(r"\.ob\b|\.ob-|body\.onboarding|^from$|^to$|^\d+%", s)]
    check("every onboarding rule is scoped to .ob or body.onboarding", not loose, loose[:5])


# ---------------------------------------------------------------- unit
def test_note_utterance_filters():
    """In-process: what counts as the turn step 3 is waiting for."""
    os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="ob-unit-")
    sys.path.insert(0, str(ROOT))
    from savta import onboarding_api as ob
    ob._armed_at, ob._turn = 0.0, None
    ob.note_utterance(b'{"text":"what time is it"}')
    check("an utterance before step 3 does not count", ob.status()["turn"] is None)
    ob.act("try")
    for body, why in ((b'{"text":"what time","speculative":true}', "a speculative guess"),
                      (b'{"text":"what time","source":"screen"}', "text that is not from the mic"),
                      (b'{"text":"   "}', "an empty press"),
                      (b'not json', "a broken body")):
        ob.note_utterance(body)
        check(f"{why} does not count", ob.status()["turn"] is None)
    ob.note_utterance(json.dumps({"text": "מה השעה?", "client": "native"}).encode())
    t = ob.status()["turn"]
    check("a real utterance after step 3 counts, with what was heard",
          t == {"heard": "מה השעה?"}, t)
    ob.act("try")
    check("arming again forgets the last turn", ob.status()["turn"] is None)
    check("an unknown action is refused", ob.act("launch")[0] == 400)
    code, body = ob.open_pane("../../etc")
    check("only the three known panes can be opened", code == 400 and body["error"] == "unknown_pane")
    check("every pane is a System Settings privacy URL",
          all(u.startswith("x-apple.systempreferences:com.apple.preference.security?Privacy_")
              for u in ob.PANES.values()))


# ---------------------------------------------------------------- endpoints
def test_endpoints(srv):
    code, d = call("GET", "/api/onboarding")
    check("GET /api/onboarding answers", code == 200, code)
    check("its shape is {show, first_run, onboarded, armed, turn}",
          set(d) == {"show", "first_run", "onboarded", "armed", "turn"}, d)
    check("a new Mac: first_run, not onboarded, so it shows",
          d["first_run"] is True and d["onboarded"] is False and d["show"] is True, d)

    code, p = call("GET", "/api/permissions")
    states = {"granted", "denied", "not_asked", "unknown"}
    check("GET /api/permissions answers", code == 200, code)
    check("its shape is {microphone, speech, accessibility, ready}",
          set(p) == {"microphone", "speech", "accessibility", "ready"}, p)
    check("every permission is granted, denied, not_asked or unknown",
          all(p[k] in states for k in ("microphone", "speech", "accessibility")), p)
    check("accessibility is never not_asked (there is no prompt state for it)",
          p["accessibility"] != "not_asked", p)
    check("ready is exactly all three granted",
          p["ready"] is all(p[k] == "granted" for k in ("microphone", "speech", "accessibility")), p)

    code, o = call("POST", "/api/permissions/open", {"pane": "accessibility"})
    check("POST /api/permissions/open accessibility (dry run here)",
          code == 200 and o == {"ok": True, "opened": "accessibility", "dry": True}, o)
    code, o = call("POST", "/api/permissions/open", {"pane": "https://example.com"})
    check("a pane that is not one of the three is refused", code == 400, (code, o))
    code, o = call("POST", "/api/onboarding", {"action": "nope"})
    check("an unknown onboarding action is refused", code == 400, (code, o))

    # QA #29: the page never used the contact list, and "name" was written twice.
    req = urllib.request.urlopen(BASE + "/api/config", timeout=10)
    raw = req.read().decode()
    check("/api/config sends no contact list to the page", "contacts" not in json.loads(raw), raw[:200])
    check("/api/config has one name key", raw.count('"name"') == 1, raw.count('"name"'))
    code, c1 = call("GET", "/api/config?contacts=1")
    check("the listener's ?contacts=1 still gets them", isinstance(c1.get("contacts"), list), c1.keys())
    # A client dropping the connection is routine: no traceback on the console.
    import io, contextlib, socket as _s
    with socket.create_connection(("127.0.0.1", PORT)) as c:
        c.setsockopt(_s.SOL_SOCKET, _s.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    check("the server still answers after a client reset its connection",
          call("GET", "/api/health")[0] == 200)
    from savta import server as _srv
    err = io.StringIO()
    try:
        raise ConnectionResetError(54, "Connection reset by peer")
    except ConnectionResetError:
        with contextlib.redirect_stderr(err):
            _srv.Server.handle_error(type("S", (), {})(), None, ("127.0.0.1", 1))
    check("a dropped connection prints nothing", err.getvalue() == "", err.getvalue()[:200])

    code, s = call("POST", "/api/settings", {"onboarded": "yes"})
    check("the settings validator refuses a non-boolean onboarded",
          "onboarded" in s.get("rejected", []), s)
    check("config has not changed after a refused value", "onboarded" not in srv.settings())


def test_skip_persists(srv, browser):
    ctx, page = new_page(browser)
    settle(page)
    check("fresh Mac: the onboarding is up in the panel", shown(page))
    check("the page underneath is hidden while it is up",
          page.evaluate("getComputedStyle(document.querySelector('.stage')).visibility") == "hidden")
    page.keyboard.press("Escape")
    page.wait_for_timeout(700)
    check("Esc skips it and it is gone", not shown(page) and not page.query_selector(".ob"))
    code, d = call("GET", "/api/onboarding")
    check("skip is saved on the server", d["onboarded"] is True and d["show"] is False, d)
    check("as the onboarded setting in settings.json", srv.settings().get("onboarded") is True,
          srv.settings())
    call("POST", "/api/settings", {"hotkey": "right-command"})
    check("a later Settings save keeps onboarded", srv.settings().get("onboarded") is True,
          srv.settings())
    call("POST", "/api/settings", {"hotkey": "right-option"})
    page.reload()
    page.wait_for_function("document.documentElement.lang.length === 2")
    page.wait_for_timeout(900)
    check("never again once onboarded, and skipping it finishes setup (first_run false)",
          not shown(page) and call("GET", "/api/config")[1]["first_run"] is False)
    check("no page errors", not page.__errors, page.__errors)
    ctx.close()


def test_only_first_run(browser):
    with Server(profile={"setup_complete": True, "step": "done", "language": "english"}):
        code, d = call("GET", "/api/onboarding")
        check("set up already: /api/onboarding says do not show", d["show"] is False, d)
        ctx, page = new_page(browser)
        settle(page)
        check("set up already: no onboarding in the panel", not shown(page))
        check("set up already: its script is not even fetched",
              not any("/onboarding/" in u for u in page.__requests), page.__requests)
        ctx.close()


def _chip(browser, path="/"):
    """What the page shows with nothing remembered in the browser: no micmic.lang in
    localStorage, the way a freshly installed app's web view starts."""
    ctx = browser.new_context(viewport={"width": 380, "height": 560})
    ctx.add_init_script(INIT)
    page = ctx.new_page()
    page.goto(BASE + path)
    page.wait_for_function("document.documentElement.lang.length === 2")
    page.wait_for_timeout(600)
    got = (page.inner_text("#langnow"), page.evaluate("document.documentElement.lang"))
    ctx.close()
    return got


def test_speech_language_default(browser):
    """English unless she chose another language in Settings. A fresh 1.0.1 came up
    listening in Hebrew (listener log locale=he-IL, chip "HE") with en-US in the
    shipped config: profile.DEFAULT carried speech_lang he-IL and /api/config read
    that placeholder as a learned language."""
    with Server():
        cfg = call("GET", "/api/config")[1]
        check("a new Mac listens in English", cfg.get("language_hint") == "en-US", cfg.get("language_hint"))
        check("a new Mac's page shows EN", _chip(browser) == ("EN", "en"), _chip(browser))
        call("POST", "/api/settings", {"language_hint": "ru-RU"})
        check("choosing Russian in Settings is what it then listens in",
              call("GET", "/api/config")[1].get("language_hint") == "ru-RU")
        _, s = call("POST", "/api/settings", {"language_hint": "en-US"})
        check("and choosing English again is echoed back",
              s.get("settings", {}).get("language_hint") == "en-US", s)
    with Server(profile={"setup_complete": True, "step": "done", "language": "hebrew",
                         "speech_lang": "he-IL"}):
        cfg = call("GET", "/api/config")[1]
        check("a language setup only heard does not choose the recogniser",
              cfg.get("language_hint") == "en-US", cfg.get("language_hint"))
        check("but it is sent as the second language a turn is read in",
              cfg.get("language") == "hebrew", cfg.get("language"))
    with Server(profile={"setup_complete": True, "step": "done", "language": "hebrew"},
                settings={"language_hint": "he-IL"}):
        cfg = call("GET", "/api/config")[1]
        check("someone who chose Hebrew in Settings keeps Hebrew",
              cfg.get("language_hint") == "he-IL", cfg.get("language_hint"))
        check("and their page shows HE", _chip(browser) == ("HE", "he"), _chip(browser))
    with Server(settings={"language_hint": "xx-XX"}):
        check("a settings value that is not one of the four falls back to English",
              call("GET", "/api/config")[1].get("language_hint") == "en-US")


def _say(text):
    code, r = call("POST", "/api/utterance", {"text": text, "speak": False,
                                              "client": "native", "activation": "push"},
                   timeout=60)
    return r


def test_onboarded_ends_voice_setup():
    """QA #16: once the window's onboarding is finished or skipped, the old spoken setup
    never runs: no "What should I call you?" tacked onto answers, no greeting on a
    silent press. The owner's 1.0.1 was left with onboarded true and setup_complete
    false; that state is resolved to done."""
    with Server(profile={}, settings={"onboarded": True}) as srv:
        check("onboarded but never set up by voice: resolved to set up at start",
              call("GET", "/api/config")[1]["first_run"] is False
              and json.loads((srv.state / "profile.json").read_text()).get("setup_complete") is True)
        r = _say("")
        check("a silent press says nothing, no greeting", r.get("did") == "empty", r)
        r = _say("what time is it micmic")
        live_check("an answer has no setup question after it",
              r.get("did") == "answered" and (r.get("say") or "").startswith("It is")
              and "call you" not in (r.get("say") or ""), r.get("say"))
    with Server() as srv:
        r = _say("what time is it micmic")
        live_check("not onboarded yet: the answer comes first, then the one-line intro in English",
              (r.get("say") or "").startswith("It is")
              and (r.get("say") or "").endswith("I'm MicMic. What should I call you?"), r.get("say"))
        call("POST", "/api/onboarding", {"action": "done"})
        check("finishing the window finishes setup",
              json.loads((srv.state / "profile.json").read_text()).get("setup_complete") is True
              and call("GET", "/api/config")[1]["first_run"] is False)
        r = _say("what time is it")
        check("and the spoken setup question never comes back",
              "call you" not in (r.get("say") or ""), r.get("say"))


def test_other_modes(srv, browser):
    for path, what in (("/", "the browser page"), ("/?bar=1", "the bar")):
        ctx, page = new_page(browser)
        settle(page, path)
        check(f"{what}: no onboarding, not even its script, on a first run",
              not shown(page) and not any("/onboarding/" in u for u in page.__requests))
        ctx.close()


def test_flow(srv, browser):
    """Every screen, by keyboard, then a real turn completes it."""
    ctx, page = new_page(browser)
    settle(page)
    check("step 1 of 3 is showing", page.evaluate(
        "document.querySelector('.ob-step.on').dataset.n") == "1")
    check("the dialog is labelled", page.get_attribute(".ob", "aria-label") == "Welcome to MicMic")
    check("progress says step 1 of 3", page.get_attribute(".ob-prog", "aria-valuetext") == "Step 1 of 3")
    page.wait_for_selector('.ob-demo[data-p="press"]', timeout=4000)
    check("the demo presses the right Option key", page.evaluate(
        "getComputedStyle(document.querySelector('.ob-hot')).color") == "rgb(255, 255, 255)")
    # Keys: Space must not open the microphone, "d" must not open the engineer's panel.
    page.keyboard.press("Space")
    page.keyboard.press("d")
    check("Space and d do not reach the page while it is up",
          page.evaluate("window.__msgs.length") == 0
          and not page.evaluate("document.getElementById('dev').classList.contains('show')"))
    page.mouse.click(190, 300)
    check("a click on the screen does not arm the microphone", page.evaluate("window.__msgs.length") == 0)
    # Tab stays inside: the language pill and the screen's own buttons.
    inside = True
    for _ in range(6):
        page.keyboard.press("Tab")
        inside &= page.evaluate("""(() => { const a = document.activeElement;
            return !!(a.closest('.ob') || a.closest('#lang')); })()""")
    check("Tab stays inside the onboarding", inside)
    page.focus(".ob-step.on .ob-title")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    check("Enter moves to step 2", page.evaluate("document.querySelector('.ob-step.on').dataset.n") == "2")
    page.wait_for_timeout(1300)
    rows = page.evaluate("[...document.querySelectorAll('.ob-perm')].map(r => [r.dataset.k, r.dataset.s])")
    real = call("GET", "/api/permissions")[1]
    check("step 2 shows what the server reports, row by row",
          all(real[k] == s for k, s in rows) and len(rows) == 3, (rows, real))
    if real["accessibility"] != "granted":
        check("Accessibility not granted: its Turn on button is there",
              page.is_visible('.ob-on[data-pane="accessibility"]'))
    if not real["ready"]:
        page.click(".ob-act button:not([hidden])")
    page.wait_for_selector('.ob-step[data-n="3"].on', timeout=4000)
    check("step 3 arms the server", call("GET", "/api/onboarding")[1]["armed"] is True)
    page.wait_for_timeout(400)
    page.click(".ob-orb")
    check("the circle hands the press to the listener", page.evaluate("window.__msgs") == ["listen"],
          page.evaluate("window.__msgs"))
    # A real utterance, the way the listener sends one, speech off.
    code, r = call("POST", "/api/utterance", {"text": "What time is it?", "speak": False,
                                               "client": "native"}, timeout=60)
    check("the utterance went through", code == 200, code)
    live_check("the window is introducing MicMic: no spoken setup question on the try",
          "call you" not in (r.get("say") or "") and (r.get("say") or "").startswith("It is"),
          r.get("say"))
    page.wait_for_selector(".ob-step.ok", timeout=5000)
    check("step 3 completes on the real turn, showing what was heard",
          page.inner_text(".ob-qtext") == "What time is it?" and page.inner_text(".ob-step.ok .ob-title") == "You're set.")
    check("finishing is saved at once", call("GET", "/api/onboarding")[1]["onboarded"] is True)
    page.wait_for_selector(".ob", state="detached", timeout=6000)
    check("then it closes into the normal window", not shown(page) and page.evaluate(
        "getComputedStyle(document.querySelector('.stage')).visibility") == "visible")
    check("the page's hooks are its own again", page.evaluate(
        "!String(window.panelState).includes('live(')"))
    check("the language pill is back in its place",
          page.evaluate("getComputedStyle(document.getElementById('lang')).right") == "52px")
    check("no page errors", not page.__errors, page.__errors)
    ctx.close()


def test_languages_and_motion(browser):
    """Four languages, both directions, both schemes, and reduced motion. No em dash in
    anything a person can read on any screen."""
    speech = {"he": "he-IL", "en": "en-US", "ar": "ar-SA", "ru": "ru-RU"}
    for lang, rtl in (("he", True), ("en", False), ("ar", True), ("ru", False)):
        # The page's language is the server's speech language; the browser's saved one
        # only covers the moment before /api/config answers.
        with Server(settings={"language_hint": speech[lang]}):
            ctx, page = new_page(browser, lang=lang, scheme="dark" if rtl else "light",
                                 reduced="reduce" if lang == "ar" else "no-preference")
            settle(page)
            texts, over = [], []
            for n in (1, 2, 3):
                texts.append(page.inner_text(".ob"))
                over += page.evaluate("""(() => [...document.querySelectorAll('.ob-step.on *')]
                    .filter(e => !e.closest('.ob-kbd'))      // cropped on purpose, clipped
                    .filter(e => { const r = e.getBoundingClientRect();
                                   return r.width && (r.left < -1 || r.right > innerWidth + 1); })
                    .map(e => e.className))()""")
                over += [] if page.evaluate(
                    "document.scrollingElement.scrollWidth <= innerWidth") else ["page scrolls sideways"]
                if n < 3:
                    page.click(".ob-act button:not([hidden])")
                    page.wait_for_timeout(900)
            check(f"{lang}: direction is {'rtl' if rtl else 'ltr'}",
                  page.evaluate("document.documentElement.dir") == ("rtl" if rtl else "ltr"))
            check(f"{lang}: no em dash on any screen", not any("—" in x for x in texts))
            check(f"{lang}: nothing runs off the 380px window", not over, over[:4])
            check(f"{lang}: the keyboard stays left to right", page.evaluate(
                "getComputedStyle(document.querySelector('.ob-kbd')).direction") == "ltr")
            if lang == "ar":
                # A second window on the same, still unfinished Mac. (Skipping here and
                # then writing onboarded false no longer brings it back: skipping now
                # finishes setup, which is the point.)
                ctx2, p2 = new_page(browser, lang="ar", reduced="reduce")
                settle(p2)
                check("reduced motion: the demo is one still frame", p2.evaluate(
                    "document.querySelector('.ob-demo').dataset.p") == "press")
                p2.wait_for_timeout(2500)
                check("reduced motion: and it stays still", p2.evaluate(
                    "document.querySelector('.ob-demo').dataset.p") == "press")
                ctx2.close()
            ctx.close()


def main():
    test_static()
    test_note_utterance_filters()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)    # bundled Chromium, never channel="chrome"
        try:
            with Server() as srv:
                for t in (test_endpoints, test_other_modes, test_flow):
                    print(f"\n-- {t.__name__}")
                    try:
                        t(srv) if t is test_endpoints else t(srv, browser)
                    except Exception as e:  # noqa: BLE001
                        check(f"{t.__name__} ran to the end", False, repr(e)[:400])
            with Server() as srv:
                print("\n-- test_skip_persists")
                try:
                    test_skip_persists(srv, browser)
                except Exception as e:  # noqa: BLE001
                    check("test_skip_persists ran to the end", False, repr(e)[:400])
            print("\n-- test_onboarded_ends_voice_setup")
            try:
                test_onboarded_ends_voice_setup()
            except Exception as e:  # noqa: BLE001
                check("test_onboarded_ends_voice_setup ran to the end", False, repr(e)[:400])
            for t in (test_only_first_run, test_speech_language_default,
                      test_languages_and_motion):
                print(f"\n-- {t.__name__}")
                try:
                    t(browser)
                except Exception as e:  # noqa: BLE001
                    check(f"{t.__name__} ran to the end", False, repr(e)[:400])
        finally:
            browser.close()
    if not LIVE:
        print(f"\n{len(NOCOST)} servers, each ended with 0 Jev calls and $0")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped"
          + ("" if LIVE else " (no model calls; MICMIC_LIVE=1 runs the skipped ones for real)"))
    for f in FAILED:
        print("  FAILED:", f)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
