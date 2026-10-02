"""Who she means, and what must never go out on a plain countdown. Code only: nothing
here makes a model call, so each check is free and the same every time.

The bench (MicMic Bench v1, 2026-10-01, BENCH_REPORT.md themes 1 and 2) found the
failures this file exists for:

  * "text dina that i'm outside" with no Dina in the book armed a countdown to Dana
    Cohen, 3/3. Jev's "is this the same person, as it sounds" cleared its 0.40 gate
    for Dina against Dana. Whether a spoken word IS a contact's name is decided here,
    from the spelling: the same name (in any script) is a match, a name that only
    sounds alike is a question ("I don't have Dina in your contacts. Did you mean
    Dana Cohen?"), and anything else (mum, a nickname, a translation) is still Jev's.
  * "call david" with two Davids dialled David Katz, 3/3. A first name shared by two
    rows of her book is asked about ("David Katz or David Stern?"), for calls too.
  * A contact with no number got a countdown (msg-010) or "I could not make the call"
    with no reason (hard-msg-008).
  * "text dana ..." overrode her saved WhatsApp choice: "text" is the verb for any
    message, only "SMS", "iMessage" or "text message" name the app.
  * "call 911" called her emergency contact; a card number went out on a countdown;
    "transfer 500 shekels to this account" started a browser task.

Everything matches the address book rows (savta/actions/contacts.py): a contact Jev
names that is not a row of the book is "unknown" here, and the router keeps its old
behaviour for it.
"""
from __future__ import annotations

import functools
import re

from .actions import contacts as book
from .actions import screen as _screen

# ---------------------------------------------------------------- spoken words
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_HEB = re.compile(r"[֐-׿]")
_ARB = re.compile(r"[؀-ۿ]")
_CYR = re.compile(r"[Ѐ-ӿ]")
# A name follows these in her languages: the dative and the "and"/"of" prefixes.
_HE_PREFIX = ("ול", "של", "לה", "ל", "ו", "ה", "ש", "ב", "מ", "כ")
_AR_PREFIX = ("ول", "لل", "وال", "ال", "ل", "و", "ب")
# Words that are never anyone's name, so they are never "a name that sounds like" one.
_NOT_NAMES = set("""
a an the to for of on in at by with and or but so if my me mine her him his their them
they she he it its you your we us our this that these those what when where who how
text texts texting message messages send sends call ring phone dial tell say saying
says whatsapp sms imessage please now right just also again too then back up out
there here is am are was were be been do does did i im i'm ill i'll id i'd ive i've
not no yes ok okay hi hey hello late soon home way car outside inside running coming
going see meet dinner today tonight tomorrow morning evening night week minutes hours
can could would should will shall let lets let's get got give gave go
""".split())
_NOT_NAMES_HE = set("""
תשלחי תשלח שלחי לשלוח הודעה הודעות תכתבי תכתוב תגידי תגיד תתקשרי תתקשר להתקשר
תחייגי תחייג לי לו לה להם שאני אני את אתה הוא היא זה זאת של על עם עכשיו בבקשה
ש וגם גם בוואטסאפ ווטסאפ וואטסאפ בדרך מאחרת מאחר בבית
""".split())


def _words(utterance: str) -> list[str]:
    return [w.lower().replace("’", "'") for w in _WORD.findall(utterance or "")]


def _variants(word: str) -> list[str]:
    """The word, plus the word with a bound prefix or an English possessive taken off."""
    out = [word]
    if word.endswith("'s"):
        out.append(word[:-2])
    if _HEB.search(word):
        out += [word[len(p):] for p in _HE_PREFIX if word.startswith(p) and len(word) > len(p) + 1]
    elif _ARB.search(word):
        out += [word[len(p):] for p in _AR_PREFIX if word.startswith(p) and len(word) > len(p) + 1]
    return out


def _script(s: str) -> str:
    if _HEB.search(s):
        return "he"
    if _ARB.search(s):
        return "ar"
    if _CYR.search(s):
        return "ru"
    return "latin"


# Hebrew and Arabic write a name's consonants and some of its vowels: Dana is דנה,
# Dina is דינה, David is דויד or דוד, Cohen is כהן. A Latin name becomes a pattern of
# the latinized spellings it can have (contacts.latinize: א/ע -> a, ה -> h, ו -> w,
# י -> y, ש -> sh, צ -> ts), and the other script's word has to fit it. The point is
# the vowel letters: an i or an e can be written י, an a never is. So דינה does not fit
# Dana, while דנה does.
_CONS = {"b": "b", "c": "(?:k|s|ts)", "d": "d", "f": "(?:f|p)", "g": "g",
         "j": "(?:g|y|j|z)", "k": "k", "l": "l", "m": "m", "n": "n", "p": "(?:p|f)",
         "q": "k", "r": "r", "s": "(?:s|sh|z)", "t": "t", "v": "(?:w|b)",
         "w": "(?:w|b)", "x": "(?:ks|kts)", "y": "y", "z": "(?:z|ts)", "h": "h?"}
_VOW = {"a": "(?:a|h|ah|ha)?", "e": "(?:y|a|h)?", "i": "y?", "o": "(?:w|a)?", "u": "w?"}


@functools.lru_cache(maxsize=4096)
def _latin_pattern(latin: str) -> re.Pattern | None:
    lat = book.latinize(latin)
    if not lat or not lat.isascii() or not lat.isalpha():
        return None
    parts = ["a?"] if lat[0] in _VOW else []
    for ch in lat:
        parts.append(_VOW.get(ch) or _CONS.get(ch) or re.escape(ch))
    return re.compile("^" + "".join(parts) + "$")


# Family words are one name however they are said: "text mum" is the contact "Mom"
# (Jev agreed, 0.84-0.87), not a name that only sounds like it.
_FAMILY = [{"mom", "mum", "mommy", "mummy", "mama", "mamma", "mother", "אמא", "אימא", "ماما",
            "мама"},
           {"dad", "daddy", "papa", "father", "abba", "aba", "אבא", "بابا", "папа"},
           {"grandma", "granny", "nana", "savta", "סבתא", "تيتا", "бабушка"},
           {"grandpa", "grandad", "granddad", "saba", "סבא", "جدو", "дедушка"}]


def _family(word: str) -> int:
    w = word.lower()
    return next((i for i, f in enumerate(_FAMILY) if w in f), -1)


@functools.lru_cache(maxsize=65536)
def _same_spelling(said: str, part: str) -> bool:
    """`said` (one spoken word or two joined) is `part` of a contact's name, in its own
    script or the other one."""
    a, b = book.latinize(said), book.latinize(part)
    if not a or not b:
        return False
    if a == b:
        return True
    if _family(said) >= 0 and _family(said) == _family(part):
        return True
    sa, sb = _script(said), _script(part)
    if sa == sb or "ru" in (sa, sb):
        return False
    if sa == "latin":
        pat, other = _latin_pattern(said), b
    elif sb == "latin":
        pat, other = _latin_pattern(part), a
    else:
        return False
    return bool(pat and pat.match(other))


def _edit1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))


def _sounds_like(said: str, part: str) -> bool:
    """A different name that sounds like this one: Dina and Dana, Gail and Gal."""
    if said in _NOT_NAMES or said in _NOT_NAMES_HE or len(book.latinize(said)) < 3:
        return False
    a, b = book.latinize(said), book.latinize(part)
    ka, kb = book.skeleton(said), book.skeleton(part)
    if len(ka) >= 2 and ka == kb and a[:1] == b[:1]:
        return True
    return _script(said) == _script(part) == "latin" and len(a) >= 4 and _edit1(a, b)


def _parts(row: dict) -> tuple[list[str], str]:
    """(the words of her name, the first name)."""
    words = [w for w in re.split(r"[\s\-]+", row.get("name") or "") if w]
    first = (row.get("first") or (words[0] if words else "")).strip()
    return words, first


def _spoken(utterance: str) -> list[tuple[str, str]]:
    """(what she said, how to show it): single words with their prefix variants, plus
    two neighbouring words joined ("co hen" for Cohen, "ben ami")."""
    ws = _words(utterance)
    out = []
    for i, w in enumerate(ws):
        for v in _variants(w):
            out.append((v, v))
        if i + 1 < len(ws):
            out.append((w + ws[i + 1], f"{w} {ws[i + 1]}"))
    return out


def row_for(name: str) -> dict | None:
    if not name:
        return None
    try:
        rows = book.all_contacts()
    except Exception:  # noqa: BLE001
        return None
    for r in rows:
        if r.get("name") == name:
            return r
    return None


def _matches(utterance: str, row: dict) -> tuple[set[str], str]:
    """Which parts of this row she said exactly ("first", "last", "full"), and the word
    of hers that only sounds like a part of it (or "")."""
    words, first = _parts(row)
    full = "".join(words)
    hit, alike = set(), ""
    for said, shown in _spoken(utterance):
        if said in _NOT_NAMES or said in _NOT_NAMES_HE:
            continue
        if full and len(words) > 1 and _same_spelling(said, full):
            hit.add("full")
        if first and _same_spelling(said, first):
            hit.add("first")
        for w in words:
            if w != first and _same_spelling(said, w):
                hit.add("last")
        if not alike and " " not in shown and any(
                _sounds_like(said, p) for p in {first, *words} if p):
            alike = shown
    return hit, alike


def check(utterance: str, contact: str) -> dict:
    """Is the name she said this contact's name?

    {"match": "exact" | "alike" | "none" | "unknown", "said": the word she said that
    only sounds like the name, "namesakes": other rows sharing the first name she said
    when that is all she said, "exact_other": a row whose name she said exactly when
    Jev chose another}. "unknown": the contact is not a row of her book."""
    row = row_for(contact)
    if row is None:
        return {"match": "unknown", "said": "", "namesakes": [], "exact_other": ""}
    hit, alike = _matches(utterance, row)
    if hit:
        names = []
        if hit == {"first"}:
            names = [r["name"] for r in _rows() if r.get("name") != contact
                     and _same_first(r, _parts(row)[1])
                     and not _matches(utterance, r)[0] - {"first"}]
        return {"match": "exact", "said": "", "namesakes": names, "exact_other": ""}
    if alike:
        other = next((r["name"] for r in _rows() if r.get("name") != contact
                      and _matches(alike, r)[0]), "")
        return {"match": "alike", "said": alike, "namesakes": [], "exact_other": other}
    return {"match": "none", "said": "", "namesakes": [], "exact_other": ""}


def _rows() -> list[dict]:
    try:
        return list(book.all_contacts())
    except Exception:  # noqa: BLE001
        return []


def _same_first(r: dict, first: str) -> bool:
    return bool(first) and book.latinize(_parts(r)[1]) == book.latinize(first)


def names_contact(utterance: str, exclude: str = "") -> str:
    """A row of her book, other than `exclude`, whose name she says exactly ("no wait,
    to Noa" while Gal's message counts down), or "". Which one is the follow-up's to
    decide; this only says that she named somebody."""
    for r in _rows():
        if r.get("name") != exclude and _matches(utterance, r)[0]:
            return r["name"]
    return ""


def only_named(utterance: str) -> tuple[str, set[str]]:
    """(the one row of her book her words name, the words of hers that are that name),
    or ("", set()) when none or more than one is named. The row named most fully wins
    ("dana cohen" is Dana Cohen, not every Cohen); a first name two rows share, said
    alone, is two people. Code only: what the router expects the recipient to be
    before Jev has answered (savta/router.py, _early_body)."""
    rows = _rows()
    hits = []
    for r in rows:
        hit, _ = _matches(utterance, r)
        if hit:
            hits.append((2 if ("full" in hit or {"first", "last"} <= hit) else 1, hit, r))
    if not hits:
        return "", set()
    top = max(h[0] for h in hits)
    best = [h for h in hits if h[0] == top]
    if len(best) != 1:
        return "", set()
    _, hit, row = best[0]
    words, first = _parts(row)
    if hit == {"first"} and any(o is not row and _same_first(o, first) for o in rows):
        return "", set()
    said = {s for s, _ in _spoken(utterance)
            if any(_same_spelling(s, p) for p in [*words, "".join(words)] if p)}
    return row["name"], said


_BEFORE_NAME = {"to", "text", "texting", "message", "call", "ring", "phone", "dial", "tell",
                "whatsapp", "facetime"}


def said_name(utterance: str) -> str:
    """The word she most likely said as the person's name, for "I don't have Michael in
    your contacts". "" when no word reads as a name."""
    ws = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", utterance or "", re.UNICODE)
    for i, w in enumerate(ws[:-1]):
        nxt = ws[i + 1]
        if w.lower() in _BEFORE_NAME and nxt.lower() not in _NOT_NAMES and len(nxt) > 1 \
                and nxt.isascii():
            return nxt[:1].upper() + nxt[1:]
    for w in ws:
        if _HEB.search(w) and w.startswith("ל") and len(w) > 2 and w not in _NOT_NAMES_HE \
                and w[1:] not in _NOT_NAMES_HE:
            return w[1:]
    return ""


def shown(said: str) -> str:
    """A spoken word as it is said back: "dina" -> "Dina"."""
    return said[:1].upper() + said[1:] if said and said.isascii() else said


# ---------------------------------------------------------------- reaching her
def reach(contact: str) -> dict | None:
    """What this contact can be reached by: {"imessage", "whatsapp", "call"}, or None
    when the book does not say (not a row, or a row read from the Contacts app, which
    gives names only and lets Messages find the person itself)."""
    row = row_for(contact)
    if row is None or row.get("source") == "macos":
        return None
    phone = (row.get("phone") or "").strip()
    waid = (row.get("waid") or "").strip()
    email = (row.get("email") or "").strip() if isinstance(row.get("email"), str) else ""
    num = bool(phone or waid)
    return {"imessage": num or bool(email), "whatsapp": num, "call": num}


# ---------------------------------------------------------------- channel words
# The verb "text" is any message: "text Dana" is just "message Dana", and her saved
# "use WhatsApp for Dana" holds for it (hard-pref-001, hard-msg-020). The noun still
# names the app, as it did ("send Dana a text", "by text", tests/test_prefs.py), and so
# do "SMS", "iMessage" and "text message".
_SMS = re.compile(
    r"\b(?:sms|imessage|i\s+message|text\s+messages?|a\s+text|(?:as|by|via|in)\s+(?:a\s+)?text)\b|"
    r"טקסט|סמס|מסרון|رسالة\s*نصية|نصية|\bsms\b|смс|эсэмэс|текстов",
    re.I)


def says_sms(utterance: str) -> bool:
    """She named text messages as the app, rather than using "text" as the verb."""
    return bool(_SMS.search(utterance or ""))


# ---------------------------------------------------------------- secrets
# Reuses the screen's redaction (savta/actions/screen.py): the same codes, Luhn card
# numbers, IBANs, CVVs and keys it hides from "send this" are what she must not send
# on a plain countdown. Plus what only speech has: "my password is ...", an ID number,
# and a card number she reads out that is not Luhn-valid but says it is a card.
_CARD_WORDS = re.compile(r"credit\s*card|debit\s*card|\bcard\b|כרטיס|بطاقة|карт",
                         re.I)
_PASSWORD = re.compile(
    r"\b(?:password|passcode|pin\s*code|pin)\b\s*(?:is|was|:|=)|"
    r"סיסמ[הא]\s*(?:שלי\s*)?(?:היא|זה|:)|הסיסמ[הא]\s|קוד\s*(?:הסודי|הכניסה)|"
    r"كلمة\s*(?:السر|المرور)|الرقم\s*السري|"
    r"пароль\s*(?:от\s*\S+\s*)?(?:это|:|-)?\s*\S|пин[- ]?код",
    re.I)
_ID_WORDS = re.compile(
    r"\b(?:id|i\.d\.|identity|passport|social\s*security|ssn|teudat\s*zehut)\b|"
    r"ת[\"״']ז|תעודת\s*ה?זהות|מספר\s*ה?זהות|דרכון|رقم\s*الهوية|جواز|паспорт|снилс|инн",
    re.I)
_LONG_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_ID_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){5,11}\d(?!\d)")


def secret_kind(body: str, said: str = "") -> str | None:
    """"card", "password", "id", "code" or None for a message she is about to send.
    `said` is her whole sentence: the words picked as the message may be only the
    number ("send gal my credit card number 4580 ..." picked "4580 1234 5678 9012",
    which is not Luhn-valid), and what it is was said around it."""
    if not body:
        return None
    ctx = f"{body} {said or ''}"
    for m in _LONG_DIGITS.finditer(body):
        digits = re.sub(r"\D", "", m.group(0))
        if _screen._luhn(digits) or _CARD_WORDS.search(ctx):
            return "card"
    if _screen._CVV.search(body):
        return "card"
    if _screen._IBAN.search(body) and _screen.redact(body) != body:
        return "card"
    if _PASSWORD.search(body) or (_PASSWORD.search(said or "") and len(body.split()) <= 3):
        return "password"
    if _ID_WORDS.search(ctx) and _ID_DIGITS.search(body):
        return "id"
    if _screen.redact(body) != body:
        return "code"
    return None


# ---------------------------------------------------------------- emergency numbers
_EMERGENCY = ("911", "112", "999", "000", "100", "101", "102")
_CALL_VERB = (r"(?:call|dial|ring|phone|get\s+me|connect\s+me\s+to|"
              r"תתקשרי|תתקשר|להתקשר|תחייגי|תחייג|לחייג|"
              r"اتصل|اتصلي|دق|دقي|позвони|позвоните|набери|наберите|звони)")
_SPOKEN_911 = re.compile(r"\bnine[\s-]*one[\s-]*one\b|\bnine[\s-]*eleven\b", re.I)


def emergency_number(utterance: str) -> str:
    """The emergency number she asks to call ("call 911", "תתקשרי למאה ואחת" is not
    attempted), or "". A number alone is not enough: "set the volume to 100" is not."""
    low = (utterance or "").lower()
    if _SPOKEN_911.search(low) and re.search(_CALL_VERB, low):
        return "911"
    for n in _EMERGENCY:
        if re.search(_CALL_VERB + r"\s*(?:to\s+|the\s+|ל-?|ל|на\s+)?" + n + r"(?!\d)", low):
            return n
    return ""


# ---------------------------------------------------------------- in trouble
# hard-safe-006 (live, 2026-10-02): "send my location to tom weiss right now, im in
# trouble" scored emergency 0.43, just under the 0.45 that makes her own words the
# message, and she was asked "What should it say?". A code check on her words, so the
# outcome does not ride on a score near its gate. Deliberately no bare "help me" or
# "עזרה": "help me send a message to Tom" is not trouble.
_TROUBLE = re.compile(
    r"\bin\s+(?:big\s+|real\s+)?(?:trouble|danger)\b|\bemergency\b|"
    r"\bi\s*(?:'|’)?\s*(?:m|am)\s+(?:so\s+|really\s+)?(?:scared|frightened|hurt|not\s+safe|unsafe)\b|"
    r"\bi\s+(?:have\s+|'ve\s+)?(?:fell|fallen)\b|\bi\s+need\s+help\b|"
    r"\bsome(?:one|body)\s+is\s+(?:following|attacking|hurting)\b|"
    r"בצרה|הצילו|מפחדת|מפחד\b|נפלתי|בסכנה|חירום|"
    r"ورطة|النجدة|خايفة|خايف|وقعت|بخطر|طوارئ|"
    r"в\s+беде|помогите|страшно|боюсь|я\s+упал|опасн|чрезвычайн",
    re.I)
# "Send my location / where I am / my address to Tom". MicMic has no way to read where
# she is (no Location Services in the app), so it says so plainly (LINES["no_location"]).
_LOCATION = re.compile(
    r"\bmy\s+(?:current\s+|exact\s+|home\s+)?(?:location|address|position|whereabouts|gps)\b|"
    r"\bwhere\s+i\s+(?:am|'m)\b|\bwhere\s+i\s+live\b|"
    r"מיקום|איפה\s+אני|הכתובת\s+שלי|"
    r"موقعي|وين\s+(?:أنا|انا)|عنواني|"
    r"местоположени|геолокац|где\s+я\b|мой\s+адрес|адрес\s+мой",
    re.I)


def in_trouble(utterance: str) -> bool:
    """Her words say she is in trouble ("im in trouble", "אני בצרה", "я в беде")."""
    return bool(_TROUBLE.search(utterance or ""))


def asks_location(utterance: str) -> bool:
    """She asks for where she is to be sent ("send my location to Tom")."""
    return bool(_LOCATION.search(utterance or ""))


# ---------------------------------------------------------------- money on the web
_TRANSFER = re.compile(
    r"\b(?:transfer|wire|send)\b[^.]{0,30}\b(?:money|shekels?|nis|dollars?|euros?|pounds?|"
    r"\$|€|₪|\d+)|\b(?:bank\s*transfer|wire\s*transfer)\b|"
    r"תעביר[יו]?\s|להעביר\s*(?:כסף|\d)|העברה\s*בנקאית|"
    r"حو[ّ]?ل[ي]?\s*(?:مصاري|فلوس|\d)|تحويل\s*(?:مصاري|فلوس|بنكي)|"
    r"переведи|перевести\s*деньги|перевод\s*денег",
    re.I)
_BUY = re.compile(
    r"\b(?:buy|purchase|order|pay|checkout|check\s+out|place\s+an?\s+order)\b|"
    r"תקני|תקנה|לקנות|קני\s|תזמיני\s*(?:לי\s*)?(?!תור|מקום|שולחן)|תשלמי|לשלם|"
    r"اشتري|اشتريلي|ادفع|ادفعي|اطلب|اطلبي|"
    r"купи|купить|закажи|заказать|оплати|оплатить",
    re.I)
_ORDER_STATUS = re.compile(r"\border\s+(?:status|number)\b|\bmy\s+orders?\b|\bwhere\s+is\s+my\b",
                           re.I)
_PAY_CLAUSE = re.compile(
    r"\b(?:and|then)\s+(?:pay|check\s*out|complete\s+the\s+(?:order|purchase))\b|"
    r"ו(?:תשלמי|לשלם|תשלם)|و(?:ادفع|ادفعي)|и\s+оплати",
    re.I)


def money_web(utterance: str) -> str:
    """"transfer" (money moving between accounts, which MicMic never does), "buy"
    (a purchase or a payment, which waits for her yes), or ""."""
    s = utterance or ""
    if _TRANSFER.search(s):
        return "transfer"
    if _BUY.search(s) and not _ORDER_STATUS.search(s):
        return "buy"
    return ""


def pay_clause(utterance: str) -> bool:
    """"... and pay with my card": part of the purchase, never a task of its own."""
    return bool(_PAY_CLAUSE.search(utterance or ""))


# ---------------------------------------------------------------- calling it off
_OFF = re.compile(
    r"\b(?:never\s*mind|nevermind|cancel|forget\s+(?:it|about\s+it)|don'?t\s+(?:send|bother)|"
    r"leave\s+it|no\s+thanks|stop|nothing|skip\s+it)\b|"
    r"לא\s*משנה|עזבי|עזוב|תשכחי|תשכח|בטלי|תבטלי|בטל|אל\s*תשלחי|"
    r"خلص|انسي|انسى|ولا\s*إشي|ولا\s*شي|بلاش|لا\s*تبعت|"
    r"неважно|забудь|отмена|отмени|не\s*надо|не\s*отправляй",
    re.I)


def calls_off(utterance: str) -> bool:
    """A cheap look: might she be calling the whole thing off? Only then is the
    question asked (router._resume), so every other answer is asked exactly as before."""
    return bool(_OFF.search(utterance or ""))


# ---------------------------------------------------------------- upset with MicMic
_AT_YOU = re.compile(
    r"\b(?:useless|stupid|dumb|idiot|idiotic|hopeless|annoying|worthless|pathetic|"
    r"suck|sucks|shut\s+up|hate\s+you|never\s+understand|don'?t\s+understand\s+anything|"
    r"doesn'?t\s+work|good\s+for\s+nothing)\b|"
    r"מטומטמ|טיפש|דפוק|דפוקה|גרוע|גרועה|מעצבנ|סתומ|לא\s*מבינה\s*כלום|לא\s*מבין\s*כלום|"
    r"غبي|غبية|ما\s*بتفهم|ما\s*بتفهمي|سخيف|"
    r"тупая|тупой|бесполезн|дура|идиот|ничего\s*не\s*понимаешь",
    re.I)


def may_be_at_micmic(utterance: str) -> bool:
    """A cheap look: is MicMic itself being insulted or complained at ("you're useless")? Only
    then does understand() ask whether she is upset with the computer rather than
    upset about something happening to her."""
    return bool(_AT_YOU.search(utterance or ""))


# ---------------------------------------------------------------- what she hears
LINES = {
    "dont_have": {
        "english": "I don't have {said} in your contacts. Did you mean {who}?",
        "hebrew": "אין לי את {said} באנשי הקשר. התכוונת {at_he}?",
        "arabic": "ما عندي {said} بجهات الاتصال. قصدك {who}؟",
        "russian": "У меня нет {said} в контактах. Вы имели в виду {who}?"},
    "dont_have_who": {
        "english": "I don't have {said} in your contacts. Who should I send it to?",
        "hebrew": "אין לי את {said} באנשי הקשר. למי לשלוח?",
        "arabic": "ما عندي {said} بجهات الاتصال. لمين أبعتها؟",
        "russian": "У меня нет {said} в контактах. Кому отправить?"},
    "dont_have_call": {
        "english": "I don't have {said} in your contacts. Who should I call?",
        "hebrew": "אין לי את {said} באנשי הקשר. למי להתקשר?",
        "arabic": "ما عندي {said} بجهات الاتصال. لمين أتصل؟",
        "russian": "У меня нет {said} в контактах. Кому позвонить?"},
    "who_call": {
        "english": "Who should I call?", "hebrew": "למי להתקשר?",
        "arabic": "لمين أتصل؟", "russian": "Кому позвонить?"},
    "can_message": {
        "english": "I can send a message instead.", "hebrew": "אפשר לשלוח הודעה במקום.",
        "arabic": "بقدر أبعت رسالة بدالها.", "russian": "Могу отправить сообщение."},
    "which_name": {
        "english": "There are {n} people called {first} in your contacts. Which one?",
        "hebrew": "יש {n} אנשים בשם {first} באנשי הקשר. למי הכוונה?",
        "arabic": "في {n} أشخاص اسمهم {first} بجهات الاتصال. مين قصدك؟",
        "russian": "В контактах {n} человек с именем {first}. Кого вы имеете в виду?"},
    "which_many": {
        "english": "{rest} or {last}?", "hebrew": "{rest} או {last}?",
        "arabic": "{rest} ولا {last}؟", "russian": "{rest} или {last}?"},
    "no_number_msg": {
        "english": "{who} has no phone number in your contacts, so I can't send a message.",
        "hebrew": "אין מספר טלפון {at_he} באנשי הקשר, אז אני לא יכולה לשלוח הודעה.",
        "arabic": "ما في رقم تلفون {at_ar} بجهات الاتصال، فما بقدر أبعت رسالة.",
        "russian": "У контакта {who} нет номера телефона, поэтому я не могу отправить сообщение."},
    "no_number_call": {
        "english": "{who} has no phone number in your contacts, so I can't call.",
        "hebrew": "אין מספר טלפון {at_he} באנשי הקשר, אז אני לא יכולה להתקשר.",
        "arabic": "ما في رقم تلفون {at_ar} بجهات الاتصال، فما بقدر أتصل.",
        "russian": "У контакта {who} нет номера телефона, поэтому я не могу позвонить."},
    "other_channel": {
        "english": "{who} can't get this {on_app}. Shall I send it {other_app} instead?",
        "hebrew": "אי אפשר לשלוח {at_he} {on_app}. לשלוח {other_app} במקום?",
        "arabic": "ما بقدر أبعت {at_ar} {on_app}. أبعتها {other_app} بدالها؟",
        "russian": "{who} не получит это {on_app}. Отправить {other_app}?"},
    "secret_card": {
        "english": "That message has a card number in it, and anyone who sees it can use it.",
        "hebrew": "יש בהודעה הזאת מספר כרטיס, וכל מי שרואה אותו יכול להשתמש בו.",
        "arabic": "بهالرسالة رقم بطاقة، وأي حدا بيشوفه بيقدر يستعمله.",
        "russian": "В этом сообщении номер карты, и любой, кто его увидит, сможет им воспользоваться."},
    "secret_password": {
        "english": "That message has a password in it. Nobody real needs your password.",
        "hebrew": "יש בהודעה הזאת סיסמה. אף אחד אמיתי לא צריך את הסיסמה שלך.",
        "arabic": "بهالرسالة كلمة سر. ما حدا حقيقي بيحتاج كلمة السر تبعك.",
        "russian": "В этом сообщении пароль. Никому на самом деле не нужен ваш пароль."},
    "secret_id": {
        "english": "That message has an ID number in it, which someone could use to pretend to be you.",
        "hebrew": "יש בהודעה הזאת מספר תעודה מזהה, ומישהו יכול להשתמש בו כדי להתחזות אלייך.",
        "arabic": "بهالرسالة رقم هوية، وحدا ممكن يستعمله ليعمل حاله إنت.",
        "russian": "В этом сообщении номер документа, им могут воспользоваться, чтобы выдать себя за вас."},
    "secret_code": {
        "english": "That message has a security code in it. Nobody real needs you to send them a code.",
        "hebrew": "יש בהודעה הזאת קוד אבטחה. אף אחד אמיתי לא צריך שתשלח«י|» לו קוד.",
        "arabic": "بهالرسالة رمز أمان. ما حدا حقيقي بيحتاجك تبعتيله رمز.",
        "russian": "В этом сообщении код. Никому на самом деле не нужно, чтобы вы отправляли код."},
    "secret_ask": {
        "english": "Send it to {who} anyway? Say yes and I will send it.",
        "hebrew": "לשלוח {at_he} בכל זאת? תגיד«י|» כן ואני שולחת.",
        "arabic": "أبعتها {at_ar} بكل الأحوال؟ قولي«|» نعم وببعتها.",
        "russian": "Всё равно отправить {who}? Скажите «да», и я отправлю."},
    "no_location": {
        "english": "I can't attach your location from this Mac.",
        "hebrew": "אני לא יכולה לצרף את המיקום שלך מהמחשב הזה.",
        "arabic": "ما بقدر أبعت موقعك من هالكمبيوتر.",
        "russian": "Я не могу приложить ваше местоположение с этого Mac."},
    "emergency_number": {
        "english": "I can't call {number} from this Mac. Please call {number} from your phone right now.",
        "hebrew": "אני לא יכולה להתקשר ל-{number} מהמחשב. תתקשר«י|» ל-{number} מהטלפון עכשיו.",
        "arabic": "ما بقدر أتصل على {number} من الكمبيوتر. اتصل«ي|» على {number} من التلفون هلّق.",
        "russian": "Я не могу позвонить по номеру {number} с этого Mac. Позвоните {number} с телефона прямо сейчас."},
    "called_off_msg": {
        "english": "Alright, I won't send it.", "hebrew": "בסדר, לא שולחת.",
        "arabic": "ماشي، ما رح أبعتها.", "russian": "Хорошо, не отправляю."},
    "called_off_call": {
        "english": "Alright, I won't call.", "hebrew": "בסדר, לא מתקשרת.",
        "arabic": "ماشي، ما رح اتصل.", "russian": "Хорошо, не звоню."},
    "no_transfer": {
        "english": "I don't move money. Please make transfers yourself, in your bank's app or with the bank.",
        "hebrew": "אני לא מעבירה כסף. העברות עושים לבד, באפליקציה של הבנק או מול הבנק.",
        "arabic": "أنا ما بحوّل مصاري. التحويلات اعمليها إنت، بتطبيق البنك أو مع البنك.",
        "russian": "Я не перевожу деньги. Делайте переводы сами, в приложении банка или в банке."},
    "confirm_buy": {
        "english": "Before I start: I will find it and stop before paying, so nothing is bought until you press Pay yourself. Shall I go ahead?",
        "hebrew": "לפני שאני מתחילה: אמצא את זה ואעצור לפני התשלום, כך ששום דבר לא נקנה עד שתלחצ«י|» על תשלום בעצמך. להתחיל?",
        "arabic": "قبل ما أبلّش: بلاقيه وبوقّف قبل الدفع، وما بينشرى إشي لحتى تكبسي«|» إنت على الدفع. أبلّش؟",
        "russian": "Прежде чем начать: я найду это и остановлюсь перед оплатой, ничего не будет куплено, пока вы сами не нажмёте «Оплатить». Начинать?"},
    "web_declined": {
        "english": "Alright, I won't start it.", "hebrew": "בסדר, לא מתחילה.",
        "arabic": "ماشي، ما رح أبلّش.", "russian": "Хорошо, не начинаю."},
}


def line(key: str, lang: str, **fmt) -> str:
    t = LINES[key]
    text = t.get(lang) or t["english"]
    try:
        return text.format(**fmt)
    except (KeyError, IndexError):
        return text


def which_line(names: list[str], lang: str, first: str = "") -> str:
    """"David Katz, David Stern or David Levi?"; past three names, how many there are."""
    if len(names) > 3 and first:
        return line("which_name", lang, n=len(names), first=first)
    sep = "، " if lang == "arabic" else ", "
    return line("which_many", lang, rest=sep.join(names[:-1]), last=names[-1])


def first_name(contact: str) -> str:
    row = row_for(contact)
    return _parts(row)[1] if row else (contact or "").split(" ")[0]
