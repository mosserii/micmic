#!/usr/bin/env python3
""""Check me in" from the airline's email (1.2.6): the booking read off the email, the
email's own Check in link opened, the form filled with what MicMic knows, her passport
details asked once and typed only after her words and a read-back, and the button that
finishes it pressed only on her yes. Fake screens, a scripted Jev, a scripted model, a
fake voice: no window, no Accessibility, no network, $0.

    cd <worktree> && uv run python tests/test_checkin_email.py

What is held here:
  * booking.py: the demo email is a check-in email; the code, the passenger, the flight
    and its date are offered to Jev as the window's own words, one request picks them,
    and nothing that is not on the screen can come back (a made-up code, a low
    confidence, "not a booking", the page's own example "ABC123");
  * her details: what she says is read as a passport number, an expiry date and a date
    of birth, in her four languages, and nothing is made up;
  * the guide's autofill: the code and her name typed without a question; her passport
    details asked for once per page, typed only after her words and her yes, never
    guessed; a password, a card or a code never typed; a payment never pressed; the
    button that finishes it pressed only after "Shall I press ...?" and her yes;
  * the router: "check me in" with the email in front reads it, opens its link and
    starts the guide, in two Jev requests; with nothing in front it says what to open;
    never the browser agent.
"""
from __future__ import annotations

import datetime
import email
import os
import re
import sys
from email import policy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_guide as TG  # noqa: E402  (scratch state dir, offline model endpoint)
from test_guide import FakeJev, check, until  # noqa: E402

from savta.actions import booking  # noqa: E402
from savta.actions import guide as G  # noqa: E402
from savta.actions import mac  # noqa: E402

DEMO = Path(os.environ.get("LARKLINE_DIR")
            or Path.home() / "jev" / "micmic-work" / "launch" / "demo-airline")
EML = DEMO / "email" / "larkline-checkin.eml"
WIN = TG.WIN
TRUTH = {"confirmation_code": "K7QM2P", "passenger_name": "Alex Rivera", "flight_number": "LL 482"}


def email_parts():
    m = email.message_from_bytes(EML.read_bytes(), policy=policy.default)
    return m, m.get_body(("plain",)).get_content(), m.get_body(("html",)).get_content()


def email_doc():
    """The email as read_document() gives it: its words and its links."""
    _m, text, html = email_parts()
    links = [(re.sub(r"<[^>]+>", "", label).strip(), url)
             for url, label in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)]
    return text, links


class BookingJev(FakeJev):
    """Answers the booking questions with the true span (or an override), and
    everything else as FakeJev."""

    def __init__(self, picks=None, booking_noul=0.97, conf=0.95):
        super().__init__()
        self.picks = dict(TRUTH if picks is None else picks)
        self.booking_noul, self.conf = booking_noul, conf
        self.booking_requests = []

    def ask(self, state, qs):
        if "is_a_booking" not in qs:
            return super().ask(state, qs)
        self.calls += 1
        self.booking_requests.append((state, qs))
        out = {"is_a_booking": {"noul": self.booking_noul}}
        for key, q in qs.items():
            if key == "is_a_booking":
                continue
            opts = list(q["criteria"])
            want = self.picks.get(key)
            pick = next((o for o in opts if want and o.lower() == want.lower()), None)
            if pick is None and want and want not in opts and key in self.picks and want != "__first__":
                pick = want                     # a pick that is not one of the options
            if pick is None:
                pick = opts[0] if want == "__first__" else "__none__"
            out[key] = {"choice": pick, "confidence": self.conf,
                        "probabilities": {o: (self.conf if o == pick else 0.0) for o in opts}}
        return out


# ---------------------------------------------------------------- the demo email
def t_demo_email():
    check("the demo email exists", EML.exists(), str(EML))
    if not EML.exists():
        return
    m, text, html = email_parts()
    check("it is an email with a plain text part and an HTML part",
          m.get_content_type() == "multipart/alternative"
          and [p.get_content_type() for p in m.iter_parts()] == ["text/plain", "text/html"],
          [p.get_content_type() for p in m.iter_parts()])
    check("from Larkline, about checking in for Lisbon",
          "Larkline" in m["From"] and m["Subject"] == "Time to check in for your flight to Lisbon",
          (m["From"], m["Subject"]))
    check("its Check in link goes to the local demo site",
          'href="http://127.0.0.1:8851/#/check-in"' in html and "Check in: http://127.0.0.1:8851/#/check-in" in text)
    check("the booking is in both parts", all(x in text and x in html for x in ("K7QM2P", "Alex Rivera", "LL 482")))
    real = re.compile(r"\b(?:Delta|United Airlines|American Airlines|TAP Air|Lufthansa|British Airways|"
                      r"Emirates|El Al|Ryanair|easyJet|KLM|Air France|JetBlue|Iberia|Southwest)\b", re.I)
    check("no real airline is named", not real.search(text + html), real.findall(text + html))
    check("no em or en dashes in what she reads", not re.search("[–—]", text + html))
    check("nothing marks it a draft to send", "X-Unsent" not in str(m))


# ---------------------------------------------------------------- reading the booking
def t_booking_read():
    text, links = email_doc()
    check("the email is a check-in email", booking.looks_like_checkin(text))
    check("a receipt is not", not booking.looks_like_checkin("Your Spotify receipt. Total $9.99. Thanks!"))
    c = booking.candidates(text)
    check("the code, the passenger, the flight and its date are offered as the email's own words",
          "K7QM2P" in c["confirmation_code"] and "Alex Rivera" in c["passenger_name"]
          and "LL 482" in c["flight_number"] and any("Oct 13" in d for d in c["flight_date"]), c)
    page = "Check in for your flight | Confirmation number | For example, ABC123 | Last name"
    check("a page's own example code is never offered", "ABC123" not in booking.candidates(page)["confirmation_code"])

    j = BookingJev()
    b = booking.extract(j, text)
    check("one Jev request reads the booking", j.calls == 1 and len(j.booking_requests) == 1, j.calls)
    check("the code, the passenger, the flight",
          (b.get("confirmation_code"), b.get("first_name"), b.get("last_name"), b.get("flight_number"))
          == ("K7QM2P", "Alex", "Rivera", "LL 482"), b)
    state, qs = j.booking_requests[0]
    check("Jev is sent the email's words and nothing else", list(state) == ["email"] and "K7QM2P" in state["email"],
          list(state))
    check("every option is a span of the email, or none of them",
          all(o == "__none__" or o in state["email"] for k, q in qs.items() if q["type"] == "choice"
              for o in q["criteria"]), qs)
    k = booking.known(b)
    check("known values carry their source", k["confirmation_code"] == {
        "value": "K7QM2P", "source": "email", "confidence": 0.95}, k)

    b = booking.extract(BookingJev(picks={**TRUTH, "confirmation_code": "K7QM2B"}), text)
    check("a code that is not on the screen never comes back", "confirmation_code" not in b, b)
    b = booking.extract(BookingJev(conf=0.3), text)
    check("an unsure pick is not taken", "confirmation_code" not in b, b)
    b = booking.extract(BookingJev(picks={**TRUTH, "confirmation_code": "__none__"}), text)
    check("'none of these' is none", "confirmation_code" not in b and b.get("last_name") == "Rivera", b)
    b = booking.extract(BookingJev(booking_noul=0.1), text)
    check("not a booking: nothing at all", set(b) == {"confidence"}, b)
    caps = "Your booking confirmation K7QM2P. Passenger RIVERA/ALEX. Flight LL 482. Check in now."
    b = booking.extract(BookingJev(picks={**TRUTH, "passenger_name": "RIVERA/ALEX"}), caps)
    check("a ticket's RIVERA/ALEX is last name then first", (b.get("first_name"), b.get("last_name")) == ("Alex", "Rivera"), b)

    check("the Check in link is the one taken", booking.checkin_link(links) == "http://127.0.0.1:8851/#/check-in", links)
    check("never a link that is not a web address", booking.checkin_link([("Check in", "javascript:x()")]) == "")
    check("no check-in link: none", booking.checkin_link([("Manage booking", "https://x/m")]) == "")

    for said in ("Hey MicMic, check me in", "check me in please", "check in for this flight",
                 "can you do my check-in", "check us in", "תעשי לי צ'ק אין", "תעשה צ'ק-אין לטיסה",
                 "سجّلني دخول", "اعمللي تشيك ان", "зарегистрируй меня на рейс", "пройди регистрацию"):
        check(f"'{said}' asks for a check-in", bool(booking.CHECK_ME_IN.search(said)))
    for said in ("check in on my mom", "what does check in mean", "check in with Dana tomorrow",
                 "play Check Yes Juliet"):
        check(f"'{said}' does not", not booking.CHECK_ME_IN.search(said))


# ---------------------------------------------------------------- her details
def t_her_details():
    today = datetime.date(2026, 10, 12)
    pv = lambda t, lang="english": G.personal_values(t, lang, today=today)  # noqa: E731
    d = datetime.date
    cases = [
        ("My passport number is P 0 0 4 2 8 1 1 7 and it expires March 14th, 2031", "english",
         {"passport_number": "P00428117", "passport_expiry": d(2031, 3, 14)}),
        ("P00428117, expiry 03/14/2031", "english", {"passport_number": "P00428117", "passport_expiry": d(2031, 3, 14)}),
        ("passport papa zero zero four two eight one one seven", "english", {"passport_number": "P00428117"}),
        ("it expires on 14 March 2031 and I was born July 22 1990", "english",
         {"passport_expiry": d(2031, 3, 14), "date_of_birth": d(1990, 7, 22)}),
        ("born 07/22/1990", "english", {"date_of_birth": d(1990, 7, 22)}),
        ("2031-03-14", "english", {"passport_expiry": d(2031, 3, 14)}),
        ("דרכון P00428117 בתוקף עד 14 במרץ 2031", "hebrew",
         {"passport_number": "P00428117", "passport_expiry": d(2031, 3, 14)}),
        ("הדרכון בתוקף עד 14/03/2031", "hebrew", {"passport_expiry": d(2031, 3, 14)}),
        ("رقم الجواز P00428117 صالح لـ 14 مارس 2031", "arabic",
         {"passport_number": "P00428117", "passport_expiry": d(2031, 3, 14)}),
        ("паспорт P00428117, действителен до 14 марта 2031 года", "russian",
         {"passport_number": "P00428117", "passport_expiry": d(2031, 3, 14)}),
    ]
    for text, lang, want in cases:
        got = pv(text, lang)
        check(f"her words: {text!r}", got == want, got)
    check("nothing said, nothing made up", pv("I don't have it with me") == {})
    check("a short number is not a passport", pv("it's 12345") == {})
    check("a date this year is neither (not expired soon enough to tell, not a birth)",
          pv("June 3 2026") == {}, pv("June 3 2026"))


# ---------------------------------------------------------------- which field is which
def ctl(label, hint="", group="", typed=True, secure=False, private=False, value="", role="field", **kw):
    return {"id": kw.get("id", 1), "role": role, "label": label, "hint": hint, "group": group,
            "typed": typed, "secure": secure, "private": private, "value": value,
            "pick": role == "menu", "checked": kw.get("checked"), "frame": (300, 200, 300, 36)}


def t_field_map():
    pp = "Passport for Alex Rivera"
    check("known fields by what the page calls them",
          [G._known_key(ctl(x), {}) for x in ("Confirmation number", "Booking reference", "Last name",
                                              "Surname", "First name", "Given names", "Full name", "Email")]
          == ["confirmation_code", "confirmation_code", "last_name", "last_name", "first_name",
              "first_name", "passenger_name", ""])
    snap = {"text": "Passport and safety | Passport for Alex Rivera"}
    check("passport number", G._personal_key(ctl("Passport number", "6 to 9 letters", pp), {}, "", snap)
          == ("passport_number", ""))
    check("one expiry box in the passport's set",
          G._personal_key(ctl("Expiration date", "For example, 03/14/2031", pp), {}, "", snap) == ("passport_expiry", ""))
    check("three boxes: the month of the expiry, the year of the birth",
          G._personal_key(ctl("Month", "", "Expiration date"), {}, "", snap) == ("passport_expiry", "month")
          and G._personal_key(ctl("Year", "", "Date of birth"), {}, "", snap) == ("date_of_birth", "year"))
    pay = {"text": "Pay and check in | Card details"}
    check("a card's expiry is never hers to say here",
          G._personal_key(ctl("Expiration date", "MM/YY", "Card details"), {}, "", pay) is None
          and G._personal_key(ctl("Expiration date", "MM/YY", "Payment"), {}, "", snap) is None)
    check("a Month box with nothing about a passport or a birth is nobody's",
          G._personal_key(ctl("Month", "", "Travel dates"), {}, "", {"text": "Book a flight"}) is None)

    d = datetime.date(2031, 3, 14)
    check("dates as each field wants them",
          [G._date_for(ctl("x", h), d, "") for h in ("For example, 03/14/2031", "MM/DD/YYYY", "DD/MM/YYYY",
                                                       "For example, 25.12.2030", "YYYY-MM-DD", "")]
          == ["03/14/2031", "03/14/2031", "14/03/2031", "14.03.2031", "2031-03-14", "03/14/2031"]
          and [G._date_for(ctl("x"), d, p) for p in ("month", "day", "year")] == ["03", "14", "2031"])

    check("a passport number box may take her words", G._private_field(True, "Passport number", "6 to 9", ""))
    check("so may the expiry box of the passport's set", G._private_field(True, "Expiration date", "", pp))
    for label, hint, group in (("Password", "", ""), ("Card number", "", ""), ("Security code", "3 digits", ""),
                               ("Expiration date", "MM/YY", "Card details"), ("Expiration date", "", "Payment"),
                               ("IBAN", "", ""), ("Passport number", "", "Sign in"),
                               ("Enter the one-time code", "", "")):
        check(f"never {label!r} in {group!r}", not G._private_field(True, label, hint, group))


def t_decisions():
    known = booking.known({"confirmation_code": "K7QM2P", "first_name": "Alex", "last_name": "Rivera",
                           "passenger_name": "Alex Rivera", "confidence": {}})
    base = {"known": known, "personal": {}, "commits": False, "say": ""}
    snap = {"browser": True, "text": "Passport for Alex Rivera"}
    pp = "Passport for Alex Rivera"

    def dec(c, kind, **kw):
        return G._auto_decide({**base, "kind": kind, **kw}, snap, c)
    check("her code from the email: typed", dec(ctl("Confirmation number"), "type") == ("fill", ("K7QM2P", False)))
    check("her last name: typed", dec(ctl("Last name"), "type") == ("fill", ("Rivera", False)))
    check("already right: on to the next", dec(ctl("Last name", value="Rivera"), "type") == ("advance", None))
    check("her passport number: asked, never guessed",
          dec(ctl("Passport number", secure=True, private=True), "secret") == ("values", "passport_number"))
    check("once she said it and said yes: typed into that box only",
          dec(ctl("Passport number", secure=True, private=True), "secret",
              personal={"passport_number": "P00428117"}) == ("fill", ("P00428117", True)))
    check("the expiry, as the box wants it",
          dec(ctl("Expiration date", "For example, 03/14/2031", pp, secure=True, private=True), "secret",
              personal={"passport_expiry": datetime.date(2031, 3, 14)}) == ("fill", ("03/14/2031", True)))
    for label in ("Password", "Card number", "Security code", "Enter the one-time code"):
        check(f"{label}: never MicMic's, whatever she said",
              dec(ctl(label, secure=True), "secret", personal={"passport_number": "P00428117"}) == ("hers", None))
    check("a field nobody told it: hers", dec(ctl("City"), "type") == ("hers", None))
    check("a list: hers", dec(ctl("Nationality", role="menu", typed=False), "choose") == ("hers", None))
    check("the box with her name: MicMic's",
          dec(ctl("Check in Alex Rivera", role="checkbox", typed=False, checked=False), "click") == ("press", None))
    check("a box that speaks for her: hers",
          dec(ctl("I'm not carrying restricted items", role="checkbox", typed=False, checked=False), "click")
          == ("hers", None))
    check("an option: hers", dec(ctl("Female", role="option", typed=False), "click") == ("hers", None))
    check("a button that goes on: MicMic's", dec(ctl("Find my trip", role="button", typed=False), "click") == ("press", None))
    check("a free seat she asked for: MicMic's",
          dec(ctl("Seat 14C, aisle, available, free", role="button", typed=False), "click") == ("press", None))
    seat = ctl("Seat 14C, aisle, available, free", role="checkbox", typed=False, checked=False)
    check("the same seat as Chrome shows it to macOS (a toggle, a box): MicMic's only when she asked for it",
          dec(seat, "click", goal="Check in. She said: check me in and get me the free aisle seat")
          == ("press", None) and dec(seat, "click", goal="Check in. She said: check me in") == ("hers", None))
    check("a seat that costs money, asked for or not: hers",
          dec(ctl("Seat 12C, aisle, available, $39", role="checkbox", typed=False, checked=False), "click",
              goal="get me an aisle seat please") == ("hers", None))
    check("a seat that costs money: hers",
          dec(ctl("Seat 15D, aisle, available, $39", role="button", typed=False), "click") == ("hers", None))
    check("Check in now: asked first", dec(ctl("Check in now", role="button", typed=False), "click")
          == ("final", "Check in now"))
    check("a button the model says finishes it: asked first",
          dec(ctl("Check in", role="button", typed=False), "click", commits=True) == ("final", "Check in"))
    check("a Confirm button is asked first even when the model does not say so (live run 2026-10-11)",
          dec(ctl("Confirm seat", role="button", typed=False), "click") == ("final", "Confirm seat"))
    snap_b = {"browser": True, "controls": [ctl("Confirm seat", role="button", typed=False)]}
    check("and Do it for me leaves it to her in a guide",
          not G.Guide._can_do(snap_b, 1, "click", "Click Confirm seat."))
    for label in ("Pay now", "Continue to payment", "Sign in"):
        check(f"{label}: never MicMic's", dec(ctl(label, role="button", typed=False), "click") == ("hers_final", None))
    check("a scroll: MicMic's on a web page, down or up as the step says",
          dec(None, "look", say="Scroll down to find Continue.") == ("scroll", 450)
          and dec(None, "look", say="Scroll up to the top of the form.") == ("scroll", -450))
    check("anything outside a browser: hers",
          G._auto_decide({**base, "kind": "type"}, {"browser": False}, ctl("Last name")) == ("hers", None))


# ---------------------------------------------------------------- the flow on a fake airline
class CheckinScreen(TG.FakeScreen):
    """The Larkline short check-in, as the guide reads it. press() moves between pages;
    MicMic's fills and her own typing land in values."""

    def __init__(self):
        super().__init__(True)
        self.page = "lookup"
        self.fills: list = []
        self.checked: dict = {}
        self.scrolls = 0
        pp = "Passport for Alex Rivera"
        self.pages = {
            "lookup": [(1, "field", "Confirmation number", "For example, ABC123", ""),
                       (2, "field", "Last name", "", ""), (3, "button", "Find my trip", "", "")],
            "trip": [(1, "checkbox", "Check in Alex Rivera", "", ""), (2, "button", "Check in", "", "")],
            "passport": [(1, "field", "Passport number", "6 to 9 letters and numbers", pp),
                         (2, "field", "Expiration date", "For example, 03/14/2031", pp),
                         (3, "menu", "Nationality", "As shown on your passport", pp),
                         (4, "checkbox", "I'm not carrying restricted items", "", ""),
                         (5, "button", "Continue", "", "")],
            "seat": [(1, "button", "Seat 14C, aisle, available, free", "", ""),
                     (2, "button", "Confirm seat", "", "")],
            "pay": [(1, "field", "Card number", "", "Card details"), (2, "field", "Password", "", ""),
                    (3, "button", "Pay now", "", "")],
            "boarding-pass": [(1, "button", "Add to Wallet", "", "")],
        }
        self.goes = {("lookup", 3): "trip", ("trip", 2): "passport", ("passport", 5): "seat",
                     ("seat", 2): "boarding-pass"}

    def front(self):
        return {"app": "Google Chrome", "pid": 42, "bundle_id": "", "window": self.page}

    def page_url(self, front):
        return f"http://127.0.0.1:8851/#/check-in/{self.page}"

    def _ctl(self, cid, role, label, hint, group):
        secure = bool(re.search(r"password|card number|passport|expir", f"{label} {hint}", re.I)) and role == "field"
        private = G._private_field(secure, label, hint, group)
        value = self.values.get((self.page, cid), "")
        return {"id": cid, "role": role, "ax_role": "", "label": label, "hint": hint, "group": group,
                "value": "" if secure else value, "checked": self.checked.get((self.page, cid), False)
                if role == "checkbox" else None, "frame": (300, 100 + 60 * cid, 300, 36),
                "secure": secure, "private": private, "typed": role == "field", "pick": role == "menu",
                **({"filled": bool(value)} if secure else {})}

    def snapshot(self, front):
        self.snaps += 1
        controls = [self._ctl(*row) for row in self.pages[self.page]]
        return {"app": "Google Chrome", "pid": 42, "window": self.page, "url": self.page_url(front),
                "title": self.page, "controls": controls, "refs": {c["id"]: (self.page, c["id"]) for c in controls},
                "raw": {c["id"]: self.values.get((self.page, c["id"])) for c in controls if c["typed"] or c["pick"]},
                "text": "Passport and safety | Passport for Alex Rivera" if self.page == "passport" else self.page,
                "win": WIN, "secure_focus": False, "browser": True, "error": None}

    def probe(self, snap, tid):
        self.probes += 1
        ref = (snap.get("refs") or {}).get(tid)
        alive = ref is not None and ref[0] == self.page
        state = None
        if alive:
            v = self.values.get((self.page, tid))
            if (self.page, tid) in self.checked:
                v = int(self.checked[(self.page, tid)])
            state = (v, None, None)
        return {"pid": 42, "title": self.page, "url": self.page_url({}),
                "struct": [self.page, bool(self.values.get(("seat", "14C")))],
                "alive": alive if ref else None, "state": state, "focused": self.focused == tid,
                "frame": (300, 100 + 60 * tid, 300, 36) if alive else None, "win": WIN,
                "anchor": 100 - 40 * self.scrolls}

    def fill(self, snap, tid, value, private=False):
        c = next(c for c in snap["controls"] if c["id"] == tid)
        if (c["secure"] and not (private and c["private"])) or not c["typed"]:
            self.fills.append((c["label"], value, "refused"))
            return False, "refused"
        self.fills.append((c["label"], value, "ok"))
        self.values[(self.page, tid)] = value
        return True, "ok"

    def press(self, snap, tid):
        page = (snap.get("refs") or {}).get(tid, ("", 0))[0]
        if page != self.page:
            return False, "stale"
        label = next(r[2] for r in self.pages[self.page] if r[0] == tid)
        self.presses.append(label)
        if label.startswith("Seat 14C"):
            self.values[("seat", "14C")] = True
        if (self.page, tid) in self.checked or any(r[0] == tid and r[1] == "checkbox" for r in self.pages[self.page]):
            self.checked[(self.page, tid)] = not self.checked.get((self.page, tid), False)
        elif (self.page, tid) in self.goes:
            self.page = self.goes[(self.page, tid)]
        return True, "ok"

    def scroll(self, snap, dy=450):
        self.scrolls += 1
        return True

    # her own hands
    def type_in(self, label, text):
        tid = next(r[0] for r in self.pages[self.page] if r[2] == label)
        self.focused = tid
        self.values[(self.page, tid)] = text
        TG.time.sleep(0.05)
        self.focused = None

    def pick(self, label, value):
        tid = next(r[0] for r in self.pages[self.page] if r[2] == label)
        self.values[(self.page, tid)] = value

    def tick(self, label):
        tid = next(r[0] for r in self.pages[self.page] if r[2] == label)
        self.checked[(self.page, tid)] = True


class CheckinModel:
    """The step call on this fake airline: the next thing the page still needs, the way
    the step prompt asks for it (fields planned ahead, commits on the last button)."""

    def __init__(self, sc, aisle=False):
        self.sc, self.aisle, self.requests = sc, aisle, []

    def __call__(self, req):
        self.requests.append(req)
        sc, page = self.sc, self.sc.page
        if page == "boarding-pass":
            return {"done": True, "finish": "You're checked in.", "say": "", "target": None, "kind": "click"}
        snap = sc.snapshot(sc.front())
        todo = []
        for c in snap["controls"]:
            filled = c.get("filled") or c.get("value") or (c["role"] == "checkbox" and c["checked"])
            if c["role"] == "menu":
                filled = bool(sc.values.get((page, c["id"])))
            if c["role"] == "checkbox" and filled:
                continue
            if c["typed"] or c["pick"] or c["role"] == "checkbox":
                if not filled:
                    todo.append(c)
            elif c["role"] == "button":
                if c["label"].startswith("Seat 14C") and (not self.aisle or sc.values.get(("seat", "14C"))):
                    continue
                todo.append(c)
        c = todo[0]
        kind = ("secret" if c["secure"] else "type") if c["typed"] else ("choose" if c["pick"] else "click")
        step = {"done": False, "say": f"Fill in {c['label']}." if c["typed"] else f"Use {c['label']}.",
                "target": c["id"], "kind": kind, "why": "", "goal": "Check in", "finish": "",
                "field": c["label"].lower() if c["typed"] else "", "ask": "", "value": "", "profile": "",
                "commits": c["label"] == "Confirm seat",
                "then": [{"say": f"Fill in {n['label']}.", "target": n["id"],
                          "kind": ("secret" if n["secure"] else "type") if n["typed"] else
                          ("choose" if n["pick"] else "click"), "field": n["label"].lower()}
                         for n in todo[1:4] if n["role"] != "button"] if c["typed"] else []}
        return step


def checkin_guide(sc, model, autofill=True, known=None):
    g = G.Guide()
    g.screen, g.model = sc, model
    g.said = []
    g.speaker = lambda text, lang: g.said.append(text)
    g.tick, g.settle, g.field_idle, g.nudge_after = 0.01, 0.05, 0.1, 30.0
    k = known if known is not None else booking.known(
        {"confirmation_code": "K7QM2P", "first_name": "Alex", "last_name": "Rivera",
         "passenger_name": "Alex Rivera", "confidence": {"confirmation_code": 0.98}})
    g.start("Check in for flight LL 482 on booking K7QM2P for Alex Rivera", "english",
            known=k, autofill=autofill)
    return g


def t_autofill_says_values():
    sc = CheckinScreen()
    g = checkin_guide(sc, CheckinModel(sc))
    check("the code and her last name are typed without a question, and Find my trip pressed",
          until(lambda: sc.page == "trip", 3)
          and [f[:2] for f in sc.fills[:2]] == [("Confirmation number", "K7QM2P"), ("Last name", "Rivera")]
          and "Find my trip" in sc.presses and not any("?" in x for x in g.said), (sc.fills, sc.presses, g.said))
    check("her name's box ticked, Check in pressed",
          until(lambda: sc.page == "passport", 3) and "Check in Alex Rivera" in sc.presses, sc.presses)
    check("on the passport page she is asked once for both details",
          until(lambda: g.awaiting() == "value", 3) and len(g.said) == 1
          and "passport number" in g.said[0] and "expires" in g.said[0], g.said)
    TG.time.sleep(0.3)
    check("nothing is typed into her passport fields before her words",
          not any(f[0] in ("Passport number", "Expiration date") for f in sc.fills) and len(g.said) == 1,
          (sc.fills, g.said))
    r = g.give("My passport number is P 0 0 4 2 8 1 1 7 and it expires March 14th, 2031", speak=False)
    check("what she said is read back, the number one character at a time",
          r["said"] == "Passport P, 0, 0, 4, 2, 8, 1, 1, 7, valid until March 14, 2031. Is that right?"
          and g.awaiting() == "confirm" and g.state()["can_do"], r.get("said"))
    TG.time.sleep(0.2)
    check("and still nothing typed before her yes",
          not any(f[0] in ("Passport number", "Expiration date") for f in sc.fills), sc.fills)
    r = g.answer(False, heard="no", speak=False)
    check("a no asks again", g.awaiting() == "value" and "What should it be" in r["said"], r)
    g.give("P00428117, it expires 03/14/2031", speak=False)
    g.answer(True, speak=False)
    check("her yes types both, as she said them, into those two boxes",
          until(lambda: ("Expiration date", "03/14/2031", "ok") in sc.fills)
          and ("Passport number", "P00428117", "ok") in sc.fills, sc.fills)
    check("the list is hers: said to her, not done",
          until(lambda: any("Nationality" in x for x in g.said)) and not any(f[0] == "Nationality" for f in sc.fills),
          g.said)
    sc.pick("Nationality", "United States")
    check("the box that speaks for her is hers too",
          until(lambda: any("restricted items" in x for x in g.said))
          and "I'm not carrying restricted items" not in sc.presses, (g.said, sc.presses))
    sc.tick("I'm not carrying restricted items")
    check("Continue is pressed for her", until(lambda: sc.page == "seat", 3), sc.page)
    check("the button that finishes it waits for her yes",
          until(lambda: g.awaiting() == "confirm") and "Confirm seat" not in sc.presses
          and "Shall I press Confirm seat?" in g.said[-1], (g.said[-1:], sc.presses))
    r = g.answer(True, speak=False)
    check("her yes presses it, and the guide ends on the boarding pass",
          r.get("ok") and until(lambda: g.state()["status"] == "done", 3) and sc.page == "boarding-pass",
          (r, g.state()["status"], sc.page))
    check("not a question about the code or her name was asked",
          not any(re.search(r"code|last name", x, re.I) and "?" in x for x in g.said), g.said)
    check("her details are forgotten when it ends", not g._s.get("personal") or g._s["status"] == "done")


def t_autofill_she_types():
    sc = CheckinScreen()
    g = checkin_guide(sc, CheckinModel(sc))
    until(lambda: g.awaiting() == "value", 4)
    asked = len(g.said)
    sc.type_in("Passport number", "P00428117")
    check("she types the passport number herself: the next box comes", until(
        lambda: g._s.get("target") == 2 and g.state()["kind"] == "secret", 3), g.state())
    TG.time.sleep(0.2)
    check("and she is not asked again for what she was already asked", len(g.said) == asked, g.said)
    sc.type_in("Expiration date", "03/14/2031")
    check("MicMic never typed into either", not any(f[0] in ("Passport number", "Expiration date") for f in sc.fills),
          sc.fills)
    g.stop()


def t_autofill_never_without_her():
    sc = CheckinScreen()
    g = checkin_guide(sc, CheckinModel(sc))
    until(lambda: g.awaiting() == "value", 4)
    g.heard("my passport number is P00428117")
    TG.time.sleep(0.4)
    check("a passport said to anything else than the question is not taken",
          not any(f[0] == "Passport number" for f in sc.fills) and not g._s["personal"], sc.fills)
    g.stop()
    # A payment page: whatever she said, a card or a password is never typed, Pay never pressed.
    sc = CheckinScreen()
    sc.page = "pay"
    g = checkin_guide(sc, CheckinModel(sc))
    g._s["personal"]["passport_number"] = "P00428117"
    TG.time.sleep(0.5)
    check("a card field is never typed into", not sc.fills, sc.fills)
    check("and its step is said to her", any("Card number" in x for x in g.said), g.said)
    g.stop()
    snap = sc.snapshot(sc.front())
    check("the screen itself refuses a password even when asked as private",
          sc.fill(snap, 2, "x", private=True)[0] is False)


def t_aisle_seat_when_asked():
    sc = CheckinScreen()
    sc.page = "seat"
    g = checkin_guide(sc, CheckinModel(sc, aisle=True))
    check("the free aisle seat she asked for is chosen for her, then Confirm seat waits for her",
          until(lambda: g.awaiting() == "confirm", 3) and sc.presses == ["Seat 14C, aisle, available, free"],
          sc.presses)
    r = g.answer(False, speak=False)
    TG.time.sleep(0.2)
    check("a no leaves the button to her", sc.presses == ["Seat 14C, aisle, available, free"]
          and "leave that one to you" in r["said"], (sc.presses, r.get("said")))
    g.stop()


def t_paid_choice_she_did_not_ask_for():
    """The live run of 2026-10-11: the step call rang "Seat 8A, $119" on a check-in where
    she asked for nothing: the step is asked again, told to keep what is chosen."""
    sc = CheckinScreen()
    sc.pages["seat"] = [(1, "checkbox", "Seat 8A, window, extra legroom, available, $119", "", ""),
                        (2, "button", "Confirm seat", "", "")]
    sc.page = "seat"
    paid = {"done": False, "say": "Click the checkbox for Seat 8A.", "target": 1, "kind": "click",
            "why": "", "goal": "Check in", "finish": ""}
    keep = {"done": False, "say": "Click Confirm seat.", "target": 2, "kind": "click",
            "why": "", "goal": "Check in", "finish": "", "commits": False}
    m = TG.FakeModel([paid, keep])
    g = checkin_guide(sc, m)
    check("a paid seat she never asked for is not her step: asked again, the seat she has is kept",
          until(lambda: g.awaiting() == "confirm", 3) and len(m.requests) == 2
          and "costs money and she did not ask for it" in m.requests[1]["prompt"]
          and not sc.presses, ([r["reason"] for r in m.requests], sc.presses))
    g.stop()
    check("the step prompt says never to pick what costs money unasked",
          "Never point at a choice that costs money" in G.SYSTEM)
    check("asked for by name, a paid upgrade is hers to choose",
          G._paid_target({"target": 1}, {"controls": [ctl("Upgrade to Business, $749", role="button", typed=False)]},
                         "Check in. She said: check me in and upgrade me to business", []) == "")
    check("a Pay button is judged as a payment, not as a choice",
          G._paid_target({"target": 1}, {"controls": [ctl("Pay $41.80", role="button", typed=False)]},
                         "help me pay my bill", []) == "")


def t_four_languages():
    for key in ("need_values", "need_value", "confirm_values", "filling", "confirm_final", "pressed_final",
                "left_final", "no_page", "v_passport_number", "v_passport_expiry", "v_date_of_birth",
                "rb_passport_number", "rb_passport_expiry", "rb_date_of_birth"):
        for lang in ("english", "hebrew", "arabic", "russian"):
            line = G.SAY[key].get(lang, "")
            check(f"{key} in {lang}", bool(line) and not re.search("[–—]", line), line)
    from savta import router
    for key, block in router._CHECKIN_SAY.items():
        for lang in ("english", "hebrew", "arabic", "russian"):
            check(f"router {key} in {lang}", bool(block.get(lang)) and not re.search("[–—]", block[lang]))
    for lang in ("english", "hebrew", "arabic", "russian"):
        line = G._ask_values(["passport_number", "passport_expiry"], lang, "")
        check(f"the ask in {lang} names both", line and "{" not in line and "«" not in line, line)


# ---------------------------------------------------------------- the router
class MailScreen(CheckinScreen):
    """Apple Mail in front with the check-in email; the link opens the airline's page in
    Chrome (opened())."""

    def __init__(self, links=True):
        super().__init__()
        self.app = "Mail"
        self.text, self.links = email_doc()
        if not links:
            self.links = []
        self.opened = []

    def front(self):
        if self.app == "Mail":
            return {"app": "Mail", "pid": 7, "bundle_id": "com.apple.mail", "window": "Time to check in"}
        return super().front()

    def read_document(self, front):
        if self.app == "Mail":
            return {"text": self.text, "links": self.links, "error": None}
        return {"text": "Check in | Confirmation number | For example, ABC123", "links": [], "error": None}

    def open_link(self, url):
        self.opened.append(url)
        self.app = "Google Chrome"


def t_router():
    from savta import router, profile as prof
    prof.save({**prof.DEFAULT, "setup_complete": True, "step": "done", "name": "",
               "language": "english", "city": "New York"})
    router.due_briefing = lambda: False
    router.get_contacts = lambda *a, **k: ["Dana Levi"]
    mac.screen_locked = lambda: False
    spoken = []
    mac.say = lambda text, lang="english", **k: spoken.append(text)
    launched = []
    saved_web, saved_open = router._launch_web, mac.open_url
    router._launch_web = lambda *a, **k: launched.append(a[1]) or {"web_run": "x"}
    base = {"intent": ("do_online", 0.9), "is_complete": 0.95, "language": ("english", 0.99)}
    try:
        sc = MailScreen()
        mac.open_url = sc.open_link
        router.GUIDE.screen, router.GUIDE.model = sc, CheckinModel(sc)
        router.GUIDE.speaker = lambda text, lang: spoken.append(text)
        router.GUIDE.tick, router.GUIDE.settle = 0.01, 0.05
        j = BookingJev()
        j.over = dict(base)
        r = router.handle(j, "Hey MicMic, check me in", speak=False)
        check("'check me in' with the email in front starts the check-in",
              r["did"] == "checkin_started" and not launched, (r["did"], launched))
        check("the booking is read back once, the code one character at a time",
              r["say"] == "Found it: booking K, 7, Q, M, 2, P, for Alex Rivera. I'm opening the check-in "
                          "page and filling in what I know.", r["say"])
        check("the email's own link is opened", sc.opened == ["http://127.0.0.1:8851/#/check-in"], sc.opened)
        check("two Jev requests: her words, and the booking", j.calls == 2, j.calls)
        k = router.GUIDE._s["known"]
        check("the guide keeps what it read, with its source",
              k["confirmation_code"]["value"] == "K7QM2P" and k["last_name"]["value"] == "Rivera"
              and all(v["source"] == "email" for v in k.values()), k)
        check("it fills the airline's page once it is in front",
              until(lambda: ("Confirmation number", "K7QM2P", "ok") in sc.fills, 10), sc.fills)
        check("the trace names what was found",
              r["detail"]["front"]["booking"]["confirmation_code"] == "K7QM2P", r["detail"])
        check("on the passport page she is asked for her details",
              until(lambda: router.GUIDE.awaiting() == "value", 10), router.GUIDE.state())
        n = j.calls
        r = router.handle(j, "My passport number is P 0 0 4 2 8 1 1 7, and it expires March 14th, 2031",
                          speak=False)
        check("her passport details are read back by MicMic itself, and go to no model, Jev included",
              r["did"] == "guide_gives_value" and j.calls == n and r["say"].startswith("Passport P, 0, 0")
              and not any("P00428117" in x or "P 0 0" in x for x in router.GUIDE._s.get("said") or []),
              (r["did"], j.calls - n, r["say"]))
        r = router.handle(j, "yes", speak=False)
        check("her yes types them", r["did"] == "guide_yes" and until(
            lambda: ("Passport number", "P00428117", "ok") in sc.fills
            and ("Expiration date", "03/14/2031", "ok") in sc.fills, 3), (r["did"], sc.fills))
        router.GUIDE.stop()

        sc = MailScreen(links=False)
        mac.open_url = sc.open_link
        router.GUIDE.screen = sc
        j = BookingJev()
        j.over = dict(base)
        r = router.handle(j, "check me in", speak=False)
        check("an email with no check-in link says so, opens nothing",
              r["did"] == "checkin_no_link" and not sc.opened and not router.GUIDE.active() and not launched,
              (r["did"], r["say"]))

        sc = MailScreen()
        sc.text = "Lunch on Thursday? Let me know. Dana"
        mac.open_url = sc.open_link
        router.GUIDE.screen = sc
        j = BookingJev()
        j.over = dict(base)
        r = router.handle(j, "check me in for my flight", speak=False)
        check("no check-in email and no page in front: she is told what to open, never the browser agent",
              r["did"] == "checkin_no_email" and not launched and not router.GUIDE.active()
              and j.calls == 1, (r["did"], launched, j.calls))

        sc = MailScreen()
        sc.app = "Google Chrome"             # the airline's check-in page itself, not an email
        router.GUIDE.screen, router.GUIDE.model = sc, CheckinModel(sc)
        mac.open_url = sc.open_link
        j = BookingJev(booking_noul=0.1)
        j.over = dict(base)
        r = router.handle(j, "check me in", speak=False)
        check("'check me in' on the airline's page: filled in there, nothing opened",
              r["did"] == "checkin_started" and not sc.opened and router.GUIDE.state()["autofill"]
              and not launched, (r["did"], sc.opened))
        router.GUIDE.stop()

        r = router.handle(j, "help me check in for my flight", speak=False)
        check("'help me check in' on the page is today's guide, she does the steps",
              str(r["did"]).startswith("guide") and not router.GUIDE.state().get("autofill"), r["did"])
        router.GUIDE.stop()

        r = router.handle(j, "what's the weather", speak=False)
        check("any other request reads nothing off the screen", sc.opened == [] and r["did"] != "checkin_started")
    finally:
        router._launch_web, mac.open_url = saved_web, saved_open
        router.GUIDE.stop()
    check("a test process never reads the real screen for it", G.Guide().front_document() is None)


def main():
    tests = [t_demo_email, t_booking_read, t_her_details, t_field_map, t_decisions,
             t_autofill_says_values, t_autofill_she_types, t_autofill_never_without_her,
             t_aisle_seat_when_asked, t_paid_choice_she_did_not_ask_for, t_four_languages, t_router]
    for t in tests:
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran to the end", False, repr(e)[:300])
    print(f"\n{len(TG.PASSED)} passed, {len(TG.FAILED)} failed")
    for f in TG.FAILED:
        print("  FAILED:", f)
    sys.stdout.flush()
    os._exit(1 if TG.FAILED else 0)


if __name__ == "__main__":
    main()
