"""The HTTP side of the proxy. Standard library HTTP; Stripe is in billing.py.

    POST /v1/jev                                  body forwarded to Jev System One
    POST /v1/gemini/<model>:generateContent       body forwarded to Gemini
    GET  /v1/usage                                today's meter for this token
    POST /v1/devices                              anonymous sign-up: a new free token
    GET  /v1/account                              plan, usage and subscription
    POST /v1/checkout                             a Stripe Checkout URL for Pro
    POST /v1/portal                               a Stripe billing portal URL
    POST /v1/stripe/webhook                       Stripe events, signature checked
    POST /v1/tts                                  a spoken reply, streamed PCM (tts.py)
    POST /v1/consent                              the terms and AI-data consent, again
    POST /v1/apple/transaction                    an iPhone's signed StoreKit transaction
    POST /v1/apple/notifications                  App Store Server Notifications V2
    POST /v1/account/delete                       delete this device's account and data
    POST /v1/diagnostics                          a phone's turn log, only while Share diagnostics is on
    GET  /health, /healthz                        liveness, no auth, no database
    GET  /ready                                   the database answers
    GET  /, /privacy, /terms, /support, /checkout/*   the public pages (pages.py)

Every other /v1 route needs `Authorization: Bearer <device token>`, except the two
webhooks, which are signed by Stripe and by Apple.

Metering, per token per UTC day:
  * Jev calls count against FREE_DAILY_CALLS / PRO_DAILY_CALLS. This is the number
    /v1/usage reports as used_today / limit_today.
  * Gemini calls have their own, smaller cap (FREE_DAILY_GEMINI_CALLS /
    PRO_DAILY_GEMINI_CALLS), reported under "gemini" in /v1/usage. They do not share
    Jev's counter: a Gemini call with a screenshot can cost more than an entire Jev
    request, so it needs a tighter ceiling of its own, and a shared counter would let
    screen descriptions eat a user's voice requests.
  * A unit is taken before the upstream call and given back if the upstream refuses
    it (non-200 or no connection). It is NOT given back on a timeout, because the
    upstream may already have done, and billed, the work.

Slow clients: the first byte of a request must come within idle_timeout, and the whole
request (line, headers, body) within request_deadline of it. Past max_connections, a
new connection is answered 503 and closed without a thread.

Privacy: request and response bodies are never logged, not even on error. They are
what someone said out loud and what is on their screen. Upstream error bodies are never
passed on either (they can echo a key), and neither is any upstream header. The log
line per request is method, route, status, milliseconds and the first 8 hex digits of
the token hash, which identifies a row in `admin list` and nothing else.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import json
import logging
import math
import queue
import re
import signal
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit

from . import apple, billing, pages, tts
from .config import Config
from .store import PRO_STATUSES, Store, hash_token, pro_source

log = logging.getLogger("micmic.proxy")

GEMINI_ROUTE = re.compile(r"^/v1/gemini/([A-Za-z0-9._-]{1,64}):generateContent$")
# Largest body we will read just to throw away before answering an error.
DRAIN_MAX = 16 * 1024 * 1024
DIGITS = re.compile(r"^[0-9]{1,12}$")
MAX_SIGNUP_BODY = 4 * 1024
MAX_WEBHOOK_BODY = 1024 * 1024
MAX_APPLE_TX_BODY = 16 * 1024
MAX_APPLE_NOTIFICATION_BODY = 64 * 1024
MAX_SMALL_BODY = 4 * 1024
# Keys that would carry sound. Diagnostics are text and timings only: an item holding
# any of these anywhere is refused, not stripped.
DIAG_AUDIO_KEYS = frozenset({"audio", "pcm", "samples", "wav", "recording", "waveform",
                             "audio_base64", "sound"})
DIAG_KIND_RE = re.compile(r"^[a-z_]{1,32}$")
PLATFORMS = ("mac", "ios")
APPLE_TX_SOURCES = ("purchase", "update", "restore", "launch")
# Where an iPhone user cancels an Apple subscription (we cannot cancel it for her).
APPLE_MANAGE_URL = "https://apps.apple.com/account/subscriptions"
VERSION_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
CLIENT_TIME_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$")
BUSY = (b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
        b"Content-Length: 17\r\nRetry-After: 1\r\nConnection: close\r\n\r\n"
        b'{"error": "busy"}')

# What a MicMic Gemini request may contain. Everything else is refused, not stripped:
# a part like {"fileData": {"fileUri": ...}} makes Google fetch and bill remote media
# (a YouTube URL, or a file on the owner's account) that no body limit bounds.
GEMINI_TOP = {"contents", "systemInstruction", "generationConfig", "safetySettings"}
GEMINI_GEN = {"maxOutputTokens", "temperature", "topP", "topK", "stopSequences",
              "responseMimeType", "candidateCount"}
GEMINI_IMAGE = re.compile(r"^image/(png|jpeg|webp|heic|heif)$")


def _parts_ok(parts, images: bool) -> bool:
    if not isinstance(parts, list):
        return False
    for p in parts:
        if not isinstance(p, dict) or len(p) != 1:
            return False
        if "text" in p:
            if not isinstance(p["text"], str):
                return False
        elif "inlineData" in p and images:
            d = p["inlineData"]
            if not (isinstance(d, dict) and set(d) == {"mimeType", "data"}
                    and isinstance(d["data"], str) and isinstance(d["mimeType"], str)
                    and GEMINI_IMAGE.match(d["mimeType"])):
                return False
        else:
            return False
    return True


def _is_count(v) -> bool:
    # bool is an int in Python, not in JSON.
    return isinstance(v, int) and not isinstance(v, bool)


def clean_gemini(body: dict, max_output: int) -> bool:
    """Check a generateContent body against what MicMic sends, and clamp its output.
    True when it may go upstream."""
    if set(body) - GEMINI_TOP:
        return False
    contents = body.get("contents")
    if not isinstance(contents, list):
        return False
    for c in contents:
        if not isinstance(c, dict) or set(c) - {"role", "parts"}:
            return False
        if c.get("role", "user") not in ("user", "model") or not _parts_ok(c.get("parts"), True):
            return False
    si = body.get("systemInstruction")
    if si is not None and (not isinstance(si, dict) or set(si) - {"role", "parts"}
                           or not _parts_ok(si.get("parts"), False)):
        return False
    ss = body.get("safetySettings")
    if ss is not None and not (isinstance(ss, list) and all(
            isinstance(x, dict) and set(x) <= {"category", "threshold"}
            and all(isinstance(v, str) for v in x.values()) for x in ss)):
        return False
    gc = body.get("generationConfig")
    if gc is None:
        gc = body["generationConfig"] = {}
    if not isinstance(gc, dict) or set(gc) - GEMINI_GEN:
        return False
    # Output tokens are the expensive side. Clamp rather than reject, so a client that
    # asks for more still gets an answer, just a bounded one. Each candidate gets the
    # full cap, so there is only ever one.
    mot = gc.get("maxOutputTokens")
    if not _is_count(mot) or mot <= 0 or mot > max_output:
        gc["maxOutputTokens"] = max_output
    if "candidateCount" in gc:
        gc["candidateCount"] = 1
    return True


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ConsentError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(body.get("error", ""))
        self.status, self.body = status, body


def check_consent(obj, cfg: Config, platform: str) -> dict:
    """The consent object the app sends (PURCHASES_ACCOUNT.md 4.3), checked, as the
    tokens columns it is stored in. consent_at is added by the caller from the server
    clock: the phone's own time is kept, never trusted."""
    if not isinstance(obj, dict):
        raise ConsentError(400, {"error": "bad_request"})
    keys = ("terms_version", "privacy_version", "ai_disclosure_version")
    versions = {k: obj.get(k) for k in keys}
    if not all(isinstance(v, str) and VERSION_RE.match(v) for v in versions.values()):
        raise ConsentError(400, {"error": "bad_request"})
    providers = obj.get("providers")
    if not (isinstance(providers, list) and 0 < len(providers) <= 16
            and all(isinstance(x, str) and x in cfg.consent_providers for x in providers)):
        raise ConsentError(400, {"error": "bad_request"})
    at = obj.get("accepted_at")
    if not (isinstance(at, str) and len(at) <= 40 and CLIENT_TIME_RE.match(at)):
        raise ConsentError(400, {"error": "bad_request"})
    locale = obj.get("locale", "")
    if not (isinstance(locale, str) and len(locale) <= 16 and locale.isprintable()):
        raise ConsentError(400, {"error": "bad_request"})
    method = obj.get("method", "checkbox")
    if method != "checkbox":
        raise ConsentError(400, {"error": "bad_request"})
    age = obj.get("age_18")
    if age is not None and not isinstance(age, bool):
        raise ConsentError(400, {"error": "bad_request"})
    if platform == "ios" and age is not True:
        # The iPhone app is 18+ (Gemini API terms); no iPhone device exists without it.
        raise ConsentError(400, {"error": "consent_required"})
    required = cfg.consent_required()
    if any(versions[k] < required[k] for k in keys):
        raise ConsentError(409, {"error": "consent_outdated", "consent_required": required})
    return {"consent_terms": versions["terms_version"],
            "consent_privacy": versions["privacy_version"],
            "consent_ai": versions["ai_disclosure_version"],
            "consent_providers": ",".join(dict.fromkeys(providers)),
            "consent_client_at": at, "consent_locale": locale, "consent_method": method,
            "consent_age18": None if age is None else int(age)}


def brain_forget(cfg: Config, authorization: str) -> tuple[int | None, dict]:
    """POST {BRAIN_URL}/v1/brain/forget with the caller's own token (contract route).
    (status, body), or (None, {}) when the brain could not be reached in time."""
    u = urlsplit(cfg.brain_url or "")
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname or "", u.port or (443 if u.scheme == "https" else 80),
               timeout=cfg.brain_timeout)
    try:
        conn.request("POST", u.path.rstrip("/") + "/v1/brain/forget", body=b'{"v": 1}',
                     headers={"Authorization": authorization, "X-MicMic-Contract": "1",
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read(64 * 1024)
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError, RecursionError):
            body = {}
        return resp.status, body if isinstance(body, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("brain forget unreachable: %s", type(e).__name__)
        return None, {}
    finally:
        conn.close()


class UpstreamTimeout(Exception):
    pass


class UpstreamPool:
    """A few keep-alive connections to one upstream host, shared by all handler threads.
    A fresh TLS handshake to Jev costs more than the Jev call itself."""

    def __init__(self, url: str, timeout: float, size: int = 8):
        u = urlsplit(url)
        self.https = u.scheme == "https"
        self.host = u.hostname or ""
        self.port = u.port or (443 if self.https else 80)
        self.path = u.path.rstrip("/")
        self.timeout = timeout
        self._idle: queue.LifoQueue = queue.LifoQueue(maxsize=size)

    def _new(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.timeout)

    def post(self, path: str, body: bytes, headers: dict) -> tuple[int, bytes]:
        for attempt in (0, 1):
            # The retry is always a fresh connection: after a quiet spell every pooled
            # one has gone stale together, and a second pooled one failed the same way
            # (the first Jev and Gemini calls of a run answered 502, MicMic Bench v1
            # phone path, 2026-10-04).
            try:
                if attempt:
                    raise queue.Empty
                conn, reused = self._idle.get_nowait(), True
            except queue.Empty:
                conn, reused = self._new(), False
            try:
                deadline = time.monotonic() + self.timeout
                conn.request("POST", self.path + path, body=body, headers=headers)
                resp, data = self._read(conn, deadline)
            except (socket.timeout, TimeoutError):
                conn.close()
                raise UpstreamTimeout()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                    ssl.SSLEOFError, ssl.SSLZeroReturnError):
                conn.close()
                # A pooled connection the upstream already closed while idle. The request
                # never reached it, so one retry on a fresh connection is safe.
                if reused and attempt == 0:
                    self._drain()
                    continue
                raise
            except BaseException:
                conn.close()
                raise
            if resp.will_close:
                conn.close()
            else:
                conn.sock.settimeout(self.timeout)
                try:
                    self._idle.put_nowait(conn)
                except queue.Full:
                    conn.close()
            return resp.status, data
        raise ConnectionError("upstream unreachable")

    def close(self) -> None:
        self._drain()

    def _drain(self) -> None:
        """Close every idle connection: one went stale, so the rest of its age did too."""
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                return

    @staticmethod
    def _read(conn: http.client.HTTPConnection,
              deadline: float) -> tuple[http.client.HTTPResponse, bytes]:
        """The response within one deadline for the whole of it. The socket timeout is
        per recv, so an upstream that sends a byte now and then would otherwise hold
        this thread, and the pooled connection, for as long as it keeps dripping."""
        def left() -> float:
            t = deadline - time.monotonic()
            if t <= 0:
                raise TimeoutError("upstream deadline passed")
            return t
        # Kept here: getresponse() drops conn.sock when the upstream says it will close,
        # while the response goes on reading from the same socket.
        sock = conn.sock
        sock.settimeout(left())
        resp = conn.getresponse()
        chunks = []
        while True:
            try:
                sock.settimeout(left())
            except OSError:
                # getresponse() closed conn.sock because the upstream said it will close
                # (Connection: close); the response still reads from its own reference,
                # under the timeout already set. Setting it again raised EBADF and turned
                # every such reply into a 502 (all Jev calls, 2026-10-05).
                left()
            b = resp.read1(65536)
            if not b:
                break
            chunks.append(b)
        data = b"".join(chunks)
        if resp.length:                   # the upstream hung up before its Content-Length
            raise http.client.IncompleteRead(data, resp.length)
        return resp, data


def _day(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _window(now: datetime) -> str:
    """The ten minutes a checkout's idempotency key covers. Stripe replays a key's first
    answer for 24 hours, errors included, so a key that lived a whole day turned one
    failed attempt (a product missing its tax code, 2026-09-25) into a day without
    checkout for that device. Ten minutes still folds a double click into one session."""
    return now.strftime("%Y-%m-%dT%H:") + f"{now.minute // 10}0"


def _total(cfg, plan: str) -> bool:
    """The free plan is a number of requests to try, in total, not a daily allowance:
    its usage lives in one bucket that never rolls over (FREE_LIMIT_SCOPE=day brings
    the daily one back). Pro is daily."""
    return plan != "pro" and cfg.free_scope == "total"


def _bucket(cfg, plan: str, now: datetime) -> str:
    return "total" if _total(cfg, plan) else _day(now)


def _reset(now: datetime) -> datetime:
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return midnight + timedelta(days=1)


def _month_reset(now: datetime) -> datetime:
    return datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc)


def _tts_bucket(scope: str, now: datetime) -> str:
    """Spoken characters: one lifetime pool (free), a calendar month and a day (Pro)."""
    if scope == "total":
        return "total"
    if scope == "month":
        return now.strftime("m%Y-%m")
    return _day(now)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_to(reset: datetime, now: datetime) -> str:
    return str(max(1, int((reset - now).total_seconds())))


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, cfg: Config, store: Store | None = None,
                 clock: Callable[[], datetime] = utc_now):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.clock = clock
        self.jev_pool = UpstreamPool(cfg.jev_upstream, cfg.jev_timeout)
        self.gemini_pool = UpstreamPool(cfg.gemini_upstream, cfg.gemini_timeout)
        self.eleven = tts.ElevenUpstream(cfg.eleven_upstream, cfg.tts_first_byte_timeout,
                                         cfg.tts_timeout)
        self.tts_cache = tts.Cache(cfg.tts_cache_dir, cfg.tts_cache_max_bytes,
                                   cfg.tts_cache_secret)
        self.pages = pages.render(cfg)
        self.apple = apple.Apple(cfg)
        self._slots = threading.BoundedSemaphore(max(1, cfg.max_connections))
        # Diagnostics uploads per device in the last minute (in memory: a restart only
        # forgets a minute). The daily item cap lives in the usage table.
        self.diag_rate: dict[str, list[float]] = {}
        self.diag_lock = threading.Lock()
        self._purge_stop = threading.Event()
        handler = type("BoundHandler", (Handler,), {"timeout": cfg.idle_timeout})
        super().__init__((cfg.host, cfg.port), handler)
        self.purge_diagnostics()
        threading.Thread(target=self._purge_loop, daemon=True, name="diag-purge").start()

    def purge_diagnostics(self) -> int:
        """Diagnostics older than DIAG_RETENTION_DAYS go. At start, hourly, and on every
        upload; `admin diagnostics-purge` does the same by hand."""
        cutoff = int(self.clock().timestamp()) - self.cfg.diag_retention_days * 86400
        try:
            n = self.store.purge_diagnostics(cutoff)
        except Exception as e:  # noqa: BLE001
            log.error("diagnostics purge failed: %s", type(e).__name__)
            return 0
        if n:
            log.info("diagnostics purge removed %d row(s)", n)
        return n

    def _purge_loop(self) -> None:
        while not self._purge_stop.wait(3600.0):
            self.purge_diagnostics()

    def server_close(self):
        self._purge_stop.set()
        super().server_close()
        self.jev_pool.close()
        self.gemini_pool.close()
        self.eleven.close()

    def handle_error(self, request, client_address):
        # The stock version prints a traceback and the client's address to stderr, which
        # the host keeps as logs. The exception text can quote a key or what someone
        # said, so only its type is logged, like every route-level catch.
        log.error("unhandled error on a connection: %s", type(sys.exc_info()[1]).__name__)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            log.warning("connection limit reached; refusing a connection")
            try:
                request.setblocking(False)
                request.send(BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _DeadlineReader(io.RawIOBase):
    """The socket as the handler reads it. Waiting for a request's first byte is bounded
    by idle_timeout; from that byte on, every read is bounded by what is left of the
    request's deadline, however steadily the client trickles."""

    def __init__(self, sock: socket.socket, handler: "Handler"):
        self._sock = sock
        self._h = handler

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        cfg = self._h.server.cfg
        deadline = self._h._deadline
        if deadline is None:
            timeout = cfg.idle_timeout
        else:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("request deadline passed")
            timeout = min(timeout, cfg.idle_timeout)
        self._sock.settimeout(timeout)
        n = self._sock.recv_into(b)
        if deadline is None and n:
            self._h._deadline = time.monotonic() + cfg.request_deadline
        return n


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "micmic-proxy"
    sys_version = ""
    server: ProxyServer
    _deadline: float | None = None

    def setup(self):
        super().setup()
        stock = self.rfile
        self.rfile = io.BufferedReader(_DeadlineReader(self.connection, self), 65536)
        stock.close()

    def handle_one_request(self):
        self._deadline = None
        super().handle_one_request()

    # ---------------------------------------------------------- logging
    # The stock handler logs the raw request line, which carries the full path and any
    # query string a client chose to send. Replace it with a line built only from
    # values this code produced itself.

    def log_request(self, code="-", size="-"):  # noqa: ARG002
        pass

    def log_message(self, format, *args):  # noqa: A002, ARG002
        # Reached only for protocol-level errors (bad request line, oversized header).
        # The args can contain what the client sent, so they are dropped.
        log.warning("protocol error from client")

    def _access(self, route: str, status: int, t0: float, tok: str | None) -> None:
        log.info("%s %s %d %dms tok=%s", self.command, route, status,
                 (time.monotonic() - t0) * 1000, tok[:8] if tok else "-")

    # ---------------------------------------------------------- responses

    def _send(self, status: int, body: bytes, headers: dict | None = None,
              content_type: str = "application/json",
              cache: str = "no-store") -> None:
        self._sent = True
        # The request is read. Writing gets the ordinary per-send timeout, not the
        # remains of the read deadline.
        try:
            self.connection.settimeout(self.server.cfg.idle_timeout)
        except OSError:
            pass
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def _json(self, status: int, obj: dict, headers: dict | None = None) -> None:
        self._send(status, json.dumps(obj).encode(), headers)

    def _length(self) -> int | None:
        """Content-Length as exactly one run of digits, else None. int() alone takes
        "1_0", "+10" and " 10 ", and a second, different header would leave the rest of
        the body to be read as the next request. A front proxy that reads either one
        differently is how requests get smuggled."""
        values = self.headers.get_all("Content-Length") or []
        if len(values) != 1 or not DIGITS.match(values[0]):
            return None
        return int(values[0])

    def _drop_body(self) -> None:
        """We are answering without using the body. Read and discard a reasonably sized
        one so the client sees our answer instead of a reset, and so the connection can
        be reused. Anything larger, or of unknown size, closes the connection: the rest
        of that stream is not a next request."""
        if self.headers.get("Content-Length") is None and not self.headers.get(
                "Transfer-Encoding"):
            return                                   # no body at all
        n = self._length()
        if self.headers.get("Transfer-Encoding") or n is None or n > DRAIN_MAX:
            self.close_connection = True
            return
        try:
            while n > 0:
                chunk = self.rfile.read(min(n, 65536))
                if not chunk:
                    self.close_connection = True
                    return
                n -= len(chunk)
        except OSError:
            self.close_connection = True

    # ---------------------------------------------------------- auth + body

    def _auth(self) -> tuple[str, str] | None:
        h = self.headers.get("Authorization", "")
        if h[:7].lower() != "bearer ":              # the scheme is case-insensitive
            return None
        token = h[7:].strip()
        if not token or len(token) > 256:
            return None
        return self.server.store.plan_for(token, self._now_s(),
                                          self.server.cfg.apple_expiry_leeway)

    def _now_s(self) -> int:
        return int(self.server.clock().timestamp())

    def _read_body(self, limit: int) -> tuple[bytes | None, int, dict | None]:
        """(raw, status, error). status is 0 when the body is in."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return None, 411, {"error": "length_required"}
        if self.headers.get("Content-Length") is None:
            self.close_connection = True
            return None, 411, {"error": "length_required"}
        n = self._length()
        if n is None:
            self.close_connection = True
            return None, 400, {"error": "bad_request"}
        if n > limit:
            self._drop_body()
            return None, 413, {"error": "body_too_large", "max_bytes": limit}
        try:
            raw = self.rfile.read(n)
        except TimeoutError:
            self.close_connection = True
            return None, 408, {"error": "request_timeout"}
        if len(raw) != n:
            self.close_connection = True
            return None, 400, {"error": "bad_request"}
        return raw, 0, None

    def _read_json(self, limit: int) -> tuple[dict | None, int, dict | None]:
        """(body, status, error). status is 0 when the body is good."""
        raw, status, err = self._read_body(limit)
        if err is not None:
            return None, status, err
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None, 400, {"error": "bad_json"}
        if not isinstance(body, dict):
            return None, 400, {"error": "bad_json"}
        return body, 0, None

    def _limit_response(self, limit: int, total: bool = False) -> None:
        now = self.server.clock()
        if total:
            # The free requests are used up for good: nothing comes back at midnight.
            # Same error name for older clients; "scope" tells newer ones to offer Pro.
            self._json(429, {"error": "daily_limit", "limit": limit, "resets_at": None,
                             "scope": "total"})
            return
        reset = _reset(now)
        self._json(429, {"error": "daily_limit", "limit": limit, "resets_at": _iso(reset),
                         "scope": "day"},
                   {"Retry-After": _seconds_to(reset, now)})

    def _unauthorized(self) -> None:
        self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})

    def _client_addr(self) -> str:
        """The address a sign-up is counted against. Behind Railway the socket peer is
        its edge, so with TRUST_PROXY_HEADERS=1 the address comes from what the edge
        adds: X-Real-IP, else the LAST X-Forwarded-For hop. Never the first: a client
        can send its own X-Forwarded-For, the edge appends to it, and the first hop
        was whatever the client wrote, which made the per-address limit forgeable."""
        if self.server.cfg.trust_proxy_headers:
            # The shape only, never an address: enough to confirm what the edge sends.
            xff_raw = self.headers.get("X-Forwarded-For") or ""
            log.info("client addr from headers: x-real-ip=%s xff_hops=%d",
                     bool(self.headers.get("X-Real-IP")), len([h for h in xff_raw.split(",") if h.strip()]))
            candidates = [(self.headers.get("X-Real-IP") or "").strip()]
            candidates += [h.strip() for h in reversed(
                (self.headers.get("X-Forwarded-For") or "").split(","))]
            # The rightmost PUBLIC one: internal hops the platform adds after it are
            # private addresses, and would otherwise put everyone on one counter.
            for c in candidates:
                try:
                    ip = ipaddress.ip_address(c)
                except ValueError:
                    continue
                if ip.is_global:
                    return str(ip)
        return self.client_address[0]

    # ---------------------------------------------------------- routes

    def do_HEAD(self):
        """The same answer as GET without the body. Link-preview crawlers and uptime
        checks ask this first, and the server used to answer every HEAD with 501."""
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    def do_GET(self):
        t0 = time.monotonic()
        self._sent = False
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/healthz"):
            self._json(200, {"ok": True})
            return self._access("/health", 200, t0, None)
        if path == "/ready":
            try:
                self.server.store.ping()
                self._json(200, {"ok": True})
                return self._access("/ready", 200, t0, None)
            except Exception as e:  # noqa: BLE001
                log.error("readiness check failed: %s", type(e).__name__)
                self._json(503, {"ok": False})
                return self._access("/ready", 503, t0, None)
        page = self.server.pages.get(path)
        if page is not None:
            body, ctype = page
            if ctype.startswith("text/html"):
                self._send(200, body, pages.HTML_HEADERS, ctype, "no-cache")
            else:
                self._send(200, body, {"X-Content-Type-Options": "nosniff"}, ctype,
                           "public, max-age=86400")
            return self._access(path, 200, t0, None)        # one of our own keys
        if path == "/v1/account":
            return self._account(t0)
        if path != "/v1/usage":
            self._json(404, {"error": "not_found"})
            return self._access("other", 404, t0, None)
        who = self._auth()
        if who is None:
            self._unauthorized()
            return self._access("/v1/usage", 401, t0, None)
        h, plan = who
        cfg, store = self.server.cfg, self.server.store
        now = self.server.clock()
        day = _bucket(cfg, plan, now)
        self._json(200, {
            "plan": plan,
            "used_today": store.used(h, "jev", day),
            "limit_today": cfg.daily_limit(plan, "jev"),
            "resets_at": None if _total(cfg, plan) else _iso(_reset(now)),
            "scope": "total" if _total(cfg, plan) else "day",
            "gemini": {"used_today": store.used(h, "gemini", day),
                       "limit_today": cfg.daily_limit(plan, "gemini")},
        })
        self._access("/v1/usage", 200, t0, h)

    def do_POST(self):
        t0 = time.monotonic()
        self._sent = False
        path = self.path.split("?", 1)[0]
        service = {"/v1/devices": self._devices, "/v1/checkout": self._checkout,
                   "/v1/portal": self._portal, "/v1/stripe/webhook": self._webhook,
                   "/v1/tts": self._tts, "/v1/consent": self._consent,
                   "/v1/apple/transaction": self._apple_transaction,
                   "/v1/apple/notifications": self._apple_notifications,
                   "/v1/account/delete": self._account_delete,
                   "/v1/diagnostics": self._diagnostics}.get(path)
        if service is not None:
            try:
                status, tok = service()
            except Exception as e:  # noqa: BLE001
                log.error("internal error on %s: %s", path, type(e).__name__)
                self.close_connection = True
                if not self._sent:
                    try:
                        self._json(500, {"error": "internal"})
                    except OSError:
                        pass
                status, tok = 500, None
            return self._access(path, status, t0, tok)
        if path == "/v1/jev":
            route, kind, model = "/v1/jev", "jev", None
        else:
            m = GEMINI_ROUTE.match(path)
            if not m:
                self._drop_body()
                self._json(404, {"error": "not_found"})
                return self._access("other", 404, t0, None)
            model = m.group(1)
            if model not in self.server.cfg.gemini_models:
                self._drop_body()
                self._json(400, {"error": "model_not_allowed"})
                return self._access("/v1/gemini/?", 400, t0, None)
            route, kind = f"/v1/gemini/{model}", "gemini"
        try:
            status, tok = self._forward(kind, model)
        except Exception as e:  # noqa: BLE001
            # The exception text can quote the body or the upstream; only its type is safe.
            log.error("internal error on %s: %s", route, type(e).__name__)
            self.close_connection = True
            if not self._sent:
                try:
                    self._json(500, {"error": "internal"})
                except OSError:
                    pass
            status, tok = 500, None
        self._access(route, status, t0, tok)

    def do_PUT(self):
        self._drop_body()
        self._json(405, {"error": "method_not_allowed"})

    do_DELETE = do_PATCH = do_PUT

    def _forward(self, kind: str, model: str | None) -> tuple[int, str | None]:
        cfg, store = self.server.cfg, self.server.store
        who = self._auth()
        if who is None:
            self._drop_body()
            self._unauthorized()
            return 401, None
        h, plan = who
        if kind == "gemini" and not cfg.gemini_key:
            self._drop_body()
            self._json(503, {"error": "not_configured"})
            return 503, h

        body, status, err = self._read_json(cfg.max_jev_body if kind == "jev"
                                            else cfg.max_gemini_body)
        if err is not None:
            self._json(status, err)
            return status, h

        if kind == "jev":
            if not isinstance(body.get("questions"), dict):
                self._json(400, {"error": "bad_request"})
                return 400, h
            payload = json.dumps(body).encode()
            headers = {"Authorization": f"Bearer {cfg.jev_key}",
                       "Content-Type": "application/json", "Connection": "keep-alive"}
            pool, up_path = self.server.jev_pool, ""
        else:
            if not clean_gemini(body, cfg.gemini_max_output_tokens):
                self._json(400, {"error": "bad_request"})
                return 400, h
            payload = json.dumps(body).encode()
            headers = {"X-goog-api-key": cfg.gemini_key or "",
                       "Content-Type": "application/json", "Connection": "keep-alive"}
            pool, up_path = self.server.gemini_pool, f"/{model}:generateContent"

        day = _bucket(cfg, plan, self.server.clock())
        limit = cfg.daily_limit(plan, kind)
        per_unit = cfg.jev_bytes_per_unit if kind == "jev" else cfg.gemini_bytes_per_unit
        units = max(1, math.ceil(len(payload) / per_unit)) if per_unit > 0 else 1
        granted, _ = store.consume(h, kind, day, limit, units)
        if not granted:
            self._limit_response(limit, total=_total(cfg, plan))
            return 429, h

        try:
            up_status, up_body = pool.post(up_path, payload, headers)
        except UpstreamTimeout:
            log.warning("upstream %s timeout", kind)
            if cfg.refund_on_timeout:
                store.refund(h, kind, day, units)
            self._json(504, {"error": "upstream_timeout"})
            return 504, h
        except Exception as e:  # noqa: BLE001
            store.refund(h, kind, day, units)
            log.warning("upstream %s unreachable: %s", kind, type(e).__name__)
            self._json(502, {"error": "upstream_error"})
            return 502, h

        if up_status == 200:
            try:
                json.loads(up_body)
            except (ValueError, UnicodeDecodeError, RecursionError):
                # A 200 that is not JSON (a CDN error page) is not an answer. Relayed,
                # the client could not parse it and would retry, paying each time.
                store.refund(h, kind, day, units)
                log.warning("upstream %s 200 with a body that is not JSON", kind)
                self._json(502, {"error": "upstream_error"})
                return 502, h
            self._send(200, up_body)
            return 200, h

        store.refund(h, kind, day, units)
        log.warning("upstream %s status %d", kind, up_status)
        if up_status in (429, 503, 529):
            # Upstream is busy, not broken. A distinct code lets the client back off and
            # retry, and keeps 429 meaning only "your daily allowance is used up".
            self._json(503, {"error": "upstream_busy"}, {"Retry-After": "1"})
            return 503, h
        if up_status in (400, 404, 413, 422):
            self._json(400, {"error": "upstream_rejected_request"})
            return 400, h
        self._json(502, {"error": "upstream_error"})
        return 502, h


    # ---------------------------------------------------------- accounts + billing

    def _devices(self) -> tuple[int, str | None]:
        cfg = self.server.cfg
        body, status, err = self._read_json(MAX_SIGNUP_BODY)
        if err is not None:
            self._json(status, err)
            return status, None
        name, version = body.get("device_name"), body.get("app_version")
        if not (isinstance(name, str) and isinstance(version, str)):
            self._json(400, {"error": "bad_request"})
            return 400, None
        # Kept only as a label in `admin list`; control characters would garble it.
        name = "".join(ch for ch in name if ch.isprintable()).strip()[:100]
        version = "".join(ch for ch in version if ch.isprintable()).strip()[:32]
        if not name or not version:
            self._json(400, {"error": "bad_request"})
            return 400, None
        # Additive, optional (PURCHASES_ACCOUNT.md 4.3): a Mac without them signs up
        # exactly as before; an iPhone cannot exist without a recorded consent.
        platform = body.get("platform", "mac")
        if platform not in PLATFORMS:
            self._json(400, {"error": "bad_request"})
            return 400, None
        consent = None
        if body.get("consent") is not None:
            try:
                consent = check_consent(body["consent"], cfg, platform)
            except ConsentError as e:
                self._json(e.status, e.body)
                return e.status, None
            consent["consent_at"] = self._now_s()
        elif platform == "ios":
            self._json(400, {"error": "consent_required"})
            return 400, None
        now = self.server.clock()
        addr = hashlib.sha256(("signup:" + self._client_addr()).encode()).hexdigest()
        made = self.server.store.register_device(
            name, version, addr, _day(now), cfg.max_devices_per_ip_per_day,
            cfg.max_devices_per_day, platform, consent)
        if made is None:
            self._json(429, {"error": "too_many_devices"},
                       {"Retry-After": _seconds_to(_reset(now), now)})
            return 429, None
        token, device_id = made
        out = {"token": token, "device_id": device_id, "plan": "free"}
        if platform == "ios":
            out["apple_account_token"] = self.server.store.apple_account_token(hash_token(token))
        self._json(201, out)
        return 201, hash_token(token)

    def _account(self, t0: float) -> None:
        cfg, store = self.server.cfg, self.server.store
        who = self._auth()
        if who is None:
            self._unauthorized()
            return self._access("/v1/account", 401, t0, None)
        h, plan = who
        a = store.account(h)
        now = self.server.clock()
        day = _bucket(cfg, plan, now)
        apple_sub = store.apple_for_device(a["device_id"], self._now_s(), cfg.apple_expiry_leeway)
        sub = None
        if a["stripe_subscription_id"] and a["sub_status"] and a["sub_period_end"]:
            sub = {"status": a["sub_status"],
                   "current_period_end": _iso(datetime.fromtimestamp(a["sub_period_end"],
                                                                     timezone.utc)),
                   "cancel_at_period_end": bool(a["sub_cancel_at_period_end"])}
        self._json(200, {
            "device_id": a["device_id"],
            "plan": plan,
            "usage": {"jev": {"used": store.used(h, "jev", day),
                              "limit": cfg.daily_limit(plan, "jev")},
                      "gemini": {"used": store.used(h, "gemini", day),
                                 "limit": cfg.daily_limit(plan, "gemini")},
                      # What a person counts: spoken requests, as the landing page
                      # promises them. Jev calls over calls-per-request, rounded up.
                      "requests": {"used": -(-store.used(h, "jev", day)
                                              // max(1, cfg.jev_calls_per_request)),
                                   "limit": cfg.daily_requests(plan)},
                      # Spoken characters in the natural voice (POST /v1/tts): the
                      # lifetime pool on free, the calendar month on Pro.
                      "tts": self._tts_usage(h, plan, now)},
            "resets_at": None if _total(cfg, plan) else _iso(_reset(now)),
            "scope": "total" if _total(cfg, plan) else "day",
            "subscription": sub,
            # Added for MicMic for iPhone (PURCHASES_ACCOUNT.md 5.1), all additive.
            "platform": a["platform"],
            "pro_source": pro_source(a["plan"], a["stripe_subscription_id"], a["sub_status"],
                                     apple_sub),
            "apple": self._apple_view(apple_sub),
            "apple_account_token": store.apple_account_token(h),
            "consent": ({"terms_version": a["consent_terms"],
                         "privacy_version": a["consent_privacy"],
                         "ai_disclosure_version": a["consent_ai"],
                         "accepted_at": _iso(datetime.fromtimestamp(a["consent_at"],
                                                                    timezone.utc))}
                        if a["consent_at"] else None),
            "consent_required": cfg.consent_required(),
        })
        self._access("/v1/account", 200, t0, h)

    @staticmethod
    def _apple_view(s: dict | None) -> dict | None:
        if s is None:
            return None
        ts = lambda v: _iso(datetime.fromtimestamp(v, timezone.utc)) if v else None  # noqa: E731
        return {"original_transaction_id_hash": apple.id_hash(s["original_tx_id"]),
                "product_id": s["product_id"], "status": s["status"],
                "expires_at": ts(s["expires_at"]), "grace_until": ts(s["grace_until"]),
                "auto_renew": bool(s["auto_renew"]), "environment": s["environment"]}

    def _tts_usage(self, h: str, plan: str, now: datetime) -> dict:
        cfg = self.server.cfg
        scope, limit = cfg.tts_limits(plan)[0]
        return {"used": self.server.store.used(h, "tts", _tts_bucket(scope, now)),
                "limit": limit, "scope": scope}

    def _tts_limit_response(self, scope: str, limit: int, now: datetime) -> None:
        if scope == "total":
            self._json(429, {"error": "tts_limit", "scope": "total", "limit": limit,
                             "resets_at": None})
            return
        reset = _month_reset(now) if scope == "month" else _reset(now)
        self._json(429, {"error": "tts_limit", "scope": scope, "limit": limit,
                         "resets_at": _iso(reset)},
                   {"Retry-After": _seconds_to(reset, now)})

    def _tts(self) -> tuple[int, str | None]:
        """One spoken reply. Never logs the words (see tts.py)."""
        cfg, store, cache = self.server.cfg, self.server.store, self.server.tts_cache
        who = self._auth()
        if who is None:
            self._drop_body()
            self._unauthorized()
            return 401, None
        h, plan = who
        if not cfg.tts_enabled:
            self._drop_body()
            self._json(503, {"error": "tts_disabled"}, {"Retry-After": "600"})
            return 503, h
        if not cfg.tts_configured:
            # No ELEVENLABS_API_KEY on this server: a clean, final answer the app
            # understands (it speaks on the Mac and rests the cloud voice for an hour),
            # never an attempt upstream without a key.
            self._drop_body()
            self._json(503, {"error": "not_configured", "message": "voice not configured"},
                       {"Retry-After": "3600"})
            return 503, h
        body, status, err = self._read_json(tts.MAX_BODY)
        if err is not None:
            self._json(status, err)
            return status, h
        try:
            req = tts.check(body, cfg)
        except tts.BadRequest as e:
            self._json(400, {"error": e.code})
            return 400, h
        now = self.server.clock()
        key_id, seed = cache.ids(cfg.tts_model, req)
        if req["cache"]:
            pcm = cache.get(key_id, seed)
            if pcm is not None:
                # Served from the volume: nothing upstream, nothing on her meter.
                self._send(200, pcm, {"X-Audio-Format": tts.AUDIO_HEADER, "X-Cache": "hit"},
                           "application/octet-stream")
                log.info("tts hit chars=%d", len(req["upstream_text"]))
                return 200, h
        chars = len(req["upstream_text"])
        caps = [(_tts_bucket(scope, now), limit) for scope, limit in cfg.tts_limits(plan)]
        granted, full = store.consume_all(h, "tts", caps, chars)
        if not granted:
            scope = next(sc for sc, _ in cfg.tts_limits(plan) if _tts_bucket(sc, now) == full)
            self._tts_limit_response(scope, dict(cfg.tts_limits(plan))[scope], now)
            return 429, h
        keep = (req["cache"] and cache.enabled
                and store.seen(key_id, int(time.time())) >= cfg.tts_cache_min_seen)

        def refund():
            for bucket, _ in caps:
                store.refund(h, "tts", bucket, chars)

        payload = json.dumps({"text": req["upstream_text"], "model_id": cfg.tts_model,
                              "language_code": req["lang"]}).encode()
        up = self.server.eleven
        started = time.monotonic()
        try:
            up_status, resp, conn = up.open(req["voice_id"], payload, cfg.eleven_key or "")
        except tts.UpstreamTimeout:
            log.warning("upstream tts timeout before any audio")
            if cfg.refund_on_timeout:
                refund()
            self._json(504, {"error": "upstream_timeout"})
            return 504, h
        except Exception as e:  # noqa: BLE001
            refund()
            log.warning("upstream tts unreachable: %s", type(e).__name__)
            self._json(502, {"error": "upstream_error"})
            return 502, h
        if up_status != 200:
            # The error body can quote the key or the words: read and dropped unseen.
            try:
                resp.read(64 * 1024)
            except Exception:  # noqa: BLE001
                pass
            up.release(conn, resp, False)
            refund()
            log.warning("upstream tts status %d", up_status)
            if up_status in (429, 503, 529):
                self._json(503, {"error": "upstream_busy"}, {"Retry-After": "1"})
                return 503, h
            if up_status in (400, 404, 413, 422):
                self._json(400, {"error": "upstream_rejected_request"})
                return 400, h
            self._json(502, {"error": "upstream_error"})
            return 502, h
        # Audio is coming: relayed chunk by chunk, so the first sound in the app is
        # ElevenLabs' first byte plus one hop, not the whole render.
        self._sent = True
        try:
            self.connection.settimeout(cfg.idle_timeout)
        except OSError:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Audio-Format", tts.AUDIO_HEADER)
        self.send_header("X-Cache", "miss")
        self.end_headers()
        got: list[bytes] = []
        total, complete, client_gone = 0, False, False
        try:
            for chunk in up.stream(resp, conn, started):
                total += len(chunk)
                if total > tts.MAX_AUDIO:
                    break
                if keep:
                    got.append(chunk)
                try:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                except OSError:
                    client_gone = True
                    break
            else:
                complete = True
        except tts.UpstreamTimeout:
            log.warning("upstream tts timeout mid-stream")
        except Exception as e:  # noqa: BLE001
            log.warning("upstream tts stream failed: %s", type(e).__name__)
        up.release(conn, resp, complete)
        if not complete:
            # No final chunk: the app sees a cut stream and speaks the rest locally.
            # The characters stay counted: ElevenLabs has already billed them.
            self.close_connection = True
            return (499 if client_gone else 502), h
        if keep and total:
            cache.put(key_id, seed, b"".join(got))     # before the end: the next ask hits
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            self.close_connection = True
        log.info("tts miss chars=%d%s", chars, " stored" if keep and total else "")
        return 200, h

    def _checkout(self) -> tuple[int, str | None]:
        self._drop_body()
        who = self._auth()
        if who is None:
            self._unauthorized()
            return 401, None
        h, plan = who
        acct = self.server.store.account(h)
        if acct["platform"] == "ios":
            # Guideline 3.1.1: digital features unlocked in the iPhone app are sold with
            # In-App Purchase only. Never a Stripe URL to an iPhone.
            self._json(403, {"error": "use_in_app_purchase"})
            return 403, h
        if plan == "pro":
            self._json(409, {"error": "already_pro"})
            return 409, h
        try:
            url = billing.create_checkout(self.server.cfg, acct, _window(self.server.clock()))
        except billing.PaymentsUnavailable:
            self._json(503, {"error": "payments_unavailable"})
            return 503, h
        except billing.StripeFailed:
            self._json(502, {"error": "payments_error"})
            return 502, h
        self._json(200, {"url": url})
        return 200, h

    def _portal(self) -> tuple[int, str | None]:
        self._drop_body()
        who = self._auth()
        if who is None:
            self._unauthorized()
            return 401, None
        h, _ = who
        try:
            url = billing.create_portal(self.server.cfg, self.server.store.account(h))
        except billing.PaymentsUnavailable:
            self._json(503, {"error": "payments_unavailable"})
            return 503, h
        except billing.StripeFailed:
            self._json(502, {"error": "payments_error"})
            return 502, h
        if url is None:
            self._json(404, {"error": "no_subscription"})
            return 404, h
        self._json(200, {"url": url})
        return 200, h

    def _webhook(self) -> tuple[int, str | None]:
        if not self.server.cfg.stripe_webhook_secret:
            self._drop_body()
            self._json(503, {"error": "payments_unavailable"})
            return 503, None
        # The signature covers these exact bytes, so they are verified before any
        # parsing, and never re-serialised.
        raw, status, err = self._read_body(MAX_WEBHOOK_BODY)
        if err is not None:
            self._json(status, err)
            return status, None
        try:
            event = billing.verify_event(self.server.cfg, raw,
                                         self.headers.get("Stripe-Signature"))
        except billing.BadSignature:
            self._json(400, {"error": "bad_signature"})
            return 400, None
        outcome = billing.handle_event(self.server.store, event)
        log.info("stripe %s %s: %s", event["type"], event["id"], outcome)
        self._json(200, {"received": True})
        return 200, None

    # ---------------------------------------------------------- iPhone: consent

    def _consent(self) -> tuple[int, str | None]:
        """The consent again: after a version bump, or to resend one not yet synced."""
        cfg, store = self.server.cfg, self.server.store
        who = self._auth()
        if who is None:
            self._drop_body()
            self._unauthorized()
            return 401, None
        h, _ = who
        body, status, err = self._read_json(MAX_SMALL_BODY)
        if err is not None:
            self._json(status, err)
            return status, h
        try:
            consent = check_consent(body.get("consent"), cfg, store.account(h)["platform"])
        except ConsentError as e:
            self._json(e.status, e.body)
            return e.status, h
        consent["consent_at"] = self._now_s()
        store.set_consent(h, consent)
        self._json(200, {"ok": True})
        return 200, h

    # ---------------------------------------------------------- iPhone: Apple

    def _apple_transaction(self) -> tuple[int, str | None]:
        """A StoreKit 2 transaction the phone posts (purchase, update, restore, launch).
        Verified against Apple's roots; never logged, not even in part."""
        cfg, store, ap = self.server.cfg, self.server.store, self.server.apple
        who = self._auth()
        if who is None:
            self._drop_body()
            self._unauthorized()
            return 401, None
        h, _ = who
        if not ap.configured:
            self._drop_body()
            self._json(503, {"error": "apple_unavailable"}, {"Retry-After": "600"})
            return 503, h
        body, status, err = self._read_json(MAX_APPLE_TX_BODY)
        if err is not None:
            self._json(status, err)
            return status, h
        source = body.get("source", "update")
        if source not in APPLE_TX_SOURCES or not isinstance(body.get("jws"), str):
            self._json(400, {"error": "bad_request"})
            return 400, h
        try:
            tx = ap.verify_transaction(body["jws"])
        except apple.Rejected as e:
            log.info("apple tx source=%s rejected: %s %s", source, e.code, e.status or "-")
            self._json(400, {"error": e.code})
            return 400, h
        except apple.RetryLater:
            log.warning("apple tx source=%s: revocation check unavailable", source)
            self._json(503, {"error": "apple_verify_retry", "retry_after_s": 30},
                       {"Retry-After": "30"})
            return 503, h
        except apple.AppleUnavailable:
            self._json(503, {"error": "apple_unavailable"}, {"Retry-After": "600"})
            return 503, h
        device_id = store.account(h)["device_id"]
        res = store.apple_apply_transaction(device_id, tx, self._now_s(),
                                            cfg.apple_max_devices_per_sub,
                                            cfg.apple_expiry_leeway)
        log.info("apple tx env=%s source=%s sub=%s: %s", tx["environment"], source,
                 apple.short_id(tx["original_tx_id"]), res["outcome"])
        if res["outcome"] != "pro":
            self._json(409, {"error": res["outcome"]})
            return 409, h
        self._json(200, {"plan": "pro", "source": "apple",
                         "expires_at": _iso(datetime.fromtimestamp(res["expires_at"],
                                                                   timezone.utc)),
                         "environment": tx["environment"], "auto_renew": res["auto_renew"]})
        return 200, h

    def _apple_notifications(self) -> tuple[int, str | None]:
        """App Store Server Notifications V2. No bearer token: Apple's signature is the
        authentication. Idempotent by notificationUUID; a storage error is a 500 so
        Apple retries."""
        cfg, store, ap = self.server.cfg, self.server.store, self.server.apple
        if not ap.configured:
            self._drop_body()
            self._json(503, {"error": "apple_unavailable"})
            return 503, None
        body, status, err = self._read_json(MAX_APPLE_NOTIFICATION_BODY)
        if err is not None:
            self._json(status, err)
            return status, None
        payload = body.get("signedPayload")
        if not isinstance(payload, str) or not payload:
            self._json(400, {"error": "bad_request"})
            return 400, None
        try:
            n = ap.verify_notification(payload)
        except apple.Rejected as e:
            log.warning("apple notification rejected: %s", e.status or e.code)
            code = "bad_request" if e.code == "bad_request" else "bad_signature"
            self._json(400, {"error": code})
            return 400, None
        except apple.RetryLater:
            self._json(503, {"error": "apple_verify_retry"}, {"Retry-After": "30"})
            return 503, None
        except apple.AppleUnavailable:
            log.warning("apple notification for an environment this deploy cannot check")
            self._json(503, {"error": "apple_unavailable"})
            return 503, None
        outcome = store.apple_notification(n, self._now_s(), cfg.apple_product_ids,
                                           cfg.apple_max_devices_per_sub,
                                           cfg.apple_expiry_leeway)
        if n["type"] == "TEST":
            log.info("apple TEST notification ok env=%s", n["environment"])
        else:
            log.info("apple notification %s/%s env=%s sub=%s: %s", n["type"],
                     n["subtype"] or "-", n["environment"],
                     apple.short_id((n["tx"] or {}).get("original_tx_id")), outcome)
        self._json(200, {"received": True})
        return 200, None

    # ---------------------------------------------------------- account deletion

    def _account_delete(self) -> tuple[int, str | None]:
        """Delete my account and data (App Store guideline 5.1.1(v)). In this order, so a
        failure halfway never leaves data she can no longer reach: a Mac's Stripe
        subscription is cancelled, then the brain forgets the device (called with her
        own token while the row still exists), then every row about the device goes in
        one transaction. Any failure before that last step deletes nothing."""
        cfg, store = self.server.cfg, self.server.store
        who = self._auth()
        if who is None:
            self._drop_body()
            self._unauthorized()
            return 401, None
        h, _ = who
        body, status, err = self._read_json(MAX_SMALL_BODY)
        if err is not None:
            self._json(status, err)
            return status, h
        if body.get("confirm") is not True:
            self._json(400, {"error": "confirm_required"})
            return 400, h
        a = store.account(h)
        if not cfg.brain_url and a["platform"] == "ios" and cfg.environment != "development":
            # The brain keeps an iPhone's notes and preferences. On any deploy (anything
            # but local development, so a misspelt ENVIRONMENT is covered too), deleting
            # an iPhone account without telling the brain to forget them would leave
            # them behind while the app and the Support and Privacy pages say they are
            # gone, so refuse honestly and delete nothing until BRAIN_URL is set.
            log.warning("account deletion refused: BRAIN_URL is not set for an iPhone device")
            self._json(503, {"error": "deletion_unavailable"})
            return 503, h
        if (a["stripe_subscription_id"] and a["sub_status"] in PRO_STATUSES
                and not a["sub_cancel_at_period_end"]):
            try:
                billing.cancel_for_deletion(cfg, a["stripe_subscription_id"])
            except billing.PaymentsUnavailable:
                self._json(503, {"error": "payments_unavailable"})
                return 503, h
            except billing.StripeFailed:
                self._json(502, {"error": "payments_error"})
                return 502, h
        brain = "not_used"
        if cfg.brain_url:
            b_status, b_body = brain_forget(cfg, self.headers.get("Authorization", ""))
            log.info("brain forget answered %s", b_status if b_status else "nothing")
            if b_status == 501:
                self._json(503, {"error": "deletion_unavailable"})
                return 503, h
            if b_status != 200 or b_body.get("erased") is not True:
                self._json(503, {"error": "brain_unavailable", "retry_after_s": 30},
                           {"Retry-After": "30"})
                return 503, h
            brain = "erased"
        res = store.delete_account(h, self._now_s(), cfg.apple_expiry_leeway)
        if res is None:                          # deleted by a call that raced this one
            self._unauthorized()
            return 401, None
        log.info("account deleted brain=%s", brain)
        self._json(200, {"deleted": True, "brain": brain,
                         "apple_subscription_active": res["apple_subscription_active"],
                         "manage_url": APPLE_MANAGE_URL})
        return 200, h

    # ---------------------------------------------------------- diagnostics

    def _diagnostics(self) -> tuple[int, str | None]:
        """A phone's turn log, sent only while its owner has Share diagnostics on:
        {"v": 1, "app_version", "build", "items": [{...one turn...}]}. Stored as sent
        (text and timings, never audio) under the device id for DIAG_RETENTION_DAYS, for
        the owner to read with `admin diagnostics`. Never logged, not even in part."""
        cfg, store = self.server.cfg, self.server.store
        who = self._auth()
        if who is None:
            self._drop_body()
            self._unauthorized()
            return 401, None
        h, _ = who
        if not cfg.diagnostics_enabled:
            self._drop_body()
            self._json(503, {"error": "diagnostics_disabled"}, {"Retry-After": "86400"})
            return 503, h
        now_ts = time.time()
        with self.server.diag_lock:
            recent = [t for t in self.server.diag_rate.get(h, []) if now_ts - t < 60.0]
            if len(recent) >= max(1, cfg.diag_per_minute):
                self.server.diag_rate[h] = recent
                limited = True
            else:
                recent.append(now_ts)
                self.server.diag_rate[h] = recent
                limited = False
            if len(self.server.diag_rate) > 10000:
                for k in [k for k, v in self.server.diag_rate.items()
                          if not v or now_ts - v[-1] > 60.0]:
                    self.server.diag_rate.pop(k, None)
        if limited:
            self._drop_body()
            self._json(429, {"error": "rate_limited", "retry_after_s": 60},
                       {"Retry-After": "60"})
            return 429, h
        body, status, err = self._read_json(cfg.diag_max_body)
        if err is not None:
            self._json(status, err)
            return status, h
        items = body.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= cfg.diag_max_items:
            self._json(400, {"error": "bad_request", "max_items": cfg.diag_max_items})
            return 400, h
        app_version = body.get("app_version") if isinstance(body.get("app_version"), str) else ""
        rows = []
        for it in items:
            if not isinstance(it, dict) or not it:
                self._json(400, {"error": "bad_request"})
                return 400, h
            if _has_audio_key(it):
                self._json(400, {"error": "audio_not_allowed"})
                return 400, h
            text = json.dumps(it, ensure_ascii=False, separators=(",", ":"))
            if len(text.encode("utf-8")) > cfg.diag_max_item_bytes:
                self._json(413, {"error": "item_too_large",
                                 "max_bytes": cfg.diag_max_item_bytes})
                return 413, h
            at = it.get("at")
            kind = it.get("kind") if isinstance(it.get("kind"), str) else "turn"
            rows.append({"client_at": at if isinstance(at, str) and len(at) <= 40
                         and CLIENT_TIME_RE.match(at) else None,
                         "kind": kind if DIAG_KIND_RE.match(kind) else "turn",
                         "body": text})
        now = self.server.clock()
        granted, _ = store.consume(h, "diag", _day(now), cfg.diag_daily_items, len(rows))
        if not granted:
            reset = _reset(now)
            self._json(429, {"error": "diagnostics_limit", "limit": cfg.diag_daily_items,
                             "resets_at": _iso(reset)},
                       {"Retry-After": _seconds_to(reset, now)})
            return 429, h
        device_id = store.account(h)["device_id"]
        n = store.add_diagnostics(device_id, rows, int(now.timestamp()),
                                  cfg.diag_max_rows_per_device, app_version)
        self.server.purge_diagnostics()
        log.info("diagnostics stored %d item(s)", n)
        self._json(200, {"stored": n, "max_items": cfg.diag_max_items,
                         "retention_days": cfg.diag_retention_days})
        return 200, h


def _has_audio_key(obj, depth: int = 0) -> bool:
    """True when any key at any depth names sound (DIAG_AUDIO_KEYS)."""
    if depth > 12:
        return True                          # nothing a phone sends is this deep
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in DIAG_AUDIO_KEYS or _has_audio_key(v, depth + 1):
                return True
    elif isinstance(obj, list):
        return any(_has_audio_key(v, depth + 1) for v in obj)
    return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    errors = cfg.boot_errors()
    for e in errors:
        log.error("refusing to start: %s", e)
    if errors:
        return 2
    srv = ProxyServer(cfg)
    ap = srv.apple

    def _stop(*_):
        # A container host stops the service with SIGTERM. Leave serve_forever the same
        # way Ctrl-C does, so the socket is closed on the way out.
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)
    log.info("micmic proxy on %s:%d (%s), gemini %s, payments %s, db %s", cfg.host,
             cfg.port, cfg.environment, "configured" if cfg.gemini_key else "NOT configured",
             "configured" if cfg.payments_configured else "NOT configured", cfg.db_path)
    log.info("apple %s (Production %s, %d root certificates, environments %s), brain %s",
             "configured" if ap.configured else "NOT configured",
             "on" if ap.production_on else "off", len(ap.roots), ",".join(ap.verifiers),
             "set" if cfg.brain_url else "not set")
    if cfg.environment != "development" and not cfg.brain_url:
        log.warning("BRAIN_URL is not set: iPhone account deletion answers 503 "
                    "deletion_unavailable until it is")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
