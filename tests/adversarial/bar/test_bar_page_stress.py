#!/usr/bin/env python3
"""Adversarial stress cases for the ?bar=1 page, headless, the way native/bar.py
drives it (see tests/bar/test_bar_page.py, which this file borrows its server/harness
pattern from but does not import, to keep the two suites independently runnable).

    .venv/bin/python3 \\
        tests/adversarial/bar/test_bar_page_stress.py

Playwright's own bundled Chromium, headless, never channel="chrome". Served by this
worktree's own server on port 8802 (started here if nothing is listening there yet),
with a throwaway MICMIC_STATE_DIR, and never port 8799.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
PORT = 8802
BASE = f"http://127.0.0.1:{PORT}"
assert PORT != 8799, "never the live server"
SHOTS = Path(os.environ.get("BAR_SHOTS", tempfile.mkdtemp(prefix="adv-bar-page-shots-")))
SHOTS.mkdir(parents=True, exist_ok=True)

PASSED, FAILED = [], []
# Jev and Gemini cost money: the server here is a proxy client pointed at a closed local
# port, so it never reads the developer's keys from .env.local, never registers a device
# with the cloud, and any model call fails at once, for free. Nothing here needs one.
OFFLINE = {"MICMIC_MODE": "proxy", "MICMIC_PROXY_URL": "http://127.0.0.1:9",
           "MICMIC_PROXY_TOKEN": "offline-test"}
SPEECH = {"en": "en-US", "he": "he-IL", "ar": "ar-SA", "ru": "ru-RU"}


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


INIT = """
window.__msgs = [];
window.webkit = {messageHandlers: {
  micmicbar: {postMessage: m => window.__msgs.push(String(m))},
  micmic:    {postMessage: m => window.__msgs.push('micmic:' + m)}}};
"""


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_server():
    if _port_open(PORT):
        page = __import__("urllib.request", fromlist=["urlopen"]).urlopen(BASE + "/?bar=1", timeout=5).read().decode()
        assert "window.barResult" in page, "something else is on 8802"
        return None
    env = dict(os.environ, MICMIC_PORT=str(PORT),
               MICMIC_STATE_DIR=tempfile.mkdtemp(prefix="adv-bar-page-state-"), **OFFLINE)
    env.pop("MICMIC_ALLOW_SEND", None)
    env.pop("MICMIC_ALLOW_CALL", None)
    proc = subprocess.Popen([str(ROOT / ".venv/bin/python3"), "-m", "savta.server"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if _port_open(PORT):
            return proc
        time.sleep(0.1)
    proc.kill()
    raise SystemExit("server on 8802 did not start")


def _guard(route):
    req = route.request
    if req.method == "POST" and not req.url.split("?")[0].endswith("/api/undo"):
        route.fulfill(status=200, content_type="application/json", body="{}")
    else:
        route.continue_()


def open_bar(browser, lang="en", width=640, extra_init=""):
    ctx = browser.new_context(viewport={"width": width, "height": 64}, device_scale_factor=2,
                              reduced_motion="reduce")
    ctx.route("**/api/**", _guard)
    # The page's language is the server's speech language (the browser's saved one only
    # covers the moment before /api/config answers), so that is where the test picks it.
    def config(route):
        if route.request.method != "GET":
            return _guard(route)
        body = route.fetch().json()
        body["language_hint"] = SPEECH[lang]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    ctx.route("**/api/config", config)
    ctx.add_init_script(INIT + f"try{{localStorage.setItem('micmic.lang', {json.dumps(lang)})}}catch(_){{}}"
                        + extra_init)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/?bar=1")
    page.wait_for_function("document.documentElement.lang === %s" % json.dumps(lang))
    page.evaluate("document.fonts.ready")
    return ctx, page, errors


def fit(page):
    h = page.evaluate("Math.ceil(document.querySelector('.stage').getBoundingClientRect().height)")
    page.set_viewport_size({"width": page.viewport_size["width"], "height": h})
    page.wait_for_timeout(60)
    return h


def rect(page, sel):
    return page.evaluate("(s => { const r = document.querySelector(s).getBoundingClientRect();"
                         " return {x:r.x, y:r.y, w:r.width, h:r.height, r:r.right, b:r.bottom}; })", sel)


# ---------------------------------------------------------------- 1. result vs. streaming words
def test_result_arrives_mid_stream():
    """DESIGN OBSERVATION (not a page-level failure once framed correctly). The
    recognizer can keep delivering partial transcripts for the tail of an utterance
    after the listener has already dispatched and shown a result. At the PAGE level,
    window.barHeard() has exactly one rule for this: "if a reply is showing, new
    heard text starts the next turn" (savta/web/index.html:1860, clearTurn()) -- it
    does not, and cannot, tell a genuinely late trailing partial of the command she
    already got an answer to apart from a real new command. Calling barHeard() even
    once after barResult() WIPES the shown result immediately; there is no grace
    window, debounce, or "is this really new" check at the page or bar.py layer.
    The only place that actually prevents this in production is a single boolean
    check in native/listener.py's Speech-result callback: `armed = self.armed_until
    > time.time()` .. `if armed: self.view_heard(text)` (listener.py:1304-1308) --
    armed_until is zeroed at the moment of dispatch (listener.py:1404), so trailing
    partials from the same session are simply never forwarded. That is the ONE
    guard; there is no redundancy at the bar.py or page layer, so a future change
    to listener.py's armed-window bookkeeping (or a caller other than listener.py
    driving the same bar) could wipe an answer with nothing left to catch it.
    Demonstrated directly against the page, bypassing listener.py's gate on purpose."""
    ctx, page, errors = open_bar(browser_singleton[0])
    page.evaluate("""() => {
        barState('listening', '');
        const words = 'turn the volume down a little bit please'.split(' ');
        for(let i = 0; i < words.length; i++){
            barHeard(words.slice(0, i + 1).join(' '));
            if(i === 3){ barResult('Turned it down.', ''); }   // result lands MID-STREAM
        }
        // trailing partials the recognizer had already queued for the SAME utterance
        barHeard('turn the volume down a little bit please extra');
    }""")
    check("a trailing partial word for the SAME utterance, arriving right after the "
          "result, wipes the result at the page level with no guard of its own "
          "(protection lives solely in listener.py's `armed` check, not here)",
          page.inner_text("#reply") == "" and page.inner_text("#heard").endswith("extra"),
          (page.inner_text("#reply"), page.inner_text("#heard")))
    check("no page errors", not errors, errors)
    ctx.close()


def test_stale_acting_class_after_heard_only_turn_reset():
    """ADVERSARIAL FINDING: window.barResult() calls setState('acting'), which
    rewrites document.body.className (savta/web/index.html:827-829). window.barHeard()
    starting a new turn only calls clearTurn() (:1828-1833, :1860), which resets the
    heard/reply TEXT and the Undo button, but never calls setState() or barState() --
    so document.body.className is left at 'acting' from the previous turn while she
    has already started speaking the next command. The orb keeps whatever visual
    treatment .acting carries (see the CSS around 'body.acting'), even though the
    words on screen are the ones she is saying right now. In the real app this is
    normally masked because listener.py's wake-word/push-to-talk path calls
    view_attention() (-> bar.set_state('listening')) before it calls view_heard()
    again -- but nothing in bar.py's own public contract (set_heard() can be called
    without a preceding set_state()) enforces that, and an armed continuation inside
    an open cancel/wake-grace window is exactly a case where set_heard() can run
    without a fresh set_state() in between. Reproduced n=3."""
    reps = []
    for n in range(3):
        ctx, page, _ = open_bar(browser_singleton[0])
        page.evaluate("barState('listening',''); barHeard('mute'); barResult('Muted.', '')")
        before = page.evaluate("document.body.className")
        page.evaluate("t => barHeard(t)", f"and pause the music {n}")
        after = page.evaluate("document.body.className")
        reps.append((before, after))
        ctx.close()
    # Fixed in barHeard: new words after an answer put the orb back to listening.
    check("new words for the NEXT turn put the orb back to listening, 3 reps",
          all(b == "acting" and a == "listening" for b, a in reps), reps)


# ---------------------------------------------------------------- 2. rapid new turn
def test_new_turn_50ms_after_result():
    for n in range(3):
        ctx, page, errors = open_bar(browser_singleton[0])
        page.evaluate("barState('listening',''); barHeard('what time is it'); "
                      "barResult('It is nine.', '')")
        page.wait_for_timeout(50)
        page.evaluate("barState('listening','')")
        page.wait_for_timeout(10)
        page.evaluate("t => barHeard(t)", "and what about tomorrow")
        undo_hidden = page.evaluate("document.getElementById('undo').hidden")
        check(f"[{n}] a fresh barState('listening') 50ms after a result properly "
              f"clears everything (reply empty, no Undo, correct body class, new words shown)",
              page.inner_text("#reply") == "" and undo_hidden
              and page.evaluate("document.body.className") == "listening"
              and page.inner_text("#heard") == "and what about tomorrow",
              (page.inner_text("#reply"), undo_hidden, page.evaluate("document.body.className")))
        check(f"[{n}] no page errors", not errors, errors)
        ctx.close()


# ---------------------------------------------------------------- 3. very long Arabic, clamp
def test_4_line_clamp_3000_char_arabic():
    long_ar = ("انتهيت. " + "أرسلت إلى دانا كل فواتير الربع الأخير بما في ذلك إيصالات الموردين "
               "وأضفت ملاحظة أن دفعة شركة الكهرباء ما زالت مفتوحة ويجب التحقق منها قبل نهاية الأسبوع. ")
    long_ar = (long_ar * ((3000 // len(long_ar)) + 2))[:3000]
    check("test string is really ~3000 chars", 2990 <= len(long_ar) <= 3010, len(long_ar))
    ctx, page, errors = open_bar(browser_singleton[0], lang="ar")
    page.evaluate("barState('listening','')")
    page.evaluate("t => barHeard(t.slice(0,140))", long_ar)
    page.evaluate("t => barResult(t, '')", long_ar)
    h = fit(page)
    lh = page.evaluate("parseFloat(getComputedStyle(document.getElementById('reply')).lineHeight)")
    rr = rect(page, "#reply")
    check("3000-char Arabic clamps to <= 4 lines", rr["h"] <= 4 * lh + 1, (rr["h"], lh))
    check("strip stays under native's 240px cap even at this extreme length", h <= 240, h)
    check("dir is rtl for Arabic", page.evaluate("document.documentElement.dir") == "rtl")
    check("nothing spills sideways", page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
    check("no page errors", not errors, errors)
    page.screenshot(path=str(SHOTS / "ar-3000char.png"))
    ctx.close()


# ---------------------------------------------------------------- 4. markup / script injection
def test_result_and_heard_with_markup_and_script_variants():
    payloads = [
        '<img src=x onerror="window.__pwned=(window.__pwned||0)+1">',
        '<script>window.__pwned=(window.__pwned||0)+1</script>',
        '"><svg onload="window.__pwned=(window.__pwned||0)+1">',
        'javascript:window.__pwned=(window.__pwned||0)+1',
        '‮ evil reversed ‭',                       # RTL/LTR override chars
        '${alert(1)}',                                        # template-literal-looking text
        '{{constructor.constructor("window.__pwned=1")()}}',  # template-engine-looking text
    ]
    for i, evil in enumerate(payloads):
        ctx, page, errors = open_bar(browser_singleton[0])
        page.evaluate("() => { window.__pwned = 0; }")
        page.evaluate("t => { barState('listening',''); barHeard(t); barResult(t, t); }", evil)
        page.wait_for_timeout(80)
        pwned = page.evaluate("window.__pwned")
        reply_text = page.inner_text("#reply")
        heard_text = page.inner_text("#heard")
        undo_text = page.inner_text("#undow") if page.evaluate("!document.getElementById('undo').hidden") else None
        check(f"[{i}] payload never executes ({evil[:40]!r})", pwned == 0, pwned)
        check(f"[{i}] #reply shows it as literal text", reply_text == evil, (reply_text, evil))
        check(f"[{i}] #heard shows it as literal text", heard_text == evil, (heard_text, evil))
        check(f"[{i}] Undo label (also caller-controlled) never executes and is literal text",
              undo_text is None or undo_text == evil or undo_text != "", (undo_text, evil))
        check(f"[{i}] no page errors", not errors, errors)
        ctx.close()


browser_singleton = [None]


def main():
    proc = ensure_server()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser_singleton[0] = browser
            try:
                for t in (test_result_arrives_mid_stream,
                          test_stale_acting_class_after_heard_only_turn_reset,
                          test_new_turn_50ms_after_result,
                          test_4_line_clamp_3000_char_arabic,
                          test_result_and_heard_with_markup_and_script_variants):
                    print(f"\n-- {t.__name__}")
                    try:
                        t()
                    except Exception as e:  # noqa: BLE001
                        check(f"{t.__name__} ran to the end", False, repr(e)[:400])
            finally:
                browser.close()
        # The server's own count of paid Jev calls and their cost: both zero.
        import urllib.request as _u
        try:
            h = json.loads(_u.urlopen(BASE + "/api/health", timeout=5).read())
            check("the server reports no paid model calls (Jev calls and cost both zero)",
                  not h.get("calls") and not h.get("cost_usd"), h)
        except Exception as e:  # noqa: BLE001
            check("the server reports no paid model calls", False, repr(e))
    finally:
        if proc is not None:
            proc.terminate()
    print(f"\nscreenshots: {SHOTS}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
