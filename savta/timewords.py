"""Time in her own words: how long, what time, which day. Parsed in code, in English,
Hebrew, Arabic and Russian.

The bench (2026-10-01, calendar_reminders 51.1%, the worst category): the minutes came
from a closed list of fourteen answers in brain.py's when_minutes, so "in fifty minutes"
became 45 without a word, "25 minutes", "four minutes" and "two hundred minutes" were
asked "In how long?", "set an alarm for seven am" and "remind me tomorrow at 9" had no
path at all, and "add lunch with Dana on Thursday at 1" had no intent.

How it is decided, within the project's rules (see brain.py):
  * A cheap code pre-check (mentions_time): her words hold a time word (a unit, a clock
    reading, "at 7") or a word for a timer, an alarm, a reminder or her calendar. Only
    then does anything ride in the understand() request; every other request is sent
    byte for byte as before.
  * What rides along (question): one choice, which kind of thing she asks for (a timer,
    a reminder, an alarm, a calendar event, how long is left, cancel one, or none of
    these), and span picks: which words are the amount of time, the clock time, the day,
    and what it is about. Jev only SELECTS words of her sentence. The candidates for the
    three time spans are only the spans this module can read as one, so those choices
    are a handful of options, not the hundreds every span of the sentence would be.
  * Code computes the number from the words Jev picked (parse_duration, parse_clock,
    parse_day) and the moment from now (resolve): "fifty minutes" is 50 because the
    parser says so, never because a list happened to hold 45.
"""
from __future__ import annotations

import datetime as _dt
import re
import time

from .brain import span_candidates, _span_questions, _span_answer
from .jev import choice

QUESTION = "time_request"
SPAN_KEYS = ("duration", "clock", "day", "about")
# The kind wins on the sum of every option but "none": a reminder split with an alarm is
# still clearly something to set.
TIME_GATE = 0.5


def _now() -> float:
    """The clock this module reads. A seam: tests set a fixed moment through it."""
    return time.time()


# ---------------------------------------------------------------- the pre-check
# Words that can only mean she wants something set for later, or a clock reading.
_KIND_WORDS = (
    r"\btimers?\b|\balarms?\b|\bremind|\breminders?\b|\bcalendar\b|\bdiary\b|\bwake me\b|"
    r"\bwake up\b|\bappointment\b|\bschedule\b|"
    r"טיימר|שעון מעורר|מעורר|תזכיר|תזכורת|יומן|לוח השנה|לוח שנה|תעירי|תעיר|להעיר|"
    r"مؤقت|تايمر|منب[ّ]?ه|ذك[ّ]?ر|فك[ّ]?ريني|تذكير|رزنامة|تقويم|صحيني|صح[ّ]?يني|فيقيني|"
    r"таймер|будильник|напомни|напоминани|календар|разбуди")
_UNIT_WORDS = (
    r"\bsec(?:ond)?s?\b|\bmin(?:ute)?s?\b|\bhours?\b|\bhrs?\b|"
    # Not "מה השעה", "كم الساعة" or "который час": asking the time is not setting one.
    r"(?<![\u0590-\u05ff])[ובלכמש]{0,2}(?:דקה|דקות|דקת|שעה|שעות|שעתיים|שנייה|שניות)|"
    r"دقيق|دقايق|دقائق|(?<!ال)ساع(?:ة|ات|تين)|ثانية|ثواني|"
    r"секунд|минут|(?<!который\s)\bчас(?:а|ов)?\b|полчаса|полтора")
_CLOCK_WORDS = (
    r"\d{1,2}[:.]\d{2}|\d\s*(?:am|pm|a\.m\.|p\.m\.)\b|\bo'?clock\b|\bnoon\b|\bmidnight\b|"
    r"\b(?:at|by)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b|"
    r"בשעה|לשעה|ב-?\d|"
    r"(?:^|\s)[בל](?:אחת|שתיים|שלוש|ארבע|חמש|שש|שבע|שמונה|תשע|עשר)(?=\s|$)|חצות|"
    r"عالساعة|الساعة\s*\d|الساعة\s+(?:وحدة|واحدة|تنتين|ثنتين|تلاتة|ثلاثة|اربعة|أربعة|خمسة|"
    r"ستة|سبعة|تمانية|ثمانية|تسعة|عشرة)|"
    r"\bв\s+\d|\bв\s+(?:час|два|три|четыре|пять|шесть|семь|восемь|девять|десять|"
    r"одиннадцать|двенадцать|половине)\b|полдень|полночь")
_PRECHECK = re.compile("|".join((_KIND_WORDS, _UNIT_WORDS, _CLOCK_WORDS)), re.IGNORECASE)
# Words that name the thing to be reminded of or put in the calendar, or an "add" or
# "put": only then is the "what is it about" span (every span of the sentence) asked
# as well. Without it the branch that needs it asks Jev on its own (one more call).
_ADD_WORDS = (r"^\W*(?:please\s+)?(?:add|put|schedule|book|set up|make)\b|"
              r"תוסיפי|תוסיף|תכניסי|תכניס|תרשמי|תרשום|תקבעי|תקבע|"
              r"ضيفي|ضيف|حطي|حط|سجلي|سجل|"
              r"добавь|добавьте|запиши|запишите|поставь")
_ABOUT_WORDS = re.compile(_KIND_WORDS + "|" + _ADD_WORDS, re.IGNORECASE)


# A message or a call whose words mention a time ("text Dana I'll be there at 8"): the
# time is part of what she says to someone, and the request goes out exactly as before.
# Live (fix/time, run B): "text david that dinner is at eight" (two Davids) went 2/3 ->
# 0/3 with the time question riding beside the contact choice. A word for a reminder,
# timer, alarm or calendar still opens it ("remind me to text Tom in ten minutes").
_SEND_START = re.compile(
    r"^\W*(?:(?:uh|um|oh|ok|okay|please|so|and|hey)\W+)*"
    r"(?:text|send|message|tell|write|whatsapp|call|ring|phone|email|ask)\b|"
    r"^\W*(?:ת?שלח|תשלחי|שלחי|תכתבי|תכתוב|כתבי|תגידי|תגיד|תתקשרי|תתקשר|תודיעי|תודיע|"
    r"תשאלי|תשאל)|"
    r"^\W*(?:ابعت|ابعتي|اكتبي|اكتب|احكي|قولي|قول|اتصلي|اتصل|راسلي|خبري)|"
    r"^\W*(?:напиши|напишите|отправь|отправьте|скажи|скажите|позвони|позвоните|передай)",
    re.IGNORECASE)


def mentions_time(utterance: str) -> bool:
    u = utterance or ""
    if not _PRECHECK.search(u):
        return False
    return not (_SEND_START.search(u) and not re.search(_KIND_WORDS, u, re.IGNORECASE))


# ---------------------------------------------------------------- tokens
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _tokens(text: str) -> list[str]:
    t = (text or "").lower().translate(_AR_DIGITS)
    t = t.replace("’", "'").replace("a.m.", "am").replace("p.m.", "pm")
    t = t.replace('אחה"צ', "אחהצ").replace("אחה״צ", "אחהצ")
    t = re.sub(r"(\d)\s*(am|pm)\b", r"\1 \2", t)
    t = re.sub(r"ё", "е", t)
    return re.findall(r"\d{1,2}(?:st|nd|rd|th)(?![^\W\d_])|\d+(?:[:.,]\d+)?|o'clock|[^\W\d_]+", t)


# ---------------------------------------------------------------- numbers
_UNITS = {
    # English
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9,
    # Hebrew (both genders, and the construct forms)
    "אפס": 0, "אחת": 1, "אחד": 1, "שתיים": 2, "שתים": 2, "שניים": 2, "שנים": 2, "שתי": 2,
    "שני": 2, "שלוש": 3, "שלושה": 3, "שלושת": 3, "ארבע": 4, "ארבעה": 4, "ארבעת": 4,
    "חמש": 5, "חמישה": 5, "חמשת": 5, "שש": 6, "שישה": 6, "ששת": 6, "שבע": 7, "שבעה": 7,
    "שבעת": 7, "שמונה": 8, "שמונת": 8, "תשע": 9, "תשעה": 9, "תשעת": 9,
    # Arabic (spoken and standard)
    "صفر": 0, "واحد": 1, "وحدة": 1, "واحدة": 1, "اثنين": 2, "اتنين": 2, "تنين": 2,
    "ثنتين": 2, "تنتين": 2, "اثنان": 2, "ثلاث": 3, "ثلاثة": 3, "تلات": 3, "تلاتة": 3,
    "تلاته": 3, "اربع": 4, "أربع": 4, "اربعة": 4, "أربعة": 4, "اربعه": 4, "خمس": 5,
    "خمسة": 5, "خمسه": 5, "ست": 6, "ستة": 6, "سته": 6, "سبع": 7, "سبعة": 7, "سبعه": 7,
    "ثمان": 8, "ثماني": 8, "ثمانية": 8, "تمن": 8, "تمانية": 8, "تمانيه": 8, "تسع": 9,
    "تسعة": 9, "تسعه": 9,
    # Russian
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "одного": 1, "два": 2, "две": 2, "двух": 2,
    "три": 3, "трех": 3, "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6,
    "шести": 6, "семь": 7, "семи": 7, "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "עשר": 10, "עשרה": 10, "עשרת": 10,
    "عشر": 10, "عشرة": 10, "عشره": 10, "احدعش": 11, "حداعش": 11, "اتنعش": 12, "طنعش": 12,
    "تلتعش": 13, "تلطعش": 13, "اربعتعش": 14, "اربعطعش": 14, "خمستعش": 15, "خمسطعش": 15,
    "ستعش": 16, "سطعش": 16, "سبعتعش": 17, "سبعطعش": 17, "تمنتعش": 18, "تمنطعش": 18,
    "تسعتعش": 19, "تسعطعش": 19,
    "десять": 10, "десяти": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
    "עשרים": 20, "שלושים": 30, "ארבעים": 40, "חמישים": 50, "שישים": 60, "שבעים": 70,
    "שמונים": 80, "תשעים": 90,
    "عشرين": 20, "عشرون": 20, "ثلاثين": 30, "تلاتين": 30, "اربعين": 40, "أربعين": 40,
    "خمسين": 50, "ستين": 60, "سبعين": 70, "ثمانين": 80, "تمانين": 80, "تسعين": 90,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
}
_HUNDREDS = {"מאה": 100, "מאתיים": 200, "مية": 100, "مئة": 100, "مائة": 100, "ميه": 100,
             "ميتين": 200, "مئتين": 200, "مائتين": 200, "сто": 100, "двести": 200,
             "триста": 300}
# "hundred" and "מאות" multiply what came before them.
_HUNDRED_MUL = {"hundred", "מאות"}
_ALL_NUM = set(_UNITS) | set(_TEENS) | set(_TENS) | set(_HUNDREDS) | _HUNDRED_MUL

# Hebrew and Arabic write a preposition or "and" onto the word. Each prefix is tried off
# a word this module does not know as it stands.
_HE_PREFIX = ("וב", "ול", "וה", "שב", "של", "כש", "ו", "ב", "ל", "ה", "כ", "מ", "ש")
_AR_PREFIX = ("وبال", "بال", "وال", "عال", "لل", "ال", "و", "ب", "ل", "ع")


def _split_prefix(tok: str, known) -> tuple[str, str]:
    """(prefix, word) for a word that is only known with a prefix taken off."""
    if tok in known:
        return "", tok
    pre = _HE_PREFIX if re.match(r"[֐-׿]", tok) else (
        _AR_PREFIX if re.match(r"[؀-ۿ]", tok) else ())
    for p in pre:
        if tok.startswith(p) and tok[len(p):] in known:
            return p, tok[len(p):]
    return "", tok


def _is_and(prefix: str) -> bool:
    return prefix.startswith("ו") or prefix.startswith("و")


def _digit(tok: str) -> float | None:
    if re.fullmatch(r"\d+(?:[.,]\d+)?", tok):
        return float(tok.replace(",", "."))
    return None


def _number_at(toks: list[str], i: int, clock: bool = False) -> tuple[float, int, list[str]] | None:
    """The number that starts at toks[i] in words or digits: (value, next index, the
    prefixes on its words). None when no number starts there. `clock`: units followed by
    "and" and tens are an hour and its minutes ("שבע ועשרים" 7:20), never one number."""
    d = _digit(toks[i])
    if d is not None:
        return d, i + 1, []
    total, cur, j, seen, prefixes = 0, 0, i, False, []
    last = None                      # the kind of the last word read
    while j < len(toks):
        pre, w = _split_prefix(toks[j], _ALL_NUM | {"and"})
        if seen and w == "and" and last == "hundred" and j + 1 < len(toks):
            j += 1
            continue
        if w not in _ALL_NUM:
            break
        if seen and pre and not _is_and(pre):
            break                    # "לשלוש" after a number starts something new
        if not seen and pre and _is_and(pre) and j == i:
            pass                     # "וחמש" at the start: the caller's "and"
        if w in _HUNDRED_MUL:
            if last not in ("unit", None) and seen:
                break
            cur = (cur or 1) * 100
            last = "hundred"
        elif w in _HUNDREDS:
            if seen and last not in ("unit",):
                break
            cur += _HUNDREDS[w]
            last = "hundred"
        elif w in _TENS:
            if seen and last not in ("hundred", "unit"):
                break
            if last == "unit" and cur % 10 and (clock or not _is_and(pre)
                                                or re.match(r"[\u0590-\u05ff]", w)):
                break                # "five twenty" is two numbers; Hebrew 27 is עשרים ושבע
            cur += _TENS[w]
            last = "tens"
        elif w in _TEENS:
            if (w in ("עשרה", "עשר", "عشر", "عشرة", "عشره") and last == "unit" and cur % 10
                    and not _is_and(pre) and not clock):
                cur += 10            # חמש עשרה, خمسة عشر
                last = "teen"
            elif seen and last not in ("hundred",):
                break
            else:
                cur += _TEENS[w]
                last = "teen"
        else:
            if seen and last not in ("hundred", "tens"):
                break
            if last == "tens" and not _is_and(pre) and re.match(r"[֐-ۿ]", w):
                break                # Hebrew and Arabic join tens and units with "and"
            cur += _UNITS[w]
            last = "unit"
        prefixes.append(pre)
        seen = True
        j += 1
    if not seen:
        return None
    return float(total + cur), j, prefixes


# ---------------------------------------------------------------- durations
_SEC = {"second", "seconds", "sec", "secs", "שנייה", "שניה", "שניות", "ثانية", "ثانيه",
        "ثواني", "ثوان", "секунда", "секунду", "секунды", "секунд"}
_MIN = {"minute", "minutes", "min", "mins", "דקה", "דקות", "דקת", "دقيقة", "دقيقه", "دقايق",
        "دقائق", "минута", "минуту", "минуты", "минут", "минутку"}
_HOUR = {"hour", "hours", "hr", "hrs", "שעה", "שעות", "שעת", "ساعة", "ساعه", "ساعات",
         "час", "часа", "часов", "часик"}
_DAY_U = {"day", "days", "יום", "ימים", "يوم", "أيام", "ايام", "день", "дня", "дней"}
# A unit with no count before it is one of it only in the singular: "in a minute", "an
# hour", "שעה", "час". A bare plural ("minutes", "דקות") is no amount at all.
_SINGULAR = {"second", "sec", "minute", "min", "hour", "hr", "day", "שנייה", "שניה", "דקה",
             "שעה", "יום", "ثانية", "ثانيه", "دقيقة", "دقيقه", "ساعة", "ساعه", "يوم",
             "секунду", "секунда", "минуту", "минута", "минутку", "час", "часик", "день"}
# One word that is a count and a unit at once.
_PAIRS = {"שעתיים": (2, 60), "דקותיים": (2, 1), "יומיים": (2, 1440), "ساعتين": (2, 60),
          "دقيقتين": (2, 1), "يومين": (2, 1440), "полчаса": (0.5, 60), "сутки": (1, 1440)}
_HALF = {"half", "חצי", "וחצי", "نص", "نصف", "ونص", "ونصف", "половина", "половиной"}
_QUARTER = {"quarter", "quarters", "רבע", "רבעי", "ורבע", "ربع", "وربع", "четверть",
            "четверти"}
_THIRD = {"תלת", "ثلث", "تلت", "треть"}
_MINUS = {"الا", "إلا", "غير"}
_ONEHALF = {"полтора", "полторы"}
_DUR_FILL = {"in", "for", "after", "within", "about", "around", "approximately", "another",
             "more", "of", "from", "now", "later", "the", "next", "just",
             "בעוד", "עוד", "תוך", "אחרי", "בערך", "מעכשיו", "מהיום", "כ",
             "بعد", "خلال", "كمان", "تقريبا", "تقريباً", "من", "هلق", "هلأ", "الآن", "الان",
             "через", "на", "за", "спустя", "еще", "ещё", "примерно", "около", "минимум"}
_AND = {"and", "ו", "و", "и", "с"}


def _unit_of(w: str) -> float | None:
    if w in _SEC:
        return 1 / 60
    if w in _MIN:
        return 1.0
    if w in _HOUR:
        return 60.0
    if w in _DAY_U:
        return 1440.0
    return None


_DUR_KNOWN = (_SEC | _MIN | _HOUR | _DAY_U | set(_PAIRS) | _HALF | _QUARTER | _THIRD | _MINUS
              | _ONEHALF | _DUR_FILL | _AND | _ALL_NUM | {"a", "an", "couple", "пару", "пара"})


def _duration_items(text: str) -> list[tuple] | None:
    toks, out, i = _tokens(text), [], 0
    while i < len(toks):
        n = _number_at(toks, i)
        if n is not None:
            val, j, pres = n
            if pres and _is_and(pres[0]):
                out.append(("and",))
            out.append(("num", val))
            i = j
            continue
        pre, w = _split_prefix(toks[i], _DUR_KNOWN)
        if pre and _is_and(pre):
            out.append(("and",))
        if w in _PAIRS:
            c, u = _PAIRS[w]
            out.append(("num", c))
            out.append(("unit", u, True))
        elif _unit_of(w) is not None:
            out.append(("unit", _unit_of(w), w in _SINGULAR))
        elif w in _HALF:
            if w.startswith(("ו", "و")):
                out.append(("and",))
            out.append(("half",))
        elif w in _QUARTER:
            if w.startswith(("ו", "و")):
                out.append(("and",))
            out.append(("quarter",))
        elif w in _THIRD:
            out.append(("third",))
        elif w in _MINUS:
            out.append(("minus",))
        elif w in _ONEHALF:
            out.append(("num", 1.5))
        elif w in ("a", "an"):
            out.append(("a",))
        elif w in ("couple", "пару", "пара"):
            out.append(("num", 2))
        elif w in _AND:
            out.append(("and",))
        elif w in _DUR_FILL:
            pass
        else:
            return None
        i += 1
    return out


def parse_duration(text: str) -> float | None:
    """Minutes, when the WHOLE of `text` is an amount of time ("in fifty minutes", "an
    hour and a half", "שעה וחצי", "ساعة ونص", "полтора часа", "90 seconds"); else None.
    Words around it are not skipped: Jev picks the span, this only reads it."""
    items = _duration_items(text)
    if not items:
        return None
    total, units, num, frac, last_unit, prev = 0.0, 0, None, None, None, None
    minus = False
    for k, it in enumerate(items):
        kind = it[0]
        nxt = items[k + 1][0] if k + 1 < len(items) else None
        if kind == "a":
            if nxt in ("unit",):
                num = 1.0 if num is None else None
            continue                              # "a half", "a quarter": no count
        if kind == "num":
            if num is not None:
                return None
            num = float(it[1])
        elif kind in ("half", "quarter", "third"):
            part = {"half": 0.5, "quarter": 0.25, "third": 1 / 3}[kind]
            if kind == "quarter" and num == 3 and prev == "num":
                frac, num = 0.75, None            # three quarters of an hour
            elif num is not None and prev == "and":
                num += part                       # two and a half hours
            elif last_unit is not None and num is None and (prev in ("and", "unit")):
                total += (-part if minus else part) * last_unit   # an hour and a half
                minus = False
            elif minus and last_unit is not None:
                total -= part * last_unit
                minus = False
            elif num is None or num == 1:
                frac, num = part, None            # half an hour, a quarter of an hour
            else:
                return None
        elif kind == "unit":
            if num is None and frac is None and not it[2]:
                return None                       # "minutes" on its own
            count = num if num is not None else (frac if frac is not None else 1.0)
            if num is not None and frac is not None:
                count = num * frac
            total += count * it[1]
            units += 1
            last_unit, num, frac = it[1], None, None
        elif kind == "minus":
            if last_unit is None:
                return None
            minus = True
        elif kind == "and":
            pass
        prev = kind
    if num is not None and last_unit is not None and prev == "num":
        # "an hour and 20": the second number is in the next smaller unit.
        total += num * (1.0 if last_unit >= 60 else 1 / 60)
        num = None
    if num is not None or frac is not None or minus or not units or total <= 0:
        return None
    return round(total, 6)


# ---------------------------------------------------------------- clock times
_AM = {"am", "morning", "בבוקר", "בוקר", "الصبح", "صباحا", "صباحاً", "الصباح", "صبحا",
       "утра", "утром"}
_PM = {"pm"}
_AFTERNOON = {"afternoon", "אחהצ", "אחרי הצהריים", "العصر", "عصرا", "дня", "днем", "днём"}
_EVENING = {"evening", "tonight", "בערב", "ערב", "הערב", "المسا", "مساء", "مساءً", "المساء",
            "مسا", "вечера", "вечером"}
_NIGHT = {"night", "בלילה", "לילה", "الليل", "بالليل", "ليلا", "ليلاً", "ночи", "ночью"}
_NOON = {"noon", "midday", "בצהריים", "צהריים", "הצהריים", "الظهر", "ظهرا", "ظهراً", "полдень",
         "полудня"}
_MIDNIGHT = {"midnight", "חצות", "полночь", "полуночи"}
_PART_DEFAULT = {"morning": (9, 0), "afternoon": (15, 0), "evening": (18, 0),
                 "night": (21, 0), "noon": (12, 0), "midnight": (0, 0)}
_CLOCK_FILL = {"at", "by", "around", "about", "for", "until", "till", "the", "in", "this",
               "sharp", "exactly", "on",
               "בשעה", "לשעה", "שעה", "בערך", "ב", "ל",
               "الساعة", "ساعة", "عالساعة", "ع", "على", "حوالي", "تقريبا", "ال",
               "в", "во", "к", "около", "на", "часов", "часа", "час", "ровно"}
_PAST = {"past", "after"}
_TO = {"to", "before", "till", "of"}
_BEFORE = {"без"}                    # без четверти семь: a quarter to seven
_ORD_GEN = {"первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5,
            "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10,
            "одиннадцатого": 11, "двенадцатого": 12}
_HALF_OF = {"половине", "пол"}
_CLOCK_KNOWN = (_AM | _PM | _AFTERNOON | _EVENING | _NIGHT | _NOON | _MIDNIGHT | _CLOCK_FILL
                | _PAST | _TO | _BEFORE | set(_ORD_GEN) | _HALF_OF | _HALF | _QUARTER
                | _MINUS | _ALL_NUM | {"o'clock", "oclock", "and", "minutes", "past"})


def _clock_items(text: str) -> list[tuple] | None:
    raw = (text or "").lower()
    toks, out, i = _tokens(raw), [], 0
    while i < len(toks):
        tok = toks[i]
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", tok)
        if m:
            out.append(("hhmm", int(m.group(1)), int(m.group(2)), tok.startswith("0")
                        or int(m.group(1)) > 12))
            i += 1
            continue
        n = _number_at(toks, i, clock=True)
        if n is not None:
            val, j, pres = n
            if pres and _is_and(pres[0]):
                out.append(("and",))
            elif pres and pres[0] in ("ל",):
                out.append(("lto",))
            if val != int(val):
                return None
            out.append(("num", int(val)))
            i = j
            continue
        if tok == "אחרי" and i + 1 < len(toks) and toks[i + 1] in ("הצהריים", "צהריים"):
            out.append(("part", "afternoon"))
            i += 2
            continue
        pre, w = _split_prefix(tok, _CLOCK_KNOWN)
        if pre and _is_and(pre):
            out.append(("and",))
        elif pre in ("ל",) and w in _ALL_NUM:
            out.append(("lto",))
        if w in _AM:
            out.append(("part", "morning" if w != "am" else "am"))
        elif w in _PM:
            out.append(("part", "pm"))
        elif w in _AFTERNOON:
            out.append(("part", "afternoon"))
        elif w in _EVENING:
            out.append(("part", "evening"))
        elif w in _NIGHT:
            out.append(("part", "night"))
        elif w in _NOON:
            out.append(("part", "noon"))
        elif w in _MIDNIGHT:
            out.append(("part", "midnight"))
        elif w in ("o'clock", "oclock"):
            out.append(("oclock",))
        elif w in _HALF or w in _HALF_OF:
            if w.startswith(("ו", "و")):
                out.append(("and",))
            out.append(("half",))
        elif w in _QUARTER:
            if w.startswith(("ו", "و")):
                out.append(("and",))
            out.append(("quarter",))
        elif w in _PAST:
            out.append(("past",))
        elif w in _TO:
            out.append(("to",))
        elif w in _BEFORE:
            out.append(("before",))
        elif w in _MINUS:
            out.append(("minus",))
        elif w in _ORD_GEN:
            out.append(("ordgen", _ORD_GEN[w]))
        elif w == "and":
            out.append(("and",))
        elif w == "minutes":
            out.append(("mins",))
        elif w in _CLOCK_FILL:
            pass
        else:
            return None
        i += 1
    return out


def parse_clock(text: str) -> dict | None:
    """A clock reading, when the WHOLE of `text` is one ("at 6:30", "seven am", "noon",
    "half past six", "in the evening", "לשבע בבוקר", "سبعة ونص", "в половине седьмого"):
    {"h", "m", "mer": "am"|"pm"|None, "part": morning|afternoon|evening|night|noon|
    midnight|None, "exact": a 24-hour reading}. None otherwise."""
    items = _clock_items(text)
    if not items:
        return None
    part = mer = None
    core = []
    for it in items:
        if it[0] == "part":
            v = it[1]
            if v in ("am", "pm"):
                mer = v
            else:
                part = v
        elif it[0] != "oclock":
            core.append(it)
    kinds = [c[0] for c in core]
    h = m = None
    exact = False

    def num(k):
        return core[k][1] if k < len(core) and core[k][0] == "num" else None
    if not core:
        if part is None:
            return None
        h, m = _PART_DEFAULT[part]
        return {"h": h, "m": m, "mer": None, "part": part, "exact": True, "default": True}
    if kinds == ["hhmm"]:
        h, m, exact = core[0][1], core[0][2], core[0][3]
    elif kinds == ["num"]:
        h, m = core[0][1], 0
    elif kinds in (["num", "num"], ["num", "and", "num"]):
        h, m = core[0][1], core[-1][1]
        if m >= 60 or (kinds == ["num", "num"] and m < 10 and _tokens(text).count("oh") == 0
                       and m != 0):
            return None
    elif kinds in (["num", "num", "num"],) and core[1][1] == 0:
        h, m = core[0][1], core[2][1]               # seven oh five
    elif kinds in (["num", "and", "half"],):
        h, m = core[0][1], 30
    elif kinds in (["num", "and", "quarter"],):
        h, m = core[0][1], 15
    elif kinds in (["num", "minus", "quarter"],):
        h, m = core[0][1] - 1, 45
    elif kinds in (["half", "past", "num"],):
        h, m = core[2][1], 30
    elif kinds in (["quarter", "past", "num"],):
        h, m = core[2][1], 15
    elif kinds in (["quarter", "to", "num"], ["quarter", "lto", "num"]):
        h, m = core[2][1] - 1, 45
    elif kinds in (["num", "past", "num"], ["num", "mins", "past", "num"]):
        h, m = core[-1][1], core[0][1]
    elif kinds in (["num", "to", "num"], ["num", "mins", "to", "num"], ["num", "lto", "num"]):
        h, m = core[-1][1] - 1, 60 - core[0][1]
    elif kinds in (["before", "quarter", "num"],):
        h, m = core[2][1] - 1, 45
    elif kinds in (["before", "num", "num"],):
        h, m = core[2][1] - 1, 60 - core[1][1]
    elif kinds in (["half", "ordgen"],):
        h, m = core[1][1] - 1, 30
    elif kinds == ["lto", "num"]:
        h, m = core[1][1], 0
    else:
        return None
    if h is None or m is None:
        return None
    if h < 0:
        h += 12
    if not (0 <= h <= 24 and 0 <= m <= 59):
        return None
    if h == 24:
        h = 0
    if part == "noon" and h != 12:
        part = "afternoon" if h < 12 else None
    if part == "midnight" and h not in (0, 12):
        return None
    return {"h": h, "m": m, "mer": mer, "part": part, "exact": exact or h > 12 or h == 0,
            "default": False}


# ---------------------------------------------------------------- days
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
    "sunday": 6, "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
    "שני": 0, "שלישי": 1, "רביעי": 2, "חמישי": 3, "שישי": 4, "שבת": 5, "ראשון": 6,
    "الاثنين": 0, "الإثنين": 0, "الاتنين": 0, "اثنين": 0, "تنين": 0, "الثلاثاء": 1,
    "الثلاثا": 1, "التلات": 1, "التلاتا": 1, "ثلاثاء": 1, "الأربعاء": 2, "الاربعاء": 2,
    "الاربعا": 2, "الأربعا": 2, "اربعاء": 2, "الخميس": 3, "خميس": 3, "الجمعة": 4, "الجمعه": 4,
    "جمعة": 4, "السبت": 5, "سبت": 5, "الأحد": 6, "الاحد": 6, "احد": 6, "أحد": 6,
    "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2, "четверг": 3, "пятница": 4,
    "пятницу": 4, "суббота": 5, "субботу": 5, "воскресенье": 6,
}
# Hebrew weekdays that are also everyday words ("שני" is two or second, "ראשון" first)
# count only after יום or with ב in front.
_HE_AMBIGUOUS = {"שני", "ראשון"}
_DAY_OFF = {"today": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1,
            "היום": 0, "הערב": 0, "הלילה": 0, "מחר": 1, "מחרתיים": 2,
            "اليوم": 0, "الليلة": 0, "بكرة": 1, "بكرا": 1, "بكره": 1, "غدا": 1, "غداً": 1,
            "сегодня": 0, "завтра": 1, "послезавтра": 2}
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
           "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
           "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_DAY_FILL = {"on", "this", "the", "coming", "of", "for", "in",
             "ביום", "יום", "ב", "ל", "הזה", "הקרוב", "הבא", "הבאה", "עד",
             "يوم", "ال", "الجاي", "الجاية", "القادم", "القادمة", "هاد", "ع", "على",
             "в", "во", "на", "этот", "эту", "этой", "ближайший", "ближайшую", "к"}
_NEXT = {"next", "הבא", "הבאה", "الجاي", "الجاية", "القادم", "следующий", "следующую",
         "следующей"}
_PARTS_DAY = {**{w: "morning" for w in _AM if w != "am"},
              **{w: "afternoon" for w in _AFTERNOON}, **{w: "evening" for w in _EVENING},
              **{w: "night" for w in _NIGHT}}
_DAY_KNOWN = (set(_WEEKDAYS) | set(_DAY_OFF) | set(_MONTHS) | _DAY_FILL | _NEXT
              | set(_PARTS_DAY) | {"after", "day", "אחרי", "بعد"})


def parse_day(text: str) -> dict | None:
    """A day, when the WHOLE of `text` names one ("tomorrow", "on Friday", "next
    Monday", "the 14th", "October 14", "מחר", "ביום חמישי", "بكرا", "в четверг"):
    {"off": days from today} | {"wd": weekday 0-6} | {"md": (month, day)} | {"dom": day}
    plus "part" (morning, evening...) when the day carries one ("tomorrow morning")."""
    toks = _tokens(text)
    if not toks:
        return None
    raw = " ".join(toks)
    if re.fullmatch(r"(?:the )?day after tomorrow|היום שאחרי מחר|بعد بكرة|بعد بكرا|بعد غد", raw):
        return {"off": 2, "part": None}
    spec: dict = {}
    part = None
    nxt = False
    i = 0
    while i < len(toks):
        tok = toks[i]
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", tok)
        if m and not spec:
            d = int(m.group(1))
            nm = toks[i + 1] if i + 1 < len(toks) else ""
            if nm in _MONTHS:
                spec = {"md": (_MONTHS[nm], d)}
                i += 2
                continue
            if 1 <= d <= 31 and (tok != m.group(1) or "the" in toks[:i]):
                spec = {"dom": d}
                i += 1
                continue
            return None
        m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})", tok)
        if m and not spec:
            d, mo = int(m.group(1)), int(m.group(2))
            if 1 <= d <= 31 and 1 <= mo <= 12:
                spec = {"md": (mo, d)}
                i += 1
                continue
            return None
        if tok in _MONTHS and not spec and i + 1 < len(toks):
            m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", toks[i + 1])
            if m:
                spec = {"md": (_MONTHS[tok], int(m.group(1)))}
                i += 2
                continue
        pre, w = _split_prefix(tok, _DAY_KNOWN)
        if w in _DAY_OFF:
            if spec:
                return None
            spec = {"off": _DAY_OFF[w]}
            if w in ("tonight", "הערב", "الليلة"):
                part = "evening" if w != "tonight" else "tonight"
            if w == "הלילה":
                part = "night"
        elif w in _WEEKDAYS:
            if spec:
                return None
            if w in _HE_AMBIGUOUS and not (pre == "ב" or (i and toks[i - 1] in ("יום", "ביום"))):
                return None
            spec = {"wd": _WEEKDAYS[w]}
        elif w in _NEXT:
            nxt = True
        elif w in _PARTS_DAY:
            part = _PARTS_DAY[w]
        elif w in _DAY_FILL or w in ("day",):
            pass
        else:
            return None
        i += 1
    if not spec:
        return None
    if nxt and "wd" in spec:
        spec["next"] = True
    spec["part"] = part
    return spec


# ---------------------------------------------------------------- when, exactly
def _day_date(day: dict, today: _dt.date, hm: tuple[int, int] | None,
              now: _dt.datetime) -> _dt.date:
    if "off" in day:
        return today + _dt.timedelta(days=day["off"])
    if "wd" in day:
        ahead = (day["wd"] - today.weekday()) % 7
        d = today + _dt.timedelta(days=ahead)
        # Today's own weekday: today while its time is still ahead (or no time was said),
        # else next week. A weekday that has passed is never dropped: it is next week's.
        if ahead == 0 and hm is not None:
            t = _dt.datetime.combine(d, _dt.time(*hm))
            if t <= now:
                d += _dt.timedelta(days=7)
        return d
    if "md" in day:
        mo, dd = day["md"]
        for y in (today.year, today.year + 1):
            try:
                d = _dt.date(y, mo, dd)
            except ValueError:
                continue
            if d >= today:
                return d
        return today
    if "dom" in day:
        y, mo = today.year, today.month
        for _ in range(3):
            try:
                d = _dt.date(y, mo, day["dom"])
                if d >= today:
                    return d
            except ValueError:
                pass
            mo += 1
            if mo > 12:
                y, mo = y + 1, 1
        return today
    return today


def _hour(clock: dict, part: str | None, day_given: bool, d: _dt.date | None,
          now: _dt.datetime) -> tuple[int, int] | list[tuple[int, int]]:
    """The hour on the 24-hour clock. A list when it is still open: "at 6:30" with no am,
    pm or part of the day is the next 6:30 or 18:30, decided against the clock."""
    h, m = clock["h"], clock["m"]
    part = clock.get("part") or part
    mer = clock.get("mer")
    if clock.get("default") or clock.get("exact"):
        return h, m
    if mer == "pm" and h < 12:
        return h + 12, m
    if mer == "am":
        return (0 if h == 12 else h), m
    if part in ("morning",):
        return (0 if h == 12 else h), m
    if part in ("afternoon", "evening", "tonight"):
        return (h + 12 if h < 12 else h), m
    if part == "night":
        return (h + 12 if 6 <= h < 12 else (0 if h == 12 else h)), m
    if part == "noon":
        return 12, m
    if day_given and d is not None and d != now.date():
        # Another day with no am or pm: what people mean by "at 1" or "at 9".
        if 1 <= h <= 6:
            return h + 12, m
        return h, m
    return [(h % 12, m), (h % 12 + 12, m)]


def resolve(duration: float | None, clock: dict | None, day: dict | None,
            now: _dt.datetime | None = None) -> dict:
    """When exactly, from what the parser read: {"mode": "in"|"at"|"day"|None,
    "at": datetime or None, "date": date or None, "time": "HH:MM" or None,
    "minutes": minutes from now or None}."""
    now = now or _dt.datetime.fromtimestamp(_now())
    today = now.date()
    if duration:
        at = now + _dt.timedelta(minutes=duration)
        return {"mode": "in", "at": at, "date": at.date(), "time": at.strftime("%H:%M"),
                "minutes": duration}
    part = (day or {}).get("part")
    if clock is None and part:
        clock = {"h": _PART_DEFAULT.get("evening" if part == "tonight" else part, (20, 0))[0],
                 "m": 0, "mer": None, "part": None, "exact": True, "default": True}
        if part == "tonight":
            clock["h"] = 20
    if clock is None and day is None:
        return {"mode": None, "at": None, "date": None, "time": None, "minutes": None}
    if clock is None:
        d = _day_date(day, today, None, now)
        return {"mode": "day", "at": None, "date": d, "time": None, "minutes": None}
    d0 = _day_date(day, today, None, now) if day else None
    hm = _hour(clock, part, day is not None, d0, now)
    if day is not None:
        if isinstance(hm, list):
            hm = hm[0] if hm[0][0] >= 7 else hm[1]
        d = _day_date(day, today, hm, now)
        at = _dt.datetime.combine(d, _dt.time(*hm))
        if at <= now and "off" in day and day["off"] == 0:
            at += _dt.timedelta(days=1)          # "today at 9" said at 10: tomorrow's
    else:
        opts = hm if isinstance(hm, list) else [hm]
        cands = []
        for (h, m) in opts:
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t <= now + _dt.timedelta(seconds=30):
                t += _dt.timedelta(days=1)
            cands.append(t)
        at = min(cands)
    return {"mode": "at", "at": at, "date": at.date(), "time": at.strftime("%H:%M"),
            "minutes": (at - now).total_seconds() / 60}


# ---------------------------------------------------------------- finding, without Jev
def find(utterance: str) -> dict:
    """The longest stretch of her words that reads as a duration, a clock time and a day,
    for when Jev was not asked to pick (the intent said timer, the time question did
    not ride): {"duration", "clock", "day"} as the parsers return them."""
    cands = span_candidates(utterance)
    out = {"duration": None, "clock": None, "day": None}
    for c in cands:
        if out["duration"] is None:
            v = parse_duration(c)
            if v:
                out["duration"] = v
    words = set()
    for c in cands:
        if out["day"] is None and parse_day(c):
            out["day"] = parse_day(c)
            words = set(c.lower().split())
    for c in cands:
        if out["clock"] is None and not (set(c.lower().split()) & words) and parse_duration(c) is None:
            ck = parse_clock(c)
            if ck and not (len(c.split()) == 1 and _digit(c) is None and not ck.get("part")
                           and out["duration"]):
                out["clock"] = ck
    return out


_ALARM_WORDS = re.compile(r"\balarm|\bwake me|\bwake up|שעון מעורר|מעורר|תעירי|תעיר|להעיר|"
                          r"منب[ّ]?ه|صح[ّ]?يني|فيقيني|будильник|разбуди", re.IGNORECASE)
_TIMER_WORDS = re.compile(r"\btimer|טיימר|مؤقت|تايمر|таймер", re.IGNORECASE)


def guess(utterance: str) -> dict:
    """What read() returns, for when the intent says timer but the time question did not
    decide it: the kind from her words, the amount, clock time and day from find()."""
    f = find(utterance)
    kind = ("alarm" if _ALARM_WORDS.search(utterance or "") else
            "timer" if _TIMER_WORDS.search(utterance or "") else "reminder")
    return {"kind": kind, "conf": 0.0, "guessed": True,
            "spans": {k: None for k in SPAN_KEYS}, "about": None, **f}


# ---------------------------------------------------------------- the question
KINDS = {
    "none": "She is not asking for a timer, an alarm, a reminder or a calendar event: a "
            "message or a call that only mentions a time (text Dana I will be there at 8), "
            "a question about the time, the date or the weather, something to play, or "
            "anything else. Also none: changing or cancelling the timer, reminder or alarm "
            "MicMic has just set (make it ten minutes later, at 7 instead).",
    "timer": "A countdown for an amount of time: set a timer for 25 minutes, timer five "
             "minutes eggs, תכווני טיימר לשלוש דקות, حطّي مؤقت عشر دقايق, таймер на пять минут.",
    "reminder": "To be reminded of something later: after an amount of time, at a time or "
                "on a day, or a reminder with no time at all. Remind me in 50 minutes to "
                "take the bread out, remind me tomorrow at 9 to call Mom, remind me to text "
                "Tom in ten minutes, add a reminder to buy milk. תזכירי לי מחר בתשע להתקשר "
                "לאמא. ذكريني بعد ساعة. Напомни мне завтра позвонить маме.",
    "alarm": "To be woken up or alerted at a clock time, or after an amount of time: set an "
             "alarm for 7, wake me up at 6:30, wake me up thirty minutes from now. תשימי "
             "שעון מעורר לשבע. صحيني الساعة سبعة. Поставь будильник на семь.",
    "calendar": "To put an event she describes in her own words into her calendar: add "
                "lunch with Dana on Thursday at 1, put the dentist on my calendar for "
                "Friday. תוסיפי ליומן ארוחת צהריים עם דנה ביום חמישי. ضيفي عالرزنامة. "
                "Добавь в календарь обед с Даной в четверг. Not 'add this to my calendar' "
                "about something on her screen.",
    "time_left": "How much time is left on a timer, reminder or alarm she has running, or "
                 "when it goes off: how much time is left on the timer, when is my alarm. "
                 "כמה זמן נשאר בטיימר. قديش باقي عالمؤقت. Сколько осталось на таймере?",
    "cancel": "To stop a timer, reminder or alarm she has running: cancel the timer, turn "
              "off my alarm, never mind the reminder. תבטלי את הטיימר. ألغي المنبه. Отмени "
              "таймер.",
}
SPANS = {
    "duration": "Which words say HOW LONG from now: the amount of time (in fifty minutes, "
                "for 25 minutes, an hour and a half, thirty minutes from now). Not a clock "
                "time and not a day.",
    "clock": "Which words say AT WHAT TIME of day: the clock time (at 6:30, seven am, at "
             "noon, at 1, in the evening, לשבע בבוקר). Not an amount of time from now, and "
             "not the day.",
    "day": "Which words say ON WHICH DAY (tomorrow, on Friday, next Monday, the 14th, "
           "tomorrow morning). Not the time of day.",
    "about": "Which words say WHAT she wants to be reminded of, or the name of the event "
             "for her calendar, starting from 'to' when she said it: to call Mom, to take "
             "the bread out, buy milk, lunch with Dana, dentist. Not the words asking for "
             "the reminder or the calendar, not the time and not the day.",
}


def question(utterance: str) -> dict | None:
    """What rides in understand() when her words hold a time word; None otherwise, and
    then the request is exactly what it was. Candidates for the three time spans are
    only spans the parser can read as one."""
    if not mentions_time(utterance):
        return None
    cands = span_candidates(utterance)
    readers = {"duration": parse_duration, "clock": parse_clock, "day": parse_day}
    qs: dict = {QUESTION: {"type": "choice",
        "instructions": "Is she asking to set a timer, a reminder or an alarm, to put an "
                        "event in her calendar, to hear how long is left on one, or to "
                        "cancel one? Or none of these?",
        "criteria": KINDS}}
    found = {}
    for name, fn in readers.items():
        ok = [c for c in cands if fn(c) is not None]
        if ok:
            found[name] = ok
            qs[f"time_{name}"], qs[f"time_{name}_exists"] = _span_questions(ok, SPANS[name])
    if _ABOUT_WORDS.search(utterance) and cands:
        qs["time_about"], qs["time_about_exists"] = _span_questions(cands, SPANS["about"])
    return {"questions": qs, "found": found}


def read(u: dict, fold: dict | None) -> dict | None:
    """What she asked for: {"kind", "conf", "duration", "clock", "day", "about",
    "spans"}; None when the question was not asked or the answer is none."""
    if not fold:
        return None
    a = u.get("raw") or {}
    if QUESTION not in a:
        return None
    pick, conf, probs = choice(a, QUESTION)
    probs = dict(probs or {pick: conf})
    rest = {k: float(v or 0.0) for k, v in probs.items() if k != "none" and k in KINDS}
    if not rest:
        return None
    best = max(rest, key=rest.get)
    total = sum(rest.values())
    if pick == "none" and probs.get("none", 0.0) >= total:
        return None
    if total < TIME_GATE and pick != best:
        return None
    if total < TIME_GATE:
        return None
    got = {"kind": best, "conf": round(total, 2), "spans": {}}
    for name in SPAN_KEYS:
        key = f"time_{name}"
        span = _span_answer(a, key)[0] if key in a else None
        got["spans"][name] = span
    sp = got["spans"]
    got["duration"] = parse_duration(sp["duration"]) if sp["duration"] else None
    got["clock"] = parse_clock(sp["clock"]) if sp["clock"] else None
    got["day"] = parse_day(sp["day"]) if sp["day"] else None
    got["about"] = sp["about"]
    return got


# ---------------------------------------------------------------- what she hears
def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def say_duration(minutes: float, lang: str, unit_hint: str | None = None,
                 case: str = "acc") -> str:
    """An amount of time as she would say it. In minutes when she said minutes or it is
    under an hour ("200 minutes" stays 200 minutes), in seconds under a minute or when
    she said seconds, else hours and minutes."""
    secs = round(minutes * 60)
    if unit_hint == "s" or secs < 60:
        n = secs
        return {"english": f"{n} second{'s' if n != 1 else ''}",
                "hebrew": "שנייה" if n == 1 else f"{n} שניות",
                "arabic": "ثانية" if n == 1 else f"{n} ثانية",
                "russian": f"{n} " + _ru_plural(n, "секунду" if case == "acc" else "секунда",
                                                 "секунды", "секунд")}.get(lang, f"{n} seconds")
    mins = round(minutes)
    if unit_hint == "m" or mins < 60:
        n = mins
        return {"english": f"{n} minute{'s' if n != 1 else ''}",
                "hebrew": "דקה" if n == 1 else ("שתי דקות" if n == 2 else f"{n} דקות"),
                "arabic": "دقيقة" if n == 1 else ("دقيقتين" if n == 2 else f"{n} دقيقة"),
                "russian": f"{n} " + _ru_plural(n, "минуту" if case == "acc" else "минута",
                                                 "минуты", "минут")}.get(lang, f"{n} minutes")
    h, m = divmod(mins, 60)
    if lang == "hebrew":
        hs = "שעה" if h == 1 else ("שעתיים" if h == 2 else f"{h} שעות")
        if m == 30:
            return hs + " וחצי"
        if m == 15:
            return hs + " ורבע"
        return hs + (f" ו-{m} דקות" if m else "")
    if lang == "arabic":
        hs = "ساعة" if h == 1 else ("ساعتين" if h == 2 else f"{h} ساعات")
        if m == 30:
            return hs + " ونص"
        if m == 15:
            return hs + " وربع"
        return hs + (f" و{m} دقيقة" if m else "")
    if lang == "russian":
        hs = f"{h} " + _ru_plural(h, "час", "часа", "часов")
        if m:
            return hs + f" {m} " + _ru_plural(m, "минуту" if case == "acc" else "минута",
                                              "минуты", "минут")
        return hs
    hs = f"{h} hour{'s' if h != 1 else ''}"
    return hs + (f" and {m} minute{'s' if m != 1 else ''}" if m else "")


def unit_hint(span: str | None) -> str | None:
    """Which unit she counted in, so it is said back in the same one."""
    toks = set(_tokens(span or ""))
    if toks & _HOUR or toks & {"שעתיים", "ساعتين", "полчаса", "полтора"} or toks & _HALF:
        return None
    if toks & _SEC:
        return "s"
    if toks & _MIN or toks & {"דקותיים", "دقيقتين"}:
        return "m"
    return None


def clock_words(at: _dt.datetime) -> str:
    return f"{at.hour}:{at.minute:02d}"


_DAYNAME = {"english": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                        "Sunday"],
            "hebrew": ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"],
            "arabic": ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"],
            "russian": ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу",
                        "в субботу", "в воскресенье"]}


def day_words(d: _dt.date, lang: str, today: _dt.date | None = None) -> str:
    """'' for today, else tomorrow or the weekday and date, as a phrase that sits before
    "at 9:00"."""
    today = today or _dt.date.fromtimestamp(_now())
    if d == today:
        return ""
    if d == today + _dt.timedelta(days=1):
        return {"english": "tomorrow", "hebrew": "מחר", "arabic": "بكرا",
                "russian": "завтра"}.get(lang, "tomorrow")
    wd = _DAYNAME.get(lang, _DAYNAME["english"])[d.weekday()]
    if lang == "english":
        return f"on {wd} {d.day} {d.strftime('%B')}"
    if lang == "hebrew":
        return f"ביום {wd} {d.day}.{d.month}" if wd != "שבת" else f"בשבת {d.day}.{d.month}"
    if lang == "arabic":
        return f"يوم {wd} {d.day}/{d.month}"
    return f"{wd} {d.day}.{d.month:02d}"


def day_for(d: _dt.date, lang: str, today: _dt.date | None = None) -> str:
    """The day as the object of "for": "tomorrow", "Friday 9 October" in English (after
    "for"); "למחר", "ליום שישי 9.10"; "لبكرا", "ليوم الجمعة 9/10"; "на завтра",
    "на пятницу 09.10"."""
    today = today or _dt.date.fromtimestamp(_now())
    wd = _DAYNAME.get(lang, _DAYNAME["english"])[d.weekday()]
    near = {0: {"english": "today", "hebrew": "להיום", "arabic": "لليوم", "russian": "на сегодня"},
            1: {"english": "tomorrow", "hebrew": "למחר", "arabic": "لبكرا", "russian": "на завтра"}}
    off = (d - today).days
    if off in near:
        return near[off].get(lang, near[off]["english"])
    if lang == "hebrew":
        return (f"ליום {wd} {d.day}.{d.month}" if wd != "שבת" else f"לשבת {d.day}.{d.month}")
    if lang == "arabic":
        return f"ليوم {wd} {d.day}/{d.month}"
    if lang == "russian":
        return "на " + wd.split(" ", 1)[1] + f" {d.day}.{d.month:02d}"
    return f"{wd} {d.day} {d.strftime('%B')}"


def _about(text: str | None, lang: str) -> str:
    """What it is about, after the time: " to call Mom" when she said "to ...", else
    ": the pasta"; Hebrew keeps an infinitive as it is ("להוציא את הלחם")."""
    t = (text or "").strip()
    if not t:
        return ""
    if lang == "english":
        return f" {t}" if t.lower().startswith("to ") else f": {t}"
    if lang == "hebrew" and t.startswith("ל"):
        return f" {t}"
    return f": {t}"


LINES = {
    "timer": {"english": "Timer for {dur}.", "hebrew": "טיימר ל{dur_he}.",
              "arabic": "مؤقت لمدة {dur}.", "russian": "Таймер на {dur}."},
    "reminder_in": {"english": "I'll remind you in {dur}{about}.",
                    "hebrew": "אזכיר לך בעוד {dur}{about}.",
                    "arabic": "بذكّرك بعد {dur}{about}.",
                    "russian": "Напомню через {dur}{about}."},
    "reminder_at": {"english": "I'll remind you {day}at {t}{about}.",
                    "hebrew": "אזכיר לך {day}בשעה {t}{about}.",
                    "arabic": "بذكّرك {day}الساعة {t}{about}.",
                    "russian": "Напомню {day}в {t}{about}."},
    "alarm_at": {"english": "Alarm for {t}{day}. I will say it out loud, so leave MicMic running.",
                 "hebrew": "שעון מעורר ל-{t}{day}. אגיד את זה בקול, אז צריך להשאיר את MicMic פתוח.",
                 "arabic": "منبّه الساعة {t}{day}. رح قولها بصوت عالي، فخلّي MicMic شغّال.",
                 "russian": "Будильник на {t}{day}. Я скажу это вслух, поэтому не закрывайте MicMic."},
    "alarm_in": {"english": "Alarm in {dur}, at {t}. I will say it out loud, so leave MicMic running.",
                 "hebrew": "שעון מעורר בעוד {dur}, ב-{t}. אגיד את זה בקול, אז צריך להשאיר את MicMic פתוח.",
                 "arabic": "منبّه بعد {dur}، الساعة {t}. رح قولها بصوت عالي، فخلّي MicMic شغّال.",
                 "russian": "Будильник через {dur}, в {t}. Я скажу это вслух, поэтому не закрывайте MicMic."},
    "row": {"english": "I added a reminder{about}.", "hebrew": "הוספתי תזכורת{about}.",
            "arabic": "ضفت تذكير{about}.", "russian": "Добавила напоминание{about}."},
    "row_day": {"english": "I added a reminder for {day}{about}.",
                "hebrew": "הוספתי תזכורת {day}{about}.",
                "arabic": "ضفت تذكير {day}{about}.",
                "russian": "Добавила напоминание {day}{about}."},
    "row_failed": {"english": "I could not add that to Reminders.",
                   "hebrew": "לא הצלחתי להוסיף את זה לתזכורות.",
                   "arabic": "ما قدرت ضيفها عالتذكيرات.",
                   "russian": "Не получилось добавить это в Напоминания."},
    "ask_duration": {"english": "For how long?", "hebrew": "לכמה זמן?", "arabic": "لقديش؟",
                     "russian": "На сколько?"},
    "ask_clock": {"english": "For what time?", "hebrew": "לאיזו שעה?", "arabic": "لأي ساعة؟",
                  "russian": "На какое время?"},
    "ask_when": {"english": "When is it? Tell me the day and the time.",
                 "hebrew": "מתי זה? באיזה יום ובאיזו שעה?",
                 "arabic": "إمتى؟ أي يوم وأي ساعة؟",
                 "russian": "Когда это? В какой день и во сколько?"},
    "left_one": {"english": "{dur} left on the timer.", "hebrew": "נשארו {dur} בטיימר.",
                 "arabic": "باقي {dur} عالمؤقت.", "russian": "На таймере осталось {dur}."},
    "left_reminder": {"english": "The reminder goes off in {dur}, at {t}{about}.",
                      "hebrew": "התזכורת בעוד {dur}, בשעה {t}{about}.",
                      "arabic": "التذكير بعد {dur}، الساعة {t}{about}.",
                      "russian": "Напоминание через {dur}, в {t}{about}."},
    "left_alarm": {"english": "Your alarm goes off at {t}, in {dur}.",
                   "hebrew": "השעון המעורר יצלצל ב-{t}, בעוד {dur}.",
                   "arabic": "المنبّه رح يرن الساعة {t}، بعد {dur}.",
                   "russian": "Будильник сработает в {t}, через {dur}."},
    "left_none": {"english": "There is no timer running.", "hebrew": "אין טיימר פעיל.",
                  "arabic": "ما في مؤقت شغّال.", "russian": "Таймер не запущен."},
    "cancelled": {"english": "I cancelled the {what}.", "hebrew": "ביטלתי את {what_he}.",
                  "arabic": "لغيت {what_ar}.", "russian": "Отменила {what_ru}."},
    # What she hears when it goes off.
    "fire_timer": {"english": "Your timer is done.", "hebrew": "הטיימר הסתיים.",
                   "arabic": "خلص المؤقت.", "russian": "Таймер сработал."},
    "fire_alarm": {"english": "Alarm. It is {t}.", "hebrew": "שעון מעורר. השעה {t}.",
                   "arabic": "المنبّه. الساعة {t}.", "russian": "Будильник. Сейчас {t}."},
    # A follow-up moved it.
    "alarm_moved": {"english": "Alright, the alarm is now at {t}.",
                    "hebrew": "בסדר, השעון המעורר עכשיו ב-{t}.",
                    "arabic": "ماشي، المنبّه صار الساعة {t}.",
                    "russian": "Хорошо, будильник теперь на {t}."},
    "row_moved": {"english": "Alright, the reminder is now for {day}{at}.",
                  "hebrew": "בסדר, התזכורת עכשיו {day}{at}.",
                  "arabic": "ماشي، التذكير صار {day}{at}.",
                  "russian": "Хорошо, напоминание теперь {day}{at}."},
    "row_missing": {"english": "I could not find that reminder in Reminders, so I changed nothing.",
                    "hebrew": "לא מצאתי את התזכורת הזאת, אז לא שיניתי כלום.",
                    "arabic": "ما لقيت هالتذكير، فما غيّرت شي.",
                    "russian": "Я не нашла это напоминание, поэтому ничего не меняла."},
    "row_change_failed": {"english": "I could not change that reminder.",
                          "hebrew": "לא הצלחתי לשנות את התזכורת.",
                          "arabic": "ما قدرت غيّر التذكير.",
                          "russian": "Не получилось изменить напоминание."},
    "row_recall": {"english": "The reminder is {about_bare}{day}.",
                   "hebrew": "התזכורת היא {about_bare}{day}.",
                   "arabic": "التذكير هو {about_bare}{day}.",
                   "russian": "Напоминание: {about_bare}{day}."},
    # Came due while MicMic was not running (mac.restore_timers): said once, at launch.
    "missed_reminder": {"english": "While MicMic was off, you had a reminder {day}at {t}{about}.",
                        "hebrew": "בזמן ש-MicMic היה כבוי, הייתה לך תזכורת {day}בשעה {t}{about}.",
                        "arabic": "لما كان MicMic مسكّر، كان عندك تذكير {day}الساعة {t}{about}.",
                        "russian": "Пока MicMic был выключен, у вас было напоминание {day}в {t}{about}."},
    "missed_alarm": {"english": "While MicMic was off, you had an alarm {day}at {t}.",
                     "hebrew": "בזמן ש-MicMic היה כבוי, היה לך שעון מעורר {day}לשעה {t}.",
                     "arabic": "لما كان MicMic مسكّر، كان عندك منبّه {day}الساعة {t}.",
                     "russian": "Пока MicMic был выключен, у вас был будильник {day}на {t}."},
    "missed_timer": {"english": "While MicMic was off, your timer ended {day}at {t}.",
                     "hebrew": "בזמן ש-MicMic היה כבוי, הטיימר שלך הסתיים {day}בשעה {t}.",
                     "arabic": "لما كان MicMic مسكّر، خلص المؤقت تبعك {day}الساعة {t}.",
                     "russian": "Пока MicMic был выключен, ваш таймер закончился {day}в {t}."},
    "row_remove": {"english": "I do not delete reminders. It is still in Reminders, and you can remove it there.",
                   "hebrew": "אני לא מוחקת תזכורות. היא עדיין באפליקציית התזכורות, ואפשר להסיר אותה שם.",
                   "arabic": "أنا ما بمحي تذكيرات. لسا موجود بتطبيق التذكيرات، وفيك تشيله من هناك.",
                   "russian": "Я не удаляю напоминания. Оно по-прежнему в Напоминаниях, и его можно убрать там."},
}

_WHAT = {"timer": ("timer", "הטיימר", "المؤقت", "таймер"),
         "alarm": ("alarm", "השעון המעורר", "المنبّه", "будильник"),
         "reminder": ("reminder", "התזכורת", "التذكير", "напоминание")}


def line(key: str, lang: str, **fmt) -> str:
    t = LINES[key]
    s = t.get(lang) or t["english"]
    if "{dur_he}" in s:
        d = fmt.get("dur", "")
        fmt["dur_he"] = ("-" + d) if d[:1].isdigit() else d
    if "{what" in s:
        w = _WHAT.get(fmt.get("what", "timer"), _WHAT["timer"])
        fmt.update(what=w[0], what_he=w[1], what_ar=w[2], what_ru=w[3])
    return s.format(**fmt)


def about_words(text: str | None, lang: str) -> str:
    return _about(text, lang)


def missed_line(entry: dict, today: _dt.date | None = None) -> str:
    """What she hears at launch about one that came due while MicMic was off."""
    lang = entry.get("language") or "english"
    at = _dt.datetime.fromtimestamp(entry["at"])
    dword = day_words(at.date(), lang, today)
    kind = entry.get("kind") if entry.get("kind") in ("alarm", "timer") else "reminder"
    text = (entry.get("text") or "").strip()
    return line(f"missed_{kind}", lang, t=clock_words(at), day=(dword + " ") if dword else "",
                about=f": {text}" if text else "")


def fire_lead(kind: str, lang: str, at: float) -> str | None:
    """The first words she hears when it goes off; None keeps the reminder's own."""
    if kind == "timer":
        return line("fire_timer", lang)
    if kind == "alarm":
        return line("fire_alarm", lang, t=clock_words(_dt.datetime.fromtimestamp(at)))
    return None


def tidy_title(text: str, names: list[str]) -> str:
    """An event title from her words: the first letter raised, and a name from her book
    written as the book writes it ("lunch with dana" -> "Lunch with Dana")."""
    t = (text or "").strip()
    firsts = {}
    for n in names or []:
        for part in str(n).split():
            if len(part) > 1:
                firsts.setdefault(part.lower(), part)
    t = " ".join(firsts.get(w.lower(), w) for w in t.split())
    return t[:1].upper() + t[1:]


DEICTIC = re.compile(r"^\W*(?:this|that|it|these|those|this one|that one|את זה|זה|הזה|"
                     r"هاد|هاي|هذا|هذه|это|вот это)\W*$", re.IGNORECASE)
