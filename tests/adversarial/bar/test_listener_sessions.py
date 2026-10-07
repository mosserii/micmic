#!/usr/bin/env python3
"""Recognition sessions and the audio engine: no leak, no loop (native/listener.py).

    native/.venv/bin/python3 \\
        tests/adversarial/bar/test_listener_sessions.py

1.2.4 on the owner's Mac reached 560% CPU with ~143 Apple speech sessions alive in the
process. The sample of the same build (2026-10-03) shows why: one serial queue per
session spinning in -[SFLocalSpeechRecognitionClient stopSpeech] -> its XPC error
handler -> os_log_error -> speechRecordingDidFail. stopSpeech is what endAudio() sends,
and stop_task() called endAudio() on EVERY session it recycled, including the ones
Apple had already closed (silence 1110, a final, our own cancel()).

FakeSpeech below models exactly that: endAudio() on a session Apple has closed makes
that session a "spinner" forever. Everything is fake: no microphone, no recogniser, no
AVAudioEngine, no server, no network. Requires the native/.venv interpreter only because
listener.py imports the PyObjC frameworks at module level.
"""
from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "native"))

import os as _os  # noqa: E402
import tempfile as _tempfile  # noqa: E402
_os.environ["MICMIC_ASR_NO_DOWNLOAD"] = "1"
_os.environ["MICMIC_ASR_DIR"] = _tempfile.mkdtemp(prefix="listener-sessions-asr-")
import listener as listenermod  # noqa: E402
from listener import Listener  # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


# Nothing here may reach the owner's MicMic on 8799, speak, or touch the bar.
_DEAD = "http://127.0.0.1:9"
listenermod.SERVER = _DEAD
listenermod.UTTERANCE_URL = _DEAD + "/api/utterance"
listenermod.CONFIG_URL = _DEAD + "/api/config?contacts=1"
listenermod.LOGIN_ITEM_URL = _DEAD + "/api/login_item_status"
listenermod.post_turn_open = lambda ts, kind: None
listenermod.post_turn_closed = lambda ts, why: {"did": "nothing_held"}
listenermod.post_login_item_status = lambda status: None
listenermod.server_duck = lambda on: None
listenermod.play_sound = lambda path: None
listenermod.stop_speaking = lambda: None
listenermod.speak_local = lambda *a, **k: None
listenermod.check_server_health = lambda: True
LOGS: list[str] = []
listenermod.log = lambda msg: LOGS.append(msg)
listenermod.on_main = lambda fn: fn()          # this test is the main thread throughout


# ------------------------------------------------------------------ Apple, faked
STARTING, RUNNING, FINISHING, CANCELING, COMPLETED = 0, 1, 2, 3, 4


class FakeError:
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def domain(self):
        return "kAFAssistantErrorDomain"

    def localizedDescription(self):
        return "fake"


class FakeTranscription:
    def __init__(self, text):
        self._t = text

    def formattedString(self):
        return self._t

    def segments(self):
        return []


class FakeResult:
    def __init__(self, text, final):
        self._tr, self._final = FakeTranscription(text), final

    def bestTranscription(self):
        return self._tr

    def isFinal(self):
        return self._final


class FakeRequest:
    """SFSpeechAudioBufferRecognitionRequest. endAudio() on a session Apple has closed
    is the 1.2.4 bug: that session's client spins forever."""
    def __init__(self, speech):
        self.speech, self.task = speech, None
        self.ends = 0
        self.buffers = 0
        self.buffers_after_close = 0

    # Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
    def init(self):
        return self

    def setShouldReportPartialResults_(self, v):
        pass

    def setRequiresOnDeviceRecognition_(self, v):
        pass

    def setContextualStrings_(self, v):
        pass

    def appendAudioPCMBuffer_(self, buf):
        self.buffers += 1
        if self.task is not None and self.task.st >= CANCELING:
            self.buffers_after_close += 1

    def endAudio(self):
        self.ends += 1
        t = self.task
        if t is None:
            return
        if t.st >= CANCELING:
            self.speech.spinners.add(t.n)          # stopSpeech on a closed client
        elif t.st < FINISHING:
            t.st = FINISHING
            self.speech.pending.append((t, "final"))


class FakeTask:
    def __init__(self, speech, n, req, handler):
        self.speech, self.n, self.req, self.handler = speech, n, req, handler
        self.st = STARTING
        self.cancels = 0

    def state(self):
        return self.st

    def cancel(self):
        self.cancels += 1
        if self.st >= CANCELING:
            return
        self.st = CANCELING
        self.speech.pending.append((self, "cancelled"))


class FakeSpeech:
    """The recogniser plus Apple's side of every session it opened. Callbacks Apple owes
    (a cancel's error, an ended session's final) wait in `pending` until pump(), the way
    they wait for the main queue in the app."""
    def __init__(self, confirm=True):
        self.tasks: list[FakeTask] = []
        self.pending: list[tuple] = []
        self.spinners: set[int] = set()
        self.confirm = confirm                       # False: Apple never answers a cancel
        self.on_open = None                          # hook: what Apple does at once

    # Speech.SFSpeechAudioBufferRecognitionRequest
    def alloc(self):
        return FakeRequest(self)

    def supportsOnDeviceRecognition(self):
        return False

    def recognitionTaskWithRequest_resultHandler_(self, req, handler):
        t = FakeTask(self, len(self.tasks), req, handler)
        req.task = t
        self.tasks.append(t)
        t.st = RUNNING
        if self.on_open is not None:
            self.on_open(t)
        return t

    def pump(self):
        while self.pending:
            t, what = self.pending.pop(0)
            if what == "cancelled":
                if not self.confirm:
                    continue
                t.st = COMPLETED
                t.handler(None, FakeError(301))
            elif what == "final":
                t.st = COMPLETED
                t.handler(FakeResult("", True), None)
            elif what == "ends" and t.st < CANCELING:
                self.apple_ends(t)

    def apple_ends(self, t, code=1110):
        """Apple closes a session on its own: 'no speech detected' in a quiet room."""
        t.st = COMPLETED
        t.handler(None, FakeError(code))

    def apple_final(self, t, text="hello"):
        t.st = COMPLETED
        t.handler(FakeResult(text, True), None)

    def live(self):
        return sum(1 for t in self.tasks if t.st < CANCELING)


class FakeNode:
    def __init__(self):
        self.taps = 0

    def removeTapOnBus_(self, bus):
        self.taps -= 1


class FakeEngine:
    def __init__(self):
        self.node = FakeNode()
        self.node.taps = 1
        self.stops = 0

    def inputNode(self):
        return self.node

    def stop(self):
        self.stops += 1


REAL_SPEECH = listenermod.Speech


def session_listener(confirm=True):
    speech = FakeSpeech(confirm=confirm)
    listenermod.Speech = types.SimpleNamespace(SFSpeechAudioBufferRecognitionRequest=speech)
    lst = Listener()
    lst.bar = lst.panel = None
    lst.cfg = {}
    lst.locale = "en-US"
    lst.recognizer = speech
    lst.running = True
    lst.set_status = lambda text, glyph="": None
    lst.announce_failure = lambda key, **kw: None
    lst.schedule_restart = lambda delay=0.3: None
    lst.send = lambda *a, **k: None
    return lst, speech


def feed(lst):
    """What the audio tap does with one buffer."""
    req = lst.request
    if req is not None:
        req.appendAudioPCMBuffer_(object())


# ------------------------------------------------------------------ the tests
def test_the_fake_reproduces_the_1_2_4_leak():
    """The old stop_task (cancel() then endAudio(), on every session) against the same
    fake: it must leak, or the tests below prove nothing."""
    lst, speech = session_listener()

    def old_stop_task():
        with lst.lock:
            lst.gen += 1
            if lst.task is not None:
                lst.task.cancel()
            if lst.request is not None:
                lst.request.endAudio()
            lst.task = None
            lst.request = None
    lst.stop_task = old_stop_task
    for i in range(30):
        lst.start_task(force=True)
        t = speech.tasks[-1]
        if i % 3 == 0:
            speech.apple_ends(t)                     # silence: Apple closed it (1110)
        elif i % 3 == 1:
            speech.apple_final(t)                    # wake mode: one utterance, final
        speech.pump()
    lst.start_task(force=True)
    speech.pump()
    # Every session the old stop_task cancelled while live (one in three here) leaks:
    # cancel() then endAudio() on the now-closed client. (The 1110 and final paths
    # leaked too in 1.2.4; the new on_result already detaches their request, so this
    # old stop_task no longer reaches them.)
    check("the fake reproduces 1.2.4: the old stop_task leaks a spinning session per cancel",
          len(speech.spinners) >= 10, len(speech.spinners))
    listenermod.Speech = REAL_SPEECH


def test_n_restarts_leave_at_most_one_live_task():
    old_burst = listenermod.START_BURST
    listenermod.START_BURST = 10 ** 6                # the loop guard has its own test
    try:
        _n_restarts()
    finally:
        listenermod.START_BURST = old_burst


def _n_restarts():
    for confirm_every in (1, 5, 0):                  # Apple answers at once, late, never
        LOGS.clear()
        lst, speech = session_listener(confirm=bool(confirm_every))
        worst_live, worst_tracked = 0, 0
        n = 300
        for i in range(n):
            lst.start_task(force=True)
            feed(lst)
            if speech.tasks and i % 7 == 3:
                speech.apple_ends(speech.tasks[-1])  # a silent session Apple closes itself
            if speech.tasks and i % 11 == 5:
                speech.apple_final(speech.tasks[-1])
            if confirm_every and i % confirm_every == 0:
                speech.pump()
            worst_live = max(worst_live, speech.live())
            worst_tracked = max(worst_tracked, len(lst.sessions))
            check_live = lst.live_sessions()
            if check_live > 1:
                break
        speech.pump()
        label = {1: "Apple confirms at once", 5: "Apple confirms late",
                 0: "Apple never confirms"}[confirm_every]
        check(f"{n} restarts ({label}): never more than one live task",
              worst_live <= 1 and lst.live_sessions() <= 1, (worst_live, lst.live_sessions()))
        check(f"{n} restarts ({label}): no session is ever ended after Apple closed it",
              not speech.spinners, sorted(speech.spinners)[:10])
        check(f"{n} restarts ({label}): no buffer reaches a closed session",
              sum(t.req.buffers_after_close for t in speech.tasks) == 0)
        check(f"{n} restarts ({label}): sessions tracked stay bounded",
              worst_tracked <= listenermod.SESSION_CAP + 2, worst_tracked)
        if confirm_every:
            check(f"{n} restarts ({label}): every restart really opened a session",
                  len(speech.tasks) == n, len(speech.tasks))
        else:
            waiting = sum(t.st == CANCELING for t in speech.tasks)
            check(f"{n} restarts ({label}): unconfirmed cancels stop at the cap, logged",
                  waiting <= listenermod.SESSION_CAP + 1
                  and any("not yet released by Apple" in m for m in LOGS),
                  (waiting, len(speech.tasks)))
        listenermod.Speech = REAL_SPEECH


def test_every_restart_releases_the_previous_session():
    lst, speech = session_listener()
    lst.start_task(force=True)
    first = speech.tasks[-1]
    first_req = lst.request
    lst.start_task(force=True)
    check("a restart cancels the previous live task",
          first.cancels == 1 and first.st == CANCELING, (first.cancels, first.st))
    check("a restart detaches the previous request from the tap",
          lst.request is not first_req and lst.request is speech.tasks[-1].req)
    check("and never sends endAudio to the one it cancelled", first_req.ends == 0, first_req.ends)
    speech.pump()
    check("a confirmed cancel is forgotten", 0 not in lst.sessions, list(lst.sessions))
    speech.apple_ends(speech.tasks[-1])
    lst.start_task(force=True)
    check("a session Apple already closed is neither cancelled nor ended",
          speech.tasks[1].cancels == 0 and speech.tasks[1].req.ends == 0,
          (speech.tasks[1].cancels, speech.tasks[1].req.ends))
    listenermod.Speech = REAL_SPEECH


def test_finishing_a_turn_ends_audio_once_on_the_live_session():
    LOGS.clear()
    lst, speech = session_listener()
    lst.turn = {"kind": "ptt", "down_at": time.time(), "mode": "toggle", "finish_at": 0.0,
                "deadline": time.time() + 30, "open_ts": time.time(), "src": "key",
                "key_at": 0.0, "heard_s": 0.0, "voiced_s": 0.0, "peak_rms": 0.0}
    lst.start_task(force=True)
    t = speech.tasks[-1]
    lst.finish_turn("pressed again")
    lst.end_audio()                                  # a second ask is a no-op
    check("finishing a turn sends endAudio exactly once", t.req.ends == 1, t.req.ends)
    speech.pump()                                    # Apple's final
    lst.end_audio()
    check("never again after Apple's final", t.req.ends == 1 and not speech.spinners,
          (t.req.ends, speech.spinners))
    lst.turn = None
    lst.start_task(force=True)
    check("the finished session is not cancelled on the next start", t.cancels == 0, t.cancels)
    listenermod.Speech = REAL_SPEECH


def test_session_cap_logs_and_holds_back():
    LOGS.clear()
    lst, speech = session_listener(confirm=False)    # Apple never answers a cancel
    old_forget = listenermod.SESSION_FORGET
    listenermod.SESSION_FORGET = 0.3
    try:
        for _ in range(50):
            lst.start_task(force=True)
        cap = listenermod.SESSION_CAP
        check("with cancels never confirmed, sessions opened stop at the cap",
              len(speech.tasks) <= cap + 2, len(speech.tasks))
        check("and the cap is logged",
              any("not yet released by Apple" in m for m in LOGS), LOGS[-3:])
        check("the open session stays open while held back", lst.live_sessions() == 1,
              lst.live_sessions())
        time.sleep(0.4)
        lst.session_hold_until = 0.0
        before = len(speech.tasks)
        lst.start_task(force=True)
        check("unconfirmed ones are dropped from the count after SESSION_FORGET, with a log",
              len(speech.tasks) == before + 1
              and any("never confirmed its end" in m for m in LOGS), (before, len(speech.tasks)))
    finally:
        listenermod.SESSION_FORGET = old_forget
        listenermod.Speech = REAL_SPEECH


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_cpu_loop_guard():
    """Every session Apple opens dies at once and something restarts it at once (the
    turn's final->restart path is forced, past the 0.7 s rate limit). Without a guard
    that is a busy loop; with it, at most START_BURST a minute, logged, and held back."""
    LOGS.clear()
    lst, speech = session_listener()
    speech.on_open = lambda t: speech.apple_ends(t, 1110)
    real_time = listenermod.time
    clock = FakeClock()
    listenermod.time = clock
    try:
        started = real_time.process_time()
        for _ in range(5000):                        # 5000 restarts, 10 ms apart: 50 s
            lst.start_task(force=True)
            speech.pump()
            clock.now += 0.01
        cpu = real_time.process_time() - started
        burst = listenermod.START_BURST
        check("a restart loop opens at most START_BURST sessions a minute",
              len(speech.tasks) <= burst + 1, len(speech.tasks))
        check("the loop is logged", any("a restart loop" in m for m in LOGS), LOGS[-3:])
        holds = [m for m in LOGS if "a restart loop" in m]
        check("and logged once per hold, not per attempt", len(holds) <= 10, len(holds))
        check("no session was ended after Apple closed it", not speech.spinners)
        check("5000 refused restarts cost well under a second of CPU", cpu < 1.0, cpu)
        clock.now += listenermod.START_WINDOW + listenermod.SESSION_HOLD_MAX
        before = len(speech.tasks)
        speech.on_open = None
        lst.start_task(force=True)
        check("after the window, sessions open again", len(speech.tasks) == before + 1,
              (before, len(speech.tasks)))
    finally:
        listenermod.time = real_time
        listenermod.Speech = REAL_SPEECH


def watchdog_listener(alive_after_rebuild):
    lst, speech = session_listener()
    lst.engine = FakeEngine()
    lst.tap_installed = True
    lst.audio_at = time.time()
    lst.engine_started_at = time.time()
    lst.last_health_check = lst.last_hotkey_check = lst.last_debug = time.time()
    lst.last_settings_poll = time.time()
    lst.refresh_settings = lambda: None
    lst.update_hotkey_status = lambda: None
    lst.consider = lambda final=False: None
    engines: list[FakeEngine] = [lst.engine]

    def fake_start_engine():
        e = FakeEngine()
        engines.append(e)
        lst.engine = e
        lst.tap_installed = True
        lst.engine_started_at = time.time()
        if alive_after_rebuild:
            lst.audio_at = time.time()
        return True
    lst.start_engine = fake_start_engine
    return lst, speech, engines


def test_engine_rebuild_releases_tap_engine_and_session():
    LOGS.clear()
    lst, speech, engines = watchdog_listener(alive_after_rebuild=True)
    lst.start_task(force=True)
    old_task = speech.tasks[-1]
    lst.recover_engine("test")
    old = engines[0]
    check("the old engine's tap is removed", old.node.taps == 0, old.node.taps)
    check("the old engine is stopped", old.stops == 1, old.stops)
    check("the old session is cancelled, not ended",
          old_task.cancels == 1 and old_task.req.ends == 0, (old_task.cancels, old_task.req.ends))
    check("one fresh session runs on the new engine",
          speech.live() == 1 and speech.tasks[-1] is not old_task, speech.live())
    check("the rebuild flag is cleared", lst.recovering is False)
    # A turn opening on a stale engine opens its own session: the rebuild does not.
    n = len(speech.tasks)
    lst.recover_engine("no audio when a turn opened", session=False)
    check("a rebuild for a turn leaves the session to the turn",
          len(speech.tasks) == n and speech.live() == 0, (len(speech.tasks), speech.live()))
    listenermod.Speech = REAL_SPEECH


def test_dead_engine_rebuilds_are_bounded_and_sessions_wait():
    LOGS.clear()
    lst, speech, engines = watchdog_listener(alive_after_rebuild=False)
    lst.start_task(force=True)
    olds = (listenermod.ENGINE_STALL, listenermod.ENGINE_STALL_MAX, listenermod.TICK)
    listenermod.ENGINE_STALL, listenermod.ENGINE_STALL_MAX, listenermod.TICK = 0.02, 0.08, 0.005
    lst.audio_at = time.time() - 1.0
    lst.engine_started_at = time.time() - 1.0
    th = threading.Thread(target=lst.watchdog, daemon=True)
    try:
        th.start()
        time.sleep(1.5)
    finally:
        lst.running = False
        th.join(timeout=2.0)
        listenermod.ENGINE_STALL, listenermod.ENGINE_STALL_MAX, listenermod.TICK = olds
    rebuilds = [m for m in LOGS if m.startswith("rebuilding audio engine")]
    cap = listenermod.ENGINE_REBUILD_MAX
    check("a dead engine is rebuilt at most ENGINE_REBUILD_MAX times in a row",
          len(rebuilds) == cap, (len(rebuilds), cap))
    check("then it says so, once",
          sum("not rebuilding again" in m for m in LOGS) == 1, LOGS[-3:])
    check("every old tap was removed and every old engine stopped",
          all(e.node.taps == 0 and e.stops == 1 for e in engines[:-1]),
          [(e.node.taps, e.stops) for e in engines])
    check("sessions are not recycled on a dead engine: one per rebuild at most",
          len(speech.tasks) <= cap + 1, len(speech.tasks))
    check("never more than one live task", speech.live() <= 1, speech.live())
    check("no session ended after Apple closed it", not speech.spinners)
    # A wake (new hardware) starts over.
    lst.audio_at = time.time()
    lst.recover_engine("system wake from sleep", fresh=True)
    check("a wake resets the count", lst.stall_rebuilds == 0 and not lst.engine_gave_up,
          (lst.stall_rebuilds, lst.engine_gave_up))
    listenermod.Speech = REAL_SPEECH


def test_rebuild_is_never_queued_twice_behind_a_blocked_main_thread():
    """1.2.4, 2026-10-07: an engine start blocked the main thread for 7 minutes
    (08:27:57 -> 08:34:50); the watchdog queued another rebuild meanwhile and it ran
    straight after (08:34:50.364 rebuilt, .376 rebuilding again)."""
    LOGS.clear()
    lst, speech, engines = watchdog_listener(alive_after_rebuild=False)
    queued: list = []
    real_on_main = listenermod.on_main
    listenermod.on_main = lambda fn: queued.append(fn)      # the main thread is stuck
    olds = (listenermod.ENGINE_STALL, listenermod.ENGINE_STALL_MAX, listenermod.TICK)
    listenermod.ENGINE_STALL, listenermod.ENGINE_STALL_MAX, listenermod.TICK = 0.02, 0.04, 0.005
    lst.audio_at = lst.engine_started_at = time.time() - 1.0
    th = threading.Thread(target=lst.watchdog, daemon=True)
    try:
        th.start()
        time.sleep(0.6)
    finally:
        lst.running = False
        th.join(timeout=2.0)
        listenermod.on_main = real_on_main
        listenermod.ENGINE_STALL, listenermod.ENGINE_STALL_MAX, listenermod.TICK = olds
    check("one rebuild queued while the main thread is blocked, not one per backoff step",
          len(queued) == 1, len(queued))
    for fn in queued:
        fn()
    check("and it clears the flag when it finally runs", lst.recovering is False)
    listenermod.Speech = REAL_SPEECH


def test_watchdog_cannot_spin_sessions():
    """The real watchdog, ticking fast, while every session Apple opens dies at once:
    the 0.7 s rate limit holds it to a couple of sessions a second."""
    LOGS.clear()
    lst, speech, engines = watchdog_listener(alive_after_rebuild=True)
    speech.on_open = lambda t: speech.pending.append((t, "ends"))   # answered next pump
    old_tick = listenermod.TICK
    listenermod.TICK = 0.002
    th = threading.Thread(target=lst.watchdog, daemon=True)

    def pulse():
        while lst.running:
            lst.audio_at = time.time()               # the engine is alive
            speech.pump()                            # Apple's callbacks, on "main"
            time.sleep(0.01)
    pt = threading.Thread(target=pulse, daemon=True)
    try:
        pt.start()
        th.start()
        time.sleep(2.0)
    finally:
        lst.running = False
        th.join(timeout=2.0)
        pt.join(timeout=1.0)
        listenermod.TICK = old_tick
    check("2 s of a dying-session loop opens at most 4 sessions",
          1 <= len(speech.tasks) <= 4, len(speech.tasks))
    check("and leaks none", not speech.spinners and speech.live() <= 1,
          (speech.spinners, speech.live()))
    listenermod.Speech = REAL_SPEECH


def main():
    for t in (test_the_fake_reproduces_the_1_2_4_leak,
              test_n_restarts_leave_at_most_one_live_task,
              test_every_restart_releases_the_previous_session,
              test_finishing_a_turn_ends_audio_once_on_the_live_session,
              test_session_cap_logs_and_holds_back,
              test_cpu_loop_guard,
              test_engine_rebuild_releases_tap_engine_and_session,
              test_dead_engine_rebuilds_are_bounded_and_sessions_wait,
              test_rebuild_is_never_queued_twice_behind_a_blocked_main_thread,
              test_watchdog_cannot_spin_sessions):
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran to the end", False, repr(e)[:400])
        finally:
            listenermod.Speech = REAL_SPEECH
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
