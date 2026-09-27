"""Where she named it done: "on Netflix", "search it on Amazon", "send it on Telegram",
"in Maps", "in Safari".

The owner: "if I ask for an app, it should do it in that app". Before this, a named
app only counted for songs (Apple Music, Spotify, YouTube) and for WhatsApp against
a text; "Friends on Netflix" went to YouTube and "search headphones on Amazon" went
to the browser agent, which chose its own site.

How it is decided, within the project's rules (see brain.py):
  * Code finds the candidates: a service from the table below whose name is in her
    words, or a program installed on this Mac named after "in", "on", "with" and the
    like. Nothing else is asked about, so a request that names no app goes out byte
    for byte as it did before this existed.
  * Jev chooses among exactly those candidates, or "none", inside the one
    understand() request already going out: a song called "Telegram" or a question
    about the Amazon river is not a place to do it. No extra round trip.
  * Code does it there: a search page it builds from the service's own address, an
    app's URL scheme, or the app opened. Where the address cannot carry her words
    (Disney+, Hulu), the site is opened and she is told to search for them, never
    told it found something it did not.

Measured 2026-09-27 (jev-latest, the exact understand() shape the router sends, n=10,
one run each, 10 real calls, $0.003): the named place came back right 8/8 at 0.99-1.00
("Friends on Netflix", "search headphones on Amazon", "order me headphones on
Amazon", "send it on Telegram" over a draft, "the Beatles in Spotify", "תפתחי את זה
בנטפליקס", "cheap flights in Safari", "1969 song on apple music"), and both near
misses said "none": "the Amazon rainforest" (none 0.99) and the song "Netflix and
Chill" (none 0.59 against Netflix 0.41: the thinnest margin, so a song title that
names a service is the case to watch).
named_app_only_look split looking from doing: search 0.90 against order 0.07. The
query span picked "Friends", "headphones" (both), "Beatles", "cheap flights"; for "it"
it picked nothing, which is why "it" falls back to what was just on.

Every `open` goes through _open, and every web page through _web, looked up when
called, so a test that stubs them stubs everything here.
"""
from __future__ import annotations
import re, subprocess, time, urllib.parse

from . import mac
from . import music as am


# key: label (how it is said in English, Russian and on screen), per-language names,
# what kind of place it is, the .app names it may be installed as, its web address,
# a search address that carries her words ({q}), and an app URL scheme that does.
# "kind": video / music (the media path plays or finds it), messenger (the message
# path sends or opens the chat), site (a search page), maps.
SERVICES: dict[str, dict] = {
    "youtube": {"label": "YouTube", "he": "יוטיוב", "ar": "يوتيوب", "kind": "video",
                "words": r"you\s?tube|יו\s?טיוב|يوتيو?ب|ют[уь]?юб\w*",
                "web": "https://www.youtube.com/",
                "search": "https://www.youtube.com/results?search_query={q}"},
    "netflix": {"label": "Netflix", "he": "נטפליקס", "ar": "نتفليكس", "kind": "video",
                "words": r"net\s?flix|נטפליקס|نتفل?يكس|нетфликс\w*",
                "web": "https://www.netflix.com/",
                "search": "https://www.netflix.com/search?q={q}"},
    "disney_plus": {"label": "Disney+", "he": "דיסני פלוס", "ar": "ديزني بلس", "kind": "video",
                    "words": r"disney\s*(?:\+|plus\b)|דיסני\s*(?:\+|פלוס)|ديزني\s*(?:\+|بلس)|дисней\s*(?:\+|плюс)",
                    "web": "https://www.disneyplus.com/"},
    "prime_video": {"label": "Prime Video", "he": "פריים וידאו", "ar": "برايم فيديو",
                    "kind": "video", "apps": ("Prime Video", "Amazon Prime Video"),
                    "words": r"prime\s*video|amazon\s*prime|פריים\s*וידאו|אמזון\s*פריים|برايم\s*فيديو|прайм\s*видео",
                    "web": "https://www.primevideo.com/",
                    "search": "https://www.primevideo.com/search/ref=atv_nb_sug?ie=UTF8&phrase={q}"},
    "apple_tv": {"label": "Apple TV", "he": "אפל טי וי", "ar": "أبل تي في", "kind": "video",
                 "words": r"apple\s*t\.?\s?v\b|אפל\s*טי\s*וי|[أا]بل\s*تي\s*في|эппл\s*тв|эпл\s*тв",
                 "web": "https://tv.apple.com/",
                 "search": "https://tv.apple.com/search?term={q}"},
    "hulu": {"label": "Hulu", "he": "הולו", "ar": "هولو", "kind": "video",
             "words": r"\bhulu\b", "web": "https://www.hulu.com/"},
    "apple_music": {"label": "Apple Music", "he": "אפל מיוזיק", "ar": "أبل ميوزك",
                    "kind": "music", "apps": ("Music",), "words": am.PLAYER_WORDS_APPLE,
                    "web": "https://music.apple.com/",
                    "search": "https://music.apple.com/search?term={q}"},
    "spotify": {"label": "Spotify", "he": "ספוטיפיי", "ar": "سبوتيفاي", "kind": "music",
                "apps": ("Spotify",), "words": am.PLAYER_WORDS_SPOTIFY,
                "web": "https://open.spotify.com/",
                "search": "https://open.spotify.com/search/{q}",
                "app_search": "spotify:search:{q}"},
    "whatsapp": {"label": "WhatsApp", "he": "וואטסאפ", "ar": "واتساب", "kind": "messenger",
                 "apps": ("WhatsApp",),
                 "words": r"what'?s\s?app|ו{1,2}א?ט[סצ]א?פ|وا?تس\s?[آا]?ب|واتس|в[оа]тс[ао]п\w*",
                 "web": "https://web.whatsapp.com/"},
    "imessage": {"label": "iMessage", "he": "iMessage", "ar": "iMessage", "kind": "messenger",
                 "apps": ("Messages",),
                 "words": r"\bi\s?-?message|\bsms\b|א?י{1,2}\s?מסג'?|آي\s?مسج|аймесс\w*"},
    "telegram": {"label": "Telegram", "he": "טלגרם", "ar": "تيليجرام", "kind": "messenger",
                 "apps": ("Telegram", "Telegram Desktop", "Telegram Lite"),
                 "words": r"telegram|טלגרם|ت[يى]?ل[يى]?[جغك]رام|телеграм\w*",
                 "web": "https://web.telegram.org/"},
    "signal": {"label": "Signal", "he": "סיגנל", "ar": "سيجنال", "kind": "messenger",
               "apps": ("Signal",),
               "words": r"\b(?:on|in|by|via|through|with|open)\s+(?:the\s+)?signal\b|סיגנל|سيجنال|(?:в|через|по)\s+сигнал\w*"},
    "google": {"label": "Google", "he": "גוגל", "ar": "جوجل", "kind": "site",
               "words": r"\bgoogle\b(?!\s*(?:chrome|maps?|drive|docs|photos|translate))|גוגל(?!\s*(?:מפות|מאפס))|[جغ]وجل(?!\s*ماب)|غوغل|гугл(?!\s*карт)\w*",
               "web": "https://www.google.com/",
               "search": "https://www.google.com/search?q={q}"},
    "wikipedia": {"label": "Wikipedia", "he": "ויקיפדיה", "ar": "ويكيبيديا", "kind": "site",
                  "words": r"wikipedia|\bwiki\b|ויקיפדיה|ويكيبيديا|википеди\w*",
                  "web": "https://{wiki}.wikipedia.org/",
                  "search": "https://{wiki}.wikipedia.org/w/index.php?search={q}"},
    "amazon": {"label": "Amazon", "he": "אמזון", "ar": "أمازون", "kind": "site",
               "words": r"\bamazon\b(?!\s*prime)|אמזון(?!\s*פריים)|[أا]مازون|амазон\w*",
               "web": "https://www.amazon.com/",
               "search": "https://www.amazon.com/s?k={q}"},
    "ebay": {"label": "eBay", "he": "איביי", "ar": "إيباي", "kind": "site",
             "words": r"\be\s?-?bay\b|איביי|[إا]يباي|ибэ?[йи]\b",
             "web": "https://www.ebay.com/",
             "search": "https://www.ebay.com/sch/i.html?_nkw={q}"},
    "aliexpress": {"label": "AliExpress", "he": "עלי אקספרס", "ar": "علي إكسبريس", "kind": "site",
                   "words": r"ali\s?express|עלי\s?אקספרס|علي\s?[إا]كسبريس|али\s?экспресс\w*",
                   "web": "https://www.aliexpress.com/",
                   "search": "https://www.aliexpress.com/wholesale?SearchText={q}"},
    "booking": {"label": "Booking.com", "he": "בוקינג", "ar": "بوكينج", "kind": "site",
                "words": r"booking\s?\.\s?com|\b(?:on|in|at|through|via|with|from)\s+booking\b|בוקינג|بوكين[جغ]|букинг\w*",
                "web": "https://www.booking.com/",
                "search": "https://www.booking.com/searchresults.html?ss={q}"},
    "airbnb": {"label": "Airbnb", "he": "איירביאנבי", "ar": "إير بي إن بي", "kind": "site",
               "words": r"air\s?b\s?(?:n|and|&)\s?b|אייר\s?בי\s?(?:אנד|אנ|ן)\s?בי|איירביאנבי|[إا]ير\s?بي\s?[إا]ن\s?بي|эйрбиэнби|аирбнб",
               "web": "https://www.airbnb.com/",
               "search": "https://www.airbnb.com/s/{p}/homes"},
    "tripadvisor": {"label": "Tripadvisor", "he": "טריפאדוויזור", "ar": "تريب أدفايزر",
                    "kind": "site",
                    "words": r"trip\s?advisor|טריפ\s?א?דוויזור|تريب\s?[أا]دفايزر|трип\s?[эа]двайзер",
                    "web": "https://www.tripadvisor.com/",
                    "search": "https://www.tripadvisor.com/Search?q={q}"},
    "apple_maps": {"label": "Maps", "he": "מפות", "ar": "الخرائط", "kind": "maps",
                   "apps": ("Maps",),
                   "words": r"\b(?:in|on)\s+(?:the\s+)?maps?(?:\s+app)?\b|apple\s*maps|אפל\s*מפות|באפליקציית\s+ה?מפות|במפות|خرائط\s*[أا]بل|في\s+الخرائط|в\s+картах|карты\s+apple",
                   "app_search": "maps://?q={q}"},
    "google_maps": {"label": "Google Maps", "he": "גוגל מפות", "ar": "خرائط جوجل", "kind": "maps",
                    "words": r"google\s*maps?|גוגל\s*(?:מפות|מאפס)|خرائط\s*[جغ]و[جغ]ل|[جغ]وجل\s*ماب\w*|гугл\s*карт\w*",
                    "web": "https://www.google.com/maps/",
                    "search": "https://www.google.com/maps/search/{p}"},
}
# Browsers she may name ("look it up in Safari"): opened on a Google search.
BROWSERS = ("Safari", "Google Chrome", "Firefox", "Arc", "Microsoft Edge", "Brave Browser",
            "Opera")
KIND_OF = {k: v["kind"] for k, v in SERVICES.items()}
MESSENGERS = tuple(k for k, v in SERVICES.items() if v["kind"] == "messenger")
PLAYERS = ("youtube", "apple_music", "spotify")      # the media path plays these itself
VIDEO = tuple(k for k, v in SERVICES.items() if v["kind"] == "video" and k != "youtube")
# Messengers MicMic cannot send through: the chat is opened and the words copied.
OPEN_ONLY = ("telegram", "signal")

_WORDS = {k: re.compile(v["words"], re.I) for k, v in SERVICES.items()}
_ALL_WORDS = re.compile("|".join(f"(?:{v['words']})" for v in SERVICES.values()), re.I)

# A program named after a word that says where: "in Safari", "on Pages", "בPhotos",
# "في Safari", "в Safari". Programs are only ever matched this way, never bare,
# because many are ordinary words (Music, Photos, Notes, News, Books, Clock).
_WHERE = (r"(?:\b(?:in|on|with|using|via|through|into|at)\s+(?:the\s+|my\s+)?"
          r"|(?<![\w])ב[-־]?\s?|\bفي\s+|\bعلى\s+|\bب\s?|\b(?:в|на|через)\s+)")
_APPS_CACHE: dict = {"at": 0.0, "names": []}


def installed_apps() -> list[str]:
    """The programs on this Mac, read at most once a minute: this runs on every turn."""
    if time.time() - _APPS_CACHE["at"] > 60:
        _APPS_CACHE.update(at=time.time(), names=list(mac.installed_apps(limit=400)))
    return _APPS_CACHE["names"]


def _service_app(name: str) -> str | None:
    for k, v in SERVICES.items():
        if name in v.get("apps", ()) or name.lower() == v["label"].lower():
            return k
    return None


def mentions(utterance: str) -> list[str]:
    """The places her words may name, most specific first: service keys, then
    "app:<Name>" for a program on this Mac. Empty for almost every request, and then
    nothing about the request changes."""
    text = utterance or ""
    found = [k for k, rx in _WORDS.items() if rx.search(text)]
    if not text.strip():
        return found
    for name in installed_apps():
        if len(name) < 3 or name == "MicMic" or _service_app(name):
            continue
        if re.search(_WHERE + re.escape(name) + r"(?![\w])", text, re.I):
            found.append(f"app:{name}")
    return found


def without_names(text: str) -> str:
    """Her words with every service name, and the on/in/ב/على before it, taken out."""
    out = re.sub(r"(?:\b(?:on|in|with|at|by|via|using|through)\s+(?:the\s+)?|(?<![\w])ב[-־]?|"
                 r"\bعلى\s+|\bعَ\s*|\bب|\bفي\s+|\bв\s+|\bна\s+|\bчерез\s+)?(?:"
                 + _ALL_WORDS.pattern + r")", " ", text or "", flags=re.I)
    return re.sub(r"\s+", " ", out).strip(" ,.-")


# Asking words, on top of the song ones: "search", "find", "look up", "show me".
_ASKING = am._ASKING | set("""
search find look up show open go get buy order book check see what is are there
and also too now then instead
גם ועכשיו עכשיו במקום
كمان هلق هلّق
и тоже также теперь
תחפשי תחפש חפשי חפש תמצאי תמצא מצאי תראי תראה תפתחי תפתח פתחי תבדקי תבדוק תזמיני תקני לחפש
ابحثي ابحث دوري دور افتحي افتح شوفي شوف لاقي
найди найдите поищи поищите открой откройте покажи покажите закажи купи
""".split())


PRONOUNS = set("""
it this that them him her one
זה זאת את אותו אותה אותם
هاد هاي هذا هذه هيدا هيدي ياه
это его её ее их
""".split())


def is_pronoun(text: str) -> bool:
    """"it", "זה", "هاد": nothing to search for by itself."""
    words = [w for w in re.findall(r"[\w']+", (text or "").lower()) if w not in _ASKING]
    return bool(text and text.strip()) and all(w in PRONOUNS for w in words)


def without_place(text: str, key: str) -> str:
    """Her words without the place she named: a service's name, or the program's."""
    text = without_names(text)
    if key.startswith("app:"):
        text = re.sub(_WHERE + re.escape(key[4:]) + r"(?![\w])|\b" + re.escape(key[4:])
                      + r"\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ,.-")


def query_from(utterance: str, key: str = "") -> str:
    """What to search for when no part of the sentence was chosen as that: her words
    without the place and without the asking words."""
    left = [w for w in re.findall(r"[\w'&.+-]+", without_place(utterance, key))
            if w.lower() not in _ASKING and w.lower() not in PRONOUNS
            and (len(w) > 1 or w.isdigit())]
    return " ".join(left)


# ---------------------------------------------------------------- the question

def label(key: str, lang: str = "english") -> str:
    if key.startswith("app:"):
        return key[4:]
    s = SERVICES.get(key) or {}
    return s.get({"hebrew": "he", "arabic": "ar"}.get(lang, ""), "") or s.get("label", key)


def _describe(key: str) -> str:
    if key.startswith("app:"):
        return f"She named the program {key[4:]} on this computer, to do it in there."
    s = SERVICES[key]
    what = {"video": "to watch it there", "music": "to play it there",
            "messenger": "to send it by that app, or to open it",
            "site": "to search, find or do it on that website",
            "maps": "to find it on that map"}[s["kind"]]
    return f"She named {s['label']} ({s['he']}, {s['ar']}), {what}."


def question(keys: list[str]) -> dict:
    """The questions that ride in understand() when mentions() found something.

    "none" comes first: a replay that never saw this question defaults to it."""
    crit = {"none": "She did not name where to do it. The name is part of what she wants "
                    "(a song, a film or a person called that, a question about the thing "
                    "itself), or only a word for music, a song, a video or a map: play some "
                    "music, תשימי מוזיקה, شغّلي موسيقى, включи музыку, tell me about the "
                    "Amazon river."}
    for k in keys:
        crit[k] = _describe(k)
    return {
        "named_app": {"type": "choice",
            "instructions": "Did she name the app, website or service where she wants this "
                            "done: where it should play, be sent, be searched, be found or be "
                            "opened?",
            "criteria": crit},
        "named_app_only_look": {"type": "noul",
            "instructions": "In that app or website she only wants to see what it has: search "
                            "it, find it, look it up, show her, open it, rather than have "
                            "something done there for her",
            "criteria": {"true": "Search headphones on Amazon. Find a hotel in Paris on "
                                 "Booking. Look it up on Google. Show me Friends on Netflix. "
                                 "תחפשי אוזניות באמזון. ابحثي عن فندق على بوكينج. Найди в гугле.",
                         "false": "Order me headphones on Amazon. Book the hotel on Booking. "
                                  "Buy it on eBay. תזמיני לי. احجزي. Закажи."}},
    }


QUERY_SPAN = ("She wants something done in a particular app or website. Which part of the "
              "sentence is WHAT she wants searched for, found, played or watched there? "
              "Choose the shortest span that is just that. Not the name of the app or "
              "website, and not words asking for it (search, find, play, open, show me, on, "
              "in).")
QUERY_EXISTS = ("Some part of the sentence says what she wants searched for, found, played "
                "or watched in that app or website, other than its name. Opening the app on "
                "its own names nothing.")


# ---------------------------------------------------------------- doing it

def installed(key: str) -> str:
    """The name the service's program is installed under, or ""."""
    if key.startswith("app:"):
        return key[4:] if key[4:] in installed_apps() else ""
    here = set(installed_apps())
    return next((a for a in SERVICES.get(key, {}).get("apps", ()) if a in here), "")


def _wiki(lang: str) -> str:
    return {"hebrew": "he", "arabic": "ar", "russian": "ru"}.get(lang, "en")


def _fill(pattern: str, q: str, lang: str) -> str:
    return pattern.format(q=urllib.parse.quote_plus(q), p=urllib.parse.quote(q),
                          wiki=_wiki(lang))


def web_url(key: str, q: str, lang: str) -> tuple[str, bool]:
    """(address, whether it carries her words). ("", False) when there is no website."""
    s = SERVICES.get(key) or {}
    if q and s.get("search"):
        return _fill(s["search"], q, lang), True
    return (_fill(s["web"], "", lang), False) if s.get("web") else ("", False)


def _open(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=8).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _web(url: str) -> bool:
    """A page opens where MicMic's pages open (mac.open_url: Chrome when it is there)."""
    try:
        mac.open_url(url)
        return True
    except Exception:  # noqa: BLE001
        return False


def _copy(text: str) -> bool:
    """Her words on the clipboard. Local, nothing is sent."""
    try:
        return subprocess.run(["pbcopy"], input=(text or "").encode("utf-8"),
                              timeout=4).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def open_there(key: str, q: str, lang: str) -> tuple[str, str, dict]:
    """Open the place she named, on her words when it can carry them.

    Returns (did, say, detail). Used for everything but a song in Music or Spotify
    (the media path's own) and a message (open_chat)."""
    name = label(key, lang)
    det = {"named": key, "query": q}
    app = installed(key)
    if key.startswith("app:"):
        if not app:
            return "not_installed", line("missing", lang, app=name), det
        if q and app in BROWSERS:
            url = _fill(SERVICES["google"]["search"], q, lang)
            ok = _open(["open", "-a", app, url])
            return ("opened_there" if ok else "app_failed",
                    line("search", lang, app=name, q=q) if ok else line("failed", lang, app=name),
                    {**det, "app": app, "url": url})
        ok = _open(["open", "-a", app])
        if not ok:
            return "app_failed", line("failed", lang, app=name), {**det, "app": app}
        return ("opened_there", line("look_for", lang, app=name, q=q) if q
                else line("opened", lang, app=name), {**det, "app": app})
    s = SERVICES[key]
    # Its own program first when it has a scheme that carries the words, or when there
    # are no words to carry; else its website.
    if app and q and s.get("app_search"):
        url = _fill(s["app_search"], q, lang)
        if _open(["open", url]):
            return "opened_there", line("search", lang, app=name, q=q), {**det, "app": app, "url": url}
    if app and not q:
        if _open(["open", "-a", app]):
            return "opened_there", line("opened", lang, app=name), {**det, "app": app}
    url, carried = web_url(key, q, lang)
    if not url:
        if app and _open(["open", "-a", app]):
            return ("opened_there", line("look_for", lang, app=name, q=q) if q
                    else line("opened", lang, app=name), {**det, "app": app})
        return "not_installed", line("missing", lang, app=name), det
    if not _web(url):
        return "app_failed", line("failed", lang, app=name), {**det, "url": url}
    det.update(url=url, web=True)
    # A program she may have expected (Spotify, Telegram) is not here: its website is
    # the same service, and she is told which one she is looking at.
    lead = line("web_instead", lang, app=name) + " " if s.get("apps") else ""
    if carried:
        key_line = "watch_search" if s["kind"] == "video" else "search"
        return "opened_there", lead + line(key_line, lang, app=name, q=q), det
    return ("opened_there", lead + (line("look_for", lang, app=name, q=q) if q
                                     else line("opened", lang, app=name)), det)


def open_chat(key: str, number: str, text: str) -> dict:
    """A messenger MicMic cannot send through: her words go on the clipboard and the
    chat (or the app, or its website) is opened. Never claimed as sent.

    Returns {"opened": bool, "how": "chat" | "app" | "web" | "missing", "copied": bool}."""
    copied = _copy(text) if text else False
    app = installed(key)
    digits = re.sub(r"[^\d+]", "", number or "")
    if app and key == "telegram" and digits.startswith("+"):
        # Telegram's own link for a chat by phone number (core.telegram.org/api/links).
        if _open(["open", f"tg://resolve?phone={digits.lstrip('+')}"]):
            return {"opened": True, "how": "chat", "copied": copied}
    if app:
        return {"opened": _open(["open", "-a", app]), "how": "app", "copied": copied}
    web = SERVICES[key].get("web")
    if web:
        return {"opened": _web(web), "how": "web", "copied": copied}
    return {"opened": False, "how": "missing", "copied": copied}


# ---------------------------------------------------------------- what she hears

LINES = {
    "search": {
        "english": "{app} is open on {q}.",
        "hebrew":  "פתחתי את {app} על {q}.",
        "arabic":  "فتحت {app} على {q}.",
        "russian": "Открыла {app} с поиском: {q}.",
    },
    "watch_search": {
        "english": "{app} is open on a search for {q}. Pick it there to watch.",
        "hebrew":  "פתחתי את {app} על חיפוש של {q}. «תבחרי|תבחר» שם מה לראות.",
        "arabic":  "فتحت {app} على بحث عن {q}. «اختاري|اختار» من هناك شو بدك تشوف«ي|».",
        "russian": "Открыла {app} с поиском: {q}. Выберите там, что смотреть.",
    },
    "look_for": {
        "english": "{app} is open. Search for {q} there.",
        "hebrew":  "פתחתי את {app}. «תחפשי|תחפש» שם את {q}.",
        "arabic":  "فتحت {app}. «دوري|دور» هناك على {q}.",
        "russian": "Открыла {app}. Найдите там {q}.",
    },
    "opened": {
        "english": "{app} is open.",
        "hebrew":  "פתחתי את {app}.",
        "arabic":  "فتحت {app}.",
        "russian": "Открыла {app}.",
    },
    "missing": {
        "english": "{app} is not on this computer.",
        "hebrew":  "{app} לא מותקן במחשב הזה.",
        "arabic":  "{app} مش منزّل على هالكمبيوتر.",
        "russian": "{app} на этом компьютере нет.",
    },
    "web_instead": {
        "english": "{app} is not on this computer, so I used its website.",
        "hebrew":  "{app} לא מותקן במחשב הזה, אז פתחתי את האתר שלו.",
        "arabic":  "{app} مش منزّل على هالكمبيوتر، ففتحت موقعه.",
        "russian": "{app} на этом компьютере нет, поэтому открыла сайт.",
    },
    "failed": {
        "english": "I could not open {app}.",
        "hebrew":  "לא הצלחתי לפתוח את {app}.",
        "arabic":  "ما قدرت أفتح {app}.",
        "russian": "Не получилось открыть {app}.",
    },
    "chat": {
        "english": "{app} is open on your chat with {who}. The message is copied: press Command-V, then Enter to send it.",
        "hebrew":  "פתחתי את {app} על השיחה עם {who}. ההודעה מועתקת: «תלחצי|תלחץ» Command-V ואז Enter כדי לשלוח.",
        "arabic":  "فتحت {app} على المحادثة مع {who}. الرسالة منسوخة: «اكبسي|اكبس» Command-V وبعدين Enter لتبعتها.",
        "russian": "Открыла {app}, чат с {who}. Сообщение скопировано: нажмите Command-V, потом Enter, чтобы отправить.",
    },
    "chat_find": {
        "english": "{app} is open and the message is copied. Find {who}, press Command-V, then Enter to send it.",
        "hebrew":  "פתחתי את {app} וההודעה מועתקת. «תמצאי|תמצא» את {who}, «תלחצי|תלחץ» Command-V ואז Enter כדי לשלוח.",
        "arabic":  "فتحت {app} والرسالة منسوخة. لاقي {who}، «اكبسي|اكبس» Command-V وبعدين Enter لتبعتها.",
        "russian": "Открыла {app}, сообщение скопировано. Найдите {who}, нажмите Command-V, потом Enter.",
    },
    "chat_web": {
        "english": "{app} is not on this computer, so its website is open and the message is copied. Find {who}, press Command-V, then Enter to send it.",
        "hebrew":  "{app} לא מותקן במחשב הזה, אז פתחתי את האתר שלו וההודעה מועתקת. «תמצאי|תמצא» את {who}, «תלחצי|תלחץ» Command-V ואז Enter.",
        "arabic":  "{app} مش منزّل على هالكمبيوتر، ففتحت موقعه والرسالة منسوخة. لاقي {who}، «اكبسي|اكبس» Command-V وبعدين Enter.",
        "russian": "{app} на этом компьютере нет, поэтому открыла сайт, сообщение скопировано. Найдите {who}, нажмите Command-V, потом Enter.",
    },
    "chat_missing": {
        "english": "{app} is not on this computer, so I did not send it.",
        "hebrew":  "{app} לא מותקן במחשב הזה, אז לא שלחתי.",
        "arabic":  "{app} مش منزّل على هالكمبيوتر، فما بعتها.",
        "russian": "{app} на этом компьютере нет, поэтому я не отправила.",
    },
    "chat_failed": {
        "english": "I could not open {app}. The message was not sent.",
        "hebrew":  "לא הצלחתי לפתוח את {app}. ההודעה לא נשלחה.",
        "arabic":  "ما قدرت أفتح {app}. الرسالة ما انبعتت.",
        "russian": "Не получилось открыть {app}. Сообщение не отправлено.",
    },
}


def line(key: str, lang: str, **fmt) -> str:
    block = LINES[key]
    text = block.get(lang) or block["english"]
    try:
        return text.format(**fmt)
    except Exception:  # noqa: BLE001
        return block["english"].format(**fmt)


def chat_line(key: str, how: str, lang: str, who: str) -> str:
    return line({"chat": "chat", "app": "chat_find", "web": "chat_web"}.get(how, "chat_missing"),
                lang, app=label(key, lang), who=who)
