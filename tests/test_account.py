#!/usr/bin/env python3
"""The device account: first-run registration, the live client swap, the Settings
sheet's /api/account, Upgrade and Manage, and every way the cloud can say no.

    .venv/bin/python3 tests/test_account.py

A FAKE proxy (stdlib, port 8831) implements the frozen contract: POST /v1/devices,
GET /v1/account, POST /v1/checkout, POST /v1/portal, and the metered /v1/jev and
/v1/gemini routes with their daily_limit refusal. MicMic's own server runs in-process
on 8832. State lives in a throwaway MICMIC_STATE_DIR, MICMIC_MODE=proxy keeps any key
on this machine out of it, mac.open_url and mac.say are stubbed, and nothing here
reaches the real Jev, Gemini, or the real cloud.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE_PORT, APP_PORT = 8831, 8832
assert 8831 <= FAKE_PORT <= 8839 and 8831 <= APP_PORT <= 8839
CLOUD = f"http://127.0.0.1:{FAKE_PORT}"
APP = f"http://127.0.0.1:{APP_PORT}"

STATE = tempfile.mkdtemp(prefix="acct-state-")
os.environ.update(MICMIC_STATE_DIR=STATE, MICMIC_MODE="proxy", MICMIC_CLOUD_URL=CLOUD,
                  MICMIC_PORT=str(APP_PORT))
for k in ("MICMIC_PROXY_URL", "MICMIC_PROXY_TOKEN", "MICMIC_ALLOW_SEND",
          "MICMIC_ALLOW_CALL", "TYPESAFE_API_KEY"):
    os.environ.pop(k, None)
Path(STATE, "profile.json").write_text(json.dumps(
    {"setup_complete": True, "language": "english", "name": "T", "gender": "feminine",
     "speech_lang": "en-US"}))
sys.path.insert(0, str(ROOT))

# Everything written to stderr, so the end of the run can prove no token ever was.
_ERR = io.StringIO()


class _Tee(io.TextIOBase):
    def __init__(self, real):
        self.real = real

    def write(self, s):
        _ERR.write(s)
        return self.real.write(s)

    def flush(self):
        self.real.flush()


sys.stderr = _Tee(sys.stderr)

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# ---------------------------------------------------------------- the fake proxy
class Fake:
    devices: dict[str, dict] = {}      # token -> {"plan", "revoked", "name", "version"}
    registrations = 0                  # POST /v1/devices that reached the handler
    requests = 0
    offline = False                    # hang up without answering
    fail = 0                           # answer every request with this 5xx
    too_many = False
    payments_off = False
    jev_limit = False
    bad_checkout_url = False
    last_token = None
    seq = 0

    @classmethod
    def mint(cls, plan="free"):
        cls.seq += 1
        tok = f"mmp_test_{cls.seq:04d}_{os.urandom(6).hex()}"
        cls.devices[tok] = {"plan": plan, "revoked": False}
        return tok


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        h = self.headers.get("Authorization", "")
        tok = h[7:] if h.startswith("Bearer ") else ""
        d = Fake.devices.get(tok)
        if d is None or d["revoked"]:
            self._json(401, {"error": "unauthorized"})
            return None
        Fake.last_token = tok
        return tok, d

    def _handle(self, method):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else b""
        Fake.requests += 1
        path = self.path.split("?", 1)[0]
        if path == "/v1/devices":
            Fake.registrations += 1
        if Fake.offline:
            self.close_connection = True
            return                                     # hang up: RemoteDisconnected
        if Fake.fail:
            return self._json(Fake.fail, {"error": "boom"})
        if method == "POST" and path == "/v1/devices":
            if Fake.too_many:
                return self._json(429, {"error": "too_many_devices"})
            d = json.loads(body or b"{}")
            tok = Fake.mint()
            Fake.devices[tok].update(name=d.get("device_name"), version=d.get("app_version"))
            return self._json(201, {"token": tok, "device_id": f"dev_{Fake.seq}",
                                    "plan": "free"})
        a = self._auth()
        if a is None:
            return
        tok, dev = a
        if method == "GET" and path == "/v1/account":
            pro = dev["plan"] == "pro"
            return self._json(200, {
                "device_id": "dev_x", "plan": dev["plan"],
                "usage": {"jev": {"used": 37, "limit": 1000 if pro else 100},
                          "gemini": {"used": 3, "limit": 200 if pro else 20}},
                "resets_at": "2026-09-25T00:00:00Z",
                "subscription": ({"status": "active", "current_period_end": "2026-10-24",
                                  "cancel_at_period_end": False} if pro else None)})
        if method == "POST" and path == "/v1/checkout":
            if Fake.payments_off:
                return self._json(503, {"error": "payments_unavailable"})
            if dev["plan"] == "pro":
                return self._json(409, {"error": "already_pro"})
            url = ("file:///etc/passwd" if Fake.bad_checkout_url
                   else "https://checkout.stripe.test/c/pay_123")
            return self._json(200, {"url": url})
        if method == "POST" and path == "/v1/portal":
            if Fake.payments_off:
                return self._json(503, {"error": "payments_unavailable"})
            if dev["plan"] != "pro":
                return self._json(404, {"error": "no_subscription"})
            return self._json(200, {"url": "https://billing.stripe.test/p/sess_456"})
        if method == "POST" and path == "/v1/jev":
            if Fake.jev_limit:
                return self._json(429, {"error": "daily_limit", "limit": 100,
                                        "resets_at": "2026-09-25T00:00:00Z"})
            return self._json(200, {"answers": {}, "usage": {"input_tokens": 1}})
        if method == "POST" and path.startswith("/v1/gemini/"):
            return self._json(200, {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
        return self._json(404, {"error": "not_found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


fake = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), FakeHandler)
threading.Thread(target=fake.serve_forever, daemon=True).start()

# ---------------------------------------------------------------- MicMic, in-process
from savta import account, jev, router  # noqa: E402
from savta import server as srv         # noqa: E402

OPENED: list[str] = []
srv.mac.open_url = lambda url: OPENED.append(url)
srv.mac.say = lambda *a, **k: None
router.mac.say = lambda *a, **k: None
account.BACKOFF = [0.4, 0.4, 0.4]

app = ThreadingHTTPServer(("127.0.0.1", APP_PORT), srv.H)
threading.Thread(target=app.serve_forever, daemon=True).start()
CREDS = Path(STATE) / "credentials.json"


def get(path):
    with urllib.request.urlopen(APP + path, timeout=15) as r:
        raw = r.read().decode()
    return json.loads(raw), raw


def post(path, body=None):
    req = urllib.request.Request(APP + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
    return json.loads(raw), raw


def say(text="what time is it"):
    return post("/api/utterance", {"text": text, "speak": False})[0]


def tokens_seen():
    return list(Fake.devices)


# ---------------------------------------------------------------- 1. before anything
def test_unconfigured_start():
    check("starts unconfigured with no key and no credentials", srv._jev is None,
          srv._NOT_CONFIGURED)
    check("router.LLM_CLIENT starts unavailable", not router.LLM_CLIENT.available)
    check("a request before registration says not_configured",
          say()["did"] == "not_configured")
    d, _ = get("/api/account")
    check("/api/account says mode none", d["mode"] == "none" and d["online"] is False, d)
    check("importing the server registered nothing", Fake.registrations == 0,
          Fake.registrations)


# ---------------------------------------------------------------- 2. offline, 5xx, then first run
def test_offline_then_registration():
    Fake.offline = True
    account.start_registration()           # what server.main() does on NotConfigured
    wait_for(lambda: Fake.registrations >= 1, 3)
    time.sleep(0.15)
    check("offline: one attempt, then it waits out the backoff",
          Fake.registrations == 1, Fake.registrations)
    check("offline: no crash, still answers", say()["did"] == "not_configured")
    d, _ = get("/api/account")
    check("offline: /api/account says offline", d["mode"] == "none" and d.get("error") == "offline", d)
    check("offline: no credentials written", not CREDS.exists())
    Fake.offline = False
    Fake.fail = 503
    before = Fake.registrations
    wait_for(lambda: Fake.registrations > before, 3)
    check("5xx: retried on the backoff, not given up", Fake.registrations > before)
    check("5xx: no credentials written", not CREDS.exists())
    Fake.fail = 0
    check("comes back: registers and swaps the clients in",
          wait_for(lambda: srv._jev is not None, 5), srv._NOT_CONFIGURED)
    check("registration sent a device name and the app version",
          any(d.get("name") and d.get("version") == account.APP_VERSION
              for d in Fake.devices.values()), len(Fake.devices))


# ---------------------------------------------------------------- 3. the credentials file
def test_credentials_file():
    mode = stat.S_IMODE(CREDS.stat().st_mode)
    check("credentials.json is mode 0600", mode == 0o600, oct(mode))
    d = json.loads(CREDS.read_text())
    check("credentials.json holds exactly proxy_url and token",
          set(d) == {"proxy_url", "token"}, sorted(d))
    check("proxy_url is the cloud it registered with", d["proxy_url"] == CLOUD, d["proxy_url"])
    check("token is the one the cloud issued", d["token"] in Fake.devices)
    check("no temp file left behind",
          not [p for p in Path(STATE).iterdir() if p.name.startswith(".credentials")])


# ---------------------------------------------------------------- 4. the live swap
def test_live_reload():
    tok = json.loads(CREDS.read_text())["token"]
    check("server._jev is now a proxy client", srv._jev.mode == "proxy")
    check("router.LLM_CLIENT is now a proxy client", router.LLM_CLIENT.available
          and router.LLM_CLIENT._proxy is not None)
    srv._jev.ask({}, {})
    check("a Jev call after registration carries the new token", Fake.last_token == tok)
    Fake.last_token = None
    out = router.LLM_CLIENT.text("hello")
    check("a Gemini call after registration goes through the proxy",
          out == "hi" and Fake.last_token == tok, (out, Fake.last_token == tok))
    h, raw = get("/api/health")
    check("/api/health says configured", h["configured"] is True, h)
    Fake.jev_limit = True
    d = say()
    check("a request after registration reaches the proxy (daily_limit)",
          d["did"] == "daily_limit", d)
    check("free plan: the limit line says Pro has more, no em dash",
          "MicMic Pro has more: open Settings." in d.get("say", "")
          and "\u2014" not in d.get("say", ""), d.get("say"))
    Fake.devices[tok]["plan"] = "pro"
    account.invalidate()
    d = say()
    check("pro plan: the limit line does not advertise Pro",
          d["did"] == "daily_limit" and "Pro" not in d.get("say", ""), d.get("say"))
    Fake.devices[tok]["plan"] = "free"
    account.invalidate()
    Fake.jev_limit = False
    for lang in ("hebrew", "arabic", "russian", "english"):
        line = router.LIMIT_SAY_FREE[lang]
        check(f"LIMIT_SAY_FREE {lang}: formats, mentions MicMic Pro, no em dash",
              "{t}" in line and "MicMic Pro" in line and "\u2014" not in line
              and line.count("«") == line.count("»"), line)


# ---------------------------------------------------------------- 5. /api/account
def test_account_endpoint():
    tok = json.loads(CREDS.read_text())["token"]
    d, raw = get("/api/account")
    check("/api/account: proxy, free, online", d["mode"] == "proxy" and d["plan"] == "free"
          and d["online"] is True, d)
    check("/api/account: usage and resets_at", d["usage"]["jev"] == {"used": 37, "limit": 100}
          and d["resets_at"] == "2026-09-25T00:00:00Z" and d["subscription"] is None, d)
    check("/api/account never contains the token", tok not in raw and "mmp_" not in raw)
    check("/api/account has the contract keys",
          {"mode", "plan", "usage", "resets_at", "subscription", "online"} <= set(d), sorted(d))
    n = Fake.requests
    get("/api/account")
    check("status is cached for 30s", Fake.requests == n, Fake.requests - n)
    get("/api/account?fresh=1")
    check("?fresh=1 skips the cache", Fake.requests == n + 1, Fake.requests - n)
    for k in ("/api/health", "/api/config"):
        _, raw = get(k)
        check(f"{k} never contains the token", tok not in raw)
    Fake.offline = True
    account.invalidate()
    d, _ = get("/api/account")
    check("offline: /api/account is proxy, online false, no crash",
          d["mode"] == "proxy" and d["online"] is False and d["plan"] is None, d)
    Fake.offline = False
    Fake.fail = 500
    check("5xx: status() is None, no crash", account.status(fresh=True) is None)
    Fake.fail = 0


# ---------------------------------------------------------------- 6. upgrade and manage
def test_checkout_and_portal():
    tok = json.loads(CREDS.read_text())["token"]
    OPENED.clear()
    d, raw = post("/api/upgrade")
    check("upgrade: ok and the checkout page opened",
          d == {"ok": True} and OPENED == ["https://checkout.stripe.test/c/pay_123"], (d, OPENED))
    check("upgrade: the answer never contains the token", tok not in raw)
    OPENED.clear()
    d, _ = post("/api/manage")
    check("manage on free: no_subscription, nothing opened",
          d == {"ok": False, "error": "no_subscription"} and not OPENED, (d, OPENED))
    Fake.payments_off = True
    d, _ = post("/api/upgrade")
    check("payments down: payments_unavailable, nothing opened",
          d == {"ok": False, "error": "payments_unavailable"} and not OPENED, (d, OPENED))
    Fake.payments_off = False
    Fake.bad_checkout_url = True
    d, _ = post("/api/upgrade")
    check("a non-https checkout URL is never opened", d["ok"] is False and not OPENED,
          (d, OPENED))
    Fake.bad_checkout_url = False
    Fake.offline = True
    d, _ = post("/api/upgrade")
    check("offline: upgrade says offline, nothing opened",
          d == {"ok": False, "error": "offline"} and not OPENED, (d, OPENED))
    Fake.offline = False
    Fake.devices[tok]["plan"] = "pro"
    account.invalidate()
    d, _ = post("/api/upgrade")
    check("upgrade on pro: already_pro", d == {"ok": False, "error": "already_pro"}, d)
    d, _ = post("/api/manage")
    check("manage on pro: the billing portal opened",
          d == {"ok": True} and OPENED == ["https://billing.stripe.test/p/sess_456"], (d, OPENED))
    a, _ = get("/api/account?fresh=1")
    check("pro: /api/account shows the subscription",
          a["plan"] == "pro" and a["subscription"]["status"] == "active", a)
    Fake.devices[tok]["plan"] = "free"
    account.invalidate()


# ---------------------------------------------------------------- 7. credentials changed on disk
def test_credentials_changed_on_disk():
    new = Fake.mint()
    CREDS.write_text(json.dumps({"proxy_url": CLOUD, "token": new}))
    Fake.last_token = None
    say()
    check("credentials rewritten on disk are used by the next request",
          Fake.last_token == new, Fake.last_token)
    check("router.LLM_CLIENT was rebuilt with them too",
          router.LLM_CLIENT._proxy is not None and router.LLM_CLIENT._proxy[4] == new)


# ---------------------------------------------------------------- 8. 401: once, never a loop
def test_revoked():
    old = json.loads(CREDS.read_text())["token"]
    Fake.devices[old]["revoked"] = True
    regs = Fake.registrations
    d = say()
    check("revoked: that request says not_configured", d["did"] == "not_configured", d)
    check("revoked: re-registers once and swaps the clients back",
          wait_for(lambda: srv._jev is not None and Fake.registrations == regs + 1, 5),
          (Fake.registrations - regs, srv._NOT_CONFIGURED))
    new = json.loads(CREDS.read_text())["token"]
    check("revoked: a new token, file still 0600",
          new != old and stat.S_IMODE(CREDS.stat().st_mode) == 0o600)
    Fake.last_token = None
    say()
    check("revoked: the next request uses the new token", Fake.last_token == new)
    # The new token is refused too: clear, and stop. No second registration.
    Fake.devices[new]["revoked"] = True
    regs = Fake.registrations
    d = say()
    check("revoked twice: not_configured", d["did"] == "not_configured", d)
    time.sleep(1.5)
    check("revoked twice: never registers again (no loop)",
          Fake.registrations == regs, Fake.registrations - regs)
    check("revoked twice: credentials emptied, still 0600",
          json.loads(CREDS.read_text()) == {} and stat.S_IMODE(CREDS.stat().st_mode) == 0o600)
    h, _ = get("/api/health")
    check("revoked twice: /api/health says not configured", h["configured"] is False, h)
    check("revoked twice: later requests stay not_configured, no network",
          say()["did"] == "not_configured" and Fake.registrations == regs)


# ---------------------------------------------------------------- 9. 429 too many devices
def test_too_many_devices():
    Fake.too_many = True
    regs = Fake.registrations
    ok = account.ensure_registered(force=True)
    check("429: registration refused, no crash", ok is False and Fake.registrations == regs + 1)
    check("429: no credentials written", json.loads(CREDS.read_text()) == {})
    d, _ = get("/api/account")
    check("429: /api/account says too_many_devices",
          d["mode"] == "none" and d.get("error") == "too_many_devices", d)
    check("429: the next non-forced attempt waits out the backoff",
          account.ensure_registered() is False and Fake.registrations == regs + 1)
    Fake.too_many = False


# ---------------------------------------------------------------- 10. own key: untouched
def test_own_key_untouched():
    os.environ.pop("MICMIC_MODE", None)
    os.environ["TYPESAFE_API_KEY"] = "own-key-for-test-not-real"
    try:
        n = Fake.requests
        account.reload_clients()
        check("own key: server._jev is an own-key client", srv._jev is not None
              and srv._jev.mode == "own_key")
        check("own key: router.LLM_CLIENT never uses the proxy",
              router.LLM_CLIENT._proxy is None)
        check("own key: no registration wanted", account.wants_registration() is False
              and account.ensure_registered(force=True) is True)
        d, _ = get("/api/account")
        check("own key: /api/account says own_key, no plan",
              d["mode"] == "own_key" and d["plan"] is None and d["online"] is True, d)
        OPENED.clear()
        for p in ("/api/upgrade", "/api/manage"):
            d, _ = post(p)
            check(f"own key: {p} answers mode own_key and opens nothing",
                  d == {"mode": "own_key"} and not OPENED, (d, OPENED))
        check("own key: the cloud was never contacted", Fake.requests == n, Fake.requests - n)
        check("own key: the daily-limit table is the plain one",
              router.limit_table() is router.LIMIT_SAY)
    finally:
        os.environ.pop("TYPESAFE_API_KEY", None)
        os.environ["MICMIC_MODE"] = "proxy"


def test_cloud_url():
    for url, ok in (("https://micmic.example.com", True), ("http://127.0.0.1:8831", True),
                    ("http://localhost:8831", True), ("http://micmic.example.com", False),
                    ("ftp://127.0.0.1", False)):
        os.environ["MICMIC_CLOUD_URL"] = url
        check(f"cloud URL {url} {'accepted' if ok else 'refused'}",
              (account.cloud_url() is not None) == ok)
    os.environ.pop("MICMIC_CLOUD_URL")
    # A checkout has no baked cloud (forks never sign up on the owner's metered one).
    # The release build bakes only what MICMIC_CLOUD_URL says: the official release
    # passes the owner's cloud, any other build gets none (decision of 2026-09-25).
    check("a checkout has no cloud of its own", account.cloud_url() is None
          and account.DEFAULT_CLOUD_URL == "")
    baked = next((l for l in (pathlib.Path(__file__).resolve().parents[1] / "native/build.sh")
                  .read_text().splitlines() if "MICMIC_CLOUD_URL:-" in l), "")
    check("the release build bakes only the cloud it is given, no default",
          '"${MICMIC_CLOUD_URL:-}"' in baked and "https://" not in baked, baked)
    os.environ["MICMIC_CLOUD_URL"] = CLOUD


def main():
    for t in (test_unconfigured_start, test_offline_then_registration, test_credentials_file,
              test_live_reload, test_account_endpoint, test_checkout_and_portal,
              test_credentials_changed_on_disk, test_revoked, test_too_many_devices,
              test_own_key_untouched, test_cloud_url):
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} ran to the end", False, repr(e)[:300])
    logged = _ERR.getvalue()
    check("no token ever reached stderr", not any(tok in logged for tok in tokens_seen()))
    app.shutdown()
    fake.shutdown()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
