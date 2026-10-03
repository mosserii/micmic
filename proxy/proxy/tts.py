"""POST /v1/tts: MicMic's spoken replies, voiced by ElevenLabs Eleven v4 Turbo.

    {"text": "Timer for 25 minutes.", "tag": null, "lang": "en", "voice": "jessica",
     "cache": true}
    -> 200, 24 kHz mono 16-bit little-endian PCM, streamed as it arrives
       (Transfer-Encoding: chunked; X-Audio-Format: pcm_s16le_24000; X-Cache: hit|miss)

    Optional "lead" (with a tag): words read BEFORE the tag, so the tag covers only
    "text". A friend's message: {"lead": "Dana:", "tag": "excited", "text": "I got the
    job!!!"} is voiced as "Dana: [excited] I got the job!!!".
    No ELEVENLABS_API_KEY on the server: 503 {"error": "not_configured", "message":
    "voice not configured"}; the app then speaks on the Mac and rests the cloud voice.

The handler side lives in app.py (_tts); this file holds what it needs:

  * check()          the request, validated. Only MicMic's own tags, languages and
                     voices go upstream; the text is bounded and has no control bytes.
  * ElevenUpstream   keep-alive connections to api.elevenlabs.io, so a turn does not pay
                     a TLS handshake (~110 ms measured) before the first audio.
  * Cache            rendered lines on the volume, LRU by size. A line is stored only
                     once tts_cache_min_seen requests have asked for it, and only when
                     the app allowed it (it never allows a read-back of her messages,
                     notes or screen). Each file is named by a keyed hash of the request
                     and encrypted with a key stream derived from the words themselves,
                     so the disk does not say what was said: reading a file back needs
                     the exact sentence (plus TTS_CACHE_SECRET when it is set).

Privacy: the text is never logged, not even on error, and neither is the audio. The log
line says the route, the status, the time, the token's hash prefix and, for a render,
hit or miss and the character count.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import queue
import socket
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

# The tags MicMic's own code may choose (savta/voice_tags.py LAUNCH_TAGS). Anything else
# is refused, so a leaked device token cannot steer the voice with free-form directions.
ALLOWED_TAGS = frozenset({"warmly", "calmly", "softly", "excited"})
LANGS = frozenset({"en", "he", "ar", "ru"})
OUTPUT_FORMAT = "pcm_24000"
AUDIO_HEADER = "pcm_s16le_24000"
MAX_BODY = 8 * 1024
# A render longer than this is not a MicMic reply (600 characters is about 45 s).
MAX_AUDIO = 8 * 1024 * 1024


class BadRequest(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _clean(text: str) -> str:
    # Control characters (and the bidi overrides among format characters) are not
    # speech; a newline is a pause, so it becomes a space.
    text = "".join(" " if ch in "\r\n\t" else ch for ch in text
                   if ch in "\r\n\t" or unicodedata.category(ch) != "Cc")
    return " ".join(text.split())


def check(body: dict, cfg) -> dict:
    """The request as it may go upstream, or BadRequest(error code)."""
    text = body.get("text")
    if not isinstance(text, str):
        raise BadRequest("bad_request")
    text = _clean(text)
    if not text:
        raise BadRequest("bad_request")
    lead = body.get("lead")
    if lead in ("", None):
        lead = None
    elif not isinstance(lead, str):
        raise BadRequest("bad_request")
    else:
        lead = _clean(lead) or None
    tag = body.get("tag")
    if tag in ("", None):
        tag = None
    elif not isinstance(tag, str) or tag.strip().lower() not in ALLOWED_TAGS:
        raise BadRequest("tag_not_allowed")
    else:
        tag = tag.strip().lower()
    lang = body.get("lang", "en")
    if not isinstance(lang, str) or lang not in LANGS:
        raise BadRequest("bad_language")
    voice = body.get("voice") or next(iter(cfg.tts_voices))
    if not isinstance(voice, str) or voice.lower() not in cfg.tts_voices:
        raise BadRequest("voice_not_allowed")
    voice = voice.lower()
    cache = body.get("cache", True)
    if not isinstance(cache, bool):
        raise BadRequest("bad_request")
    # What ElevenLabs reads: the tag is a direction, never words. With a lead it sits
    # between the lead and the text; without a tag a lead is just the start of the line.
    if tag and lead:
        upstream = f"{lead} [{tag}] {text}"
    elif tag:
        upstream = f"[{tag}] {text}"
    else:
        upstream = f"{lead} {text}" if lead else text
    if len(upstream) > cfg.tts_max_chars + (len(tag) + 3 if tag else 0):
        raise BadRequest("text_too_long")
    return {"text": text, "lead": lead if tag else None, "tag": tag, "lang": lang,
            "voice": voice, "voice_id": cfg.tts_voices[voice], "cache": cache,
            "upstream_text": upstream}


# ---------------------------------------------------------------- the cache

class Cache:
    """Rendered lines on disk. get()/put() never raise: a broken cache is a miss."""

    MAGIC = b"MMT1"

    def __init__(self, root: Path | None, max_bytes: int, secret: str = ""):
        self.root = Path(root) if root else None
        self.max_bytes = max_bytes
        self.secret = secret.encode()
        self.lock = threading.Lock()
        self.size = 0
        self.hits = self.misses = self.stored = 0
        if not self.enabled:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            self.size = sum(p.stat().st_size for p in self.root.glob("*/*.pcm"))
        except OSError:
            self.root = None

    @property
    def enabled(self) -> bool:
        return self.root is not None and self.max_bytes > 0

    def ids(self, model: str, req: dict) -> tuple[str, bytes]:
        """(file id, key stream seed) for one request. Both come from the words, the tag,
        the language, the voice and the model; neither can be turned back into them."""
        parts = [model, req["voice_id"], req["tag"] or "", req["lang"], req["text"]]
        if req.get("lead"):
            parts.append(req["lead"])
        material = json.dumps(parts, ensure_ascii=False).encode()
        key_id = hmac.new(b"micmic-tts-id-v1" + self.secret, material,
                          hashlib.sha256).hexdigest()
        seed = hmac.new(b"micmic-tts-key-v1" + self.secret, material,
                        hashlib.sha256).digest()
        return key_id, seed

    def _path(self, key_id: str) -> Path:
        return self.root / key_id[:2] / f"{key_id}.pcm"

    @staticmethod
    def _xor(data: bytes, seed: bytes) -> bytes:
        stream = hashlib.shake_256(seed).digest(len(data))
        n = len(data)
        return (int.from_bytes(data, "little") ^ int.from_bytes(stream, "little")).to_bytes(
            n, "little")

    def get(self, key_id: str, seed: bytes) -> bytes | None:
        if not self.enabled:
            return None
        p = self._path(key_id)
        try:
            raw = p.read_bytes()
        except OSError:
            self.misses += 1
            return None
        pcm = self._xor(raw[20:], seed) if raw[:4] == self.MAGIC else b""
        if not pcm or not hmac.compare_digest(raw[4:20], self._mac(seed, pcm)):
            self._drop(p)                     # cut short or not ours: a miss
            self.misses += 1
            return None
        try:
            os.utime(p)                       # most recently used
        except OSError:
            pass
        self.hits += 1
        return pcm

    @staticmethod
    def _mac(seed: bytes, pcm: bytes) -> bytes:
        return hmac.new(seed, pcm, hashlib.sha256).digest()[:16]

    def put(self, key_id: str, seed: bytes, pcm: bytes) -> None:
        if not self.enabled or not pcm or len(pcm) > MAX_AUDIO:
            return
        p = self._path(key_id)
        data = self.MAGIC + self._mac(seed, pcm) + self._xor(pcm, seed)
        try:
            p.parent.mkdir(exist_ok=True)
            tmp = p.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_bytes(data)
            old = p.stat().st_size if p.exists() else 0
            os.replace(tmp, p)
        except OSError:
            return
        with self.lock:
            self.size += len(data) - old
            self.stored += 1
            if self.size > self.max_bytes:
                self._evict()

    def _drop(self, p: Path) -> None:
        try:
            n = p.stat().st_size
            p.unlink()
            with self.lock:
                self.size -= n
        except OSError:
            pass

    def _evict(self) -> None:
        """Oldest-used first, down to 90% of the budget. Called with the lock held."""
        try:
            files = sorted(((f.stat().st_mtime, f.stat().st_size, f)
                            for f in self.root.glob("*/*.pcm")), key=lambda x: x[0])
        except OSError:
            return
        target = int(self.max_bytes * 0.9)
        for _, n, f in files:
            if self.size <= target:
                break
            try:
                f.unlink()
                self.size -= n
            except OSError:
                pass


# ---------------------------------------------------------------- upstream

class UpstreamTimeout(Exception):
    pass


class ElevenUpstream:
    """Keep-alive connections to ElevenLabs, and one streamed render at a time on each."""

    def __init__(self, url: str, first_byte_timeout: float, total_timeout: float,
                 size: int = 4):
        u = urlsplit(url)
        self.https = u.scheme == "https"
        self.host = u.hostname or ""
        self.port = u.port or (443 if self.https else 80)
        self.base = u.path.rstrip("/")
        self.first_byte_timeout = first_byte_timeout
        self.total_timeout = total_timeout
        self._idle: queue.LifoQueue = queue.LifoQueue(maxsize=size)

    def _new(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.first_byte_timeout)

    def open(self, voice_id: str, payload: bytes, key: str):
        """(status, response, connection). The response's headers are in; its body is
        the caller's to read (stream()) or drain, then release(). The socket is kept on
        the response: getresponse() drops conn.sock when the far end says it will close,
        while the body goes on arriving on it."""
        path = f"{self.base}/v1/text-to-speech/{voice_id}/stream?output_format={OUTPUT_FORMAT}"
        headers = {"xi-api-key": key, "Content-Type": "application/json",
                   "Accept": "application/octet-stream", "Connection": "keep-alive"}
        for attempt in (0, 1):
            try:
                conn, reused = self._idle.get_nowait(), True
            except queue.Empty:
                conn, reused = self._new(), False
            try:
                conn.timeout = self.first_byte_timeout
                if conn.sock is not None:
                    conn.sock.settimeout(self.first_byte_timeout)
                conn.request("POST", path, body=payload, headers=headers)
                sock = conn.sock
                resp = conn.getresponse()
                resp.micmic_sock = sock
                return resp.status, resp, conn
            except (socket.timeout, TimeoutError):
                conn.close()
                raise UpstreamTimeout()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                conn.close()
                if reused and attempt == 0:
                    continue                # an idle connection the far end had closed
                raise
            except BaseException:
                conn.close()
                raise
        raise ConnectionError("upstream unreachable")

    def stream(self, resp: http.client.HTTPResponse, conn, started: float):
        """The body as it arrives, each read bounded by what is left of the total time."""
        sock = getattr(resp, "micmic_sock", None)
        while True:
            left = self.total_timeout - (time.monotonic() - started)
            if left <= 0:
                raise UpstreamTimeout()
            try:
                if sock is not None:
                    sock.settimeout(min(left, self.first_byte_timeout))
                b = resp.read1(16384)
            except (socket.timeout, TimeoutError):
                raise UpstreamTimeout()
            if not b:
                return
            yield b

    def release(self, conn, resp, reusable: bool) -> None:
        if reusable and not resp.will_close and conn.sock is not None:
            try:
                self._idle.put_nowait(conn)
                return
            except queue.Full:
                pass
        conn.close()

    def close(self) -> None:
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                return
