#!/usr/bin/env python3
"""Lane L3 of the permission fallbacks (~/jev/micmic-work/qa/PERMISSION_FALLBACKS.md):
savta/actions/mac.py and savta/actions/music.py without the happy path.

    cd <checkout> && uv run python tests/test_mac_fallbacks.py

Nothing leaves the machine and nothing on this Mac is touched: osascript is a fake
(mac._osa), every subprocess.run (open, pbcopy, osascript) is a recorder, Automation
consent is faked (MICMIC_FAKE_AUTOMATION or a replaced mac.automation_consent), and
DisplayServices is a fake (mac._ds). MICMIC_STATE_DIR is a scratch folder, which also
makes the real consent probe and the real DisplayServices refuse to run. No Jev, no
Gemini, no voice: zero paid calls.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-macfb-state-")
for _k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL", "MICMIC_FAKE_AUTOMATION"):
    os.environ.pop(_k, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------- the wall
_REAL_RUN = subprocess.run
LAUNCHED: list[list] = []
RC = {"open": 0, "pbcopy": 0}


def _blocked_run(cmd, *a, **kw):
    if isinstance(cmd, (list, tuple)) and cmd and cmd[0] in ("open", "osascript", "pbcopy"):
        LAUNCHED.append(list(cmd) + ([kw["input"].decode()] if cmd[0] == "pbcopy"
                                     and kw.get("input") else []))

        class _R:
            returncode = RC.get(cmd[0], 0)
            stdout, stderr = "", ""
        return _R()
    if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "lsappinfo":
        raise AssertionError("lsappinfo reached in a test")
    return _REAL_RUN(cmd, *a, **kw)


subprocess.run = _blocked_run

from savta.actions import mac          # noqa: E402
from savta.actions import music as am  # noqa: E402

OSA: list[tuple[str, float]] = []
OSA_RULES: list = []                    # (substring, (ok, out)); first match wins


def fake_osa(script: str, timeout: float = 4.0):
    OSA.append((script, timeout))
    for frag, res in OSA_RULES:
        if frag in script:
            return res
    return True, ""


REAL_OSA = mac._osa
mac._osa = fake_osa
mac.LAUNCH_WAIT_S = 0.0

PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail="") -> bool:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, str(detail)))
        print(f"  FAIL  {name}\n          {detail}")
    return bool(ok)


def reset(consent: str | None = None):
    OSA.clear()
    OSA_RULES.clear()
    LAUNCHED.clear()
    RC.update(open=0, pbcopy=0)
    mac._GRANTED.clear()
    mac._DENIED_AT.clear()
    mac.set_automation_explainer(None)
    if consent is None:
        os.environ.pop("MICMIC_FAKE_AUTOMATION", None)
    else:
        os.environ["MICMIC_FAKE_AUTOMATION"] = consent


class gates:
    def __init__(self, send=True, call=True):
        self.want = (send, call)

    def __enter__(self):
        self.prev = (mac.SEND_FOR_REAL, mac.CALL_FOR_REAL)
        mac.SEND_FOR_REAL, mac.CALL_FOR_REAL = self.want

    def __exit__(self, *exc):
        mac.SEND_FOR_REAL, mac.CALL_FOR_REAL = self.prev
        return False


# ================================================================== P0-16 consent
def t_consent():
    print("\nt_consent (P0-16)")
    reset()
    check("a test process never reads the real answer", mac.automation_consent("Messages")
          == "unknown" and mac._CS[0] is None, mac._CS[0])
    reset("Messages=denied,Notes=not_asked,*=granted")
    check("the fake answers per app, '*' for the rest",
          mac.automation_states(["Messages", "Notes", "Music"])
          == {"Messages": "denied", "Notes": "not_asked", "Music": "granted"})

    reset("Messages=denied")
    ok, why = mac._tell("Messages", 'tell application "Messages" to get name')
    check("denied: (False, automation_denied:Messages) at once, no script run",
          (ok, why) == (False, "automation_denied:Messages") and not OSA, (ok, why, OSA))

    reset("Messages=not_asked")
    order = []
    mac.set_automation_explainer(lambda app: (order.append(("line", app)), True)[1])
    mac._osa = lambda s, timeout=4.0: (order.append(("osa", timeout)), (True, "ok"))[1]
    try:
        ok, out = mac._tell("Messages", "x")
    finally:
        mac._osa = fake_osa
    check("not asked: MicMic's line first, then the script with the 60 s timeout",
          ok and order == [("line", "Messages"), ("osa", mac.FIRST_ASK_TIMEOUT)], order)

    reset("Messages=not_asked")
    mac.set_automation_explainer(lambda app: False)
    ok, why = mac._tell("Messages", "x")
    check("not asked and she said Not now: automation_declined, nothing run",
          (ok, why) == (False, "automation_declined:Messages") and not OSA, (ok, why, OSA))

    reset("Messages=not_asked")
    mac.set_automation_explainer(lambda app: 1 / 0)
    ok, _ = mac._tell("Messages", "x")
    check("an explainer that breaks does not block her request", ok and OSA[-1][1] == 60.0, OSA)

    reset("Messages=not_asked")
    ok, _ = mac._tell("Messages", "x", timeout=4.0)
    check("no explainer registered: runs, with the long timeout",
          ok and OSA == [("x", mac.FIRST_ASK_TIMEOUT)], OSA)

    # The 2026-10-07 shoot: "next" over MicMic's YouTube tab put up macOS's "MicMic
    # wants access to control Google Chrome" with no MicMic line, and the turn sat out
    # the 60 s first ask. The release registers its line with savta.consent
    # (native/listener.py ensure_server), which _tell never asked. Now it does.
    from savta import consent
    try:
        reset("Google Chrome=not_asked")
        consent._reset_for_tests()
        order = []
        consent.register(lambda app: (order.append(("line", app)), True)[1])
        mac._osa = lambda s, timeout=4.0: (order.append(("osa", timeout)), (True, "0"))[1]
        try:
            ok, _ = mac._tell("Google Chrome", "x", timeout=6.0, launch=False)
        finally:
            mac._osa = fake_osa
        check("the line registered with savta.consent comes before macOS's question",
              ok and order == [("line", "Google Chrome"), ("osa", mac.FIRST_ASK_TIMEOUT)], order)
        reset("Google Chrome=not_asked")
        consent._reset_for_tests()
        consent.register(lambda app: None)          # no answer within CONSENT_WAIT
        ok, why = mac._tell("Google Chrome", "x", timeout=6.0, launch=False)
        check("her line unanswered: automation_declined at once, no script, no 60 s wait",
              (ok, why) == (False, "automation_declined:Google Chrome") and not OSA, (ok, why, OSA))
        reset("Google Chrome=not_asked")
        consent._reset_for_tests()
        consent.register(lambda app: False)          # Not now
        closed = mac.close_tab_with("abc123")
        check("a tab Chrome would not let MicMic close says why (LAST_CLOSE_FAILURE)",
              closed is False and mac.LAST_CLOSE_FAILURE[0] == "automation_declined:Google Chrome"
              and not OSA, (closed, mac.LAST_CLOSE_FAILURE, OSA))
        reset("Google Chrome=granted")
        consent._reset_for_tests()
        OSA_RULES.append(("close t", (True, "1")))
        closed = mac.close_tab_with("abc123")
        check("a closed tab clears it", closed is True and mac.LAST_CLOSE_FAILURE[0] == "",
              (closed, mac.LAST_CLOSE_FAILURE))
        reset("Messages=not_asked")
        consent._reset_for_tests()
        consent.register(lambda app: (order.append(("consent", app)), True)[1])
        order.clear()
        mac.set_automation_explainer(lambda app: (order.append(("own", app)), True)[1])
        mac._tell("Messages", "x")
        check("a host's own explainer still wins, shown once",
              [o for o in order if o[0] in ("own", "consent")] == [("own", "Messages")], order)
    finally:
        consent.register(None)
        consent._reset_for_tests()

    reset("Music=not_asked")
    called = []
    mac.set_automation_explainer(lambda app: called.append(app) or True)
    ok, why = mac._tell("Music", "x", ask=False)
    check("ask=False: never asked reads as automation_not_asked, no line, no script",
          (ok, why) == (False, "automation_not_asked:Music") and not called and not OSA,
          (ok, why, called, OSA))

    reset("*=granted")
    mac._tell("Notes", "a", timeout=4.0)
    probes = []
    real_probe = mac.automation_consent
    mac.automation_consent = lambda app: (probes.append(app), "granted")[1]
    try:
        mac._tell("Notes", "b", timeout=4.0)
    finally:
        mac.automation_consent = real_probe
    check("granted: the normal timeout, and not read again this run",
          OSA == [("a", 4.0), ("b", 4.0)] and probes == [], (OSA, probes))

    reset("Reminders=not_running")
    mac._tell("Reminders", "x")
    check("not running and the script would launch it: started hidden by bundle id first",
          LAUNCHED and LAUNCHED[0][:5] == ["open", "-g", "-j", "-b", "com.apple.reminders"],
          LAUNCHED)
    reset("Music=not_running")
    mac._tell("Music", "x", launch=False)
    check("not running and the script is guarded: nothing launched, normal timeout",
          not LAUNCHED and OSA == [("x", 4.0)], (LAUNCHED, OSA))

    reset("Reminders=denied")
    mac._tell("Reminders", "x")
    os.environ["MICMIC_FAKE_AUTOMATION"] = "Reminders=not_running"
    ok, why = mac._tell("Reminders", "x")
    check("denied a moment ago and not running now: denied again without relaunching it",
          (ok, why) == (False, "automation_denied:Reminders") and not LAUNCHED and not OSA,
          (ok, why, LAUNCHED))
    os.environ["MICMIC_FAKE_AUTOMATION"] = "Reminders=granted"
    check("a running app is read afresh: granted after she turned it on",
          mac._tell("Reminders", "x")[0] and "Reminders" not in mac._DENIED_AT)

    reset("*=granted")
    OSA_RULES.append(("x", (False, "execution error: Not authorized to send Apple events "
                                   "to Notes. (-1743)")))
    ok, why = mac._tell("Notes", "x")
    check("a -1743 from osascript itself is named automation_denied and uncached",
          (ok, why) == (False, "automation_denied:Notes") and "Notes" not in mac._GRANTED,
          (ok, why))

    reset("*=granted")
    mac._GRANTED.add("Google Chrome")
    OSA_RULES.append(("x", (False, "execution error: Not authorized to send Apple events "
                                   "to System Events. (-1743)")))
    ok, why = mac._tell("Google Chrome", "x")
    check("a -1743 that names System Events is recorded for System Events, "
          "Chrome's grant kept",
          (ok, why) == (False, "automation_denied:System Events")
          and "Google Chrome" in mac._GRANTED and "Google Chrome" not in mac._DENIED_AT
          and "System Events" in mac._DENIED_AT, (ok, why, mac._GRANTED, mac._DENIED_AT))
    check("an app the error does not name falls back to the target",
          mac._denied_app("Not authorized (-1743)", "Notes") == "Notes"
          and mac._denied_app("Not authorized to send Apple events to Gizmo. (-1743)",
                              "Notes") == "Notes")

    reset("Calendar=not_asked")
    OSA_RULES.append(("x", (False, "TimeoutExpired(['osascript'], 60.0)")))
    ok, why = mac._tell("Calendar", "x")
    check("macOS's dialog still up after 60 s: automation_unanswered",
          (ok, why) == (False, "automation_unanswered:Calendar"), (ok, why))

    reset("Notes=denied")
    ok, why = REAL_OSA("x", target="Notes")
    check("_osa(script, target=) with a denied target returns at once, no osascript",
          (ok, why) == (False, "automation_denied:Notes") and not LAUNCHED, (ok, why, LAUNCHED))
    plain = REAL_OSA("return 1", 2.0)
    check("the old two-argument _osa(script, timeout) still runs osascript",
          plain == (True, "") and LAUNCHED and LAUNCHED[-1][:2] == ["osascript", "-e"],
          (plain, LAUNCHED))


# ================================================================== P1-2 Messages
def t_messages():
    print("\nt_messages (P1-2 helper, send_message consent)")
    reset("Messages=denied")
    with gates():
        ok, why = mac.send_message("+972501234567", "hi")
    check("send_message, Messages denied: honest automation_denied, nothing run",
          (ok, why) == (False, "automation_denied:Messages") and not OSA, (ok, why))

    reset()
    with gates():
        res = mac.message_ready("+972-50-123-4567", "I'm late")
    check("a number: clipboard holds her words, Messages opened on sms:<number>",
          res == (False, "ready_paste")
          and ["pbcopy", "I'm late"] in LAUNCHED and ["open", "sms:+972501234567"] in LAUNCHED,
          (res, LAUNCHED))
    reset()
    with gates():
        res = mac.message_ready("dana@example.com", "hi")
    check("an email address: imessage:<address>",
          res == (False, "ready_paste") and ["open", "imessage:dana@example.com"] in LAUNCHED,
          LAUNCHED)
    reset()
    with gates():
        res = mac.message_ready("Dana", "hi")
    check("a name only: Messages itself (its LAST conversation), so ready_pick, never "
          "the ready_paste of the sms: path",
          res == (False, "ready_pick") and ["open", "-a", "Messages"] in LAUNCHED
          and not any(str(c[-1]).startswith(("sms:", "imessage:")) for c in LAUNCHED),
          (res, LAUNCHED))
    for who in ("Dana", "Dana Cohen", "Mom", "דנה", "1234", ""):
        for send in (True, False):
            reset()
            with gates(send=send):
                got = mac.message_ready(who, "hi")
            if got[1] == "ready_paste":
                check(f"a name-only recipient never returns ready_paste ({who!r}, send={send})",
                      False, got)
                break
    else:
        check("a name-only recipient never returns ready_paste (6 names, sending on and off)",
              True)
    reset()
    RC["pbcopy"] = 1
    with gates():
        res = mac.message_ready("Dana", "hi")
    check("a name only and the clipboard refused: ready_pick_open, still not ready_paste",
          res == (False, "ready_pick_open"), res)
    reset()
    RC["pbcopy"] = 1
    with gates():
        res = mac.message_ready("+972501234567", "hi")
    check("a number and the clipboard refused: ready_open", res == (False, "ready_open"), res)
    reset()
    RC["open"] = 1
    with gates():
        res = mac.message_ready("Dana", "hi")
    check("Messages would not open: (False, missing)", res == (False, "missing"), res)
    reset()
    with gates():
        res = mac.message_ready("+972501234567", "", attachment="/tmp/shot.png")
    check("a screenshot goes on the clipboard as the picture (not its path)",
          res == (False, "ready_paste") and any("PNGf" in s for s, _ in OSA), (res, OSA))
    reset()
    with gates(send=False):
        res = mac.message_ready("+972501234567", "hi")
    check("sending switched off: nothing opened, still never claimed as sent",
          res == (False, "ready_paste") and not LAUNCHED, (res, LAUNCHED))
    reset()
    with gates(send=False):
        res = mac.message_ready("Dana", "hi")
    check("sending switched off, a name only: ready_pick, nothing opened",
          res == (False, "ready_pick") and not LAUNCHED, (res, LAUNCHED))


# ================================================================== P0-12/13 notes, reminders
def t_notes_reminders():
    print("\nt_notes_reminders (P0-12, P0-13)")
    state = Path(os.environ["MICMIC_STATE_DIR"])
    legacy = Path(tempfile.mkdtemp(prefix="micmic-legacy-")) / "notes.txt"
    legacy.write_text("2026-09-01 10:00  old note\n", encoding="utf-8")
    real_legacy = mac.NOTES_FALLBACK
    mac.NOTES_FALLBACK = str(legacy)
    try:
        reset("Notes=denied")
        ok, msg = mac.make_note("buy eggs")
        f = state / "notes.txt"
        check("Notes denied: kept, ok stays True, the message says kept_locally",
              ok is True and msg.startswith("kept_locally") and "automation_denied:Notes" in msg,
              (ok, msg))
        check("kept in MicMic's state folder, the old notes copied across once",
              f.exists() and "old note" in f.read_text() and "buy eggs" in f.read_text())
        check("nothing written next to the code (the old in-app place)",
              "buy eggs" not in legacy.read_text())
        legacy.write_text("2026-09-01 10:00  old note\n2026-09-02 10:00  later legacy\n")
        mac.make_note("milk")
        check("the legacy copy happens once, never again over her new notes",
              "later legacy" not in f.read_text() and "buy eggs" in f.read_text())
        check("'what did I write down' reads it back, newest first",
              [l.split("  ", 1)[1] for l in mac.read_local_notes(5)][:2] == ["milk", "buy eggs"],
              mac.read_local_notes(5))
        reset("*=granted")
        check("Notes reachable: saved in Notes", mac.make_note("x") == (True, "saved in Notes"))

        reset("Reminders=denied")
        ok, msg = mac.add_reminder("call the bank", "2026-10-08")
        check("Reminders denied: (False, kept_locally), never a success",
              (ok, msg) == (False, "kept_locally"), (ok, msg))
        check("and the words are kept with their day",
              "call the bank (2026-10-08)" in (state / "notes.txt").read_text())
        reset("*=granted")
        OSA_RULES.append(('tell application "Reminders"', (True, "x-apple-reminder://R1")))
        check("Reminders reachable: (True, the row's id)",
              mac.add_reminder("x", "2026-10-08") == (True, "x-apple-reminder://R1"))
        check("a date it cannot read: bad date, nothing kept",
              mac.add_reminder("x", "Thursday") == (False, "bad date"))

        old = os.environ["MICMIC_STATE_DIR"]
        os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp() + "/MicMic.app/Contents/Resources"
        try:
            ok, msg = mac._keep_locally("x")
        finally:
            os.environ["MICMIC_STATE_DIR"] = old
        check("never writes inside an .app bundle, whatever the state folder says",
              ok is False and "bundle" in msg, (ok, msg))
    finally:
        mac.NOTES_FALLBACK = real_legacy


# ================================================================== P0-15 FaceTime
def t_facetime():
    print("\nt_facetime (P0-15, calls ON)")
    reset()
    with gates():
        ok, msg = mac.facetime("Dana", "+972-50-123-4567")
    check("a number: facetime:// handed to FaceTime, which shows its own Call button; "
          "nothing rang yet, so False with facetime_confirm, never a placed call",
          ok is False and msg == "facetime_confirm +972501234567"
          and LAUNCHED[0][0] == "open" and LAUNCHED[0][1].startswith("facetime://")
          and "972501234567" in LAUNCHED[0][1] and not OSA, (ok, msg, LAUNCHED))
    reset()
    with gates():
        mac.facetime("Dana", "0501234567", video=False)
    check("an audio call: facetime-audio://", LAUNCHED[0][1].startswith("facetime-audio://"),
          LAUNCHED)
    reset()
    RC["open"] = 1
    with gates():
        ok, msg = mac.facetime("Dana", "0501234567")
    check("FaceTime refused the URL: (False, open_failed), never 'calling'",
          ok is False and msg.startswith("open_failed"), (ok, msg))
    reset()
    with gates():
        ok, msg = mac.facetime("Dana", "")
    check("no number: FaceTime opened with open -a (no Automation), and False",
          ok is False and ["open", "-a", "FaceTime"] in LAUNCHED and not OSA, (ok, msg, OSA))
    reset()
    with gates(call=False):
        ok, msg = mac.facetime("Dana", "0501234567")
    check("calls switched off on this machine: dry run, nothing opened",
          ok and "dry run" in msg and not LAUNCHED, (ok, msg))

    env = {k: v for k, v in os.environ.items() if k != "MICMIC_ALLOW_CALL"}
    code = "from savta.actions import mac; print(mac.CALL_FOR_REAL)"
    on = _REAL_RUN([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                   text=True).stdout.strip()
    off = _REAL_RUN([sys.executable, "-c", code], cwd=ROOT,
                    env={**env, "MICMIC_ALLOW_CALL": "0"}, capture_output=True,
                    text=True).stdout.strip()
    check("calls ship ON: CALL_FOR_REAL with MICMIC_ALLOW_CALL unset; =0 turns it off",
          (on, off) == ("True", "False"), (on, off))

    reset()
    quits = []
    real = (mac.running_apps, mac.quit_app)
    mac.running_apps = lambda: [{"name": "FaceTime", "pids": [4242]},
                                {"name": "Music", "pids": [7]}]
    mac.quit_app = lambda pids: (quits.append(list(pids)), (True, "asked 1 of 1 to quit"))[1]
    try:
        r1 = mac.end_call()
        mac.running_apps = lambda: [{"name": "Music", "pids": [7]}]
        r2 = mac.end_call()
    finally:
        mac.running_apps, mac.quit_app = real
    check("hang up: FaceTime asked to quit by pid (no Automation), only FaceTime",
          r1[0] and quits == [[4242]] and not OSA, (r1, quits, OSA))
    check("hang up with FaceTime not running: no call, nothing quit", r2 == (True, "no_call"), r2)


# ================================================================== P1-7 brightness
class FakeDS:
    def __init__(self, level=0.5, displays=(1,), refuse=False):
        self.level, self.disp, self.refuse, self.sets = level, list(displays), refuse, []

    def api(self):
        return {"displays": lambda: list(self.disp), "get": lambda d: self.level,
                "set": self._set}

    def _set(self, d, v):
        self.sets.append(round(v, 4))
        if self.refuse:
            return False
        self.level = v
        return True


def t_brightness():
    print("\nt_brightness (P1-7)")
    reset()
    check("the real DisplayServices is never loaded in a test", mac._ds() is None)
    real = mac._ds
    try:
        fake = FakeDS(0.5)
        mac._ds = fake.api
        ok, how = mac.brightness(+1)
        check("brighter through DisplayServices: one key's step, read back, no System Events",
              ok and how == "level:56" and fake.sets == [0.5625] and not OSA, (ok, how, OSA))
        ok, how = mac.brightness(-1)
        check("darker: back down", ok and how == "level:50", how)
        fake.level = 1.0
        check("already as bright as it goes: at_max, nothing set",
              mac.brightness(+1) == (True, "at_max"))

        reset("System Events=denied")
        mac._ds = FakeDS(displays=()).api
        ok, how = mac.brightness(+1)
        check("no built-in display, System Events denied: (False, automation_denied), no 'done'",
              (ok, how) == (False, "automation_denied:System Events") and not OSA, (ok, how))

        reset("*=granted")
        mac._ds = FakeDS(refuse=True).api
        ok, how = mac.brightness(-1)
        check("DisplayServices refused: the brightness keys through System Events",
              (ok, how) == (True, "keys") and "key code 145" in OSA[-1][0], (ok, how, OSA))

        reset("*=granted")
        mac._ds = lambda: None
        OSA_RULES.append(("key code", (False, "System Events got an error: not allowed "
                                              "to send keystrokes (1002)")))
        ok, how = mac.brightness(+1)
        check("both ways failed: False with the reasons, never 'Alright'",
              ok is False and "no_displayservices" in how and "keys_failed" in how, how)
    finally:
        mac._ds = real
    reset()
    check("the honest next step: Displays settings, no permission",
          mac.open_display_settings() == (True, "opened_display_settings")
          and "Displays-Settings" in LAUNCHED[0][1], LAUNCHED)


# ================================================================== P1-8 pause, never by mute
def t_pause():
    print("\nt_pause (P1-8: honest pause_failed, never a mute)")
    check("mac has no mute of the Mac's output any more",
          not any(hasattr(mac, n) for n in ("mute", "unmute", "muted_by_micmic", "_MUTED_BY_US")))
    reset("Music=denied")
    ok, msg = am.control("pause")
    check("Music denied: (False, pause_failed:automation_denied:Music), nothing run, "
          "nothing muted",
          (ok, msg) == (False, "pause_failed:automation_denied:Music") and not OSA, (ok, msg, OSA))
    ok, msg = am.control("resume")
    check("then resume: honest failure, no unmute", (ok, msg) == (False, "automation_denied:Music")
          and not OSA, (ok, msg, OSA))
    reset("Spotify=denied")
    ok, msg = am.spotify_control("stop")
    check("Spotify denied: stop is pause_failed too",
          (ok, msg) == (False, "pause_failed:automation_denied:Spotify") and not OSA, (ok, msg))
    reset("*=granted")
    OSA_RULES.append(('tell application "Music"', (False, "TimeoutExpired 4.0")))
    ok, msg = am.control("pause")
    check("Music timed out: pause_failed with the reason, no mute script",
          ok is False and msg.startswith("pause_failed:")
          and not any("muted" in sc for sc, _ in OSA), (ok, msg, OSA))
    reset("Music=not_running")
    am.control("pause")
    check("Music not running: the guarded pause, never a launch, never a mute",
          not LAUNCHED and not any("muted" in sc for sc, _ in OSA), (LAUNCHED, OSA))

    reset("*=granted")
    ok, msg = am.control("pause")
    check("Music reachable: paused", ok and 'pause' in OSA[-1][0], OSA)

    # What she hears after a failed pause: MicMic's own line ("Press the space bar")
    # and every later reply, timer and reminder go out through the same output, so it
    # must never have been muted, by any of the three paths.
    reset("Music=denied,Spotify=denied,Google Chrome=denied")
    am.control("pause"); am.spotify_control("pause"); mac.tab_player("abc123", "pause", "youtube")
    reset("*=granted")
    OSA_RULES.append(("tell application", (False, "TimeoutExpired 4.0")))
    OSA_RULES.append(("URL of t contains", (False, "TimeoutExpired 6.0")))
    am.control("pause"); am.spotify_control("stop"); mac.tab_player("abc123", "pause", "youtube")
    check("after failed pauses (denied, timed out) no mute was ever sent: the next line "
          "is heard", not any("muted" in sc or "set volume" in sc for sc, _ in OSA), OSA)

    reset("Music=not_asked")
    called = []
    mac.set_automation_explainer(lambda app: called.append(app) or True)
    st = am.state("apple_music", ask=False)
    check("state(ask=False), Music never asked: '' with no line and no prompt",
          st == "" and not called and not OSA, (st, called, OSA))
    reset("Music=granted")
    OSA_RULES.append(("player state as string", (True, "paused")))
    check("state() still reads a granted player", am.state("apple_music") == "paused")


# ================================================================== U7 library
def t_library():
    print("\nt_library (U7: the library search is never a reason to ask)")
    for consent in ("Music=not_asked", "Music=not_running", "Music=denied"):
        reset(consent)
        called = []
        mac.set_automation_explainer(lambda app: called.append(app) or True)
        hits = am.library("bohemian rhapsody")
        check(f"library(), {consent}: [] with no script, no launch, no line",
              hits == [] and not OSA and not LAUNCHED and not called,
              (hits, OSA, LAUNCHED, called))
    reset("Music=unknown")
    am.library("bohemian rhapsody")
    check("library(), Music's answer unreadable (as in every test process): searched, "
          "as _gate lets it through", bool(OSA) and not LAUNCHED, (OSA, LAUNCHED))
    reset("Music=granted")
    OSA_RULES.append(('tell application "Music"', (True, "")))
    am.library("bohemian rhapsody")
    check("library(), Music granted and running: searched, normal timeout",
          OSA and OSA[-1][1] == 6.0 and not LAUNCHED, (OSA, LAUNCHED))
    reset("Music=not_running")
    mac._GRANTED.add("Music")
    am.library("bohemian rhapsody")
    check("library(), Music seen granted earlier this run: searched", bool(OSA), OSA)
    reset("Music=not_asked")
    mac.set_automation_explainer(lambda app: True)
    am.library("bohemian rhapsody", ask=True)
    check("library(ask=True) keeps the old way: line, then the long timeout",
          OSA and OSA[-1][1] == mac.FIRST_ASK_TIMEOUT, OSA)
    reset("Music=not_running")
    check("automation_ready never launches or asks", mac.automation_ready("Music") is False
          and not LAUNCHED and not OSA)


# ================================================================== U8 keystrokes
class _NoSleep:
    def __enter__(self):
        self.real = mac.time.sleep
        mac.time.sleep = lambda s: None

    def __exit__(self, *exc):
        mac.time.sleep = self.real
        return False


def t_keystrokes():
    print("\nt_keystrokes (U8: never a blind key behind macOS's first question)")
    # WhatsApp with a number: Return only with System Events already allowed, and
    # only after confirming WhatsApp is in front, inside the same script.
    reset("System Events=not_asked")
    called = []
    mac.set_automation_explainer(lambda app: called.append(app) or True)
    with gates(), _NoSleep():
        res = mac.whatsapp("+972501234567", "hi")
    check("WhatsApp, System Events never asked: no keystroke, no line, (False, ready)",
          res == (False, "ready") and not OSA and not called
          and any("whatsapp://send?phone=" in str(c[-1]) for c in LAUNCHED), (res, OSA, called))
    reset("System Events=denied")
    with gates(), _NoSleep():
        res = mac.whatsapp("+972501234567", "hi")
    check("WhatsApp, System Events denied: (False, ready), nothing pressed",
          res == (False, "ready") and not OSA, (res, OSA))
    reset("*=granted")
    OSA_RULES.append(("keystroke return", (True, "pressed")))
    with gates(), _NoSleep():
        res = mac.whatsapp("+972501234567", "hi")
    sc = OSA[-1][0] if OSA else ""
    check("WhatsApp, System Events allowed: WhatsApp brought to front and confirmed "
          "before Return, (True, pressed)",
          res == (True, "pressed") and 'set frontmost of process "WhatsApp" to true' in sc
          and sc.index('is not "WhatsApp" then return "not_front"') < sc.index("keystroke return")
          and OSA[-1][1] == 4.0, (res, sc))
    reset("*=granted")
    OSA_RULES.append(("keystroke return", (True, "not_front")))
    with gates(), _NoSleep():
        res = mac.whatsapp("+972501234567", "hi")
    check("WhatsApp not in front when Return would go: (False, ready), never True",
          res == (False, "ready"), res)
    reset("*=granted")
    OSA_RULES.append(("keystroke return", (True, "")))
    with gates(), _NoSleep():
        res = mac.whatsapp("+972501234567", "hi")
    check("a script that ran but did not say pressed is never reported as sent",
          res == (False, "ready"), res)

    # close_front_window: Cmd-W only when System Events is already allowed.
    reset("System Events=not_asked")
    called = []
    mac.set_automation_explainer(lambda app: called.append(app) or True)
    res = mac.close_front_window()
    check("close this, System Events never asked: nothing pressed, no line, no 60 s wait",
          res == (False, "automation_not_asked:System Events") and not OSA and not called,
          (res, OSA, called))
    reset("System Events=denied")
    check("close this, System Events denied: automation_denied, nothing pressed",
          mac.close_front_window() == (False, "automation_denied:System Events") and not OSA)
    reset("*=granted")
    ok, _ = mac.close_front_window()
    check("close this, System Events allowed: Cmd-W, the normal 4 s timeout",
          ok and OSA == [('tell application "System Events" to keystroke "w" using command down',
                          4.0)], OSA)


# ================================================================== tab_player
def t_tab_player():
    print("\nt_tab_player (P1-8, U8/T15)")
    # Chrome allowed, System Events never asked: Chrome's script holds NO System
    # Events keystroke; the key is its own consent-checked script, after MicMic's line.
    reset("Google Chrome=granted,System Events=not_asked")
    order = []
    mac.set_automation_explainer(lambda app: (order.append(("line", app)), True)[1])
    OSA_RULES.append(("URL of t contains", (True, "keys")))
    OSA_RULES.append(('set frontmost of process "Google Chrome"', (True, "pressed")))
    res = mac.tab_player("abc123", "pause", "youtube")
    chrome = [sc for sc, _ in OSA if "URL of t contains" in sc]
    keys = [(sc, t) for sc, t in OSA if "set frontmost" in sc]
    check("Chrome's script sends no System Events keystroke itself",
          chrome and "System Events" not in chrome[0], chrome)
    check("System Events never asked: MicMic's line for System Events first, then the key "
          "with the long timeout", order == [("line", "System Events")] and keys
          and keys[0][1] == mac.FIRST_ASK_TIMEOUT, (order, keys))
    sc = keys[0][0] if keys else ""
    check("the key goes only after Chrome is confirmed in front with this very tab active",
          res == "keys" and 'return "not_front"' in sc
          and 'does not contain "abc123" then return "moved"' in sc
          and sc.index('return "moved"') < sc.index('keystroke "k"'), (res, sc))

    reset("Google Chrome=granted,System Events=not_asked")
    mac.set_automation_explainer(lambda app: False)
    OSA_RULES.append(("URL of t contains", (True, "keys")))
    res = mac.tab_player("abc123", "pause", "youtube")
    check("she said Not now to the System Events line: failed, no key, the reason kept",
          res == "failed" and not any("set frontmost" in sc for sc, _ in OSA)
          and mac.LAST_TAB_FAILURE[0] == "automation_declined:System Events",
          (res, OSA, mac.LAST_TAB_FAILURE))

    reset("Google Chrome=granted,System Events=not_running")
    OSA_RULES.append(("URL of t contains", (True, "keys")))
    OSA_RULES.append(("set frontmost", (True, "moved")))
    res = mac.tab_player("abc123", "pause", "youtube")
    check("another tab in front by the time the key would go: failed, nothing pressed",
          res == "failed" and mac.LAST_TAB_FAILURE[0] == "moved", (res, mac.LAST_TAB_FAILURE))

    reset("Google Chrome=granted,System Events=not_running")
    OSA_RULES.append(("URL of t contains", (True, "keys")))
    OSA_RULES.append(("set frontmost", (False, "execution error: Not authorized to send "
                                               "Apple events to System Events. (-1743)")))
    res = mac.tab_player("abc123", "pause", "youtube")
    check("a -1743 on the key script is System Events' denial, never Chrome's",
          res == "failed" and mac.LAST_TAB_FAILURE[0] == "automation_denied:System Events"
          and "System Events" in mac._DENIED_AT and "Google Chrome" not in mac._DENIED_AT,
          (res, mac.LAST_TAB_FAILURE, mac._DENIED_AT))

    reset("Google Chrome=denied")
    res = mac.tab_player("abc123", "pause", "youtube")
    check("Chrome denied: failed at once, no System Events prompt, nothing muted",
          res == "failed" and not OSA
          and mac.LAST_TAB_FAILURE[0] == "automation_denied:Google Chrome", (res, OSA))
    reset("*=granted")
    OSA_RULES.append(("URL of t contains", (True, "done")))
    check("Chrome reachable and the page's player took it: done, one script",
          mac.tab_player("abc123", "pause", "youtube") == "done" and len(OSA) == 1, OSA)
    reset("*=granted")
    OSA_RULES.append(("URL of t contains", (True, "missing")))
    check("no such tab: missing, no key script",
          mac.tab_player("abc123", "pause", "youtube") == "missing" and len(OSA) == 1)
    reset("Google Chrome=granted,System Events=not_running")
    OSA_RULES.append(("URL of t contains", (True, "keys")))
    OSA_RULES.append(("set frontmost", (True, "pressed")))
    mac.tab_player("abc123", "back_10", "other")
    check("another site's arrow key: key code 123 in the confirmed key script",
          "key code 123" in OSA[-1][0] and "set frontmost" in OSA[-1][0], OSA)

    reset("*=granted")
    OSA_RULES.append(("URL of t contains", (True, "keys")))
    res = mac.tab_player("abc123", "pause", "youtube")
    check("System Events already allowed: the key inside Chrome's own script, right after "
          "it brought the tab to the front, one script",
          res == "keys" and len(OSA) == 1 and 'System Events" to keystroke "k"' in OSA[0][0]
          and OSA[0][0].index("activate") < OSA[0][0].index('keystroke "k"'), (res, OSA))
    reset("*=granted")
    OSA_RULES.append(("URL of t contains", (False, "execution error: Not authorized to send "
                                                   "Apple events to System Events. (-1743)")))
    mac._GRANTED.update({"Google Chrome", "System Events"})
    res = mac.tab_player("abc123", "pause", "youtube")
    check("a -1743 from the inline key is System Events' denial: Chrome's grant kept",
          res == "failed" and mac.LAST_TAB_FAILURE[0] == "automation_denied:System Events"
          and "Google Chrome" in mac._GRANTED and "System Events" not in mac._GRANTED,
          (res, mac.LAST_TAB_FAILURE, mac._GRANTED))
    reset("Google Chrome=not_running")
    mac.close_tab_with("abc123")
    check("Undo of a video never launches Chrome (guarded script, no hidden launch)",
          not LAUNCHED and 'if application "Google Chrome" is not running' in OSA[-1][0],
          (LAUNCHED, OSA))


# ================================================================== P1-3 .ics
def t_ics():
    print("\nt_ics (P1-3)")
    reset()
    ok, how = mac.add_event_ics("Lunch, with Dana; at noon", "2026-10-08", "13:00")
    path = Path(LAUNCHED[0][-1]) if LAUNCHED else None
    check("written in MicMic's state folder and opened in Calendar: (False, ready_confirm)",
          (ok, how) == (False, "ready_confirm") and LAUNCHED[0][:3] == ["open", "-a", "Calendar"]
          and path and path.exists()
          and Path(os.environ["MICMIC_STATE_DIR"]) in path.parents, (ok, how, LAUNCHED))
    raw = path.read_bytes() if path else b""
    text = raw.decode("utf-8")          # read_text would turn CRLF into LF
    check("one event, local time, an hour by default, CRLF lines",
          "DTSTART:20261008T130000\r\n" in text and "DTEND:20261008T140000\r\n" in text
          and text.count("BEGIN:VEVENT") == 1 and b"\r\n" in raw and not re.search(rb"[^\r]\n", raw),
          text)
    check("her words escaped as iCalendar text",
          "SUMMARY:Lunch\\, with Dana\\; at noon" in text, text)
    t = mac.event_ics("Party", "2026-12-31", "22:00", "01:00")
    check("an end after midnight is the next day", "DTEND:20270101T010000" in t, t)
    t = mac.event_ics("Trip", "2026-11-28", all_day=True)
    check("all day: DATE values, ends the next day",
          "DTSTART;VALUE=DATE:20261128" in t and "DTEND;VALUE=DATE:20261129" in t, t)
    t = mac.event_ics("א" * 120, "2026-11-28", "10:00")
    check("long lines folded at 75 octets, never inside a character",
          all(len(l.encode()) <= 75 for l in t.split("\r\n")) and
          "".join(l[1:] if l.startswith(" ") else l
                  for l in t.split("\r\n")).count("א") == 120, t[:200])
    reset()
    check("a date it cannot read: bad date, nothing opened",
          mac.add_event_ics("x", "Thursday", "10:00") == (False, "bad date") and not LAUNCHED)
    check("an impossible date: bad date", mac.add_event_ics("x", "2026-02-30", "10:00")
          == (False, "bad date"))
    check("a time it cannot read: bad time", mac.add_event_ics("x", "2026-10-08", "noon")
          == (False, "bad time"))
    RC["open"] = 1
    check("Calendar would not open it: ics_failed",
          mac.add_event_ics("x", "2026-10-08", "10:00") == (False, "ics_failed open"))
    reset("Calendar=denied")
    check("add_event itself, Calendar denied: automation_denied at once",
          mac.add_event("x", "2026-10-08", "10:00") == (False, "automation_denied:Calendar")
          and not OSA)


# ================================================================== what she hears
def t_lines_and_rules():
    print("\nt_lines_and_rules")
    for key in ("pause_failed", "pause_failed_video"):
        block = am.LINES[key]
        check(f"{key}: four languages, no em dash",
              set(block) == {"english", "hebrew", "arabic", "russian"}
              and not any("—" in v for v in block.values()), block)
    check("no line promises a mute any more",
          not any(k in am.LINES for k in ("muted", "muted_video", "unmuted")))
    check("pause_failed names the app", am.line("pause_failed", "english", app="Spotify")
          == "I could not pause Spotify from here. Press the space bar in Spotify to pause it.")
    for f in ("savta/actions/mac.py", "savta/actions/music.py"):
        src = (ROOT / f).read_text()
        check(f"{f}: no delete path", not re.search(r"os\.remove\(|\.unlink\(|rmtree|os\.rmdir",
                                                     src))
    check("mac.py: no raw 'tell application \"FaceTime\"' (no Automation prompt for calls)",
          'tell application "FaceTime"' not in (ROOT / "savta/actions/mac.py").read_text())


def main():
    for t in (t_consent, t_messages, t_notes_reminders, t_facetime, t_brightness, t_pause,
              t_library, t_keystrokes, t_tab_player, t_ics, t_lines_and_rules):
        t()
    reset()
    real_osa = [c for c in LAUNCHED if c and c[0] == "osascript"]
    check("0 real osascript escapes", not real_osa, real_osa)
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAILED: {n}\n          {d}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
