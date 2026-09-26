#!/usr/bin/env python3
"""Adversarial cases for native/listener.py's Listener <-> bar wiring (display_mode,
view_attention/view_heard/view_result/view_idle/view_hide, set_status's glyph filter).

    native/.venv/bin/python3 \\
        tests/adversarial/bar/test_listener_wiring.py

Requires the native/.venv interpreter (listener.py imports AVFoundation/Speech, which
are only installed there); this file puts THIS worktree's native/ first on sys.path,
exactly like tests/bar/test_bar_native.py does for bar.py.

Listener() itself does no AppKit/AVFoundation work in __init__ (see the class body:
it only sets plain attributes), so it is built directly, with no NSApplication and no
run loop. Its own bar/panel are replaced with fakes that record every call, so what is
under test is the WIRING (which method calls which, with what arguments, under what
guard), not bar.py or panel.py themselves (those have their own suites).

`on_main()` normally queues its block on the main run loop via
NSOperationQueue.mainQueue().addOperationWithBlock_, which nothing here is pumping, so
without a run loop the block would sit queued and never run. It is monkeypatched to
run its argument immediately and synchronously, in-process, which is call-order
faithful for every method under test here (none of them yields between "decide to call
on_main" and the call itself). Where infeasible, it is reported inline, not silently
skipped.

Nothing here opens the microphone, touches Accessibility, or reaches the network:
watchdog()'s own network/engine/speech side effects are stubbed out by hand for the one
test that runs it for real (test_armed_window_expiry_via_watchdog).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "native"))

import listener as listenermod  # noqa: E402
from listener import Listener, L  # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


# Every on_main() call in listener.py is call-order faithful when run synchronously:
# nothing here is asynchronous work that needs a real run loop, only "run this on the
# main thread instead of a speech callback's thread" — and this test IS the main
# thread throughout.
listenermod.on_main = lambda fn: fn()

# Nothing here may reach a server: the live app answers on 8799. Every turn the tests
# open is recorded here instead of being POSTed to /api/turn_open.
TURN_OPENS: list[float] = []
OPEN_KINDS: list[str] = []
listenermod.post_turn_open = lambda ts, kind: TURN_OPENS.append(ts) or OPEN_KINDS.append(kind)
TURN_CLOSES: list[tuple] = []
CLOSE_REPLY: dict = {"did": "nothing_held"}
listenermod.post_turn_closed = lambda ts, why: TURN_CLOSES.append((ts, why)) or dict(CLOSE_REPLY)
listenermod.server_duck = lambda on: None
# Nothing in this module may reach a real MicMic. The default server is 127.0.0.1:8799,
# which on the owner's Mac is the app he is using: a login-item status posted from
# here once overwrote his real one (2026-09-26). Every URL points at a closed port.
_DEAD = "http://127.0.0.1:9"
listenermod.SERVER = _DEAD
listenermod.UTTERANCE_URL = _DEAD + "/api/utterance"
listenermod.CONFIG_URL = _DEAD + "/api/config?contacts=1"
listenermod.LOGIN_ITEM_URL = _DEAD + "/api/login_item_status"
LOGIN_POSTS: list[str] = []
listenermod.post_login_item_status = lambda status: LOGIN_POSTS.append(status)


class FakeBar:
    def __init__(self):
        self.calls: list[tuple] = []
        self._visible = False

    def set_state(self, state, text=""):
        self.calls.append(("set_state", state, text))

    def set_heard(self, text):
        self.calls.append(("set_heard", text))

    def set_result(self, say, undo_label):
        self.calls.append(("set_result", say, undo_label))

    def show(self):
        self.calls.append(("show",))
        self._visible = True

    def hide(self):
        self.calls.append(("hide",))
        self._visible = False

    def is_visible(self):
        return self._visible

    def names(self):
        return [c[0] for c in self.calls]


class FakePanel:
    def __init__(self):
        self.calls: list[tuple] = []

    def show(self, activate=True):
        self.calls.append(("show", activate))

    def set_state(self, state, text=""):
        self.calls.append(("set_state", state, text))

    def set_heard(self, text):
        self.calls.append(("set_heard", text))

    def set_reply(self, text):
        self.calls.append(("set_reply", text))


def fresh_listener(bar=None, panel=None, cfg=None):
    lst = Listener()
    lst.bar = bar
    lst.panel = panel
    lst.cfg = cfg if cfg is not None else {}
    lst.locale = "en-US"
    return lst


# ---------------------------------------------------------------- full turn
def test_full_turn_result_never_clobbered():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)

    lst.view_attention()
    check("attention: bar told to listen", bar.calls[:2] == [("set_state", "listening", ""), ("show",)],
          bar.calls)

    for w in ("turn", "turn the", "turn the volume", "turn the volume down"):
        lst.view_heard(w)
    check("live words: every partial reaches the bar, in order",
          [c for c in bar.calls if c[0] == "set_heard"] ==
          [("set_heard", w) for w in ("turn", "turn the", "turn the volume", "turn the volume down")],
          bar.calls)

    before_thinking = len(bar.calls)
    lst.set_status(L("thinking", lst.locale), "◐")   # "◐" == "◐"
    check("thinking (bar already visible) reaches the bar",
          bar.calls[before_thinking:] == [("set_state", "thinking", L("thinking", lst.locale))],
          bar.calls[before_thinking:])

    lst.view_result("Turned it down.", "Undo")
    check("the result reaches the bar", bar.calls[-1] == ("set_result", "Turned it down.", "Undo"),
          bar.calls[-1])
    after_result = len(bar.calls)

    # "◉" follows every answer; must never reach the bar (it would show as an
    # unlabelled state and, worse, is not in GLYPH_STATE's whitelist for the bar at
    # all — see native/listener.py:839-846).
    lst.set_status(L("listening", lst.locale), "◉")   # "◉"
    check("'listening ◉' status after the result does not touch the bar at all",
          len(bar.calls) == after_result, bar.calls[after_result:])

    # "◼" (a send countdown) must not wipe the result either.
    lst.set_status(L("countdown", lst.locale, s=6), "◼")  # "◼"
    check("'countdown ◼' status after the result does not touch the bar at all",
          len(bar.calls) == after_result, bar.calls[after_result:])

    check("net effect: set_result is still the LAST mutating call the bar received",
          bar.calls[-1] == ("set_result", "Turned it down.", "Undo"), bar.calls[-1])


def test_thinking_before_shown_does_not_reach_a_hidden_bar():
    """"◐" is only mirrored to the bar it is already visible (native/listener.py:842:
    `glyph == "⚠" or self.bar.is_visible()`); an unusual order (thinking status set
    before view_attention ever showed the bar) must not pop it up early or silently."""
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.set_status(L("thinking", lst.locale), "◐")
    check("thinking status with the bar not yet visible reaches neither set_state nor show",
          bar.calls == [], bar.calls)


def test_empty_answer_goes_idle():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.view_attention()
    lst.view_result("", None)
    check("an empty answer (ignored / not for us) sends the bar to idle, not a blank result",
          bar.calls[-1] == ("set_state", "idle", ""), bar.calls[-1])


def test_stop_word_hides():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.view_attention()
    lst.view_hide()
    check("view_hide() (a stop word mid-answer) hides the bar", bar.calls[-1] == ("hide",))


def test_error_shows_even_if_bar_was_hidden():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    check("bar starts hidden", not bar.is_visible())
    lst.set_status("The server is not answering", "⚠")   # "⚠"
    check("an error reaches the bar even though it was never shown",
          ("set_state", "error", "The server is not answering") in bar.calls, bar.calls)
    check("an error also SHOWS the bar (native/listener.py:844-845)",
          bar.calls[-1] == ("show",), bar.calls)


def test_display_none_gets_nothing():
    bar, panel = FakeBar(), FakePanel()
    lst = fresh_listener(bar=bar, panel=panel, cfg={"display": "none"})
    lst.view_attention()
    lst.view_heard("hello")
    lst.view_result("Done.", None)
    check("display=none: the bar sees nothing from attention/heard/result",
          bar.calls == [], bar.calls)
    check("display=none: the panel sees nothing either", panel.calls == [], panel.calls)


def test_view_idle_and_view_hide_bypass_display_mode():
    """OBSERVATION, not a crash: view_attention/view_heard/view_result all gate on
    display_mode() (native/listener.py:790-814), but view_idle() and view_hide()
    (:816-822) touch self.bar unconditionally whenever it is not None, with no
    display_mode() check at all. In the ordinary case this is harmless (in "none"
    mode the bar was never shown, so idle/hide land on an already-idle, already-
    hidden bar) -- but it means the display=="none" contract is enforced in three
    places out of five, not centrally in one, so a FUTURE view_* method (or a
    display setting changed to "none" mid-turn while the bar is still up from
    before the change) is one easy-to-miss `if self.bar is not None` away from
    quietly popping the bar open again despite "none" being selected. Flagged for
    the integrator to judge intent; not counted as a failure here."""
    bar = FakeBar()
    lst = fresh_listener(bar=bar, cfg={"display": "none"})
    lst.view_idle()
    lst.view_hide()
    # Now gated like the other view_* calls: "none" means the bar is never touched.
    check("view_idle()/view_hide() leave the bar alone when display='none'",
          bar.calls == [], bar.calls)


def test_display_panel_shows_without_activating():
    panel = FakePanel()
    lst = fresh_listener(bar=None, panel=panel, cfg={"display": "panel"})
    lst.view_attention()
    check("display=panel: the panel is shown with activate=False (must not steal focus)",
          panel.calls == [("show", False)], panel.calls)


def test_display_bar_falls_back_to_panel_when_no_bar_exists():
    """"never leave her with nothing" (native/listener.py:786-788): if bar mode is
    requested/default but no MicMicBar was built (e.g. bar.py failed to import),
    display_mode() must fall back to panel rather than silently showing nothing."""
    panel = FakePanel()
    lst = fresh_listener(bar=None, panel=panel, cfg={"display": "bar"})
    check("display_mode() falls back to panel when self.bar is None",
          lst.display_mode() == "panel", lst.display_mode())
    lst.view_attention()
    check("...and view_attention() actually reaches the panel in that fallback",
          panel.calls == [("show", False)], panel.calls)


def test_display_bar_default_when_unset():
    bar = FakeBar()
    lst = fresh_listener(bar=bar, cfg={})
    check("no 'display' key at all defaults to 'bar'", lst.display_mode() == "bar")


def test_view_heard_ignores_empty_text():
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.view_heard("")
    check("view_heard('') does not call the bar at all (native/listener.py:805: `if ... and text`)",
          bar.calls == [], bar.calls)


# ---------------------------------------------------------------- watchdog: armed-window expiry
def test_armed_window_expiry_via_watchdog():
    """Runs the REAL watchdog() loop in a background thread, with every dangerous
    side effect (network health checks, settings refresh, Accessibility probing,
    audio-engine recovery, starting a real recognition task) stubbed to a no-op, so
    only the stale-arm -> view_idle() path (native/listener.py:1596-1609) is real."""
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.running = True
    lst.paused = False
    lst.muted_until = 0.0
    lst.task = object()                 # not None, so `missing` is False
    lst.task_started = time.time()      # not stale by TASK_MAX
    lst.transcript = ""                 # nothing spoken
    lst.armed_until = time.time() - 1.0   # already expired
    lst.countdown_until = 0.0
    lst.last_health_check = time.time()
    lst.last_hotkey_check = time.time()
    lst.last_buffers_at = time.time()
    lst.last_debug = time.time()

    # Defence in depth: even if a timing edge lets one of the guarded blocks above
    # run anyway, none of these may do real work in a test process.
    listenermod.check_server_health = lambda: True
    lst.refresh_settings = lambda: None
    lst.update_hotkey_status = lambda: None
    lst.start_task = lambda: None
    lst.consider = lambda final=False: None
    lst.recover_engine = lambda why: None
    listenermod.play_sound = lambda path: None
    listenermod.server_duck = lambda on: None

    th = threading.Thread(target=lst.watchdog, daemon=True)
    th.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and ("set_state", "idle", "") not in bar.calls:
        time.sleep(0.05)
    lst.running = False
    th.join(timeout=2.0)

    check("an expired armed window sends the bar to idle via view_idle()",
          ("set_state", "idle", "") in bar.calls, bar.calls)
    check("armed_until is cleared so the window is not reported stale forever",
          lst.armed_until == 0.0, lst.armed_until)
    check("the watchdog thread actually stopped when asked", not th.is_alive())


# ---------------------------------------------------------------- turns that lose words
# The owner's 1.0.1 log: "turn open (ptt)", ~100 s later "turn finishing: pressed
# again", "turn: he-IL 0.00 (final), audio=kept, second language will read it", "turn
# ended with nothing said", while the bar had been showing "מה השעה מיקמק". These
# drive the REAL press/finish_turn/on_result/start_task/_consider_turn/_pick_transcript
# with fake Speech results: no microphone, no recogniser, no server.
class FakeSeg:
    def __init__(self, c):
        self._c = c

    def confidence(self):
        return self._c


class FakeTranscription:
    def __init__(self, text, conf):
        self._t, self._conf = text, conf

    def formattedString(self):
        return self._t

    def segments(self):
        return [FakeSeg(self._conf) for _ in self._t.split()]


class FakeResult:
    def __init__(self, text, final=False, conf=0.0):
        self._tr, self._final = FakeTranscription(text, conf), final

    def bestTranscription(self):
        return self._tr

    def isFinal(self):
        return self._final


class FakeError:
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def domain(self):
        return "kAFAssistantErrorDomain"

    def localizedDescription(self):
        return "No speech detected"


class FakeTask:
    def cancel(self):
        pass

    def state(self):
        return 4


class FakeRecognizer:
    """start_task() builds a real SFSpeechAudioBufferRecognitionRequest (inert without
    audio) and hands it here; the handler is simply dropped: tests call on_result."""
    def __init__(self):
        self.sessions = 0

    def supportsOnDeviceRecognition(self):
        return False

    def recognitionTaskWithRequest_resultHandler_(self, req, handler):
        self.sessions += 1
        return FakeTask()


LOGS: list[str] = []


def turn_listener(alt=None):
    """A running listener with a fake recogniser. `alt`, when given, is what the
    second-language read of the kept audio returns: (text, confidence)."""
    for name in ("play_sound", "server_duck"):
        setattr(listenermod, name, lambda *a, **k: None)
    listenermod.stop_speaking = lambda: None
    listenermod.log = lambda msg: LOGS.append(msg)
    bar = FakeBar()
    lst = fresh_listener(bar=bar)
    lst.locale = "he-IL"
    lst.alt_locale = "en-US"
    lst.running = True
    lst.recognizer = FakeRecognizer()
    sent: list[tuple] = []
    lst.send = lambda text, activation="wake", released_at=0.0, turn=None, asr_confidence=None: \
        sent.append((text, activation))
    lst.schedule_restart = lambda delay=0.3: None
    if alt is not None:
        # "audio=kept, second language will read it", and the read comes back with this.
        lst.recognizer_alt = object()
        lst._open_turn_audio = lambda: setattr(lst, "turn_audio_path", "/nonexistent/turn.caf")
        lst._close_turn_audio = lambda: "/nonexistent/turn.caf"
        lst._drop_turn_audio = lambda: setattr(lst, "turn_audio_path", "")
        lst._read_turn_audio_alt = lambda path: setattr(lst, "alt_final", alt)
        lst.alt_task = None
    return lst, bar, sent


def partial(lst, text):
    lst.on_result(lst.gen, FakeResult(text), None)


def final(lst, text, conf=0.0):
    lst.on_result(lst.gen, FakeResult(text, final=True, conf=conf), None)


def settle_turn(lst, sent, timeout=2.0):
    """What the watchdog does every tick until the turn is sent or thrown away."""
    deadline = time.time() + timeout
    while time.time() < deadline and lst.turn is not None:
        lst.consider()
        time.sleep(0.02)
    t_end = time.time() + 1.0
    while time.time() < t_end and not sent and lst.turn is None and \
            "turn ended with nothing said" not in LOGS[-3:]:
        time.sleep(0.02)
    return sent


def test_empty_final_after_shown_words_is_sent():
    """The exact log: tap turn, words on the bar, pressed again, empty final (0.00),
    the second language finds nothing too."""
    LOGS.clear()
    lst, bar, sent = turn_listener(alt=("", 0.0))
    lst.push_to_talk()                       # tap: stays open until pressed again
    for w in ("מה", "מה השעה", "מה השעה מיקמק"):
        partial(lst, w)
    check("the bar showed her words live",
          ("set_heard", "מה השעה מיקמק") in bar.calls, bar.calls)
    lst.push_to_talk()                       # "turn finishing: pressed again"
    final(lst, "", conf=0.0)                 # "turn: he-IL 0.00 (final)"
    settle_turn(lst, sent)
    check("the second language was asked to read it, as in the log",
          any("second language will read it" in m for m in LOGS), LOGS)
    check("the turn is sent with what the bar showed, not thrown away",
          sent == [("מה השעה מיקמק", "push")], (sent, LOGS[-4:]))
    check("it never says nothing was said",
          "turn ended with nothing said" not in LOGS, LOGS[-4:])


def test_session_replaced_mid_turn_keeps_its_words():
    """Why the final was empty: in a long tap turn Apple ends the session (1110, no
    log line without MICMIC_DEBUG) and the watchdog's start_task() opened the next one
    by wiping self.transcript, the only place those shown words lived."""
    LOGS.clear()
    lst, bar, sent = turn_listener()
    lst.push_to_talk()
    partial(lst, "מה השעה מיקמק")
    lst.on_result(lst.gen, None, FakeError(1110))
    check("the ended session is logged while a turn is open",
          any("session ended mid-turn (1110)" in m for m in LOGS), LOGS)
    sessions = lst.recognizer.sessions
    lst.task_started = time.time() - 60.0    # a long turn: well past the rate limit
    lst.start_task()                         # the watchdog: task is None -> missing
    check("the watchdog really opened a new session",
          lst.recognizer.sessions == sessions + 1, lst.recognizer.sessions)
    check("the words move into the turn instead of being wiped",
          lst.turn_prefix == "מה השעה מיקמק" and lst.transcript == "",
          (lst.turn_prefix, lst.transcript))
    lst.push_to_talk()
    final(lst, "")                           # the fresh session heard only silence
    settle_turn(lst, sent)
    check("the turn is sent with the words from the replaced session",
          sent == [("מה השעה מיקמק", "push")], (sent, LOGS[-4:]))


def test_empty_final_mid_turn_keeps_partial():
    """Apple ending an utterance at a pause with an EMPTY final must not drop the
    partial it had been showing."""
    LOGS.clear()
    lst, bar, sent = turn_listener()
    lst.push_to_talk()
    partial(lst, "what time is it")
    final(lst, "")                           # mid-turn: an utterance ended, no words
    check("the partial is kept as the turn's prefix",
          lst.turn_prefix == "what time is it", lst.turn_prefix)
    partial(lst, "micmic")
    lst.push_to_talk()
    final(lst, "micmic", conf=0.9)
    settle_turn(lst, sent)
    check("both pieces are sent", sent == [("what time is it micmic", "push")], (sent, LOGS[-4:]))


def test_short_final_does_not_beat_what_was_shown():
    LOGS.clear()
    lst, bar, sent = turn_listener()
    lst.push_to_talk()
    partial(lst, "please undo the last message")
    lst.push_to_talk()
    final(lst, "please", conf=0.9)
    settle_turn(lst, sent)
    check("a final that lost most of the shown words falls back to them",
          sent == [("please undo the last message", "push")], (sent, LOGS[-4:]))


def test_hold_to_talk_release_with_empty_final():
    """Hold-to-talk ends on the key's release, through the same finish."""
    LOGS.clear()
    lst, bar, sent = turn_listener()
    lst.press()
    lst.turn["down_at"] -= 1.0               # held a second: hold, not a tap
    partial(lst, "what time is it")
    lst.release()
    check("releasing a held key finishes the turn",
          lst.turn is not None and lst.turn["finish_at"] > 0 and lst.turn["mode"] == "hold",
          lst.turn)
    final(lst, "")
    settle_turn(lst, sent)
    check("and it is sent with what was shown", sent == [("what time is it", "push")],
          (sent, LOGS[-4:]))


def test_a_good_final_still_wins():
    """Unchanged: a real final corrects the partials and is what is sent."""
    LOGS.clear()
    lst, bar, sent = turn_listener()
    lst.push_to_talk()
    for w in ("what", "what tim", "what time is"):
        partial(lst, w)
    lst.push_to_talk()
    final(lst, "what time is it", conf=0.92)
    settle_turn(lst, sent)
    check("the final is sent", sent == [("what time is it", "push")], (sent, LOGS[-4:]))


def test_second_language_still_wins_when_surer():
    """Unchanged: the shown text only stands in for the final; a second-language read
    that is more confident still beats it."""
    LOGS.clear()
    lst, bar, sent = turn_listener(alt=("what time is it", 0.85))
    lst.push_to_talk()
    partial(lst, "ווט טיים איז איט")
    lst.push_to_talk()
    final(lst, "")
    settle_turn(lst, sent)
    check("the surer second-language read is sent", sent == [("what time is it", "push")],
          (sent, LOGS[-4:]))


def test_nothing_shown_is_still_nothing_said():
    """Unchanged: a turn in which nothing ever appeared still ends quietly."""
    LOGS.clear()
    lst, bar, sent = turn_listener(alt=("", 0.0))
    lst.push_to_talk()
    lst.push_to_talk()
    final(lst, "")
    settle_turn(lst, sent)
    check("nothing is sent", sent == [], sent)
    check("and it says so in the log", "turn ended with nothing said" in LOGS, LOGS[-4:])
    check("the turn is closed", lst.turn is None)


# ---------------------------------------------------------------- tap turns end on silence
# QA P0 #3: a tap-started turn nobody tapped closed stayed open for PTT_MAX (120 s) and
# then sent everything the room said (owner's log 16:38:28 -> 16:40:28).
def test_tap_turn_ends_on_silence_after_speech():
    LOGS.clear()
    lst, bar, sent = turn_listener()
    lst.push_to_talk()                       # a tap: mode "toggle"
    partial(lst, "what time is it")
    lst.last_change = time.time() - (listenermod.TAP_SILENCE - 0.5)
    lst.consider()
    check("a short pause does not end a tap turn", lst.turn is not None
          and not lst.turn["finish_at"], lst.turn)
    lst.last_change = time.time() - (listenermod.TAP_SILENCE + 0.1)
    lst.consider()
    check("silence after her words ends the tap turn",
          lst.turn is not None and lst.turn["finish_at"] > 0
          and "turn finishing: silence after speech" in LOGS, LOGS[-3:])
    final(lst, "what time is it", conf=0.9)
    settle_turn(lst, sent)
    check("and what she said is sent", sent == [("what time is it", "push")], (sent, LOGS[-4:]))


def test_tap_turn_without_words_waits():
    LOGS.clear()
    lst, bar, sent = turn_listener()
    lst.push_to_talk()
    lst.last_change = time.time() - 10.0     # quiet, but she has not said anything yet
    lst.consider()
    check("a silent tap turn is not ended by silence alone",
          lst.turn is not None and not lst.turn["finish_at"], lst.turn)
    lst.turn["deadline"] = time.time() - 0.1  # ...only by the cap
    lst.consider()
    check("the cap still ends it", lst.turn is None or lst.turn["finish_at"] > 0, lst.turn)


def test_held_key_is_not_ended_by_silence():
    LOGS.clear()
    lst, bar, sent = turn_listener()
    lst.press()                              # key still down: mode None
    partial(lst, "call my daughter")
    lst.last_change = time.time() - 10.0
    lst.consider()
    check("holding the key keeps the turn open through a pause",
          lst.turn is not None and not lst.turn["finish_at"], lst.turn)
    lst.turn["down_at"] -= 1.0
    lst.release()
    final(lst, "call my daughter", conf=0.9)
    settle_turn(lst, sent)
    check("and the release still sends it", sent == [("call my daughter", "push")],
          (sent, LOGS[-4:]))


def test_turn_cap_is_sane():
    check("a turn nobody ends is capped at 30 s or less, not 120",
          listenermod.PTT_MAX <= 30.0, listenermod.PTT_MAX)
    lst, bar, sent = turn_listener()
    lst.push_to_talk()
    check("the open turn's deadline uses that cap",
          lst.turn["deadline"] - lst.turn["down_at"] <= 30.0 + 1e-6, lst.turn)


# ---------------------------------------------------------------- the speech language setting
# QA #10 / #22: the recogniser, the status line and the menu follow the server's speech
# language (the page's language pill and Settings both save it there), within seconds.
class FakeItem:
    def __init__(self):
        self.title = ""

    def setTitle_(self, t):
        self.title = t

    def setHidden_(self, h):
        pass

    def setEnabled_(self, e):
        pass


def language_listener(cfg):
    lst = fresh_listener(bar=FakeBar(), cfg={})
    lst.locale = "en-US"
    rebuilt = []
    lst._make_recognizers = lambda: rebuilt.append(lst.locale) or True
    lst.start_task = lambda force=False: None
    lst.register_hotkey = lambda force=False: None
    lst.menu_items = {k: FakeItem() for k in ("menu_listen", "menu_show", "menu_browser",
                                              "menu_shortcut", "menu_log", "menu_quit")}
    lst.pause_item, lst.hotkey_line = FakeItem(), FakeItem()
    listenermod.get_config = lambda: dict(cfg)
    listenermod.log = lambda msg: LOGS.append(msg)
    return lst, rebuilt


def test_speech_language_setting_is_followed():
    cfg = {"language_hint": "he-IL"}
    lst, rebuilt = language_listener(cfg)
    lst.refresh_settings()
    check("a new speech language in the server's settings rebuilds the recogniser",
          lst.locale == "he-IL" and rebuilt == ["he-IL"], (lst.locale, rebuilt))
    check("the status line switches with it", lst.status_text == L("listening", "he-IL"),
          lst.status_text)
    check("and the menu is in Hebrew",
          lst.menu_items["menu_listen"].title == "להקשיב עכשיו"
          and lst.pause_item.title == L("menu_pause", "he-IL")
          and lst.hotkey_line.title == L("hotkey_ax", "he-IL"),
          (lst.menu_items["menu_listen"].title, lst.pause_item.title))
    lst.turn = {"kind": "ptt", "finish_at": 0.0}
    listenermod.get_config = lambda: {"language_hint": "en-US"}
    lst.refresh_settings()
    check("an open turn is never switched under her", lst.locale == "he-IL", lst.locale)
    lst.turn = None
    lst.refresh_settings()
    check("the next poll after the turn switches back to English",
          lst.locale == "en-US" and lst.menu_items["menu_show"].title == "Show MicMic window"
          and lst.menu_items["menu_listen"].title == "Listen now", lst.locale)


def test_menu_titles_exist_in_every_language():
    keys = ("menu_listen", "menu_pause", "menu_resume", "menu_show", "menu_browser",
            "menu_shortcut", "menu_log", "menu_about", "menu_help", "menu_quit", "hotkey_ax")
    missing = [(lang, k) for lang in ("he", "ar", "ru", "en") for k in keys
               if not listenermod.LANG[lang].get(k)]
    check("every menu title is written in all four languages", not missing, missing)
    check("no English menu title leaks into Hebrew",
          all(listenermod.LANG["he"][k] != listenermod.LANG["en"][k] for k in keys))


# ---------------------------------------------------------------- About / Help menu items
def test_about_and_help_menu_actions():
    """Delegate.aboutMicMic_ opens the standard About panel with the app's name,
    version and a credits line naming Jev/TypeSafe, the site and the support
    address; Delegate.openHelp_ opens the same site. Both are exercised through
    fakes standing in for NSApp and NSWorkspace, never a real window."""
    class FakeAboutApp:
        def __init__(self):
            self.options = None
            self.activated = None

        def orderFrontStandardAboutPanelWithOptions_(self, opts):
            self.options = opts

        def activateIgnoringOtherApps_(self, flag):
            self.activated = flag

    class FakeWorkspace:
        def __init__(self):
            self.opened = []

        def openURL_(self, url):
            self.opened.append(str(url))

    class FakeWorkspaceClass:
        def __init__(self, ws):
            self._ws = ws

        def sharedWorkspace(self):
            return self._ws

    orig_app, orig_ws = listenermod.AppKit.NSApp, listenermod.AppKit.NSWorkspace
    orig_version = listenermod.app_version
    try:
        app = FakeAboutApp()
        ws = FakeWorkspace()
        listenermod.AppKit.NSApp = lambda: app
        listenermod.AppKit.NSWorkspace = FakeWorkspaceClass(ws)
        listenermod.app_version = lambda: "9.9.9"
        d = listenermod.Delegate.alloc().init()
        d.aboutMicMic_(None)
        opts = app.options or {}
        check("About shows the standard panel with the app's name and version",
              opts.get(listenermod.AppKit.NSAboutPanelOptionApplicationName) == "MicMic"
              and opts.get(listenermod.AppKit.NSAboutPanelOptionApplicationVersion) == "9.9.9",
              opts)
        credit = str(opts.get(listenermod.AppKit.NSAboutPanelOptionCredits) or "")
        check("and credits Jev/TypeSafe, the site and the support address",
              "TypeSafe" in credit and listenermod.HELP_URL in credit
              and listenermod.SUPPORT_EMAIL in credit, credit)
        check("About brings MicMic to the front", app.activated is True, app.activated)
        d.openHelp_(None)
        check("Help and support opens the MicMic site",
              ws.opened == [listenermod.HELP_URL], ws.opened)
    finally:
        listenermod.AppKit.NSApp, listenermod.AppKit.NSWorkspace = orig_app, orig_ws
        listenermod.app_version = orig_version


# ---------------------------------------------------------------- branded menu-bar icon
def test_menu_icon_state_mapping():
    check("idle and hearing-you map to the branded mark, not a bare SF Symbol name",
          listenermod._MENU_ICONS == {"◉": "mic-idle", "●": "mic-active"},
          listenermod._MENU_ICONS)
    check("every _MENU_ICONS glyph still has an SF Symbol fallback",
          set(listenermod._MENU_ICONS) <= set(listenermod._SYMBOLS),
          (listenermod._MENU_ICONS, listenermod._SYMBOLS))
    check("thinking/countdown/attention are untouched SF Symbols",
          listenermod._SYMBOLS["◐"] == "ellipsis.circle"
          and listenermod._SYMBOLS["◼"] == "stop.circle"
          and listenermod._SYMBOLS["⚠"] == "exclamationmark.triangle.fill",
          listenermod._SYMBOLS)


def test_load_menu_icon_reads_1x_and_2x_and_sets_template():
    listenermod._MENU_ICON_CACHE.clear()
    img = listenermod._load_menu_icon("mic-idle")
    check("the branded idle icon loads from brand/menubar", img is not None, img)
    check("it is a template image with both an @1x and an @2x representation",
          bool(img) and img.isTemplate() is True and len(img.representations()) == 2,
          (img.isTemplate() if img else None, len(img.representations()) if img else None))
    check("it is cached by name", listenermod._load_menu_icon("mic-idle") is img)
    listenermod._MENU_ICON_CACHE.clear()


def test_load_menu_icon_missing_asset_returns_none():
    listenermod._MENU_ICON_CACHE.clear()
    check("an icon with no PNGs on disk is None, not a crash",
          listenermod._load_menu_icon("mic-does-not-exist") is None)


class FakeMenuButton:
    def __init__(self):
        self.image = "unset"
        self.title = "unset"

    def setImage_(self, img):
        self.image = img

    def setTitle_(self, t):
        self.title = t


class FakeTemplateImage:
    def __init__(self):
        self.template = None

    def setTemplate_(self, v):
        self.template = v

    def isTemplate(self):
        return self.template


def test_apply_glyph_uses_the_branded_icon_when_available():
    orig_load = listenermod._load_menu_icon
    try:
        fake_idle, fake_active = FakeTemplateImage(), FakeTemplateImage()
        fake_idle.setTemplate_(True)   # _load_menu_icon sets this itself before returning
        fake_active.setTemplate_(True)
        listenermod._load_menu_icon = lambda name: {"mic-idle": fake_idle,
                                                     "mic-active": fake_active}.get(name)
        b1, b2 = FakeMenuButton(), FakeMenuButton()
        listenermod._apply_glyph(b1, "◉")
        listenermod._apply_glyph(b2, "●")
        check("idle uses the branded idle image, title cleared",
              b1.image is fake_idle and b1.title == "", (b1.image, b1.title))
        check("hearing-you uses the branded active image, title cleared",
              b2.image is fake_active and b2.title == "", (b2.image, b2.title))
    finally:
        listenermod._load_menu_icon = orig_load


def test_apply_glyph_falls_back_to_sf_symbol_when_the_asset_is_missing():
    orig_load = listenermod._load_menu_icon
    try:
        listenermod._load_menu_icon = lambda name: None
        b = FakeMenuButton()
        listenermod._apply_glyph(b, "◉")
        check("a missing branded icon falls back to a real SF Symbol image, not the text glyph",
              b.image is not None and b.title == "" and b.image.isTemplate(),
              (b.image, b.title))
    finally:
        listenermod._load_menu_icon = orig_load


def test_apply_glyph_other_states_never_touch_the_branded_icon():
    orig_load = listenermod._load_menu_icon
    calls = []
    try:
        listenermod._load_menu_icon = lambda name: calls.append(name) or None
        b = FakeMenuButton()
        listenermod._apply_glyph(b, "◐")
        check("thinking never even asks for a branded icon", calls == [], calls)
        check("and still gets an SF Symbol image", b.image is not None and b.title == "")
    finally:
        listenermod._load_menu_icon = orig_load


# ---------------------------------------------------------------- launch at login
class FakeSMService:
    """Stands in for SMAppService.mainAppService(): records every register/
    unregister call and reports whatever status the test sets, using the same
    integers the real ServiceManagement enum does (see listener.py's LOGIN_STATUS:
    0 not_registered, 1 enabled, 2 requires_approval, 3 not_found)."""

    def __init__(self, status=0, fail=None):
        self.calls: list[str] = []
        self._status = status
        self.fail = fail

    def registerAndReturnError_(self, _):
        self.calls.append("register")
        if self.fail:
            return False, self.fail
        self._status = 1
        return True, None

    def unregisterAndReturnError_(self, _):
        self.calls.append("unregister")
        if self.fail:
            return False, self.fail
        self._status = 0
        return True, None

    def status(self):
        return self._status


class FakeSMAppServiceClass:
    def __init__(self, service):
        self.service = service

    def mainAppService(self):
        return self.service


def test_apply_login_item_registers_and_unregisters():
    orig_sm, orig_bundled = listenermod.SMAppService, listenermod.is_bundled
    try:
        fake = FakeSMService()
        listenermod.SMAppService = FakeSMAppServiceClass(fake)
        listenermod.is_bundled = lambda: True
        status = listenermod.apply_login_item(True)
        check("desired=True registers, and the real status comes back enabled",
              fake.calls == ["register"] and status == "enabled", (fake.calls, status))
        status = listenermod.apply_login_item(False)
        check("desired=False unregisters, status comes back not_registered",
              fake.calls == ["register", "unregister"] and status == "not_registered",
              (fake.calls, status))
    finally:
        listenermod.SMAppService, listenermod.is_bundled = orig_sm, orig_bundled


def test_apply_login_item_dev_checkout_is_a_noop():
    orig_sm, orig_bundled, orig_log = listenermod.SMAppService, listenermod.is_bundled, listenermod.log
    try:
        fake = FakeSMService()
        listenermod.SMAppService = FakeSMAppServiceClass(fake)
        listenermod.is_bundled = lambda: False
        listenermod.log = lambda msg: LOGS.append(msg)
        LOGS.clear()
        status = listenermod.apply_login_item(True)
        check("a dev checkout never touches SMAppService", fake.calls == [], fake.calls)
        check("it reports dev_checkout and logs a no-op line, not silence",
              status == "dev_checkout" and any("no-op" in m for m in LOGS), (status, LOGS))
    finally:
        listenermod.SMAppService, listenermod.is_bundled, listenermod.log = orig_sm, orig_bundled, orig_log


def test_apply_login_item_without_service_management_framework():
    orig_sm = listenermod.SMAppService
    try:
        listenermod.SMAppService = None
        check("no ServiceManagement on this macOS: not_supported, no crash",
              listenermod.apply_login_item(True) == "not_supported")
        check("login_item_status agrees", listenermod.login_item_status() == "not_supported")
    finally:
        listenermod.SMAppService = orig_sm


def test_login_item_status_is_read_only():
    orig_sm, orig_bundled = listenermod.SMAppService, listenermod.is_bundled
    try:
        fake = FakeSMService(status=2)   # requires_approval
        listenermod.SMAppService = FakeSMAppServiceClass(fake)
        listenermod.is_bundled = lambda: True
        status = listenermod.login_item_status()
        check("reading the status never registers or unregisters anything",
              fake.calls == [] and status == "requires_approval", (fake.calls, status))
    finally:
        listenermod.SMAppService, listenermod.is_bundled = orig_sm, orig_bundled


def test_refresh_login_item_applies_once_and_reports_changes_only():
    orig_sm, orig_bundled = listenermod.SMAppService, listenermod.is_bundled
    orig_post = listenermod.post_login_item_status
    reported: list[str] = []
    try:
        fake = FakeSMService()
        listenermod.SMAppService = FakeSMAppServiceClass(fake)
        listenermod.is_bundled = lambda: True
        listenermod.post_login_item_status = lambda s: reported.append(s)
        lst = fresh_listener()
        lst.refresh_login_item({"open_at_login": True})
        check("the first poll ever always applies and reports, nothing to compare against yet",
              fake.calls == ["register"] and reported == ["enabled"], (fake.calls, reported))
        lst.refresh_login_item({"open_at_login": True})
        check("an unchanged desired value re-checks status but never registers twice",
              fake.calls == ["register"], fake.calls)
        check("and an unchanged status is not re-reported",
              reported == ["enabled"], reported)
        lst.refresh_login_item({"open_at_login": False})
        check("a changed desired value unregisters and reports the new status",
              fake.calls == ["register", "unregister"]
              and reported == ["enabled", "not_registered"], (fake.calls, reported))
    finally:
        listenermod.SMAppService, listenermod.is_bundled = orig_sm, orig_bundled
        listenermod.post_login_item_status = orig_post


def test_refresh_login_item_follows_the_request_not_the_status():
    """2026-09-26, the real app: the switch was turned on, /api/config's
    open_at_login still said False (that field is SMAppService's real status,
    not yet registered), and the listener read it as the request, so nothing
    ever registered. The request travels as open_at_login_desired. And a login
    item that was never added is not "unregistered" on every launch."""
    orig_sm, orig_bundled = listenermod.SMAppService, listenermod.is_bundled
    orig_post = listenermod.post_login_item_status
    try:
        fake = FakeSMService(status=3)   # not_found: never registered
        listenermod.SMAppService = FakeSMAppServiceClass(fake)
        listenermod.is_bundled = lambda: True
        listenermod.post_login_item_status = lambda s: None
        lst = fresh_listener()
        lst.refresh_login_item({"open_at_login": False, "open_at_login_desired": False})
        check("switch off and nothing registered: no pointless unregister",
              fake.calls == [], fake.calls)
        lst.refresh_login_item({"open_at_login": False, "open_at_login_desired": True})
        check("switch on registers even while the status still reads off",
              fake.calls == ["register"], fake.calls)
    finally:
        listenermod.SMAppService, listenermod.is_bundled = orig_sm, orig_bundled
        listenermod.post_login_item_status = orig_post


def test_login_item_status_change_without_a_settings_change_is_still_reported():
    """She switched it off herself in System Settings > General > Login Items.
    MicMic never asked for that, but the next poll should notice and say so — the
    switch must reflect reality, not just the last thing it requested."""
    orig_sm, orig_bundled = listenermod.SMAppService, listenermod.is_bundled
    orig_post = listenermod.post_login_item_status
    reported: list[str] = []
    try:
        fake = FakeSMService()
        listenermod.SMAppService = FakeSMAppServiceClass(fake)
        listenermod.is_bundled = lambda: True
        listenermod.post_login_item_status = lambda s: reported.append(s)
        lst = fresh_listener()
        lst.refresh_login_item({"open_at_login": True})
        check("registered and reported enabled", reported == ["enabled"], reported)
        fake._status = 0   # she turned it off behind MicMic's back
        lst.refresh_login_item({"open_at_login": True})   # desired is unchanged
        check("no re-register, but the newly-observed status is reported",
              fake.calls == ["register"] and reported == ["enabled", "not_registered"],
              (fake.calls, reported))
    finally:
        listenermod.SMAppService, listenermod.is_bundled = orig_sm, orig_bundled
        listenermod.post_login_item_status = orig_post


def test_watchdog_polls_settings_within_seconds():
    lst, rebuilt = language_listener({"language_hint": "en-US"})
    lst.running, lst.paused, lst.server_healthy = True, False, True
    lst.task, lst.task_started = object(), time.time()
    lst.last_health_check = lst.last_hotkey_check = lst.last_buffers_at = time.time()
    lst.last_debug = time.time()
    calls = []
    lst.refresh_settings = lambda: calls.append(time.time())
    lst.update_hotkey_status = lambda: None
    lst.consider = lambda final=False: None
    lst.recover_engine = lambda why: None
    check("settings are polled every few seconds, not every 20",
          listenermod.SETTINGS_POLL <= 5.0, listenermod.SETTINGS_POLL)
    old = listenermod.SETTINGS_POLL
    listenermod.SETTINGS_POLL = 0.3
    th = threading.Thread(target=lst.watchdog, daemon=True)
    th.start()
    time.sleep(1.2)
    lst.running = False
    th.join(timeout=2.0)
    listenermod.SETTINGS_POLL = old
    check("the watchdog refreshes settings on that cadence", len(calls) >= 3, len(calls))


# ---------------------------------------------------------------- a dead engine at turn open
# The owner's log, 2026-09-26 02:26: a send was counting down, she pressed the key to stop
# it, the audio engine had died silently, the watchdog only noticed 29 ms later ("no audio
# buffers for 6s"), the turn heard nothing, and the message went out.
class FakeEngine:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


def engine_listener(audio_age, alive_after_rebuild=True):
    LOGS.clear()
    TURN_OPENS.clear()
    lst, bar, sent = turn_listener()
    lst.engine = FakeEngine()
    lst.audio_at = time.time() - audio_age
    lst.engine_started_at = time.time() - 60.0
    starts = []

    def fake_start():
        starts.append(time.time())
        lst.engine = FakeEngine()
        lst.engine_started_at = time.time()
        if alive_after_rebuild:
            lst.audio_at = time.time()           # buffers flow again
        return True
    lst.start_engine = fake_start
    return lst, bar, sent, starts


def test_dead_engine_is_rebuilt_when_a_turn_opens():
    lst, bar, sent, starts = engine_listener(audio_age=3.0)
    lst.press()
    check("a turn opening on a silent engine rebuilds it at once, not 6 s later",
          len(starts) == 1, (starts, LOGS))
    check("and says why in the log",
          any("turn open: no audio for 3.0s, rebuilding the engine first" in m for m in LOGS), LOGS)
    waits = [c for c in bar.calls if c[:2] == ("set_state", "thinking")]
    check("the bar says one moment while it does",
          waits and waits[0][2] == L("one_moment", lst.locale), bar.calls)
    check("then shows listening, with the turn open",
          ("set_state", "listening", "") in bar.calls[bar.calls.index(waits[0]):]
          and lst.turn is not None and lst.turn["kind"] == "ptt", (bar.calls, lst.turn))
    check("the rebuild's time is not counted as holding the key",
          time.time() - lst.turn["down_at"] < 0.2, time.time() - lst.turn["down_at"])
    lst.release()                            # a quick tap
    check("a quick tap after the rebuild is still a tap turn", lst.turn["mode"] == "toggle",
          lst.turn)
    partial(lst, "no stop")
    lst.push_to_talk()
    final(lst, "no stop", conf=0.9)
    settle_turn(lst, sent)
    check("her words after the rebuild are heard and sent", sent == [("no stop", "push")],
          (sent, LOGS[-4:]))


def test_live_engine_is_left_alone():
    lst, bar, sent, starts = engine_listener(audio_age=0.05)
    lst.press()
    check("a turn on a live engine does not rebuild it", starts == [], starts)
    lst2, _, _, starts2 = engine_listener(audio_age=5.0)
    lst2.engine_started_at = time.time() - 0.1     # just restarted, no buffer yet
    lst2.press()
    check("an engine restarted a moment ago is given its moment", starts2 == [], starts2)


def test_watchdog_finds_a_dead_engine_even_while_muted():
    """Muted while a send is read back is exactly when it went unnoticed; and a rebuild
    that brings nothing back must back off, not churn."""
    lst, bar, sent, starts = engine_listener(audio_age=0.0, alive_after_rebuild=False)
    lst.running, lst.paused, lst.server_healthy = True, False, False
    lst.task, lst.task_started = object(), time.time()
    lst.last_health_check = lst.last_hotkey_check = lst.last_debug = time.time()
    lst.muted_until = time.time() + 60.0     # MicMic is talking (a send's read-back)
    lst.consider = lambda final=False: None
    lst.update_hotkey_status = lambda: None
    lst.refresh_settings = lambda: None
    old = listenermod.ENGINE_STALL
    listenermod.ENGINE_STALL = 0.2
    lst.audio_at = time.time()
    th = threading.Thread(target=lst.watchdog, daemon=True)
    th.start()
    time.sleep(1.7)
    lst.running = False
    th.join(timeout=2.0)
    listenermod.ENGINE_STALL = old
    check("a dead engine is rebuilt while muted", len(starts) >= 1, (starts, LOGS[-3:]))
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    check("each fruitless rebuild waits longer than the last (no churn)",
          len(starts) <= 4 and all(b > a for a, b in zip(gaps, gaps[1:])), gaps)
    check("the stall limit is a couple of seconds, not six",
          old <= 2.5 and listenermod.TURN_AUDIO_FRESH <= 0.5, (old, listenermod.TURN_AUDIO_FRESH))


# ---------------------------------------------------------------- the server hears every turn open
def test_turn_open_posted_once_per_turn():
    LOGS.clear()
    TURN_OPENS.clear()
    lst, bar, sent = turn_listener()
    before = time.time()
    lst.push_to_talk()
    check("a tap turn posts turn_open once, with its time", len(TURN_OPENS) == 1
          and before - 0.01 <= TURN_OPENS[0] <= time.time(), TURN_OPENS)
    partial(lst, "hello")
    lst.push_to_talk()
    final(lst, "hello", conf=0.9)
    settle_turn(lst, sent)
    check("finishing it posts nothing more", len(TURN_OPENS) == 1, TURN_OPENS)
    lst.press()
    lst.turn["down_at"] -= 1.0
    lst.release()
    check("a held-key turn posts it too", len(TURN_OPENS) == 2, TURN_OPENS)


def test_key_during_a_followup_is_the_same_turn():
    """"turn open (followup)" then 0.3-0.7 s later "turn open (ptt)" in every exchange of
    the owner's log: the second one threw away what she had started saying."""
    LOGS.clear()
    TURN_OPENS.clear()
    lst, bar, sent = turn_listener()
    lst.start_turn("followup")
    partial(lst, "talia")
    lst.press()
    check("the key takes over the open followup instead of opening a new turn",
          lst.turn["kind"] == "ptt" and lst.transcript == "talia" and len(TURN_OPENS) == 1,
          (lst.turn, lst.transcript, TURN_OPENS))
    lst.release()                            # a quick tap: listen until she stops
    check("its release decides tap or hold like any press", lst.turn["mode"] == "toggle",
          lst.turn)
    lst.push_to_talk()
    final(lst, "talia", conf=0.9)
    settle_turn(lst, sent)
    check("and what she said in it is sent", sent == [("talia", "push")], (sent, LOGS[-4:]))


def test_press_during_a_send_countdown_is_a_normal_turn():
    """After "sending", send() leaves an armed cancel window open (no turn). A press there
    must open an ordinary turn the server hears about, unmuted."""
    LOGS.clear()
    TURN_OPENS.clear()
    lst, bar, sent = turn_listener()
    now = time.time()
    lst.muted_until = now + 3.0              # the read-back is still playing
    lst.armed_until = lst.countdown_until = now + 7.0
    lst.activation = "wake"
    lst.press()
    check("the press opens a normal push turn",
          lst.turn is not None and lst.turn["kind"] == "ptt" and lst.activation == "push",
          (lst.turn, lst.activation))
    check("the server is told a turn opened", len(TURN_OPENS) == 1, TURN_OPENS)
    check("and the microphone is hers at once, not muted", lst.muted_until == 0.0,
          lst.muted_until)
    lst.turn["down_at"] -= 1.0
    partial(lst, "no")
    lst.release()
    final(lst, "no", conf=0.9)
    settle_turn(lst, sent)
    check("her no is sent as a push turn", sent == [("no", "push")], (sent, LOGS[-4:]))


# ---------------------------------------------------------------- turn_open / turn_closed / asr_confidence
# convo-fix's contract (fix/convo f176d67): turn_open {ts, kind: hold|tap|followup} before
# audio; turn_closed {ts, heard: false, why: nothing|error} ONLY when a turn ends without
# an /api/utterance, and its reply may be a held message's "Should I still send it?";
# /api/utterance carries asr_confidence only when the recogniser really gave one.
def closing_listener():
    LOGS.clear()
    TURN_OPENS.clear()
    OPEN_KINDS.clear()
    TURN_CLOSES.clear()
    CLOSE_REPLY.clear()
    CLOSE_REPLY.update({"did": "nothing_held"})
    return turn_listener()


def wait_closes(n, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end and len(TURN_CLOSES) < n:
        time.sleep(0.02)
    time.sleep(0.05)
    return TURN_CLOSES


def real_send(lst, reply=None, fail=None):
    posted = []

    def post(text, activation="", asr_confidence=None):
        if fail:
            raise fail
        posted.append((text, asr_confidence))
        return reply or {"did": "answered", "say": "It is five."}
    listenermod.post_utterance = post
    lst.send = listenermod.Listener.send.__get__(lst)
    lst.announce_failure = lambda *a, **k: None
    return posted


def test_turn_open_kinds():
    lst, bar, sent = closing_listener()
    lst.press()
    lst.turn["down_at"] -= 1.0
    lst.release()
    lst.push_to_talk()                        # Listen now
    lst.start_turn("followup")
    check("the key posts hold, Listen now posts tap, MicMic's question posts followup",
          OPEN_KINDS == ["hold", "tap", "followup"], OPEN_KINDS)


def test_no_turn_closed_when_the_utterance_is_posted():
    lst, bar, sent = closing_listener()
    posted = real_send(lst)
    lst.push_to_talk()
    partial(lst, "what time is it")
    lst.push_to_talk()
    final(lst, "what time is it", conf=0.9)
    settle_turn(lst, posted)
    wait_closes(1, timeout=0.5)
    check("a turn whose utterance reached the server posts no turn_closed",
          posted and not TURN_CLOSES, (posted, TURN_CLOSES))


def test_turn_closed_nothing_and_error():
    import urllib.error
    lst, bar, sent = closing_listener()
    lst.push_to_talk()
    lst.push_to_talk()
    final(lst, "")
    settle_turn(lst, sent)
    wait_closes(1)
    check("nothing heard: turn_closed why=nothing, paired to its open ts",
          TURN_CLOSES == [(TURN_OPENS[0], "nothing")], (TURN_OPENS, TURN_CLOSES))
    lst.press()
    lst.cancel_chord()
    wait_closes(2)
    check("a chord cancel is nothing too", TURN_CLOSES[-1] == (TURN_OPENS[1], "nothing"),
          TURN_CLOSES)
    lst.push_to_talk()
    lst.stop_task = lambda: None
    lst.set_paused(True)
    wait_closes(3)
    check("pausing mid-turn closes it", lst.turn is None
          and TURN_CLOSES[-1] == (TURN_OPENS[2], "nothing"), TURN_CLOSES)
    lst.paused = False
    real_send(lst, fail=urllib.error.URLError("refused"))
    lst.push_to_talk()
    partial(lst, "no")
    lst.push_to_talk()
    final(lst, "no", conf=0.9)
    settle_turn(lst, [])
    wait_closes(4)
    check("an utterance that never reached the server closes why=error",
          TURN_CLOSES[-1] == (TURN_OPENS[3], "error") and len(TURN_CLOSES) == 4, TURN_CLOSES)


def test_every_open_has_at_most_one_close():
    lst, bar, sent = closing_listener()
    lst.start_turn("followup")
    lst.press()                               # takes the followup over: same turn
    lst.release()
    lst.push_to_talk()
    final(lst, "")
    settle_turn(lst, sent)
    lst.start_turn("followup")
    lst.start_turn("followup")                # replaced: its successor keeps the pause
    lst.push_to_talk()
    lst.push_to_talk()
    final(lst, "")
    settle_turn(lst, sent)
    wait_closes(2)
    closed_ts = [c[0] for c in TURN_CLOSES]
    check("each close names its own open ts, never twice",
          len(set(closed_ts)) == len(closed_ts) and set(closed_ts) <= set(TURN_OPENS),
          (TURN_OPENS, TURN_CLOSES))
    check("a turn replaced by a new one is not reported as nothing heard",
          TURN_OPENS[1] not in closed_ts and len(TURN_CLOSES) == 2, (TURN_OPENS, TURN_CLOSES))


def test_held_message_question_opens_the_followup():
    """turn_closed's reply for a held message is an asked_back reply: shown, and her
    yes or no is listened for without a press."""
    lst, bar, sent = closing_listener()
    CLOSE_REPLY.clear()
    CLOSE_REPLY.update({"did": "confirm_send", "lang": "english", "asked_back": True,
                        "say": "I held the message to Talia. Should I still send it? "
                               "Say yes and I will."})
    followups = []
    lst._open_followup = lambda spoken: followups.append(spoken)
    lst.push_to_talk()
    lst.push_to_talk()
    final(lst, "")
    settle_turn(lst, sent)
    end = time.time() + 2.0
    while time.time() < end and not followups:
        time.sleep(0.02)
    check("the question is shown on the bar",
          any(c[0] == "set_result" and "Should I still send it?" in c[1] for c in bar.calls),
          bar.calls[-3:])
    check("and the followup window opens for her answer", len(followups) == 1, followups)


def test_asr_confidence_with_every_utterance():
    lst, bar, sent = closing_listener()
    posted = real_send(lst)
    lst.push_to_talk()
    partial(lst, "send it")
    lst.push_to_talk()
    final(lst, "send it", conf=0.37)
    settle_turn(lst, posted)
    check("a real final sends its own confidence", posted and posted[-1] == ("send it", 0.37),
          posted)
    lst.push_to_talk()
    partial(lst, "what time is it")
    lst.push_to_talk()
    final(lst, "")                           # empty final: the shown text stands in
    settle_turn(lst, posted)
    check("the shown-text fallback sends 0.5", posted[-1] == ("what time is it", 0.5), posted)
    lst.push_to_talk()
    partial(lst, "call my daughter now please")
    lst.on_result(lst.gen, None, FakeError(1110))
    lst.task_started = time.time() - 60.0
    lst.start_task()                         # folded in as an unscored piece
    lst.push_to_talk()
    final(lst, "", conf=0.0)
    settle_turn(lst, posted)
    check("unscored pieces send 0.5, never 0.0",
          posted[-1] == ("call my daughter now please", 0.5), posted)
    check("every utterance posted carries a confidence",
          all(c is not None and 0.0 <= c <= 1.0 for _, c in posted), posted)


def main():
    for t in (test_full_turn_result_never_clobbered,
              test_thinking_before_shown_does_not_reach_a_hidden_bar,
              test_empty_answer_goes_idle,
              test_stop_word_hides,
              test_error_shows_even_if_bar_was_hidden,
              test_display_none_gets_nothing,
              test_view_idle_and_view_hide_bypass_display_mode,
              test_display_panel_shows_without_activating,
              test_display_bar_falls_back_to_panel_when_no_bar_exists,
              test_display_bar_default_when_unset,
              test_view_heard_ignores_empty_text,
              test_armed_window_expiry_via_watchdog,
              test_empty_final_after_shown_words_is_sent,
              test_session_replaced_mid_turn_keeps_its_words,
              test_empty_final_mid_turn_keeps_partial,
              test_short_final_does_not_beat_what_was_shown,
              test_hold_to_talk_release_with_empty_final,
              test_a_good_final_still_wins,
              test_second_language_still_wins_when_surer,
              test_nothing_shown_is_still_nothing_said,
              test_tap_turn_ends_on_silence_after_speech,
              test_tap_turn_without_words_waits,
              test_held_key_is_not_ended_by_silence,
              test_turn_cap_is_sane,
              test_speech_language_setting_is_followed,
              test_menu_titles_exist_in_every_language,
              test_about_and_help_menu_actions,
              test_menu_icon_state_mapping,
              test_load_menu_icon_reads_1x_and_2x_and_sets_template,
              test_load_menu_icon_missing_asset_returns_none,
              test_apply_glyph_uses_the_branded_icon_when_available,
              test_apply_glyph_falls_back_to_sf_symbol_when_the_asset_is_missing,
              test_apply_glyph_other_states_never_touch_the_branded_icon,
              test_apply_login_item_registers_and_unregisters,
              test_apply_login_item_dev_checkout_is_a_noop,
              test_apply_login_item_without_service_management_framework,
              test_login_item_status_is_read_only,
              test_refresh_login_item_applies_once_and_reports_changes_only,
              test_refresh_login_item_follows_the_request_not_the_status,
              test_login_item_status_change_without_a_settings_change_is_still_reported,
              test_watchdog_polls_settings_within_seconds,
              test_dead_engine_is_rebuilt_when_a_turn_opens,
              test_live_engine_is_left_alone,
              test_watchdog_finds_a_dead_engine_even_while_muted,
              test_turn_open_posted_once_per_turn,
              test_key_during_a_followup_is_the_same_turn,
              test_press_during_a_send_countdown_is_a_normal_turn,
              test_turn_open_kinds,
              test_no_turn_closed_when_the_utterance_is_posted,
              test_turn_closed_nothing_and_error,
              test_every_open_has_at_most_one_close,
              test_held_message_question_opens_the_followup,
              test_asr_confidence_with_every_utterance):
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(f"{t.__name__} ran to the end", False, repr(e)[:400])
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
