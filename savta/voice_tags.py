"""Which ElevenLabs v4 audio tag, if any, a spoken reply gets. Pure code: no I/O, no
model call. The design and its measurements: micmic-work/voice/TAGS_DESIGN.md; the five
moments shipped at launch: micmic-work/voice/wow/WOW_TAGS.md ("The 5 to ship").

The rules, in order of precedence (choose() below):
  1. Kill switch (MICMIC_VOICE_TAGS=off, or "voice_tags": false in settings), or an
     engine that cannot read tags (Kokoro, say): no tag. The router does not even ask
     unless savta.voice.plan() says the cloud voice will speak the line.
  2. The reply's family, from its `did`. READBACK and SILENT families never get a tag,
     whatever else is true: a message read back before it is sent, her notes and what
     is on her screen are read in a plain voice, always. A safety confirmation ("Shall I
     call Dana?") may only ever be [calmly]. The one exception to the read-back rule is
     a friend's INCOMING message, read in the friend's tone (incoming(), WOW 2).
  3. A tag chosen by Gemini inside the call that wrote the sentence (answered, chatted),
     validated against GEMINI_ALLOW. A live answer (news, a score, a price) gets none.
  4. Her tone, when Jev was asked (reply_tone), mapped to a tag and capped by what the
     family allows. Without the question, the tone comes from signals understand()
     already returns (tone_from_signals): upset_with_micmic and distress. Stressed or
     upset is [calmly] (WOW 1); in a hurry is no tag at all.
  5. Good news (WOW 4): a web task that found what she asked, a birthday reminder, a
     food timer: [excited], 07:00 to 21:00 only, never on a live answer, never when she
     is in a hurry, stressed or sad.
  6. Good morning (WOW 5): the first spoken reply of her day after 05:00 is [warmly],
     once a day (the caller keeps the date), only for a success, an answer or a question.
  7. The family's default (only safety_alert has one: [calmly]).
  8. Night (23:00-06:00, WOW 3): a would-be [warmly]/[excited] becomes [softly]; an
     untagged acknowledgement, answer or question becomes [softly]. Safety, errors and
     read-backs are left as they are.
  9. The tag must be on the allow-list for this engine AND this language, or nothing.

A tag is a separate field from the words. The cloud voice puts it in front of the line
(or, for a friend's message, after "Dana:"); the screen, the trace text, `say` and
Kokoro never see it (mac.say strips any tag-shaped bracket from the words themselves,
as a second guard).
"""
from __future__ import annotations
import os, re

# ---------------------------------------------------------------- allow-lists
# Per engine, per language. Only tags WOW_TAGS.md found reliable AND that a launch moment
# uses; a tag that a listening check shows is read aloud or misread is deleted here, not
# worked around. A language without a set gets no tag.
# Measured on eleven_v4_turbo, Jessica (micmic-work/voice/wow/WOW_TAGS.md, 213 renders,
# and samples/micmic-eleven + samples/micmic-1.2.4): no tag was ever read aloud, in any
# of the four languages. [excited] lifts the pitch (+40 Hz on good news), [softly] is
# about 50 Hz lower and 4 to 5 dB quieter, [warmly] and [calmly] are gentler and slower.
#   English, Hebrew: all four rendered and checked.
#   Arabic, Russian: [softly] [excited] rendered in the lab; [calmly] in the 1.2.4 check.
#   [warmly] was not rendered there, so it is not on.
# [apologetic] is off: subtle on Turbo, and not one of the launch moments.
LAUNCH_TAGS = frozenset({"warmly", "calmly", "softly", "excited"})
ALLOW: dict[str, dict[str, frozenset]] = {
    "eleven_turbo": {"english": LAUNCH_TAGS,
                     "hebrew": LAUNCH_TAGS,
                     "arabic": frozenset({"calmly", "softly", "excited"}),
                     "russian": frozenset({"calmly", "softly", "excited"})},
}
# Never chosen automatically, though v4 supports most of them: a sigh or a laugh at an
# older listener reads as impatience or mockery, a whisper may simply not be heard, and
# the sound effects do nothing on Turbo ([woo] even comes out as a spoken "Whew!").
NEVER_AUTO = frozenset({"sighs", "laughs", "whispers", "crying", "sarcastic", "shouts",
                        "mischievously", "applause", "clapping", "woo", "sings", "ding",
                        "giggles", "gasps", "sad", "annoyed"})
# What Gemini may put at the start of a sentence it writes (step 3).
GEMINI_ALLOW = frozenset({"warmly", "calmly", "softly"})


def enabled(settings: dict | None = None) -> bool:
    """The kill switch: MICMIC_VOICE_TAGS=off in the environment, or voice_tags false in
    settings.json. Default on, but only reachable when the voice is ElevenLabs."""
    if os.environ.get("MICMIC_VOICE_TAGS", "").strip().lower() in ("0", "off", "no", "false"):
        return False
    return bool((settings or {}).get("voice_tags", True))


# ---------------------------------------------------------------- families
# Every did in savta (census.py: 133 static + 13 built at run time) belongs to one.
FAMILY_OF: dict[str, str] = {}


def _fam(name: str, *dids: str) -> None:
    for d in dids:
        FAMILY_OF[d] = name


_fam("readback",   # her words, someone else's words, or what it is about to do: plain
     "sending", "confirm_send", "read_messages", "read_notes", "read_screen",
     "message_recall", "again", "described_screen", "summarized_screen",
     "translated_screen", "rewrote_answer", "screen_select")
_fam("confirm",        # a safety question before it acts, with none of her words in it
     "confirm_call", "confirm_web", "confirm_calendar", "confirm_event_change")
_fam("safety_alert",   # she may be in trouble, or a send was refused for her safety
     "emergency", "checking_on_her", "offered_help", "emergency_number", "refused", "blocked",
     "screen_secret")
_fam("safety_ack",     # it stopped something, or holds it
     "cancelled", "emergency_cancelled", "held_by_another_turn", "held_dropped",
     "held_twice", "still_held", "nothing_held")
_fam("ask",            # it needs one more thing from her
     "need_who", "need_what", "need_which", "need_when", "need_city", "need_event_time",
     "need_who_to_call", "asked_back", "forget_which", "preference_unclear", "needs_her",
     "app_needs_her", "close_pending", "screen_select_wait")
_fam("success",        # it did what she asked
     "playing", "opened_app", "opened_file", "opened_photos", "opened_in_app",
     "opened_there", "noted", "timer_set", "sent", "calling", "done", "app_done",
     "closed_app", "close_this", "stopped", "louder", "quieter", "brighter", "darker",
     "preference_set", "forgot", "forgot_all", "emergency_contact_set", "reminder_moved",
     "timer_cancelled", "screenshot_saved", "did_several", "helped", "web_done",
     "web_working", "reminder_fired")
_fam("info",           # a fact from code: the clock, the weather, what is set or playing
     "looked_up", "no_messages", "no_notes", "timer_recall", "reminder_recall",
     "event_recall", "player_recall", "recalled", "nothing_playing", "nothing_to_do",
     "nothing_to_forget", "nothing_to_repeat", "nothing_to_stop", "nothing_to_undo",
     "event_same", "weather_too_far", "timer_gone", "event_missing", "reminder_missing")
_fam("cant",           # not possible, not found, switched off: nobody's fault
     "cannot_do", "cannot_control", "cannot_reach_app", "cannot_seek", "cannot_send_there",
     "cant_write_in", "not_found", "exhausted", "live_not_found", "not_open",
     "send_disabled", "call_disabled", "screen_unavailable", "screen_locked",
     "screen_empty", "screen_no_event", "screen_no_llm", "screenshot_no_permission",
     "no_smart_home", "app_blocked", "half_heard", "undo_expired", "daily_limit",
     "not_configured", "event_no_invite", "event_not_removed", "reminder_not_removed",
     "weather_unavailable", "empty", "not_started")
_fam("fault",          # MicMic's own failure
     "error", "service_unavailable", "chat_timeout", "send_failed", "call_failed",
     "note_failed", "reminder_failed", "file_failed", "guide_failed", "app_failed",
     "close_failed", "screenshot_failed", "undo_failed")
_fam("gemini",         # Gemini wrote the sentence: its inline tag (step 3)
     "answered", "chatted")
_fam("silent",         # nothing is spoken
     "ignored", "waiting", "guessed")
PREFIX_FAMILY = (("undid_", "safety_ack"), ("guide_", "instruct"), ("web_", "success"),
                 ("onboarding_", "success"))

# What each family may carry (step 4 is capped by this) and its default (step 7).
ALLOWED = {
    "readback": frozenset(), "silent": frozenset(),
    "confirm": frozenset({"calmly"}),
    "safety_alert": frozenset({"calmly"}),
    "safety_ack": frozenset({"calmly"}),
    "ask": frozenset({"calmly", "softly", "warmly"}),
    "success": frozenset({"warmly", "excited", "calmly", "softly"}),
    "info": frozenset({"calmly", "softly", "warmly"}),
    "cant": frozenset({"calmly", "softly"}),
    "fault": frozenset({"calmly", "softly"}),
    "instruct": frozenset({"calmly", "softly"}),
    "gemini": frozenset({"warmly", "calmly", "softly"}),
}
DEFAULT = {"safety_alert": "calmly"}
# [excited] from her own mood is for a big win only, and only when she is celebrating.
BIG_WINS = frozenset({"playing", "web_done"})
TONE_TAG = {"neutral": None, "warm": "warmly", "calm": "calmly", "upbeat": "warmly",
            "gentle": "softly", "brief": None}
NIGHT_SOFTEN = frozenset({"success", "info", "ask"})
GREET_FAMILIES = frozenset({"success", "info", "ask"})
GREET_TONES = frozenset({"neutral", "warm", "upbeat"})
NIGHT_FROM, NIGHT_TO = 23, 6          # WOW 3: later than TAGS_DESIGN's 22:00
GOOD_NEWS_FROM, GOOD_NEWS_TO = 7, 21  # WOW 4: [excited] only in these hours
GREET_FROM = 5                        # WOW 5: the day starts at 05:00


def family(did: str | None) -> str:
    if not did:
        return "silent"
    if did in FAMILY_OF:
        return FAMILY_OF[did]
    for p, f in PREFIX_FAMILY:
        if did.startswith(p):
            return f
    return "readback"   # an outcome nobody classified is read plainly


def night(hour: int) -> bool:
    return hour >= NIGHT_FROM or hour < NIGHT_TO


def good_news_hours(hour: int) -> bool:
    return GOOD_NEWS_FROM <= hour < GOOD_NEWS_TO


def choose(did: str | None, *, tone: str = "neutral", hour: int = 12,
           language: str = "english", model: str = "eleven_turbo",
           gemini_tag: str | None = None, live_kind: str = "not_live",
           settings: dict | None = None, good_news: bool = False,
           first_today: bool = False) -> str | None:
    """The tag for one spoken reply, or None. Pure: no I/O, no model call.

    `good_news`: the caller saw a WOW 4 moment (a web task that found it, a birthday
    reminder, a food timer). `first_today`: this is the first spoken reply of her day
    after 05:00 (the caller keeps the date, so it is True at most once a day)."""
    if not enabled(settings):
        return None
    fam = family(did)
    allowed = ALLOWED.get(fam, frozenset())
    if not allowed:
        return None
    live = live_kind not in ("not_live", "", None)
    tag = DEFAULT.get(fam)
    if fam == "gemini":
        tag = None
        if not live and gemini_tag in GEMINI_ALLOW:
            tag = gemini_tag
    want = TONE_TAG.get(tone)
    if tone == "upbeat" and did in BIG_WINS:
        want = "excited"
    if tone == "brief":
        # In a hurry: no decoration at all, except the calm of a safety alert.
        tag = tag if fam == "safety_alert" else None
    elif want and want in allowed:
        # Her state wins over a default and over Gemini's guess.
        tag = want
    elif (good_news and not live and tone not in ("calm", "gentle")
          and good_news_hours(hour)):
        tag = "excited"                  # WOW 4: good news sounds good, by day
    elif (first_today and tag is None and fam in GREET_FAMILIES and tone in GREET_TONES
          and hour >= GREET_FROM and not night(hour)):
        tag = "warmly"                   # WOW 5: good morning, once a day
    if night(hour) and fam not in ("safety_alert", "safety_ack", "confirm", "fault",
                                   "cant"):
        if tag in ("warmly", "excited"):
            tag = "softly"
        elif tag is None and fam in NIGHT_SOFTEN and tone != "brief":
            tag = "softly"
    return _allowed(tag, allowed, model, language)


def _allowed(tag, allowed, model: str, language: str) -> str | None:
    if tag is None or tag in NEVER_AUTO or tag not in allowed:
        return None
    if tag not in ALLOW.get(model, {}).get(language, frozenset()):
        return None
    return tag


# ---------------------------------------------------------------- a friend's message (WOW 2)
# Her friend's incoming message, read in her friend's tone. The tag goes after "Dana:",
# so MicMic's own words stay plain. Never for what SHE sends (confirm_send, sending).
# A bad-news word first: "passed away" is not "passed".
_BAD_NEWS = re.compile(
    r"hospital|passed\s+away|\bdied\b|\bdeath\b|accident|sorry\s+to\s+tell|funeral|"
    r"bad\s+news|\bcancer\b|\bICU\b|"
    r"בית\s*חולים|בבית\s*החולים|נפטר|נפטרה|תאונה|הלוויה|בשורה\s*רעה|"
    r"مستشفى|المستشفى|توفي|توفيت|حادث|جنازة|"
    r"больниц|умер|скончал|авари|похорон|плохие\s+новост", re.I)
_JOY_WORDS = re.compile(
    r"got\s+the\s+job|\bengaged\b|\bpregnant\b|we\s+won|\bpassed\b|\byay\b|"
    r"mazal\s+tov|mazel\s+tov|congrat\w*|"
    r"מזל\s*טוב|התארסנו|התארסה|בהריון|קיבלתי\s+את\s+העבודה|ניצחנו|עברתי|"
    r"مبروك|مبارك|خطبت|حامل|نجحت|"
    r"поздравля|ура\b|помолвк|беременн|я\s+сдал|мы\s+выиграли", re.I)
_JOY_EMOJI = ("\U0001F389", "\U0001F60D", "\U0001F973", "\u2764", "\U0001F602")   # 🎉 😍 🥳 ❤️ 😂
# "I didn't get the job!!" is not joy: a denial in the message and the joy is not read.
_DENIAL = re.compile(r"n't\b|\bnot\b|\bnever\b|\bלא\b|\bلم\b|\bلا\b|\bما\b|\bне\b", re.I)


def incoming_tone(body: str) -> str | None:
    """"excited", "softly" or None for one incoming message, from its own words."""
    body = body or ""
    if _BAD_NEWS.search(body):
        return "softly"
    joy = (body.count("!") >= 2 or any(e in body for e in _JOY_EMOJI)
           or bool(_JOY_WORDS.search(body)))
    if joy and not _DENIAL.search(body):
        return "excited"
    return None


def incoming(body: str, *, hour: int = 12, tone: str = "neutral",
             language: str = "english", model: str = "eleven_turbo",
             settings: dict | None = None) -> str | None:
    """The tag for a friend's incoming message, or None. At night her friend's joy is
    read plainly (not softly: it is the friend's joy, not MicMic's); in a hurry, none."""
    if not enabled(settings) or tone == "brief":
        return None
    tag = incoming_tone(body)
    if tag == "excited" and night(hour):
        return None
    return _allowed(tag, frozenset({"excited", "softly"}), model, language)


# ---------------------------------------------------------------- good news (WOW 4)
_BIRTHDAY = re.compile(r"birthday|b-day|יום\s*הולדת|יומולדת|عيد\s*ميلاد|день\s*рождени", re.I)
_FOOD = re.compile(
    r"\b(?:pasta|spaghetti|rice|eggs?|oven|cake|pizza|cookies?|bread|soup|lasagn\w*|"
    r"muffins?|roast|chicken|potato(?:es)?|brownies?|pie|noodles)\b|"
    r"פסטה|ספגטי|אורז|ביצ|תנור|עוגה|עוגיות|פיצה|לחם|מרק|עוף|"
    r"معكرونة|مكرونة|رز|أرز|بيض|فرن|كعكة|كيك|بيتزا|خبز|شوربة|"
    r"макарон|паст|рис\b|яйц|духовк|торт|пицц|пирог|хлеб|суп", re.I)


def birthday(text: str) -> bool:
    return bool(_BIRTHDAY.search(text or ""))


def food(text: str) -> bool:
    return bool(_FOOD.search(text or ""))


# ---------------------------------------------------------------- her tone, no new question
def tone_from_signals(u: dict) -> str:
    """Tone from what understand() already returns. Can only ever say calm or neutral:
    there is no existing signal for joy, sadness or hurry (TAGS_DESIGN.md, measured)."""
    if u.get("at_micmic", 0.0) > 0.5:
        return "calm"
    if u.get("distress", 0.0) >= 1.0 or u.get("emergency", 0.0) >= 0.5:
        return "calm"
    return "neutral"


# ---------------------------------------------------------------- the cheap gate
# reply_tone is asked only when the voice is ElevenLabs AND one of these is in her words.
# Written from general vocabulary; see TAGS_DESIGN.md for its in-sample recall (optimistic,
# the test set was written by the same hand) and its out-of-sample fire rate on Bench v1.
_CUES = re.compile(
    r"\b(?:sad|lonely|alone|miss(?:ing)?\s+(?:him|her|my|them|you)|died|passed\s+away|funeral|"
    r"cry(?:ing)?|tired|exhausted|sick|unwell|awful|terrible|"
    r"nervous|worried|worry|anxious|scared|afraid|panic\w*|stress\w*|upset|angry|"
    r"oh\s+no|help|wrong|again|still|never|stupid|useless|dumb|idiot|annoying|"
    r"happy|yay|hooray|great\s+news|wonderful|amazing|won|celebrat\w*|birthday|"
    r"engaged|wedding|congrat\w*|"
    r"thank(?:s|\s+you)|sweetie|sweetheart|darling|dear|treasure|love\s+you|good\s+morning|"
    r"quick(?:ly)?|hurry|fast(?:est)?|asap|no\s+time|late|right\s+now|immediately|"
    r"come\s+on|already)\b|!|"
    r"עצוב|בודד|לבד|מתגעגע|נפטר|הלוויה|בוכה|עייפ|חולה|נורא|"
    r"לחוצ|לחוץ|דואג|מפחד|פוחד|מתחרפנ|כועס|מעצבנ|מטומטמ|טיפש|שוב|עדיין|"
    r"שמח|כיף|יש!|מזל\s*טוב|יום\s*הולדת|חתונה|"
    r"תודה|מותק|מתוק|נשמה|יקירה|בוקר\s*טוב|"
    r"מהר|דחוף|אין\s*לי\s*זמן|מאחר|תזדרז|כבר|"
    r"حزين|وحيد|اشتقت|خايف|زعلان|مبسوط|فرحان|شكرا|حبيبتي|بسرعة|مستعجل|"
    r"грустн|одинок|скучаю|умер|волну|боюсь|страшно|злюсь|рад|ура|спасибо|дорог|"
    r"быстр|скорее|срочно|опаздыва",
    re.I)


def may_carry_feeling(utterance: str) -> bool:
    return bool(_CUES.search(utterance or ""))


# ---------------------------------------------------------------- Gemini's inline tag
GEMINI_TAG_RULE = (
    "Your reply is spoken by a voice that understands one delivery direction in square "
    "brackets at the very start. Begin with exactly one of these, or with nothing: {tags}. "
    "Use [calmly] if she sounds stressed, worried or annoyed; [softly] if she sounds sad, "
    "lonely or tired; [warmly] only if she is friendly, thankful or happy. For a plain "
    "question, a fact or a joke, use nothing. Never put square brackets anywhere else, and "
    "the direction never changes your words.")

_LEAD = re.compile(r"^\s*\[\s*([A-Za-z][A-Za-z '\-]{0,30}?)\s*\]\s*")
# Any other tag-shaped bracket the model left in: one to three lowercase words.
_STRAY = re.compile(r"\s*\[\s*[a-z][a-z '\-]{0,30}\]\s*")


def gemini_rule(allow=GEMINI_ALLOW) -> str:
    return GEMINI_TAG_RULE.format(tags=" ".join(f"[{t}]" for t in sorted(allow)))


def split_tag(text: str | None, allow=GEMINI_ALLOW) -> tuple[str | None, str | None]:
    """(tag or None, the words without any tag). A leading tag on the allow-list is kept;
    one that is not is dropped (never spoken, never shown); any other tag-shaped bracket
    is removed from the words. Runs on every Gemini line that was asked for a tag, before
    anything else looks at the words (claims_action, the screen, the trace)."""
    if text is None:
        return None, None
    tag = None
    m = _LEAD.match(text)
    if m:
        cand = m.group(1).strip().lower()
        text = text[m.end():]
        tag = cand if cand in allow else None
    text = _STRAY.sub(" ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return tag, (text or None)


def strip_tags(text: str) -> str:
    """For say and Kokoro, and the screen: no tag-shaped bracket is ever read out."""
    return re.sub(r"\s{2,}", " ", _STRAY.sub(" ", _LEAD.sub("", text or ""))).strip()


def tags_for(language: str, engine: str = "eleven_turbo") -> frozenset:
    """Every tag this engine may say in this language (empty: none at all)."""
    return ALLOW.get(engine, {}).get(language, frozenset())


def gemini_allow(language: str, engine: str = "eleven_turbo") -> frozenset:
    """The tags Gemini may choose for a line in this language."""
    return GEMINI_ALLOW & tags_for(language, engine)


def cacheable(did: str | None) -> bool:
    """The family half of PRIVATE_LINE_RULE below: only a line MicMic's own code wrote.
    Never a read-back (her messages, her notes, her screen, a message about to go),
    never a sentence Gemini wrote for her."""
    return family(did) not in ("readback", "gemini", "silent")


# ---------------------------------------------------------------- keeping a line's audio
# PRIVATE_LINE_RULE. The audio of a spoken line may be kept (on this Mac, savta/voice.py's
# cache, so the line plays again with no network and no credits; and in MicMic's cloud,
# proxy /data/tts-cache) only when ALL of these hold:
#   1. its family is one MicMic's own code writes (cacheable(): never readback, gemini
#      or silent: her messages, messages read aloud, her notes, her screen, Gemini's
#      sentences);
#   2. its did is not one whose words name a person or quote her by design
#      (PERSONAL_DIDS, PERSONAL_PREFIXES: a send, a call, an emergency call, a note, a
#      reminder, a search that found nothing "for bad bunny", a web task, a guide step);
#   3. no name from this turn is in it (the contact, the sender, her own name, her
#      emergency contact: the router hands them in);
#   4. none of her own words is echoed in it: no two words in a row of what she said
#      unless MicMic's own lines say those two words too ("send it to" is MicMic's),
#      and no single word of hers (3+ letters) that MicMic's lines never use and that is
#      not the name of an app or service on this Mac ("Dina", "bunny"; "Calculator" is
#      an app).
# Anything else is spoken fresh every time.
PERSONAL_DIDS = frozenset({
    "sending", "sent", "confirm_send", "confirm_call", "calling", "need_which",
    "emergency", "emergency_cancelled", "emergency_contact_set", "offered_help",
    "confirm_calendar", "confirm_event_change", "noted", "reminder_fired", "reminder_recall", "reminder_moved", "reminder_missing",
    "reminder_not_removed", "timer_recall", "event_recall", "event_missing", "event_same",
    "event_no_invite", "event_not_removed", "recalled", "preference_set", "forgot",
    "held_by_another_turn", "held_dropped", "held_twice", "still_held", "cancelled",
    "exhausted", "not_found", "live_not_found", "not_started", "opened_file", "opened_photos",
    "did_several", "asked_back", "send_failed", "cant_write_in",
    "cannot_send_there"})
PERSONAL_PREFIXES = ("web_", "guide_", "onboarding_", "undid_")
_WORD = re.compile(r"\w+", re.U)
_HE_PREFIX = set("לבהושמכ")


def word_pairs(text: str) -> set:
    """Two words in a row, lower case: what rule 4 compares."""
    w = [x.lower() for x in _WORD.findall(text or "")]
    return {(a, b) for a, b in zip(w, w[1:])}


def private_line(did: str | None, text: str, *, heard: str = "", names=(),
                 code_pairs=frozenset(), code_words=frozenset()) -> str | None:
    """Why PRIVATE_LINE_RULE keeps this line's audio from being kept, or None when it
    may be kept. `heard`: what she said this turn. `names`: people this turn is about.
    `code_pairs`: word_pairs() of every line MicMic's own code can say; `code_words`:
    their single words, with the apps and services on this Mac (lower case)."""
    if not cacheable(did):
        return "family"
    if did in PERSONAL_DIDS or any(str(did).startswith(p) for p in PERSONAL_PREFIXES):
        return "personal"
    words = {x.lower() for x in _WORD.findall(text or "")}
    for n in names or ():
        for part in _WORD.findall(str(n or "")):
            if len(part) >= 2 and part.lower() in words:
                return "name"
    if heard and (word_pairs(heard) & word_pairs(text)) - set(code_pairs):
        return "her_words"
    if heard:
        hers = {x.lower() for x in _WORD.findall(heard) if len(x) >= 3}
        # Hebrew joins a one-letter word to the next: "לדינה" is "to Dina".
        hers |= {x[1:] for x in hers if len(x) >= 4 and x[0] in _HE_PREFIX}
        if (hers & words) - set(code_words):
            return "her_words"
    return None


def keep_audio(did: str | None, text: str, **kw) -> bool:
    """PRIVATE_LINE_RULE: may this line's audio be kept? See private_line()."""
    return bool(text) and private_line(did, text, **kw) is None
