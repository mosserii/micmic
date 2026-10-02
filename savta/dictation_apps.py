"""Other dictation apps' push-to-talk keys, so MicMic's own does not collide with them.

The owner's fresh install (1.2.1, 2026-09-30): MicMic's default talk key is a tap of
right Option, and Handy, which he also uses, is bound to right Option too. Pressing it
ran Handy, and MicMic's "Try it" step sat on "Waiting for you".

Read-only, and only files that already exist. Nothing here ever writes to another
app's settings. Known stores:

    Handy         ~/Library/Application Support/com.pais.handy/settings_store.json
                  settings.bindings.<id>.current_binding, e.g. "option_right",
                  "option+space"
    Superwhisper  ~/Library/Preferences/com.superduper.superwhisper.plist
                  KeyboardShortcuts_<name> = '{"carbonKeyCode":61,"carbonModifiers":2048}'
                  (sindresorhus/KeyboardShortcuts: a Carbon key code and modifier mask)

Wispr Flow is not read: where it keeps its shortcut is not documented, and its default
(holding Fn) does not collide with a right-hand key.

MICMIC_DICTATION_HOME points the reads at a fake home (tests). A process with
MICMIC_STATE_DIR set and no MICMIC_DICTATION_HOME is a test harness, and reads nothing
at all: a suite run on the owner's Mac must not change its answers because Handy is
installed there.
"""
from __future__ import annotations

import json
import os
import plistlib
import time
from pathlib import Path

# MicMic's lone right-hand keys, in the order a first run tries them.
DEFAULT_ORDER = ("right-option", "right-command", "right-control")
SUGGEST_ORDER = DEFAULT_ORDER + ("right-shift", "fn-fn")

HANDY = "Library/Application Support/com.pais.handy/settings_store.json"
SUPERWHISPER = "Library/Preferences/com.superduper.superwhisper.plist"

# Handy's names for a lone right-hand modifier.
_HANDY_TAP = {
    "option_right": "right-option", "alt_right": "right-option", "right_option": "right-option",
    "command_right": "right-command", "cmd_right": "right-command", "meta_right": "right-command",
    "super_right": "right-command", "right_command": "right-command",
    "control_right": "right-control", "ctrl_right": "right-control", "right_control": "right-control",
    "shift_right": "right-shift", "right_shift": "right-shift",
}
_MOD = {"cmd": "cmd", "command": "cmd", "meta": "cmd", "super": "cmd",
        "alt": "alt", "opt": "alt", "option": "alt",
        "ctrl": "ctrl", "control": "ctrl", "shift": "shift"}

# Carbon virtual key codes (HIToolbox Events.h): the right-hand modifiers, and the
# letters and digits a MicMic combination can use.
_CARBON_TAP = {61: "right-option", 54: "right-command", 62: "right-control", 60: "right-shift"}
_CARBON_KEY = {0: "a", 11: "b", 8: "c", 2: "d", 14: "e", 3: "f", 5: "g", 4: "h", 34: "i",
               38: "j", 40: "k", 37: "l", 46: "m", 45: "n", 31: "o", 35: "p", 12: "q",
               15: "r", 1: "s", 17: "t", 32: "u", 9: "v", 13: "w", 7: "x", 16: "y", 6: "z",
               29: "0", 18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6", 26: "7",
               28: "8", 25: "9"}
_CARBON_MOD = ((256, "cmd"), (512, "shift"), (2048, "alt"), (4096, "ctrl"))


def _home() -> Path | None:
    fake = os.environ.get("MICMIC_DICTATION_HOME")
    if fake:
        return Path(fake).expanduser()
    if os.environ.get("MICMIC_STATE_DIR"):
        return None
    return Path.home()


def norm(spec: str) -> str:
    """One spelling per key, so two apps' names for it compare equal:
    "alt+shift+m" and "shift+option+m" are the same press."""
    s = str(spec or "").strip().lower()
    if s in ("", "fn-fn", "fn+fn", "globe-globe", "globe+globe"):
        return "fn-fn"
    if s in _HANDY_TAP:
        return _HANDY_TAP[s]
    if s.startswith("right-"):
        return s
    parts = [p for p in s.replace("-", "+").split("+") if p]
    if len(parts) < 2:
        return s
    *mods, key = parts
    mods = sorted({_MOD.get(m, m) for m in mods})
    return "+".join(mods + [key])


def _handy(home: Path) -> list[str]:
    f = home / HANDY
    if not f.is_file():
        return []
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        binds = (d.get("settings") or {}).get("bindings") or {}
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for b in binds.values() if isinstance(binds, dict) else []:
        if not isinstance(b, dict):
            continue
        cur = str(b.get("current_binding") or "").strip().lower()
        if cur and cur not in ("escape", "esc"):
            out.append(norm(cur))
    return out


def _superwhisper(home: Path) -> list[str]:
    f = home / SUPERWHISPER
    if not f.is_file():
        return []
    try:
        with open(f, "rb") as fh:
            d = plistlib.load(fh)
    except Exception:  # noqa: BLE001  (a plist we cannot parse is just "no keys")
        return []
    out = []
    for k, v in d.items():
        if not str(k).startswith("KeyboardShortcuts_"):
            continue
        try:
            sc = json.loads(v) if isinstance(v, (str, bytes)) else v
            code, mods = int(sc["carbonKeyCode"]), int(sc.get("carbonModifiers") or 0)
        except (ValueError, TypeError, KeyError):
            continue
        if code in _CARBON_TAP:
            out.append(_CARBON_TAP[code])
        elif code in _CARBON_KEY:
            names = [n for bit, n in _CARBON_MOD if mods & bit]
            if names:
                out.append(norm("+".join(names + [_CARBON_KEY[code]])))
    return out


READERS = (("Handy", _handy), ("Superwhisper", _superwhisper))


_cache: dict = {"at": 0.0, "home": None, "out": []}
CACHE_S = 2.0          # the onboarding polls its status every 0.7 s


def bindings() -> list[dict]:
    """[{"app": "Handy", "key": "right-option"}, ...] for every known app found."""
    home = _home()
    if home is None:
        return []
    now = time.monotonic()
    if _cache["home"] == home and now - _cache["at"] < CACHE_S:
        return list(_cache["out"])
    out = []
    for app, read in READERS:
        try:
            keys = read(home)
        except Exception:  # noqa: BLE001  (another app's file must never break MicMic)
            keys = []
        for k in dict.fromkeys(keys):
            out.append({"app": app, "key": k})
    _cache.update(at=now, home=home, out=list(out))
    return out


def user_of(spec: str, found: list[dict] | None = None) -> str | None:
    """The first known dictation app that already uses this key, or None."""
    want = norm(spec)
    for b in bindings() if found is None else found:
        if b["key"] == want:
            return b["app"]
    return None


def first_free(order=DEFAULT_ORDER, found: list[dict] | None = None) -> str | None:
    found = bindings() if found is None else found
    taken = {b["key"] for b in found}
    return next((k for k in order if k not in taken), None)
