"""Fixtures for the adversarial proxy lane.

Run from the proxy project, whose uv env has pytest and nothing else:

    cd proxy && uv run python -m pytest ../tests/adversarial/proxy -q

Every upstream here is a fake written in this file. Proxies launched as a process get
TYPESAFE_API_KEY=fake-jev and GEMINI_API_KEY=fake-gem in their environment and an env
file that does not exist, so no real key is ever read, sent or logged. MicMic servers
run from the checkout's own .venv in proxy mode against a temp state directory.

Ports: the proxy process listens on 8815, MicMic on 8814, fakes on ephemeral ports.
"""
from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PROXY_DIR = ROOT / "proxy"
MICMIC_PY = ROOT / ".venv" / "bin" / "python3"
sys.path.insert(0, str(PROXY_DIR))
sys.path.insert(1, str(ROOT))

from proxy.app import ProxyServer  # noqa: E402
from proxy.config import Config  # noqa: E402
from proxy.store import Store  # noqa: E402

PROXY_PORT = 8815
MICMIC_PORT = 8814
FAKE_JEV_KEY = "fake-jev"
FAKE_GEM_KEY = "fake-gem"
GEMINI_MODEL = "gemini-3.5-flash-lite"

GEMINI_ANSWER = {"candidates": [{"content": {"parts": [{"text": "It is sunny."}]}}],
                 "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}}

# Choice options a fake Jev prefers, so a MicMic router driven by it lands on the
# harmless clock answer and never on a message, a call or an app action.
_SAFE = ("look_up", "not_applicable", "nobody", "none", "any", "unrevealed", "english",
         "chitchat", "unclear")


def fake_jev_answers(questions: dict, prefer: tuple = (),
                     true_keys: tuple = ("about_clock", "is_complete")) -> dict:
    out = {}
    for k, q in (questions or {}).items():
        crit = q.get("criteria") if isinstance(q, dict) else None
        opts = list(crit.keys()) if isinstance(crit, dict) else []
        pick = next((o for o in (*prefer, *_SAFE) if o in opts), opts[-1] if opts else "none")
        probs = {o: (0.9 if o == pick else 0.1 / max(1, len(opts) - 1)) for o in opts} \
            or {pick: 1.0}
        out[k] = {"choice": pick, "confidence": 0.9, "probabilities": probs,
                  "noul": 1.0 if k in true_keys else 0.0, "score": 0.0,
                  "value": "", "span": ""}
    return {"answers": out, "usage": {"input_tokens": 10}}


class FakeUpstream:
    """One fake for both Jev and Gemini. .mode picks the behaviour of the next request:

    ok              a well formed answer
    status:NNN      that status with a JSON body quoting the key (a careless upstream)
    garbage         bytes that are not HTTP at all, then close
    garbage200      200 whose body is not JSON
    hang            sleep .hang seconds, then ok
    close_mid_body  headers promising 1000 bytes, 10 bytes, close
    drip            200, Content-Length .drip_n, one byte every .drip_every seconds
    """

    def __init__(self):
        self.mode = "ok"
        self.prefer: tuple = ()
        self.true_keys: tuple = ("about_clock", "is_complete")
        self.hang = 3.0
        self.drip_n = 8
        self.drip_every = 0.5
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
                mode = up.mode
                gem = "generateContent" in self.path
                if mode == "hang":
                    time.sleep(up.hang)
                    mode = "ok"
                if mode == "ok":
                    if gem:
                        out = GEMINI_ANSWER
                    else:
                        try:
                            out = fake_jev_answers(json.loads(body).get("questions"),
                                                   up.prefer, up.true_keys)
                        except Exception:  # noqa: BLE001
                            out = {"answers": {}}
                    return self._send(200, json.dumps(out).encode())
                if mode.startswith("status:"):
                    code = int(mode.split(":")[1])
                    return self._send(code, json.dumps(
                        {"error": f"upstream said no to key {FAKE_JEV_KEY}"}).encode())
                if mode == "garbage":
                    self.wfile.write(b"THIS IS NOT HTTP\r\n\r\n")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if mode == "garbage200":
                    return self._send(200, b"<html>502 Bad Gateway from a CDN</html>")
                if mode == "close_mid_body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    self.wfile.write(b'{"answers"')
                    self.wfile.flush()
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if mode == "drip":
                    body = b"{" + b" " * (up.drip_n - 2) + b"}"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.flush()
                    for b in body:
                        time.sleep(up.drip_every)
                        self.wfile.write(bytes([b]))
                        self.wfile.flush()
                    return

            def _send(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.jev_url = f"http://127.0.0.1:{self.port}/v1/systemone"
        self.gem_url = f"http://127.0.0.1:{self.port}/v1beta/models"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Clock:
    def __init__(self, now: datetime | None = None):
        self.now = now or datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def http_req(port: int, method: str, path: str, body=None, token: str | None = None,
             headers: dict | None = None, timeout: float = 30.0, conn=None):
    h = dict(headers or {})
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    c = conn or http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
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


def jev_body(marker: str = "SAID-OUT-LOUD what time is it") -> dict:
    return {"state": {"utterance": marker}, "model": "jev-latest",
            "questions": {"intent": {"type": "choice",
                                     "criteria": {"look_up": "x", "music": "y"}}}}


def gem_body(**extra) -> dict:
    b = {"contents": [{"parts": [{"text": "SCREEN-PIXELS describe"}]}]}
    b.update(extra)
    return b


GEM_PATH = f"/v1/gemini/{GEMINI_MODEL}:generateContent"


class InProc:
    """A proxy in this process with an injectable clock and any Config."""

    def __init__(self, tmp: Path, upstream: FakeUpstream, **over):
        self.clock = Clock()
        cfg = Config(jev_key=FAKE_JEV_KEY, gemini_key=FAKE_GEM_KEY, host="127.0.0.1",
                     port=0, db_path=tmp / "inproc.db",
                     jev_upstream=upstream.jev_url, gemini_upstream=upstream.gem_url,
                     free_daily_calls=5, pro_daily_calls=50,
                     free_daily_gemini=3, pro_daily_gemini=30,
                     jev_timeout=1.0, gemini_timeout=1.0)
        for k, v in over.items():
            setattr(cfg, k, v)
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.srv = ProxyServer(cfg, self.store, clock=self.clock)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.upstream = upstream
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def mint(self, plan="free") -> str:
        return self.store.mint(plan)

    def req(self, method, path, body=None, token=None, **kw):
        return http_req(self.port, method, path, body, token, **kw)

    def used(self, token: str, kind: str = "jev", day: str = "2026-09-24") -> int:
        from proxy.store import hash_token
        return self.store.used(hash_token(token), kind, day)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_port_free(port: int, t: float = 10.0) -> None:
    end = time.time() + t
    while time.time() < end:
        if _port_free(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"port {port} still busy; something from an earlier run is alive")


def _wait_up(port: int, path: str, proc: subprocess.Popen, t: float = 60.0) -> None:
    end = time.time() + t
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"process on {port} exited with {proc.returncode}")
        try:
            s, _, _, _ = http_req(port, "GET", path, timeout=2)
            if s == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"nothing answered on {port}{path}")


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)


class ProxyProc:
    """The real proxy as its own process on 8815, wired to a FakeUpstream."""

    def __init__(self, tmp: Path, upstream: FakeUpstream, real_gemini: bool = False,
                 **env_over):
        _wait_port_free(PROXY_PORT)
        self.tmp = tmp
        self.db = tmp / "proxy.db"
        self.log = tmp / "proxy.log"
        self.upstream = upstream
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MICMIC_", "TYPESAFE_", "GEMINI_", "JEV_", "FREE_",
                                    "PRO_"))}
        env.update({
            "MICMIC_PROXY_PORT": str(PROXY_PORT),
            "MICMIC_PROXY_DB": str(self.db),
            "MICMIC_PROXY_ENV_FILE": str(tmp / "no-such-env-file"),
            "TYPESAFE_API_KEY": FAKE_JEV_KEY,
            "GEMINI_API_KEY": FAKE_GEM_KEY,
            "JEV_UPSTREAM_URL": upstream.jev_url,
            "GEMINI_UPSTREAM_BASE": upstream.gem_url,
        })
        if real_gemini:
            # The one opt-in exception: the proxy reads the real Gemini key itself, from
            # the workspace env file, exactly as it does in development. This process
            # never sees it.
            for k in ("GEMINI_API_KEY", "GEMINI_UPSTREAM_BASE", "MICMIC_PROXY_ENV_FILE"):
                env.pop(k)
        env.update({k: str(v) for k, v in env_over.items()})
        self.env = env
        self.fh = self.log.open("ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "proxy"], cwd=PROXY_DIR,
                                     env=env, stdout=self.fh, stderr=self.fh)
        _wait_up(PROXY_PORT, "/healthz", self.proc)
        self.tokens: list[str] = []

    def admin(self, *args, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "proxy.admin", "--db", str(self.db),
                               *args], cwd=PROXY_DIR, env=self.env, input=stdin,
                              capture_output=True, text=True, timeout=30)

    def mint(self, plan: str = "free", creds: Path | None = None) -> str:
        creds = creds or (self.tmp / f"creds-{len(self.tokens)}.json")
        r = self.admin("mint", "--plan", plan, "--write-credentials", str(creds),
                       "--proxy-url", f"http://127.0.0.1:{PROXY_PORT}")
        assert r.returncode == 0, r.stderr
        tok = json.loads(creds.read_text())["token"]
        self.tokens.append(tok)
        return tok

    def req(self, method, path, body=None, token=None, **kw):
        return http_req(PROXY_PORT, method, path, body, token, **kw)

    def db_used(self, token: str, kind: str = "jev") -> int:
        import sqlite3
        from proxy.store import hash_token
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        c = sqlite3.connect(self.db)
        try:
            row = c.execute("SELECT n FROM usage WHERE token_hash=? AND day=? AND kind=?",
                            (hash_token(token), day, kind)).fetchone()
        finally:
            c.close()
        return row[0] if row else 0

    def stop(self):
        _stop(self.proc)
        self.fh.close()


class MicMicProc:
    """A MicMic server on 8814 in proxy mode, with a scratch state directory."""

    def __init__(self, tmp: Path, profile: dict | None = None, creds: dict | None = None,
                 extra_env: dict | None = None, wait: bool = True):
        _wait_port_free(MICMIC_PORT)
        self.state = tmp / "state"
        self.state.mkdir(exist_ok=True)
        if profile is not None:
            (self.state / "profile.json").write_text(json.dumps(profile))
        if creds is not None:
            p = self.state / "credentials.json"
            p.write_text(json.dumps(creds))
            p.chmod(0o600)
        self.log = tmp / "micmic.log"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MICMIC_", "TYPESAFE_", "GEMINI_"))}
        env.update({"MICMIC_MODE": "proxy", "MICMIC_STATE_DIR": str(self.state),
                    "MICMIC_PORT": str(MICMIC_PORT), "PYTHONUNBUFFERED": "1"})
        env.update(extra_env or {})
        assert "MICMIC_ALLOW_SEND" not in env and "MICMIC_ALLOW_CALL" not in env
        self.fh = self.log.open("ab")
        self.proc = subprocess.Popen([str(MICMIC_PY), "-u", "-m", "savta.server"],
                                     cwd=ROOT, env=env, stdout=self.fh, stderr=self.fh)
        if wait:
            _wait_up(MICMIC_PORT, "/api/health", self.proc, t=90)

    def say(self, text: str, timeout: float = 120.0) -> tuple[dict, float]:
        t0 = time.time()
        s, d, raw, _ = http_req(MICMIC_PORT, "POST", "/api/utterance",
                                {"text": text, "speak": False}, timeout=timeout)
        assert s == 200, raw
        return d, time.time() - t0

    def health(self) -> dict:
        return http_req(MICMIC_PORT, "GET", "/api/health")[1]

    def stop(self):
        _stop(self.proc)
        self.fh.close()


# Every artifact a run produced is scanned for these. credentials.json is the one place
# the token is meant to live, on the client, and is excluded.
def scan_for_secrets(root: Path, secrets: list[str]) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if not p.is_file() or p.name.startswith("creds") or p.name == "credentials.json":
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        for s in secrets:
            if s and s.encode() in data:
                hits.append(f"{p.relative_to(root)} contains {s[:10]}...")
    return hits


@pytest.fixture
def upstream():
    u = FakeUpstream()
    yield u
    u.close()


@pytest.fixture
def inproc(tmp_path, upstream):
    procs: list[InProc] = []

    def make(**over) -> InProc:
        p = InProc(tmp_path, upstream, **over)
        procs.append(p)
        return p
    yield make
    for p in procs:
        p.close()


@pytest.fixture
def proxy_proc(tmp_path, upstream):
    made: list[ProxyProc] = []

    def make(**env) -> ProxyProc:
        p = ProxyProc(tmp_path, upstream, **env)
        made.append(p)
        return p
    yield make
    for p in made:
        p.stop()
    for p in made:
        # "AIza" is the prefix of every real Google API key.
        leaks = scan_for_secrets(tmp_path, p.tokens + [FAKE_JEV_KEY, FAKE_GEM_KEY, "AIza"])
        assert not leaks, leaks


@pytest.fixture
def micmic(tmp_path):
    made: list[MicMicProc] = []

    def make(**kw) -> MicMicProc:
        m = MicMicProc(tmp_path, **kw)
        made.append(m)
        return m
    yield make
    for m in made:
        m.stop()
