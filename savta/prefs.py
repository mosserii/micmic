"""What she has told MicMic to do from now on. Said out loud, kept on this Mac.

Two kinds, kept apart on purpose:
  * Knobs the code already has a decision for: which app a message goes by (for
    everyone, or for one person), which video site, whether every message is asked
    about first, how loud it may get. Stored typed and applied by plain code at the
    one place that decision is made, so nothing about them is left to a model.
  * Anything else, in her own words ("don't call anyone after 10pm"). MicMic cannot
    carry out an arbitrary rule, so it does the honest thing: a rule is shown to Jev
    only on a request it plausibly touches (shared words, or the same kind of thing),
    and if doing what she asks now would break it, the request still wins and she
    hears the rule once.

Why it is shaped like this, measured in this codebase: every block of context added
to the one understanding request changes how the other questions are answered
(brain.py: "make it quieter" went from 6/6 to 0/6 with two extra blocks beside it).
So nothing here is added to a normal request. The preference questions ride in the
same request only when worth_asking() finds words that can state, recall or forget a
preference, and rules only when relevant() finds overlap. Everything else is sent
byte for byte as it was.

Separate from memory.json (habits counted from what worked) and profile.json (who
she is). The speech language is not stored here: it is the Settings value
(settings.json language_hint), and saying "speak to me in Hebrew" sets that.
"""
from __future__ import annotations
import json
import os
import re
import tempfile
import threading
import time

from . import paths as _paths
PATH = _paths.state("prefs.json")
_LOCK = threading.Lock()

MAX_RULES = 20
RULE_MAX_CHARS = 160
APPS = ("whatsapp", "imessage")
SITES = ("youtube", "dailymotion")
# Which player music goes to when she names none. Read by the music path through
# music_app(); an empty answer means "not chosen", and the caller keeps its default.
MUSIC_APPS = ("youtube", "apple_music", "spotify")
# "keep the volume low" and "not too loud", as a ceiling on the 0-100 output level.
VOLUME_CAPS = {"low": 35, "medium": 60}


def _blank() -> dict:
    return {"message_app": {}, "video_site": "", "music_app": "", "confirm_send": "",
            "volume_max": 0, "rules": [], "last": ""}


def load() -> dict:
    try:
        if PATH.exists():
            d = json.loads(PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                out = _blank()
                out.update({k: d[k] for k in out if k in d})
                if not isinstance(out["message_app"], dict):
                    out["message_app"] = {}
                if not isinstance(out["rules"], list):
                    out["rules"] = []
                return out
    except Exception:  # noqa: BLE001
        pass
    return _blank()


def save(d: dict) -> None:
    """Atomic. "Forget" is this too: the file rewritten without the item. Nothing in
    this project deletes a file, and the suite checks that it stays that way."""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, str(PATH))
    except Exception:  # noqa: BLE001
        pass


def _update(fn) -> dict:
    with _LOCK:
        d = load()
        fn(d)
        save(d)
        return d


# ---------------------------------------------------------------- setting

def set_app(app: str, who: str | None = None) -> None:
    if app not in APPS:
        return
    key = who or "*"

    def f(d):
        d["message_app"][key] = app
        d["last"] = f"app:{key}"
    _update(f)


def set_site(site: str) -> None:
    if site not in SITES:
        return

    def f(d):
        d["video_site"] = site
        d["last"] = "site"
    _update(f)


def set_music_app(app: str) -> None:
    if app not in MUSIC_APPS:
        return

    def f(d):
        d["music_app"] = app
        d["last"] = "music"
    _update(f)


def set_confirm(always: bool) -> None:
    def f(d):
        d["confirm_send"] = "always" if always else ""
        d["last"] = "confirm"
    _update(f)


def set_volume_cap(level: str) -> None:
    """"low", "medium", or anything else for no ceiling."""
    def f(d):
        d["volume_max"] = VOLUME_CAPS.get(level, 0)
        d["last"] = "volume"
    _update(f)


def add_rule(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip(" .,!?;:")[:RULE_MAX_CHARS]
    if not text:
        return ""

    def f(d):
        rules = [r for r in d["rules"] if isinstance(r, dict)
                 and r.get("text", "").lower() != text.lower()]
        rules.append({"text": text, "at": int(time.time())})
        d["rules"] = rules[-MAX_RULES:]
        d["last"] = f"rule:{text}"
    _update(f)
    return text


# ---------------------------------------------------------------- applying

def channel_for(contact: str | None, said: str | None) -> tuple[str, str | None]:
    """(app to send by, the preferred app she overrode or None).

    What she said in this sentence wins. Then her choice for this person, then her
    choice for everyone, then text messages as always."""
    apps = load()["message_app"]
    pref = apps.get(contact or "") or apps.get("*")
    if said in APPS:
        return said, (pref if pref in APPS and pref != said else None)
    return (pref if pref in APPS else "imessage"), None


def music_app() -> str:
    """"youtube", "apple_music", "spotify", or "" when she has not chosen one."""
    v = load().get("music_app")
    return v if v in MUSIC_APPS else ""


def always_confirm() -> bool:
    return load().get("confirm_send") == "always"


def volume_cap() -> int:
    try:
        v = int(load().get("volume_max") or 0)
    except (TypeError, ValueError):
        return 0
    return v if 0 < v <= 100 else 0


def rules() -> list[str]:
    return [r["text"] for r in load()["rules"] if isinstance(r, dict) and r.get("text")]


# ---------------------------------------------------------------- items

def items(d: dict | None = None) -> list[dict]:
    """Everything stored, one row each, newest rule last. Used by "what do you know
    about me", by "forget that ...", and by the Settings view."""
    d = d or load()
    out = []
    for who, app in d["message_app"].items():
        if app in APPS:
            out.append({"id": f"app:{who}", "kind": "app", "who": None if who == "*" else who,
                        "value": app})
    if d.get("video_site") in SITES:
        out.append({"id": "site", "kind": "site", "value": d["video_site"]})
    if d.get("music_app") in MUSIC_APPS:
        out.append({"id": "music", "kind": "music", "value": d["music_app"]})
    if d.get("confirm_send") == "always":
        out.append({"id": "confirm", "kind": "confirm", "value": "always"})
    if d.get("volume_max"):
        out.append({"id": "volume", "kind": "volume", "value": d["volume_max"]})
    for r in d["rules"]:
        if isinstance(r, dict) and r.get("text"):
            out.append({"id": f"rule:{r['text']}", "kind": "rule", "value": r["text"]})
    return out


def describe(item: dict) -> str:
    """The row as Jev is shown it, in English, when she asks to forget one."""
    k, v = item["kind"], item["value"]
    by = {"whatsapp": "WhatsApp", "imessage": "text messages"}.get(v, v)
    if k == "app":
        return (f"Messages to {item['who']} go by {by}" if item.get("who")
                else f"Messages go by {by}")
    if k == "site":
        return f"She prefers {v.title()} for videos"
    if k == "music":
        return f"Music plays on {MUSIC_NAME['english'][v]}"
    if k == "confirm":
        return "Ask her before sending any message"
    if k == "volume":
        return "Keep the volume low" if v <= VOLUME_CAPS["low"] else "Keep the volume from getting too loud"
    return f"Her rule: {v}"


def forget(item_id: str) -> dict | None:
    """Rewrite the file without this one item. Returns the item, or None."""
    gone: list = []

    def f(d):
        for it in items(d):
            if it["id"] != item_id:
                continue
            gone.append(it)
            if it["kind"] == "app":
                d["message_app"].pop(it["who"] or "*", None)
            elif it["kind"] == "site":
                d["video_site"] = ""
            elif it["kind"] == "music":
                d["music_app"] = ""
            elif it["kind"] == "confirm":
                d["confirm_send"] = ""
            elif it["kind"] == "volume":
                d["volume_max"] = 0
            else:
                d["rules"] = [r for r in d["rules"]
                              if not (isinstance(r, dict) and r.get("text") == it["value"])]
        if gone and d.get("last") == item_id:
            d["last"] = ""
    _update(f)
    return gone[0] if gone else None


def last_id() -> str:
    d = load()
    ids = [it["id"] for it in items(d)]
    return d["last"] if d.get("last") in ids else (ids[-1] if ids else "")


def forget_all() -> None:
    with _LOCK:
        save(_blank())


# ---------------------------------------------------------------- when to ask

# Words that can state, recall or forget a standing preference, in her four
# languages. Deliberately loose: a false hit costs one sentence a few extra folded
# questions; a miss means her rule is heard as an ordinary request. A normal request
# ("play music", "send Dana hi", "make it quieter") must not hit, which is what keeps
# every one of them exactly as fast as before (tests/test_prefs.py checks both sides).
_RULE_WORDS = (
    # English
    "from now on", "always", "never", "by default", "i prefer", "i'd prefer",
    "i would prefer", "i'd rather", "i would rather", "remember that", "remember i",
    "keep the volume", "don't use", "do not use", "stop using", "instead of",
    "speak to me in", "talk to me in", "speak in ", "answer me in", "reply in ",
    "don't call", "do not call", "dont use", "dont call", "don't ever", "ask me before", "ask before",
    "no need to ask", "stop asking", "don't ask", "whenever", "every time",
    "anymore", "any more", "forget", "what do you know about me",
    "what do you remember", "what have you learned", "what do you know of me",
    # Hebrew
    "מעכשיו", "מהיום", "תמיד", "אף פעם", "לעולם", "מעדיפ", "תזכרי ש", "תזכור ש",
    "אל תשתמש", "תדברי איתי", "תדבר איתי", "תעני לי ב", "תענה לי ב",
    "תשאלי אותי לפני", "תשאל אותי לפני", "בלי לשאול", "תפסיקי לשאול",
    "תשכחי", "תשכח", "יודעת עליי", "יודע עליי", "זוכרת עליי", "זוכר עליי",
    "אל תתקשר", "בכל פעם", "כל פעם", "יותר לא", "כבר לא", "תשאירי", "תשאיר",
    "תשמרי על", "תשמור על",
    # Arabic
    "من هلق", "من هلّق", "من الآن", "من هلأ", "دائما", "دايما", "دايماً", "دائماً",
    "ابدا", "أبدا", "أبداً", "بفضل", "بفضّل", "انسي", "انسى", "إنسي", "عني",
    "تذكري", "احكي معي بال", "اسألني قبل", "اسأليني قبل", "لا تستعمل", "ما تستعمل",
    # Russian
    "с этого момента", "отныне", "теперь всегда", "всегда", "никогда",
    "предпочитаю", "запомни", "забудь", "обо мне", "что ты помнишь",
    "говори со мной", "спрашивай", "не используй",
)


def worth_asking(utterance: str) -> bool:
    low = (utterance or "").lower().replace("’", "'")
    return any(w in low for w in _RULE_WORDS)


# ---------------------------------------------------------------- relevance

_KIND_WORDS = {
    "call": ("call", "phone", "ring", "facetime", "dial", "תתקשר", "להתקשר", "התקשר",
             "שיחה", "תחייג", "اتصل", "تتصل", "مكالمة", "звон", "позвон", "набери"),
    "message": ("message", "send", "text", "write", "whatsapp", "sms", "הודעה", "תשלח",
                "לשלוח", "תכתב", "וואטסאפ", "ווטסאפ", "رسالة", "ابعت", "ابعث", "واتس",
                "сообщен", "отправ", "напиши", "ватсап"),
    "music": ("music", "song", "sing", "play", "radio", "מוזיקה", "שיר", "תשמיע",
              "תשימי", "רדיו", "أغنية", "اغنية", "موسيقى", "شغل", "музык", "песн",
              "включи", "радио"),
    "watch": ("video", "watch", "film", "movie", "youtube", "סרט", "סרטון", "יוטיוב",
              "فيديو", "فيلم", "يوتيوب", "видео", "фильм", "ютуб"),
    "volume": ("volume", "loud", "quiet", "sound", "עוצמה", "ווליום", "חזק", "שקט",
               "תגביר", "תנמיכ", "صوت", "علّي", "وطّي", "громк", "тише", "звук"),
}
_STOP = {"the", "and", "for", "you", "don't", "dont", "not", "never", "always", "from",
         "now", "any", "anyone", "anybody", "after", "before", "please", "with", "use",
         "what", "that", "this", "when", "then", "than", "into", "about", "your", "mine",
         "all", "can", "will", "want", "tell", "just", "also", "but", "who", "her", "his",
         "אני", "את", "אתה", "של", "לי", "זה", "גם", "אבל", "אם", "כל", "אף", "אחד",
         "תמיד", "פעם", "אחרי", "לפני", "בבקשה", "מעכשיו"}
_WORD = re.compile(r"[\w']+", re.UNICODE)
_HE_PREFIX = re.compile(r"^[והבלמשכ](?=[֐-׿]{3,})")


def _words(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall((text or "").lower()):
        w = _HE_PREFIX.sub("", w)
        if len(w) >= 3 and w not in _STOP and not w.isdigit():
            out.add(w)
    return out


def _kinds(text: str) -> set[str]:
    low = (text or "").lower()
    return {k for k, ws in _KIND_WORDS.items() if any(w in low for w in ws)}


def _share_a_word(a: set[str], b: set[str]) -> bool:
    for x in a:
        for y in b:
            short, long_ = (x, y) if len(x) <= len(y) else (y, x)
            if short == long_ or (len(short) >= 4 and long_.startswith(short)):
                return True
    return False


def relevant(utterance: str, limit: int = 3) -> list[str]:
    """Her rules that this sentence plausibly touches: a word in common, or the same
    kind of thing (a call, a message, music, a video, the volume). Pure code, run on
    every turn; with no rules saved it is one small file read."""
    saved = rules()
    if not saved:
        return []
    uw, uk = _words(utterance), _kinds(utterance)
    out = [r for r in saved if (_kinds(r) & uk) or _share_a_word(_words(r), uw)]
    return out[-limit:]


# ---------------------------------------------------------------- questions

RULE_SPAN = ("She is telling the computer a rule for the future. Which part of the "
             "sentence is the rule itself, in her words: what should always or never be "
             "done, and when? Leave out words like 'from now on', 'please' and 'remember "
             "that'.")
RULE_EXISTS = ("Some part of the sentence states a rule for the future: something the "
               "computer should always or never do.")

QUESTIONS = {
    "pref_kind": {"type": "choice",
        "instructions": "Is she telling the computer how to behave FROM NOW ON, asking what it knows about her, or asking it to forget something? Or is this an ordinary request for right now?",
        "criteria": {
            "none": "An ordinary request or remark about right now, even one with the word always or never in it: play Always on My Mind, I never got her message, send Dana a message that I will always love her, what time is it.",
            "message_app": "Which app messages should go by from now on, for everyone or for one person: from now on use WhatsApp for Mom, always send to Dana on WhatsApp, תמיד תשלחי לדנה בוואטסאפ, send my messages as texts from now on.",
            "video_site": "Which website videos should come from from now on: don't use YouTube, use Dailymotion. תשתמשי תמיד ביוטיוב.",
            "music_app": "Which app music should play in from now on: always play music on Spotify, use Apple Music for songs, תמיד תשימי מוזיקה בספוטיפיי.",
            "speech_language": "Which language the computer should speak to her in from now on: speak to me in Hebrew, תדברי איתי באנגלית, говори со мной по-русски.",
            "confirm_send": "Whether to ask her before sending a message, from now on: always ask before sending, תמיד תשאלי אותי לפני שאת שולחת, you don't need to ask me any more.",
            "volume_limit": "How loud the computer may get, from now on: keep the volume low, never make it too loud, תשאירי את הווליום נמוך, you can make it loud again.",
            "other_rule": "Any other rule for the future, about what the computer should always or never do: don't call anyone after 10pm, never play sad songs, אל תתקשרי לאף אחד אחרי עשר בלילה.",
            "recall": "She asks what the computer knows or remembers about her: what do you know about me, מה את יודעת עליי, что ты обо мне знаешь.",
            "forget_one": "She asks it to forget one thing it was told or remembers: forget that, forget that I prefer WhatsApp, תשכחי את זה.",
            "forget_all": "She asks it to forget everything it knows about her: forget everything about me, תשכחי הכל עליי.",
        }},
    "pref_app": {"type": "choice",
        "instructions": "If she says which app messages should go by from now on, which one?",
        "criteria": {"whatsapp": "WhatsApp, ווטסאפ, וואטסאפ.",
                     "imessage": "Text messages, SMS, iMessage, ordinary messages.",
                     "not_said": "She did not say an app."}},
    "pref_site": {"type": "choice",
        "instructions": "If she says which website videos should come from from now on, which one? A site she says NOT to use is not the answer.",
        "criteria": {"youtube": "YouTube.", "dailymotion": "Dailymotion.",
                     "other": "Some other website.", "not_said": "She did not say one."}},
    "pref_music": {"type": "choice",
        "instructions": "If she says which app music should play in from now on, which one?",
        "criteria": {"youtube": "YouTube.", "apple_music": "Apple Music, or the Music app.",
                     "spotify": "Spotify.", "not_said": "She did not say one."}},
    "pref_language": {"type": "choice",
        "instructions": "If she wants the computer to speak to her in a particular language from now on, which one? The language she happens to be speaking in does not count.",
        "criteria": {"english": None, "hebrew": None, "arabic": None, "russian": None,
                     "other": "Some other language.", "not_said": "She did not say one."}},
    "pref_confirm": {"type": "choice",
        "instructions": "If she is saying whether to be asked before a message is sent, what does she want from now on?",
        "criteria": {"always_ask": "Ask her before sending every message.",
                     "no_need": "Stop asking her first; no need to ask.",
                     "not_said": "She did not say."}},
    "pref_volume": {"type": "choice",
        "instructions": "If she is saying how loud the computer may get from now on, what does she want?",
        "criteria": {"low": "Keep it low or quiet.",
                     "medium": "Not too loud, but not especially quiet either.",
                     "no_limit": "No limit any more: it may be as loud as she asks.",
                     "not_said": "She did not say."}},
}
FORGET_LAST = "the_last_one"
FORGET_NONE = "none_of_these"


def questions(utterance: str) -> dict | None:
    """The `standing` argument for brain.understand(), or None to ask nothing extra.

    {"questions": {...}, "rules": [...]}: the preference questions when her words can
    state, recall or forget one; the rule check when a saved rule is relevant."""
    qs: dict = {}
    if worth_asking(utterance):
        qs.update(QUESTIONS)
        saved = items()
        if saved:
            opts = {str(i): describe(it) for i, it in enumerate(saved[:200])}
            opts[FORGET_LAST] = ("The last thing she told the computer, when she says "
                                 "only 'forget that' or 'forget it'.")
            opts[FORGET_NONE] = "None of these is what she wants forgotten."
            qs["pref_forget"] = {"type": "choice",
                "instructions": "If she asks the computer to forget one thing, which of these?",
                "criteria": opts}
    near = relevant(utterance)
    if near:
        qs["pref_breaks_rule"] = {"type": "noul",
            "instructions": "Doing what she is asking for right now would go against one of her_standing_rules, taking into account the time and day in right_now",
            "criteria": {"true": "What she asks for now is exactly what one of her rules says not to do, at a time the rule covers.",
                         "false": "It does not go against any of them, or she is only stating or discussing a rule."}}
    if not qs:
        return None
    return {"questions": qs, "rules": near}


def forget_target(answer: dict) -> str | None:
    """The id of the item her "forget ..." picked from the folded question, or None."""
    a = (answer or {}).get("pref_forget")
    if not a:
        return None
    pick = a.get("choice")
    if pick == FORGET_LAST:
        return last_id() or None
    if pick in (None, FORGET_NONE):
        return None
    saved = items()
    try:
        return saved[int(pick)]["id"]
    except (ValueError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------- what she hears
# Every line in her four languages. «feminine|masculine» is router.degender's marker.

BY = {
    "english": {"whatsapp": "on WhatsApp", "imessage": "as text messages"},
    "hebrew":  {"whatsapp": "בוואטסאפ", "imessage": "כהודעות טקסט"},
    "arabic":  {"whatsapp": "على واتساب", "imessage": "كرسائل نصية"},
    "russian": {"whatsapp": "через WhatsApp", "imessage": "обычными сообщениями"},
}
MUSIC_NAME = {
    "english": {"youtube": "YouTube", "apple_music": "Apple Music", "spotify": "Spotify"},
    "hebrew":  {"youtube": "יוטיוב", "apple_music": "אפל מיוזיק", "spotify": "ספוטיפיי"},
    "arabic":  {"youtube": "يوتيوب", "apple_music": "آبل ميوزك", "spotify": "سبوتيفاي"},
    "russian": {"youtube": "YouTube", "apple_music": "Apple Music", "spotify": "Spotify"},
}
SITE_NAME = {
    "english": {"youtube": "YouTube", "dailymotion": "Dailymotion"},
    "hebrew":  {"youtube": "יוטיוב", "dailymotion": "דיילימושן"},
    "arabic":  {"youtube": "يوتيوب", "dailymotion": "ديلي موشن"},
    "russian": {"youtube": "YouTube", "dailymotion": "Dailymotion"},
}
LANG_IN = {
    "english": {"english": "in English", "hebrew": "in Hebrew", "arabic": "in Arabic",
                "russian": "in Russian"},
    "hebrew":  {"english": "באנגלית", "hebrew": "בעברית", "arabic": "בערבית",
                "russian": "ברוסית"},
    "arabic":  {"english": "بالإنجليزي", "hebrew": "بالعبري", "arabic": "بالعربي",
                "russian": "بالروسي"},
    "russian": {"english": "по-английски", "hebrew": "на иврите", "arabic": "по-арабски",
                "russian": "по-русски"},
}

SAY = {
    "english": {
        "music_set": "From now on I will play music on {app}.",
        "recall_music_app": "Music plays on {app}.",
        "who_unknown": "I could not find that person in your contacts, so I did not save it.",
        "app_all": "From now on I will send your messages {by}.",
        "app_one": "From now on I will send messages to {who} {by}.",
        "site_only_youtube": "I can only search YouTube for now. I will remember that you prefer {site}.",
        "site_youtube": "Alright, I will keep using YouTube.",
        "language": "From now on I will speak to you {lang_in}.",
        "confirm_always": "From now on I will ask you before sending any message.",
        "confirm_normal": "Alright, I will not ask every time. I will still check very short messages with you.",
        "volume_low": "Alright, I will keep the volume low.",
        "volume_medium": "Alright, I will not let the volume get too loud.",
        "volume_free": "Alright, the volume can go as loud as you like.",
        "rule": "Noted: {rule}. If you ask for something against it, I will remind you.",
        "huh": "I did not catch what to change. Could you say it again?",
        "rule_note": "You asked me: {rule}. I am doing it anyway, since you are asking now.",
        "app_note": "This one goes {by_now}, as you asked, not {by_pref}.",
        "volume_note": "You asked me to keep the volume low, but I am making it louder since you asked.",
        "recall_head": "Here is what I know about you.",
        "recall_none": "I do not know much about you yet.",
        "recall_name": "Your name is {name}.",
        "recall_language": "I speak to you {lang_in}.",
        "recall_app_all": "Messages go {by}.",
        "recall_app_one": "Messages to {who} go {by}.",
        "recall_site": "You prefer {site} for videos.",
        "recall_confirm": "I ask you before sending any message.",
        "recall_volume_low": "I keep the volume low.",
        "recall_volume_medium": "I do not let the volume get too loud.",
        "recall_rule": "You asked me: {rule}.",
        "recall_people": "You often talk to {people}.",
        "recall_music": "You like {music}.",
        "recall_apps": "You often open {apps}.",
        "recall_tail": "You can tell me to forget any of it.",
        "forgot": "Forgotten: {what}",
        "forget_which": "I am not sure which one to forget. Ask me what I know about you, then tell me which.",
        "forget_nothing": "There is nothing to forget. I have no preferences of yours saved.",
        "forgot_all": "I have forgotten your preferences and your habits. I still know your name.",
        "undone": "I put it back the way it was.",
        "and": " and ",
    },
    "hebrew": {
        "music_set": "מעכשיו אשים מוזיקה ב{app}.",
        "recall_music_app": "מוזיקה מתנגנת ב{app}.",
        "who_unknown": "לא מצאתי את האדם הזה באנשי הקשר שלך, אז לא שמרתי.",
        "app_all": "מעכשיו אשלח את ההודעות שלך {by}.",
        "app_one": "מעכשיו אשלח הודעות {at_he} {by}.",
        "site_only_youtube": "כרגע אני יכולה לחפש רק ביוטיוב. אזכור ש«את מעדיפה|אתה מעדיף» {site}.",
        "site_youtube": "בסדר, אמשיך להשתמש ביוטיוב.",
        "language": "מעכשיו אדבר איתך {lang_in}.",
        "confirm_always": "מעכשיו אשאל אותך לפני כל הודעה שאני שולחת.",
        "confirm_normal": "בסדר, לא אשאל כל פעם. הודעות קצרות מאוד אבדוק איתך בכל זאת.",
        "volume_low": "בסדר, אשמור על עוצמה נמוכה.",
        "volume_medium": "בסדר, לא אתן לעוצמה להיות חזקה מדי.",
        "volume_free": "בסדר, אפשר להגביר כמה ש«את רוצה|אתה רוצה».",
        "rule": "רשמתי: {rule}. אם «תבקשי|תבקש» משהו שסותר את זה, אזכיר לך.",
        "huh": "לא הבנתי מה לשנות. «תגידי|תגיד» שוב?",
        "rule_note": "ביקשת ממני: {rule}. אני עושה את זה בכל זאת, כי ביקשת עכשיו.",
        "app_note": "הפעם {by_now}, כמו שביקשת, ולא {by_pref}.",
        "volume_note": "ביקשת שאשמור על עוצמה נמוכה, אבל אני מגבירה כי ביקשת.",
        "recall_head": "זה מה שאני יודעת «עלייך|עליך».",
        "recall_none": "אני עוד לא יודעת «עלייך|עליך» הרבה.",
        "recall_name": "קוראים לך {name}.",
        "recall_language": "אני מדברת איתך {lang_in}.",
        "recall_app_all": "הודעות נשלחות {by}.",
        "recall_app_one": "הודעות {at_he} נשלחות {by}.",
        "recall_site": "«את מעדיפה|אתה מעדיף» {site} לסרטונים.",
        "recall_confirm": "אני שואלת אותך לפני כל הודעה.",
        "recall_volume_low": "אני שומרת על עוצמה נמוכה.",
        "recall_volume_medium": "אני לא נותנת לעוצמה להיות חזקה מדי.",
        "recall_rule": "ביקשת ממני: {rule}.",
        "recall_people": "«את מדברת|אתה מדבר» הרבה עם {people}.",
        "recall_music": "«את אוהבת|אתה אוהב» {music}.",
        "recall_apps": "«את פותחת|אתה פותח» הרבה את {apps}.",
        "recall_tail": "אפשר לבקש ממני לשכוח כל דבר מזה.",
        "forgot": "שכחתי: {what}",
        "forget_which": "אני לא בטוחה מה לשכוח. «תשאלי|תשאל» אותי מה אני יודעת «עלייך|עליך», ואז «תגידי|תגיד» לי מה.",
        "forget_nothing": "אין מה לשכוח. לא שמרתי שום העדפה שלך.",
        "forgot_all": "שכחתי את ההעדפות וההרגלים שלך. את השם שלך אני עדיין זוכרת.",
        "undone": "החזרתי את זה למה שהיה.",
        "and": " ו",
    },
    "arabic": {
        "music_set": "من هلّق رح شغّل الموسيقى على {app}.",
        "recall_music_app": "الموسيقى بتشتغل على {app}.",
        "who_unknown": "ما لقيت هالشخص بجهات الاتصال تبعك، فما حفظت.",
        "app_all": "من هلّق رح أبعت رسائلك {by}.",
        "app_one": "من هلّق رح أبعت الرسائل {at_ar} {by}.",
        "site_only_youtube": "هلّق بقدر دوّر بس على يوتيوب. رح أتذكر إنك بتفضّل«ي|» {site}.",
        "site_youtube": "ماشي، رح ضل أستعمل يوتيوب.",
        "language": "من هلّق رح أحكي معك {lang_in}.",
        "confirm_always": "من هلّق رح إسألك قبل ما أبعت أي رسالة.",
        "confirm_normal": "ماشي، ما رح إسألك كل مرة. الرسائل القصيرة كتير رح إتأكد منها معك.",
        "volume_low": "ماشي، رح خلّي الصوت واطي.",
        "volume_medium": "ماشي، ما رح خلّي الصوت يعلى كتير.",
        "volume_free": "ماشي، فيك تعلّي الصوت قد ما بدك.",
        "rule": "سجّلت: {rule}. إذا طلبت«ي|» إشي عكسه، رح ذكّرك.",
        "huh": "ما فهمت شو بدك غيّر. عيد«ي|» كمان مرة؟",
        "rule_note": "طلبت«ي|» مني: {rule}. رح أعملها لأنك طلبت«ي|» هلّق.",
        "app_note": "هالمرة {by_now} متل ما طلبت«ي|»، مش {by_pref}.",
        "volume_note": "طلبت«ي|» خلّي الصوت واطي، بس رح علّيه لأنك طلبت«ي|».",
        "recall_head": "هاد اللي بعرفه عنك.",
        "recall_none": "لسا ما بعرف عنك كتير.",
        "recall_name": "اسمك {name}.",
        "recall_language": "بحكي معك {lang_in}.",
        "recall_app_all": "الرسائل بتنبعت {by}.",
        "recall_app_one": "الرسائل {at_ar} بتنبعت {by}.",
        "recall_site": "بتفضّل«ي|» {site} للفيديوهات.",
        "recall_confirm": "بسألك قبل أي رسالة.",
        "recall_volume_low": "بخلّي الصوت واطي.",
        "recall_volume_medium": "ما بخلّي الصوت يعلى كتير.",
        "recall_rule": "طلبت«ي|» مني: {rule}.",
        "recall_people": "بتحكي«ي|» كتير مع {people}.",
        "recall_music": "بتحب«ي|» {music}.",
        "recall_apps": "بتفتح«ي|» كتير {apps}.",
        "recall_tail": "فيك تطلب«ي|» مني إنسى أي إشي منهم.",
        "forgot": "نسيت: {what}",
        "forget_which": "مش متأكدة شو إنسى. إسأل«ي|»ني شو بعرف عنك، وبعدين قول«ي|»لي شو.",
        "forget_nothing": "ما في إشي إنساه. ما حفظت ولا تفضيل إلك.",
        "forgot_all": "نسيت تفضيلاتك وعاداتك. اسمك لسا بتذكره.",
        "undone": "رجّعتها متل ما كانت.",
        "and": " و",
    },
    "russian": {
        "music_set": "Теперь я буду включать музыку в {app}.",
        "recall_music_app": "Музыка играет в {app}.",
        "who_unknown": "Я не нашла этого человека в ваших контактах, поэтому не сохранила.",
        "app_all": "Теперь я буду отправлять ваши сообщения {by}.",
        "app_one": "Теперь сообщения для {who} я буду отправлять {by}.",
        "site_only_youtube": "Пока я умею искать только на YouTube. Я запомню, что вы предпочитаете {site}.",
        "site_youtube": "Хорошо, буду и дальше пользоваться YouTube.",
        "language": "Теперь я буду говорить с вами {lang_in}.",
        "confirm_always": "Теперь я буду спрашивать вас перед отправкой каждого сообщения.",
        "confirm_normal": "Хорошо, не буду спрашивать каждый раз. Очень короткие сообщения всё равно уточню.",
        "volume_low": "Хорошо, буду держать громкость низкой.",
        "volume_medium": "Хорошо, не буду делать слишком громко.",
        "volume_free": "Хорошо, громкость можно делать любой.",
        "rule": "Записала: {rule}. Если попросите что-то против этого, я напомню.",
        "huh": "Я не поняла, что изменить. Повторите?",
        "rule_note": "Вы просили: {rule}. Делаю всё равно, раз вы просите сейчас.",
        "app_note": "На этот раз {by_now}, как вы просили, а не {by_pref}.",
        "volume_note": "Вы просили держать громкость низкой, но раз просите, делаю громче.",
        "recall_head": "Вот что я о вас знаю.",
        "recall_none": "Я пока мало о вас знаю.",
        "recall_name": "Вас зовут {name}.",
        "recall_language": "Я говорю с вами {lang_in}.",
        "recall_app_all": "Сообщения уходят {by}.",
        "recall_app_one": "Сообщения для {who} уходят {by}.",
        "recall_site": "Для видео вы предпочитаете {site}.",
        "recall_confirm": "Я спрашиваю вас перед каждым сообщением.",
        "recall_volume_low": "Я держу громкость низкой.",
        "recall_volume_medium": "Я не делаю слишком громко.",
        "recall_rule": "Вы просили: {rule}.",
        "recall_people": "Вы часто общаетесь с {people}.",
        "recall_music": "Вам нравится {music}.",
        "recall_apps": "Вы часто открываете {apps}.",
        "recall_tail": "Вы можете попросить меня что-то из этого забыть.",
        "forgot": "Забыла: {what}",
        "forget_which": "Я не уверена, что забыть. Спросите, что я о вас знаю, и скажите, что именно.",
        "forget_nothing": "Забывать нечего: я не сохраняла ваших предпочтений.",
        "forgot_all": "Я забыла ваши предпочтения и привычки. Ваше имя я помню.",
        "undone": "Вернула как было.",
        "and": " и ",
    },
}
UNDONE = {lang: SAY[lang]["undone"] for lang in SAY}


def say(lang: str, key: str, **fmt) -> str:
    block = SAY.get(lang) or SAY["english"]
    text = block.get(key) or SAY["english"][key]
    try:
        return text.format(**fmt) if fmt else text
    except (KeyError, IndexError, ValueError):
        return text


def join(names: list[str], lang: str) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + (SAY.get(lang) or SAY["english"])["and"] + names[-1]


def item_line(item: dict, lang: str, name) -> str:
    """One stored item as a spoken sentence. `name(canonical, lang)` returns
    {"who", "at_he", "at_ar"}: the router's own way of saying a contact's name."""
    k, v = item["kind"], item["value"]
    if k == "app":
        by = BY.get(lang, BY["english"]).get(v, v)
        if item.get("who"):
            return say(lang, "recall_app_one", by=by, **name(item["who"], lang))
        return say(lang, "recall_app_all", by=by)
    if k == "site":
        return say(lang, "recall_site", site=SITE_NAME.get(lang, SITE_NAME["english"]).get(v, v))
    if k == "music":
        return say(lang, "recall_music_app",
                   app=MUSIC_NAME.get(lang, MUSIC_NAME["english"]).get(v, v))
    if k == "confirm":
        return say(lang, "recall_confirm")
    if k == "volume":
        return say(lang, "recall_volume_low" if v <= VOLUME_CAPS["low"] else "recall_volume_medium")
    return say(lang, "recall_rule", rule=v)


def recall(lang: str, name, profile: dict, speech_lang: str | None, habits: dict,
           shown=lambda c, lang: c) -> str:
    """"What do you know about me?", read back as a short spoken list from what is on
    disk: her name, the language, each preference and rule, and the few people and
    things she comes back to most. Templates only: no model writes any of it."""
    lines = []
    if (profile.get("name") or "").strip():
        lines.append(say(lang, "recall_name", name=profile["name"].strip()))
    if speech_lang in LANG_IN["english"]:
        lines.append(say(lang, "recall_language",
                         lang_in=LANG_IN.get(lang, LANG_IN["english"])[speech_lang]))
    for it in items():
        lines.append(item_line(it, lang, name))
    people = [shown(p, lang) for p in (habits.get("people") or [])[:3]]
    if people:
        lines.append(say(lang, "recall_people", people=join(people, lang)))
    music = (habits.get("music") or [])[:2]
    if music:
        lines.append(say(lang, "recall_music", music=join(music, lang)))
    apps = (habits.get("apps") or [])[:2]
    if apps:
        lines.append(say(lang, "recall_apps", apps=join(apps, lang)))
    if not lines:
        return say(lang, "recall_none")
    return " ".join([say(lang, "recall_head")] + lines + [say(lang, "recall_tail")])
