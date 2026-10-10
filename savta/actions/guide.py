"""Guide mode: MicMic walks her through a task on screen, one step at a time, while
she does each step herself.

She is on a website (a cloud console, a bank, a settings page) and wants to get
something done there that she does not know how to do. The browser agent (web.py)
would do it in a browser of its own; this does the opposite and stays beside her in
hers: it says the next thing to do in one short sentence, rings the control to press
on the real screen, waits while she does it, notices that she did, and says the next.

Where the cost goes, deliberately:
  * One Gemini call per step: a picture of the front window plus a short list of the
    controls in it (id, role, label), the goal, and the steps already given. It
    returns the sentence AND the id of the control to point at. The id must be one of
    the ids it was given; anything else means no ring, never a guessed one.
  * Nothing at all while she works: the watcher reads a handful of accessibility
    attributes every TICK seconds (the page's address, the window title, how many
    top-level things are on the page, and the target control itself) and only asks
    for the next step once the screen has changed and held still, or she says done.

The screen goes to the model only while a guide runs, and never while a password or
card field has the keyboard: then only the list of controls goes, without a picture.
Everything read off the screen passes through screen.redact() first.

Forms (the Larkline check-in rehearsal, 2026-10-08): a field step says WHAT to enter
and where she finds it, never just "click the field". The same step call returns, for
a field, a short name for what goes there, the question to ask her for it, the value
when she already said it while being guided (checked against her own words: never
made up), which of her profile details fits, and the next few fields on the same page,
which are then given without a model call. The watcher moves on when the field's value
changed and she left it or stopped typing; a click into it with nothing typed waits
quietly, with one gentle nudge after a while. "Do it for me" types a value MicMic
knows (her profile at once; one she said, read back first), asks for one it does not,
and never types into a password, card, PIN or one-time-code field, nor presses a
button that finishes something (Check in now, Pay, Submit, I confirm).

Seams, so a test needs no screen, no model and no speaker: GUIDE.screen (front,
snapshot, screenshot, probe, press, find), GUIDE.model(request) -> dict, and
GUIDE.speaker(text, lang). The overlay is not here at all: native/guide_overlay.py
polls state() and draws the ring from state()["ring"].
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid

from . import apps, screen
from .web import FINAL_BUTTON, FORBIDDEN_FIELD

# Model calls in one guide. A step given from the queue of fields the last call
# planned (see _from_queue) costs nothing and is not counted: the Larkline check-in
# alone is ~35 steps, most of them fields.
MAX_STEPS = 60
MAX_SECONDS = 20 * 60
TICK = 0.3
# The screen has to hold still this long after a change before the next step is
# worked out: a page that is still loading would otherwise be described half built.
SETTLE = 0.9
SETTLE_MAX = 4.0
# How long "Guide me" stays on offer after a how-to question was answered.
OFFER_TTL = 120.0
# How long a caller that asked out loud waits for the step it will speak.
WAIT_FOR_STEP = 30.0
MAX_CONTROLS = 140
WALK_NODES = 5000
WALK_SECONDS = 1.5
TEXT_CHARS = 1500
SHOT_PX = 1280
MIN_RING = 6.0
# A field she typed into is filled once she leaves it, or once its words have held
# still this long with the keyboard still in it (she typed and waits to hear more).
FIELD_IDLE = 2.5
# A field step with nothing typed into it gets one gentle nudge after this long.
NUDGE_AFTER = 20.0
# What she said while being guided, kept for the step call (newest last).
MAX_SAID = 8
# Steps planned ahead on one page by one call, given without another.
MAX_QUEUE = 5

KINDS = ("click", "type", "choose", "secret", "look", "wait")
# Steps that fill something in: typed into, picked from a list, or typed by her alone.
FIELD_KINDS = ("type", "choose", "secret")
ENDED = ("done", "stopped", "failed")
# Her own details a field may ask for, which MicMic may type from her profile.
PROFILE_KEYS = ("first_name", "last_name", "full_name")

# Roles a person presses or types into, as the model is told them.
ROLES = {
    "AXButton": "button", "AXLink": "link", "AXCheckBox": "checkbox",
    "AXRadioButton": "option", "AXMenuButton": "menu button", "AXPopUpButton": "menu",
    "AXMenuItem": "menu item", "AXDisclosureTriangle": "expander", "AXTab": "tab",
    "AXComboBox": "field", "AXTextField": "field", "AXTextArea": "text box",
    "AXSearchField": "search field", "AXRow": "row", "AXCell": "cell",
    "AXSlider": "slider", "AXIncrementor": "stepper", "AXSwitch": "switch",
}
TYPED = {"AXComboBox", "AXTextField", "AXTextArea", "AXSearchField"}
PRESSABLE = set(ROLES) - TYPED
_ATTRS = ["AXRole", "AXSubrole", "AXFrame", "AXTitle", "AXDescription", "AXValue",
          "AXEnabled", "AXChildren", "AXPlaceholderValue", "AXTitleUIElement", "AXHelp",
          "AXRequired"]
_PROBE = ["AXRole", "AXFrame", "AXValue", "AXSelected", "AXExpanded", "AXFocused"]

# A step whose whole point is a secret or a sign-in, a payment, or deleting something
# is never done for her, whatever the control is called.
CREDENTIAL = re.compile(
    r"api.?key|credential|\btoken|secret|password|passcode|\bsign.?in\b|\blog.?in\b|"
    r"two.?factor|2.?step|billing|payment|\bcard\b|סיסמ|התחבר|כניסה|תשלום|כרטיס|пароль|войти|оплат|карт|"
    r"كلمة|تسجيل الدخول|دفع|بطاقة", re.I)
# Buttons and boxes that finish something or speak for her: she presses or ticks
# those herself, like a purchase (web.FINAL_BUTTON). "Check in now" commits the
# check-in; "I confirm these details are correct" is her word, not MicMic's.
FINISHING = re.compile(
    r"check.?in now|complete (?:your |the )?check.?in|continue to payment|\bpay and\b|"
    r"\bsubmit\b|\bsend\b|\bi confirm\b|\bi understand\b|\bi agree\b|\bi accept\b|"
    r"אני מאשר|אני מאשרת|אני מסכים|אני מסכימה|שליחה|לשלוח|"
    r"أؤكد|أوافق|إرسال|подтверждаю|согласен|согласна|отправить", re.I)

# The part of web.FORBIDDEN_FIELD that is an identity number rather than a secret.
_ID_NUMBER = re.compile(
    r"passport|national.?id|tax.?id|\bssn\b|social.?security|iban|account.?num|routing|"
    r"teudat|תעודת.?זהות|דרכון|رقم.?الهوية|جواز.?السفر|паспорт|снилс", re.I)

# A field the page itself calls optional ("Known Traveler Number (optional)"): never a
# step of its own unless she asked for it (the rehearsal stalled 33 s on one).
_OPTIONAL = re.compile(r"\boptional\b|\bif (?:you )?(?:have|any)\b|אופציונלי|לא חובה|اختياري|"
                       r"необязательн", re.I)

# MicMic's own windows: "this" is never MicMic itself.
_MICMIC = ("micmic",)


# ---------------------------------------------------------------- what she hears
SAY = {
    "stopped": {"hebrew": "בסדר, הפסקתי להדריך.", "arabic": "ماشي، وقّفت الإرشاد.",
                "russian": "Хорошо, я остановила подсказки.",
                "english": "Alright, I have stopped guiding."},
    "blocked": {"hebrew": "אני עדיין לא יכולה לראות את המסך. בהגדרות המערכת, תחת פרטיות ואבטחה ואז נגישות, צריך להדליק את MicMic.",
                "arabic": "لسا ما بقدر أشوف الشاشة. بإعدادات النظام، الخصوصية والأمان ثم تسهيلات الاستخدام، شغّل«ي|» MicMic.",
                "russian": "Я пока не вижу экран. В Системных настройках, в разделе Конфиденциальность и безопасность, Универсальный доступ, включите MicMic.",
                "english": "I cannot see your screen yet. In System Settings, under Privacy and Security then Accessibility, turn MicMic on."},
    "no_window": {"hebrew": "אני לא רואה חלון פתוח שאפשר להדריך בו.",
                  "arabic": "ما بشوف شباك مفتوح بقدر ساعد فيه.",
                  "russian": "Я не вижу открытого окна, в котором можно подсказать.",
                  "english": "I cannot see an open window to guide you through."},
    "no_llm": {"hebrew": "אני לא יכולה להדריך כרגע. אפשר לנסות שוב עוד מעט.",
               "arabic": "ما بقدر ساعد هلّق. جرّب«ي|» كمان شوي.",
               "russian": "Сейчас я не могу подсказывать. Попробуйте чуть позже.",
               "english": "I cannot guide you right now. Try again in a little while."},
    "too_long": {"hebrew": "עברנו הרבה שלבים, אז אני עוצרת כאן. אפשר לבקש שוב ונמשיך.",
                 "arabic": "مرقنا خطوات كتير، فبوقّف هون. اطلب«ي|» كمان مرة ومنكمّل.",
                 "russian": "Мы прошли много шагов, я остановлюсь здесь. Попросите ещё раз, и продолжим.",
                 "english": "We have been through a lot of steps, so I will stop here. Ask me again and we will carry on."},
    "finished": {"hebrew": "זהו, סיימנו.", "arabic": "خلص، خلّصنا.",
                 "russian": "Всё, готово.", "english": "That's done."},
    "no_back": {"hebrew": "זה השלב הראשון, אין לאן לחזור.",
                "arabic": "هاي أول خطوة، ما في قبلها.",
                "russian": "Это первый шаг, возвращаться некуда.",
                "english": "This is the first step, there is nothing to go back to."},
    "no_why": {"hebrew": "זה השלב הבא בדרך למה שביקשת.",
               "arabic": "هاي الخطوة الجاية للي طلبت«ي|».",
               "russian": "Это следующий шаг к тому, что вы просили.",
               "english": "It is the next step toward what you asked for."},
    "do_refused": {"hebrew": "את השלב הזה עדיף שתעש«י|» בעצמך.",
                   "arabic": "هالخطوة أحسن تعمل«ي|»ها إنت«ي|».",
                   "russian": "Этот шаг лучше сделать вам самим.",
                   "english": "This step is one you should do yourself."},
    "did_it": {"hebrew": "עשיתי את זה.", "arabic": "عملتها.",
               "russian": "Сделала.", "english": "Done that for you."},
    "not_guiding": {"hebrew": "אני לא מדריכה כרגע.", "arabic": "أنا مش عم ساعد هلّق.",
                    "russian": "Я сейчас ничего не подсказываю.",
                    "english": "I am not guiding you through anything right now."},
    # Filling in a form ("Do it for me" on a field, and what she says to it).
    "ask_value": {"hebrew": "מה להקליד כאן?", "arabic": "شو أكتب هون؟",
                  "russian": "Что сюда ввести?", "english": "What should I type here?"},
    "confirm_value": {"hebrew": "אקליד {v}. נכון?", "arabic": "رح أكتب {v}. صح؟",
                      "russian": "Введу {v}. Верно?", "english": "I'll type {v}. Is that right?"},
    "typed": {"hebrew": "הקלדתי.", "arabic": "كتبتها.", "russian": "Ввела.",
              "english": "Typed it in."},
    "type_failed": {"hebrew": "לא הצלחתי להקליד שם, אז כדאי להקליד את זה ידנית.",
                    "arabic": "ما قدرت أكتب هون، أحسن تكتب«ي|»ها إنت«ي|».",
                    "russian": "Не получилось ввести туда, введите, пожалуйста, сами.",
                    "english": "I couldn't type it there, so please type it yourself."},
    "say_again": {"hebrew": "סליחה, לא קלטתי. אפשר להגיד שוב?",
                  "arabic": "عفواً، ما سمعت منيح. فيك تعيد«ي|»ها؟",
                  "russian": "Простите, я не расслышала. Повторите, пожалуйста?",
                  "english": "Sorry, I didn't catch it. Could you say it again?"},
    "what_instead": {"hebrew": "בסדר. מה צריך להיות שם?", "arabic": "ماشي. شو لازم يكون؟",
                     "russian": "Хорошо. Что там должно быть?",
                     "english": "Okay. What should it be?"},
    "do_secret": {"hebrew": "אני אף פעם לא מקלידה סיסמאות, מספרי כרטיס או קודי אבטחה, אז את זה צריך להקליד ידנית.",
                  "arabic": "أنا ما بكتب أبداً كلمات سر أو أرقام بطاقات أو رموز أمان، فأحسن تكتب«ي|»ها إنت«ي|».",
                  "russian": "Я никогда не ввожу пароли, номера карт и коды безопасности, поэтому это введите, пожалуйста, сами.",
                  "english": "I never type passwords, card numbers or security codes, so please type this one yourself."},
    "do_private": {"hebrew": "מספרי דרכון ותעודות מזהות נשארים פרטיים, אז את זה צריך להקליד ידנית.",
                   "arabic": "أرقام جواز السفر والهوية بتضل خاصة، فأحسن تكتب«ي|»ها إنت«ي|».",
                   "russian": "Номера паспортов и удостоверений остаются в тайне, поэтому это введите, пожалуйста, сами.",
                   "english": "Passport and ID numbers stay private, so please type this one yourself."},
    "do_final": {"hebrew": "הכפתור הזה מסיים את הכול, אז עליו לוחצים לבד, כשמוכנים.",
                 "arabic": "هالزر بيخلّص كل شي، فاضغط«ي|» عليه إنت«ي|» لما تكون«ي|» جاهز«ة|».",
                 "russian": "Эта кнопка всё завершает, поэтому нажмите её сами, когда будете готовы.",
                 "english": "This one finishes it, so press it yourself when you're ready."},
    "choose_self": {"hebrew": "את זה בוחרים מהרשימה לבד: פותחים אותה ובוחרים.",
                    "arabic": "هاي اختار«ي|»ها من القائمة بنفسك: افتح«ي|»ها واختار«ي|».",
                    "russian": "Это выберите, пожалуйста, в списке сами: откройте его и выберите.",
                    "english": "Please pick this one from the list yourself: open it and choose."},
    "noted": {"hebrew": "קיבלתי, אשתמש בזה כשנגיע לשם.", "arabic": "تمام، رح أستعملها لما نوصل لهناك.",
              "russian": "Поняла, использую это, когда дойдём.",
              "english": "Got it, I'll use that when we get there."},
    "choice_hers": {"hebrew": "את הבחירה הזאת עושים לבד: בוחרים את מה שמתאים.",
                    "arabic": "هالاختيار إلك«ي|»، اختار«ي|» اللي بيناسبك.",
                    "russian": "Этот выбор за вами: выберите то, что подходит.",
                    "english": "This choice is yours, so pick the one that fits you."},
    "nudge_type": {"hebrew": "אין לחץ. כשמוכנים, מקלידים את זה בתיבה שסימנתי.",
                   "arabic": "خذ«ي|» وقتك. لما تكون«ي|» جاهز«ة|»، اكتب«ي|»ها بالخانة اللي علّمتها.",
                   "russian": "Не торопитесь. Когда будете готовы, введите это в поле, которое я обвела.",
                   "english": "Take your time. When you're ready, type it in the box I circled."},
    "nudge_tell": {"hebrew": "או שאפשר להגיד לי, ואני אקליד.",
                   "arabic": "أو احكيلي«|» وأنا بكتبها.",
                   "russian": "Или скажите мне, и я введу это за вас.",
                   "english": "Or tell me, and I'll type it for you."},
    "nudge_choose": {"hebrew": "אין לחץ. כשמוכנים, בוחרים מהרשימה שסימנתי.",
                     "arabic": "خذ«ي|» وقتك. لما تكون«ي|» جاهز«ة|»، اختار«ي|» من القائمة اللي علّمتها.",
                     "russian": "Не торопитесь. Когда будете готовы, выберите это в списке, который я обвела.",
                     "english": "Take your time. When you're ready, pick it from the list I circled."},
}


def _line(key: str, lang: str, gender: str = "", **fmt) -> str:
    t = SAY[key]
    text = t.get(lang, t["english"])
    if fmt:
        text = text.format(**fmt)
    try:
        from ..router import degender
        return degender(text, gender)
    except Exception:  # noqa: BLE001
        return re.sub(r"«([^«»|]*)\|([^«»|]*)»", r"\1", text)


def _clean_sentence(text, limit: int = 220) -> str:
    """One line to show and say: no markdown, no em or en dashes (a comma reads the
    same aloud), no quotes the model wrapped around the whole thing."""
    s = str(text or "").strip()
    s = re.sub(r"[*_#`]+", "", s)
    s = re.sub(r"\s*[—–]\s*", ", ", s)
    s = re.sub(r"\s+", " ", s).strip().strip('"').strip()
    return s[:limit]


# ---------------------------------------------------------------- the real screen
def _val(v):
    """An attribute from a multiple-attribute read: a missing one comes back as an
    AXValue holding the error, which is None here."""
    try:
        from ApplicationServices import AXValueGetType, kAXValueAXErrorType
        if type(v).__name__.startswith("AXValue") and AXValueGetType(v) == kAXValueAXErrorType:
            return None
    except Exception:  # noqa: BLE001
        pass
    return v


def _rect(v):
    """(x, y, w, h) in the accessibility space (top-left of the main screen is 0,0,
    y grows downward), or None."""
    v = _val(v)
    if v is None:
        return None
    try:
        from ApplicationServices import AXValueGetValue, kAXValueCGRectType
        ok, r = AXValueGetValue(v, kAXValueCGRectType, None)
        if not ok:
            return None
        return (float(r.origin.x), float(r.origin.y), float(r.size.width), float(r.size.height))
    except Exception:  # noqa: BLE001
        return None


def _many(el, names):
    try:
        from ApplicationServices import AXUIElementCopyMultipleAttributeValues
        err, vals = AXUIElementCopyMultipleAttributeValues(el, names, 0, None)
        if err != 0 or vals is None:
            return None
        return [_val(v) for v in vals]
    except Exception:  # noqa: BLE001
        return None


def _visible_part(r, win):
    """The part of r inside the window, or None when too little of it shows to point
    at honestly (under a quarter of it, or a sliver). Half was the rule until the
    real-Chrome rehearsal of 2026-10-11: the box next to "Alex Rivera" sat at the
    window's bottom edge, was left out of the list, and the step had no ring."""
    if not r or not win:
        return None
    x, y, w, h = r
    wx, wy, ww, wh = win
    if w < MIN_RING or h < MIN_RING:
        return None
    lx, ty = max(x, wx), max(y, wy)
    rx, by = min(x + w, wx + ww), min(y + h, wy + wh)
    if rx - lx < MIN_RING or by - ty < MIN_RING:
        return None
    if (rx - lx) * (by - ty) < 0.25 * w * h:
        return None
    return (lx, ty, rx - lx, by - ty)


def _intersects(r, win) -> bool:
    if not r or not win or r[2] <= 0 or r[3] <= 0:
        return True           # a container with no size of its own may hold visible things
    return not (r[0] + r[2] <= win[0] or r[0] >= win[0] + win[2]
                or r[1] + r[3] <= win[1] or r[1] >= win[1] + win[3])


class LiveScreen:
    """The front window, read through Accessibility. Every method is cheap enough to
    call from the watcher except snapshot(), which is called once per step."""

    def front(self) -> dict | None:
        """The app she is working in: the frontmost one, or, when MicMic itself is in
        front (she clicked its window), the nearest ordinary window behind it."""
        fm = screen.frontmost()
        if fm.get("pid") and not _is_micmic(fm.get("app", ""), fm.get("bundle_id", "")):
            return fm
        pid = _front_window_pid(skip_names=_MICMIC)
        if not pid:
            return None
        name = ""
        for row in _window_rows():
            if int(row.get("kCGWindowOwnerPID", -1)) == pid:
                name = str(row.get("kCGWindowOwnerName") or "")
                break
        return {"app": name, "pid": pid, "bundle_id": "", "window": ""}

    def snapshot(self, front: dict) -> dict:
        """Every control a person could press or type into in the visible part of the
        front window, numbered, with where it is. Refs stay here, never in state."""
        out = {"app": front.get("app", ""), "pid": front.get("pid"), "window": "",
               "url": "", "title": "", "controls": [], "refs": {}, "raw": {}, "text": "",
               "win": None, "secure_focus": False, "browser": False, "error": None}
        ax, root = screen._app_root(front.get("pid") or 0)
        if ax is None or root is None:
            out["error"] = "no_access"
            return out
        if not ax["trusted"]():
            out["error"] = "no_access"
            return out
        window = apps.find_window(ax, root)
        if window is None:
            out["error"] = "no_window"
            return out
        top = _many(window, ["AXTitle", "AXFrame"]) or [None, None]
        out["window"] = screen.redact(screen._clean(str(top[0] or "")))
        win = _rect(top[1])
        out["win"] = win
        app = front.get("app", "").strip().lower()
        out["browser"] = app in screen.BROWSER_NAMES or "chrom" in app
        out["_window"] = window
        out["_root"] = root
        # Whatever has the keyboard: a password or card field there means no picture.
        focused = apps._attr(ax, root, "AXFocusedUIElement")
        if focused is not None:
            f = _many(focused, ["AXRole", "AXSubrole", "AXTitle", "AXDescription",
                                "AXPlaceholderValue"]) or []
            f = (f + [None] * 5)[:5]
            role, sub = str(f[0] or ""), str(f[1] or "")
            label = " ".join(str(x) for x in f[2:] if isinstance(x, str))
            out["secure_focus"] = screen._is_secure(role, sub) or screen._forbidden(
                role, sub, label, "")

        started, seen = time.time(), 0
        texts: list[str] = []
        nid = 0

        def label_of(vals, el) -> str:
            role, title, desc, value, ph, tui = (vals[0], vals[3], vals[4], vals[5],
                                                 vals[8], vals[9])
            for v in (title, desc):
                if isinstance(v, str) and v.strip():
                    return v.strip()
            if tui is not None:
                t = _many(tui, ["AXValue", "AXTitle", "AXDescription"]) or []
                for v in t:
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            if isinstance(ph, str) and ph.strip():
                return ph.strip()
            if role not in TYPED and isinstance(value, str) and value.strip():
                return value.strip()
            # A link or a row whose words are its children's (Chrome, Finder's sidebar)
            return _child_text(el)

        def walk(el, depth, group=""):
            nonlocal seen, nid
            if depth > screen.MAX_DEPTH or seen >= WALK_NODES \
                    or time.time() - started > WALK_SECONDS:
                return
            vals = _many(el, _ATTRS)
            seen += 1
            if vals is None:
                return
            role, sub = str(vals[0] or ""), str(vals[1] or "")
            # A named set of fields (a web page's fieldset and legend, "Expiration
            # date" around Month, Day and Year): its name tells the fields apart.
            if role in ("AXGroup", "AXRadioGroup"):
                for g in (vals[3], vals[4]):
                    if isinstance(g, str) and 0 < len(g.strip()) <= 80:
                        group = screen._clean(g)
                        break
            frame = _rect_from(vals[2])
            if not _intersects(frame, win):
                return
            if role == "AXWebArea" and "_web" not in out:
                out["_web"] = el
                url = apps._attr(ax, el, "AXURL")
                url_s = str(url.absoluteString()) if hasattr(url, "absoluteString") else str(url or "")
                out["url"] = screen.redact(screen._clean(url_s))
                out["title"] = screen.redact(screen._clean(str(vals[3] or "")))
            if role in ("AXStaticText", "AXHeading") and sum(map(len, texts)) < TEXT_CHARS:
                t = vals[5] if isinstance(vals[5], str) else vals[3]
                if isinstance(t, str) and t.strip() and not screen._forbidden(role, sub, "", t):
                    texts.append(screen._clean(t)[:160])
            kind_role = "AXTab" if sub == "AXTabButton" else ("AXSwitch" if sub == "AXSwitch" else role)
            if kind_role in ROLES and vals[6] is not False and len(out["controls"]) < MAX_CONTROLS:
                secure = screen._is_secure(role, sub)
                label = screen._clean(label_of(vals, el))[:90]
                value = ""
                # A field's words, and the choice a list (a web page's <select>) shows.
                if (role in TYPED or role == "AXPopUpButton") and not secure \
                        and isinstance(vals[5], str):
                    value = screen._clean(vals[5])[:40]
                # What the page says about the field beside its name: its hint
                # (aria-describedby reaches AXHelp in Chrome) or its placeholder.
                hint = ""
                for h in (vals[10], vals[8]):
                    if isinstance(h, str) and h.strip() and screen._clean(h) != label:
                        hint = screen._clean(h)[:90]
                        break
                forbidden = secure or screen._forbidden(role, sub, label, value) \
                    or bool(FORBIDDEN_FIELD.search(hint)) \
                    or (role in TYPED and bool(CREDENTIAL.search(group)))
                checked = None
                if kind_role in ("AXCheckBox", "AXRadioButton", "AXSwitch") \
                        and isinstance(vals[5], (int, float)):
                    checked = bool(vals[5])
                vis = _visible_part(frame, win) if frame else None
                if (label or role in TYPED) and vis:
                    nid += 1
                    out["controls"].append({
                        "id": nid, "role": ROLES[kind_role], "ax_role": role,
                        "label": screen.redact(label),
                        "value": "" if forbidden else screen.redact(value),
                        "hint": "" if not hint else screen.redact(hint),
                        "group": screen.redact(group) if group and group != label else "",
                        "checked": checked,
                        "frame": vis, "secure": bool(forbidden),
                        "typed": role in TYPED, "pick": role == "AXPopUpButton",
                        "required": vals[11] is True or vals[11] == 1,
                        "optional": bool(_OPTIONAL.search(f"{label} {hint}"))})
                    out["refs"][nid] = el
                    if "_web" in out and "_anchor" not in out:
                        # The first control on the page: when it moves, she scrolled.
                        out["_anchor"] = el
                    # What probe() will read in the field, kept here and never sent:
                    # the watcher's starting point (see _watch).
                    if role in TYPED or role == "AXPopUpButton":
                        v = vals[5] if isinstance(vals[5], (str, int, float)) else None
                        out["raw"][nid] = (len(v) if isinstance(v, str) else None) if forbidden else v
                        if forbidden and role in TYPED:
                            # Whether a secret field holds anything, never what.
                            out["controls"][-1]["filled"] = bool(out["raw"][nid])
            kids = vals[7]
            if kids:
                for k in list(kids):
                    walk(k, depth + 1, group)

        walk(window, 0)
        out["text"] = screen.redact(" | ".join(screen._dedupe(texts)))[:TEXT_CHARS]
        return out

    def screenshot(self, pid: int) -> bytes | None:
        return screen.screenshot_png(max_px=SHOT_PX, pid=pid)

    def probe(self, snap: dict, target_id) -> dict:
        """What the watcher compares, tick to tick. No model, a few attribute reads."""
        p = {"pid": _front_window_pid(skip_names=_MICMIC), "title": None, "url": None,
             "struct": None, "alive": None, "state": None, "frame": None, "focus_key": None}
        # Where the keyboard is: a page that answers a press by moving it (an error
        # summary after Save, a dialog) has reacted even when nothing else moved.
        root = snap.get("_root")
        if root is not None:
            f = _many(root, ["AXFocusedUIElement"])
            el = f[0] if f else None
            if el is not None:
                k = _many(el, ["AXRole", "AXTitle", "AXDescription", "AXFrame"]) or []
                p["focus_key"] = tuple(str(x)[:60] if isinstance(x, str) else repr(_rect(x))
                                       for x in k)
        window = snap.get("_window")
        if window is not None:
            w = _many(window, ["AXTitle", "AXChildren", "AXFrame"])
            if w is not None:
                p["title"] = w[0]
                p["struct"] = [len(w[1] or [])]
                p["win"] = _rect(w[2])
        web = snap.get("_web")
        if web is not None:
            w = _many(web, ["AXURL", "AXChildren", "AXTitle"])
            if w is None:
                p["url"] = "gone"
            else:
                url = w[0]
                p["url"] = str(url.absoluteString()) if hasattr(url, "absoluteString") else str(url)
                p["struct"] = (p["struct"] or []) + [len(w[1] or []), str(w[2] or "")]
        ref = (snap.get("refs") or {}).get(target_id) if target_id else None
        anchor = snap.get("_anchor")
        if ref is None and anchor is not None:
            fr = _rect((_many(anchor, ["AXFrame"]) or [None])[0])
            p["anchor"] = round(fr[1]) if fr else None
        if ref is not None:
            t = _many(ref, _PROBE)
            if t is None or not t[0]:
                p["alive"] = False
            else:
                p["alive"] = True
                secure = any(c.get("id") == target_id and c.get("secure")
                             for c in snap.get("controls") or [])
                value = t[2] if isinstance(t[2], (str, int, float)) else None
                if secure:
                    # Only how long it is, to see that she typed: never the words.
                    value = len(value) if isinstance(value, str) else None
                p["state"] = (value, t[3], t[4])
                p["focused"] = bool(t[5])
                p["frame"] = _rect(t[1])
        return p

    def press(self, snap: dict, target_id) -> tuple[bool, str]:
        ref = (snap.get("refs") or {}).get(target_id)
        ax = apps._ax()
        if ref is None or ax is None:
            return False, "stale"
        try:
            err = ax["press"](ref, "AXPress")
        except Exception as e:  # noqa: BLE001
            return False, repr(e)[:100]
        return err == 0, "ok" if err == 0 else f"AX {err}"

    def fill(self, snap: dict, target_id, value: str) -> tuple[bool, str]:
        """Type `value` into the ringed field. First the field's own AXValue, read back.
        Real Chrome took none of it in the rehearsal of 2026-10-11 ("I couldn't type it
        there"), so then: her browser in front, the keyboard in that very field (AX
        focus, else a click on it), select what is in it, and type the value as
        keystrokes, read back again. Keystrokes go only to that app and only into that
        field: if either cannot be made sure of, nothing is typed. Never a secret field,
        and never a Return, which could submit the form."""
        ref = (snap.get("refs") or {}).get(target_id)
        ctl = next((c for c in snap.get("controls") or [] if c["id"] == target_id), None)
        ax = apps._ax()
        if ref is None or ax is None or ctl is None:
            return False, "stale"
        if ctl.get("secure") or not ctl.get("typed"):
            return False, "refused"
        value = re.sub(r"[\r\n]+", " ", str(value or "")).strip()
        try:
            ax["set"](ref, "AXFocused", True)
            ax["set"](ref, "AXValue", value)
        except Exception:  # noqa: BLE001
            pass
        if _holds(ref, value):
            return True, "ok"
        pid = snap.get("pid") or 0
        if not _bring_to_front(pid):
            return False, "not in front"
        try:
            # Its window the key one, so the keys reach this page and not the toolbar.
            win = (_many(ref, ["AXWindow"]) or [None])[0]
            if win is not None:
                ax["set"](win, "AXMain", True)
                ax["press"](win, "AXRaise")
            ax["set"](ref, "AXFocused", True)
        except Exception:  # noqa: BLE001
            pass
        if not _has_focus(ref):
            fr = _rect((_many(ref, ["AXFrame"]) or [None])[0])
            if fr and _front_pid() == pid:
                _click(fr[0] + min(fr[2] / 2, 40), fr[1] + fr[3] / 2)
                time.sleep(0.15)
        if not _has_focus(ref) or _front_pid() != pid:
            return False, "no focus"
        _keys(pid, select_all=True)
        _keys(pid, text=value)
        end = time.time() + 1.0
        while time.time() < end:
            if _holds(ref, value):
                return True, "typed"
            time.sleep(0.05)
        return False, "not taken"

    def page_url(self, front: dict) -> str:
        """The address of the page in the front browser window, or "": a short search
        for its web area, far cheaper than snapshot(). Needs Accessibility."""
        ax, root = screen._app_root(front.get("pid") or 0)
        if ax is None or root is None or not ax["trusted"]():
            return ""
        window = apps.find_window(ax, root)
        if window is None:
            return ""
        started, seen, todo = time.time(), 0, [window]
        while todo and seen < 800 and time.time() - started < 0.6:
            el = todo.pop(0)
            seen += 1
            vals = _many(el, ["AXRole", "AXChildren"])
            if not vals:
                continue
            if str(vals[0] or "") == "AXWebArea":
                url = apps._attr(ax, el, "AXURL")
                return screen._clean(str(url.absoluteString()) if hasattr(url, "absoluteString")
                                     else str(url or ""))
            todo.extend(list(vals[1] or []))
        return ""

    def find(self, front: dict, role: str, label: str):
        """A control with this role and label in a fresh read, for going back a step
        without asking the model again. Returns (snapshot, id) or (None, None)."""
        snap = self.snapshot(front)
        for c in snap.get("controls") or []:
            if c["role"] == role and c["label"] == label:
                return snap, c["id"]
        return snap, None


def _holds(ref, value: str) -> bool:
    """The field reads back as `value` (case, spaces and dashes aside: a page may
    format what it was given)."""
    got = (_many(ref, ["AXValue"]) or [None])[0]
    if not isinstance(got, str):
        return False
    norm = lambda t: re.sub(r"[\s\-]+", "", t).lower()  # noqa: E731
    return norm(got) == norm(value)


def _has_focus(ref) -> bool:
    return bool((_many(ref, ["AXFocused"]) or [None])[0])


def _front_pid() -> int:
    try:
        return int(screen.frontmost().get("pid") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _bring_to_front(pid: int) -> bool:
    """Her browser in front, so keystrokes reach it: it usually is already (she is
    talking, not clicking); after a press on MicMic's own card it is asked to come
    back, and checked."""
    if not pid:
        return False
    if _front_pid() == pid:
        return True
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    except Exception:  # noqa: BLE001
        return False
    end = time.time() + 1.0
    while time.time() < end:
        if _front_pid() == pid:
            return True
        time.sleep(0.05)
    return False


def _click(x: float, y: float) -> None:
    """One left click at a screen point (the field's own frame), to put the keyboard in it."""
    import Quartz
    move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y),
                                          Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.05)
    for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
        # A click count of one: without it a page can take the press for no click at all.
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, 1)
        Quartz.CGEventSetFlags(ev, 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.04)


def _keys(pid: int, text: str = "", select_all: bool = False) -> None:
    """Keystrokes to `pid` only: Cmd-A, or `text` one character at a time as Unicode
    (any keyboard layout, any language). No modifier she may be holding is carried
    over, and there is no Return: a newline in a value was turned into a space."""
    import Quartz
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)

    def post(down: bool, char: str = "", flags: int = 0) -> None:
        ev = Quartz.CGEventCreateKeyboardEvent(src, 0, down)   # key code 0, the string decides
        if char:
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(char), char)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPostToPid(pid, ev)
    if select_all:
        post(True, "", Quartz.kCGEventFlagMaskCommand)
        post(False, "", Quartz.kCGEventFlagMaskCommand)
        time.sleep(0.05)
    for ch in text:
        if ch in "\r\n":
            continue
        post(True, ch)
        post(False, ch)
        time.sleep(0.008)


def _rect_from(v):
    return _rect(v) if v is not None else None


def _child_text(el, depth: int = 0) -> str:
    if depth > 3:
        return ""
    vals = _many(el, ["AXChildren"])
    for k in list((vals or [None])[0] or [])[:6]:
        v = _many(k, ["AXRole", "AXValue", "AXTitle", "AXDescription"])
        if not v:
            continue
        for x in v[1:]:
            if isinstance(x, str) and x.strip():
                return x.strip()
        t = _child_text(k, depth + 1)
        if t:
            return t
    return ""


_BROWSER_IDS = ("com.google.chrome", "org.chromium.", "com.apple.safari", "com.microsoft.edge",
                "com.brave.browser", "company.thebrowser.", "com.operasoftware.", "com.vivaldi.",
                "org.mozilla.firefox")
# Not a page she is on: a new tab, a blank page, the browser's own pages.
_NOT_A_PAGE = re.compile(r"^(?:about:|chrome:|chrome-search:|edge:|brave:|favorites:|"
                         r"safari-resource:)|newtab", re.I)


def _is_browser(app: str, bundle: str = "") -> bool:
    a, b = (app or "").strip().lower(), (bundle or "").strip().lower()
    return a in screen.BROWSER_NAMES or "chrom" in a or "firefox" in a \
        or any(b.startswith(x) for x in _BROWSER_IDS)


def _is_micmic(app: str, bundle: str) -> bool:
    return (bundle or "").startswith("com.betterfly.micmic") or \
        (app or "").strip().lower() in _MICMIC


def _window_rows() -> list:
    try:
        import Quartz
        return list(Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or [])
    except Exception:  # noqa: BLE001
        return []


def _front_window_pid(skip_names=()) -> int | None:
    """The owner of the frontmost ordinary window, MicMic's own left out. In-process
    and about a millisecond, so the watcher can ask it every tick."""
    import os
    me = os.getpid()
    for w in _window_rows():                    # front to back
        if int(w.get("kCGWindowLayer", 1)) != 0:
            continue
        pid = int(w.get("kCGWindowOwnerPID", -1))
        name = str(w.get("kCGWindowOwnerName") or "").strip().lower()
        if pid == me or name in skip_names:
            continue
        b = w.get("kCGWindowBounds") or {}
        if float(b.get("Width", 0)) < 80 or float(b.get("Height", 0)) < 60:
            continue
        return pid
    return None


# ---------------------------------------------------------------- the model
_LANG_NAME = {"hebrew": "Hebrew", "arabic": "Arabic", "russian": "Russian",
              "english": "English"}

SYSTEM = (
    "You are MicMic, guiding a person through a task on their Mac one step at a time, "
    "while they do every step themselves. You are shown the window in front of them "
    "(a screenshot when there is one, and always a numbered list of the controls in "
    "it), the goal, and the steps already given.\n"
    "Reply with JSON only, no other words:\n"
    '{{"done": true or false, "say": "...", "target": id or null, "kind": "click" | '
    '"type" | "choose" | "secret" | "look" | "wait", "why": "...", "goal": "...", '
    '"finish": "...", "field": "...", "ask": "...", "value": "...", "profile": "...", '
    '"then": []}}\n'
    "- say: the ONE next thing to do, one short sentence in {language}, at most 16 "
    "words, naming the control exactly as it is labelled on screen and roughly where "
    "it is, for example: Click APIs and Services in the menu on the left.\n"
    "- For a field to fill in (a text box, a list to pick from), say WHAT to enter in "
    "plain words, never only to click or type in the field called X, and add where "
    "they find it whenever that is not obvious (their booking email, the photo page of "
    "their passport, a letter), in at most two short sentences and 24 words, from the "
    "field's label, its hint, the set it is in, the text near it and the page. For "
    "example: Type your 6-character confirmation code. It's in your booking email. "
    "Or: Enter your passport number exactly as printed on the photo page. Or: Type "
    "the month your passport expires, as two digits. Or: Pick the country that "
    "issued your passport.\n"
    "- For a set of options where the right one depends on them (their sex, whether "
    "they have a visa), say to pick the one that fits them, never which one, for "
    "example: Choose the sex shown on your passport. Target the first option.\n"
    "- target: the id of that control from the list, or null when no single listed "
    "control is the place. Only ids from the list. Never guess one. For a field, the "
    "field itself.\n"
    "- kind: click to press a button, a link, a checkbox or an option; type to type "
    "ordinary text into a field; choose to pick from a list; secret when the step is "
    "typing a password, a card number, a security code, a PIN or a one-time code, or "
    "signing in: tell them to type it themselves and never ask them for it; look when "
    "the thing is not visible yet and they should scroll or look somewhere; wait when "
    "the page is still loading.\n"
    "- field: for type, choose or secret, what goes in it in two to five words in "
    "{language}, for example: your confirmation code. Otherwise empty.\n"
    "- ask: for type or choose, the short question that would get it from them, in "
    "{language}, for example: What's your confirmation code? Otherwise empty.\n"
    "- value: for type or choose, exactly what to enter when they already said it in "
    "\"What they said\" below, copied from their words. Otherwise empty. Never guess a "
    "value, never make one up, never use an example from the page.\n"
    "- profile: for a field asking for their own first name, last name or full name: "
    "first_name, last_name or full_name. Otherwise empty.\n"
    "- then: when this step is a field and more things to fill follow in the list, up "
    "to 5 of the next steps in order, across the whole form as far as the list goes "
    "(fields, lists, sets of options, checkboxes), each {{\"say\", \"target\", "
    "\"kind\", \"field\", \"ask\", \"value\", \"profile\"}} written the same way. "
    "Stop before any button or link. Otherwise [].\n"
    "- A field whose list entry says it already holds a value is filled: go on to the "
    "next one unless what it holds is wrong.\n"
    "- Never point at a button that continues, submits or finishes while something on "
    "the page that it needs is still empty or unticked (a required field, the box that "
    "chooses who to check in, an answer to a question): that comes first. If it is "
    "below the visible part, say to scroll down to it.\n"
    "- Skip fields marked [optional] and fields already filled, unless the goal or what "
    "they said asks for them. When everything required is filled, the next step is the "
    "page's main button (Continue, Next, Save).\n"
    "- why: one short sentence in {language} on why this step matters.\n"
    "- goal: the goal restated in two to six words in {language}.\n"
    "- done: true only when the screen shows the goal is achieved; then finish is one "
    "short sentence in {language} saying what was achieved, for example: That's done: "
    "the Maps API is enabled. Otherwise finish is empty.\n"
    "One step only, never two joined together. If the last step did not happen yet, "
    "say it again, more clearly. Plain words: no markdown, no lists, no em dashes.{address}")


def build_request(goal: str, lang: str, history: list[str], snap: dict,
                  reason: str, said: list[str] | None = None) -> str:
    """The user part of the step call, as text. Pure, so tests can read it.
    `said` is what she said to MicMic while being guided, oldest first."""
    win = snap.get("win") or (0, 0, 1, 1)

    def where(fr):
        x = (fr[0] + fr[2] / 2 - win[0]) / max(1.0, win[2])
        y = (fr[1] + fr[3] / 2 - win[1]) / max(1.0, win[3])
        return f"({round(x * 100)}%,{round(y * 100)}%)"

    lines = [f"Goal: {goal}"]
    if history:
        lines.append("Steps already given, oldest first:")
        lines += [f"{i}. {h}" for i, h in enumerate(history, 1)]
    else:
        lines.append("No steps given yet: this is the first.")
    lines.append(f"Why you are asked now: {reason}.")
    if said:
        lines.append("What they said while being guided, oldest first:")
        lines += [f'- "{x}"' for x in said]
    lines.append(f"App: {snap.get('app', '')}   Window: {snap.get('window', '')}")
    if snap.get("url"):
        lines.append(f"Web page: {snap.get('title', '')} ({snap.get('url', '')})")
    lines.append("Controls (id, kind, label, where across and down the window):")
    for c in snap.get("controls") or []:
        extra = (" [secret field]" + {True: " (filled)", False: " (empty)"}.get(c.get("filled"), "")
                 ) if c.get("secure") else (
            f' now says "{c["value"]}"' if c.get("value") else (
                " (empty)" if c.get("typed") else ""))
        if c.get("checked") is not None:
            extra += " [ticked]" if c["checked"] else " [not ticked]"
        if c.get("required"):
            extra += " [required]"
        elif c.get("optional"):
            extra += " [optional]"
        if c.get("hint"):
            extra += f' hint "{c["hint"]}"'
        if c.get("group"):
            extra += f' in "{c["group"]}"'
        lines.append(f'{c["id"]} {c["role"]} "{c["label"]}"{extra} {where(c["frame"])}')
    if not snap.get("controls"):
        lines.append("(none could be read)")
    if snap.get("text"):
        lines.append("Text on screen: " + snap["text"])
    return "\n".join(lines)


def gemini_step(llm, req: dict) -> dict | None:
    """One call: the picture, the controls and the goal in, the next step out."""
    if llm is None or not llm.available:
        return None
    lang = req.get("lang", "english")
    from ..llm import LLM
    system = SYSTEM.format(language=_LANG_NAME.get(lang, "English"),
                           address=LLM._address(lang, req.get("gender", "")))
    parts = [{"text": req["prompt"]}]
    if req.get("image"):
        parts.insert(0, {"inlineData": {"mimeType": "image/png",
                                        "data": base64.b64encode(req["image"]).decode()}})
    # Room for the fields planned ahead ("then") on a form page.
    d = llm.generate_content([{"role": "user", "parts": parts}], timeout=30.0,
                             system=system,
                             generation_config={"maxOutputTokens": 900, "temperature": 0.1,
                                                "responseMimeType": "application/json"})
    if not d:
        return None
    try:
        raw = "".join(p.get("text", "") for p in d["candidates"][0]["content"].get("parts", []))
    except Exception:  # noqa: BLE001
        return None
    return parse_step(raw)


def parse_step(raw: str) -> dict | None:
    from ..llm import model_json
    return model_json(raw)


# ---------------------------------------------------------------- the session
class Guide:
    def __init__(self):
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._s: dict | None = None
        self._offer: dict | None = None
        self.screen = LiveScreen()
        self.model = None           # callable(request dict) -> dict | None
        self.speaker = None         # callable(text, lang)
        self.llm = None             # made on first use: a connection of its own
        self.tick = TICK
        self.settle = SETTLE
        self.field_idle = FIELD_IDLE
        self.nudge_after = NUDGE_AFTER

    # ------------------------------------------------ what the UI and overlay read
    def state(self) -> dict:
        with self._lock:
            s = self._s
            out = {"active": bool(s and s["status"] not in ENDED), "offer": self._live_offer()}
            if s is None:
                out["status"] = "idle"
                return out
            for k in ("id", "status", "goal", "goal_label", "n", "say", "why", "kind",
                      "can_do", "ring", "line", "lang", "rev", "target_label", "why_shown",
                      "asking", "awaiting"):
                out[k] = s.get(k)
            out["field"] = (s.get("field") or {}).get("name", "")
            return out

    def awaiting(self) -> str:
        """"value" while MicMic waits for her to say what to type, "confirm" while it
        waits for her yes to a value it read back, else ""."""
        with self._lock:
            s = self._s
            if not s or s["status"] != "step":
                return ""
            return s.get("awaiting") or ""

    def page_in_front(self) -> dict:
        """Is a web page in front of her? {"page": bool, "why": str, "app": str}.
        For "help me <task>" (router): a browser in front with a page in it means the
        guide, in her own window. Decided from the app in front (no Accessibility
        needed) and, when it can be read, the page's address: a new tab or a blank page
        is not a page. The installed 1.2.5 (2026-10-09) required a form read off the
        page through Accessibility, read nothing, and sent the task to the browser
        agent. A test process never reads the real screen."""
        import os
        if isinstance(self.screen, LiveScreen) and os.environ.get("MICMIC_STATE_DIR") \
                and os.environ.get("MICMIC_GUIDE_REAL_SCREEN") != "1":
            return {"page": False, "why": "test", "app": ""}
        try:
            front = self.screen.front()
        except Exception as e:  # noqa: BLE001
            return {"page": False, "why": f"front failed: {type(e).__name__}", "app": ""}
        if not front:
            return {"page": False, "why": "no window", "app": ""}
        app = str(front.get("app") or "")
        if not _is_browser(app, front.get("bundle_id", "")):
            return {"page": False, "why": "not a browser", "app": app}
        try:
            reader = getattr(self.screen, "page_url", None)
            url = reader(front) if reader else (self.screen.snapshot(front).get("url") or "")
        except Exception:  # noqa: BLE001
            url = ""
        if url and _NOT_A_PAGE.search(url):
            return {"page": False, "why": "blank page", "app": app}
        return {"page": True, "why": "web page" if url else "browser, address unread", "app": app}

    def heard(self, utterance: str) -> None:
        """Something she said to MicMic while this guide runs: the step call sees it,
        so a code she said ("my code is K7QM2P") can be typed for her later."""
        text = screen.redact(screen._clean(str(utterance or "")))[:200]
        if FORBIDDEN_FIELD.search(text):
            return              # a password, a card or a passport is never passed on
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED or not text:
                return
            said = s.setdefault("said", [])
            if not said or said[-1] != text:
                said.append(text)
            del said[:-MAX_SAID]

    def active(self) -> bool:
        with self._lock:
            return bool(self._s and self._s["status"] not in ENDED)

    def context(self) -> dict | None:
        """For understand(): what a short "next" or "why" is about."""
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return None
            out = {"goal": s.get("goal_label") or s["goal"], "current_step": s.get("say") or ""}
            if s.get("asking") and s["status"] == "step":
                out["asking"] = s["asking"]
            return out

    # ------------------------------------------------ the offer behind "Guide me"
    def offer(self, goal: str, lang: str, speak: bool = True, gender: str = "") -> dict:
        with self._lock:
            self._offer = {"id": uuid.uuid4().hex[:10], "goal": goal[:300], "lang": lang,
                           "speak": speak, "gender": gender, "at": time.time()}
            return {"id": self._offer["id"], "label": _chip_label(lang)}

    def clear_offer(self) -> None:
        with self._lock:
            self._offer = None

    def _live_offer(self) -> dict | None:
        o = self._offer
        if not o or time.time() - o["at"] > OFFER_TTL:
            return None
        if self._s and self._s["status"] not in ENDED:
            return None
        return {"id": o["id"], "at": o["at"], "goal": o["goal"]}

    def take_offer(self, offer_id: str | None = None) -> dict | None:
        with self._lock:
            o = self._offer
            if not o or time.time() - o["at"] > OFFER_TTL:
                return None
            if offer_id and offer_id != o["id"]:
                return None
            self._offer = None
            return o

    # ------------------------------------------------ starting and stopping
    def start(self, goal: str, lang: str = "english", speak: bool = True, gender: str = "",
              wait: bool = False) -> dict:
        """Begin guiding toward `goal`. wait=True: the caller will say the first step
        itself, so this returns once it is known and does not speak it."""
        with self._lock:
            if self._s and self._s["status"] not in ENDED:
                self._s["status"] = "stopped"
                self._s["ring"] = None
            sid = uuid.uuid4().hex[:10]
            self._s = {"id": sid, "goal": goal[:300], "goal_label": "", "lang": lang,
                       "gender": gender, "speak": speak, "status": "thinking", "n": 0,
                       "say": "", "why": "", "kind": "", "can_do": False, "ring": None,
                       "line": "", "rev": 0, "target": None, "target_label": "",
                       "history": [], "started": time.time(), "pending": "start",
                       "quiet": wait, "snap": None, "computed": 0, "why_shown": False,
                       "said": [], "calls": 0,
                       "queue": [], "field": {}, "asking": "", "awaiting": "",
                       "step_at": time.time(), "nudged": False}
            self._offer = None
            self.heard(goal)                 # "guide me, my code is K7QM2P"
            # A client of its own, so a step never queues behind an answer she is
            # waiting for on the router's connection. Kept from the last guide (and
            # warm, see warm()) unless the credentials changed since (savta.account
            # swaps them): a new one paid a TLS handshake on every first step.
            self.llm = self._client()
            self._cv.notify_all()
        threading.Thread(target=self._run, args=(sid,), daemon=True,
                         name="micmic-guide").start()
        if wait:
            return self._wait_for(sid, 0)
        return self.state()

    def stop(self) -> dict:
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            s["status"], s["ring"], s["can_do"] = "stopped", None, False
            s["asking"], s["awaiting"], s["queue"] = "", "", []
            s["line"] = _line("stopped", s["lang"], s["gender"])
            s["rev"] += 1
            self._cv.notify_all()
            return {**self.state(), "said": s["line"]}

    def next_step(self, wait: bool = False) -> dict:
        """She says she did it (the Done button, or "next")."""
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            done_before = s["computed"]
            if s["status"] != "thinking":
                s["pending"] = "done"
                s["quiet"] = wait
                self._cv.notify_all()
            sid = s["id"]
        if wait:
            return self._wait_for(sid, done_before)
        return self.state()

    def explain(self) -> dict:
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            s["why_shown"] = True
            s["rev"] += 1
            return {**self.state(), "said": s.get("why") or _line("no_why", s["lang"], s["gender"])}

    def repeat(self) -> dict:
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            return {**self.state(), "said": s.get("say") or ""}

    def back(self) -> dict:
        """The step before this one, again. No model call: the words are kept, and the
        control is looked for again by its role and label in a fresh read."""
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            if len(s["history"]) < 2:
                return {**self.state(), "said": _line("no_back", s["lang"], s["gender"])}
            s["history"].pop()                     # this step
            prev = s["history"][-1]
            steps = s.get("steps") or []
            prev_step = steps[-2] if len(steps) >= 2 else {"say": prev}
            s["steps"] = steps[:-1]
        snap, tid = (None, None)
        if prev_step.get("role") and prev_step.get("label"):
            try:
                front = self.screen.front()
                if front:
                    snap, tid = self.screen.find(front, prev_step["role"], prev_step["label"])
            except Exception:  # noqa: BLE001
                snap, tid = None, None
        with self._lock:
            if not self._s or self._s is not s or s["status"] in ENDED:
                return self.state()
            s["n"] = max(1, s["n"] - 1)
            s["say"], s["why"] = prev_step.get("say", prev), prev_step.get("why", "")
            s["kind"] = prev_step.get("kind", "")
            s["field"] = dict(prev_step.get("field") or {})
            s["queue"], s["asking"], s["awaiting"] = [], "", ""
            s["step_at"], s["nudged"] = time.time(), False
            s["target"] = tid
            s["target_label"] = prev_step.get("label", "") if tid else ""
            s["ring"] = _ring(snap, tid) if snap else None
            s["can_do"] = self._can_do(snap, tid, s["kind"], s["say"], s["field"]) if snap else False
            if snap:
                s["snap"] = snap
            s["why_shown"] = False
            s["rev"] += 1
            s["rebase"] = True
            self._cv.notify_all()
            return {**self.state(), "said": s["say"]}

    def do_it(self, confirm: bool | None = None, speak: bool = True,
              heard: str = "") -> dict:
        """"Do it for me": press the ringed control, or fill the ringed field, when
        that is safe to do. On a field: a value from her profile is typed at once; one
        she said is read back first ("I'll type K, 7, Q... Is that right?"); without
        one MicMic asks for it. `confirm` is her answer to that read-back (the card's
        Yes and No; by voice, answer()). `speak`: the caller says the line itself."""
        if confirm is not None:
            return self.answer(bool(confirm), heard=heard, speak=speak)
        with self._lock:
            s = self._s
            if not s or s["status"] != "step":
                return {"ok": False, **self.state()}
            snap, tid, kind = s.get("snap"), s.get("target"), s.get("kind")
            if not s.get("can_do") or not snap or not tid:
                return self._tell(s, self._refusal(s), speak, ok=False)
            if kind == "type":
                f = s.get("field") or {}
                value, source = f.get("value") or "", f.get("source") or ""
                if not value:
                    s["awaiting"] = "value"
                    s["asking"] = f.get("ask") or _line("ask_value", s["lang"], s["gender"])
                    s["rev"] += 1
                    return self._tell(s, s["asking"], speak, ok=False)
                if source != "profile":
                    return self._read_back(s, value, speak)
        if kind == "type":
            return self._type(value, speak)      # from her profile: no read-back
        ok, why = self.screen.press(snap, tid)
        return {"ok": ok, "why": why, **self.state()}

    def give(self, utterance: str, speak: bool = True) -> dict | None:
        """She said what goes in the ringed field ("it's K 7 Q M 2 P", "Rivera"):
        read it back for her yes. None when this step is not one MicMic types into."""
        with self._lock:
            s = self._s
            if not s or s["status"] != "step" or s.get("kind") != "type" \
                    or not s.get("can_do"):
                return None
            value = spoken_value(utterance)
            if not value:
                s["awaiting"] = "value"
                s["asking"] = _line("say_again", s["lang"], s["gender"])
                s["rev"] += 1
                return self._tell(s, s["asking"], speak, ok=False)
            return self._read_back(s, value, speak)

    def answer(self, yes: bool, heard: str = "", speak: bool = True) -> dict:
        """Her yes or no to the value MicMic read back. Yes types it; no asks again,
        or takes the value she said with it ("no, it's K7QM2B")."""
        with self._lock:
            s = self._s
            if not s or s["status"] != "step" or s.get("awaiting") != "confirm":
                return {"ok": False, **self.state()}
            value = s.get("pending_value") or ""
            if not yes:
                rest = _NO_WORDS.sub("", heard or "").strip()
                if rest and spoken_value(rest):
                    return self._read_back(s, spoken_value(rest), speak)
                s["awaiting"], s["pending_value"] = "value", ""
                s["asking"] = _line("what_instead", s["lang"], s["gender"])
                s["rev"] += 1
                return self._tell(s, s["asking"], speak, ok=False)
        return self._type(value, speak)

    def noted(self) -> dict:
        """She said a detail before its field came up: it is kept (heard()), and the
        step call will offer it when the field is ringed."""
        with self._lock:
            s = self._s
            if not s or s["status"] in ENDED:
                return {**self.state(), "said": _line("not_guiding", (s or {}).get("lang", "english"))}
            return {**self.state(), "said": _line("noted", s["lang"], s["gender"])}

    # ------------------------------------------------ filling a field for her
    def _refusal(self, s: dict) -> str:
        """Why MicMic will not do this step for her, said plainly."""
        snap, tid, kind = s.get("snap") or {}, s.get("target"), s.get("kind")
        ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
        lang, gender = s["lang"], s["gender"]
        text = f"{(ctl or {}).get('label', '')} {(ctl or {}).get('hint', '')} " \
               f"{(s.get('field') or {}).get('name', '')} {s.get('say', '')}"
        if kind in ("type", "secret") or (ctl and ctl.get("typed")):
            # web.FORBIDDEN_FIELD holds identity numbers too (a passport): said as such.
            if _ID_NUMBER.search(text) and not CREDENTIAL.search(text):
                return _line("do_private", lang, gender)
            return _line("do_secret", lang, gender)
        if kind == "choose" or (ctl and ctl.get("pick")):
            return _line("choose_self", lang, gender)
        if ctl and ctl.get("role") == "option":
            return _line("choice_hers", lang, gender)
        if FINAL_BUTTON.search(text) or FINISHING.search(text) \
                or re.search(r"\bpay|payment|billing|checkout|תשלום|دفع|оплат", text, re.I):
            return _line("do_final", lang, gender)
        return _line("do_refused", lang, gender)

    def _read_back(self, s: dict, value: str, speak: bool) -> dict:
        """Hold `value` for her yes, saying it the way she can check it: a code
        letter by letter. The card shows it as it will be typed."""
        s["awaiting"], s["pending_value"] = "confirm", value
        s["asking"] = _line("confirm_value", s["lang"], s["gender"], v=value)
        s["rev"] += 1
        spoken = _line("confirm_value", s["lang"], s["gender"], v=_spell(value))
        return self._tell(s, spoken, speak, ok=False)

    def _type(self, value: str, speak: bool) -> dict:
        with self._lock:
            s = self._s
            if not s or s["status"] != "step" or s.get("kind") != "type" \
                    or not s.get("can_do"):
                return {"ok": False, **self.state()}
            snap, tid = s.get("snap"), s.get("target")
        try:
            ok, why = self.screen.fill(snap, tid, value)
        except Exception as e:  # noqa: BLE001
            ok, why = False, repr(e)[:100]
        with self._lock:
            s = self._s
            if not s or s["status"] != "step" or s.get("target") != tid:
                return {"ok": ok, "why": why, **self.state()}
            s["awaiting"], s["pending_value"], s["asking"] = "", "", ""
            s["rev"] += 1
            if not ok:
                return {**self._tell(s, _line("type_failed", s["lang"], s["gender"]),
                                     speak, ok=False), "why": why}
            # Typed: the field is filled, so the next step comes now, not after the
            # pause the watcher gives her own typing.
            s["pending"] = "filled"
            self._cv.notify_all()
            return {**self._tell(s, _line("typed", s["lang"], s["gender"]), speak, ok=True),
                    "why": why}

    def _tell(self, s: dict, line: str, speak: bool, ok: bool) -> dict:
        """The answer to a button or a voice command: the state, and the line, which
        the guide says itself only when the caller will not."""
        if speak and s.get("speak") and line:
            threading.Thread(target=self._say, args=(line, s["lang"]), daemon=True).start()
        return {"ok": ok, **self.state(), "said": line}

    # ------------------------------------------------ the thread
    def _wait_for(self, sid: str, computed_before: int) -> dict:
        deadline = time.time() + WAIT_FOR_STEP
        with self._lock:
            while True:
                s = self._s
                if not s or s["id"] != sid or s["status"] in ENDED:
                    break
                if s["computed"] > computed_before and s["status"] != "thinking":
                    break
                left = deadline - time.time()
                if left <= 0:
                    break
                self._cv.wait(min(left, 0.5))
            st = self.state()
            s = self._s
            if s and s["id"] == sid:
                st["said"] = s["line"] if s["status"] in ENDED else s["say"]
            return st

    def _alive(self, sid: str) -> dict | None:
        s = self._s
        return s if (s and s["id"] == sid and s["status"] not in ENDED) else None

    def _run(self, sid: str) -> None:
        while True:
            with self._lock:
                s = self._alive(sid)
                if s is None:
                    return
                reason = s.pop("pending", None)
            if reason:
                # A field she filled, with the next one already planned on this page:
                # no model call, the next step at once.
                if reason == "filled" and self._from_queue(sid):
                    continue
                self._compute(sid, {"start": "starting the guide",
                                    "done": "she says she did the last step",
                                    "change": "the screen changed after the last step",
                                    "filled": "she filled in the field of the last step"}.get(
                                        reason, reason), cause=reason)
                continue
            got = self._watch(sid)
            if got:
                with self._lock:
                    s = self._alive(sid)
                    if s is not None and not s.get("pending"):
                        s["pending"] = got

    def _watch(self, sid: str) -> str | None:
        """Wait, cheaply, for her to do the step. Returns "done", "change", "filled"
        (a field step: its value moved and she left it or stopped typing), or None
        when the session ended."""
        base = None
        changed_at = None
        last = None
        moved_at = None             # a field's value last changed
        touched = False             # the field had the keyboard at some point
        while True:
            with self._lock:
                s = self._alive(sid)
                if s is None:
                    return None
                if s.get("pending"):
                    return None if s["pending"] is None else s.pop("pending")
                if time.time() - s["started"] > MAX_SECONDS:
                    self._end(s, "failed", _line("too_long", s["lang"], s["gender"]), speak=True)
                    return None
                snap, target, kind = s.get("snap"), s.get("target"), s.get("kind")
                if s.pop("rebase", False):
                    base, moved_at, touched = None, None, False
                self._cv.wait(self.tick)
                if s.get("pending"):
                    continue
            if snap is None:
                continue
            try:
                p = self.screen.probe(snap, target)
                # One of a set of options ("Female", "Male", "Unspecified"): she may
                # pick another than the one ringed, which is the step done all the same.
                group = _option_group(snap, target) if kind == "click" else []
                if group:
                    p["group"] = tuple(_value(self.screen.probe(snap, o)) for o in group)
            except Exception:  # noqa: BLE001
                continue
            if base is None:
                early = s.pop("early", None)
                base = dict(p)
                if early:
                    for k in ("pid", "title", "url", "struct"):
                        base[k] = early.get(k)
                # A field is judged against what it held when the step was read off
                # the screen: what she typed while the step was being worked out is
                # already her filling it in.
                raw = (snap.get("raw") or {})
                if kind in FIELD_KINDS and target in raw and base.get("alive"):
                    base["state"] = (raw[target],) + tuple((base.get("state") or (None, None, None))[1:])
                last = base
                touched = bool(p.get("focused"))
                continue
            # The ring follows the control: a scroll or a resized window moves it.
            self._follow(sid, snap, target, p)
            now = time.time()
            # MicMic asked her something ("What's your code?", "I'll type K7QM2P. Is
            # that right?"): until she answers, only a new page moves the guide on. The
            # rehearsal of 2026-10-11: the step was worked out again under the question,
            # the read-back was dropped, and her "yes" went to the browser agent.
            with self._lock:
                s = self._alive(sid)
                asking = bool(s and s.get("awaiting"))
            if asking:
                if (p.get("pid"), p.get("title"), p.get("url")) != \
                        (base.get("pid"), base.get("title"), base.get("url")):
                    if changed_at is None:
                        changed_at = now
                    elif now - changed_at >= self.settle:
                        return "change"
                else:
                    changed_at = None
                last = p
                continue
            if kind in FIELD_KINDS and target and base.get("alive"):
                if _page_moved(base, p):
                    # A new page while on a field: settled like any other change.
                    if changed_at is None:
                        changed_at = now
                    elif (_same(last, p) and now - changed_at >= self.settle) \
                            or now - changed_at >= SETTLE_MAX:
                        return "change"
                    last = p
                    continue
                changed_at = None
                touched = touched or bool(p.get("focused"))
                if _value(p) != _value(last):
                    moved_at = now
                if _value(p) != _value(base):
                    left = touched and p.get("focused") is False
                    still = moved_at is not None and now - moved_at >= (
                        self.settle if kind == "choose" else self.field_idle)
                    if left or still:
                        return "filled"
                elif self._nudge_due(sid, now):
                    self._nudge(sid)
                last = p
                continue
            if not _changed(base, p, kind):
                changed_at = None
                last = p
                continue
            if changed_at is None:
                changed_at = now
            elif _same(last, p) and now - changed_at >= self.settle:
                return "change"
            elif now - changed_at >= SETTLE_MAX:
                return "change"
            last = p

    def _nudge_due(self, sid: str, now: float) -> bool:
        with self._lock:
            s = self._alive(sid)
            return bool(s and s["status"] == "step" and not s.get("nudged")
                        and not s.get("awaiting") and now - s.get("step_at", now) >= self.nudge_after)

    def _nudge(self, sid: str) -> None:
        """One gentle word on a field she has not filled in a while: no step said
        again, no hurry, and the offer to type it for her when MicMic can."""
        with self._lock:
            s = self._alive(sid)
            if s is None or s.get("nudged"):
                return
            s["nudged"] = True
            lang, gender = s["lang"], s["gender"]
            if s.get("kind") == "choose":
                line = _line("nudge_choose", lang, gender)
            elif s.get("kind") in ("type", "secret"):
                line = _line("nudge_type", lang, gender)
                if s.get("can_do"):
                    line += " " + _line("nudge_tell", lang, gender)
            else:
                return
            speak = s.get("speak")
        if speak:
            self._say(line, lang)

    def _follow(self, sid: str, snap: dict, target, p: dict) -> None:
        if not target:
            return
        win = p.get("win") or snap.get("win")
        ring = _visible_part(p.get("frame"), win) if p.get("alive") else None
        ring = _as_ring(ring)
        with self._lock:
            s = self._alive(sid)
            if s is None or s["status"] != "step" or s.get("target") != target:
                return
            if ring != s["ring"]:
                s["ring"] = ring
                s["rev"] += 1

    def _compute(self, sid: str, reason: str, cause: str = "") -> None:
        with self._lock:
            s = self._alive(sid)
            if s is None:
                return
            if s.get("calls", 0) >= MAX_STEPS:
                self._end(s, "failed", _line("too_long", s["lang"], s["gender"]), speak=True)
                return
            s["status"], s["ring"], s["can_do"], s["why_shown"] = "thinking", None, False, False
            s["asking"], s["awaiting"], s["queue"] = "", "", []
            s["rev"] += 1
            goal, lang, gender = s["goal"], s["lang"], s["gender"]
            history = list(s["history"])
            said = list(s.get("said") or [])
        try:
            front = self.screen.front()
        except Exception:  # noqa: BLE001
            front = None
        if not front:
            return self._fail(sid, "no_window")
        snap = self.screen.snapshot(front)
        if snap.get("error") == "no_access":
            return self._fail(sid, "blocked")
        if snap.get("error"):
            return self._fail(sid, "no_window")
        # The page as it was when it was read: if she moves on while the model is
        # still thinking, the watcher sees that at once instead of waiting on a page
        # that already changed.
        try:
            early = self.screen.probe(snap, None)
        except Exception:  # noqa: BLE001
            early = None
        # A password or card field has the keyboard: the list of controls only.
        image = None if snap.get("secure_focus") else self._shot(front)
        req = {"goal": goal, "lang": lang, "gender": gender, "history": history,
               "reason": reason, "image": image, "said": said, "session": f"guide:{sid}",
               "prompt": build_request(goal, lang, history, snap, reason, said),
               "ids": [c["id"] for c in snap.get("controls") or []]}
        with self._lock:
            s = self._alive(sid)
            if s is not None:
                s["calls"] = s.get("calls", 0) + 1
        try:
            res = (self.model or self._gemini)(req)
        except Exception:  # noqa: BLE001
            res = None
        # An optional field she never asked about (the rehearsal of 2026-10-11 stalled
        # 33 s on "Type your Known Traveler Number" on a page that was already filled
        # in): asked once more, told to skip it.
        skip = _optional_target(res, snap, said)
        if skip:
            req = {**req, "reason": f"{reason}; skip the optional field \"{skip}\", she "
                                    f"did not ask for it",
                   "prompt": build_request(goal, lang, history, snap,
                                           f"{reason}; skip the optional field \"{skip}\", "
                                           f"she did not ask for it", said)}
            with self._lock:
                s = self._alive(sid)
                if s is not None:
                    s["calls"] = s.get("calls", 0) + 1
            try:
                res = (self.model or self._gemini)(req)
            except Exception:  # noqa: BLE001
                res = None
            if _optional_target(res, snap, said):
                res = None if not isinstance(res, dict) else {**res, "target": None, "kind": "look"}
        with self._lock:
            s = self._alive(sid)
            if s is None:
                return                       # stopped while the model was thinking
        if not isinstance(res, dict):
            return self._fail(sid, "no_llm")
        self._apply(sid, snap, res, early, cause=cause)

    def _from_queue(self, sid: str) -> bool:
        """The next field the last call planned on this page, given without a model
        call: only while the page is the same one, and only a control that is still
        there under the same name. Anything else drops the plan and asks the model."""
        with self._lock:
            s = self._alive(sid)
            if s is None or not s.get("queue"):
                return False
            queue, old = list(s["queue"]), s.get("snap") or {}
        try:
            front = self.screen.front()
            snap = self.screen.snapshot(front) if front else None
        except Exception:  # noqa: BLE001
            snap = None
        if not snap or snap.get("error") or (snap.get("url"), snap.get("window"), snap.get("app")) \
                != (old.get("url"), old.get("window"), old.get("app")):
            return False
        while queue:
            item = queue.pop(0)
            ctl = _match(snap, item)
            if ctl is None:
                return False
            if item.get("kind") == "type" and ctl.get("value"):
                continue                      # already filled in: on to the next
            try:
                early = self.screen.probe(snap, None)
            except Exception:  # noqa: BLE001
                early = None
            self._apply(sid, snap, {**item, "target": ctl["id"], "done": False}, early,
                        cause="queue", queue=queue)
            return True
        return False

    def _apply(self, sid: str, snap: dict, res: dict, early: dict | None = None,
               cause: str = "", queue: list | None = None) -> None:
        say = _clean_sentence(res.get("say"), 260)
        done = bool(res.get("done"))
        with self._lock:
            s = self._alive(sid)
            if s is None:
                return
            if res.get("goal") and not s["goal_label"]:
                s["goal_label"] = _clean_sentence(res.get("goal"), 80)
            if done:
                line = _clean_sentence(res.get("finish")) or _line("finished", s["lang"], s["gender"])
                self._end(s, "done", line, speak=not s.get("quiet"))
                return
            if not say:
                self._end(s, "failed", _line("no_llm", s["lang"], s["gender"]),
                          speak=not s.get("quiet"))
                return
            tid = _as_id(res.get("target"))
            ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
            if ctl is None:
                tid = None                   # not one of the ids it was given: no ring
            kind = _kind_for(ctl, res.get("kind"))
            if ctl is None and kind in ("click", "type", "choose"):
                # "Click the checkbox for Alex Rivera" with the box below the window (the
                # rehearsal of 2026-10-11): nothing to ring yet, so it is a look step,
                # and her scrolling to it brings the next step with its ring.
                kind = "look"
            field = _field_info(res, kind, s.get("said") or []) if kind in FIELD_KINDS else {}
            same = (say == s.get("say") and tid == s.get("target"))
            # The rehearsal's stall: she clicked the field, the page "changed", and
            # the same field came back worded a little differently, again and again.
            # Asked because the screen moved, the step she is on is kept as it is.
            prev = next((c for c in (s.get("snap") or {}).get("controls") or []
                         if c["id"] == s.get("target")), None)
            kept = (not same and cause in ("change", "filled") and ctl is not None
                    and prev is not None and s["status"] == "thinking"
                    and (ctl["role"], ctl["label"]) == (prev["role"], prev["label"])
                    and kind == s.get("kind"))
            if kept:
                say, field = s["say"], s.get("field") or field
                if cause == "filled":
                    s["nudged"] = True        # she typed: no "take your time" now
            if not same and not kept:
                s["n"] += 1
                s["history"].append(say)
                s.setdefault("steps", []).append(
                    {"say": say, "why": _clean_sentence(res.get("why")), "kind": kind,
                     "role": ctl["role"] if ctl else "", "label": ctl["label"] if ctl else "",
                     "field": field})
                s["step_at"], s["nudged"] = time.time(), False
            if not kept:
                s["why"] = _clean_sentence(res.get("why"))
            s["say"], s["kind"], s["field"] = say, kind, field
            s["target"], s["target_label"] = tid, (ctl or {}).get("label", "")
            s["snap"] = snap
            s["early"] = early
            s["ring"] = _ring(snap, tid)
            s["can_do"] = self._can_do(snap, tid, kind, say, field)
            s["asking"], s["awaiting"], s["pending_value"] = "", "", ""
            if queue is not None:
                s["queue"] = queue
            elif kind in FIELD_KINDS and ctl is not None:
                s["queue"] = _plan(res.get("then"), snap, tid, s.get("said") or [])
            else:
                s["queue"] = []
            s["status"] = "step"
            s["computed"] += 1
            s["rev"] += 1
            quiet = s.pop("quiet", False)
            s["quiet"] = False
            speak = s["speak"] and not quiet and not same and not kept
            lang = s["lang"]
            self._cv.notify_all()
        if speak:
            self._say(say, lang)

    @staticmethod
    def _can_do(snap: dict | None, tid, kind: str, say: str, field: dict | None = None) -> bool:
        """"Do it for me" only on a web page: a plain click, or typing into an
        ordinary field. Never a secret, a sign-in, a payment, a key, anything that
        deletes, or a button or box that finishes it or speaks for her."""
        if not snap or not tid or kind not in ("click", "type") or not snap.get("browser"):
            return False
        ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
        # Which of a set of options fits her (her sex, a visa, trip protection) is hers
        # to pick: the model rings one of them only to show where they are.
        if ctl is None or ctl.get("secure") or ctl.get("pick") or ctl.get("role") == "option":
            return False
        if bool(ctl.get("typed")) != (kind == "type"):
            return False
        text = f"{ctl.get('label', '')} {ctl.get('hint', '')} {(field or {}).get('name', '')} {say}"
        if CREDENTIAL.search(text) or FINAL_BUTTON.search(text) or FINISHING.search(text) \
                or apps.DESTRUCTIVE.search(text):
            return False
        # A password, card or identity field is judged by what the page calls it (the
        # snapshot marked it secure): "the date your passport expires" in the step's own
        # words does not make a plain Month box a card's expiry.
        return not apps.is_forbidden({"label": f"{ctl.get('label', '')} {ctl.get('hint', '')}",
                                      "value": ""})

    def _fail(self, sid: str, key: str) -> None:
        with self._lock:
            s = self._alive(sid)
            if s is not None:
                self._end(s, "failed", _line(key, s["lang"], s["gender"]),
                          speak=not s.get("quiet"))

    def _end(self, s: dict, status: str, line: str, speak: bool) -> None:
        s["status"], s["line"], s["ring"], s["can_do"] = status, line, None, False
        s["asking"], s["awaiting"], s["queue"] = "", "", []
        s["computed"] += 1
        s["rev"] += 1
        s["ended"] = time.time()
        self._cv.notify_all()
        if speak and s.get("speak") and line:
            threading.Thread(target=self._say, args=(line, s["lang"]), daemon=True).start()

    def _say(self, text: str, lang: str) -> None:
        try:
            if self.speaker is not None:
                self.speaker(text, lang)
            else:
                from . import mac
                mac.say(text, lang)
        except Exception:  # noqa: BLE001
            pass

    def _shot(self, front: dict) -> bytes | None:
        try:
            return self.screen.screenshot(front.get("pid"))
        except Exception:  # noqa: BLE001
            return None

    def _gemini(self, req: dict) -> dict | None:
        if self.llm is None:
            self.llm = self._client()
        self.llm.session = req.get("session") or ""
        return gemini_step(self.llm, req)

    def session_tag(self) -> str:
        """"guide:<id>" while a guide runs (the proxy meters a guide once), else ""."""
        with self._lock:
            s = self._s
            return f"guide:{s['id']}" if s and s["status"] not in ENDED else ""

    def _client(self):
        """The guide's own Gemini client: the one it has while the credentials are
        unchanged, else a new one."""
        from ..llm import LLM
        fresh = LLM()
        old = self.llm
        if old is not None and (old.key, old._proxy, old.model) == (
                fresh.key, fresh._proxy, fresh.model):
            return old
        return fresh

    def warm(self) -> None:
        """Her words may start or move a guide: open its Gemini connection now, on its
        own thread, while Jev reads them. Nothing else changes."""
        if self.model is not None:
            return
        with self._lock:
            self.llm = self._client()
            llm = self.llm
        threading.Thread(target=llm.refresh, daemon=True).start()


def _as_id(v):
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _value(p: dict | None):
    return ((p or {}).get("state") or (None,))[0]


def _page_moved(base: dict, p: dict) -> bool:
    """A page, a window or an app of its own, or the control gone: never her typing."""
    return (p.get("pid") != base.get("pid") or p.get("title") != base.get("title")
            or p.get("url") != base.get("url")
            or (bool(base.get("alive")) and p.get("alive") is False))


def _kind_for(ctl: dict | None, kind) -> str:
    """The kind of step, from the control itself where there is one: the model said
    "click" on a text field in the rehearsal, and the watcher then took her click into
    it for the step being done."""
    kind = kind if kind in KINDS else ""
    if ctl is None:
        return kind or "look"
    if ctl.get("secure"):
        return "secret"
    if ctl.get("typed"):
        return "secret" if kind == "secret" else "type"
    if ctl.get("pick"):
        return "choose"
    return kind if kind in ("click", "look", "wait") else "click"


# Words around a value she says: "my code is K7QM2P", "it's Rivera", "הקוד הוא ...".
_LEAD = re.compile(
    r"^.*?(?:\b(?:is|it'?s|that'?s|was|are)\b|\bit\s+is\b|:|"
    r"(?<!\S)(?:הוא|היא|זה|זו)(?!\S)|(?<!\S)(?:هو|هي|هاد|هيدا|هذا|هذه)(?!\S)|"
    r"(?<!\S)(?:это|—)(?!\S))\s+(?=\S)", re.I)
_TAIL = re.compile(r"[\s,.!?]*(?:\b(?:please|thanks|thank you)\b|בבקשה|תודה|لو سمحت|شكرا|"
                   r"пожалуйста|спасибо)?[\s,.!?]*$", re.I)
_NO_WORDS = re.compile(r"^\W*(?:no+|nope|not quite|wrong|that'?s wrong|it'?s not|לא|لا|لأ|нет)\b[\W]*",
                       re.I)
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "אפס": "0", "אחת": "1", "אחד": "1", "שתיים": "2", "שניים": "2", "שלוש": "3", "שלושה": "3",
    "ארבע": "4", "ארבעה": "4", "חמש": "5", "חמישה": "5", "שש": "6", "שישה": "6", "שבע": "7",
    "שבעה": "7", "שמונה": "8", "תשע": "9", "תשעה": "9",
    "صفر": "0", "واحد": "1", "اثنين": "2", "تنين": "2", "ثلاثة": "3", "تلاتة": "3", "أربعة": "4",
    "اربعة": "4", "خمسة": "5", "ستة": "6", "سبعة": "7", "ثمانية": "8", "تمانية": "8", "تسعة": "9",
    "ноль": "0", "один": "1", "два": "2", "три": "3", "четыре": "4", "пять": "5", "шесть": "6",
    "семь": "7", "восемь": "8", "девять": "9"}
_NATO = {w: w[0].upper() for w in (
    "alpha", "alfa", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "juliett", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey", "xray",
    "x-ray", "yankee", "zulu")}


def spoken_value(utterance: str) -> str:
    """What she said should go in the field, without the words around it. A code
    spelled out ("K 7 Q M 2 P", "P zero zero four") is joined into one; a name or an
    address stays as she said it."""
    text = re.sub(r"\s+", " ", str(utterance or "")).strip()
    text = _LEAD.sub("", text, count=1) if _LEAD.search(text) else text
    text = _TAIL.sub("", text).strip().strip("\"'“”«»").strip()
    toks = text.split()

    def piece(t: str) -> str | None:
        low = t.lower().strip(".,")
        if low in _DIGIT_WORDS:
            return _DIGIT_WORDS[low]
        if low in _NATO:
            return _NATO[low]
        t = t.strip(".,")
        if t.isdigit() or (len(t) == 1 and t.isalnum()):
            return t
        if len(t) <= 3 and t.isalnum() and (any(ch.isdigit() for ch in t) or t.isupper()):
            return t
        return None
    if len(toks) >= 2:
        parts = [piece(t) for t in toks]
        # Digit groups alone ("1200 195", a phone number) keep their spaces.
        if all(parts) and not all(t.strip(".,").isdigit() for t in toks):
            return "".join(parts).upper()
    return text[:80]


def _spell(value: str) -> str:
    """A code read back one character at a time, so she can check it by ear."""
    v = str(value or "")
    if " " not in v and 2 <= len(v) <= 16 and any(ch.isdigit() for ch in v):
        return ", ".join(v)
    return v


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").lower())


def _said_it(value: str, said: list[str]) -> bool:
    """The value the model offered is in her own words (spaces, case and dashes
    aside), or in a spelled code she said: never one it made up."""
    v = _squash(value)
    if not v:
        return False
    for line in said:
        if v in _squash(line) or v == _squash(spoken_value(line)):
            return True
    return False


def _profile_value(key: str) -> str:
    """Her own name from her profile, for a field that asks for it: the first name, or
    the last or full name only when the profile holds more than one word."""
    try:
        from .. import profile as prof
        name = re.sub(r"\s+", " ", str(prof.load().get("name") or "")).strip()
    except Exception:  # noqa: BLE001
        return ""
    parts = name.split(" ") if name else []
    if key == "first_name" and parts:
        return parts[0]
    if key == "last_name" and len(parts) >= 2:
        return parts[-1]
    if key == "full_name" and len(parts) >= 2:
        return name
    return ""


def _field_info(res: dict, kind: str, said: list[str]) -> dict:
    """What a field step knows: a short name for what goes there, the question that
    gets it, and a value MicMic may type (hers, never invented)."""
    info = {"name": _clean_sentence(res.get("field"), 60),
            "ask": _clean_sentence(res.get("ask"), 120), "value": "", "source": "",
            "profile": res.get("profile") if res.get("profile") in PROFILE_KEYS else ""}
    if kind != "type":
        return info
    value = re.sub(r"[\r\n]+", " ", str(res.get("value") or "")).strip()[:80]
    if value and _said_it(value, said):
        info["value"], info["source"] = value, "said"
    elif info["profile"]:
        v = _profile_value(info["profile"])
        if v:
            info["value"], info["source"] = v, "profile"
    return info


def _plan(then, snap: dict, current, said: list[str]) -> list:
    """The fields the model planned after this one on the same page, kept by role and
    label so they can be found again after she types. Fields, lists and boxes only."""
    out = []
    if not isinstance(then, list):
        return out
    seen = {current}
    for item in then:
        if not isinstance(item, dict) or len(out) >= MAX_QUEUE:
            break
        tid = _as_id(item.get("target"))
        ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
        say = _clean_sentence(item.get("say"), 260)
        if ctl is None or tid in seen or not say:
            break
        if not (ctl.get("typed") or ctl.get("pick")
                or ctl.get("role") in ("checkbox", "option", "switch")):
            break
        if ctl.get("optional") and not ctl.get("required") and not _asked_for(ctl["label"], said):
            continue                          # never planned: she did not ask for it
        seen.add(tid)
        out.append({"say": say, "target": tid, "role": ctl["role"], "label": ctl["label"],
                    "kind": _kind_for(ctl, item.get("kind")),
                    "field": item.get("field") or "", "ask": item.get("ask") or "",
                    "value": item.get("value") or "", "profile": item.get("profile") or "",
                    "why": "", "goal": ""})
    return out


def _asked_for(label: str, said: list[str]) -> bool:
    """Her words name this field ("my known traveler number is ..."), by any word of
    its label longer than three letters."""
    words = [w for w in re.findall(r"\w{4,}", re.sub(r"\(.*?\)", " ", label or "").lower())
             if w not in ("optional", "number", "field", "your")]
    text = " ".join(said).lower()
    return bool(words) and any(w in text for w in words)


def _optional_target(res, snap: dict, said: list[str]) -> str:
    """The label of the field the step points at when it is empty, optional, and she
    never asked for it; else ""."""
    if not isinstance(res, dict) or res.get("done"):
        return ""
    tid = _as_id(res.get("target"))
    ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
    if ctl is None or not ctl.get("optional") or ctl.get("required"):
        return ""
    if (ctl.get("typed") and (ctl.get("value") or ctl.get("filled"))) or ctl.get("checked"):
        return ""
    return "" if _asked_for(ctl.get("label", ""), said) else ctl.get("label", "")


def _match(snap: dict, item: dict) -> dict | None:
    """A planned field in a fresh read: the same id with the same role and label, or
    else the only control with that role and label."""
    controls = snap.get("controls") or []
    key = (item.get("role"), item.get("label"))
    for c in controls:
        if c["id"] == item.get("target") and (c["role"], c["label"]) == key:
            return c
    same = [c for c in controls if (c["role"], c["label"]) == key]
    return same[0] if len(same) == 1 else None


def _as_ring(r):
    if not r:
        return None
    return {"x": round(r[0], 1), "y": round(r[1], 1), "w": round(r[2], 1), "h": round(r[3], 1)}


def _ring(snap: dict | None, tid):
    if not snap or not tid:
        return None
    ctl = next((c for c in snap.get("controls") or [] if c["id"] == tid), None)
    if not ctl:
        return None
    return _as_ring(_visible_part(ctl.get("frame"), snap.get("win")))


def _changed(base: dict, p: dict, kind: str) -> bool:
    """Has she done the step? A page, a window or an app of its own is always a
    change. While she types, the words in the field and the suggestions under it are
    her typing, not the step being over: that ends when she leaves the field."""
    if p.get("pid") != base.get("pid") or p.get("title") != base.get("title") \
            or p.get("url") != base.get("url"):
        return True
    if base.get("alive") and p.get("alive") is False:
        return True
    if kind in ("type", "secret"):
        return bool(base.get("focused")) and p.get("focused") is False
    if p.get("struct") != base.get("struct"):
        return True
    if base.get("alive") and p.get("state") != base.get("state"):
        return True
    if p.get("group") != base.get("group"):
        return True
    # "Scroll down to Who's checking in": she scrolled (the page's first control moved).
    if kind == "look" and base.get("anchor") is not None and p.get("anchor") is not None \
            and abs(p["anchor"] - base["anchor"]) > 40:
        return True
    if base.get("focus_key") is not None and p.get("focus_key") is not None \
            and p["focus_key"] != base["focus_key"]:
        return True
    return bool(p.get("focused")) and not base.get("focused")


def _option_group(snap: dict, target) -> list:
    """The other options beside a ringed option (a radio button), in the same band of
    the window: the set she picks one of. Empty for anything else."""
    controls = snap.get("controls") or []
    ctl = next((c for c in controls if c["id"] == target), None)
    if ctl is None or ctl.get("role") != "option":
        return []
    if ctl.get("group"):                       # the set's own name, where the page gives one
        return [c["id"] for c in controls if c["role"] == "option" and c["id"] != target
                and c.get("group") == ctl["group"]][:6]
    cy = ctl["frame"][1] + ctl["frame"][3] / 2
    return [c["id"] for c in controls if c["role"] == "option" and c["id"] != target
            and abs(c["frame"][1] + c["frame"][3] / 2 - cy) < 120][:6]


def _same(a: dict | None, b: dict) -> bool:
    if a is None:
        return False
    keys = ("pid", "title", "url", "struct", "alive", "state", "group", "focus_key", "anchor")
    return all(a.get(k) == b.get(k) for k in keys)


_CHIP = {"hebrew": "תדריכי אותי", "arabic": "أرشدني خطوة بخطوة",
         "russian": "Покажи по шагам", "english": "Guide me"}


def _chip_label(lang: str) -> str:
    return _CHIP.get(lang, _CHIP["english"])


GUIDE = Guide()
