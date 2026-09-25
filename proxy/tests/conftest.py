"""A fake Jev and a fake Gemini on localhost, and a proxy wired to them, so every
test here costs nothing and runs offline."""
from __future__ import annotations

import http.client
import io
import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from proxy.app import ProxyServer
from proxy.config import Config
from proxy.store import Store

FAKE_JEV_KEY = "tsk-FAKE-UPSTREAM-JEV-KEY-0000111122223333"
FAKE_GEMINI_KEY = "AIza-FAKE-UPSTREAM-GEMINI-KEY-44445555"
FAKE_STRIPE_KEY = "sk_test_FAKE-LOCAL-ONLY-5555666677778888"
FAKE_WEBHOOK_SECRET = "whsec_FAKE-LOCAL-ONLY-9999aaaabbbbcccc"
JEV_ANSWER = {"answers": {"intent": {"choice": "music", "confidence": 0.9,
                                     "probabilities": {"music": 0.9, "call": 0.1}}},
              "usage": {"input_tokens": 1000}}
GEMINI_ANSWER = {"candidates": [{"content": {"parts": [{"text": "a calm blue screen"}]}}],
                 "usageMetadata": {"promptTokenCount": 12}}


class Upstream:
    """Behaviour is set per test through .mode; every request is recorded."""

    def __init__(self):
        self.mode = "ok"
        self.delay = 0.0
        self.hits: list[dict] = []
        self.lock = threading.Lock()
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(n)
                with up.lock:
                    up.hits.append({"path": self.path, "headers": dict(self.headers),
                                    "body": body})
                if up.delay:
                    time.sleep(up.delay)
                if up.mode == "ok":
                    out = GEMINI_ANSWER if "generateContent" in self.path else JEV_ANSWER
                    return self._send(200, json.dumps(out).encode())
                if up.mode == "error500":
                    # What a careless upstream does: quote the credential it rejected.
                    return self._send(500, json.dumps(
                        {"error": f"internal: key {FAKE_JEV_KEY} {FAKE_GEMINI_KEY}"}).encode())
                if up.mode == "error401":
                    return self._send(401, json.dumps(
                        {"error": f"invalid api key {FAKE_JEV_KEY}"}).encode())
                if up.mode == "busy429":
                    return self._send(429, b'{"error":"rate limited"}')
                if up.mode == "bad400":
                    return self._send(400, json.dumps(
                        {"error": f"bad questions (key {FAKE_JEV_KEY})"}).encode())
                if up.mode == "hang":
                    time.sleep(3)
                    return self._send(200, json.dumps(JEV_ANSWER).encode())
                if up.mode == "garbage200":
                    return self._send(200, b"<html>502 Bad Gateway from a CDN</html>")
                if up.mode == "drip":
                    # 200 with a body that arrives one byte every half second.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "8")
                    self.end_headers()
                    self.wfile.flush()
                    for b in b"{      }":
                        time.sleep(0.5)
                        try:
                            self.wfile.write(bytes([b]))
                            self.wfile.flush()
                        except OSError:
                            return
                    return

            def _send(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Upstream-Secret", FAKE_JEV_KEY)
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Proxy:
    def __init__(self, tmp: Path, upstream: Upstream, **cfg_over):
        self.clock = Clock()
        self.upstream = upstream
        cfg = Config(jev_key=FAKE_JEV_KEY, gemini_key=FAKE_GEMINI_KEY, host="127.0.0.1",
                     port=0, db_path=tmp / "proxy.db",
                     jev_upstream=f"http://127.0.0.1:{upstream.port}/v1/systemone",
                     gemini_upstream=f"http://127.0.0.1:{upstream.port}/v1beta/models",
                     free_daily_calls=5, pro_daily_calls=50,
                     free_daily_gemini=3, pro_daily_gemini=30,
                     max_jev_body=64 * 1024, max_gemini_body=512 * 1024,
                     jev_timeout=1.0, gemini_timeout=1.0,
                     # These tests were written around a daily free allowance; the
                     # lifetime free trial has its own tests (test_free_total.py).
                     free_scope="day")
        for k, v in cfg_over.items():
            setattr(cfg, k, v)
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.srv = ProxyServer(cfg, self.store, clock=self.clock)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.tokens: list[str] = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def mint(self, plan: str = "free") -> str:
        t = self.store.mint(plan)
        self.tokens.append(t)
        return t

    def request(self, method: str, path: str, body=None, token: str | None = None,
                headers: dict | None = None, conn=None):
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        c = conn or http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        raw = r.read()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if conn is None:
            c.close()
        return r.status, data, raw, dict(r.getheaders())

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(scope="session")
def log_buffer():
    """Everything the proxy logs, across the whole session, for the leak scan."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.DEBUG)
    lg = logging.getLogger("micmic.proxy")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    yield buf
    lg.removeHandler(h)


@pytest.fixture
def upstream():
    u = Upstream()
    yield u
    u.close()


# Strings that must never appear in anything the proxy writes: the upstream and Stripe
# keys, what the user said, what was on their screen, and anything smuggled in a query string.
SECRET_MARKERS = (FAKE_JEV_KEY, FAKE_GEMINI_KEY, FAKE_STRIPE_KEY, FAKE_WEBHOOK_SECRET,
                  "SAID-OUT-LOUD", "SCREEN-PIXELS", "QUERY-SECRET")


def assert_no_leak(text: str, tokens: list[str]) -> None:
    for s in SECRET_MARKERS + tuple(tokens):
        assert s not in text, f"leaked into proxy output: {s[:12]}..."


@pytest.fixture
def proxy(tmp_path, upstream, log_buffer, capfd):
    start = log_buffer.tell()
    p = Proxy(tmp_path, upstream)
    yield p
    p.close()
    # Every test that drives the proxy is also a leak test: whatever it did, nothing
    # secret reached the log or stdout/stderr.
    out, err = capfd.readouterr()
    assert_no_leak(log_buffer.getvalue()[start:] + out + err, p.tokens)


@pytest.fixture
def make_proxy(tmp_path, upstream, log_buffer, capfd):
    """A proxy with Config overrides, leak-checked like `proxy`. Each call gets its own
    database."""
    start = log_buffer.tell()
    made: list[Proxy] = []

    def make(**over) -> Proxy:
        d = tmp_path / f"p{len(made)}"
        d.mkdir()
        p = Proxy(d, upstream, **over)
        made.append(p)
        return p
    yield make
    for p in made:
        p.close()
    out, err = capfd.readouterr()
    assert_no_leak(log_buffer.getvalue()[start:] + out + err,
                   [t for p in made for t in p.tokens])


def jev_body(marker: str = "SAID-OUT-LOUD-play-me-some-music") -> dict:
    return {"state": f"The user said: {marker}", "model": "jev-latest",
            "questions": {"intent": {"type": "choice", "options": ["music", "call"]}}}
