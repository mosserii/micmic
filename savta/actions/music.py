"""Songs in the Music app (Apple Music) and in Spotify.

Until this existed every song went to YouTube, even "put 1969 on Apple Music". What
each app allows, checked on the owner's Mac (macOS 15.6, Apple Music subscriber,
2,188 songs in the library):

  * A song in her library is played directly: `play` of the library track, found by
    its persistent ID, and the player state is read back before she is told it is
    playing. Checked live 2026-09-27 (two quiet checks, volume 5, paused within a
    second, state put back): a streamed Apple Music track in the library that was not
    loaded started 0.35 s after `play`. In the owner's session it did not, three times
    running, with no error: Music had had an "Open Stream" dialog up earlier that day,
    and a track asked for then became Music's current track later, paused. So the
    state is read for up to three seconds, a track that loaded but sits paused is
    asked once more, and when it still will not start, a dialog in Music is looked for
    and named to her ("close it and ask me again") instead of a bare failure.
  * A song that is only in the Apple Music catalog: Music's AppleScript can only play
    what is in the library, and starting a catalog song from outside the app needs
    MusicKit with a developer token. Opening its link puts the Music app on that song
    (its album page, the song marked). On that page the song's own row carries a play
    toggle (AXCheckBox, description "play") inside a group whose AXIdentifier holds
    "AlbumTrackLockup[id=track-lockup-<album>-<track id>": pressing it through
    Accessibility started exactly that song in 0.3 s (checked live 2026-09-27). The
    album header's Play button is never pressed: it starts the album's first track.
    The player is read back after the press; only a song that is really playing is
    called playing, anything else is "press play", as before.
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
import json, os, plistlib, re, subprocess, time, unicodedata, urllib.parse, urllib.request
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


# A song she describes instead of naming: "our latest World Cup song", "the song from
# Titanic", "the one they play at weddings". The owner's session (1.1.0): the search
# kept "shakira" and played "Whenever, Wherever". Only a hint that the question is
# worth asking (brain.SONG_DESC_QUESTIONS); the answer is Jev's. Two parts, both
# needed, so an ordinary request is sent exactly as before: words that describe, and
# a sign that a song is plausible at all (a word for songs, a player, or a song on).
_DESCRIBING = re.compile(
    r"\b(?:latest|newest|recent|new\s+(?:song|single|hit|one)|last\s+(?:song|single|hit)|"
    r"first\s+(?:song|single|hit)|from\s+the\s+(?:movie|film|show|series|musical|cartoon|ad|"
    r"commercial|game)|song\s+(?:from|about|that|where|with|they|which|for|of\s+the)|"
    r"the\s+one\s+(?:that|they|from|about|with|where|which)|that\s+song|theme|anthem|"
    r"world\s*cup|mundial|eurovision|olympic\w*|wedding\w*|christmas|soundtrack|"
    r"most\s+famous|best[-\s]known|biggest\s+hit|number\s+one)\b|"
    r"החדש|החדשה|האחרון|האחרונה|מהסרט|מהסדרה|שיר\s+הנושא|הפתיח|המנון|המונדיאל|מונדיאל|"
    r"גביע\s+העולם|אירוויזיון|אולימפיאד|בחתונות|חתונה|השיר\s+ש|השיר\s+על|השיר\s+מ|המפורסם|"
    r"الجديدة|الجديد|الأخيرة|الاخيرة|من\s+فيلم|من\s+الفيلم|المسلسل|شارة|نشيد|كأس\s+العالم|"
    r"المونديال|يوروفيجن|الأعراس|العرس|الأشهر|"
    r"новую|новая|новой|последн\w*|из\s+фильма|из\s+сериала|саундтрек|гимн|"
    r"чемпионат\w*\s+мира|евровидени\w*|олимпиад\w*|свадьб\w*|свадебн\w*|самую\s+известную",
    re.I)
_SONGISH = re.compile(
    r"\b(?:songs?|singles?|tracks?|music|tunes?|hits?|anthem|theme|play|put\s+on|listen|hear)\b|"
    r"שיר|שירים|תשים|תשימי|שים|שימי|תנגן|תנגני|מוזיקה|"
    r"أغنية|اغنية|أغاني|اغاني|شغل|شغلي|شغّلي|موسيقى|"
    r"песн\w*|включи\w*|музык\w*|трек\w*", re.I)


def describes_a_song(utterance: str, song_on: bool = False) -> bool:
    """Words that may describe a song rather than name it, where a song is plausible."""
    u = utterance or ""
    return bool(_DESCRIBING.search(u)) and bool(song_on or _SONGISH.search(u)
                                                 or PLAYER_WORDS.search(u))


# She points at the screen instead of naming a song: "play this one", "the song I
# see on my screen", "look at my screen, you will see the song I want". The owner's
# session: "play the song i see on my screen" searched the literal words "see
# screen" and played "Screen" by twenty one pilots. Only a hint that the question
# below is worth asking (router._screen_song_hint reads the real screen); the
# answer is Jev's, since "this one" also means "not this one" (a rejection) and
# "the next one" (another try), which are not about the screen at all.
#
# On its own this half is far too common in ordinary speech ("I see", "that one",
# "look at") to gate a question on alone - paired with a song word or a song already
# playing (same idiom as describes_a_song below) it stays rare.
_SCREEN_REF = re.compile(
    r"\bmy\s+screen\b|\bthe\s+screen\b|\bthis\s+one\b|\bthis\s+song\b|\bthat\s+one\b|"
    r"\bthat\s+song\b|\bi\s+see\b|\byou\s+(?:will\s+)?see\b|\blook\s+at\b|"
    r"במסך|על\s+המסך|השיר\s+הזה|האחד\s+הזה|תסתכל\w*\s+(?:ב|על\s+ה)?מסך|"
    r"ع(?:ل[ىة]|الشاشة)|هاي\s+الأغنية|هالأغنية|هاد(?:\s+الشي)?|"
    r"на\s+экране|эту?\s+песню|этот\s+вариант", re.I)


def mentions_screen_song(utterance: str, song_on: bool = False) -> bool:
    """A reference to the screen where a song is plausible: her words say something
    (song, play, music...), a song is already playing, or a player was named."""
    u = utterance or ""
    return bool(_SCREEN_REF.search(u)) and bool(song_on or _SONGISH.search(u)
                                                or PLAYER_WORDS.search(u))


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


# The same search twice in one turn (a described song is checked in the catalog
# before it is played) is asked of Apple once.
_CACHE: dict[str, tuple[float, list[dict]]] = {}
CACHE_S = 120.0


def catalog(term: str, where: str | None = None, limit: int = 10,
            timeout: float = 4.0) -> list[dict] | None:
    """Songs in the Apple Music catalog. None when Apple could not be reached, which
    is a different thing to say from "no such song"."""
    q = urllib.parse.urlencode({"term": term, "entity": "song", "limit": limit,
                                "country": (where or country()).upper()})
    hit = _CACHE.get(q)
    if hit and time.time() - hit[0] < CACHE_S:
        return [dict(r) for r in hit[1]]
    try:
        data = _fetch(f"{SEARCH}?{q}", timeout)
    except Exception:  # noqa: BLE001
        return None
    rows = parse_catalog(data)
    _CACHE[q] = (time.time(), rows)
    return [dict(r) for r in rows]


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
                    "date": str(r.get("releaseDate", ""))[:10],
                    "artist_id": r.get("artistId"),
                    "url": r.get("trackViewUrl", ""), "art": r.get("artworkUrl100", ""),
                    "where": "catalog"})
    return out


LOOKUP = "https://itunes.apple.com/lookup"


def recent_by(artist_id, where: str | None = None, limit: int = 50,
              timeout: float = 4.0) -> list[dict]:
    """An artist's songs, newest first, from Apple's own release dates (the lookup
    API's sort=recent). DJ mixes and other artists' compilations are left out."""
    q = urllib.parse.urlencode({"id": artist_id, "entity": "song", "limit": limit,
                                "sort": "recent", "country": (where or country()).upper()})
    hit = _CACHE.get(q)
    if hit and time.time() - hit[0] < CACHE_S:
        return [dict(r) for r in hit[1]]
    try:
        data = _fetch(f"{LOOKUP}?{q}", timeout)
    except Exception:  # noqa: BLE001
        return []
    rows = [r for r in parse_catalog(data)
            if not re.search(r"\(mixed\)|dj mix", f"{r['title']} {r['album']}", re.I)]
    _CACHE[q] = (time.time(), rows)
    return [dict(r) for r in rows]


def artist_songs(artist_id, where: str | None = None, limit: int = 200,
                 timeout: float = 5.0) -> list[dict]:
    """As many of an artist's songs as one call reasonably holds, in Apple's own
    order (not date-sorted, unlike recent_by: a misheard title can be an old song).
    Used when her exact words find nothing and the artist is known (named, or the
    one she was just listening to), so the real fix is matching what she said
    against the real list, not asking Apple to guess again from the same words."""
    if not artist_id:
        return []
    q = urllib.parse.urlencode({"id": artist_id, "entity": "song", "limit": limit,
                                "country": (where or country()).upper()})
    hit = _CACHE.get(q)
    if hit and time.time() - hit[0] < CACHE_S:
        return [dict(r) for r in hit[1]]
    try:
        data = _fetch(f"{LOOKUP}?{q}", timeout)
    except Exception:  # noqa: BLE001
        return []
    rows = parse_catalog(data)
    _CACHE[q] = (time.time(), rows)
    return [dict(r) for r in rows]


def find_artist_id(name: str, where: str | None = None) -> int | None:
    """The catalog's artistId for a name, from one ordinary song search (Apple has
    no plain name-to-id lookup without a developer token): the artist most of the
    hits agree on."""
    hits = catalog(name, where, limit=5) or []
    ids = [r["artist_id"] for r in hits if r.get("artist_id")]
    return max(set(ids), key=ids.count) if ids else None


# ---------------------------------------------------------------- matching by sound
# A title heard through a microphone and an accent: "nueva yella" for "NUEVAYoL",
# "die die" for "Dai Dai", "where all" for whatever Bad Bunny song that was. Apple's
# own search is a lexical/popularity match, not a phonetic one, and comes back empty
# or wrong on these. Once an artist is known, matching her words against that
# artist's real titles - by spelling AND by how they sound - finds the real song
# without ever asking a model to invent one.
_VOWELS = str.maketrans("", "", "aeiou")


def _skeleton(s: str) -> str:
    """Consonants only, doubled letters collapsed: a rough stand-in for how a word
    sounds once the vowel it was actually said with is lost to a microphone and an
    accent. "die" and "dai" both collapse to "d"; "nuevayella" and "nuevayol" both
    collapse to "nvyl", one letter apart."""
    out: list[str] = []
    for c in _plain(s).replace(" ", "").translate(_VOWELS):
        if not out or out[-1] != c:
            out.append(c)
    return "".join(out)


def _edit_distance(a: str, b: str) -> int:
    """Plain Levenshtein distance. Short strings only (song titles), so the naive
    O(len(a)*len(b)) table costs nothing."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 1 - _edit_distance(a, b) / max(len(a), len(b))


def similarity(said: str, title: str) -> float:
    """How likely `said` is a mishearing of `title`: the better of a plain spelling
    match and a match on the consonant skeleton (which absorbs a wrong vowel, a
    dropped accent, or a whole extra syllable Apple's own fuzzy search does not)."""
    a, b = _plain(said).replace(" ", ""), _plain(title).replace(" ", "")
    return max(_ratio(a, b), _ratio(_skeleton(said), _skeleton(title)))


SOUND_MATCH_GATE = 0.55  # below this a "match" is coincidence, not a mishearing


def by_sound(said: str, songs: list[dict], limit: int = 5) -> list[dict]:
    """Real songs ranked by how likely `said` is a mishearing of their title, best
    first, past SOUND_MATCH_GATE, capped to `limit` - small enough that a Jev pick
    among them (when more than one is close) costs nothing extra."""
    scored = sorted(({**s, "sound_score": similarity(said, s["title"])} for s in songs),
                    key=lambda s: -s["sound_score"])
    return [s for s in scored if s["sound_score"] >= SOUND_MATCH_GATE][:limit]


# Stricter than a title's: a misheard name sounds the same once its vowels are gone
# ("ed sharon", "brunno mars", "tylor swift" all score 1.0), while "popular songs"
# against an artist called Popular scores 0.58 and must not be "corrected".
ARTIST_SOUND_GATE = 0.75


def artists(term: str, where: str | None = None, limit: int = 8,
            timeout: float = 3.0) -> list[dict]:
    """Apple's artist search: [{"name", "artist_id"}], best first. Its own matching is
    forgiving ("ed sharon" lists Ed Sheeran first, "brunno mars" Bruno Mars, "tylor
    swift" Taylor Swift; checked by hand 2026-10-02). [] when nothing or unreachable."""
    q = urllib.parse.urlencode({"term": term, "entity": "musicArtist", "limit": limit,
                                "country": (where or country()).upper()})
    hit = _CACHE.get(q)
    if hit and time.time() - hit[0] < CACHE_S:
        return [dict(r) for r in hit[1]]
    try:
        data = _fetch(f"{SEARCH}?{q}", timeout)
    except Exception:  # noqa: BLE001
        return []
    rows = [{"name": str(r["artistName"]), "artist_id": r.get("artistId")}
            for r in (data or {}).get("results") or [] if r.get("artistName")]
    _CACHE[q] = (time.time(), rows)
    return [dict(r) for r in rows]


def artist_by_sound(said: str, where: str | None = None) -> dict | None:
    """The real artist a misheard name most likely is ("ed sharon" -> Ed Sheeran), as
    {"name", "artist_id", "sound_score"}: Apple's artist search, then the same spelling
    and sound match a misheard title gets (similarity). None when the best match is
    the name she already said, or none is close enough to be a mishearing. Bench v1
    med-003: "play some ed sharon" found nothing although Ed Sheeran was right there."""
    said = (said or "").strip()
    if len(_plain(said).replace(" ", "")) < 3:
        return None
    rows = [{**r, "sound_score": similarity(said, r["name"])} for r in artists(said, where)]
    rows = [r for r in rows if r["sound_score"] >= ARTIST_SOUND_GATE]
    if not rows:
        return None
    best = max(rows, key=lambda r: r["sound_score"])
    if _plain(best["name"]) == _plain(said):
        return None
    return best


def clean_query(q: str) -> str:
    """A search query with what trips a search engine taken out: bracketed parts
    ("(Love Theme from "Titanic")", "[feat. ...]"), feat./ft. credits, quotes and
    " - " separators. Bench v1 hard-media-009: YouTube gave nothing for 'My Heart Will
    Go On (Love Theme from "Titanic") James Horner'."""
    t = q or ""
    while True:
        t, n = re.subn(r"[\(\[][^\(\)\[\]]*[\)\]]", " ", t)
        if not n:
            break
    t = re.sub(r"\s(?:feat\.?|ft\.?|featuring)\s.*?(?=\s[-\u2013\u2014|]\s|$)", " ", t, flags=re.I)
    t = re.sub(r"[\"“”„«»]", " ", t)
    t = re.sub(r"\s[-\u2013\u2014|]\s", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or (q or "").strip()


def ensure_running(app: str) -> None:
    """Start `app` in the background, not in front of her, well before her words
    are even fully understood: called the moment they plausibly want it (before the
    catalog search and Jev's pick, which take under a second), so a cold Music that
    otherwise adds several seconds to the very first song of a session has a head
    start on the same wall-clock time rather than sitting entirely on the critical
    path (the owner's session measured 7.6s for a first "play ... on apple music",
    of which Jev accounted for well under a second). `open` itself returns as soon
    as it has told launchd to start the app - it does not wait for the app to
    actually be ready - so this costs only that brief hand-off, not the launch.

    Goes through _open, like every other launch in this module, so a test that
    stubs it stubs this too."""
    _open(["open", "-g", "-a", app])


# ---------------------------------------------------------------- described songs
# "Our latest World Cup song", "Shakira's newest song": which song is found by
# searching real sources first (Wikipedia's articles about songs, Apple's catalog
# with its release dates); a model is asked only when they find nothing usable
# (router._identify_song). Measured 2026-09-27: the model alone said "Waka Waka"
# (2010) for Shakira's latest World Cup song; the sources have "Dai Dai" (2026).
RECENT = re.compile(r"\b(?:latest|newest|recent|new|last)\b|החדש|החדשה|האחרון|האחרונה|"
                    r"الجديدة|الجديد|الأخيرة|الاخيرة|нов\w*|последн\w*", re.I)
WIKI = "https://en.wikipedia.org/w/api.php"
_SONG_ARTICLE = re.compile(r"\bis\s+(?:a|an|the)\s+(?:[\w'-]+\s+){0,4}(?:song|single)\b", re.I)
_BY = re.compile(r"\b(?:song|single)\b[^.]{0,40}?\bby\s+(?:the\s+)?(?:[\w-]+\s+){0,4}?"
                 r"(?:singer(?:-songwriter)?|rapper|band|group|duo|musician|DJ|artist|"
                 r"streamer|influencer)\s+([A-Z][\w.'&-]*(?:\s+[A-Z][\w.'&-]*){0,3})")
_BY_NAME = re.compile(r"\b(?:song|single)\s+(?:recorded\s+)?by\s+([A-Z][\w.'&-]*"
                      r"(?:\s+[A-Z][\w.'&-]*){0,3})")


def wiki_songs(term: str, limit: int = 8, timeout: float = 3.0) -> list[dict]:
    """Songs among the Wikipedia articles a search finds: title, artist when the
    article says, and the first year it mentions (its release)."""
    q = urllib.parse.urlencode({"action": "query", "format": "json",
                                "generator": "search", "gsrsearch": term,
                                "gsrlimit": limit, "prop": "extracts", "exintro": 1,
                                "explaintext": 1, "exlimit": limit, "exchars": 400})
    try:
        data = _fetch(f"{WIKI}?{q}", timeout)
    except Exception:  # noqa: BLE001
        return []
    pages = sorted(((data or {}).get("query") or {}).get("pages", {}).values(),
                   key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        text = re.sub(r"\s+", " ", p.get("extract") or "")
        if not _SONG_ARTICLE.search(text[:200]):
            continue
        title = p.get("title") or ""
        tag = re.search(r"\s*\(([^)]*\b(?:song|single))\)$", title)
        artist = ""
        if tag:
            title = title[:tag.start()]
            artist = re.sub(r"\s*\b(?:song|single)$", "", tag.group(1)).strip()
        m = _BY.search(text[:300]) or _BY_NAME.search(text[:300])
        artist = artist or (m.group(1).strip() if m else "")
        year = re.search(r"\b(19\d\d|20\d\d)\b", text)
        out.append({"id": f"wiki:{p.get('pageid')}", "title": title, "artist": artist,
                    "year": year.group(1) if year else "",
                    "date": year.group(1) if year else "",
                    "about": text[:160], "where": "wiki"})
    return out


def search_title(title: str) -> str:
    """A catalog title as words to search for: no "(feat. ...)", versions or mixes."""
    t = re.sub(r"\s*[\(\[](?:feat|ft|featuring|with)\b\.?[^\)\]]*[\)\]]", "", title,
               flags=re.I)
    t = re.sub(r"\s*[\(\[][^\)\]]*\b(?:version|remaster\w*|mix|remix|edit|live)\b[^\)\]]*"
               r"[\)\]]", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip() or title


def main_artist(artist: str) -> str:
    return re.split(r"\s*(?:,|&|\bx\b|\band\b|\bfeat\.?|\bft\.?|\bwith\b)\s*",
                    artist or "", maxsplit=1, flags=re.I)[0].strip()


def merge_songs(rows: list[dict]) -> list[dict]:
    """One entry per song across the sources. Its date is the EARLIEST any source
    gives, since a compilation re-dates an old song ("Waka Waka" on a 2023 hits
    album); its albums are kept, since "Official FIFA World Cup 2026 Album" is what
    makes a song the World Cup one. A catalog row is preferred as the song itself."""
    out: list[dict] = []
    for r in rows:
        k = row_key(r)
        same = next((o for o in out if same_key(k, row_key(o))), None)
        if same is None:
            out.append({**r, "albums": [r["album"]] if r.get("album") else []})
            continue
        albums = same["albums"] + ([r["album"]] if r.get("album")
                                   and r["album"] not in same["albums"] else [])
        dates = [d for d in (same.get("date"), r.get("date")) if d]
        first = min(dates, key=lambda d: d[:4] + d[4:].ljust(6, "9")) if dates else ""
        base = same
        if r["where"] == "catalog" and (same["where"] != "catalog"
                                        or (r.get("date") or "9") < (same.get("date") or "9")):
            base = r
        merged = {**base, "albums": albums, "about": same.get("about") or r.get("about"),
                  "date": first, "year": first[:4]}
        same.clear()
        same.update(merged)
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


# "Another one" played "MIA (feat. Drake)" and then "MÍA (feat. Drake)": the same
# song, a single and the album cut, told apart only by an accent (the owner's session,
# 1.1.0). What was played is kept as a key that such versions share.
_VIDEO_WORDS = re.compile(r"\b(?:official|video|audio|lyrics?|clip|hd|4k|mv|visualizer|"
                          r"live|remaster(?:ed)?|version)\b", re.I)


# "1969 - Remastered 2009" is a version of 1969, not a song called that by "1969".
_VERSION = re.compile(r"(?:\d{4}\s+)?(?:remaster|live\b|radio\s+edit|edit\b|mono\b|"
                      r"stereo\b|version|single\s+version|acoustic|demo\b|from\s)", re.I)


def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", s).strip()


def song_key(title: str, artist: str = "") -> tuple[str, str]:
    """(title, main artist), casefolded, accents gone, extras dropped. A YouTube title
    "Bad Bunny - MÍA ft. Drake (Official Video)" gives the same key as the catalog's
    "MIA (feat. Drake)" by "Bad Bunny"."""
    t = (title or "").strip()
    parts = re.split(r"\s[-\u2013\u2014]\s", t, maxsplit=1)
    if not artist and len(parts) == 2 and not _VERSION.match(parts[1]):
        artist, t = parts
    base = t
    while True:                      # innermost first: "[... World Cup (TM) Song]"
        base, n = re.subn(r"[\(\[][^\(\)\[\]]*[\)\]]", " ", base)
        if not n:
            break
    base = re.sub(r"\s[-\u2013\u2014|]\s.*$|\s(?:feat\.?|ft\.?|featuring)\s.*$", " ",
                  base, flags=re.I)
    title_k = _plain(_VIDEO_WORDS.sub(" ", base)) or _plain(t)
    main = re.split(r"\s*(?:,|&|\bx\b|\band\b|\bfeat\.?|\bft\.?|\bwith\b)\s*",
                    artist or "", maxsplit=1, flags=re.I)[0]
    return title_k, _plain(re.sub(r"(?i)\s*-\s*topic$|vevo$", "", main))


def same_key(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """The same song: the same title, and artists that agree or one of them unknown."""
    if not a[0] or a[0] != b[0]:
        return False
    return not a[1] or not b[1] or a[1] == b[1] or a[1] in b[1] or b[1] in a[1]


def row_key(r: dict) -> tuple[str, str]:
    return song_key(r.get("title", ""), r.get("artist") or "")


def played_before(r: dict, keys: list[tuple[str, str]]) -> bool:
    k = row_key(r)
    return any(same_key(k, o) for o in keys)


# ---------------------------------------------------------------- playing

def play_library(pid: str, audible: bool = True) -> tuple[bool, dict]:
    """Play one of her own songs. `audible`: Music's own volume slider at 0 plays in
    silence while the reply says "playing", so it is brought up first."""
    raise_it = ('if sound volume < 5 then\nset sound volume to 60\nset raised to "yes"\n'
                'end if\n') if audible else ""
    # A streamed track takes a moment, so the state is read for up to three seconds
    # (0.35 s measured live when Music is free). In the owner's session the track
    # asked for became Music's current track later and sat paused: one more `play`
    # when it is loaded but not playing. See the module docstring.
    script = f'''tell application "Music"
set raised to "no"
{raise_it}set t to (first track of library playlist 1 whose persistent ID is "{_q(pid)}")
play t
repeat 12 times
if player state is playing then exit repeat
delay 0.25
end repeat
if player state is not playing then
try
if persistent ID of current track is "{_q(pid)}" then
play
repeat 4 times
if player state is playing then exit repeat
delay 0.25
end repeat
end if
end try
end if
set now to ""
try
set now to name of current track
end try
return (player state as text) & "{FIELD}" & now & "{FIELD}" & raised
end tell'''
    ok, text = mac._osa(script, timeout=10.0)
    parts = (text or "").split(FIELD)
    state = parts[0] if ok and parts else ""
    info = {"state": state or text[:120], "now": parts[1] if len(parts) > 1 else "",
            "raised_volume": len(parts) > 2 and parts[2] == "yes"}
    playing = ok and state == "playing"
    if not playing:
        info["dialog"] = music_dialog()
    return playing, info


# ---------------------------------------------------------------- Accessibility
# Music's own window, read the way VoiceOver reads it (actions/apps.py). The bundled
# app has Accessibility permission; without it nothing here is found and she is told
# to press play, as before. Every lookup goes through these two seams, so a test
# hands in a fake tree and never touches the real Music.

def _ax():
    from . import apps
    return apps._ax()


def _music_pid():
    from . import apps
    return apps._pid_for("Music")


def _get(ax, el, name):
    try:
        err, val = ax["get"](el, name, None)
        return val if err == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _music_root():
    ax = _ax()
    if ax is None:
        return None, None
    try:
        if not ax["trusted"]():
            return None, None
    except Exception:  # noqa: BLE001
        return None, None
    pid = _music_pid()
    if pid is None:
        return None, None
    root = ax["app"](pid)
    if ax.get("timeout"):
        try:
            ax["timeout"](root, 2.0)
        except Exception:  # noqa: BLE001
            pass
    return ax, root


def music_dialog() -> bool:
    """A dialog or sheet is up in Music ("Open Stream", a sign-in, an alert). Music
    answers `play` without error and plays nothing while one is open. Read only:
    never dismissed, it is hers."""
    ax, root = _music_root()
    if ax is None:
        return False
    for w in _get(ax, root, "AXWindows") or []:
        sub = str(_get(ax, w, "AXSubrole") or "")
        if sub in ("AXDialog", "AXSystemDialog", "AXFloatingWindow") or _get(ax, w, "AXModal"):
            return True
        if any(str(_get(ax, k, "AXRole") or "") == "AXSheet"
               for k in _get(ax, w, "AXChildren") or []):
            return True
    return False


def _song_toggle(ax, root, track_id, cap: int = 4000):
    """The play toggle in the row of this very song on its album page, or None."""
    want = re.compile(r"AlbumTrackLockup\[id=track-lockup-\d+-" + re.escape(str(track_id))
                      + r"\b")
    seen = 0
    stack = list(_get(ax, root, "AXWindows") or [])
    while stack and seen < cap:
        el = stack.pop()
        seen += 1
        if want.search(str(_get(ax, el, "AXIdentifier") or "")):
            for kid in _get(ax, el, "AXChildren") or []:
                if str(_get(ax, kid, "AXRole") or "") == "AXCheckBox":
                    return kid
            return None
        stack.extend(reversed(_get(ax, el, "AXChildren") or []))
    return None


def _now_playing() -> tuple[str, str]:
    ok, text = mac._osa('if application "Music" is running then\ntell application "Music"\n'
                        'set now to ""\ntry\nset now to name of current track\nend try\n'
                        f'return (player state as text) & "{FIELD}" & now\nend tell\nend if',
                        timeout=4.0)
    parts = (text or "").split(FIELD) if ok else []
    return (parts[0], parts[1]) if len(parts) == 2 else ("", "")


def press_song_play(track_id, title: str, wait_s: float = 3.0,
                    confirm_s: float = 2.0) -> tuple[bool, dict]:
    """After open_in_music: press the song's own play toggle on its page, then read
    the player back. True only when Music is playing a song with this title.

    `wait_s`/`confirm_s`: a real press starts the song in 0.3s (checked live
    2026-09-27); these are how long to keep looking before giving up. They were 5.0
    and 3.0 - the owner's session paid the full 5s on a page whose toggle never
    appeared at all ("bad bunny song", 8.1s total turn, wait_s used in full) and got
    nothing for it. 3.0/2.0 still leaves 10x that measured 0.3s of headroom for a
    slow render while capping the worst case; not re-measured live (no live Music
    playback in this lane's tests), so watch the next real session for a page that
    now times out where it used to just make it."""
    info = {"pressed": False}
    state, _ = _now_playing()
    if not state:
        # Music cannot be read (not running, or osascript refused): pressing blind
        # would leave nothing to check the press against.
        info["why"] = "music_unreadable"
        return False, info
    ax, root = _music_root()
    if ax is None:
        info["why"] = "no_accessibility"
        return False, info
    end, toggle = time.time() + wait_s, None
    while toggle is None:
        toggle = _song_toggle(ax, root, track_id)
        if toggle is not None or time.time() > end:
            break
        time.sleep(0.2)
    if toggle is None:
        info["why"] = "no_play_button"
        return False, info
    try:
        err = ax["press"](toggle, "AXPress")
    except Exception as e:  # noqa: BLE001
        err = repr(e)[:80]
    info["pressed"] = err == 0
    if err != 0:
        info["why"] = f"press_refused {err}"
        return False, info
    want = row_key({"title": title})[0]
    end = time.time() + confirm_s
    while True:
        state, now = _now_playing()
        if state == "playing" and row_key({"title": now})[0] == want:
            info.update(state=state, now=now)
            return True, info
        if time.time() > end:
            info.update(state=state, now=now, why="not_playing_after_press")
            return False, info
        time.sleep(0.15)


def open_in_music(url: str) -> bool:
    """Put the Music app on a catalog song. It does not start playing by itself."""
    return _open(["open", "-a", "Music", app_link(url)])


# The same words drive Music and Spotify. The _10 pair are "back 10 seconds" and
# "forward 10 seconds" said as a follow-up (savta/followup.py).
_CONTROLS = {"pause": "pause", "stop": "pause", "resume": "play",
             "restart": "set player position to 0\nplay",
             "forward": "set player position to (player position + 30)",
             "back": "set player position to (player position - 15)",
             "forward_10": "set player position to (player position + 10)",
             "back_10": "set player position to (player position - 10)",
             "next": "next track", "previous": "previous track"}


def state(app: str) -> str:
    """"playing", "paused", "stopped", or "not_running", for "apple_music" or
    "spotify"; "" when it could not be read. Never launches the app to find out."""
    name = {"apple_music": "Music", "spotify": "Spotify"}.get(app)
    if not name:
        return ""
    ok, out = mac._osa(f'if application "{name}" is running then\n'
                       f'tell application "{name}" to return (player state as string)\n'
                       f'else\nreturn "not_running"\nend if')
    out = (out or "").strip().lower()
    return out if ok and out in ("playing", "paused", "stopped", "not_running") else ""


def control(action: str) -> tuple[bool, str]:
    """pause, resume, restart, forward, back, stop, for whatever Music is playing.
    `stop` pauses rather than stops, so carrying on later picks up where it was."""
    cmd = _CONTROLS.get(action)
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
    cmd = _CONTROLS.get(action)
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
    "dialog": {
        "english": "Music has a window open asking you something, so it would not start {title}. Close that window and ask me again.",
        "hebrew":  "באפליקציית המוזיקה פתוח חלון ששואל משהו, אז {title} לא התחיל. «תסגרי|תסגור» את החלון «ותבקשי|ותבקש» שוב.",
        "arabic":  "في شباك مفتوح بتطبيق الموسيقى عم يسأل إشي، فما اشتغلت {title}. «سكّري|سكّر» الشباك و«اطلبي|اطلب» مرة تانية.",
        "russian": "В Music открыто окно с вопросом, поэтому {title} не включилась. Закройте его и попросите ещё раз.",
    },
    "identified_year": {
        "english": "That is {title}, from {year}.",
        "hebrew":  "זה {title}, מ־{year}.",
        "arabic":  "هاي {title}، من سنة {year}.",
        "russian": "Это {title}, {year} года.",
    },
    "identified": {
        "english": "That is {title} by {artist}.",
        "hebrew":  "זה {title} של {artist}.",
        "arabic":  "هاي {title} لـ{artist}.",
        "russian": "Это {title}, {artist}.",
    },
    "identified_missing": {
        "english": "I think you mean {title} by {artist}, but I could not find it in {app}.",
        "hebrew":  "נראה לי ש«את מתכוונת|אתה מתכוון» ל{title} של {artist}, אבל לא מצאתי אותו ב{app}.",
        "arabic":  "بظن «قصدك|قصدك» {title} لـ{artist}، بس ما لقيتها على {app}.",
        "russian": "Думаю, вы имеете в виду {title}, {artist}, но я не нашла её в {app}.",
    },
    "not_identified": {
        "english": "I could not work out which song that is. What is it called?",
        "hebrew":  "לא הצלחתי להבין איזה שיר זה. איך קוראים לו?",
        "arabic":  "ما عرفت أي أغنية هاي. شو اسمها؟",
        "russian": "Я не поняла, какая это песня. Как она называется?",
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
