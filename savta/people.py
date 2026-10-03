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


def sound_alike(said: str, limit: int = 3) -> list[str]:
    """The rows of her book whose name only sounds like the name she said ("Donna" is
    Dana Cohen and Dana Levi), for "I don't have Donna in your contacts. Did you mean
    ...?" when Jev itself found nobody. Rows she named exactly are not here (that is
    names_contact). Only rows a read of her book produced: when it could not be read
    there are none, and the caller says so (router._book_unreadable), never "I don't
    have Donna". Code only, no call; at most `limit` names, in book order."""
    w = (said or "").strip().lower()
    if not w or " " in w or w in _NOT_NAMES or w in _NOT_NAMES_HE:
        return []
    out = []
    for r in _rows():
        words, first = _parts(r)
        parts = {first, *words} - {""}
        if any(_same_spelling(w, p) for p in parts):
            continue
        if any(_sounds_like(w, p) for p in parts):
            out.append(r["name"])
            if len(out) >= limit:
                break
    return out


# A whole new request to message or call someone, said from its first word: "text Gal
# I'm on my way", "call Mom", "תשלחי לגל שאני בדרך", "напиши Гале".
_NEW_REQUEST = re.compile(
    r"^\W*(?:(?:hey\s+)?micmic\W+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:text|message|whatsapp|sms|imessage|call|ring|phone|dial|facetime|write\s+to"
    r"|send\s+(?:a\s+|an\s+)?(?:message|text|whatsapp|imessage)\s+to)\b"
    r"|^\W*(?:תשלח(?:י)?|תכתוב|תכתבי|תתקשר(?:י)?|תחייג(?:י)?)\s"
    r"|^\W*(?:ابعت(?:ي)?|ابعث(?:ي)?|اكتب(?:ي)?|اتصل(?:ي)?|رن(?:ي)?)\s"
    r"|^\W*(?:напиши(?:те)?|отправь(?:те)?|позвони(?:те)?|набери(?:те)?)\s",
    re.IGNORECASE)
_FILLER = _NOT_NAMES - {"late", "soon", "home", "way", "outside", "inside", "running",
                        "coming", "going", "see", "meet", "dinner", "today", "tonight",
                        "tomorrow", "morning", "evening", "night", "week", "minutes", "hours"}


def new_request_to(utterance: str, need: str, asked_about: str = "") -> str:
    """An answer to "what should it say?" or "who should I send it to?" that is really
    a new request ("text Gal I'm on my way" said while the question about Dana was
    open, seen over HTTP: Gal's words became a draft to Dana Levi). Returns the row of
    her book it names, or "" when her words read as an answer:
      * to "what": a message or call verb first, and a person of her book other than
        `asked_about` named exactly ("tell her Gal is coming" is still the message);
      * to "who": a verb first, a person named, and words of a message besides
        ("message Gal" and "send it to Gal" answer the question).
    When her book could not be read (contacts.unreadable) nobody in it can be named,
    so to "who" the name she says after the verb stands in for the row and is
    returned as said: her new words then go to that name without the book, never
    into the open request's draft. Code only, no model call; anything unsure is an
    answer, as before."""
    if need not in ("what", "who") or not _NEW_REQUEST.search(utterance or ""):
        return ""
    named, said = only_named(utterance)
    if not named and need == "who" and book.unreadable():
        named = said_name(utterance)
        said = {named.lower()} if named else set()
    if need == "what":
        # The row she named most fully ("text Dana Cohen ..." is Dana, not every Cohen);
        # a first name two rows share is still somebody else when Dana is not one of them.
        row = named or names_contact(utterance, exclude=asked_about)
        return row if row and row != asked_about else ""
    row = named
    if not row:
        return ""
    rest = [w for w in _words(utterance)
            if w not in said and w not in _FILLER and w not in _NOT_NAMES_HE
            and not any(v in said for v in _variants(w))]
    return row if rest else ""


# "Send it to 054-123-4567", "תשלחי ל-0541234567", "на номер +7...": a number she says
# as the recipient needs no book at all. Only right after "to"/"ל"/"на"/"على", so a
# number inside the message ("tell dana the code is 1234567") is never taken for it.
_NUM_TO = re.compile(
    r"(?:\b(?:to|for)\s+(?:the\s+)?(?:number\s+)?|(?<![\w])ל(?:מספר\s*)?-?\s*|"
    r"\bна\s+(?:номер\s+)?|(?:\bعلى|(?<![\w])ل)\s*(?:الرقم\s*)?)"
    r"(\+?\d[\d\s\-]{6,}\d)")


def said_number(utterance: str, region: str = "") -> str:
    """The phone number she named as the recipient, international ("+972541234567"),
    or "". A local number is made international only where her region says how
    (emergency_region: "IL", "US")."""
    m = _NUM_TO.search(utterance or "")
    if not m:
        return ""
    d = re.sub(r"[^\d+]", "", m.group(1))
    if d.startswith("00"):
        d = "+" + d[2:]
    if d.startswith("+"):
        return d if 9 <= len(d) <= 16 else ""
    if region == "IL" and d.startswith("0") and len(d) in (9, 10):
        return "+972" + d[1:]
    if region == "US" and len(d) == 10:
        return "+1" + d
    if region == "US" and len(d) == 11 and d.startswith("1"):
        return "+" + d
    return ""


def bare_name(utterance: str) -> str:
    """An answer that is only a name ("tamar", "tamar cohen", "לתמר"), or "". "Yes
    please", "you should have it on whatsapp" and the like are not names."""
    ws = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", utterance or "", re.UNICODE)
    if not 0 < len(ws) <= 3:
        return ""
    keep = []
    for w in ws:
        low = w.lower()
        if _HEB.search(w) and w.startswith("ל") and len(w) > 2 and w not in _NOT_NAMES_HE:
            w = w[1:]
        if low in _NOT_NAMES or w in _NOT_NAMES_HE or low in ("yeah", "sure", "כן", "לא",
                                                              "да", "нет", "نعم", "لا"):
            continue
        keep.append(w)
    return shown(" ".join(keep)) if 0 < len(keep) <= 2 else ""


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


# ---------------------------------------------------------------- an emergency, said
# Owner decision 2026-10-03: an emergency gets the number to dial from her phone first
# (by region), then her emergency contact after a short countdown she can stop. A bare
# "call 911" gets the number and an offer. Code checks on her words, so the request to
# Jev is unchanged and the emergency path does not ride on a score near its gate.
#
# Asking for the service by name: "call an ambulance", "get me the police", "תתקשרי
# למד"א". The kind is only which number to give in Israel; asking is not by itself
# a sign of an emergency (that is emergency_kind, below).
_SERVICE = (
    (re.compile(r"\bfire\s*(?:department|brigade|service|fighters?|men|trucks?)\b|"
                r"\bfirefighters?\b|מכבי\s*(?:ה)?אש|כבאים|כיבוי\s*אש|כבאות|"
                r"الإطفاء|الاطفاء|المطافي|إطفائية|пожарн", re.I), "fire"),
    (re.compile(r"\bpolice\b|\bcops\b|משטרה|למשטרה|الشرطة|الشرطه|البوليس|полици", re.I),
     "police"),
    (re.compile(r"\bambulance\b|\bparamedics?\b|אמבולנס|מד[\"״']?א|מגן\s*דוד|"
                r"الإسعاف|الاسعاف|إسعاف|اسعاف|скор(?:ую|ая)|неотложк", re.I), "medical"),
    (re.compile(r"\bemergency\s*(?:services?|number|line)\b|\bthe\s+emergency\b|"
                r"שירותי\s*(?:ה)?חירום|חירום|الطوارئ|طوارئ|экстренн|службу\s+спасения|"
                r"спасател", re.I), ""),
)


def asks_emergency_service(utterance: str) -> tuple[bool, str]:
    """(she asks to call an emergency service by name, which one: fire/police/medical/"")."""
    low = (utterance or "").lower()
    if not re.search(_CALL_VERB, low):
        return False, ""
    for rx, kind in _SERVICE:
        if rx.search(low):
            return True, kind
    return False, ""


# Words that show a real emergency is happening, not just a request for a number.
# Deliberately phrased ("there's a fire", "having a heart attack", "collapsed"), so
# "play fire by bts" or "the fire alarm test is tomorrow" are not emergencies.
_WHO_RX = (r"(?:some(?:one|body)|he|she|they|my\s+\w+|mom|mum|dad|grandma|grandpa|"
           r"granny|husband|wife|a\s+man|a\s+woman)")
_KIND = (
    ("fire", re.compile(
        r"\bthere(?:'|’)?s\s+a\s+fire\b|\bthere\s+is\s+a\s+fire\b|"
        r"\ba\s+fire\s+in\s+(?:the|my|our)\s+(?:house|home|kitchen|building|apartment|flat|room|garage)\b|"
        r"\b(?:house|home|kitchen|building|apartment|flat|car|room|stove|oven|pan)\s+(?:is\s+)?on\s+fire\b|"
        r"\bsomething(?:'|’)?s?\s+(?:is\s+)?burning\b|\bi\s+(?:can\s+)?smell\s+smoke\b|"
        r"\bfull\s+of\s+smoke\b|"
        r"יש\s+שריפה|שריפה\s+ב(?:בית|מטבח|בניין|דירה|חדר)|יש\s+אש\s+ב|עולה\s+באש|"
        r"מריח(?:ה)?\s+עשן|יש\s+עשן|"
        r"في\s+حريق|فيه\s+حريق|صار\s+حريق|حريقة\s+ب|عم\s+يحترق\s+البيت|ريحة\s+دخان|"
        r"у\s+нас\s+пожар|начался\s+пожар|пожар\s+в\s+(?:доме|квартире|кухне)|"
        r"пахнет\s+дымом|запах\s+дыма", re.I)),
    ("police", re.compile(
        r"\bsome(?:one|body)\s+(?:is\s+)?(?:breaking|broke|broken)\s+in(?:to)?\b|\bintruder\b|"
        r"\bburglar\b|\b(?:i'?m|i\s+am|i\s+was|i've\s+been)\s+(?:being\s+)?(?:robbed|attacked|mugged)\b|"
        r"\bsome(?:one|body)\s+is\s+(?:attacking|hurting)\b|\bhas\s+a\s+(?:gun|knife)\b|"
        r"פורץ\s+ל?בית|פורצים|פרצו\s+(?:לי|לבית|לדירה)|תוקפים\s+אותי|תוקף\s+אותי|"
        r"حرامي\s+بالبيت|في\s+حرامي|عم\s+يسرقوني|عم\s+يهاجمني|"
        r"кто-то\s+вломился|ко\s+мне\s+вломил|меня\s+грабят|на\s+меня\s+напал", re.I)),
    ("medical", re.compile(
        r"\bhaving\s+a\s+(?:heart\s+attack|stroke|seizure)\b|\b" + _WHO_RX +
        r"\s+(?:just\s+|has\s+|suddenly\s+)?(?:collapsed|fainted|passed\s+out)\b|"
        r"\bunconscious\b|\bnot\s+breathing\b|\bcan(?:'|’)?t\s+breathe\b|\bcannot\s+breathe\b|"
        r"\b(?:is|i'?m|he'?s|she'?s)\s+choking\b|"
        r"\bbleeding\s+(?:badly|heavily|a\s+lot|everywhere)\b|\bwon(?:'|’)?t\s+stop\s+bleeding\b|"
        r"\bchest\s+pains?\b|\boverdose\b|"
        r"\b(?:fell|fallen)\s+(?:down\s+)?and\s+(?:i\s+|he\s+|she\s+)?can(?:'|’|no)?t\s+get\s+up\b|"
        r"התקף\s+לב|(?:מישהו|מישהי|הוא|היא|אבא|אמא|סבא|סבתא|בעלי|אשתי)\s+(?:פתאום\s+)?"
        r"(?:התמוטט|התמוטטה|התעלף|התעלפה)|מחוסר(?:ת)?\s+הכרה|לא\s+נושם|לא\s+נושמת|"
        r"לא\s+יכול(?:ה)?\s+לנשום|נחנק|נחנקת|מדמם\s+הרבה|מדממת\s+הרבה|כאבים\s+בחזה|"
        r"נפלתי\s+ואני\s+לא\s+יכול(?:ה)?\s+לקום|"
        r"نوبة\s+قلبية|جلطة|أغمي\s+عليه|اغمي\s+عليه|أغمي\s+عليها|اغمي\s+عليها|مغمى\s+عليه|"
        r"ما\s+عم\s+(?:ي|ت)تنفس|ما\s+بقدر\s+(?:ا|أ)تنفس|وجع\s+بصدري|"
        r"инфаркт|сердечный\s+приступ|инсульт|потерял(?:а)?\s+сознание|без\s+сознания|"
        r"не\s+дышит|не\s+могу\s+дышать|истекает\s+кровью|упал(?:а)?\s+и\s+не\s+могу\s+встать",
        re.I)),
)


def emergency_kind(utterance: str) -> str:
    """"fire", "police" or "medical" when her words say an emergency is happening, else ""."""
    s = utterance or ""
    for kind, rx in _KIND:
        if rx.search(s):
            return kind
    return ""


# Stopping the countdown: "no", "stop", "cancel", "I'm fine", "don't call". Checked by
# code during the 3 seconds, never a model call: a stop has to be instant.
_STOP_CALL = re.compile(
    r"^\W*(?:no+|nope|stop|wait|cancel|don'?t|do\s+not|false\s+alarm|never\s*mind|"
    r"i'?m\s+(?:ok|okay|fine|alright|all\s+right)|i\s+am\s+(?:ok|okay|fine|alright))\b|"
    r"\b(?:stop|cancel|don'?t\s+call|do\s+not\s+call|false\s+alarm|i'?m\s+(?:ok|okay|fine)|"
    r"i\s+am\s+(?:ok|okay|fine))\b|"
    r"^\W*לא\b|עצרי|עצור|תעצרי|די\b|בטלי|תבטלי|ביטול|אל\s+תתקשרי|אל\s+תתקשר|אני\s+בסדר|רגע|"
    r"^\W*(?:لا|لأ)\b|وقفي|وقف|بلاش|خلص|ما\s+تتصلي|ما\s+تتصل|أنا\s+منيحة|انا\s+منيحة|"
    r"إلغاء|الغاء|"
    r"^\W*нет\b|стоп|отмена|отмени|не\s+звони|не\s+надо|я\s+в\s+порядке|подожди", re.I)
_GO_CALL = re.compile(
    r"^\W*(?:yes|yeah|yep|please|call\s+(?:her|him|them|now)|hurry|now|do\s+it|go\s+ahead)\b|"
    r"^\W*(?:כן|תתקשרי|תתקשר|מהר|עכשיו|בבקשה)|"
    r"^\W*(?:نعم|آه|اه|أيوه|ايوه|اتصلي|بسرعة|هلق|هلّق)|"
    r"^\W*(?:да|звони|позвони|быстрее|скорее|сейчас)", re.I)


def stops_call(utterance: str) -> bool:
    """During the countdown: is she stopping the call?"""
    return bool(_STOP_CALL.search(utterance or ""))


def wants_call_now(utterance: str) -> bool:
    """During the countdown: is she saying yes, call now?"""
    return bool(_GO_CALL.search(utterance or ""))


# Which emergency number to give: the profile's country when it is set, else the
# country of her city when it is one this table knows, else "" (911 and 112 both).
_EU112 = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
          "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
          "GB", "NO", "CH", "IS"}
_COUNTRY_NAMES = {
    "israel": "IL", "ישראל": "IL", "إسرائيل": "IL", "اسرائيل": "IL", "израиль": "IL",
    "united states": "US", "usa": "US", "us": "US", "america": "US", "ארצות הברית": "US",
    "сша": "US", "الولايات المتحدة": "US", "canada": "US",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "france": "FR", "germany": "DE",
    "spain": "ES", "italy": "IT", "netherlands": "NL", "belgium": "BE", "austria": "AT",
    "portugal": "PT", "greece": "GR", "ireland": "IE", "poland": "PL", "sweden": "SE",
    "denmark": "DK", "finland": "FI", "cyprus": "CY", "czechia": "CZ", "hungary": "HU",
    "romania": "RO", "switzerland": "CH", "norway": "NO", "germania": "DE",
}
_CITY_COUNTRY = {
    "IL": ("tel aviv", "jerusalem", "haifa", "beer sheva", "beersheba", "netanya", "ashdod",
           "rishon", "petah tikva", "holon", "ramat gan", "herzliya", "eilat", "nazareth",
           "kfar saba", "raanana", "rehovot", "bat yam", "ashkelon", "modiin", "hadera",
           "תל אביב", "ירושלים", "חיפה", "באר שבע", "נתניה", "אשדוד", "ראשון", "פתח תקווה",
           "חולון", "רמת גן", "הרצליה", "אילת", "נצרת", "כפר סבא", "רעננה", "רחובות",
           "בת ים", "אשקלון", "מודיעין", "חדרה",
           "تل أبيب", "تل ابيب", "القدس", "حيفا", "الناصرة", "يافا", "عكا", "أم الفحم",
           "тель-авив", "тель авив", "иерусалим", "хайфа", "беэр-шева", "нетания", "ашдод"),
    "US": ("new york", "los angeles", "chicago", "houston", "miami", "boston", "san francisco",
           "seattle", "washington", "philadelphia", "atlanta", "dallas", "austin", "denver",
           "нью-йорк", "ניו יורק"),
    "EU": ("london", "paris", "berlin", "madrid", "rome", "amsterdam", "vienna", "brussels",
           "lisbon", "athens", "dublin", "prague", "warsaw", "budapest", "stockholm",
           "copenhagen", "helsinki", "barcelona", "munich", "milan", "nicosia", "limassol",
           "лондон", "париж", "берлин"),
}


def emergency_region(profile: dict) -> str:
    """"IL", "US", "EU" or "" (unknown)."""
    c = str((profile or {}).get("country") or "").strip()
    if c:
        code = _COUNTRY_NAMES.get(c.lower(), c.upper() if len(c) == 2 else "")
        if code == "IL":
            return "IL"
        if code == "US":
            return "US"
        if code in _EU112:
            return "EU"
    city = str((profile or {}).get("city") or "").strip().lower()
    if city:
        for region, names in _CITY_COUNTRY.items():
            if any(city == n or city.startswith(n + " ") or city.startswith(n + ",")
                   for n in names):
                return region
    return ""


def emergency_line(lang: str, region: str, kind: str = "", asked: str = "") -> str:
    """The number to dial from her phone, now. `asked` is the number she named herself
    ("call 911"), which is the one she hears; otherwise her region's, by kind in Israel."""
    if asked:
        return line("emergency_number", lang, number=asked)
    if region == "IL":
        num = {"fire": "102", "police": "100"}.get(kind, "101")
        svc = LINES["emergency_il_" + ({"fire": "fire", "police": "police"}.get(kind, "medical"))]
        return line("emergency_il", lang, number=num, service=svc.get(lang) or svc["english"])
    if region == "US":
        return line("emergency_go", lang, number="911")
    if region == "EU":
        return line("emergency_go", lang, number="112")
    return line("emergency_unknown", lang)


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
# Owner decision 2026-10-03 (bench hard-web-001): book, reserve and purchase are
# purchase words too, like buy and order. Not a table, an appointment or a meeting (no
# money changes hands; the Hebrew תזמיני above already leaves those out), and not the
# noun ("read me a book", "my facebook").
_BOOK = re.compile(
    r"(?<!\ba\s)(?<!\ban\s)(?<!the\s)(?<!my\s)(?<!this\s)(?<!that\s)(?<!your\s)(?<!her\s)"
    r"(?<!his\s)(?<!our\s)"
    r"\b(?:book|reserve|purchase)\b(?:\s+(?:it|them|that|this|one)\b|\s+"
    r"(?!(?:me\s+)?(?:a\s+|an\s+|the\s+|my\s+|some\s+)?(?:table|tables|appointment|meeting|"
    r"slot|time|doctor|doctor's|dentist|haircut|call|massage|class|lesson|court|club|store|"
    r"shop|report|review|shelf|case|mark|seat\s+at\s+the\s+table)\b)\w+)|"
    r"תשריני|תשריין|לשריין|תרכשי|תרכוש|לרכוש|"
    r"احجز|احجزي|احجزيلي|إحجز|إحجزي|اشتريها|"
    r"забронируй|забронировать|бронируй|зарезервируй|зарезервировать|приобрети|приобрести",
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
    if (_BUY.search(s) or _BOOK.search(s)) and not _ORDER_STATUS.search(s):
        return "buy"
    return ""


def books(utterance: str) -> bool:
    """She asks to book or reserve (rather than buy): the question says "book"."""
    return bool(re.search(r"\b(?:book|reserve)\b|תזמיני|תשריני|לשריין|احجز|احجزي|احجزيلي|"
                          r"заброни|бронир|зарезерв", utterance or "", re.I))


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
    "dont_have_lead": {
        "english": "I don't have {said} in your contacts.",
        "hebrew": "אין לי את {said} באנשי הקשר.",
        "arabic": "ما عندي {said} بجهات الاتصال.",
        "russian": "У меня нет {said} в контактах."},
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
    # Her book could not be read (actions/contacts.unreadable). Never "I don't have
    # Tamar in your contacts" then: MicMic did not look (owner's fresh 1.2.4 install).
    "book_cant_see": {
        "english": "I can't see your contacts yet, so I can't find {said}.",
        "hebrew": "אני עוד לא רואה את אנשי הקשר שלך, אז אני לא מוצאת את {said}.",
        "arabic": "لسا ما بشوف جهات الاتصال تبعك، فما بقدر لاقي {said}.",
        "russian": "Я пока не вижу ваши контакты, поэтому не могу найти {said}."},
    "book_cant_see_any": {
        "english": "I can't see your contacts yet.",
        "hebrew": "אני עוד לא רואה את אנשי הקשר שלך.",
        "arabic": "لسا ما بشوف جهات الاتصال تبعك.",
        "russian": "Я пока не вижу ваши контакты."},
    "book_none": {
        "english": "I don't see anyone in your contacts, so I can't find {said}.",
        "hebrew": "אני לא רואה אף אחד באנשי הקשר שלך, אז אני לא מוצאת את {said}.",
        "arabic": "ما بشوف حدا بجهات الاتصال تبعك، فما بقدر لاقي {said}.",
        "russian": "Я не вижу никого в ваших контактах, поэтому не могу найти {said}."},
    "book_none_any": {
        "english": "I don't see anyone in your contacts.",
        "hebrew": "אני לא רואה אף אחד באנשי הקשר שלך.",
        "arabic": "ما بشوف حدا بجهات الاتصال تبعك.",
        "russian": "Я не вижу никого в ваших контактах."},
    "book_still": {
        "english": "I still can't see your contacts.",
        "hebrew": "אני עדיין לא רואה את אנשי הקשר שלך.",
        "arabic": "لسا ما بشوف جهات الاتصال تبعك.",
        "russian": "Я всё ещё не вижу ваши контакты."},
    "book_kept": {
        "english": "I kept your message.", "hebrew": "שמרתי את ההודעה.",
        "arabic": "خليت الرسالة محفوظة.", "russian": "Я сохранила сообщение."},
    "book_how_wa": {
        "english": "To let me, turn on MicMic in System Settings, Privacy and Security, Full Disk Access. Should I open it for you?",
        "hebrew": "כדי לאפשר את זה, צריך להפעיל את MicMic בהגדרות המערכת, פרטיות ואבטחה, גישה מלאה לדיסק. לפתוח לך את זה?",
        "arabic": "لتسمح«ي|» إلي، شغّل«ي|» MicMic بإعدادات النظام، الخصوصية والأمن، الوصول الكامل إلى القرص. بدك أفتحلك ياه؟",
        "russian": "Чтобы разрешить, включите MicMic в Системных настройках: Конфиденциальность и безопасность, Полный доступ к диску. Открыть их?"},
    "book_how_auto": {
        "english": "To let me, open System Settings, Privacy and Security, Automation, and turn on Contacts under MicMic. Should I open it for you?",
        "hebrew": "כדי לאפשר את זה, צריך לפתוח את הגדרות המערכת, פרטיות ואבטחה, אוטומציה, ולהפעיל את אנשי הקשר תחת MicMic. לפתוח לך את זה?",
        "arabic": "لتسمح«ي|» إلي، افتح«ي|» إعدادات النظام، الخصوصية والأمن، الأتمتة، وشغّل«ي|» جهات الاتصال تحت MicMic. بدك أفتحلك ياه؟",
        "russian": "Чтобы разрешить, откройте Системные настройки: Конфиденциальность и безопасность, Автоматизация, и включите Контакты у MicMic. Открыть их?"},
    "book_waiting": {
        "english": "macOS is still asking you about it. Answer it, then tell me the name again.",
        "hebrew": "macOS עדיין שואל אותך על זה. אחרי שתענ«י|», תגיד«י|» לי שוב את השם.",
        "arabic": "macOS لسا عم يسألك عن هالشي. جاوب«ي|» عليه، وبعدين قول«ي|» لي الاسم مرة تانية.",
        "russian": "macOS ещё спрашивает вас об этом. Ответьте, а потом снова назовите имя."},
    "book_not_asked": {
        "english": "Ask me again when you'd like me to look at your contacts.",
        "hebrew": "אפשר לבקש שוב כשתרצ«י|» שאסתכל באנשי הקשר.",
        "arabic": "اطلب«ي|» مني مرة تانية لما بدك إياني شوف جهات الاتصال.",
        "russian": "Попросите ещё раз, когда захотите, чтобы я посмотрела контакты."},
    # Her book could not be read, and MicMic still helps (owner, 2026-10-03: "even if
    # i dont give access to contact it should be able to open whatsapp and send no?").
    # WhatsApp is opened with her words ready; it is never said to be sent.
    "wa_ready_pick": {
        "english": "I can't see your contacts, so I opened WhatsApp with your message ready. Pick {who} and press send.",
        "hebrew": "אני לא רואה את אנשי הקשר שלך, אז פתחתי את וואטסאפ עם ההודעה מוכנה. תבחר«י|» את {who} ותלחצ«י|» שליחה.",
        "arabic": "ما بشوف جهات الاتصال تبعك، ففتحت واتساب والرسالة جاهزة. اختار«ي|» {who} واكبس«ي|» إرسال.",
        "russian": "Я не вижу ваши контакты, поэтому открыла WhatsApp с готовым сообщением. Выберите {who} и нажмите «Отправить»."},
    "wa_ready_open": {
        "english": "WhatsApp is open with your message ready. Pick {who} and press send.",
        "hebrew": "וואטסאפ פתוח עם ההודעה מוכנה. תבחר«י|» את {who} ותלחצ«י|» שליחה.",
        "arabic": "واتساب مفتوح والرسالة جاهزة. اختار«ي|» {who} واكبس«ي|» إرسال.",
        "russian": "WhatsApp открыт, сообщение готово. Выберите {who} и нажмите «Отправить»."},
    # mac.whatsapp() on a number: that very chat is open with her words in it, and
    # nothing was pressed (it was not sure WhatsApp was in front).
    "wa_ready_number": {
        "english": "Your message to {who} is ready in WhatsApp. Press Return to send it.",
        "hebrew": "ההודעה {at_he} מוכנה בוואטסאפ. «לחצי|לחץ» Enter כדי לשלוח.",
        "arabic": "الرسالة {at_ar} جاهزة بواتساب. «اكبسي|اكبس» Enter لتبعتها.",
        "russian": "Сообщение для контакта {who} готово в WhatsApp. Нажмите Return, чтобы отправить."},
    "wa_ready_chat": {
        "english": "Your message is ready in WhatsApp. Check that it's {who}, then press send.",
        "hebrew": "ההודעה מוכנה בוואטסאפ. «תבדקי|תבדוק» שזה {who}, ואז תלחצ«י|» שליחה.",
        "arabic": "الرسالة جاهزة بواتساب. تأكد«ي|» إنه {who}، وبعدين اكبس«ي|» إرسال.",
        "russian": "Сообщение готово в WhatsApp. Проверьте, что это {who}, и нажмите «Отправить»."},
    "pane_opened": {
        "english": "I opened it. Turn on MicMic there, then tell me the name again.",
        "hebrew": "פתחתי. תפעיל«י|» שם את MicMic, ואז תגיד«י|» לי שוב את השם.",
        "arabic": "فتحته. شغّل«ي|» MicMic هناك، وبعدين قول«ي|» لي الاسم مرة تانية.",
        "russian": "Открыла. Включите там MicMic, а потом снова назовите имя."},
    # Asked who twice and still no name she meant: what is wrong, never a third
    # "Who should I send it to?" (owner's 1.2.4 session asked it five times).
    "who_explain": {
        "english": "I still can't tell who you mean. I can only send to people in your contacts, so say the name as it is saved there, or say never mind.",
        "hebrew": "אני עדיין לא מבינה למי הכוונה. אני יכולה לשלוח רק למי שנמצא באנשי הקשר, אז תגיד«י|» את השם כמו שהוא שמור שם, או תגיד«י|» עזבי.",
        "arabic": "لسا مش فاهمة مين قصدك. بقدر أبعت بس للي بجهات الاتصال، فقول«ي|» الاسم متل ما هو محفوظ، أو قول«ي|» خلص.",
        "russian": "Я всё ещё не понимаю, кого вы имеете в виду. Я могу отправлять только тем, кто есть в контактах: назовите имя так, как оно там записано, или скажите «неважно»."},
    "who_explain_call": {
        "english": "I still can't tell who you mean. I can only call people in your contacts, so say the name as it is saved there, or say never mind.",
        "hebrew": "אני עדיין לא מבינה למי הכוונה. אני יכולה להתקשר רק למי שנמצא באנשי הקשר, אז תגיד«י|» את השם כמו שהוא שמור שם, או תגיד«י|» עזבי.",
        "arabic": "لسا مش فاهمة مين قصدك. بقدر أتصل بس باللي بجهات الاتصال، فقول«ي|» الاسم متل ما هو محفوظ، أو قول«ي|» خلص.",
        "russian": "Я всё ещё не понимаю, кого вы имеете в виду. Я могу звонить только тем, кто есть в контактах: назовите имя так, как оно там записано, или скажите «неважно»."},
    "leave_it": {
        "english": "Alright, I'll leave it for now. Ask me again whenever you like.",
        "hebrew": "בסדר, אני משאירה את זה כרגע. אפשר לבקש שוב מתי שתרצ«י|».",
        "arabic": "ماشي، بتركها هلق. اطلب«ي|» مني مرة تانية وقت ما بدك.",
        "russian": "Хорошо, пока оставлю это. Попросите снова, когда захотите."},
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
    # An emergency (owner decision 2026-10-03): the number first, then her contact.
    "emergency_go": {
        "english": "Call {number} from your phone now.",
        "hebrew": "תתקשר«י|» עכשיו ל-{number} מהטלפון.",
        "arabic": "اتصل«ي|» هلّق على {number} من التلفون.",
        "russian": "Позвоните {number} с телефона прямо сейчас."},
    "emergency_unknown": {
        "english": "Call the emergency number from your phone now: 911 in the US, 112 in Europe.",
        "hebrew": "תתקשר«י|» עכשיו מהטלפון למספר החירום: 911 בארצות הברית, 112 באירופה.",
        "arabic": "اتصل«ي|» هلّق من التلفون برقم الطوارئ: 911 بأمريكا، 112 بأوروبا.",
        "russian": "Позвоните с телефона в экстренную службу прямо сейчас: 911 в США, 112 в Европе."},
    "emergency_il": {
        "english": "Call {number} for {service} from your phone now.",
        "hebrew": "תתקשר«י|» עכשיו ל-{number}, {service}, מהטלפון.",
        "arabic": "اتصل«ي|» هلّق على {number}، {service}، من التلفون.",
        "russian": "Позвоните {number}, {service}, с телефона прямо сейчас."},
    "emergency_il_fire": {
        "english": "the fire service", "hebrew": "כיבוי אש",
        "arabic": "الإطفاء", "russian": "пожарная служба"},
    "emergency_il_police": {
        "english": "the police", "hebrew": "משטרה",
        "arabic": "الشرطة", "russian": "полиция"},
    "emergency_il_medical": {
        "english": "an ambulance", "hebrew": "מד״א",
        "arabic": "الإسعاف", "russian": "скорая помощь"},
    "emergency_countdown": {
        "english": "I will call {who} in 3 seconds. Say stop to cancel.",
        "hebrew": "בעוד 3 שניות אני מתקשרת {at_he}. «תגידי עצרי|תגיד עצור» כדי לבטל.",
        "arabic": "بعد 3 ثواني بتصل {at_ar}. «قولي وقفي|قول وقف» لتلغي.",
        "russian": "Через 3 секунды я позвоню {who}. Скажите «стоп», чтобы отменить."},
    "emergency_offer": {
        "english": "Shall I call {who}, your emergency contact?",
        "hebrew": "שאתקשר {at_he}, איש הקשר שלך לחירום?",
        "arabic": "بتصل {at_ar}، جهة الاتصال للطوارئ؟",
        "russian": "Позвонить {who}, вашему контакту для экстренных случаев?"},
    "emergency_no_contact": {
        "english": "You have no emergency contact yet. To set one, say: my emergency contact is, and their name.",
        "hebrew": "עוד אין לך איש קשר לחירום. כדי לקבוע, תגיד«י|»: איש הקשר שלי לחירום הוא, ואת השם.",
        "arabic": "لسا ما عندك جهة اتصال للطوارئ. لتحددي«|» وحدة، قول«ي|»: جهة الاتصال للطوارئ هي، والاسم.",
        "russian": "У вас пока нет контакта для экстренных случаев. Чтобы его задать, скажите: мой экстренный контакт, и имя."},
    "emergency_stopped": {
        "english": "Alright, I will not call {who}.",
        "hebrew": "בסדר, אני לא מתקשרת {at_he}.",
        "arabic": "ماشي، ما رح اتصل {at_ar}.",
        "russian": "Хорошо, я не буду звонить {who}."},
    # Book, reserve, buy after a search (bench hard-web-001): it searches, then asks.
    "find_then_book": {
        "english": "I am searching for it now, without booking anything. Shall I go ahead and book? I will stop before paying.",
        "hebrew": "אני מחפשת את זה עכשיו, בלי להזמין כלום. להמשיך ולהזמין? אעצור לפני התשלום.",
        "arabic": "عم دوّر عليه هلّق، بدون ما أحجز إشي. بكمّل وبحجز؟ بوقّف قبل الدفع.",
        "russian": "Ищу сейчас, ничего не бронирую. Продолжить и забронировать? Я остановлюсь перед оплатой."},
    "find_then_buy": {
        "english": "I am searching for it now, without buying anything. Shall I go ahead and buy it? I will stop before paying.",
        "hebrew": "אני מחפשת את זה עכשיו, בלי לקנות כלום. להמשיך ולקנות? אעצור לפני התשלום.",
        "arabic": "عم دوّر عليه هلّق، بدون ما أشتري إشي. بكمّل وبشتريه؟ بوقّف قبل الدفع.",
        "russian": "Ищу сейчас, ничего не покупаю. Продолжить и купить? Я остановлюсь перед оплатой."},
    "ask_book": {
        "english": "Shall I go ahead and book? I will stop before paying.",
        "hebrew": "להמשיך ולהזמין? אעצור לפני התשלום.",
        "arabic": "بكمّل وبحجز؟ بوقّف قبل الدفع.",
        "russian": "Продолжить и забронировать? Я остановлюсь перед оплатой."},
    "ask_buy": {
        "english": "Shall I go ahead and buy it? I will stop before paying.",
        "hebrew": "להמשיך ולקנות? אעצור לפני התשלום.",
        "arabic": "بكمّل وبشتريه؟ بوقّف قبل الدفع.",
        "russian": "Продолжить и купить? Я остановлюсь перед оплатой."},
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


# The System Settings pane each reason points at (savta/onboarding_api.py PANES).
BOOK_PANES = {"whatsapp": "full_disk", "automation": "automation"}


def book_line(reason: str, lang: str, said: str = "", kept: bool = False,
              still: bool = False) -> str:
    """What she hears when her book could not be read (contacts.unreadable): that
    MicMic cannot see it, that her message is kept, and how to allow it. `still`: she
    has already been told once, so the first sentence is shorter."""
    if still:
        parts = [line("book_still", lang)]
    elif reason == "none":
        parts = [line("book_none" if said else "book_none_any", lang, said=said)]
    else:
        parts = [line("book_cant_see" if said else "book_cant_see_any", lang, said=said)]
    if kept:
        parts.append(line("book_kept", lang))
    how = {"whatsapp": "book_how_wa", "automation": "book_how_auto",
           "waiting": "book_waiting", "not_asked": "book_not_asked"}.get(reason)
    if how:
        parts.append(line(how, lang))
    return " ".join(parts)


def which_line(names: list[str], lang: str, first: str = "") -> str:
    """"David Katz, David Stern or David Levi?"; past three names, how many there are."""
    if len(names) > 3 and first:
        return line("which_name", lang, n=len(names), first=first)
    sep = "، " if lang == "arabic" else ", "
    return line("which_many", lang, rest=sep.join(names[:-1]), last=names[-1])


def first_name(contact: str) -> str:
    row = row_for(contact)
    return _parts(row)[1] if row else (contact or "").split(" ")[0]
