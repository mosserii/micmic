"""A booking read off her screen: the airline's "Time to check in" email in Apple Mail
(or any window), for "check me in".

Code proposes, Jev disposes, as everywhere else: the window's own words are cut into
candidate spans by shape (a six-character code, a name in capitals, a flight number, a
date) and ONE Jev request picks which span is which. Nothing is written by a model, so
a code that is not on the screen can never come back. Where the "Check in" link goes
is read from the window itself (the link's own address), never guessed.

Seams, so a test needs no screen and no Jev: read_window(reader) takes anything with
front() and read_document(front) (guide.LiveScreen has both), and extract(j, text)
takes any Jev-shaped object.
"""
from __future__ import annotations

import re

from ..jev import choice, noul

# The email, as the router sees it: words that make it a check-in email.
_CHECKIN = re.compile(r"check.?in|הצ'?ק.?אין|تسجيل الوصول|регистраци", re.I)
_BOOKING = re.compile(r"confirmation|booking (?:code|ref|reference|number)|record locator|"
                      r"reservation (?:code|number)|\bPNR\b|קוד הזמנה|رقم الحجز|код бронирования",
                      re.I)
_FLIGHT = re.compile(r"\bflight\b|טיסה|رحلة|рейс", re.I)
# "Check me in", "check in for this flight", "do my check-in", in her four languages.
CHECK_ME_IN = re.compile(
    r"\bcheck (?:me|us|her|him) in\b|\bcheck[- ]?in for (?:this|my|the|our) flight\b|"
    r"\bdo (?:my|the|our) check[- ]?in\b|\bcheck[- ]?in (?:to|on|for) (?:this|my) flight\b|"
    r"(?:תעש(?:י|ה)|תבצע(?:י)?)(?: לי)? צ'?ק.?אין|"
    r"سجّ?ل(?:ني|ي|لي)? ?(?:لي )?(?:دخول|وصول|على الرحلة)|تسجيل (?:الوصول|الدخول) (?:للرحلة|لرحلتي)|"
    r"(?:اعمل|اعملي|اعمللي|اعمليلي)\s*(?:لي\s*)?(?:تشيك|تشك)\s*(?:ان|إن|اين)|"
    r"зарегистрируй(?:те)? меня|пройди(?:те)? регистрацию", re.I)

# Shapes. A booking code: 5 to 8 capitals and digits (Larkline K7QM2P, most airlines
# six). A flight: two letters or a letter and a digit, then 1 to 4 digits ("LL 482").
_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9]{5,8}(?![A-Za-z0-9])")
_FLIGHT_NO = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9] ?\d{1,4}(?![A-Za-z0-9])")
_NAME_RUN = re.compile(r"(?<![\w])[A-Z][a-z]+(?:-[A-Z][a-z]+)?(?: [A-Z][a-z]+(?:-[A-Z][a-z]+)?)+(?![\w])")
# Capitalised words an airline's email is full of that are never part of a name.
_NOT_NAMES = {"dear", "hi", "hello", "time", "check", "checkin", "check-in", "flight", "passenger",
              "passengers", "date", "departs", "arrives", "seat", "terminal", "gate", "boarding",
              "manage", "booking", "your", "the", "confirmation", "number", "economy", "business",
              "aircraft", "airbus", "boeing", "new", "york", "airport", "international", "status"}
_NAME_CAPS = re.compile(r"(?<![\w])([A-Z]{2,}(?: ?/ ?| )[A-Z]{2,}(?: [A-Z]{2,})?)(?![\w])")
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|"
           "December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
_DATE = re.compile(
    rf"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,? )?(?:\d{{1,2}} (?:{_MONTHS})\.?(?: \d{{4}})?|"
    rf"(?:{_MONTHS})\.? \d{{1,2}}(?:, \d{{4}})?|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}})")
# Words in capitals that are never a code: what an email shouts.
_NOT_CODES = {"LISBON", "LARKLINE", "BOARDING", "ECONOMY", "CHECKIN", "FLIGHT", "TICKET",
              "PASSENGER", "DEPARTS", "ARRIVES", "TERMINAL", "GATE", "SEAT", "MANAGE",
              "BOOKING", "TRAVEL", "AIRLINE", "AIRLINES", "NONSTOP", "DIRECT"}
MAX_CANDIDATES = 60
# What Jev reads: the part of the window around the booking (a webmail tab holds the
# inbox list too), at most this long.
FOCUS_BEFORE, FOCUS_CHARS = 600, 2500

QUESTIONS = {
    "confirmation_code": "Which of these is the booking's confirmation code (also called the "
                         "booking reference, record locator or PNR), the code the airline asks "
                         "for at check-in?",
    "passenger_name": "Which of these is the name of the passenger being checked in (one "
                      "person's name as written in the email)?",
    "flight_number": "Which of these is the flight number of the flight to check in for?",
    "flight_date": "Which of these is the date of that flight's departure?",
}
GATE = 0.5


def looks_like_checkin(text: str) -> bool:
    """A check-in email (or page) is in front of her: it speaks of checking in, of a
    flight, and of a booking code."""
    t = text or ""
    return bool(_CHECKIN.search(t) and _BOOKING.search(t) and _FLIGHT.search(t))


def candidates(text: str) -> dict[str, list[str]]:
    """Spans of the window's own words that could be each field, by shape alone."""
    t = re.sub(r"\s+", " ", text or "")

    def uniq(xs):
        seen, out = set(), []
        for x in xs:
            x = x.strip(" .,")
            if x and x.lower() not in seen:
                seen.add(x.lower())
                out.append(x)
        return out[:MAX_CANDIDATES]
    # A page's own example ("For example, ABC123") is never her code.
    examples = {m.group(1) for m in re.finditer(
        r"(?:example|e\.g\.|such as|like|לדוגמה|مثلاً|مثلا|например)[:,]?\s+([A-Z0-9]{5,8})\b", t, re.I)}
    codes = [c for c in _CODE.findall(t) if c not in _NOT_CODES and c not in examples
             and not c.isdigit() and not re.fullmatch(r"[A-Z]{2}\d{1,4}", c)]
    # Names: every two or three capitalised words in a row, so "Passenger: Alex Rivera
    # Flight: LL 482" still offers "Alex Rivera"; Jev picks which one is the passenger.
    names = []
    for m in _NAME_RUN.finditer(t):
        words = re.split(r"[ ]", m.group(0))
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                part = words[i:i + n]
                if not any(w.lower() in _NOT_NAMES for w in part):
                    names.append(" ".join(part))
    names += _NAME_CAPS.findall(t)
    return {"confirmation_code": uniq(codes),
            "passenger_name": uniq(names),
            "flight_number": uniq(_FLIGHT_NO.findall(t)),
            "flight_date": uniq(_DATE.findall(t))}


def focus(text: str) -> str:
    """The part of the window's words around the booking: from a little before the
    first word of a booking code or a check-in, FOCUS_CHARS long."""
    t = re.sub(r"\s+", " ", text or "")
    m = _BOOKING.search(t) or _CHECKIN.search(t)
    start = max(0, m.start() - FOCUS_BEFORE) if m else 0
    return t[start:start + FOCUS_CHARS]


def extract(j, text: str) -> dict:
    """{"confirmation_code", "passenger_name", "first_name", "last_name", "flight_number",
    "flight_date"} as found in `text` (each only when Jev picked one of the window's
    own spans with confidence), plus "confidence" per field. One Jev request."""
    text = focus(text)
    cands = candidates(text)
    qs = {}
    for key, ask in QUESTIONS.items():
        if not cands[key]:
            continue
        opts = {c: None for c in cands[key]}
        opts["__none__"] = "None of these is it."
        qs[key] = {"type": "choice", "instructions": ask, "criteria": opts}
    out: dict = {"confidence": {}}
    if not qs:
        return out
    qs["is_a_booking"] = {"type": "noul",
                          "instructions": "This text is an airline's message or page about one "
                                          "booking of a flight that can be checked in for"}
    a = j.ask({"email": text}, qs)
    if noul(a, "is_a_booking") < 0.4:
        return out
    for key in qs:
        if key == "is_a_booking":
            continue
        pick, conf, _ = choice(a, key)
        out["confidence"][key] = round(conf, 2)
        if pick and pick != "__none__" and pick in cands[key] and conf >= GATE:
            out[key] = pick
    name = out.get("passenger_name")
    if name:
        if "/" in name:                      # RIVERA/ALEX, the ticket's own order
            last, first = [p.strip() for p in name.split("/", 1)]
        else:
            parts = name.split()
            first, last = parts[0], parts[-1]
        out["first_name"], out["last_name"] = first.title(), last.title()
    return out


def known(b: dict) -> dict:
    """The guide's known values ({key: {"value", "source", "confidence"}}) from extract()."""
    conf = b.get("confidence") or {}
    out = {}
    for key in ("confirmation_code", "passenger_name", "first_name", "last_name",
                "flight_number", "flight_date"):
        if b.get(key):
            src = "passenger_name" if key in ("first_name", "last_name") else key
            out[key] = {"value": b[key], "source": "email", "confidence": conf.get(src, 0.0)}
    return out


def checkin_link(links: list[tuple[str, str]]) -> str:
    """The address behind the window's "Check in" link (or button), or ""."""
    best = ""
    for label, url in links or []:
        if not re.match(r"https?://", url or ""):
            continue
        if re.search(r"\bcheck[- ]?in\b", label or "", re.I):
            if re.search(r"check[- ]?in now|online check[- ]?in|^check[- ]?in$", label.strip(), re.I):
                return url
            best = best or url
    return best


def read_window(reader) -> dict | None:
    """{"app", "bundle_id", "front", "text", "links"} of the window in front, read
    through Accessibility (no AppleScript, so no Automation prompt), or None."""
    try:
        front = reader.front()
        if not front:
            return None
        doc = reader.read_document(front)
    except Exception:  # noqa: BLE001
        return None
    if not doc or doc.get("error"):
        return None
    return {"app": front.get("app", ""), "bundle_id": front.get("bundle_id", ""),
            "front": front, "text": doc.get("text", ""), "links": doc.get("links") or []}
