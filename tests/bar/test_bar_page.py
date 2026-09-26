#!/usr/bin/env python3
"""The ?bar=1 page, driven headless the way native/bar.py drives it.

    .venv/bin/python3 tests/bar/test_bar_page.py

Playwright's own bundled Chromium, headless, never channel="chrome": that one is her
real browser and has closed her windows before. The page is served by this worktree's
own server on port 8802 (started here if nothing is listening there), with a throwaway
MICMIC_STATE_DIR, and never from 8799, which is the one she is using.

What it proves: every state; hundreds of live-word updates a second; very long Hebrew,
Arabic and English answers clamped without overflow or overlap; RTL; all four
languages; light and dark; Undo against success, undone:false, a network failure, a
500, bad JSON and the real 404 (the endpoint does not exist in this tree yet); Esc;
the height messages; that the bar never opens a microphone; and that the bar's styling
and hooks leave the plain page and ?panel=1 alone (see test_other_modes_unchanged).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PORT = 8802
BASE = f"http://127.0.0.1:{PORT}"
assert PORT != 8799, "never the live server"
SHOTS = Path(os.environ.get("BAR_SHOTS", tempfile.mkdtemp(prefix="bar-page-shots-")))
SHOTS.mkdir(parents=True, exist_ok=True)

PASSED, FAILED = [], []
# Jev and Gemini cost money: the server here is a proxy client pointed at a closed local
# port, so it never reads the developer's keys from .env.local, never registers a device
# with the cloud, and any model call fails at once, for free. Nothing here needs one.
OFFLINE = {"MICMIC_MODE": "proxy", "MICMIC_PROXY_URL": "http://127.0.0.1:9",
           "MICMIC_PROXY_TOKEN": "offline-test"}


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


# The page's messages to native land here instead of in WKWebView. The microphone
# APIs are replaced with counters: in bar mode both must stay at zero, and the plain
# page is the positive control that proves the counters work.
INIT = """
window.__msgs = [];
window.webkit = {messageHandlers: {
  micmicbar: {postMessage: m => window.__msgs.push(String(m))},
  micmic:    {postMessage: m => window.__msgs.push('micmic:' + m)}}};
window.__mic = {sr: 0, gum: 0};
class FakeSR { constructor(){ window.__mic.sr++; } start(){} stop(){} abort(){} }
window.SpeechRecognition = FakeSR; window.webkitSpeechRecognition = FakeSR;
try{
  Object.defineProperty(navigator, 'mediaDevices', {configurable: true, value: {
    getUserMedia: () => { window.__mic.gum++; return new Promise(()=>{}); }}});
}catch(_){}
"""

COPY = {  # must match the COPY table in index.html
    "he": {"undo": "ביטול הפעולה", "undoFailed": "לא הצלחתי לבטל את זה",
           "undoNone": "אי אפשר לבטל את זה כרגע",
           "undoNet": "לא הצלחתי להתחבר למחשב, שום דבר לא בוטל", "undone": "בוטל",
           "ready": "מקשיבה", "think": "רגע"},
    "en": {"undo": "Undo", "undoFailed": "I couldn't undo that",
           "undoNone": "Undo isn't available right now",
           "undoNet": "I couldn't reach the computer, so nothing was undone",
           "undone": "Undone", "ready": "listening", "think": "one moment"},
    "ar": {"undo": "تراجع", "undoFailed": "لم أتمكن من التراجع عن ذلك",
           "undoNone": "التراجع غير متاح الآن",
           "undoNet": "لم أتمكن من الوصول إلى الكمبيوتر، لم يتم التراجع عن أي شيء",
           "undone": "تم التراجع", "ready": "أستمع", "think": "لحظة"},
    "ru": {"undo": "Отменить", "undoFailed": "Не получилось отменить",
           "undoNone": "Отмена сейчас недоступна",
           "undoNet": "Не удалось связаться с компьютером, ничего не отменено",
           "undone": "Отменено", "ready": "слушаю", "think": "секунду"},
}

LONG = {
    "he": ("סיימתי. " + "שלחתי לדנה את כל החשבוניות של הרבעון האחרון, כולל הקבלות מהספקים, "
           "והוספתי הערה שהתשלום לספק החשמל עדיין פתוח ושכדאי לבדוק אותו עד סוף השבוע. " * 4),
    "ar": ("انتهيت. " + "أرسلت إلى دانا كل فواتير الربع الأخير، بما في ذلك إيصالات الموردين، "
           "وأضفت ملاحظة أن دفعة شركة الكهرباء ما زالت مفتوحة ويجب التحقق منها قبل نهاية الأسبوع. " * 4),
    "en": ("Done. " + "I sent Dana every invoice from the last quarter, including the supplier "
           "receipts, and added a note that the electricity payment is still open and worth "
           "checking before the end of the week. " * 4),
    "ru": ("Готово. " + "Я отправила Дане все счета за последний квартал, включая квитанции "
           "поставщиков, и добавила заметку, что оплата за электричество всё ещё открыта. " * 4),
}


# ---------------------------------------------------------------- server
def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_server():
    """This worktree's server on 8802, private state, sends and calls disarmed."""
    if _port_open(PORT):
        page = urllib.request.urlopen(BASE + "/?bar=1", timeout=5).read().decode()
        assert "window.barResult" in page, "something else is on 8802"
        return None
    env = dict(os.environ, MICMIC_PORT=str(PORT),
               MICMIC_STATE_DIR=tempfile.mkdtemp(prefix="bar-page-state-"), **OFFLINE)
    env.pop("MICMIC_ALLOW_SEND", None)
    env.pop("MICMIC_ALLOW_CALL", None)
    proc = subprocess.Popen([str(ROOT / ".venv/bin/python3"), "-m", "savta.server"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(150):
        if _port_open(PORT):
            return proc
        time.sleep(0.1)
    proc.kill()
    raise SystemExit("server on 8802 did not start")


def no_model_calls():
    """The server's own count of Jev calls and their cost, which must both be zero."""
    import urllib.request as _u
    try:
        h = json.loads(_u.urlopen(BASE + "/api/health", timeout=5).read())
    except Exception as e:  # noqa: BLE001
        return check("the server reports no paid model calls", False, repr(e))
    check("the server reports no paid model calls (Jev calls and cost both zero)",
          not h.get("calls") and not h.get("cost_usd"), h)


# ---------------------------------------------------------------- helpers
def _guard(route):
    """No POST from these pages reaches the server except /api/undo (a 404 in this
    tree). /api/duck turns her real Mac's volume down; /api/utterance runs the router;
    /api/settings rewrites the config. None of that belongs in a page test."""
    req = route.request
    if req.method == "POST" and not req.url.split("?")[0].endswith("/api/undo"):
        route.fulfill(status=200, content_type="application/json", body="{}")
    else:
        route.continue_()


SPEECH = {"en": "en-US", "he": "he-IL", "ar": "ar-SA", "ru": "ru-RU"}


def new_ctx(browser, lang=None, **kw):
    """lang: the server's speech language for this context. The page takes its language
    from /api/config (the browser's saved one only covers the moment before it answers),
    so that is where a test picks it."""
    ctx = browser.new_context(**kw)
    ctx.route("**/api/**", _guard)
    if lang:
        def config(route):
            if route.request.method != "GET":
                return _guard(route)
            body = route.fetch().json()
            body["language_hint"] = SPEECH[lang]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        ctx.route("**/api/config", config)
    return ctx


def open_bar(browser, lang="en", scheme="light", width=640, extra_init=""):
    ctx = new_ctx(browser, lang=lang, viewport={"width": width, "height": 64}, color_scheme=scheme,
                              device_scale_factor=2, reduced_motion="reduce")
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
    """Do what native does with a height message: make the viewport that tall."""
    h = page.evaluate("Math.ceil(document.querySelector('.stage').getBoundingClientRect().height)")
    page.set_viewport_size({"width": page.viewport_size["width"], "height": h})
    page.wait_for_timeout(60)
    return h


def rect(page, sel):
    return page.evaluate("(s => { const r = document.querySelector(s).getBoundingClientRect();"
                         " return {x:r.x, y:r.y, w:r.width, h:r.height, r:r.right, b:r.bottom}; })",
                         sel)


def visible(page, sel):
    return page.evaluate("s => { const e = document.querySelector(s); const cs = getComputedStyle(e);"
                         " return cs.display !== 'none' && cs.visibility !== 'hidden'"
                         " && e.getBoundingClientRect().height > 0; }", sel)


def msgs(page):
    return page.evaluate("window.__msgs.slice()")


def heights(page):
    return [int(m.split(":", 1)[1]) for m in msgs(page) if m.startswith("height:")]


def overlap(a, b):
    return not (a["r"] <= b["x"] or b["r"] <= a["x"] or a["b"] <= b["y"] or b["b"] <= a["y"])


# ---------------------------------------------------------------- tests
def test_states(browser):
    for lang in ("he", "en", "ar", "ru"):
        ctx, page, errors = open_bar(browser, lang)
        c = COPY[lang]
        page.evaluate("barState('listening', '')")
        check(f"[{lang}] listening: body class", page.evaluate("document.body.className") == "listening")
        check(f"[{lang}] listening: placeholder in her language",
              page.inner_text("#state") == c["ready"], page.inner_text("#state"))
        check(f"[{lang}] listening: no words, no answer, no Undo",
              not visible(page, "#heard") and not visible(page, "#reply") and not visible(page, "#undo"))
        page.evaluate("barHeard('turn the volume down a bit')")
        check(f"[{lang}] heard replaces the placeholder",
              visible(page, "#heard") and not visible(page, "#state"))
        page.evaluate("barState('thinking', '')")
        page.wait_for_timeout(80)   # let the (reduced-motion, near-instant) colour transition land
        check(f"[{lang}] thinking: the orb's thinking ring is on",
              page.evaluate("document.body.classList.contains('thinking')")
              and page.evaluate("getComputedStyle(document.getElementById('ring')).borderTopColor")
              == "rgba(0, 0, 0, 0)",
              page.evaluate("[document.body.className, getComputedStyle(document.getElementById('ring')).borderTopColor]"))
        check(f"[{lang}] thinking keeps her words", page.inner_text("#heard") == "turn the volume down a bit")
        page.evaluate("barState('acting', '')")
        check(f"[{lang}] acting class", page.evaluate("document.body.classList.contains('acting')"))
        page.evaluate("barState('error', '')")
        check(f"[{lang}] error without text says the page's own network line",
              page.evaluate("document.documentElement.classList.contains('b-err')")
              and visible(page, "#reply") and page.inner_text("#reply").strip() != "")
        err_color = page.evaluate("getComputedStyle(document.getElementById('reply')).color")
        check(f"[{lang}] error reads as broken (red)", err_color == "rgb(181, 84, 74)", err_color)
        page.evaluate("barState('error', 'The server is not answering')")
        check(f"[{lang}] error with native's text shows that text",
              page.inner_text("#reply") == "The server is not answering")
        page.evaluate("barState('idle', '')")
        check(f"[{lang}] idle clears everything",
              page.inner_text("#heard") == "" and page.inner_text("#reply") == ""
              and page.inner_text("#state") == "" and not visible(page, "#undo"))
        page.evaluate("barState('listening', 'מקשיבה לך')")
        check(f"[{lang}] native's own state text wins over the copy",
              page.inner_text("#state") == "מקשיבה לך")
        page.evaluate("barState('bogus', '')")
        check(f"[{lang}] unknown state falls back to idle",
              page.evaluate("document.body.className") == "idle")
        check(f"[{lang}] no page errors", not errors, errors)
        ctx.close()


def test_rapid_heard(browser):
    ctx, page, errors = open_bar(browser, "he")
    page.evaluate("barState('listening', '')")
    words = "תזכירי לי מחר בשמונה בבוקר להתקשר לרופא ולשאול על התוצאות של הבדיקות".split()
    # 1000 synchronous updates, the way a recogniser's partials pile up.
    ms = page.evaluate("""words => { const t0 = performance.now();
        for(let i = 0; i < 1000; i++) barHeard(words.slice(0, 1 + (i % words.length)).join(' ') + ' ' + i);
        return performance.now() - t0; }""", words)
    check("1000 synchronous barHeard calls take under 250ms", ms < 250, f"{ms:.1f}ms")
    check("the last one is what is on screen", page.inner_text("#heard").endswith(" 999"))
    # And spread over time: 300 a second for two seconds, while the page lays out.
    page.evaluate("""words => new Promise(done => { let i = 0;
        const id = setInterval(() => { for(let k = 0; k < 3; k++){ barHeard(words.slice(0, 1 + (i % words.length)).join(' ') + ' #' + i); i++; }
          if(i >= 600){ clearInterval(id); done(); } }, 10); })""", words)
    page.wait_for_timeout(100)
    check("600 timed updates: last one shown", page.inner_text("#heard").endswith(" #599"))
    hs = heights(page)
    dupes = sum(1 for a, b in zip(hs, hs[1:]) if a == b)
    check("height messages are deduplicated", dupes == 0, hs[:20])
    # The test words alternate between one and two lines, so the strip really does
    # change height; what must not happen is a message per word or a third height.
    check("height only ever takes the one- or two-line value", len(set(hs)) <= 3, sorted(set(hs)))
    long_heard = " ".join(words * 6)
    page.evaluate("t => barHeard(t)", long_heard)
    fit(page)
    hr = rect(page, "#heard")
    lh = page.evaluate("parseFloat(getComputedStyle(document.getElementById('heard')).lineHeight)")
    check("a long sentence stays two lines tall", hr["h"] <= 2 * lh + 1, (hr["h"], lh))
    # The newest words stay visible: the last word's box is inside the heard box.
    last_visible = page.evaluate("""() => { const h = document.getElementById('heard');
        const r = document.createRange(); const n = h.firstChild; r.setStart(n, n.length - 3); r.setEnd(n, n.length);
        const a = r.getBoundingClientRect(), b = h.getBoundingClientRect();
        return a.top >= b.top - 1 && a.bottom <= b.bottom + 1; }""")
    check("while she keeps talking, the NEWEST words are the ones in view", last_visible)
    page.wait_for_timeout(60)
    check("the overflow is marked with a fade at the top",
          page.evaluate("document.getElementById('heard').classList.contains('over')"))
    page.screenshot(path=str(SHOTS / "he-heard-long.png"))
    page.evaluate("barHeard('תזכירי לי מחר')")
    page.wait_for_timeout(60)
    check("a sentence that fits has no fade",
          not page.evaluate("document.getElementById('heard').classList.contains('over')"))
    check("no page errors under load", not errors, errors)
    ctx.close()


def test_long_results(browser):
    for lang in ("he", "ar", "en", "ru"):
        for scheme in ("light", "dark"):
            ctx, page, errors = open_bar(browser, lang, scheme)
            page.evaluate("barState('listening', '')")
            page.evaluate("t => barHeard(t)", LONG[lang][:140])
            page.evaluate("t => barResult(t, '')", LONG[lang])
            h = fit(page)
            rr, ur, orb = rect(page, "#reply"), rect(page, "#undo"), rect(page, "#orb")
            hr = rect(page, "#heard")
            lh = page.evaluate("parseFloat(getComputedStyle(document.getElementById('reply')).lineHeight)")
            tag = f"[{lang}/{scheme}]"
            check(f"{tag} long answer clamps to 4 lines", rr["h"] <= 4 * lh + 1, (rr["h"], lh))
            check(f"{tag} strip stays under native's 240px cap", h <= 240, h)
            check(f"{tag} answer, heard, Undo and orb do not overlap",
                  not overlap(rr, ur) and not overlap(rr, orb) and not overlap(hr, ur)
                  and not overlap(hr, orb), (rr, ur, orb, hr))
            check(f"{tag} nothing spills sideways",
                  page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            check(f"{tag} everything inside the strip",
                  min(rr["x"], ur["x"], orb["x"]) >= 0 and max(rr["r"], ur["r"], orb["r"]) <= 640
                  and rr["b"] <= h and ur["b"] <= h)
            rtl = lang in ("he", "ar")
            check(f"{tag} dir is {'rtl' if rtl else 'ltr'}",
                  page.evaluate("document.documentElement.dir") == ("rtl" if rtl else "ltr"))
            if rtl:
                check(f"{tag} RTL: orb on the right, Undo on the left",
                      orb["x"] > rr["r"] and ur["r"] < rr["x"] + 1)
            else:
                check(f"{tag} LTR: orb on the left, Undo on the right",
                      orb["r"] < rr["x"] and ur["x"] > rr["r"] - 1)
            check(f"{tag} Undo label in her language", page.inner_text("#undow") == COPY[lang]["undo"],
                  page.inner_text("#undow"))
            page.screenshot(path=str(SHOTS / f"{lang}-{scheme}-long.png"))
            check(f"{tag} no page errors", not errors, errors)
            ctx.close()
    # One unbroken token (a pasted URL) must wrap, not push the Undo button off the strip.
    ctx, page, errors = open_bar(browser, "en")
    page.evaluate("barResult('https://example.com/' + 'a'.repeat(600), 'Close the tab')")
    fit(page)
    ur, rr = rect(page, "#undo"), rect(page, "#reply")
    check("an unbroken 600-character URL wraps and clamps", ur["r"] <= 640 and not overlap(rr, ur)
          and page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
    check("a caller's own Undo label is used", page.inner_text("#undow") == "Close the tab")
    page.evaluate("barResult('Opened it.', 'Put everything back exactly the way it was before, please')")
    fit(page)
    check("a very long Undo label is capped, not stretched", rect(page, "#undo")["w"] <= 191,
          rect(page, "#undo")["w"])
    ctx.close()


def test_mixed_direction(browser):
    ctx, page, _ = open_bar(browser, "he")
    page.evaluate("barState('listening', ''); barHeard('open youtube'); barResult('Opened YouTube.', null)")
    fit(page)
    rr, orb = rect(page, "#reply"), rect(page, "#orb")
    # The English line sits against the orb (UI start), its full stop at the END.
    right_edge = page.evaluate("""() => { const e = document.getElementById('reply');
        const r = document.createRange(); r.selectNodeContents(e); return r.getBoundingClientRect().right; }""")
    check("[he UI, English answer] aligned to the orb's side", orb["x"] - right_edge < 20,
          (orb["x"], right_edge))
    dot = page.evaluate("""() => { const e = document.getElementById('reply'); const n = e.firstChild;
        const r = document.createRange(); r.setStart(n, n.length - 1); r.setEnd(n, n.length);
        const w = document.createRange(); w.setStart(n, 0); w.setEnd(n, 1);
        return [r.getBoundingClientRect().left, w.getBoundingClientRect().left]; }""")
    check("[he UI, English answer] reads left to right (full stop after the text)",
          dot[0] > dot[1], dot)
    check("result with undo_label null shows no Undo", not visible(page, "#undo"))
    page.screenshot(path=str(SHOTS / "he-mixed.png"))
    ctx.close()
    ctx, page, _ = open_bar(browser, "en")
    page.evaluate("barState('listening',''); barHeard('תשלחי לדנה שאני מאחרת'); barResult('שלחתי לדנה.', '')")
    fit(page)
    dot = page.evaluate("""() => { const e = document.getElementById('reply'); const n = e.firstChild;
        const r = document.createRange(); r.setStart(n, n.length - 1); r.setEnd(n, n.length);
        const w = document.createRange(); w.setStart(n, 0); w.setEnd(n, 1);
        return [r.getBoundingClientRect().left, w.getBoundingClientRect().left]; }""")
    check("[en UI, Hebrew answer] reads right to left (full stop on the left)", dot[0] < dot[1], dot)
    page.screenshot(path=str(SHOTS / "en-mixed.png"))
    ctx.close()


def test_escaping(browser):
    ctx, page, errors = open_bar(browser, "en")
    evil = '<img src=x onerror="window.__pwned=1"></p><script>window.__pwned=2</script>'
    page.evaluate("t => { barHeard(t); barResult(t, t); }", evil)
    page.wait_for_timeout(100)
    check("markup in her words or the answer is shown as text, never run",
          page.evaluate("!window.__pwned && !document.querySelector('.stage img:not(#thumb)')")
          and page.inner_text("#reply") == evil)
    ctx.close()


def test_turns(browser):
    ctx, page, _ = open_bar(browser, "en")
    page.evaluate("barState('listening',''); barHeard('mute'); barResult('Muted.', '')")
    page.evaluate("barHeard('mute')")
    check("the same words again do not wipe the answer", page.inner_text("#reply") == "Muted.")
    page.evaluate("barHeard('and pause the music')")
    check("new words after an answer start the next turn",
          page.inner_text("#reply") == "" and not visible(page, "#undo")
          and page.inner_text("#heard") == "and pause the music")
    page.evaluate("barResult('Paused.', '')")
    page.evaluate("barState('listening','')")
    check("listening clears the last turn",
          page.inner_text("#reply") == "" and page.inner_text("#heard") == "" and not visible(page, "#undo"))
    ctx.close()


def test_heights(browser):
    ctx, page, _ = open_bar(browser, "en")
    page.evaluate("barState('listening','')")
    page.wait_for_timeout(80)
    hs = heights(page)
    check("first height message is the slim 64px strip", hs and hs[-1] == 64, hs)
    page.evaluate("barHeard('what is on my calendar tomorrow')")
    page.evaluate("barResult('Two things: the dentist at nine, and lunch with Noa at one.', '')")
    page.wait_for_timeout(80)
    grown = heights(page)[-1]
    check("a result grows the strip and says so", grown > 64, heights(page))
    page.evaluate("barState('listening','')")
    page.wait_for_timeout(80)
    check("the next turn shrinks it back to 64", heights(page)[-1] == 64, heights(page))
    check("every message is well formed",
          all(m == "dismiss" or m == "touch" or (m.startswith("height:") and m[7:].isdigit())
              for m in msgs(page)), msgs(page))
    ctx.close()


def test_esc_and_keys(browser):
    ctx, page, _ = open_bar(browser, "en")
    page.evaluate("barState('listening','')")
    page.keyboard.press("Escape")
    check("Esc posts dismiss", "dismiss" in msgs(page))
    page.keyboard.press("Space")
    page.keyboard.press("Enter")
    page.mouse.click(300, 30)
    page.keyboard.press("d")
    check("Space, Enter, a click and 'd' do not reach the page's mic or dev handlers",
          not any(m.startswith("micmic:") for m in msgs(page))
          and page.evaluate("!document.getElementById('dev').classList.contains('show')")
          and page.evaluate("window.__mic.sr === 0 && window.__mic.gum === 0"))
    ctx.close()


def mock_undo(page, status=200, body=None, abort=False, delay=0):
    calls = []

    def handler(route):
        calls.append(route.request.method)
        if delay:
            time.sleep(delay)
        if abort:
            route.abort("connectionrefused")
        else:
            route.fulfill(status=status, content_type="application/json",
                          body=body if isinstance(body, str) else json.dumps(body))
    page.route("**/api/undo", handler)
    return calls


def undo_case(browser, name, lang, expect_text, expect_fail, **mock):
    ctx, page, errors = open_bar(browser, lang)
    calls = mock_undo(page, **mock) if mock else None
    page.evaluate("barState('listening',''); barHeard('mute the tv'); barResult('Muted.', '')")
    page.dblclick("#undo")            # an impatient double press is still one undo
    page.wait_for_function("document.getElementById('undo').hidden", timeout=5000)
    got = page.inner_text("#reply")
    check(f"undo {name}: shows {expect_text!r}", got == expect_text, got)
    check(f"undo {name}: button gone afterwards", not visible(page, "#undo"))
    failed = page.evaluate("document.documentElement.classList.contains('b-undofail')")
    check(f"undo {name}: {'marked NOT done' if expect_fail else 'not marked as a failure'}",
          failed == expect_fail)
    if calls is not None:
        check(f"undo {name}: exactly one POST even when clicked twice", calls == ["POST"], calls)
    check(f"undo {name}: tells native to hold the bar up", msgs(page).count("touch") >= 2, msgs(page))
    check(f"undo {name}: no page errors", not errors, errors)
    page.screenshot(path=str(SHOTS / f"undo-{name.replace(' ', '-')}.png"))
    ctx.close()


def test_undo(browser):
    undo_case(browser, "success", "en", "Unmuted the TV.", False,
              body={"undone": True, "did": "unmuted", "say": "Unmuted the TV."})
    undo_case(browser, "success without say", "he", COPY["he"]["undone"], False,
              body={"undone": True, "did": "unmuted", "say": ""})
    undo_case(browser, "undone false with say", "en", "That message already went out.", True,
              body={"undone": False, "did": "too_late", "say": "That message already went out."})
    undo_case(browser, "undone false without say", "ar", COPY["ar"]["undoFailed"], True,
              body={"undone": False, "did": "nothing", "say": ""})
    undo_case(browser, "undone as a string is not true", "en", COPY["en"]["undoFailed"], True,
              body={"undone": "true", "did": "x"})
    undo_case(browser, "network failure", "ru", COPY["ru"]["undoNet"], True, abort=True)
    undo_case(browser, "server 500", "en", COPY["en"]["undoFailed"], True, status=500, body={})
    undo_case(browser, "bad json", "he", COPY["he"]["undoFailed"], True, body="not json{")
    # A 404 (an older server without the endpoint) says Undo is not available.
    undo_case(browser, "404 from an older server", "en", COPY["en"]["undoNone"], True,
              status=404, body={})
    # The real server with nothing done: undone:false and its own sentence, shown as is.
    import json as _json, urllib.request as _u
    real = _json.loads(_u.urlopen(_u.Request(f"{BASE}/api/undo", data=b"{}", method="POST"),
                                  timeout=5).read())
    check("the real /api/undo answers the frozen contract",
          real.get("undone") is False and isinstance(real.get("say"), str) and real["say"], real)
    undo_case(browser, "real server with nothing to undo", "en", real["say"], True)


def test_undo_keyboard(browser):
    for key in ("Enter", "Space", "Meta+z"):
        ctx, page, _ = open_bar(browser, "en")
        calls = mock_undo(page, body={"undone": True, "did": "x", "say": "Put it back."})
        page.evaluate("barState('listening',''); barHeard('close this tab'); barResult('Closed it.', '')")
        if key != "Meta+z":
            page.keyboard.press("Tab")
            focused = page.evaluate("document.activeElement && document.activeElement.id")
            check(f"Tab reaches the Undo button", focused == "undo", focused)
            ring = page.evaluate("getComputedStyle(document.getElementById('undo')).outlineStyle")
            check("the focused Undo button shows a focus ring", ring != "none", ring)
        page.keyboard.press(key)
        page.wait_for_function("document.getElementById('undo').hidden", timeout=5000)
        check(f"{key} on Undo undoes, once", page.inner_text("#reply") == "Put it back." and calls == ["POST"],
              (page.inner_text("#reply"), calls))
        ctx.close()


def test_no_mic(browser):
    ctx, page, _ = open_bar(browser, "en")
    page.wait_for_timeout(600)
    check("bar mode never constructs a recogniser or opens the microphone",
          page.evaluate("window.__mic.sr === 0 && window.__mic.gum === 0"),
          page.evaluate("window.__mic"))
    ctx.close()
    # Positive control: the plain page does both, so the counters are real.
    ctx = new_ctx(browser, viewport={"width": 1000, "height": 800})
    ctx.add_init_script(INIT)
    page = ctx.new_page()
    page.goto(BASE + "/")
    page.wait_for_function("window.__mic.sr > 0", timeout=5000)
    check("control: the plain page does construct a recogniser (counters work)",
          page.evaluate("window.__mic.sr") > 0)
    ctx.close()


def test_language_follows_panel(browser):
    ctx = new_ctx(browser, viewport={"width": 640, "height": 64})
    ctx.add_init_script(INIT)
    bar = ctx.new_page()
    bar.goto(BASE + "/?bar=1")
    bar.wait_for_function("document.documentElement.lang.length === 2")
    other = ctx.new_page()
    other.set_viewport_size({"width": 380, "height": 560})
    other.goto(BASE + "/?panel=1")
    other.wait_for_function("document.getElementById('langmenu').children.length === 4")
    other.click("#langbtn")
    other.click("#langmenu button[data-code='ar']")
    bar.wait_for_function("document.documentElement.lang === 'ar'", timeout=3000)
    bar.evaluate("barResult('تم.', '')")
    check("a language picked in the panel reaches the open bar (lang, dir, copy)",
          bar.evaluate("document.documentElement.dir") == "rtl"
          and bar.inner_text("#undow") == COPY["ar"]["undo"])
    ctx.close()


# Every style rule that exists for the bar, removed from the live page. What is left is
# the page as it would be without the bar's styling.
STRIP_BAR_RULES = """() => {
  let n = 0;
  const strip = list => { for(let i = list.cssRules.length - 1; i >= 0; i--){
    const r = list.cssRules[i];
    if(r.cssRules && !r.selectorText) strip(r);
    // bar-only: it names barmode, and not just to rule the bar out (":not(.barmode)")
    else if(r.selectorText && r.selectorText.replaceAll(':not(.barmode)', '').includes('barmode')){
      list.deleteRule(i); n++; }
  } };
  for(const sheet of document.styleSheets){ try{ strip(sheet); }catch(_){ /* the web font's, cross-origin */ } }
  return n; }"""

LAYOUT = """() => {
  const out = {};
  for(const e of document.querySelectorAll('[id]')){
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    out[e.id] = [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height),
                 cs.display, cs.color, cs.fontSize, cs.visibility, (e.textContent||'').slice(0, 60)];
  }
  out.__body = document.body.className; out.__html = document.documentElement.className;
  return out; }"""


def settled_shot(page):
    """A picture of a page that has stopped changing: two frames in a row the same. One
    frame straight after a change can still be mid-composite (the card's glass)."""
    last = page.screenshot()
    for _ in range(8):
        page.wait_for_timeout(120)
        now = page.screenshot()
        if now == last:
            return now
        last = now
    return last


def test_other_modes_unchanged(browser):
    """The plain page and ?panel=1 are not touched by the bar, in every state we can put
    them in. This used to compare them with a frozen commit (4b5de6a), which failed on
    every intended change to the page since and so stopped protecting anything. Now each
    state is compared with itself minus every bar-only style rule: any bar styling that
    leaks into these modes changes a pixel or a box and fails, while deliberate changes
    to the page do not. The bar's hooks, its Undo button and its messages to native
    must not exist here either."""
    scenarios = [
        ("page idle", "/", (1200, 820), "light", "en", ""),
        ("page idle dark he", "/", (1200, 820), "dark", "he", ""),
        ("page answered card", "/", (1200, 820), "light", "en",
         "render({did:'answered', say:'It is 21 degrees and sunny in Tel Aviv.'}); threadAdd('a','b'); threadAdd('c','d');"),
        ("page sending card ar", "/", (1000, 900), "light", "ar",
         "render({did:'sending', say:'x', detail:{display:'Dana', text:'I am late', countdown:6}});"),
        ("page settings open ru", "/", (1000, 900), "dark", "ru", "openSettings();"),
        ("page error state", "/", (1000, 800), "light", "en", "setState('error','neterr');"),
        ("panel idle", "/?panel=1", (380, 560), "light", "en", ""),
        ("panel idle dark he", "/?panel=1", (380, 560), "dark", "he", ""),
        ("panel mirrored listener", "/?panel=1", (380, 560), "light", "he",
         "panelState('thinking','רגע'); panelHeard('תפתחי יוטיוב'); panelReply('פתחתי.');"),
        ("panel card", "/?panel=1", (380, 560), "dark", "en",
         "render({did:'timer_set', say:'ok', detail:{minutes:5, text:'tea'}});"),
        ("panel settings", "/?panel=1", (380, 560), "light", "ar", "openSettings();"),
    ]
    for name, path, (w, h), scheme, lang, script in scenarios:
        ctx = new_ctx(browser, lang=lang, viewport={"width": w, "height": h}, color_scheme=scheme,
                      reduced_motion="reduce")
        # A set-up Mac: on this test server's fresh state the first-run screens would
        # cover the panel, and every panel picture would be of them instead.
        ctx.route("**/api/onboarding", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"show": False, "first_run": False, "onboarded": True}))
            if r.request.method == "GET" else _guard(r))
        ctx.add_init_script(INIT + f"try{{localStorage.setItem('micmic.lang', {json.dumps(lang)})}}catch(_){{}}")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + path)
        page.wait_for_function("document.documentElement.lang === %s" % json.dumps(lang))
        page.evaluate("document.fonts.ready")
        if script:
            page.evaluate(script)
        page.wait_for_timeout(700)
        before, lay_before = settled_shot(page), page.evaluate(LAYOUT)
        removed = page.evaluate(STRIP_BAR_RULES)
        after, lay_after = settled_shot(page), page.evaluate(LAYOUT)
        check(f"{name}: there were bar-only style rules to take out", removed > 20, removed)
        if before != after:
            (SHOTS / f"diff-{name.replace(' ', '-')}-with-bar-css.png").write_bytes(before)
            (SHOTS / f"diff-{name.replace(' ', '-')}-without.png").write_bytes(after)
        check(f"{name}: the bar's styling changes no pixel here", before == after,
              "see diff-*.png in " + str(SHOTS))
        diffs = {k: (lay_before.get(k), lay_after.get(k)) for k in set(lay_before) | set(lay_after)
                 if lay_before.get(k) != lay_after.get(k)}
        check(f"{name}: nor any element's box, display, colour, size or text", not diffs, diffs)
        check(f"{name}: not in bar mode, and the panel itself is what is on screen",
              "barmode" not in lay_before["__html"] and "onboarding" not in lay_before["__body"],
              [lay_before["__html"], lay_before["__body"]])
        check(f"{name}: the Undo button and the working line do not exist visually",
              lay_before["undo"][4] == "none" and lay_before["work"][4] == "none")
        check(f"{name}: none of the bar's hooks are defined",
              page.evaluate("['barState','barHeard','barResult'].every(k => typeof window[k] === 'undefined')"))
        check(f"{name}: the bar never posts to micmicbar here",
              all(m.startswith("micmic:") for m in msgs(page)), msgs(page))
        check(f"{name}: no page errors", not errors, errors[:2])
        ctx.close()


def main():
    proc = ensure_server()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)   # bundled Chromium, never channel="chrome"
            try:
                for t in (test_states, test_rapid_heard, test_long_results, test_mixed_direction,
                          test_escaping, test_turns, test_heights, test_esc_and_keys, test_undo,
                          test_undo_keyboard, test_no_mic, test_language_follows_panel,
                          test_other_modes_unchanged):
                    print(f"\n-- {t.__name__}")
                    try:
                        t(browser)
                    except Exception as e:  # noqa: BLE001
                        check(f"{t.__name__} ran to the end", False, repr(e)[:400])
            finally:
                browser.close()
        no_model_calls()
    finally:
        if proc is not None:
            proc.terminate()
    print(f"\nscreenshots: {SHOTS}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
