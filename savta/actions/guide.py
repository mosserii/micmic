"""Guide mode: MicMic walks her through a task on screen, one step at a time, while
she does each step herself.

She is on a website (a cloud console, a bank, a settings page) and wants to get
something done there that she does not know how to do. The browser agent (web.py)
would do it in a browser of its own; this does the opposite and stays beside her in
hers: it says the next thing to do in one short sentence, rings the control to press
on the real screen, waits while she does it, notices that she did, and says the next.

Where the cost goes, deliberately:
  * One Gemini call per step: a picture of the front window plus a short list of the
    controls in it (id, role, label), the goal, and the steps already given. It
    returns the sentence AND the id of the control to point at. The id must be one of
    the ids it was given; anything else means no ring, never a guessed one.
  * Nothing at all while she works: the watcher reads a handful of accessibility
    attributes every TICK seconds (the page's address, the window title, how many
    top-level things are on the page, and the target control itself) and only asks
    for the next step once the screen has changed and held still, or she says done.

The screen goes to the model only while a guide runs, and never while a password or
card field has the keyboard: then only the list of controls goes, without a picture.
Everything read off the screen passes through screen.redact() first.

Seams, so a test needs no screen, no model and no speaker: GUIDE.screen (front,
snapshot, screenshot, probe, press, find), GUIDE.model(request) -> dict, and
GUIDE.speaker(text, lang). The overlay is not here at all: native/guide_overlay.py
polls state() and draws the ring from state()["ring"].
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid

from . import apps, screen
from .web import FINAL_BUTTON

MAX_STEPS = 25
MAX_SECONDS = 20 * 60
TICK = 0.3
# The screen has to hold still this long after a change before the next step is
# worked out: a page that is still loading would otherwise be described half built.
SETTLE = 0.9
SETTLE_MAX = 4.0
# How long "Guide me" stays on offer after a how-to question was answered.
OFFER_TTL = 120.0
# How long a caller that asked out loud waits for the step it will speak.
WAIT_FOR_STEP = 30.0
MAX_CONTROLS = 140
WALK_NODES = 5000
WALK_SECONDS = 1.5
TEXT_CHARS = 1500
SHOT_PX = 1280
MIN_RING = 6.0

KINDS = ("click", "type", "secret", "look", "wait")
ENDED = ("done", "stopped", "failed")

# Roles a person presses or types into, as the model is told them.
ROLES = {
    "AXButton": "button", "AXLink": "link", "AXCheckBox": "checkbox",
    "AXRadioButton": "option", "AXMenuButton": "menu button", "AXPopUpButton": "menu",
    "AXMenuItem": "menu item", "AXDisclosureTriangle": "expander", "AXTab": "tab",
    "AXComboBox": "field", "AXTextField": "field", "AXTextArea": "text box",
    "AXSearchField": "search field", "AXRow": "row", "AXCell": "cell",
    "AXSlider": "slider", "AXIncrementor": "stepper", "AXSwitch": "switch",
}
TYPED = {"AXComboBox", "AXTextField", "AXTextArea", "AXSearchField"}
PRESSABLE = set(ROLES) - TYPED
_ATTRS = ["AXRole", "AXSubrole", "AXFrame", "AXTitle", "AXDescription", "AXValue",
          "AXEnabled", "AXChildren", "AXPlaceholderValue", "AXTitleUIElement"]
_PROBE = ["AXRole", "AXFrame", "AXValue", "AXSelected", "AXExpanded", "AXFocused"]

# A step whose whole point is a secret or a sign-in, a payment, or deleting something
# is never done for her, whatever the control is called.
CREDENTIAL = re.compile(
    r"api.?key|credential|\btoken|secret|password|passcode|\bsign.?in\b|\blog.?in\b|"
    r"two.?factor|2.?step|billing|payment|\bcard\b|סיסמ|התחבר|כניסה|תשלום|כרטיס|пароль|войти|оплат|карт|"
    r"كلمة|تسجيل الدخول|دفع|بطاقة", re.I)

# MicMic's own windows: "this" is never MicMic itself.
_MICMIC = ("micmic",)


# ---------------------------------------------------------------- what she hears
SAY = {
    "stopped": {"hebrew": "בסדר, הפסקתי להדריך.", "arabic": "ماشي، وقّفت الإرشاد.",
                "russian": "Хорошо, я остановила подсказки.",
                "english": "Alright, I have stopped guiding."},
    "blocked": {"hebrew": "אני עדיין לא יכולה לראות את המסך. בהגדרות המערכת, תחת פרטיות ואבטחה ואז נגישות, צריך להדליק את MicMic.",
                "arabic": "لسا ما بقدر أشوف الشاشة. بإعدادات النظام، الخصوصية والأمان ثم تسهيلات الاستخدام، شغّل«ي|» MicMic.",
                "russian": "Я пока не вижу экран. В Системных настройках, в разделе Конфиденциальность и безопасность, Универсальный доступ, включите MicMic.",
                "english": "I cannot see your screen yet. In System Settings, under Privacy and Security then Accessibility, turn MicMic on."},
    "no_window": {"hebrew": "אני לא רואה חלון פתוח שאפשר להדריך בו.",
                  "arabic": "ما بشوف شباك مفتوح بقدر ساعد فيه.",
                  "russian": "Я не вижу открытого окна, в котором можно подсказать.",
                  "english": "I cannot see an open window to guide you through."},
    "no_llm": {"hebrew": "אני לא יכולה להדריך כרגע. אפשר לנסות שוב עוד מעט.",
               "arabic": "ما بقدر ساعد هلّق. جرّب«ي|» كمان شوي.",
               "russian": "Сейчас я не могу подсказывать. Попробуйте чуть позже.",
               "english": "I cannot guide you right now. Try again in a little while."},
    "too_long": {"hebrew": "עברנו הרבה שלבים, אז אני עוצרת כאן. אפשר לבקש שוב ונמשיך.",
                 "arabic": "مرقنا خطوات كتير، فبوقّف هون. اطلب«ي|» كمان مرة ومنكمّل.",
                 "russian": "Мы прошли много шагов, я остановлюсь здесь. Попросите ещё раз, и продолжим.",
                 "english": "We have been through a lot of steps, so I will stop here. Ask me again and we will carry on."},
    "finished": {"hebrew": "זהו, סיימנו.", "arabic": "خلص، خلّصنا.",
                 "russian": "Всё, готово.", "english": "That's done."},
    "no_back": {"hebrew": "זה השלב הראשון, אין לאן לחזור.",
                "arabic": "هاي أول خطوة، ما في قبلها.",
                "russian": "Это первый шаг, возвращаться некуда.",
                "english": "This is the first step, there is nothing to go back to."},
    "no_why": {"hebrew": "זה השלב הבא בדרך למה שביקשת.",
               "arabic": "هاي الخطوة الجاية للي طلبت«ي|».",
               "russian": "Это следующий шаг к тому, что вы просили.",
               "english": "It is the next step toward what you asked for."},
    "do_refused": {"hebrew": "את השלב הזה עדיף שתעש«י|» בעצמך.",
                   "arabic": "هالخطوة أحسن تعمل«ي|»ها إنت«ي|».",
                   "russian": "Этот шаг лучше сделать вам самим.",
                   "english": "This step is one you should do yourself."},
    "did_it": {"hebrew": "עשיתי את זה.", "arabic": "عملتها.",
               "russian": "Сделала.", "english": "Done that for you."},
    "not_guiding": {"hebrew": "אני לא מדריכה כרגע.", "arabic": "أنا مش عم ساعد هلّق.",
                    "russian": "Я сейчас ничего не подсказываю.",
                    "english": "I am not guiding you through anything right now."},
}


def _line(key: str, lang: str, gender: str = "") -> str:
    t = SAY[key]
    text = t.get(lang, t["english"])
    try:
        from ..router import degender
        return degender(text, gender)
    except Exception:  # noqa: BLE001
        return re.sub(r"«([^«»|]*)\|([^«»|]*)»", r"\1", text)


def _clean_sentence(text, limit: int = 220) -> str:
    """One line to show and say: no markdown, no em or en dashes (a comma reads the
    same aloud), no quotes the model wrapped around the whole thing."""
    s = str(text or "").strip()
    s = re.sub(r"[*_#`]+", "", s)
    s = re.sub(r"\s*[—–]\s*", ", ", s)
    s = re.sub(r"\s+", " ", s).strip().strip('"').strip()
    return s[:limit]


# ---------------------------------------------------------------- the real screen
def _val(v):
    """An attribute from a multiple-attribute read: a missing one comes back as an
    AXValue holding the error, which is None here."""
    try:
        from ApplicationServices import AXValueGetType, kAXValueAXErrorType
        if type(v).__name__.startswith("AXValue") and AXValueGetType(v) == kAXValueAXErrorType:
            return None
    except Exception:  # noqa: BLE001
        pass
    return v


def _rect(v):
    """(x, y, w, h) in the accessibility space (top-left of the main screen is 0,0,
    y grows downward), or None."""
    v = _val(v)
    if v is None:
        return None
    try:
        from ApplicationServices import AXValueGetValue, kAXValueCGRectType
        ok, r = AXValueGetValue(v, kAXValueCGRectType, None)
        if not ok:
            return None
        return (float(r.origin.x), float(r.origin.y), float(r.size.width), float(r.size.height))
    except Exception:  # noqa: BLE001
        return None


def _many(el, names):
    try:
        from ApplicationServices import AXUIElementCopyMultipleAttributeValues
        err, vals = AXUIElementCopyMultipleAttributeValues(el, names, 0, None)
        if err != 0 or vals is None:
            return None
        return [_val(v) for v in vals]
    except Exception:  # noqa: BLE001
        return None


def _visible_part(r, win):
    """The part of r inside the window, or None when too little of it shows to point
    at honestly (under half of it, or a sliver)."""
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


def _intersects(r, win) -> bool:
    if not r or not win or r[2] <= 0 or r[3] <= 0:
        return True           # a container with no size of its own may hold visible things
    return not (r[0] + r[2] <= win[0] or r[0] >= win[0] + win[2]
                or r[1] + r[3] <= win[1] or r[1] >= win[1] + win[3])


class LiveScreen:
    """The front window, read through Accessibility. Every method is cheap enough to
    call from the watcher except snapshot(), which is called once per step."""

    def front(self) -> dict | None:
        """The app she is working in: the frontmost one, or, when MicMic itself is in
        front (she clicked its window), the nearest ordinary window behind it."""
        fm = screen.frontmost()
        if fm.get("pid") and not _is_micmic(fm.get("app", ""), fm.get("bundle_id", "")):
            return fm
        pid = _front_window_pid(skip_names=_MICMIC)
        if not pid:
            return None
        name = ""
        for row in _window_rows():
            if int(row.get("kCGWindowOwnerPID", -1)) == pid:
                name = str(row.get("kCGWindowOwnerName") or "")
                break
        return {"app": name, "pid": pid, "bundle_id": "", "window": ""}

    def snapshot(self, front: dict) -> dict:
        """Every control a person could press or type into in the visible part of the
        front window, numbered, with where it is. Refs stay here, never in state."""
        out = {"app": front.get("app", ""), "pid": front.get("pid"), "window": "",
               "url": "", "title": "", "controls": [], "refs": {}, "text": "",
               "win": None, "secure_focus": False, "browser": False, "error": None}
        ax, root = screen._app_root(front.get("pid") or 0)
        if ax is None or root is None:
            out["error"] = "no_access"
            return out
        if not ax["trusted"]():
            out["error"] = "no_access"
            return out
        window = apps.find_window(ax, root)
        if window is None:
            out["error"] = "no_window"
            return out
        top = _many(window, ["AXTitle", "AXFrame"]) or [None, None]
        out["window"] = screen.redact(screen._clean(str(top[0] or "")))
        win = _rect(top[1])
        out["win"] = win
        app = front.get("app", "").strip().lower()
        out["browser"] = app in screen.BROWSER_NAMES or "chrom" in app
        out["_window"] = window
        # Whatever has the keyboard: a password or card field there means no picture.
        focused = apps._attr(ax, root, "AXFocusedUIElement")
        if focused is not None:
            f = _many(focused, ["AXRole", "AXSubrole", "AXTitle", "AXDescription",
                                "AXPlaceholderValue"]) or []
            f = (f + [None] * 5)[:5]
            role, sub = str(f[0] or ""), str(f[1] or "")
            label = " ".join(str(x) for x in f[2:] if isinstance(x, str))
            out["secure_focus"] = screen._is_secure(role, sub) or screen._forbidden(
                role, sub, label, "")

        started, seen = time.time(), 0
        texts: list[str] = []
        nid = 0

        def label_of(vals, el) -> str:
            role, title, desc, value, ph, tui = (vals[0], vals[3], vals[4], vals[5],
                                                 vals[8], vals[9])
            for v in (title, desc):
                if isinstance(v, str) and v.strip():
                    return v.strip()
            if tui is not None:
                t = _many(tui, ["AXValue", "AXTitle", "AXDescription"]) or []
                for v in t:
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            if isinstance(ph, str) and ph.strip():
                return ph.strip()
            if role not in TYPED and isinstance(value, str) and value.strip():
                return value.strip()
            # A link or a row whose words are its children's (Chrome, Finder's sidebar)
            return _child_text(el)

        def walk(el, depth):
            nonlocal seen, nid
            if depth > screen.MAX_DEPTH or seen >= WALK_NODES \
                    or time.time() - started > WALK_SECONDS:
                return
            vals = _many(el, _ATTRS)
            seen += 1
            if vals is None:
                return
            role, sub = str(vals[0] or ""), str(vals[1] or "")
            frame = _rect_from(vals[2])
            if not _intersects(frame, win):
                return
            if role == "AXWebArea" and "_web" not in out:
                out["_web"] = el
                url = apps._attr(ax, el, "AXURL")
                url_s = str(url.absoluteString()) if hasattr(url, "absoluteString") else str(url or "")
                out["url"] = screen.redact(screen._clean(url_s))
                out["title"] = screen.redact(screen._clean(str(vals[3] or "")))
            if role in ("AXStaticText", "AXHeading") and sum(map(len, texts)) < TEXT_CHARS:
                t = vals[5] if isinstance(vals[5], str) else vals[3]
                if isinstance(t, str) and t.strip() and not screen._forbidden(role, sub, "", t):
                    texts.append(screen._clean(t)[:160])
            kind_role = "AXTab" if sub == "AXTabButton" else ("AXSwitch" if sub == "AXSwitch" else role)
            if kind_role in ROLES and vals[6] is not False and len(out["controls"]) < MAX_CONTROLS:
                secure = screen._is_secure(role, sub)
                label = screen._clean(label_of(vals, el))[:90]
                value = ""
                if role in TYPED and not secure and isinstance(vals[5], str):
                    value = screen._clean(vals[5])[:40]
                forbidden = secure or screen._forbidden(role, sub, label, value)
                vis = _visible_part(frame, win) if frame else None
                if (label or role in TYPED) and vis:
                    nid += 1
                    out["controls"].append({
                        "id": nid, "role": ROLES[kind_role], "ax_role": role,
                        "label": screen.redact(label),
                        "value": "" if forbidden else screen.redact(value),
                        "frame": vis, "secure": bool(forbidden),
                        "typed": role in TYPED})
                    out["refs"][nid] = el
            kids = vals[7]
            if kids:
                for k in list(kids):
                    walk(k, depth + 1)

        walk(window, 0)
        out["text"] = screen.redact(" | ".join(screen._dedupe(texts)))[:TEXT_CHARS]
        return out

    def screenshot(self, pid: int) -> bytes | None:
        return screen.screenshot_png(max_px=SHOT_PX, pid=pid)

    def probe(self, snap: dict, target_id) -> dict:
        """What the watcher compares, tick to tick. No model, a few attribute reads."""
        p = {"pid": _front_window_pid(skip_names=_MICMIC), "title": None, "url": None,
             "struct": None, "alive": None, "state": None, "frame": None}
        window = snap.get("_window")
        if window is not None:
            w = _many(window, ["AXTitle", "AXChildren", "AXFrame"])
            if w is not None:
                p["title"] = w[0]
                p["struct"] = [len(w[1] or [])]
                p["win"] = _rect(w[2])
        web = snap.get("_web")
        if web is not None:
            w = _many(web, ["AXURL", "AXChildren", "AXTitle"])
            if w is None:
                p["url"] = "gone"
            else:
                url = w[0]
                p["url"] = str(url.absoluteString()) if hasattr(url, "absoluteString") else str(url)
                p["struct"] = (p["struct"] or []) + [len(w[1] or []), str(w[2] or "")]
        ref = (snap.get("refs") or {}).get(target_id) if target_id else None
        if ref is not None:
            t = _many(ref, _PROBE)
            if t is None or not t[0]:
                p["alive"] = False
            else:
                p["alive"] = True
                secure = any(c.get("id") == target_id and c.get("secure")
                             for c in snap.get("controls") or [])
                value = None if secure else (t[2] if isinstance(t[2], (str, int, float)) else None)
                p["state"] = (value, t[3], t[4])
                p["focused"] = bool(t[5])
                p["frame"] = _rect(t[1])
        return p

    def press(self, snap: dict, target_id) -> tuple[bool, str]:
        ref = (snap.get("refs") or {}).get(target_id)
        ax = apps._ax()
        if ref is None or ax is None:
            return False, "stale"
        try:
            err = ax["press"](ref, "AXPress")
        except Exception as e:  # noqa: BLE001
            return False, repr(e)[:100]
        return err == 0, "ok" if err == 0 else f"AX {err}"

    def find(self, front: dict, role: str, label: str):
        """A control with this role and label in a fresh read, for going back a step
        without asking the model again. Returns (snapshot, id) or (None, None)."""
        snap = self.snapshot(front)
        for c in snap.get("controls") or []:
            if c["role"] == role and c["label"] == label:
                return snap, c["id"]
        return snap, None


def _rect_from(v):
    return _rect(v) if v is not None else None


def _child_text(el, depth: int = 0) -> str:
    if depth > 3:
        return ""
    vals = _many(el, ["AXChildren"])
    for k in list((vals or [None])[0] or [])[:6]:
        v = _many(k, ["AXRole", "AXValue", "AXTitle", "AXDescription"])
        if not v:
            continue
        for x in v[1:]:
            if isinstance(x, str) and x.strip():
                return x.strip()
        t = _child_text(k, depth + 1)
        if t:
            return t
    return ""


def _is_micmic(app: str, bundle: str) -> bool:
    return (bundle or "").startswith("com.betterfly.micmic") or \
        (app or "").strip().lower() in _MICMIC


def _window_rows() -> list:
    try:
        import Quartz
        return list(Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or [])
    except Exception:  # noqa: BLE001
        return []


def _front_window_pid(skip_names=()) -> int | None:
    """The owner of the frontmost ordinary window, MicMic's own left out. In-process
    and about a millisecond, so the watcher can ask it every tick."""
    import os
    me = os.getpid()
    for w in _window_rows():                    # front to back
        if int(w.get("kCGWindowLayer", 1)) != 0:
            continue
        pid = int(w.get("kCGWindowOwnerPID", -1))
        name = str(w.get("kCGWindowOwnerName") or "").strip().lower()
        if pid == me or name in skip_names:
            continue
        b = w.get("kCGWindowBounds") or {}
        if float(b.get("Width", 0)) < 80 or float(b.get("Height", 0)) < 60:
            continue
        return pid
    return None


# ---------------------------------------------------------------- the model
_LANG_NAME = {"hebrew": "Hebrew", "arabic": "Arabic", "russian": "Russian",
              "english": "English"}

SYSTEM = (
    "You are MicMic, guiding a person through a task on their Mac one step at a time, "
    "while they do every step themselves. You are shown the window in front of them "
    "(a screenshot when there is one, and always a numbered list of the controls in "
    "it), the goal, and the steps already given.\n"
    "Reply with JSON only, no other words:\n"
    '{{"done": true or false, "say": "...", "target": id or null, "kind": "click" | '
    '"type" | "secret" | "look" | "wait", "why": "...", "goal": "...", "finish": "..."}}\n'
    "- say: the ONE next thing to do, one short sentence in {language}, at most 16 "
    "words, naming the control exactly as it is labelled on screen and roughly where "
    "it is, for example: Click APIs and Services in the menu on the left.\n"
    "- target: the id of that control from the list, or null when no single listed "
    "control is the place. Only ids from the list. Never guess one.\n"
    "- kind: click to press something; type to type ordinary text into a field (say "
    "what to type when it is known); secret when the step is typing a password, a "
    "card number, a security code or signing in: tell them to type it themselves and "
    "never ask them for it; look when the thing is not visible yet and they should "
    "scroll or look somewhere; wait when the page is still loading.\n"
    "- why: one short sentence in {language} on why this step matters.\n"
    "- goal: the goal restated in two to six words in {language}.\n"
    "- done: true only when the screen shows the goal is achieved; then finish is one "
    "short sentence in {language} saying what was achieved, for example: That's done: "
    "the Maps API is enabled. Otherwise finish is empty.\n"
    "One step only, never two joined together. If the last step did not happen yet, "
    "say it again, more clearly. Plain words: no markdown, no lists, no em dashes.{address}")


def build_request(goal: str, lang: str, history: list[str], snap: dict,
                  reason: str) -> str:
    """The user part of the step call, as text. Pure, so tests can read it."""
    win = snap.get("win") or (0, 0, 1, 1)

    def where(fr):
        x = (fr[0] + fr[2] / 2 - win[0]) / max(1.0, win[2])
        y = (fr[1] + fr[3] / 2 - win[1]) / max(1.0, win[3])
        return f"({round(x * 100)}%,{round(y * 100)}%)"

    lines = [f"Goal: {goal}"]
    if history:
        lines.append("Steps already given, oldest first:")
        lines += [f"{i}. {h}" for i, h in enumerate(history, 1)]
    else:
        lines.append("No steps given yet: this is the first.")
    lines.append(f"Why you are asked now: {reason}.")
    lines.append(f"App: {snap.get('app', '')}   Window: {snap.get('window', '')}")
    if snap.get("url"):
        lines.append(f"Web page: {snap.get('title', '')} ({snap.get('url', '')})")
    lines.append("Controls (id, kind, label, where across and down the window):")
    for c in snap.get("controls") or []:
        extra = " [secret field]" if c.get("secure") else (
            f' now says "{c["value"]}"' if c.get("value") else "")
        lines.append(f'{c["id"]} {c["role"]} "{c["label"]}"{extra} {where(c["frame"])}')
    if not snap.get("controls"):
        lines.append("(none could be read)")
    if snap.get("text"):
        lines.append("Text on screen: " + snap["text"])
    return "\n".join(lines)


def gemini_step(llm, req: dict) -> dict | None:
    """One call: the picture, the controls and the goal in, the next step out."""
    if llm is None or not llm.available:
        return None
    lang = req.get("lang", "english")
    from ..llm import LLM
    system = SYSTEM.format(language=_LANG_NAME.get(lang, "English"),
                           address=LLM._address(lang, req.get("gender", "")))
    parts = [{"text": req["prompt"]}]
    if req.get("image"):
        parts.insert(0, {"inlineData": {"mimeType": "image/png",
                                        "data": base64.b64encode(req["image"]).decode()}})
    d = llm.generate_content([{"role": "user", "parts": parts}], timeout=30.0,
                             system=system,
                             generation_config={"maxOutputTokens": 400, "temperature": 0.1,
                                                "responseMimeType": "application/json"})
    if not d:
        return None
    try:
        raw = "".join(p.get("text", "") for p in d["candidates"][0]["content"].get("parts", []))
    except Exception:  # noqa: BLE001
        return None
    return parse_step(raw)


def parse_step(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


# ---------------------------------------------------------------- the session
class Guide:
    def __init__(self):
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._s: dict | None = None
        self._offer: dict | None = None
        self.screen = LiveScreen()
        self.model = None           # callable(request dict) -> dict | None
        self.speaker = None         # callable(text, lang)
        self.llm = None             # made on first use: a connection of its own
        self.tick = TICK
        self.settle = SETTLE

    # ------------------------------------------------ what the UI and overlay read
    def state(self) -> dict:
        with self._lock:
            s = self._s
            out = {"active": bool(s and s["status"] not in ENDED), "offer": self._live_offer()}
            if s is None:
                out["status"] = "idle"
                return out
            for k in ("id", "status", "goal", "goal_label", "n", "say", "why", "kind",
                      "can_do", "ring", "line", "lang", "rev", "target_label", "why_shown"):
                out[k] = s.get(k)
            return out

    def active(self) -> bool:
        with self._lock:
            return bool(self._s and self._s["status"] not in ENDED)

    def context(self) -> dict | None:
        """For understand(): what a short "next" or "why" is about."""
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return None
            return {"goal": s.get("goal_label") or s["goal"], "current_step": s.get("say") or ""}

    # ------------------------------------------------ the offer behind "Guide me"
    def offer(self, goal: str, lang: str, speak: bool = True, gender: str = "") -> dict:
        with self._lock:
            self._offer = {"id": uuid.uuid4().hex[:10], "goal": goal[:300], "lang": lang,
                           "speak": speak, "gender": gender, "at": time.time()}
            return {"id": self._offer["id"], "label": _chip_label(lang)}

    def clear_offer(self) -> None:
        with self._lock:
            self._offer = None

    def _live_offer(self) -> dict | None:
        o = self._offer
        if not o or time.time() - o["at"] > OFFER_TTL:
            return None
        if self._s and self._s["status"] not in ENDED:
            return None
        return {"id": o["id"], "at": o["at"], "goal": o["goal"]}

    def take_offer(self, offer_id: str | None = None) -> dict | None:
        with self._lock:
            o = self._offer
            if not o or time.time() - o["at"] > OFFER_TTL:
                return None
            if offer_id and offer_id != o["id"]:
                return None
            self._offer = None
            return o

    # ------------------------------------------------ starting and stopping
    def start(self, goal: str, lang: str = "english", speak: bool = True, gender: str = "",
              wait: bool = False) -> dict:
        """Begin guiding toward `goal`. wait=True: the caller will say the first step
        itself, so this returns once it is known and does not speak it."""
        with self._lock:
            if self._s and self._s["status"] not in ENDED:
                self._s["status"] = "stopped"
                self._s["ring"] = None
            sid = uuid.uuid4().hex[:10]
            self._s = {"id": sid, "goal": goal[:300], "goal_label": "", "lang": lang,
                       "gender": gender, "speak": speak, "status": "thinking", "n": 0,
                       "say": "", "why": "", "kind": "", "can_do": False, "ring": None,
                       "line": "", "rev": 0, "target": None, "target_label": "",
                       "history": [], "started": time.time(), "pending": "start",
                       "quiet": wait, "snap": None, "computed": 0, "why_shown": False}
            self._offer = None
            # A client of its own per guide: credentials may have changed since the
            # last one (savta.account swaps them), and a step never queues behind an
            # answer she is waiting for on the router's connection.
            self.llm = None
            self._cv.notify_all()
        threading.Thread(target=self._run, args=(sid,), daemon=True,
                         name="micmic-guide").start()
        if wait:
            return self._wait_for(sid, 0)
        return self.state()

    def stop(self) -> dict:
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            s["status"], s["ring"], s["can_do"] = "stopped", None, False
            s["line"] = _line("stopped", s["lang"], s["gender"])
            s["rev"] += 1
            self._cv.notify_all()
            return {**self.state(), "said": s["line"]}

    def next_step(self, wait: bool = False) -> dict:
        """She says she did it (the Done button, or "next")."""
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            done_before = s["computed"]
            if s["status"] != "thinking":
                s["pending"] = "done"
                s["quiet"] = wait
                self._cv.notify_all()
            sid = s["id"]
        if wait:
            return self._wait_for(sid, done_before)
        return self.state()

    def explain(self) -> dict:
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            s["why_shown"] = True
            s["rev"] += 1
            return {**self.state(), "said": s.get("why") or _line("no_why", s["lang"], s["gender"])}

    def repeat(self) -> dict:
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            return {**self.state(), "said": s.get("say") or ""}

    def back(self) -> dict:
        """The step before this one, again. No model call: the words are kept, and the
        control is looked for again by its role and label in a fresh read."""
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            if len(s["history"]) < 2:
                return {**self.state(), "said": _line("no_back", s["lang"], s["gender"])}
            s["history"].pop()                     # this step
            prev = s["history"][-1]
            steps = s.get("steps") or []
            prev_step = steps[-2] if len(steps) >= 2 else {"say": prev}
            s["steps"] = steps[:-1]
        snap, tid = (None, None)
        if prev_step.get("role") and prev_step.get("label"):
            try:
                front = self.screen.front()
                if front:
                    snap, tid = self.screen.find(front, prev_step["role"], prev_step["label"])
            except Exception:  # noqa: BLE001
                snap, tid = None, None
        with self._lock:
            if not self._s or self._s is not s or s["status"] in ENDED:
                return self.state()
            s["n"] = max(1, s["n"] - 1)
            s["say"], s["why"] = prev_step.get("say", prev), prev_step.get("why", "")
            s["kind"] = prev_step.get("kind", "")
            s["target"] = tid
            s["target_label"] = prev_step.get("label", "") if tid else ""
            s["ring"] = _ring(snap, tid) if snap else None
            s["can_do"] = self._can_do(snap, tid, s["kind"], s["say"]) if snap else False
            if snap:
                s["snap"] = snap
            s["why_shown"] = False
            s["rev"] += 1
            s["rebase"] = True
            self._cv.notify_all()
            return {**self.state(), "said": s["say"]}

    def do_it(self) -> dict:
        """Press the ringed control for her, when that is safe to do."""
        with self._lock:
            s = self._s
            if not s or s["status"] != "step":
                return {"ok": False, **self.state()}
            snap, tid = s.get("snap"), s.get("target")
            if not s.get("can_do") or not snap or not tid:
                return {"ok": False, "said": _line("do_refused", s["lang"], s["gender"]),
                        **self.state()}
        ok, why = self.screen.press(snap, tid)
        return {"ok": ok, "why": why, **self.state()}

    # ------------------------------------------------ the thread
    def _wait_for(self, sid: str, computed_before: int) -> dict:
        deadline = time.time() + WAIT_FOR_STEP
        with self._lock:
            while True:
                s = self._s
                if not s or s["id"] != sid or s["status"] in ENDED:
                    break
                if s["computed"] > computed_before and s["status"] != "thinking":
                    break
                left = deadline - time.time()
                if left <= 0:
                    break
                self._cv.wait(min(left, 0.5))
            st = self.state()
            s = self._s
            if s and s["id"] == sid:
                st["said"] = s["line"] if s["status"] in ENDED else s["say"]
            return st

    def _alive(self, sid: str) -> dict | None:
        s = self._s
        return s if (s and s["id"] == sid and s["status"] not in ENDED) else None

    def _run(self, sid: str) -> None:
        while True:
            with self._lock:
                s = self._alive(sid)
                if s is None:
                    return
                reason = s.pop("pending", None)
            if reason:
                self._compute(sid, {"start": "starting the guide",
                                    "done": "she says she did the last step",
                                    "change": "the screen changed after the last step"}.get(
                                        reason, reason))
                continue
            got = self._watch(sid)
            if got:
                with self._lock:
                    s = self._alive(sid)
                    if s is not None and not s.get("pending"):
                        s["pending"] = got

    def _watch(self, sid: str) -> str | None:
        """Wait, cheaply, for her to do the step. Returns "done", "change", or None
        when the session ended."""
        base = None
        changed_at = None
        last = None
        while True:
            with self._lock:
                s = self._alive(sid)
                if s is None:
                    return None
                if s.get("pending"):
                    return None if s["pending"] is None else s.pop("pending")
                if time.time() - s["started"] > MAX_SECONDS:
                    self._end(s, "failed", _line("too_long", s["lang"], s["gender"]), speak=True)
                    return None
                snap, target, kind = s.get("snap"), s.get("target"), s.get("kind")
                if s.pop("rebase", False):
                    base = None
                self._cv.wait(self.tick)
                if s.get("pending"):
                    continue
            if snap is None:
                continue
            try:
                p = self.screen.probe(snap, target)
            except Exception:  # noqa: BLE001
                continue
            if base is None:
                early = s.pop("early", None)
                base = dict(p)
                if early:
                    for k in ("pid", "title", "url", "struct"):
                        base[k] = early.get(k)
                last = base
                continue
            # The ring follows the control: a scroll or a resized window moves it.
            self._follow(sid, snap, target, p)
            if not _changed(base, p, kind):
                changed_at = None
                last = p
                continue
            now = time.time()
            if changed_at is None:
                changed_at = now
            elif _same(last, p) and now - changed_at >= self.settle:
                return "change"
            elif now - changed_at >= SETTLE_MAX:
                return "change"
            last = p

    def _follow(self, sid: str, snap: dict, target, p: dict) -> None:
        if not target:
            return
        win = p.get("win") or snap.get("win")
        ring = _visible_part(p.get("frame"), win) if p.get("alive") else None
        ring = _as_ring(ring)
        with self._lock:
            s = self._alive(sid)
            if s is None or s["status"] != "step" or s.get("target") != target:
                return
            if ring != s["ring"]:
                s["ring"] = ring
                s["rev"] += 1

    def _compute(self, sid: str, reason: str) -> None:
        with self._lock:
            s = self._alive(sid)
            if s is None:
                return
            if s["n"] >= MAX_STEPS:
                self._end(s, "failed", _line("too_long", s["lang"], s["gender"]), speak=True)
                return
            s["status"], s["ring"], s["can_do"], s["why_shown"] = "thinking", None, False, False
            s["rev"] += 1
            goal, lang, gender = s["goal"], s["lang"], s["gender"]
            history = list(s["history"])
        try:
            front = self.screen.front()
        except Exception:  # noqa: BLE001
            front = None
        if not front:
            return self._fail(sid, "no_window")
        snap = self.screen.snapshot(front)
        if snap.get("error") == "no_access":
            return self._fail(sid, "blocked")
        if snap.get("error"):
            return self._fail(sid, "no_window")
        # The page as it was when it was read: if she moves on while the model is
        # still thinking, the watcher sees that at once instead of waiting on a page
        # that already changed.
        try:
            early = self.screen.probe(snap, None)
        except Exception:  # noqa: BLE001
            early = None
        # A password or card field has the keyboard: the list of controls only.
        image = None if snap.get("secure_focus") else self._shot(front)
        req = {"goal": goal, "lang": lang, "gender": gender, "history": history,
               "reason": reason, "image": image,
               "prompt": build_request(goal, lang, history, snap, reason),
               "ids": [c["id"] for c in snap.get("controls") or []]}
        try:
            res = (self.model or self._gemini)(req)
        except Exception:  # noqa: BLE001
            res = None
        with self._lock:
            s = self._alive(sid)
            if s is None:
                return                       # stopped while the model was thinking
        if not isinstance(res, dict):
            return self._fail(sid, "no_llm")
        self._apply(sid, snap, res, early)

    def _apply(self, sid: str, snap: dict, res: dict, early: dict | None = None) -> None:
        say = _clean_sentence(res.get("say"))
        done = bool(res.get("done"))
        with self._lock:
            s = self._alive(sid)
            if s is None:
                return
            if res.get("goal") and not s["goal_label"]:
                s["goal_label"] = _clean_sentence(res.get("goal"), 80)
            if done:
                line = _clean_sentence(res.get("finish")) or _line("finished", s["lang"], s["gender"])
                self._end(s, "done", line, speak=not s.get("quiet"))
                return
            if not say:
                self._end(s, "failed", _line("no_llm", s["lang"], s["gender"]),
                          speak=not s.get("quiet"))
                return
            tid = res.get("target")
            try:
                tid = int(tid) if tid is not None and str(tid).strip() != "" else None
            except (TypeError, ValueError):
                tid = None
            ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
            if ctl is None:
                tid = None                   # not one of the ids it was given: no ring
            kind = res.get("kind") if res.get("kind") in KINDS else ("click" if ctl else "look")
            if ctl and ctl.get("secure"):
                kind = "secret"
            same = (say == s.get("say") and tid == s.get("target"))
            if not same:
                s["n"] += 1
                s["history"].append(say)
                s.setdefault("steps", []).append(
                    {"say": say, "why": _clean_sentence(res.get("why")), "kind": kind,
                     "role": ctl["role"] if ctl else "", "label": ctl["label"] if ctl else ""})
            s["say"], s["why"], s["kind"] = say, _clean_sentence(res.get("why")), kind
            s["target"], s["target_label"] = tid, (ctl or {}).get("label", "")
            s["snap"] = snap
            s["early"] = early
            s["ring"] = _ring(snap, tid)
            s["can_do"] = self._can_do(snap, tid, kind, say)
            s["status"] = "step"
            s["computed"] += 1
            s["rev"] += 1
            quiet = s.pop("quiet", False)
            s["quiet"] = False
            speak = s["speak"] and not quiet and not same
            lang = s["lang"]
            self._cv.notify_all()
        if speak:
            self._say(say, lang)

    @staticmethod
    def _can_do(snap: dict | None, tid, kind: str, say: str) -> bool:
        """"Do it for me" only for a plain click on a web page, never a secret, a
        sign-in, a payment, a key or anything that deletes."""
        if not snap or not tid or kind != "click" or not snap.get("browser"):
            return False
        ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
        if ctl is None or ctl.get("secure") or ctl.get("typed"):
            return False
        text = f"{ctl.get('label', '')} {say}"
        if CREDENTIAL.search(text) or FINAL_BUTTON.search(text) or apps.DESTRUCTIVE.search(text):
            return False
        return not apps.is_forbidden({"label": ctl.get("label", ""), "value": ""})

    def _fail(self, sid: str, key: str) -> None:
        with self._lock:
            s = self._alive(sid)
            if s is not None:
                self._end(s, "failed", _line(key, s["lang"], s["gender"]),
                          speak=not s.get("quiet"))

    def _end(self, s: dict, status: str, line: str, speak: bool) -> None:
        s["status"], s["line"], s["ring"], s["can_do"] = status, line, None, False
        s["computed"] += 1
        s["rev"] += 1
        s["ended"] = time.time()
        self._cv.notify_all()
        if speak and s.get("speak") and line:
            threading.Thread(target=self._say, args=(line, s["lang"]), daemon=True).start()

    def _say(self, text: str, lang: str) -> None:
        try:
            if self.speaker is not None:
                self.speaker(text, lang)
            else:
                from . import mac
                mac.say(text, lang)
        except Exception:  # noqa: BLE001
            pass

    def _shot(self, front: dict) -> bytes | None:
        try:
            return self.screen.screenshot(front.get("pid"))
        except Exception:  # noqa: BLE001
            return None

    def _gemini(self, req: dict) -> dict | None:
        if self.llm is None:
            from ..llm import LLM
            self.llm = LLM()
        return gemini_step(self.llm, req)


def _as_ring(r):
    if not r:
        return None
    return {"x": round(r[0], 1), "y": round(r[1], 1), "w": round(r[2], 1), "h": round(r[3], 1)}


def _ring(snap: dict | None, tid):
    if not snap or not tid:
        return None
    ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
    if not ctl:
        return None
    return _as_ring(_visible_part(ctl.get("frame"), snap.get("win")))


def _changed(base: dict, p: dict, kind: str) -> bool:
    """Has she done the step? A page, a window or an app of its own is always a
    change. While she types, the words in the field and the suggestions under it are
    her typing, not the step being over: that ends when she leaves the field."""
    if p.get("pid") != base.get("pid") or p.get("title") != base.get("title") \
            or p.get("url") != base.get("url"):
        return True
    if base.get("alive") and p.get("alive") is False:
        return True
    if kind in ("type", "secret"):
        return bool(base.get("focused")) and p.get("focused") is False
    if p.get("struct") != base.get("struct"):
        return True
    if base.get("alive") and p.get("state") != base.get("state"):
        return True
    return bool(p.get("focused")) and not base.get("focused")


def _same(a: dict | None, b: dict) -> bool:
    if a is None:
        return False
    keys = ("pid", "title", "url", "struct", "alive", "state")
    return all(a.get(k) == b.get(k) for k in keys)


_CHIP = {"hebrew": "תדריכי אותי", "arabic": "أرشدني خطوة بخطوة",
         "russian": "Покажи по шагам", "english": "Guide me"}


def _chip_label(lang: str) -> str:
    return _CHIP.get(lang, _CHIP["english"])


GUIDE = Guide()
