"""The device's account with the MicMic cloud: getting credentials, reading the plan.

A new install has no key and no credentials, and until now the only way to get them
was for the owner to mint a token by hand. This registers the device on first run,
writes credentials.json, and swaps the live Jev and Gemini clients over to it without
a restart. It also reads the plan and today's usage for the Settings sheet and hands
out the checkout and billing-portal links.

Nothing here may take the app down. Offline, a 5xx, a refused registration: each one
is recorded and retried later on a backoff, never on every request, and never by
raising into the caller. The device token is a bearer secret. It is written to one
file with mode 0600 and never printed, logged or returned to the page.
"""
from __future__ import annotations

import hashlib, json, os, subprocess, sys, threading, time
from pathlib import Path
import urllib.error, urllib.request
from typing import Callable

from . import jev as _jev
from . import paths

# The integrator sets the real one. MICMIC_CLOUD_URL overrides it (tests, staging).
# Which cloud a build talks to is decided when the release is built, not in source:
# native/build.sh writes savta/cloud_url.txt into the app bundle. A checkout or a fork
# has no such file, so it uses its own keys (TYPESAFE_API_KEY, GEMINI_API_KEY) and never
# signs devices up on someone else's metered cloud.
def _baked_cloud_url() -> str:
    f = Path(__file__).with_name("cloud_url.txt")
    try:
        return f.read_text().strip()
    except OSError:
        return ""


DEFAULT_CLOUD_URL = _baked_cloud_url()
APP_VERSION = "1.0.1"   # keep equal to build.sh --version

STATUS_TTL = 30.0
# Seconds to wait before the next registration attempt after each consecutive failure.
BACKOFF = [30.0, 120.0, 600.0, 1800.0, 3600.0]
# Too many devices on this account is not going to fix itself in thirty seconds.
REFUSED_BACKOFF = 3600.0
TIMEOUT = 8.0

_lock = threading.RLock()
_reg_lock = threading.Lock()
_wake = threading.Event()
_thread: threading.Thread | None = None
_failures = 0
_next_attempt = 0.0
_last_error = ""
# One automatic re-registration per process after the proxy refuses a token. A second
# refusal means something is wrong on the server side, and registering again would
# just mint devices in a loop.
_reregistered = False
_status: tuple[float, str, dict] | None = None   # (fetched at, token digest, body)
_plan_hint: str | None = None
_listeners: list[Callable] = []
_fingerprint: tuple | None = None


class Offline(Exception):
    """The cloud could not be reached, or answered with a 5xx."""


def cloud_url() -> str | None:
    url = (os.environ.get("MICMIC_CLOUD_URL") or DEFAULT_CLOUD_URL).strip().rstrip("/")
    return url if url and _jev._safe_proxy_url(url) else None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _device_name() -> str:
    try:
        r = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True,
                           text=True, timeout=3)
        name = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        name = ""
    return (name or "Mac")[:64]


def _call(method: str, base: str, path: str, token: str | None = None,
          body: dict | None = None, timeout: float = TIMEOUT) -> tuple[int, dict]:
    """(status, JSON body) for any 2xx/4xx. Raises Offline for no answer or a 5xx."""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception as e:  # noqa: BLE001  (URLError, timeout, reset)
        raise Offline(type(e).__name__) from None
    if status >= 500:
        raise Offline(f"HTTP {status}")
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        d = {}
    return status, (d if isinstance(d, dict) else {})


def _write_credentials(creds: dict) -> None:
    """Atomic, and 0600 from the first byte: the temp file is created private, so the
    token never sits in a world-readable file even for a moment. The temp name is
    fixed rather than random, so a write that dies halfway leaves one stale file that
    the next write overwrites, and nothing ever has to be deleted (the project has no
    delete path, and test_micmic.py holds it to that)."""
    tmp = paths.state(".credentials.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, paths.state(_jev.CREDENTIALS))


def _clear_credentials(token: str) -> None:
    """Empty credentials.json, but only if it still holds the refused token: a fresh
    one written in the meantime must survive. Emptied, not removed: {} reads as no
    credentials everywhere."""
    p = paths.state(_jev.CREDENTIALS)
    try:
        if json.loads(p.read_text()).get("token") == token:
            _write_credentials({})
    except (OSError, ValueError, AttributeError):
        pass


def wants_registration() -> bool:
    """No way to reach Jev, and nobody asked for own-key only. mode() is only "none"
    without an own key, except under MICMIC_MODE=proxy, which asks for the proxy even
    on a machine that has one."""
    if os.environ.get("MICMIC_MODE", "").strip().lower() == "own_key":
        return False
    return _jev.mode() == "none"


def ensure_registered(force: bool = False) -> bool:
    """Register this device if it has nothing to talk to Jev with. True when it has
    credentials afterwards. Returns at once while a backoff is running unless forced."""
    global _failures, _next_attempt, _last_error, _plan_hint
    if not wants_registration():
        return _jev.mode() != "none"
    with _reg_lock:
        if not wants_registration():          # another thread just did it
            return True
        if not force and time.time() < _next_attempt:
            return False
        url = cloud_url()
        if url is None:
            _last_error = "bad_cloud_url"
            _next_attempt = time.time() + REFUSED_BACKOFF
            return False
        try:
            status, d = _call("POST", url, "/v1/devices",
                              body={"device_name": _device_name(),
                                    "app_version": APP_VERSION})
        except Offline:
            status, d = 0, {}
        token = str(d.get("token") or "").strip() if status in (200, 201) else ""
        if token:
            try:
                _write_credentials({"proxy_url": url, "token": token})
            except OSError as e:
                token, status = "", -1
                print(f"  MicMic could not save its credentials ({type(e).__name__})",
                      file=sys.stderr)
        if token:
            _failures, _next_attempt, _last_error = 0, 0.0, ""
            _plan_hint = str(d.get("plan") or "free")
            print(f"  MicMic registered this device with {url}", file=sys.stderr)
            return True
        if status == 429:
            _last_error = str(d.get("error") or "too_many_devices")
            _next_attempt = time.time() + REFUSED_BACKOFF
        else:
            _last_error = {0: "offline", -1: "could_not_save"}.get(status, f"http_{status}")
            _next_attempt = time.time() + BACKOFF[min(_failures, len(BACKOFF) - 1)]
            _failures += 1
        print(f"  MicMic registration did not complete ({_last_error}); "
              f"retrying in {_next_attempt - time.time():.0f}s", file=sys.stderr)
        return False


def _registration_loop(first_now: bool) -> None:
    force = first_now
    while wants_registration():
        if ensure_registered(force=force):
            reload_clients()
            return
        force = False
        _wake.wait(timeout=max(0.05, _next_attempt - time.time()))
        if _wake.is_set():
            _wake.clear()
            force = True
    # Someone else supplied credentials (a key, a file) while this waited.
    if _jev.mode() != "none":
        refresh_if_changed()


def start_registration(first_now: bool = True) -> None:
    """Register in the background and swap the clients over once it works. One thread
    at most; a second call only wakes the running one."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            if first_now:
                _wake.set()
            return
        if not wants_registration():
            return
        _thread = threading.Thread(target=_registration_loop, args=(first_now,),
                                   name="micmic-register", daemon=True)
        _thread.start()


def retry_soon() -> None:
    """The person is looking at the account: try now instead of waiting out the backoff."""
    with _lock:
        if _thread is not None and _thread.is_alive():
            _wake.set()


def on_revoked(token: str) -> None:
    """The proxy refused this token (401). Forget it, drop to unconfigured, and register
    again once. Called from jev.revoked(), outside any client lock."""
    global _reregistered, _status, _failures, _next_attempt
    _clear_credentials(token)
    with _lock:
        _status = None
        again = not _reregistered
        _reregistered = True
        if again:
            _failures, _next_attempt = 0, 0.0
    reload_clients()
    if again:
        start_registration(first_now=True)


# ---------------------------------------------------------------- live clients

def on_reload(fn: Callable) -> None:
    """fn(jev_or_None, not_configured_message) after every rebuild."""
    if fn not in _listeners:
        _listeners.append(fn)


def _fp() -> tuple:
    try:
        st = paths.state(_jev.CREDENTIALS).stat()
        f = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        f = None
    return (f, os.environ.get("MICMIC_PROXY_URL", ""),
            _digest(os.environ.get("MICMIC_PROXY_TOKEN", "")), len(_jev._REVOKED))


def mark_built() -> None:
    """The clients that exist now match the credentials on disk."""
    global _fingerprint
    _fingerprint = _fp()


def reload_clients():
    """Build a new Jev client and a new router.LLM_CLIENT from whatever credentials
    exist right now, and hand the Jev one to every listener (server.py's _jev). A
    request already running keeps the client it started with."""
    global _status
    from .llm import LLM
    with _lock:
        try:
            j, err = _jev.Jev(), ""
        except Exception as e:  # noqa: BLE001  (NotConfigured)
            j, err = None, str(e)[:200]
        router = sys.modules.get(f"{__package__}.router")
        if router is not None:
            router.LLM_CLIENT = LLM()
        for fn in list(_listeners):
            try:
                fn(j, err)
            except Exception:  # noqa: BLE001
                pass
        _status = None
        mark_built()
    return j


def refresh_if_changed() -> bool:
    """Rebuild the clients if credentials.json or the proxy env changed since the last
    build: a stat, so cheap enough to call on every request."""
    if _fp() == _fingerprint:
        return False
    reload_clients()
    return True


# ---------------------------------------------------------------- the account

def invalidate() -> None:
    global _status
    with _lock:
        _status = None


def status(fresh: bool = False, timeout: float = TIMEOUT) -> dict | None:
    """GET /v1/account, cached for STATUS_TTL. None offline or without credentials."""
    global _status, _plan_hint
    creds = _jev.proxy_credentials()
    if creds is None:
        return None
    url, token = creds
    dg = _digest(token)
    with _lock:
        c = _status
    if not fresh and c and c[1] == dg and time.time() - c[0] < STATUS_TTL:
        return c[2]
    try:
        code, d = _call("GET", url, "/v1/account", token=token, timeout=timeout)
    except Offline:
        return None
    if code == 401:
        _jev.revoked(token)
        return None
    if code != 200:
        return None
    with _lock:
        _status = (time.time(), dg, d)
        _plan_hint = str(d.get("plan") or "") or _plan_hint
    return d


def plan() -> str | None:
    """"free", "pro", or None when there is no account (own key, unconfigured). Uses
    the cached status when there is one; offline, the last plan seen."""
    if _jev.mode() != "proxy":
        return None
    d = status(timeout=3.0)
    return str(d.get("plan") or "free") if d else (_plan_hint or "free")


def summary(fresh: bool = False) -> dict:
    """What the Settings sheet shows. Never contains the token."""
    m = _jev.mode()
    out = {"mode": m, "plan": None, "usage": None, "resets_at": None,
           "subscription": None, "online": m == "own_key"}
    if m == "proxy":
        d = status(fresh=fresh)
        if d is not None:
            out.update(plan=d.get("plan"), usage=d.get("usage"),
                       resets_at=d.get("resets_at"), subscription=d.get("subscription"),
                       scope=d.get("scope") or "day", online=True)
    elif m == "none":
        retry_soon()
        if _last_error:
            out["error"] = _last_error
    return out


def _link(path: str) -> tuple[str | None, str]:
    """(url, "") or (None, error) for POST /v1/checkout or /v1/portal."""
    if _jev.mode() != "proxy":
        return None, "not_configured"
    creds = _jev.proxy_credentials()
    if creds is None:
        return None, "not_configured"
    base, token = creds
    try:
        code, d = _call("POST", base, path, token=token, body={})
    except Offline as e:
        return None, "payments_unavailable" if "503" in str(e) else "offline"
    if code == 401:
        _jev.revoked(token)
        return None, "unauthorized"
    url = str(d.get("url") or "")
    if code == 200 and url.startswith("https://"):
        invalidate()          # the plan is about to change; do not show the old one
        return url, ""
    return None, str(d.get("error") or f"http_{code}")


def checkout_url() -> tuple[str | None, str]:
    return _link("/v1/checkout")


def portal_url() -> tuple[str | None, str]:
    return _link("/v1/portal")


_jev.on_revoked(on_revoked)
