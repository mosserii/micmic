"""Thin Jev client. Jev returns typed answers, never text, so every call here
returns something the calling code can branch on directly.

Where the calls go, in this order:
  1. Own key. TYPESAFE_API_KEY in the environment or in .env.local: straight to Jev,
     exactly as before. The owner's proxy is never contacted in this mode, not even
     for Gemini, because someone who brings their own key has not agreed to send
     anything through our server.
  2. Proxy. MICMIC_PROXY_URL + MICMIC_PROXY_TOKEN from the environment, or
     credentials.json ({"proxy_url": ..., "token": ...}) in the state directory. The
     proxy holds the paid keys and meters calls per device token per UTC day.
  3. Neither: Jev() raises NotConfigured, whose message says what to set.

MICMIC_MODE=own_key|proxy forces one of the first two, which is how a test runs the
proxy path on a machine that also has a key in .env.local.
"""
from __future__ import annotations
import http.client, json, os, threading, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlsplit

API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
CREDENTIALS = "credentials.json"
# A kept-alive connection idle longer than this is treated as already closed by the
# far end. Measured 2026-09-25, with tests/perf/bench.py: the proxy on Railway keeps an
# idle connection 45 s and has closed it by 70 s; api.typesafe.ai keeps it past 130 s.
# She rarely speaks twice inside a minute, so without this nearly every turn in the
# field paid a failed send and a new TLS handshake before any real work began.
IDLE_FRESH = 40.0
# Kept-alive connections held ready. Two, so two questions that do not depend on each
# other can be asked at once (router: a message's recipient check and its words), each
# on a warm connection. A connection opened on demand costs ~500 ms more than the
# question on it, so a parallel call without a warm spare would be slower than none.
POOL_SIZE = 2


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


class NotConfigured(RuntimeError):
    """No key and no proxy credentials, or the proxy no longer accepts this device's
    token. A RuntimeError so every caller that already handled "no key" still does."""


class QuotaExceeded(Exception):
    """The proxy says this device has used its calls for today. Retrying cannot help
    until resets_at, so this is raised at once instead of going through the retry loop."""

    def __init__(self, limit: int, resets_at: str, scope: str = "day"):
        super().__init__(f"limit of {limit} calls reached ({scope}); resets at {resets_at}")
        self.limit = limit
        self.resets_at = resets_at
        # "total": the free requests to try are used up for good; only Pro helps.
        self.scope = scope


def _env_local(name: str) -> str | None:
    for p in (Path(__file__).resolve().parents[2] / ".env.local",
              Path(__file__).resolve().parents[1] / ".env.local"):
        if p.exists():
            for line in p.read_text().splitlines():
                if line.startswith(name + "="):
                    v = line.split("=", 1)[1].strip()
                    if v:
                        return v
    return None


def own_key() -> str | None:
    k = os.environ.get("TYPESAFE_API_KEY")
    if k and k.strip():
        return k.strip()
    return _env_local("TYPESAFE_API_KEY")


def _key() -> str:
    k = own_key()
    if not k:
        raise RuntimeError("TYPESAFE_API_KEY not found in env or .env.local")
    return k


def _safe_proxy_url(url: str) -> bool:
    """https anywhere, plain http only to this machine. The token is a bearer secret,
    and sending it in clear text across a network would hand it to anyone on the path."""
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if not u.hostname:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")


# Tokens the proxy has refused (401) in this process. A refused token is treated as
# absent everywhere, so mode() drops to "none" instead of retrying a dead credential,
# and savta.account can register the device again.
_REVOKED: set[str] = set()
_revoke_listeners: list = []


def on_revoked(fn) -> None:
    """fn(token) whenever the proxy refuses a token. savta.account registers here."""
    if fn not in _revoke_listeners:
        _revoke_listeners.append(fn)


def revoked(token: str) -> None:
    """The proxy answered 401 for this token. Called outside every client lock."""
    if not token or token in _REVOKED:
        return
    _REVOKED.add(token)
    for fn in list(_revoke_listeners):
        try:
            fn(token)
        except Exception:  # noqa: BLE001
            pass


def proxy_credentials() -> tuple[str, str] | None:
    """(proxy_url, device token), or None when there are none or they are unusable."""
    url = os.environ.get("MICMIC_PROXY_URL", "").strip()
    tok = os.environ.get("MICMIC_PROXY_TOKEN", "").strip()
    if not (url and tok) or tok in _REVOKED:
        from .paths import state_dir
        try:
            d = json.loads((state_dir() / CREDENTIALS).read_text())
            url, tok = str(d.get("proxy_url") or "").strip(), str(d.get("token") or "").strip()
        except (OSError, ValueError, AttributeError):
            return None
    if not (url and tok) or tok in _REVOKED or not _safe_proxy_url(url):
        return None
    return url.rstrip("/"), tok


def mode() -> str:
    """"own_key", "proxy" or "none". Resolved fresh each time, so a credentials file
    written after startup is picked up by the next client that is created (see
    savta.account.reload_clients, which is what creates it)."""
    forced = os.environ.get("MICMIC_MODE", "").strip().lower()
    if forced == "proxy":
        return "proxy" if proxy_credentials() else "none"
    if own_key():
        return "own_key"
    if forced != "own_key" and proxy_credentials():
        return "proxy"
    return "none"


def not_configured_message() -> str:
    from .paths import state_dir
    return ("MicMic has no way to reach Jev: set TYPESAFE_API_KEY (env or .env.local), "
            "or MICMIC_PROXY_URL and MICMIC_PROXY_TOKEN, or put credentials.json in "
            f"{state_dir()} (a proxy URL must be https unless it is this machine)")


def _endpoint(url: str) -> tuple[bool, str, int, str]:
    u = urlsplit(url)
    https = u.scheme == "https"
    return https, u.hostname or "", u.port or (443 if https else 80), u.path.rstrip("/")


class Jev:
    HOST = "api.typesafe.ai"
    PATH = "/v1/systemone"

    def __init__(self, timeout: float = 12.0):
        self.mode = mode()
        if self.mode == "own_key":
            self.key = _key()
            self._https, self._host, self._port, self._path = True, self.HOST, 443, self.PATH
            self._auth = self.key
        elif self.mode == "proxy":
            url, token = proxy_credentials()  # type: ignore[misc]
            # The device token is not an API key; keep it out of the attribute that
            # always held one, so nothing that prints .key can leak it.
            self.key = None
            self._https, self._host, self._port, base = _endpoint(url)
            self._path = base + "/v1/jev"
            self._auth = token
        else:
            raise NotConfigured(not_configured_message())
        self.timeout = timeout
        self.calls = 0
        self.input_tokens = 0
        self.last_ms = 0.0
        # Wall time spent inside ask(), retries included. The router reads the change
        # across one turn, which is what tells a slow turn from a turn with many calls.
        self.busy_ms = 0.0
        self._idle: list[tuple] = []       # (connection, last answered at), newest last
        self._lock = threading.Lock()      # guards _idle and the counters

    def _new_conn(self):
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=self.timeout)
        conn.connect()
        return conn

    def _take(self):
        """The most recently used idle connection, or None."""
        with self._lock:
            return self._idle.pop()[0] if self._idle else None

    def _give(self, conn) -> None:
        with self._lock:
            if len(self._idle) < POOL_SIZE:
                self._idle.append((conn, time.time()))
                return
        _close(conn)

    def warmup(self):
        """Open the TLS connections ahead of the first real question. Without this the
        first call pays ~8s of DNS plus handshake, which is the whole latency budget."""
        self.refresh(max_idle=float("inf"))

    def refresh(self, max_idle: float = IDLE_FRESH) -> None:
        """Called when she starts speaking: connections idle past max_idle are
        replaced now, while she talks, instead of failing on her first question, and
        the pool is filled back up to POOL_SIZE."""
        now = time.time()
        with self._lock:
            stale = [c for c, at in self._idle if now - at > max_idle]
            self._idle = [(c, at) for c, at in self._idle if now - at <= max_idle]
            need = POOL_SIZE - len(self._idle)
        for c in stale:
            _close(c)

        def one():
            try:
                self._give(self._new_conn())
            except Exception:  # noqa: BLE001  (offline: the question itself will say so)
                pass
        threads = [threading.Thread(target=one, daemon=True) for _ in range(need)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def ask(self, state, questions: dict) -> dict:
        t_in = time.time()
        try:
            return self._ask(state, questions)
        finally:
            with self._lock:
                self.busy_ms += (time.time() - t_in) * 1000

    def _ask(self, state, questions: dict) -> dict:
        payload = json.dumps({"state": state, "model": MODEL, "questions": questions})
        headers = {"Authorization": f"Bearer {self._auth}", "Content-Type": "application/json",
                   "Connection": "keep-alive"}
        # Through the proxy, 503 is "Jev is busy, try again" and 429 is reserved for the
        # daily allowance, which retrying cannot fix.
        retry_on = (429, 529) if self.mode == "own_key" else (503, 529)
        last = None
        fresh = False            # after a failure, never trust another idle connection
        for attempt in range(3):
            t0 = time.time()
            conn = None
            try:
                conn = None if fresh else self._take()
                if conn is None:
                    conn = self._new_conn()
                conn.request("POST", self._path, body=payload, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
                self._give(conn)
                conn = None
                if status == 200:
                    d = json.loads(raw)
                    with self._lock:
                        self.last_ms = (time.time() - t0) * 1000
                        self.calls += 1
                        self.input_tokens += d.get("usage", {}).get("input_tokens", 0)
                    return d["answers"]
                if self.mode == "proxy":
                    if status == 401:
                        revoked(self._auth)
                    self._proxy_refusal(status, raw)
                last = f"HTTP {status}: {raw[:200].decode(errors='replace')}"
                if status in retry_on and attempt < 2:
                    time.sleep(0.4 * (2 ** attempt)); continue
                break
            except (QuotaExceeded, NotConfigured):
                raise
            except Exception as e:  # noqa: BLE001
                last = repr(e)[:200]
                if conn is not None:
                    _close(conn)
                fresh = True
                # Through the proxy a timeout is not retried: the proxy is still waiting
                # on Jev and has already counted the call, so each retry cost her one
                # more of her daily requests (three for one hung question) and twelve
                # more seconds of silence. Only a request that never got through (a
                # refused connection, a keep-alive the server had already closed) is
                # worth sending again.
                never_sent = isinstance(e, (ConnectionRefusedError, BrokenPipeError,
                                            http.client.RemoteDisconnected))
                if self.mode == "proxy" and not never_sent:
                    break
                if attempt < 2:
                    continue
        raise RuntimeError(f"jev call failed: {last}")

    @staticmethod
    def _proxy_refusal(status: int, raw: bytes) -> None:
        """Turn the two proxy answers that no retry can fix into their own exceptions."""
        if status == 401:
            raise NotConfigured("the MicMic proxy did not accept this device's token")
        if status == 429:
            try:
                d = json.loads(raw)
            except ValueError:
                return
            if isinstance(d, dict) and d.get("error") == "daily_limit":
                raise QuotaExceeded(int(d.get("limit", 0)), str(d.get("resets_at") or ""),
                                    str(d.get("scope") or "day"))

    @property
    def cost_usd(self) -> float:
        return self.input_tokens * 0.042 / 1e6


# ---- answer helpers -------------------------------------------------------

def choice(ans: dict, key: str):
    """Return (selected, confidence, probabilities)."""
    a = ans[key]
    return a["choice"], a["confidence"], a["probabilities"]


def noul(ans: dict, key: str) -> float:
    return ans[key]["noul"]


def score(ans: dict, key: str) -> float:
    return ans[key]["score"]


def top_n(ans: dict, key: str, n: int = 3):
    a = ans[key]
    return sorted(a["probabilities"].items(), key=lambda kv: -kv[1])[:n]
