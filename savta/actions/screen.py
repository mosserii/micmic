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

# What holds a window's document, as opposed to the chrome around it.
DOCUMENT_ROLES = {"AXWebArea", "AXTextArea"}

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


def _preflight_granted() -> bool:
    """The real reading from macOS. Only _screen_recording_granted calls it."""
    try:
        import Quartz
        # Preflight only: it reads the current grant and never puts up a prompt.
        # CGRequestScreenCaptureAccess() would, and requirement 4 is explicit that
        # this function must never trigger one on its own.
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        return False


def _screen_recording_granted() -> bool:
    """May MicMic capture the screen right now? Every capture path and permissions()
    asks this, so a test fake (_fake_screen_state) steers all of them the same way as
    screen_recording_state(): "granted" is True, anything else False, and the real
    preflight is never read while a fake is set."""
    fake = _fake_screen_state()
    if fake is not None:
        return fake == "granted"
    return _preflight_granted()


def permissions() -> dict:
    ax = apps._ax()
    accessibility = bool(ax and ax["trusted"]())
    return {"accessibility": accessibility, "screen_recording": _screen_recording_granted()}


# ---------------------------------------------------------------- Screen Recording state
# CGPreflightScreenCaptureAccess() only says yes or no. "No" covers two very different
# situations: macOS has never been asked (MicMic is not even in the Screen Recording
# list yet, so "turn MicMic on there" sends her looking for a row that does not exist),
# and she was asked and said no (or turned it on, and macOS wants MicMic reopened
# before it counts). macOS keeps the difference in a database MicMic cannot read
# without Full Disk Access, so MicMic keeps its own note: whoever asks (consent.py,
# lane L4, with CGRequestScreenCaptureAccess) calls note_screen_recording_asked()
# right after. Nothing in this file ever asks.
SCREEN_ASKED_MARKER = "screen_recording_asked"
SCREEN_STATES = ("granted", "not_asked", "denied")


def _screen_asked_marker():
    from ..paths import state
    return state(SCREEN_ASKED_MARKER)


def _fake_screen_state() -> str | None:
    """Tests: MICMIC_FAKE_SCREEN_RECORDING=granted|not_asked|denied, or a
    "screen_recording" key in MICMIC_FAKE_PERMISSIONS (the table onboarding_api.py
    fakes the other permissions with). None only when neither is set.

    Once either is set, nothing real is read or asked, the same promise
    onboarding_api.py makes for its table: a value that is not one of SCREEN_STATES,
    a table without a "screen_recording" key, or a table that is not JSON all read
    as "not_asked" (onboarding_api's default for a kind it was not given), never as
    a fall back to the real preflight or the real "asked" note."""
    raw = os.environ.get("MICMIC_FAKE_SCREEN_RECORDING", "").strip()
    if raw:
        return raw if raw in SCREEN_STATES else "not_asked"
    table = os.environ.get("MICMIC_FAKE_PERMISSIONS")
    if not table:
        return None
    try:
        import json
        d = json.loads(table)
        raw = str(d.get("screen_recording") or "") if isinstance(d, dict) else ""
    except ValueError:
        raw = ""
    return raw if raw in SCREEN_STATES else "not_asked"


def screen_recording_asked() -> bool:
    """Has MicMic ever put the Screen Recording question to macOS on this Mac?"""
    try:
        return _screen_asked_marker().exists()
    except OSError:
        return False


def note_screen_recording_asked() -> bool:
    """Called by whoever just asked (consent.ask_screen_recording, lane L4), right
    after CGRequestScreenCaptureAccess(). True when the note was kept. Never asks."""
    try:
        _screen_asked_marker().write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        return True
    except OSError:
        return False


def screen_recording_state() -> str:
    """One of "granted" | "not_asked" | "denied", read without ever putting up a prompt.

    "granted": MicMic can capture now.
    "not_asked": MicMic has never asked, so it is not in the Screen Recording list
        yet. The router hands this to the ask (MicMic's own line, then macOS's).
    "denied": asked before and still not on. Either she said no, or she turned it on
        and macOS wants MicMic reopened first (Screen Recording only counts after a
        relaunch). The honest line covers both; macOS will not prompt a second time.
    """
    fake = _fake_screen_state()
    if fake is not None:
        return fake
    if _screen_recording_granted():
        return "granted"
    return "denied" if screen_recording_asked() else "not_asked"


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
    out = {"app": "", "window": "", "text": "", "truncated": False, "elements": 0,
           "document": ""}
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
    # The text inside the window's document only: a mail's body, a note, a page, an
    # editor's text (an AXWebArea or AXTextArea and what is under it), without the
    # mailbox list, sidebars and toolbars around it. "Send this to Dana" with an email
    # open sends that, not every subject line in her inbox. Gathered on the same walk.
    doc_parts: list[str] = []
    count = 0
    truncated = False
    too_deep = False      # text below MAX_DEPTH exists and was not read: say so

    def walk(el, depth: int, in_doc: bool = False):
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
            doc_here = in_doc or role in DOCUMENT_ROLES
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
                            if doc_here:
                                doc_parts.append(c)
            walk(kid, depth + 1, doc_here)

    walk(window, 0)
    out["elements"] = count
    out["text"], out["truncated"] = _finalize_text(parts, max_chars, truncated or too_deep)
    out["document"] = _finalize_text(doc_parts, max_chars, False)[0]
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


DESKTOP = os.path.expanduser("~/Desktop")   # kept for anything that still names it; nothing here saves there

# Where MicMic keeps the pictures it takes: ~/Pictures/MicMic. Not the Desktop: the
# Desktop, Documents and Downloads are folders macOS guards with a prompt of its own
# ("MicMic would like to access files in your Desktop folder"), and a screenshot was
# the first thing to put that prompt up, with no line from MicMic before it. Pictures
# is not guarded (only the Photos library inside it is), so writing there, and
# reading the file back for the clipboard, asks nothing.
SHOTS_DIR = os.path.expanduser("~/Pictures/MicMic")
SHOTS_LABEL = "Pictures/MicMic"
_PROTECTED = ("~/Desktop", "~/Documents", "~/Downloads", "~/Library/Mobile Documents",
              "/Volumes")


def in_protected_folder(path: str) -> bool:
    """Is `path` inside a folder macOS guards with its own Files & Folders prompt?"""
    real = os.path.realpath(os.path.expanduser(path))
    for root in _PROTECTED:
        r = os.path.realpath(os.path.expanduser(root))
        if real == r or real.startswith(r + os.sep):
            return True
    return False


def shots_dir() -> str:
    """The folder screenshots go to, created if it is not there yet: ~/Pictures/MicMic
    (MICMIC_SHOTS_DIR overrides it, which is how a test keeps her real folders out of
    it). If that cannot be made, MicMic's own Application Support folder. Never a
    folder macOS guards (in_protected_folder): an override pointing at one is ignored
    rather than obeyed. Raises OSError only when not even the fallback can be made."""
    override = os.environ.get("MICMIC_SHOTS_DIR", "").strip()
    picks = []
    if override and not in_protected_folder(override):
        picks.append(os.path.expanduser(override))
    picks.append(SHOTS_DIR)
    for d in picks:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            continue
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    from ..paths import state_dir
    d = os.path.join(str(state_dir()), "Screenshots")
    os.makedirs(d, exist_ok=True)
    return d


def _shot_path(kind: str) -> str:
    """A name nothing is using yet: two shots in the same second must not land on
    one file (the second would overwrite the first, and a cancelled drag would read
    the earlier picture as its own)."""
    d = shots_dir()
    ts = time.strftime("%Y-%m-%d %H.%M.%S")
    path = os.path.join(d, f"MicMic {kind} {ts}.png")
    n = 2
    while os.path.exists(path) and n < 1000:
        path = os.path.join(d, f"MicMic {kind} {ts} ({n}).png")
        n += 1
    return path


def folder_label(path: str) -> str:
    """The folder `path` is in, as words to say out loud: "Pictures/MicMic" for the
    usual place, otherwise the folder relative to her home."""
    d = os.path.dirname(path)
    if os.path.realpath(d) == os.path.realpath(SHOTS_DIR):
        return SHOTS_LABEL
    home = os.path.expanduser("~")
    return d[len(home) + 1:] if d.startswith(home + os.sep) else d


def _to_clipboard(path: str) -> bool:
    """The picture itself onto the clipboard. mac.copy_file_to_clipboard first (the
    path every send already uses); if that fails, the clipboard directly from this
    process, which needs no osascript at all. True only when one of them worked."""
    try:
        if mac.copy_file_to_clipboard(path):
            return True
    except Exception:  # noqa: BLE001
        pass
    pb = _pasteboard()
    if pb is None:
        return False
    try:
        from Foundation import NSData
        data = NSData.dataWithContentsOfFile_(path)
        if data is None or not data.length():
            return False
        pb.clearContents()
        return bool(pb.setData_forType_(data, "public.png"))
    except Exception:  # noqa: BLE001
        return False


def take_screenshot(pid: int | None = None) -> dict:
    """Capture the screen (or `pid`'s front window) and save it where she can find
    it: ~/Pictures/MicMic (shots_dir), under a name that says what it is and when,
    never a temp file this module then has to remember to clean up - there is no
    cleanup path anywhere in this project (tests/test_micmic.py, rule 2), so an undo
    was never on the table and this does not pretend otherwise. Also copies the image
    onto the clipboard, so a paste into any app drops the picture without a Finder trip.

    {"ok": True, "path": ..., "folder": "Pictures/MicMic", "clipboard": bool} on
    success: "folder" is the place to name when saying where it went, "clipboard"
    whether the copy really worked. {"ok": False, "why": "screen_recording"} when the
    permission is not granted (screen_recording_state() tells the caller whether to
    ask or to explain), or {"ok": False, "why": "no_window"} / {"ok": False, "why":
    "capture_failed"} otherwise. Never raises.
    """
    if not _screen_recording_granted():
        return {"ok": False, "why": "screen_recording"}
    try:
        path = _shot_path("screenshot")
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": "capture_failed"}
    if pid:
        wid = _front_window_id(pid)
        if wid is None:
            return {"ok": False, "why": "no_window"}
        cmd = ["screencapture", "-x", "-o", "-l", str(wid), path]
    else:
        cmd = ["screencapture", "-x", "-C", path]
    try:
        subprocess.run(cmd, timeout=6, capture_output=True)
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": "capture_failed"}
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {"ok": False, "why": "capture_failed"}
    clip = _to_clipboard(path)
    return {"ok": True, "path": path, "folder": folder_label(path), "clipboard": clip}


# Two minutes is generous for a drag she may need both hands and her glasses for,
# and short enough that a capture nobody ever finishes does not sit open forever.
REGION_CAPTURE_TIMEOUT = 120.0


def capture_region(on_start=None) -> dict:
    """Interactive drag-to-select: a crosshair appears, she drags a region or
    presses Space and clicks a window, Esc cancels. BLOCKS until she finishes or
    the timeout above passes - call this from a background thread, never the one
    answering her turn (see router._capture_region_async).

    `on_start`, if given, is called once with the live subprocess.Popen the moment
    the crosshair is up, so the caller can kill it early (she resolved the send
    another way - see router.py's "I selected it" path) without waiting it out.

    {"ok": True, "path": ...} / {"ok": False, "why": "screen_recording"|"cancelled"
    |"capture_failed"}. "cancelled" means Esc: screencapture -i writes no file at
    all rather than an empty one, which is how this tells the two apart. Never
    raises."""
    if not _screen_recording_granted():
        return {"ok": False, "why": "screen_recording"}
    try:
        path = _shot_path("selection")
        proc = subprocess.Popen(["screencapture", "-i", "-x", path])
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": "capture_failed"}
    if on_start:
        try:
            on_start(proc)
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=REGION_CAPTURE_TIMEOUT)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "why": "capture_failed"}
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {"ok": False, "why": "cancelled"}
    clip = _to_clipboard(path)
    return {"ok": True, "path": path, "folder": folder_label(path), "clipboard": clip}


# ---------------------------------------------------------------- without Screen Recording
# Two ways to still get her a screenshot when MicMic may not capture one itself. Both
# use macOS's own screenshot tools, which capture with their own rights, so neither
# needs Screen Recording for MicMic and neither puts up a prompt.
#
#  1. "take a screenshot": open the Screenshot app (open_screenshot_app). She clicks
#     Capture; it saves wherever she usually saves screenshots.
#  2. "send this screen to Dana": she presses Control-Shift-Command-4 and drags, and
#     macOS puts the picture on the clipboard. MicMic does not press those keys for
#     her (a keystroke needs Accessibility, and a blind one could land anywhere): it
#     says them, watches the clipboard for the new picture (clipboard_count, then
#     wait_for_clipboard_image), and then opens the chat for her to paste.
#     Pasting it herself (Command-V) is the first choice for EVERY channel: it is her
#     own paste, so macOS asks nothing. save_clipboard_image keeps a copy in
#     shots_dir only for a send that truly needs a file, and it reads the picture's
#     bytes from MicMic's own process, which newer macOS (26 and later) can answer
#     with its own "MicMic would like to paste from ..." alert. So it checks
#     paste_access() first and, unless the caller has already said MicMic's own line
#     and passes allow_ask=True, refuses with "would_ask" instead of letting that
#     alert come up unannounced. Watching the counter and the types (clipboard_count,
#     clipboard_has_image) never reads the data, and never asks.
SCREENSHOT_APP = "Screenshot"
CLIPBOARD_KEYS = "Control-Shift-Command-4"
_IMAGE_TYPES = ("public.png", "public.tiff")


def open_screenshot_app() -> tuple[bool, str]:
    """Open macOS's own Screenshot app (the Command-Shift-5 toolbar). Needs no
    permission. (True, "opened") only when `open` says it worked, else
    (False, "open_failed"). Never raises, and never claims a picture was taken:
    she clicks Capture herself."""
    try:
        p = subprocess.run(["open", "-a", SCREENSHOT_APP], capture_output=True, text=True,
                           timeout=_CMD_TIMEOUT)
    except Exception:  # noqa: BLE001
        return False, "open_failed"
    return (True, "opened") if getattr(p, "returncode", 1) == 0 else (False, "open_failed")


def _pasteboard():
    try:
        from AppKit import NSPasteboard
        return NSPasteboard.generalPasteboard()
    except Exception:  # noqa: BLE001
        return None


def clipboard_count() -> int:
    """The clipboard's change counter right now, -1 when it cannot be read. Take it
    BEFORE telling her the keys and hand it to wait_for_clipboard_image, so a picture
    that was already on the clipboard is never mistaken for the new one."""
    pb = _pasteboard()
    try:
        return int(pb.changeCount()) if pb is not None else -1
    except Exception:  # noqa: BLE001
        return -1


def clipboard_has_image() -> bool:
    pb = _pasteboard()
    try:
        types = [str(t) for t in (pb.types() or [])] if pb is not None else []
    except Exception:  # noqa: BLE001
        return False
    return any(t in types for t in _IMAGE_TYPES)


def wait_for_clipboard_image(since: int, timeout: float = REGION_CAPTURE_TIMEOUT,
                             poll: float = 0.3, cancel=None) -> tuple[bool, str]:
    """Wait until the clipboard changes after `since` (from clipboard_count) and holds
    a picture. BLOCKS: run it on a background thread, like capture_region.

    (True, "on_clipboard"): a new picture is there, ready to paste.
    (False, "not_an_image"): the clipboard changed, but to something else.
    (False, "timeout") / (False, "cancelled"): nothing came. `cancel`, when given, is
        a callable checked every round; True stops the wait.
    (False, "no_clipboard"): the clipboard could not be read at all.
    """
    if since < 0 or _pasteboard() is None:
        return False, "no_clipboard"
    end = time.monotonic() + max(0.0, timeout)
    while True:
        if cancel is not None:
            try:
                if cancel():
                    return False, "cancelled"
            except Exception:  # noqa: BLE001
                pass
        now = clipboard_count()
        if now < 0:
            return False, "no_clipboard"
        if now != since:
            return (True, "on_clipboard") if clipboard_has_image() else (False, "not_an_image")
        if time.monotonic() >= end:
            return False, "timeout"
        time.sleep(poll)


# NSPasteboardAccessBehavior (macOS 15.4 and later). Spelled out here so a PyObjC
# without the constants still reads the number right.
_PB_DEFAULT, _PB_ASK, _PB_ALLOW, _PB_DENY = 0, 1, 2, 3
PASTE_ACCESS = ("free", "ask", "denied")


def paste_access() -> str:
    """Would reading the clipboard's data from MicMic's process put up a macOS alert?

    "free":   no alert. macOS before pasteboard privacy (no accessBehavior at all), or
              she chose Always Allow for MicMic.
    "ask":    macOS would show its own "MicMic would like to paste" alert. Also the
              answer for the system default, the conservative reading: on macOS 26
              the default is to ask, and guessing "free" wrongly is exactly the
              unannounced prompt this exists to prevent.
    "denied": she chose Never; a read gets nothing, so do not try.

    Reading accessBehavior itself never asks. MICMIC_FAKE_PASTE_ACCESS=free|ask|denied
    fakes it for tests (any other value reads as "ask"); no clipboard at all is
    "denied"."""
    fake = os.environ.get("MICMIC_FAKE_PASTE_ACCESS", "").strip()
    if fake:
        return fake if fake in PASTE_ACCESS else "ask"
    pb = _pasteboard()
    if pb is None:
        return "denied"
    getter = getattr(pb, "accessBehavior", None)
    if getter is None:
        return "free"
    try:
        v = int(getter())
    except Exception:  # noqa: BLE001
        return "ask"
    if v == _PB_ALLOW:
        return "free"
    if v == _PB_DENY:
        return "denied"
    return "ask"


def save_clipboard_image(allow_ask: bool = False) -> tuple[bool, str]:
    """Keep the picture on the clipboard as a PNG in shots_dir, for a send that needs
    a file (an iMessage attachment). Reads the clipboard in this process: no
    osascript, and no prompt unless the caller allows one.

    Prefer having her paste (Command-V) over this: call it only when a file is truly
    needed. Before reading, paste_access() is checked:
      "denied"                      -> (False, "paste_denied"), nothing read.
      "ask" and not allow_ask       -> (False, "would_ask"), nothing read: fall back
                                       to the paste line, or say MicMic's own line
                                       first and call again with allow_ask=True.
      "free", or "ask" + allow_ask  -> the read happens.
    (True, path) or (False, "no_image" | "save_failed" | "would_ask" | "paste_denied").
    """
    pb = _pasteboard()
    if pb is None:
        return False, "no_image"
    access = paste_access()
    if access == "denied":
        return False, "paste_denied"
    if access == "ask" and not allow_ask:
        return False, "would_ask"
    try:
        data = pb.dataForType_("public.png")
        if data is None:
            tiff = pb.dataForType_("public.tiff")
            if tiff is None:
                return False, "no_image"
            from AppKit import NSBitmapImageRep, NSBitmapImageFileTypePNG
            rep = NSBitmapImageRep.imageRepWithData_(tiff)
            data = (rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
                    if rep is not None else None)
            if data is None:
                return False, "no_image"
        raw = bytes(data)
    except Exception:  # noqa: BLE001
        return False, "no_image"
    if not raw:
        return False, "no_image"
    try:
        path = _shot_path("selection")
        with open(path, "wb") as fh:
            fh.write(raw)
        if os.path.getsize(path) == 0:
            return False, "save_failed"
    except Exception:  # noqa: BLE001
        return False, "save_failed"
    return True, path


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
    visible["document"] = redact(visible.get("document", ""))
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


# ---------------------------------------------------------------- questions about it
# "What time does my flight leave?" with the booking in front of her. No "this", no
# "screen", so nothing pointed at the screen and it was answered blind (bench
# 2026-10-01: scr-010/011/014). This is only the cheap gate that decides whether
# understand() asks about it at all (brain.SCREEN_ASK_QUESTIONS): a sentence it
# misses is judged exactly as before, and one it catches is decided by Jev, then
# grounded in what is actually on the screen.
_SCREEN_NOUNS = (
    r"flights?|booking|reservation|order|recipe|ticket|tickets|appointment|meeting|"
    r"e-?mail|mail|invoice|receipt|bill|total|seat|gate|terminal|departure|arrival|"
    r"confirmation|code|reference|itinerary|delivery|package|tracking|train|bus|"
    r"hotel|check-?in|check-?out|deadline|balance|amount|price|cost|ingredients?|"
    r"steps?|document|page|article|letter|form|schedule|event|address|number|date")
_SCREEN_ASK = [re.compile(p, re.I) for p in (
    rf"\b(?:my|this|the|our|that)\s+(?:\w+\s+)?(?:{_SCREEN_NOUNS})\b",
    r"\bhow\s+(?:many|much|long)\b",
    r"\bwhat\s+time\s+(?:does|do|is|are|was|did)\s+(?:my|the|this|our|that)\b",
    r"\bwhen\s+(?:does|do|is|are)\s+(?:my|the|this|our)\b",
    r"\b(?:does|do)\s+(?:it|this|that)\s+need\b",
    r"\b(?:do|did)\s+i\s+(?:have|need|get|pay|owe)\b",
    # Hebrew: how many / at what time / when, or a possessive "...שלי" on a noun.
    r"כמה|באיזו שעה|באיזה שעה|מתי|"
    r"(?:טיסה|הזמנה|מתכון|כרטיס|תור|פגישה|מייל|קוד|קבלה|חשבונית|מושב|שער|סכום|מחיר)",
    # Arabic.
    r"\bكم\b|[أا]ي ساعة|\bمتى\b|[إا]يمتى|\bامتى\b|"
    r"رحلت|حجز|الوصفة|تذكرت|موعد|الكود|رمز|الفاتورة|المقعد|المبلغ|السعر",
    # Russian.
    r"сколько|во сколько|\bкогда\b|"
    r"рейс|брон|рецепт|билет|запис|\bкод\b|заказ|сч[её]т|\bчек\b|мест[оа]\b|сумм|цен[аы]",
)]


def may_ask_about_screen(text: str) -> bool:
    """Her words MAY ask for a detail of something open on her screen. A wide net on
    purpose (it only decides whether one question rides along); the deciding is
    Jev's, and the answer is checked against the screen itself."""
    t = text or ""
    return any(rx.search(t) for rx in _SCREEN_ASK)


_NUM_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
              "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
              "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
              "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
              "eighty": 80, "ninety": 90, "hundred": 100}
_DIGITS = re.compile(r"\d+")
_NUM_WORD_RX = re.compile(r"\b(" + "|".join(_NUM_WORDS) + r")\b", re.I)


def _digit_groups(text: str) -> set[str]:
    """Every run of digits, in any script's digits, without leading zeros."""
    norm = "".join(str(unicodedata.digit(c)) if c.isdigit() else c for c in (text or ""))
    return {g.lstrip("0") or "0" for g in _DIGITS.findall(norm)}


def _counted(text: str) -> list[tuple[str, str]]:
    """(number, the word right after it) for every number in the text, number words
    included: "4 eggs" and "four eggs" both give ("4", "egg")."""
    norm = "".join(str(unicodedata.digit(c)) if c.isdigit() else c for c in (text or ""))
    norm = _NUM_WORD_RX.sub(lambda m: str(_NUM_WORDS[m.group(1).lower()]), norm)
    toks = re.findall(r"\d+|[^\W\d_]+", norm)
    out = []
    for i, t in enumerate(toks):
        if t.isdigit():
            nxt = toks[i + 1].lower() if i + 1 < len(toks) and not toks[i + 1].isdigit() else ""
            out.append((t.lstrip("0") or "0", nxt[:-1] if nxt.endswith("s") else nxt))
    return out


def numbers_on_screen(answer: str, screen_text: str) -> bool:
    """Every number in the answer is one the screen shows: digits as written, and the
    English number words ("four eggs"). And when the answer counts something the
    screen also counts ("2 eggs"), the screen must show that number of that thing:
    the recipe said "4 eggs, 2 tomatoes", so "2 eggs" is invented even though a 2 is
    on the screen. Such an answer is never passed on as if it were read off it."""
    seen = _digit_groups(screen_text)
    pairs = _counted(answer)
    if not {n for n, _ in pairs} <= seen:
        return False
    on_screen = _counted(screen_text)
    things = {w for _, w in on_screen if w}
    for n, w in pairs:
        if w in things and (n, w) not in on_screen:
            return False
    return True
