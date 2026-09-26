"""Who is using this. Learned by talking, not by editing a file.

MicMic is meant for people who cannot fill in a settings screen, so the setup is a
short spoken conversation the first time it runs. It asks two things and infers the
rest: the language from how she answers, and the people who matter from whoever she
actually talks to on WhatsApp.
"""
from __future__ import annotations
import json, time
from pathlib import Path

from . import paths as _paths
PATH = _paths.state("profile.json",
                    legacy=Path(__file__).resolve().parent.parent / "profile.json")

DEFAULT = {
    "setup_complete": False,
    "step": "greet",
    "name": "",
    "language": "",          # hebrew | english | arabic | russian
    "speech_lang": "",       # what setup heard; Settings alone picks the recogniser
    "city": "",
    "gender": "",            # feminine | masculine — how to ADDRESS her or him
    "emergency_contact": "",
    "pinned": [],
    "created": 0,
    "uses": 0,
}

STEPS = ["greet", "name", "city", "who", "done"]

LANG_TO_SPEECH = {"hebrew": "he-IL", "arabic": "ar-SA", "russian": "ru-RU",
                  "english": "en-US"}
SPEECH_TO_LANG = {v: k for k, v in LANG_TO_SPEECH.items()}

# What it says at each step, in every language it knows.
SCRIPT = {
    # A silent first press: nothing to answer, so the greeting is the whole reply, in
    # the language chosen in Settings (English unless she chose another).
    "hello": {
        "hebrew":  "שלום, אני מיקמיק. איך קוראים לך?",
        "english": "Hello, I'm MicMic. What should I call you?",
        "arabic":  "مرحبا، أنا ميكميك. ما اسمك؟",
        "russian": "Здравствуйте, я МикМик. Как вас зовут?",
    },
    # She asked for something on her very first press. The answer comes first and this
    # one short line rides after it, in the language she spoke. It must still ask her
    # name: this is what advances the step to "name".
    "greet": {
        "hebrew":  "אני מיקמיק. איך קוראים לך?",
        "english": "I'm MicMic. What should I call you?",
        "arabic":  "أنا ميكميك. ما اسمك؟",
        "russian": "Я МикМик. Как вас зовут?",
    },
    # Gender not yet known here (it is only inferred later, from what she says), so
    # this defaults to feminine like the rest of the script until it is. Marker syntax
    # «feminine|masculine» matches router.py's degender() convention — see there.
    "city": {
        "hebrew":  "נעים מאוד, {name}. באיזו עיר «את גרה|אתה גר»?",
        "english": "Nice to meet you, {name}. Which city do you live in?",
        "arabic":  "تشرفنا يا {name}. في أي مدينة «تعيشين|تعيش»؟",
        "russian": "Приятно познакомиться, {name}. В каком городе вы живёте?",
    },
    # The safety-critical question. Without it the emergency call falls back to
    # whoever she messages most, which on a real machine was a work contact.
    "who": {
        "hebrew":  "ועוד דבר אחד, {name}: למי שאתקשר אם יקרה משהו?",
        "english": "And one more thing, {name}: who should I call if something happens?",
        "arabic":  "وإشي أخير يا {name}: لمين أتصل إذا صار إشي؟",
        "russian": "И последнее, {name}: кому позвонить, если что-то случится?",
    },
    "who_ok": {
        "hebrew":  "רשמתי. אם יקרה משהו אני מתקשרת {who}.",
        "english": "Noted. If anything happens I will call {who}.",
        "arabic":  "سجّلت. إذا صار إشي بتصل {who}.",
        "russian": "Записала. Если что-то случится, я позвоню {who}.",
    },
    "who_skip": {
        "hebrew":  "בסדר, נסדר את זה אחר כך.",
        "english": "That is alright, we can sort that out later.",
        "arabic":  "ماشي، منرتبها بعدين.",
        "russian": "Хорошо, разберёмся с этим позже.",
    },
    "done": {
        "hebrew":  "מצוין. עכשיו פשוט תגיד«י|» לי מה ש«את|אתה» רוצה. "
                   "לדוגמה: תשים«י|» לי מוזיקה, או תשלח«י|» הודעה.",
        "english": "Lovely. Now just tell me what you want. "
                   "For example: play me some music, or send a message.",
        "arabic":  "رائع. الآن «قولي|قل» لي ما تريد«ين|».",
        "russian": "Отлично. Теперь просто скажите, что вы хотите.",
    },
}

def load() -> dict:
    if PATH.exists():
        try:
            d = dict(DEFAULT)
            d.update(json.loads(PATH.read_text()))
            return d
        except Exception:  # noqa: BLE001
            # A half-written file must never be read as "we have never met".
            # Returning DEFAULT here is what used to send her back through setup.
            return {**dict(DEFAULT), "setup_complete": True, "step": "done",
                    "_unreadable": True}
    return dict(DEFAULT)


def save(p: dict) -> None:
    """Write atomically, and never downgrade a finished profile to an unfinished one.

    A transient read failure used to hand back the blank default, which then got saved
    over her real profile and threw her back into onboarding mid-conversation."""
    try:
        if not p.get("setup_complete") and PATH.exists() and not p.get("_resetting"):
            try:
                existing = json.loads(PATH.read_text())
                if existing.get("setup_complete"):
                    return
            except Exception:  # noqa: BLE001
                return
        # "_resetting" (and any other leading-underscore key) is an instruction to
        # this function, not profile data. It must never reach disk — once it did,
        # the guard above read it back as permanently true and stayed disabled for
        # every future save of this profile.
        persisted = {k: v for k, v in p.items() if not k.startswith("_")}
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(persisted, ensure_ascii=False, indent=2))
        tmp.replace(PATH)
    except Exception:  # noqa: BLE001
        pass


def finish_setup() -> dict:
    """Setup is over without the spoken conversation: the app's own onboarding window
    was finished or skipped. Whatever it had not asked yet stays empty, and the spoken
    path fills a field whenever she mentions it."""
    p = load()
    if not p.get("setup_complete"):
        p["setup_complete"] = True
        p["step"] = "done"
        save(p)
    return p


def clear() -> None:
    """Deliberate reset, the only way back to onboarding.

    Overwrites rather than deletes. MicMic has no delete path anywhere by design, and
    the test suite enforces that, so even her own profile is reset by writing a fresh
    one over the top."""
    try:
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(DEFAULT), ensure_ascii=False, indent=2))
        tmp.replace(PATH)
    except Exception:  # noqa: BLE001
        pass


def line(step: str, lang: str, **fmt) -> str:
    block = SCRIPT.get(step, {})
    text = block.get(lang) or block.get("english") or ""
    try:
        return text.format(**fmt)
    except Exception:  # noqa: BLE001
        return text


def reset() -> dict:
    clear()
    p = dict(DEFAULT)
    p["created"] = int(time.time())
    p["_resetting"] = True
    save(p)
    p.pop("_resetting", None)
    return p


if __name__ == "__main__":
    # `python3 -m savta.profile` shows what it knows about her;
    # `python3 -m savta.profile --reset` puts it back to a machine that has never
    # met anyone, so the whole first conversation runs again.
    import sys
    if "--reset" in sys.argv:
        if PATH.exists():
            backup = PATH.with_suffix(".json.bak")
            backup.write_text(PATH.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"previous profile kept at {backup}")
        reset()
        print("profile cleared — the next thing you say starts onboarding")
    else:
        p = load()
        if not p.get("setup_complete"):
            print("no profile yet: the next thing you say starts onboarding")
        for k in ("name", "language", "city", "emergency_contact", "uses"):
            print(f"  {k:<18} {p.get(k)!r}")
        print(f"  {'pinned people':<18} {len(p.get('pinned') or [])}")
