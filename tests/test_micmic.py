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

The one real network call is Jev, plus a couple of Gemini calls; by default (see
tests/replay.py) neither happens at all. The suite runs against a recording made
once and replayed from tests/recordings/test_micmic.jsonl, so a normal run costs
$0 and makes 0 live calls. See tests/replay.py for MICMIC_REPLAY=record/replay/off
and how to re-record after a prompt or test question changes.
"""
from __future__ import annotations

import os
import sys

# Pinned before anything else runs, and only by restarting: PYTHONHASHSEED only takes
# effect at interpreter start-up, so setting it after import would do nothing. With a
# random seed, two `MICMIC_REPLAY=replay` runs against the same recording did not
# agree with each other; with this pinned, they do, byte for byte, every time (see
# tests/README.md). Contact-matching builds a `set` of name variants along the way,
# and a `set`'s iteration order is exactly this seed, so it is the first suspect,
# though pinning it did not close every gap between a live run and a replayed one
# (also in the README) - it is kept because it is a real, verified source of drift
# in its own right, not because it is the whole story.
if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable] + sys.argv)

import atexit
import json
import re
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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

# The other real network call, patched before anything in this file calls Jev.ask()
# or LLM.text()/answer()/chat(): see tests/replay.py.
import replay                                  # noqa: E402
replay.install()

# The daily briefing rides its own weather lookup and message-store read on a
# background thread (router._in_parallel) that the turn which triggered it still
# waits on, so it is not a leak past that one turn - but it fires at most once per
# process, on whichever call happens to be the first with a fresh "never briefed
# today" profile, which under this suite is effectively "whichever test runs
# first". That is exactly the kind of question a recording cannot pin down: which
# test that is depends on nothing this suite controls. Off by default, exactly as
# scripted_turns already turns it off for its own scope; the two sections that
# mean to exercise a real briefing (t_one_briefing_even_when_turns_fail,
# t_weather_never_from_a_model) put the real one back for their own duration.
REAL_DUE_BRIEFING = router.due_briefing
router.due_briefing = lambda: False

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
                "feels": 16, "low": 14, "high": 19, "rain_pct": 70,
                "lat": 32.8, "lon": 35.0,
                "days": [{"date": "d0", "code": 296, "low": 14, "high": 19, "rain_pct": 70},
                         {"date": "d1", "code": 113, "low": 21, "high": 28, "rain_pct": 5},
                         {"date": "d2", "code": 119, "low": 12, "high": 15, "rain_pct": 20}]}
facts.conditions = lambda place="": (WEATHER_ASKED.append(place), dict(FAKE_WEATHER))[1]
# Places: every name is a real place at the fake weather's coordinates, unless a
# section says otherwise.
facts.geocode = lambda name, lang="en": (WEATHER_ASKED.append(name), [
    {"name": name, "country": "", "country_code": "IL", "admin1": "", "lat": 32.8,
     "lon": 35.0, "population": 1000}])[1]
facts.conditions_at = lambda lat, lon: dict(FAKE_WEATHER)
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
REAL_UNREAD = book.unread_summary          # kept for the store-lock test; stubbed below
REAL_READ_RECENT = book._read_recent
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
SKIPPED = 0
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


def check_latency(name: str, ok, detail: str = "") -> bool:
    """A pure Jev/Gemini round-trip time assertion. Under MICMIC_REPLAY=replay
    (the default) the call is a dict lookup, not a network round trip, so the
    budget would always pass without measuring anything real; skip it instead of
    banking a free pass. Real network calls (record/off) still check it."""
    global SKIPPED
    if replay.skip_latency():
        SKIPPED += 1
        print(f"  SKIP  {name} (replay mode: no live round trip to time)")
        return True
    return check(name, ok, detail)


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


# ---------------------------------------------------------------- scripted turns
# The follow-up tests below are about what the ROUTER keeps between turns, not about
# how well Jev reads a sentence, so every model answer in them is scripted: they cost
# no calls and come out the same on every run. The understanding each turn gets is the
# one Jev really gave for that sentence (measured 2026-09-26, the values in comments),
# so a script cannot quietly drift into something Jev never says.
class _ScriptedJev:
    """Answers the router's own follow-up questions from a table keyed by utterance."""

    def __init__(self):
        self.calls, self.cost_usd, self.busy_ms = 0, 0.0, 0.0
        self.asked: list[str] = []
        self.same: dict = {}      # utterance -> same_person
        self.spans: dict = {}     # utterance -> the words pick_span should choose
        self.cancel: dict = {}    # utterance -> (stop_it, says_what_instead)
        self.answer: dict = {}    # utterance -> (is_answer, restates_request)
        self.yes: dict = {}       # utterance -> ("yes"|"no"|"neither", confidence)
        self.named: dict = {}     # utterance -> the contact her answer to "who?" names
        # pick_result: what she asked for -> a piece of the title Jev chooses. None
        # leaves pick_result refusing everything, as it always did here.
        self.best: dict | None = None

    def ask(self, state, qs):
        self.calls += 1
        utt = (state.get("utterance") or state.get("she_said")
               or state.get("her_answer") or "")
        out = {}
        for k, q in qs.items():
            self.asked.append(k)
            if self.best is not None and "she_asked_for" in state and k in ("best",
                                                                            "any_good"):
                want = self.best.get(state["she_asked_for"], "")
                pick = next((i for i, t in q.get("criteria", {}).items()
                             if want and want in t), "0")
                out[k] = ({"choice": pick, "confidence": 0.9, "probabilities": {pick: 0.9}}
                          if k == "best" else {"noul": 0.9})
            elif k == "same_person":
                out[k] = {"noul": self.same.get(utt, 0.05)}
            elif k in ("stop_it", "says_what_instead"):
                out[k] = {"noul": self.cancel.get(utt, (0.05, 0.05))[k != "stop_it"]}
            elif k == "span":
                out[k] = {"choice": self.spans.get(utt) or "__none__", "confidence": 0.9,
                          "probabilities": {}}
            elif k == "exists":
                out[k] = {"noul": 0.9 if self.spans.get(utt) else 0.1}
            elif k == "contact" and utt in self.named:
                out[k] = {"choice": self.named[utt], "confidence": 0.9, "probabilities": {}}
            elif k == "yes_no":
                pick, conf = self.yes.get(utt, ("neither", 0.9))
                out[k] = {"choice": pick, "confidence": conf, "probabilities": {pick: conf}}
            elif k in ("is_answer", "restates_request"):
                out[k] = {"noul": self.answer.get(utt, (0.1, 0.1))[k == "restates_request"]}
            elif q["type"] == "choice":
                opts = list(q["criteria"])
                pick = "nobody" if "nobody" in opts else opts[0]
                out[k] = {"choice": pick, "confidence": 0.9, "probabilities": {pick: 0.9}}
            elif q["type"] == "noul":
                out[k] = {"noul": 0.0}
            else:
                out[k] = {"score": 0.0}
        return out


class _ScriptedLLM:
    """Writes "in French" as a visible marker, so a test can see it happened once."""
    available = True
    calls, busy_ms, last_ms, last_error = 0, 0.0, 0.0, None

    def __init__(self):
        self.answered: list[dict] = []

    def text(self, prompt, system="", **kw):
        self.calls += 1
        return "[fr] " + prompt.split("The message: ", 1)[1]

    def answer(self, question, language="hebrew", context="", gender="", asked_before=""):
        self.calls += 1
        self.answered.append({"question": question, "context": context,
                              "asked_before": asked_before})
        return "[answer]"

    def split_steps(self, *a, **k):
        return None


_U_KEYS = ("intent_confidence", "wants_full_length", "names_title", "contact_confidence",
           "contact_named", "has_message_content", "money_involved", "sounds_coached",
           "control_confidence", "wants_recent", "asking_for_notes", "is_complete",
           "noise", "refers_back", "is_compound", "needs_knowledge", "about_weather",
           "about_clock", "setting_emergency_contact", "inside_an_app",
           "speaker_gender_confidence", "rejects_last", "describes_instead",
           "refers_to_screen", "distress", "emergency", "wants_undo", "amends_message")


def _u(intent="message", **kw) -> dict:
    u = {k: 0.0 for k in _U_KEYS}
    u.update({"raw": {}, "spans": {}, "intent": intent, "intent_probs": {intent: 0.9},
              "media_kind": "not_applicable", "contact": "nobody",
              "control_action": "not_applicable", "player_action": "not_applicable",
              "channel": "imessage", "file_kind": "any", "when_minutes": "none",
              "language": "english", "speaker_gender": "unrevealed",
              "screen_task": "not_applicable", "weather_day": "today",
              "write_in": "not_applicable", "message_app": "unchanged",
              "intent_confidence": 0.95, "is_complete": 0.95, "contact_confidence": 0.9})
    u.update(kw)
    return u


class scripted_turns:
    """understand(), Jev and the language model all scripted for one section."""

    def __init__(self, understood: dict):
        self.understood = understood
        self.drafts: list = []          # what understand() was told about the draft
        self.spans: list = []           # which span questions rode along, per turn

    def __enter__(self):
        self.prev = (router.understand, router.LLM_CLIENT, router.get_contacts,
                     router.due_briefing, router.CANCEL_WINDOW)

        def understand(j, utt, contacts, recent="", playing="", likes=None,
                       spans=None, draft=None):
            self.drafts.append(dict(draft) if draft else None)
            self.spans.append(sorted(spans or {}))
            return _u(**self.understood[utt])
        router.understand = understand
        router.LLM_CLIENT = self.llm = _ScriptedLLM()
        router.get_contacts = lambda *a, **k: ["Zohar Levin", "Gal Ben Ami", "Nir Cohen",
                                               "Dana", "Matan"]
        router.due_briefing = lambda: False
        router.CANCEL_WINDOW = 30.0     # nothing fires underneath; the test fires it
        self.j = _ScriptedJev()
        return self

    def __exit__(self, *exc):
        router._cancel_pending()
        (router.understand, router.LLM_CLIENT, router.get_contacts,
         router.due_briefing, router.CANCEL_WINDOW) = self.prev
        return False


def _d(r) -> dict:
    return r.get("detail") if isinstance(r.get("detail"), dict) else {}


def _answer(s, utt: str, yes: str = "yes", conf: float = 0.9) -> dict:
    """Her answer to "send it?", with Jev's reading of it scripted."""
    s.j.answer[utt] = (0.95, 0.05)
    s.j.yes[utt] = (yes, conf)
    return router.handle(s.j, utt, speak=False)


def _instead(span: str | None, score: float) -> dict:
    """The "instead" span, as understand() hands it back while something plays."""
    return {"span_instead": {"choice": span or "__none__", "confidence": 0.9,
                             "probabilities": {}},
            "span_instead_exists": {"noul": score}}


@with_gates_on
def t_follow_ups_over_a_playing_film(j):
    """The owner, on 1.0.2: a film by an actor was playing. Naming the film she meant
    (misheard, then said again, then shortened) played the next result of the actor
    search three times, and three questions about him were each told "that is everything
    I found". A question is answered; a named title is a new search for her own words;
    only "another one" goes down the old list. Names and titles here are stand-ins; the
    readings are Jev's for the owner's sentences (the trace, and the calibration run in
    router.NAMES_INSTEAD_GATE)."""
    hanks = [
        {"id": "h1", "title": "Big (1988) Tom Hanks Full Movie", "channel": "Old Films",
         "length": "1:44:00", "views": "500K views"},
        {"id": "h2", "title": "The Burbs Full Movie", "channel": "Old Films",
         "length": "1:41:00", "views": "300K views"},
        {"id": "h3", "title": "Splash 1984 full movie", "channel": "Films",
         "length": "1:51:00", "views": "200K views"},
    ]
    gump = [
        {"id": "g1", "title": "Forrest Gump (1994) Full Movie HD", "channel": "Cinema",
         "length": "2:22:00", "views": "4M views"},
        {"id": "g2", "title": "Forrest Gump - Official Trailer", "channel": "Paramount Movies",
         "length": "2:10", "views": "9M views"},
        {"id": "g3", "title": "Forrest Gump best scenes", "channel": "Clips",
         "length": "12:00", "views": "1M views"},
    ]
    he_film = [
        {"id": "t1", "title": "טיטאניק סרט מלא", "channel": "סרטים", "length": "3:14:00",
         "views": "1M views"},
        {"id": "t2", "title": "Titanic 1997 full movie", "channel": "Films",
         "length": "3:14:00", "views": "2M views"},
    ]

    def search(q, n=18):
        YT_QUERIES.append(q)
        ql = q.lower()
        rows = (gump if ("forest" in ql or "forrest" in ql)
                else he_film if "טיטאניק" in q else hanks)
        return [dict(v) for v in rows]

    first, first_he = "find me a movie of tom hanks", "תמצאי לי סרט של טום הנקס"
    garbled, again_named, short_named = ("i was thinking like a forest gum",
                                         "no i meant the forrest gump or something like that",
                                         "no forrest")
    q_famous, q_list, q_he = ("tell me what was his most famous film",
                              "can you give me a list of his movies",
                              "מה הסרט הכי מפורסם שלו")
    film = dict(intent="watch", media_kind="feature_film", wants_full_length=0.2)
    understood = {
        first: dict(film, spans={"subject": ("tom hanks", 0.95)}),
        first_he: dict(film, spans={"subject": ("טום הנקס", 0.95)}),
        # The owner's corrections: chitchat 0.28 / watch 0.77 / stop 0.51, each with
        # rejects_last well over its gate.
        garbled: dict(intent="chitchat", intent_confidence=0.28, is_complete=0.55,
                      rejects_last=0.51, refers_back=0.66, needs_knowledge=0.5,
                      raw=_instead("forest gum", 0.48)),
        again_named: dict(intent="watch", intent_confidence=0.77, rejects_last=0.79,
                          refers_back=0.73, needs_knowledge=0.59, media_kind="feature_film",
                          raw=_instead("forrest gump", 0.90)),
        short_named: dict(intent="stop", intent_confidence=0.51, is_complete=0.68,
                          rejects_last=0.67, refers_back=0.77, raw=_instead("forrest", 0.53)),
        # The owner's questions: look_up 0.83-1.0 with rejects_last 0.34-0.75.
        q_famous: dict(intent="look_up", intent_confidence=1.0, rejects_last=0.50,
                       refers_back=0.93, needs_knowledge=0.95,
                       spans={"term": ("his", 0.9)}, raw=_instead(None, 0.06)),
        q_list: dict(intent="look_up", intent_confidence=0.83, rejects_last=0.75,
                     refers_back=0.37, needs_knowledge=0.86, raw=_instead(None, 0.06)),
        q_he: dict(intent="look_up", intent_confidence=0.98, rejects_last=0.62,
                   refers_back=0.94, needs_knowledge=0.93, language="hebrew",
                   spans={"term": ("שלו", 0.9)}, raw=_instead(None, 0.06)),
        "another one": dict(intent="again", intent_confidence=0.69, rejects_last=0.89,
                            refers_back=0.88, raw=_instead(None, 0.06)),
        "no": dict(intent="stop", intent_confidence=0.74, rejects_last=0.64,
                   refers_back=0.81, raw=_instead(None, 0.07)),
        "stop": dict(intent="stop", intent_confidence=0.91, rejects_last=0.27,
                     refers_back=0.62, raw=_instead(None, 0.04)),
        "לא, התכוונתי לטיטאניק": dict(intent="watch", intent_confidence=0.79,
                                      rejects_last=0.95, language="hebrew",
                                      media_kind="feature_film",
                                      raw=_instead("לטיטאניק", 0.96)),
    }
    real_search, real_wiki = yt.search, facts.wiki
    wiki_terms: list[str] = []
    yt.search = search
    facts.wiki = lambda term, lang="en": (wiki_terms.append(term), "")[1]
    try:
        # ---- the owner's sequence -------------------------------------------------
        reset_state()
        with scripted_turns(understood) as s:
            s.j.best = {"tom hanks, as a feature film": "Big (1988)",
                        "forest gum, as a feature film": "Forrest Gump (1994)",
                        "forrest gump, as a feature film": "Forrest Gump (1994)",
                        "forrest, as a feature film": "Forrest Gump (1994)"}
            r = router.handle(s.j, first, speak=False)
            check("a film by an actor is playing to start with",
                  r["did"] == "playing" and _d(r).get("video_id") == "h1",
                  f"did={r['did']} detail={short(_d(r))}")
            check("nothing extra is asked while nothing plays",
                  "instead" not in s.spans[-1], str(s.spans[-1]))

            # Each from the actor's film, as each of hers followed a wrong one.
            for utt, words in ((garbled, "forest gum"), (again_named, "forrest gump"),
                               (short_named, "forrest")):
                router.MEM = router.Memory()
                router.handle(s.j, first, speak=False)
                YT_QUERIES.clear(); CLOSED.clear()
                r = router.handle(s.j, utt, speak=False)
                check(f"{utt!r}: the name rode along in the same understanding",
                      "instead" in s.spans[-1], str(s.spans[-1]))
                check(f"{utt!r}: a new search for her own words, not the next result",
                      r["did"] == "playing" and YT_QUERIES
                      and YT_QUERIES[0].startswith(words)
                      and not _d(r).get("after_rejection") and _d(r).get("corrected"),
                      f"did={r['did']} queries={YT_QUERIES} detail={short(_d(r))}")
                check(f"{utt!r}: Jev picks the real film from the new results, which "
                      f"replaces the one playing",
                      _d(r).get("video_id") == "g1" and CLOSED == [1],
                      f"picked={_d(r).get('title')} closed={CLOSED}")
            check("the search she follows up on is the corrected one",
                  router.MEM.play_query == "forrest", repr(router.MEM.play_query))

            dids = []
            for utt in (q_famous, q_list):
                YT_QUERIES.clear()
                n = len(s.llm.answered)
                r = router.handle(s.j, utt, speak=False)
                dids.append(r["did"])
                check(f"{utt!r}: a question over the film is answered",
                      r["did"] == "answered" and r.get("say") == "[answer]"
                      and len(s.llm.answered) == n + 1 and not YT_QUERIES,
                      f"did={r['did']} say={short(r.get('say'))} queries={YT_QUERIES}")
                check(f"{utt!r}: the answer is told what she asked for and what is playing",
                      "forrest" in s.llm.answered[-1]["asked_before"]
                      and "Forrest Gump (1994)" in s.llm.answered[-1]["asked_before"],
                      short(s.llm.answered[-1]))
            check("'his' is grounded in what she asked to see, not the word 'his'",
                  wiki_terms and wiki_terms[0] == "forrest" and "his" not in wiki_terms,
                  str(wiki_terms))
            check("a question never ends in 'that is everything I found'",
                  "exhausted" not in dids, str(dids))

            YT_QUERIES.clear(); CLOSED.clear()
            r = router.handle(s.j, "another one", speak=False)
            check("'another one' still continues the same list",
                  r["did"] == "playing" and _d(r).get("after_rejection")
                  and YT_QUERIES and YT_QUERIES[0].startswith("forrest")
                  and _d(r).get("video_id") != "g1",
                  f"did={r['did']} queries={YT_QUERIES} detail={short(_d(r))}")

            YT_QUERIES.clear()
            r = router.handle(s.j, "no", speak=False)
            check("a bare 'no' still continues the list, as before",
                  r["did"] in ("playing", "exhausted") and not _d(r).get("corrected"),
                  f"did={r['did']} detail={short(_d(r))}")

            CLOSED.clear()
            r = router.handle(s.j, "stop", speak=False)
            check("a bare 'stop' still stops what is playing",
                  r["did"] == "stopped" and CLOSED == [1] and router.MEM.last_played is None,
                  f"did={r['did']} closed={CLOSED}")

        # ---- the question in Hebrew ----------------------------------------------
        reset_state(); wiki_terms.clear()
        with scripted_turns(understood) as s:
            s.j.best = {"טום הנקס, as a feature film": "Big (1988)"}
            router.handle(s.j, first_he, speak=False)
            r = router.handle(s.j, q_he, speak=False)
            check("he: 'what is his most famous film' is answered in Hebrew",
                  r["did"] == "answered" and r.get("lang") == "hebrew"
                  and s.llm.answered and "טום הנקס" in s.llm.answered[-1]["asked_before"],
                  f"did={r['did']} answered={short(s.llm.answered)}")
            check("he: grounded in the actor she asked for, not in 'שלו'",
                  wiki_terms == ["טום הנקס"], str(wiki_terms))

        # ---- "no, <title>" in Hebrew ----------------------------------------------
        reset_state()
        with scripted_turns(understood) as s:
            s.j.best = {"טום הנקס, as a feature film": "Big (1988)",
                        "לטיטאניק, as a feature film": "טיטאניק"}
            router.handle(s.j, first_he, speak=False)
            YT_QUERIES.clear()
            r = router.handle(s.j, "לא, התכוונתי לטיטאניק", speak=False)
            check("he: 'no, I meant <a title>' searches for the title",
                  r["did"] == "playing" and YT_QUERIES and "טיטאניק" in YT_QUERIES[0]
                  and _d(r).get("video_id") == "t1",
                  f"did={r['did']} queries={YT_QUERIES} detail={short(_d(r))}")

        # ---- a movie request may be the whole film --------------------------------
        check("a movie request keeps the whole-film uploads to choose from",
              [r["id"] for r in yt.screen_results(gump)] == ["g1", "g2", "g3"],
              str([r["id"] for r in yt.screen_results(gump)]))
        check("a trailer request still drops them",
              [r["id"] for r in yt.screen_results(gump + [dict(gump[1], id="g4")],
                                                  trailer=True)] == ["g2", "g4"])
        reset_state()
        with scripted_turns(understood) as s:
            s.j.best = {"tom hanks, as a feature film": "Big (1988)"}
            r = router.handle(s.j, first, speak=False)
            check("a movie request that did not say 'whole' may still play the whole film",
                  r["did"] == "playing" and "Full Movie" in (_d(r).get("title") or ""),
                  f"did={r['did']} title={_d(r).get('title')}")
    finally:
        yt.search, facts.wiki = real_search, real_wiki


@with_gates_on
def t_a_follow_up_keeps_the_message(j):
    """The owner, on 1.0.2: a message went out, "no wait, send it in French" asked who
    to send it to, then what it should say, and a one-word mishearing of the answer went
    out as the whole message; "send her on WhatsApp" then asked who and what all over
    again. A follow-up that changes one thing about a message keeps everything else.
    Words she did not say in that turn are confirmed with a yes before they go (3c)."""
    # ---- the owner's exact shape (names and words are stand-ins) ----------------
    first = "please send a message to Gal in French telling that I love her"
    fr, wa = "no wait send it in french", "please send her on whatsapp"
    understood = {
        # The first turn did not pick up "in French" (that is what happened); the
        # follow-up has to put it right.
        first: dict(contact="Gal Ben Ami", contact_named=0.95, has_message_content=0.95),
        # Jev's real reading of these two, with a message just prepared: it names the
        # person from context, calls the pronoun a named person (0.67, 0.53) and thinks
        # there is message content (0.69, 0.63). The old code checked "her" against the
        # contact's name, failed, and asked who.
        fr: dict(intent_confidence=0.74, contact="Gal Ben Ami", contact_named=0.67,
                 refers_back=0.83, has_message_content=0.69, amends_message=0.96,
                 write_in="french"),
        wa: dict(intent_confidence=0.97, contact="Gal Ben Ami", contact_named=0.53,
                 refers_back=0.90, has_message_content=0.63, channel="whatsapp",
                 amends_message=0.88, message_app="whatsapp"),
    }
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[first], s.j.same[first] = "I love her", 0.9
        r1 = router.handle(s.j, first, speak=False)
        check("owner shape: the first message is read back and armed",
              r1["did"] == "sending" and _d(r1).get("text") == "I love her",
              f"did={r1['did']} detail={short(_d(r1))}")
        router._fire_pending()                  # her countdown ran out: it went
        r2 = router.handle(s.j, fr, speak=False)
        check("owner shape: 'send it in French' does not ask who or what",
              r2["did"] == "confirm_send", f"did={r2['did']} say={short(r2.get('say'))}")
        aw = router.AWAITING or {}
        check("owner shape: same person, her words, now in French, awaiting her yes",
              aw.get("contact") == "Gal Ben Ami" and aw.get("body") == "[fr] I love her"
              and aw.get("channel") == "imessage" and aw.get("in_lang") == "french",
              short(aw))
        check("owner shape: the question names the language (S3)",
              "in French" in (r2.get("say") or "") and "[fr] I love her" in r2["say"],
              short(r2.get("say")))
        check("owner shape: understand() was told which message she meant",
              (s.drafts[-1] or {}).get("to") == "Gal Ben Ami"
              and (s.drafts[-1] or {}).get("text") == "I love her", short(s.drafts[-1]))
        r = _answer(s, "yes")
        check("owner shape: her yes reads it back, in French, and starts the countdown",
              r["did"] == "sending" and "in French" in (r.get("say") or "")
              and router.PENDING is not None, f"did={r['did']} say={short(r.get('say'))}")
        router._fire_pending()
        r3 = router.handle(s.j, wa, speak=False)
        aw = router.AWAITING or {}
        check("owner shape: 'send her on WhatsApp' does not ask who or what",
              r3["did"] == "confirm_send", f"did={r3['did']} say={short(r3.get('say'))}")
        check("owner shape: same person, same French words, on WhatsApp",
              aw.get("contact") == "Gal Ben Ami" and aw.get("body") == "[fr] I love her"
              and aw.get("channel") == "whatsapp" and aw.get("in_lang") == "french",
              short(aw))
        _answer(s, "yes")
        router._fire_pending()
        check("owner shape: what went out is exactly what was read back",
              SENT == [("Gal Ben Ami", "I love her"), ("Gal Ben Ami", "[fr] I love her")]
              and [t for _, t in WA] == ["[fr] I love her"], f"sent={SENT} wa={WA}")
        check("owner shape: translated once, not again for the app change",
              s.llm.calls == 1, f"llm calls={s.llm.calls}")
        check("owner shape: a pointed-back person is never checked as a spoken name",
              s.j.asked.count("same_person") == 1, str(s.j.asked))   # only the first turn

    # ---- a pronoun with no message kept: the same-name check alone ----------------
    reset_state()
    her = "send her a message"
    with scripted_turns({her: dict(contact="Gal Ben Ami", contact_named=0.6,
                                   refers_back=0.9)}) as s:
        router.MEM.contact = "Gal Ben Ami"
        r = router.handle(s.j, her, speak=False)
        check("'send her a message' after dealing with her asks what, not who",
              r["did"] == "need_what" and (router.AWAITING or {}).get("contact") == "Gal Ben Ami",
              f"did={r['did']} awaiting={short(router.AWAITING)}")

    # ---- the app changes while it counts down (Hebrew) ----------------------------
    arm_he, to_wa, to_nir, words = ("תשלחי לזוהר שאני מאחרת קצת", "תשלחי את זה בוואטסאפ",
                                    "לא, לניר", "במקום זה תגידי שאני כבר בדרך")
    understood = {
        arm_he: dict(contact="Zohar Levin", contact_named=0.95, has_message_content=0.9),
        to_wa: dict(contact="Zohar Levin", contact_named=0.81, refers_back=0.92,
                    has_message_content=0.68, channel="whatsapp", amends_message=0.87,
                    message_app="whatsapp"),
        to_nir: dict(intent_confidence=0.85, contact="Nir Cohen", contact_named=0.96,
                     refers_back=0.74, has_message_content=0.17, amends_message=0.81),
        words: dict(contact="Nir Cohen", contact_named=0.3, refers_back=0.7,
                    has_message_content=0.92, amends_message=0.89),
    }
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[arm_he], s.j.same[arm_he] = "שאני מאחרת קצת", 0.9
        s.j.spans[words] = "שאני כבר בדרך"
        s.j.same[to_nir] = 0.9
        router.handle(s.j, arm_he, speak=False)
        r = router.handle(s.j, to_wa, speak=False)
        aw = router.AWAITING or {}
        check("he: 'send it on WhatsApp' keeps who and what, changes only the app",
              r["did"] == "confirm_send" and aw.get("contact") == "Zohar Levin"
              and aw.get("body") == "שאני מאחרת קצת" and aw.get("channel") == "whatsapp",
              f"did={r['did']} awaiting={short(aw)}")
        check("he: the message counting down is replaced, never sent as well",
              router.PENDING is None and not SENT and not WA, f"sent={SENT} wa={WA}")
        r = _answer(s, "כן")
        pend = router.PENDING or {}
        check("he: her yes arms it on WhatsApp",
              r["did"] == "sending" and pend.get("channel") == "whatsapp"
              and pend.get("to") == "Zohar Levin", f"did={r['did']} pending={short(pend)}")
        r = router.handle(s.j, to_nir, speak=False)
        aw = router.AWAITING or {}
        check("he: 'no, to Nir' keeps the words and the app",
              r["did"] == "confirm_send" and aw.get("contact") == "Nir Cohen"
              and aw.get("body") == "שאני מאחרת קצת" and aw.get("channel") == "whatsapp"
              and router.PENDING is None, f"did={r['did']} awaiting={short(aw)}")
        _answer(s, "כן")
        r = router.handle(s.j, words, speak=False)
        pend = router.PENDING or {}
        check("he: new words she just said keep the person and the app, on a countdown",
              r["did"] == "sending" and pend.get("to") == "Nir Cohen"
              and pend.get("text") == "שאני כבר בדרך" and pend.get("channel") == "whatsapp",
              f"did={r['did']} pending={short(pend)}")
        check("he: it reads the new message back in Hebrew",
              "שאני כבר בדרך" in (r.get("say") or "") and r.get("lang") == "hebrew",
              short(r.get("say")))
        router._fire_pending()
        check("he: only the last version went out, once",
              not SENT and [t for _, t in WA] == ["שאני כבר בדרך"], f"sent={SENT} wa={WA}")

    # ---- recipient and words, in English -----------------------------------------
    arm_en, to_dana, late = ("send a message to Zohar that I am fine", "no, to Dana",
                             "say I'm running late instead")
    understood = {
        arm_en: dict(contact="Zohar Levin", contact_named=0.95, has_message_content=0.9),
        to_dana: dict(intent_confidence=0.9, contact="Dana", contact_named=0.97,
                      refers_back=0.54, has_message_content=0.13, amends_message=0.91,
                      rejects_last=0.51),
        late: dict(intent_confidence=0.93, contact="Dana", contact_named=0.37,
                   refers_back=0.73, has_message_content=0.92, amends_message=0.89),
    }
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[arm_en], s.j.same[arm_en] = "I am fine", 0.9
        s.j.spans[late] = "I m running late"
        s.j.same[to_dana] = 0.9
        router.handle(s.j, arm_en, speak=False)
        r = router.handle(s.j, to_dana, speak=False)
        aw = router.AWAITING or {}
        check("en: 'no, to Dana' keeps the words",
              r["did"] == "confirm_send" and aw.get("contact") == "Dana"
              and aw.get("body") == "I am fine", f"did={r['did']} awaiting={short(aw)}")
        check("en: and the name she said was still checked against the book",
              "same_person" in s.j.asked, str(s.j.asked))
        _answer(s, "yes")
        r = router.handle(s.j, late, speak=False)
        pend = router.PENDING or {}
        check("en: 'say I'm running late instead' keeps the person",
              r["did"] == "sending" and pend.get("to") == "Dana"
              and pend.get("text") == "I m running late", f"did={r['did']} pending={short(pend)}")
        check("en: nothing went out while she was correcting it", not SENT and not WA,
              f"sent={SENT} wa={WA}")

    # ---- answering "what should it say?", then changing the app -------------------
    ask_m, answer, on_wa = "send a message to Matan", "that I'm running late", "on WhatsApp"
    ask_g, answer_he, him_wa = "תשלחי הודעה לגל", "שאני אוהבת אותו מאוד", "תשלחי לו בוואטסאפ"
    understood = {
        ask_m: dict(contact="Matan", contact_named=0.98, has_message_content=0.1),
        # A bare "on WhatsApp" can read as small talk; with a message right there it
        # is about that message.
        on_wa: dict(intent="chitchat", intent_confidence=0.5, amends_message=0.8,
                    message_app="whatsapp", channel="whatsapp"),
        ask_g: dict(contact="Gal Ben Ami", contact_named=0.97, has_message_content=0.1),
        him_wa: dict(contact="Gal Ben Ami", contact_named=0.7, refers_back=0.9,
                     has_message_content=0.6, channel="whatsapp", amends_message=0.85,
                     message_app="whatsapp"),
    }
    for lang_, ask, ans, follow, who, body, yes in (
            ("en", ask_m, answer, on_wa, "Matan", "that I'm running late", "yes"),
            ("he", ask_g, answer_he, him_wa, "Gal Ben Ami", "שאני אוהבת אותו מאוד", "כן")):
        reset_state()
        with scripted_turns(understood) as s:
            s.j.same[ask] = 0.9
            s.j.answer[ans] = (0.95, 0.05)
            r = router.handle(s.j, ask, speak=False)
            check(f"{lang_}: a message with no words asks what it should say",
                  r["did"] == "need_what", f"did={r['did']}")
            r = router.handle(s.j, ans, speak=False)
            check(f"{lang_}: her answer fills the words and the message is armed",
                  r["did"] == "sending" and _d(r).get("to") == who
                  and _d(r).get("text") == body, f"did={r['did']} detail={short(_d(r))}")
            r = router.handle(s.j, follow, speak=False)
            aw = router.AWAITING or {}
            check(f"{lang_}: the app change after an answered question keeps who and what",
                  r["did"] == "confirm_send" and aw.get("contact") == who
                  and aw.get("body") == body and aw.get("channel") == "whatsapp",
                  f"did={r['did']} awaiting={short(aw)}")
            r = _answer(s, yes)
            check(f"{lang_}: and goes on WhatsApp after her yes",
                  r["did"] == "sending" and (router.PENDING or {}).get("channel") == "whatsapp",
                  f"did={r['did']}")

    # ---- a new request inherits nothing ------------------------------------------
    nir, dana, dana_he = ("send a message to Nir that dinner is at eight",
                          "send a message to Dana", "תשלחי הודעה לדנה שאני מאחרת היום")
    understood = {
        arm_en: dict(contact="Zohar Levin", contact_named=0.95, has_message_content=0.9,
                     channel="whatsapp"),
        nir: dict(contact="Nir Cohen", contact_named=0.98, refers_back=0.09,
                  has_message_content=0.9, amends_message=0.06),
        dana: dict(contact="Dana", contact_named=0.98, refers_back=0.08,
                   has_message_content=0.33, amends_message=0.45),
        dana_he: dict(contact="Dana", contact_named=0.97, refers_back=0.13,
                      has_message_content=0.81, amends_message=0.53),
    }
    for utt, want in ((nir, "dinner is at eight"), (dana, None),
                      (dana_he, "שאני מאחרת היום")):
        reset_state()
        with scripted_turns(understood) as s:
            s.j.spans[arm_en], s.j.same[arm_en] = "I am fine", 0.9
            s.j.spans[utt] = want
            s.j.same[utt] = 0.9
            router.handle(s.j, arm_en, speak=False)       # a WhatsApp to Zohar
            r = router.handle(s.j, utt, speak=False)
            if want is None:
                check(f"{utt!r}: a new message to someone else asks what, keeping nothing",
                      r["did"] == "need_what" and (router.AWAITING or {}).get("body") is None
                      and (router.AWAITING or {}).get("channel") == "imessage",
                      f"did={r['did']} awaiting={short(router.AWAITING)}")
            else:
                check(f"{utt!r}: a new message keeps nothing from the last one",
                      r["did"] == "sending" and _d(r).get("to") != "Zohar Levin"
                      and _d(r).get("text") == want and _d(r).get("channel") == "imessage"
                      and not _d(r).get("amended"), f"did={r['did']} detail={short(_d(r))}")

    # ---- expiry, and "new chat" ---------------------------------------------------
    later = "send it on WhatsApp"
    understood = {arm_en: dict(contact="Zohar Levin", contact_named=0.95,
                               has_message_content=0.9),
                  later: dict(amends_message=0.9, message_app="whatsapp",
                              channel="whatsapp", refers_back=0.8)}
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[arm_en], s.j.same[arm_en] = "I am fine", 0.9
        router.handle(s.j, arm_en, speak=False)
        router._fire_pending()
        router.MEM.draft["at"] -= router.DRAFT_TTL + 5
        r = router.handle(s.j, later, speak=False)
        # The person may still be offered for "him" or "her" (that is MEM.contact,
        # older than this); the words and the send itself never are.
        check("a message from minutes ago is not carried into 'send it on WhatsApp'",
              s.drafts[-1] is None and r["did"] in ("need_who", "need_what")
              and router.PENDING is None and (router.AWAITING or {}).get("body") is None,
              f"did={r['did']} draft={short(s.drafts[-1])} awaiting={short(router.AWAITING)}")
        check("and nothing went out a second time", len(SENT) == 1 and not WA,
              f"sent={SENT} wa={WA}")
        reset_state()
        router.handle(s.j, arm_en, speak=False)
        router.new_conversation()
        check("'new chat' drops the message and stops its countdown",
              router.MEM.live_draft() is None and router.PENDING is None and not SENT,
              f"draft={short(router.MEM.draft)} pending={short(router.PENDING)}")

    # ---- "no wait, ..." while it counts down --------------------------------------
    nw, stop_nw, bare = ("no wait, send it on WhatsApp", "no, send it on WhatsApp instead",
                         "no")
    ch = dict(contact="Zohar Levin", contact_named=0.6, refers_back=0.8,
              has_message_content=0.6, amends_message=0.9, message_app="whatsapp",
              channel="whatsapp")
    understood = {arm_en: dict(contact="Zohar Levin", contact_named=0.95,
                               has_message_content=0.9),
                  nw: ch, stop_nw: ch, later: ch}
    for utt, score in ((nw, (0.40, 0.85)), (stop_nw, (0.90, 0.85))):
        reset_state()
        with scripted_turns(understood) as s:
            s.j.spans[arm_en], s.j.same[arm_en] = "I am fine", 0.9
            s.j.cancel[utt] = score
            router.handle(s.j, arm_en, speak=False)
            r = router.handle(s.j, utt, speak=False)
            aw = router.AWAITING or {}
            check(f"{utt!r} during the countdown becomes the WhatsApp version",
                  r["did"] == "confirm_send" and aw.get("channel") == "whatsapp"
                  and aw.get("body") == "I am fine" and router.PENDING is None
                  and not SENT and not WA,
                  f"did={r['did']} awaiting={short(aw)} sent={SENT}")
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[arm_en], s.j.same[arm_en] = "I am fine", 0.9
        s.j.cancel[bare] = (0.90, 0.17)
        router.handle(s.j, arm_en, speak=False)
        r = router.handle(s.j, bare, speak=False)
        check("a bare 'no' still just stops it",
              r["did"] == "cancelled" and router.PENDING is None and not SENT,
              f"did={r['did']}")
        r = router.handle(s.j, later, speak=False)
        aw = router.AWAITING or {}
        check("and the stopped message can still go on WhatsApp without saying it again",
              r["did"] == "confirm_send" and aw.get("contact") == "Zohar Levin"
              and aw.get("body") == "I am fine" and aw.get("channel") == "whatsapp",
              f"did={r['did']} awaiting={short(aw)}")

    # ---- the safety rules still hold ---------------------------------------------
    stranger = "no, to Bartholomew"
    understood = {arm_en: dict(contact="Zohar Levin", contact_named=0.95,
                               has_message_content=0.9),
                  # Jev picks the nearest row even for a name not in her book.
                  stranger: dict(contact="Gal Ben Ami", contact_named=0.95,
                                 refers_back=0.5, amends_message=0.9),
                  fr: dict(contact="Zohar Levin", contact_named=0.6, refers_back=0.8,
                           amends_message=0.9, write_in="french"),
                  later: ch}
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[arm_en], s.j.same[arm_en] = "I am fine", 0.9
        router.handle(s.j, arm_en, speak=False)
        r = router.handle(s.j, stranger, speak=False)
        check("a name not in her book is never swapped for a real contact",
              r["did"] == "need_who" and router.PENDING is None and not SENT
              and (router.AWAITING or {}).get("body") == "I am fine",
              f"did={r['did']} pending={short(router.PENDING)} awaiting={short(router.AWAITING)}")
        s.j.answer["Gal"], s.j.same["Gal"], s.j.named["Gal"] = (0.95, 0.05), 0.9, "Gal Ben Ami"
        r = router.handle(s.j, "Gal", speak=False)
        check("and naming someone then asks before sending words she said earlier",
              r["did"] == "confirm_send" and router.PENDING is None
              and (router.AWAITING or {}).get("body") == "I am fine",
              f"did={r['did']} awaiting={short(router.AWAITING)}")
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[arm_en], s.j.same[arm_en] = "I am fine", 0.9
        router.handle(s.j, arm_en, speak=False)
        s.llm.available = False
        r = router.handle(s.j, fr, speak=False)
        check("a language it cannot write in stops and says so, it never sends the old words",
              r["did"] == "cant_write_in" and router.PENDING is None and not SENT
              and "language" in (r.get("say") or ""), f"did={r['did']} say={short(r.get('say'))}")
    reset_state()
    with scripted_turns(understood) as s:
        router.MEM.remember_draft("Zohar Levin", None, "imessage", "english")
        r = router.handle(s.j, later, speak=False)
        check("a message with no words yet is never sent: it asks what",
              r["did"] == "need_what" and router.PENDING is None,
              f"did={r['did']} pending={short(router.PENDING)}")
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[arm_en], s.j.same[arm_en] = "I am fine", 0.9
        with gates_on():
            router.handle(s.j, arm_en, speak=False)
        mac.SEND_FOR_REAL = False
        try:
            r = router.handle(s.j, later, speak=False)
        finally:
            mac.SEND_FOR_REAL = True
        check("with sending switched off a follow-up is refused out loud, not armed",
              r["did"] == "send_disabled" and router.PENDING is None
              and _d(r).get("channel") == "whatsapp", f"did={r['did']} detail={short(_d(r))}")
    check("no follow-up reached osascript", not OSA, f"{len(OSA)} escaped")


@with_gates_on
def t_a_message_goes_only_when_she_means_it(j):
    """Release blockers from the owner's session on 1.0.2. She pressed the key to
    correct a message and it went anyway; a one-word mishearing went out, twice, as
    the whole message."""
    arm_en = "send a message to Dana that I will be home at six"
    arm_he = "תשלחי לזוהר שאני אגיע הביתה בשש"
    louder = "make it louder"
    understood = {
        arm_en: dict(contact="Dana", contact_named=0.95, has_message_content=0.9),
        arm_he: dict(contact="Zohar Levin", contact_named=0.95, has_message_content=0.9),
        louder: dict(intent="control", control_action="louder", control_confidence=0.95),
    }

    def armed(s, utt=arm_en, body="I will be home at six"):
        s.j.spans[utt], s.j.same[utt] = body, 0.9
        r = router.handle(s.j, utt, speak=False)
        assert r["did"] == "sending", r
        return r

    # ---- S1: a key press stops the clock ------------------------------------------
    reset_state()
    with scripted_turns(understood) as s:
        router.CANCEL_WINDOW = 0.3
        armed(s)
        held = router.pause_pending()
        time.sleep(0.6)
        check("S1: a key press stops the countdown: nothing goes when it runs out",
              held is not None and not SENT and (router.PENDING or {}).get("paused"),
              f"sent={SENT} pending={short(router.PENDING)}")
        r = router.turn_closed(speak=False)
        aw = router.AWAITING or {}
        check("S1: a turn that heard nothing asks whether to still send it",
              r["did"] == "confirm_send" and "Dana" in (r.get("say") or "")
              and aw.get("need") == "confirm_send" and aw.get("body") == "I will be home at six"
              and router.PENDING is None and not SENT,
              f"r={short(r)} awaiting={short(aw)}")
        router.CANCEL_WINDOW = 30.0
        r = _answer(s, "yes")
        check("S1: an explicit yes reads it back and starts a fresh countdown",
              r["did"] == "sending" and "I will be home at six" in (r.get("say") or "")
              and router.PENDING is not None and not SENT, f"did={r['did']}")
    reset_state()
    with scripted_turns(understood) as s:
        armed(s)
        router.pause_pending()
        router.turn_closed(speak=False)
        r = _answer(s, "no", yes="no")
        check("S1: a no to 'still send it?' sends nothing",
              r["did"] == "send_declined" and router.PENDING is None and not SENT,
              f"did={r['did']}")
    for label, yes, conf in (("an unclear answer", "neither", 0.9),
                             ("a hesitant yes", "yes", 0.5)):
        reset_state()
        with scripted_turns(understood) as s:
            armed(s)
            router.pause_pending()
            router.turn_closed(speak=False)
            r = _answer(s, "hmm", yes=yes, conf=conf)
            check(f"S1: {label} is not a yes",
                  r["did"] == "send_declined" and router.PENDING is None and not SENT,
                  f"did={r['did']}")
    reset_state()
    with scripted_turns(understood) as s:
        armed(s)
        router.pause_pending()
        r = router.handle(s.j, louder, speak=False)
        check("S1: an unrelated request is done, and the held message is asked about",
              r["did"] == "louder" and VOLUME and "Dana" in (r.get("say") or "")
              and (router.AWAITING or {}).get("need") == "confirm_send"
              and router.PENDING is None and not SENT, f"r={short(r)}")
    reset_state()
    with scripted_turns(understood) as s:
        armed(s)
        router.pause_pending()
        s.j.cancel["no"] = (0.9, 0.1)
        r = router.handle(s.j, "no", speak=False)
        check("S1: 'no' after the key press stops it as before, no question",
              r["did"] == "cancelled" and router.AWAITING is None and not SENT,
              f"did={r['did']} awaiting={short(router.AWAITING)}")
    reset_state()
    with scripted_turns(understood) as s:
        armed(s)
        router.pause_pending()
        nir = "send a message to Nir that dinner is at eight"
        understood[nir] = dict(contact="Nir Cohen", contact_named=0.98,
                               has_message_content=0.9)
        s.j.spans[nir], s.j.same[nir] = "dinner is at eight", 0.9
        router.handle(s.j, nir, speak=False)
        router._fire_pending()
        check("S1: a held message is never sent because another one was armed",
              SENT == [("Nir Cohen", "dinner is at eight")], f"sent={SENT}")
    reset_state()
    with scripted_turns(understood) as s:
        armed(s, arm_he, "שאני אגיע הביתה בשש")
        router.pause_pending()
        r = router.turn_closed(speak=False)
        check("S1 he: the question is asked in Hebrew",
              r["did"] == "confirm_send" and "עצרתי" in (r.get("say") or ""),
              short(r.get("say")))
        r = _answer(s, "כן")
        check("S1 he: 'כן' sends it on a fresh countdown",
              r["did"] == "sending" and router.PENDING is not None, f"did={r['did']}")
    # The listener's pairing: {"ts"} on open, {"ts", "sent", "reason"} on close.
    reset_state()
    with scripted_turns(understood) as s:
        armed(s)
        router.pause_pending(100.0)
        r = router.turn_closed(speak=False, turn_ts=100.0, sent=False, reason="replaced")
        check("S1: a turn replaced by another keeps the message held, no question yet",
              r["did"] == "still_held" and (router.PENDING or {}).get("paused")
              and router.AWAITING is None, f"r={r}")
        router.pause_pending(101.0)
        r = router.turn_closed(speak=False, turn_ts=100.0, sent=False, reason="nothing said")
        check("S1: a late close from the older turn is ignored",
              r["did"] == "held_by_another_turn" and (router.PENDING or {}).get("paused"),
              f"r={r}")
        r = router.turn_closed(speak=False, turn_ts=101.0, sent=False, reason="nothing said")
        check("S1: the newer turn ending with nothing said asks",
              r["did"] == "confirm_send" and router.PENDING is None and not SENT, f"r={r}")
    reset_state()
    with scripted_turns(understood) as s:
        armed(s)
        router.pause_pending(200.0)
        router.handle(s.j, louder, speak=False)
        r = router.turn_closed(speak=False, turn_ts=200.0, sent=True, reason="sent")
        check("S1: the close after a handled utterance changes nothing",
              r["did"] == "nothing_held"
              and (router.AWAITING or {}).get("need") == "confirm_send" and not SENT,
              f"r={r}")
    reset_state()
    with scripted_turns(understood) as s:
        prev = router.HELD_WATCHDOG
        router.HELD_WATCHDOG = 0.2
        try:
            armed(s)
            router.pause_pending(300.0)
            time.sleep(0.6)
        finally:
            router.HELD_WATCHDOG = prev
        check("S1: with no close at all, the watchdog asks and never sends",
              (router.AWAITING or {}).get("need") == "confirm_send"
              and router.PENDING is None and not SENT,
              f"awaiting={short(router.AWAITING)} pending={short(router.PENDING)}")
    # Both payload shapes the server accepts (contract frozen 2026-09-26), through the
    # same functions the HTTP handler calls.
    from savta import server as srv
    for shape, closed in (("listener", {"ts": 400.0, "heard": False, "why": "nothing",
                                        "speak": False}),
                          ("sent/reason", {"ts": 400.0, "sent": False,
                                           "reason": "nothing said", "speak": False})):
        reset_state()
        with scripted_turns(understood) as s:
            armed(s)
            check(f"S1 {shape}: the armed listen-for-no window never holds a message",
                  srv.turn_open({"ts": 399.0, "kind": "armed"}) == {"paused": False}
                  and not (router.PENDING or {}).get("paused"))
            r = srv.turn_open({"ts": 400.0, "kind": "hold"})
            check(f"S1 {shape}: turn_open holds it", r["paused"] and r["to"] == "Dana", str(r))
            r = srv.turn_closed(closed)
            check(f"S1 {shape}: a close with nothing heard asks, never sends",
                  r["did"] == "confirm_send" and router.PENDING is None and not SENT
                  and (router.AWAITING or {}).get("need") == "confirm_send", str(r))
        reset_state()
        with scripted_turns(understood) as s:
            armed(s)
            srv.turn_open({"ts": 500.0, "kind": "tap"})
            late = dict(closed, ts=499.0)
            r = srv.turn_closed(late)
            check(f"S1 {shape}: a close from an older turn is ignored",
                  r["did"] == "held_by_another_turn" and (router.PENDING or {}).get("paused"),
                  str(r))
    reset_state()
    with scripted_turns(understood) as s:
        armed(s)
        srv.turn_open({"ts": 600.0})
        r1 = srv.turn_closed({"ts": 600.0, "sent": True, "reason": "sent", "speak": False})
        r2 = srv.turn_closed({"ts": 600.0, "sent": False, "reason": "replaced",
                              "speak": False})
        check("S1: sent=true is a no-op and 'replaced' keeps it held",
              r1["did"] == "nothing_to_do" and r2["did"] == "still_held"
              and (router.PENDING or {}).get("paused") and not SENT, f"{r1} {r2}")
    reset_state()
    with scripted_turns(understood) as s:
        check("S1: with nothing counting down a key press pauses nothing",
              router.pause_pending() is None and router.turn_closed(speak=False)["did"]
              == "nothing_held")

    # ---- S2: short, low-confidence, or not said this turn ------------------------
    ask_m, jet = "send a message to Matan", "jet"
    ask_g, jet_he = "תשלחי הודעה לגל", "ג'ט"
    yes_d = "tell Dana yes"
    long_ = "send a message to Dana that the keys are under the mat"
    understood.update({
        ask_m: dict(contact="Matan", contact_named=0.98, has_message_content=0.1),
        ask_g: dict(contact="Gal Ben Ami", contact_named=0.97, has_message_content=0.1),
        yes_d: dict(contact="Dana", contact_named=0.97, has_message_content=0.8),
        long_: dict(contact="Dana", contact_named=0.97, has_message_content=0.9),
    })
    for lang_, ask, word, who, no in (("en", ask_m, jet, "Matan", "no"),
                                      ("he", ask_g, jet_he, "Gal Ben Ami", "לא")):
        reset_state()
        with scripted_turns(understood) as s:
            s.j.same[ask] = 0.9
            s.j.answer[word] = (0.95, 0.05)
            router.handle(s.j, ask, speak=False)
            r = router.handle(s.j, word, speak=False)
            check(f"S2 {lang_}: a one-word answer to 'what should it say?' is confirmed",
                  r["did"] == "confirm_send" and router.PENDING is None
                  and word in (r.get("say") or "") and _d(r).get("why") == "short",
                  f"did={r['did']} say={short(r.get('say'))}")
            r = _answer(s, no, yes="no")
            check(f"S2 {lang_}: and 'no' leaves it unsent", r["did"] == "send_declined"
                  and not SENT and not WA and router.PENDING is None, f"did={r['did']}")
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[yes_d], s.j.same[yes_d] = "yes", 0.9
        r = router.handle(s.j, yes_d, speak=False)
        check("S2: a one-word message said in one breath is confirmed, not counted down",
              r["did"] == "confirm_send" and router.PENDING is None
              and 'Send "yes" to Dana?' in (r.get("say") or ""), short(r.get("say")))
    reset_state()
    with scripted_turns(understood) as s:
        s.j.spans[long_], s.j.same[long_] = "the keys are under the mat", 0.9
        r = router.handle(s.j, long_, speak=False, asr_conf=0.4)
        check("S2: a turn the recogniser was unsure of is confirmed",
              r["did"] == "confirm_send" and _d(r).get("why") == "low_confidence"
              and router.PENDING is None, f"did={r['did']} detail={short(_d(r))}")
        reset_state()
        r = router.handle(s.j, long_, speak=False, asr_conf=0.9)
        check("S2: the same message heard clearly still goes on the countdown",
              r["did"] == "sending" and router.PENDING is not None, f"did={r['did']}")

    # ---- S3: the language is said out loud (Hebrew) --------------------------------
    fr_he = "תכתבי את זה בצרפתית"
    understood[fr_he] = dict(contact="Zohar Levin", contact_named=0.7, refers_back=0.9,
                             amends_message=0.9, write_in="french")
    reset_state()
    with scripted_turns(understood) as s:
        armed(s, arm_he, "שאני אגיע הביתה בשש")
        r = router.handle(s.j, fr_he, speak=False)
        check("S3 he: the question says it is in French",
              r["did"] == "confirm_send" and "בצרפתית" in (r.get("say") or ""),
              short(r.get("say")))
        r = _answer(s, "כן")
        check("S3 he: and so does the read-back",
              r["did"] == "sending" and "בצרפתית" in (r.get("say") or ""),
              short(r.get("say")))
    check("nothing reached osascript", not OSA, f"{len(OSA)} escaped")

def t_onboarding(j: Jev):
    """4. The first conversation, from an empty machine."""
    reset_state()
    try:
        PROFILE.unlink(missing_ok=True)
        check("profile.json is gone before onboarding starts", not PROFILE.exists())

        # A silent first press: nothing heard yet, so it greets in the language chosen
        # in Settings, which is English unless she chose another. It used to greet in
        # Hebrew first, in the Hebrew voice, on every new Mac.
        r0 = router.handle(j, "", speak=False)
        check("a silent first press greets in English only by default",
              r0["did"] == "onboarding_greet" and r0.get("lang") == "english"
              and (r0.get("say") or "").startswith("Hello, I'm MicMic")
              and "מיקמיק" not in (r0.get("say") or ""),
              f"did={r0['did']!r} lang={r0.get('lang')!r} say={short(r0.get('say'))}")
        saved = router.SETTINGS.read_bytes() if router.SETTINGS.exists() else None
        try:
            router.save_settings({"language_hint": "he-IL"})
            PROFILE.unlink(missing_ok=True)
            r0 = router.handle(j, "", speak=False)
            check("with Hebrew chosen in Settings, the silent first press is Hebrew only",
                  r0.get("lang") == "hebrew" and (r0.get("say") or "").startswith("שלום")
                  and "MicMic" not in (r0.get("say") or ""),
                  f"lang={r0.get('lang')!r} say={short(r0.get('say'))}")
        finally:
            if saved is None:
                router.SETTINGS.unlink(missing_ok=True)
            else:
                router.SETTINGS.write_bytes(saved)
        # A real first request is answered FIRST; the one-line introduction follows.
        PROFILE.unlink(missing_ok=True)
        r0 = router.handle(j, "what time is it micmic", speak=False)
        say0 = r0.get("say") or ""
        check("a first request is answered before the introduction",
              r0["did"] == "answered" and say0.startswith("It is ")
              and say0.endswith("I'm MicMic. What should I call you?")
              and "Hello" not in say0,
              f"did={r0['did']!r} say={short(say0)}")
        # Speaking Hebrew is enough to pick the language, and the request still happens,
        # answered first, then introduced, in her language only.
        PROFILE.unlink(missing_ok=True)
        r0 = router.handle(j, "מה השעה מיקמק", speak=False)
        say0 = r0.get("say") or ""
        check("speaking first greets only in her language, after the answer",
              r0["did"] == "answered" and say0.startswith("השעה")
              and say0.endswith("אני מיקמיק. איך קוראים לך?") and "MicMic" not in say0,
              f"did={r0['did']!r} say={short(say0)}")

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
        check_latency(f"{utt[:30]!r} answers in under 3s", dt < 3000, f"{dt:.0f}ms")
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
        check_latency(f"{utt!r} answers in under 1.5s", dt < 1500, f"{dt:.0f}ms")
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
        r = router.handle(j, "תשלחי הודעה לזוהר שאני בסדר גמור היום", speak=False)
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
        for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
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
    check("a text box gets the month spelled out, which no page can misread",
          bool(plain) and as_date(plain) == today + datetime.timedelta(days=1)
          and "/" not in plain and plain[:3].isalpha(), repr(plain))

    # 02/10 is 2 October or 10 February depending on who reads it; Google Flights read
    # February, and "next Friday" (2 October 2026) failed 3/3. The shapes, offline:
    oct2 = time.mktime((2026, 10, 2, 12, 0, 0, 0, 0, -1))
    for label, want in (("Departure", "Oct 2, 2026"),
                        ("Departure (MM/DD/YYYY)", "10/02/2026"),
                        ("Date dd/mm/yyyy", "02/10/2026"),
                        ("Datum TT.MM.JJJJ dd.mm.yyyy", "02.10.2026"),
                        ("yyyy-mm-dd", "2026-10-02")):
        got = time.strftime(web.date_format({"label": label, "type": "text"}),
                            time.localtime(oct2))
        check(f"2 October in a box labelled {label!r} is {want!r}", got == want, got)
    check("a native date input still gets ISO",
          web.date_format({"label": "Departure", "type": "date"}) == "%Y-%m-%d")

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
          as_date(rows[0][0]) == today, rows[0][0])


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



class _LLMSpy:
    """Stands in for Gemini inside one block: records every prompt, answers from a
    script, and puts the real client back afterwards."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __enter__(self):
        self.real = router.LLM_CLIENT.text

        def text(prompt, system="", max_tokens=400, temperature=0.2, timeout=None):
            self.calls.append({"prompt": prompt, "system": system, "timeout": timeout})
            return self.answers.pop(0) if self.answers else None
        router.LLM_CLIENT.text = text
        return self

    def __exit__(self, *exc):
        router.LLM_CLIENT.text = self.real
        return False


def t_weather_never_from_a_model(j):
    """QA v1.0.1 #5: "מה מזג האוויר בליסבון" reached wttr as "בליסבון", which it does
    not know, and a model then made the weather up. The weather comes from the weather
    service or it is not given."""
    real_geo, real_at = facts.geocode, facts.conditions_at
    known = {"ליסבון", "Lisbon", "חיפה", "Haifa", "ירושלים", "תל אביב"}
    asked: list[str] = []

    def only_real_names(name, lang="en"):
        asked.append(name)
        return ([{"name": name, "country": "", "country_code": "PT", "admin1": "",
                  "lat": 32.8, "lon": 35.0, "population": 500000}]
                if name in known else [])
    facts.geocode = only_real_names
    try:
        for utt in ("מה מזג האוויר בליסבון", "מה התחזית לליסבון", "what's the weather in Lisbon"):
            reset_state(); asked.clear()
            with _LLMSpy("נעים מאוד") as spy:
                r = router.handle(j, utt, speak=False)
            say = r.get("say") or ""
            check(f"{utt!r} is answered from the weather service",
                  r["did"] == "answered" and "17" in say and (r.get("detail") or {}).get(
                      "source") == "wttr.in", f"did={r['did']} say={say} asked={asked}")
            check(f"{utt!r} never asks a model", not spy.calls, str(spy.calls)[:120])
            check(f"{utt!r} says Lisbon back once, with one preposition",
                  ("בליסבון" in say and "בבליסבון" not in say) or "In Lisbon" in say, say)

        # The follow-up. Just under the 0.5 gate a bare "Lisbon" went to the model,
        # which described Lisbon's weather with no data on 9 of 10 turns.
        for utt, recent in (("Lisbon", "מה השעה -> answered | what's the weather in Lisbon -> answered"),):
            reset_state(); asked.clear()
            with _LLMSpy("The weather in Lisbon is lovely right now.") as spy:
                r = router.handle(j, utt, recent, speak=False)
            check(f"{utt!r} right after a weather question is weather, from wttr",
                  r["did"] == "answered" and (r.get("detail") or {}).get("source") == "wttr.in"
                  and not spy.calls, f"did={r['did']} say={r.get('say')} llm={len(spy.calls)}")
        with _LLMSpy("Lisbon is the capital of Portugal.") as spy:
            reset_state()
            r = router.handle(j, "tell me about Lisbon", "what's the weather in Lisbon -> answered",
                              speak=False)
        check("but 'tell me about Lisbon' is still a question about the city",
              (r.get("detail") or {}).get("source") != "wttr.in", f"did={r['did']} {short(r.get('detail'))}")
        check("and the model is told it has no weather to give",
              spy.calls and "no live weather" in spy.calls[0]["system"], str(spy.calls)[:100])
        with _LLMSpy("The weather in Lisbon is lovely.") as spy:
            router.LLM_CLIENT.chat("Lisbon", "english")
        check("so is the small-talk model",
              spy.calls and "no live weather" in spy.calls[0]["system"], str(spy.calls)[:100])

        reset_state(); asked.clear()
        with _LLMSpy("נעים מאוד") as spy:
            r = router.handle(j, "מה מזג האוויר בקסקזזבלוף", speak=False)
        check("a place wttr does not know is said so, honestly and in Hebrew",
              r["did"] == "weather_unavailable" and "לא מצאתי" in (r.get("say") or "")
              and not spy.calls, f"did={r['did']} say={r.get('say')} llm={spy.calls}")

        facts.conditions_at = lambda lat, lon: None
        reset_state()
        with _LLMSpy("It is lovely and sunny") as spy:
            r = router.handle(j, "what is the weather in Lisbon", speak=False)
        check("with wttr down she hears that, not a model's guess",
              r["did"] == "weather_unavailable" and "not answering" in (r.get("say") or "")
              and "sunny" not in (r.get("say") or "") and not spy.calls,
              f"did={r['did']} say={r.get('say')}")
        # A report from somewhere else is someone else's weather.
        facts.conditions_at = lambda lat, lon: dict(FAKE_WEATHER, lat=40.0, lon=-3.7)
        reset_state()
        with _LLMSpy("It is lovely and sunny") as spy:
            r = router.handle(j, "what is the weather in Lisbon", speak=False)
        check("a report for a place far from the one she named is not spoken",
              r["did"] == "weather_unavailable" and "could not find" in (r.get("say") or ""),
              f"did={r['did']} say={r.get('say')}")
    finally:
        facts.geocode, facts.conditions_at = real_geo, real_at

    # The daily briefing carries the weather too, and it used to go through a model
    # to be rephrased in her language. This is what the suite-wide default of
    # router.due_briefing = False exists to keep out of every OTHER test: it is put
    # back for exactly this section, which is the one that means to exercise it.
    reset_state()
    p = prof.load()
    p["last_briefed"] = ""
    prof.save(p)
    router.due_briefing = REAL_DUE_BRIEFING
    try:
        with _LLMSpy("בוקר טוב, היום נעים מאוד") as spy:
            r = router.handle(j, "מה השעה", speak=False)
        brief = r.get("briefing") or ""
        check("the morning briefing's weather is wttr's numbers, in Hebrew, with no model",
              "17" in brief and "מעלות" in brief and not spy.calls,
              f"briefing={brief!r} llm={len(spy.calls)}")
        check("and the question she asked is still answered", r["did"] == "answered", r["did"])
        check("the briefing is marked done for today", not router.due_briefing())
    finally:
        router.due_briefing = lambda: False       # back to the suite default

    for said, want in (("בליסבון", "ליסבון"), ("לליסבון", "ליסבון"), ("מלונדון", "לונדון"),
                       ("הרצליה", "רצליה"), ("ובחיפה", "חיפה"), ("في باريس", "باريس")):
        got = router._place_readings(said)
        check(f"{said!r} is tried as said first, then as {want!r}",
              got[0] == said and want in got, str(got))
    check("a name with no bound letter is tried only as said",
          router._place_readings("Lisbon") == ["Lisbon"], str(router._place_readings("Lisbon")))


def t_undo_answers_in_her_language(j):
    """QA #13: "תבטלי את מה שעשית" with nothing to undo answered in English."""
    reset_state(); router._drop_undo()
    r = router.handle(j, "תבטלי את מה שעשית", speak=False)
    check("nothing to undo, said in Hebrew", r["did"] == "nothing_to_undo"
          and r["say"] == router._UNDO_NOTHING["hebrew"], f"did={r['did']} say={r['say']}")
    router._offer_undo("louder", "english", lambda: True, router._DONE["volume"])
    u = router.undo_last("hebrew")
    check("an English action undone by a Hebrew request answers in Hebrew",
          u["undone"] and u["say"] == router._DONE["volume"]["hebrew"], str(u))
    router._offer_undo("louder", "hebrew", lambda: True, router._DONE["volume"])
    router.undo_last()
    check("the button with nothing left answers in the last action's language",
          router.undo_last()["say"] == router._UNDO_NOTHING["hebrew"])


def t_the_trailer_she_asked_for(j):
    """QA #14: "play the Titanic trailer" searched "Titanic full movie" and played a
    parody or a pirated film. Jev still chooses; only among real results."""
    titanic = [
        {"id": "full1", "title": "TITANIC 1997 FULL MOVIE ENGLISH HD JACK AND ROSE",
         "channel": "crafting M", "length": "2:52:58", "views": "6.6M views"},
        {"id": "orange", "title": "Annoying Orange: Titanic", "channel": "Annoying Orange",
         "length": "3:12", "views": "9M views"},
        {"id": "parody", "title": "Titanic Parody | Iceberg Edition", "channel": "Funny",
         "length": "4:02", "views": "2M views"},
        {"id": "official", "title": 'Titanic | "Official Trailer" | Paramount Movies',
         "channel": "Paramount Movies", "length": "1:42", "views": "8.6M views"},
        {"id": "rt", "title": "Titanic (1997) Trailer #1 | Movieclips Classic Trailers",
         "channel": "Rotten Tomatoes Classic Trailers", "length": "2:03", "views": "2.9M views"},
    ]
    real = yt.search
    yt.search = lambda q, n=18: (YT_QUERIES.append(q), [dict(v) for v in titanic])[1]
    try:
        for utt in ("play the Titanic trailer", "official trailer for the movie Titanic",
                    "תשימי לי את הטריילר של טיטאניק"):
            reset_state()
            r = router.handle(j, utt, speak=False)
            d = r.get("detail") or {}
            check(f"{utt!r} searches for a trailer, not the full movie",
                  YT_QUERIES and ("trailer" in YT_QUERIES[0].lower() or "טריילר" in YT_QUERIES[0])
                  and "full movie" not in YT_QUERIES[0], str(YT_QUERIES))
            check(f"{utt!r} plays an official trailer",
                  r["did"] == "playing" and d.get("video_id") in ("official", "rt"),
                  f"did={r['did']} picked={d.get('title')}")
    finally:
        yt.search = real
    kept = yt.screen_results(titanic, trailer=True)
    check("parodies and whole-film uploads never reach the trailer choice",
          {r["id"] for r in kept} == {"official", "rt"}, str([r["id"] for r in kept]))
    check("a title is read without its channel, pipes or quotes",
          yt.spoken_title('Titanic | "Iceberg, Right Ahead!" | Paramount', "Paramount Movies")
          == "Titanic, Iceberg, Right Ahead!",
          yt.spoken_title('Titanic | "Iceberg, Right Ahead!" | Paramount', "Paramount Movies"))
    check("and an artist's own channel does not eat the artist",
          yt.spoken_title("Adele - Hello (Official Music Video)", "AdeleVEVO") == "Adele - Hello",
          yt.spoken_title("Adele - Hello (Official Music Video)", "AdeleVEVO"))


def t_answers_are_in_one_language(j):
    """QA #15: a Hebrew answer came back with Arabic spliced into it, grounded in an
    article about the wrong thing, ending in a question nobody asked."""
    from savta import llm as _llm
    mixed = "המרחק הוא כשماء وثلاثمائة ألف קילומטר. בערך 384 אלף קילומטרים."
    check("Arabic inside a Hebrew answer is caught", _llm.foreign_script(mixed, "hebrew"))
    check("a Latin name inside a Hebrew answer is fine",
          not _llm.foreign_script("את הספר כתבה ג'יין אוסטן (Jane Austen).", "hebrew"))
    with _LLMSpy(mixed, "המרחק הממוצע הוא 384 אלף קילומטרים.") as spy:
        out = router.LLM_CLIENT.answer("כמה רחוק הירח", "hebrew")
    check("a mixed answer is asked for again, strictly, and the clean one is used",
          out == "המרחק הממוצע הוא 384 אלף קילומטרים." and len(spy.calls) == 2
          and "ONLY in Hebrew" in spy.calls[1]["system"], f"{out!r} calls={len(spy.calls)}")
    with _LLMSpy(mixed, mixed) as spy:
        out = router.LLM_CLIENT.answer("כמה רחוק הירח", "hebrew")
    check("twice mixed: only the sentences in her script are kept",
          out == "בערך 384 אלף קילומטרים.", repr(out))
    with _LLMSpy("המרחק הוא 384 אלף קילומטרים. את מתעניינת באסטרונומיה?") as spy:
        out = router.LLM_CLIENT.answer("כמה רחוק הירח", "hebrew")
    check("an answer does not end in a question back", out == "המרחק הוא 384 אלף קילומטרים.",
          repr(out))
    check("spoken answers wait at most 8 s",
          spy.calls and spy.calls[0]["timeout"] == _llm.SPOKEN_TIMEOUT == 8.0, str(spy.calls))

    real_wiki = facts.wiki
    for utt, passage, keep in (
            ("כמה רחוק הירח מכדור הארץ",
             "מופע הירח הוא צורת הירח הנראית לצופה הנמצא בכדור הארץ.", False),
            ("who wrote Pride and Prejudice",
             "Pride and Prejudice is a novel by English author Jane Austen, published in 1813.",
             True)):
        facts.wiki = lambda term, lang="he", p=passage: p
        reset_state()
        with _LLMSpy("384 thousand kilometres." if not keep else "Jane Austen.") as spy:
            r = router.handle(j, utt, speak=False)
        grounded = bool(spy.calls) and passage in spy.calls[0]["prompt"]
        check(f"{utt[:28]!r}: the passage is {'kept' if keep else 'dropped'}",
              r["did"] == "answered" and grounded == keep,
              f"did={r['did']} grounded={grounded} detail={short(r.get('detail'))}")
    # Knowledge speed: the term rides in understand(), and Wikipedia gets 1.5 s.
    facts.wiki = lambda term, lang="he": "Pride and Prejudice is a novel by Jane Austen."
    reset_state()
    with _LLMSpy("Jane Austen wrote it.") as spy:
        r = router.handle(j, "who wrote Pride and Prejudice", speak=False)
    check("a knowledge answer asks Jev twice, not three times (the term rides along)",
          r["did"] == "answered" and r["timing"]["jev_n"] == 2, str(r.get("timing")))

    def slow_wiki(term, lang="he"):
        time.sleep(4)
        return "Pride and Prejudice is a novel by Jane Austen."
    facts.wiki = slow_wiki
    reset_state()
    t0 = time.time()
    with _LLMSpy("Jane Austen wrote it.") as spy:
        r = router.handle(j, "who wrote Pride and Prejudice", speak=False)
    waited = time.time() - t0
    check("a Wikipedia that has not answered in 1.5 s is not waited for",
          r["did"] == "answered" and spy.calls and "Jane Austen, published" not in spy.calls[0]["prompt"]
          and "novel by Jane Austen" not in spy.calls[0]["prompt"] and waited < 3.5,
          f"did={r['did']} waited={waited:.1f}s")
    facts.wiki = real_wiki


def t_small_talk_does_not_hang(j):
    """QA #17: one small-talk turn waited 20.8 s on Gemini, then said a stock line."""
    from savta import llm as _llm
    seen = {}
    real_post = router.LLM_CLIENT._post

    def post(payload, model=None, timeout=None):
        seen["timeout"] = timeout
        return None
    router.LLM_CLIENT._post = post
    try:
        router.LLM_CLIENT.chat("how are you", "english")
    finally:
        router.LLM_CLIENT._post = real_post
    check("small talk gives the model 8 s, not 20", seen.get("timeout") == 8.0, str(seen))
    reset_state()
    real_avail = type(router.LLM_CLIENT).available
    type(router.LLM_CLIENT).available = property(lambda self: True)
    try:
        with _LLMSpy() as spy:
            r = router.handle(j, "היי מה שלומך היום", speak=False)
    finally:
        type(router.LLM_CLIENT).available = real_avail
    check("a model that did not answer is admitted to, in her language",
          r["did"] == "chat_timeout" and "זמן" in (r.get("say") or ""),
          f"did={r['did']} say={r.get('say')}")


def t_what_she_sees_is_plain(j):
    """QA #19 and #24: the browser card showed the agent's English log with em dashes,
    Hebrew said "פתחתי Calculator.", and several steps said "Done." twice."""
    from savta.actions import web
    log = ["consent: Accept all", "only 3 things on the page, waiting for it to finish",
           "waited", "type Where from? = Tel Aviv", "click Search flights  [changed nothing]",
           "nothing moving, closed whatever was on top", "click: no target",
           "click Pay  [refused: completes a purchase]"]
    he = web.shown_steps(log, "hebrew")
    check("the card's steps are in her language", he and all(
        re.search(r"[\u0590-\u05FF]", x) for x in he), str(he))
    check("with no developer words and no dashes", not any(
        w in " ".join(he) for w in ("—", "[", "no target", "consent", "changed nothing")), str(he))
    src = (ROOT / "savta" / "actions" / "web.py").read_text()
    check("no step the agent logs carries an em dash",
          not re.search(r'steps\.append\([^)]*—', src))
    real_run, real_where = web.run, web.where_to_start
    web.where_to_start = lambda j_, task: ("https://example.com", "general")
    try:
        for did in ("done", "partly_done", "needs_payment", "blocked", "stuck"):
            web.run = lambda *a, did=did, **k: {"did": did, "steps": log, "url": "u",
                                               "title": "t", "why": ""}
            for lg in ("english", "russian", "hebrew"):
                reset_state()
                r = router.handle(j, {"english": "book me a table for two at an italian restaurant tonight",
                                      "russian": "забронируй мне столик на двоих в итальянском ресторане сегодня",
                                      "hebrew": "תזמיני לי שולחן לשניים במסעדה איטלקית הערב"}[lg],
                                  speak=False)
                if not r["did"].startswith("web_"):
                    continue
                check(f"web_{did} in {lg} speaks with no em dash",
                      "—" not in (r.get("say") or "") and "—" not in " ".join(
                          (r.get("detail") or {}).get("steps") or []), r.get("say") or "")
    finally:
        web.run, web.where_to_start = real_run, real_where
    check("the Russian read-back has no em dash", "—" not in router._narrate("Мириам", "привет", "russian"))
    reset_state()
    r = router.handle(j, "תפתחי את המחשבון", speak=False)
    check("Hebrew hears the calculator by its Hebrew name",
          r["did"] == "opened_app" and "Calculator" not in (r.get("say") or ""),
          f"did={r['did']} say={r.get('say')}")
    check("steps that each said 'Done.' are said once",
          router._once(["Done.", "I opened Calculator.", "Done."], "english") == ["I opened Calculator."]
          and router._once(["Done.", "Done."], "english") == ["Done."])


def t_one_briefing_even_when_turns_fail(j):
    """tests/test_account.py died of SIGBUS: with the proxy refusing, every turn failed
    after starting the day's briefing and before claiming it, so each turn started
    another, and their copies of the message store overwrote one another's open file."""
    import threading as _th
    reset_state()
    p = prof.load(); p["last_briefed"] = ""; prof.save(p)
    ran = []
    real_brief, real_understand = router.briefing, router.understand

    def counting_briefing(lang):
        ran.append(lang)
        return ""

    def refusing(*a, **k):
        raise KeyError("intent")
    router.briefing, router.understand = counting_briefing, refusing
    router.due_briefing = REAL_DUE_BRIEFING       # this section is what it exercises
    try:
        for _ in range(3):
            try:
                router.handle(j, "what time is it", speak=False)
            except KeyError:
                pass
        time.sleep(0.2)
    finally:
        router.briefing, router.understand = real_brief, real_understand
        check("three failing turns start one briefing, not three", len(ran) == 1, str(ran))
        check("and the day is claimed", not router.due_briefing())
        router.due_briefing = lambda: False       # back to the suite default

    # The store itself: two readers may never copy over each other.
    inside, overlap = [0], []
    real_copy = book._copy_db

    def slow_copy(name):
        inside[0] += 1
        if inside[0] > 1:
            overlap.append(name)
        time.sleep(0.05)
        inside[0] -= 1
        return None
    book._copy_db = slow_copy
    try:
        ts = [_th.Thread(target=f) for f in (REAL_UNREAD, lambda: REAL_READ_RECENT(40),
                                             REAL_UNREAD)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        book._copy_db = real_copy
    check("reads of the message store run one at a time", not overlap, str(overlap))


def t_the_day_and_the_place_she_asked_about(j):
    """"מה מזג האוויר מחר בתל אביב" was answered with today's weather, and a Hebrew town
    name reached wttr to be resolved however it liked ("Al Mas`Udiya")."""
    for utt, lang, want in (
            ("מה מזג האוויר מחר בתל אביב", "hebrew", ("מחר", "21", "28")),
            ("what's the weather tomorrow in London", "english", ("Tomorrow", "21", "28")),
            ("מה מזג האוויר מחרתיים בירושלים", "hebrew", ("מחרתיים", "12", "15")),
            ("what will the weather be in Tokyo the day after tomorrow", "english",
             ("day after tomorrow", "12", "15")),
            ("מה מזג האוויר עכשיו באילת", "hebrew", ("17 מעלות",))):
        reset_state()
        r = router.handle(j, utt, speak=False)
        say = r.get("say") or ""
        check(f"{utt!r} speaks the day she asked about",
              r["did"] == "answered" and all(w in say for w in want), f"did={r['did']} say={say}")
    reset_state()
    r = router.handle(j, "what's the weather in Paris next week", speak=False)
    check("next week is beyond the forecast, and says so",
          r["did"] == "weather_too_far" and "only have" in (r.get("say") or ""),
          f"did={r['did']} say={r.get('say')}")

    london = [{"name": "London", "country": "United Kingdom", "country_code": "GB",
               "admin1": "England", "lat": 51.5, "lon": -0.12, "population": 8961989},
              {"name": "London", "country": "Canada", "country_code": "CA",
               "admin1": "Ontario", "lat": 42.98, "lon": -81.2, "population": 422324}]
    check("the far bigger London is taken without asking",
          router._choose_place(j, london, "what's the weather in London")["country_code"] == "GB")
    portland = [{"name": "Portland", "country": "United States", "country_code": "US",
                 "admin1": "Oregon", "lat": 45.5, "lon": -122.7, "population": 652503},
                {"name": "Portland", "country": "United States", "country_code": "US",
                 "admin1": "Maine", "lat": 43.66, "lon": -70.26, "population": 68408},
                {"name": "Portland", "country": "Australia", "country_code": "AU",
                 "admin1": "Victoria", "lat": -38.3, "lon": 141.6, "population": 9900}]
    got = router._choose_place(j, portland, "what's the weather in Portland Maine")
    check("two real places she could mean: Jev chooses by what she said",
          got["admin1"] == "Maine", str(got))


# Every line the browser agent logs, in the shapes it logs them: tracebacks, labels
# with dashes, refusals. What the card shows is shown_steps() of these.
WEB_LOG_SAMPLES = [
    "out of time after 91s",
    "could not read the page: TargetClosedError('Target page, context or browser has been closed')",
    "only 3 things on the page, waiting for it to finish",
    "nothing moving, closed whatever was on top",
    "nothing moving, went back to the top of the page",
    "dismissed: Close — newsletter",
    "consent: Reject all",
    "thought it was finished, but the goal is not visible yet",
    "nothing usable yet, waiting for the page",
    "waiting for the page to finish loading",
    "waited", "scrolled",
    "click: no target", "type: nothing to enter",
    "click Pay now  [refused: this completes a purchase]",
    "type Card number  [refused: password or payment field]",
    "click Search flights  [TimeoutError('locator.click: Timeout 1500ms exceeded')]",
    "click Search flights  [changed nothing]",
    "click Done  [going round in circles]",
    "click Next  [tried three times, moving on]",
    "type Where from? = Tel Aviv",
    "select Adults = 2",
    "click Flights — cheapest first",
]
_DEV_WORDS = ("could not read", "nothing moving", "things on the page", "consent",
              "dismissed", "no target", "nothing to enter", "Error", "Exception",
              "Timeout", "(", ")", "[", "]", "—", "refused", "changed nothing",
              "round in circles", "tried three", "thought it was", "waited", "scrolled",
              "out of time", "usable yet")


def t_small_things_she_hears(j):
    """QA v1.0.2 re-verify: a tomorrow forecast recorded today's numbers, Hebrew read
    "ל31" as one token, and the browser card could show the agent's own log."""
    j1 = {"current_condition": [{"weatherDesc": [{"value": "Sunny"}], "weatherCode": "113",
                                 "temp_C": "26", "FeelsLikeC": "27"}],
          "nearest_area": [{"areaName": [{"value": "Al Mas`Udiya"}],
                            "latitude": "32.083", "longitude": "34.783"}],
          "weather": [{"date": "d0", "mintempC": "25", "maxtempC": "27",
                       "hourly": [{"weatherCode": "113", "chanceofrain": "3"}] * 8},
                      {"date": "d1", "mintempC": "24", "maxtempC": "31",
                       "hourly": [{"weatherCode": "113", "chanceofrain": "0"}] * 8},
                      {"date": "d2", "mintempC": "-3", "maxtempC": "2",
                       "hourly": [{"weatherCode": "338", "chanceofrain": "80"}] * 8}]}
    cond = facts._parse(j1)
    check("each day is exactly wttr's mintempC and maxtempC",
          [(d["low"], d["high"]) for d in cond["days"]] == [(25, 27), (24, 31), (-3, 2)],
          str(cond["days"]))
    real_at, real_geo = facts.conditions_at, facts.geocode
    facts.conditions_at = lambda lat, lon: facts._parse(j1)
    facts.geocode = lambda name, lang="en": [
        {"name": name, "country": "", "country_code": "IL", "admin1": "",
         "lat": 32.0809, "lon": 34.7806, "population": 432892}]
    try:
        for utt, want, lo, hi in (
                ("מה מזג האוויר מחר בתל אביב", "בין 24 ל-31 מעלות", 24, 31),
                ("what's the weather tomorrow in Tel Aviv", "between 24 and 31", 24, 31),
                ("מה מזג האוויר בתל אביב", "היום בין 25 ל-27", 25, 27)):
            reset_state()
            r = router.handle(j, utt, speak=False)
            d = r.get("detail") or {}
            check(f"{utt!r} says {want!r} and records the same numbers",
                  want in (r.get("say") or "") and (d.get("low"), d.get("high")) == (lo, hi),
                  f"say={r.get('say')} detail low/high={d.get('low')}/{d.get('high')}")
    finally:
        facts.conditions_at, facts.geocode = real_at, real_geo
    frost = router._weather_line(cond, "מוסקבה", "hebrew", 2)
    check("below zero is said, not dashed twice", "ל-2" in frost and "--" not in frost, frost)
    check("no Hebrew bound letter touches a digit in any weather line", not any(
        re.search(r"(?<![\u0590-\u05FF])[בלמוהכש]\d", router._weather_line(cond, "חיפה", "hebrew", k))
        for k in (0, 1, 2)))

    from savta.actions import web
    for lg in ("english", "hebrew"):
        shown = web.shown_steps(WEB_LOG_SAMPLES, lg)
        bad = [x for x in shown if any(w in x for w in _DEV_WORDS)]
        check(f"every logged step reaches the card plain ({lg})", shown and not bad, str(bad))
        if lg == "hebrew":
            check("and in Hebrew", all(re.search(r"[\u0590-\u05FF]", x) for x in shown),
                  str([x for x in shown if not re.search(r"[\u0590-\u05FF]", x)]))
    # A new line logged by the agent must be added to WEB_LOG_SAMPLES (and mapped).
    src = (ROOT / "savta" / "actions" / "web.py").read_text()
    logged = re.findall(r'steps\.append\(\s*f?"([^"{]*)', src)
    unknown = [head for head in logged if head.strip() and not any(
        smp.startswith(head.strip()[:12]) for smp in WEB_LOG_SAMPLES)]
    check("every kind of line the agent logs has a sample here", not unknown, str(unknown))

    # #24, re-checked with stubs.
    reset_state()
    r = router.handle(j, "תפתחי את המחשבון", speak=False)
    check("Hebrew still hears 'פתחתי מחשבון'",
          r["did"] == "opened_app" and "מחשבון" in (r.get("say") or ""), r.get("say") or "")
    real = yt.search
    yt.search = lambda q, n=18: [{"id": "tt", "title": 'טיטאניק | "קרחון, ישר לפנינו!" | Paramount',
                                  "channel": "Paramount Movies", "length": "2:10",
                                  "views": "5M views"}]
    try:
        reset_state()
        r = router.handle(j, "תשימי לי את הטריילר של טיטאניק", speak=False)
    finally:
        yt.search = real
    check("a Hebrew video title is read clean",
          r["did"] == "playing" and (r.get("say") or "").endswith("טיטאניק, קרחון, ישר לפנינו!")
          and "|" not in (r.get("say") or "") and "Paramount" not in (r.get("say") or ""),
          r.get("say") or "")


TESTS = [
    ("0zg. the small things she hears", t_small_things_she_hears),
    ("0zf. the day and the place she asked about", t_the_day_and_the_place_she_asked_about),
    ("0ze. one briefing, even when turns fail", t_one_briefing_even_when_turns_fail),
    ("0y. the weather never comes from a model", t_weather_never_from_a_model),
    ("0z. undo answers in her language", t_undo_answers_in_her_language),
    ("0za. the trailer she asked for", t_the_trailer_she_asked_for),
    ("0zb. answers are in one language", t_answers_are_in_one_language),
    ("0zc. small talk does not hang", t_small_talk_does_not_hang),
    ("0zd. what she sees is plain", t_what_she_sees_is_plain),
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
    ("3b. a follow-up changes only what she changed", t_a_follow_up_keeps_the_message),
    ("3d. a question or a named film over a playing one", t_follow_ups_over_a_playing_film),
    ("3c. a message goes only when she means it", t_a_message_goes_only_when_she_means_it),
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
        # In pure replay mode nothing this pool warms will ever be asked a real
        # question, so skip the TCP+TLS handshake: one less thing the suite needs
        # the network for, and one less thing making it slower for nothing.
        if replay.MODE != "replay" or replay.ALLOW_LIVE:
            j.warmup()
    except Exception as e:  # noqa: BLE001
        print(f"cannot reach Jev: {e!r}\n"
              f"(TYPESAFE_API_KEY must be in the environment or in "
              f"{ROOT.parent / '.env.local'})")
        return 2

    t_all = time.time()
    print(f"MicMic regression suite   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"sends are gated off (MICMIC_ALLOW_SEND unset), osascript is blocked")
    print(f"replay: {replay.MODE}" + (" (ALLOW_LIVE)" if replay.ALLOW_LIVE else "") + "\n")

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
    print(f"{PASSED} passed, {len(FAILED)} failed, {SKIPPED} skipped   "
          f"{j.calls} jev calls  ${j.cost_usd:.5f}  {dt:.1f}s")
    print(f"{replay.summary_line()}")
    print(f"never sent for real: {len(OSA_TOTAL)} osascript calls escaped the stub "
          f"(must be 0); {len(DELIBERATE)} deliberate probes of the real functions; "
          f"{len(LAUNCHED)} launcher calls, all blocked")
    return 1 if (FAILED or OSA_TOTAL) else 0


if __name__ == "__main__":
    code = main()
    _restore_profile()
    sys.exit(code)
