#!/usr/bin/env python3
"""The page half of the permission fallbacks (PERMISSION_FALLBACKS.md, lane L5).

    cd <worktree> && .venv/bin/python3 tests/permission_page/test_permission_page.py

P1-9  A reply's detail.fix = {"pane": ...} is ONE "Open Settings" button: under the
      reply in the window and the panel, in the bar's action slot. It POSTs
      /api/permissions/open {pane} and says "System Settings is open" only when the
      server answered ok:true. detail.fix as a plain string (the dev "start the server
      with ..." notes) or an unknown pane shows nothing. The native hooks take it as an
      optional extra argument: panelReply(text, fix), barResult(say, undo, fix).
P1-10 The Settings comment no longer claims Screen Recording is asked just in time;
      Screen Recording and Automation are listed read only when /api/permissions
      reports them, with no button and no request.
P2-4  Settings > Open at login: "requires_approval" (GET /api/config
      "open_at_login_status") shows one line and an Open Login Items button.

Playwright's bundled Chromium, headless, never channel="chrome". The server is this
worktree's own on port 8894, with a throwaway MICMIC_STATE_DIR, MICMIC_DRY_OPEN=1 (no
System Settings jumps in front of anyone), fake permissions, and no way to reach Jev,
Gemini or ElevenLabs: zero paid calls, checked against its own /api/health at the end.
Never 8799. Every POST the page makes is intercepted except /api/permissions/open,
which the server answers dry.
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
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "savta" / "web"
PORT = 8894
BASE = f"http://127.0.0.1:{PORT}"
assert PORT != 8799, "never the live server"

PASSED, FAILED = [], []
OFFLINE = {"MICMIC_MODE": "proxy", "MICMIC_PROXY_URL": "http://127.0.0.1:9",
           "MICMIC_PROXY_TOKEN": "offline-test", "MICMIC_CLOUD_VOICE": "off"}
PANES = ["accessibility", "microphone", "speech", "screen_recording", "full_disk",
         "automation", "login_items"]

# Must match the COPY tables in index.html.
COPY = {
    "en": {"open": "Open Settings", "opened": "System Settings is open",
           "fail": "I couldn't open Settings", "loginOpen": "Open Login Items",
           "approve": "To open at login, MicMic needs your OK in Login Items in System Settings.",
           "screen": "Screen Recording", "auto": "Automation", "on": "On", "off": "Off"},
    "he": {"open": "פתיחת ההגדרות", "opened": "ההגדרות פתוחות"},
    "ar": {"open": "فتح الإعدادات", "opened": "إعدادات النظام مفتوحة"},
    "ru": {"open": "Открыть настройки", "opened": "Системные настройки открыты"},
}


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


def _port_open():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


class Server:
    def __init__(self):
        if _port_open():
            raise SystemExit(f"something is already listening on {PORT}; stop it first")
        self.state = Path(tempfile.mkdtemp(prefix="perm-page-state-"))
        self.asr = Path(tempfile.mkdtemp(prefix="perm-page-asr-"))
        (self.state / "profile.json").write_text(json.dumps(
            {"setup_complete": True, "step": "done", "language": "english"}))
        (self.state / "settings.json").write_text(json.dumps({"onboarded": True}))

    def __enter__(self):
        env = dict(os.environ, MICMIC_PORT=str(PORT), MICMIC_STATE_DIR=str(self.state),
                   MICMIC_ASR_DIR=str(self.asr), MICMIC_ASR_NO_DOWNLOAD="1",
                   MICMIC_DRY_OPEN="1", **OFFLINE,
                   MICMIC_FAKE_PERMISSIONS=json.dumps(
                       {"microphone": "granted", "speech": "granted", "accessibility": "denied"}))
        for k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL", "MICMIC_BUNDLE_PATH",
                  "MICMIC_FAKE_MOVE", "MICMIC_DICTATION_HOME"):
            env.pop(k, None)
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
        try:
            h = json.loads(urllib.request.urlopen(BASE + "/api/health", timeout=5).read())
            check("the server made no paid model calls (Jev calls and cost both zero)",
                  not h.get("calls") and not h.get("cost_usd"), h)
        except Exception as e:  # noqa: BLE001
            check("the server made no paid model calls", False, repr(e))
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


INIT = """
window.__msgs = [];
window.webkit = {messageHandlers: {
  micmic:    {postMessage: m => window.__msgs.push(String(m))},
  micmicbar: {postMessage: m => window.__msgs.push('bar:' + m)}}};
class FakeSR { start(){} stop(){} abort(){} }
window.SpeechRecognition = FakeSR; window.webkitSpeechRecognition = FakeSR;
"""


class Page:
    """A page whose POSTs are all intercepted, except /api/permissions/open (dry on
    the server) unless `open_status` overrides it. `reply` is what /api/utterance
    answers; `config_extra` / `perms_extra` are merged into the real GET answers."""

    def __init__(self, browser, path="/?panel=1", lang="en", width=380, height=560):
        self.ctx = browser.new_context(viewport={"width": width, "height": height})
        self.ctx.add_init_script(INIT + f"try{{localStorage.setItem('micmic.lang',{json.dumps(lang)})}}catch(_){{}}")
        self.reply = {"did": "answered", "say": "ok"}
        self.config_extra = {}
        self.perms_extra = {}
        self.open_status = None            # None: the real (dry) server; else (code, body)
        self.guide_state = None            # None: the real server; else GET /api/guide/state
        self.opens, self.asks, self.posts, self.errors = [], [], [], []
        self.ctx.route("**/api/**", self._route)
        self.page = self.ctx.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(BASE + path)
        self.page.wait_for_function("document.documentElement.lang.length === 2")
        self.page.wait_for_timeout(700)

    def _route(self, route):
        req = route.request
        url = req.url
        if req.method == "POST":
            body = {}
            try:
                body = json.loads(req.post_data or "{}")
            except Exception:  # noqa: BLE001
                pass
            if "/api/permissions/open" in url:
                self.opens.append(body)
                if self.open_status is None:
                    return route.continue_()
                code, payload = self.open_status
                return route.fulfill(status=code, content_type="application/json",
                                     body=json.dumps(payload))
            if "/api/permissions/request" in url:
                self.asks.append(body)
                return route.fulfill(status=200, content_type="application/json", body='{"ok":false}')
            self.posts.append(url.split(BASE)[-1])
            if "/api/utterance" in url:
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps(self.reply))
            return route.fulfill(status=200, content_type="application/json", body="{}")
        if "/api/guide/state" in url and self.guide_state is not None:
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(self.guide_state))
        if ("/api/config" in url and self.config_extra) or \
           (re.search(r"/api/permissions(\?|$)", url) and self.perms_extra):
            resp = route.fetch()
            d = resp.json()
            d.update(self.config_extra if "/api/config" in url else self.perms_extra)
            return route.fulfill(response=resp, body=json.dumps(d))
        return route.continue_()

    def ev(self, js, arg=None):
        return self.page.evaluate(js, arg)

    def chip(self):
        return self.ev("""() => { const b = document.getElementById('fixchip');
            return {hidden: b.hidden, shown: !!b.getClientRects().length,
                    label: document.getElementById('fixchipw').textContent,
                    pane: b.dataset.pane, bad: b.classList.contains('bad')}; }""")

    def close(self):
        self.ctx.close()


# ---------------------------------------------------------------- static
def strip_comments(src: str) -> str:
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"(^|[^:\"'])//.*$", r"\1", line) for line in src.splitlines())


def test_static():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    code = strip_comments(html)
    check("index.html: no em dash outside comments", "—" not in code,
          [l.strip()[:80] for l in code.splitlines() if "—" in l][:3])
    keys = ["fixOpened", "fixFail", "sLoginApprove", "sLoginOpen", "spScreen", "spAuto", "obOpenSettings"]
    for table in ("he_f", "en", "ar", "ru"):
        m = re.search(rf"\n  {table}:\{{(.*?)\}},\n", html, flags=re.S)
        body = m.group(1) if m else ""
        missing = [k for k in keys if f"{k}:" not in body]
        check(f"COPY.{table} has every new permission string", m is not None and not missing, missing)
    m = re.search(r"var FIX_PANES = (\[.*?\]);", html, flags=re.S)
    panes = json.loads(m.group(1)) if m else None
    check("the button's panes are exactly the spec's seven", panes == PANES, panes)
    check("the Settings comment no longer claims Screen Recording is asked just in time",
          "MicMic asks\n   for those just in time" not in html
          and "MicMic asks for those just in time" not in html)
    check("the button opens through /api/permissions/open, never a URL of its own",
          "x-apple.systempreferences" not in code)


# ---------------------------------------------------------------- P1-9 in the window
def test_reply_button(browser):
    p = Page(browser, "/?panel=1")
    p.reply = {"did": "cannot_press", "say": "To press buttons in other apps I need Accessibility.",
               "detail": {"fix": {"pane": "accessibility"}}}
    p.ev("send('press 5 in calculator')")
    p.page.wait_for_selector("#fixchip:not([hidden])", timeout=4000)
    c = p.chip()
    check("a reply with detail.fix.pane shows one Open Settings button",
          c["shown"] and c["label"] == COPY["en"]["open"] and c["pane"] == "accessibility", c)
    check("exactly one such button on the page",
          p.ev("document.querySelectorAll('.fixchip:not([hidden])').length") == 1)
    p.page.click("#fixchip")
    p.page.wait_for_function(f"document.getElementById('fixchipw').textContent === {json.dumps(COPY['en']['opened'])}",
                             timeout=4000)
    check("pressing it POSTs /api/permissions/open with that pane, once",
          p.opens == [{"pane": "accessibility"}], p.opens)
    check("after the server's ok:true it says System Settings is open", p.chip()["label"] == COPY["en"]["opened"])
    check("nothing asked macOS for a permission", not p.asks, p.asks)

    # A plain-string fix (the dev notes) and an unknown pane are not buttons.
    p.reply = {"did": "send_disabled", "say": "Sending is off.",
               "detail": {"fix": "start the server with MICMIC_ALLOW_SEND=1"}}
    p.ev("send('send dana hi')")
    p.page.wait_for_timeout(500)
    check("detail.fix as a string shows no button, and the last one is gone", p.chip()["hidden"], p.chip())
    p.reply = {"did": "x", "say": "x", "detail": {"fix": {"pane": "javascript:alert(1)"}}}
    p.ev("send('another one')")
    p.page.wait_for_timeout(500)
    check("an unknown pane shows no button", p.chip()["hidden"], p.chip())

    # Every pane of the seven is accepted, and a plain reply takes it away again.
    shown = []
    for pane in PANES:
        p.reply = {"did": "needs_permission", "say": "x", "detail": {"fix": {"pane": pane}}}
        p.ev(f"send('ask {pane}')")
        p.page.wait_for_selector("#fixchip:not([hidden])", timeout=3000)
        shown.append(p.chip()["pane"])
    check("each of the seven panes gets the button", shown == PANES, shown)
    p.reply = {"did": "answered", "say": "It is three o'clock."}
    p.ev("send('what time is it')")
    p.page.wait_for_timeout(500)
    check("the next reply without a fix takes the button away", p.chip()["hidden"])

    # A failed open is said as failed, never "is open".
    p.reply = {"did": "needs_permission", "say": "x", "detail": {"fix": {"pane": "login_items"}}}
    p.open_status = (400, {"ok": False, "error": "unknown_pane"})
    p.ev("send('open at login please')")
    p.page.wait_for_selector("#fixchip:not([hidden])", timeout=3000)
    p.page.click("#fixchip")
    p.page.wait_for_function(f"document.getElementById('fixchipw').textContent === {json.dumps(COPY['en']['fail'])}",
                             timeout=4000)
    c = p.chip()
    check("a refused open says it could not open Settings, in the warning colour",
          c["label"] == COPY["en"]["fail"] and c["bad"], c)
    p.open_status = (200, {"ok": False, "error": "could_not_open"})
    p.ev("fixShow('full_disk')")
    p.page.click("#fixchip")
    p.page.wait_for_function(f"document.getElementById('fixchipw').textContent === {json.dumps(COPY['en']['fail'])}",
                             timeout=4000)
    check("a 200 with ok:false is a failure too", p.chip()["label"] == COPY["en"]["fail"])

    # The new-conversation button clears it.
    p.ev("fixShow('speech')")
    p.page.click("#newchat")
    check("a new conversation takes the button away", p.chip()["hidden"])
    check("no page errors", not p.errors, p.errors)
    p.close()


# ---------------------------------------------------------------- P1-9 through native's hooks
def test_panel_hooks(browser):
    p = Page(browser, "/?panel=1")
    p.ev("panelReply('I can\\'t read WhatsApp on this Mac yet.', 'full_disk')")
    c = p.chip()
    check("panelReply(text, pane) shows the button", c["shown"] and c["pane"] == "full_disk", c)
    p.ev("panelHeard('call mom')")
    check("her next words take it away", p.chip()["hidden"])
    p.ev("panelReply('x', {pane: 'automation'})")
    check("panelReply(text, {pane}) works too", p.chip()["pane"] == "automation")
    p.ev("panelReply('Done.')")
    check("panelReply(text) alone, as native calls it today, shows none", p.chip()["hidden"])
    check("no page errors", not p.errors, p.errors)
    p.close()


def test_bar_hooks(browser):
    p = Page(browser, "/?bar=1", width=560, height=120)
    p.ev("barState('listening'); barHeard('take a screenshot')")
    p.ev("barResult('To take screenshots myself I need Screen Recording.', null, 'screen_recording')")
    c = p.chip()
    area = p.ev("getComputedStyle(document.getElementById('fixchip')).gridArea")
    check("barResult(say, null, pane) puts the button in the action slot",
          c["shown"] and c["pane"] == "screen_recording" and area.startswith("act"), (c, area))
    # screen_recording joins the server's PANES in lane L4; answered here so this test
    # does not depend on which lane merged first.
    p.open_status = (200, {"ok": True, "opened": "screen_recording", "dry": True})
    p.page.click("#fixchip")
    p.page.wait_for_function(f"document.getElementById('fixchipw').textContent === {json.dumps(COPY['en']['opened'])}",
                             timeout=4000)
    msgs = p.ev("window.__msgs")
    check("pressing it on the bar tells native she is using it (touch)", "bar:touch" in msgs, msgs)
    check("and opens that pane", p.opens == [{"pane": "screen_recording"}], p.opens)
    p.ev("barState('listening')")
    check("a new turn clears it", p.chip()["hidden"])
    p.ev("barResult('Sent.', 'Undo', 'accessibility')")
    check("with something to undo, Undo keeps the slot and no second button shows",
          p.chip()["hidden"] and not p.ev("document.getElementById('undo').hidden"))
    p.ev("barState('idle'); barResult('x', null, {pane:'accessibility'})")
    check("barResult(..., {pane}) works too", p.chip()["pane"] == "accessibility")
    p.ev("barState('error', 'No connection')")
    check("an error takes it away", p.chip()["hidden"])
    p.ev("barState('idle'); barResult('Played.', null)")
    check("barResult(say, undo) alone, as native calls it today, shows none", p.chip()["hidden"])
    check("the bar never asked macOS for anything", not p.asks, p.asks)
    check("no page errors", not p.errors, p.errors)
    p.close()


# The bar while a step-by-step guide runs: the guide's layout has no "act" column, and
# "Do it for me" without Accessibility is exactly the reply that carries a fix. The
# button goes in the guide's own rows, under the step, never in a column of its own.
GUIDE_STEP = {"active": True, "id": "g-fix", "status": "step", "n": 2,
              "goal_label": "Save the photo", "say": "Click the blue Save button at the top.",
              "can_do": True, "why": "That keeps the photo on your Mac.", "why_shown": False}
BAR_GUIDE_JS = """() => {
  const R = e => { const r = e.getBoundingClientRect();
    return {x: r.left, y: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height}; };
  const st = document.querySelector('.stage'), cs = getComputedStyle(st);
  const b = document.getElementById('fixchip');
  return {guide: document.documentElement.classList.contains('b-guide'),
          cols: cs.gridTemplateColumns, rows: cs.gridTemplateRows.split(' ').length,
          stage: R(st), say: R(document.getElementById('gSay')),
          acts: R(document.getElementById('gActs')), chip: R(b),
          chipShown: !b.hidden && !!b.getClientRects().length,
          area: getComputedStyle(b).gridArea, vw: innerWidth}; }"""


def _fit(p):
    """What native does with a height message: the window is as tall as .stage."""
    h = p.ev("Math.ceil(document.querySelector('.stage').getBoundingClientRect().height)")
    p.page.set_viewport_size({"width": p.page.viewport_size["width"], "height": max(64, min(300, h))})
    p.page.wait_for_timeout(60)


def test_bar_guide_fix(browser):
    for lang in ("en", "he"):
        p = Page(browser, "/?bar=1", lang=lang, width=560, height=160)
        p.guide_state = dict(GUIDE_STEP)
        if lang != "en":
            p.ev(f"applyLanguage({json.dumps(lang)}, {{remember:false}})")
        p.page.wait_for_function("document.documentElement.classList.contains('b-guide')",
                                 timeout=4000)
        p.ev("barState('listening'); barHeard('do it for me')")
        p.ev("barResult('I need Accessibility to press it for you.', null)")
        _fit(p)
        before = p.ev(BAR_GUIDE_JS)
        p.ev("barResult('I need Accessibility to press it for you.', null, 'accessibility')")
        _fit(p)
        after = p.ev(BAR_GUIDE_JS)
        tag = f"({lang}) "
        check(tag + "a guide is on the bar for this check", after["guide"], after)
        check(tag + "with a guide up, the fix button shows on the bar",
              after["chipShown"] and p.chip()["pane"] == "accessibility", after)
        check(tag + "it does not add a column: the guide's columns are unchanged",
              after["cols"] == before["cols"], (before["cols"], after["cols"]))
        check(tag + "it sits in the guide's own row, not the act slot",
              after["area"].startswith("gfix"), after["area"])
        check(tag + "the step keeps its full width",
              abs(after["say"]["w"] - before["say"]["w"]) < 1, (before["say"], after["say"]))
        s, c = after["stage"], after["chip"]
        check(tag + "the button is inside the strip (and the window)",
              c["x"] >= s["x"] and c["r"] <= s["r"] + 0.5 and c["y"] >= s["y"]
              and c["b"] <= s["b"] + 0.5 and c["r"] <= after["vw"] + 0.5, (s, c))
        check(tag + "the strip stays under the bar's 300px cap", s["h"] <= 300, s)
        a = after["acts"]
        check(tag + "it does not sit on the guide's buttons",
              c["b"] <= a["y"] + 0.5 or c["y"] >= a["b"] - 0.5, (c, a))
        check(tag + "it lines up with the step text on the orb's side",
              abs((c["r"] if lang == "he" else c["x"]) - (after["say"]["r"] if lang == "he"
                  else after["say"]["x"])) < 1.5, (c, after["say"]))
        if lang == "en":
            p.open_status = (200, {"ok": True, "opened": "accessibility", "dry": True})
            p.page.click("#fixchip")
            p.page.wait_for_function(
                f"document.getElementById('fixchipw').textContent === {json.dumps(COPY['en']['opened'])}",
                timeout=4000)
            check("pressing it during a guide opens that pane", p.opens == [{"pane": "accessibility"}],
                  p.opens)
            # The guide ends: the strip is a plain answer again and the button goes back
            # to the action slot beside it.
            p.guide_state = {"active": False, "id": "g-fix", "status": "stopped", "line": ""}
            p.ev("hideGuide()")
            p.page.wait_for_function("!document.documentElement.classList.contains('b-guide')",
                                     timeout=4000)
            _fit(p)
            plain = p.ev(BAR_GUIDE_JS)
            check("with the guide gone it is back in the action slot",
                  plain["chipShown"] and plain["area"].startswith("act"), plain)
        check(tag + "nothing asked macOS for a permission", not p.asks, p.asks)
        check(tag + "no page errors", not p.errors, p.errors)
        p.close()


def test_languages(browser):
    for lang in ("he", "ar", "ru", "en"):
        p = Page(browser, "/?panel=1", lang=lang)
        # The server's speech language wins over the browser's at load (English here),
        # so the language is chosen the way the pill does it.
        p.ev(f"applyLanguage({json.dumps(lang)}, {{remember:false}})")
        p.ev("panelReply('x', 'accessibility')")
        c = p.chip()
        dir_ = p.ev("document.documentElement.dir")
        box = p.ev("""(() => { const r = document.getElementById('fixchip').getBoundingClientRect();
                       return {l: r.left, r: r.right, w: innerWidth}; })()""")
        check(f"{lang}: the button reads {COPY[lang]['open']!r}, inside the window ({dir_})",
              c["label"] == COPY[lang]["open"] and box["l"] >= 0 and box["r"] <= box["w"]
              and dir_ == ("rtl" if lang in ("he", "ar") else "ltr"), (c, box, dir_))
        p.page.click("#fixchip")
        p.page.wait_for_function(f"document.getElementById('fixchipw').textContent === {json.dumps(COPY[lang]['opened'])}",
                                 timeout=4000)
        check(f"{lang}: after opening it says {COPY[lang]['opened']!r}", True)
        check(f"{lang}: no page errors", not p.errors, p.errors)
        p.close()
    # Switching language repaints a showing button.
    p = Page(browser, "/?panel=1", lang="en")
    p.ev("panelReply('x', 'microphone'); applyLanguage('he', {remember:false})")
    check("a language switch repaints the showing button", p.chip()["label"] == COPY["he"]["open"], p.chip())
    p.close()


# ---------------------------------------------------------------- P1-10 and P2-4 in Settings
def test_settings_readonly_rows(browser):
    p = Page(browser, "/?panel=1")
    p.ev("openSettings()")
    p.page.wait_for_timeout(700)
    check("no Screen Recording or Automation rows while /api/permissions does not report them",
          p.ev("document.getElementById('spros').hidden"))
    p.ev("closeSettings()")
    p.perms_extra = {"screen_recording": "denied",
                     "automation": {"Messages": "granted", "Calendar": "denied", "Music": "not_asked"}}
    p.ev("openSettings()")
    p.page.wait_for_selector("#spros:not([hidden])", timeout=4000)
    scr = p.ev("[document.getElementById('spScreenN').textContent, document.getElementById('spScreenS').textContent]")
    check("Screen Recording is listed with its state", scr == [COPY["en"]["screen"], COPY["en"]["off"]], scr)
    chips = p.ev("[...document.querySelectorAll('#spAutoL .spauto')].map(c => [c.textContent, c.dataset.s])")
    check("each Automation target is listed with its state", chips == [
        ["Messages: On", "granted"], ["Calendar: Off", "denied"], ["Music: Not yet", "not_asked"]], chips)
    check("the read-only rows have no button",
          p.ev("document.querySelectorAll('#spros button').length") == 0)
    check("the three askable rows are still exactly three",
          p.ev("document.querySelectorAll('.sperm').length") == 3)
    check("opening Settings asked macOS for nothing", not p.asks, p.asks)
    p.ev("applyLanguage('ar', {remember:false})")
    check("the rows follow the language", p.ev("document.getElementById('spScreenN').textContent") == "تسجيل الشاشة")
    check("no page errors", not p.errors, p.errors)
    p.close()


def test_settings_login_approval(browser):
    p = Page(browser, "/?panel=1")
    p.ev("openSettings()")
    p.page.wait_for_timeout(600)
    check("an older server without open_at_login_status shows no approval line",
          p.ev("document.getElementById('hLoginApprove').hidden"))
    p.ev("closeSettings()")
    p.config_extra = {"open_at_login_status": "requires_approval", "open_at_login": False}
    p.ev("openSettings()")
    p.page.wait_for_selector("#hLoginApprove:not([hidden])", timeout=4000)
    check("requires_approval: one line naming Login Items",
          p.page.inner_text("#hLoginApproveT") == COPY["en"]["approve"], p.page.inner_text("#hLoginApproveT"))
    check("and its own button", p.page.inner_text("#loginOpen") == COPY["en"]["loginOpen"])
    p.open_status = (200, {"ok": True, "opened": "login_items", "dry": True})
    p.page.click("#loginOpen")
    p.page.wait_for_function(f"document.getElementById('loginOpen').textContent === {json.dumps(COPY['en']['opened'])}",
                             timeout=4000)
    check("the button opens the login_items pane", p.opens == [{"pane": "login_items"}], p.opens)
    p.ev("closeSettings()")
    p.config_extra = {"open_at_login_status": "enabled", "open_at_login": True}
    p.ev("openSettings()")
    p.page.wait_for_timeout(600)
    check("enabled: the line goes", p.ev("document.getElementById('hLoginApprove').hidden"))
    p.ev("closeSettings()")
    p.config_extra = {"open_at_login_status": "not_registered", "open_at_login": False}
    p.ev("openSettings()")
    p.page.wait_for_timeout(600)
    check("not_registered (plain off) shows no approval line",
          p.ev("document.getElementById('hLoginApprove').hidden"))
    p.ev("closeSettings()")
    p.config_extra = {"open_at_login_status": "requires_approval"}
    p.ev("openSettings()")
    p.page.wait_for_selector("#hLoginApprove:not([hidden])", timeout=4000)
    p.ev("applyLanguage('he', {remember:false})")
    he = p.page.inner_text("#hLoginApproveT")
    check("the line follows the language (he)", "MicMic" in he and "פריטי ההתחברות" in he, he)
    check("nothing asked macOS for a permission", not p.asks, p.asks)
    check("no page errors", not p.errors, p.errors)
    p.close()


def main():
    test_static()
    with Server(), sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)    # bundled Chromium, never channel="chrome"
        try:
            for t in (test_reply_button, test_panel_hooks, test_bar_hooks, test_bar_guide_fix,
                      test_languages,
                      test_settings_readonly_rows, test_settings_login_approval):
                print(f"\n-- {t.__name__}")
                try:
                    t(browser)
                except Exception as e:  # noqa: BLE001
                    check(f"{t.__name__} ran to the end", False, repr(e)[:400])
        finally:
            browser.close()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed (no model calls)")
    for f in FAILED:
        print("  FAILED:", f)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
