"""macOS actions. Anything that leaves the machine is gated behind an explicit opt-in."""
from __future__ import annotations
import os, re, subprocess, threading, time
from pathlib import Path

# Sending a message is outward facing and cannot be undone. It stays off until the
# owner of the machine turns it on deliberately.
SEND_FOR_REAL = os.environ.get("MICMIC_ALLOW_SEND", "").lower() in ("1", "true", "yes")

# Placing a call rings a real phone, so it has its own switch. Owner decision
# 2026-10-03: calls SHIP ON in the release. The call goes out as a facetime:// URL and
# FaceTime itself shows the number with its own Call button, so nothing rings before
# she confirms it there. Still behind SEND_FOR_REAL (off in every test process, on in
# the running server unless she turned sending off), and MICMIC_ALLOW_CALL=0 turns
# calls off on a machine that wants them off.
CALL_FOR_REAL = os.environ.get("MICMIC_ALLOW_CALL", "1").strip().lower() in ("1", "true", "yes")


def _osa(script: str, timeout: float = 4.0, target: str | None = None) -> tuple[bool, str]:
    """Run one AppleScript. Short by default: a script that hangs leaves her in silence.

    `target` (an app name, "Messages") makes it consent-aware first (see _tell): asked
    of macOS without a prompt, a denied target returns (False, "automation_denied:App")
    at once, and a target never asked gets MicMic's own line and a long timeout. The
    old two-argument call is unchanged. Inside this module and music.py every caller
    goes through _tell instead, which calls this with (script, timeout) only, so a test
    that replaces _osa with a two-argument stub still catches every script."""
    if target:
        go, timeout, why = _gate(target, timeout, launch=True, ask=True)
        if not go:
            return False, why
    return _run_osascript(script, timeout)


def _run_osascript(script: str, timeout: float) -> tuple[bool, str]:
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:200]


# ---------------------------------------------------------------- Automation consent
# The first Apple Event MicMic sends to an app raises macOS's "MicMic wants access to
# control X". That dialog blocks osascript until she answers, and the 4 s timeout used
# to kill the script while the dialog was still up: MicMic spoke a failure (or a false
# success) over it, and her later "Allow" did nothing. So before the first event to a
# target the answer is READ, never asked (AEDeterminePermissionToAutomateTarget with
# askUserIfNeeded false): granted runs as before; denied takes the fallback at once,
# with no dialog and no wait; not yet asked gets MicMic's own line first (the
# registered explainer, like contacts.set_explainer) and then a 60 s timeout, so her
# answer to macOS is the one that counts.
AUTOMATION_BUNDLES = {
    "Messages": "com.apple.MobileSMS", "Notes": "com.apple.Notes",
    "Reminders": "com.apple.reminders", "Calendar": "com.apple.iCal",
    "Music": "com.apple.Music", "Spotify": "com.spotify.client",
    "Google Chrome": "com.google.Chrome", "System Events": "com.apple.systemevents",
    "FaceTime": "com.apple.FaceTime", "Contacts": "com.apple.AddressBook",
    "WhatsApp": "net.whatsapp.WhatsApp", "Finder": "com.apple.finder",
}
FIRST_ASK_TIMEOUT = 60.0     # long enough to read macOS's dialog and answer it
LAUNCH_WAIT_S = 3.0          # a target must be running before its answer can be read
_GRANTED: set[str] = set()   # seen granted this run: not asked again
# A denial seen recently, for an app that is not running now: trusted for this long
# rather than starting the app hidden again on every request just to read "denied"
# once more. A switch she turns on in Settings counts after it (or at once, if the
# app is running, since a running app is always read afresh).
DENIED_MEMORY_S = 600.0
_DENIED_AT: dict[str, float] = {}
_explain_automation = None   # fn(app) -> bool: MicMic's line; False = "Not now"

# AEDeterminePermissionToAutomateTarget results (AppleEvents.h).
_AE_GRANTED, _AE_DENIED, _AE_NOT_ASKED, _AE_NOT_RUNNING = 0, -1743, -1744, -600


def set_automation_explainer(fn) -> None:
    """The listener (or any host with a UI) registers MicMic's own line here: fn(app)
    shows it ("To send it, MicMic uses the Messages app. macOS will ask you next.")
    and returns her answer, True to go on. Unset: no line, the script runs (with the
    long timeout) as her request asked. Same shape as contacts.set_explainer."""
    global _explain_automation
    _explain_automation = fn


set_explainer = set_automation_explainer


def _fake_consent(app: str) -> str | None:
    """MICMIC_FAKE_AUTOMATION="Messages=denied,Notes=not_asked,*=granted": a test's
    answer for each target, like MICMIC_FAKE_PERMISSIONS for the server."""
    spec = os.environ.get("MICMIC_FAKE_AUTOMATION")
    if spec is None:
        return None
    rows = dict(p.split("=", 1) for p in spec.split(",") if "=" in p)
    rows = {k.strip(): v.strip() for k, v in rows.items()}
    return rows.get(app, rows.get("*", "unknown"))


_CS = [None]


def _coreservices():
    if _CS[0] is None:
        import ctypes

        class AEDesc(ctypes.Structure):
            _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]
        cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        cs.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long,
                                    ctypes.POINTER(AEDesc)]
        cs.AECreateDesc.restype = ctypes.c_int16
        cs.AEDeterminePermissionToAutomateTarget.argtypes = [
            ctypes.POINTER(AEDesc), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_ubyte]
        cs.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
        cs.AEDisposeDesc.argtypes = [ctypes.POINTER(AEDesc)]
        cs.AEDisposeDesc.restype = ctypes.c_int16
        _CS[0] = (ctypes, cs, AEDesc)
    return _CS[0]


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode("ascii"), "big")


def automation_consent(app: str) -> str:
    """macOS's answer for MicMic sending Apple Events to `app`, read WITHOUT a prompt:
    "granted", "denied", "not_asked", "not_running" (macOS can only tell for a running
    target) or "unknown" (an app not in AUTOMATION_BUNDLES, an error, or a test
    process: MICMIC_STATE_DIR never reads the real answer, MICMIC_FAKE_AUTOMATION
    gives one)."""
    fake = _fake_consent(app)
    if fake is not None:
        return fake
    if os.environ.get("MICMIC_STATE_DIR"):
        return "unknown"
    bundle = AUTOMATION_BUNDLES.get(app)
    if not bundle:
        return "unknown"
    try:
        ctypes, cs, AEDesc = _coreservices()
        desc, raw = AEDesc(), bundle.encode("utf-8")
        if cs.AECreateDesc(_fourcc("bund"), raw, len(raw), ctypes.byref(desc)) != 0:
            return "unknown"
        try:
            st = cs.AEDeterminePermissionToAutomateTarget(
                ctypes.byref(desc), _fourcc("****"), _fourcc("****"), 0)
        finally:
            cs.AEDisposeDesc(ctypes.byref(desc))
    except Exception:  # noqa: BLE001
        return "unknown"
    return {_AE_GRANTED: "granted", _AE_DENIED: "denied", _AE_NOT_ASKED: "not_asked",
            _AE_NOT_RUNNING: "not_running"}.get(st, "unknown")


def automation_states(apps=None) -> dict:
    """{app: automation_consent(app)} for Settings > Permissions. Read only: nothing is
    launched and nothing is asked."""
    return {a: automation_consent(a) for a in (apps or AUTOMATION_BUNDLES)}


def _launch_hidden(app: str) -> str:
    """Start `app` hidden and in the background (its script would launch it anyway),
    then read its answer again once it is running."""
    bundle = AUTOMATION_BUNDLES.get(app)
    if not bundle:
        return "unknown"
    try:
        subprocess.run(["open", "-g", "-j", "-b", bundle], capture_output=True, timeout=8)
    except Exception:  # noqa: BLE001
        return "not_running"
    st, end = "not_running", time.time() + LAUNCH_WAIT_S
    while True:
        st = automation_consent(app)
        if st != "not_running" or time.time() >= end:
            return st
        time.sleep(0.25)


def _explain(app: str) -> bool:
    fn = _explain_automation
    if fn is None:
        return True
    try:
        return bool(fn(app))
    except Exception:  # noqa: BLE001
        return True


def _gate(app: str, timeout: float, launch: bool, ask: bool) -> tuple[bool, float, str]:
    """(go, timeout to use, why not). `launch`: the script itself would start the app
    (a guarded "if application X is running" script never does, and so never raises a
    prompt for an app that is not running). `ask`: False reads a not-yet-asked target
    as a refusal without showing anything, for checks nobody asked for."""
    if app in _GRANTED:
        return True, timeout, ""
    st = automation_consent(app)
    if st == "not_running" and launch:
        if time.time() - _DENIED_AT.get(app, -1e9) < DENIED_MEMORY_S:
            return False, timeout, f"automation_denied:{app}"
        st = _launch_hidden(app)
    if st == "granted":
        _GRANTED.add(app)
        _DENIED_AT.pop(app, None)
        return True, timeout, ""
    if st == "denied":
        _DENIED_AT[app] = time.time()
        return False, timeout, f"automation_denied:{app}"
    if st == "not_asked":
        if not ask:
            return False, timeout, f"automation_not_asked:{app}"
        if not _explain(app):
            return False, timeout, f"automation_declined:{app}"
        return True, max(timeout, FIRST_ASK_TIMEOUT), "first_ask"
    return True, timeout, ""


def _tell(app: str, script: str, timeout: float = 4.0, launch: bool = True,
          ask: bool = True) -> tuple[bool, str]:
    """An AppleScript to `app`, consent-aware (see _gate), through _osa(script, timeout)
    so a test stub of _osa still sees it. Failures come back named: automation_denied,
    automation_declined (her "Not now" to MicMic's line), automation_not_asked (ask
    False), automation_unanswered (macOS's dialog was still up after 60 s)."""
    go, t, why = _gate(app, timeout, launch, ask)
    if not go:
        return False, why
    ok, out = _osa(script, timeout=t)
    if not ok:
        if "-1743" in (out or ""):
            # The app the refusal names, not always `app`: a Chrome script that also
            # reaches System Events is refused for System Events, and Chrome's own
            # grant must not be dropped for it.
            who = _denied_app(out, app)
            _GRANTED.discard(who)
            _DENIED_AT[who] = time.time()
            return False, f"automation_denied:{who}"
        if why == "first_ask" and "Timeout" in (out or ""):
            return False, f"automation_unanswered:{app}"
    return ok, out


_DENIED_TO = re.compile(r"send (?:apple ?events|keystrokes) to ([^.\n(]+?)\s*[.(]", re.I)


def _denied_app(err: str, fallback: str) -> str:
    """The app a -1743 error names ("Not authorized to send Apple events to System
    Events. (-1743)"), when it is one MicMic knows; else `fallback`."""
    m = _DENIED_TO.search(err or "")
    name = (m.group(1).strip() if m else "")
    return name if name in AUTOMATION_BUNDLES else fallback


def automation_ready(app: str) -> bool:
    """True when an Apple Event to `app` would go through with no question at all:
    granted this run, or granted on a read of the running app. Never launches the app
    and never asks, so it is the cheap check before something nobody needs right now
    (Music's library search: the catalog link plays without it). An app that is not
    running and not seen granted this run, not asked or denied, reads False.
    "unknown" (macOS could not be read; every test process) reads True, as _gate
    lets it through."""
    if app in _GRANTED:
        return True
    st = automation_consent(app)
    if st == "granted":
        _GRANTED.add(app)
        _DENIED_AT.pop(app, None)
        return True
    return st == "unknown"


def contacts(limit: int = 60) -> list[str]:
    """Names from the Contacts app. Falls back to an empty list if access is denied,
    which is normal until the user grants permission."""
    ok, out = _osa('tell application "Contacts" to get name of every person', timeout=8)
    if not ok or not out:
        return []
    names = [n.strip() for n in out.split(",") if n.strip()]
    seen, uniq = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower()); uniq.append(n)
    return uniq[:limit]


def send_message(name: str, text: str, attachment: str | None = None) -> tuple[bool, str]:
    """A text, a file, or both, to `name` over iMessage. `attachment` is a local path
    (a screenshot, so far); Messages accepts it directly as `POSIX file`, no separate
    upload step. When both are given the file goes first, exactly as it would if she
    had dragged it into the conversation and then typed a line under it."""
    if not SEND_FOR_REAL:
        what = f"{text!r}" if text else "a file"
        if attachment and text:
            what = f"{attachment} and {text!r}"
        elif attachment:
            what = attachment
        return True, f"[dry run] would send {what} to {name}"
    esc_n = name.replace("\\", "\\\\").replace('"', '\\"')
    lines = ['tell application "Messages"',
             '  set svc to 1st service whose service type = iMessage',
             f'  set bud to buddy "{esc_n}" of svc']
    if attachment:
        esc_p = attachment.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  send (POSIX file "{esc_p}") to bud')
    if text:
        esc_t = text.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  send "{esc_t}" to bud')
    lines.append('end tell')
    return _tell("Messages", "\n".join(lines))


def message_ready(name_or_number: str, text: str = "",
                  attachment: str | None = None) -> tuple[bool, str]:
    """Messages without Automation (P1-2): her words (or the picture) go on the
    clipboard and Messages is opened. Nothing is typed into it and nothing is pressed,
    so it never claims a send. The body is NOT prefilled in the URL: with the clipboard
    as well she could end up sending the words twice.

    Two different places, two different results, because the line she hears differs:
      (False, "ready_paste")  Messages opened ON THE PERSON (sms:<number> for a phone
                              number, imessage:<address> for an email): "Messages is
                              open on Dana. Paste with Command-V and press Return."
      (False, "ready_pick")   a name with no number or address: Messages itself, which
                              opens on WHATEVER conversation was last shown. She must
                              pick or start Dana's conversation first ("Start a message
                              to Dana, paste with Command-V and press Return."), never
                              be told to paste and press Return straight away.
    "_open" in place of "_paste" ("ready_open", "ready_pick_open") when it opened but
    the clipboard refused; (False, "missing") when Messages would not open. Dry run
    (sending off): nothing opened, the same ready_paste / ready_pick."""
    import urllib.parse
    who = (name_or_number or "").strip()
    digits = "".join(c for c in who if c.isdigit() or c == "+")
    if "@" in who and " " not in who:
        cmd, on_person = ["open", f"imessage:{urllib.parse.quote(who, safe='@+.')}"], True
    elif len(digits.lstrip("+")) >= 5:
        cmd, on_person = ["open", f"sms:{digits}"], True
    else:
        cmd, on_person = ["open", "-a", "Messages"], False
    base = "ready_paste" if on_person else "ready_pick"
    if not SEND_FOR_REAL:
        return False, base
    copied = (copy_file_to_clipboard(attachment) if attachment
              else _copy_text(text) if text else False)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=8)
    except Exception:  # noqa: BLE001
        return False, "missing"
    if r.returncode != 0:
        return False, "missing"
    if copied or not (text or attachment):
        return False, base
    return False, "ready_open" if on_person else "ready_pick_open"


def _copy_text(text: str) -> bool:
    """Her words on the clipboard (local, nothing sent). pbcopy, as targets._copy."""
    try:
        return subprocess.run(["pbcopy"], input=(text or "").encode("utf-8"),
                              timeout=4).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def copy_file_to_clipboard(path: str) -> bool:
    """Put a file's own bytes on the clipboard - not its path as text - so a paste
    into Messages, WhatsApp or Mail drops the picture itself. Never gated behind
    SEND_FOR_REAL: nothing leaves the machine, the clipboard is local."""
    esc = path.replace("\\", "\\\\").replace('"', '\\"')
    ok, _ = _osa(f'set the clipboard to (read (POSIX file "{esc}") as «class PNGf»)',
                 timeout=6.0)
    return ok


def facetime(name: str, number: str = "", video: bool = True) -> tuple[bool, str]:
    """Get the call ready: the number goes to FaceTime as a facetime:// (video) or
    facetime-audio:// URL, which needs no permission, and FaceTime shows the number
    with its own Call button. NOTHING RINGS until she presses it, so this is never a
    placed call: (False, "facetime_confirm <digits>") when FaceTime took the URL (the
    same convention as add_event_ics and message_ready: ready, she confirms in the
    app's own sheet), (False, "open_failed <digits>") when it did not. A caller must
    not say "calling" on facetime_confirm, and an emergency loop must stop there
    rather than open FaceTime for the next contact. No number: FaceTime is opened with
    `open -a` (no Automation prompt) and the result is False. Dry run (calls off):
    (True, "[dry run] ..."), nothing opened."""
    target = (number or name or "").strip()
    digits = "".join(c for c in target if c.isdigit() or c == "+")
    if not digits.lstrip("+"):
        # No number to dial. Open FaceTime rather than pretending the call happened.
        ok, msg = open_app("FaceTime")
        return False, (f"no number for {name!r}; "
                       + ("opened FaceTime instead" if ok else f"FaceTime did not open ({msg})"))
    if not (SEND_FOR_REAL and CALL_FOR_REAL):
        return True, (f"[dry run] would {'video' if video else 'audio'} call {name} "
                      f"on {digits} (set MICMIC_ALLOW_CALL=1 to place real calls)")
    import urllib.parse
    scheme = "facetime" if video else "facetime-audio"
    try:
        r = subprocess.run(["open", f"{scheme}://{urllib.parse.quote(digits)}"],
                           capture_output=True, timeout=8)
        opened = r.returncode == 0
    except Exception:  # noqa: BLE001
        opened = False
    return False, (f"facetime_confirm {digits}" if opened else f"open_failed {digits}")


def screen_locked() -> bool:
    """Is the Mac locked right now?

    Worth knowing for two reasons. Nothing visual can be done while it is — no video
    plays, no window opens, and no other application will show its controls, which
    from the inside looks exactly like a missing permission. And she can still be
    heard while it is locked, so she has to be told why the thing she asked for did
    not appear rather than being left guessing.
    """
    try:
        import Quartz
        d = Quartz.CGSessionCopyCurrentDictionary()
        return bool(d and d.get("CGSSessionScreenIsLocked"))
    except Exception:  # noqa: BLE001
        return False        # cannot tell; assume not, and fail the normal way


def end_call() -> tuple[bool, str]:
    """Hang up. A false alarm has to be retractable, or she learns to stay quiet when
    something is actually wrong.

    FaceTime is asked to quit the way Cmd-Q does (quit_app: NSRunningApplication, no
    Automation prompt), which ends a call it is placing. (True, "asked ...") when it
    was asked, (True, "no_call") when FaceTime is not running (no call is going), or
    (False, why)."""
    pids = [p for r in running_apps() if r.get("name") == "FaceTime" for p in r.get("pids", [])]
    if not pids:
        return True, "no_call"
    return quit_app(pids)


def volume(delta: int) -> tuple[bool, str]:
    """Nudge the output volume and report where it landed, so the screen can show
    the real level rather than a guess that drifts away from the hardware."""
    ok, cur = _osa("output volume of (get volume settings)")
    try:
        new = max(0, min(100, int(cur) + delta))
    except ValueError:
        new = 50
    done, _ = _osa(f"set volume output volume {new}")
    return done, str(new)


# ---------------------------------------------------------------- ducking
# A song MicMic started plays out of the speakers straight back into the microphone.
# Chrome's recogniser only finalises a sentence on silence, and a song is never
# silent, so it showed her words as they came in, never finished them, and the listen
# window expired with the request dropped. The embedded player used to pause itself
# when the mic opened; a video in a browser tab cannot be paused from here without a
# play/pause TOGGLE, which would start music if nothing was playing. Lowering the
# volume is unambiguous and always reversible.
_DUCK_LOCK = threading.Lock()
_ducked_from: int | None = None
_duck_timer: threading.Timer | None = None
DUCK_TO = 12
# If the page vanishes mid-listen it never says "unduck", and she is left with a Mac
# that has gone quiet for no reason. So the duck undoes itself regardless.
DUCK_MAX_S = 75.0             # longer than the longest listen window Settings allows (60s)


def duck() -> bool:
    """Lower the output while she is speaking, remembering where it was."""
    global _ducked_from, _duck_timer
    with _DUCK_LOCK:
        if _ducked_from is None:
            ok, cur = _osa("output volume of (get volume settings)")
            try:
                cur_i = int(cur)
            except ValueError:
                return False
            if not ok or cur_i <= DUCK_TO:
                return True              # already quiet: nothing to lower, nothing to restore
            _ducked_from = cur_i
            _osa(f"set volume output volume {DUCK_TO}")
        if _duck_timer is not None:
            _duck_timer.cancel()
        _duck_timer = threading.Timer(DUCK_MAX_S, unduck)
        _duck_timer.daemon = True
        _duck_timer.start()
        return True


def unduck() -> bool:
    """Put the volume back where it was before duck(). Safe to call any number of times."""
    global _ducked_from, _duck_timer
    with _DUCK_LOCK:
        if _duck_timer is not None:
            _duck_timer.cancel()
            _duck_timer = None
        if _ducked_from is None:
            return True
        level, _ducked_from = _ducked_from, None
    ok, _ = _osa(f"set volume output volume {level}")
    return ok


def get_volume() -> int | None:
    """The output level right now, 0-100, so an Undo can put back exactly this."""
    ok, cur = _osa("output volume of (get volume settings)")
    try:
        return int(cur) if ok else None
    except ValueError:
        return None


def set_volume(level: int) -> bool:
    ok, _ = _osa(f"set volume output volume {max(0, min(100, int(level)))}")
    return ok


# ---------------------------------------------------------------- no mute as a pause
# A pause that Music, Spotify or Chrome would not take is NOT replaced by muting the
# Mac. MicMic speaks through the same output (say, afplay, the page's audio): the line
# explaining the mute could not be heard, nor any later reply, timer or reminder, and
# a mute lost across a restart could not be undone by voice. A failed pause comes back
# as pause_failed and she hears how to pause it herself (space bar, K).


def close_tab_with(fragment: str) -> bool:
    """Close only the Chrome tab whose address contains this fragment (a video id).

    Undo for "play this" must never be close_front_window(): that presses Cmd-W in
    whatever app is in front when she presses Undo, which by then may be a document
    in another app entirely. Finding the tab by its address can only close the thing
    MicMic opened; if it is already gone, nothing else is touched."""
    frag = fragment.replace('"', "")
    if not frag:
        return False
    # Never launches Chrome just to close a tab it cannot have.
    ok, out = _tell(
        "Google Chrome",
        'if application "Google Chrome" is not running then return 0\n'
        'tell application "Google Chrome"\n'
        '  set n to 0\n'
        '  repeat with w in windows\n'
        '    repeat with t in (tabs of w whose URL contains "' + frag + '")\n'
        '      close t\n'
        '      set n to n + 1\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return n\n'
        'end tell', timeout=6.0, launch=False)
    return ok and out.strip().isdigit() and int(out.strip()) > 0


def brightness(delta: int) -> tuple[bool, str]:
    """One brightness key's worth (1/16) up or down per unit of `delta`.

    First through DisplayServices, the private framework the brightness keys use
    (ctypes, no permission, built-in displays only); the level is read back, so "done"
    means the screen really changed. Then the old way, F1/F2 through System Events
    (needs Automation for System Events and Accessibility). Returns (True, "level:NN")
    (NN percent, read back), (True, "at_max"/"at_min") when it already was, (True,
    "keys") for the System Events path, or (False, why): why names the last thing that
    failed, "automation_denied:System Events" included. On False nothing changed, and
    open_display_settings() is the honest next step."""
    ok, how = _ds_step(delta)
    if ok:
        return True, how
    key = 144 if delta > 0 else 145  # F1/F2 brightness keys
    done, msg = _tell("System Events", f'tell application "System Events" to key code {key}')
    if done:
        return True, "keys"
    return False, msg if str(msg).startswith("automation_") else f"{how}; keys_failed"


def open_display_settings() -> tuple[bool, str]:
    """System Settings on Displays, where she can drag the brightness herself. No
    permission. (True, "opened_display_settings") or (False, why)."""
    try:
        r = subprocess.run(["open", "x-apple.systempreferences:com.apple.Displays-Settings"
                                    ".extension"], capture_output=True, timeout=8)
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:120]
    return (True, "opened_display_settings") if r.returncode == 0 else (False, "open_failed")


DS_STEP = 1 / 16             # one press of a brightness key
_DS = [None]


def _ds():
    """DisplayServices and CoreGraphics through ctypes, as {"displays", "get", "set"},
    or None. Never in a test process (MICMIC_STATE_DIR): a test must not change the
    brightness of the screen it runs on; tests hand in a fake by replacing _ds."""
    if os.environ.get("MICMIC_STATE_DIR"):
        return None
    if _DS[0] is None:
        try:
            import ctypes
            ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/"
                             "DisplayServices")
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32,
                                                        ctypes.POINTER(ctypes.c_float)]
            ds.DisplayServicesGetBrightness.restype = ctypes.c_int
            ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
            ds.DisplayServicesSetBrightness.restype = ctypes.c_int
            ds.DisplayServicesCanChangeBrightness.argtypes = [ctypes.c_uint32]
            ds.DisplayServicesCanChangeBrightness.restype = ctypes.c_bool
            cg.CGGetOnlineDisplayList.argtypes = [ctypes.c_uint32,
                                                  ctypes.POINTER(ctypes.c_uint32),
                                                  ctypes.POINTER(ctypes.c_uint32)]
            cg.CGGetOnlineDisplayList.restype = ctypes.c_int32
            cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
            cg.CGDisplayIsBuiltin.restype = ctypes.c_bool
        except Exception:  # noqa: BLE001
            return None

        def displays():
            ids, n = (ctypes.c_uint32 * 16)(), ctypes.c_uint32(0)
            if cg.CGGetOnlineDisplayList(16, ids, ctypes.byref(n)) != 0:
                return []
            return [ids[i] for i in range(n.value) if cg.CGDisplayIsBuiltin(ids[i])
                    and ds.DisplayServicesCanChangeBrightness(ids[i])]

        def get(d):
            f = ctypes.c_float()
            return f.value if ds.DisplayServicesGetBrightness(d, ctypes.byref(f)) == 0 else None

        def put(d, level):
            return ds.DisplayServicesSetBrightness(d, ctypes.c_float(level)) == 0

        _DS[0] = {"displays": displays, "get": get, "set": put}
    return _DS[0]


def _ds_step(delta: int) -> tuple[bool, str]:
    ds = _ds()
    if ds is None:
        return False, "no_displayservices"
    try:
        shown = ds["displays"]()
    except Exception:  # noqa: BLE001
        shown = []
    if not shown:
        return False, "no_builtin_display"
    for d in shown:
        cur = ds["get"](d)
        if cur is None:
            continue
        new = max(0.0, min(1.0, cur + delta * DS_STEP))
        if abs(new - cur) < 1e-3:
            return True, "at_max" if delta > 0 else "at_min"
        if not ds["set"](d, new):
            continue
        after = ds["get"](d)
        if after is not None and abs(after - cur) > 1e-3:
            return True, f"level:{round(after * 100)}"
    return False, "displayservices_refused"


_GENDER_MARK = __import__("re").compile(r"«([^«»|]*)\|([^«»|]*)»")


# The `say` process currently reading something out. Without holding on to it there is
# no way to stop a long answer, so "enough, stop" had nothing to act on: she had to sit
# through twenty seconds of speech before the machine would listen again.
_SPEAKING: dict = {}
_SPEECH_LOCK = threading.Lock()


def speaking() -> bool:
    """Is a sentence being read out right now? (by `say`, or by the natural voice)"""
    with _SPEECH_LOCK:
        proc = _SPEAKING.get("proc")
    if proc and proc.poll() is None:
        return True
    from .. import voice as _voice
    return _voice.busy()


def stop_speaking() -> bool:
    """Cut the current sentence off mid-word. True if something was actually stopped."""
    from .. import voice as _voice
    natural = _voice.stop()
    with _SPEECH_LOCK:
        proc = _SPEAKING.get("proc")
        _SPEAKING["proc"] = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            return True
        except Exception:  # noqa: BLE001
            return natural
    return natural


SAY_VOICES = {"hebrew": "Carmit", "arabic": "Majed", "russian": "Milena"}


def say(text: str, language: str = "english", tag: str | None = None,
        cache: bool = False, tag_after: str | None = None) -> None:
    """Speak a line, cut off whatever was being said. `tag` is an audio direction for
    the cloud voice only (savta/voice_tags.py), never words: no other engine hears it.
    `tag_after` ("Dana:") puts the tag after the LAST place those words appear instead
    of in front of the line: a friend's message in her friend's tone, MicMic's own words
    plain. `cache` lets MicMic's cloud keep the audio of a line many people hear; a line
    that may carry her words (a read-back, her messages, her screen) is never cached."""
    # Last line of defence. A gendered marker that reaches the speech synthesiser is
    # read out as punctuation, so nothing may leave here still carrying one.
    if "«" in text:
        from ..profile import load as _load
        keep = 2 if _load().get("gender") == "masculine" else 1
        text = _GENDER_MARK.sub(lambda m: m.group(keep), text)
    # What any voice should read: no emoji names, no "(Official Video)" (savta/voice.py).
    from .. import voice as _voice
    from .. import voice_tags as _tags
    lead = None
    cut = text.rfind(tag_after) if (tag and tag_after) else -1
    if cut >= 0:
        # The two halves are cleaned apart, so the tag lands exactly between them.
        lead = _voice.prepare(_tags.strip_tags(text[:cut + len(tag_after)]), language)
        rest = _voice.prepare(_tags.strip_tags(text[cut + len(tag_after):]), language)
        text = f"{lead} {rest}".strip() if rest else lead
    else:
        # No tag-shaped bracket is ever read out, by any engine: the tag travels apart.
        text = _voice.prepare(_tags.strip_tags(text), language)
    # Starting a new sentence always stops the old one. Two voices talking over each
    # other is worse than either of them, and it happens whenever an answer arrives
    # while a previous one is still being read out.
    stop_speaking()
    with _SPEECH_LOCK:
        _SPEAKING["at"] = time.time()
        _SPEAKING["text"] = text[:120]
    # The natural voice, when she chose it and it is ready; whatever it cannot say
    # (a failure halfway) comes back here as `say`.
    if _voice.speak(text, language, lambda rest: _say_process(rest, language),
                    tag=tag, cache=cache, **({"lead": lead} if lead else {})):
        return
    _say_process(text, language)


def _say_process(text: str, language: str) -> None:
    voice = SAY_VOICES.get(language)
    args = ["say"]
    if voice:
        args += ["-v", voice]
    args += ["-r", "170", text]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        with _SPEECH_LOCK:
            _SPEAKING["proc"] = proc
            _SPEAKING["at"] = time.time()
            _SPEAKING["text"] = text[:120]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- inventory
# Everything below enumerates real things on THIS machine so Jev can select one.
# Jev cannot invent an app name or a file path; it can only point at a row here.

def installed_apps(limit: int = 200) -> list[str]:
    import os
    out = []
    for d in ("/Applications", "/System/Applications",
              "/System/Applications/Utilities", os.path.expanduser("~/Applications")):
        try:
            for n in os.listdir(d):
                if n.endswith(".app"):
                    out.append(n[:-4])
        except OSError:
            pass
    return sorted(set(out))[:limit]


KIND_QUERY = {
    "photo":    'kMDItemContentTypeTree == "public.image"',
    "pdf":      'kMDItemContentTypeTree == "com.adobe.pdf"',
    "document": 'kMDItemContentTypeTree == "public.content"',
    "video":    'kMDItemContentTypeTree == "public.movie"',
    "any":      "",
}

# Spotlight's "public.content" includes source code, so a broad document search on a
# developer's machine returns .py files and never her actual letters. She will never
# want a source file, so filter to things a person opens and reads.
KIND_EXT = {
    "photo":    {"jpg", "jpeg", "png", "heic", "heif", "gif", "tiff", "tif", "webp", "bmp"},
    "pdf":      {"pdf"},
    "video":    {"mp4", "mov", "m4v", "avi", "mkv", "mpg", "mpeg", "webm"},
    "document": {"pdf", "doc", "docx", "pages", "rtf", "odt", "txt",
                 "key", "ppt", "pptx", "numbers", "xls", "xlsx", "csv", "epub"},
}
KIND_EXT["any"] = KIND_EXT["document"] | KIND_EXT["photo"] | KIND_EXT["video"]

_NOISE = ("/node_modules/", "/.git/", "/site-packages/", "/Library/", "/.Trash/",
          "/.venv/", "/dist/", "/build/", "/__pycache__/", "/.cache/")


def find_files(words: str = "", kind: str = "any", limit: int = 40,
               recent_days: int | None = None) -> list[dict]:
    """Spotlight search. Returns real paths only; Jev picks one of them."""
    import os, subprocess, threading, time, time
    # Ask Spotlight for the exact extensions we want. Its content-type tree is not
    # reliable here ("public.content" misses PDFs and sweeps in source code), and a
    # generic query buries real documents under cache files.
    exts = KIND_EXT.get(kind, KIND_EXT["any"])
    clauses = ["(" + " || ".join(f'kMDItemFSName == "*.{e}"c' for e in sorted(exts)) + ")"]
    if words.strip():
        # Match ANY of her words, not the exact phrase. "security guide" must find
        # "macOS_Security_Complete_Guide.pdf", which an exact-substring match never will.
        toks = [w for w in words.replace('"', " ").split() if len(w) > 2][:6]
        if toks:
            clauses.append("(" + " || ".join(
                f'kMDItemDisplayName == "*{w}*"cd' for w in toks) + ")")
    if recent_days:
        clauses.append(f"kMDItemContentModificationDate >= $time.today(-{recent_days})")
    q = " && ".join(clauses)
    home = os.path.expanduser("~")
    try:
        out = subprocess.run(["mdfind", "-onlyin", home, q],
                             capture_output=True, text=True, timeout=12).stdout
    except Exception:  # noqa: BLE001
        return []
    allowed = KIND_EXT.get(kind, KIND_EXT["any"])
    rows = []
    for path in out.splitlines()[:limit * 20]:
        if not path or any(n in path for n in _NOISE):
            continue
        ext = os.path.splitext(os.path.basename(path))[1].lstrip(".").lower()
        if ext not in allowed:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        rows.append({"path": path, "name": os.path.basename(path),
                     "folder": os.path.basename(os.path.dirname(path)),
                     "mtime": st.st_mtime,
                     "when": time.strftime("%d %b %Y", time.localtime(st.st_mtime)),
                     "mb": round(st.st_size / 1e6, 1)})
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: -r["mtime"])
    return rows


def open_path(path: str) -> tuple[bool, str]:
    import subprocess
    try:
        subprocess.run(["open", path], check=False, timeout=10)
        return True, path
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:150]


# ---------------------------------------------------------------- whatsapp
def whatsapp(name_or_number: str, text: str) -> tuple[bool, str]:
    """Open WhatsApp with the message composed. Sending is gated like Messages.

    Only a NUMBER is a resolved recipient. With none, WhatsApp's link opens its own
    "send to..." picker, and the return pressed 2.2 s later used to go to whatever
    chat or picker row happened to be in front. Now the words are left ready and
    nothing is pressed: (False, "ready"), and the caller tells her to pick the person
    and press send herself, never that it was sent."""
    import urllib.parse
    digits = "".join(c for c in name_or_number if c.isdigit() or c == "+")
    if not digits.lstrip("+"):
        ok, how = whatsapp_ready(text)
        return False, how if ok else "missing"
    if not SEND_FOR_REAL:
        return True, f"[dry run] would WhatsApp {name_or_number}: {text}"
    url = f"whatsapp://send?phone={urllib.parse.quote(digits)}&text={urllib.parse.quote(text)}"
    import subprocess
    subprocess.run(["open", url], check=False)
    # WhatsApp composes the message but does not send it; Return sends it. A keystroke
    # goes to whatever app is in FRONT, so it is pressed only when System Events is
    # already allowed (ask=False: macOS's first question would hold the keystroke for
    # up to a minute and let it land wherever she is by then) and only once WhatsApp is
    # confirmed in front, inside the same script. Anything less: (False, "ready"), the
    # chat open with her words in the box, and she presses Return herself.
    time.sleep(2.2)
    ok, out = _tell("System Events", _front_then('process "WhatsApp"', "keystroke return"),
                    ask=False)
    if ok and (out or "").strip() == "pressed":
        return True, "pressed"
    return False, "ready"


def _front_then(process: str, keys: str, check: str = "") -> str:
    """A System Events script that brings `process` to the front, confirms it IS the
    front app (and runs `check`, one AppleScript line that returns "moved" when the
    wrong thing is in front) and only then sends `keys`. Returns "pressed",
    "not_front" or "moved"."""
    name = process.split('"')[1]
    lines = ['tell application "System Events"',
             f'  set frontmost of {process} to true',
             '  delay 0.3',
             f'  if name of first application process whose frontmost is true is not "{name}" '
             'then return "not_front"',
             'end tell']
    if check:
        lines.append(check)
    lines += [f'tell application "System Events" to {keys}', 'return "pressed"']
    return "\n".join(lines)


def whatsapp_ready(text: str) -> tuple[bool, str]:
    """WhatsApp opened with her words ready and nobody chosen (its own "send to..."
    picker), the words on the clipboard as well. Nothing is pressed: she picks the
    person and presses send. (True, "ready"), or (False, "missing") with no WhatsApp."""
    import urllib.parse
    if not SEND_FOR_REAL:
        return True, "ready"            # [dry run]: nothing opened
    from .targets import _copy
    _copy(text)
    try:
        r = subprocess.run(["open", f"whatsapp://send?text={urllib.parse.quote(text)}"],
                           capture_output=True, timeout=8)
    except Exception:  # noqa: BLE001
        return False, "missing"
    return (True, "ready") if r.returncode == 0 else (False, "missing")


# ---------------------------------------------------------------- whatsapp, by its window
# Her book could not be read (no Contacts, no WhatsApp data permission), so a name
# has no number. WhatsApp's own window still has her chats, and Accessibility, which
# she gave at setup, reads it like VoiceOver does: its search field, the chats it
# finds, the message box, the Send button. No new permission is asked for. Only one
# chat that clearly carries the name she said is opened; the words are typed there,
# and Send is pressed only after her countdown, only if that chat is still in front
# with exactly her words in the box. Anything less and it is left ready for her.
_WA = "WhatsApp"
_WA_SEARCH = re.compile(r"search|חיפוש|חפש|بحث|ابحث|поиск|искать", re.I)
_WA_COMPOSE = re.compile(r"message|type|הודעה|הקלד|رسالة|اكتب|сообщени|введите", re.I)
_WA_SEND = re.compile(r"^\s*(?:send|שליחה|שלח|إرسال|ارسال|отправить)\s*$", re.I)


def _wa_ui_allowed() -> bool:
    """Never from a test process (MICMIC_STATE_DIR, as savta/dictation_apps.py), and
    never with sending switched off: driving her WhatsApp is an outward act."""
    return SEND_FOR_REAL and not os.environ.get("MICMIC_STATE_DIR")


def _row_name(label: str) -> str:
    """A chat row's label carries the name first, then the last message and the time."""
    return re.split(r"[,\n|·•]", label or "", maxsplit=1)[0].strip()


def wa_rows_named(said: str, labels: list[str]) -> list[str]:
    """The chat rows whose name is the name she said: every word of hers is a word of
    the row's name, spelled the way people.py hears names (Tamar, תמר, Тамар)."""
    from .. import people
    words = [w for w in re.findall(r"[^\W\d_]+", said or "") if len(w) > 1]
    if not words:
        return []
    out = []
    for lab in labels:
        name = _row_name(lab)
        parts = re.findall(r"[^\W\d_]+", name)
        if parts and all(any(people._same_spelling(w.lower(), p.lower()) for p in parts)
                         for w in words):
            if name not in out:
                out.append(name)
    return out


def whatsapp_find_chat(said: str, text: str) -> dict:
    """Find her chat with the person she named in WhatsApp's own window and leave her
    words typed in it, unsent. {"ok": bool, "who": the chat's name, "why": ...}; why is
    "no_accessibility", "no_whatsapp", "no_search", "not_found", "several" (more than one
    chat carries that name), "no_box" or "test"."""
    from . import apps
    if not _wa_ui_allowed():
        return {"ok": False, "who": "", "why": "test"}
    ok, _why = apps.available()
    if not ok:
        return {"ok": False, "who": "", "why": "no_accessibility"}
    if not apps.ensure_open(_WA):
        return {"ok": False, "who": "", "why": "no_whatsapp"}
    snap = apps.snapshot(_WA, max_elements=200)
    if snap.get("error"):
        return {"ok": False, "who": "", "why": "no_accessibility"}
    field = next((e for e in snap["elements"] if e["kind"] == "type"
                  and (e["role"] == "AXSearchField" or _WA_SEARCH.search(e["label"]))), None)
    if field is None:
        return {"ok": False, "who": "", "why": "no_search"}
    apps.act(snap, field, "type", said)
    time.sleep(0.9)
    snap = apps.snapshot(_WA, max_elements=200)
    rows = [e for e in snap.get("elements") or [] if e["kind"] == "click" and e["label"]]
    named = wa_rows_named(said, [e["label"] for e in rows])
    if len(named) != 1:
        return {"ok": False, "who": "", "why": "several" if named else "not_found"}
    row = next(e for e in rows if _row_name(e["label"]) == named[0])
    done, _msg = apps.act(snap, row, "click")
    if not done:
        return {"ok": False, "who": "", "why": "not_found"}
    time.sleep(0.8)
    snap = apps.snapshot(_WA, max_elements=200)
    box = next((e for e in snap.get("elements") or [] if e["kind"] == "type"
                and e["role"] != "AXSearchField" and not _WA_SEARCH.search(e["label"])
                and _WA_COMPOSE.search(e["label"])), None)
    if box is None or not apps.act(snap, box, "type", text)[0]:
        return {"ok": False, "who": named[0], "why": "no_box"}
    return {"ok": True, "who": named[0], "why": ""}


def whatsapp_press_send(who: str, text: str) -> tuple[bool, str]:
    """Her countdown ran out: press WhatsApp's Send, but only if her chat with `who` is
    still in front with exactly her words in the box. (True, "sent"), else (False,
    "ready") and nothing was pressed."""
    from . import apps
    if not SEND_FOR_REAL:
        return True, f"[dry run] would press Send in WhatsApp for {who}"
    if not _wa_ui_allowed():
        return False, "ready"
    snap = apps.snapshot(_WA, max_elements=200)
    if snap.get("error"):
        return False, "ready"
    # Her words in the box are the proof that this is the chat they were typed into:
    # WhatsApp keeps a draft per chat, so another chat in front shows another box.
    seen = [snap.get("title", "")] + (snap.get("text") or "").split(" | ")
    box = next((e for e in snap["elements"] if e["kind"] == "type"
                and _WA_COMPOSE.search(e["label"]) and not _WA_SEARCH.search(e["label"])), None)
    send = next((e for e in snap["elements"] if e["kind"] == "click"
                 and _WA_SEND.match(e["label"])), None)
    if (box is None or send is None or (box.get("value") or "").strip() != text.strip()[:80]
            or not wa_rows_named(who, seen)):
        return False, "ready"
    done, _msg = apps.act(snap, send, "click")
    return (True, "sent") if done else (False, "ready")


def whatsapp_open_chat(name_or_number: str) -> tuple[bool, str]:
    """Open a chat and leave it there, with nothing typed and nothing sent.

    WhatsApp desktop has no attach-a-file hook this project can drive the way Messages'
    `send (POSIX file ...)` does, so a screenshot cannot actually go out on this channel
    from here. What can honestly be done is put the picture on the clipboard (already
    done by the caller before this runs) and get her to the right conversation, so a
    single paste and Enter is all that is left for her - never claimed as sent, because
    it was not."""
    if not SEND_FOR_REAL:
        return True, f"[dry run] would open WhatsApp chat with {name_or_number}"
    import urllib.parse
    digits = "".join(c for c in name_or_number if c.isdigit() or c == "+")
    url = (f"whatsapp://send?phone={urllib.parse.quote(digits)}" if digits.lstrip("+")
           else "whatsapp://send")
    subprocess.run(["open", url], check=False)
    return True, "opened chat"


# ---------------------------------------------------------------- notes etc
# Where notes.txt used to be: next to the code. In the release that is INSIDE the
# signed bundle (MicMic.app/Contents/Resources/app/notes.txt), and a write there breaks
# the seal (savta/paths.py), silently, triggered exactly by a denied Notes Automation;
# an update also replaced the bundle and lost the notes. Kept only as the legacy copy
# that is carried over once; nothing is ever written there again.
NOTES_FALLBACK = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "notes.txt")


def notes_file() -> Path:
    """MicMic's own notes file in its state folder (Application Support/MicMic in the
    app, the project root in a checkout, MICMIC_STATE_DIR in a test). An older
    notes.txt from inside the app is copied across the first time, never moved."""
    from ..paths import state
    return state("notes.txt", legacy=Path(NOTES_FALLBACK))


def _keep_locally(text: str) -> tuple[bool, str]:
    """If Notes is not reachable, the words still must not be lost. She said something
    she wanted kept; dropping it is worse than any permission dialog."""
    try:
        f = notes_file()
        if ".app/Contents/" in str(f.resolve()):
            return False, "refused: inside the app bundle"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M')}  {text}\n")
        return True, f"kept in {f.name}"
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:90]


def read_local_notes(limit: int = 5) -> list[str]:
    """Newest first, and never the same note twice: hearing "buy eggs, buy eggs" read
    back makes it sound broken."""
    try:
        lines = [l.strip() for l in open(notes_file(), encoding="utf-8") if l.strip()]
    except Exception:  # noqa: BLE001
        return []
    seen, out = set(), []
    for line in reversed(lines):
        body = line.split("  ", 1)[-1].strip().lower()
        if body and body not in seen:
            seen.add(body)
            out.append(line)
        if len(out) >= limit:
            break
    return out


def make_note(text: str, title: str = "") -> tuple[bool, str]:
    """A note in her Notes app: (True, "saved in Notes"). When Notes cannot be reached
    (Automation denied or declined, no iCloud account, a timeout) the words are kept in
    MicMic's own notes file instead and the result is still True, because they are kept,
    but the text starts with "kept_locally" so the reply can say where they went:
    (True, "kept_locally (<why Notes failed>)"). (False, why) only when even that failed."""
    body = (title + "<br>" + text) if title else text
    esc = body.replace("\\", "\\\\").replace('"', '\\"')
    ok, msg = _tell("Notes", f'tell application "Notes" to make new note at folder "Notes" '
                             f'of account "iCloud" with properties {{body:"{esc}"}}')
    if ok:
        return True, "saved in Notes"
    ok2, msg2 = _keep_locally(text)
    return ok2, (f"kept_locally ({str(msg)[:60]})" if ok2 else f"not_kept ({msg2})")


def _reminder_date(date: str, at: str = "") -> str | None:
    """AppleScript for the due moment in variable d, built field by field (see
    add_event), or None when `date` is not YYYY-MM-DD or `at` not HH:MM."""
    import re as _re
    m = _re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date or "")
    t = _re.fullmatch(r"(\d{1,2}):(\d{2})", at or "") if at else None
    if not m or (at and not t):
        return None
    y, mo, d = (int(x) for x in m.groups())
    h, mi = (int(x) for x in t.groups()) if t else (9, 0)
    return _as_date("d", y, mo, d, h, mi)


def add_reminder(text: str, date: str = "", at: str = "") -> tuple[bool, str]:
    """A row in her Reminders list. `date` (YYYY-MM-DD) makes it due that day, and `at`
    (HH:MM) at that time, when Reminders itself will alert her. On success the text is
    the new row's id: what a follow-up ("make it the day after") changes, and nothing
    else.

    When Reminders cannot be reached the words are kept in MicMic's notes file so they
    are not lost, but nothing in Reminders will ever alert her, so this is NOT a
    success: (False, "kept_locally"). The caller sets MicMic's own spoken reminder
    (set_timer) and says so. (False, "not_kept: ...") when even the notes file
    refused, (False, "bad date") for a date or time it cannot read."""
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    when = _reminder_date(date, at) if date else ""
    if when is None:
        return False, "bad date"
    due = ("" if not date else ", allday due date:d" if not at
           else ", due date:d, remind me date:d")
    ok, msg = _tell("Reminders", when + 'tell application "Reminders"\n'
                    f'  set r to make new reminder with properties {{name:"{esc}"{due}}}\n'
                    '  return id of r\nend tell')
    if ok:
        return True, msg.strip() or "saved in Reminders"
    ok2, msg2 = _keep_locally(text + (f" ({date}{' ' + at if at else ''})" if date else ""))
    return False, ("kept_locally" if ok2 else f"not_kept: {msg2}")


def change_reminder(rid: str, date: str, at: str = "") -> tuple[bool, str]:
    """Move the one reminder MicMic added, found by its id, to another day or time.
    (True, "ok"), (False, "missing") when it is not there any more, or (False, why).
    Never looks at, and so never changes, any other reminder; nothing is deleted."""
    when = _reminder_date(date, at)
    if not rid or when is None or not rid.startswith("x-apple-reminder://"):
        return False, "missing" if not rid else "bad id or date"
    sets = ("  set allday due date of r to d\n" if not at
            else "  set due date of r to d\n  set remind me date of r to d\n")
    ok, out = _tell("Reminders", when + 'tell application "Reminders"\n'
                    f'  set rs to (every reminder whose id is "{_esc(rid)}")\n'
                    '  if (count of rs) is 0 then return "missing"\n'
                    '  set r to item 1 of rs\n' + sets +
                    '  return "ok"\nend tell', timeout=10.0)
    if ok and out.strip() == "missing":
        return False, "missing"
    return ok and out.strip() == "ok", out


def music(action: str, query: str = "") -> tuple[bool, str]:
    table = {"play": "play", "pause": "pause", "next": "next track",
             "previous": "previous track", "stop": "stop"}
    cmd = table.get(action)
    if cmd:
        return _osa(f'tell application "Music" to {cmd}')
    return False, "unknown music action"


def battery() -> str:
    import subprocess
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=8).stdout
        import re as _re
        m = _re.search(r"(\d+)%", out)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def open_url(url: str) -> None:
    """Open a link where the user is actually looking.

    A bare `open` uses the DEFAULT browser, which on this machine is Safari while
    everything else about MicMic happens in Chrome: the page itself, and the browser
    agent, which is launched with channel="chrome". A song landing in a second browser
    the user was not using reads as nothing having happened at all. Chrome when it is
    installed, whatever they have set otherwise.
    """
    if Path("/Applications/Google Chrome.app").exists():
        # A cold `open -a "Google Chrome" <url>` launches Chrome and drops the link:
        # measured, it came up on chrome://newtab/ with the URL gone, which looks
        # exactly like nothing happened. Warm, the same command is reliable. So make
        # it warm first, then hand the link over.
        if not _running("Google Chrome"):
            subprocess.run(["open", "-a", "Google Chrome"], check=False)
            for _ in range(24):
                time.sleep(0.25)
                if _running("Google Chrome"):
                    break
            time.sleep(0.7)          # the first window still has to exist
        if subprocess.run(["open", "-a", "Google Chrome", url],
                          check=False).returncode == 0:
            return
    subprocess.run(["open", url], check=False)


def _running(app: str) -> bool:
    return subprocess.run(["pgrep", "-x", app], capture_output=True).returncode == 0


def open_app(app: str) -> tuple[bool, str]:
    """`open -a` needs no automation permission and returns in about 100ms, where the
    AppleScript equivalent needs a grant per app and can hang without one."""
    try:
        p = subprocess.run(["open", "-a", app], capture_output=True, text=True, timeout=8)
        if p.returncode == 0:
            return True, app
        return False, (p.stderr or "could not open").strip()[:120]
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:120]


# ---------------------------------------------------------------- quitting apps
def _clean(name: str) -> str:
    """Strip invisible format characters. WhatsApp reports its own name as
    '\u200eWhatsApp' — a left-to-right mark in front — so matching on the raw string
    would never find it, and a candidate list shown to Jev would carry the junk."""
    import unicodedata
    return "".join(ch for ch in (name or "") if unicodedata.category(ch) != "Cf").strip()


def running_apps() -> list[dict]:
    """The programs open right now that a person would call an app: the ones with a
    Dock icon. One row per name, with every process id behind it — Chrome alone runs
    as three. Finder is left out: it cannot really be quit, it only relaunches.

    Read from `lsappinfo`, NOT NSWorkspace. NSWorkspace keeps its list current only
    through notifications delivered on a main run loop, which the server's request
    thread does not have. Measured: Calculator's process was gone within 0.25s of
    quitting and NSWorkspace still listed it two seconds later — and an app launched
    after the server started would never have appeared at all.
    """
    try:
        out = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True,
                             timeout=4).stdout
    except Exception:  # noqa: BLE001
        return []
    import re as _re
    rows: dict[str, list[int]] = {}
    name = None
    for line in out.splitlines():
        m = _re.match(r'\s*\d+\) "(.*)" ASN:', line)
        if m:
            name = _clean(m.group(1))
            continue
        m = _re.search(r'\bpid = (\d+)\b.*\btype="Foreground"', line)
        if m and name and name != "Finder":
            rows.setdefault(name, []).append(int(m.group(1)))
        if "pid =" in line:
            name = None
    return [{"name": n, "pids": pids} for n, pids in rows.items()]


def is_running(pids: list[int]) -> bool:
    """Asked of the process itself, so it cannot be stale."""
    for pid in pids:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            continue
        except PermissionError:
            return True              # exists, just not ours to signal
    return False


def quit_app(pids: list[int]) -> tuple[bool, str]:
    """Quit it the way Cmd-Q does. The app is ASKED, not killed, so anything unsaved
    gets its own "save changes?" prompt, exactly as if she had pressed it herself. By
    process id rather than by name, and with no Automation permission needed — which
    `tell application "X" to quit` would require, once per app."""
    try:
        from AppKit import NSRunningApplication
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:120]
    asked = 0
    for pid in pids:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None and app.terminate():
            asked += 1
    return asked > 0, f"asked {asked} of {len(pids)} to quit"


def close_front_window() -> tuple[bool, str]:
    """Cmd-W to the front window. Only with System Events already allowed (ask=False):
    behind macOS's first question the keystroke would wait up to a minute and then
    close whatever is in front by then, MicMic's own window included. Never asked:
    (False, "automation_not_asked:System Events") and nothing is pressed."""
    return _tell("System Events",
                 'tell application "System Events" to keystroke "w" using command down',
                 ask=False)


# ---------------------------------------------------------------- timers
_TIMERS: list[dict] = []
# Every spoken timer, reminder and alarm is also written to a small state file, so a
# "remind me tomorrow at 9" or an alarm is not lost when MicMic quits, the Mac restarts
# or an update installs. At launch the future ones are armed again and the ones that
# came due while MicMic was off are said once (router.restore_timers). An entry leaves
# the file when it goes off or is cancelled; going off CLAIMS it first, under a lock, so
# nothing is said twice, even by a second MicMic reading the same file.
_FILE_LOCK = threading.Lock()


def _timer_file() -> Path:
    from ..paths import state
    return state("timers.json")


def _read_entries() -> list[dict]:
    """The entries in the file. A file that is not ours to read (bad JSON, wrong shape)
    is kept beside it as timers.json.corrupt and read as empty: a broken file must never
    stop MicMic from starting, nor be silently overwritten without a copy."""
    import json
    f = _timer_file()
    try:
        raw = f.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return []
    try:
        d = json.loads(raw)
        rows = d.get("timers") if isinstance(d, dict) else None
        if not isinstance(rows, list):
            raise ValueError("no timers list")
        return [r for r in rows if isinstance(r, dict) and r.get("id")
                and isinstance(r.get("at"), (int, float))]
    except ValueError:
        try:
            (f.parent / (f.name + ".corrupt")).write_text(raw, encoding="utf-8")
        except OSError:
            pass
        return []


def _write_entries(rows: list[dict]) -> bool:
    """Atomically: a whole new file, then renamed over the old one."""
    import json
    f = _timer_file()
    tmp = f.parent / f"{f.name}.tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "timers": rows}, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, f)
        return True
    except OSError:
        return False


def _locked(fn):
    """Run fn(entries) -> (result, new entries or None) under the in-process lock and
    an advisory lock on the file, so two MicMics never interleave a read and a write."""
    import fcntl
    with _FILE_LOCK:
        lock = _timer_file().parent / "timers.json.lock"
        try:
            fh = open(lock, "a")
        except OSError:
            fh = None
        try:
            if fh is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            res, rows = fn(_read_entries())
            if rows is not None:
                ok = _write_entries(rows)
                if isinstance(res, dict):
                    res["persisted"] = ok
            return res
        finally:
            if fh is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()


def _persist(row: dict) -> bool:
    entry = {k: row.get(k) for k in ("id", "at", "text", "kind", "language", "lead")}
    entry["set_at"] = time.time()
    res = _locked(lambda rows: ({}, [r for r in rows if r.get("id") != entry["id"]] + [entry]))
    return bool(res.get("persisted"))


def _claim(timer_id: str) -> bool:
    """Take this entry out of the file. True when it was there: whoever claims it is
    the one who says it. False when it was not (already said, or cancelled)."""
    def take(rows):
        hit = any(r.get("id") == timer_id for r in rows)
        return ({"hit": hit}, [r for r in rows if r.get("id") != timer_id] if hit else None)
    return bool(_locked(take).get("hit"))


def _fire_words(kind: str, text: str, language: str, lead: str | None) -> str:
    first = lead or {"hebrew": "תזכורת.", "arabic": "تذكير.", "russian": "Напоминание.",
                     "english": "Reminder."}.get(language, "Reminder.")
    return f"{first} {text}".strip() if kind == "reminder" else first


def _fired_tag(row: dict, lang: str) -> dict:
    """The cloud voice's tag for a reminder or timer going off (savta/voice_tags.py):
    [excited] by day for a food timer or a birthday reminder (the birthday once a day),
    [softly] at night. It carries her words, so it is never cached."""
    from .. import voice as _v, voice_tags as _t
    try:
        if _v.plan(lang) != "eleven_turbo":
            return {}
        hour = time.localtime().tm_hour
        label = row.get("text") or ""
        good = (_t.food(label) if row.get("kind") == "timer"
                else _t.birthday(label) and _t.good_news_hours(hour)
                and _v.once_today("birthday"))
        tag = _t.choose("reminder_fired", hour=hour, language=lang, good_news=bool(good))
        return {"tag": tag} if tag else {}
    except Exception:  # noqa: BLE001
        return {}


def _arm(row: dict) -> dict:
    """Start the wall-clock timer for a row (new, or read back from the file)."""
    rid = row["id"]

    def fire():
        # Said only by whoever takes it out of the file. A row the file never held
        # (the disk refused the write) is still said: it was set in this process.
        if row.get("persisted") and not _claim(rid):
            _TIMERS[:] = [t for t in _TIMERS if t.get("id") != rid]
            return
        lang = row.get("language", "english")
        say(_fire_words(row.get("kind", "reminder"), row.get("text", ""), lang,
                        row.get("lead")), lang, **_fired_tag(row, lang))
        _TIMERS[:] = [t for t in _TIMERS if t.get("id") != rid]

    t = _WallTimer(row["at"], fire)
    row["timer"] = t
    t.start()
    _TIMERS.append(row)
    return row


class _WallTimer:
    """Goes off by the wall clock. threading.Timer counts monotonic time, which stops
    while the Mac sleeps: an alarm for 7:00 set at night went off late by the whole
    night. This waits in short steps and looks at the clock each time."""
    STEP = 20.0

    def __init__(self, at: float, fn):
        self.at, self.fn = at, fn
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            left = self.at - time.time()
            if left <= 0:
                self.fn()
                return
            if self._stop.wait(min(left, self.STEP)):
                return


def set_timer(minutes: float, text: str, language: str = "english",
              lead: str | None = None, kind: str = "reminder") -> dict:
    """A spoken reminder, timer or alarm. Needs no permission from anybody.

    Deliberately not a macOS reminder: those need an automation grant, and half the
    time what she wants is 'tell me in ten minutes', which is a sentence said out
    loud, not a row in a database. `lead` is what she hears first when it goes off
    ("Your timer is done.", "Alarm. It is 7:00."); without one, it is "Reminder." and
    then what it is about. Written to the state file too, so a restart keeps it."""
    when = time.time() + minutes * 60
    # "id": what a follow-up ("make it 10 minutes instead", "cancel it") points at,
    # so it moves or stops this reminder and never another one she has running.
    row = {"at": when, "text": text, "minutes": minutes, "id": f"{when:.6f}",
           "kind": kind, "language": language, "lead": lead}
    row["persisted"] = _persist(row)
    return _arm(row)


def restore_timers(now: float | None = None) -> list[dict]:
    """At launch: arm again every entry still to come, and return the ones that came
    due while MicMic was not running, each claimed (taken out of the file) so it is
    returned once, ever. Already-armed ids are left alone, so calling it twice arms
    nothing twice."""
    now = time.time() if now is None else now
    armed = {t.get("id") for t in _TIMERS}
    missed = []
    for e in _locked(lambda rows: (list(rows), None)) or []:
        if e["id"] in armed:
            continue
        if e["at"] > now:
            _arm({**e, "minutes": (e["at"] - now) / 60, "persisted": True})
        elif _claim(e["id"]):
            missed.append(e)
    return sorted(missed, key=lambda e: e["at"])


def timer_row(timer_id: str) -> dict | None:
    """The reminder with this id while it has not gone off yet, else None."""
    now = time.time()
    return next((t for t in _TIMERS if t.get("id") == timer_id and t["at"] > now), None)


def cancel_timer(timer_id: str) -> bool:
    """Stop this one pending reminder, and only it. Its line in MicMic's own state file
    goes with it; nothing of hers is deleted anywhere."""
    row = timer_row(timer_id)
    if row is None:
        return False
    try:
        row["timer"].cancel()
    except Exception:  # noqa: BLE001
        return False
    _TIMERS[:] = [t for t in _TIMERS if t is not row]
    _claim(timer_id)
    return True


def pending_timers() -> list[dict]:
    now = time.time()
    return [{"in_minutes": round((t["at"] - now) / 60, 1), "text": t["text"],
             "kind": t.get("kind", "reminder"), "at": t["at"], "id": t.get("id", "")}
            for t in _TIMERS if t["at"] > now]


def add_event(title: str, date: str, start: str, end: str = "",
              location: str = "", all_day: bool = False) -> tuple[bool, str]:
    """Add one event to her calendar. date is YYYY-MM-DD, start/end are HH:MM (24h).
    `all_day` ignores start/end and adds it as an all-day event instead - for a
    date on the screen with no time anywhere near it ("Pyramids of Giza (November
    28, 2026)"), rather than refusing the whole thing for want of a clock reading.

    The date is built field by field, never from a string: AppleScript parses date
    strings in the Mac's own locale, so "2026-10-01 10:00" means something different,
    or nothing, on a Hebrew or Russian system. Day is set to 1 first so that moving
    from the 31st into a shorter month cannot roll over into the month after.
    Written into the first calendar she can write to: the first one in the list is
    often a read-only subscription such as holidays, which silently refuses.

    On success the text is the calendar's name and the new event's uid, one per line:
    what a follow-up ("move it to Friday") needs to change this event and no other."""
    import re as _re
    m = _re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date or "")
    if not m:
        return False, "bad date"
    y, mo, d = (int(x) for x in m.groups())
    esc = _esc

    def when(var, h, mi):
        return _as_date(var, y, mo, d, h, mi)
    if all_day:
        script = (when("s", 0, 0) +
                  'tell application "Calendar"\n'
                  '  set cal to first calendar whose writable is true\n'
                  f'  set ev to make new event at end of events of cal with properties '
                  f'{{summary:"{esc(title)}", start date:s, allday event:true, '
                  f'location:"{esc(location)}"}}\n'
                  '  return (name of cal) & linefeed & (uid of ev)\n'
                  'end tell')
        return _tell("Calendar", script, timeout=10.0)
    t = _re.fullmatch(r"(\d{1,2}):(\d{2})", start or "")
    if not t:
        return False, "bad time"
    sh, sm = (int(x) for x in t.groups())
    e = _re.fullmatch(r"(\d{1,2}):(\d{2})", end or "")
    eh, em = (int(x) for x in e.groups()) if e else (sh + 1, sm)
    script = (when("s", sh, sm) + when("e", eh, em) +
              'tell application "Calendar"\n'
              '  set cal to first calendar whose writable is true\n'
              f'  set ev to make new event at end of events of cal with properties '
              f'{{summary:"{esc(title)}", start date:s, end date:e, location:"{esc(location)}"}}\n'
              '  return (name of cal) & linefeed & (uid of ev)\n'
              'end tell')
    ok, out = _tell("Calendar", script, timeout=10.0)
    return ok, out


def _esc(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace('"', '\\"')[:200]


def _as_date(var: str, y: int, mo: int, d: int, h: int, mi: int) -> str:
    """An AppleScript date built field by field (see add_event for why)."""
    return (f"set {var} to current date\n"
            f"set day of {var} to 1\n"
            f"set year of {var} to {y}\n"
            f"set month of {var} to {mo}\n"
            f"set day of {var} to {d}\n"
            f"set hours of {var} to {min(h, 23)}\n"
            f"set minutes of {var} to {mi}\n"
            f"set seconds of {var} to 0\n")


def change_event(uid: str, calendar: str, date: str, start: str = "", end: str = "",
                 all_day: bool = False) -> tuple[bool, str]:
    """Move or resize the one event MicMic created, found by its uid in the calendar it
    was written to. Returns (True, "ok"), (False, "missing") when it is not there any
    more (she deleted or moved it herself), or (False, why). Never looks at, and so can
    never change, any other event: an event without a uid on record is refused here."""
    import re as _re
    m = _re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date or "")
    if not (uid and calendar and m):
        return False, "missing" if not uid else "bad date"
    y, mo, d = (int(x) for x in m.groups())
    head = ('tell application "Calendar"\n'
            f'  set evs to (every event of (first calendar whose name is "{_esc(calendar)}") '
            f'whose uid is "{_esc(uid)}")\n'
            '  if (count of evs) is 0 then return "missing"\n'
            '  set ev to item 1 of evs\n')
    if all_day:
        script = (_as_date("s", y, mo, d, 0, 0) + head +
                  '  set allday event of ev to true\n'
                  '  set start date of ev to s\n'
                  '  return "ok"\nend tell')
    else:
        t = _re.fullmatch(r"(\d{1,2}):(\d{2})", start or "")
        e = _re.fullmatch(r"(\d{1,2}):(\d{2})", end or "")
        if not (t and e):
            return False, "bad time"
        (sh, sm), (eh, em) = ((int(x) for x in t.groups()), (int(x) for x in e.groups()))
        # Moved later, the end goes first, so the event never ends before it starts.
        script = (_as_date("s", y, mo, d, sh, sm) + _as_date("e", y, mo, d, eh, em) + head +
                  '  set allday event of ev to false\n'
                  '  if s > (end date of ev) then\n'
                  '    set end date of ev to e\n'
                  '    set start date of ev to s\n'
                  '  else\n'
                  '    set start date of ev to s\n'
                  '    set end date of ev to e\n'
                  '  end if\n'
                  '  return "ok"\nend tell')
    ok, out = _tell("Calendar", script, timeout=10.0)
    if ok and out.strip() == "missing":
        return False, "missing"
    return ok and out.strip() == "ok", out


# ---------------------------------------------------------------- Calendar without Automation
# P1-3: a one-event .ics file, opened in Calendar. Calendar shows its own "add event"
# sheet with her calendars to pick from, and she confirms there: MicMic needs no
# permission and nothing is added that she did not see.

def _ics_text(v: str) -> str:
    """RFC 5545 TEXT: backslash, semicolon, comma and newlines escaped."""
    return ((v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))[:300]


def _ics_fold(line: str) -> str:
    """Lines of at most 75 octets, continued with CRLF and a space (RFC 5545 3.1),
    never cutting a UTF-8 character in two."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def event_ics(title: str, date: str, start: str = "", end: str = "", location: str = "",
              all_day: bool = False, uid: str = "") -> str | None:
    """The .ics text for one event, in her Mac's local time (floating), or None when
    the date (YYYY-MM-DD) or time (HH:MM) cannot be read. No end: one hour."""
    import datetime as _dt
    import re as _re
    import uuid as _uuid
    m = _re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date or "")
    if not m:
        return None
    try:
        day = _dt.date(*(int(x) for x in m.groups()))
    except ValueError:
        return None
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MicMic//MicMic//EN",
            "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
            f"UID:{uid or _uuid.uuid4()}@micmic", f"DTSTAMP:{stamp}"]
    if all_day:
        rows += [f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                 f"DTEND;VALUE=DATE:{day + _dt.timedelta(days=1):%Y%m%d}"]
    else:
        t = _re.fullmatch(r"(\d{1,2}):(\d{2})", start or "")
        if not t or int(t.group(1)) > 23 or int(t.group(2)) > 59:
            return None
        s = _dt.datetime.combine(day, _dt.time(int(t.group(1)), int(t.group(2))))
        e = _re.fullmatch(r"(\d{1,2}):(\d{2})", end or "")
        if e and int(e.group(1)) <= 23 and int(e.group(2)) <= 59:
            fin = _dt.datetime.combine(day, _dt.time(int(e.group(1)), int(e.group(2))))
            if fin <= s:
                fin += _dt.timedelta(days=1)          # "10 pm to 1 am"
        else:
            fin = s + _dt.timedelta(hours=1)
        rows += [f"DTSTART:{s:%Y%m%dT%H%M%S}", f"DTEND:{fin:%Y%m%dT%H%M%S}"]
    rows.append(f"SUMMARY:{_ics_text(title) or 'Event'}")
    if location:
        rows.append(f"LOCATION:{_ics_text(location)}")
    rows += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_ics_fold(r) for r in rows) + "\r\n"


def add_event_ics(title: str, date: str, start: str = "", end: str = "",
                  location: str = "", all_day: bool = False) -> tuple[bool, str]:
    """Calendar's own sheet for one event, for when Calendar's Automation is denied.

    The event is written to MicMic's state folder (calendar/<id>.ics; files are only
    ever added there, never removed) and opened in Calendar, which asks her which
    calendar and adds it when she clicks OK. MicMic did not add it, so ok is False:
    (False, "ready_confirm") when Calendar was opened on it; (False, "bad date") /
    (False, "bad time") for what cannot be read; (False, "ics_failed ...") when the
    file could not be written or Calendar would not open it."""
    import uuid as _uuid
    uid = str(_uuid.uuid4())
    text = event_ics(title, date, start, end, location, all_day, uid=uid)
    if text is None:
        date_ok = event_ics("x", date, all_day=True) is not None
        return False, "bad time" if date_ok else "bad date"
    try:
        from ..paths import state_dir
        folder = state_dir() / "calendar"
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"{uid}.ics"
        if ".app/Contents/" in str(f.resolve()):
            return False, "ics_failed inside the app bundle"
        f.write_text(text, encoding="utf-8", newline="")
    except OSError as e:
        return False, f"ics_failed {repr(e)[:90]}"
    try:
        r = subprocess.run(["open", "-a", "Calendar", str(f)], capture_output=True, timeout=8)
    except Exception as e:  # noqa: BLE001
        return False, f"ics_failed {repr(e)[:90]}"
    return (False, "ready_confirm") if r.returncode == 0 else (False, "ics_failed open")


# ---------------------------------------------------------------- a video in a tab
# What MicMic put on in the browser (YouTube, Netflix and the rest) is driven in its own
# tab and nowhere else: the tab is found by the address MicMic opened (a video id, the
# service's host). If it is gone, nothing is pressed anywhere. Exact control through the
# page's own video element when Chrome allows JavaScript from Apple Events (off by
# default); otherwise the site's own keyboard shortcuts, pressed with that tab in front.
_VIDEO_JS = {
    "pause": "if(v.paused)return 'already';v.pause();return 'done';",
    "resume": "if(!v.paused)return 'already';v.play();return 'done';",
    "back_10": "v.currentTime=Math.max(0,v.currentTime-10);return 'done';",
    "forward_10": "v.currentTime=v.currentTime+10;return 'done';",
    "restart": "v.currentTime=0;v.play();return 'done';",
}
# System Events: a character, or (key code, modifiers).
_TAB_KEYS = {
    "youtube": {"pause": "k", "resume": "k", "back_10": "j", "forward_10": "l",
                "full_screen": "f", "exit_full_screen": (53, ""), "restart": "0",
                "next": ("n", "shift"), "next_episode": ("n", "shift"),
                "previous": ("p", "shift")},
    "other": {"pause": (49, ""), "resume": (49, ""), "back_10": (123, ""),
              "forward_10": (124, ""), "full_screen": "f", "exit_full_screen": (53, "")},
}


def _press(key) -> str:
    if isinstance(key, str):
        return f'tell application "System Events" to keystroke "{key}"'
    k, mods = key
    using = f" using {mods} down" if mods else ""
    if isinstance(k, str):
        return f'tell application "System Events" to keystroke "{k}"{using}'
    return f'tell application "System Events" to key code {k}{using}'


# Why the last tab_player() call did not do it ("" when it did): "automation_denied:
# Google Chrome", "automation_declined:System Events", "not_front" (Chrome was not in
# front when the key would have gone), "moved" (another tab was), "script". The return
# value stays one of the words the router already knows; this says which line fits.
LAST_TAB_FAILURE = [""]


def tab_player(fragment: str, action: str, site: str = "other") -> str:
    """Do `action` to the video in the Chrome tab whose address holds `fragment`.
    Returns "done", "already" (it was paused already, and so on), "keys" (the site's
    shortcut was pressed in that tab), "missing" (no such tab: nothing was touched),
    "unsupported" (that site has no way to do it), or "failed" (nothing was done; the
    reason in LAST_TAB_FAILURE[0]). Nothing is ever muted instead.

    Chrome's script finds the tab and runs the page's own player (JavaScript); when
    it cannot, the site's key is pressed in that tab. The key is a System Events
    keystroke, so System Events' consent is read (never asked) first:
      - already allowed (automation_ready): the key is pressed inside Chrome's script,
        right after it brought that tab to the front, as always;
      - anything else: Chrome's script only brings the tab to the front and answers
        "keys"; the key is then a script of its own through _tell("System Events"),
        so MicMic's line comes before macOS's first question about System Events too,
        and it is pressed only after confirming, in that same script, that Chrome is
        in front with this very tab active: after a minute behind macOS's dialog a
        blind key could land anywhere."""
    LAST_TAB_FAILURE[0] = ""
    frag = (fragment or "").replace('"', "")
    keys = _TAB_KEYS.get(site, _TAB_KEYS["other"])
    js = _VIDEO_JS.get(action)
    key = keys.get(action)
    if not frag or (js is None and key is None):
        return "unsupported" if frag else "missing"
    run_js = ""
    if js:
        body = ("(function(){var v=document.querySelector('video');"
                "if(!v)return 'novideo';" + js + "})()")
        run_js = ('        try\n'
                  f'          set r to (execute t javascript "{body}")\n'
                  '        end try\n')
    inline = key is not None and automation_ready("System Events")
    front = ('        if r is "keys" or r is "novideo" then\n'
             + ('          set active tab index of w to i\n'
                '          set index of w to 1\n'
                '          activate\n'
                + ('          delay 0.3\n'
                   f'          {_press(key)}\n' if inline else '')
                if key is not None else '          return "unsupported"\n')
             + '          return "keys"\n'
               '        end if\n')
    script = ('if application "Google Chrome" is not running then return "missing"\n'
              'tell application "Google Chrome"\n'
              '  repeat with w in windows\n'
              '    set i to 0\n'
              '    repeat with t in tabs of w\n'
              '      set i to i + 1\n'
              f'      if URL of t contains "{frag}" then\n'
              '        set r to "keys"\n'
              + run_js + front +
              '        return r\n'
              '      end if\n'
              '    end repeat\n'
              '  end repeat\n'
              '  return "missing"\n'
              'end tell')
    ok, out = _tell("Google Chrome", script, timeout=6.0, launch=False)
    out = (out or "").strip()
    if not ok:
        LAST_TAB_FAILURE[0] = out if out.startswith("automation_") else "script"
        return "failed"
    if out != "keys" or inline:
        if out in ("done", "already", "keys", "missing", "unsupported"):
            return out
        LAST_TAB_FAILURE[0] = "script"
        return "failed"
    press = _press(key).split(' to ', 1)[1]          # 'keystroke "k"', 'key code 49'
    check = ('tell application "Google Chrome" to if URL of active tab of front window '
             f'does not contain "{frag}" then return "moved"')
    done, why = _tell("System Events",
                      _front_then('process "Google Chrome"', press, check=check),
                      timeout=6.0)
    if done and (why or "").strip() == "pressed":
        return "keys"
    why = (why or "").strip()
    LAST_TAB_FAILURE[0] = (why if why.startswith("automation_") or why in ("not_front", "moved")
                           else "script")
    return "failed"


def cancel_last_timer() -> bool:
    """Undo for "remind me in five minutes": only the reminder just set, never the
    others she already had running."""
    while _TIMERS:
        t = _TIMERS.pop()
        if t["at"] > time.time():
            try:
                t["timer"].cancel()
            except Exception:  # noqa: BLE001
                return False
            _claim(t.get("id", ""))
            return True
    return False


def cancel_timers() -> int:
    n = 0
    for t in _TIMERS:
        try:
            t["timer"].cancel(); n += 1
        except Exception:  # noqa: BLE001
            pass
        _claim(t.get("id", ""))
    _TIMERS.clear()
    return n
