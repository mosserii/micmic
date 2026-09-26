"""Tiny local server. No framework, no dependencies."""
from __future__ import annotations
import json, os, sys, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .jev import Jev
from .router import handle, load_config, get_contacts, limit_table, local_hhmm
from .actions import mac
from . import account
from . import onboarding_api
from . import login_item
from . import profile as _prof

WEB = Path(__file__).resolve().parent / "web"
# MICMIC_PORT lets a second copy run beside the one she is using — a test, a
# development branch — without either fighting over the port. The app's launcher
# already honours the same variable when it checks whether a server is up.
PORT = int(os.environ.get("MICMIC_PORT", "8799"))

# No key of her own and no proxy credentials: the app must still start and say so,
# rather than crash at launch, which looks exactly like a broken download.
try:
    _jev = Jev()
    _NOT_CONFIGURED = ""
except Exception as _e:  # noqa: BLE001  (savta.jev.NotConfigured, a RuntimeError)
    _jev = None
    _NOT_CONFIGURED = str(_e)[:200]


def _warm_while_she_speaks() -> None:
    """The microphone just opened (the app ducks on every press). Replace any
    connection to Jev or Gemini that has sat idle long enough to have been closed at
    the far end, now, while she is still talking, and not on her first question."""
    from . import router as _router
    for c in (_jev, _router.LLM_CLIENT):
        if c is not None and hasattr(c, "refresh"):
            threading.Thread(target=c.refresh, daemon=True).start()


def _use_clients(j, err: str) -> None:
    """account.reload_clients() built a new client: registration finished, credentials
    changed on disk, or the proxy refused the token. Counters carry over so /api/health
    keeps adding up across the swap."""
    global _jev, _NOT_CONFIGURED
    old = _jev
    if j is not None and old is not None:
        j.calls += old.calls
        j.input_tokens += old.input_tokens
    _jev, _NOT_CONFIGURED = j, err


account.on_reload(_use_clients)
account.mark_built()

# A voice request is a few hundred bytes; a settings save a few kilobytes. Nothing
# the page sends comes near this.
MAX_BODY = 1024 * 1024

_SETUP_SAY = {
    "hebrew": "MicMic עוד לא מוגדר. צריך להתחבר לחשבון או להכניס מפתח אישי בהגדרות.",
    "arabic": "MicMic لسا مش مضبوط. لازم تسجّل«ي|» دخول أو تحط«ي|» مفتاح خاص بالإعدادات.",
    "russian": "MicMic ещё не настроен. Нужно войти в аккаунт или указать свой ключ в настройках.",
    "english": "MicMic is not set up yet. Sign in, or add your own key in Settings.",
}



_OUTAGE_SAY = {
    "hebrew": "אני לא מצליחה להגיע לשירות שלי כרגע. אפשר לנסות שוב בעוד דקה.",
    "arabic": "ما عم بقدر أوصل للخدمة تبعي هلّق. جرّب«ي|» كمان دقيقة.",
    "russian": "Сейчас я не могу связаться со своим сервисом. Попробуйте через минуту.",
    "english": "I cannot reach my service right now. Please try again in a minute.",
}
_CRASH_SAY = {
    "hebrew": "משהו השתבש אצלי. אפשר לנסות שוב?",
    "arabic": "صار عندي خلل. بتجرّب«ي|» كمان مرة؟",
    "russian": "У меня что-то сломалось. Попробуете ещё раз?",
    "english": "Something went wrong on my side. Could you try again?",
}


def _her_lang(text: str = "") -> str:
    """The language of what she just said, read off its script: when Jev itself is
    unreachable the router never ran, so its language detection never ran either.
    Latin script is English; no text at all falls back to her profile."""
    for ch in text:
        o = ord(ch)
        if 0x0590 <= o <= 0x05FF:
            return "hebrew"
        if 0x0600 <= o <= 0x06FF:
            return "arabic"
        if 0x0400 <= o <= 0x04FF:
            return "russian"
        if ch.isalpha():
            return "english"
    try:
        return _prof.load().get("language") or "english"
    except Exception:  # noqa: BLE001
        return "english"


def _in_her_words(table: dict, text: str = "", **kw) -> tuple[str, str]:
    from .router import degender
    lang = _her_lang(text)
    line = table.get(lang, table["english"]).format(**kw)
    try:
        line = degender(line, _prof.load().get("gender", ""))
    except Exception:  # noqa: BLE001
        pass
    return line, lang

_recent: list[str] = []
_lock = threading.Lock()

# The only place user input reaches a file the app reads back at startup, so nothing
# is taken on trust. A rejected value is reported rather than silently dropped: a
# setting that appears to save and then is not there is worse than an error.
SPEECH_LANGS = ("he-IL", "en-US", "ar-SA", "ru-RU")
ACTIVATIONS = ("push", "wake", "always")
# What she sees when she talks to it: the full window, the slim Spotlight-style bar,
# or nothing at all (it just does the thing and says so out loud).
DISPLAYS = ("panel", "bar", "none")
_MODS = {"cmd", "command", "ctrl", "control", "alt", "opt", "option", "shift"}


def _valid_hotkey(spec: str) -> bool:
    """"fn-fn" (the default double-tap) or "mod+...+key", matching parse_combo() in
    native/listener.py. Kept in step with it by hand: the listener is not importable
    from here, and a spec this accepts but the listener cannot parse would leave the
    user with no working hotkey and no error."""
    spec = spec.strip().lower()
    if spec in ("fn-fn", "fn+fn", "globe-globe", "globe+globe"):
        return True
    # A lone tap of a right-hand modifier, like Handy's Right Option. Must match
    # TAP_KEYS in native/listener.py.
    if spec in ("right-command", "right-option", "right-control", "right-shift"):
        return True
    parts = [x for x in spec.replace("-", "+").split("+") if x]
    if len(parts) < 2:
        return False                      # a bare letter would fire while typing
    *mods, key = parts
    if not mods or any(m not in _MODS for m in mods):
        return False
    return len(key) == 1 and key.isalnum()


def _ts(payload: dict) -> float | None:
    """The listener's turn id: the epoch seconds it opened at."""
    try:
        return float(payload["ts"])
    except (KeyError, TypeError, ValueError):
        return None


def turn_open(payload: dict) -> dict:
    """POST /api/turn_open. Every kind holds a counting-down message except "armed",
    which is the listen-for-"no" window of the countdown itself."""
    from . import router as _r
    if (payload.get("kind") or "") == "armed":
        return {"paused": False}
    held = _r.pause_pending(_ts(payload))
    return {"paused": held is not None, "to": (held or {}).get("to")}


def turn_closed(payload: dict) -> dict:
    """POST /api/turn_closed, in either shape: the listener's {"heard", "why"} or
    {"sent", "reason"}. heard or sent false: nothing reached the server this turn,
    so a message still held is asked about. sent true: its utterance was already
    handled, nothing to do. reason "replaced": the turn that took over keeps it."""
    from . import router as _r
    reason = str(payload.get("reason") or payload.get("why") or "")[:40]
    if payload.get("sent") is True or payload.get("heard") is True:
        return {"did": "nothing_to_do"}
    return _r.turn_closed(speak=bool(payload.get("speak", True)), turn_ts=_ts(payload),
                          sent=False, reason=reason)


def _validate_settings(payload: dict) -> tuple[dict, list[str]]:
    ok: dict = {}
    bad: list[str] = []
    if not isinstance(payload, dict):
        return ok, ["payload"]

    if "language_hint" in payload:
        v = str(payload["language_hint"])
        (ok.__setitem__("language_hint", v) if v in SPEECH_LANGS
         else bad.append("language_hint"))
    if "activation" in payload:
        v = str(payload["activation"])
        (ok.__setitem__("activation", v) if v in ACTIVATIONS
         else bad.append("activation"))
    if "listen_seconds" in payload:
        try:
            n = int(payload["listen_seconds"])
        except (TypeError, ValueError):
            n = -1
        # Under about four seconds she cannot finish a sentence; past a minute the
        # microphone is effectively always on without her having chosen that.
        (ok.__setitem__("listen_seconds", n) if 4 <= n <= 60
         else bad.append("listen_seconds"))
    if "wake_words" in payload:
        v = payload["wake_words"]
        words = [str(w).strip() for w in v][:12] if isinstance(v, list) else []
        words = [w for w in words if 2 <= len(w) <= 40]
        (ok.__setitem__("wake_words", words) if words else bad.append("wake_words"))
    if "display" in payload:
        v = str(payload["display"])
        (ok.__setitem__("display", v) if v in DISPLAYS else bad.append("display"))
    if "hotkey" in payload:
        v = str(payload["hotkey"]).strip().lower()
        (ok.__setitem__("hotkey", v) if _valid_hotkey(v) else bad.append("hotkey"))
    if "allow_send" in payload:
        v = payload["allow_send"]
        (ok.__setitem__("allow_send", v) if isinstance(v, bool) else bad.append("allow_send"))
    if "onboarded" in payload:
        v = payload["onboarded"]
        (ok.__setitem__("onboarded", v) if isinstance(v, bool) else bad.append("onboarded"))
    if "open_at_login" in payload:
        v = payload["open_at_login"]
        (ok.__setitem__("open_at_login", v) if isinstance(v, bool) else bad.append("open_at_login"))
    return ok, bad


def speech_language(cfg: dict | None = None) -> str:
    """The language MicMic listens in and the page speaks: English, unless she chose
    another one in Settings. Nothing else may pick it. A profile default of he-IL was
    read as a "learned" language on a Mac nobody had set up, and a fresh install came
    up listening in Hebrew with English in the shipped config."""
    if cfg is None:
        cfg = load_config()
    v = cfg.get("language_hint")        # the shipped config, then Settings over it
    return v if v in SPEECH_LANGS else "en-US"


def apply_send_setting() -> None:
    """Sending messages for her is ON unless she turned it off in Settings (the owner's
    decision for the public build, 2026-09-25): every send is still read back with a
    few seconds to say no, and an Undo. An explicit MICMIC_ALLOW_SEND in the
    environment (a developer's machine) wins. Only the running server calls this; the
    test suite never does, so its "nothing leaves the machine" guarantee stands."""
    env = os.environ.get("MICMIC_ALLOW_SEND")
    if env is not None and env.strip() != "":
        mac.SEND_FOR_REAL = env.strip().lower() in ("1", "true", "yes")
        return
    from .router import load_settings
    mac.SEND_FOR_REAL = bool(load_settings().get("allow_send", True))


class Server(ThreadingHTTPServer):
    """A client that goes away mid-request (the page reloading, the listener's poll
    timing out) is routine, not an error: it used to print a full traceback each time."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # quieter console
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s\n" % (fmt % a))

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith(("/api/onboarding", "/api/permissions")):
            return onboarding_api.get(self)
        if self.path.startswith("/api/config"):
            cfg = load_config()
            return self._send(200, json.dumps({
                "language_hint": speech_language(cfg),
                # The language the setup conversation heard her speak. Not the
                # recogniser's language (Settings alone decides that), but the second
                # language a turn is also read in beside English (native/listener.py
                # _make_recognizers), so a Russian speaker is still read in Russian.
                "language": _prof.load().get("language", ""),
                "activation": cfg.get("activation", "push"),
                "wake_words": cfg.get("wake_words", ["savta"]),
                "listen_seconds": cfg.get("listen_seconds", 12),
                # A lone right Option (hold or tap) unless she chose her own.
                "hotkey": cfg.get("hotkey", "right-option"),
                "display": cfg.get("display", "bar"),
                # What is really in force, not just what was saved.
                "allow_send": bool(mac.SEND_FOR_REAL),
                # Nobody has been set up on this Mac yet: the app opens its window
                # instead of starting as an invisible bar.
                "first_run": not _prof.load().get("setup_complete"),
                # The page addresses her in her own grammatical gender. It has been
                # wired to use this for a while and defaulting to feminine until it
                # arrives, which is wrong for half the people who will use this.
                "gender": _prof.load().get("gender", ""),
                "name": _prof.load().get("name", "") or cfg.get("name", "MicMic"),
                # The Settings switch's real position: SMAppService's own word once
                # native/listener.py has polled it, so turning the login item off in
                # System Settings > General > Login Items is reflected here too, not
                # just what was last asked for (savta/login_item.py).
                "open_at_login": login_item.enabled(),
                # What she asked for, which the listener applies. It is not the same
                # as the line above: reading the real status back as the request
                # meant a switch turned on never registered (2026-09-26).
                "open_at_login_desired": login_item.desired(),
                # Contact names only for the one caller that uses them: the listener
                # hands them to the recogniser as contextual strings. The page never
                # read them, and got sixty names with every config fetch. Whatever is
                # cached, never a wait: the first read copies WhatsApp's data (2.5 s).
                **({"contacts": get_contacts(wait=0.0)}
                   if "contacts=1" in (self.path or "") else {}),
            }, ensure_ascii=False).encode())
        if self.path.startswith("/api/memory"):
            from . import memory as _mem
            return self._send(200, json.dumps(
                {"likes": _mem.summary(), "raw": _mem.load()},
                ensure_ascii=False, default=str).encode())
        if self.path.startswith("/api/trace"):
            # What she said, what it understood, why it chose what it chose. The only
            # way to answer "it did something weird" after the fact.
            from . import trace as _trace
            import urllib.parse as _up
            q = _up.parse_qs(_up.urlparse(self.path).query)
            limit = int((q.get("limit") or ["40"])[0])
            conv = (q.get("conversation") or [""])[0]
            return self._send(200, json.dumps(
                {"turns": _trace.read(min(limit, 200), conv)},
                ensure_ascii=False, default=str).encode())
        if self.path.startswith("/api/update"):
            from . import update as _upd
            return self._send(200, json.dumps(_upd.check()).encode())
        if self.path.startswith("/api/speaking"):
            # So a client can tell whether MicMic is still talking, rather than
            # guessing from the length of the sentence it sent.
            return self._send(200, json.dumps({
                "speaking": mac.speaking(),
                "text": (mac._SPEAKING.get("text") or "") if mac.speaking() else "",
            }, ensure_ascii=False).encode())
        if self.path.startswith("/api/sent"):
            # What actually happened to the messages that were queued. The page used
            # to print "sent" off its own countdown timer, whether or not the send
            # had succeeded — it had no way to find out.
            from .router import _HISTORY
            return self._send(200, json.dumps({
                "recent": [{"did": h.get("did"), "to": h.get("to"),
                            "text": (h.get("text") or "")[:120],
                            "result": str(h.get("result") or "")[:120]}
                           for h in _HISTORY[-5:]],
                "pending": bool(__import__("savta.router", fromlist=["PENDING"]).PENDING),
            }, ensure_ascii=False).encode())
        if self.path.startswith("/api/account"):
            # Plan and today's usage for the Settings sheet. account.summary() never
            # carries the device token.
            account.refresh_if_changed()
            fresh = "fresh=1" in self.path
            return self._send(200, json.dumps(account.summary(fresh=fresh),
                                              ensure_ascii=False).encode())
        if self.path.startswith("/api/health"):
            account.refresh_if_changed()
            if _jev is None:
                return self._send(200, json.dumps({"ok": True, "configured": False,
                                                   "calls": 0, "cost_usd": 0.0}).encode())
            return self._send(200, json.dumps({"ok": True, "configured": True,
                                               "calls": _jev.calls,
                                               "cost_usd": round(_jev.cost_usd, 6)}).encode())
        # Strip the query BEFORE matching, not after: "/?panel=1" is still the index,
        # and testing self.path against "/" first sent every query on the root to 404.
        path = self.path.split("?", 1)[0]
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        f = WEB / name
        # WEB / "../../etc/passwd" resolves outside WEB. Nothing reachable asks for
        # that today, but this server is about to ship inside an app.
        try:
            inside = f.resolve().is_relative_to(WEB.resolve())
        except (OSError, ValueError):
            inside = False
        if not inside or not f.exists() or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript",
                 "css": "text/css"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        return self._send(200, f.read_bytes(), ctype)

    def _drain(self) -> bytes:
        """Read the request body, always.

        HTTP keeps the connection open. A body left unread stays in the socket and is
        parsed as the beginning of the NEXT request on that connection — which showed
        up as a 501 for the method "{}GET" the moment the page reloaded after posting
        to one of these. Every POST branch must consume its body even when it does not
        care what is in it.
        """
        body = getattr(self, "_body", None)
        return body if body is not None else b""

    def _read_body(self) -> bytes | None:
        """The whole body, or None when the client went away before sending it.

        A short read is not an error to the socket: rfile.read() just returns fewer
        bytes at EOF. Treating that as a request meant a dropped Undo still undid the
        action while the page told her nothing was undone (adversarial bar lane, 3/3).
        A body that never arrives now ends at the handler's socket timeout."""
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return None
        if n <= 0:
            return b""
        if n > MAX_BODY:
            return None
        try:
            body = self.rfile.read(n)
        except Exception:  # noqa: BLE001  (socket.timeout, reset)
            return None
        return body if len(body) == n else None

    # Seconds a connection may sit without sending: a body that never arrives must not
    # hold a thread forever.
    timeout = 30

    def do_POST(self):
        body = self._read_body()
        if body is None:
            self.close_connection = True
            return                      # nobody is listening for an answer; do nothing
        self._body = body
        if self.path.startswith("/api/utterance"):
            onboarding_api.note_utterance(body)     # first-run "Try it" step
        return self._post()

    def _account_link(self, which: str):
        """Upgrade or Manage: ask the cloud for the Stripe page and open it in her
        browser. Own-key users have no plan to change."""
        self._drain()
        account.refresh_if_changed()
        from .jev import mode
        if mode() == "own_key":
            return self._send(200, json.dumps({"mode": "own_key"}).encode())
        url, err = account.checkout_url() if which == "upgrade" else account.portal_url()
        if url is None:
            return self._send(200, json.dumps({"ok": False, "error": err}).encode())
        try:
            mac.open_url(url)
        except Exception as e:  # noqa: BLE001
            return self._send(200, json.dumps(
                {"ok": False, "error": "could_not_open", "detail": type(e).__name__}).encode())
        return self._send(200, json.dumps({"ok": True}).encode())

    def _post(self):
        if self.path.startswith(("/api/onboarding", "/api/permissions")):
            return onboarding_api.post(self, self._drain())
        if self.path.startswith("/api/upgrade"):
            return self._account_link("upgrade")
        if self.path.startswith("/api/manage"):
            return self._account_link("manage")
        if self.path.startswith("/api/forget_me"):
            self._drain()
            # A record of her habits is hers to erase. This is the one delete path in
            # the project, and it deletes only what MicMic itself wrote about her.
            from . import memory as _mem
            _mem.forget_all()
            return self._send(200, json.dumps({"forgotten": True}).encode())
        if self.path.startswith(("/api/turn_open", "/api/turn_closed")):
            # The listener's turn, opened and closed (native/listener.py). Opening one
            # stops a message's countdown at once; however the turn ends, a held
            # message is only sent after a yes. Contract, both POST:
            #   /api/turn_open   {"ts": <epoch s>, "kind": "hold"|"tap"|"followup"}
            #   /api/turn_closed {"ts": <the same>, "heard": false, "why": "nothing"|"error"}
            # A close comes only when no /api/utterance did; a turn replaced by a new
            # one sends none (its open keeps the hold). The listen-for-"no" window
            # after a read-back posts nothing. Either post can be lost, so
            # router.HELD_WATCHDOG asks on its own if no close arrives. The frozen
            # contract also accepts {"sent", "reason"}: see turn_open / turn_closed.
            body = self._drain()
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
            res = (turn_open(payload) if self.path.startswith("/api/turn_open")
                   else turn_closed(payload))
            return self._send(200, json.dumps(res, ensure_ascii=False, default=str).encode())
        if self.path.startswith("/api/new_conversation"):
            self._drain()
            # Talking about music and then about flights should not leave the flight
            # request reasoning about music.
            from .router import new_conversation
            return self._send(200, json.dumps(
                {"conversation": new_conversation()}).encode())
        if self.path.startswith("/api/settings"):
            body = self._drain()
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
            from .router import save_settings, load_config
            changes, rejected = _validate_settings(payload)
            if changes:
                # router.save_settings() only persists its own SETTABLE keys and
                # silently ignores the rest, so open_at_login is saved here instead:
                # it is applied by native/listener.py (SMAppService), not router.py.
                if "open_at_login" in changes:
                    login_item.set_desired(changes["open_at_login"])
                save_settings(changes)
                if "allow_send" in changes:
                    apply_send_setting()
            cfg = load_config()
            return self._send(200, json.dumps(
                {"saved": sorted(changes), "rejected": rejected,
                 "settings": {**{k: cfg.get(k) for k in
                                 ("language_hint", "activation", "listen_seconds",
                                  "wake_words", "hotkey", "display")},
                              "language_hint": speech_language(cfg),
                              "allow_send": bool(mac.SEND_FOR_REAL),
                              # Echo what was just asked for, not the polled OS status:
                              # the switch should hold its new position at once, and
                              # only correct itself against reality next time Settings
                              # opens (GET /api/config's open_at_login, see login_item.py).
                              "open_at_login": login_item.desired()}},
                ensure_ascii=False).encode())
        if self.path.startswith("/api/login_item_status"):
            # native/listener.py, after applying (or merely re-checking) SMAppService.
            body = self._drain()
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
            login_item.set_status(str(payload.get("status") or ""))
            return self._send(200, json.dumps({"ok": True}).encode())
        if self.path.startswith("/api/undo"):
            # The bar's Undo button. Reverses the last undoable action exactly once and
            # answers in the language it was done in. Contract, frozen: {"undone": bool,
            # "did": str, "say": str}. Never speaks; the bar shows the line.
            self._drain()
            from .router import undo_last
            return self._send(200, json.dumps(undo_last(), ensure_ascii=False).encode())
        if self.path.startswith("/api/duck"):
            # The page opens its microphone. Anything MicMic started playing would
            # otherwise be transcribed along with her, and a song never goes quiet
            # long enough for the recogniser to finish her sentence.
            self._drain()
            _warm_while_she_speaks()
            return self._send(200, json.dumps({"ducked": mac.duck()}).encode())
        if self.path.startswith("/api/unduck"):
            self._drain()
            return self._send(200, json.dumps({"restored": mac.unduck()}).encode())
        if self.path.startswith("/api/stop_speaking"):
            self._drain()
            # "Enough", "stop", pressing the button — anything that means she has
            # heard as much as she wants to. Cutting the sentence off is the whole
            # point; there is nothing to think about, so this does not go near Jev.
            stopped = mac.stop_speaking()
            return self._send(200, json.dumps({"stopped": stopped}).encode())
        if not self.path.startswith("/api/utterance"):
            self._drain()
            return self._send(404, b"{}")
        try:
            payload = json.loads(self._drain() or b"{}")
        except Exception:  # noqa: BLE001
            return self._send(400, b'{"error":"bad json"}')
        text = (payload.get("text") or "").strip()
        # Credentials written since the last request (registration, a sign-in, a file
        # dropped in by hand) take effect now, not at the next launch.
        account.refresh_if_changed()
        if _jev is None:
            said, lang = _in_her_words(_SETUP_SAY, text)
            return self._send(200, json.dumps(
                {"did": "not_configured", "say": said, "lang": lang},
                ensure_ascii=False).encode())
        # Everything that reaches the router comes from her microphone and nowhere else.
        # Message bodies, file names and page text are DATA: they are read aloud or
        # shown, never routed. Enforced here, in code, before any model sees them,
        # because a model instructed not to obey text is still a model that can be
        # talked round. Jev cannot emit text, so it cannot be talked into a new action
        # either; this makes that an invariant rather than a happy accident.
        if payload.get("source") not in (None, "", "microphone"):
            return self._send(200, json.dumps(
                {"did": "refused", "detail": "only speech from the microphone is acted on"}
            ).encode())
        speak = bool(payload.get("speak", True))
        if not text:
            # A silent first press is how onboarding introduces itself. Short-circuiting
            # it here meant a brand new machine answered the very first press with
            # nothing at all.
            if not onboarding_api.setup_done():
                res = handle(_jev, "", "", speak=speak,
                             client=(payload.get("client") or "web"))
                return self._send(200, json.dumps(res, ensure_ascii=False,
                                                  default=str).encode())
            return self._send(200, json.dumps({"did": "empty"}).encode())
        try:
            with _lock:
                recent = " | ".join(_recent[-2:])
            # Only the browser page can play a video inside itself. The Mac app has
            # no window at all, so it needs a real browser opened for it — without
            # this it answered "here you go" and played nothing.
            client = (payload.get("client") or "web").strip()[:16] or "web"
            # How it was captured, which is not the same as what the config file says:
            # the Mac app listens continuously while the config names the browser orb's
            # push mode, so the not-for-us filter was off for the one client needing it.
            activation = (payload.get("activation") or "").strip()[:12]
            # Why this arrived, when it was not something she said out loud — closing
            # the video window, for instance, which must not be mistaken for her
            # cancelling a message that is counting down.
            reason = (payload.get("reason") or "").strip()[:24]
            # A guess made from a half-finished sentence. The page fires these to
            # warm the slow parts while she is still speaking, and then sends the real
            # request. The server used to treat them as real: it SPOKE the guess, was
            # cut off mid-sentence when the real answer arrived, and — far worse —
            # could carry out the guess. A half-heard "send a message to…" armed one
            # send, and the finished sentence armed another.
            speculative = bool(payload.get("speculative"))
            # The recogniser's confidence in this transcript, 0 to 1, when the listener
            # sends it. A message heard at low confidence is confirmed before it goes.
            try:
                asr_conf = float(payload["asr_confidence"])
                asr_conf = asr_conf if 0.0 <= asr_conf <= 1.0 else None
            except (KeyError, TypeError, ValueError):
                asr_conf = None
            res = handle(_jev, text, recent, speak=speak and not speculative,
                         client=client, activation=activation, reason=reason,
                         speculative=speculative, asr_conf=asr_conf)
            with _lock:
                if res.get("did") not in ("ignored", "waiting"):
                    _recent.append(f"{text} -> {res.get('did')}")
                    del _recent[:-6]
            return self._send(200, json.dumps(res, ensure_ascii=False, default=str).encode())
        except Exception as e:  # noqa: BLE001
            from .jev import QuotaExceeded, NotConfigured
            if isinstance(e, NotConfigured):
                # The proxy refused this device's token mid-session. jev.revoked() has
                # already dropped it and started one re-registration; until that lands
                # she hears what a fresh install hears, not "I did not understand".
                said, lang = _in_her_words(_SETUP_SAY, text)
                return self._send(200, json.dumps(
                    {"did": "not_configured", "say": said, "lang": lang},
                    ensure_ascii=False).encode())
            if isinstance(e, QuotaExceeded):
                # The free daily allowance on the proxy is used up. Not an error, and
                # not her fault: say when it comes back, in her own clock, and on the
                # free plan where more of it is.
                from .router import FREE_USED_SAY
                if getattr(e, "scope", "day") == "total":
                    said, lang = _in_her_words(FREE_USED_SAY, text)
                else:
                    said, lang = _in_her_words(limit_table(), text, t=local_hhmm(e.resets_at))
                try:
                    if speak:
                        mac.say(said, lang)
                except Exception:  # noqa: BLE001
                    pass
                return self._send(200, json.dumps(
                    {"did": "daily_limit", "say": said, "lang": lang,
                     "detail": {"limit": e.limit, "resets_at": e.resets_at}},
                    ensure_ascii=False).encode())
            # She must never talk to a machine that then says nothing at all. Even a
            # crash gets a spoken line telling her it is not her fault and what to try.
            traceback.print_exc()
            # An outage behind the proxy (a 5xx, a timeout, no network) is not her
            # fault, and the old line "I did not understand, say it in other words"
            # told her it was. It also came out in Hebrew for English and with raw
            # gender marks. In her words now, degendered, and honest about what broke.
            outage = isinstance(e, (OSError, TimeoutError)) or "call failed" in str(e) \
                or "HTTP 5" in str(e)
            try:
                said, lang = _in_her_words(_OUTAGE_SAY if outage else _CRASH_SAY, text)
                from .actions import mac as _mac
                if speak:
                    _mac.say(said, lang)
            except Exception:  # noqa: BLE001
                said, lang = "Something went wrong on my side. Please try again.", "english"
            return self._send(200, json.dumps(
                {"did": "service_unavailable" if outage else "error", "say": said,
                 "lang": lang, "detail": repr(e)[:300]}, ensure_ascii=False).encode())


def main():
    apply_send_setting()
    if _jev is None:
        print(f"\n  MicMic is not set up: {_NOT_CONFIGURED}\n")
        # A new install: get this device its own credentials in the background, and
        # swap the clients over when they arrive. Offline, it retries on a backoff.
        account.start_registration()
    else:
        _jev.warmup()
    # Who she talks to: the first turn would otherwise wait on a copy of WhatsApp's
    # whole message store (savta/actions/contacts.py RECENT_SECONDS). Not on a fresh
    # install of the app: reading WhatsApp's data asks macOS for "data from other
    # apps", and that prompt must not be the first thing a new user sees, before the
    # onboarding has said what MicMic is.
    from .actions import contacts as _book
    from .paths import is_bundled as _bundled
    # A Mac whose onboarding window was finished but whose spoken setup never was
    # (onboarded true, setup_complete false) is set up: say so before anything reads it.
    onboarding_api.setup_done()
    fresh = _bundled() and not _prof.load().get("setup_complete")
    if not fresh:
        threading.Thread(target=_book.recent_chats, daemon=True,
                         name="micmic-recent-warm").start()
    srv = Server(("127.0.0.1", PORT), H)
    print(f"\n  MicMic listening on http://127.0.0.1:{PORT}")
    if not _bundled():
        print(f"  contacts: {get_contacts()}")
    if _jev is not None:
        print(f"  jev warm: {_jev.last_ms:.0f}ms\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        if _jev is not None:
            print(f"\n  {_jev.calls} jev calls, ${_jev.cost_usd:.5f}\n")


if __name__ == "__main__":
    main()
