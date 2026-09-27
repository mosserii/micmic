"""Songs in the Music app (Apple Music) and in Spotify.

Until this existed every song went to YouTube, even "put 1969 on Apple Music". What
each app allows, checked on the owner's Mac (macOS 15.6, Apple Music subscriber,
2,188 songs in the library):

  * A song in her library is played directly: `play` of the library track, found by
    its persistent ID, and the player state is read back before she is told it is
    playing. Not confirmed live on the owner's Mac: Music had an "Open Stream" dialog
    open (not ours, left alone), `play` returned no error and the state stayed
    "stopped", so what she would have heard is "Music would not start it".
  * A song that is only in the Apple Music catalog: Music's AppleScript can only play
    what is in the library, and starting a catalog song from outside the app needs
    MusicKit with a developer token. Opening its link puts the Music app on that song
    (its album page, the song marked); pressing play there is hers, and that is what
    she is told, rather than "playing" for something that is not. Pressing Play for
    her through Accessibility was not tried live (Music sat on another Space with a
    dialog up), and the album page's own Play button starts the album's first track,
    which is not always this song.
  * Spotify's search needs an API key, so without one Spotify is opened on a search
    for the exact song (title and artist, from the Apple catalog) and she presses play.
    Without the Spotify app, its web player (open.spotify.com) is opened on the same
    search. Spotify is not installed on the owner's Mac, so the app path is tested
    offline only.

Finding the song needs no key: Apple's iTunes Search API
(https://itunes.apple.com/search), in the storefront of the Apple ID signed in to
Music, which is not always the Mac's region (the owner's Mac says en_FR, the store is
Israel, and a French catalog link for a song the Israeli store lacks will not open).

Every AppleScript goes through mac._osa, looked up when called, so a test that stubs
it stubs this too.
"""
from __future__ import annotations
import json, os, plistlib, re, subprocess, urllib.parse, urllib.request
from pathlib import Path

from . import mac

SEARCH = "https://itunes.apple.com/search"
UA = "MicMic/1.0 (+https://getmicmic.vercel.app)"
FIELD, ROW = "\x1f", "\x1e"      # separators AppleScript hands back rows in

# Apple storefront id -> the country code the Search API takes.
STOREFRONTS = {
    "143441": "us", "143442": "fr", "143443": "de", "143444": "gb", "143445": "at",
    "143446": "be", "143447": "fi", "143448": "gr", "143449": "ie", "143450": "it",
    "143451": "lu", "143452": "nl", "143453": "pt", "143454": "es", "143455": "ca",
    "143456": "se", "143457": "no", "143458": "dk", "143459": "ch", "143460": "au",
    "143461": "nz", "143462": "jp", "143463": "hk", "143464": "sg", "143466": "kr",
    "143467": "in", "143468": "mx", "143469": "ru", "143470": "tw", "143472": "za",
    "143478": "pl", "143479": "sa", "143480": "tr", "143481": "ae", "143482": "hu",
    "143489": "cz", "143491": "il", "143492": "ua", "143493": "kw", "143497": "lb",
    "143498": "qa", "143503": "br", "143505": "ar", "143516": "eg", "143517": "kz",
    "143528": "jo",
}
_ITUNESCLOUD = Path.home() / "Library/Preferences/com.apple.itunescloud.plist"

# Her words naming where to play it. Only a hint that the question is worth asking
# (see brain.PLAYER); the answer is Jev's. Also used to keep the player's name out
# of the search: "1969 on Apple Music" searches for "1969".
PLAYER_WORDS_APPLE = (
    r"apple\s*music|i\s?tunes|\b(?:in|on)\s+(?:the\s+)?music(?:\s+app)?\b|"
    r"אפל\s*מיוזיק|אפל\s*מוזיק|מיוזיק|אפליקציית\s+ה?מוזיקה|"
    r"[أا]بل\s*ميوز\w*|ميوزك|ميوزيك|تطبيق\s+ال?موسيقى|"
    r"[эе]пп?л\s*м[ьу]юзик|мьюзик|айтюнс|в\s+(?:приложении\s+)?музыке")
PLAYER_WORDS_SPOTIFY = r"spotif\w*|ספוטיפ\w*|سبوتيف\w*|спотиф\w*"
PLAYER_WORDS = re.compile(PLAYER_WORDS_APPLE + "|" + PLAYER_WORDS_SPOTIFY
                          + r"|you\s?tube|יו\s?טיוב|يوتيو?ب|ють?юб\w*", re.I)


def names_a_player(utterance: str) -> bool:
    return bool(PLAYER_WORDS.search(utterance or ""))


def without_player(text: str) -> str:
    """The search words with the player's name, and the on/in/ב/على before it, taken out."""
    out = re.sub(r"(?:\b(?:on|in|with|at)\s+|\bב[־-]?|\bعلى\s+|\bعَ\s*|\bب|\bв\s+|\bна\s+)?(?:"
                 + PLAYER_WORDS.pattern + r")", " ", text or "", flags=re.I)
    return re.sub(r"\s+", " ", out).strip(" ,.-")


# Words that ask for a song rather than name one. Apple's search is fuzzy, and it
# does not skip them: "put 1969 song" came back as Pink Floyd, "1969" as The
# Stooges, and "תשימי 1969" (the verb and the name, as the span picked it) as
# nothing at all. Only which searches are made depends on this list; which song
# plays is still Jev's pick among what they found, judged against her whole sentence.
_ASKING = set("""
put play please song songs music track some something me my the a an on in of by for
to it can you could would like want i hear listen turn start
תשים תשימי שים שימי תנגן תנגני תפעיל תפעילי תדליק תדליקי את השיר שיר שירים לי בבקשה של
מוזיקה המוזיקה אפשר רוצה אני משהו
شغل شغلي شغليلي شغّلي شغّل حط حطي حطيلي أغنية اغنية الأغنية أغاني اغاني موسيقى بدي لو سمحت من
إلي الي لي
включи включите поставь поставьте песню песня песни музыку пожалуйста мне хочу можно
""".split())


# Hebrew glues "to" onto the name: "תחליפי לבאד באני" gave the name as "לבאד באני",
# which Apple's catalog does not find (checked 2026-09-27: two unrelated songs, while
# "באד באני" finds the artist). The name is also searched without it, but only when her
# sentence holds more than the name: "תשימי את ליאור נרקיס" is a name that starts
# with the letter, and her sentence stays the last phrase (the library's search).
_HE_TO = re.compile(r"^ל(?=[\u05d0-\u05ea]{3,})")


def search_terms(named: str | None, utterance: str) -> tuple[list[str], list[str]]:
    """What to search the catalog for. First the name she gave and her sentence
    without the player and the asking words; only if those find nothing, each word
    that is left on its own."""
    left = [w for w in re.findall(r"[\w'&.-]+", without_player(utterance))
            if w.lower() not in _ASKING and (len(w) > 1 or w.isdigit())]
    name, said = without_player(named or ""), " ".join(left)
    glued = _HE_TO.sub("", name) if said.lower() != name.lower() else ""
    phrases = _uniq([name, glued, said])
    return phrases, [w for w in _uniq(left) if w.lower() not in
                     {p.lower() for p in phrases}][:4] if len(left) > 1 else []


def _uniq(items: list[str]) -> list[str]:
    seen, out = set(), []
    for t in items:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


# ---------------------------------------------------------------- the catalog

def country() -> str:
    """The Apple ID's store if Music has one on record, else the Mac's region."""
    try:
        d = plistlib.loads(_ITUNESCLOUD.read_bytes())
        sf = str((d.get("ICDefaultsKeyLastKnownLocalStoreAccountProperties") or {})
                 .get("storefrontIdentifier") or "").split("-")[0]
        if sf in STOREFRONTS:
            return STOREFRONTS[sf]
    except Exception:  # noqa: BLE001
        pass
    loc = os.environ.get("MICMIC_LOCALE") or _apple_locale()
    m = re.search(r"_([A-Za-z]{2})\b", loc or "")
    return m.group(1).lower() if m else "us"


def _apple_locale() -> str:
    try:
        d = plistlib.loads((Path.home() / "Library/Preferences/.GlobalPreferences.plist")
                           .read_bytes())
        return str(d.get("AppleLocale") or "")
    except Exception:  # noqa: BLE001
        return ""


def _fetch(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def catalog(term: str, where: str | None = None, limit: int = 10,
            timeout: float = 4.0) -> list[dict] | None:
    """Songs in the Apple Music catalog. None when Apple could not be reached, which
    is a different thing to say from "no such song"."""
    q = urllib.parse.urlencode({"term": term, "entity": "song", "limit": limit,
                                "country": (where or country()).upper()})
    try:
        data = _fetch(f"{SEARCH}?{q}", timeout)
    except Exception:  # noqa: BLE001
        return None
    return parse_catalog(data)


def parse_catalog(data: dict) -> list[dict]:
    out, seen = [], set()
    for r in (data or {}).get("results") or []:
        if r.get("kind") != "song" or not r.get("trackId") or not r.get("trackName"):
            continue
        if r.get("isStreamable") is False:
            continue
        key = (r["trackName"].lower(), str(r.get("artistName", "")).lower(),
               str(r.get("collectionName", "")).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": f"am:{r['trackId']}", "track_id": r["trackId"],
                    "title": r["trackName"], "artist": r.get("artistName", ""),
                    "album": r.get("collectionName", ""),
                    "year": str(r.get("releaseDate", ""))[:4],
                    "url": r.get("trackViewUrl", ""), "art": r.get("artworkUrl100", ""),
                    "where": "catalog"})
    return out


def app_link(url: str) -> str:
    """The catalog page as the Music app's own link, so it opens in Music and not in
    a browser: music://music.apple.com/il/album/1969/89319049?i=89317104"""
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k == "i"]
    return urllib.parse.urlunsplit(("music", u.netloc or "music.apple.com", u.path,
                                    urllib.parse.urlencode(q), ""))


# ---------------------------------------------------------------- her library

def _q(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def library(term: str, limit: int = 12) -> list[dict]:
    """Songs in her own library matching the words, as Music's own search finds them."""
    if not term.strip():
        return []
    script = f'''tell application "Music"
set rs to search library playlist 1 for "{_q(term)}" only songs
set out to ""
set n to 0
repeat with t in rs
set n to n + 1
if n > {int(limit)} then exit repeat
set out to out & (persistent ID of t) & "{FIELD}" & (name of t) & "{FIELD}" & (artist of t) & "{FIELD}" & (album of t) & "{ROW}"
end repeat
return out
end tell'''
    ok, text = mac._osa(script, timeout=6.0)
    return parse_library(text) if ok else []


def parse_library(text: str) -> list[dict]:
    out = []
    for row in (text or "").split(ROW):
        parts = row.strip("\n").split(FIELD)
        if len(parts) != 4 or not parts[0] or not parts[1]:
            continue
        out.append({"id": f"lib:{parts[0]}", "pid": parts[0], "title": parts[1],
                    "artist": parts[2], "album": parts[3], "where": "library"})
    return out


def _norm(s: str) -> str:
    s = re.sub(r"[\(\[].*?[\)\]]", " ", (s or "").lower())
    s = re.sub(r"\s-\s.*$", " ", s)          # "1969 - Remastered"
    return re.sub(r"[^\w]+", " ", s).strip()


def same_song(a: dict, b: dict) -> bool:
    ta, tb = _norm(a.get("title", "")), _norm(b.get("title", ""))
    ra, rb = _norm(a.get("artist", "")), _norm(b.get("artist", ""))
    return bool(ta) and ta == tb and bool(ra) and (ra == rb or ra in rb or rb in ra)


def in_library(song: dict, rows: list[dict] | None = None) -> dict | None:
    """Her own copy of a catalog song, if she has one."""
    for r in rows if rows is not None else library(song.get("title", ""), 20):
        if same_song(song, r):
            return r
    return None


# ---------------------------------------------------------------- playing

def play_library(pid: str, audible: bool = True) -> tuple[bool, dict]:
    """Play one of her own songs. `audible`: Music's own volume slider at 0 plays in
    silence while the reply says "playing", so it is brought up first."""
    raise_it = ('if sound volume < 5 then\nset sound volume to 60\nset raised to "yes"\n'
                'end if\n') if audible else ""
    # A song from Apple Music that is in her library but not downloaded takes a
    # moment to start, so the state is read for up to two seconds, not once. With a
    # dialog open in Music (the owner's Mac had "Open Stream" up) `play` returns
    # without error and nothing plays: that is exactly why the state is read back.
    script = f'''tell application "Music"
set raised to "no"
{raise_it}set t to (first track of library playlist 1 whose persistent ID is "{_q(pid)}")
play t
repeat 8 times
if player state is playing then exit repeat
delay 0.25
end repeat
set now to ""
try
set now to name of current track
end try
return (player state as text) & "{FIELD}" & now & "{FIELD}" & raised
end tell'''
    ok, text = mac._osa(script, timeout=8.0)
    parts = (text or "").split(FIELD)
    state = parts[0] if ok and parts else ""
    info = {"state": state or text[:120], "now": parts[1] if len(parts) > 1 else "",
            "raised_volume": len(parts) > 2 and parts[2] == "yes"}
    return ok and state == "playing", info


def open_in_music(url: str) -> bool:
    """Put the Music app on a catalog song. It does not start playing by itself."""
    return _open(["open", "-a", "Music", app_link(url)])


def control(action: str) -> tuple[bool, str]:
    """pause, resume, restart, forward, back, stop, for whatever Music is playing.
    `stop` pauses rather than stops, so carrying on later picks up where it was."""
    table = {"pause": "pause", "stop": "pause", "resume": "play",
             "restart": "set player position to 0\nplay",
             "forward": "set player position to (player position + 30)",
             "back": "set player position to (player position - 15)"}
    cmd = table.get(action)
    if not cmd:
        return False, "unknown"
    # Never launches Music just to pause it.
    return mac._osa(f'if application "Music" is running then\ntell application "Music"\n'
                    f'{cmd}\nend tell\nend if')


# ---------------------------------------------------------------- Spotify

def spotify_installed() -> bool:
    return any(p.exists() for p in (Path("/Applications/Spotify.app"),
                                    Path.home() / "Applications/Spotify.app"))


def spotify_search(words: str, web: bool = False) -> bool:
    """Spotify on a search. Its search API needs a key, so there is no track id to
    hand to `play track`; she chooses on the page. `web`: Spotify is not installed, so
    its web player (open.spotify.com) is opened on the same search instead: she named
    Spotify, and YouTube is not Spotify."""
    if web:
        return _web("https://open.spotify.com/search/" + urllib.parse.quote(words))
    return _open(["open", "spotify:search:" + urllib.parse.quote(words)])


def spotify_control(action: str) -> tuple[bool, str]:
    cmd = {"pause": "pause", "stop": "pause", "resume": "play",
           "restart": "set player position to 0\nplay",
           "forward": "set player position to (player position + 30)",
           "back": "set player position to (player position - 15)"}.get(action)
    if not cmd:
        return False, "unknown"
    return mac._osa(f'if application "Spotify" is running then\ntell application "Spotify"\n'
                    f'{cmd}\nend tell\nend if')


def _web(url: str) -> bool:
    try:
        mac.open_url(url)
        return True
    except Exception:  # noqa: BLE001
        return False


def _open(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=6).returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- what she hears

APP_NAME = {"apple_music": "Apple Music", "spotify": "Spotify", "youtube": "YouTube"}

LINES = {
    "playing": {
        "english": "Playing {title} by {artist} in Apple Music.",
        "hebrew":  "מנגנת את {title} של {artist} באפל מיוזיק.",
        "arabic":  "هلّق بشغّل {title} لـ{artist} على أبل ميوزك.",
        "russian": "Включаю {title}, {artist}, в Apple Music.",
    },
    "press_play": {
        "english": "{title} by {artist} is open in Apple Music. Press play.",
        "hebrew":  "פתחתי את {title} של {artist} באפל מיוזיק. «תלחצי|תלחץ» על נגן.",
        "arabic":  "فتحت {title} لـ{artist} على أبل ميوزك. «اكبسي|اكبس» على تشغيل.",
        "russian": "Открыла {title}, {artist}, в Apple Music. Нажмите Play.",
    },
    "not_found": {
        "english": "I could not find {q} in Apple Music.",
        "hebrew":  "לא מצאתי את {q} באפל מיוזיק.",
        "arabic":  "ما لقيت {q} على أبل ميوزك.",
        "russian": "Я не нашла {q} в Apple Music.",
    },
    "unreachable": {
        "english": "I could not reach Apple Music just now.",
        "hebrew":  "לא הצלחתי להגיע לאפל מיוזיק כרגע.",
        "arabic":  "ما قدرت أوصل لأبل ميوزك هلّق.",
        "russian": "Сейчас не получилось связаться с Apple Music.",
    },
    "not_started": {
        "english": "I found {title} by {artist} in your library but Music would not start it.",
        "hebrew":  "מצאתי את {title} של {artist} בספרייה, אבל המוזיקה לא התחילה לנגן.",
        "arabic":  "لقيت {title} لـ{artist} بمكتبتك، بس ما اشتغلت.",
        "russian": "Я нашла {title}, {artist}, в вашей медиатеке, но Music не начала играть.",
    },
    "spotify_search": {
        "english": "Spotify is open on {title} by {artist}. Press play on the top one.",
        "hebrew":  "פתחתי את ספוטיפיי על {title} של {artist}. «תלחצי|תלחץ» על נגן בראשון.",
        "arabic":  "فتحت سبوتيفاي على {title} لـ{artist}. «اكبسي|اكبس» تشغيل على أول وحدة.",
        "russian": "Открыла Spotify на {title}, {artist}. Нажмите Play на первой.",
    },
    "spotify_words": {
        "english": "Spotify is open on a search for {q}. Press play on the one you want.",
        "hebrew":  "פתחתי את ספוטיפיי על חיפוש של {q}. «תלחצי|תלחץ» על נגן במה «שתרצי|שתרצה».",
        "arabic":  "فتحت سبوتيفاي على بحث عن {q}. «اكبسي|اكبس» تشغيل على اللي بدك ياه.",
        "russian": "Открыла Spotify на поиске {q}. Нажмите Play на нужной.",
    },
    "spotify_web": {
        "english": "Spotify is not on this computer, so its website is open on {title} by {artist}. Press play on the top one.",
        "hebrew":  "ספוטיפיי לא מותקן במחשב הזה, אז פתחתי את האתר שלו על {title} של {artist}. «תלחצי|תלחץ» על נגן בראשון.",
        "arabic":  "سبوتيفاي مش منزّل على هالكمبيوتر، ففتحت موقعه على {title} لـ{artist}. «اكبسي|اكبس» تشغيل على أول وحدة.",
        "russian": "Spotify на этом компьютере нет, поэтому открыла сайт Spotify на {title}, {artist}. Нажмите Play на первой.",
    },
    "spotify_web_words": {
        "english": "Spotify is not on this computer, so its website is open on a search for {q}. Press play on the one you want.",
        "hebrew":  "ספוטיפיי לא מותקן במחשב הזה, אז פתחתי את האתר שלו על חיפוש של {q}. «תלחצי|תלחץ» על נגן במה «שתרצי|שתרצה».",
        "arabic":  "سبوتيفاي مش منزّل على هالكمبيوتر، ففتحت موقعه على بحث عن {q}. «اكبسي|اكبس» تشغيل على اللي بدك ياه.",
        "russian": "Spotify на этом компьютере нет, поэтому открыла сайт Spotify на поиске {q}. Нажмите Play на нужной.",
    },
    "stopped": {
        "english": "I paused the music.",
        "hebrew":  "עצרתי את המוזיקה.",
        "arabic":  "وقّفت الموسيقى.",
        "russian": "Поставила музыку на паузу.",
    },
}


def line(key: str, lang: str, **fmt) -> str:
    block = LINES[key]
    text = block.get(lang) or block["english"]
    try:
        return text.format(**fmt)
    except Exception:  # noqa: BLE001
        return block["english"].format(**fmt) if fmt else text
