#!/usr/bin/env python3
"""MicMic regression suite.

    python3 tests/test_micmic.py

No pytest, no dependencies. Plain asserts, one printed line per check, non-zero
exit on any failure.

NOTHING IN HERE MAY LEAVE THE MACHINE. Before a single line of savta is imported
this file refuses to run if MICMIC_ALLOW_SEND is set, then replaces every
outward-facing function in savta.actions.mac with a recorder, INCLUDING the
osascript bridge `_osa` itself. So even if a test accidentally reaches the real
send_message, there is a second wall behind it: OSA collects the script instead
of running it, and a test asserts that list stayed empty.

The one thing that is real is Jev. The suite costs ~33 calls, well under a cent.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------- wall one
# Refuse to run at all if real sending is armed. This check is deliberately the
# first executable statement in the file.
if os.environ.get("MICMIC_ALLOW_SEND", "").lower() in ("1", "true", "yes"):
    print("REFUSING TO RUN: MICMIC_ALLOW_SEND is set in this shell.\n"
          "This suite arms real message sends against real contacts and relies on\n"
          "the dry-run gate to stop them. Unset MICMIC_ALLOW_SEND and run again.")
    sys.exit(2)
os.environ.pop("MICMIC_ALLOW_SEND", None)

# ---------------------------------------------------------------- wall two
# Nothing this suite does may land in her real state. It used to: every run said
# "play me some music" and "תשימי לי מוזיקה" through the real router, which wrote
# them into memory.json as things she likes (n=17 each, from test runs alone), and
# wrote the suite's weather and calculator turns into trace.jsonl as if she had said
# them. Her taste profile was mostly this file's traffic.
#
# savta/paths.py resolves every piece of state through MICMIC_STATE_DIR when it is
# set, and COPIES the real file in on first access. So the suite starts from her real
# profile, memory and trace, and every write goes to the copy. It has to be set
# before savta is imported: the paths are resolved at import time.
import shutil as _shutil                      # noqa: E402
import tempfile as _tempfile                  # noqa: E402
_STATE = _tempfile.mkdtemp(prefix="micmic-test-state-")
os.environ["MICMIC_STATE_DIR"] = _STATE
atexit.register(lambda: _shutil.rmtree(_STATE, ignore_errors=True))

from savta import router                      # noqa: E402
from savta import brain                       # noqa: E402
from savta import profile as prof             # noqa: E402
from savta.jev import Jev                     # noqa: E402
from savta.actions import contacts as book    # noqa: E402
from savta.actions import facts               # noqa: E402
from savta.actions import mac                 # noqa: E402
from savta.actions import youtube as yt       # noqa: E402

# ---------------------------------------------------------------- wall two
# Keep honest references to the functions whose safety gate is under test,
# then stub everything with a side effect.
REAL_SAY = mac.say
REAL_STOP_SPEAKING = mac.stop_speaking
REAL_SEND_MESSAGE = mac.send_message
REAL_WHATSAPP = mac.whatsapp
REAL_FACETIME = mac.facetime

# Wall three. `open facetime://...` is not osascript, so wall two never covered it, and
# a test that exercised the dial path with the gate open really did launch FaceTime.
# Nothing in this suite is allowed to hand a URL to the operating system.
import subprocess as _sp_mod

LAUNCHED: list[list] = []
_REAL_RUN = _sp_mod.run


def _blocked_run(cmd, *a, **kw):
    if isinstance(cmd, (list, tuple)) and cmd and cmd[0] in ("open", "osascript"):
        LAUNCHED.append(list(cmd))
        class _R:
            returncode, stdout, stderr = 0, "[test] launch blocked", ""
        return _R()
    return _REAL_RUN(cmd, *a, **kw)


_sp_mod.run = _blocked_run
mac.subprocess.run = _blocked_run

OSA: list[str] = []          # any osascript that got through. Must stay empty.
SENT: list[tuple] = []
WA: list[tuple] = []
SAID: list[tuple] = []
OPENED: list[str] = []
APPS: list[str] = []
PATHS: list[str] = []
NOTES: list[str] = []
REMINDERS: list[str] = []
VOLUME: list[int] = []
BRIGHT: list[int] = []
CLOSED: list[int] = []
CALLED: list[str] = []
HUNGUP: list[int] = []
YT_QUERIES: list[str] = []
QUIT: list[list] = []
VOLSET: list[int] = []
TABS_CLOSED: list[str] = []

RECORDERS = (OSA, SENT, WA, SAID, OPENED, APPS, PATHS, NOTES, REMINDERS,
             VOLUME, BRIGHT, CLOSED, CALLED, HUNGUP, YT_QUERIES, QUIT, VOLSET, TABS_CLOSED)


OSA_TOTAL: list[str] = []    # never cleared, so the final line is cumulative
# A handful of checks deliberately call the REAL mac.py functions to prove they build
# the right script. Those are expected to reach the (blocked) stub, so they are counted
# separately — otherwise the headline number says "2 escaped" when nothing escaped, and
# a safety counter that cries wolf is a safety counter nobody reads.
DELIBERATE: list[str] = []


class expected_osa:
    def __enter__(self):
        self.mark = len(OSA_TOTAL)
        return self

    def __exit__(self, *exc):
        moved = OSA_TOTAL[self.mark:]
        del OSA_TOTAL[self.mark:]
        DELIBERATE.extend(moved)
        return False


def _blocked_osa(script: str, timeout: float = 15.0):
    OSA.append(script)
    OSA_TOTAL.append(script)
    return False, "[test] osascript blocked"


mac._osa = _blocked_osa
mac.send_message = lambda n, t: (SENT.append((n, t)), (True, "[test stub] imessage"))[1]
mac.whatsapp = lambda n, t: (WA.append((n, t)), (True, "[test stub] whatsapp"))[1]
mac.say = lambda t, l="english": SAID.append((l, t))
mac.open_url = lambda u: OPENED.append(u)
mac.open_app = lambda a: (APPS.append(a), (True, a))[1]
mac.open_path = lambda p: (PATHS.append(p), (True, p))[1]
mac.make_note = lambda t, title="": (NOTES.append(t), (True, "ok"))[1]
mac.add_reminder = lambda t: (REMINDERS.append(t), (True, "ok"))[1]
mac.facetime = lambda n, number="", video=True: (CALLED.append((n, number)), (True, "ok"))[1]
mac.end_call = lambda: (HUNGUP.append(1), (True, "quit"))[1]
# The suite must not depend on whether whoever runs it happened to lock their Mac.
# The normal case is an unlocked screen; the one section that cares flips this itself.
mac.screen_locked = lambda: False


def with_gates_on(fn):
    """Decorator form, so a section reads exactly as it did before."""
    def wrapped(j):
        with gates_on():
            return fn(j)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


class gates_on:
    """Turn the outward-facing gates on for one section.

    They are OFF by default, exactly as they are on a fresh machine, so the sections
    that prove a switched-off gate blocks keep proving it. A section that needs to
    exercise what happens BEHIND the gate opens it deliberately and closes it again.
    Everything it reaches is already a recording stub, so nothing leaves the machine.
    """

    def __enter__(self):
        self.prev = (mac.SEND_FOR_REAL, mac.CALL_FOR_REAL)
        mac.SEND_FOR_REAL = mac.CALL_FOR_REAL = True
        return self

    def __exit__(self, *exc):
        mac.SEND_FOR_REAL, mac.CALL_FOR_REAL = self.prev
        return False
mac.volume = lambda d: (VOLUME.append(d), (True, f"vol{d:+}"))[1]
mac.brightness = lambda d: (BRIGHT.append(d), (True, "bright"))[1]
mac.close_front_window = lambda: (CLOSED.append(1), (True, "closed"))[1]
# Quitting a program goes through AppKit, not subprocess, so the launch blocker above
# would never see it: a misrouted test utterance could really quit one of her apps.
# Walled off like every other side effect, against a fixed list of "running" apps.
FAKE_RUNNING = [{"name": "WhatsApp", "pids": [4101]},
                {"name": "Google Chrome", "pids": [4102, 4103]},
                {"name": "Messages", "pids": [4104]}]
mac.running_apps = lambda: [dict(r, pids=list(r["pids"])) for r in FAKE_RUNNING]
mac.quit_app = lambda pids: (QUIT.append(list(pids)), (True, "[test] quit blocked"))[1]
mac.is_running = lambda pids: False
# Undo's own reach into the Mac, walled like everything else.
mac.get_volume = lambda: 50
mac.set_volume = lambda level: (VOLSET.append(int(level)), True)[1]
mac.close_tab_with = lambda frag: (TABS_CLOSED.append(frag), True)[1]
mac.music = lambda a, q="": (True, "[test stub] music")
mac.contacts = lambda limit=60: []
mac.installed_apps = lambda limit=200: ["Calculator", "Calendar", "Photos",
                                        "Mail", "Music", "Notes", "Chess"]
mac.find_files = lambda *a, **k: []

# No network in a regression run except Jev itself.
FAKE_VIDEOS = [
    {"id": "v1", "title": "Umm Kulthum - Enta Omri (full concert)",
     "channel": "Classics", "length": "1:02:11", "views": "3.1M views"},
    {"id": "v2", "title": "Greatest hits compilation",
     "channel": "Music Box", "length": "58:02", "views": "900K views"},
]
yt.search = lambda q, n=18: (YT_QUERIES.append(q), [dict(v) for v in FAKE_VIDEOS])[1]
WEATHER_ASKED: list[str] = []
FAKE_WEATHER = {"where": "Haifa", "desc": "Light rain", "code": 296, "temp": 17,
                "feels": 16, "low": 14, "high": 19, "rain_pct": 70}
facts.conditions = lambda place="": (WEATHER_ASKED.append(place), dict(FAKE_WEATHER))[1]
facts.forecast = lambda *a, **k: ""
facts.weather = lambda *a, **k: ""
facts.wiki = lambda *a, **k: ""

# recent_chats copies the real (large) WhatsApp sqlite on every call and is hit
# two or three times per handle(). Memoise the real thing so the data stays real
# and the suite stays fast.
# The address book comes from WhatsApp, which macOS gates behind a privacy permission.
# A test run must not depend on whether that permission happens to be granted to
# whatever process is running it — so if the real book cannot be read, the suite uses
# a fixed one. Every name here is deliberately shaped like a real entry: Hebrew and
# Latin, one with no number at all, so the "who can actually be dialled" logic is
# exercised either way.
FIXED_BOOK = [
    {"name": "Zohar Levin", "first": "Zohar", "phone": "+972500000001",
     "waid": "972500000001@s.whatsapp.net", "source": "fixture"},
    {"name": "אמא", "first": "אמא", "phone": "+972501112233",
     "waid": "972501112233@s.whatsapp.net", "source": "fixture"},
    {"name": "רותי כהן", "first": "רותי", "phone": "+972502223344",
     "waid": "972502223344@s.whatsapp.net", "source": "fixture"},
    {"name": "Miriam Levi", "first": "Miriam", "phone": "+972503334455",
     "waid": "972503334455@s.whatsapp.net", "source": "fixture"},
    {"name": "Gal Ben Ami", "first": "Gal", "phone": "+972500000002",
     "waid": "972500000002@s.whatsapp.net", "source": "fixture"},
    {"name": "No Number Nora", "first": "Nora", "phone": "", "waid": "",
     "source": "fixture"},
]
for _r in FIXED_BOOK:
    _r["keys"] = book.keys_for(_r["name"]) | book.keys_for(_r["first"])

# Always, not only as a fallback. A suite whose results depend on whether a macOS
# privacy permission happens to be granted to the process running it is not a suite.
REAL_ALL_CONTACTS = book.all_contacts      # kept so the hang test can exercise it
REAL_RECENT_CHATS = book.recent_chats
book._CACHE.update(at=time.time() * 10, rows=list(FIXED_BOOK))
book.all_contacts = lambda force=False, wait=None: list(FIXED_BOOK)
book.recent_chats = lambda limit=30: [dict(r) for r in FIXED_BOOK[:limit]]
book.unread_summary = lambda limit=8: []
book.messages_from = lambda frag, limit=5: []

_REAL_RECENT = book.recent_chats
_RECENT_CACHE: dict = {}


def _cached_recent(limit: int = 30):
    if limit not in _RECENT_CACHE:
        _RECENT_CACHE[limit] = _REAL_RECENT(limit)
    return _RECENT_CACHE[limit]


book.recent_chats = _cached_recent

# The copy inside the test state dir, never the real one next to the code.
PROFILE = prof.PATH
PROFILE_BACKUP = PROFILE.read_bytes() if PROFILE.exists() else None


def _restore_profile():
    """Always put her profile back, however this process ends."""
    try:
        if PROFILE_BACKUP is None:
            PROFILE.unlink(missing_ok=True)
        elif not PROFILE.exists() or PROFILE.read_bytes() != PROFILE_BACKUP:
            PROFILE.write_bytes(PROFILE_BACKUP)
    except Exception as e:  # noqa: BLE001
        print(f"  !! could not restore profile.json: {e!r}")


def _ensure_setup_complete():
    """Every test that goes through handle() lands in the onboarding branch if
    profile.json is missing or half-written. Test 4 deletes it on purpose, and on
    this machine other processes write it too, so re-assert it before each test
    rather than trusting the previous one to have tidied up."""
    try:
        if prof.load().get("setup_complete"):
            return
    except Exception:  # noqa: BLE001
        pass
    if PROFILE_BACKUP is not None:
        PROFILE.write_bytes(PROFILE_BACKUP)
    else:
        # Fresh machine, no profile to put back. Stand up the minimum the router
        # needs; the atexit handler removes it again, leaving the machine fresh.
        prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done",
                   "name": "Test", "language": "hebrew", "speech_lang": "he-IL",
                   "city": "חיפה"})


atexit.register(_restore_profile)
atexit.register(lambda: router._cancel_pending())

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


def reset_state():
    router._cancel_pending()
    router.AWAITING = None
    router.MEM = router.Memory()
    for r in RECORDERS:
        r.clear()
    _ensure_setup_complete()


def short(v, n: int = 90) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


# ================================================================== tests

def t_gates_refuse_loudly(j: Jev):
    """With sending switched off it used to narrate a send, wait six seconds and do
    nothing — indistinguishable from a message that was sent and never arrived."""
    from savta.actions import mac as _mac
    real_send, real_call = _mac.SEND_FOR_REAL, _mac.CALL_FOR_REAL
    _mac.SEND_FOR_REAL = _mac.CALL_FOR_REAL = False
    router.PENDING = None; router.AWAITING = None; router.LAST_EMERGENCY = None
    try:
        r = router.handle(j, "תשלחי וואטסאפ לזוהר שאני בסדר", speak=False)
        check("a send with the gate off is refused, not narrated",
              r["did"] == "send_disabled", f"did={r['did']}")
        check("and it never claims to be sending",
              "שולחת" not in (r.get("say") or ""), short(r.get("say")))
        check("and nothing was queued", router.PENDING is None)
        check("and it says how to switch it on",
              "MICMIC_ALLOW_SEND" in str(r.get("detail")), str(r.get("detail")))

        router.PENDING = None; router.AWAITING = None; router.LAST_EMERGENCY = None
        r = router.handle(j, "I fell and I cannot get up", speak=False)
        check("an emergency with calling off says so", r["did"] == "call_disabled",
              f"did={r['did']}")
        check("and never claims a call is happening",
              "switched off" in (r.get("say") or "").lower()
              and "i am calling" not in (r.get("say") or "").lower(),
              short(r.get("say")))
        check("and still names who it would have called",
              bool((r.get("detail") or {}).get("would_call")), str(r.get("detail")))
    finally:
        _mac.SEND_FOR_REAL, _mac.CALL_FOR_REAL = real_send, real_call
        router.LAST_EMERGENCY = None


def t_safety(j: Jev):
    """1. With MICMIC_ALLOW_SEND unset nothing may actually leave the machine."""
    check("MICMIC_ALLOW_SEND is unset in this process",
          not os.environ.get("MICMIC_ALLOW_SEND"),
          repr(os.environ.get("MICMIC_ALLOW_SEND")))
    check("mac.SEND_FOR_REAL is False", mac.SEND_FOR_REAL is False,
          f"got {mac.SEND_FOR_REAL!r}")

    OSA.clear()
    ok, msg = REAL_SEND_MESSAGE("Nobody Real", "this must never be delivered")
    check("send_message takes the dry-run path",
          ok and isinstance(msg, str) and msg.startswith("[dry run]"), repr(msg))
    check("send_message never reached osascript", not OSA,
          f"{len(OSA)} osascript call(s): {short(OSA[:1])}")

    OSA.clear()
    ok, msg = REAL_WHATSAPP("+972500000000", "this must never be delivered")
    check("whatsapp takes the dry-run path",
          ok and isinstance(msg, str) and msg.startswith("[dry run]"), repr(msg))
    check("whatsapp never reached osascript", not OSA,
          f"{len(OSA)} osascript call(s): {short(OSA[:1])}")

    # The path that matters: a send armed by the router, fired by its own timer,
    # with the REAL send function underneath. The dry-run gate is the only thing
    # standing between this test and a delivered message.
    mac.send_message, mac.whatsapp = REAL_SEND_MESSAGE, REAL_WHATSAPP
    try:
        OSA.clear()
        router.CANCEL_WINDOW = 0.3
        n0 = len(router._HISTORY)
        router._arm_send("Nobody Real", "must never be delivered", "english")
        time.sleep(1.0)
        fired = router._HISTORY[n0:]
        check("router's own timer fired the send",
              len(fired) == 1 and fired[0]["did"] == "sent", short(fired))
        result = str(fired[0].get("result", "")) if fired else ""
        check("the fired send was a dry run, not a delivery",
              result.startswith("[dry run]"), repr(result))
        check("no osascript ran on the live arm-and-fire path", not OSA,
              f"{len(OSA)} osascript call(s): {short(OSA[:1])}")
    finally:
        mac.send_message = lambda n, t: (SENT.append((n, t)), (True, "[test stub] imessage"))[1]
        mac.whatsapp = lambda n, t: (WA.append((n, t)), (True, "[test stub] whatsapp"))[1]
        router.CANCEL_WINDOW = 6.0


CANCEL_WORDS = ["ביטול", "לא", "רגע", "אל תשלחי", "no", "wait", "stop"]


@with_gates_on
def t_cancel_window(j: Jev):
    """2. Every way she can say no, plus the case where she says nothing."""
    # Long window: the timer must not fire underneath the test.
    router.CANCEL_WINDOW = 30.0
    for word in CANCEL_WORDS:
        reset_state()
        router._arm_send("Zohar Levin", "אני מרגישה טוב", "hebrew")
        r = router.handle(j, word, speak=False)
        conf = ""
        if isinstance(r.get("detail"), dict):
            conf = f" stop_confidence={r['detail'].get('stop_confidence')}"
        check(f"{word!r} cancels the pending send",
              r["did"] == "cancelled" and router.PENDING is None and not SENT and not WA,
              f"did={r['did']!r}{conf} sent={SENT} wa={WA}")
        router._cancel_pending()

    # Saying nothing lets it through, and not one moment before the window is up.
    reset_state()
    router.CANCEL_WINDOW = 1.2
    router._arm_send("Zohar Levin", "silence sends this", "hebrew")
    time.sleep(0.4)
    check("send is still pending part-way through the window",
          not SENT and router.PENDING is not None,
          f"sent={SENT} pending={router.PENDING is not None}")
    time.sleep(1.4)
    check("saying nothing lets the send fire after the window",
          SENT == [("Zohar Levin", "silence sends this")], f"sent={SENT}")
    check("a fired send says so out loud", any("שלחתי" in t for _, t in SAID),
          f"said={short(SAID)}")

    # The mirror of the rule above, and the more dangerous direction: if ordinary
    # talk read as a cancel, her messages would silently never go.
    router.CANCEL_WINDOW = 30.0
    for word in ["תודה רבה", "make it louder"]:
        reset_state()
        router._arm_send("Zohar Levin", "אני מרגישה טוב", "hebrew")
        r = router.handle(j, word, speak=False)
        check(f"{word!r} does NOT cancel the pending send",
              r["did"] != "cancelled" and router.PENDING is not None,
              f"did={r['did']!r} pending={router.PENDING is not None}")
        router._cancel_pending()

    # Two regression guards for two different bugs this suite found.
    #
    # The first: a second message used to inherit the first one's already-running
    # timer, so it fired after a fraction of its own window.
    #
    # The second, and worse: arming a second message silently DESTROYED the first —
    # after it had been read back to her and she had not objected. "Message David I am
    # fine and tell Ruti the same" told her both were going and delivered only Ruti's.
    # A message that was narrated and not cancelled is a promise; the displaced one is
    # sent at the moment it is displaced.
    reset_state()
    window = 1.0
    router.CANCEL_WINDOW = window
    router._arm_send("PersonA", "first message", "english")
    time.sleep(0.6)
    rearmed = time.time()
    router._arm_send("PersonB", "second message", "english")
    check("the displaced message is delivered, not thrown away",
          any(n == "PersonA" for n, _ in SENT), f"sent={SENT}")
    deadline = rearmed + window
    b_at = None
    while time.time() < deadline + 1.0:
        if any(n == "PersonB" for n, _ in SENT):
            b_at = time.time()
            break
        time.sleep(0.02)
    time.sleep(0.3)
    got = (b_at - rearmed) if b_at else None
    check("re-arming gives the NEW message its own full cancel window",
          b_at is not None and b_at >= deadline - 0.05,
          f"second message fired after {got if got is None else round(got, 2)}s "
          f"of its {window}s window; sent={SENT}")
    check("and both messages reached their own recipient",
          sorted(n for n, _ in SENT) == ["PersonA", "PersonB"], f"sent={SENT}")
    # A correction is still a correction: objecting stops it, it is not "displaced".
    reset_state()
    router._arm_send("PersonC", "third message", "english")
    router._cancel_pending()
    time.sleep(window + 0.4)
    check("a cancelled message is never delivered by a later one", not SENT, f"sent={SENT}")
    router._cancel_pending()
    router.CANCEL_WINDOW = 6.0


@with_gates_on
def t_multi_turn(j: Jev):
    """3. Slot filling across three turns, and abandoning a half-asked question."""
    reset_state()
    router.CANCEL_WINDOW = 30.0

    r1 = router.handle(j, "send a message", speak=False)
    check("turn 1 'send a message' asks who",
          r1["did"] == "need_who" and router.AWAITING is not None,
          f"did={r1['did']!r} awaiting={short(router.AWAITING)}")

    r2 = router.handle(j, "Zohar", speak=False)
    check("turn 2 'Zohar' is taken as the answer and asks what",
          r2["did"] == "need_what", f"did={r2['did']!r} detail={short(r2.get('detail'))}")

    r3 = router.handle(j, "I am fine", speak=False)
    d = r3.get("detail") if isinstance(r3.get("detail"), dict) else {}
    to = str(d.get("to") or "")
    check("turn 3 'I am fine' arms the send",
          r3["did"] == "sending" and router.PENDING is not None,
          f"did={r3['did']!r} detail={short(d)}")
    check("the send is addressed to Zohar",
          book.skeleton(to).startswith("zr") or "zohar" in book.latinize(to),
          f"to={to!r} skeleton={book.skeleton(to)!r}")
    check("the message body is what she said last",
          d.get("text", "").strip().lower() == "i am fine", f"text={d.get('text')!r}")
    check("nothing was sent before the countdown ran out", not SENT and not WA,
          f"sent={SENT} wa={WA}")
    router._cancel_pending()

    # Changing the subject mid-question abandons it rather than stuffing the new
    # words into the half-built message.
    reset_state()
    r1 = router.handle(j, "send a message", speak=False)
    check("a fresh question is pending again",
          r1["did"] == "need_who" and router.AWAITING is not None, f"did={r1['did']!r}")
    r2 = router.handle(j, "turn the volume up", speak=False)
    check("changing the subject drops the half-asked question",
          router.AWAITING is None, f"awaiting={short(router.AWAITING)}")
    check("and no message was armed or sent from the new subject",
          router.PENDING is None and not SENT and not WA,
          f"pending={short(router.PENDING)} sent={SENT}")
    check("and the new request was acted on instead",
          r2["did"] == "louder" and VOLUME == [18],
          f"did={r2['did']!r} volume={VOLUME} detail={short(r2.get('detail'))}")
    router.CANCEL_WINDOW = 6.0


def t_onboarding(j: Jev):
    """4. The first conversation, from an empty machine."""
    reset_state()
    try:
        PROFILE.unlink(missing_ok=True)
        check("profile.json is gone before onboarding starts", not PROFILE.exists())

        # A silent first press: nothing said yet, so it cannot know the language.
        r0 = router.handle(j, "", speak=False)
        check("a silent first press greets in both languages",
              r0["did"] == "onboarding_greet" and "מיקמיק" in (r0.get("say") or "")
              and "MicMic" in (r0.get("say") or ""),
              f"did={r0['did']!r} say={short(r0.get('say'))}")
        # Speaking Hebrew is enough to pick the language, and the request still happens.
        PROFILE.unlink(missing_ok=True)
        r0 = router.handle(j, "שלום", speak=False)
        check("speaking first greets only in her language",
              "מיקמיק" in (r0.get("say") or "") and "MicMic" not in (r0.get("say") or ""),
              f"did={r0['did']!r} say={short(r0.get('say'))}")

        r1 = router.handle(j, "קוראים לי מרים", speak=False)
        p1 = prof.load()
        check("step 2 hears the name",
              r1["did"] == "onboarding_name" and "מרים" in (p1.get("name") or ""),
              f"did={r1['did']!r} name={p1.get('name')!r} detail={short(r1.get('detail'))}")
        check("step 2 infers the language from how she answered",
              p1.get("language") == "hebrew", f"language={p1.get('language')!r}")
        check("step 2 sets the matching speech locale",
              p1.get("speech_lang") == "he-IL", f"speech_lang={p1.get('speech_lang')!r}")

        r2 = router.handle(j, "אני גרה בחיפה", speak=False)
        p2 = prof.load()
        check("step 3 hears the city and asks who to call",
              r2["did"] == "onboarding_city" and p2.get("setup_complete") is not True,
              f"did={r2['did']!r} setup_complete={p2.get('setup_complete')!r}")
        check("the Hebrew preposition is stripped: 'בחיפה' -> 'חיפה'",
              p2.get("city") == "חיפה",
              f"city={p2.get('city')!r} (raw span was {short(r2.get('detail'))})")

        # The safety-critical question. Falling back to her most-messaged contact put
        # a work colleague on the emergency call.
        r3 = router.handle(j, "תתקשרי לזוהר", speak=False)
        p3 = prof.load()
        check("step 4 finishes setup",
              r3["did"] == "onboarding_done" and p3.get("setup_complete") is True,
              f"did={r3['did']!r} setup_complete={p3.get('setup_complete')!r}")
        check("step 4 stores an emergency contact from her real address book",
              (p3.get("emergency_contact") or "").startswith("Zohar"),
              f"emergency_contact={p3.get('emergency_contact')!r}")
        check("and says back who it will call",
              "Zohar" in (r3.get("say") or ""), short(r3.get("say")))
        check("a Latin name gets the maqaf after the Hebrew preposition",
              "לZohar" not in (r3.get("say") or ""), short(r3.get("say")))
        check("onboarding is over: a later utterance is no longer onboarding",
              prof.load().get("setup_complete") is True)
    finally:
        _restore_profile()
        # A fresh checkout has no profile to put back, so "restored" means absent.
        check("her real profile.json was restored",
              (not PROFILE.exists()) if PROFILE_BACKUP is None
              else PROFILE.exists() and PROFILE.read_bytes() == PROFILE_BACKUP)


COLLIDE = [("זוהר", "Zohar"), ("מרים", "Miriam"), ("רחל", "Rachel"),
           ("דוד", "David"), ("משה", "Moshe")]


def t_contact_matching(j: Jev):
    """5. Hebrew and Latin spellings of the same name land on one skeleton."""
    for heb, lat in COLLIDE:
        a, b = book.skeleton(heb), book.skeleton(lat)
        check(f"{heb} / {lat} collide", a == b and a != "",
              f"skeleton({heb!r})={a!r}  skeleton({lat!r})={b!r}")
    # Negative control: the skeleton must still tell different people apart.
    check("different names do not collide",
          len({book.skeleton(l) for _, l in COLLIDE}) == len(COLLIDE),
          str({l: book.skeleton(l) for _, l in COLLIDE}))
    check("the skeleton survives a full name",
          book.skeleton("Zohar Levin").startswith(book.skeleton("Zohar")),
          f"{book.skeleton('Zohar Levin')!r} vs {book.skeleton('Zohar')!r}")


def t_escape_hatch(j: Jev):
    """6. Asked for something absent, it must refuse rather than pick the nearest row."""
    rows = [{"name": n} for n in ("Calculator", "Calendar", "Photos", "Mail", "Chess")]
    hit, conf, good = brain.pick_from(
        j, rows, lambda r: r["name"],
        "Which program on this computer is she asking to open? Match by what the "
        "program is for, not only by name.",
        {"she_said": "open the washing machine"})
    check("pick_from refuses when the thing she asked for is not in the list",
          hit is None, f"picked={short(hit)} conf={conf:.2f} any_good={good:.2f}")

    span, sconf = brain.pick_span(
        j, "make the volume a bit louder please",
        "Which words name the town or city she lives in? Just the place name itself.")
    check("pick_span returns nothing when no span answers the question",
          span is None, f"span={span!r} conf={sconf:.2f}")

    junk = [{"id": f"c{i}", "title": t, "channel": "Home Cooking",
             "length": "4:12", "views": "1.2M views"}
            for i, t in enumerate(["How to poach an egg", "Five minute banana bread",
                                   "Sourdough for beginners", "Knife sharpening basics",
                                   "The perfect omelette"])]
    pick, pconf, pgood = brain.pick_result(
        j, "a Leonardo DiCaprio feature film", junk, True, True)
    check("pick_result refuses a page of results that answer a different question",
          pick is None, f"picked={short(pick)} conf={pconf:.2f} any_good={pgood:.2f}")

    # The version of this that would actually hurt her: a name that is not in her
    # address book at all must not become the nearest real person.
    for utt in ["send a message to Napoleon Bonaparte that hello",
                "תשלחי הודעה לברטולומיאו שאני בסדר"]:
        reset_state()
        router.CANCEL_WINDOW = 30.0
        r = router.handle(j, utt, speak=False)
        d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
        check(f"a name absent from her book does not become a real recipient: {utt[:34]!r}",
              r["did"] == "need_who" and router.PENDING is None and not SENT and not WA,
              f"did={r['did']!r} to={d.get('to')!r} sent={SENT} wa={WA}")
        router._cancel_pending()
        router.CANCEL_WINDOW = 6.0

    # Background speech is not a request. Acting on it is worse than ignoring it.
    # This only applies when the microphone is open without being asked: in push mode
    # she pressed a button first, so every word is addressed to MicMic and gets an
    # answer rather than silence.
    _real_cfg = router.load_config
    router.load_config = lambda: {**_real_cfg(), "activation": "wake"}
    for utt in ["and then I told her that the", "no no I was talking to the cat"]:
        reset_state()
        r = router.handle(j, utt, speak=False)
        check(f"stray speech is not acted on: {utt[:34]!r}",
              r["did"] in ("ignored", "waiting") and not OPENED and not SENT
              and not APPS and not VOLUME,
              f"did={r['did']!r} opened={OPENED} apps={APPS} sent={SENT}")
    router.load_config = _real_cfg
    # ...and in push mode the same words get a reply instead of being dropped.
    reset_state()
    r = router.handle(j, "no no I was talking to the cat", speak=False)
    check("but in push mode nothing she says is silently dropped",
          r["did"] != "ignored" and bool(r.get("say")), f"did={r['did']!r}")


SAVTA_PKG = ROOT / "savta"
DESTRUCTIVE = ["os.remove", "os.unlink", ".unlink(", "rmtree", "rm -rf",
               "DELETE FROM", "DROP TABLE", "shutil.move"]


def t_no_delete_path(j: Jev):
    """Rule 2: there is no delete path in the codebase. Keep it that way."""
    hits = []
    for py in sorted(SAVTA_PKG.rglob("*.py")):
        for n, line in enumerate(py.read_text(errors="replace").splitlines(), 1):
            for bad in DESTRUCTIVE:
                if bad in line:
                    hits.append(f"{py.relative_to(ROOT)}:{n}: {line.strip()[:70]}")
    check("no destructive call anywhere in savta/", not hits,
          "; ".join(hits[:4]))
    ro = (SAVTA_PKG / "actions" / "contacts.py").read_text(errors="replace")
    check("every sqlite connection to her WhatsApp data is read-only",
          ro.count("sqlite3.connect") == ro.count("mode=ro"),
          f"{ro.count('sqlite3.connect')} connects, {ro.count('mode=ro')} read-only")


LANGS = [("תשימי לי מוזיקה של אום כולתום", "hebrew"),
         ("please play me some music", "english"),
         ("شغلي لي أغنية", "arabic"),
         ("включи мне музыку", "russian")]


def t_language_routing(j: Jev):
    """7. She is understood in whichever of the four she happens to use."""
    small = ["Zohar Levin", "מרים", "Rachel"]
    for utt, want in LANGS:
        u = brain.understand(j, utt, small)
        check(f"{want}: {utt!r} is detected as {want}",
              u.get("language") == want, f"got {u.get('language')!r}")


@with_gates_on
def t_message_end_to_end(j: Jev):
    """8. The whole thing in one Hebrew breath: who, what, read back, armed."""
    reset_state()
    router.CANCEL_WINDOW = 30.0
    try:
        r = router.handle(j, "תשלחי הודעה לזוהר שאני מרגישה הרבה יותר טוב היום",
                          speak=False)
        d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
        to = str(d.get("to") or "")
        check("a one-breath Hebrew request arms a send",
              r["did"] == "sending" and router.PENDING is not None,
              f"did={r['did']!r} detail={short(d)}")
        check("it is addressed to Zohar",
              book.skeleton(to).startswith("zr") or "zohar" in book.latinize(to),
              f"to={to!r}")
        check("the body is her words, not the instruction to send them",
              "טוב" in str(d.get("text") or "") and "תשלחי" not in str(d.get("text") or ""),
              f"text={d.get('text')!r}")
        say = r.get("say") or ""
        check("it reads the whole message back before sending it",
              str(d.get("text") or "") in say and str(d.get("display") or "") in say,
              f"say={short(say)} text={d.get('text')!r} display={d.get('display')!r}")
        # Wording is allowed to change; what may not change is that the narration
        # tells her a word that this suite has proved actually cancels.
        toks = set(re.split(r"[\s.,:;!?]+", say))
        check("it offers her a stop word that really works",
              any(w in toks for w in CANCEL_WORDS)
              or any(" " in w and w in say for w in CANCEL_WORDS),
              f"say={short(say)} none of {CANCEL_WORDS} appears in it")
        check("nothing has been sent yet", not SENT and not WA, f"sent={SENT} wa={WA}")
    finally:
        router._cancel_pending()
        router.CANCEL_WINDOW = 6.0


@with_gates_on
def t_calling(j: Jev):
    """10. A call must be dialled, not merely announced.

    Regression guard for a bug this suite found, since fixed in mac.py: facetime
    used to activate the FaceTime app without the person's number and still
    return success, so the router told her "calling Miriam" while nothing was
    dialled and she was left looking at an empty window she cannot operate."""
    # With the gate shut it must refuse; with it open the number must reach the dialler.
    # Both directions matter: the old bug was returning success having dialled nothing.
    prev = (mac.SEND_FOR_REAL, mac.CALL_FOR_REAL)
    mac.SEND_FOR_REAL = mac.CALL_FOR_REAL = False
    try:
        ok, msg = REAL_FACETIME("Miriam", "+972-50-123-4567")
        check("with calling switched off it says so and dials nothing",
              ok and "dry run" in str(msg) and "MICMIC_ALLOW_CALL" in str(msg), repr(msg))
    finally:
        mac.SEND_FOR_REAL, mac.CALL_FOR_REAL = prev
    LAUNCHED.clear()
    ok, msg = REAL_FACETIME("Miriam", "+972-50-123-4567")
    check("a call carries the person's number into the dial path",
          ok and "972501234567" in str(msg).replace("-", ""), repr(msg))
    check("and it goes out as a facetime URL, not an app activation",
          any(str(c[-1]).startswith("facetime") and "972501234567" in str(c[-1])
              for c in LAUNCHED), str(LAUNCHED))
    check("and the dialler was never actually handed that URL by this suite",
          all(c[0] == "open" for c in LAUNCHED) and _sp_mod.run is _blocked_run)
    with expected_osa():
        check("a call with no number refuses instead of opening an empty window",
              REAL_FACETIME("Miriam", "")[0] is False, repr(REAL_FACETIME("Miriam", "")))

    # With no number there is nothing to dial. That branch legitimately opens the
    # FaceTime app, so blind the osascript recorder for exactly this one call
    # rather than letting it trip the "nothing escaped" gate at the end.
    with expected_osa():
        ok2, msg2 = REAL_FACETIME("Nobody With No Number")
    check("a call it cannot dial reports failure instead of claiming success",
          ok2 is False, f"ok={ok2!r} msg={msg2!r}")

    # And the router must only say "calling" when the dial actually succeeded.
    src = (SAVTA_PKG / "router.py").read_text(errors="replace")
    check("the router only announces a call when facetime succeeded",
          '"calling" if ok else "call_failed"' in src
          or re.search(r'finish\(\s*"calling"\s+if\s+ok', src) is not None,
          "router no longer branches its spoken line on the dial result")



def t_web_safety(j):
    from savta.actions import web
    # The agent is meant to walk all the way to a payment page, so this is the line
    # that has to hold. A real checkout is rarely in English.
    SECRET = [("Card number", "cardnumber"), ("Kartennummer", "kartennummer"),
              ("Numéro de carte", "card"), ("Número de tarjeta", "card"),
              ("رقم البطاقة", "card"), ("Номер карты", "card"),
              ("מספר כרטיס אשראי", "cc"), ("CVV", "cvv"), ("CVC", "cvc"),
              ("Security code", "cvc"), ("Expiry date", "cc-exp"), ("MM / YY", "exp"),
              ("Name on card", "ccname"), ("Password", "password"), ("Пароль", "pass"),
              ("كلمة السر", "pass"), ("Contraseña", "pw"), ("סיסמה", "pw"),
              ("One-time code", "otp"), ("IBAN", "iban"), ("CPF", "cpf"),
              ("Passport number", "passport"), ("דרכון", "passport")]
    missed = [l for l, n in SECRET
              if not web.is_forbidden({"type": "text", "name": n, "label": l})]
    check("every card, secret and identity field is refused, in every language",
          not missed, f"not refused: {missed}")
    ORDINARY = [("Number of guests", "guests"), ("Your name", "name"),
                ("Email", "email"), ("City", "city"), ("Phone", "tel"),
                ("Search", "q"), ("Delivery address", "address")]
    over = [l for l, n in ORDINARY
            if web.is_forbidden({"type": "text", "name": n, "label": l})]
    check("and ordinary fields are still filled", not over, f"wrongly refused: {over}")

    FINAL = ["Pay now", "Place order", "Buy now", "Complete purchase", "Confirm and pay",
             "Pay $49.99", "Submit payment", "Order now", "Book and pay",
             "Confirm booking", "Zahlungspflichtig bestellen", "Payer maintenant",
             "Finalizar compra", "ادفع الآن", "Оплатить", "לשלם עכשיו", "בצע הזמנה"]
    missed_b = [b for b in FINAL if not web.is_final({"label": b})]
    check("every button that spends money is refused, in every language",
          not missed_b, f"not stopped: {missed_b}")
    HARMLESS = ["Search", "Find a table", "Next", "חפש", "Continue", "See results",
                "Select", "Apply", "Sign in", "Add to basket"]
    over_b = [b for b in HARMLESS if web.is_final({"label": b})]
    check("and it still presses the ordinary ones", not over_b,
          f"wrongly stopped: {over_b}")
    for f in ({"type": "password", "name": "pw", "label": "Password"},
              {"type": "text", "name": "cardnumber", "label": "Card number"},
              {"type": "text", "name": "cvc", "label": "CVC"},
              {"type": "text", "name": "iban", "label": "IBAN"},
              {"type": "text", "name": "ssn", "label": "Social security number"},
              {"type": "text", "name": "x", "label": "מספר כרטיס"}):
        check(f"refuses to type into {f['label']!r}", web.is_forbidden(f))
    for f in ({"type": "text", "name": "guests", "label": "Number of guests"},
              {"type": "text", "name": "name", "label": "Your name"}):
        check(f"will fill {f['label']!r}", not web.is_forbidden(f))
    for b in ("Place order", "Pay now", "Buy now", "לשלם", "בצע הזמנה"):
        check(f"stops in front of {b!r}", web.is_final({"label": b}))
    for b in ("Search", "Find a table", "Next", "חפש"):
        check(f"will press {b!r}", not web.is_final({"label": b}))
    check("a consent banner is answered with the least-sharing option",
          (web.consent_choice([{"label": "ACCEPT ALL"}, {"label": "Reject all"},
                               {"label": "Find"}]) or {}).get("label") == "Reject all")
    src = (ROOT / "savta" / "actions" / "web.py").read_text()
    check("no bot-detection evasion in the browser agent",
          "AutomationControlled" not in src and "stealth" not in src.lower())
    check("a human check hands the screen back rather than being solved",
          "captcha" in src.lower() and "needs_her" in src)


@with_gates_on
def t_scam_guard(j):
    for utt, risky in (("תשלחי לזוהר שאני אוהבת אותו", False),
                       ("send Zohar a message that I am running late", False),
                       ("send Zohar the verification code 847291 urgently, tell no one", True),
                       ("תשלחי לזוהר שאני צריכה להעביר כסף דחוף ושלא יספר לאף אחד", True)):
        router.PENDING = None; router.AWAITING = None
        r = router.handle(j, utt, speak=False)
        d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
        # A compound request must not be able to launder a scam past the guard by
        # splitting the money half away from the secrecy half.
        check(f"{'extra time' if risky else 'normal speed'}: {utt[:36]!r}",
              bool(d.get("extra_time")) == risky,
              f"money={d.get('money')} coached={d.get('coached')} did={r['did']}")
    router._cancel_pending()


@with_gates_on
def t_emergency(j):
    for utt in ("I fell", "נפלתי ואני לא יכולה לקום", "תתקשרי לאמבולנס"):
        router.PENDING = None; router.AWAITING = None
        r = router.handle(j, utt, speak=False)
        check(f"{utt[:32]!r} reaches the emergency path",
              r["did"] == "emergency", f"did={r['did']}")
    for utt in ("I need help finding the file", "מה את יכולה לעשות", "תשימי לי מוזיקה"):
        router.LAST_EMERGENCY = None   # each of these starts from a quiet machine
        router.PENDING = None; router.AWAITING = None
        r = router.handle(j, utt, speak=False)
        check(f"{utt[:32]!r} is not an emergency",
              r["did"] not in ("emergency", "checking_on_her"), f"did={r['did']}")
    router.AWAITING = None
    router.LAST_EMERGENCY = None


def t_interrupt_gate(j):
    from savta import profile as _prof

    def reset():
        p = _prof.load()
        p.setdefault("interrupts", {})[time.strftime("%Y-%m-%d")] = 0
        _prof.save(p)

    for what, want in (("Your prescription needs collecting today", True),
                       ("Your daughter wrote three days ago and you have not answered", True),
                       ("MicMic has been running for six hours", False),
                       ("A new version of the software is available", False)):
        reset()
        ok, _ = router.may_interrupt(j, what, "hebrew", busy=False)
        check(f"{'speaks' if want else 'holds'}: {what[:42]!r}", ok == want)
    reset()
    ok, m = router.may_interrupt(j, "Your prescription needs collecting today",
                                 "hebrew", busy=True)
    check("never interrupts her mid-conversation", not ok, str(m.get("why")))
    reset()


def t_volume_vs_content(j):
    """"Too loud" is a complaint about the music and reads as a rejection of it.
    She wants the knob turned, not a different song. Both readings are defensible,
    which is exactly why this needs pinning down."""
    from savta.actions import mac as _mac
    vol = []
    real_volume, real_url = _mac.volume, _mac.open_url
    _mac.volume = lambda d: (vol.append(d), (True, "62"))[1]
    _mac.open_url = lambda u: None
    router.MEM = router.Memory()
    router.LAST_EMERGENCY = None      # section 13 leaves a call armed
    try:
        router.handle(j, "play me a song by Umm Kulthum", speak=False)
        check("something is playing before the complaint", bool(router.MEM.last_played))
        for utt, want in (("it's too quiet", "louder"), ("I cannot hear it", "louder"),
                          ("תגבירי", "louder"), ("make it louder", "louder"),
                          ("turn it down", "quieter"), ("it is too loud", "quieter"),
                          ("too noisy", "quieter"), ("תנמיכי את הקול", "quieter")):
            router.PENDING = None; router.AWAITING = None
            r = r0 = router.handle(j, utt, speak=False)
            check(f"{utt!r} turns the knob, not the track", r["did"] == want,
                  f"did={r['did']}")
        check("the volume was actually moved", len(vol) == 8, f"{len(vol)} calls")
        check("the screen is told the real level, not a guess",
              r0.get("detail", {}).get("level") == 62, str(r0.get("detail")))
        check("louder is positive, quieter is negative",
              vol[:4] == [18, 18, 18, 18] and vol[4:] == [-18, -18, -18, -18], str(vol))
        router.PENDING = None; router.AWAITING = None
        r = router.handle(j, "no, something else", speak=False)
        check("a real rejection still swaps the track", r["did"] == "playing",
              f"did={r['did']}")
        # Only two fake videos exist, so the second refusal exhausts them. She must
        # not be told it was never found — she refused it.
        router.PENDING = None; router.AWAITING = None
        r = router.handle(j, "תשימי משהו אחר", speak=False)
        check("refusing everything says so honestly", r["did"] == "exhausted",
              f"did={r['did']}")
        check("and does not claim it could not be found",
              "לא מצאתי" not in (r.get("say") or ""), r.get("say"))
    finally:
        _mac.volume, _mac.open_url = real_volume, real_url
        router.AWAITING = None


@with_gates_on
def t_emergency_undo(j):
    """A fall is dialled instantly, with no six-second window — that is the right
    trade. The price is false alarms, so the call has to be retractable, and it must
    never dial the same person twice."""
    router.LAST_EMERGENCY = None
    CALLED.clear(); HUNGUP.clear()
    r = router.handle(j, "I fell and I cannot get up", speak=False)
    check("a fall dials immediately", r["did"] == "emergency" and len(CALLED) == 1,
          f"did={r['did']} calls={len(CALLED)}")

    for utt in ("I cannot get up", "help me", "נפלתי"):
        router.PENDING = None; router.AWAITING = None
        r = router.handle(j, utt, speak=False)
        check(f"{utt!r} does not dial a second time", len(CALLED) == 1,
              f"{len(CALLED)} calls")
        check(f"{utt!r} is told help is already coming",
              r["did"] == "emergency" and r["detail"].get("redialled") is False,
              f"did={r['did']} detail={r.get('detail')}")

    # The bug this guards: the generic cancel question says yes to "turn it down",
    # which would hang up a call that is ringing because she fell.
    for utt in ("turn it down", "play me some music", "open the calculator"):
        router.PENDING = None; router.AWAITING = None
        r = router.handle(j, utt, speak=False)
        check(f"{utt!r} does not hang up the call", len(HUNGUP) == 0,
              f"did={r['did']} hangups={len(HUNGUP)}")

    router.PENDING = None; router.AWAITING = None
    r = router.handle(j, "no, stop, I am fine", speak=False)
    check("she can call off a false alarm", r["did"] == "emergency_cancelled",
          f"did={r['did']}")
    check("and the call is actually hung up", len(HUNGUP) == 1, f"{len(HUNGUP)}")
    check("the retraction is not silent", bool(r.get("say")))

    router.PENDING = None; router.AWAITING = None
    r = router.handle(j, "I fell again and I cannot get up", speak=False)
    check("a real fall after a retraction still dials",
          r["did"] == "emergency" and len(CALLED) == 2,
          f"did={r['did']} calls={len(CALLED)}")
    router.LAST_EMERGENCY = None
    CALLED.clear(); HUNGUP.clear()


def t_language_from_script(j):
    """Three of the four languages are written in a script English does not use, so
    the characters settle it. Left to the model, "make it louder" came back in Arabic."""
    for utt, want in (("make it louder", "english"), ("open the calculator", "english"),
                      ("play something by Umm Kulthum", "english"),
                      ("תגבירי", "hebrew"), ("תשימי לי Beatles", "hebrew"),
                      ("شو الطقس اليوم", "arabic"), ("какая погода", "russian")):
        check(f"{utt!r} is {want}", router.language_of(utt, "hebrew") == want,
              router.language_of(utt, "hebrew"))
    check("digits carry no language, so her own is kept",
          router.language_of("123", "hebrew") == "hebrew")
    check("and so does silence", router.language_of("", "russian") == "russian")

    # End to end: an English request must not be answered in another language.
    for utt in ("make it louder", "open the calculator"):
        router.PENDING = None; router.AWAITING = None
        r = router.handle(j, utt, speak=False)
        check(f"{utt!r} is answered in English", r.get("lang") == "english",
              f"lang={r.get('lang')} say={r.get('say')!r}")
        check(f"{utt!r} has no Hebrew or Arabic in the reply",
              not re.search(r"[\u0590-\u05FF\u0600-\u06FF]", r.get("say") or ""),
              r.get("say"))
    router.AWAITING = None


def t_weather(j):
    """The second most asked question. It must use the town she named, speak her
    language, and not wait on a model in another country to phrase three numbers."""
    import time as _t
    for utt, lang, place, must in (
            ("מה מזג האוויר בתל אביב", "hebrew", "תל אביב", "מעלות"),
            ("what is the weather in London", "english", "London", "degrees"),
            ("какая погода в Москве", "russian", "Москве", "градус")):
        WEATHER_ASKED.clear()
        router.PENDING = None; router.AWAITING = None
        t0 = _t.time()
        r = router.handle(j, utt, speak=False)
        dt = (_t.time() - t0) * 1000
        say = r.get("say") or ""
        check(f"{utt[:30]!r} is answered", r["did"] == "answered", f"did={r['did']}")
        check(f"{utt[:30]!r} looks up the town she named",
              any(place.split()[0] in w for w in WEATHER_ASKED), str(WEATHER_ASKED))
        check(f"{utt[:30]!r} says that town back, not wttr's spelling",
              place.split()[0] in say and "Haifa" not in say, say)
        check(f"{utt[:30]!r} answers in her language", must in say, say)
        check(f"{utt[:30]!r} gives the temperature", "17" in say, say)
        check(f"{utt[:30]!r} answers in under 3s", dt < 3000, f"{dt:.0f}ms")
    check("rain over 40 percent tells her to take an umbrella",
          "מטריה" in (router._weather_line(FAKE_WEATHER, "תל אביב", "hebrew")))
    dry = dict(FAKE_WEATHER, rain_pct=10, code=113)
    check("a clear day does not", "מטריה" not in router._weather_line(dry, "תל אביב", "hebrew"))
    check("a Latin town gets the maqaf after the Hebrew preposition",
          "ב-Paris" in router._weather_line(dry, "Paris", "hebrew"),
          router._weather_line(dry, "Paris", "hebrew"))
    # The town is stored without the preposition now (see the place-name section), so
    # the line always adds exactly one — including for towns whose own name starts
    # with the same letter.
    check("the preposition is added exactly once",
          router._weather_line(dry, "תל אביב", "hebrew").startswith("בתל אביב"),
          router._weather_line(dry, "תל אביב", "hebrew"))
    check("and a town that itself begins with ב still gets one",
          router._weather_line(dry, "באר שבע", "hebrew").startswith("בבאר שבע"),
          router._weather_line(dry, "באר שבע", "hebrew"))
    router.AWAITING = None


def t_clock(j):
    """She asks the time more than she asks anything else. It must be instant, right,
    and never routed to a model in another country that may not have a clock."""
    import time as _t
    # Warm the address-book cache first. Its first load is allowed to be slow (it
    # copies a database behind a privacy permission); what she actually experiences,
    # every time after that, is what this section measures.
    router.handle(j, "מה השעה", speak=False)
    for utt, want_lang in (("what time is it", "english"), ("מה השעה", "hebrew"),
                           ("איזה יום היום", "hebrew"), ("what is the date today", "english"),
                           ("какое сегодня число", "russian")):
        router.PENDING = None; router.AWAITING = None
        t0 = _t.time()
        r = router.handle(j, utt, speak=False)
        dt = (_t.time() - t0) * 1000
        check(f"{utt!r} is answered", r["did"] == "answered", f"did={r['did']}")
        check(f"{utt!r} comes from this machine's clock",
              (r.get("detail") or {}).get("source") == "system clock", str(r.get("detail")))
        check(f"{utt!r} answers in under 1.5s", dt < 1500, f"{dt:.0f}ms")
        say = r.get("say") or ""
        check(f"{utt!r} contains the actual time",
              _t.strftime("%H:%M") in say or _t.strftime("%-I:%M") in say, say)
        check(f"{utt!r} never claims to have no clock",
              "no clock" not in say.lower() and "do not have" not in say.lower(), say)
    check("English says 21st, not 21th",
          not any(f"{d}th" in router._clock_line("english")
                  for d in (1, 2, 3, 21, 22, 23, 31)),
          router._clock_line("english"))
    # A timer is not a clock question; it must still reach the timer.
    router.PENDING = None; router.AWAITING = None
    r = router.handle(j, "תזכירי לי בעוד חמש דקות", speak=False)
    check("setting a timer is not mistaken for asking the time",
          r["did"] == "timer_set", f"did={r['did']}")
    router.AWAITING = None


@with_gates_on
def t_answer_to_a_call_is_not_a_message(j):
    """Agreeing to a CALL used to send the person a TEXT containing the word "yes",
    and the call was never placed. The dispatch defaulted every unrecognised question
    to "treat her answer as the message body"."""
    reset_state()
    router.AWAITING = None; router.PENDING = None; router.LAST_EMERGENCY = None
    r = router.handle(j, "אני כל כך לחוצה ומפוחדת, אני לא יודעת מה לעשות", speak=False)
    check("sounding frightened produces an offer to call someone",
          r["did"] in ("offered_help", "checking_on_her"), f"did={r['did']}")
    check("and the question is remembered",
          router.AWAITING is not None
          and router.AWAITING["need"] in ("confirm_call", "confirm_emergency"),
          str(router.AWAITING))

    r = router.handle(j, "כן בבקשה", speak=False)
    check("saying yes places the call", r["did"] == "emergency", f"did={r['did']}")
    check("and NOTHING was sent as a message", not SENT and not WA, f"{SENT} {WA}")
    check("and somebody was actually dialled", len(CALLED) == 1, str(CALLED))

    # ...and declining must not call, and must not text either.
    reset_state()
    router.AWAITING = None; router.LAST_EMERGENCY = None
    router.handle(j, "אני כל כך לחוצה ומפוחדת, אני לא יודעת מה לעשות", speak=False)
    if router.AWAITING:
        r = router.handle(j, "לא, אני בסדר", speak=False)
        check("saying no places no call", not CALLED, str(CALLED))
        check("and sends no message", not SENT and not WA, f"{SENT} {WA}")
        check("and still says something", bool(r.get("say")), f"did={r['did']}")
    router.AWAITING = None; router.LAST_EMERGENCY = None


@with_gates_on
def t_a_call_is_only_announced_if_it_happened(j):
    """"I am calling David" while no call was placed is the most dangerous sentence
    this program can say. It used to say it whenever a name existed, even when the
    contact had no number and even when the dial failed."""
    from savta.actions import mac as _mac
    reset_state(); router.LAST_EMERGENCY = None; router.AWAITING = None

    real_ft = _mac.facetime
    _mac.facetime = lambda n, number="", video=True: (False, "no number")
    try:
        r = router.handle(j, "I fell and I cannot get up", speak=False)
        check("a call that fails is not announced as a call",
              r["did"] != "emergency", f"did={r['did']} say={short(r.get('say'))}")
        check("she is told plainly that it did not go through",
              bool(r.get("say")), short(r.get("say")))
        check("and the do-not-redial latch is NOT armed on a failed call",
              router.LAST_EMERGENCY is None, str(router.LAST_EMERGENCY))
    finally:
        _mac.facetime = real_ft

    # a working call still works, and only then is the latch armed
    reset_state(); router.LAST_EMERGENCY = None
    r = router.handle(j, "I fell and I cannot get up", speak=False)
    check("a call that succeeds is announced", r["did"] == "emergency", f"did={r['did']}")
    check("and the latch is armed", router.LAST_EMERGENCY is not None)
    check("in every language, not just Hebrew and English",
          all(k in router.SPEECH for k in ("hebrew", "arabic", "russian", "english")))
    for lg in ("hebrew", "arabic", "russian", "english"):
        out = router.attempt_call(lg, "Zohar Levin")
        check(f"the call line exists in {lg}", bool(out.get("say")), str(out))
        check(f"{lg} never glues a preposition onto a Latin name",
              "לZohar" not in out["say"] and "بـZohar" not in out["say"], out["say"])
    router.LAST_EMERGENCY = None


def t_unknown_question_fails_closed(j):
    """A question kind nobody wrote a handler for must lose her answer, not repurpose
    it. This is the guard that would have caught the "yes please" text."""
    reset_state()
    router.AWAITING = {"need": "some_future_question", "question": "anything at all",
                       "lang": "hebrew", "contact": "Zohar Levin", "body": None}
    r = router.handle(j, "כן בבקשה", speak=False)
    check("an unknown pending question never becomes a message",
          not SENT and not WA and r["did"] != "sending", f"did={r['did']} {SENT} {WA}")
    check("and the stale question is cleared rather than left to catch the next thing",
          router.AWAITING is None or router.AWAITING.get("need") != "some_future_question",
          str(router.AWAITING))
    check("every question MicMic can ask has a declared meaning",
          set(router.NEED_KINDS) >= {"who", "what", "confirm_call", "confirm_emergency",
                                     "who_to_call"},
          str(sorted(router.NEED_KINDS)))
    router.AWAITING = None


def t_every_line_speaks_four_languages(j):
    """A spoken line written in only two languages reads out in English to an Arabic
    speaker, in Milena's Russian voice. This audits the source rather than guessing
    which branches a test happens to reach."""
    import ast
    LANGS = {"hebrew", "arabic", "russian", "english"}
    src = (ROOT / "savta" / "router.py").read_text()
    gaps = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if not (keys & LANGS):
            continue
        # Only dicts whose values are sentences. Tables of prefixes and script regexes
        # legitimately have no English entry.
        spoken = any(
            isinstance(v, ast.JoinedStr) or
            (isinstance(v, ast.Constant) and isinstance(v.value, str) and " " in v.value)
            for v in node.values)
        missing = sorted(LANGS - (keys & LANGS))
        # English is allowed to live in the .get() default instead of the dict.
        if spoken and missing and missing != ["english"]:
            gaps.append((node.lineno, missing))
    check("every spoken line exists in all four languages",
          not gaps, "; ".join(f"router.py:{ln} missing {m}" for ln, m in gaps))

    for key in router.SPEECH["english"]:
        for lg in ("hebrew", "arabic", "russian"):
            check(f"SPEECH[{lg}] has {key!r}", key in router.SPEECH[lg],
                  f"{lg} is missing {key!r}")


@with_gates_on
def t_emergency_contact_by_voice(j):
    """Onboarding can end without one, and the only other way to set it was editing a
    JSON file — useless to someone who cannot type. It must be sayable."""
    reset_state()
    router.AWAITING = None; router.LAST_EMERGENCY = None
    before = prof.load().get("emergency_contact")
    try:
        r = router.handle(j, "אם יקרה לי משהו תתקשרי לזוהר", speak=False)
        check("naming who to call in an emergency is understood as a setting",
              r["did"] == "emergency_contact_set", f"did={r['did']}")
        check("and it does not call anybody right now", not CALLED, str(CALLED))
        check("and it is actually stored",
              (prof.load().get("emergency_contact") or "").startswith("Zohar"),
              repr(prof.load().get("emergency_contact")))

        # ...while an ordinary request to call someone still calls them.
        reset_state(); router.AWAITING = None; router.LAST_EMERGENCY = None
        r = router.handle(j, "תתקשרי לזוהר", speak=False)
        check("'call Zohar' still places a call now", r["did"] == "calling",
              f"did={r['did']}")
        check("and does not quietly change the emergency contact",
              len(CALLED) == 1, str(CALLED))
    finally:
        pr = prof.load(); pr["emergency_contact"] = before or ""; prof.save(pr)
        router.LAST_EMERGENCY = None


def t_the_address_book_can_never_hang_a_request(j):
    """The worst bug of the day: reading WhatsApp's container is gated by macOS
    privacy and BLOCKS instead of failing when the permission is not there. Every
    request called it, so the server answered /api/health and nothing else — asking
    the time got no reply at all. Nothing she says may ever wait on the address book."""
    import time as _t
    from savta.actions import contacts as _book

    real_copy = _book._copy_with_timeout
    real_cache = dict(_book._CACHE)
    real_fail = _book._LAST_FAIL[0]
    real_failed = dict(_book._COPY_FAILED)

    def never_returns(src, dst, timeout=_book.COPY_TIMEOUT):
        _t.sleep(timeout)               # exactly what the real block looks like
        return False

    _book._copy_with_timeout = never_returns
    _book._CACHE.update(at=0.0, rows=[])
    _book._LAST_FAIL[0] = 0.0
    _book._COPY_FAILED.clear()
    try:
        t0 = _t.time()
        rows = REAL_ALL_CONTACTS()          # the real one, not the suite's fixture
        first = _t.time() - t0
        check("an unreadable address book gives up instead of hanging",
              first < _book.LOAD_BUDGET + 1.0, f"took {first:.1f}s")
        check("and it returns an empty book rather than raising", rows == [], str(rows)[:60])

        # ...and the second call must not pay the whole budget over again.
        _t.sleep(_book.COPY_TIMEOUT + 0.3)     # let the background loader finish failing
        t0 = _t.time()
        REAL_ALL_CONTACTS()
        second = _t.time() - t0
        check("a known-unreadable book is not waited on a second time",
              second < 0.5, f"took {second:.1f}s")

        # The message store is much larger and has exactly the same trap.
        t0 = _t.time()
        REAL_RECENT_CHATS()
        chats = _t.time() - t0
        check("reading her messages cannot hang either",
              chats < _book.COPY_TIMEOUT * 2 + 1.0, f"took {chats:.1f}s")

        # And a whole turn still completes with no address book at all.
        reset_state()
        router.AWAITING = None; router.LAST_EMERGENCY = None
        t0 = _t.time()
        r = router.handle(j, "מה השעה", speak=False)
        turn = _t.time() - t0
        check("she can still ask the time with no address book",
              r["did"] == "answered", f"did={r['did']}")
        check("and the answer is not slowed to a crawl by it",
              turn < 6.0, f"took {turn:.1f}s")
    finally:
        _book._copy_with_timeout = real_copy
        _book._CACHE.update(real_cache)
        _book._LAST_FAIL[0] = real_fail
        _book._COPY_FAILED.clear(); _book._COPY_FAILED.update(real_failed)


@with_gates_on
def t_a_long_answer_can_be_interrupted(j):
    """She had to sit through twenty seconds of speech before the machine would listen
    again, because `say` was started and forgotten. Nothing could stop it."""
    from savta.actions import mac as _mac
    real_say, real_stop = _mac.say, _mac.stop_speaking
    _mac.say, _mac.stop_speaking = REAL_SAY, REAL_STOP_SPEAKING
    try:
        _mac.say("A sentence long enough that it is certainly still being read out "
                 "by the time the next line of this test runs.", "english")
        time.sleep(0.4)
        check("a spoken sentence is known to be in progress", _mac.speaking())
        check("and it can be cut off", _mac.stop_speaking())
        time.sleep(0.2)
        check("after which nothing is being said", not _mac.speaking())
        check("stopping silence reports that nothing was stopped",
              not _mac.stop_speaking())
        # A new answer must not talk over the old one.
        _mac.say("First sentence, quite a long one so it is still going.", "english")
        time.sleep(0.3)
        _mac.say("Second.", "english")
        time.sleep(0.1)
        check("a new answer replaces the old one rather than overlapping it",
              _mac.speaking())
        _mac.stop_speaking()
    finally:
        _mac.say, _mac.stop_speaking = real_say, real_stop



def t_close_app(j):
    """"תסגרי בבקשה את whatsapp" reached the volume and brightness controls and was
    told it could not be done — there was no way to quit a program at all. The
    candidates are the programs actually running, so it can only close one that is."""
    reset_state()
    for utt in ("תסגרי בבקשה את whatsapp", "close WhatsApp"):
        QUIT.clear()
        r = router.handle(j, utt, speak=False)
        check(f"{utt!r} closes the program", r["did"] == "closed_app", f"did={r['did']}")
        check(f"{utt!r} quits WhatsApp and nothing else", QUIT == [[4101]], str(QUIT))
    QUIT.clear()
    r = router.handle(j, "close Spotify", speak=False)
    check("a program that is not open is said so", r["did"] == "not_open", f"did={r['did']}")
    check("and nothing is quit", not QUIT, str(QUIT))
    QUIT.clear()
    r = router.handle(j, "close this", speak=False)
    check("'close this' still closes what is in front, not a whole program",
          r["did"] != "closed_app" and not QUIT, f"did={r['did']} quit={QUIT}")


def t_screen(j):
    """"What is on my screen", "summarize this", "send this to Miriam", "add this to my
    calendar". The screen is stubbed: what matters here is the routing, what reaches
    the model, what is sent, and that nothing is done without being asked first."""
    import datetime as _dt
    reset_state()
    real = (router._screen_context, router._screen_shot, router._screen_llm, mac.add_event)
    PROMPTS, EVENTS = [], []
    tomorrow = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    state = {"ctx": None, "reply": "You are reading an article about olive harvests."}

    def ctx(**kw):
        base = {"permissions": {"accessibility": True, "screen_recording": False},
                "frontmost": {"app": "Google Chrome", "pid": 1, "bundle_id": "com.google.Chrome",
                              "window": "Olive harvest"},
                "selected": "", "focused": {"role": "", "value": "", "secure": False},
                "visible": {"text": "The olive harvest in the Galilee starts in October. "
                                    "Dentist appointment Thursday 10:00.", "truncated": False},
                "page": {"url": "https://example.org/olives", "title": "Olive harvest"},
                "has_image": False}
        base.update(kw)
        return base

    router._screen_context = lambda max_chars=6000: state["ctx"]
    router._screen_shot = lambda pid=None: None
    router._screen_llm = lambda prompt, system, image=None, max_tokens=300: (
        PROMPTS.append((prompt, system)), state["reply"])[1]
    mac.add_event = lambda *a: (EVENTS.append(a), (True, "[test stub] added"))[1]
    SCREEN_DIDS = ("described_screen", "summarized_screen", "translated_screen")
    try:
        state["ctx"] = ctx()
        for utt in ("what is on my screen?", "מה יש לי על המסך?", "תסכמי את זה",
                    "что у меня на экране?"):
            PROMPTS.clear()
            r = router.handle(j, utt, speak=False)
            check(f"{utt!r} reads the screen", r["did"] in SCREEN_DIDS, f"did={r['did']}")
            check(f"{utt!r} shows the model the screen text",
                  PROMPTS and "olive harvest" in PROMPTS[-1][0].lower(), short(PROMPTS))
            check(f"{utt!r} keeps screen content out of the trace detail",
                  "olive" not in json.dumps(r.get("detail") or {}).lower(), short(r.get("detail")))

        r = router.handle(j, "this is great, thank you", speak=False)
        check("a thank-you is not a screen request", r["did"] not in SCREEN_DIDS, f"did={r['did']}")

        state["ctx"] = ctx(permissions={"accessibility": False, "screen_recording": False})
        r = router.handle(j, "what is on my screen?", speak=False)
        check("no Accessibility says how to turn it on",
              r["did"] == "screen_unavailable" and "Accessibility" in (r.get("say") or ""),
              f"did={r['did']} say={short(r.get('say'))}")

        state["ctx"] = ctx(frontmost={"app": "MicMic", "pid": 2, "bundle_id": "com.betterfly.micmic",
                                      "window": ""})
        r = router.handle(j, "what is on my screen?", speak=False)
        check("MicMic in front is said, never described to itself",
              r["did"] == "screen_unavailable" and r["detail"]["why"] == "front_is_me",
              f"did={r['did']} detail={r.get('detail')}")

        state["ctx"] = ctx(focused={"role": "AXSecureTextField", "value": "", "secure": True},
                           selected="")
        PROMPTS.clear()
        router.handle(j, "what is on my screen?", speak=False)
        check("a password field adds nothing to the prompt",
              PROMPTS and "focused field" not in PROMPTS[-1][0], short(PROMPTS))

        state["ctx"] = ctx(selected="Meet me at the north gate at seven.")
        r = router.handle(j, "read this to me", speak=False)
        check("'read this to me' reads the selection with no model",
              r["did"] == "read_screen" and "north gate" in (r.get("say") or ""),
              f"did={r['did']} say={short(r.get('say'))}")
        state["ctx"] = ctx(focused={"role": "AXTextField", "value": "771246", "secure": False})
        r = router.handle(j, "read this to me", speak=False)
        check("with nothing selected it reads the screen, never the focused field alone",
              r["did"] == "read_screen" and "771246" not in (r.get("say") or ""),
              f"did={r['did']} say={short(r.get('say'))}")
        from savta import trace as _trace
        rows = _trace.PATH.read_text(encoding="utf-8") if _trace.PATH.exists() else ""
        check("the trace never holds what was read off the screen",
              "north gate" not in rows and "olive harvest in the galilee" not in rows.lower()
              and "from the screen" in rows, f"{len(rows)} bytes of trace")

        # --- send this -----------------------------------------------------
        with gates_on():
            reset_state()
            state["ctx"] = ctx(selected="Meet me at the north gate at seven.")
            r = router.handle(j, "send this to Miriam", speak=False)
            check("'send this to Miriam' sends the selection",
                  r["did"] == "sending" and router.PENDING
                  and router.PENDING.get("text") == "Meet me at the north gate at seven.",
                  f"did={r['did']} pending={router.PENDING}")
            check("and does not read the whole selection back", "north gate" not in (r.get("say") or ""),
                  short(r.get("say")))
            router.PENDING = None

            reset_state()
            state["ctx"] = ctx(selected="")
            r = router.handle(j, "תשלחי את זה למרים", speak=False)
            check("nothing selected in a browser sends the page link",
                  r["did"] == "sending" and router.PENDING
                  and router.PENDING.get("text") == "https://example.org/olives",
                  f"did={r['did']} pending={router.PENDING}")
            router.PENDING = None

            reset_state()
            state["ctx"] = ctx(selected="", page=None)
            r = router.handle(j, "send this to Miriam", speak=False)
            check("nothing selected and no page asks her to select it",
                  r["did"] == "screen_select" and router.PENDING is None,
                  f"did={r['did']} pending={router.PENDING}")

            reset_state()
            state["ctx"] = ctx(selected="Meet me at the north gate at seven.")
            r = router.handle(j, "send Miriam a message that I am running late", speak=False)
            check("a message with its own words ignores the screen",
                  r["did"] == "sending" and router.PENDING
                  and "north gate" not in router.PENDING.get("text", ""),
                  f"did={r['did']} pending={router.PENDING}")
            router.PENDING = None

            reset_state()
            r = router.handle(j, "send this", speak=False)
            check("'send this' with nobody named asks who", r["did"] == "need_who", f"did={r['did']}")
            r = router.handle(j, "Miriam", speak=False)
            check("naming her then sends the selection",
                  r["did"] == "sending" and router.PENDING
                  and router.PENDING.get("text") == "Meet me at the north gate at seven.",
                  f"did={r['did']} pending={router.PENDING}")
            router.PENDING = None
            rows = _trace.PATH.read_text(encoding="utf-8") if _trace.PATH.exists() else ""
            check("and the trace keeps the selection's length, not its words",
                  "north gate" not in rows, f"{len(rows)} bytes of trace")

        with gates_on():
            reset_state()
            state["ctx"] = ctx(selected="Your code is [hidden]")
            r = router.handle(j, "send this to Miriam", speak=False)
            check("a selection that held a code is never sent",
                  r["did"] == "screen_secret" and router.PENDING is None and router.AWAITING is None,
                  f"did={r['did']} pending={router.PENDING}")
            reset_state()
            state["ctx"] = ctx(selected="word " * 1500)
            r = router.handle(j, "send this to Miriam", speak=False)
            check("a very long selection sends the start and says so",
                  r["did"] == "sending" and len(router.PENDING.get("text", "")) == router.SEND_SCREEN_MAX
                  and "only the beginning" in (r.get("say") or ""),
                  f"did={r['did']} say={short(r.get('say'))}")
            router.PENDING = None

        reset_state()
        state["ctx"] = ctx(frontmost={"app": "Google Chrome", "pid": 1, "bundle_id": "com.google.Chrome",
                                      "window": "MicMic"},
                           page={"url": "http://127.0.0.1:8799/", "title": "MicMic"})
        r = router.handle(j, "what is on my screen?", speak=False)
        check("MicMic's own page in a browser is not described to itself",
              r["did"] == "screen_unavailable", f"did={r['did']}")

        state["ctx"] = ctx(visible={"text": "Inbox | Your code is [hidden] | Lunch at noon", "truncated": False})
        r = router.handle(j, "read me the screen", speak=False)
        check("read aloud turns separators into pauses and never says the placeholder",
              r["did"] == "read_screen" and "|" not in r["say"] and "hidden" not in r["say"],
              short(r.get("say")))

        # --- add this to my calendar ---------------------------------------
        reset_state()
        state["ctx"] = ctx()
        state["reply"] = json.dumps({"title": "Dentist", "date": tomorrow, "start": "10:00",
                                     "end": "", "location": ""})
        EVENTS.clear()
        r = router.handle(j, "add this to my calendar", speak=False)
        check("'add this to my calendar' asks first",
              r["did"] == "confirm_calendar" and not EVENTS and "Dentist" in (r.get("say") or ""),
              f"did={r['did']} events={EVENTS} say={short(r.get('say'))}")
        r = router.handle(j, "yes please", speak=False)
        check("and a yes adds exactly that event",
              r["did"] == "calendar_added" and EVENTS == [("Dentist", tomorrow, "10:00", "", "")],
              f"did={r['did']} events={EVENTS}")

        reset_state(); EVENTS.clear()
        router.handle(j, "תכניסי את זה ליומן", speak=False)
        r = router.handle(j, "לא, עזבי", speak=False)
        check("a no adds nothing", r["did"] == "calendar_declined" and not EVENTS,
              f"did={r['did']} events={EVENTS}")

        reset_state(); EVENTS.clear()
        for bad in ('{"none": true}', "not json at all",
                    json.dumps({"title": "X", "date": "2020-01-01", "start": "10:00"}),
                    json.dumps({"title": "X", "date": tomorrow, "start": "25:00"})):
            state["reply"] = bad
            r = router.handle(j, "add this to my calendar", speak=False)
            check(f"no usable event ({bad[:30]!r}) is said, and nothing is added",
                  r["did"] == "screen_no_event" and not EVENTS and router.AWAITING is None,
                  f"did={r['did']} events={EVENTS}")
    finally:
        router._screen_context, router._screen_shot, router._screen_llm, mac.add_event = real
        router.AWAITING = None; router.PENDING = None


def t_music_swaps(j):
    """While a song plays: a stray sound changes nothing, "no" replays the same search,
    and describing what she wants instead is a NEW search. All three were wrong in her
    session: 'כ' changed the song, and 'שיר יותר גברי בבקשה' replayed the old query and
    played the same children's song again. History is passed the way the server does,
    because without it 'כ' measured 0.23 and with it, in real use, 0.79."""
    reset_state()
    # Umm Kulthum, because FAKE_VIDEOS holds one of her concerts: any other artist is
    # correctly not_found against the stub, and then nothing is playing to swap.
    first = "תשימי לי שיר של אום כולתום"
    r = router.handle(j, first, speak=False)
    check("a song is playing to start with", r["did"] == "playing", f"did={r['did']}")
    subject = router.MEM.play_query or "\x00"   # never matches, if setup failed
    hist = f"{first} -> playing"

    YT_QUERIES.clear(); OPENED.clear()
    r = router.handle(j, "כ", hist, speak=False)
    check("a stray letter does not change the song",
          r["did"] != "playing" and not OPENED, f"did={r['did']} opened={OPENED}")

    YT_QUERIES.clear()
    r = router.handle(j, "לא", hist, speak=False)
    d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
    check("'לא' plays another one", r["did"] == "playing" and d.get("after_rejection"),
          f"did={r['did']} detail={short(d)}")
    check("from the same search", bool(YT_QUERIES) and YT_QUERIES[0].startswith(subject),
          str(YT_QUERIES))

    YT_QUERIES.clear()
    r = router.handle(j, "שיר יותר שמח בבקשה", hist, speak=False)
    d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
    check("'a happier song' searches again instead of replaying",
          bool(YT_QUERIES) and not YT_QUERIES[0].startswith(subject)
          and not d.get("after_rejection"), f"did={r['did']} queries={YT_QUERIES}")

    YT_QUERIES.clear()
    r = router.handle(j, "something in English", hist, speak=False)
    check("'something in English' is a music request, not small talk",
          r["did"] != "chatted" and any("english" in q.lower() for q in YT_QUERIES),
          f"did={r['did']} queries={YT_QUERIES}")

    # The override that rescues "something in English" must never reach an action.
    # Measured: this scores 0.53-0.62 on describes_instead — above the gate — because
    # it names a language too. It is a message, and has to stay one.
    YT_QUERIES.clear()
    router._cancel_pending(); router.AWAITING = None
    r = router.handle(j, "send Matan a message in English", hist, speak=False)
    check("a message that names a language is still a message, not music",
          r["did"] not in ("playing", "not_found") and not YT_QUERIES,
          f"did={r['did']} queries={YT_QUERIES}")
    router._cancel_pending(); router.AWAITING = None


@with_gates_on
def t_undo(j):
    """The bar offers one Undo for the thing just done. It must reverse exactly that,
    only once, never reach past a newer action, never be offered where there is no
    real inverse, and never close something she already had open."""
    reset_state(); router._drop_undo()

    r = router.handle(j, "make it louder", speak=False)
    check("a volume change offers Undo", r["did"] == "louder" and r.get("undo"), short(r))
    u = router.undo_last()
    check("Undo puts the volume back to exactly what it was", u["undone"] and VOLSET == [50],
          f"{u} volset={VOLSET}")
    check("and a second Undo finds nothing", router.undo_last()["did"] == "nothing_to_undo")

    # By voice, in four languages, with the memory snapshot as context (the shape the
    # app sends). These used to reach stop / go_back and close the front window.
    real_stop = mac.stop_speaking
    mac.stop_speaking = lambda: True
    try:
        for phrase in ("undo", "בטלי", "отмени", "no wait put it back"):
            reset_state(); router._drop_undo(); VOLSET.clear(); CLOSED.clear()
            router.handle(j, "make it louder", speak=False)
            r = router.handle(j, phrase, speak=False)
            check(f"{phrase!r} after a volume change undoes it by voice",
                  r["did"] == "undid_louder" and VOLSET == [50] and not CLOSED,
                  f"did={r['did']} volset={VOLSET} closed={CLOSED}")
        reset_state(); router._drop_undo(); CLOSED.clear()
        r = router.handle(j, "undo", speak=False)
        check("'undo' with nothing to undo says so and closes nothing",
              r["did"] == "nothing_to_undo" and not CLOSED, f"did={r['did']} closed={CLOSED}")
        reset_state(); CLOSED.clear()
        r = router.handle(j, "stop", speak=False)
        check("'stop' with nothing playing never closes her window",
              r["did"] == "stopped" and not CLOSED, f"did={r['did']} closed={CLOSED}")
    finally:
        mac.stop_speaking = real_stop

    # Asked what the message should say, she said the instruction again; that used
    # to become the message itself ("תכתבי לניר כהן" sent to Nir Cohen).
    with gates_on():
        reset_state()
        r = router.handle(j, "send a message to Miriam", speak=False)
        check("a message with no words asks what to say", r["did"] == "need_what", f"did={r['did']}")
        r = router.handle(j, "write to Miriam", speak=False)
        check("repeating the instruction asks again instead of sending it",
              r["did"] == "need_what" and router.PENDING is None, f"did={r['did']} pending={router.PENDING}")
        r = router.handle(j, "I am running late", speak=False)
        check("and the real answer is what gets sent",
              r["did"] == "sending" and (router.PENDING or {}).get("text") == "I am running late",
              f"did={r['did']} pending={router.PENDING}")
        router.PENDING = None

    r = router.handle(j, "open the calculator", speak=False)
    check("opening an app that was not running offers Undo", bool(r.get("undo")), short(r))
    FAKE_RUNNING.append({"name": "Calculator", "pids": [4199]})
    try:
        QUIT.clear()
        u = router.undo_last()
        check("Undo quits only that app", u["undone"] and QUIT == [[4199]], f"{u} quit={QUIT}")
    finally:
        FAKE_RUNNING.pop()

    # Mail: in the stub's installed list AND running, so it is "already open".
    FAKE_RUNNING.append({"name": "Mail", "pids": [4198]})
    try:
        r = router.handle(j, "open Mail", speak=False)
        check("opening an app that was ALREADY running offers no Undo (it would quit her app)",
              r["did"] == "opened_app" and not r.get("undo"),
              f"did={r['did']} undo={r.get('undo')}")
    finally:
        FAKE_RUNNING.pop()

    APPS.clear()
    r = router.handle(j, "close WhatsApp", speak=False)
    u = router.undo_last()
    check("Undo after closing an app reopens it", r["did"] == "closed_app" and u["undone"]
          and APPS == ["WhatsApp"], f"did={r['did']} undo={u} apps={APPS}")

    r = router.handle(j, "remind me in 5 minutes to call mom", speak=False)
    before = len(router.mac.pending_timers())
    u = router.undo_last()
    check("Undo cancels the reminder just set", r["did"] == "timer_set" and u["undone"]
          and len(router.mac.pending_timers()) == before - 1, f"{u}")

    router.CANCEL_WINDOW = 30.0
    try:
        r = router.handle(j, "תשלחי הודעה לזוהר שאני בסדר", speak=False)
        u = router.undo_last()
        check("Undo during the countdown stops the message", r["did"] == "sending"
              and u["undone"] and router.PENDING is None and not SENT,
              f"did={r['did']} undo={u} sent={SENT}")
    finally:
        router._cancel_pending(); router.CANCEL_WINDOW = 6.0

    router._offer_undo("sending", "english", router._undo_send, router._DONE["send"])
    u = router.undo_last()
    check("Undo after it already went says so, never pretends",
          not u["undone"] and "already sent" in u["say"], str(u))

    TABS_CLOSED.clear()
    r = router.handle(j, "תשימי לי שיר של אום כולתום", speak=False)
    vid = (r.get("detail") or {}).get("video_id")
    u = router.undo_last()
    check("Undo on a song closes only that video's tab", r["did"] == "playing" and u["undone"]
          and TABS_CLOSED == [vid] and not CLOSED, f"undo={u} tabs={TABS_CLOSED} cmd_w={CLOSED}")

    router.handle(j, "make it louder", speak=False)
    router.handle(j, "what time is it", speak=False)
    check("a newer action takes the old Undo away",
          router.undo_last()["did"] == "nothing_to_undo")

    router._offer_undo("louder", "english", lambda: True, router._DONE["volume"])
    router._UNDO["at"] -= router.UNDO_WINDOW_S + 1
    check("an Undo too old is refused, honestly", router.undo_last()["did"] == "undo_expired")


def t_volume_words_with_context(j):
    """"make it quieter" was 6/6 on the bare question and 0/6 once the router added
    her memory snapshot AND her likes together, so MicMic answered "I can't do that".
    Through the real router, with the real context, several times."""
    reset_state()
    for utt, want in (("make it quieter", "quieter"), ("make it louder", "louder"),
                      ("it's too loud", "quieter"), ("תנמיכי", "quieter")):
        dids = [router.handle(j, utt, speak=False)["did"] for _ in range(3)]
        check(f"{utt!r} changes the volume every time", dids.count(want) == 3, str(dids))

def t_it_admits_it_cannot_skip(j):
    """Forward and back used to say "Alright" and jump to a fixed point in the video.

    The embedded player is gone, so videos play in a browser tab MicMic does not
    drive and pause/resume cannot be carried out either. One honest answer now covers
    all four, and it still has to name what IS possible."""
    reset_state()
    router.MEM = router.Memory()
    router.MEM.last_played = {"id": "x", "title": "something"}
    for utt in ("skip forward", "קפצי קדימה"):
        router.PENDING = None; router.AWAITING = None
        r = router.handle(j, utt, speak=False)
        if r["did"] == "cannot_seek":
            check(f"{utt!r} says plainly that it cannot skip", True)
            check("and it does not say Alright",
                  "בסדר" not in (r.get("say") or "") and
                  (r.get("say") or "").lower() != "alright", short(r.get("say")))
            check("and it offers what it CAN do",
                  bool((r.get("detail") or {}).get("supported")), str(r.get("detail")))
            break
    else:
        check("a skip request is answered honestly", False,
              "neither phrasing reached the seek branch")
    router.MEM = router.Memory()


def t_inside_other_apps(j):
    """Driving another Mac application through its accessibility tree. The same shape
    as the browser agent: code enumerates the real controls, Jev points at one."""
    from savta.actions import apps

    # Refusals are enforced in plain Python, before any model pick can reach them.
    for label in ("Delete", "Move to Trash", "Empty Trash", "Reset", "Uninstall",
                  "מחק", "حذف", "удалить"):
        el = {"id": 1, "label": label, "role": "AXButton", "kind": "click",
              "value": "", "secure": False}
        check(f"{label!r} is refused as destructive", apps.is_destructive(el))
    # "Clear" needs judgement rather than a blanket rule. A calculator's "All Clear"
    # wipes a number nobody minds losing; "Clear History" does not come back. Found by
    # watching the agent route around a refused Delete by pressing All Clear instead —
    # which is how a guard that matches labels gets defeated.
    for label in ("Clear History", "Clear All Data", "Clear Messages", "Clear Cache",
                  "Clear all", "נקה הכל"):
        el = {"id": 1, "label": label, "role": "AXButton", "kind": "click",
              "value": "", "secure": False}
        check(f"{label!r} is refused", apps.is_destructive(el))
    for label in ("All Clear", "Clear", "Clear Display", "Clear Formatting"):
        el = {"id": 1, "label": label, "role": "AXButton", "kind": "click",
              "value": "", "secure": False}
        check(f"{label!r} is allowed", not apps.is_destructive(el))

    for label in ("Save", "Send", "Next", "Search", "New Note"):
        el = {"id": 1, "label": label, "role": "AXButton", "kind": "click",
              "value": "", "secure": False}
        check(f"{label!r} is not mistaken for destructive", not apps.is_destructive(el))

    check("a secure text field is refused outright",
          apps.is_forbidden({"label": "", "role": "AXSecureTextField", "value": "",
                             "secure": True}))
    check("a card field is refused inside an app too",
          apps.is_forbidden({"label": "Card number", "role": "AXTextField",
                             "value": "", "secure": False}))

    # ...and act() stops before dispatching, not after.
    fake = {"refs": {1: object()}}
    for label, op, expect in (("Delete", "click", "delete"),
                              ("Pay now", "click", "purchase"),
                              ("Password", "type", "password")):
        el = {"id": 1, "label": label, "kind": op, "value": "",
              "role": "AXSecureTextField" if label == "Password" else "AXButton",
              "secure": label == "Password"}
        ok, msg = apps.act(fake, el, op, "x")
        check(f"act() refuses {label!r} before doing anything",
              not ok and expect in msg, f"ok={ok} msg={msg}")

    check("this project still has no delete path of its own",
          "AXPress" in (ROOT / "savta" / "actions" / "apps.py").read_text()
          and apps.DESTRUCTIVE.search("delete") is not None)

    # The honest degradation: it says what is missing rather than doing nothing.
    ok, why = apps.available()
    check("it can say whether it is usable at all", isinstance(ok, bool) and bool(why))
    seen, why2 = apps.can_see_inside("Finder")
    check("and whether it can actually read inside an app",
          isinstance(seen, bool) and bool(why2), why2)
    if not seen:
        check("...naming the permission when it cannot",
              "Accessibility" in why2 or "no window" in why2 or "not running" in why2,
              why2)


@with_gates_on
def t_the_room_cannot_cancel_her_message(j):
    """The microphone is open during the countdown, so it hears whoever else is in the
    room. "No no, I was talking to the cat" used to score 0.65 and silently bin a
    message she had never objected to.

    The asymmetry matters: a missed cancel sends a message she did not want, which is
    far worse than a false cancel, which only makes her say it again. So this is fixed
    by making the question sharper, never by demanding more confidence."""
    reset_state()
    router.AWAITING = None; router.LAST_EMERGENCY = None
    for utt in ("ביטול", "לא", "stop", "wait", "אל תשלחי", "no don't send that"):
        v = router._is_cancel(j, utt)[1]
        check(f"{utt!r} still stops a message", v > 0.5, f"{v:.2f}")
    for utt in ("no no I was talking to the cat", "לא לא דיברתי עם החתול",
                "I said no to him yesterday", "yes send it", "כן תשלחי"):
        v = router._is_cancel(j, utt)[1]
        check(f"{utt[:34]!r} does not", v <= 0.5, f"{v:.2f}")

    # ...and end to end, with a real countdown running.
    router.CANCEL_WINDOW = 30.0
    try:
        router._arm_send("Zohar Levin", "test", "hebrew")
        r = router.handle(j, "no no I was talking to the cat", speak=False,
                          activation="wake")
        check("background talk leaves the countdown running",
              router.PENDING is not None and r["did"] != "cancelled",
              f"did={r['did']} pending={router.PENDING is not None}")
        r = router.handle(j, "ביטול", speak=False, activation="wake")
        check("and she can still stop it herself",
              r["did"] == "cancelled" and router.PENDING is None,
              f"did={r['did']}")
    finally:
        router._cancel_pending()
        router.CANCEL_WINDOW = 6.0


def t_dates_are_worked_out_not_guessed(j):
    """Filling a date field needs no writing model at all: code lists every date in the
    next month with its weekday, and Jev points at the right row. That is arithmetic a
    language model gets wrong often enough to matter, and it is free here."""
    import datetime
    from savta.actions import web
    today = datetime.date.today()

    def ask(phrase, field_type=""):
        el = {"id": 1, "kind": "type", "type": field_type, "name": "", "label": "Date",
              "value": ""}
        return web.field_value(j, None, f"Book a table {phrase}.", el, "a page")

    def as_date(text):
        """Either shape is correct; which one depends on the field it is going into."""
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    for phrase, want in (("tomorrow", today + datetime.timedelta(days=1)),
                         ("the day after tomorrow", today + datetime.timedelta(days=2)),
                         ("in a week", today + datetime.timedelta(days=7)),
                         ("in two weeks", today + datetime.timedelta(days=14))):
        got = ask(phrase)
        check(f"{phrase!r} resolves to the right date", bool(got), "nothing returned")
        d = as_date(got) if got else None
        check(f"{phrase!r} is {want}", d == want, f"got {got!r}")

    # A named weekday must land on that weekday, whatever the date turns out to be.
    for phrase, weekday in (("next Sunday", "Sunday"), ("next Friday", "Friday")):
        d = as_date(ask(phrase) or "")
        check(f"{phrase!r} lands on a {weekday}",
              d is not None and d.strftime("%A") == weekday, str(d))
        check(f"{phrase!r} is in the future", d is not None and d > today, str(d))

    # The candidate list itself must be sane: every row a real, parseable, future date.
    # A native <input type="date"> takes only YYYY-MM-DD and silently discards
    # anything else; a plain text box wants what a person would actually type.
    iso = ask("tomorrow", "date")
    check("a real date input gets ISO format",
          bool(iso) and as_date(iso) == today + datetime.timedelta(days=1)
          and iso[4] == "-", repr(iso))
    plain = ask("tomorrow", "text")
    check("a text box gets the everyday format",
          bool(plain) and as_date(plain) == today + datetime.timedelta(days=1)
          and "/" in plain, repr(plain))

    rows = web.date_candidates(30)
    check("a month of candidates is offered", 25 <= len(rows) <= 40, str(len(rows)))
    bad = []
    for value, desc in rows:
        try:
            d = as_date(value)
            if d is None or d < today or not desc:
                bad.append(value)
        except ValueError:
            bad.append(value)
    check("every candidate is a real date, today or later", not bad, str(bad[:4]))
    check("the first one is today",
          rows[0][0] == today.strftime("%d/%m/%Y"), rows[0][0])


def t_the_app_loop_actually_drives(j):
    """The loop itself, against a window made up for the purpose.

    A locked Mac hides every real window from the accessibility tree, so the live read
    cannot be exercised here. What CAN be exercised is everything after it: that Jev
    picks a sensible control from what code offers, that the action is dispatched, that
    a changed window ends the run, and that a refusal stops it dead.
    """
    from savta.actions import apps

    pressed = []

    def fake_snapshot(app_name, max_elements=120):
        # A plain dialog: two buttons and a text field. `done` flips once Save is hit.
        val = "typed" if "typed" in pressed else ""
        return {
            "app": app_name, "title": "Saved" if "Save" in pressed else "Untitled",
            "text": "Saved" if "Save" in pressed else "a document that is not saved",
            "elements": [
                {"id": 1, "role": "AXTextField", "kind": "type", "label": "Title",
                 "value": val, "secure": False},
                {"id": 2, "role": "AXButton", "kind": "click", "label": "Save",
                 "value": "", "secure": False},
                {"id": 3, "role": "AXButton", "kind": "click", "label": "Delete",
                 "value": "", "secure": False},
            ],
            "refs": {1: "ref1", 2: "ref2", 3: "ref3"},
        }

    def fake_act(snap, el, op, text=""):
        if apps.is_forbidden(el) or apps.is_destructive(el):
            return apps.act({"refs": {}}, el, op, text)   # the real refusal path
        pressed.append("typed" if op == "type" else el["label"])
        return True, "ok"

    real_snap, real_act, real_avail = apps.snapshot, apps.act, apps.available
    apps.snapshot, apps.act = fake_snapshot, fake_act
    apps.available = lambda: (True, "ready")
    try:
        r = apps.run(j, None, "Save this document, call it Report", "TextEdit",
                     max_steps=6, time_budget=30)
        check("the loop drives a window to completion",
              r["did"] in ("done", "blocked") and "Save" in pressed,
              f"did={r['did']} pressed={pressed}")
        check("and it never pressed Delete", "Delete" not in pressed, str(pressed))
        check("every step is recorded", bool(r["steps"]), str(r["steps"]))

        # Now a goal that can only be satisfied destructively: it must refuse and stop.
        pressed.clear()
        r = apps.run(j, None, "Delete this document", "TextEdit",
                     max_steps=4, time_budget=20)
        check("a destructive goal is never carried out",
              "Delete" not in pressed, str(pressed))
        check("and it says so rather than going quiet",
              bool(r.get("did")) and r["did"] != "done", f"did={r['did']}")
    finally:
        apps.snapshot, apps.act, apps.available = real_snap, real_act, real_avail


@with_gates_on
def t_a_locked_screen_is_explained(j):
    """She can still be heard while the Mac is locked, so asking for a film has to
    produce a sentence rather than a dark screen and silence."""
    from savta.actions import mac as _mac
    real_locked = _mac.screen_locked
    _mac.screen_locked = lambda: True
    reset_state()
    router.AWAITING = None; router.LAST_EMERGENCY = None
    try:
        for utt in ("תשימי לי שיר של אייל גולן", "open the calculator"):
            router.PENDING = None
            r = router.handle(j, utt, speak=False)
            check(f"{utt[:30]!r} explains the locked screen",
                  r["did"] == "screen_locked", f"did={r['did']}")
            check(f"{utt[:30]!r} also says what it CAN do",
                  len(r.get("say") or "") > 40, short(r.get("say")))
        # ...and the things that need no screen still work.
        for utt, want in (("מה השעה", "answered"), ("מה מזג האוויר בחיפה", "answered")):
            router.PENDING = None
            r = router.handle(j, utt, speak=False)
            check(f"{utt!r} still works with the screen locked", r["did"] == want,
                  f"did={r['did']}")
    finally:
        _mac.screen_locked = real_locked


@with_gates_on
def t_an_in_app_task_is_routed_there(j):
    """"Press the blue button" used to be answered with "I cannot change that" — it
    read as a machine control like the volume, which it is not."""
    reset_state()
    router.AWAITING = None; router.LAST_EMERGENCY = None
    for utt in ("tick the box in the settings window",
                "תלחצי על הכפתור הכחול בתוכנה"):
        router.PENDING = None
        r = router.handle(j, utt, speak=False)
        check(f"{utt[:34]!r} is treated as an in-app task",
              r["did"] in ("app_done", "app_blocked", "app_needs_her",
                           "cannot_reach_app"), f"did={r['did']}")
        check(f"{utt[:34]!r} gets a spoken answer either way", bool(r.get("say")))
    # Naming the application does NOT make it a request to open it. "In the
    # calculator, press five" reads as open_app at about 0.72, while a genuine "open
    # the calculator" reads 1.00 — so the in-app question is what decides, not the
    # intent. Getting this wrong opened the app and did nothing in it.
    for utt in ("במחשבון תלחצי על חמש", "in the calculator press five"):
        router.PENDING = None
        r = router.handle(j, utt, speak=False)
        check(f"{utt[:30]!r} works INSIDE the app, not just opens it",
              r["did"] in ("app_done", "app_blocked", "app_needs_her",
                           "cannot_reach_app"), f"did={r['did']}")
    for utt in ("תפתחי את המחשבון", "open the calculator"):
        router.PENDING = None
        r = router.handle(j, utt, speak=False)
        check(f"{utt[:30]!r} still just opens it",
              r["did"] in ("opened_app", "app_failed"), f"did={r['did']}")

    # ...while a real machine control still goes to the machine.
    router.PENDING = None
    r = router.handle(j, "תגבירי", speak=False)
    check("'louder' is still the volume, not an app task", r["did"] == "louder",
          f"did={r['did']}")
    router.PENDING = None
    r = router.handle(j, "תפתחי את המחשבון", speak=False)
    check("'open the calculator' still just opens it",
          r["did"] in ("opened_app", "app_failed"), f"did={r['did']}")


def t_place_names_survive(j):
    """Hebrew glues "in" onto a place name, and several real towns simply begin with
    the same letter. Blind stripping stored "באר שבע" as "אר שבע"."""
    for said, want in (("בחיפה", "חיפה"), ("בתל אביב", "תל אביב"), ("בירושלים", "ירושלים"),
                       ("באר שבע", "באר שבע"), ("בת ים", "בת ים"), ("מודיעין", "מודיעין"),
                       ("בני ברק", "בני ברק"), ("לוד", "לוד")):
        got = router.unprefixed_place(j, said)
        check(f"{said!r} is heard as {want!r}", got == want, f"got {got!r}")
    line = router._weather_line({"where": "x", "desc": "Clear", "code": 113, "temp": 20,
                                 "feels": 20, "low": 18, "high": 24, "rain_pct": 0},
                                "באר שבע", "hebrew")
    check("and a town beginning with ב still gets its preposition",
          line.startswith("בבאר שבע"), line)


def t_nothing_says_alright_after_a_failure(j):
    """"בסדר" spoken after something did not happen is the worst kind of lie: she has
    no screen she can read to find out otherwise."""
    from savta.actions import mac as _mac
    reset_state()
    real_open = _mac.open_app
    _mac.open_app = lambda a: (False, "timed out waiting for permission")
    try:
        r = router.handle(j, "show me my photos", speak=False)
        check("Photos failing to open is said out loud",
              r["did"] == "app_failed", f"did={r['did']}")
        check("and it does not say Alright", "בסדר" not in (r.get("say") or ""),
              short(r.get("say")))
    finally:
        _mac.open_app = real_open

    reset_state()
    router.MEM = router.Memory()
    router._LAST_SPOKEN.clear()
    r = router.handle(j, "תגידי את זה שוב", speak=False)
    check("'say that again' with nothing to repeat says so",
          r["did"] == "nothing_to_repeat", f"did={r['did']} say={short(r.get('say'))}")
    check("and does not claim it repeated something",
          "בסדר" not in (r.get("say") or ""), short(r.get("say")))


def t_a_stale_question_is_forgotten(j):
    """An unanswered question used to sit there forever and swallow an unrelated
    sentence hours later, turning it into the body of a message."""
    reset_state()
    router.AWAITING = {"at": time.time() - (router.AWAITING_TTL + 5),
                       "need": "what", "question": "what should the message say",
                       "contact": "Zohar Levin", "lang": "hebrew", "body": None}
    r = router.handle(j, "תשימי לי שיר של אום כולתום", speak=False)
    check("a question from long ago no longer captures a new request",
          r["did"] != "sending" and not SENT and not WA, f"did={r['did']} {SENT}{WA}")
    check("and the stale question is dropped", router.AWAITING is None, str(router.AWAITING))

    # a fresh one still works
    reset_state()
    router.AWAITING = {"at": time.time(), "need": "what",
                       "question": "what should the message say",
                       "contact": "Zohar Levin", "lang": "hebrew", "body": None,
                       "channel": "whatsapp"}
    r = router.handle(j, "שאני מרגישה הרבה יותר טוב היום", speak=False)
    check("but a question asked a moment ago is still answered",
          r["did"] in ("sending", "send_disabled"), f"did={r['did']}")
    router._cancel_pending()
    router.AWAITING = None


def t_names_are_pronounceable(j):
    """A bound Hebrew or Arabic preposition glued to a Latin name is read by the
    synthesiser as one mangled token: "שלחתי לDavid"."""
    for lg, glued in (("hebrew", "לZohar"), ("arabic", "لـZohar")):
        spoken = router._sp(lg, "sent", who=router.for_speech("sent", "Zohar Levin", lg))
        check(f"{lg} 'sent' does not glue the preposition", glued not in spoken, spoken)
        narrated = router._narrate(router.display_name("Zohar Levin", lg), "שלום", lg)
        check(f"{lg} narration does not glue it either", glued not in narrated, narrated)
    check("a Hebrew name keeps the preposition attached, as Hebrew does",
          router._sp("hebrew", "sent", who=router.for_speech("sent", "אמא", "hebrew"))
          == "שלחתי לאמא.",
          router._sp("hebrew", "sent", who=router.for_speech("sent", "אמא", "hebrew")))


def t_a_follow_up_is_not_a_cut_off_sentence(j):
    """A short follow-up leaning on the turn before it - "and what about Italy" -
    was scored as someone trailing off mid-sentence and answered with "I only
    caught part of that", which is the multi-turn amnesia users actually feel.
    The fix is in what is_complete ASKS, not in the threshold it is compared to:
    a fragment that is whole given what just happened is a whole request."""
    whole = [("and what about Italy", "what is the capital of France -> answered"),
             ("the second one", "play me something by Fairuz -> played"),
             ("a bit louder", "play me something -> played")]
    for q, recent in whole:
        u = brain.understand(j, q, [], recent)
        check(f"{q!r} counts as a whole request after {recent.split(' ->')[0]!r}",
              u["is_complete"] >= 0.35, f"is_complete={u['is_complete']:.2f}")

    # The signal still has to do its original job, or every half-sentence gets acted on.
    cut = [("play me something by", ""), ("תשלחי הודעה ל", ""),
           ("and I wanted to also say that", "תשלחי הודעה לזוהר -> waiting")]
    for q, recent in cut:
        u = brain.understand(j, q, [], recent)
        check(f"{q!r} is still recognised as cut off",
              u["is_complete"] < 0.35, f"is_complete={u['is_complete']:.2f}")



def t_a_follow_up_question_keeps_its_subject(j):
    """Scoring the follow-up as complete only got it as far as the knowledge call,
    which was handed the bare words "and what about Italy" and answered about Italy
    in general. The conversation has to travel with the question."""
    if not router.LLM_CLIENT.available:
        check("(skipped: no Gemini key)", True, "")
        return
    before = "what is the capital of France -> answered"
    with_ctx = router.LLM_CLIENT.answer("and what about Italy", "english",
                                        asked_before=before) or ""
    check("a follow-up resolves against what was asked before",
          "rome" in with_ctx.lower(), with_ctx[:120])

    without = router.LLM_CLIENT.answer("and what about Italy", "english") or ""
    check("and the context is what makes the difference, not luck",
          "rome" not in without.lower(), without[:120])



TESTS = [
    ("0. a switched-off gate refuses out loud", t_gates_refuse_loudly),
    ("0b. answering a call offer never sends a message", t_answer_to_a_call_is_not_a_message),
    ("0c. a call is announced only if it happened", t_a_call_is_only_announced_if_it_happened),
    ("0d. an unknown pending question fails closed", t_unknown_question_fails_closed),
    ("0e. real place names survive being heard", t_place_names_survive),
    ("0o. inside other Mac applications", t_inside_other_apps),
    ("0p. an in-app task is routed there", t_an_in_app_task_is_routed_there),
    ("0q. the app loop actually drives a window", t_the_app_loop_actually_drives),
    ("0s. dates are worked out, not guessed", t_dates_are_worked_out_not_guessed),
    ("0t. the room cannot cancel her message", t_the_room_cannot_cancel_her_message),
    ("0r. a locked screen is explained, not silent", t_a_locked_screen_is_explained),
    ("0l. a long answer can be interrupted", t_a_long_answer_can_be_interrupted),
    ("0n. it admits it cannot skip inside a video", t_it_admits_it_cannot_skip),
    ("0k. the address book can never hang a request",
     t_the_address_book_can_never_hang_a_request),
    ("0i. every line speaks four languages", t_every_line_speaks_four_languages),
    ("0j. the emergency contact can be set by voice", t_emergency_contact_by_voice),
    ("0f. nothing says Alright after a failure", t_nothing_says_alright_after_a_failure),
    ("0g. a stale question is forgotten", t_a_stale_question_is_forgotten),
    ("0h. names stay pronounceable", t_names_are_pronounceable),
    ("0i. a follow-up is not a cut-off sentence", t_a_follow_up_is_not_a_cut_off_sentence),
    ("0j. a follow-up question keeps its subject", t_a_follow_up_question_keeps_its_subject),
    ("0t. closing a program", t_close_app),
    ("0u. swapping the song", t_music_swaps),
    ("0v. undo", t_undo),
    ("0w. volume words with real context", t_volume_words_with_context),
    ("0x. what is on the screen", t_screen),
    ("1. safety: nothing leaves the machine", t_safety),
    ("2. the six second cancel window", t_cancel_window),
    ("3. multi-turn slot filling", t_multi_turn),
    ("4. onboarding from an empty machine", t_onboarding),
    ("5. contact matching across scripts", t_contact_matching),
    ("6. the escape hatch", t_escape_hatch),
    ("7. language routing", t_language_routing),
    ("8. a whole message, one breath", t_message_end_to_end),
    ("9. no delete path exists", t_no_delete_path),
    ("10. a call is dialled, not just announced", t_calling),
    ("11. the browser agent stops at money and secrets", t_web_safety),
    ("12. a message about money gets more time", t_scam_guard),
    ("13. a cry for help outranks everything else", t_emergency),
    ("14. it decides when NOT to speak", t_interrupt_gate),
    ("15. the volume knob, not a different song", t_volume_vs_content),
    ("16. a false alarm can be called off", t_emergency_undo),
    ("17. the clock is on this machine", t_clock),
    ("18. the weather, in her town and her language", t_weather),
    ("19. the script she spoke in decides the language", t_language_from_script),
]


def main() -> int:
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    try:
        j = Jev()
        j.warmup()
    except Exception as e:  # noqa: BLE001
        print(f"cannot reach Jev: {e!r}\n"
              f"(TYPESAFE_API_KEY must be in the environment or in "
              f"{ROOT.parent / '.env.local'})")
        return 2

    t_all = time.time()
    print(f"MicMic regression suite   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"sends are gated off (MICMIC_ALLOW_SEND unset), osascript is blocked\n")

    for title, fn in TESTS:
        if only and not any(o in title for o in only):
            continue
        print(title)
        try:
            fn(j)
        except Exception:  # noqa: BLE001
            FAILED.append((title, "raised: " + traceback.format_exc(limit=6)))
            print(f"  FAIL  {title} raised an exception")
            print("          " + traceback.format_exc(limit=6).replace("\n", "\n          "))
        reset_state()
        print()

    dt = time.time() - t_all
    print("-" * 78)
    if FAILED:
        print(f"{len(FAILED)} FAILURE(S):")
        for name, detail in FAILED:
            print(f"  - {name}\n      {detail}")
    print(f"{PASSED} passed, {len(FAILED)} failed   "
          f"{j.calls} jev calls  ${j.cost_usd:.5f}  {dt:.1f}s")
    print(f"never sent for real: {len(OSA_TOTAL)} osascript calls escaped the stub "
          f"(must be 0); {len(DELIBERATE)} deliberate probes of the real functions; "
          f"{len(LAUNCHED)} launcher calls, all blocked")
    return 1 if (FAILED or OSA_TOTAL) else 0


if __name__ == "__main__":
    code = main()
    _restore_profile()
    sys.exit(code)
