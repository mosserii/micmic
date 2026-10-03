"""POST /v1/tts against a fake ElevenLabs on localhost. Nothing here reaches ElevenLabs,
and nothing is played: the audio is bytes compared with bytes."""
from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FAKE_ELEVEN_KEY

PCM = bytes(range(256)) * 40          # 10,240 bytes of "audio", recognisable in a dump
SAID = "SAID-OUT-LOUD Sending Dana: I'm running late."


class FakeEleven:
    """Streams PCM back in chunks, the way /v1/text-to-speech/{id}/stream does."""

    def __init__(self):
        self.mode = "ok"
        self.gap = 0.0                 # seconds between the first chunk and the rest
        self.hits: list[dict] = []
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                up.hits.append({"path": self.path, "headers": dict(self.headers),
                                "body": json.loads(body)})
                if up.mode == "error401":
                    return self._plain(401, json.dumps(
                        {"detail": f"invalid key {FAKE_ELEVEN_KEY} for SAID-OUT-LOUD"}).encode())
                if up.mode == "busy429":
                    return self._plain(429, b'{"detail":"too_many_concurrent_requests"}')
                if up.mode == "bad422":
                    return self._plain(422, b'{"detail":"bad text SAID-OUT-LOUD"}')
                if up.mode == "hang":
                    time.sleep(3)
                    return self._plain(200, PCM)
                self.send_response(200)
                self.send_header("Content-Type", "audio/pcm")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("character-cost", "3")
                self.send_header("X-Secret-Echo", FAKE_ELEVEN_KEY)
                self.end_headers()
                parts = [PCM[i:i + 2048] for i in range(0, len(PCM), 2048)]
                for i, part in enumerate(parts):
                    if up.mode == "cut" and i == 2:
                        self.wfile.flush()
                        self.connection.shutdown(2)    # hangs up mid-stream
                        return
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                    if i == 0 and up.gap:
                        time.sleep(up.gap)
                self.wfile.write(b"0\r\n\r\n")

            def _plain(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def eleven():
    e = FakeEleven()
    yield e
    e.close()


@pytest.fixture
def tp(make_proxy, eleven):
    """A proxy wired to the fake ElevenLabs."""
    def make(**over):
        base = dict(eleven_key=FAKE_ELEVEN_KEY, eleven_upstream=f"http://127.0.0.1:{eleven.port}",
                    tts_first_byte_timeout=1.0, tts_timeout=3.0)
        base.update(over)
        return make_proxy(**base)
    return make


def say(p, token, text=SAID, **kw):
    body = {"text": text, "lang": "en", **kw}
    return p.request("POST", "/v1/tts", body, token=token)


# ---------------------------------------------------------------- the happy path

def test_streams_pcm_from_eleven_turbo(tp, eleven):
    p = tp()
    tok = p.mint("free")
    status, _, raw, headers = say(p, tok, "Sent to Dana.", tag="warmly")
    assert status == 200
    assert raw == PCM
    assert headers["X-Audio-Format"] == "pcm_s16le_24000"
    assert headers["Transfer-Encoding"] == "chunked"
    assert headers["X-Cache"] == "miss"
    hit = eleven.hits[-1]
    assert hit["path"] == "/v1/text-to-speech/cgSgspJ2msm6clMCkdW9/stream?output_format=pcm_24000"
    assert hit["headers"]["xi-api-key"] == FAKE_ELEVEN_KEY
    assert hit["body"] == {"text": "[warmly] Sent to Dana.", "model_id": "eleven_v4_turbo",
                           "language_code": "en"}
    # No upstream header comes back to the app (one echoes the key here).
    assert "X-Secret-Echo" not in headers and "character-cost" not in headers


def test_first_audio_is_relayed_before_the_render_ends(tp, eleven):
    p = tp()
    tok = p.mint("free")
    eleven.gap = 0.6
    c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=5)
    t0 = time.monotonic()
    c.request("POST", "/v1/tts", body=json.dumps({"text": "Alright.", "lang": "en"}),
              headers={"Authorization": f"Bearer {tok}"})
    r = c.getresponse()
    first = r.read1(65536)
    t_first = time.monotonic() - t0
    rest = r.read()
    assert first and t_first < 0.5, t_first
    assert first + rest == PCM
    c.close()


def test_meter_counts_characters_including_the_tag(tp):
    p = tp()
    tok = p.mint("free")
    say(p, tok, "Sent.", tag="warmly")
    _, acct, _, _ = p.request("GET", "/v1/account", token=tok)
    assert acct["usage"]["tts"] == {"used": len("[warmly] Sent."), "limit": 15000,
                                     "scope": "total"}


# ---------------------------------------------------------------- what may go upstream

@pytest.mark.parametrize("body,code", [
    ({"text": "Hi.", "tag": "laughs"}, "tag_not_allowed"),
    ({"text": "Hi.", "tag": "[shouts] get out"}, "tag_not_allowed"),
    ({"text": "Hi.", "lang": "fr"}, "bad_language"),
    ({"text": "Hi.", "voice": "someone-else"}, "voice_not_allowed"),
    ({"text": "x" * 601}, "text_too_long"),
    ({"text": "   "}, "bad_request"),
    ({"text": 5}, "bad_request"),
    ({"text": "Hi.", "cache": "yes"}, "bad_request"),
])
def test_refuses_what_micmic_never_sends(tp, eleven, body, code):
    p = tp()
    tok = p.mint("free")
    status, data, _, _ = p.request("POST", "/v1/tts", {"lang": "en", **body}, token=tok)
    assert (status, data) == (400, {"error": code})
    assert not eleven.hits


def test_control_characters_are_not_sent(tp, eleven):
    p = tp()
    tok = p.mint("free")
    say(p, tok, "Line one.\nLine\x00 two.\x07")
    assert eleven.hits[-1]["body"]["text"] == "Line one. Line two."


def test_needs_a_token(tp, eleven):
    p = tp()
    status, _, _, _ = p.request("POST", "/v1/tts", {"text": "Hi."})
    assert status == 401 and not eleven.hits


def test_kill_switch(tp, eleven):
    p = tp(tts_enabled=False)
    tok = p.mint("pro")
    status, data, _, headers = say(p, tok)
    assert (status, data) == (503, {"error": "tts_disabled"})
    assert headers.get("Retry-After") == "600"
    assert not eleven.hits


def test_without_a_key(tp, eleven):
    p = tp(eleven_key=None)
    tok = p.mint("pro")
    status, data, _, headers = say(p, tok)
    # A clean, final answer the app knows: it speaks on the Mac and rests the voice.
    assert (status, data) == (503, {"error": "not_configured",
                                    "message": "voice not configured"})
    assert headers.get("Retry-After") == "3600"
    assert not eleven.hits
    # Nothing was metered for it.
    _, acct, _, _ = p.request("GET", "/v1/account", token=tok)
    assert acct["usage"]["tts"]["used"] == 0


def test_a_friends_message_puts_the_tag_after_her_name(tp, eleven):
    p = tp()
    tok = p.mint("free")
    status, _, raw, _ = say(p, tok, "I got the job!!! Drinks tonight?", tag="excited",
                            lead="Dana:", cache=False)
    assert status == 200 and raw == PCM
    assert eleven.hits[-1]["body"]["text"] == "Dana: [excited] I got the job!!! Drinks tonight?"
    _, acct, _, _ = p.request("GET", "/v1/account", token=tok)
    assert acct["usage"]["tts"]["used"] == len("Dana: [excited] I got the job!!! Drinks tonight?")


def test_a_lead_without_a_tag_is_just_the_start_of_the_line(tp, eleven):
    p = tp()
    tok = p.mint("free")
    say(p, tok, "see you at five.", lead="Gal:")
    assert eleven.hits[-1]["body"]["text"] == "Gal: see you at five."


@pytest.mark.parametrize("body,code", [
    ({"text": "Hi.", "tag": "excited", "lead": 7}, "bad_request"),
    ({"text": "Hi.", "tag": "apologetic"}, "tag_not_allowed"),
    ({"text": "x" * 300, "tag": "excited", "lead": "y" * 301}, "text_too_long"),
])
def test_refuses_a_bad_lead_or_a_retired_tag(tp, eleven, body, code):
    p = tp()
    tok = p.mint("free")
    status, data, _, _ = p.request("POST", "/v1/tts", {"lang": "en", **body}, token=tok)
    assert (status, data) == (400, {"error": code})
    assert not eleven.hits


def test_the_lead_is_part_of_the_cache_key():
    from proxy.tts import Cache
    c = Cache(None, 0)
    base = {"voice_id": "v", "tag": "excited", "lang": "en", "text": "we won!!"}
    assert c.ids("m", {**base, "lead": "Dana:"})[0] != c.ids("m", {**base, "lead": "Gal:"})[0]
    # A line without a lead keeps the id it always had (caches made before stay valid).
    assert c.ids("m", base)[0] == c.ids("m", {**base, "lead": None})[0]


# ---------------------------------------------------------------- caps

def test_free_pool_is_for_life(tp, eleven):
    p = tp(free_tts_chars=20)
    tok = p.mint("free")
    assert say(p, tok, "Timer for 25 min.")[0] == 200          # 17
    status, data, _, headers = say(p, tok, "Done.")              # 17 + 5 > 20
    assert status == 429
    assert data == {"error": "tts_limit", "scope": "total", "limit": 20, "resets_at": None}
    assert "Retry-After" not in headers
    assert len(eleven.hits) == 1
    p.clock.now = p.clock.now.replace(day=28)
    assert say(p, tok, "Done.")[0] == 429                        # never refilled


def test_pro_month_and_day(tp):
    p = tp(pro_monthly_tts_chars=30, pro_daily_tts_chars=12, tts_cache_max_bytes=0)
    tok = p.mint("pro")
    assert say(p, tok, "Alright.")[0] == 200                     # 8
    status, data, _, headers = say(p, tok, "Sent.")              # 13 > 12 a day
    assert status == 429 and data["scope"] == "day"
    assert data["resets_at"] == "2026-09-25T00:00:00Z" and int(headers["Retry-After"]) > 0
    p.clock.now = p.clock.now.replace(day=25)
    assert say(p, tok, "Alright.")[0] == 200                     # 16 this month
    p.clock.now = p.clock.now.replace(day=26)
    assert say(p, tok, "Alright.")[0] == 200                     # 24
    p.clock.now = p.clock.now.replace(day=27)
    status, data, _, _ = say(p, tok, "Alright.")                 # 32 > 30 a month
    assert status == 429 and data["scope"] == "month"
    assert data["resets_at"] == "2026-10-01T00:00:00Z"
    p.clock.now = p.clock.now.replace(month=10, day=1)
    assert say(p, tok, "Alright.")[0] == 200                     # a new month


def test_a_refused_render_is_given_back(tp, eleven):
    p = tp()
    tok = p.mint("free")
    for mode, want in (("error401", (502, {"error": "upstream_error"})),
                       ("busy429", (503, {"error": "upstream_busy"})),
                       ("bad422", (400, {"error": "upstream_rejected_request"}))):
        eleven.mode = mode
        status, data, raw, _ = say(p, tok)
        assert (status, data) == want
        assert FAKE_ELEVEN_KEY.encode() not in raw and b"SAID" not in raw
    _, acct, _, _ = p.request("GET", "/v1/account", token=tok)
    assert acct["usage"]["tts"]["used"] == 0


def test_no_audio_in_time_is_a_timeout(tp, eleven):
    p = tp(tts_first_byte_timeout=0.5)
    tok = p.mint("free")
    eleven.mode = "hang"
    t0 = time.monotonic()
    assert say(p, tok)[:2] == (504, {"error": "upstream_timeout"})
    assert time.monotonic() - t0 < 2.5


def test_a_stream_cut_midway_is_not_finished(tp, eleven):
    p = tp()
    tok = p.mint("free")
    eleven.mode = "cut"
    c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=5)
    c.request("POST", "/v1/tts", body=json.dumps({"text": "Sent.", "lang": "en"}),
              headers={"Authorization": f"Bearer {tok}"})
    r = c.getresponse()
    assert r.status == 200
    with pytest.raises(http.client.IncompleteRead):
        r.read()
    c.close()
    # ElevenLabs billed the part it rendered: the characters stay counted.
    _, acct, _, _ = p.request("GET", "/v1/account", token=tok)
    assert acct["usage"]["tts"]["used"] == len("Sent.")


# ---------------------------------------------------------------- the cache

def test_a_common_line_is_served_from_the_cache(tp, eleven):
    p = tp()
    a, b = p.mint("free"), p.mint("pro")
    assert say(p, a, "Alright.")[3]["X-Cache"] == "miss"         # seen once: not stored
    assert say(p, b, "Alright.")[3]["X-Cache"] == "miss"         # twice: stored
    status, _, raw, headers = say(p, a, "Alright.")
    assert (status, raw, headers["X-Cache"]) == (200, PCM, "hit")
    assert len(eleven.hits) == 2
    # A hit costs nothing upstream, so it costs her nothing either.
    _, acct, _, _ = p.request("GET", "/v1/account", token=a)
    assert acct["usage"]["tts"]["used"] == len("Alright.")
    # Another tag, language or voice is another line.
    assert say(p, a, "Alright.", tag="softly")[3]["X-Cache"] == "miss"
    assert say(p, a, "Alright.", lang="he")[3]["X-Cache"] == "miss"


def test_the_cache_does_not_say_what_was_said(tp):
    p = tp()
    tok = p.mint("free")
    for _ in range(3):
        say(p, tok, "Timer for 25 minutes.")
    files = list(p.cfg.tts_cache_dir.glob("*/*.pcm"))
    assert len(files) == 1
    data = files[0].read_bytes()
    assert b"Timer" not in data and PCM[:64] not in data
    assert "Timer" not in str(files[0])


def test_a_private_line_is_never_stored(tp, eleven):
    p = tp()
    tok = p.mint("free")
    for _ in range(3):
        assert say(p, tok, SAID, cache=False)[3]["X-Cache"] == "miss"
    assert len(eleven.hits) == 3
    assert not list(p.cfg.tts_cache_dir.glob("*/*.pcm"))
    # and leaves no trace of having been asked for
    assert p.store._conn().execute("SELECT COUNT(*) FROM tts_seen").fetchone()[0] == 0


def test_a_damaged_file_is_a_miss(tp, eleven):
    p = tp()
    tok = p.mint("free")
    for _ in range(2):
        say(p, tok, "Alright.")
    f = next(p.cfg.tts_cache_dir.glob("*/*.pcm"))
    f.write_bytes(f.read_bytes()[:-100])
    status, _, raw, headers = say(p, tok, "Alright.")
    assert (status, raw, headers["X-Cache"]) == (200, PCM, "miss")


def test_the_cache_keeps_to_its_size(tp):
    p = tp(tts_cache_max_bytes=3 * (len(PCM) + 20) + 10, tts_cache_min_seen=1)
    tok = p.mint("pro")
    for i in range(6):
        say(p, tok, f"Line {i}.")
        time.sleep(0.01)
    files = list(p.cfg.tts_cache_dir.glob("*/*.pcm"))
    assert 1 <= len(files) <= 3
    assert p.srv.tts_cache.size == sum(f.stat().st_size for f in files)


def test_a_cut_render_is_not_cached(tp, eleven):
    p = tp(tts_cache_min_seen=1)
    tok = p.mint("free")
    eleven.mode = "cut"
    c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=5)
    c.request("POST", "/v1/tts", body=json.dumps({"text": "Okay.", "lang": "en"}),
              headers={"Authorization": f"Bearer {tok}"})
    r = c.getresponse()
    with pytest.raises(http.client.IncompleteRead):
        r.read()
    c.close()
    assert not list(p.cfg.tts_cache_dir.glob("*/*.pcm"))


def test_upstream_connection_is_reused(tp, eleven):
    p = tp()
    tok = p.mint("pro")
    ports = set()
    orig = eleven.srv.RequestHandlerClass.do_POST

    def spy(self):
        ports.add(self.client_address[1])
        return orig(self)
    eleven.srv.RequestHandlerClass.do_POST = spy
    for i in range(3):
        assert say(p, tok, f"Line {i}.")[0] == 200
    assert len(ports) == 1
