"""Reading what is on screen, the local capture layer only.

No network call lives in this file, and none may be added to it: the model call that
turns "what do I have on my screen?" into an answer belongs to whoever wires this
module into the router, not to this module. What this file does is the boring, load
bearing part underneath that: find the frontmost app without touching the request
thread's stale view of the world, read its text through Accessibility the same way
apps.py already reads controls, and never once let a password or a card number ride
along in the result.

Three things this file leans on, deliberately, instead of re-deriving them:

  * `apps._ax()` for the accessibility bindings and the AX messaging timeout, exactly
    the mechanism apps.py already uses to keep a beachballing app from blocking a step.
  * `apps.find_window()` for "which of this app's windows is the real one", the same
    three-fallback rule apps.snapshot() already applies.
  * `apps.is_forbidden()` / `web.FORBIDDEN_FIELD` for "is this a password or a card
    field", so there is exactly one list of forbidden patterns in the whole project,
    not a second one copied in here.
"""
from __future__ import annotations
import os
import re
import subprocess
import tempfile
import time
import unicodedata

from . import apps, mac
from .apps import AX_TIMEOUT, _attr, _label, is_forbidden

# lsappinfo, screencapture and sips are all instant on a live Mac; a hung one is the
# one case worth bounding for, the same reasoning apps.py applies to AX calls.
_CMD_TIMEOUT = 3.0

BROWSER_NAMES = {
    "safari", "safari technology preview", "google chrome", "google chrome canary",
    "chromium", "microsoft edge", "brave browser", "arc", "opera", "vivaldi",
}

# Roles worth reading for a screen summary: anything that carries text a person
# would actually read, plus the interactive controls whose label is itself content
# (a link's title, a checkbox's caption). Groups, splitters and images are not — a
# window is mostly those and none of them is something to read aloud.
TEXT_ROLES = {
    "AXStaticText", "AXTextField", "AXTextArea", "AXTextView", "AXValueIndicator",
    "AXLink", "AXButton", "AXCheckBox", "AXRadioButton", "AXMenuItem", "AXCell",
    "AXHeading", "AXComboBox", "AXSearchField", "AXPopUpButton", "AXMenuButton",
}
# AXStaticText too: its text is its AXValue, and reading it through a label cut every
# paragraph at 90 characters.
VALUE_BEARING = {"AXTextField", "AXTextArea", "AXTextView", "AXValueIndicator",
                 "AXComboBox", "AXSearchField", "AXStaticText", "AXHeading"}

_WS_RE = re.compile(r"\s+")


def _clean(text) -> str:
    """Strip invisible format characters (Cf) and collapse whitespace.

    Reuses mac._clean()'s Cf-stripping — the same fix WhatsApp's own self-reported
    name needed (it carries a leading U+200E) — rather than writing that loop twice.
    Hebrew, Arabic and other RTL text is untouched: those characters are letters, not
    format controls, and category Cf never matches them.
    """
    if not isinstance(text, str) or not text:
        return ""
    # U+200D (zero-width joiner) is Cf too, but it is what holds a family or a
    # profession emoji together; stripping it split one emoji into three.
    kept = "".join(ch for ch in text
                   if ch == "\u200d" or unicodedata.category(ch) != "Cf")
    return _WS_RE.sub(" ", kept).strip()


def _dedupe(parts: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = p.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _finalize_text(parts: list[str], max_chars: int, truncated: bool) -> tuple[str, bool]:
    """Join, de-duplicate and cap the collected pieces. Pulled out of visible_text()
    so the truncation arithmetic has a unit test that needs no window or display."""
    text = " | ".join(_dedupe(parts))
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return text, truncated


def _is_secure(role: str, subrole: str) -> bool:
    """A password field's AXRole is "AXTextField" — the ordinary one. What actually
    marks it is AXSubrole == "AXSecureTextField". Measured against a real
    NSSecureTextField while building the harness: checking AXRole alone (which is
    what apps.py's own snapshot() did before this file needed the same check) never
    matches a real secure field at all, and its AXValue comes back as masked
    private-use glyphs rather than an AXError, so a role-only check would silently
    hand those glyphs back as "not secure" instead of refusing them outright."""
    return role == "AXSecureTextField" or subrole == "AXSecureTextField"


def _forbidden(role: str, subrole: str, label: str, value: str) -> bool:
    """One check, reused everywhere in this file: apps.is_forbidden() already knows
    the difference between a secure element and a label/value match against
    web.FORBIDDEN_FIELD — asking it directly keeps this file from holding a second
    opinion about what counts as a password or a card field.

    Matched on cleaned text: "Pass\u200bword" slipped past the pattern and then came
    out as "Password". And a long value is judged by its label only: one word like
    "expire" or "swift" in a letter withheld the whole letter. Secrets inside long
    text are redact()'s job, which hides the secret and keeps the letter."""
    label, value = _clean(label), _clean(value)
    if len(value) > 120:
        value = ""
    return is_forbidden({"secure": _is_secure(role, subrole), "role": role,
                          "label": label, "value": value})


# The window's own buttons. Their help text ("this button also has an action to zoom
# the window") is not what is on her screen, and it was read out loud.
_CHROME = {"AXCloseButton", "AXMinimizeButton", "AXZoomButton", "AXFullScreenButton",
           "AXToolbarButton"}


def _screen_label(ax, el) -> str:
    """The name a person would read next to this element. A field whose label is a
    separate element points at it with AXTitleUIElement; that label is the one that
    says "Card number", so it is followed first. AXHelp is tooltip text, never shown."""
    linked = _attr(ax, el, "AXTitleUIElement")
    if linked is not None:
        for a in ("AXValue", "AXTitle", "AXDescription"):
            v = _attr(ax, linked, a)
            if isinstance(v, str) and v.strip():
                return v.strip()[:90]
    for a in ("AXTitle", "AXDescription", "AXPlaceholderValue"):
        v = _attr(ax, el, a)
        if isinstance(v, str) and v.strip():
            return v.strip()[:90]
    return ""


def _screen_recording_granted() -> bool:
    try:
        import Quartz
        # Preflight only: it reads the current grant and never puts up a prompt.
        # CGRequestScreenCaptureAccess() would, and requirement 4 is explicit that
        # this function must never trigger one on its own.
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        return False


def permissions() -> dict:
    ax = apps._ax()
    accessibility = bool(ax and ax["trusted"]())
    return {"accessibility": accessibility, "screen_recording": _screen_recording_granted()}


def _lsappinfo(args: list[str]) -> str:
    try:
        p = subprocess.run(["lsappinfo", *args], capture_output=True, text=True,
                           timeout=_CMD_TIMEOUT)
        return p.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


_INFO_LINE = re.compile(r'^\s*"?([A-Za-z]+)"?\s*=\s*(.*?)\s*$')


def _lsappinfo_info(asn: str) -> dict:
    out = {}
    for line in _lsappinfo(["info", "-only", "name,pid,bundleID", asn]).splitlines():
        m = _INFO_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip('"')
        if val in ("(null)", "[ NULL ]", ""):
            val = ""
        out[key] = val
    return out


def frontmost() -> dict:
    """The app in front right now, read live.

    `lsappinfo front` / `info` rather than NSWorkspace.frontmostApplication(): the
    same staleness apps.running_apps() documents applies here too, and this is the
    one call the whole module builds on, so it has to be the live one.
    """
    out = {"app": "", "pid": 0, "bundle_id": "", "window": ""}
    asn = _lsappinfo(["front"]).strip()
    if not asn:
        return out
    info = _lsappinfo_info(asn)
    out["app"] = _clean(info.get("LSDisplayName", ""))
    try:
        out["pid"] = int(info.get("pid") or 0)
    except ValueError:
        out["pid"] = 0
    out["bundle_id"] = info.get("CFBundleIdentifier", "")
    if out["pid"]:
        out["window"] = _clean(_window_title(out["pid"]))
    return out


def _app_root(pid: int):
    """An AX handle on `pid`'s application element, messaging-timeout already set.
    Returns (ax, root) or (None, None) when accessibility is not usable at all."""
    ax = apps._ax()
    if ax is None or not pid:
        return None, None
    root = ax["app"](pid)
    if ax.get("timeout"):
        try:
            ax["timeout"](root, AX_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass
    _wake_electron(ax, root, pid)
    return ax, root


# Electron and Chromium apps (VS Code, Slack, WhatsApp, Notion, Discord) build their
# accessibility tree only when an assistive technology asks for it, and otherwise
# show about seven empty elements: measured on VS Code, 0 characters of text. Setting
# AXManualAccessibility is the documented Electron switch; other apps refuse the
# attribute and nothing changes. Asked once per process, and the first read after it
# waits a moment because the tree is built asynchronously.
_WOKEN: set[int] = set()
MAX_DEPTH = 60


def _wake_electron(ax, root, pid: int) -> None:
    if pid in _WOKEN or not ax.get("set"):
        return
    _WOKEN.add(pid)
    try:
        err = ax["set"](root, "AXManualAccessibility", True)
    except Exception:  # noqa: BLE001
        return
    if err == 0:
        time.sleep(0.35)


def _window_title(pid: int) -> str:
    ax, root = _app_root(pid)
    if ax is None:
        return ""
    window = apps.find_window(ax, root)
    if window is None:
        return ""
    return str(_attr(ax, window, "AXTitle") or "")


def selected_text() -> str:
    """The text selected in the frontmost app's focused control, or "" when there is
    none, the element cannot be read, or the field is one of the forbidden ones."""
    return _selected_text_for(frontmost())


def _selected_text_for(fm: dict) -> str:
    ax, root = _app_root(fm["pid"])
    if ax is None or root is None:
        return ""
    focused = _attr(ax, root, "AXFocusedUIElement")
    if focused is None:
        return ""
    role = str(_attr(ax, focused, "AXRole") or "")
    subrole = str(_attr(ax, focused, "AXSubrole") or "")
    if _is_secure(role, subrole):
        return ""
    label = _screen_label(ax, focused)
    sel = _attr(ax, focused, "AXSelectedText")
    text = sel if isinstance(sel, str) else ""
    if _forbidden(role, subrole, label, text):
        return ""
    return _clean(text)


def focused_text() -> dict:
    """The control that has keyboard focus in the frontmost app.

    `secure` is true whenever the value is withheld, whether that is because the
    role is AXSecureTextField or because the label/value matched a forbidden
    pattern (a "Card number" AXTextField is not literally secure, but its content
    must be treated exactly as if it were).
    """
    return _focused_text_for(frontmost())


def _focused_text_for(fm: dict) -> dict:
    out = {"role": "", "value": "", "secure": False}
    ax, root = _app_root(fm["pid"])
    if ax is None or root is None:
        return out
    focused = _attr(ax, root, "AXFocusedUIElement")
    if focused is None:
        return out
    role = str(_attr(ax, focused, "AXRole") or "")
    subrole = str(_attr(ax, focused, "AXSubrole") or "")
    out["role"] = role
    label = _screen_label(ax, focused)
    raw = _attr(ax, focused, "AXValue")
    value = raw if isinstance(raw, str) else (str(raw) if isinstance(raw, (int, float)) else "")
    if _is_secure(role, subrole) or _forbidden(role, subrole, label, value):
        out["secure"] = True
        out["value"] = ""
        return out
    out["value"] = _clean(value)
    return out


def visible_text(max_chars: int = 6000, max_elements: int = 2500,
                 budget_s: float = 1.5) -> dict:
    """A flattened read of everything on screen in the frontmost window, bounded on
    every axis that can otherwise run away: wall clock, element count, and output
    size. Reports `truncated` honestly rather than quietly cutting content."""
    return _visible_text_for(frontmost(), max_chars, max_elements, budget_s)


def _visible_text_for(fm: dict, max_chars: int, max_elements: int,
                      budget_s: float) -> dict:
    out = {"app": "", "window": "", "text": "", "truncated": False, "elements": 0}
    out["app"] = fm["app"]
    ax, root = _app_root(fm["pid"])
    if ax is None or root is None:
        return out
    window = apps.find_window(ax, root)
    if window is None:
        return out
    out["window"] = _clean(str(_attr(ax, window, "AXTitle") or ""))

    started = time.time()
    parts: list[str] = []
    count = 0
    truncated = False
    too_deep = False      # text below MAX_DEPTH exists and was not read: say so

    def walk(el, depth: int):
        nonlocal count, truncated, too_deep
        # Deep on purpose: Electron and Chrome wrap text in 20 to 40 nested groups,
        # and a cap of 14 read 7 empty elements out of VS Code. The element count
        # and the wall clock are what bound the walk, not the depth.
        if truncated:
            return
        if depth > MAX_DEPTH:
            too_deep = too_deep or bool(_attr(ax, el, "AXChildren"))
            return
        for kid in (_attr(ax, el, "AXChildren") or []):
            if truncated:
                return
            if count >= max_elements or (time.time() - started) > budget_s:
                truncated = True
                return
            count += 1
            role = str(_attr(ax, kid, "AXRole") or "")
            subrole = str(_attr(ax, kid, "AXSubrole") or "") if role in TEXT_ROLES else ""
            if role in TEXT_ROLES and subrole not in _CHROME:
                label = _screen_label(ax, kid)
                value = ""
                # Never even read AXValue for a secure element — the OS masks it
                # with private-use placeholder glyphs rather than the real text, but
                # there is no reason to touch it at all once the subrole says not to.
                if role in VALUE_BEARING and not _is_secure(role, subrole):
                    raw = _attr(ax, kid, "AXValue")
                    if isinstance(raw, str):
                        value = raw
                    elif isinstance(raw, (int, float)):
                        value = str(raw)
                # Withhold the whole entry, label included, rather than surface a
                # "Card number" label next to nothing: it is a harmless label on its
                # own, but treating it as safe to show is exactly the judgement call
                # that must never be delegated to a label-matching regex under load.
                if not (_is_secure(role, subrole) or _forbidden(role, subrole, label, value)):
                    lab, val = _clean(label), _clean(value)
                    # A static text's label is often its own value cut short; say it once.
                    if lab and val and (val.startswith(lab) or lab.startswith(val)):
                        lab = "" if len(val) >= len(lab) else lab
                        val = val if lab == "" else ""
                    for c in (lab, val):
                        if c:
                            parts.append(c)
            walk(kid, depth + 1)

    walk(window, 0)
    out["elements"] = count
    out["text"], out["truncated"] = _finalize_text(parts, max_chars, truncated or too_deep)
    return out


def browser_page() -> dict | None:
    """{"url","title"} when the frontmost app is a browser and Accessibility alone
    can see its web area, else None.

    Caveat for whoever wires this up: Safari and the Chromium-family browsers
    (Chrome, Edge, Brave, Arc, Opera, Vivaldi) expose AXURL on their AXWebArea and
    are the ones this was written against. Firefox's Gecko accessibility tree does
    not reliably expose the same attribute through pure AX, so a Firefox frontmost
    app will usually come back None here rather than lie with a stale or empty URL.
    """
    return _browser_page_for(frontmost())


def _browser_page_for(fm: dict) -> dict | None:
    if fm["app"].strip().lower() not in BROWSER_NAMES:
        return None
    ax, root = _app_root(fm["pid"])
    if ax is None or root is None:
        return None
    window = apps.find_window(ax, root)
    if window is None:
        return None

    started = time.time()

    def find_web_area(el, depth: int):
        if depth > 10 or (time.time() - started) > 1.0:
            return None
        if str(_attr(ax, el, "AXRole") or "") == "AXWebArea":
            return el
        for kid in (_attr(ax, el, "AXChildren") or []):
            found = find_web_area(kid, depth + 1)
            if found is not None:
                return found
        return None

    web_area = find_web_area(window, 0)
    if web_area is None:
        return None
    url = _attr(ax, web_area, "AXURL")
    if url is None:
        return None
    # AXURL comes back as an NSURL/CFURL object, not a plain string.
    url_s = str(url.absoluteString()) if hasattr(url, "absoluteString") else str(url)
    title = str(_attr(ax, web_area, "AXTitle") or "") or str(_attr(ax, window, "AXTitle") or "")
    return {"url": _clean(url_s), "title": _clean(title)}


def _front_window_id(pid: int) -> int | None:
    """The on-screen window of `pid` nearest the front, as a CGWindowID."""
    try:
        import Quartz
        rows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
    except Exception:  # noqa: BLE001
        return None
    for w in rows:                      # already ordered front to back
        if int(w.get("kCGWindowOwnerPID", -1)) == pid and int(w.get("kCGWindowLayer", 1)) == 0:
            return int(w.get("kCGWindowNumber"))
    return None


def screenshot_png(max_px: int = 1600, pid: int | None = None) -> bytes | None:
    """PNG bytes of `pid`'s front window, or of the whole screen when no pid is
    given; None when Screen Recording is not granted or that window cannot be found.

    With a pid it never falls back to the whole screen: the whole screen carries
    other apps and notifications, which is more than "this" ever meant.

    Never attempts `screencapture` without the grant: without it the tool silently
    returns just the desktop wallpaper, which is worse than admitting it cannot see
    the screen, and it never puts up a permission prompt of its own.
    """
    if not _screen_recording_granted():
        return None
    # NamedTemporaryFile's own context manager cleans this up on the way out. That
    # is deliberate, not a style choice: rule 2 for this project is that there is no
    # delete path in savta/ anywhere, at all, and a regression test greps the whole
    # package for the handful of calls that remove a file or a row. The file this
    # function creates never held anything but its own scratch screenshot, but the
    # rule reads the source, not the intent, so the cleanup has to happen inside the
    # standard library's own teardown rather than a call written out here.
    try:
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            path = tmp.name
            if pid:
                wid = _front_window_id(pid)
                if wid is None:
                    return None
                cmd = ["screencapture", "-x", "-o", "-l", str(wid), path]
            else:
                cmd = ["screencapture", "-x", "-C", path]
            subprocess.run(cmd, timeout=6, capture_output=True)
            if os.path.getsize(path) == 0:
                return None
            try:
                subprocess.run(["sips", "-Z", str(max_px), path], timeout=6,
                               capture_output=True)
            except Exception:  # noqa: BLE001
                pass  # a full-size PNG beats none; downscaling is a nicety, not a must
            with open(path, "rb") as fh:
                return fh.read()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- redaction
# A secret field is withheld by its role or its label. A secret that is just text on
# the page is not: "Your code is 482913" in a message, a card number in a receipt, an
# API key in a terminal. Measured by the adversarial lane: all of those reached the
# model prompt, and "read me the screen" spoke them aloud. So everything this module
# returns passes through redact() once, here, before any caller can see it: the
# prompt, the spoken reading and "send this" all get the same cleaned text.
HIDDEN = "[hidden]"
_CODE_WORDS = (r"code|otp|pin|passcode|password|verification|security|one[- ]time|"
               r"קוד|סיסמ|אימות|код|пароль|пин|رمز|كود|كلمة السر|التحقق")
_CODE_NEAR = re.compile(
    rf"((?:{_CODE_WORDS})[^\d\n]{{0,40}}?)(?<![\d-])(\d(?:[ ]?\d){{3,7}})(?![\d:/-])(?!\.\d)",
    re.IGNORECASE)
_CODE_BEFORE = re.compile(
    rf"(?<![\d:/.])(\d{{4,8}})(?![\d:/-])(?!\.\d)([^\d\n]{{0,30}}?(?:{_CODE_WORDS}))",
    re.IGNORECASE)
_CVV = re.compile(
    r"((?:cvv|cvc|cvn|csc|security code|קוד אבטחה|3 ספרות|код безопасности|رمز الأمان)"
    r"[^\d\n]{0,15}?)(?<!\d)(\d{3,4})(?!\d)", re.IGNORECASE)
_CARD = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")
_KEY = re.compile(
    r"\b(?:sk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})")
_URL_SECRET = re.compile(
    r"([?&#](?:token|access_token|id_token|code|key|api_key|apikey|auth|sig|signature|"
    r"session|sessionid|password|pwd|otp|secret)=)[^&#\s]+", re.IGNORECASE)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1
    except ValueError:
        return False


def redact(text: str) -> str:
    """The same text with codes, card numbers, IBANs, keys and URL secrets hidden.
    Checksums (Luhn, IBAN mod 97) keep ordinary long numbers such as order ids and
    phone numbers readable; a short number is hidden only next to a word like "code"."""
    if not text:
        return text or ""
    t = _KEY.sub(HIDDEN, text)
    t = _URL_SECRET.sub(lambda m: m.group(1) + HIDDEN, t)
    t = _CARD.sub(lambda m: HIDDEN if _luhn(re.sub(r"\D", "", m.group(0))) else m.group(0), t)
    t = _IBAN.sub(lambda m: HIDDEN if _iban_ok(m.group(0)) else m.group(0), t)
    t = _CVV.sub(lambda m: m.group(1) + HIDDEN, t)
    t = _CODE_NEAR.sub(lambda m: m.group(1) + HIDDEN, t)
    t = _CODE_BEFORE.sub(lambda m: HIDDEN + m.group(2), t)
    return t


def context(max_chars: int = 6000) -> dict:
    """Everything the model call needs in one shot, gathered locally.

    `has_image` reports whether Screen Recording is granted, i.e. whether
    screenshot_png() *could* produce bytes right now — it does not itself capture a
    screenshot. Capturing and downscaling a full screen is the most expensive call
    in this module; doing it on every context() call the router makes would be a
    latency tax paid whether or not the caller ends up wanting the image. Call
    screenshot_png() separately once the caller has decided it needs the bytes.

    `frontmost()` itself shells out to `lsappinfo` twice; every other function here
    calls it again internally when used on its own. Fetching it once and passing it
    to each function's private `_..._for(fm)` twin — rather than five independent
    `lsappinfo` round trips — is most of the difference between this and a naive
    sum of the public functions' own latencies.
    """
    fm = frontmost()
    focused = _focused_text_for(fm)
    focused["value"] = redact(focused.get("value", ""))
    visible = _visible_text_for(fm, max_chars, 2500, 1.5)
    visible["text"] = redact(visible.get("text", ""))
    page = _browser_page_for(fm)
    if page:
        page = {"url": redact(page.get("url", "")), "title": redact(page.get("title", ""))}
    fm = {**fm, "window": redact(fm.get("window", ""))}
    return {
        "permissions": permissions(),
        "frontmost": fm,
        "selected": redact(_selected_text_for(fm)),
        "focused": focused,
        "visible": visible,
        "page": page,
        "has_image": _screen_recording_granted(),
    }
