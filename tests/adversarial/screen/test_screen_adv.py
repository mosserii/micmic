#!/usr/bin/env python3
"""Adversarial tests for savta/actions/screen.py: own windows, real Accessibility,
real screen.py, and the exact string router._screen_text() would hand the model.

    uv sync
    .venv/bin/python3 tests/adversarial/screen/test_screen_adv.py

Every check here states the CORRECT behaviour; a FAIL is a product failure or a leak
this suite found, not a broken test. Each scenario runs ADV_REPEAT times (default 3)
so every failure is reported as k/n. ADV_ONLY=name1,name2 runs a subset.

Every read is guarded (common.guarded_context): this process must be frontmost
before and after, and the context's own target pid must be this process, or the run
aborts with exit 3 and the result is discarded unread. No real app is ever read.
"""
from __future__ import annotations

import time

import common as C  # noqa: I001  (must come first: sets MICMIC_STATE_DIR, builds the app)
from common import check, note, show, make_window, static_label, pump, guarded_context

from AppKit import (  # noqa: E402
    NSTextField, NSSecureTextField, NSTextView, NSScrollView, NSView, NSImageView,
    NSImage, NSMakeRect, NSMakeRange,
)

from savta.actions import screen  # noqa: E402
from savta import router  # noqa: E402


FAKE_KEY = "sk-" + "proj-" + "Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56"   # built, never literal

def model_text(ctx: dict) -> str:
    """The exact string the model is sent for describe/summarize/translate."""
    return router._screen_text(ctx)


def editable(frame, value: str, ax_label: str | None = None,
             placeholder: str | None = None) -> NSTextField:
    tf = NSTextField.alloc().initWithFrame_(NSMakeRect(*frame))
    tf.setStringValue_(value)
    if ax_label is not None:
        tf.setAccessibilityLabel_(ax_label)
    if placeholder is not None:
        tf.setPlaceholderString_(placeholder)
    return tf


def text_view(win, content: str, frame=(20, 20, 560, 300)) -> NSTextView:
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(*frame))
    tv = NSTextView.alloc().initWithFrame_(scroll.bounds())
    tv.setString_(content)
    tv.setEditable_(True)
    scroll.setDocumentView_(tv)
    win.contentView().addSubview_(scroll)
    return tv


# ============================================================ content

HE_PARA = ("זהו פסקה ארוכה בעברית שנועדה לבדוק האם הקורא של המסך מעביר את כל הטקסט "
           "ולא רק את תחילתו. סבתא קוראת מכתב מהבנק ורוצה לשמוע את כולו, כולל הסוף "
           "שבו כתוב המועד האחרון לתשלום: יום רביעי הבא. סוף הפסקה העברית.")
AR_PARA = ("هذه فقرة طويلة باللغة العربية لاختبار ما إذا كان قارئ الشاشة ينقل النص كله "
           "وليس بدايته فقط. الجدة تقرأ رسالة من الطبيب وتريد أن تسمع كل شيء بما في ذلك "
           "موعد الزيارة القادمة يوم الخميس. نهاية الفقرة العربية.")
MIXED = "Meeting at 10:30 עם דנה في المكتب, room 4B"
ZWJ_FAMILY = "family 👨‍👩‍👧 photo"
FLAG = "flag 🇮🇱 here"
BIDI_RLO = "‮evil‬ order"
ISOLATE = "⁧שלום⁩ world isolate"


def _piece_len(vis: str, start: str) -> int:
    return max((len(p) for p in vis.split(" | ") if p.startswith(start)), default=0)


def t_content_rtl_mixed():
    win = make_window("Content RTL", w=620, h=360)
    cv = win.contentView()
    rows = [HE_PARA, AR_PARA, MIXED, ZWJ_FAMILY, FLAG, BIDI_RLO, ISOLATE]
    for i, text in enumerate(rows):
        cv.addSubview_(static_label((20, 300 - i * 40, 580, 36), text))
    show(win)
    ctx = guarded_context()
    vis = ctx["visible"]["text"]
    check("content: Hebrew paragraph (%d chars) reaches visible text in full" % len(HE_PARA),
          HE_PARA in vis, f"got {_piece_len(vis, 'זהו')} chars of it; "
          f"tail present={('סוף הפסקה העברית' in vis)}")
    check("content: Arabic paragraph (%d chars) reaches visible text in full" % len(AR_PARA),
          AR_PARA in vis, f"got {_piece_len(vis, 'هذه')} chars; tail present={('نهاية الفقرة العربية' in vis)}")
    check("content: a line that was cut short is reported truncated=True",
          (HE_PARA in vis and AR_PARA in vis) or ctx["visible"]["truncated"],
          f"truncated={ctx['visible']['truncated']} while paragraphs were cut")
    check("content: mixed-direction line survives exactly", MIXED in vis)
    check("content: ZWJ emoji sequence survives (family is one glyph, not three people)",
          ZWJ_FAMILY in vis,
          f"got {[p for p in vis.split(' | ') if p.startswith('family')]!r}; _clean strips U+200D (Cf)")
    check("content: regional-indicator flag survives", FLAG in vis)
    check("content: RLO override stripped, logical text kept", "evil order" in vis)
    check("content: bidi isolates stripped, text kept", "שלום world isolate" in vis)


def t_huge_static_200k():
    marker_tail = "TAIL_MARKER_END"
    big = "".join(f"w{i:06d} " for i in range(25000)) + marker_tail
    win = make_window("Huge static", w=600, h=400)
    win.contentView().addSubview_(static_label((10, 10, 580, 380), big))
    show(win)
    t0 = time.time()
    ctx = guarded_context()
    dt = time.time() - t0
    vis = ctx["visible"]
    got = len(vis["text"])
    note(f"huge static ({len(big)} chars): visible text {got} chars, "
         f"truncated={vis['truncated']}, context() {dt * 1000:.0f}ms")
    check("huge static: 200k-char label yields up to max_chars (6000) of its text",
          got >= 5000, f"only {got} chars of a {len(big)}-char label reached visible text")
    check("huge static: a label cut short is reported truncated=True",
          vis["truncated"] is True, f"truncated={vis['truncated']} with {got}/{len(big)} chars read")


def t_huge_textview_200k():
    big = "".join(f"line {i:06d} of the long document. " for i in range(6000))[:200_000]
    win = make_window("Huge textview", w=600, h=400)
    text_view(win, big)
    show(win)
    samples = []
    for _ in range(5):
        t0 = time.time()
        ctx = guarded_context()
        samples.append(time.time() - t0)
    vis = ctx["visible"]
    note(f"huge NSTextView ({len(big)} chars): visible {len(vis['text'])} chars, "
         f"truncated={vis['truncated']}; {C.latency_row('context()', samples)}")
    check("huge textview: capped at max_chars", len(vis["text"]) <= 6000, len(vis["text"]))
    check("huge textview: reported truncated", vis["truncated"] is True, vis["truncated"])
    check("huge textview: the document text is actually read",
          "line 000000 of the long document." in vis["text"])
    check("huge textview: model text stays within 6000 chars",
          len(model_text(ctx)) <= 6000, len(model_text(ctx)))


def t_many_5000():
    win = make_window("Many elements", w=600, h=400)
    cv = win.contentView()
    for i in range(5000):
        cv.addSubview_(static_label((0, 0, 40, 14), f"item {i}"))
    show(win)
    t0 = time.time()
    ctx = guarded_context()
    dt = time.time() - t0
    vis = ctx["visible"]
    read = sum(1 for p in vis["text"].split(" | ") if p.startswith("item "))
    note(f"5000 labels: elements={vis['elements']} items_in_text={read} "
         f"truncated={vis['truncated']} chars={len(vis['text'])} context()={dt * 1000:.0f}ms")
    check("5000 elements: truncated reported", vis["truncated"] is True, vis)
    check("5000 elements: element cap 2500 honoured", vis["elements"] <= 2501, vis["elements"])
    check("5000 elements: context() returns within 1.5s budget + 1s overhead",
          dt < 2.5, f"{dt:.2f}s")


def _nest(parent, depth: int, markers: dict[int, str]):
    for d in range(1, depth + 1):
        v = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 300))
        v.setAccessibilityElement_(True)
        v.setAccessibilityRole_("AXGroup")
        parent.addSubview_(v)
        parent = v
        if d in markers:
            parent.addSubview_(static_label((0, 0, 300, 18), markers[d]))


def t_deep_nesting_80():
    markers = {d: f"DEPTH_{d:02d}_MARKER" for d in (10, 40, 55, 62, 70, 80)}
    win = make_window("Deep nesting", w=500, h=360)
    _nest(win.contentView(), 80, markers)
    show(win)
    ctx = guarded_context()
    vis = ctx["visible"]
    found = [d for d, m in markers.items() if m in vis["text"]]
    note(f"deep nesting: markers found at depths {found}; elements={vis['elements']} "
         f"truncated={vis['truncated']}")
    check("deep nesting: control marker at depth 40 is read (AX groups are walkable)",
          40 in found, found)
    check("deep nesting: text 80 groups deep is read (or truncated=True says it was not)",
          80 in found or vis["truncated"],
          f"depth-80 text lost with truncated={vis['truncated']}; found={found}")


def t_empty_and_images_only():
    win = make_window("Empty window")
    show(win)
    ctx = guarded_context()
    txt = ctx["visible"]["text"]
    chrome = [p for p in txt.split(" | ") if p and p != "Empty window"]
    note(f"empty window: visible text is window chrome only: {chrome!r}")
    check("empty window: nothing but window chrome is read", len(txt) < 200, txt)
    C.close_all_windows()

    win = make_window("Images only", w=400, h=300)
    for i, name in enumerate(("NSComputer", "NSFolder", "NSNetwork")):
        iv = NSImageView.alloc().initWithFrame_(NSMakeRect(20 + i * 120, 100, 100, 100))
        iv.setImage_(NSImage.imageNamed_(name))
        win.contentView().addSubview_(iv)
    show(win)
    ctx = guarded_context()
    txt = ctx["visible"]["text"]
    check("images only: no crash, no invented text", "NSComputer" not in txt, txt)
    check("images only: has_image is a bool", isinstance(ctx["has_image"], bool))


# ============================================================ secrets

# (label kind, label text, value). Every value here is a secret the router's own
# rules say must not reach the model: FORBIDDEN_FIELD lists passwords, OTP /
# verification codes, PINs and card fields; _SCREEN_SYSTEM says "never read out a
# password, a card number or a code".
LABELLED = [
    ("ax_label", "Card number", "4111111111111111"),
    ("ax_label", "CVV", "737"),
    ("ax_label", "סיסמה", "hunter2he"),
    ("ax_label", "пароль", "hunter2ru"),
    ("ax_label", "Password", "hunter2en"),
    ("placeholder", "Password", "hunter2ph"),
    # OTP / PIN fields in the languages this product is built for.
    ("ax_label", "קוד אימות", "482913"),
    ("ax_label", "קוד סודי", "8824"),
    ("ax_label", "Код подтверждения", "593104"),
    ("ax_label", "ПИН-код", "7731"),
    ("ax_label", "رمز التحقق", "771246"),
    ("ax_label", "الرقم السري", "9054"),
    # The same English word with an invisible character in it (U+200B, U+00AD).
    ("ax_label", "Pass​word", "zwsp-secret-1"),
    ("ax_label", "Pass­word", "shy-secret-2"),
]
# Label linked through AXTitleUIElement (the standard "labelled by" link, and what
# web <label for=...> maps to), or only placed beside the field with no link at all.
TITLED_BY = [("Card number", "5500 0000 0000 0004"), ("CVV", "123"),
             ("Password", "linked-secret-3")]
BESIDE = [("Card number", "4000 0566 5566 5556"), ("Security code", "942")]


def t_secret_fields():
    win = make_window("Checkout", w=700, h=720)
    cv = win.contentView()
    y = 680
    for kind, label, value in LABELLED:
        f = editable((200, y, 300, 22), value, **({"ax_label": label} if kind == "ax_label"
                                                   else {"placeholder": label}))
        cv.addSubview_(f)
        y -= 30
    for label, value in TITLED_BY:
        lab = static_label((20, y, 170, 22), label)
        f = editable((200, y, 300, 22), value)
        f.setAccessibilityTitleUIElement_(lab)
        cv.addSubview_(lab)
        cv.addSubview_(f)
        y -= 30
    for label, value in BESIDE:
        cv.addSubview_(static_label((20, y, 170, 22), label + ":"))
        cv.addSubview_(editable((200, y, 300, 22), value))
        y -= 30
    ok_field = editable((200, y, 300, 22), "Zohar Adversary", ax_label="Full name")
    cv.addSubview_(ok_field)
    show(win)
    ctx = guarded_context()
    sent = model_text(ctx)
    check("secret fields: guard is not swallowing everything (Full name survives)",
          "Zohar Adversary" in sent)
    for kind, label, value in LABELLED:
        check(f"secret field [{kind} {label!r}] value never reaches the model",
              value not in sent, f"LEAK: {value!r} is in _screen_text()")
    for label, value in TITLED_BY:
        check(f"secret field [AXTitleUIElement {label!r}] value never reaches the model",
              value not in sent, f"LEAK: {value!r} is in _screen_text()")
    for label, value in BESIDE:
        check(f"secret field [label beside, no link {label!r}] value never reaches the model",
              value not in sent, f"LEAK: {value!r} is in _screen_text()")

    # Focus the linked card field: focused_text must treat it as secure.
    card = [v for v in cv.subviews() if isinstance(v, NSTextField) and
            str(v.stringValue()) == TITLED_BY[0][1]][0]
    win.makeFirstResponder_(card)
    pump(0.2)
    ctx = guarded_context()
    check("focused [AXTitleUIElement 'Card number'] field is reported secure",
          ctx["focused"]["secure"] is True,
          f"LEAK: focused={ctx['focused']!r}")


def t_secure_focused_selected():
    secret = "S3cure!Pass#42"
    win = make_window("Login")
    cv = win.contentView()
    cv.addSubview_(static_label((20, 260, 300, 20), "Account password:"))
    sec = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(20, 230, 300, 24))
    sec.setStringValue_(secret)
    cv.addSubview_(sec)
    show(win)
    win.makeFirstResponder_(sec)
    pump(0.2)
    ed = win.fieldEditor_forObject_(True, sec)
    if ed is not None:
        ed.setSelectedRange_(NSMakeRange(0, len(secret)))
    pump(0.2)
    ctx = guarded_context()
    check("secure field focused+selected: secret absent from context()", secret not in repr(ctx))
    check("secure field focused+selected: secret absent from model text",
          secret not in model_text(ctx))
    check("secure field focused+selected: focused.secure is True", ctx["focused"]["secure"] is True,
          ctx["focused"])


PLAIN_SECRETS = [
    # (visible line, the token that must not reach the model, why it is a secret)
    ("Your code is 482913", "482913", "OTP (no keyword the regex knows)"),
    ("Your verification code is 771100", "771100", "OTP (keyword control)"),
    ("הקוד שלך הוא 318274", "318274", "OTP in Hebrew"),
    ("Ваш код: 593104. Никому не сообщайте.", "593104", "OTP in Russian"),
    ("رمزك هو 640215", "640215", "OTP in Arabic"),
    ("Paid with card 4111 1111 1111 1111, thank you", "4111 1111 1111 1111",
     "card number in plain text"),
    ("Transfer to GB82WEST12345698765432 by Friday", "GB82WEST12345698765432",
     "IBAN without the word IBAN"),
    ("IBAN: GB33BUKB20201555555555", "GB33BUKB20201555555555", "IBAN with keyword (control)"),
    ("key = " + FAKE_KEY, FAKE_KEY,
     "API key"),
    ("aws AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE", "AWS access key id"),
]


def t_plain_text_secrets():
    win = make_window("Inbox", w=620, h=480)
    cv = win.contentView()
    for i, (line, _, _) in enumerate(PLAIN_SECRETS):
        cv.addSubview_(static_label((20, 440 - i * 40, 580, 30), line))
    show(win)
    ctx = guarded_context()
    sent = model_text(ctx)
    for line, token, why in PLAIN_SECRETS:
        check(f"plain text secret [{why}] never reaches the model",
              token not in sent, f"LEAK: {token!r} is in _screen_text()")


ARTICLE_EXPIRE = ("Your library books expire next Tuesday. Please return them to the "
                  "front desk or renew them online. Opening hours are nine to five.")
ARTICLE_MAPS = "Откройте карты, чтобы найти ближайшую аптеку. Она открыта до восьми вечера."
ARTICLE_SWIFT = "The swift fox jumped over the fence, and the children laughed at it."


def t_overblocking():
    """A secret-shaped keyword anywhere in a text area withholds the WHOLE text area:
    an ordinary letter that says 'expire', a Russian sentence with 'карты' (maps), a
    story with 'swift'. That is the model seeing nothing where there is plenty."""
    for title, body in (("Letter", ARTICLE_EXPIRE), ("Maps", ARTICLE_MAPS),
                        ("Story", ARTICLE_SWIFT)):
        win = make_window(title, w=600, h=340)
        text_view(win, body)
        show(win)
        ctx = guarded_context()
        check(f"over-blocking: an ordinary {title.lower()} text area is not withheld whole",
              body[:40] in model_text(ctx),
              f"the whole text area was dropped; model saw: {model_text(ctx)[:200]!r}")
        C.close_all_windows()


# ============================================================ selection

DOC = ("Hello team, the meeting moved to 3pm. Card 4111 1111 1111 1111 is the "
       "company card. Your code is 482913. Password hunter2 for the wifi.")


def _select(win, tv, sub: str | None):
    win.makeFirstResponder_(tv)
    pump(0.15)
    if sub is None:
        tv.setSelectedRange_(NSMakeRange(0, 0))
    else:
        i = DOC.index(sub) if sub in DOC else 0
        tv.setSelectedRange_(NSMakeRange(i, len(sub)))
    pump(0.2)


def t_selection():
    win = make_window("Notes", w=600, h=340)
    tv = text_view(win, DOC)
    show(win)
    _select(win, tv, "the meeting moved to 3pm")
    ctx = guarded_context()
    check("selection: selected substring returned exactly",
          ctx["selected"] == "the meeting moved to 3pm", repr(ctx["selected"]))
    check("selection: selected text leads the model text",
          model_text(ctx).find("Selected text:\nthe meeting moved") > 0)
    _select(win, tv, None)
    ctx = guarded_context()
    check("selection: none selected -> ''", ctx["selected"] == "", repr(ctx["selected"]))
    for sub, why in (("4111 1111 1111 1111", "card number"),
                     ("Your code is 482913", "OTP"),
                     ("Password hunter2", "password (keyword control)")):
        _select(win, tv, sub)
        ctx = guarded_context()
        check(f"selection containing a secret [{why}] is withheld from selected",
              sub not in (ctx["selected"] or ""), f"LEAK: selected={ctx['selected']!r}")
    # The text area as a whole contains 'Password', so the entire document is
    # withheld from visible text: record it (this is the over-blocking side).
    note("selection doc: the NSTextView holds 'Password', so visible text withholds "
         f"the whole document: {'meeting moved' not in guarded_context()['visible']['text']}")


def t_selection_huge():
    big = "".join(f"s{i:06d} " for i in range(25000))  # 200k chars
    win = make_window("Big selection", w=600, h=340)
    tv = text_view(win, big)
    show(win)
    win.makeFirstResponder_(tv)
    tv.setSelectedRange_(NSMakeRange(0, len(big)))
    pump(0.3)
    t0 = time.time()
    ctx = guarded_context()
    dt = time.time() - t0
    note(f"huge selection: selected={len(ctx['selected'])} chars (uncapped in screen.py), "
         f"model text={len(model_text(ctx))} chars, context()={dt * 1000:.0f}ms")
    check("huge selection: model text still capped at 6000", len(model_text(ctx)) <= 6000)


# ============================================================ latency / wake / grants

def t_latency():
    rows = []

    def measure(label, build, n=15):
        win = build()
        show(win)
        guarded_context()  # warm
        s = []
        for _ in range(n):
            t0 = time.time()
            guarded_context()
            s.append(time.time() - t0)
        rows.append(C.latency_row(label, s))
        C.close_all_windows()
        return s

    def normal():
        w = make_window("Latency normal")
        for i in range(40):
            w.contentView().addSubview_(static_label((20, 20 + i * 7, 400, 12), f"line {i} text"))
        return w

    def many():
        w = make_window("Latency 5000", w=600, h=400)
        for i in range(5000):
            w.contentView().addSubview_(static_label((0, 0, 40, 14), f"item {i}"))
        return w

    def huge_tv():
        w = make_window("Latency 200k textview", w=600, h=400)
        text_view(w, "".join(f"line {i:06d} of text. " for i in range(10000))[:200_000])
        return w

    def deep():
        w = make_window("Latency deep 80", w=500, h=360)
        _nest(w.contentView(), 80, {80: "deep"})
        return w

    normal_s = measure("context() normal window (40 labels)", normal)
    many_s = measure("context() 5000 labels", many)
    measure("context() 200k-char NSTextView", huge_tv)
    measure("context() 80-deep AX groups", deep)
    for r in rows:
        note("latency " + r)
    check("latency: normal window context() p95 < 500ms", C.pct(normal_s, .95) < 0.5,
          f"p95={C.pct(normal_s, .95) * 1000:.0f}ms")
    check("latency: 5000-label context() p95 < 2.5s (1.5s budget + overhead)",
          C.pct(many_s, .95) < 2.5, f"p95={C.pct(many_s, .95) * 1000:.0f}ms")


def t_wake_electron_once():
    """_wake_electron's 0.35s wait must happen once per pid, not per read. Real AX
    for a Cocoa app refuses AXManualAccessibility (-25205, measured) and never
    sleeps, so the Electron path is driven with a fake ax whose set() succeeds."""
    calls = []
    fake = {"set": lambda root, attr, val: calls.append(attr) or 0}
    pid = 999_999_01
    screen._WOKEN.discard(pid)
    times = []
    for _ in range(5):
        t0 = time.time()
        screen._wake_electron(fake, object(), pid)
        times.append(time.time() - t0)
    note("wake_electron (fake Electron pid) per-call ms: " +
         ", ".join(f"{t * 1000:.0f}" for t in times))
    check("wake: first call sleeps ~0.35s", 0.3 < times[0] < 0.6, times[0])
    check("wake: later calls for the same pid do not sleep again",
          all(t < 0.05 for t in times[1:]), times[1:])
    check("wake: AXManualAccessibility is set exactly once per pid", len(calls) == 1, calls)
    # Real process: the first context() after a fresh pid in _WOKEN.
    screen._WOKEN.discard(C.MY_PID)
    win = make_window("Wake real")
    show(win)
    t0 = time.time()
    guarded_context()
    first = time.time() - t0
    t0 = time.time()
    guarded_context()
    second = time.time() - t0
    note(f"wake real Cocoa pid: first context() {first * 1000:.0f}ms, second {second * 1000:.0f}ms")


def t_screen_recording_absent():
    granted = screen.permissions()["screen_recording"]
    png = screen.screenshot_png()
    if granted:
        note("Screen Recording IS granted to this interpreter: absent-grant case not exercised")
        check("screenshot: returns bytes when granted", isinstance(png, bytes))
    else:
        check("screenshot: returns None cleanly without Screen Recording", png is None)
        win = make_window("No grant")
        win.contentView().addSubview_(static_label((20, 200, 300, 24), "text still readable"))
        show(win)
        ctx = guarded_context()
        check("screenshot: has_image False without the grant", ctx["has_image"] is False)
        check("screenshot: text still read without the grant",
              "text still readable" in ctx["visible"]["text"])


R = C.REPEAT
TESTS = [
    ("content_rtl_mixed", t_content_rtl_mixed, R),
    ("huge_static_200k", t_huge_static_200k, R),
    ("huge_textview_200k", t_huge_textview_200k, R),
    ("many_5000", t_many_5000, R),
    ("deep_nesting_80", t_deep_nesting_80, R),
    ("empty_and_images_only", t_empty_and_images_only, R),
    ("secret_fields", t_secret_fields, R),
    ("secure_focused_selected", t_secure_focused_selected, R),
    ("plain_text_secrets", t_plain_text_secrets, R),
    ("overblocking", t_overblocking, R),
    ("selection", t_selection, R),
    ("selection_huge", t_selection_huge, R),
    ("wake_electron_once", t_wake_electron_once, 1),
    ("screen_recording_absent", t_screen_recording_absent, R),
    ("latency", t_latency, 1),
]

if __name__ == "__main__":
    import sys
    sys.exit(C.run(TESTS, "adversarial screen.py suite"))
