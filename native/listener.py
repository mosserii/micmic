#!/usr/bin/env python3
"""
MicMic native listener — always-on microphone, no browser.

What this is
------------
The whole point of this file is that MicMic should not need a Chrome window open
to hear her. It runs inside MicMic.app (see build.sh), which is what lets macOS
grant it microphone and speech-recognition permission at all: a bare `python3
listener.py` does not crash politely, it dies with SIGABRT the instant it asks
for speech permission, because TCC requires an Info.plist with the usage strings.
So: always launch the .app, never this file directly.

What it does
------------
  microphone  ->  AVAudioEngine tap
              ->  SFSpeechRecognizer (partial results, continuous)
              ->  wake word matched in the streaming transcript
              ->  POST {"text": ..., "speak": true} to 127.0.0.1:8799/api/utterance

It knows nothing about the savta package. It talks to the running server over
HTTP and nothing else, so it can be restarted, killed, or replaced without
touching the assistant.

Two things it does beyond "hear a wake word"
--------------------------------------------
1. The cancel window. When the server answers "sending" it has armed a 6 second
   countdown and expects her to be able to say "stop" with no ceremony. She will
   not say the wake word again to cancel — nobody does. So for the length of that
   countdown this listener stays armed and forwards whatever it hears with no
   wake word required. Rule 1 of this project survives the move off Chrome.
2. Not hearing itself. The Mac speaks its replies out loud through the same room
   the microphone is in. Every reply would otherwise come straight back as a new
   command. While a reply is being spoken the audio tap is dropped on the floor,
   and as a second line of defence anything that reads like the sentence we just
   said is ignored.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Optional

import AppKit
import AVFoundation
import Foundation
import objc
import Speech

try:
    from bar import MicMicBar            # native/bar.py: the slim Spotlight-style bar
except Exception as _bar_err:  # noqa: BLE001
    MicMicBar = None                     # no bar: the panel stands in for it
    _BAR_IMPORT_ERROR = repr(_bar_err)
else:
    _BAR_IMPORT_ERROR = ""

try:
    from panel import MicMicPanel
except Exception as _panel_err:  # noqa: BLE001
    MicMicPanel = None          # the app still listens; it just has no window
    _PANEL_IMPORT_ERROR = repr(_panel_err)
else:
    _PANEL_IMPORT_ERROR = ""

# --------------------------------------------------------------------------- config
SERVER = os.environ.get("MICMIC_SERVER", "http://127.0.0.1:8799")
UTTERANCE_URL = SERVER.rstrip("/") + "/api/utterance"
CONFIG_URL = SERVER.rstrip("/") + "/api/config"

DEFAULT_LOCALE = os.environ.get("MICMIC_LOCALE", "")       # "" = ask the server
DEFAULT_WAKE = ["מיקמיק", "מיק מיק", "micmic", "mic mic", "hey micmic"]

SILENCE_END = 1.2       # seconds of no new words before we decide she finished
CANCEL_END = 0.45       # …but during a countdown every tenth of a second is hers
WAKE_GRACE = 7.0        # heard the wake word, nothing after it yet: how long to wait
STALE_AFTER = 1.9       # measured: Apple hands back ONE utterance per recognition
                        # session for he-IL and then goes quiet, no error, no final.
                        # So every finished utterance gets a fresh session this soon.
TASK_MAX = 45.0         # and recycle an idle session before Apple's ~1 min cap
MUTE_MAX = 20.0         # never deafen ourselves for longer than this
POST_TIMEOUT = 45.0     # the router can take a few seconds (LLM step)
TICK = 0.2              # watchdog cadence
# A turn: she pressed the key (or MicMic asked her something) and the microphone is
# hers until she says she is done. Silence never ends a key turn: she was cut off
# mid-thought by the 1.2s endpoint above, which is for the wake word only.
HOLD_MIN = 0.35         # held at least this long = hold-to-talk; shorter = tap-toggle
PTT_MAX = 120.0         # a turn nobody ends is ended here
FINAL_WAIT = 1.2        # after the key: how long to wait for the recogniser's final words
ALT_WAIT = 2.0          # ...and for the second language to read the whole turn
FOLLOWUP_WINDOW = 8.0   # MicMic asked a question: how long to wait for the first word
FOLLOWUP_SILENCE = 1.6  # ...and the pause that ends her answer
# A turn's audio is also kept, and when her own language's recogniser is unsure of
# what it heard, a second one (English beside anything else) reads the same audio and
# the more confident transcript wins. Measured on recorded speech, n=6 (3 English, 3
# Hebrew): the right language 0.52-0.98, the wrong one 0.00-0.24, 6/6 correct. The
# two cannot run at once: Speech gives a process one live session and fails the other
# with 1110 (measured, both orders, on-device and server). So the second runs after,
# and only when the first scored under PRIMARY_SURE: a clear sentence costs nothing.
# Live through a real microphone the wrong language scored far higher than on clean
# files (en-US 0.71 for a Hebrew sentence), so skipping the second read on a "sure"
# first one was wrong. It always runs; this stays as the switch.
PRIMARY_SURE = float(os.environ.get("MICMIC_PRIMARY_SURE", "1.01"))
ALT_LOCALE = os.environ.get("MICMIC_ALT_LOCALE", "en-US")

DUP_WINDOW = 4.0        # drop a second POST of the identical command inside this long
ENGINE_STALL = 6.0      # no new audio buffers for this long (while not muted/paused)
                        # means the engine died under us, not that the room is quiet
HEALTH_POLL = 20.0      # how often to re-probe /api/health once we're up and running
INTERRUPT_CAP = 5.0     # cap our own self-mute at this; listen for a stop word after
FAILURE_REPEAT = 10.0   # do not re-speak the same spoken failure more often than this

# Global push-to-talk hotkey. She often says the wake word and Apple mishears it
# (README: מיקמק, מייק מייק, Mike Mike, Mick Mick) with no other way in. Default is
# a double-tap of the Fn/Globe key, which nothing else on the Mac uses; override with
# a "mod+mod+letter" spec such as "cmd+shift+m" via MICMIC_HOTKEY in listener.env.
# An explicit MICMIC_HOTKEY in listener.env still wins — it is how you pin a machine
# to one combination regardless of what is in Settings. Empty means "ask the server",
# which is where the Settings screen writes it.
HOTKEY_OVERRIDE = os.environ.get("MICMIC_HOTKEY", "").strip().lower()
DEFAULT_HOTKEY = "right-option"   # like Handy: one key, hold or tap

# Standard ANSI-US virtual keycodes, only for the letters/digits MICMIC_HOTKEY could
# plausibly name. This table is deliberately plain data (no AppKit) so parse_combo()
# can be exercised by `--check` without a display or an event loop.
_KEYCODES = {
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4, "i": 34,
    "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35, "q": 12, "r": 15,
    "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25, "space": 49,
}
_MODIFIER_BITS = {"cmd": 1 << 20, "shift": 1 << 17, "opt": 1 << 19, "alt": 1 << 19,
                   "ctrl": 1 << 18}
# Those four bits are NSEventModifierFlagCommand/Shift/Option/Control's actual raw
# values (AppKit.NSEventModifierFlagCommand etc.) — spelled out as plain ints here,
# not read from AppKit, for the same testability reason as _KEYCODES above.

# Sounds she can hear even if she never looks at the screen: one for "I'm listening
# now", one for "stopped listening". Both ship with every Mac.
SOUND_ARM = "/System/Library/Sounds/Tink.aiff"
SOUND_DISARM = "/System/Library/Sounds/Pop.aiff"

# Words that interrupt a long answer without the wake word (bugs #8). Normalized
# (see normalize()) at lookup time, so niqqud/punctuation/case do not matter.
STOP_WORDS = {"די", "תפסיקי", "תפסיק", "עצרי", "עצור", "מספיק", "שקט",
              "كفى", "توقفي", "توقف", "اسكتي",
              "хватит", "стоп", "тихо",
              "stop", "enough", "quiet"}

# macOS voices used for the listener's own local speech fallback (see speak_local()).
# These ship with the OS; if one was never downloaded, `say` errs silently to a
# device with no audio, which is a degraded-but-not-fatal outcome we accept here.
VOICES = {"he": "Carmit", "ar": "Maged", "ru": "Milena", "en": "Samantha"}

# Status text and spoken failures, in the four languages README.md says she may
# speak. Keyed the same way in every language so L() below is a single lookup.
LANG = {
    "he": {
        "speech_denied": "אין לי הרשאה לזהות דיבור. צריך לאשר את זה בהגדרות המערכת.",
        "mic_denied": "אין לי הרשאה למיקרופון. אני לא שומעת כלום עד שמאשרים בהגדרות המערכת.",
        "no_recognizer": "אין תמיכה בזיהוי דיבור בשפה הזאת במחשב הזה.",
        "mic_failed": "המיקרופון לא נפתח. נסי להפעיל אותי מחדש.",
        "server_down": "המחשב לא מוכן, תנסי שוב עוד רגע.",
        "unreachable": "אי אפשר להתחבר למיקמיק כרגע.",
        "listening": "מאזינה למילת ההפעלה",
        "heard_wake": "שמעתי את מילת ההפעלה: מקשיבה",
        "armed_grace": "מקשיבה: תגידי מה את רוצה",
        "push_to_talk": "לחצת על האזנה: תגידי מה את רוצה",
        "update": "יש גרסה חדשה: {v}. להורדה",
        "thinking": "חושבת…",
        "countdown": "סופרת לאחור {s:.0f} שניות: תגידי עצרי כדי לבטל",
        "paused": "בהשהיה",
    },
    "ar": {
        "speech_denied": "لا أملك إذن التعرف على الصوت. يجب الموافقة في إعدادات النظام.",
        "mic_denied": "لا أملك إذن استخدام الميكروفون. لا أسمع شيئًا حتى تتم الموافقة.",
        "no_recognizer": "لا يوجد دعم للتعرف على الصوت بهذه اللغة على هذا الكمبيوتر.",
        "mic_failed": "تعذر تشغيل الميكروفون. حاولي إعادة تشغيلي.",
        "server_down": "الكمبيوتر غير جاهز، جربي بعد قليل.",
        "unreachable": "لا يمكن الوصول إلى ميك ميك الآن.",
        "listening": "أستمع لكلمة التنشيط",
        "heard_wake": "سمعت كلمة التنشيط: أستمع",
        "armed_grace": "أستمع: قولي ما تريدين",
        "push_to_talk": "ضغطتِ للاستماع: قولي ما تريدين",
        "update": "في نسخة جديدة: {v}. للتنزيل",
        "thinking": "أفكر…",
        "countdown": "أعد العد {s:.0f} ثوانٍ: قولي توقفي للإلغاء",
        "paused": "متوقفة مؤقتًا",
    },
    "ru": {
        "speech_denied": "Нет разрешения на распознавание речи. Разрешите его в настройках системы.",
        "mic_denied": "Нет разрешения на микрофон. Я ничего не слышу, пока это не разрешат.",
        "no_recognizer": "На этом компьютере нет распознавания речи для этого языка.",
        "mic_failed": "Микрофон не включился. Попробуйте перезапустить меня.",
        "server_down": "Компьютер ещё не готов, попробуйте через минуту.",
        "unreachable": "Сейчас не получается связаться с МикМик.",
        "listening": "Слушаю ключевое слово",
        "heard_wake": "Услышала ключевое слово: слушаю",
        "armed_grace": "Слушаю: скажите, что вам нужно",
        "push_to_talk": "Вы нажали слушать: скажите, что нужно",
        "update": "Доступна новая версия {v}. Скачать",
        "thinking": "Думаю…",
        "countdown": "Обратный отсчёт {s:.0f} секунд: скажите стоп для отмены",
        "paused": "На паузе",
    },
    "en": {
        "speech_denied": "Speech recognition is not allowed. Please approve it in System Settings.",
        "mic_denied": "Microphone access is not allowed. I can't hear anything until it's approved.",
        "no_recognizer": "This computer has no speech recognition for that language.",
        "mic_failed": "The microphone could not start. Try restarting me.",
        "server_down": "The computer isn't ready yet, try again in a moment.",
        "unreachable": "I can't reach MicMic right now.",
        "listening": "listening for the wake word",
        "heard_wake": "heard the wake word: listening",
        "armed_grace": "listening: say what you want",
        "push_to_talk": "push-to-talk: say what you want",
        "update": "Update available: {v}. Download",
        "thinking": "thinking",
        "countdown": "counting down {s:.0f}s: say stop to cancel",
        "paused": "paused",
    },
}

# Next to the code in a checkout. In the signed app, ~/Library/Logs/MicMic: a log
# written inside the bundle broke its seal the first time the app ran.
if ".app/Contents/" in os.path.abspath(__file__):
    _LOG_DIR = os.path.expanduser("~/Library/Logs/MicMic")
    os.makedirs(_LOG_DIR, exist_ok=True)
    LOG_PATH = os.path.join(_LOG_DIR, "micmic-listener.log")
else:
    LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "micmic-listener.log")
LOG_MAX = 2_000_000
DEBUG = os.environ.get("MICMIC_DEBUG", "") not in ("", "0")


# --------------------------------------------------------------------------- logging
_log_lock = threading.Lock()


# MicMic.app's launcher redirects stdout into the very file this writes to, so
# printing unconditionally put every line in the log twice. Print only when a human
# is watching a terminal; the file is the record either way.
_TTY = sys.stdout.isatty()


def log(msg: str) -> None:
    # Milliseconds: a turn's steps are a few hundred ms apart, and whole seconds hid
    # every one of them (tests/perf/bench.py --listener reads these).
    now = time.time()
    lt = time.localtime(now)
    line = f"{time.strftime('%H:%M:%S', lt)}.{int(now * 1000) % 1000:03d}  {msg}"
    with _log_lock:
        if _TTY:
            print(line, flush=True)
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
                os.rename(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d ", lt) + line + "\n")
        except Exception:  # noqa: BLE001  logging must never take the app down
            pass


# --------------------------------------------------------------------------- text
_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def normalize(text: str) -> str:
    """Lowercase, drop punctuation and Hebrew/Arabic diacritics, collapse spaces.

    Hebrew recognition comes back with and without niqqud and with assorted
    punctuation depending on the sentence, so every comparison in this file
    happens on this normalized form.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _NON_WORD.sub(" ", text.lower())
    return _WS.sub(" ", text).strip()


def wake_variants(words: list[str]) -> set[str]:
    """Wake words as space-free normalized strings.

    Space-free is what makes "mic mic", "micmic", "מיק מיק" and "מיקמיק" the same
    thing without writing four rules. The extra Hebrew spellings are there because
    Apple transcribes the English word "mic" as מייק at least as often as מיק.
    """
    out: set[str] = set()
    for w in words:
        n = normalize(w).replace(" ", "")
        if n:
            out.add(n)
    # Every one of these was produced by Apple on this Mac for a spoken "מיקמיק"
    # or "mic mic" (see README, "what was actually tested"). מיקמק and mikemike are
    # not typos, they are transcripts.
    for extra in ("מייקמייק", "מיקמייק", "מייקמיק", "היימיקמיק", "מיקמיקי", "מיקמק",
                  "mikemike", "mikemic", "micmike", "mickmick"):
        out.add(normalize(extra).replace(" ", ""))
    return out


def near(a: str, b: str) -> bool:
    """True if a and b differ by at most one letter (insert, delete or swap).

    Apple wrote back מיקמק for a spoken מיקמיק on the first test on this machine.
    A name the recogniser has never seen will keep coming back a letter short or a
    letter long forever, so the wake word is matched loosely rather than exactly.
    One letter is the whole allowance: at six letters, a stray one-letter neighbour
    of "מיקמיק" is not a word anyone says by accident.
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1 or min(la, lb) < 5:
        return False
    if la == lb:                                   # one substitution?
        return sum(x != y for x, y in zip(a, b)) == 1
    if la > lb:                                    # make a the shorter one
        a, b, la, lb = b, a, lb, la
    for i in range(lb):                            # one deletion?
        if b[:i] + b[i + 1:] == a:
            return True
    return False


_MI = ("mi", "מי", "my", "мі", "ми")


def doubled(a: str, b: str) -> bool:
    """A word said twice, and the word starts like "mic".

    Apple writes the name she is calling however it likes — it came back as
    "Mike Mike" in English and "מיק מיק" in Hebrew in the same afternoon — but the
    shape of it survives: the same short syllable twice, mi- at the front and a k
    sound at the back. Matching the shape catches spellings nobody has thought of
    yet, which is the whole problem with a wake word that is not a real word. The
    k is what keeps "מים מים" (water, water) and "my my" out.
    """
    return (a == b and len(a) >= 3 and a.startswith(_MI)
            and any(c in a for c in "קkcq"))


def find_wake(tokens: list[str], variants: set[str]) -> Optional[tuple[int, int]]:
    """Last (start, end) token span that spells a wake word. Inclusive of end.

    Walks every span of up to 4 tokens and compares them glued together, which is
    how "hey mic mic" and "מיק מיק" both land on the same entry. Taking the LAST
    match means a second "micmic ..." inside a still-growing transcript wins over
    the first, so she can restate a command without waiting for a reset.
    """
    hit = None
    for i in range(len(tokens)):
        # "Later wins" only applies to a wake word that starts after the previous one
        # ENDS. Without that, "mic mic make it louder" matched "mic"+"make" at i=1,
        # overrode the real hit at i=0, and swallowed the verb: she got "it louder".
        if hit is not None and i <= hit[1]:
            continue
        if i + 1 < len(tokens) and doubled(tokens[i], tokens[i + 1]):
            hit = (i, i + 1)
            continue
        glued = ""
        for j in range(i, min(i + 4, len(tokens))):
            glued += tokens[j]
            # Fuzzy only against the long spellings. "מיקמק" is five letters and
            # a one-letter allowance on it would swallow ordinary Hebrew words.
            if glued in variants or any(near(glued, v) for v in variants if len(v) >= 6):
                hit = (i, j)
                break          # shortest span at this start; do not keep eating words
    return hit


def speech_seconds(text: str) -> float:
    """Rough length of `say -r 170 <text>`. Used only to mute ourselves."""
    if not text:
        return 0.0
    return min(MUTE_MAX, 0.5 + len(text) / 13.0)


def is_stop_word(text: str) -> bool:
    """One normalized token exactly matching STOP_WORDS. Token match, not substring
    — a substring test on a whole sentence is exactly what let an echoed "…אני
    עוצרת" cancel a message nobody objected to (see README/bugs #4); a stop word
    heard mid-answer needs the same exact-word discipline."""
    return any(t in STOP_WORDS for t in text.split())


def lang_code(locale: str) -> str:
    return (locale or "he-IL").split("-")[0].lower()


def L(key: str, locale: str, **kw) -> str:
    """Look up a status/failure string in the profile's language, falling back to
    English for a language we have not written (or a key that is new)."""
    table = LANG.get(lang_code(locale)) or LANG["en"]
    text = table.get(key) or LANG["en"].get(key, key)
    return text.format(**kw) if kw else text


def speak_local(key: str, locale: str, **kw) -> None:
    """A handful of failures happen before there is any request to hand the server
    — denied permission, no recognizer, a dead audio engine, the server itself
    unreachable. mac.say() (savta/actions/mac.py) only ever runs after a request
    lands in savta/router.py, so it cannot cover any of these; this is listener.py's
    own minimal, profile-language-aware fallback, spoken directly via `say`, no
    server round-trip (see README/bugs #1, #5, #9)."""
    text = L(key, locale, **kw)
    voice = VOICES.get(lang_code(locale), VOICES["en"])
    try:
        subprocess.Popen(["say", "-v", voice, text],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001  speaking must never take the app down
        log(f"local speech failed ({key}): {e!r}")


def play_sound(path: str) -> None:
    """An audible cue for a state a screen reader would announce and she will not
    read: the push-to-talk/wake-grace window opening and closing (see README/
    bugs #7). Both sounds ship with every Mac."""
    try:
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        log(f"could not play {path}: {e!r}")


_libHIServices = None


def is_accessibility_trusted() -> bool:
    """AXIsProcessTrusted(), called directly via ctypes. PyObjC's Quartz binding
    does not expose it (no metadata for this symbol), and a global hotkey monitor
    (see register_hotkey below) silently receives nothing at all — no error, no
    callback, ever — until this is true. That silence is exactly the failure mode
    bug #6 exists to avoid, so it has to be checked and reported explicitly."""
    global _libHIServices
    try:
        if _libHIServices is None:
            _libHIServices = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            _libHIServices.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(_libHIServices.AXIsProcessTrusted())
    except Exception as e:  # noqa: BLE001
        log(f"could not check Accessibility trust: {e!r}")
        return True  # do not nag about a permission we failed to even check


# A lone tap of one modifier, the way Handy uses Right Option. A combination like
# cmd+u has a cost a tap does not: a global monitor can SEE a keystroke but cannot
# swallow it, so cmd+u would also underline text in Mail, Pages or Notes every time.
# A modifier tapped on its own does nothing in any app. Right-hand keys only: the
# left ones are used constantly as modifiers and would misfire.
#   spec -> (virtual key code, which modifier flag it sets)
TAP_KEYS = {
    "right-command": (54, "NSEventModifierFlagCommand"),
    "right-option":  (61, "NSEventModifierFlagOption"),
    "right-control": (62, "NSEventModifierFlagControl"),
    "right-shift":   (60, "NSEventModifierFlagShift"),
}


class TapDetector:
    """Pressed and released within MAX_S with nothing else pressed in between.

    Pure data, no AppKit, so `--check` can drive it with synthetic key sequences —
    there is no other way to test a global hotkey without a person at the keyboard.
    """
    MAX_S = 0.45
    # Device-independent modifier bits (Command, Shift, Control, Option, Fn), as plain
    # numbers so this class stays AppKit-free.
    MODIFIERS = (1 << 20) | (1 << 17) | (1 << 18) | (1 << 19) | (1 << 23)

    def __init__(self, key_code: int, flag: int) -> None:
        self.key_code, self.flag = key_code, flag
        self.others = self.MODIFIERS & ~flag
        self.down_at: float | None = None

    def key_down(self) -> None:
        # An ordinary key while it is held: it is being used AS a modifier (cmd+c),
        # which must never also start listening.
        self.down_at = None

    def flags_changed(self, key_code: int, flags: int, t: float) -> bool:
        if key_code != self.key_code:
            self.down_at = None          # another modifier moved: not a lone tap
            return False
        if flags & self.flag:
            # Pressed. With Shift or Command already held it is part of a chord
            # (Shift+Option-drag), never a lone tap: measured firing 5/5 before.
            self.down_at = None if flags & self.others else t
            return False
        fired = (self.down_at is not None and (t - self.down_at) <= self.MAX_S
                 and not flags & self.others)
        self.down_at = None              # released
        return fired


def request_accessibility() -> bool:
    """Ask macOS for Accessibility with its own dialog.

    AXIsProcessTrusted() only answers the question; AXIsProcessTrustedWithOptions with
    the prompt option is what puts MicMic into the Accessibility list and shows the
    "would like to control this computer" dialog with a button straight to the right
    pane — so she flips one switch instead of hunting for an app to add. macOS shows
    it once; after that the call is a plain check."""
    try:
        global _libHIServices
        if _libHIServices is None:
            _libHIServices = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            _libHIServices.AXIsProcessTrusted.restype = ctypes.c_bool
        fn = _libHIServices.AXIsProcessTrustedWithOptions
        fn.restype, fn.argtypes = ctypes.c_bool, [ctypes.c_void_p]
        # NSDictionary is toll-free bridged to CFDictionary; the constant's value is
        # the string "AXTrustedCheckOptionPrompt".
        opts = Foundation.NSDictionary.dictionaryWithObject_forKey_(
            True, "AXTrustedCheckOptionPrompt")
        return bool(fn(ctypes.c_void_p(objc.pyobjc_id(opts))))
    except Exception as e:  # noqa: BLE001
        log(f"could not ask for Accessibility: {e!r}")
        return False


ACCESSIBILITY_PANE = ("x-apple.systempreferences:"
                      "com.apple.preference.security?Privacy_Accessibility")


def parse_combo(spec: str) -> tuple[int, int]:
    """"cmd+shift+m" -> (keyCode, wantedModifierMask). Pure data, no AppKit — so
    a bad MICMIC_HOTKEY value is caught by `--check` before it ever reaches a
    running app. Raises ValueError on anything it cannot parse."""
    parts = [p for p in spec.replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty hotkey spec")
    key = parts[-1]
    code = _KEYCODES.get(key)
    if code is None:
        raise ValueError(f"unknown key {key!r} in MICMIC_HOTKEY {spec!r}")
    mask = 0
    for m in parts[:-1]:
        bit = _MODIFIER_BITS.get(m)
        if bit is None:
            raise ValueError(f"unknown modifier {m!r} in MICMIC_HOTKEY {spec!r}")
        mask |= bit
    return code, mask


# --------------------------------------------------------------------------- server
def check_server_health() -> bool:
    """Ground truth for whether the server is actually up, independent of whatever
    get_config() or post_utterance() last managed. Used at startup and on a
    HEALTH_POLL interval so "listening for the wake word" is never shown while the
    server that would answer her is down (see README/bugs #1)."""
    try:
        with urllib.request.urlopen(SERVER.rstrip("/") + "/api/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def is_bundled() -> bool:
    """True when this file is running from inside a release .app bundle."""
    return ".app/Contents/" in os.path.realpath(__file__)


def ensure_server() -> None:
    """Make sure something is answering /api/health before the listener starts.

    A dev checkout leaves this alone: the shell launcher already starts the server as
    a separate process, or you started one yourself in a terminal, and starting a
    second one here would only fight over the port.

    A release bundle has neither. It ships exactly one interpreter and this is it, so
    the server runs in a daemon thread of this very process. That is also the only
    arrangement TCC likes: a second python process would be a second identity, and the
    microphone grant belongs to this bundle.
    """
    if check_server_health() or not is_bundled():
        return
    log("no server answering — starting one inside this process")
    try:
        from savta import server as _server
    except Exception as e:  # noqa: BLE001
        log(f"cannot import the bundled server: {e!r}")
        return
    threading.Thread(target=_server.main, name="micmic-server", daemon=True).start()
    for _ in range(40):                    # 10s, the same budget the shell launcher used
        time.sleep(0.25)
        if check_server_health():
            log("embedded server is up")
            return
    log("embedded server did not come up — see micmic-listener.log")


def server_duck(on: bool) -> None:
    """Ask the server to lower (or restore) the Mac's volume while she speaks. A song
    MicMic started plays straight into this microphone, and a recogniser listening
    over music never hears the silence that ends her sentence. Fire and forget, off
    the main thread: a missing server must never delay the microphone opening."""
    def go():
        try:
            req = urllib.request.Request(
                SERVER.rstrip("/") + ("/api/duck" if on else "/api/unduck"),
                data=b"{}", method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:  # noqa: BLE001
            pass                      # the server's own 75s failsafe restores it anyway
    threading.Thread(target=go, daemon=True).start()


def stop_speaking() -> None:
    """Pressing the key talks over MicMic: cut its sentence off, in the background."""
    def go():
        try:
            req = urllib.request.Request(SERVER.rstrip("/") + "/api/stop_speaking",
                                         data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2).read()
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=go, daemon=True).start()


def is_speaking() -> bool:
    try:
        with urllib.request.urlopen(SERVER.rstrip("/") + "/api/speaking", timeout=1) as r:
            return bool(json.loads(r.read() or b"{}").get("speaking"))
    except Exception:  # noqa: BLE001
        return False


def new_utterance(prev: str, now: str) -> bool:
    """Speech started over inside the same session. After a pause it does not always
    send a final result: the partial text simply restarts, shorter, with the words
    before the pause gone (measured: "please find how I can uninstall Microsoft
    AutoUpdate ... for my mac please" arrived as "for my mac please")."""
    p, n = prev.split(), now.split()
    return len(p) >= 3 and 0 < len(n) < len(p) and n[0] != p[0]


def _final_conf(result) -> float:
    try:
        segs = result.bestTranscription().segments()
        return sum(float(x.confidence()) for x in segs) / max(1, len(segs))
    except Exception:  # noqa: BLE001
        return 0.0


def get_config() -> dict:
    try:
        with urllib.request.urlopen(CONFIG_URL, timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        log(f"config: server not answering ({e!r}); using built-in defaults")
        return {}


def post_utterance(text: str, activation: str = "") -> dict:
    # "client": "native" tells the router this has no browser window to play a
    # video in — without it the server defaults to "web" and a music/video
    # request here says "here you go" and then plays nothing at all (bug #17).
    #
    # "activation" tells it whether this utterance passed a deliberate trigger
    # (a matched wake word, or a push-to-talk hotkey/menu press) or was picked
    # up by an open microphone with nothing said to name it — server.py reads
    # this and router.py's not-for-us filter is keyed off it
    # (open_mic = activation != "push"). Leaving it out makes the router fall
    # back to micmic.config.json's "push" default, which is right for the
    # browser orb but exactly backwards for this always-listening client: it
    # would switch the filter OFF for the one caller that actually needs it.
    body = {"text": text, "speak": True, "client": "native"}
    if activation:
        body["activation"] = activation
    body = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        UTTERANCE_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as r:
        return json.loads(r.read().decode() or "{}")


# --------------------------------------------------------------------------- main object
def on_main(fn) -> None:
    """Run fn on the main thread.

    The block handed to AppKit must return nothing. Passed directly, a function that
    returns a value — request_accessibility() returns a bool — makes PyObjC raise
    "did not return None, expecting void return value" inside the run loop, which is
    an uncaught NSException and kills the app. It did, at launch, for anyone who had
    not granted Accessibility yet: every new user. So the result is always dropped
    here, and an exception is logged instead of taking the app down with it."""
    def block():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log(f"main-thread task {getattr(fn, '__name__', fn)!r} failed: {e!r}")
    Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(block)


def alert(title: str, message: str) -> None:
    """A denied/never-seen permission dialog used to leave a ⚠ glyph nobody would
    click and nothing else (bugs #5) — the launcher script already proves an
    NSAlert works from this app (MicMic.app/Contents/MacOS/MicMic's `osascript
    display alert`); this is the same idea from inside listener.py, for the
    failures only listener.py can see."""
    def show():
        a = AppKit.NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(message)
        a.addButtonWithTitle_("OK")
        a.runModal()
    on_main(show)


# Mirrors savta/trace.py SCREEN_DIDS. The listener runs in its own interpreter and
# does not import the savta package, so the set is repeated here on purpose.
SCREEN_DIDS = {"read_screen", "described_screen", "summarized_screen",
               "translated_screen", "confirm_calendar", "calendar_added"}


class Listener:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cfg = {}
        self.locale = DEFAULT_LOCALE or "he-IL"
        self.variants = wake_variants(DEFAULT_WAKE)
        self.contextual: list[str] = []

        self.recognizer = None
        self.recognizer_alt = None    # English beside her language, only during a turn
        self.alt_locale = ALT_LOCALE
        self.alt_request = None
        self.alt_task = None
        self.alt_text = ""            # normalized, like transcript
        self.primary_final = None     # (normalized text, confidence) once final
        self.alt_final = None
        self.turn = None              # {"kind": "ptt"|"followup", "down_at", "mode", "finish_at", "deadline"}
        self.turn_audio = None        # AVAudioFile being written while a turn is open
        self.alt_gen = 0
        self.alt_parts: list = []      # (text, confidence, words) per utterance of the file read
        self.turn_prefix = ""         # what earlier recognition sessions of this turn heard
        self.turn_audio_path = ""
        self.input_format = None
        self.swallow_release = False  # the press that ENDED a tap turn: ignore its release
        self.engine = None
        self.gen = 0                  # session generation; stale callbacks are dropped
        self.request = None
        self.task = None
        self.tap_installed = False

        self.transcript = ""          # normalized, current recognition task only
        self.last_change = 0.0
        self.task_started = 0.0
        self.pending_tail = ""        # command heard after the wake word
        self.wake_at = 0.0            # when the wake word was heard
        self.armed_until = 0.0        # cancel window: no wake word needed
        self.activation = "wake"      # why armed_until is open: "wake" or "push" —
                                       # sent to the server so its not-for-us filter
                                       # (keyed off this) stays ON for an open mic
                                       # and OFF for a deliberate trigger
        self.countdown_until = 0.0    # a message is mid-countdown: answer fast
        self.muted_until = 0.0        # we are talking; ignore the microphone
        self.interrupt_until = 0.0    # ordinary answer still playing: stop words only
        self.last_spoken = ""         # normalized, for echo suppression
        self.last_sent_text = ""      # dup guard (bugs #3): last command actually posted
        self.last_sent_at = 0.0
        self.paused = False
        self.running = False
        self.buffers = 0
        self.last_buffers = 0         # engine-stall detection (bugs #2)
        self.last_buffers_at = 0.0
        self.tap_error = ""
        self.last_debug = 0.0
        self.status_text = "starting"
        self.status_item = None
        self.status_line = None
        self.hotkey_monitors = []      # so a changed hotkey can unregister the old ones
        self.hotkey_active = ""        # what is currently registered
        self.panel = None
        self.bar = None
        self.hotkey_line = None
        self._hotkey_trusted_last = None
        self.last_hotkey_check = 0.0
        self.last_update_check = 0.0
        self.update_item = None
        self.update_url = ""

        self.server_healthy = None    # None = not checked yet; see watchdog
        self.last_health_check = 0.0
        self.last_failure_spoken: dict[str, float] = {}  # per-key speech cooldown

        self.speech_auth = 0
        self.mic_auth = False

    # ------------------------------------------------------------------ menu bar
    def build_status_item(self, delegate) -> None:
        bar = AppKit.NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        _apply_glyph(item.button(), "◉")
        menu = AppKit.NSMenu.alloc().init()

        line = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("starting…", None, "")
        line.setEnabled_(False)
        menu.addItem_(line)

        # Hidden until is_accessibility_trusted() is false — see update_hotkey_status().
        # Bug #6's hotkey needs Accessibility permission and gives no error, no
        # callback, nothing, until it is granted; this is the "say so" half of
        # "detect that and say so in the menu bar AND in the log".
        hotkey_line = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        hotkey_line.setEnabled_(False)
        hotkey_line.setHidden_(True)
        menu.addItem_(hotkey_line)
        # Hidden until the releases page has a newer version than this one.
        update_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "", "openUpdate:", "")
        update_item.setTarget_(delegate)
        update_item.setHidden_(True)
        menu.addItem_(update_item)
        self.update_item = update_item
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        listen_now = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Listen now", objc.selector(delegate.listenNow_, signature=b"v@:@"), "")
        listen_now.setTarget_(delegate)
        menu.addItem_(listen_now)

        pause = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Pause listening", objc.selector(delegate.togglePause_, signature=b"v@:@"), "")
        pause.setTarget_(delegate)
        menu.addItem_(pause)

        showp = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show MicMic window", objc.selector(delegate.showPanel_, signature=b"v@:@"), "")
        showp.setTarget_(delegate)
        menu.addItem_(showp)

        openw = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open in browser", objc.selector(delegate.openWeb_, signature=b"v@:@"), "")
        openw.setTarget_(delegate)
        menu.addItem_(openw)

        keyset = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Set up keyboard shortcut…", objc.selector(delegate.setUpShortcut_, signature=b"v@:@"), "")
        keyset.setTarget_(delegate)
        menu.addItem_(keyset)

        logi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show log", objc.selector(delegate.showLog_, signature=b"v@:@"), "")
        logi.setTarget_(delegate)
        menu.addItem_(logi)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit MicMic", objc.selector(delegate.quitApp_, signature=b"v@:@"), "q")
        quit_item.setTarget_(delegate)
        menu.addItem_(quit_item)

        item.setMenu_(menu)
        if MicMicPanel is not None and self.panel is None:
            try:
                self.panel = MicMicPanel(SERVER, on_listen=lambda: on_main(self.push_to_talk))
                self.panel.set_state("idle", "")
            except Exception as e:  # noqa: BLE001
                log(f"could not build the window: {e!r} — running menu-bar only")
                self.panel = None
        elif MicMicPanel is None:
            log(f"window unavailable: {_PANEL_IMPORT_ERROR} — running menu-bar only")
        if MicMicBar is not None and self.bar is None:
            try:
                self.bar = MicMicBar(SERVER)       # kept warm and hidden until needed
            except Exception as e:  # noqa: BLE001
                log(f"could not build the bar: {e!r} — the panel will be used instead")
                self.bar = None
        self.status_item = item
        self.status_line = line
        self.hotkey_line = hotkey_line
        self.pause_item = pause

    # ---------------------------------------------------------------- what she sees
    # Settings > display: "panel" (the full window), "bar" (the slim strip at the top,
    # the default), or "none" (nothing appears; it does the thing and says so out
    # loud). The panel keeps mirroring everything whatever the choice, so opening it
    # from the Dock always shows the truth; the choice is only what POPS UP.
    def display_mode(self) -> str:
        mode = str((self.cfg or {}).get("display") or "bar")
        if mode == "bar" and self.bar is None:
            return "panel"                  # no bar available: never leave her with nothing
        return mode if mode in ("panel", "bar", "none") else "bar"

    def view_attention(self) -> None:
        """She has just addressed MicMic (hotkey, Listen now, or the wake word)."""
        mode = self.display_mode()
        if mode == "bar":
            # State first, then show: a new turn clears the last result before the
            # strip appears, so the old answer never flashes up again.
            def arm():
                self.bar.set_state("listening")
                self.bar.show()
            on_main(arm)
        elif mode == "panel" and self.panel is not None:
            # Not activated: "this" means the app she was in, which must stay in front.
            on_main(lambda: self.panel.show(activate=False))

    def view_heard(self, text: str) -> None:
        if self.display_mode() == "bar" and text:
            on_main(lambda: self.bar.set_heard(text))

    def view_result(self, say: str, undo_label) -> None:
        if self.display_mode() != "bar":
            return
        if say:
            on_main(lambda: self.bar.set_result(say, undo_label))
        else:
            on_main(lambda: self.bar.set_state("idle"))   # ignored or not for us: go away

    def view_working(self) -> None:
        if self.bar is not None and self.display_mode() == "bar":
            on_main(lambda: self.bar.set_state("thinking"))

    def view_idle(self) -> None:
        if self.bar is not None and self.display_mode() == "bar":
            on_main(lambda: self.bar.set_state("idle"))

    def view_hide(self) -> None:
        if self.bar is not None and self.display_mode() == "bar":
            on_main(self.bar.hide)

    # Every set_status() call already passes a glyph that says which state this is,
    # so the panel can be driven from here without touching fifteen call sites.
    GLYPH_STATE = {"◉": "listening", "◐": "thinking", "●": "listening",
                   "◼": "acting", "⚠": "error"}

    def set_status(self, text: str, glyph: str = "") -> None:
        self.status_text = text

        def apply():
            if self.status_line is not None:
                self.status_line.setTitle_(text)
            if glyph and self.status_item is not None:
                _apply_glyph(self.status_item.button(), glyph)
            if self.panel is not None:
                self.panel.set_state(self.GLYPH_STATE.get(glyph, "idle"), text)
            # Only thinking and errors reach the bar. "◉" follows every answer and "◼"
            # a send countdown: mirrored, either would wipe the result she is reading.
            if (self.bar is not None and self.display_mode() == "bar"
                    and glyph in ("◐", "⚠") and (glyph == "⚠" or self.bar.is_visible())):
                self.bar.set_state(self.GLYPH_STATE[glyph], text)
                if glyph == "⚠":
                    self.bar.show()
        on_main(apply)
        if glyph == "●":                   # push-to-talk and the wake word both land here
            self.view_attention()

    def announce_failure(self, key: str, **kw) -> None:
        """set_status() paints a glyph nobody elderly/illiterate-in-English will
        check; this is the spoken half (bugs #1, #9). Rate-limited per key so a
        repeated failure (the watchdog retries every tick) does not turn into a
        loop of `say` processes talking over each other."""
        now = time.time()
        if now - self.last_failure_spoken.get(key, 0.0) < FAILURE_REPEAT:
            return
        self.last_failure_spoken[key] = now
        speak_local(key, self.locale, **kw)

    def update_hotkey_status(self) -> None:
        """Global hotkey monitors deliver nothing at all — no error — until the app
        is Accessibility-trusted. Checked once at registration and then on a slow
        poll from the watchdog, since she can grant it later without a relaunch."""
        trusted = is_accessibility_trusted()
        if trusted == self._hotkey_trusted_last:
            return
        first_check = self._hotkey_trusted_last is None
        self._hotkey_trusted_last = trusted
        if trusted and not first_check:
            # Granted while running. Monitors added while untrusted can stay deaf, so
            # replace them now rather than asking her to quit and reopen.
            log("Accessibility granted — re-arming the hotkey")
            on_main(lambda: self.register_hotkey(force=True))
        elif not trusted and first_check:
            on_main(request_accessibility)

        def apply():
            if self.hotkey_line is None:
                return
            if trusted:
                self.hotkey_line.setHidden_(True)
            else:
                self.hotkey_line.setTitle_("⚠ Hotkey needs Accessibility permission")
                self.hotkey_line.setHidden_(False)
        on_main(apply)
        if not trusted:
            log("push-to-talk hotkey needs Accessibility permission — System "
                "Settings > Privacy & Security > Accessibility > turn MicMic on "
                "(the 'Listen now' menu item and the wake word work without it)")

    # ------------------------------------------------------------------ permissions
    def start_auth(self) -> None:
        """Ask for speech, then microphone, then start. Both prompts are real
        macOS dialogs and only appear because MicMic.app has the usage strings."""
        def speech_done(status):
            self.speech_auth = int(status)
            log(f"speech authorization status = {self.speech_auth} (3 = authorized)")
            AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AVFoundation.AVMediaTypeAudio, mic_done)

        def mic_done(granted):
            self.mic_auth = bool(granted)
            log(f"microphone access granted = {self.mic_auth}")
            on_main(self.begin)

        log("requesting speech recognition authorization…")
        Speech.SFSpeechRecognizer.requestAuthorization_(speech_done)

    def poll_permission_recovery(self) -> None:
        """begin() used to run exactly once, from applicationDidFinishLaunching_, so
        a denied permission was permanent until she quit and relaunched the app —
        even after fixing the switch in System Settings (bugs #5). TCC's own status
        calls (authorizationStatus / authorizationStatusForMediaType_) never prompt,
        so this can poll them safely in the background."""
        def tick():
            for _ in range(120):                       # ~10 minutes, then give up
                time.sleep(5.0)
                if self.running:
                    return
                speech = int(Speech.SFSpeechRecognizer.authorizationStatus())
                mic = int(AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
                    AVFoundation.AVMediaTypeAudio))
                if speech == 3 and mic == 3:
                    self.speech_auth = speech
                    self.mic_auth = True
                    log("permission granted in System Settings — resuming without a relaunch")
                    on_main(self.begin)
                    return
            log("still missing microphone/speech permission after 10 minutes of "
                "polling — giving up; relaunch MicMic after fixing it")
        threading.Thread(target=tick, daemon=True).start()

    # ------------------------------------------------------------------ setup
    def begin(self) -> None:
        if self.speech_auth != 3:
            self.set_status(L("speech_denied", self.locale), "⚠")
            log("speech recognition was not authorized — open System Settings > "
                "Privacy & Security > Speech Recognition and allow MicMic")
            alert("MicMic can't hear anything",
                  "Speech recognition was not allowed. Open System Settings > "
                  "Privacy & Security > Speech Recognition and turn MicMic on.")
            speak_local("speech_denied", self.locale)
            self.poll_permission_recovery()
            return
        if not self.mic_auth:
            self.set_status(L("mic_denied", self.locale), "⚠")
            log("microphone was not authorized — System Settings > Privacy & Security "
                "> Microphone and allow MicMic")
            alert("MicMic can't hear anything",
                  "Microphone access was not allowed. Open System Settings > "
                  "Privacy & Security > Microphone and turn MicMic on.")
            speak_local("mic_denied", self.locale)
            self.poll_permission_recovery()
            return

        self.cfg = get_config()
        # The built-in wake words are always in, on top of whatever the server
        # lists. A config that forgets them should not leave her unable to call it.
        self.variants = wake_variants(DEFAULT_WAKE + list(self.cfg.get("wake_words") or []))
        if not DEFAULT_LOCALE:
            self.locale = self.cfg.get("language_hint") or "he-IL"
        # Contact names as contextual strings measurably help the recognizer get
        # a name right. They come from the server, which already knows them.
        self.contextual = [str(c) for c in (self.cfg.get("contacts") or [])][:80]
        self.contextual += list(DEFAULT_WAKE)

        # get_config() above already tried and, on failure, silently returned {}.
        # A dead server should not leave the menu bar claiming to be "listening" —
        # probe it directly and, if it's down, say so out loud now rather than
        # optimistically declaring health we do not have (bugs #1).
        self.server_healthy = check_server_health()
        if not self.server_healthy:
            log("server health check failed at startup — /api/health did not answer")
            self.set_status(L("server_down", self.locale), "⚠")
            self.announce_failure("server_down")
            # Still proceed to start the microphone: when the server comes back
            # (the launcher retries it too — see MicMic.app/Contents/MacOS/MicMic)
            # the watchdog's own health poll will notice and recover the status.

        if not self._make_recognizers():
            return
        on_device = bool(self.recognizer.supportsOnDeviceRecognition())
        log(f"locale={self.locale} available={bool(self.recognizer.isAvailable())} "
            f"on_device={on_device} wake={sorted(self.variants)}")
        if not on_device:
            log(f"{self.locale} has no on-device model: every utterance goes to "
                f"Apple's servers. Dictation must be ON in System Settings.")

        if not self.start_engine():
            alert("MicMic can't hear anything",
                  "The microphone could not start. Quit MicMic, check that no "
                  "other app has exclusive use of it, and reopen it.")
            speak_local("mic_failed", self.locale)
            return
        self.running = True
        self.last_buffers_at = time.time()
        self.start_task()
        threading.Thread(target=self.watchdog, daemon=True).start()
        self.install_recovery_observers()
        self.register_hotkey()
        self.update_hotkey_status()
        self.set_status(L("listening", self.locale), "◉")

    def _make_recognizers(self) -> bool:
        """Her language, and the second one that runs beside it during a turn."""
        loc = Foundation.NSLocale.localeWithLocaleIdentifier_(self.locale)
        self.recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(loc)
        if self.recognizer is None:
            self.set_status(L("no_recognizer", self.locale), "⚠")
            log(f"SFSpeechRecognizer has no support for locale {self.locale}")
            alert("MicMic can't hear anything",
                  f"There is no speech recognizer for locale {self.locale!r} on "
                  "this Mac. Check MICMIC_LOCALE in listener.env.")
            speak_local("no_recognizer", self.locale)
            return False
        # The second language: English beside anything else; beside English, the
        # language she speaks to it otherwise (Hebrew unless her profile says).
        other = ALT_LOCALE
        if lang_code(self.locale) == lang_code(ALT_LOCALE):
            other = {"hebrew": "he-IL", "arabic": "ar-SA", "russian": "ru-RU"}.get(
                str(self.cfg.get("language") or "hebrew"), "he-IL")
        self.alt_locale = other
        if other:
            try:
                alt = Speech.SFSpeechRecognizer.alloc().initWithLocale_(
                    Foundation.NSLocale.localeWithLocaleIdentifier_(other))
                self.recognizer_alt = alt if alt is not None and alt.isAvailable() else None
            except Exception as e:  # noqa: BLE001
                log(f"no {other} recogniser beside {self.locale}: {e!r}")
                self.recognizer_alt = None
            if self.recognizer_alt is not None:
                log(f"turns also listen in {other}")
        return True

    def set_locale(self, locale: str) -> None:
        """Settings changed the speech language: rebuild both recognisers now."""
        if not locale or locale == self.locale or self.turn is not None:
            return
        old = self.locale
        self.locale = locale
        if self._make_recognizers():
            log(f"speech language {old} -> {locale}")
            self.start_task(force=True)
        else:
            self.locale = old
            self._make_recognizers()

    def start_engine(self) -> bool:
        try:
            self.engine = AVFoundation.AVAudioEngine.alloc().init()
            node = self.engine.inputNode()
            fmt = node.outputFormatForBus_(0)
            self.input_format = fmt
            log(f"input format: {fmt.sampleRate():.0f} Hz, {fmt.channelCount()} ch")

            def tap(buf, when):
                # Dropping the buffer is how we avoid hearing our own voice.
                if self.muted_until > time.time() or self.paused:
                    return
                req, alt = self.request, self.alt_request
                if req is not None:
                    try:
                        req.appendAudioPCMBuffer_(buf)
                        self.buffers += 1
                    except Exception as e:  # noqa: BLE001
                        if not self.tap_error:
                            self.tap_error = repr(e)
                            log(f"audio tap cannot feed the recognizer: {e!r}")
                if alt is not None:
                    try:
                        alt.appendAudioPCMBuffer_(buf)
                    except Exception:  # noqa: BLE001
                        pass
                rec = self.turn_audio
                if rec is not None:
                    try:
                        rec.writeFromBuffer_error_(buf, None)
                    except Exception:  # noqa: BLE001
                        self.turn_audio = None

            node.installTapOnBus_bufferSize_format_block_(0, 2048, fmt, tap)
            self.tap_installed = True
            self.engine.prepare()
            ok, err = self.engine.startAndReturnError_(None)
            if not ok:
                log(f"audio engine failed to start: {err}")
                self.set_status(L("mic_failed", self.locale), "⚠")
                return False
            log("audio engine running")
            return True
        except Exception as e:  # noqa: BLE001
            log(f"audio engine exception: {e!r}")
            self.set_status(L("mic_failed", self.locale), "⚠")
            return False

    def recover_engine(self, why: str) -> None:
        """AVAudioEngine does not restart itself after a route/config change (a
        device plugged/unplugged) or across sleep — README used to list both as
        "not tested" (bugs #2). Sleep/wake and device-change notifications, plus
        the watchdog's own buffer-stall check below, both land here."""
        log(f"rebuilding audio engine: {why}")
        try:
            if self.engine is not None:
                self.engine.stop()
        except Exception:  # noqa: BLE001
            pass
        self.tap_installed = False
        ok = self.start_engine()
        self.last_buffers_at = time.time()
        self.last_buffers = self.buffers
        if ok:
            log("audio engine rebuilt")
        else:
            log("audio engine rebuild failed — still deaf")
            self.announce_failure("mic_failed")

    def install_recovery_observers(self) -> None:
        nc = Foundation.NSNotificationCenter.defaultCenter()
        nc.addObserverForName_object_queue_usingBlock_(
            AVFoundation.AVAudioEngineConfigurationChangeNotification, None,
            Foundation.NSOperationQueue.mainQueue(),
            lambda note: self.recover_engine("audio route/config change"))
        AppKit.NSWorkspace.sharedWorkspace().notificationCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceDidWakeNotification, None,
            Foundation.NSOperationQueue.mainQueue(),
            lambda note: self.recover_engine("system wake from sleep"))

    def check_update(self) -> None:
        """Show "Update available" in the menu when the releases page has a newer one."""
        try:
            with urllib.request.urlopen(SERVER.rstrip("/") + "/api/update", timeout=10) as r:
                d = json.loads(r.read() or b"{}")
        except Exception:  # noqa: BLE001
            return
        if not d.get("newer") or self.update_item is None:
            return
        self.update_url = str(d.get("url") or "")
        title = L("update", self.locale, v=str(d.get("latest") or "").lstrip("vV"))
        log(f"update available: {d.get('latest')} (this is {d.get('current')})")

        def show():
            self.update_item.setTitle_(title)
            self.update_item.setHidden_(False)
        on_main(show)

    def refresh_settings(self) -> None:
        """Re-read the config the Settings screen writes, and apply what changed here.

        Only the hotkey and the wake words are the listener's to own; the rest
        (activation, language, listen seconds) is read fresh on each turn anyway.
        """
        cfg = get_config()
        if not cfg:
            return                       # server hiccup: keep what is already working
        self.cfg = cfg
        self.register_hotkey()           # no-op unless the spec actually changed
        if not DEFAULT_LOCALE and cfg.get("language_hint"):
            self.set_locale(str(cfg["language_hint"]))
        words = list(cfg.get("wake_words") or [])
        variants = wake_variants(DEFAULT_WAKE + words)
        if variants != self.variants:
            self.variants = variants
            log(f"wake words updated: {words}")

    # ------------------------------------------------------------------ push-to-talk
    def register_hotkey(self, force: bool = False) -> None:
        """A wake word is the only way in otherwise, and Apple mishears her made-up
        name often enough (README: מיקמק, מייק מייק, Mike Mike, Mick Mick) that
        there has to be a door in that does not depend on speech recognition at
        all (bugs #6). Default is a double-tap of Fn/Globe; Settings (or
        MICMIC_HOTKEY) can choose a lone modifier tap or a "mod+key" combination.

        `force` re-registers even when the spec is unchanged: a monitor added before
        Accessibility was granted can stay deaf after it is, so the moment trust
        arrives the monitors are replaced."""
        spec = (HOTKEY_OVERRIDE
                or str(self.cfg.get("hotkey") or "").strip().lower()
                or DEFAULT_HOTKEY)
        if spec == self.hotkey_active and self.hotkey_monitors and not force:
            return
        # Remove the old ones first, or a changed hotkey leaves the previous one live.
        for m in self.hotkey_monitors:
            try:
                AppKit.NSEvent.removeMonitor_(m)
            except Exception as e:  # noqa: BLE001
                log(f"could not remove an old hotkey monitor: {e!r}")
        self.hotkey_monitors = []
        try:
            if spec in ("", "fn-fn", "fn+fn", "globe-globe", "globe+globe"):
                self._register_double_fn()
                log("push-to-talk hotkey: double-tap Fn/Globe")
            elif spec in TAP_KEYS:
                self._register_tap(spec)
                log(f"push-to-talk hotkey: tap {spec}")
            else:
                self._register_combo(spec)
                log(f"push-to-talk hotkey: {spec}")
            self.hotkey_active = spec
        except Exception as e:  # noqa: BLE001
            log(f"could not register push-to-talk hotkey {spec!r}: {e!r} "
                f"— the 'Listen now' menu item still works")

    def _install(self, mask, handler) -> None:
        """One handler, two monitors. A GLOBAL monitor sees keys aimed at every app
        except this one — so on its own the hotkey went dead exactly when the MicMic
        window had focus. A LOCAL monitor covers our own window; it must hand the
        event back or the keystroke would be eaten."""
        def local(event):
            handler(event)
            return event
        self.hotkey_monitors.append(
            AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, handler))
        self.hotkey_monitors.append(
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, local))

    def _register_tap(self, spec: str) -> None:
        """A lone right-hand modifier: pressed opens a turn, released either sends it
        (held) or leaves it open until the next press (tapped). Used as a modifier in
        a chord (Option+E for an accent, Shift+Option-drag) it cancels quietly."""
        code, flag_name = TAP_KEYS[spec]
        flag = int(getattr(AppKit, flag_name))
        others = TapDetector.MODIFIERS & ~flag
        state = {"down": False, "chord": False}

        def handler(event):
            try:
                if event.type() == AppKit.NSEventTypeKeyDown:
                    if state["down"] and not state["chord"]:
                        state["chord"] = True
                        on_main(self.cancel_chord)
                    return
                key, flags = int(event.keyCode()), int(event.modifierFlags())
                if key != code:
                    if state["down"] and not state["chord"]:
                        state["chord"] = True
                        on_main(self.cancel_chord)
                    return
                if flags & flag:
                    if flags & others:
                        return                      # already part of a chord
                    state["down"], state["chord"] = True, False
                    on_main(self.press)
                elif state["down"]:
                    state["down"] = False
                    if not state["chord"]:
                        on_main(self.release)
            except Exception as e:  # noqa: BLE001
                log(f"hotkey handler error: {e!r}")
        self._install(AppKit.NSEventMaskFlagsChanged | AppKit.NSEventMaskKeyDown, handler)

    def _register_double_fn(self) -> None:
        state = {"last": 0.0, "was_down": False}

        def handler(event):
            try:
                down = bool(event.modifierFlags() & AppKit.NSEventModifierFlagFunction)
                if down and not state["was_down"]:
                    now = time.time()
                    if now - state["last"] < 0.5:
                        state["last"] = 0.0
                        on_main(self.push_to_talk)
                    else:
                        state["last"] = now
                state["was_down"] = down
            except Exception as e:  # noqa: BLE001
                log(f"hotkey handler error: {e!r}")
        self._install(AppKit.NSEventMaskFlagsChanged, handler)

    def _register_combo(self, spec: str) -> None:
        code, want_mask = parse_combo(spec)
        relevant = (AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagShift
                    | AppKit.NSEventModifierFlagOption | AppKit.NSEventModifierFlagControl)

        state = {"down": False}

        def handler(event):
            try:
                if event.keyCode() != code:
                    return
                if event.type() == AppKit.NSEventTypeKeyDown:
                    if event.isARepeat():
                        return                      # holding it: one press, not thirty
                    if (int(event.modifierFlags()) & relevant) == want_mask:
                        state["down"] = True
                        on_main(self.press)
                elif state["down"]:                 # key up; the modifiers may be gone
                    state["down"] = False
                    on_main(self.release)
            except Exception as e:  # noqa: BLE001
                log(f"hotkey handler error: {e!r}")
        self._install(AppKit.NSEventMaskKeyDown | AppKit.NSEventMaskKeyUp, handler)

    def push_to_talk(self) -> None:
        """"Listen now" in the menu and the double-Fn hotkey: no key to hold, so it is
        always a tap turn. Pressed again while open, it sends."""
        t = self.turn
        if t is not None and t["kind"] == "ptt" and t["finish_at"] == 0.0:
            self.finish_turn("pressed again")
            return
        if self.start_turn("ptt"):
            self.turn["mode"] = "toggle"

    # --- the hotkey as a key: hold to talk, or tap to start and tap to send --------
    def press(self) -> None:
        t = self.turn
        if t is not None and t["finish_at"]:
            log("turn: key pressed while it is being sent, ignored")
            self.swallow_release = True
            return
        if t is not None and t["kind"] == "ptt" and t["mode"] == "toggle" \
                and t["finish_at"] == 0.0:
            self.swallow_release = True
            self.finish_turn("tapped again")
            return
        self.swallow_release = False
        self.start_turn("ptt")

    def release(self) -> None:
        if self.swallow_release:
            self.swallow_release = False
            return
        t = self.turn
        if t is None or t["kind"] != "ptt" or t["mode"] is not None:
            return
        if time.time() - t["down_at"] >= HOLD_MIN:
            t["mode"] = "hold"
            self.finish_turn("released")
        else:
            t["mode"] = "toggle"                 # a tap: keep listening until the next one
            log("turn: tapped, listening until the key is tapped again")

    def cancel_chord(self) -> None:
        """The key was a modifier in a chord after all. Nothing she said is sent."""
        t = self.turn
        if t is None or t["kind"] != "ptt" or t["mode"] is not None:
            return
        log("turn: the hotkey was part of a chord, cancelled")
        self.end_turn_empty(sound=False)

    def start_turn(self, kind: str) -> bool:
        if not self.running:
            log("turn requested before the listener finished starting")
            return False
        if self.paused:
            log("turn requested while paused, ignoring")
            return False
        now = time.time()
        with self.lock:
            self.turn = {"kind": kind, "down_at": now, "mode": None, "finish_at": 0.0,
                         "deadline": now + (PTT_MAX if kind == "ptt" else FOLLOWUP_WINDOW)}
            self.transcript = ""
            self.alt_text = ""
            self.turn_prefix = ""
            self.primary_final = self.alt_final = None
            self.pending_tail = ""
            self.wake_at = 0.0
            # The watchdog's own armed-window expiry must not end a turn; the turn
            # has its own deadline.
            self.armed_until = now + PTT_MAX + 5.0
            self.activation = "push"
            self.muted_until = 0.0              # she wants the microphone now
            self.interrupt_until = 0.0
            self.last_change = now
            self._open_turn_audio()
        if kind == "ptt":
            stop_speaking()                     # pressing the key talks over MicMic
        self.start_task(force=True)
        self.set_status(L("push_to_talk", self.locale), "●")
        play_sound(SOUND_ARM)
        server_duck(True)
        log(f"turn open ({kind})")
        return True

    def _open_turn_audio(self) -> None:
        self._close_turn_audio()
        if self.recognizer_alt is None or self.input_format is None:
            return
        try:
            import tempfile
            fd, path = tempfile.mkstemp(prefix="micmic-turn-", suffix=".caf")
            os.close(fd)
            f, err = AVFoundation.AVAudioFile.alloc().initForWriting_settings_error_(
                Foundation.NSURL.fileURLWithPath_(path), self.input_format.settings(), None)
            if f is None:
                os.unlink(path)
                return
            self.turn_audio, self.turn_audio_path = f, path
        except Exception as e:  # noqa: BLE001
            log(f"cannot keep the turn's audio: {e!r}")

    def _close_turn_audio(self) -> str:
        """Stop writing; the file is complete once the AVAudioFile is gone."""
        f, path = self.turn_audio, self.turn_audio_path
        self.turn_audio = None
        if f is not None:
            try:
                if f.respondsToSelector_("close"):
                    f.close()
            except Exception:  # noqa: BLE001
                pass
            del f
        return path

    def _drop_turn_audio(self) -> None:
        path = self._close_turn_audio()
        self.turn_audio_path = ""
        if path:
            try:
                os.unlink(path)            # her voice is not kept past the turn
            except OSError:
                pass

    def _read_turn_audio_alt(self, path: str) -> None:
        """The second language reads the turn's audio. Main thread: Speech delivers
        results on the main queue, and the app's run loop is already turning."""
        def go():
            try:
                req = Speech.SFSpeechURLRecognitionRequest.alloc().initWithURL_(
                    Foundation.NSURL.fileURLWithPath_(path))
                if self.recognizer_alt.supportsOnDeviceRecognition():
                    req.setRequiresOnDeviceRecognition_(True)
                if self.contextual:
                    req.setContextualStrings_(self.contextual)
                self.alt_gen += 1
                self.alt_parts = []
                self._alt_live = ""
                gen = self.alt_gen
                self.alt_task = self.recognizer_alt.recognitionTaskWithRequest_resultHandler_(
                    req, lambda result, error: self.on_alt_result(gen, result, error))
            except Exception as e:  # noqa: BLE001
                log(f"second-language read failed: {e!r}")
                with self.lock:
                    self.alt_final = ("", 0.0)
        on_main(go)

    def finish_turn(self, why: str) -> None:
        """She is done. Ask both recognisers for their final words; consider() sends
        whichever is more confident once both answer, or after FINAL_WAIT."""
        with self.lock:
            t = self.turn
            if t is None or t["finish_at"]:
                return
            t["finish_at"] = time.time()
            for req in (self.request, self.alt_request):
                if req is not None:
                    try:
                        req.endAudio()
                    except Exception:  # noqa: BLE001
                        pass
        log(f"turn finishing: {why}")
        # Say so NOW. The final words and the second-language read take a moment, and
        # with nothing changing on screen she pressed again and threw the turn away.
        self.set_status(L("thinking", self.locale), "◐")
        self.view_working()
        self.consider()

    def end_turn_empty(self, sound: bool = True) -> None:
        with self.lock:
            self._drop_turn_audio()
            self.turn = None
            self.turn_prefix = ""
            self.armed_until = 0.0
            self.pending_tail = ""
            self.transcript = ""
            self.alt_text = ""
        server_duck(False)
        self.view_idle()
        self.set_status(L("listening", self.locale), "◉")
        if sound:
            play_sound(SOUND_DISARM)
        self.schedule_restart(0.2)

    # ------------------------------------------------------------------ recognition
    def start_task(self, force: bool = False) -> None:
        """Open a fresh recognition session. Sessions are disposable by design.

        Every session carries a generation number and its result handler ignores
        anything that arrives after the session was replaced. Without that, the
        error our own cancel() raises looks like a recognition failure, which
        triggers a restart, whose cancel raises the same error — a loop that
        hammered Apple three times a second the first time this ran.
        """
        with self.lock:
            now = time.time()
            if now - self.task_started < 0.7 and not force:
                return                       # rate limit: never churn sessions
            self.stop_task()
            req = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
            req.setShouldReportPartialResults_(True)
            if self.recognizer.supportsOnDeviceRecognition() and \
                    os.environ.get("MICMIC_ON_DEVICE", "1") != "0":
                req.setRequiresOnDeviceRecognition_(True)
            try:
                if self.contextual:
                    req.setContextualStrings_(self.contextual)
            except Exception:  # noqa: BLE001
                pass
            self.gen += 1
            gen = self.gen
            self.request = req
            self.transcript = ""
            self.last_change = now
            self.task_started = now
            self.task = self.recognizer.recognitionTaskWithRequest_resultHandler_(
                req, lambda result, error: self.on_result(gen, result, error))
            self.primary_final = None
            # Never a second live session (see PRIMARY_SURE): it would kill this one.
            if False:
                alt = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
                alt.setShouldReportPartialResults_(True)
                try:
                    if self.recognizer_alt.supportsOnDeviceRecognition():
                        alt.setRequiresOnDeviceRecognition_(True)
                    if self.contextual:
                        alt.setContextualStrings_(self.contextual)
                except Exception:  # noqa: BLE001
                    pass
                self.alt_request = alt
                self.alt_text = ""
                self.alt_final = None
                self.alt_task = self.recognizer_alt.recognitionTaskWithRequest_resultHandler_(
                    alt, lambda result, error: self.on_alt_result(gen, result, error))

    def stop_task(self) -> None:
        with self.lock:
            self.gen += 1                    # anything still in flight is stale now
            if self.task is not None:
                try:
                    self.task.cancel()
                except Exception:  # noqa: BLE001
                    pass
            if self.request is not None:
                try:
                    self.request.endAudio()
                except Exception:  # noqa: BLE001
                    pass
            self.task = None
            self.request = None

    def on_alt_result(self, gen, result, error) -> None:
        """The second language. It only records; the turn decides in consider()."""
        try:
            if gen != self.alt_gen:
                return
            if error is not None:
                with self.lock:
                    if self.alt_final is None:
                        self.alt_final = ("", 0.0)     # finished with nothing
                return
            if result is None:
                return
            norm = normalize(str(result.bestTranscription().formattedString()))
            with self.lock:
                if result.isFinal():
                    # A file read ends one utterance at each pause, like the live one;
                    # keep every piece rather than only the last ("פליז" for "undo ...
                    # please").
                    words = len(norm.split())
                    if words:
                        self.alt_parts.append((norm, _final_conf(result), words))
                    self._alt_live = ""
                    text = " ".join(x[0] for x in self.alt_parts)
                    wsum = sum(x[2] for x in self.alt_parts)
                    conf = sum(x[1] * x[2] for x in self.alt_parts) / max(1, wsum)
                    self.alt_final = (text, conf)
                    self.alt_text = text
                elif norm:
                    live = getattr(self, "_alt_live", "")
                    if new_utterance(live, norm):
                        self.alt_parts.append((live, 0.5, len(live.split())))
                    self._alt_live = norm
                    self.alt_text = " ".join([x[0] for x in self.alt_parts] + [norm])
            if result.isFinal():
                self.consider()
        except Exception as e:  # noqa: BLE001
            log(f"alt result handler blew up: {e!r}")

    def on_result(self, gen, result, error) -> None:
        """Called by the Speech framework on its own queue, repeatedly, as the
        transcript grows. Everything here is cheap; the work happens in watchdog."""
        try:
            if gen != self.gen:
                return                       # a session we have already replaced
            if error is not None:
                code = int(error.code())
                with self.lock:
                    self.task = None         # this session is over either way
                    t = self.turn
                    if t is not None and t["finish_at"] and self.primary_final is None:
                        # Finishing and the session ended with no final: what it heard
                        # so far is all there is.
                        self.primary_final = ((self.turn_prefix + " " + self.transcript).strip(), 0.0)
                if code == 1110:
                    # "No speech detected" — routine. Apple closes a session that
                    # heard nothing; the watchdog opens the next one.
                    if DEBUG:
                        log("  session closed: no speech detected")
                    return
                log(f"recognition error {error.domain()} {code}: "
                    f"{error.localizedDescription()}")
                if code == 203:
                    log("  code 203 'Retry' usually means Dictation has never been "
                        "turned on in System Settings > Keyboard > Dictation")
                    time.sleep(1.0)
                return
            if result is None:
                return
            text = str(result.bestTranscription().formattedString())
            norm = normalize(text)
            if DEBUG and norm != self.transcript:
                log(f"  heard[{'final' if result.isFinal() else 'part'}]: {text!r}")
            restart = False
            with self.lock:
                final = bool(result.isFinal())
                t = self.turn
                conf = _final_conf(result) if final else 0.0
                words = len(norm.split())
                if t is not None and final and words:
                    t.setdefault("confs", []).append((conf, words))
                if t is not None and final and not t["finish_at"]:
                    # Speech ended one utterance at her pause; the turn is not over.
                    # Keep what it heard and open the next session, or everything
                    # before the pause is lost ("undo ... please" arrived as "please").
                    self.turn_prefix = (self.turn_prefix + " " + norm).strip()
                    self.transcript = ""
                    self.last_change = time.time()
                    restart, final = True, False
                else:
                    if t is not None and new_utterance(self.transcript, norm):
                        t.setdefault("confs", []).append((0.5, len(self.transcript.split())))
                        self.turn_prefix = (self.turn_prefix + " " + self.transcript).strip()
                    if norm != self.transcript:
                        self.transcript = norm
                        self.last_change = time.time()
                    if final:
                        if t is not None:
                            # Judged on the whole turn: the last piece after a pause can
                            # be one word, or empty, and scored 0.00 for a clear sentence.
                            cs = t.get("confs") or [(conf, 1)]
                            whole = sum(c * w for c, w in cs) / max(1, sum(w for _, w in cs))
                            self.primary_final = ((self.turn_prefix + " " + norm).strip(), whole)
                        else:
                            self.primary_final = (norm, conf)
                armed = self.armed_until > time.time()
                shown = (self.turn_prefix + " " + text).strip() if t is not None else text
            if restart:
                self.start_task(force=True)
                return
            # Only while armed. In wake mode the recogniser hears the whole room all the
            # time; putting that on screen would pop the bar up for every conversation.
            if armed:
                self.view_heard(shown)
            self.consider(final=final)
        except Exception as e:  # noqa: BLE001
            log(f"result handler blew up: {e!r}")

    def schedule_restart(self, delay: float = 0.3) -> None:
        def go():
            time.sleep(delay)
            # A turn opened meanwhile owns the session; restarting it would drop
            # the words she has already said into it.
            if self.running and not self.paused and self.turn is None:
                self.start_task()
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------------ decisions
    def consider(self, final: bool = False) -> None:
        """Look at the transcript so far and decide whether anything is owed."""
        now = time.time()
        if self.turn is not None:
            self._consider_turn(now, final)
            return
        with self.lock:
            if self.muted_until > now or self.paused:
                return
            text = self.transcript
            if not text:
                return
            # Was that us talking rather than her? Only a long fragment counts. The
            # narration before a send is "…תגידי ביטול אם לא" — it contains the very
            # word she has to say to stop it, so a plain substring test silently ate
            # every cancellation. Four words and twelve characters is an echo; one
            # word is a person.
            tokens = text.split()
            if (self.last_spoken and len(tokens) >= 4 and len(text) >= 12
                    and text in self.last_spoken):
                return

            # A long ordinary answer is still (partly) playing. We are not fully
            # muted for it any more (bugs #8) — reopened early so she is never
            # deafened for the whole 20s with no way to interrupt — but everything
            # heard in this window is judged ONLY as a stop word. Anything else is
            # her own reply playing back, or unrelated room noise, and dispatching
            # it as a fresh command would be worse than missing a real one.
            if self.interrupt_until > now:
                if is_stop_word(text):
                    log(f"stop word heard mid-answer: {text!r} — going quiet "
                        f"(the `say` process itself is not ours to kill from here; "
                        f"see README/bugs #8)")
                    self.interrupt_until = 0.0
                    self.transcript = ""
                    self.last_change = now
                    self.gen += 1
                    stop_now = True
                else:
                    return
            else:
                stop_now = False

            if not stop_now:
                armed = self.armed_until > now
                # A send is counting down. Six seconds sounds generous until you hear
                # the machine spend three and a half of them reading the message back,
                # so stop waiting for a comfortable pause and act on the first word.
                enough = CANCEL_END if self.countdown_until > now else SILENCE_END

                if armed:
                    tail = " ".join(tokens)
                else:
                    hit = find_wake(tokens, self.variants)
                    if hit is None:
                        return
                    if self.wake_at == 0.0:
                        self.wake_at = now
                        self.set_status(L("heard_wake", self.locale), "●")
                    tail = " ".join(tokens[hit[1] + 1:])

                self.pending_tail = tail
                quiet = now - self.last_change
                if not tail:
                    # She said only the wake word. Apple will not give us a second
                    # utterance in this recognition session, so there is no point
                    # waiting: arm, and let the watchdog hand us a fresh session. Her
                    # next sentence is then taken whole, with no wake word needed.
                    if quiet >= enough:
                        self.armed_until = now + WAKE_GRACE
                        self.activation = "wake"
                        self.wake_at = 0.0
                        # Forget the wake word itself. Once armed, whatever is in the
                        # transcript is treated as her command — and "מיקמיק" on its own
                        # would be sent to the server as one.
                        self.transcript = ""
                        self.last_change = now
                        self.task_started = 0.0     # watchdog: fresh session, now
                        self.set_status(L("armed_grace", self.locale), "●")
                        play_sound(SOUND_ARM)
                    return
                if not (final or quiet >= enough):
                    return
                to_send = tail
                self.pending_tail = ""
                self.wake_at = 0.0
                self.armed_until = 0.0
                if not armed:
                    # Dispatched straight off a matched wake word in the same
                    # sentence ("מיקמיק תתקשרי לדנה") — armed_until (and whatever
                    # it was last set to: push, a previous wake-grace, or a
                    # countdown cancel window) never came into it here.
                    self.activation = "wake"
                activation = self.activation
                # Mute now: the reply is spoken the moment the server answers.
                self.muted_until = now + 2.0
                # Dispatch and transcript-invalidation happen atomically, in the
                # same lock, instead of relying on schedule_restart()'s delayed
                # start_task() to clear self.transcript later — that delay used to
                # leave the just-dispatched sentence sitting in self.transcript,
                # and the next watchdog poll matched the same wake word + command
                # again and posted it a second time (bugs #3: 'thot a' sent twice
                # in one second, see micmic-listener.log 11:05:53).
                self.transcript = ""
                self.last_change = now
                self.gen += 1
                if to_send == self.last_sent_text and now - self.last_sent_at < DUP_WINDOW:
                    log(f"↺ dropped duplicate of {to_send!r} within {DUP_WINDOW:.0f}s")
                    return
                self.last_sent_text = to_send
                self.last_sent_at = now

        if stop_now:
            self.view_hide()
            self.set_status(L("listening", self.locale), "◉")
            play_sound(SOUND_DISARM)
            # The session that just heard the stop word already produced one
            # final result; Apple goes quiet after that (see watchdog's docstring)
            # rather than erroring, so ask for a fresh one now instead of waiting
            # for TASK_MAX to notice it went stale.
            self.schedule_restart(0.3)
            return

        log(f"→ {to_send!r} (activation={activation})")
        if self.panel is not None:
            on_main(lambda: self.panel.set_heard(to_send))
        self.view_heard(to_send)
        self.set_status(L("thinking", self.locale), "◐")
        threading.Thread(target=self.send, args=(to_send, activation), daemon=True).start()

    def _open_followup(self, spoken: float) -> None:
        """Wait for the question to finish being spoken, then listen for the answer."""
        started = time.time()
        time.sleep(0.4)
        while time.time() - started < spoken + 3.0 and is_speaking():
            time.sleep(0.15)
        time.sleep(0.25)                   # the tail of the voice, and the room
        if self.turn is None and not self.paused:
            on_main(lambda: self.start_turn("followup"))

    def _consider_turn(self, now: float, final: bool) -> None:
        """A turn is open: nothing is sent on a pause (unless MicMic asked a question
        and is waiting for the answer); it is sent when she ends it."""
        with self.lock:
            t = self.turn
            if t is None:
                return
            said = (self.turn_prefix + " " + self.transcript).strip() or self.alt_text
            self.pending_tail = said
            if not t["finish_at"]:
                quiet = now - self.last_change
                if t["kind"] == "followup":
                    if said and quiet >= FOLLOWUP_SILENCE:
                        t["finish_at"] = now
                    elif not said and now > t["deadline"]:
                        t["finish_at"] = now
                    else:
                        return
                elif now > t["deadline"]:
                    t["finish_at"] = now
                else:
                    return
                for req in (self.request, self.alt_request):
                    if req is not None:
                        try:
                            req.endAudio()
                        except Exception:  # noqa: BLE001
                            pass
            if self.primary_final is None and now < t["finish_at"] + FINAL_WAIT:
                return
            prim_conf = (self.primary_final or ("", 0.0))[1]
            unsure = (self.recognizer_alt is not None and self.turn_audio_path
                      and prim_conf < PRIMARY_SURE)
            if not t.get("logged"):
                t["logged"] = True
                log(f"turn: {self.locale} {prim_conf:.2f} "
                    f"({'final' if self.primary_final else 'no final'}), "
                    f"audio={'kept' if self.turn_audio_path else 'none'}, "
                    f"second language {'will read it' if unsure else 'not needed'}")
            if unsure and not t.get("alt_at"):
                t["alt_at"] = now
                self.alt_final = None
                self._read_turn_audio_alt(self._close_turn_audio())
                return
            if unsure and now < t["alt_at"] + ALT_WAIT and not self._alt_done():
                return
            to_send, lang_won = self._pick_transcript()
            self._drop_turn_audio()
            self.turn_prefix = ""
            self.turn = None
            self.pending_tail = ""
            self.armed_until = 0.0
            self.muted_until = now + 2.0
            self.transcript = ""
            self.alt_text = ""
            self.last_change = now
            self.gen += 1
            if to_send:
                self.last_sent_text = to_send
                self.last_sent_at = now
        if not to_send:
            log("turn ended with nothing said")
            self.end_turn_empty()
            return
        log(f"→ {to_send!r} (turn, {lang_won})")
        if self.panel is not None:
            on_main(lambda: self.panel.set_heard(to_send))
        self.view_heard(to_send)
        self.set_status(L("thinking", self.locale), "◐")
        threading.Thread(target=self.send, args=(to_send, "push", t["finish_at"]),
                         daemon=True).start()

    def _alt_done(self) -> bool:
        task = self.alt_task
        if self.alt_final is None:
            return False
        try:
            return task is None or int(task.state()) == 4     # SFSpeechRecognitionTaskStateCompleted
        except Exception:  # noqa: BLE001
            return True

    def _pick_transcript(self) -> tuple[str, str]:
        """Her language or English, by the recognisers' own final confidence. Falls
        back to whatever text there is when a final never came."""
        prim = self.primary_final or ((self.turn_prefix + " " + self.transcript).strip(), 0.0)
        alt = self.alt_final or (self.alt_text, 0.0)
        if not alt[0]:
            return prim[0], self.locale
        if not prim[0]:
            return alt[0], self.alt_locale
        log(f"turn: {self.locale} {prim[1]:.2f} {prim[0]!r} vs {self.alt_locale} {alt[1]:.2f} {alt[0]!r}")
        if alt[1] > prim[1]:
            return alt[0], f"{self.alt_locale} {alt[1]:.2f} over {prim[1]:.2f}"
        return prim[0], f"{self.locale} {prim[1]:.2f} over {alt[1]:.2f}"

    def send(self, text: str, activation: str = "wake", released_at: float = 0.0) -> None:
        server_duck(False)            # she has finished speaking: bring the music back
        posted_at = time.time()
        try:
            res = post_utterance(text, activation=activation)
        except urllib.error.URLError as e:
            log(f"server unreachable: {e!r}  (is `python3 -m savta.server` running?)")
            self.set_status(L("server_down", self.locale), "⚠")
            self.announce_failure("server_down")
            with self.lock:
                self.muted_until = 0.0
            self.schedule_restart(0.2)
            return
        except Exception as e:  # noqa: BLE001
            log(f"post failed: {e!r}")
            self.set_status(L("unreachable", self.locale), "⚠")
            self.announce_failure("unreachable")
            self.schedule_restart(0.2)
            return

        did = res.get("did")
        said = str(res.get("say") or "")
        # A reply read off the screen is the screen: the log keeps its length only,
        # like the trace does (savta/trace.py SCREEN_DIDS).
        det0 = res.get("detail") if isinstance(res.get("detail"), dict) else {}
        if did in SCREEN_DIDS or det0.get("from_screen"):
            log(f"← {did}  [from the screen, {len(said)} chars]")
        else:
            log(f"← {did}  {said[:120]!r}")
        # The reply is already being spoken when it arrives (the server says it before
        # answering), so this is release to first word, less the voice's own start.
        tm = res.get("timing") if isinstance(res.get("timing"), dict) else {}
        log(f"timing: release->dispatch {(posted_at - released_at) if released_at else -1:.3f}s "
            f"dispatch->reply {time.time() - posted_at:.3f}s server {res.get('ms')}ms "
            f"jev {tm.get('jev_n')}x{tm.get('jev_ms')}ms llm {tm.get('llm_n')}x{tm.get('llm_ms')}ms")
        if self.panel is not None and said:
            on_main(lambda: self.panel.set_reply(said))
        undo = res.get("undo") if isinstance(res.get("undo"), dict) else None
        self.view_result(said, (undo or {}).get("label"))
        spoken = speech_seconds(said)
        detail = res.get("detail") if isinstance(res.get("detail"), dict) else {}
        countdown = float(detail.get("countdown") or 0.0)

        with self.lock:
            self.last_spoken = normalize(said)
            self.interrupt_until = 0.0
            if countdown > 0:
                # A message is counting down and she must be able to say "stop"
                # with no wake word. The old code capped the self-mute to
                # countdown*0.45 so there was still "her time" left in a fixed
                # six-second window — but that reopened the mic WHILE the Mac was
                # still reading "…תגידי לי לא ואני עוצרת" out loud, and the tail of
                # our own sentence (containing the exact cancel word) got treated
                # as her objection (bugs #4). Stay fully deaf for the whole
                # narration instead, and only start the cancel window once it
                # should be over — never shorter than the server's own countdown.
                # The real fix is a true end-of-speech signal from the server
                # (mac.say() in savta/actions/mac.py is a fire-and-forget Popen
                # with no completion callback); this is the best mitigation
                # possible from the client side until that exists.
                now = time.time()
                self.muted_until = now + spoken
                self.armed_until = max(self.muted_until, now + countdown) + 1.0
                self.countdown_until = self.armed_until
                # The mic is open here without her having asked for it — exactly
                # what the not-for-us filter (savta/router.py) exists for. Tag it
                # "wake", not "push": the PENDING/cancel check runs well before
                # the filter is ever reached, so a real cancel word still short-
                # circuits past it — verified against a live countdown (ביטול,
                # לא, stop, רגע all still cancel) — while anything that is NOT a
                # cancel word (e.g. someone else in the room talking) now gets
                # filtered instead of being treated as a fresh command.
                self.activation = "wake"
                self.set_status(L("countdown", self.locale, s=countdown), "◼")
            elif res.get("asked_back"):
                # MicMic asked her something ("what should the message say?"). Her
                # answer is the whole point, so she should not have to press anything:
                # stay deaf while the question is spoken, then open a turn by itself.
                now = time.time()
                self.muted_until = now + spoken + 1.0
                self.interrupt_until = 0.0
                self.armed_until = 0.0
                self.countdown_until = 0.0
                threading.Thread(target=self._open_followup, args=(spoken,),
                                 daemon=True).start()
            else:
                # An ordinary answer can run up to MUTE_MAX = 20s, and used to
                # deafen us for the whole thing with no way to interrupt it — she'd
                # repeat "מיקמיק די" into a microphone that was switched off
                # (bugs #8). Reopen after a short, capped mute and listen for a
                # stop word for the rest of the narration; see consider()'s
                # interrupt_until handling for why nothing else is acted on there.
                now = time.time()
                self.muted_until = now + min(spoken, INTERRUPT_CAP)
                self.interrupt_until = now + spoken
                self.armed_until = 0.0
                self.countdown_until = 0.0
                self.set_status(L("listening", self.locale), "◉")
        # Fresh recognition task: the old transcript already contains the command
        # and would otherwise be matched again.
        self.schedule_restart(max(0.2, min(spoken, 3.0)))

    # ------------------------------------------------------------------ watchdog
    def watchdog(self) -> None:
        """Endpointing (she stopped talking) and session recycling live here.

        Two things make this necessary. The Speech callback only fires when the
        transcript changes, and silence by definition does not change it. And a
        recognition session for a network locale hands back one utterance and then
        stops answering — no final result, no error, just silence — so a session
        that has already produced a sentence has to be thrown away and replaced or
        the microphone is effectively dead from then on.
        """
        while self.running:
            time.sleep(TICK)
            try:
                now = time.time()
                if DEBUG and now - self.last_debug > 5:
                    self.last_debug = now
                    log(f"  debug: buffers={self.buffers} transcript={self.transcript!r} "
                        f"task={'live' if self.task else 'none'} "
                        f"armed={max(0, self.armed_until - now):.1f} "
                        f"muted={max(0, self.muted_until - now):.1f}")

                # /api/health, not just "did the last POST succeed" — otherwise a
                # server that has been down since before we last tried to reach it
                # never gets noticed, and the menu bar keeps lying (bugs #1).
                if now - self.last_health_check > HEALTH_POLL:
                    self.last_health_check = now
                    healthy = check_server_health()
                    if healthy != self.server_healthy:
                        if healthy:
                            log("server is answering again")
                            if not self.paused:
                                self.set_status(L("listening", self.locale), "◉")
                        else:
                            log("server stopped answering /api/health")
                            self.set_status(L("server_down", self.locale), "⚠")
                            self.announce_failure("server_down")
                        self.server_healthy = healthy
                    # Settings are changed in a web page, not here, so the listener has
                    # to notice. Same cadence as the health probe: a hotkey the user
                    # just chose should start working without quitting the app.
                    if healthy:
                        self.refresh_settings()
                        if now - self.last_update_check > 600:
                            self.last_update_check = now
                            self.check_update()

                if now - self.last_hotkey_check > 5.0:
                    self.last_hotkey_check = now
                    self.update_hotkey_status()

                if self.paused:
                    continue

                # A recognition session staying open is not proof the microphone
                # is alive: AVAudioEngine does not restart itself after a route
                # change or sleep, and buffers (already tracked, just never
                # checked outside MICMIC_DEBUG) is the one signal that actually
                # moves only when real audio is arriving (bugs #2).
                if self.muted_until <= now and not self.paused:
                    if self.buffers != self.last_buffers:
                        self.last_buffers = self.buffers
                        self.last_buffers_at = now
                    elif now - self.last_buffers_at > ENGINE_STALL:
                        # AVAudioEngine setup elsewhere always runs on the main
                        # thread (begin() is reached via on_main); stay consistent
                        # rather than touching the engine from this watchdog thread.
                        self.last_buffers_at = now   # don't re-fire every tick while it rebuilds
                        on_main(lambda: self.recover_engine(
                            f"no audio buffers for {ENGINE_STALL:.0f}s — engine looks dead"))

                self.consider(final=False)   # may send, and may clear the transcript

                # An armed window (wake-grace or push-to-talk) that nobody talked
                # into just expires in total silence otherwise — the one status
                # line she has left up lying about still being open (bugs #7).
                with self.lock:
                    stale_arm = (self.armed_until != 0.0 and self.armed_until <= now
                                 and self.countdown_until <= now)
                    if stale_arm:
                        self.armed_until = 0.0
                if stale_arm:
                    server_duck(False)
                    log("armed window expired with nothing heard")
                    self.view_idle()
                    self.set_status(L("listening", self.locale), "◉")
                    play_sound(SOUND_DISARM)

                with self.lock:
                    started = self.task_started
                    missing = self.task is None
                    spoke = bool(self.transcript)
                    quiet = now - self.last_change
                    busy = bool(self.pending_tail)
                spent = (spoke and quiet >= STALE_AFTER and not busy)
                idle = (now - started > TASK_MAX and not spoke)
                if missing or spent or idle:
                    self.start_task()
            except Exception as e:  # noqa: BLE001
                log(f"watchdog: {e!r}")

    # ------------------------------------------------------------------ controls
    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            self.stop_task()
            self.set_status(L("paused", self.locale), "○")
        else:
            self.start_task()
            self.set_status(L("listening", self.locale), "◉")

    def shutdown(self) -> None:
        self.running = False
        self.stop_task()
        try:
            if self.engine is not None:
                self.engine.stop()
        except Exception:  # noqa: BLE001
            pass


# A menu bar is thirty small grey shapes in a row. "◉" was one more of them, so
# "I opened MicMic and nothing happened" really meant "I could not find it".
# An SF Symbol mic reads as a microphone at a glance and matches every other
# native icon up there. The text glyphs stay as the fallback for macOS versions
# without a given symbol, so this can only ever improve on what was there.
_SYMBOLS = {
    "◉": "mic",                             # idle, listening for the wake word
    "●": "mic.fill",                        # hearing you right now
    "◐": "ellipsis.circle",                 # thinking
    "◼": "stop.circle",                     # counting down
    "⚠": "exclamationmark.triangle.fill",   # something needs attention
}


def _apply_glyph(button, glyph: str) -> None:
    name = _SYMBOLS.get(glyph)
    image = None
    if name and hasattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "MicMic")
    if image is not None:
        image.setTemplate_(True)   # so it follows light/dark menu bars
        button.setImage_(image)
        button.setTitle_("")
    else:
        button.setImage_(None)
        button.setTitle_(glyph)


LISTENER = Listener()


class Delegate(AppKit.NSObject):
    def applicationDidFinishLaunching_(self, note):
        LISTENER.build_status_item(self)
        LISTENER.start_auth()
        # Double-clicking an app and getting no window, no Dock icon and no sound is
        # indistinguishable from a crash — this was reported as "nothing opens up".
        # Launched by hand: show the window. Launched at login by launchd: stay quiet.
        if os.environ.get("MICMIC_AUTOSTART") != "1":
            # Launched by hand. "panel" opens the window; "bar" and "none" were chosen
            # precisely so nothing big appears, and the menu bar icon already says it
            # is running. Nothing chosen yet counts as the bar.
            # Settings are normally read later, after the microphone check; read them
            # now, or a saved "panel" choice would be ignored on every launch.
            LISTENER.cfg = get_config() or LISTENER.cfg or {}
            # A first launch opens the window whatever the display setting says: as
            # an invisible bar a new download showed nothing but a menu bar icon.
            if LISTENER.display_mode() == "panel" or LISTENER.cfg.get("first_run"):
                self.showPanel_(None)
        Foundation.NSDistributedNotificationCenter.defaultCenter() \
            .addObserver_selector_name_object_(
                self, objc.selector(self.showFromOtherLaunch_, signature=b"v@:@"),
                SHOW_NOTE, None)

    def showFromOtherLaunch_(self, note):
        log("a second launch asked for the window — showing it")
        self.showPanel_(None)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        # A click on the Dock icon while MicMic runs arrives here, not as a new launch.
        self.showPanel_(None)
        return True

    def togglePause_(self, sender):
        paused = not LISTENER.paused
        LISTENER.set_paused(paused)
        sender.setTitle_("Resume listening" if paused else "Pause listening")

    def listenNow_(self, sender):
        LISTENER.push_to_talk()

    def setUpShortcut_(self, sender):
        request_accessibility()
        AppKit.NSWorkspace.sharedWorkspace().openURL_(
            Foundation.NSURL.URLWithString_(ACCESSIBILITY_PANE))

    def showPanel_(self, sender):
        if LISTENER.panel is not None:
            LISTENER.panel.show()
        else:
            # No window could be built — a browser tab beats silence.
            self.openWeb_(sender)

    def openUpdate_(self, sender):
        url = getattr(LISTENER, "update_url", "") or ""
        if url.startswith("https://"):
            AppKit.NSWorkspace.sharedWorkspace().openURL_(Foundation.NSURL.URLWithString_(url))

    def openWeb_(self, sender):
        AppKit.NSWorkspace.sharedWorkspace().openURL_(
            Foundation.NSURL.URLWithString_(SERVER))

    def showLog_(self, sender):
        AppKit.NSWorkspace.sharedWorkspace().openFile_(LOG_PATH)

    def quitApp_(self, sender):
        LISTENER.shutdown()
        AppKit.NSApp().terminate_(self)


# ---------------------------------------------------------------- one copy only
# With an icon in the Dock, clicking it while MicMic is already running launches a
# SECOND copy: two listeners on one microphone, two recognisers transcribing the same
# audio, two windows. Nothing prevented that. The first copy now holds a lock, and a
# second launch only asks it to show its window, then leaves.
SHOW_NOTE = "com.betterfly.micmic.show"
_LOCK_FD = None


def take_single_instance_lock() -> bool:
    """True if this is the only listener. The lock lives in the user's state folder,
    never beside this file: inside a signed .app that would be a write into Contents/."""
    global _LOCK_FD
    import fcntl
    folder = os.path.expanduser("~/Library/Application Support/MicMic")
    os.makedirs(folder, exist_ok=True)
    _LOCK_FD = open(os.path.join(folder, "listener.lock"), "w")
    try:
        fcntl.flock(_LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _LOCK_FD.write(str(os.getpid())); _LOCK_FD.flush()
    return True                       # held until the process exits; the OS releases it


def main() -> None:
    log("=" * 60)
    if not take_single_instance_lock():
        log(f"another MicMic is already running — asking it to show its window (pid={os.getpid()} exits)")
        Foundation.NSDistributedNotificationCenter.defaultCenter() \
            .postNotificationName_object_userInfo_deliverImmediately_(SHOW_NOTE, None, None, True)
        return
    log(f"MicMic listener starting  pid={os.getpid()}  server={SERVER}")
    ensure_server()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    delegate = Delegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    # A one-shot self test that needs no microphone and no permissions, so the
    # wake-word matching can be checked from a terminal: `python3 listener.py --check`
    if "--check" in sys.argv:
        v = wake_variants(DEFAULT_WAKE)
        cases = [
            ("מיקמיק תתקשרי לדנה", "תתקשרי לדנה"),
            ("מיק מיק מה השעה", "מה השעה"),
            ("היי מיקמיק, נגני מוזיקה", "נגני מוזיקה"),
            ("hey MicMic, what time is it?", "what time is it"),
            ("mic mic play music", "play music"),
            ("Mic-Mic!! call Dana", "call dana"),
            ("מייקמייק תשלחי הודעה לדנה", "תשלחי הודעה לדנה"),
            # what Apple's recogniser actually returned on this Mac, one letter short
            ("מיקמק מה השעה", "מה השעה"),
            ("מיקמיק", ""),
            # transcripts Apple actually returned for a spoken wake word
            ("Mike Mike what time is it", "what time is it"),
            ("Hey Mick Mick call Dana", "call dana"),
            ("מייק מייק תדליקי אור", "תדליקי אור"),
            # The wake word must not eat the first word of the request. "mic"+"make"
            # is within one edit of "micmic", so a fuzzy match at the wrong offset
            # used to swallow the verb and leave "it louder".
            ("mic mic make it louder", "make it louder"),
            ("mic mic make a note", "make a note"),
            ("hey mic mic mark this down", "mark this down"),
            ("מיקמיק תעשי לי רשימה", "תעשי לי רשימה"),
            # but a genuinely second wake word still wins, so she can restate
            ("micmic play music micmic stop", "stop"),
            ("just talking about nothing", None),
            ("אמא הלכה לשוק", None),
            ("תשלחי הודעה לדנה", None),
        ]
        bad = 0
        for raw, want in cases:
            toks = normalize(raw).split()
            hit = find_wake(toks, v)
            got = " ".join(toks[hit[1] + 1:]) if hit else None
            ok = (got == want)
            bad += 0 if ok else 1
            print(("ok  " if ok else "FAIL"), repr(raw), "->", repr(got))

        # is_stop_word (bugs #8): exact-token match, not substring — a substring
        # test is exactly what let an echoed narration cancel a message nobody
        # objected to (bugs #4), so the same mistake must not come back here.
        stop_cases = [
            ("די", True), ("תפסיקי בבקשה", True), ("stop", True),
            ("stop it please", True), ("хватит", True), ("توقفي", True),
            ("מה השעה", False), ("דיאטה", False),  # "דיאטה" contains "די" but is not it
            ("", False),
        ]
        for raw, want in stop_cases:
            got = is_stop_word(normalize(raw))
            ok = (got == want)
            bad += 0 if ok else 1
            print(("ok  " if ok else "FAIL"), "is_stop_word", repr(raw), "->", got)

        # parse_combo (bugs #6): MICMIC_HOTKEY parsing needs no AppKit/display, so
        # a bad value in listener.env is caught here, not the first time someone
        # presses the hotkey.
        combo_cases = [
            ("cmd+shift+m", (46, _MODIFIER_BITS["cmd"] | _MODIFIER_BITS["shift"])),
            ("m", (46, 0)),
            ("ctrl+opt+a", (0, _MODIFIER_BITS["ctrl"] | _MODIFIER_BITS["opt"])),
        ]
        for spec, want in combo_cases:
            got = parse_combo(spec)
            ok = (got == want)
            bad += 0 if ok else 1
            print(("ok  " if ok else "FAIL"), "parse_combo", repr(spec), "->", got)
        for spec in ("cmd+nosuchkey", "nosuchmod+m", ""):
            try:
                parse_combo(spec)
                ok = False
            except ValueError:
                ok = True
            bad += 0 if ok else 1
            print(("ok  " if ok else "FAIL"), "parse_combo rejects", repr(spec))

        # TapDetector: a lone modifier tap, driven with synthetic key sequences. The
        # right-command key is code 54; its flag is the command bit.
        CMD = 1 << 20
        def run_taps(events):
            d = TapDetector(54, CMD); fired = False
            for ev in events:
                if ev[0] == "key":
                    d.key_down()
                else:
                    fired = d.flags_changed(ev[1], ev[2], ev[3]) or fired
            return fired
        tap_cases = [
            ("a clean tap fires",               [("f", 54, CMD, 0.0), ("f", 54, 0, 0.2)], True),
            ("cmd+c is not a tap",              [("f", 54, CMD, 0.0), ("key",), ("f", 54, 0, 0.2)], False),
            ("held too long is not a tap",      [("f", 54, CMD, 0.0), ("f", 54, 0, 0.9)], False),
            ("another modifier in between",     [("f", 54, CMD, 0.0), ("f", 56, CMD, 0.1), ("f", 54, 0, 0.2)], False),
            ("a release with no press",         [("f", 54, 0, 0.1)], False),
            ("the LEFT command key is ignored", [("f", 55, CMD, 0.0), ("f", 55, 0, 0.2)], False),
            ("two clean taps fire again",       [("f", 54, CMD, 0.0), ("f", 54, 0, 0.1),
                                                 ("f", 54, CMD, 1.0), ("f", 54, 0, 1.1)], True),
        ]
        for name, events, want in tap_cases:
            ok = run_taps(events) == want
            bad += 0 if ok else 1
            print(("ok  " if ok else "FAIL"), "tap:", name)
        for spec in TAP_KEYS:
            ok = spec in TAP_KEYS and isinstance(TAP_KEYS[spec][0], int)
            bad += 0 if ok else 1
            print(("ok  " if ok else "FAIL"), "tap spec", spec)

        # L() (bugs #9): every status/failure key must resolve in every language
        # this app claims to speak, or a user gets an English string (or the key
        # name back) mid-conversation instead of a sentence in her own language.
        all_keys = set(LANG["en"])
        for lang, table in LANG.items():
            missing = all_keys - set(table)
            ok = not missing
            bad += 0 if ok else 1
            print(("ok  " if ok else "FAIL"), "LANG", lang, "missing", missing or "-")
        for key in all_keys:
            for locale in ("he-IL", "ar-SA", "ru-RU", "en-US", "xx-XX"):
                try:
                    text = L(key, locale, s=6, v="1.0.1")
                    ok = bool(text)
                except Exception as e:  # noqa: BLE001
                    ok, text = False, repr(e)
                bad += 0 if ok else 1
                if not ok:
                    print("FAIL", "L", key, locale, "->", text)

        sys.exit(1 if bad else 0)
    main()
