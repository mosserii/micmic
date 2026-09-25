"""The first-run onboarding's endpoints: whether to show it, what the Mac has granted,
the one System Settings pane to open, and whether a real turn has come through yet.

The page half lives in web/onboarding/ and is only ever loaded by the app's own window
(?panel=1) on a Mac nobody has been set up on. Everything here is read-only except two
things: the "onboarded" setting, written once when it is finished or skipped, and
`open`, which can only ever open one of three fixed System Settings panes.

    GET  /api/onboarding          {show, first_run, onboarded, armed, turn}
    POST /api/onboarding          {"action": "try" | "done" | "skip"}
    GET  /api/permissions         {microphone, speech, accessibility, ready}
    POST /api/permissions/open    {"pane": "accessibility" | "microphone" | "speech"}

Each permission is "granted", "denied", "not_asked" or "unknown". "unknown" is an
honest answer, not a soft pass: the page never shows a tick for it.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

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


def status() -> dict:
    first_run = not _prof.load().get("setup_complete")
    done = _onboarded()
    with _lock:
        turn = dict(_turn) if (_turn and _armed_at and _turn["at"] >= _armed_at) else None
        armed = bool(_armed_at)
    if turn:
        turn.pop("at", None)
    return {"show": first_run and not done, "first_run": first_run, "onboarded": done,
            "armed": armed, "turn": turn}


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
        save_settings({"onboarded": True})
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


# ---------------------------------------------------------------- routing
def _reply(h, code: int, payload: dict):
    return h._send(code, json.dumps(payload, ensure_ascii=False).encode())


def get(h):
    if h.path.startswith("/api/permissions"):
        return _reply(h, 200, permissions())
    return _reply(h, 200, status())


def post(h, body: bytes):
    try:
        payload = json.loads(body or b"{}")
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if h.path.startswith("/api/permissions/open"):
        return _reply(h, *open_pane(str(payload.get("pane") or "")))
    if h.path.startswith("/api/permissions"):
        return _reply(h, 404, {"error": "not_found"})
    return _reply(h, *act(str(payload.get("action") or "")))
