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
def _song(tid, name, artist, album, cid, streamable=True):
    return {"wrapperType": "track", "kind": "song", "trackId": tid, "trackName": name,
            "artistName": artist, "collectionName": album, "releaseDate": "1969-08-05T07:00:00Z",
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
STATE = {"library": ["achille"], "catalog": CATALOG, "playing": True, "more": {}}


def fake_fetch(url, timeout):
    FETCHED.append(url)
    if STATE["catalog"] is None:
        raise OSError("offline")
    from urllib.parse import parse_qs, urlsplit
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
         extra=None):
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
    r = router.handle(j, utterance, speak=False, client="native", activation="push")
    return r, j


def fresh():
    tm.reset_state()
    for x in (FETCHED, SCRIPTS, OPENS, COPIED, tm.OPENED, tm.LAUNCHED, tm.APPS, tm.SENT,
              tm.WA, tm.YT_QUERIES):
        x.clear()
    APPS_HERE[:] = ["Calculator", "Music", "Maps", "Pages", "Photos", "Safari", "Telegram",
                    "WhatsApp", "Messages"]
    STATE.update(library=["achille"], catalog=CATALOG, playing=True, more={})
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
    check("nothing opened in a browser or app", not OPENS and not tm.OPENED, (OPENS, tm.OPENED))
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
          r["did"] == "not_found" and not OPENS, (r["did"], OPENS))
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
        import time as _t
        for _ in range(40):
            if STARTS:
                break
            _t.sleep(0.05)
        check("'order me headphones on Amazon': the agent, started on Amazon's search",
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


def main():
    for t in (t_units, t_owner_case_catalog_only, t_measured_span_shapes, t_in_library_plays,
              t_stop_and_undo_pause_music, t_another_one_stays_in_music, t_hebrew,
              t_follow_ups_stay_in_the_player,
              t_spotify, t_unspecified_and_preference, t_not_found_and_unreachable,
              t_named_units, t_named_media, t_named_messages, t_named_search,
              t_named_generic_apps):
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
