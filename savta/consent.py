"""MicMic's own line before a macOS prompt it cannot avoid: Automation and Screen Recording.

The owner's rule (CLAUDE.md "PERMISSIONS"): everything after the onboarding is asked
just in time, with MicMic's own line first, and a refusal is never nagged. Until now
only the microphone, speech and the contacts read had a line; every Automation target
(Messages, Notes, Reminders, Calendar, Music, Spotify, Chrome, System Events, FaceTime,
Contacts) put macOS's raw "MicMic wants access to control ..." on her screen with no
word from MicMic, and Screen Recording was never asked at all (MicMic was not even in
its list). PERMISSION_FALLBACKS.md P0-17 / P0-18.

The line itself is an NSAlert, which only the app's own process can show: the listener
(native/listener.py) registers itself here when it shares the server's process (the
release bundle, listener.ensure_server). Unregistered (a test, a source build whose
listener is a separate process) there is no line and nothing is held back: explain()
says yes, the way contacts.may_read_first does, and ask_screen_recording() says
"unavailable" so the caller takes its no-permission path.

This is a contract seam. Extend it with optional arguments, never change these:

    register(fn)            fn(target: str) -> True | False | None
                            shows the target's line; True = Continue, False = Not now,
                            None = no answer in time (the alert may still be up)
    explain(target) -> bool call right before the FIRST Apple Event to `target` when
                            macOS has not been asked yet (mac._osa, consent "not asked").
                            True: go ahead (macOS asks next). False: she said Not now
                            this run, or did not answer: take the fallback, send nothing.
    forget(target)          she asked again explicitly ("yes, use Messages"): the next
                            explain() shows the line once more
    remember(target, yes)   an answer that arrived late, or that another line already
                            covered (the contacts line covers the Contacts app)
    answer(target)          True | False | None (not asked this run). Never prompts.
    register_screen(fn)     fn(first_time: bool) -> True | False | None
    ask_screen_recording()  "granted" | "asked" | "asked_before" | "declined" | "unavailable"
    note_screen_asked()     her Continue on the Screen Recording line arrived after the
                            turn stopped waiting: macOS was asked, record it
    note_screen_declined()  her Not now arrived late: hold it for the run, never nag

One line per target per run: a yes stands for the run, and so does a Not now (the
caller's fallback is the answer from then on, never a second alert), until forget().
"""
from __future__ import annotations

import threading

# The targets MicMic sends Apple Events to, by the name `tell application "..."` uses.
TARGETS = ("Messages", "Notes", "Reminders", "Calendar", "Music", "Spotify",
           "Google Chrome", "System Events", "FaceTime", "Contacts")

# Any spelling a caller may have at hand: the AppleScript name, its bundle id, the old
# iCal name. Matched lowercased; an unknown target keeps its own name (and gets the
# listener's generic line, which names it).
_ALIASES = {
    "messages": "Messages", "com.apple.mobilesms": "Messages", "imessage": "Messages",
    "notes": "Notes", "com.apple.notes": "Notes",
    "reminders": "Reminders", "com.apple.reminders": "Reminders",
    "calendar": "Calendar", "ical": "Calendar", "com.apple.ical": "Calendar",
    "music": "Music", "com.apple.music": "Music", "itunes": "Music",
    "spotify": "Spotify", "com.spotify.client": "Spotify",
    "google chrome": "Google Chrome", "chrome": "Google Chrome",
    "com.google.chrome": "Google Chrome",
    "system events": "System Events", "com.apple.systemevents": "System Events",
    "facetime": "FaceTime", "com.apple.facetime": "FaceTime",
    "contacts": "Contacts", "com.apple.addressbook": "Contacts", "address book": "Contacts",
}


def canonical(target) -> str:
    t = str(target or "").strip()
    return _ALIASES.get(t.lower(), t[:40])


_lock = threading.Lock()
_target_locks: dict = {}
_answers: dict = {}             # canonical target -> True | False, this run only
_explainer = None


def register(fn) -> None:
    """The listener's line (native/listener.py Listener.explain_automation)."""
    global _explainer
    _explainer = fn


def registered() -> bool:
    return _explainer is not None


def answer(target):
    """Her answer to this target's line this run: True, False, or None. Never prompts."""
    return _answers.get(canonical(target))


def remember(target, yes: bool) -> None:
    t = canonical(target)
    if t:
        _answers[t] = bool(yes)


def forget(target) -> None:
    _answers.pop(canonical(target), None)


def _lock_for(t: str) -> threading.Lock:
    with _lock:
        lk = _target_locks.get(t)
        if lk is None:
            lk = _target_locks[t] = threading.Lock()
        return lk


def explain(target) -> bool:
    """MicMic's line before macOS first asks about `target`; True when the Apple Event
    may go ahead. Two turns racing for the same target see one alert: the second waits
    for the first one's answer."""
    t = canonical(target)
    if not t:
        return True
    if t in _answers:
        return _answers[t]
    fn = _explainer
    if fn is None:
        return True                   # no line to show: her request goes ahead as asked
    with _lock_for(t):
        if t in _answers:
            return _answers[t]
        try:
            got = fn(t)
        except Exception:  # noqa: BLE001  a broken alert must not cost her the request
            return True
        if got is None:
            return False              # not answered in time: nothing goes out this turn
        _answers[t] = bool(got)
        return _answers[t]


# ---------------------------------------------------------------- Screen Recording
# macOS lists an app under Screen Recording only after it has called
# CGRequestScreenCaptureAccess() once, and shows its dialog only that first time; the
# grant is seen only after MicMic restarts. MicMic used to only preflight, so her
# "turn MicMic on" found no MicMic to turn on (PERMISSION_FALLBACKS.md 2.15).
_screen_asker = None
_screen_state: list = [""]       # "" | "asked" | "declined", this run
_screen_lock = threading.Lock()
SCREEN_MARKER = "screen_capture_asked"


def register_screen(fn) -> None:
    """The listener's ask (Listener.ask_screen_recording): its line, then macOS's
    request, then the restart line."""
    global _screen_asker
    _screen_asker = fn


def _screen_granted() -> bool:
    """Preflight only: reads the grant, never prompts."""
    try:
        from .actions import screen
        return bool(screen._screen_recording_granted())
    except Exception:  # noqa: BLE001
        return False


def _marker():
    from . import paths
    return paths.state(SCREEN_MARKER)


def _asked_on_this_mac() -> bool:
    try:
        return _marker().exists()
    except OSError:
        return False


def _note_asked() -> None:
    try:
        _marker().write_text("1")
    except OSError:
        pass


def ask_screen_recording() -> str:
    """Ask for Screen Recording once this run, after MicMic's own line.

    "granted"       already on: take the screenshot
    "asked"         she said Continue just now: macOS was asked (or, asked on an earlier
                    run, the pane was offered); MicMic must restart before it works
    "asked_before"  asked earlier this run and still off: no second alert
    "declined"      Not now (this call or earlier this run), or no answer in time
    "unavailable"   nobody here can show the line (a test, a separate-process listener)
    Anything but "granted" means: take the no-permission path for this turn."""
    if _screen_granted():
        return "granted"
    with _screen_lock:
        st = _screen_state[0]
        if st == "asked":
            return "asked_before"
        if st == "declined":
            return "declined"
        fn = _screen_asker
        if fn is None:
            return "unavailable"
        try:
            got = fn(not _asked_on_this_mac())
        except Exception:  # noqa: BLE001
            return "unavailable"
        if got is None:
            # Still on screen (or another line of it is): no answer is not a yes. Her
            # answer, when it comes, arrives through note_screen_asked/declined.
            return "declined"
        if got:
            _note_asked()
            _screen_state[0] = "asked"
            return "asked"
        _screen_state[0] = "declined"
        return "declined"


def note_screen_asked() -> None:
    """A Continue that arrived after ask_screen_recording() stopped waiting (the
    listener calls this from the alert's yes). macOS's one-time request already went
    out, so the next ask must not claim a first time: the marker and "asked" both hold,
    the way remember() keeps a late Continue on an Automation line. No lock: it runs on
    the main thread, which a waiting ask_screen_recording() may be blocked on."""
    _note_asked()
    _screen_state[0] = "asked"


def note_screen_declined() -> None:
    """A Not now that arrived late: it stands for the run, like an on-time one."""
    if _screen_state[0] != "asked":
        _screen_state[0] = "declined"


def forget_screen() -> None:
    """She asked again explicitly: the next ask_screen_recording() shows the line."""
    _screen_state[0] = ""


def _reset_for_tests() -> None:
    global _explainer, _screen_asker
    _explainer = None
    _screen_asker = None
    _answers.clear()
    _screen_state[0] = ""
