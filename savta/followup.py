"""A follow-up changes the last thing MicMic did, in the same place, instead of starting
over.

The owner, on 1.1.0: "play Shakira in Apple Music", then "change to Bad Bunny", went
to the wrong place. That one was fixed for music alone (router._media_player). He wants
the same everywhere, built once: "make it 10 minutes instead" after a reminder, "move it
to Friday" after a calendar event, "pause" or "back 10 seconds" over whatever MicMic put
on, "shorter" or "in Hebrew" after it described her screen.

How it is decided, within the project's rules (see brain.py):
  * The conversation memory keeps the LAST ACTION: what kind of thing was done, where,
    the ids that point at it (the timer, the calendar event's uid, the video's tab) and
    its parameters. A new request that is not a follow-up clears it (router.finish), and
    so does a new chat (router.new_conversation).
  * A cheap code pre-check: her words hold a modifier word (instead, move, make it,
    pause, next, shorter, cancel, "and ...?", in four languages) AND there is a recent
    last action, or something MicMic put on is playing. Only then is anything asked.
    Every other request goes out byte for byte as it did before this existed.
  * ONE choice question rides in the understand() request already going out: which
    change to that action she is asking for, or "none" (a new request). Its options are
    enumerated by code from the action's kind, each carrying its value ("reminder at
    18:00", "event on Friday 2 October", "back 10 seconds"), so no second question is
    needed for the value. The winning field is read by summing its options'
    probabilities: "at 7" split between 07:00 and 19:00 is still clearly a new time.
  * Code does it: a timer is moved or stopped, the event MicMic created (by its uid,
    nothing else) is changed after a yes, the player that is playing is driven by
    AppleScript or key presses to its own tab. No model call, except where a sentence
    must be written ("shorter" and "in Hebrew" rewrite an answer: one Gemini call).

Measured 2026-09-27 (jev-latest, the exact understand() shape the router sends: fixture
contacts, the memory snapshot, the folded spans and this fold; n=15, one run each, 15
real calls, $0.0048, understand() 394-435 ms warm): 14/15 right, both controls "none".
Timer: "make it 10 minutes instead" in-10 0.79 (10-later 0.15), "move it to 6pm" 18:00
0.94, "תבטלי את זה" cancel 1.00, "what was it again?" recall 0.99. Event: "make it an
hour and a half" 90 min 0.67, "at 7 instead" 19:00 (start field 0.63; the yes/no step
is what catches a wrong half of the day), "תוסיפי את דנה" invite 0.93; the one miss,
"move it to Friday", had the field right (day 0.92) and the value split between the
event's own day (0.47) and Friday (0.45), which is why an event's current day, start
and length are no longer offered (NOT re-measured: the call budget was spent). Player:
"pause" 1.00, "back 10 seconds" 1.00, "תעשי מסך מלא" full screen 1.00. Answer:
"shorter" 1.00, "say that in Hebrew" 0.99. Controls: "change to Bad Bunny" over Apple
Music none 0.98, "and what's the weather in Paris?" after a reminder none 1.00. The
gate (FOLLOW_GATE 0.5 on the summed field) sits between the controls' best change
(0.02) and the lowest right answer (0.63).
Gemini, 2 calls: "shorter" 874 ms, "in Hebrew" 906 ms, no em dash in either.

A message MicMic sent, or is counting down, is a last action too (1.2.0, the owner's
session): the link to the page she had open went out on iMessage, "oh i meant on
whatsapp" was asked what the message should say, "send her this page" was asked it
again as a repeated instruction, and "just a link to this page on whatsapp" was then
queued as the message, word for word. Now "on WhatsApp" re-sends the same thing there,
"to Gal too" sends it to Gal as well, "I meant Gal" sends it to Gal instead, and "what
did I send?" reads it back, all through the usual read-back and countdown. The draft
(router: amending) already carries a message of her OWN words to another app or person,
so for those only "too", "what did I send" and an app MicMic cannot use are asked here;
content from her screen (a link, a selection, a screenshot), which the draft cannot
carry, is changed here in every way. Its pre-check is its own (says_message_change):
an app's name, too, instead, I meant, the same, what did I send.

While that message is still counting down, a bare correction is about it too (1.2.0,
2026-09-28, the owner's session): "to Dana not Dina" held none of the words above, was
never asked as a change, and the screenshot went to both. corrects_message() opens the
same question then, and only then; a change of person or app REPLACES the one counting
down (router._arm_send, _send_again), and she hears which one was stopped. Measured
2026-09-28 (jev-latest, the router's exact understand() request, a screenshot counting
down, n=1 each, 6 real calls in all, $0.0020, 382-828 ms): "to Dana not Dina" and
"לדנה לא לדינה" send-it-to-another-person-instead 1.00 and 0.99, contact Dana 0.98;
"please only send on whatsapp from now on not on messages" send-again-on-WhatsApp 0.83
with pref_kind message_app 1.00 (saved too, router._standing_app); the control "no,
what's the weather tomorrow" none 1.00, and the countdown kept going.

And her answer to "what should it say?" (router._resume) may be an instruction about
the content rather than the content: "this page", "the link", "the same", "on
WhatsApp", "tell her I'm coming". When points_at_content() sees such words, one choice
rides in the question already going out for that answer (answer_question), plus a span
for the words when she reports speech ("tell her ..."); every other answer is asked
exactly what it was, and is sent whole, as it always was.

Measured 2026-09-27 (jev-latest, commit c182384 + this change, the exact shapes the
router sends: understand() through router.handle with the link just sent to Dana on
iMessage, and _resume for "what should it say?" on WhatsApp; n=8, one run each, 8 real
calls, $0.0015; understand() 387-523 ms warm, 934 ms cold; _resume 304-338 ms): 8/8
right. Follow-up: "oh i meant on whatsapp" and "התכוונתי בוואטסאפ" send-again-on-WhatsApp
1.00; the controls, asked with the pre-check forced open, "send Dana hi" none 0.99 and
"play Bad Bunny" none 1.00. Answer: "send her this page" screen 0.97, "just a link to
this page on whatsapp" screen 0.89, "תשלחי לה את הקישור לדף הזה" screen 0.93; the
control "tell Dana I'm late" words 1.00 with the span "I m late" (exists 0.81), sent as
"I'm late". Both gates (FOLLOW_GATE, ANSWER_GATE 0.5) sit between the controls' best
wrong option (0.01, 0.00) and the lowest right answer (1.00, 0.89).
"""
from __future__ import annotations
import datetime as _dt
import re
import time

from .actions import targets as tg
from .brain import span_candidates, _span_questions, _span_answer

# How long a last action stays something she can change by pointing at it. "A few
# minutes": past this, "make it later" is more likely about something new.
FOLLOW_TTL = 300.0
# A film or a playlist plays for a long time; "pause" an hour in still means it.
PLAYER_TTL = 3 * 3600.0
# The winning change (its options' probabilities summed) must reach this, or the
# sentence is handled as a new request exactly as it would have been.
FOLLOW_GATE = 0.5

# ---------------------------------------------------------------- the pre-check
# Words that change something rather than ask for something. Broad on purpose: this only
# decides whether the question is ASKED, and Jev answers "none" for a new request. What
# it must never do is fire on a request with no recent action, which it cannot: that is
# checked first.
_EN = (r"\binstead\b|\bchange\b|\bmove\b|\bmake it\b|\bmake that\b|\bmore\b|\bless\b|"
       r"\blouder\b|\bquieter\b|\bsofter\b|\bturn it (?:up|down)\b|\bpause\b|\bresume\b|"
       r"\bcontinue\b|\bcarry on\b|\bunpause\b|\bskip\b|\bnext\b|\bprevious\b|\bback\b|"
       r"\bforward\b|\brewind\b|\bshorter\b|\blonger\b|\bsimpler\b|\bcancel\b|\bcall it off\b|"
       r"\bwhat was (?:it|that)\b|\bagain\b|\bfull ?screen\b|\bbigger\b|\bearlier\b|"
       r"\blater\b|\bpush it\b|\bpostpone\b|\bdelay\b|\bbring it\b|\badd \w+ to it\b|"
       r"\binvite\b|\bsend (?:it|that|this) to\b|\bin (?:hebrew|english|arabic|russian)\b|"
       r"\bthe (?:first|second|third|last|cheaper|cheapest) one\b|"
       r"\b(?:at|to|for) \d|^\s*and\b|\bhow long\b|\bstop it\b")
_HE = (r"במקום|תשני|לשנות|שני את|תזיזי|תזיז|להזיז|תעבירי|תעביר|תדחי|תדחה|לדחות|תקדימי|"
       r"תעשי את זה|תעשה את זה|שיהיה|יותר|פחות|תגבירי|תגביר|תנמיכי|תנמיך|חזק|בשקט|"
       r"עצרי|תעצרי|תעצור|השהי|תשהי|תמשיכי|תמשיך|המשיכי|דלגי|תדלגי|תדלג|הבא|הבאה|הקודם|"
       r"הקודמת|אחורה|קדימה|קצר|תקצרי|תקצר|בקיצור|בטלי|תבטלי|תבטל|לבטל|מה זה היה|מה היה|"
       r"שוב|מסך מלא|בעברית|באנגלית|בערבית|ברוסית|תשלחי את זה|תשלח את זה|תוסיפי את|"
       r"הראשון|השני|השנייה|הזול|מוקדם|מאוחר|בשעה|לשעה|^\s*ו[^ו]")
_AR = (r"بدل|غيّر|غير|انقل|أجّل|اجّل|خلّيها|خليها|أكتر|أكثر|أقل|علّي|علي الصوت|وطّي|وطي|"
       r"وقّف|وقف|كمّل|كمل|التالي|اللي بعد|رجّع|ارجع|لورا|قصّر|قصر|أقصر|ألغي|الغي|لغي|"
       r"شو كان|كمان مرة|شاشة كاملة|بالعبري|بالإنجليزي|بالانجليزي|بالعربي|بالروسي|ابعتي|"
       r"ابعت|الأول|التاني|الثاني|الأرخص|أبكر|أبكير|متأخر|^\s*و")
_RU = (r"вместо|измени|поменяй|перенеси|передвинь|сдвинь|сделай|больше|меньше|громче|"
       r"тише|пауз|останови|продолж|дальше|следующ|пропусти|предыдущ|назад|вперёд|вперед|"
       r"короче|отмени|что это было|ещё раз|еще раз|весь экран|полный экран|по-русски|"
       r"на иврите|по-английски|по-арабски|на английском|отправь это|перв|втор|дешевле|"
       r"дешёв|раньше|позже|^\s*(?:а|и)\s")
MODIFIERS = re.compile("|".join((_EN, _HE, _AR, _RU)), re.IGNORECASE)


def says_modifier(utterance: str) -> bool:
    return bool(MODIFIERS.search(utterance or ""))


# A message just sent is asked about only when her words hold one of these: the name of
# an app to send by, one MicMic cannot send by, too, instead or "I meant", the same, or
# "what did I send". Words about her own message's app or person stay with the draft.
MESSENGER_WORDS = "|".join(f"(?:{tg.SERVICES[k]['words']})" for k in tg.MESSENGERS)
_TEXT_APP = (r"\bby text\b|\bas a text\b|\ba text message\b|\btext message\b|\bsms\b|"
             r"הודעת טקסט|בהודעה רגילה|בסמס|במסרון|رسالة نصية|برسالة عادية|\bсмс\b|эсэмэс")
_OTHER_APPS = (r"\be-?mail\b|\bmail\b|\bmessenger\b|\bfacebook\b|\binstagram\b|\bviber\b|"
               r"\bwechat\b|\bskype\b|מייל|אימייל|פייסבוק|מסנג'?ר|ויי?בר|אינסטגרם|"
               r"[إا]يميل|بريد|فيس\s?بوك|ما?سنجر|فايبر|[إا]نستغرام|почт|[иеэ]?мейл|"
               r"фейсбук|мессенджер|вайбер|инстаграм")
_ALSO = (r"\btoo\b|\balso\b|\bas well\b|(?:^|\s|ו)גם(?=\s|$|[?.!,])|كمان|[أا]يضا|برضو|"
         r"\bтоже\b|\bтакже\b")
_RECALL = (r"what (?:did|have) (?:i|you) (?:just )?sen[dt]|what was (?:in )?(?:the|that) "
           r"message|what did (?:it|the message) say|what did you write|מה שלחתי|מה שלחת|"
           r"מה כתבת|מה היה כתוב|מה נשלח|شو بعتت|شو بعتي|شو كتبت|شو انبعت|"
           r"что (?:я|ты|вы) отправил|что отправила|что ушло|что было в сообщении")
_INSTEAD = (r"\bmeant\b|\binstead\b|התכוונתי|במקום|قصدي|قصدت|بدل|имел[аи]? в виду|вместо")
_SAME = (r"\bthe same\b|\bsame (?:link|thing|page|one|message)\b|אותו דבר|אותו הדבר|"
         r"אותו קישור|אותו לינק|אותה הודעה|نفس|\bто же\b|\bтот же\b|\bту же\b")
_MSG_ANY = re.compile("|".join((MESSENGER_WORDS, _TEXT_APP, _OTHER_APPS, _ALSO, _RECALL,
                                _INSTEAD, _SAME)), re.IGNORECASE)
_MSG_OWN_WORDS = re.compile("|".join((_OTHER_APPS, _ALSO, _RECALL)), re.IGNORECASE)


def says_message_change(utterance: str, own_words: bool = False) -> bool:
    """Her words may be about the message just sent. `own_words`: that message was
    words she said, whose app and person the draft changes (see the module notes)."""
    return bool((_MSG_OWN_WORDS if own_words else _MSG_ANY).search(utterance or ""))


# While a message from her screen is still counting down, a bare correction is about it
# too: "to Dana not Dina", "no, Dana", "not Dina", "לדנה לא לדינה". The owner's session
# (1.2.0, 2026-09-28) held none of the words above, so it was never asked as a change.
# Only then: after the countdown these words start a new request, as they always did.
_CORRECTS = re.compile(r"\bnot\b|^\s*no\b|\bno,|(?:^|\s)לא(?=\s|$|[,.!?])|"
                       r"(?:^|\s)(?:مش|لا|لأ)(?=\s|$|[,.!?،])|\bне\b|^\s*нет\b",
                       re.IGNORECASE)


def corrects_message(utterance: str) -> bool:
    return bool(_CORRECTS.search(utterance or ""))


def asks_for_both(utterance: str) -> bool:
    """"To Gal too", "the same to Gal": she wants it to go to another person AS WELL."""
    return bool(re.search(f"{_ALSO}|{_SAME}", utterance or "", re.IGNORECASE))


def app_named(utterance: str) -> str | None:
    """The app to send by that her words name (a key of targets.MESSENGERS), or None."""
    for k in tg.MESSENGERS:
        if re.search(tg.SERVICES[k]["words"], utterance or "", re.IGNORECASE):
            return k
    return "imessage" if re.search(_TEXT_APP, utterance or "", re.IGNORECASE) else None


# Her answer to "what should it say?" is looked at again only when it may point at the
# content instead of being it: the screen or a page, a link, the same again, an app,
# or a reported-speech opening ("tell her ...", "תגידי לה ...") whose words are the
# message. Broad on purpose: this only decides whether the question is asked.
_SCREEN_REF = (r"\bpage\b|\blink\b|\burl\b|\bweb ?site\b|\bscreen\b|\bscreenshot\b|"
               r"\bwhat i (?:selected|highlighted|marked)\b|^\W*(?:this|that|it)\W*$|"
               r"דף|עמוד|קישור|לינק|מסך|אתר|מה שסימנתי|^\W*(?:את\s+)?(?:זה|הזה)\W*$|"
               r"صفحة|رابط|لينك|شاشة|موقع|اللي حددته|^\W*(?:هاد|هاي|هذا|هذه)\W*$|"
               r"страниц|ссылк|экран|скриншот|сайт|что я выделил|^\W*(?:это|вот это)\W*$")
_REPORTED = (r"^\W*(?:(?:oh|ok|okay|so|and|please|just|well|um|uh)\W+)*"
             r"(?:tell|say|ask|let|write|text)\b|"
             r"^\W*(?:ו?(?:ש)?(?:תגיד|תגידי|תכתבי|תכתוב|תרשמי|תשאלי|תשאל|תודיעי|תודיע|"
             r"תאמרי|תמסרי|תמסור))|"
             r"^\W*(?:قولي|قوليل|قوله|احكي|احكيل|اكتبي|اسألي|خبري|بلغي)|"
             r"^\W*(?:скажи|скажите|напиши|напишите|передай|передайте|спроси|спросите)")
_POINTS = re.compile("|".join((_SCREEN_REF, _SAME, MESSENGER_WORDS, _TEXT_APP, _REPORTED)),
                     re.IGNORECASE)


def points_at_content(utterance: str) -> bool:
    return bool(_POINTS.search(utterance or ""))


def reports_speech(utterance: str) -> bool:
    """"Tell her I'm coming": the message is part of what she said. Only then is a part
    of her answer taken as the message; anything else she says is sent whole, as ever."""
    return bool(re.search(_REPORTED, utterance or "", re.IGNORECASE))


# ---------------------------------------------------------------- what is in play
def in_play(last_action: dict | None, last_played: dict | None,
            now: float | None = None) -> list[dict]:
    """The things a follow-up could be about, newest first: the last action while it
    is recent, and whatever MicMic put on while it may still be playing."""
    now = time.time() if now is None else now
    out = []
    if last_action and now - last_action.get("at", 0) <= FOLLOW_TTL:
        out.append(last_action)
    if (last_played and last_played.get("player")
            and now - last_played.get("at", now) <= PLAYER_TTL
            and not (last_action and last_action.get("kind") == "player")):
        out.append({"kind": "player", **last_played})
    return out


# ---------------------------------------------------------------- the question
_HALF_HOURS = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
_TIMER_MIN = (1, 2, 3, 5, 10, 15, 20, 25, 30, 40, 45, 60, 90, 120, 180, 240)
_SHIFTS = (5, 10, 15, 30, 60, 120)
_EVENT_LEN = (15, 30, 45, 60, 90, 120, 180, 240)
SPOKEN = ("english", "hebrew", "arabic", "russian")
PLAYER_ACTS = ("pause", "resume", "next", "previous", "back_10", "forward_10", "louder",
               "quieter", "full_screen", "exit_full_screen", "next_episode", "restart",
               "recall")
_PLAYER_KEYS = {
    "pause": ("pause it", "Pause it, stop it for a moment, hold on. עצרי רגע, השהי. "
                          "وقّفي شوي. пауза."),
    "resume": ("carry on playing", "Carry on, resume, unpause, play it again from where "
                                   "it stopped. תמשיכי. كمّلي. продолжи."),
    "next": ("the next song or video", "Next, skip this one, the next one. הבא, דלגי. "
                                       "التالي. следующую."),
    "previous": ("the previous song or video", "The one before, go back to the last "
                                               "song. הקודם. اللي قبل. предыдущую."),
    "back_10": ("back 10 seconds", "Back a little, rewind, back ten seconds, I missed "
                                   "that. אחורה קצת. رجّعي شوي. назад на десять секунд."),
    "forward_10": ("forward 10 seconds", "Forward a little, skip ahead ten seconds. "
                                         "קדימה קצת. قدّمي شوي. вперёд немного."),
    "louder": ("louder", "Louder, turn it up, I cannot hear it. תגבירי. علّي. громче."),
    "quieter": ("quieter", "Quieter, turn it down, too loud. תנמיכי. وطّي. тише."),
    "full_screen": ("full screen", "Make it full screen, make the picture fill the screen, "
                                   "bigger. מסך מלא. شاشة كاملة. на весь экран."),
    "exit_full_screen": ("leave full screen", "Leave full screen, make it small again. "
                                              "צאי ממסך מלא. صغّري. выйди из полного экрана."),
    "next_episode": ("the next episode", "The next episode, the next part. הפרק הבא. "
                                         "الحلقة الجاية. следующую серию."),
    "restart": ("start it again", "From the beginning, start it over. מההתחלה. من الأول. "
                                  "сначала."),
    "recall": ("say what is playing", "What is this, what is playing, what was it again. "
                                      "מה זה. شو هاد. что это."),
}
_LANG_KEY = {"english": "in English", "hebrew": "in Hebrew", "arabic": "in Arabic",
             "russian": "in Russian"}


def _options(item: dict, today: _dt.date) -> dict[str, tuple]:
    """{option shown to Jev: (kind, field, value, description or None)} for one thing in play."""
    kind = item["kind"]
    o: dict[str, tuple] = {}
    if kind == "timer":
        for n in _TIMER_MIN:
            o[f"reminder in {n} minutes from now"] = ("timer", "in", n, None)
        for t in _HALF_HOURS:
            o[f"reminder at {t}"] = ("timer", "at", t, None)
        for n in _SHIFTS:
            o[f"reminder {n} minutes later"] = ("timer", "later", n, None)
            o[f"reminder {n} minutes earlier"] = ("timer", "earlier", -n, None)
        o["cancel the reminder"] = ("timer", "cancel", None,
                                    "Cancel it, stop the reminder, never mind it. בטלי את "
                                    "זה. ألغيه. отмени его.")
        o["say what the reminder is"] = ("timer", "recall", None,
                                         "What was it again, what did I ask you to remind "
                                         "me, when is it. מה זה היה. شو كان. что это было.")
    elif kind == "event":
        # What the event already is is never offered: that change would change nothing,
        # and measured, the option naming its own day drew the answer away from the one
        # she said ("move it to Friday": its own Monday 0.47 against Friday 0.45).
        try:
            ev_day = _dt.date.fromisoformat(item.get("date") or "")
        except ValueError:
            ev_day = today
        now_len = _length(item)
        last = max(today + _dt.timedelta(days=13), ev_day + _dt.timedelta(days=7))
        d = today
        while d <= last and len(o) < 21:
            if d != ev_day:
                label = d.strftime("%A %-d %B")
                note = ("Today." if d == today
                        else "Tomorrow." if d == today + _dt.timedelta(days=1) else None)
                o[f"event on {label}"] = ("event", "day", d.isoformat(), note)
            d += _dt.timedelta(days=1)
        for t in _HALF_HOURS:
            if t != item.get("start"):
                o[f"event starts at {t}"] = ("event", "at", t, None)
        for n in _EVENT_LEN:
            if n != now_len:
                o[f"event lasts {n} minutes"] = ("event", "length", n, None)
        o["event lasts the whole day"] = ("event", "all_day", True, None)
        for n in (15, 30, 60, 120):
            o[f"event {n} minutes later"] = ("event", "later", n, None)
            o[f"event {n} minutes earlier"] = ("event", "earlier", -n, None)
        o["invite someone to the event"] = ("event", "invite", None,
                                            "Add a person to it, invite someone, tell someone "
                                            "about it. תוסיפי את דנה. ضيفي دانا. добавь Дану.")
        o["say what the event is"] = ("event", "recall", None,
                                      "What was it again, when is it. מה זה היה. شو كان. "
                                      "что это было.")
        o["remove the event"] = ("event", "remove", None,
                                 "Cancel it, remove it, take it off the calendar. תבטלי "
                                 "אותו. شيليه. удали его.")
    elif kind == "player":
        for act in PLAYER_ACTS:
            key, desc = _PLAYER_KEYS[act]
            o[key] = ("player", act, None, desc)
    elif kind == "answer":
        o["say it shorter"] = ("answer", "shorter", None,
                               "Shorter, in short, just the main point. תקצרי, בקיצור. "
                               "باختصار. короче.")
        for lang, key in _LANG_KEY.items():
            o[f"say it {key}"] = ("answer", "language", lang, None)
        o["send it to someone"] = ("answer", "send", None,
                                   "Send that to Dana, send it to my son. תשלחי את זה לדנה. "
                                   "ابعتيه لدانا. отправь это Дане.")
        o["say it again"] = ("answer", "repeat", None,
                             "Say it again, what did you say. תגידי שוב. عيدي. повтори.")
    elif kind == "message":
        # The app it went by is not offered again: that change would change nothing.
        # Her own words to another app or person are the draft's (module notes).
        own = bool(item.get("own_words"))
        if not own:
            for app, (key, desc) in _SEND_AGAIN.items():
                if app != item.get("channel"):
                    o[key] = ("message", "channel", app, desc)
        o["send it to another person too"] = (
            "message", "also", None,
            "Send it to Gal too, and to my son as well. תשלחי את זה גם לגל. ابعتيه لدانا "
            "كمان. Отправь это и Гале тоже.")
        if not own:
            o["send it to another person instead"] = (
                "message", "to", None,
                "It was for someone else: no, I meant Gal, send it to Gal instead. לא, "
                "התכוונתי לגל. قصدي لدانا. Я имела в виду Галю.")
        o["say what was sent"] = ("message", "recall", None,
                                  "What did I send, what did you send her, what was in it. "
                                  "מה שלחתי, מה שלחת לה. شو بعتت. Что я отправила?")
        o["send it by an app MicMic cannot use"] = (
            "message", "unsupported", None,
            "By email, on Facebook Messenger, on Viber or Instagram: any app that is not "
            "WhatsApp, Telegram, Signal or a text message. במייל, בפייסבוק. بالإيميل. "
            "по почте.")
    return o


# "Send it again on ...", one per app MicMic sends by or opens.
_SEND_AGAIN = {
    "whatsapp": ("send it again on WhatsApp",
                 "Oh I meant on WhatsApp, send it on WhatsApp. התכוונתי בוואטסאפ, תשלחי את "
                 "זה בוואטסאפ. ابعتيه عالواتساب. Отправь в WhatsApp."),
    "imessage": ("send it again as a text message",
                 "By text instead, as an ordinary text message, by SMS or iMessage. בהודעה "
                 "רגילה, בסמס. برسالة نصية. По смс."),
    "telegram": ("send it again on Telegram",
                 "On Telegram. בטלגרם. على تيليجرام. В Телеграм."),
    "signal": ("send it again on Signal", "On Signal. בסיגנל. على سيجنال. В Signal."),
}


def _asked_about(item: dict, utterance: str) -> bool:
    """Each kind has its own pre-check: a message its words, everything else a modifier."""
    if item["kind"] == "message":
        own = bool(item.get("own_words"))
        return (says_message_change(utterance, own_words=own)
                or (bool(item.get("counting")) and not own and corrects_message(utterance)))
    return says_modifier(utterance)


def _length(item: dict) -> int | None:
    """An event's length in minutes, or None for a whole-day one."""
    try:
        (sh, sm), (eh, em) = ((int(x) for x in item[k].split(":")) for k in ("start", "end"))
        return (eh * 60 + em) - (sh * 60 + sm)
    except (KeyError, ValueError, AttributeError):
        return None


def _describe(item: dict) -> dict:
    """What Jev is told about one thing in play. Never the screen itself."""
    kind = item["kind"]
    if kind == "timer":
        left = max(0, round((item.get("due", 0) - time.time()) / 60))
        return {"what": "set a spoken reminder", "reminder_about": item.get("text", ""),
                "goes_off_at": time.strftime("%H:%M", time.localtime(item.get("due", 0))),
                "minutes_from_now": left}
    if kind == "event":
        d = {"what": "added an event to her calendar", "title": item.get("title", ""),
             "date": item.get("date", "")}
        try:
            d["day"] = _dt.date.fromisoformat(item.get("date", "")).strftime("%A")
        except ValueError:
            pass
        if item.get("all_day"):
            d["time"] = "the whole day"
        else:
            d["from"], d["to"] = item.get("start", ""), item.get("end", "")
        return d
    if kind == "player":
        return {"what": "put something on to play", "title": item.get("title", ""),
                "playing_in": {"apple_music": "Apple Music", "spotify": "Spotify",
                               "youtube": "YouTube, in the browser"}.get(
                                   item.get("player"), str(item.get("player", "")).replace("_", " ").title())}
    if kind == "answer":
        return {"what": {"describe": "told her what is on her screen",
                         "summarize": "summarized what is on her screen",
                         "translate": "translated what is on her screen"}.get(
                             item.get("task"), "answered her"),
                "it_said": (item.get("text") or "")[:400]}
    if kind == "message":
        return {"what": "sent a message for her", "to": item.get("to") or "",
                "by": _APP_LABEL.get(item.get("channel"), "text message"),
                "it_was": _SENT_WHAT.get(item.get("from_screen") or "")
                or (item.get("text") or "")[:400]}
    return {"what": kind}


_APP_LABEL = {"whatsapp": "WhatsApp", "imessage": "text message", "telegram": "Telegram",
              "signal": "Signal"}
# What Jev is told a message from her screen was. Never the screen itself.
_SENT_WHAT = {"send_link": "the link to the web page she had open",
              "send_screen": "text she had selected on her screen",
              "send_screenshot": "a screenshot of her screen"}


QUESTION = "follow_up"


def question(items: list[dict], utterance: str, today: _dt.date | None = None) -> dict | None:
    """The question that rides in understand(), or None when nothing is to be asked:
    nothing recent in play, or no modifier word in her sentence (for a message just
    sent, none of its own words)."""
    items = [it for it in items if _asked_about(it, utterance)]
    if not items:
        return None
    today = today or _dt.date.today()
    options: dict[str, tuple] = {}
    for it in items:
        for k, v in _options(it, today).items():
            options.setdefault(k, v)
    # "none" first: a replay that never saw this question defaults to the first option.
    crit = {"none": "She is not changing it: a new, separate request or question, "
                    "something new to play, watch, send, find or be reminded of, a "
                    "different song, film, singer or person to play, or anything that "
                    "leaves what was just done as it is. Play Bad Bunny instead is a new "
                    "request. What is the weather is a new request."}
    crit.update({k: v[3] for k, v in options.items()})
    state = [_describe(it) for it in items]
    return {
        "questions": {QUESTION: {"type": "choice",
            "instructions": "MicMic has just done something for her (last_thing_micmic_did), "
                            "or put something on that may be playing. Is what she says now "
                            "a change to THAT, done to that same thing, and which change? "
                            "Times are on the 24-hour clock: read 6pm as 18:00, and 'at 7' "
                            "as the 7 nearest to the time it is at now. Or is it none of "
                            "these: a new request?",
            "criteria": crit}},
        "state": state[0] if len(state) == 1 else state,
        "options": options,
        "items": items,
    }


def read(u: dict, fold: dict | None) -> dict | None:
    """The change she asked for, or None: {"kind", "field", "value", "conf", "item"}.

    The field wins on the sum of its options' probabilities, the value on its own."""
    if not fold:
        return None
    a = (u.get("raw") or {}).get(QUESTION)
    if not a:
        return None
    probs = dict(a.get("probabilities") or {})
    if not probs and a.get("choice"):
        probs = {a["choice"]: a.get("confidence", 0.0)}
    groups: dict[tuple, float] = {}
    for k, p in probs.items():
        spec = fold["options"].get(k)
        if spec:
            groups[spec[:2]] = groups.get(spec[:2], 0.0) + float(p or 0.0)
    if not groups:
        return None
    field, total = max(groups.items(), key=lambda kv: kv[1])
    if total < FOLLOW_GATE or total <= float(probs.get("none", 0.0) or 0.0):
        return None
    key = max((k for k in probs if fold["options"].get(k, ())[:2] == field),
              key=lambda k: probs[k])
    kind, fld, value, _ = fold["options"][key]
    item = next((it for it in fold["items"] if it["kind"] == kind), None)
    return {"kind": kind, "field": fld, "value": value, "conf": round(total, 2),
            "option": key, "item": item}


# ---------------------------------------------------------------- "what should it say?"
ANSWER = "answer_is"
WORDS_SPAN = "message_words"
# The pointing answer (the screen, the same again, only an app) must reach this, or her
# answer is read as the words of the message, as it always was.
ANSWER_GATE = 0.5


def answer_question(utterance: str, last_sent: dict | None = None) -> dict | None:
    """What rides in router._resume's question when her answer to "what should it say?"
    may point at the content instead of being it; None otherwise, and then that
    question is exactly what it was. `last_sent`: the message just sent, if any, which
    is what "the same" means."""
    if not points_at_content(utterance):
        return None
    # "words" first: a replay that never saw this question defaults to the first option,
    # which is how every answer was read before.
    crit = {"words": "Her answer is the words of the message, or tells the machine what "
                     "to say: I am running late. Happy birthday. Tell her I am coming (the "
                     "words are: I am coming). תגידי לה שאני בדרך. قولي لها إني جاية. "
                     "Скажи, что я еду.",
            "screen": "She means something on her computer screen instead of saying "
                      "words: this page, the link, a link to this page, send the page, "
                      "this, what I selected, the screenshot. את הדף הזה, את הקישור. هاي "
                      "الصفحة، الرابط. Эту страницу, ссылку."}
    if last_sent:
        crit["same"] = ("The same thing that was just sent (message_just_sent) again: the "
                        "same, the same link, what you sent before. אותו דבר. نفس الشي. "
                        "То же самое.")
    crit["app_only"] = ("She only says which app to send it by, with no words for the "
                        "message: on WhatsApp, by text. בוואטסאפ. عالواتساب. В WhatsApp.")
    qs = {ANSWER: {"type": "choice",
                   "instructions": "The machine asked her what the message should say. "
                                   "Is her answer the words of the message, or does it "
                                   "point at something else to send, or name only the app?",
                   "criteria": crit}}
    cands = span_candidates(utterance) if reports_speech(utterance) else []
    if cands:
        qs[WORDS_SPAN], qs[WORDS_SPAN + "_exists"] = _span_questions(
            cands, "Which part of her answer is the message itself, the words the other "
                   "person should read? Not the instruction to tell or send it, not the "
                   "person, and not the app (WhatsApp, a text).")
    state = {"message_just_sent": _describe({"kind": "message", **last_sent})} if last_sent else {}
    return {"questions": qs, "state": state}


def read_answer(a: dict, fold: dict | None) -> dict | None:
    """{"is": "words" | "screen" | "same" | "app_only", "conf", "words": span or None}."""
    if not fold or ANSWER not in a:
        return None
    pick = a[ANSWER].get("choice")
    conf = float((a[ANSWER].get("probabilities") or {}).get(pick)
                 or a[ANSWER].get("confidence") or 0.0)
    if pick not in ("screen", "same", "app_only") or conf < ANSWER_GATE:
        pick = "words"
    words = _span_answer(a, WORDS_SPAN)[0] if WORDS_SPAN in a else None
    return {"is": pick, "conf": round(conf, 2), "words": words}


def as_said(span: str | None, utterance: str) -> str | None:
    """A span is made of her words with the punctuation taken out ("I m running late");
    this is that stretch of what she said, as she said it ("I'm running late")."""
    if not span:
        return None
    rx = r"[\W_]*".join(re.escape(w) for w in span.split())
    m = re.search(rx, utterance or "", re.IGNORECASE)
    return m.group(0).strip() if m else span


# ---------------------------------------------------------------- timers
def minutes_until(hhmm: str, now: float | None = None) -> float:
    """Minutes from now to the next time the clock reads hh:mm (today, else tomorrow)."""
    now = time.time() if now is None else now
    h, m = (int(x) for x in hhmm.split(":"))
    t = _dt.datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0)
    if t.timestamp() <= now + 30:
        t += _dt.timedelta(days=1)
    return (t.timestamp() - now) / 60


def timer_minutes(item: dict, field: str, value, now: float | None = None) -> float | None:
    """How many minutes from now the moved reminder goes off, or None."""
    now = time.time() if now is None else now
    if field == "in":
        return float(value)
    if field == "at":
        return minutes_until(value, now)
    if field in ("later", "earlier"):
        left = (item.get("due", now) - now) / 60
        return max(1.0, left + float(value))
    return None


# ---------------------------------------------------------------- events
def event_change(item: dict, field: str, value) -> dict | None:
    """The event as it would be after the change: {"date", "start", "end", "all_day"}.
    None when nothing would change."""
    ev = {k: item.get(k) for k in ("date", "start", "end", "all_day")}
    ev["all_day"] = bool(ev.get("all_day"))

    def mins(t: str) -> int:
        h, m = (int(x) for x in t.split(":"))
        return h * 60 + m

    def hhmm(n: int) -> str:
        n = max(0, min(n, 23 * 60 + 59))
        return f"{n // 60:02d}:{n % 60:02d}"
    start = ev.get("start") or "09:00"
    end = ev.get("end") or hhmm(mins(start) + 60)
    length = max(15, mins(end) - mins(start)) if not ev["all_day"] else 60
    if field == "day":
        ev["date"] = value
    elif field == "at":
        ev.update(start=value, end=hhmm(mins(value) + length), all_day=False)
    elif field in ("later", "earlier"):
        if ev["all_day"]:
            return None
        ev.update(start=hhmm(mins(start) + int(value)), end=hhmm(mins(end) + int(value)))
    elif field == "length":
        ev.update(start=start, end=hhmm(mins(start) + int(value)), all_day=False)
    elif field == "all_day":
        ev.update(start="", end="", all_day=True)
    else:
        return None
    before = {k: item.get(k) for k in ("date", "start", "end")}
    before["all_day"] = bool(item.get("all_day"))
    return None if ev == before else ev


# ---------------------------------------------------------------- what she hears
LINES = {
    "timer_in": {"hebrew": "בסדר, אזכיר לך בעוד {n} דקות.",
                 "arabic": "ماشي، بذكّرك بعد {n} دقيقة.",
                 "russian": "Хорошо, напомню через {n} мин.",
                 "english": "Alright, I will remind you in {n} minutes instead."},
    "timer_at": {"hebrew": "בסדר, אזכיר לך בשעה {t}.",
                 "arabic": "ماشي، بذكّرك الساعة {t}.",
                 "russian": "Хорошо, напомню в {t}.",
                 "english": "Alright, I will remind you at {t} instead."},
    "timer_cancelled": {"hebrew": "ביטלתי את התזכורת.", "arabic": "لغيت التذكير.",
                        "russian": "Отменила напоминание.",
                        "english": "I cancelled the reminder."},
    "timer_gone": {"hebrew": "התזכורת הזאת כבר צלצלה.", "arabic": "هالتذكير رنّ من قبل.",
                   "russian": "Это напоминание уже сработало.",
                   "english": "That reminder has already gone off."},
    "timer_recall": {"hebrew": "התזכורת היא על {text}, בעוד {n} דקות.",
                     "arabic": "التذكير عن {text}، بعد {n} دقيقة.",
                     "russian": "Напоминание: {text}, через {n} мин.",
                     "english": "The reminder is about {text}, in {n} minutes."},
    "timer_recall_gone": {"hebrew": "זה היה על {text}, והיא כבר צלצלה.",
                          "arabic": "كان عن {text}، ورنّ خلص.",
                          "russian": "Это было про {text}, и оно уже сработало.",
                          "english": "It was about {text}, and it has already gone off."},
    "event_ask": {"hebrew": "לשנות את {title} ל{when}, מ-{start} עד {end}?",
                  "arabic": "بغيّر {title} لـ {when}، من {start} لـ {end}؟",
                  "russian": "Изменить «{title}»: {when}, с {start} до {end}?",
                  "english": "Shall I change {title} to {when}, {start} to {end}?"},
    "event_ask_allday": {"hebrew": "לשנות את {title} ל{when}, ליום שלם?",
                         "arabic": "بغيّر {title} لـ {when}، ليوم كامل؟",
                         "russian": "Изменить «{title}»: {when}, на весь день?",
                         "english": "Shall I change {title} to {when}, for the whole day?"},
    "event_done": {"hebrew": "שיניתי. {title} עכשיו {when}, מ-{start} עד {end}.",
                   "arabic": "غيّرتها. {title} صارت {when}، من {start} لـ {end}.",
                   "russian": "Готово. «{title}» теперь {when}, с {start} до {end}.",
                   "english": "Done. {title} is now {when}, {start} to {end}."},
    "event_done_allday": {"hebrew": "שיניתי. {title} עכשיו {when}, ליום שלם.",
                          "arabic": "غيّرتها. {title} صارت {when}، ليوم كامل.",
                          "russian": "Готово. «{title}» теперь {when}, на весь день.",
                          "english": "Done. {title} is now {when}, for the whole day."},
    "event_kept": {"hebrew": "בסדר, השארתי את זה כמו שהיה.",
                   "arabic": "ماشي، تركتها متل ما كانت.",
                   "russian": "Хорошо, оставила как было.",
                   "english": "Alright, I left it as it was."},
    "event_same": {"hebrew": "זה כבר ככה ביומן.", "arabic": "هيك هي بالرزنامة من قبل.",
                   "russian": "В календаре уже так.",
                   "english": "That is already how it is in your calendar."},
    "event_missing": {"hebrew": "לא מצאתי את האירוע הזה ביומן, אז לא שיניתי כלום.",
                      "arabic": "ما لقيت هالموعد بالرزنامة، فما غيّرت شي.",
                      "russian": "Я не нашла это событие в календаре, поэтому ничего не меняла.",
                      "english": "I could not find that event in your calendar, so I changed nothing."},
    "event_failed": {"hebrew": "לא הצלחתי לשנות את זה ביומן.",
                     "arabic": "ما قدرت غيّرها بالرزنامة.",
                     "russian": "Не получилось изменить это в календаре.",
                     "english": "I could not change that in your calendar."},
    "event_recall": {"hebrew": "{title}, {when} בשעה {start}.",
                     "arabic": "{title}، {when} الساعة {start}.",
                     "russian": "«{title}», {when} в {start}.",
                     "english": "{title}, {when} at {start}."},
    "event_recall_allday": {"hebrew": "{title}, {when}, יום שלם.",
                            "arabic": "{title}، {when}، يوم كامل.",
                            "russian": "«{title}», {when}, весь день.",
                            "english": "{title}, {when}, for the whole day."},
    "event_remove": {"hebrew": "אני לא מוחקת אירועים מהיומן. {title} עדיין שם, ואפשר להסיר אותו באפליקציית לוח השנה.",
                     "arabic": "أنا ما بمحي مواعيد من الرزنامة. {title} لسا موجودة، وفيك تشيلها من تطبيق الرزنامة.",
                     "russian": "Я не удаляю события из календаря. «{title}» по-прежнему там, его можно убрать в Календаре.",
                     "english": "I do not delete calendar events. {title} is still there, and you can remove it in Calendar."},
    "event_no_invite": {"hebrew": "אני עוד לא יכולה לשלוח הזמנות ליומן.",
                        "arabic": "لسا ما بقدر أبعت دعوات للرزنامة.",
                        "russian": "Я пока не умею отправлять приглашения в календаре.",
                        "english": "I cannot send calendar invitations yet."},
    "event_invite_body": {"hebrew": "{title}, {when} בשעה {start}.",
                          "arabic": "{title}، {when} الساعة {start}.",
                          "russian": "«{title}», {when} в {start}.",
                          "english": "{title}, {when} at {start}."},
    "not_playing": {"hebrew": "כלום ממה ששמתי לא מתנגן עכשיו.",
                    "arabic": "ما في شي من اللي شغّلته عم يشتغل هلّق.",
                    "russian": "Сейчас ничего из того, что я включила, не играет.",
                    "english": "Nothing I put on is playing right now."},
    "tab_gone": {"hebrew": "זה כבר לא פתוח בדפדפן.", "arabic": "هاد ما عاد مفتوح بالمتصفح.",
                 "russian": "Это уже не открыто в браузере.",
                 "english": "That is not open in the browser any more."},
    "cannot_there": {"hebrew": "את זה אני לא יכולה לעשות ב{app} מכאן.",
                     "arabic": "هاد ما بقدر أعمله بـ{app} من هون.",
                     "russian": "Это я не могу сделать в {app} отсюда.",
                     "english": "I cannot do that in {app} from here."},
    "already_paused": {"hebrew": "זה כבר בהשהיה.", "arabic": "هو واقف من قبل.",
                       "russian": "Уже на паузе.", "english": "It is already paused."},
    "player_recall": {"hebrew": "זה {title}.", "arabic": "هاد {title}.",
                      "russian": "Это {title}.", "english": "This is {title}."},
    "no_rewrite": {"hebrew": "לא הצלחתי לנסח את זה מחדש כרגע.",
                   "arabic": "ما قدرت عيد صياغتها هلّق.",
                   "russian": "Сейчас не получилось это переформулировать.",
                   "english": "I could not put that another way right now."},
    # A message sent again: to another app or person. Never a word about the first one
    # being taken back; it went, and nothing here can recall it.
    "msg_again": {"hebrew": "שולחת {at_he} שוב את {what}, {on_app}. תגיד«י|» לי לא ואני עוצרת.",
                  "arabic": "ببعت {at_ar} نفس {what} {on_app}. احكيلي«|» لأ وبوقّف.",
                  "russian": "Отправляю {who} {what} ещё раз, {on_app}. Скажите «нет», и я не отправлю.",
                  "english": "Sending the same {what} to {who} {on_app}. Say no and I will stop."},
    # The message counting down is REPLACED by her correction, so it never went: not
    # "again", and the one it replaced is named, so she knows it will not go.
    "msg_instead": {"hebrew": "אז שולחת {at_he} את {what} {on_app}. תגיד«י|» לי לא ואני עוצרת.",
                    "arabic": "إذن ببعت {at_ar} {what} {on_app}. احكيلي«|» لأ وبوقّف.",
                    "russian": "Тогда отправляю {who} {what}, {on_app}. Скажите «нет», и я не отправлю.",
                    "english": "Sending the {what} to {who} {on_app} instead. Say no and I will stop."},
    "stopped_to": {"hebrew": "עצרתי את מה שהלך {at_he}.",
                   "arabic": "وقّفت اللي كان رايح {at_ar}.",
                   "russian": "Я остановила отправку {who}.",
                   "english": "I stopped the one to {who}."},
    "stopped_on": {"hebrew": "זה לא יוצא {on_app}.",
                   "arabic": "مش رح تروح {on_app}.",
                   "russian": "Это не уйдёт {on_app}.",
                   "english": "It will not go {on_app}."},
    # The last safety net (router._twice): the same thing just went to someone else.
    "msg_twice": {"hebrew": "בדיוק שלחתי את אותו הדבר {at_he_other}, אז עצרתי את זה {at_he}. "
                            "לשלוח גם {at_he}? תגיד«י|» כן ואני שולחת.",
                  "arabic": "هلّق بعتت نفس الشي {at_ar_other}، فوقّفت اللي {at_ar}. "
                            "أبعته {at_ar} كمان؟ قولي«|» نعم وببعته.",
                  "russian": "То же самое только что ушло {other}, поэтому я придержала "
                             "отправку {who}. Отправить {who} тоже? Скажите «да», и я отправлю.",
                  "english": "The same thing just went to {other}, so I held the one to {who}. "
                             "Should it go to {who} too? Say yes and I will send it."},
    # Two people in her book sound like the name she said: which one, before any countdown.
    "which_one": {"hebrew": "{a} או {b}?", "arabic": "{a} ولا {b}؟",
                  "russian": "{a} или {b}?", "english": "{a} or {b}?"},
    "same_link": {"hebrew": "הקישור", "arabic": "الرابط", "russian": "ту же ссылку",
                  "english": "link"},
    "same_selection": {"hebrew": "הטקסט שסימנת", "arabic": "النص المحدد",
                       "russian": "тот же выделенный текст", "english": "text you selected"},
    "same_shot": {"hebrew": "צילום המסך", "arabic": "صورة الشاشة", "russian": "тот же скриншот",
                  "english": "screenshot"},
    "same_words": {"hebrew": "ההודעה", "arabic": "الرسالة", "russian": "то же сообщение",
                   "english": "message"},
    "msg_recall": {"hebrew": "ההודעה האחרונה יצאה {at_he} {on_app}: {what}.",
                   "arabic": "آخر رسالة راحت {at_ar} {on_app}: {what}.",
                   "russian": "Последнее сообщение ушло {who} {on_app}: {what}.",
                   "english": "The last message went to {who} {on_app}: {what}."},
    "msg_recall_going": {"hebrew": "זה עומד לצאת {at_he} {on_app}: {what}. תגיד«י|» לי לא ואני עוצרת.",
                         "arabic": "رح تروح {at_ar} {on_app}: {what}. احكيلي«|» لأ وبوقّف.",
                         "russian": "Сейчас уйдёт {who} {on_app}: {what}. Скажите «нет», и я не отправлю.",
                         "english": "It is about to go to {who} {on_app}: {what}. Say no and I will stop."},
    "sent_link": {"hebrew": "הקישור לדף שהיה פתוח", "arabic": "رابط الصفحة اللي كانت مفتوحة",
                  "russian": "ссылка на открытую страницу",
                  "english": "the link to the page you had open"},
    "sent_selection": {"hebrew": "הטקסט שסימנת", "arabic": "النص اللي حدّدته",
                       "russian": "выделенный текст", "english": "the text you selected"},
    "sent_shot": {"hebrew": "צילום מסך", "arabic": "صورة شاشة", "russian": "скриншот",
                  "english": "a screenshot"},
    "msg_apps": {"hebrew": "אני יכולה לשלוח הודעות רק בהודעת טקסט או בוואטסאפ, או לפתוח אותן בטלגרם או בסיגנל.",
                 "arabic": "بقدر أبعت رسائل بس برسالة نصية أو على واتساب، أو أفتحها على تيليجرام أو سيجنال.",
                 "russian": "Я могу отправлять сообщения только по SMS или в WhatsApp, либо открыть их в Telegram или Signal.",
                 "english": "I can only send messages as a text or on WhatsApp, or open them in Telegram or Signal."},
    # "What should it say?" answered with only an app.
    "what_on": {"hebrew": "בסדר, {on_app}. מה לכתוב?", "arabic": "ماشي، {on_app}. شو أكتب؟",
                "russian": "Хорошо, {on_app}. Что написать?",
                "english": "Alright, {on_app}. What should it say?"},
}

# Which of the lines above names what a message from her screen was.
SAME_WHAT = {"send_link": "same_link", "send_screen": "same_selection",
             "send_screenshot": "same_shot"}
SENT_WHAT = {"send_link": "sent_link", "send_screen": "sent_selection",
             "send_screenshot": "sent_shot"}


def line(key: str, lang: str, **fmt) -> str:
    t = LINES[key]
    return (t.get(lang) or t["english"]).format(**fmt)


# The rewrite of an answer she already heard: one Gemini call, the writing task.
REWRITE = {
    "shorter": "Say this again much shorter, in one or two short sentences, in {language}. "
               "Only the new wording.",
    "language": "Say this again in {language}, as a sentence said out loud. Only the new "
                "wording.",
}
