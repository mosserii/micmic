#!/usr/bin/env python3
"""Nothing in MicMic's window is drawn on top of anything else, and nothing is cut off.

    cd <worktree> && uv run python tests/layout/test_layout.py

The window (?panel=1, 380x560, the only size native/panel.py gives it: no resize
handle, no zoom) and the bar (?bar=1), in English and Hebrew, dark and light: every
onboarding screen and every state the listener can put the panel in, including the
one that shipped broken in 1.0.1, where the listener's first state change during the
onboarding brought the typing box, "+" and the gear back underneath it.

For each picture, every visible control and every visible piece of text is measured
(opacity and visibility followed up the tree, so a layer that is faded out does not
count). It fails when two of them intersect and neither contains the other, when one
is cut by an overflow:hidden box or by the window's edge, when the page scrolls
sideways, or when a control or label sits under the window's close and minimise
buttons. It also checks that labels cannot be selected and that a press that moves on
the background asks native to drag the window, while a plain click still listens.

Its own server on 8883 with a fresh state directory, never 8799; every POST is
answered in the browser, so nothing here changes a setting or finishes an onboarding.
Screenshots go to $LAYOUT_SHOTS (a temp directory by default).
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

ROOT = Path(__file__).resolve().parents[2]
PORT = 8883
BASE = f"http://127.0.0.1:{PORT}"
assert PORT != 8799, "never the live server"
W, H = 380, 560                       # native/panel.py
SHOTS = Path(os.environ.get("LAYOUT_SHOTS") or tempfile.mkdtemp(prefix="layout-shots-"))
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


# ---------------------------------------------------------------- server
def _port_open():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def start_server():
    if _port_open():
        raise SystemExit(f"something is already listening on {PORT}; stop it first")
    state = Path(tempfile.mkdtemp(prefix="layout-state-"))
    # A profile of its own, or the checkout's real one is copied in and the Mac looks
    # set up (paths.state's legacy migration).
    (state / "profile.json").write_text("{}")
    env = dict(os.environ, MICMIC_PORT=str(PORT), MICMIC_STATE_DIR=str(state),
               MICMIC_DRY_OPEN="1", **OFFLINE)
    env.pop("MICMIC_ALLOW_SEND", None)
    env.pop("MICMIC_ALLOW_CALL", None)
    proc = subprocess.Popen([sys.executable, "-m", "savta.server"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        if _port_open():
            return proc
        time.sleep(0.1)
    proc.kill()
    raise SystemExit(f"server on {PORT} did not start")


def no_model_calls():
    """The server's own count of Jev calls and their cost, which must both be zero."""
    import urllib.request as _u
    try:
        h = json.loads(_u.urlopen(BASE + "/api/health", timeout=5).read())
    except Exception as e:  # noqa: BLE001
        return check("the server reports no paid model calls", False, repr(e))
    check("the server reports no paid model calls (Jev calls and cost both zero)",
          not h.get("calls") and not h.get("cost_usd"), h)


# ---------------------------------------------------------------- the check
# Everything a person reads or presses. Decoration (the colour field, glows, rings,
# ripples, the keys' faces) is left out: it is meant to sit behind things.
LAYOUT_JS = r"""
(() => {
  const W = innerWidth, H = innerHeight, panel = document.body.classList.contains('panel');
  const out = [];
  const TEXTY = el => [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
  const PARTS = '.ob-demo,.ob-bar,.ob-kbd,.ob-key,.ob-perms,.ob-orb,.ob-quote,.ob-pill,.orb,.card,.typerow,.ob-prog';
  const effOpacity = el => { let o = 1; for(let e = el; e && e.nodeType === 1; e = e.parentElement)
    o *= parseFloat(getComputedStyle(e).opacity); return o; };
  const shown = el => {
    if(el.closest('.ob-sr,[aria-hidden="true"]:not(.ob-demo),#dev,.field,svg')) return false;
    const cs = getComputedStyle(el);
    if(cs.visibility !== 'visible' || cs.display === 'none') return false;
    const r = el.getBoundingClientRect();
    if(r.width < 1 || r.height < 1) return false;
    return effOpacity(el) > 0.05;
  };
  const name = el => {
    let s = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
      (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : '');
    const txt = (el.innerText || el.value || el.placeholder || '').trim().slice(0, 24);
    return txt ? `${s} "${txt}"` : s;
  };
  // Hidden behind an opaque layer (the settings sheet is the whole window) is not on
  // screen. A see-through layer hides nothing: the onboarding is one, and what showed
  // through it in 1.0.1 is exactly what this has to catch.
  const opaque = el => {
    const m = getComputedStyle(el).backgroundColor.match(/rgba?\(([^)]+)\)/);
    if(!m) return false;
    const v = m[1].split(/[\s,\/]+/).filter(Boolean);
    return (v.length > 3 ? parseFloat(v[3]) : 1) >= 0.9 && effOpacity(el) >= 0.9;
  };
  const covered = el => {
    const r = el.getBoundingClientRect();
    const x = Math.min(Math.max((r.left + r.right) / 2, 0), W - 1), y = Math.min(Math.max((r.top + r.bottom) / 2, 0), H - 1);
    for(const top of document.elementsFromPoint(x, y)){
      if(top === el || el.contains(top) || top.contains(el)) return false;
      if(opaque(top)) return true;
    }
    return false;
  };
  const all = [...document.querySelectorAll('body *')].filter(el =>
    (el.matches('button,input,select,textarea,a,[role=progressbar]') || el.matches(PARTS) || TEXTY(el)) && shown(el) && !covered(el));
  // A label is where its words are drawn, not its padded box: a pinned title row is
  // tall on purpose, and only its words have to stay clear of things.
  const box = el => {
    if(el.matches('button,input,select,textarea,a,[role=progressbar]') || el.matches(PARTS))
      return el.getBoundingClientRect();
    const r = document.createRange(); r.selectNodeContents(el);
    const q = r.getBoundingClientRect(), o = el.getBoundingClientRect();
    if(!(q.width && q.height)) return o;
    // its own clamp or ellipsis is how it is meant to end, not a cut
    const cs = getComputedStyle(el);
    if(cs.overflowX === 'visible' && cs.overflowY === 'visible') return q;
    return {left:Math.max(q.left, o.left), top:Math.max(q.top, o.top),
            right:Math.min(q.right, o.right), bottom:Math.min(q.bottom, o.bottom)};
  };
  // What is actually visible of it: a clamped title's hidden lines and a transcript
  // turn scrolled out of its box are not on screen.
  const seen = el => {
    let r = box(el), L = r.left, T = r.top, Rr = r.right, B = r.bottom;
    for(let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement){
      const cs = getComputedStyle(p);
      if(cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      const q = p.getBoundingClientRect();
      L = Math.max(L, q.left); T = Math.max(T, q.top); Rr = Math.min(Rr, q.right); B = Math.min(B, q.bottom);
    }
    return {left:L, top:T, right:Rr, bottom:B};
  };
  const R = new Map(all.map(el => [el, seen(el)]));
  const TOL = 0.5;
  for(const el of [...all]){ const r = R.get(el);
    if(r.right - r.left < 1 || r.bottom - r.top < 1){ all.splice(all.indexOf(el), 1); R.delete(el); } }
  const hit = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > TOL &&
                        Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > TOL;
  // A solid pinned bar (Settings' Done row) is what scrolled content passes under.
  const pinned = el => { for(let e = el; e && e !== document.body; e = e.parentElement)
    if(getComputedStyle(e).position === 'sticky') return opaque(e) ? e : null; return null; };
  const scroller = el => { for(let e = el.parentElement; e; e = e.parentElement)
    if(['auto','scroll'].includes(getComputedStyle(e).overflowY)) return e; return null; };
  const under = (a, b) => { const s = pinned(a); if(!s || s.contains(b)) return false;
    const box = scroller(s); return !!box && box.contains(b); };
  // 1. no two of them intersect unless one is inside the other
  for(let i = 0; i < all.length; i++) for(let j = i + 1; j < all.length; j++){
    const a = all[i], b = all[j];
    if(a.contains(b) || b.contains(a) || under(a, b) || under(b, a)) continue;
    if(hit(R.get(a), R.get(b))) out.push(`overlap: ${name(a)}  x  ${name(b)}`);
  }
  // 2. nothing cut by the window or by a clipping box (a scroll box is fine: what is
  //    outside it is reachable; so is text that ends in an ellipsis on purpose)
  for(const el of all){
    const r = box(el);
    if(r.left < -TOL || r.right > W + TOL || r.top < -TOL || r.bottom > H + TOL){
      let scrolled = false;
      for(let p = el.parentElement; p; p = p.parentElement){
        const o = getComputedStyle(p).overflowY; if(o === 'auto' || o === 'scroll'){ scrolled = true; break; }
      }
      if(!scrolled) out.push(`off the window: ${name(el)} [${r.left|0},${r.top|0},${r.right|0},${r.bottom|0}]`);
    }
    for(let p = el.parentElement; p && p !== document.body; p = p.parentElement){
      const cs = getComputedStyle(p);
      if(cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      if(['auto','scroll'].includes(cs.overflowY) || cs.textOverflow === 'ellipsis') break;
      const q = p.getBoundingClientRect();
      if(r.left < q.left - TOL || r.right > q.right + TOL || r.top < q.top - TOL || r.bottom > q.bottom + TOL)
        out.push(`clipped: ${name(el)} by ${name(p)}`);
      break;
    }
  }
  // 3. the page never scrolls sideways
  const se = document.scrollingElement;
  if(se.scrollWidth > W + 1) out.push(`horizontal overflow: ${se.scrollWidth} > ${W}`);
  // 4. the window's own close and minimise buttons sit over the page's top left corner
  if(panel){
    const lights = {left:0, top:0, right:70, bottom:30};
    for(const el of all) if(hit(R.get(el), lights) && !el.matches('.typerow,.card'))
      out.push(`under the window buttons: ${name(el)}`);
  }
  return out;
})()
"""

# The column's own edges: whatever is in it is either whole or scrolled fully away,
# never half under the top row (the orb above a tall card) or half under the typing box.
WHOLE_JS = r"""
(() => {
  const st = document.querySelector('.stage').getBoundingClientRect();
  const bar = document.getElementById('typerow').getBoundingClientRect();
  const out = [];
  for(const id of ['orb', 'state', 'heard', 'reply', 'card']){
    const e = document.getElementById(id);
    if(!e || !e.getClientRects().length || getComputedStyle(e).display === 'none') continue;
    if(id !== 'orb' && id !== 'card' && !e.textContent.trim()) continue;
    if(id === 'card' && !e.classList.contains('show')) continue;
    const r = e.getBoundingClientRect();
    const top = st.top, bottom = Math.min(st.bottom, bar.top);
    const inside = r.top >= top - 0.5 && r.bottom <= bottom + 0.5;
    const away = r.bottom <= top || r.top >= bottom;
    if(!inside && !away) out.push(`${id} [${r.top|0}-${r.bottom|0}] cut by [${top|0}-${bottom|0}]`);
  }
  return out;
})()
"""

INIT = """
window.__msgs = [];
window.webkit = {messageHandlers: {
  micmic:    {postMessage: m => window.__msgs.push(String(m))},
  micmicbar: {postMessage: m => window.__msgs.push('bar:' + m)}}};
"""


def layout(page, label, shot=True):
    page.evaluate("document.fonts.ready")
    problems = page.evaluate(LAYOUT_JS)
    if shot:
        page.screenshot(path=str(SHOTS / f"{label}.png"))
    check(f"clean layout: {label}", not problems, "; ".join(problems[:6]))
    return problems


SPEECH = {"en": "en-US", "he": "he-IL", "ar": "ar-SA", "ru": "ru-RU"}
ALL_ON = {"microphone": "granted", "speech": "granted", "accessibility": "granted", "ready": True}


class Api:
    """Answers in the browser: every POST (recorded), and whatever GET a test wants to
    decide: /api/permissions, /api/onboarding, a patched /api/config."""

    def __init__(self, perms=ALL_ON):
        self.perms = perms              # None: whatever the test server says
        self.heard = None
        self.config = None              # fields laid over the real /api/config
        self.replies = {}               # POST path suffix -> JSON body, or "abort"
        self.posts = []

    def route(self, route):
        req = route.request
        path = req.url.split("?")[0]
        if req.method == "POST":
            try:
                body = json.loads(req.post_data or "{}")
            except ValueError:
                body = req.post_data
            self.posts.append((path.split("/api/")[-1], body))
            for suffix, reply in self.replies.items():
                if path.endswith(suffix):
                    if reply == "abort":
                        return route.abort()
                    return route.fulfill(status=200, content_type="application/json",
                                         body=json.dumps(reply, ensure_ascii=False))
            return route.fulfill(status=200, content_type="application/json", body="{}")
        if path.endswith("/api/permissions") and self.perms is not None:
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(self.perms))
        if path.endswith("/api/onboarding") and self.heard is not None:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"show": True, "first_run": True, "turn": {"heard": self.heard}}))
        if path.endswith("/api/config") and self.config is not None:
            real = route.fetch().json()
            real.update(self.config)
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(real))
        return route.continue_()


def open_panel(browser, lang, scheme, step=None, api=None, first_run=True):
    ctx = browser.new_context(viewport={"width": W, "height": H}, color_scheme=scheme,
                              device_scale_factor=2, reduced_motion="reduce")
    api = api or Api()
    # The page's language is the server's speech language (the browser's copy only
    # covers the moment before /api/config answers), so that is where a test sets it.
    api.config = {"language_hint": SPEECH[lang], **(api.config or {})}
    ctx.route("**/api/**", api.route)
    init = INIT + f"try{{localStorage.setItem('micmic.lang',{json.dumps(lang)})}}catch(_){{}}"
    init += (f"try{{sessionStorage.setItem('micmic.onboarding.step','{step}')}}catch(_){{}}"
             if step else "try{sessionStorage.clear()}catch(_){}")
    ctx.add_init_script(init)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/?panel=1")
    page.wait_for_function("document.documentElement.lang === %s" % json.dumps(lang))
    if first_run:
        page.wait_for_selector(".ob.in", timeout=8000)
    page.wait_for_timeout(700)
    return ctx, page, errors


# ---------------------------------------------------------------- tests
def test_onboarding(browser):
    for lang in ("en", "he"):
        for scheme in ("dark", "light"):
            tag = f"{lang}-{scheme}"
            # 1: every phase of the demo, then the listener changing state under it,
            #    which is what 1.0.1 got wrong.
            ctx, page, errors = open_panel(browser, lang, scheme, step=1)
            for ph in ("idle", "press", "work", "result"):
                page.evaluate("""p => { const d = document.querySelector('.ob-demo'); d.dataset.p = p;
                  d.dataset.w = '1'; document.querySelectorAll('.ob-words b').forEach(b => b.classList.add('in')); }""", ph)
                page.wait_for_timeout(80)
                layout(page, f"ob1-{ph}-{tag}")
            for s in ("listening", "thinking", "acting", "idle"):
                page.evaluate("s => window.panelState(s, '')", s)
                page.wait_for_timeout(60)
                layout(page, f"ob1-after-panelState-{s}-{tag}", shot=(s == "thinking"))
            check(f"onboarding keeps the stage hidden through panelState ({tag})", page.evaluate(
                "document.body.classList.contains('onboarding') && "
                "getComputedStyle(document.getElementById('typebox')).visibility === 'hidden' && "
                "getComputedStyle(document.getElementById('gear')).visibility === 'hidden' && "
                "getComputedStyle(document.getElementById('newchat')).visibility === 'hidden'"))
            check(f"no page errors, step 1 ({tag})", not errors, errors[:2])
            ctx.close()

            # 2: waiting, two switched off (the "Turn on" buttons), all on
            api = Api()
            for kind, perms in (
                    ("unknown", {}),
                    ("denied", {"microphone": "granted", "speech": "denied",
                                "accessibility": "denied", "ready": False}),
                    ("granted", {"microphone": "granted", "speech": "granted",
                                 "accessibility": "granted", "ready": False})):
                api.perms = perms
                ctx, page, errors = open_panel(browser, lang, scheme, step=2, api=api)
                page.wait_for_timeout(300)
                page.evaluate("window.panelState('listening','')")
                page.wait_for_timeout(60)
                layout(page, f"ob2-{kind}-{tag}")
                ctx.close()

            # 3: waiting, thinking, and heard
            api = Api()
            ctx, page, errors = open_panel(browser, lang, scheme, step=3, api=api)
            layout(page, f"ob3-idle-{tag}")
            page.evaluate("window.panelState('thinking','')")
            page.wait_for_timeout(80)
            layout(page, f"ob3-thinking-{tag}")
            api.heard = "What time is it?" if lang == "en" else "מה השעה?"
            page.wait_for_selector(".ob-step.ok", timeout=5000)
            page.wait_for_timeout(200)
            layout(page, f"ob3-heard-{tag}")
            check(f"no page errors, step 3 ({tag})", not errors, errors[:2])
            ctx.close()

    # The longest copy the page has, so a language nobody looked at cannot wrap a
    # button into the next one.
    for lang in ("ru", "ar"):
        for step in (1, 2, 3):
            ctx, page, _ = open_panel(browser, lang, "dark", step=step)
            page.evaluate("window.panelState('listening','')")
            page.wait_for_timeout(60)
            layout(page, f"ob{step}-{lang}-dark")
            ctx.close()


def test_panel_states(browser):
    for lang in ("en", "he"):
        for scheme in ("dark", "light"):
            tag = f"{lang}-{scheme}"
            api = Api()
            ctx, page, errors = open_panel(browser, lang, scheme, api=api, first_run=False)
            dismiss_onboarding(page)
            layout(page, f"panel-idle-{tag}")
            page.evaluate("window.panelState('listening','')")
            page.wait_for_timeout(80)
            layout(page, f"panel-listening-{tag}")
            page.evaluate("window.panelHeard(%s); window.panelState('thinking','')" % json.dumps(
                "What time is it in Tokyo right now?" if lang == "en" else "מה השעה בטוקיו עכשיו?"))
            page.wait_for_timeout(80)
            layout(page, f"panel-thinking-{tag}")
            page.evaluate("window.panelReply(%s); window.panelState('acting','')" % json.dumps(
                "It is 4:12 in the afternoon in Tokyo." if lang == "en" else "בטוקיו עכשיו 16:12."))
            page.wait_for_timeout(80)
            layout(page, f"panel-result-{tag}")
            page.click("#langbtn")
            page.wait_for_timeout(250)
            # the open menu is a popover and covers what is under it by design; what
            # must hold is that it stays inside the window
            menu = page.evaluate("(() => { const r = document.getElementById('langmenu').getBoundingClientRect();"
                                 " return [r.left, r.right, r.bottom]; })()")
            check(f"language menu opens inside the window ({tag})",
                  menu[0] >= 0 and menu[1] <= W and menu[2] <= H, menu)
            page.keyboard.press("Escape")
            page.click("#gear")
            page.wait_for_timeout(400)
            layout(page, f"panel-settings-{tag}")
            # NEW: "Open at login" switch, named in her language, saved like every
            # other segmented setting (no overlap check needed beyond layout()'s own,
            # already run over this same screenshot above).
            check(f"the login-item row is present and named ({tag})",
                  page.text_content("#lLogin") == page.evaluate("t('sLogin')")
                  and page.is_visible("#segLogin"), page.text_content("#lLogin"))
            page.click('#segLogin button[data-v="on"]')
            page.wait_for_timeout(150)
            check(f"turning it on saves open_at_login ({tag})",
                  ("settings", {"open_at_login": True}) in api.posts, api.posts)
            page.click("#sdone")
            check(f"no page errors, panel ({tag})", not errors, errors[:2])
            ctx.close()


def test_bar(browser):
    for lang in ("en", "he"):
        for scheme in ("dark", "light"):
            tag = f"{lang}-{scheme}"
            ctx = browser.new_context(viewport={"width": 640, "height": 64}, color_scheme=scheme,
                                      device_scale_factor=2, reduced_motion="reduce")
            bar_api = Api(); bar_api.config = {"language_hint": SPEECH[lang]}
            ctx.route("**/api/**", bar_api.route)
            ctx.add_init_script(INIT + f"try{{localStorage.setItem('micmic.lang',{json.dumps(lang)})}}catch(_){{}}")
            page = ctx.new_page()
            page.goto(BASE + "/?bar=1")
            page.wait_for_function("document.documentElement.lang === %s" % json.dumps(lang))

            def fit():
                h = page.evaluate("Math.ceil(document.querySelector('.stage').getBoundingClientRect().height)")
                page.set_viewport_size({"width": 640, "height": h})
                page.wait_for_timeout(60)
            for label, code in (
                    ("listening", "barState('listening','')"),
                    ("heard", "barHeard(%s)" % json.dumps("Play some music" if lang == "en" else "תשימי קצת מוזיקה")),
                    ("working", "barState('thinking','')"),
                    ("result", "barResult(%s, '')" % json.dumps("Playing music" if lang == "en" else "מנגנת מוזיקה"))):
                page.evaluate(code)
                fit()
                layout(page, f"bar-{label}-{tag}")
            ctx.close()


def test_selection_and_drag(browser):
    ctx, page, _ = open_panel(browser, "en", "dark", step=3)
    # WebKit honours only the prefixed property; Chromium reports both.
    sel = page.evaluate("""(() => { const cs = getComputedStyle(document.querySelector('.ob-step.on .ob-sub'));
      return [cs.webkitUserSelect || '', cs.userSelect || '']; })()""")
    check("labels cannot be selected", "none" in sel and all(v in ("none", "") for v in sel), sel)
    body = open(ROOT / "savta/web/index.html", encoding="utf-8").read()
    check("the page sets -webkit-user-select:none (the app's WebKit needs the prefix)",
          "-webkit-user-select:none" in body)
    page.mouse.click(40, 300)
    page.wait_for_timeout(60)
    check("a click on a label selects nothing",
          page.evaluate("String(getSelection())") == "")
    ctx.close()

    ctx, page, _ = open_panel(browser, "en", "dark", first_run=False)
    dismiss_onboarding(page)
    page.evaluate("window.__msgs.length = 0")

    def gesture(x, y, dx):
        page.evaluate("window.__msgs.length = 0")
        page.mouse.move(x, y)
        page.mouse.down()
        if dx:
            page.mouse.move(x + dx / 2, y, steps=3)
            page.mouse.move(x + dx, y, steps=3)
        page.mouse.up()
        page.wait_for_timeout(60)
        return page.evaluate("window.__msgs.slice()")

    m = gesture(190, 22, 40)
    check("press and move on the top strip drags the window, and does not also listen",
          m == ["drag"], m)
    m = gesture(40, 330, 40)
    check("press and move on the background drags the window", m == ["drag"], m)
    m = gesture(40, 330, 0)
    check("a plain click on the background still listens", m == ["listen"], m)
    box = page.evaluate("(() => { const r = document.getElementById('typebox').getBoundingClientRect();"
                        " return [r.left + 30, r.top + r.height / 2]; })()")
    m = gesture(box[0], box[1], 40)
    check("a drag in the typing box never moves the window", "drag" not in m, m)
    g = page.evaluate("(() => { const r = document.getElementById('gear').getBoundingClientRect();"
                      " return [r.left + r.width / 2, r.top + r.height / 2]; })()")
    m = gesture(g[0], g[1], 30)
    check("a drag that starts on a button never moves the window", "drag" not in m, m)
    page.keyboard.press("Escape")
    ctx.close()


# ---------------------------------------------------------------- what the page renders
# The answers QA drove through the typing box (the router's own wording), in both
# languages: every card kind, and the no-card replies that used to lose their end.
SAY = {
  "answered": {"en": "Lisbon is 18 degrees and sunny right now, with a light wind from the north and no rain expected until Sunday.",
               "he": "בליסבון עכשיו 18 מעלות ושמש, רוח קלה מצפון ולא צפוי גשם עד יום ראשון."},
  "playing": {"en": "Here is Titanic.", "he": "הנה טיטאניק."},
  "sending": {"en": "Sending to David.", "he": "שולחת לדוד."},
  "send_disabled": {"en": "Sending messages is turned off in Settings, so nothing went to David.",
                    "he": "שליחת הודעות כבויה בהגדרות, אז שום דבר לא נשלח לדוד."},
  "timer_set": {"en": "Timer set for 10 minutes.", "he": "טיימר ל-10 דקות."},
  "did_several": {"en": "Done.", "he": "סיימתי."},
  "read_messages": {"en": "", "he": ""},
  "weather_unavailable": {"en": "I couldn't find the weather for Springfield. Which Springfield did you mean?",
                          "he": "לא מצאתי את מזג האוויר בספרינגפילד. לאיזו ספרינגפילד התכוונת?"},
  "chat_timeout": {"en": "Sorry, that took too long. Could you say it again?",
                   "he": "סליחה, זה לקח יותר מדי זמן. אפשר להגיד את זה שוב?"},
  "not_configured": {"en": "MicMic is not set up yet. Sign in, or add your own key in Settings.",
                     "he": "MicMic עוד לא מוגדר. צריך להתחבר לחשבון או להכניס מפתח אישי בהגדרות."},
  "daily_limit": {"en": "You have used your free requests. To keep going, upgrade to MicMic Pro in Settings.",
                  "he": "נגמרו הבקשות החינמיות לניסיון. כדי להמשיך, אפשר לשדרג ל-MicMic Pro בהגדרות."},
  "service_unavailable": {"en": "I cannot reach my service right now. Please try again in a minute.",
                          "he": "אני לא מצליחה להגיע לשירות שלי כרגע. אפשר לנסות שוב בעוד דקה."},
  "half_heard": {"en": "Hello, I am MicMic. What should I call you? I only caught part of that. Could you say it again?",
                 "he": "שלום, אני מיקמיק. איך קוראים לך? שמעתי רק חלק. תגידי לי את זה שוב?"},
}
ASK = {"en": "send David a message that I'm feeling much better",
       "he": "תשלחי לדוד שאני מרגישה יותר טוב"}
THUMB = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='480' height='270'>"
         "<rect width='480' height='270' fill='%23446'/></svg>")


def utter(kind, lang):
    d = {"did": kind, "say": SAY[kind][lang]}
    if kind == "playing":
        d["detail"] = {"title": "Titanic (1997) Official Trailer - Leonardo DiCaprio, Kate Winslet Movie HD 4K Remastered",
                       "thumb": THUMB, "length": "3:47", "views": "12M views"}
    elif kind == "sending":
        d["detail"] = {"display": "David" if lang == "en" else "דוד", "text": "I'm feeling much better", "countdown": 6}
    elif kind == "send_disabled":
        d["detail"] = {"text": "I'm feeling much better"}
    elif kind == "timer_set":
        d["detail"] = {"minutes": 10, "text": "take the pasta out" if lang == "en" else "להוציא את הפסטה"}
    elif kind == "did_several":
        d["detail"] = {"results": [{"step": x} for x in (
            ["Opened Calculator", "Turned the volume down to 30%", "Set a timer for 5 minutes to call the pharmacy back"]
            if lang == "en" else ["פתחתי את המחשבון", "הנמכתי את הווליום ל-30%", "טיימר ל-5 דקות להתקשר לבית המרקחת"])]}
    elif kind == "read_messages":
        d["detail"] = {"messages": [
            {"who": "David", "text": "Are you coming on Sunday? Grandma is making the soup you like."},
            {"who": "Maya", "text": "Call me when you can"},
            {"who": "Dr. Levi clinic", "text": "Reminder: your appointment is on Tuesday at 10:30. Reply 1 to confirm."}]}
    return d


def dismiss_onboarding(page):
    """Skip it the way she would (Escape). The skip is answered in the browser, so the
    test server still shows the onboarding to the next page."""
    if page.evaluate("!!document.querySelector('.ob')"):
        page.keyboard.press("Escape")
        page.wait_for_selector(".ob", state="detached", timeout=5000)
    page.wait_for_timeout(450)


def bare_panel(browser, lang, scheme, api=None):
    """The panel with the onboarding out of the way, as after a finished first run."""
    api = api or Api()
    ctx, page, errors = open_panel(browser, lang, scheme, api=api, first_run=False)
    dismiss_onboarding(page)
    return ctx, page, errors, api


def typed(page, text):
    page.fill("#typebox", text)
    page.press("#typebox", "Enter")
    page.wait_for_timeout(500)


def test_results(browser):
    for lang in ("en", "he"):
        for scheme in ("dark", "light"):
            tag = f"{lang}-{scheme}"
            for kind in SAY:
                api = Api()
                api.replies["/api/utterance"] = utter(kind, lang)
                ctx, page, errors, _ = bare_panel(browser, lang, scheme, api)
                typed(page, ASK[lang])
                layout(page, f"result-{kind}-{tag}")
                cut = page.evaluate(WHOLE_JS)
                check(f"NEW-4 nothing in the column is cut by its edges ({kind}, {tag})", not cut, cut)
                st = page.evaluate("[document.body.className, document.getElementById('state').textContent]")
                if kind in ("not_configured", "daily_limit", "service_unavailable", "half_heard"):
                    check(f"#6 a reply with no card settles to idle ({kind}, {tag})",
                          "thinking" not in st[0].split(), st)
                    sizes = page.evaluate("[parseFloat(getComputedStyle(document.getElementById('reply')).fontSize),"
                                          " parseFloat(getComputedStyle(document.getElementById('heard')).fontSize)]")
                    check(f"the answer is the big line, her words the small one ({kind}, {tag})",
                          sizes[0] > sizes[1], sizes)
                    check(f"#7 the whole reply shows ({kind}, {tag})", page.evaluate(
                        "(() => { const r = document.getElementById('reply');"
                        " return r.scrollHeight <= r.clientHeight + 1 && r.textContent.length > 20; })()"))
                if kind in ("weather_unavailable", "chat_timeout"):
                    check(f"{kind} gets the answer card ({tag})", page.evaluate(
                        "document.getElementById('card').classList.contains('show') && "
                        "document.getElementById('cardT').textContent === %s" % json.dumps(SAY[kind][lang])))
                if kind == "read_messages":
                    more = page.evaluate("(() => { const b = document.querySelector('.card .body');"
                                         " return [b.scrollHeight > b.clientHeight + 1, b.classList.contains('more')]; })()")
                    check(f"NEW-4 a card taller than its room says there is more ({tag})",
                          more[0] == more[1], more)
                    check(f"#12 other-script lines in a card read in their own direction ({tag})",
                          page.evaluate("getComputedStyle(document.querySelector('.msg span')).unicodeBidi") == "plaintext")
                if kind == "sending" and lang == "en":
                    check("#24 the sending card's title is a sentence",
                          page.inner_text("#cardT").startswith("Sending to David"), page.inner_text("#cardT"))
                if kind == "did_several":
                    check(f"#24 'Done.' does not repeat 'All done' ({tag})",
                          page.evaluate("document.getElementById('reply').textContent") == "")
                check(f"no page errors, result {kind} ({tag})", not errors, errors[:2])
                ctx.close()

            # a conversation: the transcript is not squeezed and her words stay off the card
            seq = [utter("answered", lang), utter("timer_set", lang), utter("send_disabled", lang), utter("answered", lang)]
            api = Api()
            ctx, page, errors, _ = bare_panel(browser, lang, scheme, api)
            for i, d in enumerate(seq):
                api.replies["/api/utterance"] = d
                typed(page, ASK[lang] + ("" if i < 3 else " and tomorrow"))
            layout(page, f"result-thread-{tag}")
            cut = page.evaluate(WHOLE_JS)
            check(f"NEW-4 nothing in the column is cut by its edges (thread, {tag})", not cut, cut)
            h = page.evaluate("document.getElementById('thread').getBoundingClientRect().height")
            check(f"#8 the transcript keeps room for two turns with a card up ({tag})", h >= 60, h)
            ctx.close()

            # the server unreachable
            api = Api()
            api.replies["/api/utterance"] = "abort"
            ctx, page, errors, _ = bare_panel(browser, lang, scheme, api)
            typed(page, ASK[lang])
            err = page.inner_text("#err")
            check(f"#9 an unreachable server reads as a sentence ({tag})",
                  err and "Error" not in err and "failed" not in err, err)
            state = page.text_content("#state") or ""
            check(f"NEW-5 the sentence is said once, not also in the state line ({tag})",
                  state.strip().lower() != err.strip().lower(), [state, err])
            layout(page, f"result-network-{tag}")
            ctx.close()


def test_panel_details(browser):
    for lang in ("en", "he"):
        tag = f"{lang}-dark"
        # #11: panelState with no text shows the state's own line
        ctx, page, _, _ = bare_panel(browser, lang, "dark")
        want = page.evaluate("t('think')")
        page.evaluate("window.panelState('thinking','')")
        check(f"#11 thinking with no text says so ({tag})", page.text_content("#state") == want,
              page.text_content("#state"))
        ctx.close()

        # #4: Accessibility off: the notice, the button, and an idle line that promises nothing
        api = Api(perms={"microphone": "granted", "speech": "granted", "accessibility": "denied", "ready": False})
        ctx, page, _, _ = bare_panel(browser, lang, "dark", api)
        page.wait_for_function("document.body.classList.contains('noax')", timeout=5000)
        page.wait_for_timeout(150)
        layout(page, f"panel-noax-{tag}")
        check(f"#4 the notice is on screen ({tag})", page.is_visible("#axnote"))
        check(f"#4 the idle line does not promise the key ({tag})",
              page.text_content("#state") == page.evaluate("t('idleNoKey')"), page.text_content("#state"))
        page.click("#axbtn")
        page.wait_for_timeout(100)
        check(f"#4 its button opens the Accessibility pane ({tag})",
              ("permissions/open", {"pane": "accessibility"}) in api.posts, api.posts)
        check(f"#4 and does not also start listening ({tag})",
              page.evaluate("window.__msgs.includes('listen')") is False)
        api.perms = ALL_ON
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        page.wait_for_function("!document.body.classList.contains('noax')", timeout=5000)
        check(f"#4 it goes once Accessibility is on ({tag})", not page.is_visible("#axnote"))
        ctx.close()

        # #23: copy names the shortcut she has
        api = Api(); api.config = {"hotkey": "right-command"}
        ctx, page, _, _ = bare_panel(browser, lang, "dark", api)
        page.evaluate("window.panelState('idle','')")
        st = page.text_content("#state")
        check(f"#23 the idle line names her shortcut ({tag})", "⌘" in st and "⌥" not in st, st)
        ctx.close()

        # #10: the pill is the speech language too
        api = Api()
        ctx, page, _, _ = bare_panel(browser, lang, "dark", api)
        other = "he" if lang == "en" else "en"
        page.click("#langbtn"); page.wait_for_timeout(200)
        page.click(f'#langmenu button[data-code="{other}"]'); page.wait_for_timeout(200)
        check(f"#10 picking a language on the pill saves the speech language ({tag})",
              ("settings", {"language_hint": {"he": "he-IL", "en": "en-US"}[other]}) in api.posts, api.posts)
        page.click("#gear"); page.wait_for_timeout(300)
        page.click('#segLang button[data-v="%s"]' % {"he": "he-IL", "en": "en-US"}[lang]); page.wait_for_timeout(200)
        check(f"#10 and Settings > Speech language moves the page's language ({tag})",
              page.evaluate("document.documentElement.lang") == lang)
        ctx.close()

        # #21 and #27: a rejected setting, and the sheet scrolled to the end
        api = Api(); api.replies["/api/settings"] = {"saved": [], "rejected": ["hotkey"], "settings": {}}
        ctx, page, _, _ = bare_panel(browser, lang, "dark", api)
        page.click("#gear"); page.wait_for_timeout(300)
        page.click('#segHotkey button[data-v="fn-fn"]'); page.wait_for_timeout(200)
        check(f"#21 a rejected setting is said in the sheet, in her language ({tag})",
              page.inner_text("#savedw") == page.evaluate("t('sNotSaved')")
              and page.inner_text("#err") == "", [page.inner_text("#savedw"), page.inner_text("#err")])
        page.evaluate("document.querySelector('.sheet').scrollTop = 1e6")
        page.wait_for_timeout(150)
        layout(page, f"panel-settings-bottom-{tag}")
        check(f"NEW-6 the pinned Settings title is solid, with no fade over the list ({tag})",
              page.evaluate("getComputedStyle(document.getElementById('sTitle'), '::after').content") in ("none", "normal")
              and page.evaluate("getComputedStyle(document.getElementById('sTitle')).backgroundColor")
              == page.evaluate("getComputedStyle(document.body).backgroundColor"))
        check(f"#28 the listen slider is drawn by the page ({tag})",
              page.evaluate("getComputedStyle(document.getElementById('listenRange')).webkitAppearance") == "none")
        ctx.close()

        # #26: Tab reaches the top row, and the controls are named in her language
        ctx, page, _, _ = bare_panel(browser, lang, "dark")
        seen = set()
        for _ in range(8):
            page.keyboard.press("Tab")
            seen.add(page.evaluate("document.activeElement.id"))
        check(f"#26 Tab reaches +, the language pill and the gear ({tag})",
              {"newchat", "langbtn", "gear", "typebox"} <= seen, seen)
        check(f"#26 the gear is named in her language ({tag})",
              page.get_attribute("#gear", "aria-label") == page.evaluate("t('settings')"))
        ctx.close()

    # #25: the bar's error line is readable on the dark ground
    ctx = browser.new_context(viewport={"width": 640, "height": 64}, color_scheme="dark")
    ctx.route("**/api/**", Api().route)
    ctx.add_init_script(INIT)
    page = ctx.new_page()
    page.goto(BASE + "/?bar=1")
    page.wait_for_function("document.documentElement.lang.length === 2")
    page.evaluate("barState('error','')")
    rgb = page.evaluate("getComputedStyle(document.getElementById('reply')).color")
    nums = [int(x) for x in rgb[rgb.index("(") + 1:rgb.index(")")].split(",")[:3]]
    lum = (0.2126 * nums[0] + 0.7152 * nums[1] + 0.0722 * nums[2]) / 255
    check("#25 the bar's error line is light enough on the dark bar", lum > 0.5, rgb)
    ctx.close()


def test_lead_items(browser):
    # 1: the listener's answer with no card, short and long, both languages and schemes
    long_en = ("It is 4:12 in the afternoon in Tokyo, which is sixteen hours ahead of you, so "
               "it is already tomorrow there. The sun sets at 5:38, and the forecast says clear "
               "skies all evening with a low of nine degrees.")
    long_he = ("בטוקיו עכשיו 16:12, שש עשרה שעות לפניך, אז שם כבר מחר. השמש שוקעת ב-17:38, "
               "והתחזית אומרת שמיים בהירים כל הערב עם מינימום של תשע מעלות.")
    for lang in ("en", "he"):
        for scheme in ("dark", "light"):
            tag = f"{lang}-{scheme}"
            ctx, page, errors, _ = bare_panel(browser, lang, scheme)
            for size, text in (("short", "It is 4:12 in the afternoon in Tokyo." if lang == "en" else "בטוקיו עכשיו 16:12."),
                               ("long", long_en if lang == "en" else long_he)):
                page.evaluate("([h, r]) => { panelHeard(h); panelReply(r); panelState('acting',''); }",
                              ["What time is it in Tokyo right now?" if lang == "en" else "מה השעה בטוקיו עכשיו?", text])
                page.wait_for_timeout(120)
                layout(page, f"answer-{size}-{tag}")
                f = page.evaluate("[parseFloat(getComputedStyle(document.getElementById('reply')).fontSize),"
                                  " parseFloat(getComputedStyle(document.getElementById('heard')).fontSize),"
                                  " (() => { const r = document.getElementById('reply'); return r.scrollHeight <= r.clientHeight + 1; })()]")
                check(f"1: the answer is prominent and her words smaller ({size}, {tag})", f[0] > f[1], f)
                check(f"1: the whole answer is there, nothing clipped ({size}, {tag})", f[2], f)
            ctx.close()

    # 2: the server's language wins over the browser's, and corrects it
    for server, browser_lang in (("en", "he"), ("he", "en"), ("ar", "ru")):
        api = Api(); api.config = {"language_hint": SPEECH[server]}
        ctx = browser.new_context(viewport={"width": W, "height": H}, reduced_motion="reduce")
        ctx.route("**/api/**", api.route)
        ctx.add_init_script(INIT + f"try{{localStorage.setItem('micmic.lang',{json.dumps(browser_lang)})}}catch(_){{}}")
        page = ctx.new_page()
        page.goto(BASE + "/?panel=1")
        page.wait_for_function("document.documentElement.lang === %s" % json.dumps(server), timeout=5000)
        page.wait_for_timeout(200)
        got = page.evaluate("[document.documentElement.lang, localStorage.getItem('micmic.lang')]")
        check(f"2: server {server} beats browser {browser_lang}, and the browser is corrected",
              got == [server, server], got)
        ctx.close()
    # with /api/config down, the browser's language is kept
    ctx = browser.new_context(viewport={"width": W, "height": H}, reduced_motion="reduce")
    ctx.route("**/api/config", lambda r: r.abort())
    ctx.add_init_script(INIT + "try{localStorage.setItem('micmic.lang','he')}catch(_){}")
    page = ctx.new_page()
    page.goto(BASE + "/?panel=1")
    page.wait_for_timeout(800)
    check("2: with no answer from the server, the browser's language stays",
          page.evaluate("document.documentElement.lang") == "he")
    ctx.close()

    # 3: the onboarding keyboard presses the shortcut she has
    for spec, want in (("right-option", ["⌥"]), ("right-command", ["⌘"]), ("right-control", ["⌃"]),
                       ("right-shift", ["⇧"]), ("fn-fn", ["fn"]), ("cmd+shift+m", ["⌘", "⇧", "M"])):
        for lang in ("en", "he"):
            api = Api(); api.config = {"hotkey": spec}
            ctx, page, errors = open_panel(browser, lang, "dark", step=1, api=api)
            page.evaluate("""() => { const d = document.querySelector('.ob-demo'); d.dataset.p = 'press'; d.dataset.w = '1';
              document.querySelectorAll('.ob-words b').forEach(b => b.classList.add('in')); }""")
            page.wait_for_timeout(100)
            hot = page.evaluate("[...document.querySelectorAll('.ob-kbd .ob-hot em')].map(e => e.textContent)")
            check(f"3: the keyboard presses {spec} ({lang})", hot == want, hot)
            title = page.text_content(".ob-step.on .ob-title")
            if spec != "right-option":
                check(f"3: the headline names it too ({spec}, {lang})", "⌥" not in title, title)
            if spec in ("fn-fn", "cmd+shift+m"):
                sub = page.text_content(".ob-step.on .ob-sub")
                check(f"3: no 'on the right' for a key that is not ({spec}, {lang})",
                      not any(w in sub for w in ("right", "ימין")), sub)
            if spec == "cmd+shift+m":
                check(f"3: a combination reads in key order inside the sentence ({lang})",
                      "\u2066⌘⇧M\u2069" in title, title)
            layout(page, f"ob1-key-{spec}-{lang}")
            ctx.close()


def test_qa_v2(browser):
    # NEW-7: the bar's "working" seconds in her language's own unit
    want = {"en": "3s", "he": "3 שנ׳", "ar": "3 ث", "ru": "3 с"}
    for lang in ("en", "he", "ar", "ru"):
        api = Api(); api.config = {"language_hint": SPEECH[lang]}
        ctx = browser.new_context(viewport={"width": 640, "height": 64}, reduced_motion="reduce")
        ctx.route("**/api/**", api.route)
        ctx.add_init_script(INIT)
        page = ctx.new_page()
        page.goto(BASE + "/?bar=1")
        page.wait_for_function("document.documentElement.lang === %s" % json.dumps(lang))
        page.evaluate("barState('listening',''); barHeard('x'); barState('thinking','')")
        page.wait_for_timeout(3300)
        work = page.text_content("#work") or ""
        check(f"NEW-7 the bar's seconds read in {lang}", work.endswith(want[lang]) and "3s" not in work
              if lang != "en" else work.endswith("3s"), work)
        ctx.close()

    # #12: a clamped line takes its direction (and so its ellipsis) from its own words
    for lang, title, want_dir in (("he", "Titanic (1997) Official Trailer - Leonardo DiCaprio, Kate Winslet Movie HD 4K Remastered", "ltr"),
                                  ("he", "טיטאניק (1997) טריילר רשמי - לאונרדו דיקפריו, קייט וינסלט, סרט באיכות גבוהה מאוד משוחזר", "rtl"),
                                  ("en", "טיטאניק (1997) טריילר רשמי - לאונרדו דיקפריו, קייט וינסלט, סרט באיכות גבוהה מאוד משוחזר", "rtl")):
        api = Api()
        api.replies["/api/utterance"] = {"did": "playing", "say": "",
                                         "detail": {"title": title, "thumb": THUMB, "length": "3:47", "views": "12M views"}}
        ctx, page, errors, _ = bare_panel(browser, lang, "light", api)
        typed(page, ASK[lang])
        got = page.evaluate("getComputedStyle(document.getElementById('cardT')).direction")
        check(f"#12 a {want_dir} title in the {lang} window is laid out {want_dir}, ellipsis at its end",
              got == want_dir, got)
        layout(page, f"card-title-{want_dir}-in-{lang}")
        ctx.close()


def main():
    proc = start_server()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)    # bundled Chromium, never channel="chrome"
            try:
                for t in (test_onboarding, test_panel_states, test_results, test_panel_details, test_lead_items, test_qa_v2, test_bar,
                          test_selection_and_drag):
                    print(f"\n-- {t.__name__}")
                    try:
                        t(browser)
                    except Exception as e:  # noqa: BLE001
                        check(f"{t.__name__} ran to the end", False, repr(e)[:400])
            finally:
                browser.close()
        no_model_calls()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\nscreenshots: {SHOTS}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print("  FAILED:", f)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
