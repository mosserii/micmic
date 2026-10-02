#!/usr/bin/env python3
"""Songs in Apple Music and Spotify, and every other app she names, offline.

"On Netflix", "search headphones on Amazon", "send it on Telegram", "in Safari": the
named place wins (savta/actions/targets.py). Those sections are below the songs.

    cd <checkout> && .venv/bin/python3 tests/test_music.py

Nothing leaves the machine. The stub wall is tests/test_micmic.py, loaded the way
tests/perf/bench.py loads it (its main() never runs): `open` and osascript are
blocked, state lives in a scratch directory. On top of it: the iTunes Search API
answers from a fixture, osascript is a fake that knows the four Music scripts, and
Jev is scripted, so the whole flow runs through router.handle() for $0.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types
from pathlib import Path

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-music-state-")
for _k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL"):
    os.environ.pop(_k, None)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "perf"))
import bench                                   # noqa: E402

tm = bench.load_wall()
router, mac = tm.router, tm.mac
from savta.actions import music as am          # noqa: E402

PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail: str = "") -> bool:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return bool(ok)


# ---------------------------------------------------------------- fixtures
def _song(tid, name, artist, album, cid, streamable=True, date="1969-08-05", aid=None):
    return {"wrapperType": "track", "kind": "song", "trackId": tid, "trackName": name,
            "artistName": artist, "collectionName": album, "releaseDate": f"{date}T07:00:00Z",
            **({"artistId": aid} if aid else {}),
            "isStreamable": streamable,
            "trackViewUrl": f"https://music.apple.com/il/album/x/{cid}?i={tid}&uo=4"}


CATALOG = {"resultCount": 5, "results": [
    _song(89317104, "1969", "The Stooges", "The Stooges (Deluxe Edition)", 89319049),
    _song(281208462, "1969", "Boards of Canada", "Geogaddi", 281208446),
    _song(393789243, "1969", "The Stooges", "The Stooges", 393789238),
    _song(1617969126, "1969", "Aviv Geffen", "שנות הירח", 1617968844),
    _song(111, "1969 (Karaoke Version)", "Karaoke Hits", "Karaoke", 110, streamable=False),
]}
FIELD, ROW = am.FIELD, am.ROW
LIB = {"achille": ["423EEDBA1657F8CB", "1969 (feat. Boss Doms)", "Achille Lauro", "1969 - Rebirth"],
       "stooges": ["A1B2C3D4E5F60718", "1969", "The Stooges", "The Stooges"]}

# A second artist, for "change it to <someone else>" over a song already playing.
ABBA = {"resultCount": 2, "results": [
    _song(501, "Dancing Queen", "ABBA", "Arrival", 500),
    _song(502, "Mamma Mia", "ABBA", "ABBA", 503),
]}

FETCHED: list[str] = []
SCRIPTS: list[str] = []
OPENS: list[list] = []
STATE = {"library": ["achille"], "catalog": CATALOG, "playing": True, "more": {},
         "now": ("paused", ""), "wiki": {"query": {"pages": {}}}, "lookup": {"results": []}}


def fake_fetch(url, timeout):
    FETCHED.append(url)
    if STATE["catalog"] is None:
        raise OSError("offline")
    from urllib.parse import parse_qs, urlsplit
    if "wikipedia.org" in url:
        return STATE["wiki"]
    if "/lookup?" in url:
        return STATE["lookup"]
    term = parse_qs(urlsplit(url).query)["term"][0]
    for word, found in STATE["more"].items():
        if word in term.lower():
            return found
    # Apple's search is fuzzy: with an asking word in it, 1969 is not found.
    if "1969" not in term or any(w in term for w in ("תשימי", "put", "song")):
        return {"results": []}
    return STATE["catalog"]


def fake_osa(script: str, timeout: float = 4.0):
    SCRIPTS.append(script)
    if "search library playlist 1" in script:
        # Like Music's own search: every word must be in the title, artist or album.
        term = script.split('for "', 1)[1].split('" only songs', 1)[0].lower().split()
        rows = [LIB[k] for k in STATE["library"]
                if all(w in " ".join(LIB[k][1:]).lower() for w in term)]
        return True, "".join(FIELD.join(r) + ROW for r in rows)
    if "whose persistent ID is" in script:
        return True, FIELD.join(["playing" if STATE["playing"] else "stopped", "1969", "yes"])
    if "name of current track" in script:          # music._now_playing
        return True, FIELD.join(STATE["now"])
    return True, ""


def fake_open(cmd):
    OPENS.append(list(cmd))
    return True


from savta.actions import targets as tg     # noqa: E402

COPIED: list[str] = []
tg._copy = lambda text: (COPIED.append(text), True)[1]
APPS_HERE = ["Calculator", "Music", "Maps", "Pages", "Photos", "Safari", "Telegram",
             "WhatsApp", "Messages"]
tg.installed_apps = lambda: list(APPS_HERE)

am._fetch = fake_fetch
REAL_YT_SEARCH = router.yt.search
am._open = fake_open
am.spotify_installed = lambda: False
REAL_COUNTRY = am.country
am.country = lambda: "il"


# ---------------------------------------------------------------- scripted Jev
QUIET = ("not_applicable", "unspecified", "none", "nobody", "__none__", "any", "today",
         "unrevealed", "unchanged", "other")


class ScriptedJev:
    """Answers every question with its quiet default, except what a test scripts."""
    mode = "replay"

    def __init__(self):
        self.calls, self.cost_usd, self.busy_ms = 0, 0.0, 0.0
        self.asked: list[dict] = []
        self.say: dict = {}
        self.pick = None           # fn(options) -> key, for pick_from
        self.fits = None           # fn(instructions) -> noul, for fits_<i>

    def ask(self, state, qs):
        self.calls += 1
        self.asked.append(qs)
        out = {}
        for k, q in qs.items():
            t = q.get("type")
            if t == "choice":
                keys = list(q["criteria"])
                sel = next((c for c in QUIET if c in keys), keys[0])
                out[k] = {"choice": sel, "confidence": 0.9,
                          "probabilities": {c: (0.9 if c == sel else 0.1 / max(1, len(keys) - 1))
                                            for c in keys}}
            elif t == "score":
                out[k] = {"score": 0.0}
            else:
                out[k] = {"noul": 0.95 if k == "is_complete" else 0.0}
        if "best" in qs:           # pick_result, the YouTube path: the first video
            out["best"] = {"choice": "0", "confidence": 0.9, "probabilities": {"0": 0.9}}
            out["any_good"] = {"noul": 0.9}
        if "pick" in qs and self.pick:
            sel = self.pick(qs["pick"]["criteria"])
            out["pick"] = {"choice": sel, "confidence": 0.9, "probabilities": {sel: 0.9}}
            out["any_good"] = {"noul": 0.9}
        if self.fits:
            for k, q in qs.items():
                if k.startswith("fits_"):
                    out[k] = {"noul": self.fits(q["instructions"])}
        for k, v in self.say.items():
            if k in qs:
                out[k] = ({"choice": v, "confidence": 0.9, "probabilities": {v: 0.9}}
                          if isinstance(v, str) else {"noul": v})
        return out

    def warmup(self, *a, **k):
        pass


def by(artist_word: str, lib: bool | None = None):
    def fn(opts):
        for k, label in opts.items():
            if k == "__none__" or artist_word not in (label or ""):
                continue
            if lib is None or (("already in her library" in label) == lib):
                return k
        return "__none__"
    return fn


def turn(utterance, *, intent="music", player=None, subject=None, pick=None, lang=None,
         extra=None, fits=None):
    j = ScriptedJev()
    j.say = {"intent": intent}
    if player:
        j.say["named_app"] = player
    if subject:
        j.say.update(span_subject=subject, span_subject_exists=0.95,
                     names_a_specific_title=0.9)
    if lang:
        j.say["language"] = lang
    j.say.update(extra or {})
    j.pick = pick
    j.fits = fits
    r = router.handle(j, utterance, speak=False, client="native", activation="push")
    return r, j


# Music's own proactive warm-up (actions/music.ensure_running), fired the moment her
# words plausibly want it, even on a turn that ends up finding nothing: a different
# thing from "a song was opened", so tests that check the latter filter this out.
WARMUP_OPEN = ["open", "-g", "-a", "Music"]


def opened_songs() -> list:
    return [o for o in OPENS if o != WARMUP_OPEN]


def fresh():
    tm.reset_state()
    for x in (FETCHED, SCRIPTS, OPENS, COPIED, tm.OPENED, tm.LAUNCHED, tm.APPS, tm.SENT,
              tm.WA, tm.YT_QUERIES):
        x.clear()
    APPS_HERE[:] = ["Calculator", "Music", "Maps", "Pages", "Photos", "Safari", "Telegram",
                    "WhatsApp", "Messages"]
    STATE.update(library=["achille"], catalog=CATALOG, playing=True, more={},
                 now=("paused", ""), wiki={"query": {"pages": {}}}, lookup={"results": []})
    am._CACHE.clear()
    am._ax = lambda: None                    # no Accessibility unless a test hands a fake
    router.yt.search = REAL_YT_SEARCH
    mac._osa = fake_osa
    router.prefs._update(lambda d: d.update(music_app=""))


# ================================================================== unit
def t_units():
    rows = am.parse_catalog(CATALOG)
    check("catalog: songs parsed, the one Apple says will not stream dropped",
          [r["artist"] for r in rows] == ["The Stooges", "Boards of Canada", "The Stooges",
                                          "Aviv Geffen"], [r["artist"] for r in rows])
    check("catalog: rows carry the page link",
          rows[0]["url"].startswith("https://music.apple.com/il/album/"))
    check("app link opens in Music and keeps only the song",
          am.app_link(rows[0]["url"]) == "music://music.apple.com/il/album/x/89319049?i=89317104",
          am.app_link(rows[0]["url"]))
    lib = am.parse_library(FIELD.join(LIB["stooges"]) + ROW + "garbage" + ROW)
    check("library rows parsed, garbage skipped",
          len(lib) == 1 and lib[0]["pid"] == "A1B2C3D4E5F60718", lib)
    check("same song across catalog and library, remaster suffixes ignored",
          am.same_song({"title": "1969 - Remastered", "artist": "The Stooges"},
                       {"title": "1969", "artist": "the stooges"}))
    check("a different artist is not the same song",
          not am.same_song(rows[0], rows[1]))
    for s, want in [("put 1969 song on apple music", True), ("play Shakira on Spotify", True),
                    ("תשים את השיר ב־Apple Music", True), ("תשימי 1969 באפל מיוזיק", True),
                    ("put the trailer on YouTube", True), ("شغليلي فيروز على سبوتيفاي", True),
                    ("включи 1969 в Эппл Мьюзик", True), ("play it in Music", True),
                    ("play 1969", False), ("play some music", False),
                    ("תשימי שיר של זוהר ארגוב", False), ("включи музыку", False)]:
        check(f"names a player: {s!r} -> {want}", am.names_a_player(s) == want)
    for s, want in [("1969 on apple music", "1969"), ("Shakira on Spotify", "Shakira"),
                    ("1969 באפל מיוזיק", "1969"), ("השיר ב־Apple Music", "השיר"),
                    ("فيروز على سبوتيفاي", "فيروز"), ("1969 в Эппл Мьюзик", "1969")]:
        check(f"player name kept out of the search: {s!r}", am.without_player(s) == want,
              am.without_player(s))
    # The store comes from the Apple ID Music is signed in to, not the Mac's region.
    import plistlib
    d = tempfile.mkdtemp()
    p = Path(d) / "c.plist"
    p.write_bytes(plistlib.dumps({"ICDefaultsKeyLastKnownLocalStoreAccountProperties":
                                  {"storefrontIdentifier": "143491-2,42"}}))
    real = am._ITUNESCLOUD
    am._ITUNESCLOUD = p
    check("country from the Apple ID store (143491 is Israel)", REAL_COUNTRY() == "il")
    am._ITUNESCLOUD = Path(d) / "missing.plist"
    os.environ["MICMIC_LOCALE"] = "en_FR"
    check("no store on record: the Mac's region", REAL_COUNTRY() == "fr")
    os.environ.pop("MICMIC_LOCALE")
    am._ITUNESCLOUD = real
    mac._osa = fake_osa
    ok, info = am.play_library("A1B2C3D4E5F60718")
    check("play_library plays by persistent ID and reports it playing",
          ok and 'persistent ID is "A1B2C3D4E5F60718"' in SCRIPTS[-1], SCRIPTS[-1][:120])
    check("a silent Music volume slider is brought up first",
          "set sound volume to 60" in SCRIPTS[-1] and info["raised_volume"])
    am.control("pause")
    check("pause never launches Music to pause it",
          SCRIPTS[-1].startswith('if application "Music" is running then'), SCRIPTS[-1])
    check("replies carry no em dash",
          not any("\u2014" in t for block in am.LINES.values() for t in block.values()))


# ================================================================== the flow
def t_owner_case_catalog_only():
    """"please put 1969 song on apple music" read as watch, and went to YouTube."""
    fresh()
    r, j = turn("please put 1969 song on apple music", intent="watch", player="apple_music",
                subject="1969", pick=by("The Stooges", lib=False))
    check("owner case: Apple Music, not YouTube", r["did"] == "opened_in_app",
          f"did={r['did']} say={r.get('say')}")
    check("the named-app question rode in understand()", "named_app" in j.asked[0])
    check("two round trips, the same as YouTube (understand + pick)", j.calls == 2,
          f"calls={j.calls}")
    check("searched the catalog in her store for the song, not for 'apple music'",
          FETCHED and all("music" not in u.split("term=")[1].lower() for u in FETCHED)
          and any("term=1969&" in u and "country=IL" in u for u in FETCHED), FETCHED)
    check("her library was searched too",
          any("search library playlist 1" in s for s in SCRIPTS))
    check("Music was opened on that exact song",
          OPENS and OPENS[-1][:3] == ["open", "-a", "Music"]
          and OPENS[-1][3].endswith("?i=89317104"), OPENS)
    check("and she is told to press play, not that it is playing",
          r["say"] == "1969 by The Stooges is open in Apple Music. Press play.", r["say"])
    check("no YouTube opened", not tm.OPENED, tm.OPENED)


def t_measured_span_shapes():
    """The two shapes the name span really came back in (6 real Jev calls, 2026-09-27)."""
    fresh()
    r, j = turn("put 1969 song on apple music", intent="music", player="apple_music",
                subject=None, pick=by("The Stooges", lib=False))
    check("no name span at all: still finds 1969 from her own words",
          r["did"] == "opened_in_app" and "The Stooges" in r["say"], f"{r['did']} {r['say']}")
    check("and searched for 1969, not for what she likes",
          any("term=1969&" in u for u in FETCHED) and not any("popular" in u for u in FETCHED),
          FETCHED)
    check("still two round trips", j.calls == 2, j.calls)
    fresh()
    r, _ = turn("תשימי 1969 באפל מיוזיק", player="apple_music", subject="תשימי 1969",
                lang="hebrew", pick=by("The Stooges", lib=False))
    check("the span with the verb in it: 1969 still found",
          r["did"] == "opened_in_app" and "The Stooges" in r["say"], f"{r['did']} {r['say']}")
    check("another one then searches for the words that found it",
          router.MEM.play_query == "1969", router.MEM.play_query)


def t_in_library_plays():
    fresh()
    STATE["library"] = ["achille", "stooges"]
    r, j = turn("put 1969 on apple music", player="apple_music", subject="1969",
                pick=by("The Stooges", lib=True))
    check("in her library: played directly", r["did"] == "playing", f"did={r['did']}")
    check("by its persistent ID",
          any('persistent ID is "A1B2C3D4E5F60718"' in s for s in SCRIPTS))
    check("and said so", r["say"] == "Playing 1969 by The Stooges in Apple Music.", r["say"])
    check("nothing opened in a browser or app",
          not opened_songs() and not tm.OPENED, (OPENS, tm.OPENED))
    check("the undo button is offered", bool(r.get("undo")))
    check("the catalog copy of a library song is not offered twice",
          sum("The Stooges, from The Stooges" in (l or "")
              for l in j.asked[1]["pick"]["criteria"].values()) == 1)

    # Catalog pick that she also owns, which the search for her words missed.
    fresh()
    STATE["library"] = ["stooges"]
    r, _ = turn("put Iggy Pop 1969 on apple music", player="apple_music",
                subject="Iggy Pop 1969", pick=by("Deluxe"))
    check("a catalog pick she owns plays her own copy", r["did"] == "playing",
          f"did={r['did']}")

    # Music would not start it: honest, and Music is put on the song instead.
    fresh()
    STATE.update(library=["stooges"], playing=False)
    r, _ = turn("put 1969 on apple music", player="apple_music", subject="1969",
                pick=by("The Stooges", lib=True))
    check("her copy would not start: never says playing", r["did"] != "playing",
          f"did={r['did']} say={r['say']}")


def t_stop_and_undo_pause_music():
    fresh()
    STATE["library"] = ["stooges"]
    turn("put 1969 on apple music", player="apple_music", subject="1969",
         pick=by("The Stooges", lib=True))
    SCRIPTS.clear()
    tm.CLOSED.clear()
    r, _ = turn("stop", intent="stop")
    check("stop pauses Music", r["did"] == "stopped"
          and any(s.startswith('if application "Music" is running') and "pause" in s
                  for s in SCRIPTS), SCRIPTS)
    check("and does not close her front window", not tm.CLOSED, tm.CLOSED)
    check("and says so", r["say"] == "I paused the music.", r["say"])
    r, _ = turn("carry on", intent="player", extra={"player_action": "resume"})
    check("carry on resumes Music", r["did"] == "resume"
          and 'tell application "Music"\nplay' in SCRIPTS[-1], SCRIPTS[-1:])

    fresh()
    STATE["library"] = ["stooges"]
    turn("put 1969 on apple music", player="apple_music", subject="1969",
         pick=by("The Stooges", lib=True))
    SCRIPTS.clear()
    u = router.undo_last()
    check("undo pauses Music", u["undone"] and "pause" in SCRIPTS[-1], (u, SCRIPTS))

    # Opened but never started: "play" must not start whatever Music played last.
    fresh()
    turn("put 1969 on apple music", player="apple_music", subject="1969",
         pick=by("The Stooges", lib=False))
    SCRIPTS.clear()
    r, _ = turn("play it", intent="player", extra={"player_action": "resume"})
    check("resume on a song only opened: asks her to press play, sends no play",
          r["did"] == "opened_in_app" and not SCRIPTS, (r["did"], SCRIPTS))


def t_another_one_stays_in_music():
    fresh()
    turn("put 1969 on apple music", player="apple_music", subject="1969",
         pick=by("The Stooges", lib=False))
    first = router.MEM.last_played["id"]
    OPENS.clear()
    r, j = turn("another one", intent="music", extra={"rejects_last": 0.9},
                pick=by("1969", lib=False))
    check("another one: a different song, in Apple Music",
          r["did"] == "opened_in_app" and router.MEM.last_played["id"] != first,
          f"did={r['did']} id={router.MEM.last_played and router.MEM.last_played.get('id')}")
    check("the refused one is not offered again",
          all("Deluxe" not in (l or "") for l in j.asked[-1]["pick"]["criteria"].values()))
    check("no YouTube search for it", not tm.OPENED, tm.OPENED)


def t_hebrew():
    fresh()
    r, _ = turn("תשים את השיר 1969 באפל מיוזיק", player="apple_music", subject="1969",
                lang="hebrew", pick=by("The Stooges", lib=False))
    check("Hebrew: answered in Hebrew", r["lang"] == "hebrew"
          and r["say"].startswith("פתחתי את 1969 של The Stooges באפל מיוזיק."), r["say"])


def t_spotify():
    fresh()
    tm.YT_QUERIES.clear()
    tm.OPENED.clear()
    r, _ = turn("play Shakira on Spotify", player="spotify", subject="Shakira",
                pick=None)
    check("Spotify not on this Mac: its web player, on her words, and says so",
          r["did"] == "opened_in_app"
          and r["say"].startswith("Spotify is not on this computer, so its website is open")
          and tm.OPENED == ["https://open.spotify.com/search/Shakira"],
          f"did={r['did']} say={r['say']} opened={tm.OPENED}")
    check("and nothing was handed to a Spotify app or to YouTube",
          not OPENS and not tm.YT_QUERIES, (OPENS, tm.YT_QUERIES))

    fresh()
    am.spotify_installed = lambda: True
    try:
        r, _ = turn("play 1969 on Spotify", player="spotify", subject="1969",
                    pick=by("The Stooges", lib=False))
        check("Spotify: opened on a search for the exact song",
              r["did"] == "opened_in_app"
              and OPENS[-1] == ["open", "spotify:search:1969%20The%20Stooges"], OPENS)
        check("and told to press play", "Press play" in r["say"], r["say"])
        check("her Music library is not searched for Spotify",
              not any("search library" in s for s in SCRIPTS))
        SCRIPTS.clear()
        r, _ = turn("pause", intent="stop")
        check("stop pauses Spotify", any('application "Spotify"' in s for s in SCRIPTS),
              SCRIPTS)
        # Apple unreachable: Spotify still gets her words.
        fresh()
        STATE["catalog"] = None
        r, _ = turn("play 1969 on Spotify", player="spotify", subject="1969")
        check("Apple unreachable: Spotify opened on her own words",
              OPENS and OPENS[-1] == ["open", "spotify:search:1969"], OPENS)
    finally:
        am.spotify_installed = lambda: False


def t_unspecified_and_preference():
    fresh()
    r, j = turn("play 1969", subject="1969")
    check("no player named: the question is not even asked",
          "named_app" not in j.asked[0] and "span_app_query" not in j.asked[0])
    check("and it is YouTube, as before", r["did"] == "playing" and tm.OPENED
          and not FETCHED, f"did={r['did']}")

    fresh()
    router.prefs.set_music_app("apple_music")
    r, _ = turn("play 1969", subject="1969", pick=by("The Stooges", lib=False))
    check("stored preference apple_music: a song goes to Apple Music",
          r["did"] == "opened_in_app", f"did={r['did']}")
    fresh()
    router.prefs.set_music_app("apple_music")
    r, _ = turn("play the Titanic film", intent="watch", subject="Titanic")
    check("the preference is for songs: a film still goes to YouTube",
          r["did"] == "playing" and not FETCHED, f"did={r['did']}")
    fresh()
    router.prefs.set_music_app("apple_music")
    r, _ = turn("put 1969 on YouTube", player="youtube", subject="1969")
    check("a named YouTube beats the preference", r["did"] == "playing" and not FETCHED,
          f"did={r['did']}")
    fresh()
    check("a weak player answer does not switch apps",
          router._media_player({"named_app": "apple_music", "named_app_confidence": 0.3},
                               "music", False) == "youtube")


def t_not_found_and_unreachable():
    fresh()
    STATE.update(catalog={"results": []}, library=[])
    r, j = turn("put 1969 on apple music", player="apple_music", subject="1969")
    check("nothing in Apple Music: says so, no Jev pick", r["did"] == "not_found"
          and r["say"] == "I could not find 1969 in Apple Music." and j.calls == 1, r["say"])
    fresh()
    r, _ = turn("put 1969 on apple music", player="apple_music", subject="1969",
                pick=lambda opts: "__none__")
    check("Jev finds none of them right: not found, nothing opened",
          r["did"] == "not_found" and not opened_songs(), (r["did"], OPENS))
    fresh()
    STATE.update(catalog=None, library=[])
    r, _ = turn("put 1969 on apple music", player="apple_music", subject="1969")
    check("Apple unreachable: says that, not 'not found'",
          r["say"] == "I could not reach Apple Music just now.", r["say"])


# ================================================================== any named app
IN_APP_DID = ("app_done", "app_blocked", "app_needs_her", "cannot_reach_app")


def t_follow_ups_stay_in_the_player():
    """Over what MicMic put on, a follow-up naming someone new is a new search in the
    SAME player, never the in-app driver. The owner's session (1.1.0): after a song in
    Apple Music, two requests for another artist (intent music, rejects_last 0.96,
    inside_an_app 0.75; then naming Apple Music, refers_back 0.6, inside_an_app 0.83)
    went to the Accessibility driver instead."""
    # bench's YouTube stand-in keeps no record of what was searched; this one does.
    real_search = tm.yt.search
    tm.yt.search = lambda q, n=18: (YT.append(q), real_search(q, n))[1]
    try:
        _follow_ups()
    finally:
        tm.yt.search = real_search


YT: list[str] = []


def _follow_ups():
    def apple_first():
        fresh()
        STATE["more"] = {"abba": ABBA, "אבבא": ABBA}
        turn("put 1969 on apple music", player="apple_music", subject="1969",
             pick=by("The Stooges", lib=False))
        FETCHED.clear()
        OPENS.clear()
        tm.OPENED.clear()
        YT.clear()

    apple_first()
    r, j = turn("change it to Abba please", subject="Abba", pick=by("ABBA"),
                extra={"rejects_last": 0.96, "inside_an_app": 0.75,
                       "span_instead": "Abba", "span_instead_exists": 0.8})
    check("'change to <artist>' over Apple Music: that artist, in Apple Music",
          r["did"] == "opened_in_app" and r["detail"].get("player") == "apple_music"
          and r["detail"].get("artist") == "ABBA" and any("Abba" in f for f in FETCHED),
          (r["did"], r["detail"]))
    check("  not the in-app driver, not YouTube",
          r["did"] not in IN_APP_DID and not YT, (r["did"], YT))
    check("  one understanding and one pick: no extra round trip", j.calls == 2, j.calls)
    check("  and it is what is playing now", router.MEM.last_played.get("artist") == "ABBA"
          and router.MEM.last_played.get("player") == "apple_music", router.MEM.last_played)

    apple_first()
    r, _ = turn("switch it to Abba", subject="Abba", pick=by("ABBA"),
                extra={"describes_instead": 0.8, "inside_an_app": 0.9})
    check("described rather than corrected: still Apple Music, still not the driver",
          r["detail"].get("player") == "apple_music" and r["detail"].get("artist") == "ABBA"
          and r["did"] not in IN_APP_DID, (r["did"], r["detail"]))

    apple_first()
    r, j = turn("on apple music, Abba now", player="apple_music", subject="Abba",
                pick=by("ABBA"),
                extra={"rejects_last": 0.93, "refers_back": 0.6, "inside_an_app": 0.83,
                       "span_instead": "Abba", "span_instead_exists": 0.8})
    check("'on apple music, <artist> now': that artist, in Apple Music, not the driver",
          r["did"] == "opened_in_app" and r["detail"].get("artist") == "ABBA"
          and r["did"] not in IN_APP_DID, (r["did"], r["detail"]))

    apple_first()
    r, _ = turn("תחליפי לאבבא", subject="אבבא", pick=by("ABBA"), lang="hebrew",
                extra={"rejects_last": 0.9, "inside_an_app": 0.8,
                       "span_instead": "אבבא", "span_instead_exists": 0.8})
    check("Hebrew 'change to <artist>': Apple Music, answered in Hebrew",
          r["detail"].get("player") == "apple_music" and r["detail"].get("artist") == "ABBA"
          and r["lang"] == "hebrew", (r["did"], r["detail"], r["lang"]))

    # Measured live: Jev gave the Hebrew name with its glued "to" ("ל" + the name),
    # which Apple's catalog does not find; the name is searched without it too.
    check("Hebrew 'to <name>': the catalog is also searched for the bare name",
          am.search_terms("לבאד באני", "תחליפי לבאד באני")[0]
          == ["לבאד באני", "באד באני", "תחליפי לבאד באני"],
          am.search_terms("לבאד באני", "תחליפי לבאד באני"))
    check("  a name that starts with the letter is left alone, the library term too",
          am.search_terms("ליאור נרקיס", "תשימי את ליאור נרקיס")[0] == ["ליאור נרקיס"],
          am.search_terms("ליאור נרקיס", "תשימי את ליאור נרקיס"))

    apple_first()
    r, _ = turn("play Abba", subject="Abba", pick=by("ABBA"))
    check("the next song asked for while Music plays goes to Music too",
          r["detail"].get("player") == "apple_music" and not YT, r["detail"])
    apple_first()
    r, _ = turn("play the Titanic film", intent="watch", subject="Titanic")
    check("...but a film still goes to YouTube", r["did"] == "playing"
          and r["detail"].get("video_id") and not FETCHED, (r["did"], r["detail"]))

    apple_first()
    r, _ = turn("another one", extra={"rejects_last": 0.9, "inside_an_app": 0.8},
                pick=by("1969", lib=False))
    check("'another one' keeps its meaning: the next song of the same search, in Music",
          r["detail"].get("after_rejection") and r["detail"].get("player") == "apple_music"
          and not any("Abba" in f for f in FETCHED), r["detail"])

    apple_first()
    SCRIPTS.clear()
    r, _ = turn("play it again", intent="again")
    check("'again' over a song in Music starts it over there, never a YouTube address",
          not tm.OPENED and r["detail"].get("player") == "apple_music",
          (r["did"], tm.OPENED, r["detail"]))

    # A genuine in-app request still goes to the in-app driver, even with a song on.
    apple_first()
    r, _ = turn("in the calculator press five", intent="open_app",
                extra={"inside_an_app": 0.95})
    check("'in the calculator press five' with a song on: still the in-app driver",
          r["did"] in IN_APP_DID, r["did"])

    # Spotify: the same, in Spotify.
    fresh()
    STATE["more"] = {"abba": ABBA}
    am.spotify_installed = lambda: True
    try:
        turn("play 1969 on Spotify", player="spotify", subject="1969",
             pick=by("The Stooges", lib=False))
        r, _ = turn("now Abba", subject="Abba", pick=by("ABBA"),
                    extra={"inside_an_app": 0.8, "span_instead": "Abba",
                           "span_instead_exists": 0.7})
        check("Spotify: 'now <artist>' is a new search in Spotify",
              OPENS and OPENS[-1][1].startswith("spotify:search:") and "ABBA" in OPENS[-1][1]
              and r["did"] not in IN_APP_DID, (r["did"], OPENS[-1:]))
    finally:
        am.spotify_installed = lambda: False

    # YouTube: a correction stays on YouTube even when she prefers Apple Music.
    fresh()
    router.prefs.set_music_app("apple_music")
    turn("put 1969 on YouTube", player="youtube", subject="1969")
    FETCHED.clear()
    YT.clear()
    r, _ = turn("change it to Abba", subject="Abba",
                extra={"rejects_last": 0.9, "inside_an_app": 0.8,
                       "span_instead": "Abba", "span_instead_exists": 0.8})
    check("YouTube: 'change it to <artist>' searches YouTube, not her preferred app",
          r["did"] == "playing" and "Abba" in r["detail"].get("query", "")
          and r["detail"].get("video_id") and not FETCHED, (r["did"], r["detail"], FETCHED))

    # A service she named: Netflix is searched again for the one she meant.
    fresh()
    place_turn("show me Friends on Netflix", "netflix", intent="watch", subject="Friends")
    tm.OPENED.clear()
    YT.clear()
    r, _ = turn("no, I meant Seinfeld", intent="watch", subject="Seinfeld",
                extra={"rejects_last": 0.9, "inside_an_app": 0.8,
                       "span_instead": "Seinfeld", "span_instead_exists": 0.8})
    check("Netflix: 'no, I meant <title>' is a Netflix search for it",
          tm.OPENED == ["https://www.netflix.com/search?q=Seinfeld"] and not YT,
          (r["did"], tm.OPENED, YT))
    tm.OPENED.clear()
    r, _ = turn("another one", intent="watch", extra={"rejects_last": 0.9})
    check("  'another one' there puts that search back, never a YouTube video",
          tm.OPENED == ["https://www.netflix.com/search?q=Seinfeld"] and not YT,
          (r["did"], tm.OPENED, YT))


def place_turn(utterance, place, *, intent="look_up", query=None, conf=0.9, extra=None,
               lang=None, subject=None, pick=None):
    """A turn where Jev says she named `place`, and `query` is what to look for there."""
    x = {"named_app": place} if place else {}
    if query:
        x.update(span_app_query=query, span_app_query_exists=0.95)
    x.update(extra or {})
    j = ScriptedJev()
    j.say = {"intent": intent, **x}
    if subject:
        j.say.update(span_subject=subject, span_subject_exists=0.95,
                     names_a_specific_title=0.9)
    if lang:
        j.say["language"] = lang
    j.pick = pick
    real = j.ask

    def ask(state, qs):
        out = real(state, qs)
        if "named_app" in qs and place:
            out["named_app"] = {"choice": place, "confidence": conf,
                                "probabilities": {place: conf}}
        return out
    j.ask = ask
    r = router.handle(j, utterance, speak=False, client="native", activation="push")
    return r, j


def t_named_units():
    for sentence, want in [
            ("show me Friends on Netflix", ["netflix"]),
            ("search headphones on Amazon", ["amazon"]),
            ("send it on Telegram", ["telegram"]),
            ("play the Beatles in Spotify", ["spotify"]),
            ("תפתחי את זה בנטפליקס", ["netflix"]),
            ("find a hotel in Paris on Booking", ["booking"]),
            ("look it up in Safari", ["app:Safari"]),
            ("open my budget in Pages", ["app:Pages"]),
            ("תבדקי במפות איפה הדואר", ["apple_maps"]),
            ("شغليلي فيروز على سبوتيفاي", ["spotify"]),
            ("найди это в гугле", ["google"]),
            ("שלחי לדנה בוואטסאפ שאני בדרך", ["whatsapp"]),
            # Nothing named: the request must not change at all.
            ("play some music", []), ("play 1969", []), ("booking a table for two", []),
            ("open the calculator", []), ("what is the weather in Paris", []),
            ("send a message to Gal", []), ("תשימי שיר של זוהר ארגוב", [])]:
        check(f"mentions {sentence!r} -> {want}", tg.mentions(sentence) == want,
              tg.mentions(sentence))
    q = tg.question(["amazon", "app:Pages"])["named_app"]["criteria"]
    check("the question: 'none' first, then exactly the places found",
          list(q) == ["none", "amazon", "app:Pages"], list(q))
    for sentence, want in [("search headphones on Amazon", "headphones"),
                           ("find a hotel in Paris on Booking", "hotel Paris"),
                           ("תפתחי את זה בנטפליקס", ""), ("open Netflix", ""),
                           ("תחפשי אוזניות באמזון", "אוזניות")]:
        check(f"query from her words {sentence!r} -> {want!r}", tg.query_from(sentence) == want,
              tg.query_from(sentence))
    check("'it' and 'זה' are nothing to search for",
          tg.is_pronoun("it") and tg.is_pronoun("את זה") and not tg.is_pronoun("Friends"))
    check("Amazon's search page carries her words",
          tg.web_url("amazon", "red shoes", "english") == ("https://www.amazon.com/s?k=red+shoes", True))
    check("Disney+ has no search address: its front page, and it says so",
          tg.web_url("disney_plus", "Frozen", "english") == ("https://www.disneyplus.com/", False))
    check("Wikipedia in her language",
          tg.web_url("wikipedia", "גאווה ודעה קדומה", "hebrew")[0].startswith(
              "https://he.wikipedia.org/w/index.php?search="))
    check("no named-app line has an em dash",
          not any("\u2014" in t for b in tg.LINES.values() for t in b.values())
          and not any("\u2014" in t for b in am.LINES.values() for t in b.values()))


def t_named_media():
    fresh()
    r, j = place_turn("show me Friends on Netflix", "netflix", intent="watch",
                      subject="Friends")
    check("Netflix: its search page for the title, not YouTube",
          r["did"] == "opened_there"
          and tm.OPENED == ["https://www.netflix.com/search?q=Friends"]
          and not tm.YT_QUERIES, (r["did"], tm.OPENED, tm.YT_QUERIES))
    check("and she is told to pick it there, not that it is playing",
          r["say"] == "Netflix is open on a search for Friends. Pick it there to watch.",
          r["say"])
    check("one round trip, the named-app question inside it",
          j.calls == 1 and "named_app" in j.asked[0] and "span_app_query" in j.asked[0],
          j.calls)

    fresh()
    router.MEM.play_query = "Friends"
    r, _ = place_turn("תפתחי את זה בנטפליקס", "netflix", intent="watch", lang="hebrew",
                      extra={"refers_back": 0.9})
    check("Hebrew 'open it on Netflix': what was just on, searched on Netflix",
          tm.OPENED == ["https://www.netflix.com/search?q=Friends"]
          and r["say"].startswith("פתחתי את נטפליקס על חיפוש של Friends."),
          (tm.OPENED, r["say"]))

    fresh()
    r, _ = place_turn("put Frozen on Disney plus", "disney_plus", intent="watch",
                      subject="Frozen")
    check("Disney+: its front page, and she is told to search there",
          tm.OPENED == ["https://www.disneyplus.com/"]
          and r["say"] == "Disney+ is open. Search for Frozen there.", (tm.OPENED, r["say"]))

    fresh()
    r, _ = place_turn("show me Friends on Netflix", "netflix", intent="watch",
                      subject="Friends", conf=0.3)
    check("a weak answer about the place: YouTube, as before",
          r["did"] == "playing" and any("youtube.com/watch" in u for u in tm.OPENED)
          and not any("netflix" in u for u in tm.OPENED), (r["did"], tm.OPENED))

    fresh()
    router.prefs.set_music_app("apple_music")
    r, _ = place_turn("play the Beatles on Spotify", "spotify", intent="music",
                      subject="the Beatles", pick=by("The Stooges", lib=False))
    check("Spotify named beats her Apple Music preference",
          r["detail"].get("player") == "spotify" and not any(
              "search library" in s for s in SCRIPTS), r["detail"])

    fresh()
    r, _ = place_turn("put on The Boys on Amazon", "amazon", intent="watch", query="The Boys",
                      subject="The Boys")
    check("a film on a site the media path does not play: found there, not on YouTube",
          tm.OPENED == ["https://www.amazon.com/s?k=The+Boys"], tm.OPENED)

    fresh()
    r, _ = place_turn("search cats on YouTube", "youtube", intent="look_up", query="cats")
    check("'search cats on YouTube': YouTube's results page",
          tm.OPENED == ["https://www.youtube.com/results?search_query=cats"], tm.OPENED)


def t_named_messages():
    base = {"contact": "Gal Ben Ami", "contact_is_named": 0.95, "same_person": 0.95,
            "message_has_content": 0.95, "span": "I am running late", "exists": 0.95}
    fresh()
    r, j = place_turn("send Gal on Telegram that I am running late", "telegram",
                      intent="message", extra=base)
    check("Telegram: her chat with Gal opened by number, the words copied",
          r["did"] == "opened_chat"
          and ["open", "tg://resolve?phone=972500000002"] in tm.LAUNCHED
          and COPIED == ["I am running late"], (r["did"], tm.LAUNCHED, COPIED))
    check("told the two keys, never that it was sent",
          r["say"].startswith("Telegram is open on your chat with Gal")
          and "Command-V" in r["say"] and "sent" not in r["say"].lower().replace("to send", ""),
          r["say"])
    check("nothing sent by iMessage or WhatsApp, no countdown",
          not tm.SENT and not tm.WA and router.PENDING is None, (tm.SENT, tm.WA))
    check("still the two round trips a message takes (understand + the words)",
          j.calls <= 3, j.calls)

    fresh()
    APPS_HERE.remove("Telegram")
    r, _ = place_turn("send Gal on Telegram that I am running late", "telegram",
                      intent="message", extra=base)
    check("Telegram not on this Mac: its website, words copied, and says so",
          tm.OPENED == ["https://web.telegram.org/"]
          and r["say"].startswith("Telegram is not on this computer, so its website is open"),
          (tm.OPENED, r["say"]))

    fresh()
    r, _ = place_turn("send Gal on signal that I am running late", "signal",
                      intent="message", extra=base)
    check("Signal not on this Mac and no website: says so plainly, opens nothing, sends nothing",
          r["did"] == "not_installed"
          and r["say"] == "Signal is not on this computer, so I did not send it."
          and not tm.OPENED and not tm.LAUNCHED and not tm.SENT, (r["did"], r["say"]))

    fresh()
    router.prefs.set_app("whatsapp")
    r0, _ = place_turn("send Gal that I am running late", None, intent="message", extra=base)
    router._cancel_pending()
    fresh()
    r, _ = place_turn("send Gal by iMessage that I am running late", "imessage",
                      intent="message", extra=base)
    check("iMessage named beats her stored WhatsApp choice",
          (r0.get("detail") or {}).get("channel") == "whatsapp"
          and (r.get("detail") or {}).get("channel") == "imessage",
          (r0.get("detail"), r.get("detail")))
    router._cancel_pending()
    router.prefs._update(lambda d: d.update(message_app={}))

    fresh()
    router.MEM.remember_draft("Gal Ben Ami", "I am running late", "imessage", "english")
    r, _ = place_turn("send it on Telegram", "telegram", intent="message",
                      extra={"amends_message": 0.95, "contact": "Gal Ben Ami",
                             "refers_back": 0.9})
    check("'send it on Telegram' after a message: that message, in Telegram",
          r["did"] == "opened_chat" and COPIED == ["I am running late"], (r["did"], COPIED))


def t_named_search():
    fresh()
    real_run, real_where = router.web.run, router.web.where_to_start
    STARTS: list = []
    router.web.run = lambda j_, llm, task, start, **k: (STARTS.append(start),
                                                       {"did": "done", "steps": []})[1]
    router.web.where_to_start = lambda *a: (_ for _ in ()).throw(AssertionError("asked"))
    try:
        r, j = place_turn("search headphones on Amazon", "amazon", intent="do_online",
                          query="headphones", extra={"named_app_only_look": 0.9})
        check("'search headphones on Amazon': Amazon's results, fast, no agent",
              r["did"] == "opened_there"
              and tm.OPENED == ["https://www.amazon.com/s?k=headphones"] and not STARTS,
              (r["did"], tm.OPENED))
        check("one round trip", j.calls == 1, j.calls)
        check("and said", r["say"] == "Amazon is open on headphones.", r["say"])

        fresh()
        r, _ = place_turn("order me headphones on Amazon", "amazon", intent="do_online",
                          query="headphones", extra={"named_app_only_look": 0.1})
        # A purchase waits for her yes, and she hears it stops before paying
        # (tests/test_people.py t_money; the bench's hard-safe-002).
        check("'order me headphones on Amazon': asked first, nothing started",
              r["did"] == "confirm_web" and not STARTS and "before paying" in (r["say"] or ""),
              (r["did"], STARTS, r["say"]))
        yes = ScriptedJev()
        yes.say = {"is_answer": 0.95, "agreed": 0.95}
        r = router.handle(yes, "yes", speak=False, client="native", activation="push")
        import time as _t
        for _ in range(40):
            if STARTS:
                break
            _t.sleep(0.05)
        check("and on her yes: the agent, started on Amazon's search",
              r["did"] == "web_working" and STARTS == ["https://www.amazon.com/s?k=headphones"],
              (r["did"], STARTS))
    finally:
        router._web_stop()
        router.web.run, router.web.where_to_start = real_run, real_where

    fresh()
    r, _ = place_turn("find a hotel in Paris on Booking", "booking", query="a hotel in Paris")
    check("Booking: its search for her words",
          tm.OPENED == ["https://www.booking.com/searchresults.html?ss=a+hotel+in+Paris"],
          tm.OPENED)

    fresh()
    r, _ = place_turn("search headphones on Amazon", "amazon", intent="chitchat",
                      query="headphones", extra={"named_app_only_look": 0.9}, conf=0.8)
    check("a soft intent with a clearly named site still goes there",
          tm.OPENED == ["https://www.amazon.com/s?k=headphones"], tm.OPENED)

    fresh()
    router.MEM.app_query = "headphones"
    r, _ = place_turn("and on eBay", "ebay", extra={"refers_back": 0.9})
    check("'and on eBay': what she last searched, now on eBay",
          tm.OPENED == ["https://www.ebay.com/sch/i.html?_nkw=headphones"], tm.OPENED)

    fresh()
    r, _ = place_turn("תבדקי במפות איפה הדואר", "apple_maps", query="איפה הדואר", lang="hebrew")
    check("'in Maps': the Maps app on her words, through its own link",
          any(c[:1] == ["open"] and c[1].startswith("maps://?q=") for c in tm.LAUNCHED)
          and not tm.OPENED, tm.LAUNCHED)

    fresh()
    real_answer = router.LLM_CLIENT.answer
    router.LLM_CLIENT.answer = lambda *a, **k: "The Amazon is a rainforest."
    try:
        r, _ = place_turn("tell me about the Amazon rainforest", "none")
    finally:
        router.LLM_CLIENT.answer = real_answer
    check("control: the Amazon river is not Amazon.com (Jev says none): nothing opened there",
          r["did"] != "opened_there" and not any("amazon.com" in u for u in tm.OPENED),
          (r["did"], tm.OPENED))


def t_named_generic_apps():
    fresh()
    r, _ = place_turn("look up cheap flights in Safari", "app:Safari", query="cheap flights")
    check("a browser she names: that browser, on a search",
          ["open", "-a", "Safari", "https://www.google.com/search?q=cheap+flights"] in tm.LAUNCHED
          and r["say"] == "Safari is open on cheap flights.", (tm.LAUNCHED, r["say"]))

    fresh()
    r, _ = place_turn("open my budget in Pages", "app:Pages", intent="open_app",
                      query="my budget")
    check("an app with no search it can be handed: opened, and she is told what next",
          ["open", "-a", "Pages"] in tm.LAUNCHED
          and r["say"] == "Pages is open. Search for my budget there.", (tm.LAUNCHED, r["say"]))

    fresh()
    r, j = place_turn("open it in Photos", "app:Photos", intent="open_app")
    check("'open it in Photos': opened like any app, with its undo, and no second call",
          r["did"] == "opened_app" and tm.APPS == ["Photos"] and j.calls == 1,
          (r["did"], tm.APPS, j.calls))

    fresh()
    r, j = place_turn("open Telegram", "telegram", intent="open_app")
    check("'open Telegram': the program, no 'which program' question",
          r["did"] == "opened_app" and tm.APPS == ["Telegram"] and j.calls == 1,
          (r["did"], tm.APPS, j.calls))

    fresh()
    r, _ = place_turn("open Signal", "signal", intent="open_app")
    check("named, not installed, no website: says so plainly",
          r["did"] == "not_installed" and r["say"] == "Signal is not on this computer.",
          (r["did"], r["say"]))

    fresh()
    r, _ = place_turn("close Telegram", "telegram", intent="close_app")
    check("'close Telegram' is still closing, not opening",
          r["did"] != "opened_there" and not tm.APPS and not tm.LAUNCHED, (r["did"], tm.APPS))



# ================================================================== the owner's 1.1.0 session
def _bb(tid, name, album, cid):
    return _song(tid, name, "Bad Bunny", album, cid)


BAD_BUNNY = {"results": [
    _bb(901, "MIA (feat. Drake)", "MIA (feat. Drake) - Single", 900),
    _bb(902, "MÍA (feat. Drake)", "X 100PRE", 910),
    _bb(903, "EL MUNDO ES MÍO", "EL ÚLTIMO TOUR DEL MUNDO", 920),
    _bb(904, "DtMF", "DeBÍ TiRAR MáS FOToS", 930),
]}


def t_song_keys_and_precheck():
    k = am.song_key
    check("key: accents and (feat.) do not make a new song",
          am.same_key(k("MIA (feat. Drake)", "Bad Bunny"), k("MÍA (feat. Drake)", "Bad Bunny")))
    check("key: a YouTube title is the same song as the catalog's",
          am.same_key(k("Bad Bunny - MÍA ft. Drake (Official Video)"),
                      k("MIA (feat. Drake)", "Bad Bunny")), k("Bad Bunny - MÍA ft. Drake (Official Video)"))
    check("key: '- Remastered' and '[Official ...]' dropped",
          am.same_key(k("Hips Don't Lie - Remastered 2009"), k("Hips Don't Lie [Official Audio]", "Shakira")))
    check("key: featured and joint artists match the main artist",
          am.same_key(k("Hips Don't Lie (feat. Wyclef Jean)", "Shakira & Wyclef Jean"),
                      k("Hips Don't Lie", "Shakira")))
    check("key: a different song is different",
          not am.same_key(k("EL MUNDO ES MÍO", "Bad Bunny"), k("MIA", "Bad Bunny")))
    check("key: the same title by someone else is different",
          not am.same_key(k("1969", "The Stooges"), k("1969", "Boards of Canada")))
    for s, on, want in [("please change to our latest world cup song", False, True),
                        ("play the song from Titanic", False, True),
                        ("play Shakira's newest song", False, True),
                        ("put on the one they play at weddings", False, True),
                        ("תשימי את השיר החדש של עומר אדם", False, True),
                        ("her latest one", True, True),
                        ("play Waka Waka", False, False), ("play Bad Bunny", False, False),
                        ("another one", True, False), ("another song please", True, False),
                        ("put 1969 on apple music", False, False),
                        ("now switch to shakira", True, False),
                        ("what's the latest news", False, False)]:
        check(f"describes a song: {s!r} -> {want}", am.describes_a_song(s, on) == want)


def t_another_one_never_repeats():
    """21:02:20 MIA (feat. Drake), 21:02:29 "another one": MÍA (feat. Drake)."""
    fresh()
    STATE["more"] = {"bad bunny": BAD_BUNNY}
    STATE["library"] = []
    r, _ = turn("please play bad bunny on apple music", player="apple_music",
                subject="bad bunny", pick=by("MIA"))
    check("first: MIA", r["did"] == "opened_in_app" and r["say"].startswith("MIA (feat. Drake)"),
          r["say"])
    seen = ["mia"]
    for n in range(3):
        # The picker takes the first choice offered: the worst case for a repeat.
        r, j = turn("another one", extra={"rejects_last": 0.9, "refers_back": 0.87},
                    pick=lambda opts: "0")
        if n < 2:
            title = router.MEM.last_played.get("title", "")
            key = am.song_key(title)[0]
            check(f"another one #{n + 1}: a new song, not a version of one played ({title})",
                  r["did"] == "opened_in_app" and key not in seen, f"{r['did']} {r['say']}")
            labels = " ".join(j.asked[-1]["pick"]["criteria"].values())
            check(f"  neither MIA nor MÍA was offered again",
                  "MIA (feat" not in labels and "MÍA (feat" not in labels, labels[:300])
            seen.append(key)
        else:
            check("everything found has been played: says so, not 'not found'",
                  r["did"] == "exhausted" and "not find" not in r["say"], f"{r['did']} {r['say']}")

    # YouTube: "Bad Bunny - MÍA (Lyrics)" after "Bad Bunny - MIA ft. Drake".
    fresh()
    vids = [{"id": "y1", "title": "Bad Bunny - MIA ft. Drake (Official Video)", "channel": "Bad Bunny",
             "length": "3:30", "views": "1B views"},
            {"id": "y2", "title": "Bad Bunny - MÍA (feat. Drake) [Lyrics]", "channel": "Lyrics Hub",
             "length": "3:31", "views": "50M views"},
            {"id": "y3", "title": "Bad Bunny - DtMF (Visualizer)", "channel": "Bad Bunny",
             "length": "3:57", "views": "300M views"}]
    router.yt.search = lambda q, n=18: [dict(v) for v in vids]
    turn("play bad bunny", subject="bad bunny")
    r, _ = turn("another one", extra={"rejects_last": 0.9})
    check("YouTube: another one skips the lyrics copy of the same song",
          r["did"] == "playing" and router.MEM.last_played["id"] == "y3",
          f"{r['did']} {router.MEM.last_played and router.MEM.last_played.get('title')}")


class FakeLLM:
    available = True
    last_ms = 0.0
    calls, busy_ms = 0, 0.0

    def __init__(self, got):
        self.got, self.asked = got, []

    def identify_song(self, request, language="english", context="", singer_on="", gender=""):
        self.asked.append({"request": request, "singer_on": singer_on, "context": context})
        return self.got

    def answer(self, *a, **k):
        return None


def _with_llm(got):
    llm = FakeLLM(got)
    router.LLM_CLIENT = llm
    return llm


REAL_LLM = router.LLM_CLIENT
SHAKIRA = {"results": [_song(301, "Hips Don't Lie (feat. Wyclef Jean)", "Shakira", "Oral Fixation", 300),
                       _song(302, "Whenever, Wherever", "Shakira", "Laundry Service", 310)]}
WAKA = {"results": [_song(371784883, "Waka Waka (This Time for Africa) [The Official 2010 FIFA "
                                     "World Cup (TM) Song]", "Shakira", "Listen Up!", 371784865)]}


def t_described_song():
    """21:03:32 "please change to our latest world cup song" over Shakira: the search
    stayed "shakira" and Whenever, Wherever was offered."""
    try:
        fresh()
        STATE["more"] = {"waka": WAKA, "shakira": SHAKIRA}
        turn("now switch to shakira on apple music", player="apple_music", subject="shakira",
             pick=by("Hips"))
        llm = _with_llm({"title": "Waka Waka", "artist": "Shakira",
                         "sentence": "Shakira's latest World Cup song is Waka Waka."})
        FETCHED.clear()
        r, j = turn("please change to our latest world cup song",
                    extra={"describes_song": 0.9, "rejects_last": 0.9}, pick=by("Waka"))
        check("the describes-song question rode in understand()", "describes_song" in j.asked[0])
        check("identified, then played: says which song first",
              r["did"] == "opened_in_app" and r["say"].startswith(
                  "Shakira's latest World Cup song is Waka Waka. Waka Waka"), r["say"])
        check("the singer she was listening to went to the identification",
              llm.asked and llm.asked[0]["singer_on"] == "Shakira", llm.asked)
        check("the catalog was searched for the title, never replayed 'shakira'",
              any("Waka" in u for u in FETCHED) and not any("term=shakira" in u for u in FETCHED),
              FETCHED)
        # The sources' songs held no Waka Waka (the pick said none), so this went on to
        # the one Gemini call: understand + the pick among found songs + the play pick.
        check("sources gave nothing usable: three Jev round trips, then the model",
              j.calls == 3 and r["detail"]["identified"]["source"] == "model", j.calls)
        check("the trace says what was identified",
              r["detail"]["identified"]["title"] == "Waka Waka", r["detail"])

        # Named by the model, but the real search does not have it: honest.
        fresh()
        _with_llm({"title": "Imaginary Anthem", "artist": "Shakira", "sentence": ""})
        r, j = turn("play shakira's newest song on apple music", player="apple_music",
                    extra={"describes_song": 0.9}, pick=by("Waka"))
        check("not in the catalog: 'I think you mean ..., but I could not find it'",
              r["did"] == "not_found" and r["say"] ==
              "I think you mean Imaginary Anthem by Shakira, but I could not find it in Apple Music.",
              r["say"])
        check("  and nothing opened", not opened_songs(), OPENS)

        # The model does not know: asks, never guesses.
        fresh()
        _with_llm(None)
        r, _ = turn("play the song from that film", extra={"describes_song": 0.9})
        check("unknown: asks what it is called", r["did"] == "asked_back" and r.get("asked_back")
              and "What is it called" in r["say"], r["say"])

        # YouTube: the song from Titanic.
        fresh()
        _with_llm({"title": "My Heart Will Go On", "artist": "Celine Dion",
                   "sentence": "The song from Titanic is My Heart Will Go On."})
        vids = [{"id": "t1", "title": "Céline Dion - My Heart Will Go On (Official HD Video)",
                 "channel": "CelineDionVEVO", "length": "4:41", "views": "1B views"}]
        router.yt.search = lambda q, n=18: [dict(v) for v in vids]
        r, _ = turn("play the song from Titanic", extra={"describes_song": 0.9})
        check("YouTube: identified, checked in the search, played, and said which",
              r["did"] == "playing" and r["say"].startswith(
                  "The song from Titanic is My Heart Will Go On."), f"{r['did']} {r['say']}")

        # The model named it but wrote no sentence: a plain one says which.
        fresh()
        _with_llm({"title": "My Heart Will Go On", "artist": "Celine Dion", "sentence": ""})
        router.yt.search = lambda q, n=18: [dict(v) for v in vids]
        r, _ = turn("play the song from Titanic", extra={"describes_song": 0.9})
        check("no sentence from the model: 'That is ... by ...' first",
              r["say"].startswith("That is My Heart Will Go On by Celine Dion."), r["say"])

        # What the model sends back is checked before anything is said or searched.
        from savta.llm import LLM
        l = LLM()
        for out, want in [
                ('```json\n{"title": "Waka Waka", "artist": "Shakira", "sentence": '
                 '"Her World Cup song is Waka Waka \u2014 from 2010."}\n```',
                 {"title": "Waka Waka", "artist": "Shakira",
                  "sentence": "Her World Cup song is Waka Waka, from 2010."}),
                ('{"title": "Waka Waka", "artist": "Shakira", "sentence": "It is her big hit."}',
                 {"title": "Waka Waka", "artist": "Shakira", "sentence": ""}),
                ('{"title": ""}', None), ("I am not sure.", None),
                ('{"title": "' + "x" * 200 + '", "artist": "y"}', None)]:
            l.text = lambda *a, _o=out, **k: _o
            check(f"identify_song parses {out[:40]!r}", l.identify_song("q") == want,
                  l.identify_song("q"))

        # Ordinary requests are sent exactly as before.
        fresh()
        _with_llm(None)
        for s in ("play Waka Waka", "play Bad Bunny"):
            _, j = turn(s, subject=s.split(" ", 1)[1])
            check(f"{s!r}: the question is not asked", "describes_song" not in j.asked[0])
        _, j = turn("another one", extra={"rejects_last": 0.9})
        check("'another one': the question is not asked", "describes_song" not in j.asked[0])
        # A high score on a sentence that is not a music request does nothing.
        fresh()
        llm = _with_llm({"title": "X", "artist": "Y", "sentence": ""})
        r, _ = turn("what's the latest world cup song", intent="look_up",
                    extra={"describes_song": 0.9, "needs_world_knowledge": 0.9})
        check("a question about a song is answered, not played", not llm.asked, r["did"])
    finally:
        router.LLM_CLIENT = REAL_LLM


# ---------------------------------------------------------------- a fake Music window
def _el(role, children=(), **attrs):
    return {"AXRole": role, "AXChildren": list(children), **attrs}


class FakeAX:
    """Music's window as Accessibility shows it (read off the owner's Mac, 2026-09-27):
    the album header's Play button, and the song's row with its own play toggle."""

    def __init__(self, track_id=371784883, album=371784865, row=True, sheet=False,
                 starts=True, title="Waka Waka"):
        self.pressed = []
        header = _el("AXUnknown", [_el("AXButton", AXTitle="Play", AXDescription="Play")],
                     AXIdentifier=f"Music.shelfItem.AlbumDetailHeaderLockup[id=album-detail-header-{album}")
        rows = [_el("AXGroup", [_el("AXStaticText", AXValue="1"),
                                _el("AXButton", AXTitle="R. Kelly")],
                    AXIdentifier=f"Music.shelfItem.AlbumTrackLockup[id=track-lockup-{album}-371784873,p")]
        if row:
            self.toggle = _el("AXCheckBox", AXSubrole="AXToggle", AXDescription="play", AXValue=0)
            rows.append(_el("AXGroup", [_el("AXButton", AXDescription="Favourite"), self.toggle],
                            AXIdentifier=f"Music.shelfItem.AlbumTrackLockup[id=track-lockup-{album}-{track_id},p"))
        win = _el("AXWindow", [_el("AXSplitGroup", [header] + rows)]
                  + ([_el("AXSheet")] if sheet else []), AXTitle="Music")
        self.root = {"AXWindows": [win]}
        self.starts, self.title = starts, title

    def get(self, el, name, _):
        v = el.get(name) if isinstance(el, dict) else None
        return (0, v) if v is not None else (-25212, None)

    def press(self, el, action):
        self.pressed.append(el)
        if self.starts:
            STATE["now"] = ("playing", self.title)
        return 0

    def api(self):
        return {"app": lambda pid: self.root, "get": self.get, "press": self.press,
                "trusted": lambda: True, "timeout": None}


def t_apple_music_playback():
    # Catalog: the song's own play toggle is pressed, and Music is read back.
    fresh()
    STATE["more"] = {"waka": WAKA}
    fake = FakeAX(title="Waka Waka (This Time for Africa) [The Official 2010 FIFA World Cup (TM) Song]")
    am._ax, am._music_pid = fake.api, lambda: 4242
    r, _ = turn("play waka waka on apple music", player="apple_music", subject="waka waka",
                pick=by("Waka"))
    check("catalog song: its row's play toggle pressed, and now it is playing",
          r["did"] == "playing" and fake.pressed == [fake.toggle]
          and r["say"].startswith("Playing Waka Waka"), f"{r['did']} {r['say']} {r['detail'].get('press')}")
    check("  the album's own Play button was never pressed",
          all(p.get("AXDescription") == "play" for p in fake.pressed))
    check("  not left as 'open only': pause/resume work on it",
          not router.MEM.last_played.get("open_only"))

    # Pressed, but Music does not start it: never "playing".
    fresh()
    STATE["more"] = {"waka": WAKA}
    fake = FakeAX(starts=False)
    am._ax, am._music_pid = fake.api, lambda: 4242
    r, _ = turn("play waka waka on apple music", player="apple_music", subject="waka waka",
                pick=by("Waka"))
    check("pressed but not playing: 'press play', honestly",
          r["did"] == "opened_in_app" and "Press play" in r["say"]
          and r["detail"]["press"]["why"] == "not_playing_after_press", r["detail"].get("press"))

    # No such row on the page (another layout, another language): nothing pressed.
    fresh()
    STATE["more"] = {"waka": WAKA}
    fake = FakeAX(row=False)
    am._ax, am._music_pid = fake.api, lambda: 4242
    real_find = am.press_song_play.__defaults__
    am.press_song_play.__defaults__ = (0.3, 0.3)
    r, _ = turn("play waka waka on apple music", player="apple_music", subject="waka waka",
                pick=by("Waka"))
    am.press_song_play.__defaults__ = real_find
    check("no row for the song: nothing pressed, 'press play'",
          r["did"] == "opened_in_app" and not fake.pressed, r["detail"].get("press"))

    # Music cannot be read: never press blind.
    fresh()
    STATE["more"] = {"waka": WAKA}
    STATE["now"] = ("", "")
    fake = FakeAX()
    am._ax, am._music_pid = fake.api, lambda: 4242
    r, _ = turn("play waka waka on apple music", player="apple_music", subject="waka waka",
                pick=by("Waka"))
    check("Music unreadable: nothing pressed", not fake.pressed and r["did"] == "opened_in_app",
          r["detail"].get("press"))

    # Library copy will not start because a dialog is up in Music: said, not dismissed.
    fresh()
    STATE.update(library=["stooges"], playing=False)
    fake = FakeAX(sheet=True)
    am._ax, am._music_pid = fake.api, lambda: 4242
    r, _ = turn("put 1969 on apple music", player="apple_music", subject="1969",
                pick=by("The Stooges", lib=True))
    check("a dialog in Music: named to her, nothing pressed or opened",
          r["did"] == "not_started" and r["say"].startswith("Music has a window open")
          and not fake.pressed and not opened_songs(), f"{r['did']} {r['say']} {OPENS}")

    # Library copy will not start, no dialog: its catalog twin's page, and its button.
    fresh()
    STATE.update(library=["stooges"], playing=False)
    fake = FakeAX(track_id=89317104, album=89319049, title="1969")
    am._ax, am._music_pid = fake.api, lambda: 4242
    r, _ = turn("put 1969 on apple music", player="apple_music", subject="1969",
                pick=by("The Stooges", lib=True))
    check("her copy would not start: the same song's catalog page, pressed, playing",
          r["did"] == "playing" and OPENS and fake.pressed, f"{r['did']} {OPENS}")
    check("the library play waits longer and asks once more when loaded but paused",
          any("repeat 12 times" in s_ and "if persistent ID of current track is" in s_
              for s_ in SCRIPTS))


# What the sources held on 2026-09-27 (iTunes Search in the Israeli store, English
# Wikipedia), trimmed. The model alone said "Waka Waka" for the latest World Cup song.
SHAK = 889327
WC_ROWS = {"results": [
    _song(701, "La La La (Brazil 2014) [feat. Carlinhos Brown]", "Shakira",
          "The 2014 FIFA World Cup™ Official Album", 700, date="2014-03-21", aid=SHAK),
    _song(702, "Waka Waka (This Time for Africa) [The Official 2010 FIFA World Cup (TM) Song]",
          "Shakira", "Listen Up! The Official 2010 FIFA World Cup Album", 710, date="2010-05-07", aid=SHAK),
    _song(703, "Waka Waka (This Time for Africa)", "Shakira", "Hits Internacionais Virais 2023",
          720, date="2023-01-11", aid=SHAK),
    _song(704, "Dai Dai", "Shakira & Burna Boy", "Official FIFA World Cup 2026™ Album", 730,
          date="2026-05-14", aid=SHAK),
    _song(705, "Whenever, Wherever", "Shakira", "Laundry Service", 740, date="2001-08-27", aid=SHAK),
    _song(706, "World Cup (Champions)", "IShowSpeed", "World Cup (Champions) - Single", 750,
          date="2026-06-01", aid=5),
]}
SHAK_ROWS = {"results": [r for r in WC_ROWS["results"] if r.get("artistId") == SHAK]}
SHAK_RECENT = {"results": [
    {"wrapperType": "artist", "artistId": SHAK},
    _song(801, "AGUA", "Shakira", "AGUA - Single", 800, date="2026-09-17", aid=SHAK),
    _song(802, "Dai Dai (Clean Bandit Remix)", "Shakira & Burna Boy", "Dai Dai - EP", 810,
          date="2026-06-11", aid=SHAK),
    _song(803, "Hips Don't Lie (Edit) [Mixed]", "Shakira", "SYBER: 019 (DJ Mix)", 820,
          date="2026-08-08", aid=SHAK),
]}
WC_WIKI = {"query": {"pages": {
    "1": {"pageid": 1, "index": 1, "title": "Shakira",
          "extract": "Shakira Isabel Mebarak Ripoll (born 2 February 1977) is a Colombian singer-songwriter."},
    "2": {"pageid": 2, "index": 2, "title": "Waka Waka (This Time for Africa)",
          "extract": "\"Waka Waka (This Time for Africa)\" is a song by Colombian singer Shakira, the official song of the 2010 FIFA World Cup."},
    "3": {"pageid": 3, "index": 3, "title": "Dai Dai",
          "extract": "\"Dai Dai\" is a song by Colombian singer Shakira and Nigerian singer Burna Boy. It was released on 15 May 2026, as the official song of the 2026 FIFA World Cup."},
    "4": {"pageid": 4, "index": 4, "title": "World Cup (Champions)",
          "extract": "\"World Cup (Champions)\" is a song by American influencer IShowSpeed, released on June 1, 2026."},
}}}
TITANIC_ROWS = {"results": [
    _song(901, "My Heart Will Go On (Love Theme from \"Titanic\")", "James Horner & Céline Dion",
          "Titanic (Music from the Motion Picture)", 900, date="1997-11-18", aid=11),
    _song(902, "Titanic", "Robin Schulz", "Sugar", 910, date="2015-09-25", aid=12),
]}
TITANIC_WIKI = {"query": {"pages": {
    "1": {"pageid": 11, "index": 1, "title": "Titanic (Falco song)",
          "extract": "\"Titanic\" is a song by Falco from his 1992 studio album Nachtflug."},
    "2": {"pageid": 12, "index": 2, "title": "My Heart Will Go On",
          "extract": "\"My Heart Will Go On\" is a song recorded by the Canadian singer Celine Dion as the theme for the 1997 film Titanic."},
}}}


def _world_cup_fits(instr):
    # Jev's judgement, scripted: a World Cup song by Shakira fits; another singer's does not.
    label = instr.split(": this song matches", 1)[0]
    return 0.9 if ("World Cup" in label and ", by Shakira" in label) else 0.05


def t_described_song_from_sources():
    """Search first: the sources' candidates, Jev's pick, the sources' dates."""
    try:
        fresh()
        am.country = lambda: "il"
        STATE["more"] = {"world cup": WC_ROWS, "shakira": SHAK_ROWS}
        STATE.update(wiki=WC_WIKI, lookup=SHAK_RECENT)
        turn("now switch to shakira on apple music", player="apple_music", subject="shakira",
             pick=lambda o: "0")
        llm = _with_llm({"title": "Waka Waka", "artist": "Shakira", "sentence": ""})
        r, j = turn("please change to our latest world cup song",
                    extra={"describes_song": 0.9, "rejects_last": 0.9},
                    pick=lambda o: "0", fits=_world_cup_fits)
        ident = (r.get("detail") or {}).get("identified") or {}
        check("owner's sentence: Dai Dai, the newest World Cup song by the sources' dates",
              r["did"] == "opened_in_app" and r["say"].startswith("That is Dai Dai, from 2026.")
              and ident.get("source") == "search", f"{r['did']} {r['say']} {ident.get('search')}")
        check("  no Gemini call", not llm.asked, llm.asked)
        check("  the World Cup songs Jev said fit, with their dates",
              ident["search"]["fitting"] and all("World Cup" in f or "Dai Dai" in f or "La La La" in f
                                                  for f in ident["search"]["fitting"]),
              ident["search"].get("fitting"))
        check("  Waka Waka dated 2010, not its 2023 compilation",
              any(f.startswith("Waka Waka") and "2010-05-07" in f for f in ident["search"]["fitting"]),
              ident["search"]["fitting"])
        check("  two Jev round trips: understand + the pick among what was found",
              j.calls == 2, j.calls)
        check("  Music opened on Dai Dai itself", OPENS and "?i=704" in OPENS[-1][-1], OPENS)

        # "Shakira's newest song": the lookup's release dates, not popularity.
        fresh()
        STATE["more"] = {"shakira": SHAK_ROWS}
        STATE.update(wiki={"query": {"pages": {}}}, lookup=SHAK_RECENT)
        llm = _with_llm(None)
        r, j = turn("play Shakira's newest song on apple music", player="apple_music",
                    extra={"describes_song": 0.9}, pick=lambda o: "0",
                    fits=lambda instr: 0.9 if ", by Shakira" in instr else 0.1)
        check("newest song: AGUA (2026-09-17), from Apple's dates; DJ mixes left out",
              r["say"].startswith("That is AGUA, from 2026.") and not llm.asked,
              f"{r['say']} {r['detail'].get('identified', {}).get('search')}")
        check("  the artist's newest songs were looked up",
              any("/lookup?" in u and "sort=recent" in u for u in FETCHED), FETCHED)

        # "the song from Titanic": a plain pick among real songs, played on YouTube.
        fresh()
        STATE["more"] = {"titanic": TITANIC_ROWS}
        STATE["wiki"] = TITANIC_WIKI
        llm = _with_llm(None)
        vids = [{"id": "t1", "title": "Céline Dion - My Heart Will Go On (Official HD Video)",
                 "channel": "CelineDionVEVO", "length": "4:41", "views": "1B views"}]
        router.yt.search = lambda q, n=18: [dict(v) for v in vids]
        r, j = turn("play the song from Titanic", extra={"describes_song": 0.9},
                    pick=lambda o: next(k for k, v in o.items() if "My Heart" in (v or "")))
        check("Titanic: My Heart Will Go On from the sources, played, said first, no Gemini",
              r["did"] == "playing" and r["say"].startswith("That is My Heart Will Go On")
              and "1997" in r["say"] and not llm.asked, f"{r['did']} {r['say']}")
        check("  Wikipedia's song article came in as a candidate",
              r["detail"]["identified"]["search"]["wiki_songs"] == 2, r["detail"]["identified"]["search"])

        # Nothing usable in the sources: the one Gemini call, validated as before.
        fresh()
        STATE["more"] = {"waka": WAKA}
        llm = _with_llm({"title": "Waka Waka", "artist": "Shakira", "sentence": ""})
        r, _ = turn("play the one they play at weddings on apple music", player="apple_music",
                    extra={"describes_song": 0.9}, pick=by("Waka"))
        check("sources empty: Gemini, once, still checked in the catalog",
              len(llm.asked) == 1 and r["detail"]["identified"]["source"] == "model"
              and r["did"] == "opened_in_app", f"{r['did']} {r['say']}")
    finally:
        router.LLM_CLIENT = REAL_LLM


# ==================================================== 2026-09-28 owner session
# fix/music2: "the song on my screen", misheard titles matched by sound within a
# known artist, a year refining a described-song search, rejecting plays another
# instead of "not found", a named app winning mid-rejection, and turn speed.

def t_screen_song():
    """"now play this one", "play the song i see on my screen", "no look at my
    screen where you will see the song i wanna play": the owner's session searched
    the literal words ("see screen") and played the wrong thing entirely. All three
    happen mid Apple-Music session, as they did live (a Shakira song already
    playing), so the app carries over exactly as it does for any other follow-up."""
    real_ctx = router._screen_context
    try:
        fresh()
        STATE["more"] = {"shakira": SHAK_ROWS, "waka": SHAK_ROWS, "dai dai": SHAK_ROWS}
        turn("play shakira on apple music", player="apple_music", subject="shakira",
            pick=by("Whenever"))

        # Music itself is in front: its own current-track pointer names the song,
        # read the same way "playing" already is (music._now_playing), never her
        # sentence's own words.
        FETCHED.clear()
        STATE["now"] = ("stopped", "Waka Waka (This Time for Africa)")
        router._screen_context = lambda max_chars=6000: {
            "permissions": {"accessibility": True, "screen_recording": False},
            "frontmost": {"app": "Music", "pid": 9, "bundle_id": "com.apple.Music", "window": "Music"},
            "selected": "", "focused": {"role": "", "value": "", "secure": False},
            "visible": {"text": "", "truncated": False}, "page": None, "has_image": False}
        r, j = turn("now play this one", extra={"screen_song": 0.9}, pick=by("Waka Waka"))
        check("Music's own current track is read, not her sentence's words",
              r["did"] == "opened_in_app" and "Waka Waka" in (r["say"] or ""), r["say"])
        check("never searched the literal words 'this one'",
              not any("term=this" in u.lower() or "term=one" in u.lower() for u in FETCHED), FETCHED)

        # A browser tab's title carries the song, the way YouTube and Spotify's web
        # player do; the site's own name and an "(Official Video)" tag are stripped.
        FETCHED.clear()
        router._screen_context = lambda max_chars=6000: {
            "permissions": {"accessibility": True, "screen_recording": False},
            "frontmost": {"app": "Google Chrome", "pid": 1, "bundle_id": "com.google.Chrome",
                          "window": "Dai Dai"},
            "selected": "", "focused": {"role": "", "value": "", "secure": False},
            "visible": {"text": "", "truncated": False},
            "page": {"url": "https://youtube.com/watch?v=x",
                     "title": "Dai Dai (Official Video) - YouTube"},
            "has_image": False}
        r, j = turn("play the song i see on my screen", extra={"screen_song": 0.9},
                    pick=by("Dai Dai"))
        check("the tab's own title names the song ('Dai Dai'), not 'see screen'",
              r["did"] == "opened_in_app" and r["say"].startswith("Dai Dai"), r["say"])
        check("never searched 'see screen'",
              not any("term=see" in u.lower() or "screen" in u.split("term=")[-1].lower()
                      for u in FETCHED), FETCHED)

        # Nothing on screen looks like a song at all: she is asked, never told a
        # literal search of her own words ("see screen") failed.
        FETCHED.clear()
        router._screen_context = lambda max_chars=6000: {
            "permissions": {"accessibility": True, "screen_recording": False},
            "frontmost": {"app": "Finder", "pid": 2, "bundle_id": "com.apple.finder",
                         "window": "Desktop"},
            "selected": "", "focused": {"role": "", "value": "", "secure": False},
            "visible": {"text": "", "truncated": False}, "page": None, "has_image": False}
        r, j = turn("no look at my screen where you will see the song i wanna play",
                    extra={"screen_song": 0.9})
        check("nothing on screen: asks, never 'could not find see screen'",
              r["did"] == "asked_back" and "see screen" not in (r["say"] or "").lower()
              and not FETCHED, f"{r['did']} {r['say']} {FETCHED}")
    finally:
        router._screen_context = real_ctx


# A small, self-contained artist catalog (her real library search finds nothing so
# the exact-search path always fails first, forcing the sound-match fallback).
BB_ID = 42042
BAD_BUNNY_FULL = {"results": [
    _song(6001, "NUEVAYoL", "Bad Bunny", "Debí Tirar Más Fotos", 6100, date="2025-01-05", aid=BB_ID),
    _song(6002, "MIA (feat. Drake)", "Bad Bunny", "X 100PRE", 6200, date="2018-12-24", aid=BB_ID),
    _song(6003, "DtMF", "Bad Bunny", "Debí Tirar Más Fotos", 6300, date="2025-01-05", aid=BB_ID),
    _song(6004, "EL CLúB", "Bad Bunny", "Debí Tirar Más Fotos", 6400, date="2025-01-05", aid=BB_ID),
]}


def t_sound_match_within_artist():
    """"nueva yella" (= NUEVAYoL) right after a Bad Bunny song, and "die die"
    (= Dai Dai) right after Shakira played through the rejection path: Apple's own
    search finds nothing for either, and the owner's session reused a stale query
    ("could not find Waka Waka ... Shakira") instead of her actual new words."""
    fresh()
    STATE["more"] = {"bad bunny": BAD_BUNNY_FULL}
    STATE["lookup"] = BAD_BUNNY_FULL
    STATE["library"] = []
    turn("play bad bunny on apple music", player="apple_music", subject="bad bunny", pick=by("MIA"))
    check("setup: MIA is playing", router.MEM.last_played["title"].startswith("MIA"))

    r, j = turn("nueva yella", subject="nueva yella")
    check("a fresh, garbled title is matched by sound within the known artist",
          r["did"] == "opened_in_app" and r["say"].startswith("NUEVAYoL"),
          f"{r['did']} {r['say']}")
    check("  no extra Jev call needed: one clear match, decided in code",
          j.calls == 1, j.calls)

    # "die die" for Shakira's "Dai Dai", through the rejection path: a real title
    # attempt, not a bare "no", must never replay the stale previous search.
    fresh()
    STATE["more"] = {"shakira": SHAK_ROWS, "waka": SHAK_ROWS}
    STATE["lookup"] = SHAK_ROWS
    STATE["library"] = []
    turn("play shakira on apple music", player="apple_music", subject="shakira",
         pick=by("Whenever"))
    r, j = turn("die die", extra={"rejects_last": 0.9})
    check("'die die' plays Dai Dai, not a stale replay of the Shakira search",
          r["did"] in ("opened_in_app", "playing") and r["say"].startswith("Dai Dai"),
          f"{r['did']} {r['say']} {r['detail'].get('query')}")
    check("  the previous turn's stale query was never reused as the search",
          r["detail"].get("query") != "shakira", r["detail"])

    # An artist named right in the sentence, with no session context at all
    # ("change to where all by bad bunny"): the name after "by" is enough on its own.
    fresh()
    STATE["more"] = {"bad bunny": BAD_BUNNY_FULL}
    STATE["lookup"] = BAD_BUNNY_FULL
    STATE["library"] = []
    r, j = turn("change to nueva yol by bad bunny on apple music", player="apple_music",
                subject="nueva yol")
    check("an artist named in this very sentence is enough, no prior context needed",
          r["did"] == "opened_in_app" and r["say"].startswith("NUEVAYoL"),
          f"{r['did']} {r['say']}")


def t_named_app_switch_on_reject():
    """"ok so on youtube" right after a song in Apple Music: the named app wins,
    switching there with the same artist, rather than yet another Apple Music pick
    (the owner's session kept replaying Bad Bunny in Apple Music instead)."""
    fresh()
    STATE["more"] = {"bad bunny": BAD_BUNNY_FULL}
    STATE["lookup"] = BAD_BUNNY_FULL
    STATE["library"] = []
    turn("play bad bunny on apple music", player="apple_music", subject="bad bunny", pick=by("MIA"))
    check("setup: playing in Apple Music", router.MEM.last_played["player"] == "apple_music")

    vids = [{"id": "y1", "title": "Bad Bunny - NUEVAYoL (Official Video)", "channel": "Bad Bunny",
            "length": "3:14", "views": "50M views"}]
    queries: list[str] = []
    router.yt.search = lambda q, n=18: (queries.append(q), [dict(v) for v in vids])[1]
    r, j = turn("ok so on youtube", player="youtube", extra={"rejects_last": 0.9})
    check("switched to YouTube with the same subject, not another Apple Music pick",
          r["did"] == "playing" and router.MEM.last_played["player"] == "youtube",
          f"{r['did']} {router.MEM.last_played}")
    check("  searched YouTube for what she was already listening to (bad bunny)",
          any("bad bunny" in (q or "").lower() for q in queries), queries)
    router.yt.search = REAL_YT_SEARCH


def t_reject_plays_another_never_not_found():
    """"not this one" / "look well for sure you should find": rejecting a song must
    hand her another real one by the artist, never "not found" - the owner's
    session said "I could not find bad bunny" for an artist that plainly was found,
    just not yet exhausted."""
    fresh()
    STATE["more"] = {"bad bunny": BAD_BUNNY_FULL}
    STATE["lookup"] = BAD_BUNNY_FULL
    STATE["library"] = []
    turn("play bad bunny on apple music", player="apple_music", subject="bad bunny", pick=by("MIA"))
    # Jev's own pick call scores every option too low to trust (as it did live);
    # code must still hand her a fresh, real song rather than giving up.
    r, j = turn("not this one", extra={"rejects_last": 0.9}, pick=lambda o: "__none__")
    check("a rejection is answered with another real song, never 'not found'",
          r["did"] != "not_found", f"{r['did']} {r['say']}")
    check("  a different song than the one just rejected",
          router.MEM.last_played["title"] != "MIA (feat. Drake)", router.MEM.last_played)
    check("  the fallback is named honestly in the trace",
          r["detail"].get("fallback_pick") is True, r["detail"])


def t_year_refines_described_song_search():
    """"ok now the one from 2026" right after a (wrongly) picked 2010 song: a year
    refines the SAME described-song search rather than a blind new one. The owner's
    session searched "Shakira ok 2026" from nothing and a model call guessed wrong,
    when the real dated candidates - Dai Dai included - had already been fetched a
    moment before, for "our latest World Cup song"."""
    fresh()
    year_rows = {"results": [
        _song(8801, "Waka Waka (This Time for Africa)", "Shakira", "Sale el Sol", 8800,
              date="2010-04-26", aid=8888),
        _song(8802, "Dai Dai", "Shakira", "Dai Dai - Single", 8810, date="2026-05-14", aid=8888),
    ]}
    year_wiki = {"query": {"pages": {
        "1": {"pageid": 1, "index": 1, "title": "Waka Waka (This Time for Africa)",
              "extract": "\"Waka Waka (This Time for Africa)\" is a song by Colombian singer "
                        "Shakira, the official song of the 2010 FIFA World Cup."},
        "2": {"pageid": 2, "index": 2, "title": "Dai Dai",
              "extract": "\"Dai Dai\" is a song by Colombian singer Shakira, the official song "
                        "of the 2026 FIFA World Cup."},
    }}}
    STATE["more"] = {"shakira": year_rows, "world cup": year_rows}
    STATE.update(wiki=year_wiki, lookup={"results": []})
    turn("now switch to shakira on apple music", player="apple_music", subject="shakira",
         pick=lambda o: "0")

    def prefer_waka(instr):
        return 0.9 if "Waka Waka" in instr.split(": this", 1)[0] else 0.05
    r, j = turn("please change to our latest world cup song",
                extra={"describes_song": 0.9, "rejects_last": 0.9},
                pick=lambda o: "0", fits=prefer_waka)
    check("setup: the (live-Jev) pick is the 2010 song, not Dai Dai",
          "Waka Waka" in r["say"] and "2010" in r["say"], r["say"])
    fetched_before = len(FETCHED)

    r2, j2 = turn("ok now the one from 2026", extra={"describes_song": 0.9})
    check("a year on its own resolves to Dai Dai, from the search already done",
          r2["say"].startswith("That is Dai Dai, from 2026.")
          and r2["detail"]["identified"].get("source") == "cache", f"{r2['did']} {r2['say']}")
    check("  no blind new described-song search: no Wikipedia call, no fresh 'ok "
          "2026'-style catalog query, only the one _play_in_app makes to find the "
          "preset song's own playable row",
          not any("wikipedia.org" in u or "term=ok" in u.lower() or "term=2026" in u.lower()
                  for u in FETCHED[fetched_before:]), FETCHED[fetched_before:])
    check("  only the plain understanding round trip, no extra Jev or model call",
          j2.calls == 1, j2.calls)
    check("  never 'I could not work out which song that is'",
          r2["did"] != "asked_back", r2["say"])


def t_warms_apple_music_and_shorter_press_wait():
    """Speed (owner's session: 7.6s for the first "play ... on apple music", of
    which Jev accounted for well under a second; 8.1s on a press that waited the
    full old timeout and still found nothing). Not independently re-measured live
    (no live Music playback in this lane's tests, per its budget); this checks the
    two changes are actually wired in."""
    fresh()
    started: list[str] = []
    real_ensure = am.ensure_running
    am.ensure_running = lambda app: started.append(app)
    try:
        turn("play shakira on apple music", player="apple_music", subject="shakira")
        check("Music is started in the background before the search/pick round trip",
              "Music" in started, started)
        started.clear()
        turn("make it louder", intent="control", extra={"control_action": "louder"})
        check("an unrelated request never starts Music",
              "Music" not in started, started)
    finally:
        am.ensure_running = real_ensure
    check("press_song_play's worst-case wait was cut (5.0/3.0 -> 3.0/2.0)",
          am.press_song_play.__defaults__[:2] == (3.0, 2.0), am.press_song_play.__defaults__)


def t_source_units():
    got = am.wiki_songs.__wrapped__ if hasattr(am.wiki_songs, "__wrapped__") else None
    STATE["wiki"] = TITANIC_WIKI
    songs = am.wiki_songs("Titanic song")
    check("wiki: song articles kept, their artist and year read",
          [(s_["title"], s_["artist"], s_["year"]) for s_ in songs] ==
          [("Titanic", "Falco", "1992"), ("My Heart Will Go On", "Celine Dion", "1997")], songs)
    STATE["wiki"] = WC_WIKI
    check("wiki: a biography is not a song", all(s_["title"] != "Shakira"
                                                 for s_ in am.wiki_songs("x")))
    check("search title drops feat. and versions, keeps the rest",
          am.search_title("La La La (Brazil 2014) [feat. Carlinhos Brown]") == "La La La (Brazil 2014)"
          and am.search_title("Dai Dai (Clean Bandit Remix)") == "Dai Dai")
    STATE["wiki"] = {"query": {"pages": {}}}


# ================================================================== Bench v1 media fixes
SHEERAN_VIDS = [{"id": "JGwWNGJdvx8", "title": "Ed Sheeran - Shape of You (Official Music Video)",
                 "channel": "Ed Sheeran", "length": "4:24", "views": "6.5B"},
                {"id": "2Vv-BfVoq4g", "title": "Ed Sheeran - Perfect (Official Music Video)",
                 "channel": "Ed Sheeran", "length": "4:39", "views": "3.9B"}]
SHARON_VIDS = [{"id": "aaaaaaaaaaa", "title": "Sharon Osbourne interview 2019", "channel": "TalkTV",
                "length": "12:03", "views": "90K"},
               {"id": "bbbbbbbbbbb", "title": "Ed and Sharon wedding dance", "channel": "Ed T",
                "length": "3:11", "views": "2K"}]
ARTISTS = {"ed sharon": {"resultCount": 2, "results": [
    {"wrapperType": "artist", "artistType": "Artist", "artistName": "Ed Sheeran",
     "artistId": 183313439},
    {"wrapperType": "artist", "artistType": "Artist", "artistName": "Ed O'Brien",
     "artistId": 156334706}]}}


class PickyJev(ScriptedJev):
    """pick_result (the YouTube path) refuses everything unless she asked for Sheeran,
    as live Jev did for "ed sharon" (Bench v1 med-003: any_good 0.82, no pick)."""

    def ask(self, state, qs):
        out = super().ask(state, qs)
        if "best" in qs:
            ok = "sheeran" in str(state.get("she_asked_for", "")).lower()
            out["best"] = {"choice": "0" if ok else "__none__", "confidence": 0.9,
                           "probabilities": {"0" if ok else "__none__": 0.9}}
            out["any_good"] = {"noul": 0.9 if ok else 0.82}
        return out


def _with_artist_search(fn):
    """am._fetch answering Apple's artist search from ARTISTS, everything else as before."""
    def fetch(url, timeout):
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(url).query)
        if q.get("entity") == ["musicArtist"]:
            FETCHED.append(url)
            return ARTISTS.get(q["term"][0].lower(), {"results": []})
        return fn(url, timeout)
    return fetch


def t_misheard_artist_youtube():
    """Bench v1 med-003: "play some ed sharon" -> "I looked for ed sharon and found
    nothing". Apple's artist search plus matching by sound finds Ed Sheeran."""
    fresh()
    queries: list[str] = []
    router.yt.search = lambda q, n=18: (queries.append(q), [dict(v) for v in (
        SHEERAN_VIDS if "sheeran" in q.lower() else SHARON_VIDS)])[1]
    am._fetch = _with_artist_search(fake_fetch)
    try:
        j = PickyJev()
        j.say = {"intent": "music", "span_subject": "ed sharon", "span_subject_exists": 0.95,
                 "names_a_specific_title": 0.2}
        r = router.handle(j, "play some ed sharon", speak=False, client="native",
                          activation="push")
        check("a misheard artist plays the real one, matched by sound",
              r["did"] == "playing" and "Sheeran" in str(r["detail"]),
              f"{r['did']} {r['say']} {r['detail']}")
        check("  searched YouTube again for the real name, once",
              queries[-1] == "Ed Sheeran" and len(queries) == 2, queries)
        det = r["detail"] if isinstance(r["detail"], dict) else {}
        check("  the detail says what was heard and what it was taken as",
              det.get("heard_as") == "ed sharon" and det.get("artist_fixed") == "Ed Sheeran", det)
        check("  one more pick only (understand, pick, pick)", j.calls == 3, j.calls)
    finally:
        am._fetch = fake_fetch
        router.yt.search = REAL_YT_SEARCH


def t_exact_artist_no_extra_lookup():
    """An artist found the first time costs nothing more: no artist search."""
    fresh()
    router.yt.search = lambda q, n=18: [dict(v) for v in SHEERAN_VIDS]
    am._fetch = _with_artist_search(fake_fetch)
    try:
        j = PickyJev()
        j.say = {"intent": "music", "span_subject": "ed sheeran", "span_subject_exists": 0.95}
        r = router.handle(j, "play ed sheeran", speak=False, client="native", activation="push")
        check("found first time: playing, two Jev calls, no artist search",
              r["did"] == "playing" and j.calls == 2
              and not any("musicArtist" in u for u in FETCHED), (r["did"], j.calls))
    finally:
        am._fetch = fake_fetch
        router.yt.search = REAL_YT_SEARCH


def t_misheard_artist_apple_music():
    """The same in Apple Music: no song for "ed sharon", Ed Sheeran's for the real name."""
    fresh()
    STATE["library"] = []
    STATE["more"] = {"ed sheeran": {"results": [
        _song(1193701392, "Shape of You", "Ed Sheeran", "÷ (Deluxe)", 1193701079,
              date="2017-01-06", aid=183313439)]}}
    am._fetch = _with_artist_search(fake_fetch)
    try:
        r, j = turn("play ed sharon on apple music", player="apple_music", subject="ed sharon",
                    pick=by("Sheeran"))
        check("Apple Music: the real artist's song", r["did"] in ("opened_in_app", "playing")
              and r["say"].startswith("Shape of You"), f"{r['did']} {r['say']}")
        check("  says what it was taken as", isinstance(r["detail"], dict)
              and r["detail"].get("artist_fixed") == "Ed Sheeran", r["detail"])
    finally:
        am._fetch = fake_fetch


def t_youtube_empty_retried_once():
    """Bench v1 hard-media-009: the Titanic song identified, then "found nothing" for
    'My Heart Will Go On (Love Theme from "Titanic") James Horner'; hard-media-002:
    "I looked for popular songs and found nothing". YouTube's page sometimes comes
    back empty: one retry, with the query cleaned."""
    fresh()
    queries: list[str] = []

    def flaky(q, n=18):
        queries.append(q)
        return [] if len(queries) == 1 else [dict(v) for v in SHEERAN_VIDS]
    router.yt.search = flaky
    try:
        subj = 'My Heart Will Go On (Love Theme from "Titanic") James Horner'
        r, j = turn("play " + subj, subject=subj)
        check("an empty first search is tried once more and plays",
              r["did"] == "playing" and len(queries) == 2, (r["did"], queries))
        check("  with the brackets and quotes cleaned out",
              queries[1] == "My Heart Will Go On James Horner", queries)
        fresh()
        queries.clear()
        router.yt.search = lambda q, n=18: (queries.append(q), [])[1]
        r, j = turn("play popular songs", subject="popular songs")
        check("empty twice: not found, and only one retry",
              r["did"] == "not_found" and len(queries) == 2, (r["did"], queries))
    finally:
        router.yt.search = REAL_YT_SEARCH
    check("clean_query keeps a plain query as it is",
          am.clean_query("bad bunny") == "bad bunny")
    check("clean_query drops feat., brackets, quotes and separators",
          am.clean_query('Shakira - La La La (Brazil 2014) [feat. Carlinhos Brown]')
          == "Shakira La La La", am.clean_query('Shakira - La La La (Brazil 2014) [feat. Carlinhos Brown]'))


def t_artist_by_sound_units():
    am._fetch = _with_artist_search(fake_fetch)
    try:
        got = am.artist_by_sound("ed sharon")
        check("ed sharon -> Ed Sheeran", got and got["name"] == "Ed Sheeran", got)
        ARTISTS["ed sheeran"] = ARTISTS["ed sharon"]
        check("an exact name is not 'fixed'", am.artist_by_sound("ed sheeran") is None)
        check("nothing close enough: None", am.artist_by_sound("billy eyelash") is None)
    finally:
        am._fetch = fake_fetch


def t_youtube_page_fetch():
    """Media p95 was 5.9 s (hard media 11 s) with Jev under a second: the slow step is
    YouTube's results page. One page fetch now: gzip, a duplicate after YT_HEDGE_S
    when the first has not answered, and the same page within a turn fetched once."""
    from savta.actions import youtube as ytm
    real = ytm._fetch_page
    seen: list[tuple[str, dict]] = []
    try:
        ytm._PAGES.clear()
        calls = {"n": 0}

        def stall_first(url, headers, timeout):
            calls["n"] += 1
            seen.append((url, headers))
            if calls["n"] == 1:
                time.sleep(2.0)
                return "<first>"
            return "<second>"
        ytm._fetch_page = stall_first
        ytm.YT_HEDGE_S, old = 0.2, ytm.YT_HEDGE_S
        t0 = time.time()
        page = ytm._page("https://www.youtube.com/results?search_query=x")
        took = time.time() - t0
        ytm.YT_HEDGE_S = old
        check("a stalled page fetch is hedged: the duplicate's page, fast",
              page == "<second>" and took < 1.0, (page, round(took, 2)))
        check("  asked for gzip", seen and seen[0][1].get("Accept-Encoding") == "gzip", seen[:1])
        n = calls["n"]
        again = ytm._page("https://www.youtube.com/results?search_query=x")
        check("  the same page again within the turn is not fetched again",
              again == page and calls["n"] == n, calls)
        ytm._PAGES.clear()
        ytm._fetch_page = lambda url, headers, timeout: None
        check("  both failing: None, not an exception",
              ytm._page("https://www.youtube.com/results?search_query=y") is None)
    finally:
        ytm._fetch_page = real
        ytm._PAGES.clear()


def main():
    for t in (t_units, t_owner_case_catalog_only, t_measured_span_shapes, t_in_library_plays,
              t_stop_and_undo_pause_music, t_another_one_stays_in_music, t_hebrew,
              t_follow_ups_stay_in_the_player,
              t_spotify, t_unspecified_and_preference, t_not_found_and_unreachable,
              t_named_units, t_named_media, t_named_messages, t_named_search,
              t_named_generic_apps, t_song_keys_and_precheck, t_another_one_never_repeats,
              t_described_song, t_apple_music_playback, t_source_units,
              t_described_song_from_sources,
              t_screen_song, t_sound_match_within_artist, t_named_app_switch_on_reject,
              t_reject_plays_another_never_not_found, t_year_refines_described_song_search,
              t_warms_apple_music_and_shorter_press_wait,
              t_misheard_artist_youtube, t_exact_artist_no_extra_lookup,
              t_misheard_artist_apple_music, t_youtube_empty_retried_once,
              t_artist_by_sound_units, t_youtube_page_fetch):
        print(f"\n{t.__name__}")
        try:
            t()
        except Exception:  # noqa: BLE001
            import traceback
            FAILED.append((t.__name__, traceback.format_exc()))
            print(traceback.format_exc())
    mac._osa = tm._blocked_osa
    real_osa = [c for c in tm.LAUNCHED if c and c[0] == "osascript"]
    check("0 osascript escapes (every script went to the fake)",
          not tm.OSA and not real_osa, f"{len(tm.OSA)} + {len(real_osa)}")
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAIL {n}: {str(d)[:400]}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
