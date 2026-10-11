#!/usr/bin/env python3
"""A headless Chromium stand-in for guide.LiveScreen, driving the Larkline demo
airline (micmic-work/launch/demo-airline) instead of a real macOS window.

guide.py's Guide only ever touches its screen through one seam: GUIDE.screen with
front(), snapshot(front), screenshot(pid), probe(snap, target_id), press(snap,
target_id), fill(snap, target_id, value), find(front, role, label). tests/test_guide.py
has FakeScreen, a scripted console, for the fast unit tests. This file is the other
kind of double: a REAL browser, so the Guide's own logic (model prompts, the watcher,
field planning, the finishing-button guard) can be rehearsed end to end against a page
that actually renders, validates and navigates, with nobody's real screen involved.

Where the controls come from: Chrome's own accessibility tree (CDP
Accessibility.getFullAXTree), because Chrome's computed accessible name and
description are exactly what macOS Accessibility would read off a real Chrome window.
The AX tree gives us role, name (-> label) and description (-> hint, e.g. an
aria-describedby). The box and the live value come from the DOM, reached from the AX
node's backendDOMNodeId via CDP DOM.resolveNode + Runtime.callFunctionOn, run as one
small function on the element itself: it also tags the element with a throwaway
data-mm-ref attribute so a later press()/fill()/probe() can find it again through a
normal Playwright locator, without needing a live object handle on another thread.

Playwright's sync API may only be touched from the thread that created it, but the
Guide's watcher calls probe() on its own timer thread while something else calls
snapshot(). So everything here that touches Playwright runs on ONE dedicated worker
thread; every public method just queues a function for that thread and blocks for the
answer (a queue.Queue of (callable, concurrent.futures.Future) pairs).

    cd <worktree> && uv run python tests/larkline_screen.py

Self-test only: no Guide, no model, no speaker.
"""
from __future__ import annotations

import concurrent.futures
import functools
import os
import http.server
import queue
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from savta.actions.web import FORBIDDEN_FIELD  # noqa: E402
from savta.actions.guide import _private_field  # noqa: E402

# The demo site lives outside this repo; LARKLINE_DIR points at another copy.
DEMO_DIR = Path(os.environ.get("LARKLINE_DIR")
                or Path.home() / "jev" / "micmic-work" / "launch" / "demo-airline")

# Chrome's own AX role strings (Accessibility.getFullAXTree's node.role.value), mapped
# to the words guide.ROLES uses. Everything else is either not a control a person
# presses or types into, or (combobox) needs the DOM tag to tell apart a native
# <select> from an editable combobox input.
_ROLE_WORD = {
    "button": "button", "link": "link", "checkbox": "checkbox", "radio": "option",
    "tab": "tab", "switch": "switch",
}
_TYPED_ROLES = {"textbox", "searchbox", "spinbutton"}
MAX_CONTROLS = 140
TEXT_CHARS = 1500


def _map_role(ax_role: str, tag: str):
    """(role word, typed, pick) for one AX role + DOM tag, or (None, False, False)
    when it is not a control we show. A <select> is its own case: Chrome calls it a
    combobox too, but it opens a native list rather than taking typed text."""
    if ax_role in _ROLE_WORD:
        return _ROLE_WORD[ax_role], False, False
    if ax_role == "searchbox":
        return "search field", True, False
    if ax_role in _TYPED_ROLES:
        return "field", True, False
    if ax_role == "combobox":
        if tag == "select":
            return "menu", False, True
        return "field", True, False
    return None, False, False


def _visible_part(r, win):
    """The part of r inside win, or None when under half of it (or a sliver) shows.
    Mirrors savta.actions.guide._visible_part exactly, copied rather than imported so
    this file does not pull in guide.py's own screen/apps modules just for one
    15-line geometry helper."""
    MIN_RING = 6.0
    if not r or not win:
        return None
    x, y, w, h = r
    wx, wy, ww, wh = win
    if w < MIN_RING or h < MIN_RING:
        return None
    lx, ty = max(x, wx), max(y, wy)
    rx, by = min(x + w, wx + ww), min(y + h, wy + wh)
    if rx - lx < MIN_RING or by - ty < MIN_RING:
        return None
    if (rx - lx) * (by - ty) < 0.5 * w * h:
        return None
    return (lx, ty, rx - lx, by - ty)


# ---------------------------------------------------------------- serving the demo
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler, but silent: a guide run should not spam the test log
    with one line per request."""

    def log_message(self, fmt, *args):  # noqa: A003
        pass


def serve(root=DEMO_DIR):
    """A ThreadingHTTPServer on 127.0.0.1, on a free port the OS picks for us (never
    8799, the owner's running app, and never 8851, the rehearsal server). Returns
    (base_url, stop_fn)."""
    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def stop_fn():
        httpd.shutdown()
        httpd.server_close()

    return f"http://127.0.0.1:{port}", stop_fn


# ---------------------------------------------------------------- the JS run on each
# control's own element, via CDP Runtime.callFunctionOn(objectId=<that element>).
# `this` is the element. It tags itself (so a later Playwright locator can find it
# again) and reads back everything the DOM knows that the AX tree does not: its tag,
# input type, current value, the selected option's words, whether it is checked or
# disabled, its box, and whether it has the keyboard right now.
_TAG_AND_READ_JS = """function(attempt) {
    this.setAttribute('data-mm-ref', String(attempt));
    var r = this.getBoundingClientRect();
    var tag = (this.tagName || '').toLowerCase();
    var type = (this.type || '').toLowerCase();
    var selectedText = '';
    if (tag === 'select' && this.options && this.selectedIndex >= 0) {
        var opt = this.options[this.selectedIndex];
        selectedText = opt ? opt.textContent.trim() : '';
    }
    return {
        tag: tag, type: type,
        value: (typeof this.value === 'string') ? this.value : '',
        placeholder: this.placeholder || '',
        checked: (typeof this.checked === 'boolean') ? this.checked : null,
        disabled: !!this.disabled,
        rect: {x: r.x, y: r.y, w: r.width, h: r.height},
        selectedText: selectedText,
        focused: (document.activeElement === this),
    };
}"""

# page.evaluate(_PROBE_JS, selector): what the watcher compares tick to tick. Takes
# the CSS selector straight (not an id) since the id a caller passes us is the public,
# renumbered-per-snapshot one, not the data-mm-ref value the element was tagged with.
_PROBE_JS = """(selector) => {
    const out = {title: document.title, url: location.href, structCount: 0,
                 structH1: '', found: false, connected: false, value: null,
                 checked: null, rect: null, focused: false};
    const m = document.querySelector('main') || document.body;
    out.structCount = m ? m.children.length : 0;
    const h1 = document.querySelector('h1');
    out.structH1 = h1 ? h1.textContent.trim() : '';
    const a = document.activeElement;
    out.focusKey = a ? [a.tagName, a.id || '', (a.textContent || '').trim().slice(0, 40)].join('|') : '';
    out.scrollY = window.scrollY;
    if (selector) {
        const el = document.querySelector(selector);
        if (el) {
            out.found = true;
            out.connected = el.isConnected;
            const tag = el.tagName.toLowerCase();
            if (tag === 'select') {
                const opt = el.options[el.selectedIndex];
                out.value = opt ? opt.textContent.trim() : '';
            } else if (typeof el.value === 'string') {
                out.value = el.value;
            }
            if (typeof el.checked === 'boolean') out.checked = el.checked;
            const r = el.getBoundingClientRect();
            out.rect = {x: r.x, y: r.y, w: r.width, h: r.height};
            out.focused = document.activeElement === el;
        }
    }
    return out;
}"""

# read_document(): the visible words in reading order and every link with its address.
_DOC_JS = """() => {
    const texts = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
        const t = n.textContent.replace(/\\s+/g, ' ').trim();
        const el = n.parentElement;
        if (!t || !el || el.closest('script,style,[hidden]')) continue;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        texts.push(t);
    }
    const links = Array.from(document.querySelectorAll('a[href]')).map(
        a => [(a.getAttribute('aria-label') || a.textContent || '').replace(/\\s+/g, ' ').trim(), a.href]);
    return {text: texts.join(' | ').slice(0, 6000), links};
}"""

_CLEAR_TAGS_JS = (
    "() => { document.querySelectorAll('[data-mm-ref]')"
    ".forEach(e => e.removeAttribute('data-mm-ref')); }"
)


class LarklineScreen:
    """The same seam as guide.LiveScreen (front, snapshot, screenshot, probe, press,
    fill, find), plus a few "a person did this" helpers (user_click, user_type,
    user_tab, user_select, user_check), against a headless Chromium page instead of a
    real window. One dedicated worker thread owns Playwright; every public method
    marshals its work onto it and blocks for the result, so it is safe to call from
    several threads at once (the Guide's watcher calls probe() on its own timer)."""

    def __init__(self, base_url: str, width: int = 1440, height: int = 900):
        self._base_url = base_url.rstrip("/")
        self._width, self._height = width, height
        self._tasks: "queue.Queue" = queue.Queue()
        self._stop = False
        self._worker = threading.Thread(target=self._worker_main, daemon=True)
        self._worker.start()
        self._run(self._start_impl)   # blocks until Chromium + the page are up

    # -------------------------------------------------------- the worker thread
    def _worker_main(self):
        while True:
            func, fut = self._tasks.get()
            try:
                result = func()
            except Exception as e:  # noqa: BLE001
                if not fut.cancelled():
                    fut.set_exception(e)
            else:
                if not fut.cancelled():
                    fut.set_result(result)
            if self._stop:
                return

    def _run(self, func, timeout: float = 20.0):
        """Queue `func` (no arguments) onto the worker thread and block for its
        result. Safe to call from any thread."""
        fut: "concurrent.futures.Future" = concurrent.futures.Future()
        self._tasks.put((func, fut))
        return fut.result(timeout=timeout)

    def _start_impl(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._page = self._browser.new_page(
            viewport={"width": self._width, "height": self._height})
        # The site's own view-enter transition animates opacity 0 -> 1 on every step
        # change (css/app.css .view-enter); a snapshot taken mid-fade finds the whole
        # step marked not-rendered in Chrome's AX tree. The page's own stylesheet
        # already honours prefers-reduced-motion for exactly this reason, so ask for
        # it rather than adding an artificial sleep after every navigation.
        self._page.emulate_media(reduced_motion="reduce")
        self._cdp = self._page.context.new_cdp_session(self._page)
        return True

    def close(self):
        def _do():
            try:
                self._browser.close()
            finally:
                self._pw.stop()
            self._stop = True
            return True
        self._run(_do)
        self._worker.join(timeout=5.0)

    # -------------------------------------------------------- navigation
    def goto(self, path: str):
        def _do():
            url = self._base_url + (path if path.startswith("/") else "/" + path)
            self._page.goto(url, wait_until="load")
            self._page.wait_for_selector("h1", timeout=5000)
            return True
        return self._run(_do)

    # -------------------------------------------------------- the seam
    def front(self) -> dict | None:
        return self._run(lambda: {
            "app": "Google Chrome", "pid": 4242, "bundle_id": "",
            "window": self._page.title()})

    def snapshot(self, front: dict) -> dict:
        return self._run(lambda: self._snapshot_impl(front))

    def screenshot(self, pid: int) -> bytes | None:
        return self._run(lambda: self._page.screenshot(type="png"))

    def probe(self, snap: dict, target_id) -> dict:
        return self._run(lambda: self._probe_impl(snap, target_id))

    def press(self, snap: dict, target_id) -> tuple[bool, str]:
        return self._run(lambda: self._press_impl(snap, target_id))

    def fill(self, snap: dict, target_id, value: str, private: bool = False) -> tuple[bool, str]:
        return self._run(lambda: self._fill_impl(snap, target_id, value, private))

    def page_url(self, front: dict) -> str:
        return self._run(lambda: self._page.url)

    def scroll(self, snap: dict, dy: int = 450) -> bool:
        """MicMic scrolls her page (guide autofill's look step), like LiveScreen.scroll."""
        self.user_scroll(dy)
        return True

    def read_document(self, front: dict) -> dict:
        """The page's words and its links, like LiveScreen.read_document reads a window:
        {"text", "links": [(label, url)], "error"}."""
        def _do():
            got = self._page.evaluate(_DOC_JS)
            return {"text": got["text"], "links": [tuple(x) for x in got["links"]], "error": None}
        return self._run(_do)

    def open(self, url: str):
        """A link opened in her browser (mac.open_url in the app): this page goes there."""
        def _do():
            self._page.goto(url, wait_until="load")
            return True
        return self._run(_do)

    def find(self, front: dict, role: str, label: str):
        def _do():
            snap = self._snapshot_impl(front)
            for c in snap.get("controls") or []:
                if c["role"] == role and c["label"] == label:
                    return snap, c["id"]
            return snap, None
        return self._run(_do)

    # -------------------------------------------------------- "a person did this"
    def user_click(self, snap: dict, target_id):
        return self._run(lambda: self._locator(snap, target_id).click())

    def user_type(self, snap: dict, target_id, text: str, delay_ms: int = 40):
        def _do():
            loc = self._locator(snap, target_id)
            loc.click()
            self._page.keyboard.type(str(text), delay=delay_ms)
        return self._run(_do)

    def user_tab(self):
        """She leaves the field: Tab, never Enter."""
        return self._run(lambda: self._page.keyboard.press("Tab"))

    def user_select(self, snap: dict, target_id, option_label: str):
        return self._run(
            lambda: self._locator(snap, target_id).select_option(label=option_label))

    def user_check(self, snap: dict, target_id):
        return self._run(lambda: self._locator(snap, target_id).check())

    def user_scroll(self, dy: int = 500):
        """She scrolls the page down (up for a negative dy) with the mouse wheel."""
        def _do():
            self._page.mouse.move(self._width / 2, self._height / 2)
            self._page.mouse.wheel(0, dy)
            self._page.wait_for_timeout(250)
        return self._run(_do)

    # -------------------------------------------------------- impl, worker thread only
    def _locator(self, snap: dict, target_id):
        selector = (snap.get("refs") or {}).get(target_id)
        if not selector:
            raise ValueError(f"no ref for id {target_id!r}")
        return self._page.locator(selector)

    def _snapshot_impl(self, front: dict) -> dict:
        page = self._page
        win = (0, 0, self._width, self._height)
        out = {"app": front.get("app", ""), "pid": front.get("pid"),
               "window": page.title(), "url": page.url, "title": page.title(),
               "controls": [], "refs": {}, "raw": {}, "text": "", "win": win,
               "secure_focus": False, "browser": True, "error": None}
        page.evaluate(_CLEAR_TAGS_JS)
        try:
            tree = self._cdp.send("Accessibility.getFullAXTree", {})
        except Exception as e:  # noqa: BLE001
            out["error"] = repr(e)[:200]
            return out

        texts: list[str] = []
        # The nearest named group above each node (a fieldset's legend), like the
        # "group" LiveScreen passes down its walk.
        by_id = {n.get("nodeId"): n for n in tree.get("nodes") or []}

        def group_of(node):
            seen_ids = set()
            p = by_id.get(node.get("parentId"))
            while p is not None and p.get("nodeId") not in seen_ids:
                seen_ids.add(p.get("nodeId"))
                prole = (p.get("role") or {}).get("value", "")
                pname = ((p.get("name") or {}).get("value", "") or "").strip()
                # a named section (a region) is an AXGroup to macOS too
                if prole in ("group", "radiogroup", "region") and pname and len(pname) <= 80:
                    return pname
                p = by_id.get(p.get("parentId"))
            return ""
        attempt = 0   # unique per node we resolve, used only for the data-mm-ref tag
        nid = 0       # public id, 1..N, only for controls we actually keep

        for node in tree.get("nodes") or []:
            if node.get("ignored"):
                continue
            role = (node.get("role") or {}).get("value", "")
            name = (node.get("name") or {}).get("value", "") or ""
            if role in ("StaticText", "heading") and sum(map(len, texts)) < TEXT_CHARS:
                t = name.strip()
                if t and (not texts or texts[-1] != t):
                    texts.append(t[:160])
                continue
            role_word_guess, _, _ = _map_role(role, "")
            if role_word_guess is None and role != "combobox":
                continue
            backend_id = node.get("backendDOMNodeId")
            if backend_id is None or len(out["controls"]) >= MAX_CONTROLS:
                continue
            try:
                resolved = self._cdp.send("DOM.resolveNode", {"backendNodeId": backend_id})
                object_id = resolved["object"]["objectId"]
            except Exception:  # noqa: BLE001
                continue
            attempt += 1
            try:
                res = self._cdp.send("Runtime.callFunctionOn", {
                    "objectId": object_id, "functionDeclaration": _TAG_AND_READ_JS,
                    "arguments": [{"value": attempt}], "returnByValue": True})
                self._cdp.send("Runtime.releaseObject", {"objectId": object_id})
            except Exception:  # noqa: BLE001
                continue
            info = (res.get("result") or {}).get("value")
            if not isinstance(info, dict) or info.get("disabled"):
                continue

            tag = info.get("tag", "")
            role_word, typed, pick = _map_role(role, tag)
            if role_word is None:
                continue
            label = name.strip()
            desc = (node.get("description") or {}).get("value", "") or ""
            hint = ""
            for h in (desc.strip(), (info.get("placeholder") or "").strip()):
                if h and h != label:
                    hint = h[:90]
                    break
            if not label and not typed:
                continue

            raw_value = info.get("value") or ""
            secure = (info.get("type", "") == "password") \
                or bool(FORBIDDEN_FIELD.search(label)) \
                or bool(FORBIDDEN_FIELD.search(hint)) \
                or bool(FORBIDDEN_FIELD.search(raw_value))
            if pick and secure and _private_field(info.get("type", "") != "password", label, hint,
                                                  group_of(node)):
                secure = False                # as LiveScreen: a country list is no secret
            if pick:
                value = "" if secure else (info.get("selectedText") or "")
            elif typed:
                value = "" if secure else raw_value
            else:
                value = ""

            checked = None
            if role_word in ("checkbox", "option", "switch"):
                checked = bool(info.get("checked"))

            rect = info.get("rect") or {}
            raw_frame = (rect.get("x", 0.0), rect.get("y", 0.0),
                         rect.get("w", 0.0), rect.get("h", 0.0))
            vis = _visible_part(raw_frame, win)
            if not vis:
                continue

            nid += 1
            group = (lambda g: g if g != label else "")(group_of(node))
            out["controls"].append({
                "id": nid, "role": role_word, "ax_role": role, "label": label[:90],
                "value": value[:40] if isinstance(value, str) else value,
                "hint": hint, "group": group,
                "checked": checked, "frame": vis, "secure": secure,
                "private": _private_field(secure and info.get("type", "") != "password",
                                          label, hint, group),
                "typed": typed, "pick": pick})
            out["refs"][nid] = f'[data-mm-ref="{attempt}"]'
            # What probe() will read in this field (its length only for a secret one):
            # the guide's watcher starts from it, as from LiveScreen's "raw".
            if typed or pick:
                out["raw"][nid] = ((len(raw_value) if secure else raw_value) if typed
                                   else (info.get("selectedText") or ""))
                if secure and typed:
                    out["controls"][-1]["filled"] = bool(raw_value)

        out["text"] = " | ".join(texts)[:TEXT_CHARS]
        try:
            out["secure_focus"] = bool(page.evaluate(
                "() => { const el = document.activeElement; "
                "return !!el && el.tagName === 'INPUT' "
                "&& (el.type || '').toLowerCase() === 'password'; }"))
        except Exception:  # noqa: BLE001
            pass
        return out

    def _probe_impl(self, snap: dict, target_id) -> dict:
        selector = (snap.get("refs") or {}).get(target_id) if target_id is not None else None
        info = self._page.evaluate(_PROBE_JS, selector)
        p = {"pid": 4242, "title": info["title"], "url": info["url"],
             "struct": [info["structCount"], info["structH1"]], "alive": None,
             "state": None, "focused": False, "frame": None,
             "focus_key": info.get("focusKey"),
             "win": (0, 0, self._width, self._height)}
        if target_id is None:
            # Where the page's first control is, as LiveScreen reads it: she scrolled.
            p["anchor"] = round(-info.get("scrollY", 0))
            return p
        if not info["found"]:
            p["alive"] = False
            return p
        p["alive"] = bool(info["connected"])
        ctl = next((c for c in snap.get("controls") or [] if c["id"] == target_id), None)
        raw_value = info.get("value")
        checked = info.get("checked")
        if ctl is not None and ctl.get("role") in ("checkbox", "option", "switch"):
            value, selected = (1 if checked else 0), bool(checked)
        elif ctl is not None and ctl.get("pick"):
            value, selected = (raw_value if isinstance(raw_value, str) else ""), None
        elif ctl is not None and ctl.get("typed"):
            value = raw_value if isinstance(raw_value, str) else ""
            if ctl.get("secure"):
                value = len(value) if isinstance(value, str) else None
            selected = None
        else:
            value, selected = raw_value, None
        p["state"] = (value, selected, None)
        p["focused"] = bool(info.get("focused"))
        r = info.get("rect")
        if r:
            p["frame"] = (r["x"], r["y"], r["w"], r["h"])
        return p

    def _press_impl(self, snap: dict, target_id) -> tuple[bool, str]:
        selector = (snap.get("refs") or {}).get(target_id)
        if not selector:
            return False, "stale"
        loc = self._page.locator(selector)
        if loc.count() == 0:
            return False, "stale"
        try:
            loc.click()
        except Exception as e:  # noqa: BLE001
            return False, repr(e)[:100]
        return True, "ok"

    def _fill_impl(self, snap: dict, target_id, value: str, private: bool = False) -> tuple[bool, str]:
        ctl = next((c for c in snap.get("controls") or [] if c["id"] == target_id), None)
        selector = (snap.get("refs") or {}).get(target_id)
        if ctl is None or not selector:
            return False, "stale"
        if (ctl.get("secure") and not (private and ctl.get("private"))) or not ctl.get("typed"):
            return False, "refused"
        value = re.sub(r"[\r\n]+", " ", str(value or "")).strip()
        loc = self._page.locator(selector)
        if loc.count() == 0:
            return False, "stale"
        try:
            loc.fill(value)
            loc.dispatch_event("change")
        except Exception as e:  # noqa: BLE001
            return False, repr(e)[:100]
        try:
            got = loc.evaluate("el => (typeof el.value === 'string') ? el.value : ''")
        except Exception:  # noqa: BLE001
            got = None
        if isinstance(got, str) and got.strip().lower() == value.lower():
            return True, "ok"
        return False, "not taken"


# ======================================================================= self-test
PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


def until(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def _wait_for_step(screen, snap, url_suffix, h1_text, timeout=5.0):
    """Wait for both the hash route and the rendered H1 to match: the URL alone can
    update a tick before render() has replaced #main's content (probe()'s own
    struct[1] is that H1), and taking a snapshot in that gap would read the old
    page's controls under the new URL."""
    def _ready():
        p = screen.probe(snap, None)
        return p["url"].endswith(url_suffix) and p["struct"][1] == h1_text
    return until(_ready, timeout)


def _find(snap, label, role=None):
    for c in snap["controls"] or []:
        if c["label"] == label and (role is None or c["role"] == role):
            return c
    return None


def _print_controls(snap, heading):
    print(f"---- {heading} ----")
    for c in snap["controls"]:
        print(f'{c["id"]:>3} {c["role"]:<10} label={c["label"]!r:<40} '
              f'hint={c["hint"]!r:<45} value={c["value"]!r}')


def main() -> int:
    base_url, stop_server = serve()
    # Tall enough that every step used below shows whole without scrolling: the
    # trip and documents steps run to ~2900px (itinerary card, pax list, the full
    # documents form). A real guide run scrolls instead; this just keeps the
    # self-test's control lists from depending on scroll position.
    screen = LarklineScreen(base_url, height=2200)
    try:
        # ---- home / check-in form -------------------------------------------
        # The full 8-step flow (?flow=full): the site's default became the 4-step one
        # on 2026-10-11, and this self-test walks the trip, travelers and documents steps.
        screen.goto("/?reset=1&flow=full")
        screen.goto("/#/check-in")
        front = screen.front()
        check("front() names Chrome", front is not None and front["app"] == "Google Chrome",
              str(front))

        snap = screen.snapshot(front)
        _print_controls(snap, "check-in form controls")

        conf = _find(snap, "Confirmation number")
        last = _find(snap, "Last name")
        btn = _find(snap, "Find my trip")
        check("confirmation number field found", conf is not None)
        check("last name field found", last is not None)
        check("Find my trip button found", btn is not None)
        for nm, c in (("confirmation", conf), ("last name", last), ("find my trip", btn)):
            sane = bool(c and c["frame"] and c["frame"][2] > 0 and c["frame"][3] > 0)
            check(f"{nm} frame is sane", sane, str(c["frame"]) if c else "missing")

        if conf and last and btn:
            screen.user_type(snap, conf["id"], "K7QM2P")
            p = screen.probe(snap, conf["id"])
            check("confirmation number typed and focused",
                  p["state"][0] == "K7QM2P" and p["focused"], str(p))

            screen.user_type(snap, last["id"], "Rivera")
            p = screen.probe(snap, last["id"])
            check("last name typed and focused",
                  p["state"][0] == "Rivera" and p["focused"], str(p))

            screen.press(snap, btn["id"])
            ok = _wait_for_step(screen, snap, "#/check-in/trip",
                                 "Your trip is ready for check-in")
            check("navigated to the trip step", ok, screen.probe(snap, None)["url"])

        # ---- trip: tick Alex, continue ---------------------------------------
        snap2 = screen.snapshot(screen.front())
        pax = _find(snap2, "Check in Alex Rivera")
        cont = _find(snap2, "Check in selected traveler")
        check("'Check in Alex Rivera' checkbox found", pax is not None)
        check("trip continue button found", cont is not None)
        if pax and cont:
            screen.user_check(snap2, pax["id"])
            p = screen.probe(snap2, pax["id"])
            check("checkbox reads ticked", p["state"] == (1, True, None), str(p))
            screen.press(snap2, cont["id"])
            ok = _wait_for_step(screen, snap2, "#/check-in/travelers",
                                 "Confirm traveler details")
            check("navigated to the travelers step", ok, screen.probe(snap2, None)["url"])

        # ---- travelers: continue with the prefilled defaults ------------------
        snap3 = screen.snapshot(screen.front())
        cont2 = _find(snap3, "Continue")
        check("travelers continue button found", cont2 is not None)
        if cont2:
            screen.press(snap3, cont2["id"])
            ok = _wait_for_step(screen, snap3, "#/check-in/documents",
                                 "Add your travel documents")
            check("navigated to the documents step", ok, screen.probe(snap3, None)["url"])

        # ---- documents -----------------------------------------------------
        snap4 = screen.snapshot(screen.front())
        _print_controls(snap4, "documents page controls")

        passport = _find(snap4, "Passport number")
        check("passport number field found", passport is not None)
        check("passport number hint reads correctly",
              passport is not None
              and passport["hint"] == "6 to 9 letters and numbers, no spaces.",
              str(passport))
        check("passport number is marked secure (FORBIDDEN_FIELD matches the label)",
              passport is not None and passport["secure"] is True, str(passport))

        nationality = _find(snap4, "Nationality")
        check("nationality is a pick menu", nationality is not None
              and nationality["role"] == "menu" and nationality["pick"] is True,
              str(nationality))

        # ---- probe() from a different thread, to prove the marshalling works --
        box: dict = {}

        def _other_thread():
            tid = passport["id"] if passport else None
            box["probe"] = screen.probe(snap4, tid)
            box["thread_name"] = threading.current_thread().name

        th = threading.Thread(target=_other_thread, name="not-the-main-thread")
        th.start()
        th.join(timeout=5.0)
        check("probe() answered when called from another thread",
              "probe" in box and box["probe"].get("alive") is not None, str(box))
    finally:
        screen.close()
        stop_server()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
