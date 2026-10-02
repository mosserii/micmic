"""The first-run onboarding's endpoints: whether to show it, what the Mac has granted,
the one System Settings pane to open, and whether a real turn has come through yet.

The page half lives in web/onboarding/ and is only ever loaded by the app's own window
(?panel=1) on a Mac nobody has been set up on. Everything here is read-only except three
things: the "onboarded" setting, written once when it is finished or skipped; `open`,
which can only ever open one of three fixed System Settings panes; and `request`, which
asks macOS for exactly one of three fixed permissions, because she just clicked its
button.

    GET  /api/onboarding             {show, first_run, onboarded, armed, turn, move, clash}
    POST /api/onboarding             {"action": "try" | "done" | "skip"}
    GET  /api/permissions            {microphone, speech, accessibility, ready}
    POST /api/permissions/open       {"pane": "accessibility" | "microphone" | "speech"}
    POST /api/permissions/request    {"k": "microphone" | "speech" | "accessibility"}
    GET  /api/permissions/next       {"k": <kind or null>}   (the listener, out of process)
    POST /api/install/move           copy to Applications, reopen from there (install_place)

`move` says whether this copy runs from the disk image (or translocated) and should be
moved to Applications before anything else; `clash` names a dictation app (Handy,
Superwhisper) that already uses MicMic's talk key (savta/dictation_apps.py).

Nothing asks for a permission at launch any more (the first-run "permission storm"):
the one prompt for each comes from its own button, on the onboarding or in Settings,
or just in time from the listener when she tries to talk. The prompt itself belongs to
the app process: in the release bundle the listener shares this process and registers
itself with set_requester(); a listener running as its own process (a source build)
collects the request from /api/permissions/next instead.

Each permission is "granted", "denied", "not_asked" or "unknown". "unknown" is an
honest answer, not a soft pass: the page never shows a tick for it.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from . import dictation_apps as _dict
from . import install_place as _place
from . import profile as _prof

_PANE_ROOT = "x-apple.systempreferences:com.apple.preference.security?"
PANES = {
    "accessibility": _PANE_ROOT + "Privacy_Accessibility",
    "microphone": _PANE_ROOT + "Privacy_Microphone",
    "speech": _PANE_ROOT + "Privacy_SpeechRecognition",
}

# Step 3 ("Try it") arms this, and the next real utterance after that completes it. A
# turn that arrived before the step started does not count: she has to see it work.
_lock = threading.Lock()
_armed_at = 0.0
_turn: dict | None = None


def _onboarded() -> bool:
    from .router import load_settings
    return bool(load_settings().get("onboarded"))


def setup_done() -> bool:
    """Is setup over, by voice or in the window? Finishing or skipping the window's
    onboarding is the end of setup: the old spoken setup ("What should I call you?")
    must not start again after it. A Mac left with onboarded true and setup_complete
    false (the owner's 1.0.1) is resolved to done here."""
    if _prof.load().get("setup_complete"):
        return True
    if _onboarded():
        _prof.finish_setup()
        return True
    return False


def in_progress() -> bool:
    """Step 3 ("Try it") is waiting for her first real turn. The spoken setup keeps
    out of it: the window is already doing the introducing."""
    return bool(_armed_at)


def status() -> dict:
    first_run = not _prof.load().get("setup_complete")
    done = _onboarded()
    with _lock:
        turn = dict(_turn) if (_turn and _armed_at and _turn["at"] >= _armed_at) else None
        armed = bool(_armed_at)
    if turn:
        turn.pop("at", None)
    show = first_run and not done
    out = {"show": show, "first_run": first_run, "onboarded": done,
           "armed": armed, "turn": turn, "move": {"needed": False}, "clash": None}
    if show:
        ensure_free_default()
        out["move"] = _place.status()
        out["clash"] = hotkey_clash()
    return out


# ---------------------------------------------------------------- the talk key
def ensure_free_default() -> str | None:
    """A first run with no key chosen yet: right Option, unless a dictation app she
    already uses has it (the owner's Handy is on right Option), then the first free of
    right Command and right Control. Saved as her "hotkey" setting, once, so the
    listener registers that one. Never on a Mac already set up."""
    try:
        from .router import load_settings, save_settings
        s = load_settings()
        if "hotkey" in s or s.get("onboarded") or _prof.load().get("setup_complete"):
            return None
        found = _dict.bindings()
        if not _dict.user_of(_dict.DEFAULT_ORDER[0], found):
            return None
        free = _dict.first_free(found=found)
        if not free or free == _dict.DEFAULT_ORDER[0]:
            return None
        save_settings({"hotkey": free})
        return free
    except Exception:  # noqa: BLE001  (another app's settings must never stop a launch)
        return None


def hotkey_clash() -> dict | None:
    """{"app": "Handy", "key": "right-option", "suggest": "right-command", "free": [...]}
    when a known dictation app uses MicMic's current key, else None."""
    try:
        from .router import load_config
        cur = str(load_config().get("hotkey") or _dict.DEFAULT_ORDER[0])
        found = _dict.bindings()
        app = _dict.user_of(cur, found)
        if not app:
            return None
        taken = {b["key"] for b in found} | {_dict.norm(cur)}
        free = [k for k in _dict.SUGGEST_ORDER if k not in taken]
        # "key" in MicMic's own spelling, the one the page compares with its hotkey.
        return {"app": app, "key": cur.strip().lower(), "suggest": free[0] if free else None,
                "free": free}
    except Exception:  # noqa: BLE001
        return None


def note_utterance(body: bytes) -> None:
    """Called for every POST to /api/utterance, before the router sees it. Only records
    anything while step 3 is waiting, and never raises: onboarding must not be able to
    break the one request that matters."""
    global _turn
    try:
        if not _armed_at:
            return
        payload = json.loads(body or b"{}")
        if not isinstance(payload, dict) or payload.get("speculative"):
            return
        if payload.get("source") not in (None, "", "microphone"):
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        with _lock:
            _turn = {"heard": text[:200], "at": time.time()}
    except Exception:  # noqa: BLE001
        pass


def act(action: str) -> tuple[int, dict]:
    global _armed_at, _turn
    if action == "try":
        with _lock:
            _armed_at, _turn = time.time(), None
        return 200, status()
    if action in ("done", "skip"):
        from .router import save_settings
        # contacts_gate: this Mac was set up with the just-in-time contacts read
        # (actions/contacts.read_before); a Mac onboarded before it was not.
        save_settings({"onboarded": True, "contacts_gate": True})
        _prof.finish_setup()
        with _lock:
            _armed_at, _turn = 0.0, None
        return 200, status()
    return 400, {"error": "unknown_action"}


# ---------------------------------------------------------------- permissions
# TCC answers for the process that is responsible for this one. When MicMic.app starts
# the server that is the app, which is exactly the grant the listener needs; run from a
# terminal it is the terminal's. The status calls never prompt.
_TCC = {0: "not_asked", 1: "denied", 2: "denied", 3: "granted"}
_classes: dict = {}


def _objc_class(name: str, framework: str):
    if name not in _classes:
        import objc
        objc.loadBundle(framework, {},
                        bundle_path=f"/System/Library/Frameworks/{framework}.framework")
        _classes[name] = objc.lookUpClass(name)
    return _classes[name]


def _microphone() -> str:
    try:
        cls = _objc_class("AVCaptureDevice", "AVFoundation")
        return _TCC.get(int(cls.authorizationStatusForMediaType_("soun")), "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _speech() -> str:
    try:
        cls = _objc_class("SFSpeechRecognizer", "Speech")
        return _TCC.get(int(cls.authorizationStatus()), "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _accessibility() -> str:
    try:
        from .actions import screen
        return "granted" if screen.permissions().get("accessibility") else "denied"
    except Exception:  # noqa: BLE001
        return "unknown"


def permissions() -> dict:
    fake = _fake_state()
    if fake is not None:
        out = dict(fake["state"])
        if out["accessibility"] == "not_asked":
            out["accessibility"] = "denied"      # as the real check reports it
    else:
        out = {"microphone": _microphone(), "speech": _speech(),
               "accessibility": _accessibility()}
    out["ready"] = all(v == "granted" for v in out.values())
    return out


def open_pane(pane: str) -> tuple[int, dict]:
    """Only the three panes above, never a URL from the page.

    Not mac.open_url(): that hands every link to Chrome when Chrome is installed, and
    Chrome does not open System Settings. `open` gives the URL to its real handler."""
    url = PANES.get(pane)
    if not url:
        return 400, {"ok": False, "error": "unknown_pane"}
    # A test must be able to press the button without System Settings jumping in front
    # of whoever is using this Mac.
    if os.environ.get("MICMIC_DRY_OPEN") == "1":
        return 200, {"ok": True, "opened": pane, "dry": True}
    try:
        subprocess.run(["open", url], check=False, timeout=8)
    except Exception as e:  # noqa: BLE001
        return 200, {"ok": False, "error": "could_not_open", "detail": type(e).__name__}
    return 200, {"ok": True, "opened": pane}


# ---------------------------------------------------------------- asking, one at a time
KINDS = ("microphone", "speech", "accessibility")
_requester = None            # the listener's request_permission, when it shares this process
_pending: dict | None = None  # {"k", "at"} for a listener in another process
PENDING_TTL = 20.0           # a click nobody collected in time is forgotten, never replayed later


def set_requester(fn) -> None:
    """The listener, running in this very process, takes the requests itself: it asks
    on the main thread, which is where AppKit wants the prompt to come from."""
    global _requester
    _requester = fn


# Tests: MICMIC_FAKE_PERMISSIONS='{"microphone": "not_asked", ..., "answer": {...},
# "delay": 0.4}' replaces every TCC call with this table. A request moves its kind to
# answer[k] (default "granted") after `delay` seconds, and is logged in _fake_asked.
# Nothing real is ever asked or read, so no test can put a macOS prompt on the screen.
_fake: dict | None = None
_fake_asked: list = []


def _fake_state() -> dict | None:
    global _fake
    raw = os.environ.get("MICMIC_FAKE_PERMISSIONS")
    if not raw:
        return None
    if _fake is None:
        try:
            d = json.loads(raw)
        except ValueError:
            d = {}
        _fake = {"state": {k: str(d.get(k) or "not_asked") for k in KINDS},
                 "answer": dict(d.get("answer") or {}), "delay": float(d.get("delay", 0.4))}
    return _fake


def request(k: str) -> tuple[int, dict]:
    """Ask macOS for exactly this one permission, now, because she clicked for it.

    Already granted: nothing to ask. Microphone or speech already refused: macOS never
    asks twice, so the only way left is its pane, which is opened instead. Otherwise
    the app asks (see the module docstring) and the page polls /api/permissions for
    the answer; this returns at once with the status as it stands."""
    global _pending
    if k not in KINDS:
        return 400, {"ok": False, "error": "unknown_permission"}
    now = permissions()[k]
    if now == "granted":
        return 200, {"ok": True, "k": k, "status": now, "asked": False}
    if now == "denied" and k != "accessibility":
        # Accessibility has no "refused" of its own: off is its only other state, and
        # its request is the one that knows whether to prompt or open the pane.
        code, o = open_pane(k)
        return 200, {"ok": bool(o.get("ok")), "k": k, "status": now, "asked": False,
                     "opened": k if o.get("ok") else None}
    fake = _fake_state()
    if fake is not None:
        _fake_asked.append(k)
        answer = str(fake["answer"].get(k) or "granted")

        def land():
            fake["state"][k] = answer
        t = threading.Timer(fake["delay"], land)
        t.daemon = True
        t.start()
        return 200, {"ok": True, "k": k, "status": now, "asked": True, "via": "fake"}
    if _requester is not None:
        try:
            _requester(k)
        except Exception as e:  # noqa: BLE001
            return 200, {"ok": False, "k": k, "status": now, "error": "could_not_ask",
                         "detail": type(e).__name__}
        return 200, {"ok": True, "k": k, "status": now, "asked": True, "via": "app"}
    with _lock:
        _pending = {"k": k, "at": time.time()}
    return 200, {"ok": True, "k": k, "status": now, "asked": True, "via": "listener"}


def next_request() -> dict:
    """For a listener that is its own process: the one request waiting, taken once."""
    global _pending
    with _lock:
        p, _pending = _pending, None
    if not p or time.time() - p["at"] > PENDING_TTL:
        return {"k": None}
    return {"k": p["k"]}


# ---------------------------------------------------------------- routing
def _own_origin(origin) -> bool:
    if not origin:
        return True                      # not a browser: the listener, a test, curl
    from urllib.parse import urlparse
    return urlparse(origin).hostname in ("127.0.0.1", "localhost")


def _reply(h, code: int, payload: dict):
    return h._send(code, json.dumps(payload, ensure_ascii=False).encode())


def get(h):
    if h.path.startswith("/api/permissions/next"):
        return _reply(h, 200, next_request())
    if h.path.startswith("/api/permissions"):
        return _reply(h, 200, permissions())
    return _reply(h, 200, status())


def post(h, body: bytes):
    if h.path.startswith("/api/install/move"):
        # Only MicMic's own page may copy the app and quit it.
        if not _own_origin(h.headers.get("Origin")):
            return _reply(h, 403, {"ok": False, "error": "foreign_origin"})
        return _reply(h, *_place.move())
    try:
        payload = json.loads(body or b"{}")
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if h.path.startswith("/api/permissions/open"):
        return _reply(h, *open_pane(str(payload.get("pane") or "")))
    if h.path.startswith("/api/permissions/request"):
        # Only MicMic's own page may put a macOS prompt on her screen, never a web page
        # she happens to have open that posts to this port.
        if not _own_origin(h.headers.get("Origin")):
            return _reply(h, 403, {"ok": False, "error": "foreign_origin"})
        return _reply(h, *request(str(payload.get("k") or "")))
    if h.path.startswith("/api/permissions"):
        return _reply(h, 404, {"error": "not_found"})
    return _reply(h, *act(str(payload.get("action") or "")))
