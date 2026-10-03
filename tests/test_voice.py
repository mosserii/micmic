#!/usr/bin/env python3
"""savta/voice.py, savta/voice_tags.py and savta/voice_models.py: how MicMic sounds,
offline.

    .venv/bin/python3 tests/test_voice.py

Nothing here loads a model, plays a sound or touches the network: the engine and the
audio player are fakes (voice.ENGINE_FACTORY, voice.PLAYER_FACTORY), `say` is a fake
Popen that records its arguments, the download is served from a temp dir on a free
local port (MICMIC_VOICE_DIR is a temp dir), and the cloud voice talks to a fake voice
server on 127.0.0.1 (FakeCloud), never to ElevenLabs or MicMic's cloud. Never 8799.
"""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="voice-test-state-")
os.environ["MICMIC_VOICE_DIR"] = tempfile.mkdtemp(prefix="voice-test-models-")
os.environ["MICMIC_VOICE_SUPPORTED"] = "1"
os.environ.pop("MICMIC_ASR_NO_DOWNLOAD", None)
# No real voice server, whatever this shell has: the cloud tests set their own fakes.
for _k in ("ELEVENLABS_API_KEY", "MICMIC_ELEVEN_URL", "MICMIC_PROXY_URL", "MICMIC_PROXY_TOKEN",
           "MICMIC_CLOUD_VOICE", "MICMIC_VOICE_TAGS"):
    os.environ.pop(_k, None)

from savta import voice as V  # noqa: E402
from savta import voice_models as VM  # noqa: E402
from savta.actions import mac  # noqa: E402
from savta import router  # noqa: E402

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


# ---------------------------------------------------------------- fakes

POPENS: list[list[str]] = []


class FakeProc:
    def __init__(self, args, **_k):
        POPENS.append(list(args))
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False


class _FakeSubprocess:
    DEVNULL = -3

    @staticmethod
    def Popen(args, **k):
        if args and args[0] != "say":
            raise AssertionError(f"unexpected process {args}")
        return FakeProc(args, **k)


mac.subprocess = _FakeSubprocess          # `say` is never really started here


class FakeEngine:
    sample_rate = 24000

    def __init__(self, fail_on: str | None = None, delay: float = 0.0):
        self.calls: list[str] = []
        self.fail_on, self.delay = fail_on, delay

    def synth(self, text):
        self.calls.append(text)
        time.sleep(self.delay)
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("synthesis failed")
        return b"\x00\x00\x80\x3f" * 2400       # 0.1 s of 1.0f


PLAYERS: list["FakePlayer"] = []


class FakePlayer:
    """Holds 'playback' open until released or stopped, like a real queue would."""

    def __init__(self, rate, fmt="f32"):
        self.rate, self.fmt = rate, fmt
        self.played, self.stopped, self.closed = [], False, False
        self.release = threading.Event()
        PLAYERS.append(self)

    def play(self, pcm):
        self.played.append(pcm)

    def drain(self, cancel):
        while not (self.release.is_set() or cancel.is_set() or self.stopped):
            time.sleep(0.005)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


V.PLAYER_FACTORY = FakePlayer


def use_engine(engine):
    V._STATE.update(engine=engine, lang="english", loading=False, current=None)
    V._CACHE.clear()


def wait_until(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.01)
    return fn()


# ---------------------------------------------------------------- what any voice reads

def test_prepare():
    t = V.prepare("Here you go. Masha and the Bear 💥 NEW EPISODE 2022 ✨Tales from the East ✨ "
                  "(Official Video)")
    check("emoji are not read out", "💥" not in t and "✨" not in t, t)
    check("(Official Video) is dropped", "Official" not in t, t)
    check("a shouted title is brought to normal case", "New Episode" in t, t)
    check("the words themselves stay", t.startswith("Here you go. Masha and the Bear"), t)
    check("a lone acronym stays as it is", "BBC" in V.prepare("Playing the BBC news."))
    check("a dash between title parts becomes a pause",
          V.prepare("Joy Crookes - Feet Don't Fail Me Now") == "Joy Crookes, Feet Don't Fail Me Now")
    check("a semicolon list from a music catalog becomes a pause",
          V.prepare("מיד. בוא אלי פרפר נחמד Music;Nurit Hirsh", "hebrew")
          == "מיד. בוא אלי פרפר נחמד Music, Nurit Hirsh")
    he = "פתחתי WhatsApp. שולחת לדנה: אני מאחרת."
    check("Hebrew without clutter is untouched", V.prepare(he, "hebrew") == he)
    check("plain English is untouched",
          V.prepare("Sending Dana: I'm running late. Say no and I will stop.")
          == "Sending Dana: I'm running late. Say no and I will stop.")
    check("a line of only emoji still says something", V.prepare("👍") == "👍")


def test_normalize_en():
    n = V.normalize_en
    check("7:09 pm is 'seven oh nine PM'", n("It is 7:09 pm on Thursday") == "It is 7 oh 9 PM on Thursday",
          n("It is 7:09 pm on Thursday"))
    check("12:38 am", n("12:38 am") == "12 38 AM", n("12:38 am"))
    check("00:40 is twenty to one in the night, said as 12 40 AM", n("00:40") == "12 40 AM", n("00:40"))
    check("3:00 is three o'clock", n("at 3:00") == "at 3 o'clock", n("at 3:00"))
    check("9 a.m. is 9 AM", n("at 9 a.m. tomorrow") == "at 9 AM tomorrow", n("at 9 a.m. tomorrow"))
    check("a version number is left alone", n("macOS 15.6.1") == "macOS 15.6.1")


def test_chunks():
    c = V.chunks("Sending Dana: I'm running late. Say no and I will stop.")
    check("the first part ends at the colon", c == ["Sending Dana:", "I'm running late.",
                                                    "Say no and I will stop."], c)
    c = V.chunks("Playing Hips Don't Lie by Shakira on Spotify.")
    check("no early comma: cut where a phrase starts",
          c == ["Playing Hips Don't Lie", "by Shakira on Spotify."], c)
    c = V.chunks("Okay. In 10 minutes I'll remind you to take your pills, at 3:45.")
    check("a short first sentence is its own first part", c[0] == "Okay.", c)
    c = V.chunks("Should I also email Mrs. O'Brien at 9 a.m. tomorrow?")
    check("Mrs. and a.m. do not end a sentence", c[-1].endswith("9 a.m. tomorrow?") and
          not any(x.endswith("Mrs.") for x in c), c)
    long = "word " * 120
    check("nothing longer than the model reads at once",
          all(len(x) <= V.MAX_CHARS for x in V.chunks(long)))
    lines = json.loads((Path(__file__).parent / "voice_lines.json").read_text())
    firsts = [V.chunks(V.prepare(l))[0] for l in lines]
    check("every typical line starts with at most five words (first sound comes fast)",
          all(len(f.split()) <= 5 for f in firsts), [f for f in firsts if len(f.split()) > 5])


KOKORO_SYMBOLS = set(" !\"(),.:;?AIOQSTWYabcdefhijklmnopqrstuvwxyzæçðøŋœɐɑɒɔɕɖəɚɛɜɟɡɣɤɥɨɪɯɰɲɳɴɸɹɻɽɾʁʂʃʈʊʋʌʎ"
                     "ʒʔʝʣʤʥʦʧʨʰʲˈˌː̃βθχᵊᵝᵻ—“”…→↓↗↘ꭧ")


def test_lexicon_and_phonemes():
    bad = {w: p for w, p in V.LEXICON.items() if set(p) - KOKORO_SYMBOLS}
    check("every lexicon entry is in Kokoro's phoneme set", not bad, bad)
    seen = []
    g2p = lambda s: seen.append(s) or f"<{s.strip()}>"  # noqa: E731
    ps = V.phonemize("Welcome to MicMic, open whatsapp on macOS 15.6.1.", g2p)
    check("MicMic is said as written in the lexicon", "mˈɪkmɪk" in ps, ps)
    check("lexicon words match in any case", V.LEXICON["WhatsApp"] in ps, ps)
    check("macOS is not left to the rules", V.LEXICON["macOS"] in ps and "macOS" not in "".join(seen), seen)
    check("a version number reaches the rules whole", any("15.6.1" in s for s in seen), seen)
    check("punctuation stays (it is the pause)", ps.count(",") == 1 and ps.endswith("."), ps)
    ps = V.phonemize("It is 7:09 pm.", g2p)
    check("times are written out before the rules see them", "<It is 7 oh 9 PM>" in ps, ps)


# ---------------------------------------------------------------- speaking

def settings(voice):
    router.save_settings({"voice": voice})


def test_mac_voice_when_chosen():
    POPENS.clear()
    use_engine(FakeEngine())
    settings("mac")
    mac.say("Opening WhatsApp.", "english")
    check("with the Mac voice chosen, say speaks", POPENS and POPENS[-1][0] == "say", POPENS)
    check("the natural engine is not used", not V._STATE["engine"].calls)
    check("a Hebrew line keeps Carmit", (mac.say("פתחתי WhatsApp.", "hebrew") or True)
          and POPENS[-1][:3] == ["say", "-v", "Carmit"], POPENS[-1])
    mac.stop_speaking()


def test_natural_voice_speaks_and_stops():
    POPENS.clear(); PLAYERS.clear()
    eng = FakeEngine()
    use_engine(eng)
    settings("cloud")
    mac.say("Sending Dana: I'm running late. Say no and I will stop.", "english")
    check("the natural voice takes an English line", wait_until(lambda: len(eng.calls) == 3), eng.calls)
    check("say is not started for it", not POPENS, POPENS)
    check("it is reported as speaking while it plays", mac.speaking())
    check("the first part is synthesised first", eng.calls[0] == "Sending Dana:", eng.calls)
    check("audio reaches the player", PLAYERS and len(PLAYERS[-1].played) == 3)
    check("time to first audio is recorded", V.STATS and V.STATS[-1] < 1.0, V.STATS[-1:])
    check("/api/speaking's text is the line", "Sending Dana" in mac._SPEAKING.get("text", ""))
    check("stop_speaking cuts it off", mac.stop_speaking())
    check("the player was stopped", wait_until(lambda: PLAYERS[-1].stopped and PLAYERS[-1].closed))
    check("then nothing is being said", wait_until(lambda: not mac.speaking()))
    check("stopping silence stops nothing", not mac.stop_speaking())


def test_new_line_replaces_old():
    PLAYERS.clear()
    eng = FakeEngine()
    use_engine(eng)
    settings("cloud")
    mac.say("A first answer that is still going.", "english")
    wait_until(lambda: PLAYERS)
    first = PLAYERS[-1]
    mac.say("Second.", "english")
    check("a new answer stops the old one", wait_until(lambda: first.stopped), first.stopped)
    check("and is spoken itself", wait_until(lambda: len(PLAYERS) == 2 and PLAYERS[-1].played))
    mac.stop_speaking()


def test_stop_while_synthesising():
    PLAYERS.clear()
    eng = FakeEngine(delay=0.15)
    use_engine(eng)
    settings("cloud")
    mac.say("Okay. A second sentence. And a third one here.", "english")
    time.sleep(0.05)
    check("stop works before any sound came out", mac.stop_speaking())
    time.sleep(0.4)
    check("nothing more is synthesised after a stop", len(eng.calls) <= 1, eng.calls)
    check("and nothing more is played", all(not p.played for p in PLAYERS), [len(p.played) for p in PLAYERS])


def test_failure_falls_back_to_say():
    POPENS.clear(); PLAYERS.clear()
    eng = FakeEngine(fail_on="Say no")
    use_engine(eng)
    settings("cloud")
    mac.say("Sending Dana: I'm running late. Say no and I will stop.", "english")
    check("a failure halfway hands the rest to say",
          wait_until(lambda: POPENS and POPENS[-1][-1] == "Say no and I will stop."), POPENS)
    check("what was already spoken is not repeated", POPENS and "Sending" not in POPENS[-1][-1])
    mac.stop_speaking()
    POPENS.clear()
    use_engine(FakeEngine(fail_on="Okay"))
    mac.say("Okay.", "english")
    check("a failure on the first part: say speaks the whole line",
          wait_until(lambda: POPENS and POPENS[-1][-1] == "Okay."), POPENS)
    mac.stop_speaking()


def test_other_languages_without_the_cloud_stay_on_say():
    POPENS.clear()
    use_engine(FakeEngine())
    settings("cloud")
    for lang, v in (("hebrew", "Carmit"), ("arabic", "Majed"), ("russian", "Milena")):
        mac.say("שלום" if lang == "hebrew" else "test", lang)
        check(f"{lang} is spoken by {v} with no cloud to reach", POPENS[-1][:3] == ["say", "-v", v],
              POPENS[-1])
    check("Kokoro was not asked (English only)", not V._STATE["engine"].calls)
    mac.stop_speaking()


def test_not_loaded_yet_uses_say_and_loads():
    POPENS.clear()
    V._STATE.update(engine=None, lang=None, loading=False, current=None)
    settings("cloud")
    loaded = threading.Event()

    def factory(lang):
        loaded.set()
        return FakeEngine()
    V.ENGINE_FACTORY = factory
    _install_fake_model()
    for d in (VM.model_dir("english"), VM.runtime_dir()):   # installed, as far as it knows
        d.mkdir(parents=True, exist_ok=True)
        (d / ".verified").write_text("{}")
    mac.say("Opening WhatsApp.", "english")
    check("while the voice loads, say speaks the line", POPENS and POPENS[-1][0] == "say", POPENS)
    check("and the voice starts loading", loaded.wait(2))
    check("the next line is the natural voice's",
          wait_until(lambda: V._STATE["engine"] is not None))
    mac.stop_speaking()
    for d in (VM.model_dir("english"), VM.runtime_dir()):    # back to not installed
        (d / ".verified").unlink()


def test_repeated_line_is_cached():
    eng = FakeEngine()
    use_engine(eng)
    settings("cloud")
    for _ in range(2):
        mac.say("Opening WhatsApp.", "english")
        wait_until(lambda: not V.busy() or PLAYERS[-1].played)
        PLAYERS[-1].release.set()
        wait_until(lambda: not V.busy())
    check("the same line is synthesised once", eng.calls.count("Opening WhatsApp.") == 1, eng.calls)


# ---------------------------------------------------------------- the download

SERVE = Path(tempfile.mkdtemp(prefix="voice-serve-"))
HITS: list[str] = []


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SERVE), **k)

    def log_message(self, *a):
        pass

    def send_head(self):
        HITS.append(self.path)
        return super().send_head()


srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def put(name: str, data: bytes):
    p = SERVE / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return name, len(data), hashlib.sha256(data).hexdigest()


def wheel(name: str, files: dict) -> dict:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    n, size, sha = put(name, buf.getvalue())
    return {"name": n, "url": f"{BASE}/{n}", "size": size, "sha256": sha}


REAL_RUNTIME, REAL_MODELS = VM.RUNTIME, VM.MODELS


def _install_fake_model():
    """The catalog swapped for tiny files on the local server."""
    VM.RUNTIME = [wheel("rt.whl", {"mlx/__init__.py": "X = 1\n",
                                   "espeakng_loader/libespeak-ng.dylib": "bin"})]
    import savta.asr_models as A
    A.HF = BASE + "/{name}"
    VM.MODELS = {"english": {"engine": "tiny", "repo": "r", "rev": "v", "voice": "af_heart",
                             "license": "test",
                             "files": [put("config.json", b"{}"),
                                       put("kokoro-v1_0.safetensors", os.urandom(200_000)),
                                       put("voices/af_heart.safetensors", os.urandom(5_000))]}}


def wait_download(timeout=10.0):
    end = time.time() + timeout
    while time.time() < end and VM.status()["state"] == "downloading":
        time.sleep(0.05)
    return VM.status()


def test_download():
    _install_fake_model()
    os.environ["MICMIC_VOICE_SUPPORTED"] = "0"
    check("a Mac that cannot run it says so", VM.status()["state"] == "unsupported")
    check("and nothing is downloaded there", VM.install()["state"] == "unsupported")
    os.environ["MICMIC_VOICE_SUPPORTED"] = "1"
    check("Hebrew has no natural voice yet", VM.status("hebrew")["state"] == "say")
    s = VM.status()
    check("nothing downloaded at first", s["state"] in ("none", "ready"), s)
    ready = []
    VM.ON_READY.append(ready.append)
    VM.install()
    s = wait_download()
    check("the download finishes ready", s["state"] == "ready", s)
    check("the runtime is unpacked", (VM.runtime_dir() / "mlx" / "__init__.py").exists())
    check("the voice file is in its folder",
          (VM.model_dir("english") / "voices" / "af_heart.safetensors").exists())
    check("the server is told it is ready", ready == ["english"], ready)
    HITS.clear()
    VM.install()
    check("installing again downloads nothing", wait_download()["state"] == "ready" and not HITS, HITS)


def test_download_seam():
    """The seam every other test suite relies on: no bytes move, the job waits."""
    VM.MODELS = {"english": {**VM.MODELS["english"], "engine": "seam"}}
    os.environ["MICMIC_ASR_NO_DOWNLOAD"] = "1"
    try:
        import shutil
        shutil.rmtree(VM.runtime_dir(), ignore_errors=True)   # test temp dir only
        VM.install()
        time.sleep(0.2)
        s = VM.status()
        check("with the seam on, the job waits and no model byte arrives",
              s["state"] == "downloading" and s["done"] < s["total"]
              and not any(VM.model_dir("english").glob("*.safetensors")), s)
        check("the request is recorded in the voice folder",
              (VM.base() / "DOWNLOAD_REQUESTED").exists())
        VM.cancel()
        check("cancel ends it", wait_download()["state"] != "downloading", VM.status())
    finally:
        os.environ.pop("MICMIC_ASR_NO_DOWNLOAD", None)
# No real voice server, whatever this shell has: the cloud tests set their own fakes.
for _k in ("ELEVENLABS_API_KEY", "MICMIC_ELEVEN_URL", "MICMIC_PROXY_URL", "MICMIC_PROXY_TOKEN",
           "MICMIC_CLOUD_VOICE", "MICMIC_VOICE_TAGS"):
    os.environ.pop(_k, None)
    check("no real file was ever asked for",
          not (VM.base() / "REAL_DOWNLOAD_IN_TEST").exists())


def test_real_catalog_is_pinned():
    files = [f for m in REAL_MODELS.values() for f in m["files"]]
    check("every model file has a size and a sha256",
          all(isinstance(f[1], int) and len(f[2]) == 64 for f in files))
    check("every runtime wheel has a size and a sha256",
          all(r["size"] > 0 and len(r["sha256"]) == 64 for r in REAL_RUNTIME))
    check("the model is pinned to a revision", all(len(m["rev"]) == 40 for m in REAL_MODELS.values()))
    check("runtime wheels match the bundled CPython 3.11 on arm64",
          all(("cp311" in r["name"] or "py3-none" in r["name"]) and
              ("arm64" in r["name"] or "any" in r["name"]) for r in REAL_RUNTIME))


# ---------------------------------------------------------------- settings

def test_settings_validation():
    from savta.server import _validate_settings, voice_status
    for v in ("cloud", "device", "mac"):
        ok, bad = _validate_settings({"voice": v})
        check(f"voice={v} is a setting", ok == {"voice": v} and not bad, (ok, bad))
    ok, bad = _validate_settings({"voice": "robot"})
    check("anything else is refused", not ok and bad == ["voice"], (ok, bad))
    ok, bad = _validate_settings({"voice_tags": False})
    check("the tags' kill switch is a setting", ok == {"voice_tags": False}, (ok, bad))
    ok, bad = _validate_settings({"voice_tags": "no"})
    check("only as true or false", bad == ["voice_tags"], (ok, bad))
    settings("mac")
    s = voice_status()
    check("/api/voice says her choice and the download state",
          s["choice"] == "mac" and "state" in s and "total" in s, s)
    check("and never a key or a token", "token" not in json.dumps(s).lower()
          and "secret" not in json.dumps(s).lower(), s)


# ---------------------------------------------------------------- the cloud voice

PCM16 = b"\x01\x00\xff\x7f" * 6000            # 0.5 s of 16-bit "audio" at 24 kHz


class FakeCloud:
    """Answers like MicMic's /v1/tts and like ElevenLabs' stream endpoint, on 127.0.0.1.
    Streams PCM in chunks; .mode makes it refuse, stall or cut the stream."""

    def __init__(self):
        self.mode, self.status_body, self.delay, self.chunk_delay = "ok", None, 0.0, 0.0
        self.hits: list[dict] = []
        self.ports: set = set()
        cloud = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                cloud.ports.add(self.client_address[1])
                cloud.hits.append({"path": self.path, "headers": dict(self.headers),
                                   "body": json.loads(body)})
                if cloud.mode.startswith("status"):
                    st, payload, extra = cloud.status_body
                    raw = json.dumps(payload).encode()
                    self.send_response(st)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    for k, v in extra.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if cloud.delay:
                    time.sleep(cloud.delay)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                parts = [PCM16[i:i + 4801] for i in range(0, len(PCM16), 4801)]  # odd sizes
                for i, part in enumerate(parts):
                    if cloud.chunk_delay and i:
                        time.sleep(cloud.chunk_delay)
                    if cloud.mode == "cut" and i == 2:
                        self.wfile.flush()
                        self.connection.shutdown(2)
                        return
                    try:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                        self.wfile.flush()
                    except OSError:
                        return
                self.wfile.write(b"0\r\n\r\n")

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.srv.handle_error = lambda *a: None     # a kept-alive connection closed by us
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


CLOUD = FakeCloud()
FAKE_TOKEN = "mmp_FAKE-DEVICE-TOKEN-for-tests"


def via_proxy():
    """This Mac signed in to (a fake) MicMic cloud: the cloud voice goes through it."""
    os.environ.update(MICMIC_MODE="proxy", MICMIC_PROXY_URL=CLOUD.url,
                      MICMIC_PROXY_TOKEN=FAKE_TOKEN)
    os.environ.pop("ELEVENLABS_API_KEY", None)
    reset_cloud()


def via_own_key():
    os.environ.update(ELEVENLABS_API_KEY="sk_FAKE-OWN-ELEVEN-KEY", MICMIC_ELEVEN_URL=CLOUD.url)
    reset_cloud()


def no_cloud():
    for k in ("MICMIC_MODE", "MICMIC_PROXY_URL", "MICMIC_PROXY_TOKEN", "ELEVENLABS_API_KEY",
              "MICMIC_ELEVEN_URL", "MICMIC_CLOUD_VOICE"):
        os.environ.pop(k, None)
    reset_cloud()


def reset_cloud():
    mac.stop_speaking()
    CLOUD.mode, CLOUD.delay, CLOUD.status_body, CLOUD.chunk_delay = "ok", 0.0, None, 0.0
    V.ONLINE_PROBE = None
    CLOUD.hits.clear()
    CLOUD.ports.clear()
    V._COOL.update(until=0.0, why="", fails=0)
    with V._CONN_LOCK:
        c = V._CONN["conn"]
        V._CONN.update(conn=None, key=None)
    if c is not None:
        c.close()
    V.FIRST_AUDIO_S = 1.5
    clear_kept()
    POPENS.clear(); PLAYERS.clear()


def clear_kept():
    import shutil
    shutil.rmtree(V.keep_dir(), ignore_errors=True)        # the test's own temp folder
    V.KEEP_STATS.update(hits=0, stored=0, ignored=0)


def kept_files():
    return sorted(p.name for p in V.keep_dir().glob("*.pcm"))


def played_bytes(p):
    return sum(len(x) for x in p.played)


def finish_line(timeout=3.0):
    """Let the fake player finish what it was given."""
    ok = wait_until(lambda: PLAYERS and played_bytes(PLAYERS[-1]) >= len(PCM16), timeout)
    if PLAYERS:
        PLAYERS[-1].release.set()
    wait_until(lambda: not V.busy(), timeout)
    return ok


def test_cloud_voice_speaks_hebrew_through_micmic_cloud():
    via_proxy()
    use_engine(FakeEngine())
    settings("cloud")
    check("Hebrew's engine is the cloud voice", V.plan("hebrew") == "eleven_turbo", V.plan("hebrew"))
    check("English is on the cloud voice too, Kokoro only as a downloaded stand-in",
          V.engines("english") == ["eleven_turbo", "kokoro", "say"]
          and V.plan("english") == "eleven_turbo", (V.engines("english"), V.plan("english")))
    mac.say("השעה 19:08, יום חמישי, 24 בספטמבר.", "hebrew")
    check("the audio streams into the player", finish_line(), [played_bytes(p) for p in PLAYERS])
    p = PLAYERS[-1]
    check("as 24 kHz 16-bit samples", p.rate == 24000 and p.fmt == "s16", (p.rate, p.fmt))
    check("every buffer is whole samples", all(len(x) % 2 == 0 for x in p.played))
    check("all of it, then a little silence", b"".join(p.played).startswith(PCM16))
    hit = CLOUD.hits[-1]
    check("it asked MicMic's cloud, not ElevenLabs", hit["path"] == "/v1/tts", hit["path"])
    check("with the device token", hit["headers"].get("Authorization") == f"Bearer {FAKE_TOKEN}")
    check("the cleaned line, its language and the voice",
          hit["body"] == {"text": "השעה 19:08, יום חמישי, 24 בספטמבר.", "lang": "he",
                          "voice": "jessica", "cache": False}, hit["body"])
    check("say was not started", not POPENS, POPENS)
    check("first audio is measured", V.CLOUD_STATS and V.CLOUD_STATS[-1] < 1.0, V.CLOUD_STATS[-1:])
    check("Kokoro never heard it", not V._STATE["engine"].calls)


def test_cloud_tag_and_cache_reach_only_the_cloud():
    via_proxy()
    settings("cloud")
    mac.say("[softly] It's late. I'll remind you tomorrow at 9.", "hebrew", tag="softly",
            cache=True)
    finish_line()
    b = CLOUD.hits[-1]["body"]
    check("the tag travels apart from the words",
          b.get("tag") == "softly" and b["text"] == "It's late. I'll remind you tomorrow at 9.", b)
    check("a template line may be cached", b["cache"] is True, b)
    # The same line on the Mac voices: no tag, no bracket.
    settings("mac")
    mac.say("[softly] It's late.", "hebrew", tag="softly")
    check("say never reads a tag", POPENS and POPENS[-1][-1] == "It's late.", POPENS[-1:])
    eng = FakeEngine()
    use_engine(eng)
    settings("device")                  # On-device voice: Kokoro, which she downloaded
    mac.say("[warmly] Sent to Dana.", "english", tag="warmly")
    wait_until(lambda: eng.calls)
    mac.stop_speaking()
    check("Kokoro never reads a tag", eng.calls and not any("[" in c or "warmly" in c
                                                            for c in eng.calls), eng.calls)


def test_own_key_goes_to_elevenlabs_directly():
    via_own_key()
    settings("cloud")
    mac.say("Sent.", "russian", tag="calmly")
    finish_line()
    hit = CLOUD.hits[-1]
    check("straight to the stream endpoint, as PCM",
          hit["path"] == "/v1/text-to-speech/cgSgspJ2msm6clMCkdW9/stream?output_format=pcm_24000",
          hit["path"])
    check("with her key", hit["headers"].get("xi-api-key") == "sk_FAKE-OWN-ELEVEN-KEY")
    check("the tag in front of the words, Turbo, Russian",
          hit["body"] == {"text": "[calmly] Sent.", "model_id": "eleven_v4_turbo",
                          "language_code": "ru"}, hit["body"])
    no_cloud()


def test_on_device_keeps_it_on_the_mac():
    via_proxy()
    settings("device")
    mac.say("שלום", "hebrew")
    check("On-device voice: say speaks Hebrew", POPENS and POPENS[-1][:3] == ["say", "-v", "Carmit"])
    check("and nothing was sent", not CLOUD.hits, CLOUD.hits)
    check("the router sees no cloud voice", V.plan("hebrew") == "say")
    settings("cloud")
    os.environ["MICMIC_CLOUD_VOICE"] = "off"
    mac.say("שלום", "hebrew")
    check("the app's kill switch: say", POPENS[-1][:3] == ["say", "-v", "Carmit"] and not CLOUD.hits)
    os.environ.pop("MICMIC_CLOUD_VOICE")


def test_out_of_characters_falls_back_and_pauses():
    via_proxy()
    settings("cloud")
    CLOUD.mode = "status"
    CLOUD.status_body = (429, {"error": "tts_limit", "scope": "month", "limit": 100000,
                               "resets_at": "2099-01-01T00:00:00Z"}, {})
    mac.say("שלום לך.", "hebrew")
    check("the whole line is said by the Mac", wait_until(lambda: POPENS)
          and POPENS[-1][-1] == "שלום לך.", POPENS)
    check("the cloud voice rests until the month resets", V.plan("hebrew") == "say"
          and V._COOL["why"] == "limit" and V._COOL["until"] > time.time() + 86400 * 30)
    n = len(CLOUD.hits)
    mac.say("עוד משהו.", "hebrew")
    check("and is not asked again meanwhile", len(CLOUD.hits) == n, len(CLOUD.hits))
    check("Settings can say why", V.status()["cloud"]["paused"] == "limit", V.status()["cloud"])
    reset_cloud()
    CLOUD.mode = "status"
    CLOUD.status_body = (503, {"error": "tts_disabled"}, {"Retry-After": "600"})
    mac.say("שלום.", "hebrew")
    wait_until(lambda: POPENS)
    check("the cloud's kill switch: the Mac speaks, the voice rests 10 minutes",
          POPENS and V._COOL["why"] == "off" and 500 < V._COOL["until"] - time.time() <= 600)
    reset_cloud()


def test_an_old_cloud_or_a_failing_one_rests_the_voice():
    via_proxy()
    settings("cloud")
    CLOUD.mode = "status"
    CLOUD.status_body = (404, {"error": "not_found"}, {})
    mac.say("שלום.", "hebrew")
    check("a cloud without /v1/tts: the Mac speaks", wait_until(lambda: POPENS)
          and POPENS[-1][-1] == "שלום.")
    check("and the cloud voice rests an hour instead of costing every line a round trip",
          V._COOL["why"] == "off" and V._COOL["until"] - time.time() > 3000, V._COOL)
    reset_cloud()
    for status, err in ((502, "upstream_error"), (500, "boom"), (400, "tag_not_allowed"),
                        (504, "upstream_timeout")):
        reset_cloud()
        CLOUD.mode = "status"
        CLOUD.status_body = (status, {"error": err}, {})
        for line in ("אחד.", "שתיים."):
            POPENS.clear()
            mac.say(line, "hebrew")
            wait_until(lambda: POPENS)
        check(f"a {status}: both lines spoken by the Mac, the cloud asked only once",
              len(CLOUD.hits) == 1 and POPENS and POPENS[-1][-1] == "שתיים.",
              (len(CLOUD.hits), POPENS))
        check(f"a {status}: the cloud voice rests (first step of the backoff)",
              V._COOL["why"] == "unreachable" and V._COOL["fails"] == 1
              and 5 < V._COOL["until"] - time.time() <= V.BACKOFF_S[0], V._COOL)
    # The rest grows with each failure in a row, and one success resets it.
    reset_cloud()
    CLOUD.mode = "status"
    CLOUD.status_body = (502, {"error": "upstream_error"}, {})
    rests = []
    for _ in range(5):
        V._COOL["until"] = 0.0             # the rest is over: it tries again
        POPENS.clear()
        mac.say("אחד.", "hebrew")
        wait_until(lambda: POPENS)
        rests.append(round(V._COOL["until"] - time.time()))
    check("the rest grows: 10 s, 1 min, 5 min, 15 min, 15 min",
          [abs(r - b) <= 2 for r, b in zip(rests, (10, 60, 300, 900, 900))] == [True] * 5, rests)
    V._COOL["until"] = 0.0
    CLOUD.mode = "ok"
    mac.say("שלום.", "hebrew")
    finish_line()
    check("one line that works resets it", V._COOL["fails"] == 0, V._COOL)
    reset_cloud()


def test_no_audio_in_time_goes_to_the_next_engine():
    via_proxy()
    settings("cloud")
    V.FIRST_AUDIO_S = 0.3
    CLOUD.delay = 1.0
    t0 = time.time()
    mac.say("שלום לך.", "hebrew")
    check("say takes the whole line", wait_until(lambda: POPENS) and POPENS[-1][-1] == "שלום לך.")
    check("without waiting for the slow server", time.time() - t0 < 0.9, time.time() - t0)
    time.sleep(1.2)
    check("and the late audio is never played", not any(p.played for p in PLAYERS))
    reset_cloud()


def test_a_cut_stream_finishes_on_the_mac():
    via_proxy()
    settings("cloud")
    CLOUD.mode = "cut"
    line = "First sentence here. Second sentence here. Third one to finish."
    stop_releasing = threading.Event()

    def release():                      # the speaker finishes what it was given
        while not stop_releasing.is_set():
            for p in list(PLAYERS):
                p.release.set()
            time.sleep(0.01)
    threading.Thread(target=release, daemon=True).start()
    mac.say(line, "hebrew")
    check("the rest is spoken by say", wait_until(lambda: POPENS, 4), POPENS)
    heard = sum(len(x) for p in PLAYERS for x in p.played) / 48000
    check("what arrived was played first", heard > 0.15, heard)
    check("from the first sentence not heard, never skipping one",
          POPENS and POPENS[-1][-1] == V.rest_after(line, heard) and POPENS[-1][-1].endswith(
              "Third one to finish."), (heard, POPENS[-1:]))
    stop_releasing.set()
    reset_cloud()


def test_rest_after():
    line = "Okay. In 10 minutes I'll remind you. Anything else?"
    check("nothing heard: all of it", V.rest_after(line, 0) == line)
    check("the first sentence heard", V.rest_after(line, 0.6) == "In 10 minutes I'll remind you. Anything else?",
          V.rest_after(line, 0.6))
    check("halfway through the second: from the second",
          V.rest_after(line, 1.5).startswith("In 10 minutes"), V.rest_after(line, 1.5))
    check("all of it heard: nothing", V.rest_after(line, 10) == "")


def test_english_on_the_cloud_is_one_line_of_config():
    via_proxy()
    real = V._config
    V._config = lambda: {**real(), "voice_engines": {"english": ["eleven_turbo", "kokoro", "say"]}}
    try:
        eng = FakeEngine()
        use_engine(eng)
        settings("cloud")
        check("English's engine is now the cloud voice", V.plan("english") == "eleven_turbo")
        mac.say("Timer for 25 minutes.", "english")
        finish_line()
        check("it went to the cloud", CLOUD.hits and CLOUD.hits[-1]["body"]["lang"] == "en")
        check("Kokoro was not used", not eng.calls, eng.calls)
        CLOUD.mode = "status"
        CLOUD.status_body = (502, {"error": "upstream_error"}, {})
        PLAYERS.clear()
        mac.say("Done. That one is set.", "english")
        check("a cloud failure hands the line to Kokoro, the next in the list",
              wait_until(lambda: eng.calls) and eng.calls[0] == "Done.", eng.calls)
        check("not to say", not POPENS, POPENS)
        mac.stop_speaking()
    finally:
        V._config = real
        reset_cloud()


def test_one_connection_for_many_lines():
    via_proxy()
    settings("cloud")
    for line in ("שלום.", "עוד אחד.", "ועוד."):
        mac.say(line, "hebrew")
        finish_line()
    check("three lines, one kept-alive connection", len(CLOUD.ports) == 1, CLOUD.ports)
    CLOUD.ports.clear()
    V.prewarm("hebrew")
    check("prewarm opens it without a request", wait_until(lambda: V._CONN["conn"] is not None)
          and not CLOUD.hits[3:], CLOUD.hits[3:])
    reset_cloud()


def test_a_test_never_reaches_a_real_voice_server():
    os.environ.update(MICMIC_MODE="proxy", MICMIC_PROXY_URL="https://cloud.example.invalid",
                      MICMIC_PROXY_TOKEN=FAKE_TOKEN)
    reset_cloud()
    settings("cloud")
    mac.say("שלום.", "hebrew")
    check("refused before any connection, and say speaks", wait_until(lambda: POPENS)
          and POPENS[-1][-1] == "שלום.")
    from savta.paths import state_dir
    trip = state_dir() / V.TEST_TRIPWIRE
    check("the tripwire says where it tried", trip.exists() and "example.invalid" in trip.read_text())
    trip.unlink()
    no_cloud()


def test_tags_pipeline_in_the_router():
    import savta.router as R
    real_plan = V.plan
    V.plan = lambda lang: "eleven_turbo"
    try:
        u = {"distress": 0.0, "emergency": 0.0, "at_micmic": 0.0, "live_kind": "not_live"}
        check("a read-back is never tagged nor cached",
              R._voice_kw("sending", {**u, "reply_tone": "upbeat", "reply_tone_confidence": 0.9},
                          "english") == {}, R._voice_kw("sending", u, "english"))
        night = time.localtime().tm_hour >= 22 or time.localtime().tm_hour < 6
        kw = R._voice_kw("timer_set", {**u, "reply_tone": "upbeat", "reply_tone_confidence": 0.9},
                         "english", said="Timer set for 10 minutes.",
                         heard="set a timer for ten minutes please")
        check("a success she is happy about: warmly (softly at night), cached",
              kw == {"cache": True, "tag": "softly" if night else "warmly"}, kw)
        kw = R._voice_kw("emergency", u, "english")
        check("a safety alert is calm", kw.get("tag") == "calmly", kw)
        kw = R._voice_kw("chatted", u, "english", gemini_tag="warmly")
        check("Gemini's tag is kept, its line is never cached",
              kw.get("tag") == ("softly" if night else "warmly") and "cache" not in kw, kw)
        kw = R._voice_kw("timer_set", {**u, "reply_tone": "upbeat", "reply_tone_confidence": 0.9},
                         "hebrew")
        check("no tag where none is cleared for the language",
              "tag" not in kw or kw["tag"] in R.voice_tags.tags_for("hebrew"), kw)
        check("the tone question rides only on a feeling cue",
              R._ask_tone("thank you so much!") and not R._ask_tone("open the calendar"))
        os.environ["MICMIC_VOICE_TAGS"] = "off"
        check("kill switch: no tone question, no tag",
              not R._ask_tone("thank you so much!")
              and "tag" not in R._voice_kw("emergency", u, "english"))
        os.environ.pop("MICMIC_VOICE_TAGS")
        check("Gemini is offered the tags only with the cloud voice",
              R._gemini_tags("english") == frozenset({"warmly", "calmly", "softly"}))
    finally:
        V.plan = real_plan
    check("without the cloud voice nothing changes: no kwargs, no tone, no Gemini tags",
          R._voice_kw("emergency", {}, "english") == {} and not R._ask_tone("thank you!")
          and R._gemini_tags("english") is None)


def test_strip_and_split():
    from savta import voice_tags as T
    check("split keeps an allowed leading tag", T.split_tag("[warmly] Hello there.", T.GEMINI_ALLOW)
          == ("warmly", "Hello there."))
    check("and drops any other", T.split_tag("[laughs] Hello [sighs] there.", T.GEMINI_ALLOW)
          == (None, "Hello there."))
    check("strip_tags leaves no bracket", T.strip_tags("[softly] It's late.") == "It's late.")
    check("a tag is never chosen for a language not cleared",
          all(T.choose("timer_set", tone="upbeat", language=l) in T.tags_for(l) | {None}
              for l in ("hebrew", "arabic", "russian")))
    check("never a sigh, a laugh or a whisper", not (T.NEVER_AUTO & T.tags_for("english")))


# ---------------------------------------------------------------- 1.2.4: never worse than `say`

def test_every_language_defaults_to_the_cloud_voice():
    no_cloud()
    settings("cloud")
    cfg = json.loads((ROOT / "micmic.config.json").read_text())["voice_engines"]
    check("config: every language starts with the cloud voice, then say",
          all(v[0] == "eleven_turbo" and v[-1] == "say" for v in cfg.values())
          and set(cfg) == {"english", "hebrew", "arabic", "russian"}, cfg)
    check("config: Kokoro only for English, after the cloud voice",
          cfg["english"] == ["eleven_turbo", "kokoro", "say"]
          and not any("kokoro" in cfg[l] for l in ("hebrew", "arabic", "russian")), cfg)
    check("code default matches", all(V.ENGINE_ORDER[l][0] == "eleven_turbo"
                                      for l in V.ENGINE_ORDER), V.ENGINE_ORDER)
    # Kokoro not downloaded: a cloud failure goes straight to say, nothing loads.
    via_proxy()
    V._STATE.update(engine=None, lang=None, loading=False, current=None)
    CLOUD.mode = "status"
    CLOUD.status_body = (502, {"error": "upstream_error"}, {})
    mac.say("Timer for 25 minutes.", "english")
    check("no Kokoro on this Mac: the Mac's voice says it", wait_until(lambda: POPENS)
          and POPENS[-1][-1] == "Timer for 25 minutes.", POPENS)
    check("and no 380 MB model was loaded or fetched", V._STATE["engine"] is None
          and not V._STATE["loading"])
    reset_cloud()


def test_onboarding_never_downloads_kokoro():
    js = (ROOT / "savta/web/onboarding/onboarding.js").read_text()
    html = (ROOT / "savta/web/index.html").read_text()
    server = (ROOT / "savta/server.py").read_text()
    check("onboarding asks for no voice download", "/api/voice" not in js)
    i = server.index("voice_models.install(")
    check("the server downloads Kokoro only when she clicks it (one call, the install action)",
          server.count("voice_models.install(") == 1
          and 'action == "install"' in server[i - 600:i], server[i - 600:i])
    check("Settings keeps it under 'On-device voice (English)'",
          "On-device voice (English)" in html)


def test_offline_means_say_at_once():
    via_proxy()
    settings("cloud")
    V.ONLINE_PROBE = lambda host: False
    t0 = time.perf_counter()
    mac.say("Opening WhatsApp.", "english")
    took = time.perf_counter() - t0
    check("offline: say speaks the line", POPENS and POPENS[-1][-1] == "Opening WhatsApp.", POPENS)
    check("at once (no connection tried)", took < 0.1 and not CLOUD.hits, (took, CLOUD.hits))
    check("the router asks for no tag offline", V.plan("english") == "say")
    V.ONLINE_PROBE = lambda host: True
    mac.say("Opening WhatsApp.", "english")
    finish_line()
    check("back online: the cloud voice again", len(CLOUD.hits) == 1, len(CLOUD.hits))
    check("the real probe answers without raising", isinstance(V._reachable(), bool))
    reset_cloud()


def test_a_hang_before_any_byte_is_cut_by_the_watchdog():
    """DNS or TLS can hang past any socket timeout: the watchdog hands the line on."""
    via_proxy()
    settings("cloud")
    V.FIRST_AUDIO_S = 0.3
    real = V._take_conn

    def stuck(base, timeout):
        time.sleep(1.2)                  # like a DNS lookup that never comes back
        return real(base, timeout)
    V._take_conn = stuck
    try:
        t0 = time.time()
        mac.say("Your timer is done.", "english")
        check("say takes the whole line on time", wait_until(lambda: POPENS, 1.0)
              and POPENS[-1][-1] == "Your timer is done.", POPENS)
        check("within the deadline, not after the hang", time.time() - t0 < 0.8,
              time.time() - t0)
        time.sleep(1.5)
        check("the late stream is never played", not any(p.played for p in PLAYERS))
        check("said once only", len(POPENS) == 1, POPENS)
        check("and the cloud voice rests", V.plan("english") == "say"
              and V._COOL["why"] == "slow", V._COOL)
    finally:
        V._take_conn = real
        reset_cloud()


def test_stop_mid_stream():
    via_proxy()
    settings("cloud")
    CLOUD.chunk_delay = 0.15
    mac.say("A long answer that keeps going for a while.", "english")
    check("streaming", wait_until(lambda: PLAYERS and PLAYERS[-1].played))
    t0 = time.time()
    check("stop says it stopped something", mac.stop_speaking())
    n = len(PLAYERS[-1].played)
    check("the speaker is cut at once", PLAYERS[-1].stopped)
    check("the worker is done quickly", wait_until(lambda: not V.busy(), 1.0)
          and time.time() - t0 < 1.0, time.time() - t0)
    time.sleep(0.5)
    check("nothing more is played after stop", len(PLAYERS[-1].played) == n)
    check("and the rest is NOT handed to say", not POPENS, POPENS)
    check("a stop is not a failure", V._COOL["fails"] == 0 and V._COOL["until"] < time.time(),
          V._COOL)
    reset_cloud()


def test_two_replies_never_overlap():
    via_proxy()
    settings("cloud")
    # 1) A line replaced while the cloud is still silent: its watchdog must not speak.
    V.FIRST_AUDIO_S = 0.3
    CLOUD.delay = 0.8
    mac.say("First reply.", "english")
    time.sleep(0.1)
    settings("mac")
    mac.say("Second reply.", "english")
    time.sleep(1.2)
    check("only the second reply is spoken", [x[-1] for x in POPENS] == ["Second reply."], POPENS)
    check("and the first's late audio never plays", not any(p.played for p in PLAYERS))
    reset_cloud()
    # 2) A line replaced mid-stream whose stream then breaks: no fallback for it.
    settings("cloud")
    CLOUD.mode, CLOUD.chunk_delay = "cut", 0.2
    mac.say("Old line one. Old line two. Old line three.", "english")
    check("the old line streams", wait_until(lambda: PLAYERS and PLAYERS[-1].played))
    old = PLAYERS[-1]
    settings("mac")
    mac.say("New line.", "english")
    time.sleep(0.8)                       # the old stream is cut meanwhile
    check("the old line's rest never comes back through say",
          [x[-1] for x in POPENS] == ["New line."], POPENS)
    check("the old speaker was stopped", old.stopped)
    reset_cloud()
    # 3) The fallback that just started is what a new line stops.
    settings("cloud")
    CLOUD.mode = "status"
    CLOUD.status_body = (502, {"error": "upstream_error"}, {})
    mac.say("Falls back.", "english")
    check("fell back to say", wait_until(lambda: POPENS))
    proc = mac._SPEAKING.get("proc")
    mac.say("Next.", "english")
    check("a new line stops the fallback's say", proc is not None and not proc.alive)
    reset_cloud()


def test_a_cloud_without_its_key():
    via_proxy()
    settings("cloud")
    CLOUD.mode = "status"
    CLOUD.status_body = (503, {"error": "not_configured", "message": "voice not configured"},
                         {"Retry-After": "3600"})
    t0 = time.time()
    mac.say("Opening Spotify.", "english")
    check("voice not configured: the Mac says the line at once",
          wait_until(lambda: POPENS) and POPENS[-1][-1] == "Opening Spotify."
          and time.time() - t0 < 0.5, (POPENS, time.time() - t0))
    check("and the cloud voice rests an hour", V._COOL["why"] == "off"
          and V._COOL["until"] - time.time() > 3000, V._COOL)
    n = len(CLOUD.hits)
    mac.say("Opening Notes.", "english")
    check("no second request meanwhile", len(CLOUD.hits) == n)
    reset_cloud()


def test_too_long_for_the_cloud_goes_to_say():
    via_proxy()
    settings("cloud")
    line = ("Word " * 130).strip() + "."
    mac.say(line, "english")
    check("a line over 600 characters is not sent", not CLOUD.hits and POPENS, len(CLOUD.hits))
    check("and is not a failure", V._COOL["fails"] == 0)
    reset_cloud()


def test_the_app_never_sends_an_elevenlabs_key():
    via_proxy()
    settings("cloud")
    mac.say("Sent.", "english", tag="warmly")
    finish_line()
    hit = CLOUD.hits[-1]
    check("through MicMic's cloud: a device token, no ElevenLabs key",
          "xi-api-key" not in {k.lower() for k in hit["headers"]}
          and "sk_" not in json.dumps(hit["body"]), hit["headers"])
    check("a test never reads the owner's .env.local", V._own_eleven_key() is None)
    build = (ROOT / "native/build.sh").read_text()
    check("the release build carries no .env.local", 'rm -f "$RES/app/.env.local"' in build)
    reset_cloud()


def test_a_friends_message_in_her_friends_tone():
    via_proxy()
    settings("cloud")
    mac.say("Dana: I got the job!!! Drinks tonight? 🎉", "english", tag="excited",
            tag_after="Dana:")
    finish_line()
    b = CLOUD.hits[-1]["body"]
    check("the tag goes after 'Dana:', MicMic's words stay plain",
          b.get("lead") == "Dana:" and b.get("tag") == "excited"
          and b["text"] == "I got the job!!! Drinks tonight?", b)
    check("her friend's message is never cached", b["cache"] is False, b)
    # Several messages: the tag covers only the last one.
    mac.say("Gal: see you at five. Dana: we won!!", "english", tag="excited", tag_after="Dana:")
    finish_line()
    b = CLOUD.hits[-1]["body"]
    check("with several, only the last message carries it",
          b.get("lead") == "Gal: see you at five. Dana:" and b["text"] == "we won!!", b)
    # Her own key: the tag inside the one text, after the name.
    via_own_key()
    mac.say("Dana: I got the job!!!", "english", tag="excited", tag_after="Dana:")
    finish_line()
    check("direct to ElevenLabs: 'Dana: [excited] I got the job!!!'",
          CLOUD.hits[-1]["body"]["text"] == "Dana: [excited] I got the job!!!",
          CLOUD.hits[-1]["body"])
    no_cloud()
    settings("mac")
    mac.say("Dana: I got the job!!!", "english", tag="excited", tag_after="Dana:")
    check("say reads the whole message, no tag", POPENS[-1][-1] == "Dana: I got the job!!!",
          POPENS[-1:])


def test_the_five_launch_moments():
    from savta import voice_tags as T
    D, N, LATE = 12, 23, 22
    # 1. Calm when she is stressed: any family but read-backs and silence.
    check("1: stressed -> [calmly] on a success, an answer, a can't",
          all(T.choose(d, tone="calm", hour=D) == "calmly"
              for d in ("timer_set", "looked_up", "not_found", "chatted", "need_who")))
    check("1: stressed -> never on her own read-back",
          T.choose("confirm_send", tone="calm") is None and T.choose("sending", tone="calm") is None)
    check("1: stressed signals alone give calm", T.tone_from_signals({"distress": 1.4}) == "calm")
    # Safety confirmations: [calmly] or nothing.
    check("safety confirmations only ever [calmly]",
          {T.choose(d, tone=t, hour=h) for d in ("confirm_call", "confirm_web", "held_dropped",
                                                  "cancelled")
           for t in ("neutral", "warm", "upbeat", "calm", "gentle") for h in (D, N)}
          <= {None, "calmly"})
    # 2. A friend's message.
    check("2: joy -> [excited]", T.incoming("I got the job!!! Drinks tonight?", hour=D) == "excited")
    check("2: a joy emoji", T.incoming("look \U0001F973", hour=D) == "excited")
    check("2: Hebrew joy", T.incoming("מזל טוב! הנכדה נולדה", hour=D, language="hebrew") == "excited")
    check("2: bad news -> [softly]", T.incoming("Dad is in the hospital, call me", hour=D) == "softly")
    check("2: 'passed away' is not 'passed'", T.incoming("Grandma passed away", hour=D) == "softly")
    check("2: a denial is no joy", T.incoming("I didn't get the job!!", hour=D) is None)
    check("2: a plain message: no tag", T.incoming("Dinner at 7", hour=D) is None)
    check("2: at night her friend's joy is read plainly", T.incoming("We won!!!", hour=N) is None)
    check("2: in a hurry: no tag", T.incoming("We won!!!", hour=D, tone="brief") is None)
    # 3. Night.
    check("3: night is 23:00 to 06:00", T.night(23) and T.night(5) and not T.night(22)
          and not T.night(6))
    check("3: at night a success is [softly]", T.choose("timer_set", hour=N) == "softly")
    check("3: [warmly] becomes [softly] at night",
          T.choose("timer_set", tone="warm", hour=N) == "softly")
    check("3: never on safety, faults or read-backs at night",
          T.choose("emergency", hour=N) == "calmly" and T.choose("error", hour=N) is None
          and T.choose("read_notes", hour=N) is None)
    check("3: 22:00 is not night yet", T.choose("timer_set", hour=LATE) is None)
    # 4. Good news, by day.
    check("4: good news by day -> [excited]", T.choose("web_done", hour=D, good_news=True) == "excited")
    check("4: not before 07:00 nor from 21:00",
          T.choose("web_done", hour=6, good_news=True) != "excited"
          and T.choose("web_done", hour=21, good_news=True) is None)
    check("4: never when she is in a hurry or stressed",
          T.choose("web_done", hour=D, good_news=True, tone="brief") is None
          and T.choose("web_done", hour=D, good_news=True, tone="calm") == "calmly")
    check("4: never on a live answer",
          T.choose("web_done", hour=D, good_news=True, live_kind="news") is None)
    check("4: food timers and birthdays are good news",
          T.food("pasta") and T.food("עוגה") and T.birthday("Mom's birthday")
          and not T.food("call the bank"))
    # 5. Good morning.
    check("5: the first reply of the day is [warmly]",
          T.choose("timer_set", hour=8, first_today=True) == "warmly")
    check("5: not for a read-back, not when she is in a hurry",
          T.choose("sending", hour=8, first_today=True) is None
          and T.choose("timer_set", hour=8, first_today=True, tone="brief") is None)
    check("5: not before 05:00", T.choose("timer_set", hour=4, first_today=True) == "softly")
    # The allow-list.
    for lang in ("english", "hebrew", "arabic", "russian"):
        allow = T.tags_for(lang)
        check(f"{lang}: only reliable launch tags",
              allow <= T.LAUNCH_TAGS and not (allow & {"laughs", "applause", "woo", "sings",
                                                         "whispers", "sighs"}), allow)
    check("never chosen automatically: laughs, applause, woo, sings",
          {"laughs", "applause", "woo", "sings"} <= T.NEVER_AUTO)
    check("Gemini's tag outside the list is dropped",
          T.split_tag("[laughs] Ha, good one.") == (None, "Ha, good one."))


def test_good_morning_once_a_day_and_the_router_wiring():
    import savta.router as R
    from savta.paths import state
    real_plan = V.plan
    V.plan = lambda lang: "eleven_turbo"
    real_lt = time.localtime
    fake_hour = [9]
    time_mod = R.time

    class _T:
        def __getattr__(self, k):
            return getattr(time_mod, k)

        @staticmethod
        def localtime(*a):
            t = real_lt(*a)
            return time_mod.struct_time((t.tm_year, t.tm_mon, t.tm_mday, fake_hour[0], 0, 0,
                                         t.tm_wday, t.tm_yday, t.tm_isdst))
    R.time = _T()
    V.time = _T()
    mac.time = _T()
    try:
        try:
            state("voice_day.json").unlink()
        except OSError:
            pass
        u = {"distress": 0.0, "emergency": 0.0, "at_micmic": 0.0, "live_kind": "not_live"}
        first = R._voice_kw("timer_set", u, "english")
        second = R._voice_kw("timer_set", u, "english")
        check("first reply of the day: [warmly]", first.get("tag") == "warmly", first)
        check("the second: no greeting again", "tag" not in second, second)
        fake_hour[0] = 4
        state("voice_day.json").unlink()
        check("before 05:00 the day has not started",
              R._voice_kw("timer_set", u, "english").get("tag") == "softly")
        fake_hour[0] = 10
        kw = R._voice_kw("read_messages", u, "english", incoming=("Dana", "I got the job!!!"))
        check("read_messages: the friend's tone, after her name, never cached",
              kw == {"tag": "excited", "tag_after": "Dana:"}, kw)
        kw = R._voice_kw("read_messages", u, "english", incoming=("Dana", "see you"))
        check("a plain message: no tag, still never cached", kw == {}, kw)
        kw = R._voice_kw("sending", {**u, "reply_tone": "upbeat", "reply_tone_confidence": .9},
                         "english")
        check("her own outgoing message: never a tag", kw == {}, kw)
        kw = R._voice_kw("web_done", u, "english", good_news=True,
                         said="Found it: two tickets for Saturday.", heard="find tickets")
        check("a web task that found it: [excited], never kept (it is about her task)",
              kw == {"tag": "excited"}, kw)
        kw = R._voice_kw("confirm_call", {**u, "distress": 1.6}, "english")
        check("a safety confirmation when she is stressed: [calmly] only",
              kw.get("tag") == "calmly", kw)
        # A reminder going off.
        fake_hour[0] = 18
        check("a pasta timer by day: [excited]",
              mac._fired_tag({"kind": "timer", "text": "pasta"}, "english") == {"tag": "excited"})
        check("a birthday reminder: [excited], once a day",
              mac._fired_tag({"kind": "reminder", "text": "Dana's birthday"}, "english")
              == {"tag": "excited"}
              and mac._fired_tag({"kind": "reminder", "text": "Dana's birthday"}, "english") == {})
        check("any other reminder by day: plain",
              mac._fired_tag({"kind": "reminder", "text": "call the bank"}, "english") == {})
        fake_hour[0] = 23
        check("at night a reminder is [softly], even the pasta",
              mac._fired_tag({"kind": "timer", "text": "pasta"}, "english") == {"tag": "softly"})
    finally:
        R.time = time_mod
        V.time = time_mod
        mac.time = time_mod
        V.plan = real_plan


def test_router_reads_messages_in_the_friends_tone():
    """read_messages, through the router's own branch: the line and its tag kwargs."""
    import savta.router as R
    src = (ROOT / "savta/router.py").read_text()
    i = src.index('return finish("read_messages", spoken,')
    check("the router hands the last message to the voice",
          "voice_incoming=(last[\"who\"], last[\"text\"][:180])" in src[i:i + 200])
    check("finish passes it to _voice_kw", "incoming=incoming" in src)


def test_cloud_voice_end_to_end_against_a_local_proxy_without_its_key():
    """The real proxy (uv run in proxy/), no ELEVENLABS_API_KEY anywhere: a clean 503
    "voice not configured", and the app speaks on the Mac at once and rests the voice."""
    import shutil
    import socket
    import subprocess
    import urllib.request
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    check("uv is here to run the proxy", os.path.exists(uv), uv)
    if not os.path.exists(uv):
        return
    tmp = Path(tempfile.mkdtemp(prefix="voice-e2e-proxy-"))
    (tmp / "empty.env").write_text("")
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ELEVENLABS", "TYPESAFE", "GEMINI", "STRIPE", "MICMIC_"))}
    env.update(MICMIC_PROXY_PORT=str(port), MICMIC_PROXY_HOST="127.0.0.1",
               MICMIC_PROXY_DB=str(tmp / "proxy.db"), MICMIC_PROXY_ENV_FILE=str(tmp / "empty.env"),
               TYPESAFE_API_KEY="tsk-FAKE-only-to-boot", JEV_UPSTREAM_URL="http://127.0.0.1:9",
               ELEVEN_UPSTREAM_URL="http://127.0.0.1:9", TTS_CACHE_DIR=str(tmp / "cache"))
    proc = subprocess.Popen([uv, "run", "--quiet", "python", "-m", "proxy"], cwd=ROOT / "proxy",
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        up = wait_until(lambda: _get_ok(base + "/health") or proc.poll() is not None, 120)
        up = up and proc.poll() is None
        check("the local proxy is up", up, "" if up else proc.stderr.read(2000))
        if not up:
            return
        req = urllib.request.Request(base + "/v1/devices", method="POST",
                                     data=json.dumps({"device_name": "e2e", "app_version": "1.2.4"}).encode(),
                                     headers={"Content-Type": "application/json"})
        token = json.loads(urllib.request.urlopen(req, timeout=5).read())["token"]
        req = urllib.request.Request(base + "/v1/tts", method="POST",
                                     data=json.dumps({"text": "Hello.", "lang": "en"}).encode(),
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            st, body = 200, {}
        except urllib.error.HTTPError as e:
            st, body = e.code, json.loads(e.read() or b"{}")
        check("the proxy answers 503 'voice not configured'",
              st == 503 and body == {"error": "not_configured", "message": "voice not configured"},
              (st, body))
        os.environ.update(MICMIC_MODE="proxy", MICMIC_PROXY_URL=base, MICMIC_PROXY_TOKEN=token)
        os.environ.pop("ELEVENLABS_API_KEY", None)
        reset_cloud()
        settings("cloud")
        t0 = time.time()
        mac.say("Your timer is done.", "english")
        check("the app: the Mac says it at once", wait_until(lambda: POPENS, 2)
              and POPENS[-1][-1] == "Your timer is done." and time.time() - t0 < 1.0,
              (POPENS, time.time() - t0))
        check("and rests the cloud voice for an hour", V._COOL["why"] == "off"
              and V._COOL["until"] - time.time() > 3000, V._COOL)
        check("so the next line is the Mac's straight away", V.plan("english") == "say")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:  # noqa: BLE001
            proc.kill()
        no_cloud()


# ---------------------------------------------------------------- lines kept on this Mac

def test_a_kept_line_plays_without_the_network():
    via_proxy()
    settings("cloud")
    line = "Turning it up."
    mac.say(line, "english", cache=True)
    check("first time: the cloud voice says it", finish_line() and len(CLOUD.hits) == 1,
          CLOUD.hits)
    check("and it is kept, once, in full", wait_until(lambda: len(kept_files()) == 1)
          and V.kept(V.keep_key(line, "english")) == PCM16, kept_files())
    CLOUD.hits.clear(); PLAYERS.clear()
    mac.say(line, "english", cache=True)
    check("the second time it plays from this Mac", finish_line()
          and b"".join(PLAYERS[-1].played).startswith(PCM16)
          and PLAYERS[-1].fmt == "s16", [played_bytes(p) for p in PLAYERS])
    check("with no request at all (no network, no credits)", not CLOUD.hits, CLOUD.hits)
    check("and `say` was not started", not POPENS, POPENS)
    V.ONLINE_PROBE = lambda host: False
    PLAYERS.clear()
    mac.say(line, "english", cache=True)
    check("offline too", finish_line() and not CLOUD.hits and not POPENS, (CLOUD.hits, POPENS))
    V.ONLINE_PROBE = None
    PLAYERS.clear()
    mac.say(line, "english", tag="softly", cache=True)
    finish_line()
    check("another tag is another line: asked for", len(CLOUD.hits) == 1
          and CLOUD.hits[-1]["body"].get("tag") == "softly", CLOUD.hits)
    CLOUD.hits.clear()
    mac.say(line, "hebrew", cache=True)
    finish_line()
    check("another language is another line", len(CLOUD.hits) == 1, CLOUD.hits)
    CLOUD.hits.clear()
    mac.say(line, "english")
    finish_line()
    check("a line not marked as keepable never plays from the cache", len(CLOUD.hits) == 1)
    PLAYERS.clear()
    mac.say(line, "english", cache=True)
    check("stopping a kept line cuts it at once",
          wait_until(lambda: PLAYERS and PLAYERS[-1].played) and mac.stop_speaking()
          and PLAYERS[-1].stopped and wait_until(lambda: not V.busy(), 1.0))
    settings("device")
    CLOUD.hits.clear()
    use_engine(FakeEngine())
    mac.say(line, "english", cache=True)
    time.sleep(0.2)
    mac.stop_speaking()
    check("On-device voice: the kept cloud audio is not used", not any(
        p.fmt == "s16" for p in PLAYERS[-1:]) or not PLAYERS, [p.fmt for p in PLAYERS])
    settings("cloud")
    V._STATE.update(engine=None, lang=None)       # no Kokoro downloaded
    os.environ["MICMIC_CLOUD_VOICE"] = "off"
    POPENS.clear()
    mac.say(line, "english", cache=True)
    check("the kill switch silences the kept audio too: `say`",
          POPENS and POPENS[-1][-1] == line, POPENS)
    os.environ.pop("MICMIC_CLOUD_VOICE")
    reset_cloud()


def test_a_private_line_is_never_kept():
    import savta.router as R
    from savta import voice_tags as T
    pairs = R._code_pairs()
    check("the code's own lines are known as word pairs",
          ("send", "it") in pairs and ("who", "should") in pairs, len(pairs))
    keep = lambda did, said, heard="", names=(): T.keep_audio(  # noqa: E731
        did, said, heard=heard, names=names, code_pairs=pairs,
        code_words=R._code_vocab()[1] | {"calculator"})
    check("a template line may be kept: Turning it up", keep("louder", "Turning it up", "louder"))
    check("Who should I send it to? (her words are MicMic's own here)",
          keep("need_who", "Who should I send it to?", "send it to her please"))
    check("I opened Calculator.", keep("opened_app", "I opened Calculator.", "open calculator"))
    for did, said, why in (
            ("read_messages", "Dana wrote: see you at 5.", "a message read aloud"),
            ("sending", "Sending to Dana Cohen. The message: running late.", "a read-back"),
            ("confirm_send", "Send it to Dana?", "a send"),
            ("described_screen", "Your screen shows a bank statement.", "her screen"),
            ("answered", "It is 1:12 pm.", "a Gemini line"),
            ("chatted", "I am here whenever you need me.", "a Gemini line"),
            ("calling", "Calling Dana.", "a call"),
            ("emergency", "I am calling Gal now.", "an emergency call"),
            ("noted", "Noted: buy milk.", "her note"),
            ("not_found", "I could not find bad bunny in Apple Music.", "her search"),
            ("exhausted", "That is everything I found for who is america.", "her search"),
            ("web_done", "Found two tickets.", "her web task"),
            ("guide_step", "Click the blue button.", "her screen, guided")):
        check(f"never kept: {why} ({did})", not keep(did, said), T.private_line(
            did, said, code_pairs=pairs))
    check("never kept: a contact's name in an otherwise plain line",
          not keep("closed_app", "Closed the chat with Dana.", names=["Dana Cohen"]))
    words = R._code_vocab()[1] | {"calculator"}
    keepw = lambda did, said, heard, names=(): T.keep_audio(  # noqa: E731
        did, said, heard=heard, names=names, code_pairs=pairs, code_words=words)
    check("never kept: a name she said, echoed back (one word of hers)",
          not keepw("need_who", "I don't have Dina in your contacts.", "text dina hi"))
    check("never kept: the same in Hebrew, where the name carries a prefix",
          not keepw("need_who", "אין לי את דינה באנשי הקשר.", "תשלחי לדינה שאני מאחרת"))
    check("never kept: a contact the reply suggests",
          not keepw("need_who", "Did you mean Dana Cohen?", "text her", names=["Dana Cohen"]))
    check("an app's name she said is not hers: I opened Calculator. stays keepable",
          keepw("opened_app", "I opened Calculator.", "open the calculator"))
    check("never kept: an event from her calendar or her mail",
          not keep("confirm_calendar", "Shall I add Dinner at Rosa's to your calendar?"))
    check("never kept: two of her own words in a row",
          T.private_line("playing", "Playing Whenever, Wherever by Shakira in Apple Music.",
                         heard="play whenever wherever by shakira", code_pairs=pairs)
          == "her_words")
    # Through the router, as a reply is spoken.
    via_proxy()
    settings("cloud")
    u = {"distress": 0.0, "emergency": 0.0, "at_micmic": 0.0, "live_kind": "not_live"}
    kw = R._voice_kw("sending", u, "english", said="Sending to Dana Cohen. The message: hi.",
                     heard="text dana hi", detail={"to": "Dana Cohen"})
    check("router: a read-back gets no cache", "cache" not in kw, kw)
    kw = R._voice_kw("timer_set", u, "english", said="Timer set for 10 minutes, Dana.",
                     heard="ten minute timer", detail={"who": "Dana"})
    check("router: a name from the turn gets no cache", "cache" not in kw, kw)
    kw = R._voice_kw("need_who", u, "english", said="Who should I send it to?",
                     heard="send it to")
    check("router: a template line is kept", kw.get("cache") is True, kw)
    kw = R._voice_kw("need_who", u, "english", said="I don't have Dina. Did you mean Dana Cohen?",
                     heard="text dina", detail={"said": "Dina", "suggest": "Dana Cohen"})
    check("router: the names in a reply's detail keep it from being kept", "cache" not in kw, kw)
    mac.say("Sending to Dana Cohen. The message: running late.", "english",
            **R._voice_kw("sending", u, "english",
                          said="Sending to Dana Cohen. The message: running late.",
                          heard="tell dana running late", detail={"to": "Dana Cohen"}))
    finish_line()
    check("a private line is said by the cloud voice but never written to disk",
          CLOUD.hits and CLOUD.hits[-1]["body"]["cache"] is False and not kept_files()
          and V.KEEP_STATS["stored"] == 0, (kept_files(), V.KEEP_STATS))
    mac.say("Dana: I got the job!", "english", tag="excited", tag_after="Dana:", cache=True)
    finish_line()
    check("a friend's message after her name is never kept, even if asked",
          not kept_files(), kept_files())
    reset_cloud()


def test_the_kept_lines_stay_under_their_cap():
    clear_kept()
    real = V.KEEP_MAX_BYTES
    entry = len(PCM16) + V.KEEP_HEAD.size
    V.KEEP_MAX_BYTES = 4 * entry + 10            # room for three, and the spare
    try:
        keys = [V.keep_key(f"Line number {i}.", "english") for i in range(6)]
        t = time.time() - 100
        for i, k in enumerate(keys):
            check(f"line {i} kept", V.put_kept(k, PCM16))
            os.utime(V.keep_dir() / f"{k}.pcm", (t + i, t + i))
            if i == 3:
                # Line 1 is heard again: it is now the most recently heard.
                check("an old line heard again is a hit", V.kept(keys[1]) == PCM16)
                os.utime(V.keep_dir() / f"{keys[1]}.pcm", (t + 3.5, t + 3.5))
        total = sum(p.stat().st_size for p in V.keep_dir().iterdir()
                    if p.suffix in (".pcm", ".spare"))
        check("the folder never holds more than the cap", total <= V.KEEP_MAX_BYTES,
              (total, V.KEEP_MAX_BYTES))
        have = {k for k in keys if V.kept(k)}
        check("the least recently heard went first, the one heard again stayed",
              have == {keys[1], keys[4], keys[5]}, sorted(keys.index(k) for k in have))
        check("50 MB is the real cap", real == 50 * 1024 * 1024)
        src = (ROOT / "savta" / "voice.py").read_text()
        check("eviction deletes nothing (MicMic has no delete path)", not any(
            bad in src for bad in ("os.remove", "os.unlink", ".unlink(", "rmtree", "shutil.move")))
    finally:
        V.KEEP_MAX_BYTES = real
        clear_kept()


def test_a_corrupt_kept_file_is_ignored():
    via_proxy()
    settings("cloud")
    line = "Turning it down."
    k = V.keep_key(line, "english")
    p = V.keep_dir() / f"{k}.pcm"
    p.write_bytes(b"not audio at all")
    check("garbage is a miss, not a crash", V.kept(k) is None and V.KEEP_STATS["ignored"] == 1)
    V.put_kept(k, PCM16)
    good = p.read_bytes()
    p.write_bytes(good[:-100])                   # cut short
    check("a file cut short is a miss", V.kept(k) is None)
    p.write_bytes(good[:-2] + b"\x00\x01")      # same length, other audio
    check("a file whose audio changed is a miss", V.kept(k) is None)
    mac.say(line, "english", cache=True)
    check("the line goes to the cloud voice instead", finish_line() and len(CLOUD.hits) == 1)
    check("and its good audio is written over the bad file", V.kept(k) == PCM16)
    reset_cloud()


def test_a_stopped_line_is_not_kept_half_way():
    via_proxy()
    settings("cloud")
    CLOUD.chunk_delay = 0.15
    mac.say("A line she stops in the middle.", "english", cache=True)
    check("streaming", wait_until(lambda: PLAYERS and PLAYERS[-1].played))
    mac.stop_speaking()
    time.sleep(0.8)
    check("a stopped line is not kept", not kept_files(), kept_files())
    reset_cloud()
    settings("cloud")
    CLOUD.mode, CLOUD.chunk_delay = "cut", 0.05
    mac.say("A line whose stream is cut.", "english", cache=True)
    time.sleep(1.0)
    check("a cut stream is not kept", not kept_files(), kept_files())
    reset_cloud()
    settings("cloud")
    V.FIRST_AUDIO_S = 0.3
    CLOUD.delay = 0.8
    mac.say("A line that came too late.", "english", cache=True)
    time.sleep(1.4)
    check("a line handed to `say` by the watchdog is not kept", not kept_files(), kept_files())
    reset_cloud()


def _get_ok(url: str) -> bool:
    import urllib.request
    try:
        return urllib.request.urlopen(url, timeout=1).status == 200
    except Exception:  # noqa: BLE001
        return False


TESTS = [test_prepare, test_normalize_en, test_chunks, test_lexicon_and_phonemes,
         test_mac_voice_when_chosen, test_natural_voice_speaks_and_stops,
         test_new_line_replaces_old, test_stop_while_synthesising,
         test_failure_falls_back_to_say, test_other_languages_without_the_cloud_stay_on_say,
         test_not_loaded_yet_uses_say_and_loads, test_repeated_line_is_cached,
         test_download, test_download_seam, test_real_catalog_is_pinned,
         test_settings_validation,
         test_cloud_voice_speaks_hebrew_through_micmic_cloud,
         test_cloud_tag_and_cache_reach_only_the_cloud, test_own_key_goes_to_elevenlabs_directly,
         test_on_device_keeps_it_on_the_mac, test_out_of_characters_falls_back_and_pauses,
         test_an_old_cloud_or_a_failing_one_rests_the_voice,
         test_no_audio_in_time_goes_to_the_next_engine, test_a_cut_stream_finishes_on_the_mac,
         test_rest_after, test_english_on_the_cloud_is_one_line_of_config,
         test_one_connection_for_many_lines, test_a_test_never_reaches_a_real_voice_server,
         test_tags_pipeline_in_the_router, test_strip_and_split,
         test_every_language_defaults_to_the_cloud_voice, test_onboarding_never_downloads_kokoro,
         test_offline_means_say_at_once, test_a_hang_before_any_byte_is_cut_by_the_watchdog,
         test_stop_mid_stream, test_two_replies_never_overlap, test_a_cloud_without_its_key,
         test_too_long_for_the_cloud_goes_to_say, test_the_app_never_sends_an_elevenlabs_key,
         test_a_friends_message_in_her_friends_tone, test_the_five_launch_moments,
         test_good_morning_once_a_day_and_the_router_wiring,
         test_router_reads_messages_in_the_friends_tone,
         test_cloud_voice_end_to_end_against_a_local_proxy_without_its_key,
         test_a_kept_line_plays_without_the_network, test_a_private_line_is_never_kept,
         test_the_kept_lines_stay_under_their_cap, test_a_corrupt_kept_file_is_ignored,
         test_a_stopped_line_is_not_kept_half_way]

if __name__ == "__main__":
    for t in TESTS:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(t.__name__ + " ran", False, repr(e))
    mac.stop_speaking()
    from savta.paths import state_dir as _sd
    check("no test reached a real voice server", not (_sd() / V.TEST_TRIPWIRE).exists())
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
