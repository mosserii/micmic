#!/usr/bin/env python3
"""Screenshot feature regression suite (take a screenshot / send a screenshot).

    python3 tests/test_screenshot.py

Same shape as tests/test_micmic.py: plain asserts, one printed line per check,
non-zero exit on any failure, nothing leaves the machine. Kept in its own file
rather than folded into test_micmic.py because most of what is exercised here is
brand-new code with no recorded Jev traffic of its own, and tests/replay.py keys
a recording by state+questions together - a MISS raises loudly by design, and a
recording drifts out of date as brain.py grows (measured: merging main in moved
the recorded schema and turned what used to be a cache hit here into a miss).
So this suite never depends on a recording at all: router.understand() is always
replaced by a controlled stand-in built from _u() (the same well-formed, fully-
keyed fake brain.understand() answer tests/test_micmic.py's own scripted_turns
uses), and the Jev object passed to router.handle() is _ScriptedJev, copied from
the same file, which answers any question it is not told about with a safe
default rather than reaching the network. router._resume()/_same_person()/
_cancel_check() are each replaced the same way for the one turn that needs them.
This suite makes 0 real Jev calls under any MICMIC_REPLAY setting; replay.install()
is kept only as a defence-in-depth wall, so a path this file forgot to stand in
for fails loudly (REPLAY MISS) instead of silently going live.

The keyword matching itself (_says_any against _SCREENSHOT_WORDS) is plain string
code with no model in the loop at all, and is checked directly.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if os.environ.get("MICMIC_ALLOW_SEND", "").lower() in ("1", "true", "yes"):
    print("REFUSING TO RUN: MICMIC_ALLOW_SEND is set in this shell.\n"
          "This suite arms real sends and relies on the dry-run gate to stop them.")
    sys.exit(2)
os.environ.pop("MICMIC_ALLOW_SEND", None)

_STATE = tempfile.mkdtemp(prefix="micmic-test-screenshot-state-")
os.environ["MICMIC_STATE_DIR"] = _STATE
atexit.register(lambda: shutil.rmtree(_STATE, ignore_errors=True))

from savta import router                       # noqa: E402
from savta import profile as prof              # noqa: E402
from savta.actions import mac                  # noqa: E402
from savta.actions import screen               # noqa: E402
from savta.actions import contacts as book     # noqa: E402

import replay                                  # noqa: E402
replay.install()

router.due_briefing = lambda: False

prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done",
           "name": "Test", "language": "hebrew", "speech_lang": "he-IL", "city": "חיפה"})

# ---------------------------------------------------------------- walls
# Nothing this suite does may reach a real app, a real file outside the Desktop
# fakes below, or a real network call other than the one cached Jev lookup.
import subprocess as _sp_mod              # noqa: E402

LAUNCHED: list[list] = []
_REAL_RUN = _sp_mod.run
_BLOCK_CMDS = ("open", "osascript", "screencapture", "sips", "lsappinfo", "mdfind", "pmset")


def _blocked_run(cmd, *a, **kw):
    if isinstance(cmd, (list, tuple)) and cmd and cmd[0] in _BLOCK_CMDS:
        LAUNCHED.append(list(cmd))
        class _R:
            returncode, stdout, stderr = 0, "[test] blocked", ""
        return _R()
    return _REAL_RUN(cmd, *a, **kw)


_sp_mod.run = _blocked_run
mac.subprocess.run = _blocked_run
screen.subprocess.run = _blocked_run

OSA: list[str] = []


def _blocked_osa(script, timeout=15.0):
    OSA.append(script)
    return False, "[test] osascript blocked"


mac._osa = _blocked_osa

SENT: list[tuple] = []
SENT_ATTACH: list[tuple] = []
WA: list[tuple] = []
WA_OPENED: list[str] = []
SAID: list[tuple] = []
CLIP: list[str] = []


def _stub_send_message(n, t, attachment=None):
    SENT.append((n, t))
    if attachment:
        SENT_ATTACH.append((n, attachment))
    return True, "[test stub] imessage"


REAL_COPY_TO_CLIPBOARD = mac.copy_file_to_clipboard
REAL_SEND_MESSAGE = mac.send_message
REAL_WHATSAPP_OPEN_CHAT = mac.whatsapp_open_chat

mac.send_message = _stub_send_message
mac.whatsapp = lambda n, t: (WA.append((n, t)), (True, "[test stub] whatsapp"))[1]
mac.whatsapp_open_chat = lambda n: (WA_OPENED.append(n), (True, "[test stub] opened chat"))[1]
mac.copy_file_to_clipboard = lambda p: (CLIP.append(p), True)[1]
mac.say = lambda t, l="english": SAID.append((l, t))
mac.screen_locked = lambda: False

FIXED_BOOK = [{"name": "Zohar Levin", "phone": "+972500000001",
              "waid": "972500000001@s.whatsapp.net", "source": "fixture"}]
book.all_contacts = lambda force=False, wait=None: list(FIXED_BOOK)
router.get_contacts = lambda utterance="", wait=None: ["Zohar Levin"]

REAL_TAKE_SCREENSHOT = screen.take_screenshot
REAL_FRONTMOST = screen.frontmost

# ---------------------------------------------------------------- harness
PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail: str = "") -> bool:
    global PASSED
    ok = bool(ok)
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return ok


def short(v, n=200):
    s = repr(v)
    return s if len(s) <= n else s[:n] + "..."


def reset_state():
    router._cancel_pending()
    router.AWAITING = None
    router.MEM = router.Memory()
    for r in (SENT, SENT_ATTACH, WA, WA_OPENED, SAID, CLIP, OSA, LAUNCHED):
        r.clear()


# A well-formed, fully-keyed fake brain.understand() answer and a Jev double that
# never reaches the network, both copied from tests/test_micmic.py's own
# scripted_turns/_ScriptedJev (_U_KEYS, _u, _ScriptedJev there) rather than a live
# or replayed call: those two are the single source of truth for what
# brain.understand() currently returns, kept up to date by whoever last touched
# brain.py, so copying them here tracks that schema instead of re-deriving it and
# silently drifting from it.
_U_KEYS = ("intent_confidence", "wants_full_length", "names_title", "contact_confidence",
          "contact_named", "has_message_content", "money_involved", "sounds_coached",
          "control_confidence", "wants_recent", "asking_for_notes", "is_complete",
          "noise", "refers_back", "is_compound", "needs_knowledge", "about_weather",
          "about_clock", "setting_emergency_contact", "inside_an_app",
          "speaker_gender_confidence", "rejects_last", "describes_instead",
          "refers_to_screen", "distress", "emergency", "wants_undo", "amends_message",
          "live_kind_confidence")


def _u(intent="message", **kw) -> dict:
    u = {k: 0.0 for k in _U_KEYS}
    u.update({"raw": {}, "spans": {}, "intent": intent, "intent_probs": {intent: 0.9},
              "media_kind": "not_applicable", "contact": "nobody",
              "control_action": "not_applicable", "player_action": "not_applicable",
              "channel": "imessage", "file_kind": "any", "when_minutes": "none",
              "language": "english", "speaker_gender": "unrevealed",
              "screen_task": "not_applicable", "weather_day": "today",
              "write_in": "not_applicable", "message_app": "unchanged",
              "live_kind": "not_live", "standing": {},
              "intent_confidence": 0.95, "is_complete": 0.95, "contact_confidence": 0.9})
    u.update(kw)
    return u


class _ScriptedJev:
    """A Jev that never reaches the network: answers any question it is not told
    about with a safe default. Only router.handle()'s bookkeeping (j.calls etc.)
    and the odd path this file forgot to stand in for should ever reach .ask()."""

    def __init__(self):
        self.calls, self.cost_usd, self.busy_ms = 0, 0.0, 0.0

    def ask(self, state, qs):
        self.calls += 1
        out = {}
        for k, q in qs.items():
            if q["type"] == "choice":
                opts = list(q["criteria"])
                pick = "nobody" if "nobody" in opts else opts[0]
                out[k] = {"choice": pick, "confidence": 0.9, "probabilities": {pick: 0.9}}
            elif q["type"] == "noul":
                out[k] = {"noul": 0.0}
            else:
                out[k] = {"score": 0.0}
        return out


_j = _ScriptedJev()


def fake_u(**overrides) -> dict:
    u = _u(intent="screen", channel="not_applicable", screen_task="not_applicable")
    u.update(overrides)
    return u


_REAL_UNDERSTAND = router.understand


def with_understand(u: dict):
    """Context manager: router.handle()'s call to understand() returns `u`,
    whatever utterance and contacts it was actually given."""
    class _Ctx:
        def __enter__(self):
            router.understand = lambda *a, **k: u
            return self

        def __exit__(self, *exc):
            router.understand = _REAL_UNDERSTAND
            return False
    return _Ctx()


def with_same_person(confidence: float = 1.0):
    """router._same_person() is a real Jev call (independent of understand()) that
    checks a named contact against her book. The router-level scenarios below drive
    it with a resolved fake contact, so this stands in for that one check too rather
    than needing a second recorded fixture."""
    real = router._same_person

    class _Ctx:
        def __enter__(self):
            router._same_person = lambda j, utterance, contact: confidence
            return self

        def __exit__(self, *exc):
            router._same_person = real
            return False
    return _Ctx()


def with_cancel_check(stop: bool, sconf: float = 1.0, instead: float = 0.0):
    """router._cancel_check() (is she saying no to a pending send?) is its own Jev
    call, independent of understand(). Stood in for the same reason as _same_person."""
    real = router._cancel_check

    class _Ctx:
        def __enter__(self):
            router._cancel_check = lambda j, utterance: (stop, sconf, instead)
            return self

        def __exit__(self, *exc):
            router._cancel_check = real
            return False
    return _Ctx()


def with_resume(slot: dict | None):
    """Context manager: router._resume() (the Jev call that reads her answer to a
    question MicMic asked) returns `slot` without touching Jev at all."""
    real = router._resume

    class _Ctx:
        def __enter__(self):
            router._resume = lambda j, utterance, contacts: slot
            return self

        def __exit__(self, *exc):
            router._resume = real
            return False
    return _Ctx()


def with_frontmost(app: str, pid: int = 4242):
    real = screen.frontmost

    class _Ctx:
        def __enter__(self):
            screen.frontmost = lambda: {"app": app, "pid": pid, "bundle_id": "",
                                        "window": app}
            return self

        def __exit__(self, *exc):
            screen.frontmost = real
            return False
    return _Ctx()


def with_capture(result: dict):
    real = screen.take_screenshot

    class _Ctx:
        def __enter__(self):
            screen.take_screenshot = lambda pid=None: dict(result)
            return self

        def __exit__(self, *exc):
            screen.take_screenshot = real
            return False
    return _Ctx()


class gates_on:
    def __enter__(self):
        self.prev = mac.SEND_FOR_REAL
        mac.SEND_FOR_REAL = True
        return self

    def __exit__(self, *exc):
        mac.SEND_FOR_REAL = self.prev
        return False


FAKE_PNG = os.path.join(_STATE, "MicMic screenshot fake.png")
with open(FAKE_PNG, "wb") as _fh:
    _fh.write(b"\x89PNG\r\n")


# ---------------------------------------------------------------- keyword matching
def t_keyword_matching():
    """1. _says_any recognises "screenshot" in all four languages, and the window
    scope words separately - pure string code, no model call."""
    hits = ["take a screenshot", "screenshot this window", "screen shot please",
            "צלמי מסך", "תצלמי את המסך", "צילום מסך של הדף",
            "لقطة شاشة", "صورة للشاشة", "التقطي لقطة",
            "скриншот", "сделай снимок экрана"]
    for phrase in hits:
        check(f"recognised as a screenshot request: {phrase!r}",
              router._says_any(phrase, router._SCREENSHOT_WORDS))
    misses = ["play me some music", "what is on my screen", "send this to Matan",
              "מה השעה", "תשלחי הודעה"]
    for phrase in misses:
        check(f"NOT a screenshot request: {phrase!r}",
              not router._says_any(phrase, router._SCREENSHOT_WORDS))
    windows = ["screenshot this window", "take a screenshot of the front window",
              "צלמי את החלון הזה", "لقطة شاشة لهذه النافذة", "скриншот этого окна"]
    for phrase in windows:
        check(f"window-scoped: {phrase!r}",
              router._says_any(phrase, router._SCREENSHOT_WINDOW_WORDS))
    check("a plain 'take a screenshot' is NOT window-scoped",
          not router._says_any("take a screenshot", router._SCREENSHOT_WINDOW_WORDS))


# ---------------------------------------------------------------- screen.py unit
def t_take_screenshot_unit():
    """2. screen.take_screenshot(): permission, capture, and window lookup, each in
    isolation, with subprocess and the permission check faked."""
    real_granted = screen._screen_recording_granted
    real_front = screen._front_window_id
    try:
        screen._screen_recording_granted = lambda: False
        res = screen.take_screenshot()
        check("no Screen Recording permission is reported honestly",
              res == {"ok": False, "why": "screen_recording"}, short(res))

        screen._screen_recording_granted = lambda: True
        real_capture_run = _sp_mod.run

        def fake_capture(cmd, *a, **kw):
            # Actually drop a file where screencapture would have, so the size check
            # after it passes exactly as it does against a real capture.
            path = cmd[-1]
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG\r\n")
            class _R:
                returncode, stdout, stderr = 0, "", ""
            return _R()

        screen.subprocess.run = fake_capture
        res = screen.take_screenshot()
        check("a granted, successful capture reports ok with a path",
              res.get("ok") and res.get("path", "").endswith(".png"), short(res))
        check("it is saved under the Desktop, named MicMic screenshot ...",
              "Desktop" in res.get("path", "") and "MicMic screenshot" in res.get("path", ""),
              short(res))
        check("the picture is copied to the clipboard (mac.copy_file_to_clipboard, "
              "stubbed here; the real one is probed in t_mac_unit)",
              CLIP and CLIP[-1] == res["path"], short(CLIP))

        screen._front_window_id = lambda pid: None
        res = screen.take_screenshot(pid=123)
        check("a window scope with no matching window fails honestly, not silently",
              res == {"ok": False, "why": "no_window"}, short(res))

        screen._front_window_id = lambda pid: 55
        res = screen.take_screenshot(pid=123)
        check("a window scope with a real window still succeeds",
              res.get("ok"), short(res))

        def empty_capture(cmd, *a, **kw):
            open(cmd[-1], "wb").close()          # 0 bytes: exactly what a failed
            class _R:                            # screencapture leaves behind
                returncode, stdout, stderr = 0, "", ""
            return _R()

        screen.subprocess.run = empty_capture
        res = screen.take_screenshot()
        check("an empty file from screencapture is reported as a failed capture",
              res == {"ok": False, "why": "capture_failed"}, short(res))
        screen.subprocess.run = real_capture_run
    finally:
        screen._screen_recording_granted = real_granted
        screen._front_window_id = real_front
        screen.subprocess.run = _blocked_run
        OSA.clear()


# ---------------------------------------------------------------- mac.py unit
def t_mac_unit():
    """3. mac.send_message with an attachment, mac.whatsapp_open_chat, and
    mac.copy_file_to_clipboard, each against the blocked-osascript wall."""
    with gates_on():
        OSA.clear()
        ok, msg = REAL_SEND_MESSAGE("Zohar Levin", "", attachment="/tmp/x.png")
        check("an attachment-only send reaches osascript", not ok and bool(OSA),
              f"ok={ok} osa={OSA}")
        check("it sends the file, and no empty text line",
              "POSIX file" in OSA[-1] and '"/tmp/x.png"' in OSA[-1]
              and 'send ""' not in OSA[-1], short(OSA[-1]))

        OSA.clear()
        ok, msg = REAL_SEND_MESSAGE("Zohar Levin", "look at this", attachment="/tmp/x.png")
        check("attachment plus text sends both, file first",
              OSA[-1].index("POSIX file") < OSA[-1].index('"look at this"'), short(OSA[-1]))

        OSA.clear()
        ok = REAL_COPY_TO_CLIPBOARD("/tmp/x.png")
        check("copy_file_to_clipboard reaches osascript with the PNG class and the path",
              ok is False  # osascript is blocked here - only the script itself is checked
              and "PNGf" in OSA[-1] and "/tmp/x.png" in OSA[-1], short(OSA[-1]))

        LAUNCHED.clear()
        ok, msg = REAL_WHATSAPP_OPEN_CHAT("+972500000001")
        check("whatsapp_open_chat opens a chat via `open`, not osascript",
              ok and any(c[0] == "open" and "whatsapp://" in c[1] for c in LAUNCHED),
              f"launched={LAUNCHED}")
        check("it never presses Return - nothing may be claimed as sent",
              not any("keystroke" in s for s in OSA), short(OSA))

    mac.SEND_FOR_REAL = False
    ok, msg = REAL_SEND_MESSAGE("Zohar Levin", "", attachment="/tmp/x.png")
    check("with sending switched off, an attachment send is a dry run",
          ok and str(msg).startswith("[dry run]"), repr(msg))
    ok, msg = REAL_WHATSAPP_OPEN_CHAT("+972500000001")
    check("with sending switched off, opening a chat is also a dry run",
          ok and str(msg).startswith("[dry run]"), repr(msg))
    OSA.clear()
    LAUNCHED.clear()


# ---------------------------------------------------------------- router: plain take
def t_take_alone():
    """4. "take a screenshot", no one named: captured, saved, said plainly, and
    never offered an Undo (this project keeps no delete path)."""
    reset_state()
    u = fake_u(intent="screen", screen_task="not_applicable")
    with with_understand(u), with_frontmost("Safari"), \
         with_capture({"ok": True, "path": FAKE_PNG}):
        r = router.handle(_j, "take a screenshot", speak=False)
    check("a plain screenshot request is saved, not asked about",
          r["did"] == "screenshot_saved", short(r))
    check("the reply names the Desktop", "Desktop" in (r.get("say") or ""), short(r.get("say")))
    check("no Undo is offered for a screenshot",
          "undo" not in r or r.get("undo") in (None, False), short(r.get("undo")))
    check("nothing was sent", not SENT and not WA, f"sent={SENT} wa={WA}")


def t_take_window_scope():
    """5. "screenshot this window" passes the scope through to the capture."""
    reset_state()
    captured = {}

    def spy_capture(pid=None):
        captured["pid"] = pid
        return {"ok": True, "path": FAKE_PNG}

    u = fake_u(intent="screen")
    with with_understand(u), with_frontmost("Notes", pid=777):
        real = screen.take_screenshot
        screen.take_screenshot = spy_capture
        try:
            r = router.handle(_j, "screenshot this window", speak=False)
        finally:
            screen.take_screenshot = real
    check("a window-scoped request captures the front window's pid",
          captured.get("pid") == 777, short(captured))
    check("and still reports success", r["did"] == "screenshot_saved", short(r))


def t_take_no_permission():
    """6. Screen Recording not granted: told plainly, never silently."""
    reset_state()
    u = fake_u(intent="screen")
    with with_understand(u), with_frontmost("Safari"), \
         with_capture({"ok": False, "why": "screen_recording"}):
        r = router.handle(_j, "take a screenshot", speak=False)
    check("a missing permission is reported as such",
          r["did"] == "screenshot_no_permission", short(r))
    check("the reply says how to fix it (Screen Recording)",
          "Screen Recording" in (r.get("say") or ""), repr(r.get("say")))


def t_take_capture_failed():
    """7. A capture that fails for any other reason is reported, not swallowed."""
    reset_state()
    u = fake_u(intent="screen")
    with with_understand(u), with_frontmost("Safari"), \
         with_capture({"ok": False, "why": "capture_failed"}):
        r = router.handle(_j, "take a screenshot", speak=False)
    check("a failed capture is reported", r["did"] == "screenshot_failed", short(r))


def t_take_screen_locked():
    """8. A locked screen refuses the whole thing before it tries to capture."""
    reset_state()
    real_locked = mac.screen_locked
    mac.screen_locked = lambda: True
    try:
        u = fake_u(intent="screen")
        with with_understand(u), with_frontmost("Safari"):
            r = router.handle(_j, "take a screenshot", speak=False)
    finally:
        mac.screen_locked = real_locked
    check("a locked screen is refused up front", r["did"] == "screen_locked", short(r))


# ---------------------------------------------------------------- router: send it
def t_send_named_contact():
    """9. "send the screenshot to Zohar Levin": captured, armed with the normal
    countdown and read-back, and delivered with the file attached - all exactly
    like sending selected text, just with a picture standing in for it."""
    reset_state()
    with gates_on():
        router.CANCEL_WINDOW = 0.3
        u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0,
                  channel="not_applicable")
        with with_understand(u), with_same_person(), with_frontmost("Safari"), \
             with_capture({"ok": True, "path": FAKE_PNG}):
            r = router.handle(_j, "send the screenshot to Zohar Levin", speak=False)
        check("it is armed for sending, not sent on the spot",
              r["did"] == "sending" and not SENT, short(r))
        check("the read-back names her and says 'screenshot', not a raw placeholder",
              "Zohar" in (r.get("say") or "") and "{what}" not in (r.get("say") or ""),
              repr(r.get("say")))
        time.sleep(0.6)
        check("the countdown delivers it with the file attached",
              SENT == [("Zohar Levin", "")] and SENT_ATTACH == [("Zohar Levin", FAKE_PNG)],
              f"sent={SENT} attach={SENT_ATTACH}")
        check("a fired screenshot send says so out loud",
              any("שלחתי" in t or "Sent" in t for _, t in SAID), short(SAID))
        router.CANCEL_WINDOW = 6.0


def t_send_cancel():
    """10. The same "say no" window a text send gets - proving the attachment does
    not bypass the existing cancel machinery."""
    reset_state()
    with gates_on():
        router.CANCEL_WINDOW = 30.0
        u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0)
        with with_understand(u), with_same_person(), with_frontmost("Safari"), \
             with_capture({"ok": True, "path": FAKE_PNG}):
            r = router.handle(_j, "send the screenshot to Zohar Levin", speak=False)
        check("armed", r["did"] == "sending" and router.PENDING is not None, short(r))
        with with_cancel_check(stop=True):
            r2 = router.handle(_j, "no", speak=False)
        check("saying no cancels a screenshot send exactly like a text one",
              r2["did"] == "cancelled" and router.PENDING is None and not SENT,
              short(r2))
        router.CANCEL_WINDOW = 6.0


def t_send_no_contact_then_who():
    """11. "send a screenshot" with nobody named: it asks who, and remembers the
    captured file (not a fresh capture) for when she answers."""
    reset_state()
    with gates_on():
        router.CANCEL_WINDOW = 0.3
        u = fake_u(intent="message", contact="nobody", contact_named=0.0)
        with with_understand(u), with_frontmost("Safari"), \
             with_capture({"ok": True, "path": FAKE_PNG}):
            r = router.handle(_j, "send a screenshot", speak=False)
        check("with nobody named it asks who, rather than guessing",
              r["did"] == "need_who", short(r))
        aw = router.AWAITING or {}
        check("the captured file is held for the follow-up",
              aw.get("attachment") == FAKE_PNG and aw.get("from_screen") == "send_screenshot",
              short(aw))
        check("nothing was sent while it waited", not SENT, f"sent={SENT}")

        slot = {**aw, "contact": "Zohar Levin", "need": "who"}
        with with_resume(slot):
            r2 = router.handle(_j, "Zohar Levin", speak=False)
        check("naming her on the follow-up arms the send with the SAME file",
              r2["did"] == "sending" and router.PENDING is not None
              and router.PENDING.get("attachment") == FAKE_PNG, short(r2))
        time.sleep(0.6)
        check("it goes out to her, attached", SENT_ATTACH == [("Zohar Levin", FAKE_PNG)],
              f"attach={SENT_ATTACH}")
        router.CANCEL_WINDOW = 6.0


def t_send_disabled():
    """12. With sending switched off, a screenshot send refuses exactly like a
    text one - never a silent no-op."""
    reset_state()
    mac.SEND_FOR_REAL = False
    u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0)
    with with_understand(u), with_same_person(), with_frontmost("Safari"), \
         with_capture({"ok": True, "path": FAKE_PNG}):
        r = router.handle(_j, "send the screenshot to Zohar Levin", speak=False)
    check("send_disabled, not a silent success", r["did"] == "send_disabled", short(r))
    check("nothing armed and nothing sent", router.PENDING is None and not SENT, short(r))


def t_send_whatsapp_fallback():
    """13. Asked for WhatsApp by name: no fake send. The picture is on the
    clipboard already (from the capture); this only opens the chat and tells her
    to paste it, and never claims a delivery that did not happen."""
    reset_state()
    with gates_on():
        router.CANCEL_WINDOW = 0.3
        u = fake_u(intent="message", contact="Zohar Levin", contact_named=1.0,
                  channel="whatsapp")
        with with_understand(u), with_same_person(), with_frontmost("Safari"), \
             with_capture({"ok": True, "path": FAKE_PNG}):
            r = router.handle(_j, "send the screenshot to Zohar Levin on whatsapp",
                              speak=False)
        check("armed exactly like any other send", r["did"] == "sending", short(r))
        time.sleep(0.6)
        check("the chat is opened, not a message actually delivered",
              WA_OPENED == ["+972500000001"] and not WA and not SENT,
              f"opened={WA_OPENED} wa={WA} sent={SENT}")
        check("she is told to paste it herself",
              any("Command-V" in t or "V" in t and "Command" in t for _, t in SAID)
              or any("שלחתי" not in t and "מועתק" in t for _, t in SAID),
              short(SAID))
        router.CANCEL_WINDOW = 6.0


# ---------------------------------------------------------------- no delete path
def t_no_delete_path():
    """14. Rule 2, scoped to the two files this feature touches (the whole package
    is already swept by tests/test_micmic.py's own copy of this check)."""
    destructive = ["os.remove", "os.unlink", ".unlink(", "rmtree", "rm -rf",
                   "DELETE FROM", "DROP TABLE", "shutil.move"]
    hits = []
    for rel in ("savta/actions/screen.py", "savta/actions/mac.py", "savta/router.py"):
        p = ROOT / rel
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            for bad in destructive:
                if bad in line:
                    hits.append(f"{rel}:{n}: {line.strip()[:70]}")
    check("no destructive call in the files this feature touches", not hits,
          "; ".join(hits[:5]))


TESTS = [
    ("1. keyword matching (EN/HE/AR/RU)", t_keyword_matching),
    ("2. screen.take_screenshot()", t_take_screenshot_unit),
    ("3. mac.py: attachment send, clipboard, whatsapp_open_chat", t_mac_unit),
    ("4. take a screenshot, no one named", t_take_alone),
    ("5. screenshot this window", t_take_window_scope),
    ("6. no Screen Recording permission", t_take_no_permission),
    ("7. capture fails", t_take_capture_failed),
    ("8. screen is locked", t_take_screen_locked),
    ("9. send the screenshot to a named contact", t_send_named_contact),
    ("10. saying no cancels a screenshot send", t_send_cancel),
    ("11. send a screenshot, nobody named, then answer who", t_send_no_contact_then_who),
    ("12. sending switched off", t_send_disabled),
    ("13. WhatsApp fallback: paste it herself", t_send_whatsapp_fallback),
    ("14. no delete path", t_no_delete_path),
]


def main() -> int:
    for name, fn in TESTS:
        print(name)
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback
            FAILED.append((name, "raised an exception"))
            print(f"  FAIL  {name} raised an exception\n" +
                 "".join(f"          {l}\n" for l in traceback.format_exc().splitlines()))
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    print(f"replayed {replay.counts['replayed']}, live {replay.counts['live']} "
         f"(MICMIC_REPLAY={replay.MODE})")
    print(f"never sent for real: {len(OSA)} osascript calls escaped the stub "
         f"(must be 0 outside t_mac_unit/t_take_screenshot_unit's deliberate probes)")
    if FAILED:
        print("\nFailures:")
        for name, detail in FAILED:
            print(f"  - {name}\n      {detail}" if detail else f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
