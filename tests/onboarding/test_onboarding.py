#!/usr/bin/env python3
"""The first-run onboarding: its endpoints, when it shows, and the page itself.

    .venv/bin/python3 tests/onboarding/test_onboarding.py

Each server this starts is this worktree's own, on port 8881, with a fresh
MICMIC_STATE_DIR, sends and calls disarmed, and MICMIC_DRY_OPEN=1 so pressing "Turn on"
never brings System Settings up in front of whoever is using the Mac. Never 8799.
Permissions are fake (MICMIC_FAKE_PERMISSIONS, savta/onboarding_api.py) except on the
one server that checks the real status calls answer, which only ever reads: no test
can put a macOS permission dialog on the screen.
Playwright's bundled Chromium, headless; one server and one browser at a time.

Free by default: the servers cannot reach Jev or Gemini (see OFFLINE below), and the
checks that judge a real answer are skipped. MICMIC_LIVE=1 runs those too, for about
a cent.
Screenshots of the Terms line are opt-in: set MICMIC_SHOTS_DIR to a directory to get
clickwrap-en.png and clickwrap-he.png there; unset, the suite writes none.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "savta" / "web"
PORT = 8881
BASE = f"http://127.0.0.1:{PORT}"
assert PORT != 8799, "never the live server"

PASSED, FAILED, SKIPPED = [], [], []
NOCOST = []           # /api/health of each server that ended with zero Jev calls
# Jev and Gemini cost money, so by default the servers here cannot reach them: they run
# as a proxy client pointed at a closed local port, which also skips reading the
# developer's keys from .env.local, and no device gets registered with the cloud. Every
# turn then answers "cannot reach my service", which is all most checks need. The few
# that judge a real answer run only with MICMIC_LIVE=1 and are listed as skipped
# otherwise, never counted as passed.
LIVE = os.environ.get("MICMIC_LIVE") == "1"
OFFLINE = {} if LIVE else {"MICMIC_MODE": "proxy", "MICMIC_PROXY_URL": "http://127.0.0.1:9",
                           "MICMIC_PROXY_TOKEN": "offline-test"}


ALL_GRANTED = {"microphone": "granted", "speech": "granted", "accessibility": "granted"}
NEW_MAC = {"microphone": "not_asked", "speech": "not_asked", "accessibility": "denied"}


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   <- {detail}"))


def live_check(name, ok, detail=""):
    """A check of what Jev answered: real money, so only with MICMIC_LIVE=1."""
    if LIVE:
        return check(name, ok, detail)
    SKIPPED.append(name)
    print("SKIP " + name + "   (needs a live turn: MICMIC_LIVE=1)")


# ---------------------------------------------------------------- server
def _port_open():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


class Server:
    """A private server with its own state directory, stopped on exit."""

    def __init__(self, profile: dict | None = None, settings: dict | None = None,
                 perms: dict | str | None = None, env: dict | None = None):
        # Fake permissions unless perms == "real": all granted by default, or the table
        # given ({"microphone": "not_asked", ..., "answer": {...}, "delay": s}).
        self.perms = perms
        self.env = dict(env or {})
        if _port_open():
            raise SystemExit(f"something is already listening on {PORT}; stop it first")
        self.state = Path(tempfile.mkdtemp(prefix="ob-test-state-"))
        # Never the owner's real asr dir, and never a real model download: the
        # downloader's seam (MICMIC_ASR_NO_DOWNLOAD) holds every job at 0 bytes.
        self.asr = Path(tempfile.mkdtemp(prefix="ob-test-asr-"))
        # Always write one, even for "a new Mac": with no profile.json in the state
        # dir, the app copies in the checkout's own (legacy migration in paths.state),
        # and a developer's real, finished profile made every Mac look set up.
        (self.state / "profile.json").write_text(json.dumps(profile or {}))
        if settings is not None:
            (self.state / "settings.json").write_text(json.dumps(settings))

    def __enter__(self):
        env = dict(os.environ, MICMIC_PORT=str(PORT), MICMIC_STATE_DIR=str(self.state),
                   MICMIC_ASR_DIR=str(self.asr), MICMIC_ASR_NO_DOWNLOAD="1",
                   MICMIC_DRY_OPEN="1", **OFFLINE)
        env.pop("MICMIC_FAKE_PERMISSIONS", None)
        if self.perms != "real":
            env["MICMIC_FAKE_PERMISSIONS"] = json.dumps(self.perms or ALL_GRANTED)
        env.pop("MICMIC_ALLOW_SEND", None)
        env.pop("MICMIC_ALLOW_CALL", None)
        # Never where this app really runs from, never the real Handy/Superwhisper
        # settings, never a real move: only what a test hands in.
        for k in ("MICMIC_BUNDLE_PATH", "MICMIC_FAKE_MOVE", "MICMIC_DICTATION_HOME"):
            env.pop(k, None)
        env.update(self.env)
        self.proc = subprocess.Popen([str(ROOT / ".venv/bin/python3"), "-m", "savta.server"],
                                     cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(200):
            if _port_open():
                return self
            time.sleep(0.1)
        self.proc.kill()
        raise SystemExit(f"server on {PORT} did not start")

    def __exit__(self, *exc):
        # savta/asr_models.py leaves this tripwire when anything tried a real model
        # download (733 MB) under a test state dir.
        trip = self.asr / "REAL_DOWNLOAD_IN_TEST"
        if trip.exists():
            check("no real model download was started", False, trip.read_text())
        # Every server here is keyless unless MICMIC_LIVE=1: its own count of paid Jev
        # calls says so before it goes.
        if not LIVE:
            try:
                h = json.loads(urllib.request.urlopen(BASE + "/api/health", timeout=5).read())
                ok = not h.get("calls") and not h.get("cost_usd")
            except Exception as e:  # noqa: BLE001
                h, ok = repr(e), False
            if not ok:
                check("this server made no paid model calls", False, h)
            else:
                NOCOST.append(h)
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def settings(self) -> dict:
        f = self.state / "settings.json"
        return json.loads(f.read_text()) if f.exists() else {}


def call(method, path, body=None, timeout=10, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# The page's messages to native land here instead of in WKWebView.
INIT = """
window.__msgs = [];
window.webkit = {messageHandlers: {
  micmic:    {postMessage: m => window.__msgs.push(String(m))},
  micmicbar: {postMessage: m => window.__msgs.push('bar:' + m)}}};
"""


def new_page(browser, lang="en", scheme="light", reduced="no-preference"):
    ctx = browser.new_context(viewport={"width": 380, "height": 560}, color_scheme=scheme,
                              reduced_motion=reduced)
    ctx.add_init_script(INIT + f"try{{localStorage.setItem('micmic.lang',{json.dumps(lang)})}}catch(_){{}}"
                        "try{sessionStorage.clear()}catch(_){}")
    page = ctx.new_page()
    page.__errors = []
    page.__requests = []
    page.on("pageerror", lambda e: page.__errors.append(str(e)))
    page.on("request", lambda r: page.__requests.append(r.url))
    page.__asks = []            # every /api/permissions/request the page posted, in order
    page.on("request", lambda r: page.__asks.append(json.loads(r.post_data or "{}").get("k"))
            if r.method == "POST" and "/api/permissions/request" in r.url else None)
    return ctx, page


def settle(page, path="/?panel=1"):
    page.goto(BASE + path)
    page.wait_for_function("document.documentElement.lang.length === 2")
    page.wait_for_timeout(900)          # config, the loader, /api/onboarding, the sheet


def shown(page):
    return page.evaluate("!!document.querySelector('.ob') && document.body.classList.contains('onboarding')")


# ---------------------------------------------------------------- static
def strip_comments(src: str) -> str:
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # // comments, but not the // inside a URL
    return "\n".join(re.sub(r"(^|[^:\"'])//.*$", r"\1", line) for line in src.splitlines())


def test_static():
    for f in (WEB / "index.html", WEB / "onboarding/onboarding.js", WEB / "onboarding/onboarding.css"):
        code = strip_comments(f.read_text(encoding="utf-8"))
        check(f"{f.relative_to(ROOT)}: no em dash outside comments", "—" not in code,
              [l.strip()[:80] for l in code.splitlines() if "—" in l][:3])
    for f in (WEB / "onboarding/onboarding.js", WEB / "onboarding/onboarding.css"):
        check(f"{f.relative_to(ROOT)}: no em dash at all", "—" not in f.read_text(encoding="utf-8"))
        check(f"{f.relative_to(ROOT)}: no external assets",
              not re.search(r"https?://", strip_comments(f.read_text(encoding="utf-8"))))
    html = (WEB / "index.html").read_text(encoding="utf-8")
    keys = set(re.findall(r"\b(ob[0-9A-Z]\w*):", html))
    for table in ("he_f", "en", "ar", "ru"):
        m = re.search(rf"\n  {table}:\{{(.*?)\}},\n", html, flags=re.S)
        body = m.group(1) if m else ""
        missing = sorted(k for k in keys if f"{k}:" not in body)
        check(f"COPY.{table} has every onboarding string", m is not None and not missing, missing)
    js = (WEB / "onboarding/onboarding.js").read_text(encoding="utf-8")
    used = set(re.findall(r"""data-t="(ob\w+)"|t\("(ob\w+)"\)|"(ob[A-Z0-9]\w*)"|:"(ob[A-Z0-9]\w*)\"""", js))
    used = {x for tup in used for x in tup if x}
    check("every string the script uses exists in COPY", used <= keys, sorted(used - keys))
    check("the script adds no element with an id", not re.search(r"""\bid=["']""", js))
    css = (WEB / "onboarding/onboarding.css").read_text(encoding="utf-8")
    sels = [s.strip() for block in re.findall(r"(^|})\s*([^{}@]+)\{", strip_comments(css), flags=re.M)
            for s in block[1].split(",")]
    loose = [s for s in sels if s and not re.search(r"\.ob\b|\.ob-|body\.onboarding|^from$|^to$|^\d+%", s)]
    check("every onboarding rule is scoped to .ob or body.onboarding", not loose, loose[:5])


# ---------------------------------------------------------------- unit
def test_note_utterance_filters():
    """In-process: what counts as the turn Try it is waiting for."""
    os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="ob-unit-")
    sys.path.insert(0, str(ROOT))
    from savta import onboarding_api as ob
    ob._armed_at, ob._turn = 0.0, None
    ob.note_utterance(b'{"text":"what time is it"}')
    check("an utterance before Try it does not count", ob.status()["turn"] is None)
    ob.act("try")
    for body, why in ((b'{"text":"what time","speculative":true}', "a speculative guess"),
                      (b'{"text":"what time","source":"screen"}', "text that is not from the mic"),
                      (b'{"text":"   "}', "an empty press"),
                      (b'not json', "a broken body")):
        ob.note_utterance(body)
        check(f"{why} does not count", ob.status()["turn"] is None)
    ob.note_utterance(json.dumps({"text": "מה השעה?", "client": "native"}).encode())
    t = ob.status()["turn"]
    check("a real utterance after Try it is up counts, with what was heard",
          t == {"heard": "מה השעה?"}, t)
    ob.act("try")
    check("arming again forgets the last turn", ob.status()["turn"] is None)
    check("an unknown action is refused", ob.act("launch")[0] == 400)
    code, body = ob.open_pane("../../etc")
    check("only the three known panes can be opened", code == 400 and body["error"] == "unknown_pane")
    check("every pane is a System Settings privacy URL",
          all(u.startswith("x-apple.systempreferences:com.apple.preference.security?Privacy_")
              for u in ob.PANES.values()))


def test_request_unit():
    """In-process: /api/permissions/request asks for exactly the one kind, through the
    app when it shares the process, queued for a listener that does not, and never
    at all when it is already granted."""
    os.environ.pop("MICMIC_FAKE_PERMISSIONS", None)
    from savta import onboarding_api as ob
    ob._fake = None
    real = ob.permissions
    now = {"microphone": "not_asked", "speech": "not_asked", "accessibility": "denied", "ready": False}
    ob.permissions = lambda: dict(now)
    got: list[str] = []
    try:
        ob.set_requester(got.append)
        code, r = ob.request("microphone")
        check("request microphone reaches the app with microphone, and only that",
              code == 200 and got == ["microphone"] and r["asked"] is True and r["via"] == "app", (got, r))
        check("an unknown kind is refused and asks nothing",
              ob.request("camera")[0] == 400 and got == ["microphone"], got)
        now["speech"] = "granted"
        code, r = ob.request("speech")
        check("already granted: nothing is asked", r["asked"] is False and got == ["microphone"], (got, r))
        now["microphone"] = "denied"
        os.environ["MICMIC_DRY_OPEN"] = "1"
        code, r = ob.request("microphone")
        check("refused before: macOS will not ask again, so its pane opens instead",
              r.get("opened") == "microphone" and r["asked"] is False and got == ["microphone"], (got, r))
        code, r = ob.request("accessibility")
        check("Accessibility off is asked (its own prompt, then its pane)",
              got == ["microphone", "accessibility"] and r["asked"] is True, (got, r))
        ob.set_requester(None)
        ob.request("accessibility")
        check("a listener in another process: the click is queued once",
              ob.next_request() == {"k": "accessibility"} and ob.next_request() == {"k": None})
        ob.request("accessibility")
        ob._pending["at"] -= ob.PENDING_TTL + 1
        check("an old click nobody collected is never replayed later", ob.next_request() == {"k": None})
    finally:
        ob.permissions = real
        ob.set_requester(None)


def test_contacts_upgrade_from_an_older_setup():
    """A Mac set up before the just-in-time read has read her contacts at every launch,
    so its prompts were answered long ago: upgrading must not show MicMic's line on her
    first request about a person. A Mac set up with this version waits, as above."""
    from pathlib import Path
    from savta.actions import contacts as book
    from savta import router
    prev = (os.environ.get("MICMIC_STATE_DIR"), router.SETTINGS)

    def mac(settings):
        d = tempfile.mkdtemp(prefix="ob-contacts-up-")
        os.environ["MICMIC_STATE_DIR"] = d
        router.SETTINGS = Path(d) / "settings.json"
        if settings is not None:
            router.save_settings(settings)
    try:
        mac({"onboarded": True})
        check("an older setup counts as read before", book.read_before() and book.read_before("macos"))
        check("and it is remembered", book._marker()[0] == "legacy")
        # In an installed app the old answers were given to another build: once more.
        os.environ["MICMIC_CODE_IDENTITY"] = "1.2.5/aaaa1111"
        try:
            check("in an installed app, an older setup explains once on this build",
                  not book.read_before())
        finally:
            os.environ.pop("MICMIC_CODE_IDENTITY", None)
        mac({"onboarded": True, "contacts_gate": True})
        check("a setup done with this version still waits for a read of her own",
              not book.read_before())
        mac(None)
        check("a Mac never set up waits too", not book.read_before())
    finally:
        if prev[0] is None:
            os.environ.pop("MICMIC_STATE_DIR", None)
        else:
            os.environ["MICMIC_STATE_DIR"] = prev[0]
        router.SETTINGS = prev[1]


def test_contacts_wait_for_a_read_of_her_own():
    """Reading her address book is a macOS prompt the first time (Contacts Automation,
    or WhatsApp's "data from other apps"). On a Mac where no read has worked yet
    (no contacts_ok), only a turn about a person reads, after MicMic's own line; the
    listener's names at launch and the Try it turn read nothing. Once a read has
    worked, every turn reads, exactly as before."""
    os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="ob-contacts-")
    from savta.actions import contacts as book
    from savta import router, onboarding_api as ob
    reads: list[str] = []
    said: list[str] = []
    answer = {"yes": False}
    prev = (book._read_whatsapp, book._read_macos, book.recent_chats, book._copy_db,
            book._explain_first)
    prev_pins = (router.prof.load, router.load_config)
    # Pinned contacts from this Mac's own profile or config would answer the launch
    # read without a read: the fixture must not depend on who runs it.
    router.prof.load = lambda *a, **k: {}
    router.load_config = lambda *a, **k: {}
    # Nothing here may touch this Mac's real WhatsApp data or Contacts app.
    book.recent_chats = lambda limit=30: []
    book._copy_db = lambda name: None
    book._read_whatsapp = lambda: reads.append("whatsapp") or []
    book._read_macos = lambda: reads.append("macos") or [{"name": "Dana Levi", "first": "Dana",
                                                          "phone": "", "waid": "", "source": "macos"}]
    book.set_explainer(lambda: said.append("line") or answer["yes"])
    book._first_ok[0] = None
    book._CACHE.update(at=0.0, rows=[]); book._LAST_FAIL[0] = 0.0
    fresh = lambda: book._CACHE.update(at=0.0, rows=[]) or book._LAST_FAIL.__setitem__(0, 0.0)
    try:
        router.get_contacts(wait=0.0)
        time.sleep(0.3)
        check("fresh Mac: the listener's names at launch read nothing", reads == [], reads)
        for utt in ("what's the weather", "what time is it", "go to google", "מה מזג האוויר",
                    "какая погода", "كيف الطقس"):
            router.get_contacts(utt, wait=0.3)
        ob.act("try"); router.get_contacts("What time is it?", wait=0.3); ob.act("skip")
        time.sleep(0.3)
        check("fresh Mac: weather, time, Try it, in four languages: no read, no line",
              reads == [] and said == [], (reads, said))
        check("the person check sees sends and calls in four languages", all(
            router._about_a_person(u) for u in ("send Dana hi", "text mom", "call Gal", "tell Rina I'm late",
                                                 "remind me to call the doctor", "a message to Dana",
                                                 "on WhatsApp", "תשלחי לדנה שאני בדרך", "תתקשרי לאמא",
                                                 "اتصلي بأمي", "ابعت رسالة لدانا", "позвони маме",
                                                 "напиши Дане")))
        router.get_contacts("send Dana hi", wait=0.5)
        time.sleep(0.2)
        check("a send turn: MicMic's line first, and Not now reads nothing",
              said == ["line"] and reads == [], (said, reads))
        router.get_contacts("send Dana hi", wait=0.5)
        check("Not now holds for a while: not asked again at once", said == ["line"], said)
        book._first_ok[0] = None
        answer["yes"] = True
        router.get_contacts("send Dana hi", wait=2.0)
        for _ in range(40):
            if book.read_before():
                break
            time.sleep(0.05)
        check("her yes: the send turn is the first read, and it is remembered",
              said == ["line", "line"] and reads == ["whatsapp", "macos"] and book.read_before("macos"),
              (said, reads))
        router.PENDING = {"to": "Dana", "text": "hi"}
        try:
            check("a message counting down counts as about a person", router._about_a_person("yes"))
        finally:
            router.PENDING = None
        fresh(); reads.clear()
        router.get_contacts("what's the weather", wait=1.0)
        time.sleep(0.2)
        check("with contacts_ok: the weather turn reads, as it always did", reads != [], reads)
        fresh(); reads.clear()
        router.get_contacts(wait=1.0)
        time.sleep(0.2)
        check("with contacts_ok: the listener's names at launch read too", reads != [], reads)
        check("and no line is shown again", said == ["line", "line"], said)
    finally:
        (book._read_whatsapp, book._read_macos, book.recent_chats, book._copy_db,
         book._explain_first) = prev
        router.prof.load, router.load_config = prev_pins
        book._first_ok[0] = None


# ---------------------------------------------------------------- endpoints
def test_endpoints(srv):
    code, d = call("GET", "/api/onboarding")
    check("GET /api/onboarding answers", code == 200, code)
    # move and clash were added (2026-09-30) beside the original five, none changed;
    # moved_itself (2026-10-03) the same way.
    check("its shape is {show, first_run, onboarded, armed, turn, move, clash, moved_itself}",
          set(d) == {"show", "first_run", "onboarded", "armed", "turn", "move", "clash",
                     "moved_itself"} and d["moved_itself"] is False, d)
    check("a checkout is not on a disk image, and this test reads no one's hotkeys",
          d["move"]["needed"] is False and d["clash"] is None, d)
    check("a new Mac: first_run, not onboarded, so it shows",
          d["first_run"] is True and d["onboarded"] is False and d["show"] is True, d)

    code, p = call("GET", "/api/permissions")
    states = {"granted", "denied", "not_asked", "unknown"}
    check("GET /api/permissions answers", code == 200, code)
    check("its shape is {microphone, speech, accessibility, ready, backup_hotkey}",
          set(p) == {"microphone", "speech", "accessibility", "ready", "backup_hotkey"}, p)
    check("every permission is granted, denied, not_asked or unknown",
          all(p[k] in states for k in ("microphone", "speech", "accessibility")), p)
    check("accessibility is never not_asked (there is no prompt state for it)",
          p["accessibility"] != "not_asked", p)
    check("ready is exactly all three granted",
          p["ready"] is all(p[k] == "granted" for k in ("microphone", "speech", "accessibility")), p)

    code, o = call("POST", "/api/permissions/open", {"pane": "accessibility"})
    check("POST /api/permissions/open accessibility (dry run here)",
          code == 200 and o == {"ok": True, "opened": "accessibility", "dry": True}, o)
    code, o = call("POST", "/api/permissions/open", {"pane": "https://example.com"})
    check("a pane that is not one of the three is refused", code == 400, (code, o))
    code, o = call("POST", "/api/onboarding", {"action": "nope"})
    check("an unknown onboarding action is refused", code == 400, (code, o))
    code, o = call("POST", "/api/permissions/request", {"k": "camera"})
    check("POST /api/permissions/request: an unknown kind is refused", code == 400, (code, o))
    code, o = call("POST", "/api/permissions/request", {"k": "microphone"},
                   headers={"Origin": "https://example.com"})
    check("POST /api/permissions/request from another web page is refused", code == 403, (code, o))
    code, o = call("GET", "/api/permissions/next")
    check("GET /api/permissions/next answers, nothing queued", code == 200 and o == {"k": None}, o)

    # QA #29: the page never used the contact list, and "name" was written twice.
    req = urllib.request.urlopen(BASE + "/api/config", timeout=10)
    raw = req.read().decode()
    check("/api/config sends no contact list to the page", "contacts" not in json.loads(raw), raw[:200])
    check("/api/config has one name key", raw.count('"name"') == 1, raw.count('"name"'))
    code, c1 = call("GET", "/api/config?contacts=1")
    check("the listener's ?contacts=1 still gets them", isinstance(c1.get("contacts"), list), c1.keys())
    # A client dropping the connection is routine: no traceback on the console.
    import io, contextlib, socket as _s
    with socket.create_connection(("127.0.0.1", PORT)) as c:
        c.setsockopt(_s.SOL_SOCKET, _s.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    check("the server still answers after a client reset its connection",
          call("GET", "/api/health")[0] == 200)
    from savta import server as _srv
    err = io.StringIO()
    try:
        raise ConnectionResetError(54, "Connection reset by peer")
    except ConnectionResetError:
        with contextlib.redirect_stderr(err):
            _srv.Server.handle_error(type("S", (), {})(), None, ("127.0.0.1", 1))
    check("a dropped connection prints nothing", err.getvalue() == "", err.getvalue()[:200])

    code, s = call("POST", "/api/settings", {"onboarded": "yes"})
    check("the settings validator refuses a non-boolean onboarded",
          "onboarded" in s.get("rejected", []), s)
    check("config has not changed after a refused value", "onboarded" not in srv.settings())


def test_skip_persists(srv, browser):
    ctx, page = new_page(browser)
    settle(page)
    check("fresh Mac: the onboarding is up in the panel", shown(page))
    check("the page underneath is hidden while it is up",
          page.evaluate("getComputedStyle(document.querySelector('.stage')).visibility") == "hidden")
    page.keyboard.press("Escape")
    # Polled, not a fixed 700 ms: at load averages of 60 the closing animation and the
    # skip's POST took longer, and the check failed while the skip itself landed.
    for _ in range(40):
        if not shown(page) and not page.query_selector(".ob"):
            break
        page.wait_for_timeout(100)
    for _ in range(30):
        if call("GET", "/api/onboarding")[1].get("onboarded") is True:
            break
        page.wait_for_timeout(100)
    check("Esc skips it and it is gone", not shown(page) and not page.query_selector(".ob"))
    code, d = call("GET", "/api/onboarding")
    check("skip is saved on the server", d["onboarded"] is True and d["show"] is False, d)
    check("as the onboarded setting in settings.json", srv.settings().get("onboarded") is True,
          srv.settings())
    call("POST", "/api/settings", {"hotkey": "right-command"})
    check("a later Settings save keeps onboarded", srv.settings().get("onboarded") is True,
          srv.settings())
    call("POST", "/api/settings", {"hotkey": "right-option"})
    page.reload()
    page.wait_for_function("document.documentElement.lang.length === 2")
    page.wait_for_timeout(900)
    check("never again once onboarded, and skipping it finishes setup (first_run false)",
          not shown(page) and call("GET", "/api/config")[1]["first_run"] is False)
    check("no page errors", not page.__errors, page.__errors)
    ctx.close()


def test_only_first_run(browser):
    with Server(profile={"setup_complete": True, "step": "done", "language": "english"}):
        code, d = call("GET", "/api/onboarding")
        check("set up already: /api/onboarding says do not show", d["show"] is False, d)
        ctx, page = new_page(browser)
        settle(page)
        check("set up already: no onboarding in the panel", not shown(page))
        check("set up already: its script is not even fetched",
              not any("/onboarding/" in u for u in page.__requests), page.__requests)
        ctx.close()


def test_onboarding_language_choice(browser):
    """The first screen is only the language: "1 of 4", one question, the four as a
    radiogroup, keyboard- and click-reachable, and picking one is what she is then set
    up to listen and answer in."""
    with Server():
        ctx, page = new_page(browser)
        settle(page)
        check("the first screen is the language, 1 of 4, in the permission steps' frame",
              page.evaluate("document.querySelector('.ob-step.on').dataset.n") == "1"
              and page.get_attribute(".ob-pcard", "data-k") == "language"
              and page.text_content(".ob-pofn") == "1 of 4"
              and page.inner_text(".ob-step.on .ob-title") == "Which language will you speak?",
              (page.text_content(".ob-pofn"), page.inner_text(".ob-step.on .ob-title")))
        check("one main action: no Allow button, no Not now, Continue",
              page.is_hidden(".ob-allow") and page.is_hidden(".ob-ghost")
              and page.is_visible('.ob-next[data-go="next"]'))
        opts = page.evaluate(
            "[...document.querySelectorAll('.ob-lopt')].map(b => b.dataset.code)")
        check("it shows all four languages, in the page's own order",
              opts == ["he", "en", "ar", "ru"], opts)
        check("it is a radiogroup", page.get_attribute(".ob-lopts", "role") == "radiogroup")
        check("English (the default) starts checked", page.get_attribute(
            '.ob-lopt[data-code="en"]', "aria-checked") == "true")
        page.click('.ob-lopt[data-code="he"]')
        # The POST is fire-and-forget from the page's side, so poll rather than
        # guess how long the round trip takes under load.
        hint = None
        for _ in range(30):
            hint = call("GET", "/api/config")[1].get("language_hint")
            if hint == "he-IL":
                break
            page.wait_for_timeout(100)
        check("choosing Hebrew is what she is set up to listen and answer in",
              hint == "he-IL", hint)
        check("and the page repaints in Hebrew at once",
              page.evaluate("document.documentElement.lang") == "he"
              and page.evaluate("document.documentElement.dir") == "rtl")
        check("Hebrew is now the one checked", page.get_attribute(
            '.ob-lopt[data-code="he"]', "aria-checked") == "true")
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()


def test_onboarding_downloads_the_english_model(browser):
    """The owner's decision: English's local model downloads on its own from the
    onboarding (Apple hears every turn meanwhile). Here the downloader's seam stands in
    for the network: it must be CALLED for English, and never for Hebrew, whose
    recogniser is Apple's own ("Built in")."""
    with Server(perms={**NEW_MAC}) as srv:
        ctx, page = new_page(browser)
        settle(page)
        seam = srv.asr / "DOWNLOAD_REQUESTED"
        check("nothing is requested before she has settled on a language", not seam.exists())
        check("the language screen asks for no permission", page.__asks == [], page.__asks)
        page.click('.ob-next[data-go="next"]')      # English, as preselected
        st = {}
        for _ in range(40):
            st = call("GET", "/api/asr")[1]
            if st.get("state") == "downloading" and seam.exists():
                break
            page.wait_for_timeout(100)
        check("moving on from the language in English requests the English model",
              st.get("state") == "downloading" and seam.exists()
              and str(st.get("engine", "")).startswith("parakeet-unified-en"),
              (st, seam.exists()))
        line = ""
        for _ in range(30):
            line = page.evaluate("[...document.querySelectorAll('.ob-asr')].map(e => e.textContent).join('|')")
            if "0%" in line:
                break
            page.wait_for_timeout(100)
        check("the onboarding shows the download's progress", "0%" in line, line)
        check("and the next screen is the microphone, 2 of 4",
              page.get_attribute(".ob-pcard", "data-k") == "microphone"
              and page.text_content(".ob-pofn") == "2 of 4", page.text_content(".ob-pofn"))
        to_try_it(page)
        check("Try it still shows the download's progress line",
              page.is_visible('.ob-step[data-n="2"] .ob-asr')
              and "0%" in page.inner_text('.ob-step[data-n="2"] .ob-asr'))
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()
    with Server() as srv:
        ctx, page = new_page(browser)
        settle(page)
        page.click('.ob-lopt[data-code="he"]')
        page.wait_for_timeout(300)
        page.click('.ob-next[data-go="next"]')
        hint = state = None
        for _ in range(30):
            hint = call("GET", "/api/config")[1].get("language_hint")
            state = call("GET", "/api/asr")[1].get("state")
            if hint == "he-IL":
                break
            page.wait_for_timeout(100)
        page.wait_for_timeout(500)
        check("Hebrew: Apple's recogniser, nothing to download",
              hint == "he-IL" and state == "apple", (hint, state))
        check("and the downloader was never called for Hebrew",
              not (srv.asr / "DOWNLOAD_REQUESTED").exists())
        ctx.close()


def test_settings_asr_row(browser):
    """QA: the Settings row for the local recogniser (right after Speech language)
    shows what /api/asr says, per language."""
    with Server(profile={"setup_complete": True, "step": "done"},
                settings={"language_hint": "he-IL"}):
        ctx, page = new_page(browser, lang="he")
        page.goto(BASE + "/")
        page.wait_for_function("document.documentElement.lang.length === 2")
        page.wait_for_timeout(700)
        page.click("#gear")
        page.wait_for_timeout(300)
        check("Hebrew: Apple's recogniser, a hint and no button",
              not page.is_hidden("#hAsr") and page.is_hidden("#asrBtn"))
        ctx.close()
    with Server(profile={"setup_complete": True, "step": "done"},
                settings={"language_hint": "en-US"}):
        ctx, page = new_page(browser)
        page.goto(BASE + "/")
        page.wait_for_function("document.documentElement.lang.length === 2")
        page.wait_for_timeout(700)
        page.click("#gear")
        page.wait_for_timeout(300)
        check("English: a Download button naming the model's size",
              "733 MB" in page.inner_text("#asrBtn") and page.is_hidden("#hAsr"))
        ctx.close()


def _chip(browser, path="/"):
    """What the page shows with nothing remembered in the browser: no micmic.lang in
    localStorage, the way a freshly installed app's web view starts."""
    ctx = browser.new_context(viewport={"width": 380, "height": 560})
    ctx.add_init_script(INIT)
    page = ctx.new_page()
    page.goto(BASE + path)
    page.wait_for_function("document.documentElement.lang.length === 2")
    page.wait_for_timeout(600)
    got = (page.inner_text("#langnow"), page.evaluate("document.documentElement.lang"))
    ctx.close()
    return got


def test_speech_language_default(browser):
    """English unless she chose another language in Settings. A fresh 1.0.1 came up
    listening in Hebrew (listener log locale=he-IL, chip "HE") with en-US in the
    shipped config: profile.DEFAULT carried speech_lang he-IL and /api/config read
    that placeholder as a learned language."""
    with Server():
        cfg = call("GET", "/api/config")[1]
        check("a new Mac listens in English", cfg.get("language_hint") == "en-US", cfg.get("language_hint"))
        check("a new Mac's page shows EN", _chip(browser) == ("EN", "en"), _chip(browser))
        call("POST", "/api/settings", {"language_hint": "ru-RU"})
        check("choosing Russian in Settings is what it then listens in",
              call("GET", "/api/config")[1].get("language_hint") == "ru-RU")
        _, s = call("POST", "/api/settings", {"language_hint": "en-US"})
        check("and choosing English again is echoed back",
              s.get("settings", {}).get("language_hint") == "en-US", s)
    with Server(profile={"setup_complete": True, "step": "done", "language": "hebrew",
                         "speech_lang": "he-IL"}):
        cfg = call("GET", "/api/config")[1]
        check("a language setup only heard does not choose the recogniser",
              cfg.get("language_hint") == "en-US", cfg.get("language_hint"))
        check("but it is sent as the second language a turn is read in",
              cfg.get("language") == "hebrew", cfg.get("language"))
    with Server(profile={"setup_complete": True, "step": "done", "language": "hebrew"},
                settings={"language_hint": "he-IL"}):
        cfg = call("GET", "/api/config")[1]
        check("someone who chose Hebrew in Settings keeps Hebrew",
              cfg.get("language_hint") == "he-IL", cfg.get("language_hint"))
        check("and their page shows HE", _chip(browser) == ("HE", "he"), _chip(browser))
    with Server(settings={"language_hint": "xx-XX"}):
        check("a settings value that is not one of the four falls back to English",
              call("GET", "/api/config")[1].get("language_hint") == "en-US")


def _say(text):
    code, r = call("POST", "/api/utterance", {"text": text, "speak": False,
                                              "client": "native", "activation": "push"},
                   timeout=60)
    return r


def test_onboarded_ends_voice_setup():
    """QA #16: once the window's onboarding is finished or skipped, the old spoken setup
    never runs: no "What should I call you?" tacked onto answers, no greeting on a
    silent press. The owner's 1.0.1 was left with onboarded true and setup_complete
    false; that state is resolved to done."""
    with Server(profile={}, settings={"onboarded": True}) as srv:
        check("onboarded but never set up by voice: resolved to set up at start",
              call("GET", "/api/config")[1]["first_run"] is False
              and json.loads((srv.state / "profile.json").read_text()).get("setup_complete") is True)
        r = _say("")
        check("a silent press says nothing, no greeting", r.get("did") == "empty", r)
        r = _say("what time is it micmic")
        live_check("an answer has no setup question after it",
              r.get("did") == "answered" and (r.get("say") or "").startswith("It is")
              and "call you" not in (r.get("say") or ""), r.get("say"))
    with Server() as srv:
        r = _say("what time is it micmic")
        live_check("not onboarded yet: the answer comes first, then the one-line intro in English",
              (r.get("say") or "").startswith("It is")
              and (r.get("say") or "").endswith("I'm MicMic. What should I call you?"), r.get("say"))
        call("POST", "/api/onboarding", {"action": "done"})
        check("finishing the window finishes setup",
              json.loads((srv.state / "profile.json").read_text()).get("setup_complete") is True
              and call("GET", "/api/config")[1]["first_run"] is False)
        r = _say("what time is it")
        check("and the spoken setup question never comes back",
              "call you" not in (r.get("say") or ""), r.get("say"))


def test_other_modes(srv, browser):
    for path, what in (("/", "the browser page"), ("/?bar=1", "the bar")):
        ctx, page = new_page(browser)
        settle(page, path)
        check(f"{what}: no onboarding, not even its script, on a first run",
              not shown(page) and not any("/onboarding/" in u for u in page.__requests))
        ctx.close()


def test_flow(srv, browser):
    """Every screen, by keyboard, in order: the language, the microphone, speech
    recognition, Accessibility ("1 of 4" to "4 of 4"), then Try it, which a real turn
    completes."""
    ctx, page = new_page(browser)
    settle(page)
    on = lambda: page.evaluate("[document.querySelector('.ob-step.on').dataset.n, "
                               "document.querySelector('.ob-pcard').dataset.k, "
                               "document.querySelector('.ob-pofn').textContent]")
    order = [on()]
    check("the language first: step 1, 1 of 4", order[0] == ["1", "language", "1 of 4"], order)
    check("the dialog is labelled", page.get_attribute(".ob", "aria-label") == "Welcome to MicMic")
    check("one progress only: no second row of steps above it", not page.query_selector(".ob-prog"))
    # Keys: Space must not open the microphone, "d" must not open the engineer's panel.
    page.keyboard.press("Space")
    page.keyboard.press("d")
    check("Space and d do not reach the page while it is up",
          page.evaluate("window.__msgs.length") == 0
          and not page.evaluate("document.getElementById('dev').classList.contains('show')"))
    page.mouse.click(190, 300)
    check("a click on the screen does not arm the microphone", page.evaluate("window.__msgs.length") == 0)
    # Tab stays inside: the language pill and the screen's own buttons.
    inside = True
    for _ in range(6):
        page.keyboard.press("Tab")
        inside &= page.evaluate("""(() => { const a = document.activeElement;
            return !!(a.closest('.ob') || a.closest('#lang')); })()""")
    check("Tab stays inside the onboarding", inside)
    page.focus(".ob-step.on .ob-title")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    order.append(on())
    check("Enter moves on to the microphone, 2 of 4", order[-1] == ["1", "microphone", "2 of 4"], order)
    page.wait_for_timeout(600)
    check("nothing was asked on the way here: no request, all three still not asked",
          page.__asks == [] and call("GET", "/api/permissions")[1]["microphone"] == "not_asked",
          page.__asks)
    # By keyboard: Enter on each permission's screen is its Allow button.
    for i, k in enumerate(("microphone", "speech", "accessibility")):
        page.wait_for_function(f"document.querySelector('.ob-pcard').dataset.k === '{k}' && "
                               "document.querySelector('.ob-pcard').dataset.s === 'ask'", timeout=5000)
        if i:
            order.append(on())
        page.keyboard.press("Enter")
        page.wait_for_function(f"document.querySelector('.ob-pcard').dataset.s === 'granted' || "
                               f"document.querySelector('.ob-pcard').dataset.k !== '{k}' || "
                               "document.querySelector('.ob-step.on').dataset.n === '2'", timeout=5000)
    check("Enter on each step asked for exactly that one, in order",
          page.__asks == ["microphone", "speech", "accessibility"], page.__asks)
    check("the order is language, microphone, speech, Accessibility: 1 to 4 of 4", order == [
        ["1", "language", "1 of 4"], ["1", "microphone", "2 of 4"],
        ["1", "speech", "3 of 4"], ["1", "accessibility", "4 of 4"]], order)
    page.wait_for_selector('.ob-step[data-n="2"].on', timeout=4000)
    check("then Try it, which arms the server", call("GET", "/api/onboarding")[1]["armed"] is True)
    page.wait_for_selector(".ob-howto:not([hidden])", timeout=4000)
    check("Try it teaches the key: Hold [⌥] and say, then hold or tap",
          page.inner_text('.ob-step[data-n="2"] .ob-sub') == "Hold ⌥ and say"
          and page.text_content(".ob-kcap") == "⌥"
          and page.inner_text(".ob-howto") == "The ⌥ on the right. Let go to send. "
                                               "Or tap it once to start, and again to send.",
          (page.inner_text('.ob-step[data-n="2"] .ob-sub'), page.inner_text(".ob-howto")))
    page.wait_for_timeout(400)
    page.click(".ob-orb")
    check("the circle hands the press to the listener", page.evaluate("window.__msgs") == ["listen"],
          page.evaluate("window.__msgs"))
    # A real utterance, the way the listener sends one, speech off.
    code, r = call("POST", "/api/utterance", {"text": "What time is it?", "speak": False,
                                               "client": "native"}, timeout=60)
    check("the utterance went through", code == 200, code)
    live_check("the window is introducing MicMic: no spoken setup question on the try",
          "call you" not in (r.get("say") or "") and (r.get("say") or "").startswith("It is"),
          r.get("say"))
    page.wait_for_selector(".ob-step.ok", timeout=5000)
    check("Try it completes on the real turn, showing what was heard",
          page.inner_text(".ob-qtext") == "What time is it?" and page.inner_text(".ob-step.ok .ob-title") == "You're set.")
    check("finishing is saved at once", call("GET", "/api/onboarding")[1]["onboarded"] is True)
    page.wait_for_selector(".ob", state="detached", timeout=6000)
    check("then it closes into the normal window", not shown(page) and page.evaluate(
        "getComputedStyle(document.querySelector('.stage')).visibility") == "visible")
    check("the page's hooks are its own again", page.evaluate(
        "!String(window.panelState).includes('live(')"))
    check("the language pill is back in its place",
          page.evaluate("getComputedStyle(document.getElementById('lang')).right") == "52px")
    check("no page errors", not page.__errors, page.__errors)
    ctx.close()


def test_no_key_before_permissions(browser):
    """The owner's 1.2.4 fresh install: the first screen showed the language AND a
    "Hold ⌘ and talk" key demo with a fake "Play some music" card; he held right ⌘,
    nothing happened (no microphone yet), and it felt broken. Now no screen before the
    permissions are through names, draws or animates a key, and the teaching is on Try
    it: her actual key (right ⌘ here: Handy has right ⌥), hold it or tap it."""
    home = fake_home(handy="option_right")
    with Server(perms={**NEW_MAC, "delay": 0.2}, env={"MICMIC_DICTATION_HOME": str(home)}):
        ctx, page = new_page(browser)
        settle(page)
        screens = []

        def look():
            screens.append({
                "k": page.get_attribute(".ob-pcard", "data-k"),
                "text": page.inner_text(".ob"),
                "drawn": page.evaluate("[...document.querySelectorAll('.ob-step.on .ob-kcap, .ob-demo, "
                                       ".ob-kbd, .ob-key, .ob-bar')].filter(e => e.getClientRects().length)"
                                       ".map(e => e.className)"),
                "howto": page.is_visible(".ob-howto")})
            # The one exception (feat/backup-hotkey, owner 2026-10-03): Accessibility's
            # why line names her key ("So Right ⌘ works from any app"). Named in words,
            # never drawn, and no how-to before Try it.
            if screens[-1]["k"] == "accessibility":
                why = page.inner_text('.ob-step[data-n="1"] .ob-sub')
                screens[-1]["text"] = screens[-1]["text"].replace(why, "") if why == page.evaluate(
                    "t('obAxWhy')") else screens[-1]["text"]
        look()
        check("the first screen is only the language: no key, no demo card, no hint to try",
              screens[0]["k"] == "language" and not screens[0]["drawn"] and not screens[0]["howto"]
              and not any(w in screens[0]["text"] for w in
                          ("⌘", "⌥", "Hold", "tap it", "Play some music", "Playing music")),
              screens[0])
        page.click('.ob-next[data-go="next"]')
        for k in ("microphone", "speech", "accessibility"):
            page.wait_for_selector(f'.ob-pcard[data-k="{k}"][data-s="ask"]', timeout=5000)
            look()
            page.click(".ob-allow")
        check("no permission screen names or draws a key either",
              [s["k"] for s in screens] == ["language", "microphone", "speech", "accessibility"]
              and not any(s["drawn"] or s["howto"] or "⌘" in s["text"] or "⌥" in s["text"]
                          or "Hold" in s["text"] for s in screens),
              [s for s in screens if s["drawn"] or "⌘" in s["text"] or "⌥" in s["text"]][:1])
        page.wait_for_selector('.ob-step[data-n="2"].on', timeout=6000)
        page.wait_for_selector(".ob-howto:not([hidden])", timeout=4000)
        check("Try it, after them: her own key drawn as a key (right ⌘, Handy has right ⌥)",
              page.text_content(".ob-kcap") == "⌘"
              and page.inner_text('.ob-step[data-n="2"] .ob-sub') == "Hold ⌘ and say",
              page.inner_text('.ob-step[data-n="2"] .ob-sub'))
        check("and how: hold it and let go, or tap it once and again",
              page.inner_text(".ob-howto") == "The ⌘ on the right. Let go to send. "
                                              "Or tap it once to start, and again to send.",
              page.inner_text(".ob-howto"))
        check("with the example to say, and the circle still there to click",
              page.inner_text(".ob-qtext") == "What time is it?" and page.is_visible(".ob-orbt"))
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()


def test_permission_steps(browser):
    """One permission at a time, in order; nothing is asked before she clicks; each
    button asks for its own; Not now moves on; a refusal is shown, not hidden."""
    with Server(perms={**NEW_MAC, "answer": {"microphone": "denied"}, "delay": 0.3}):
        ctx, page = new_page(browser)
        settle(page)
        check("the page loaded and nothing was asked", page.__asks == [] and all(
            call("GET", "/api/permissions")[1][k] == v for k, v in NEW_MAC.items()), page.__asks)
        page.click(".ob-act button:not([hidden])")               # past the language
        page.wait_for_timeout(600)
        seen = [page.evaluate("document.querySelector('.ob-pcard').dataset.k")]
        check("after the language, one permission at a time, the microphone first",
              seen == ["microphone"] and page.inner_text('.ob-step[data-n="1"] .ob-title') == "Microphone")
        check("its button is Allow microphone, and Not now is there",
              page.inner_text(".ob-allow") == "Allow microphone" and page.inner_text(".ob-ghost") == "Not now")
        check("its line says why, in one line", page.inner_text('.ob-step[data-n="1"] .ob-sub')
              == "So MicMic can hear you.")
        page.click(".ob-ghost")                                   # Not now
        page.wait_for_timeout(500)
        seen.append(page.evaluate("document.querySelector('.ob-pcard').dataset.k"))
        check("Not now moves on, and asks nothing", page.__asks == [] and seen[-1] == "speech", (seen, page.__asks))
        check("speech says why it is needed even for English",
              page.inner_text('.ob-step[data-n="1"] .ob-sub') == "To turn your voice into text on your Mac.")
        page.click(".ob-ghost")
        page.wait_for_timeout(500)
        seen.append(page.evaluate("document.querySelector('.ob-pcard').dataset.k"))
        check("then Accessibility, 4 of 4", seen == ["microphone", "speech", "accessibility"]
              and page.text_content(".ob-pofn") == "4 of 4", (seen, page.text_content(".ob-pofn")))
        page.click(".ob-allow")
        page.wait_for_timeout(150)
        check("its button posts accessibility, and only that", page.__asks == ["accessibility"], page.__asks)
        check("while it is off: turn MicMic on, then come back",
              page.inner_text('.ob-step[data-n="1"] .ob-sub') == "Turn MicMic on in the list, then come back here.")
        page.wait_for_selector('.ob-step[data-n="2"].on', timeout=5000)
        check("MicMic sees the switch and moves on by itself", True)
        check("Try it without the microphone: typing still works, and Done",
              page.inner_text(".ob-nohear").startswith("Without the microphone")
              and page.is_visible('[data-go="done"]'), page.inner_text(".ob-nohear"))
        ctx.close()

        # The same Mac, a second window: the microphone asked for and refused.
        ctx, page = new_page(browser)
        settle(page)
        page.click(".ob-act button:not([hidden])")
        page.wait_for_selector('.ob-pcard[data-k="microphone"][data-s="ask"]', timeout=4000)
        page.click(".ob-allow")
        check("the microphone button posts microphone", page.__asks == ["microphone"], page.__asks)
        page.wait_for_selector('.ob-pcard[data-s="denied"]', timeout=4000)
        check("refused: it says so, calmly, and offers Settings and Continue",
              page.inner_text('.ob-step[data-n="1"] .ob-sub') == "You can turn it on later in Settings."
              and page.inner_text(".ob-allow") == "Open Settings"
              and page.is_visible('.ob-act .ob-next[data-go="next"]') and not page.is_visible(".ob-ghost"))
        page.click(".ob-allow")
        page.wait_for_timeout(300)
        check("Open Settings goes through the same request (the server opens the pane)",
              page.__asks == ["microphone", "microphone"], page.__asks)
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()


def test_settings_permissions(browser):
    """Settings > Permissions: each missing one has its own button, the same request."""
    with Server(profile={"setup_complete": True, "step": "done", "language": "english"},
                perms={"microphone": "denied", "speech": "not_asked", "accessibility": "granted",
                       "delay": 0.2}):
        ctx, page = new_page(browser)
        settle(page, "/?panel=1")
        page.evaluate("openSettings()")
        page.wait_for_timeout(700)
        rows = page.evaluate("""[...document.querySelectorAll('.sperm')].map(r => [r.dataset.k, r.dataset.s,
            r.querySelector('.spbtn').hidden ? '' : r.querySelector('.spbtn').textContent])""")
        check("Settings lists the three, each with its state and button", rows == [
            ["microphone", "denied", "Open Settings"], ["speech", "not_asked", "Allow speech recognition"],
            ["accessibility", "granted", ""]], rows)
        page.click('.sperm[data-k="speech"] .spbtn')
        page.wait_for_function("document.querySelector('.sperm[data-k=\"speech\"]').dataset.s === 'granted'",
                               timeout=5000)
        check("its button asks for that one, and the row follows the answer", page.__asks == ["speech"], page.__asks)
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()


def test_languages_and_motion(browser):
    """Four languages, both directions, both schemes, and reduced motion. No em dash in
    anything a person can read on any screen."""
    speech = {"he": "he-IL", "en": "en-US", "ar": "ar-SA", "ru": "ru-RU"}
    for lang, rtl in (("he", True), ("en", False), ("ar", True), ("ru", False)):
        # The page's language is the server's speech language; the browser's saved one
        # only covers the moment before /api/config answers.
        with Server(settings={"language_hint": speech[lang]},
                    perms={**NEW_MAC, "answer": {"speech": "denied"}, "delay": 0.3}):
            ctx, page = new_page(browser, lang=lang, scheme="dark" if rtl else "light",
                                 reduced="reduce" if lang == "ar" else "no-preference")
            settle(page)
            texts, over = [], []

            def measure():
                texts.append(page.inner_text(".ob"))
                over.extend(page.evaluate("""(() => [...document.querySelectorAll('.ob-step.on *')]
                    .filter(e => { const r = e.getBoundingClientRect();
                                   return r.width && (r.left < -1 || r.right > innerWidth + 1); })
                    .map(e => e.className))()"""))
                over.extend([] if page.evaluate(
                    "document.scrollingElement.scrollWidth <= innerWidth") else ["page scrolls sideways"])

            def card(k, st):
                page.wait_for_function(f"(() => {{ const c = document.querySelector('.ob-pcard');"
                                       f" return c.dataset.k === '{k}' && c.dataset.s === '{st}'; }})()",
                                       timeout=5000)
                page.wait_for_timeout(250)

            sub = lambda: page.inner_text('.ob-step[data-n="1"] .ob-sub')
            setup = []                                              # every screen before Try it
            measure(); setup.append(texts[-1])                      # 1 of 4: the language
            check(f"{lang}: the first screen is the language, 1 of 4, with its why line",
                  page.get_attribute(".ob-pcard", "data-k") == "language"
                  and page.text_content(".ob-pofn") == page.evaluate("t('obPermOf')").replace("{n}", "1")
                  and sub() == page.evaluate("t('obLangWhy')"), (page.text_content(".ob-pofn"), sub()))
            page.click(".ob-act button:not([hidden])")
            card("microphone", "ask"); measure(); setup.append(texts[-1])
            check(f"{lang}: then the microphone, 2 of 4, with its why line",
                  page.text_content(".ob-pofn") == page.evaluate("t('obPermOf')").replace("{n}", "2")
                  and sub() == page.evaluate("t('obMicWhy')"), (page.text_content(".ob-pofn"), sub()))
            page.click(".ob-allow")
            card("microphone", "granted"); measure()
            check(f"{lang}: done state says it is on, with a tick",
                  sub() == page.evaluate("t('obPermOn')") and page.is_visible(".ob-ptick"), sub())
            card("speech", "ask"); measure(); setup.append(texts[-1])
            page.click(".ob-allow")
            card("speech", "denied"); measure(); setup.append(texts[-1])
            check(f"{lang}: denied state says it can be turned on later in Settings",
                  sub() == page.evaluate("t('obPermDenied')")
                  and page.inner_text(".ob-allow") == page.evaluate("t('obOpenSettings')"), sub())
            page.click('.ob-act .ob-next[data-go="next"]')
            card("accessibility", "ask"); measure(); setup.append(texts[-1])
            # Except Accessibility's why line, which names her key in words (owner, 2026-10-03).
            setup[-1] = setup[-1].replace(page.evaluate("t('obAxWhy')"), "")
            check(f"{lang}: no screen before Try it names or shows a key",
                  not any(g in x for x in setup for g in "⌥⌘⌃⇧")
                  and not page.query_selector(".ob-step.on .ob-kcap, .ob-demo, .ob-kbd"),
                  [x for x in setup if any(g in x for g in "⌥⌘⌃⇧")][:1])
            page.click(".ob-ghost")                                  # Not now
            page.wait_for_selector('.ob-step[data-n="2"].on', timeout=4000)
            page.wait_for_timeout(600)
            measure()
            check(f"{lang}: without speech, Try it says what still works (typing), with Done",
                  page.inner_text(".ob-nohear") == page.evaluate("t('obNoSpeech')")
                  and page.is_visible('[data-go="done"]') and not page.is_visible(".ob-pill"),
                  page.inner_text(".ob-nohear"))
            check(f"{lang}: direction is {'rtl' if rtl else 'ltr'}",
                  page.evaluate("document.documentElement.dir") == ("rtl" if rtl else "ltr"))
            check(f"{lang}: no em dash on any screen", not any("—" in x for x in texts))
            check(f"{lang}: nothing runs off the 380px window", not over, over[:4])
            check(f"{lang}: no step 1 key demo is left in the page at all",
                  not page.query_selector(".ob-demo, .ob-kbd, .ob-key, .ob-prog"))
            if lang == "ar":
                # A second window on the same, still unfinished Mac (reduced motion):
                # it starts again on the language. (Skipping here and then writing
                # onboarded false no longer brings it back: skipping now finishes setup.)
                ctx2, p2 = new_page(browser, lang="ar", reduced="reduce")
                settle(p2)
                check("a second window starts on the language again, 1 of 4", p2.get_attribute(
                    ".ob-pcard", "data-k") == "language" and p2.evaluate(
                    "document.querySelector('.ob-step.on').dataset.n") == "1")
                ctx2.close()
            ctx.close()


# ---------------------------------------------------------------- the owner's fresh install
# 1.2.1, 2026-09-30, a real first run: MicMic opened straight from the disk image, Handy
# answered right Option, Try it said "Hold ⌥" with Accessibility off, the download line
# stuck at 93% after the model was ready, and the circle did not look like a button.
MOVE_STEPS = ("copy", "trash", "rename", "relaunch", "quit")


def fake_home(handy=None, superwhisper=None) -> Path:
    """A home with another dictation app's settings in it, the way each app writes them."""
    import plistlib
    home = Path(tempfile.mkdtemp(prefix="ob-test-home-"))
    if handy:
        f = home / "Library/Application Support/com.pais.handy/settings_store.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"settings": {"post_process_api_keys": {"x": "never read"},
            "bindings": {"transcribe": {"id": "transcribe", "current_binding": handy},
                         "cancel": {"id": "cancel", "current_binding": "escape"}}}}))
    if superwhisper:
        f = home / "Library/Preferences/com.superduper.superwhisper.plist"
        f.parent.mkdir(parents=True)
        with open(f, "wb") as fh:
            plistlib.dump({"KeyboardShortcuts_pushToTalk": json.dumps(superwhisper),
                           "recordingViewEnabled": False}, fh)
    return home


def to_try_it(page):
    """Past the language (Continue), and past any permission still to be asked (Not now)."""
    for _ in range(60):
        if page.evaluate("document.querySelector('.ob-step.on').dataset.n") == "2":
            page.wait_for_timeout(300)
            return
        for sel in (".ob-ghost", '.ob-next[data-go="next"]'):
            if page.is_visible(sel):
                try:
                    page.click(sel, timeout=800)
                except Exception:  # noqa: BLE001  (it moved on by itself meanwhile)
                    pass
                break
        page.wait_for_timeout(150)
    raise AssertionError("never reached Try it")


def test_move_unit():
    """Fix 1, in-process: where a copy runs from, and what a move does, all faked."""
    sys.path.insert(0, str(ROOT))
    from savta import install_place as ip
    check("/Volumes/MicMic/MicMic.app is on a disk image",
          ip.kind("/Volumes/MicMic/MicMic.app") == "disk_image")
    check("an App Translocation path is translocated", ip.kind(
        "/private/var/folders/x9/T/AppTranslocation/0A1B/d/MicMic.app") == "translocated")
    check("/Applications and ~/Applications are fine",
          ip.kind("/Applications/MicMic.app") is None
          and ip.kind(str(Path.home() / "Applications/MicMic.app")) is None)
    check("any read-only volume counts as a disk image", ip.kind(
        "/Users/x/Downloads/MicMic.app", read_only=lambda p: True) == "disk_image"
        and ip.kind("/Users/x/Downloads/MicMic.app", read_only=lambda p: False) is None)
    check("a source checkout never asks to move", ip.kind(None) is None)

    class Rec(ip.RealOps):
        def __init__(self, existing=False, fail=""):
            self.existing, self.fail, self.steps = existing, fail, []

        def writable(self, d):
            return str(d) == "/Applications"

        def exists(self, p):
            return self.existing and p.name == "MicMic.app"

        def _do(self, step, *a):
            self.steps.append((step, *a))
            return step != self.fail

        def trash(self, p): return self._do("trash", str(p))
        def copy(self, src, dest): return self._do("copy", src, str(dest))
        def rename(self, a, b): return self._do("rename", str(a), str(b))
        def relaunch(self, dest, pid, volume): self._do("relaunch", str(dest), volume)
        def quit(self): self._do("quit")

    ip._moving = False
    ops = Rec(existing=True)
    code, r = ip.move(ops=ops, src="/Volumes/MicMic/MicMic.app")
    time.sleep(0.9)                           # the quit waits for the reply to go out
    names = [x[0] for x in ops.steps]
    check("move: copy beside it, the old one to the Trash, the copy takes its name, "
          "reopen, quit", r.get("ok") and names == list(MOVE_STEPS), (r, ops.steps))
    check("the copy lands in /Applications, the old MicMic.app only ever goes to the Trash",
          ops.steps[0][2] != "/Applications/MicMic.app"
          and ops.steps[1] == ("trash", "/Applications/MicMic.app")
          and ops.steps[2][2] == "/Applications/MicMic.app", ops.steps)
    check("it reopens from Applications and ejects the image it ran from",
          ops.steps[3] == ("relaunch", "/Applications/MicMic.app", "/Volumes/MicMic"), ops.steps[3])
    code, r = ip.move(ops=Rec(), src="/Volumes/MicMic/MicMic.app")
    check("a second press after a move does nothing", r.get("error") == "already_moving", r)
    ip._moving = False
    ops = Rec(existing=True, fail="copy")
    code, r = ip.move(ops=ops, src="/Volumes/MicMic/MicMic.app")
    check("a failed copy leaves the MicMic already in Applications alone",
          not r.get("ok") and ("trash", "/Applications/MicMic.app") not in ops.steps
          and "quit" not in [x[0] for x in ops.steps], ops.steps)
    ops = Rec()
    code, r = ip.move(ops=ops, src="/private/var/folders/x/T/AppTranslocation/1/d/MicMic.app")
    time.sleep(0.9)
    check("translocated: moved and reopened, no disk image to eject, nothing trashed",
          r.get("ok") and ops.steps[-2][2] is None and not any(x[0] == "trash" for x in ops.steps),
          ops.steps)
    ip._moving = False
    ops = Rec()
    check("an app already in Applications is never moved", ip.move(
        ops=ops, src="/Applications/MicMic.app")[1].get("error") == "not_needed" and not ops.steps)
    os.environ["MICMIC_BUNDLE_PATH"] = "/Volumes/MicMic/MicMic.app"
    try:
        check("a faked location always means faked file operations",
              isinstance(ip._ops(), ip.FakeOps))
    finally:
        os.environ.pop("MICMIC_BUNDLE_PATH", None)
    src = (ROOT / "savta/install_place.py").read_text()
    check("the move never removes a file (no rm, unlink, rmtree, shutil.move)",
          not re.search(r"\brm\b|unlink|rmtree|os\.remove|shutil\.move", src))


def test_relocate_unit():
    """Run from the disk image (the owner opened MicMic in the DMG window with a copy
    already in /Applications): a silent move at launch, all faked."""
    sys.path.insert(0, str(ROOT))
    from savta import install_place as ip
    old_state = os.environ.get("MICMIC_STATE_DIR")
    st = Path(tempfile.mkdtemp(prefix="ob-test-relocate-"))
    os.environ["MICMIC_STATE_DIR"] = str(st)

    class Rec(ip.RealOps):
        def __init__(self, existing=None, mine="1.2.4", fail=""):
            self.existing, self.mine, self.fail, self.steps = existing, mine, fail, []

        def writable(self, d):
            return str(d) == "/Applications"

        def exists(self, p):
            return (self.existing is not None and str(p) == "/Applications/MicMic.app")

        def version(self, app):
            return self.existing if str(app).startswith("/Applications/") else self.mine

        def _do(self, step, *a):
            self.steps.append((step, *a))
            return step != self.fail

        def trash(self, p): return self._do("trash", str(p))
        def copy(self, src, dest): return self._do("copy", src, str(dest))
        def rename(self, a, b): return self._do("rename", str(a), str(b))
        def relaunch(self, dest, pid, volume): self._do("relaunch", str(dest), volume)
        def quit(self): self._do("quit")

    DMG = "/Volumes/MicMic/MicMic.app"
    t0 = 1_000_000.0

    def run(ops, src=DMG, at=None):
        nonlocal t0
        t0 += 1000                               # past the one-try window each time
        return ip.relocate_at_launch(ops=ops, src=src, now=at or t0)

    def note():
        f = st / ip.MOVED_NOTE
        return json.loads(f.read_text()) if f.exists() else None

    try:
        ops = Rec(existing="1.2.4")
        check("DMG + the same version in Applications: that copy opens, this one quits",
              run(ops) == "opened"
              and ops.steps == [("relaunch", "/Applications/MicMic.app", "/Volumes/MicMic")],
              ops.steps)
        check("nothing copied, nothing trashed, no line on the screen", note() is None)
        ops = Rec(existing="1.3.0")
        check("a newer copy in Applications opens too, untouched",
              run(ops) == "opened" and [x[0] for x in ops.steps] == ["relaunch"], ops.steps)
        ops = Rec(existing="1.2.3")
        check("an older copy in Applications: this one is copied in, the old one to the Trash",
              run(ops) == "moved"
              and [x[0] for x in ops.steps] == ["copy", "trash", "rename", "relaunch"]
              and ops.steps[1] == ("trash", "/Applications/MicMic.app")
              and ops.steps[3] == ("relaunch", "/Applications/MicMic.app", "/Volumes/MicMic"),
              ops.steps)
        (st / ip.MOVED_NOTE).write_text("{}")
        ops = Rec(existing=None)
        check("none in Applications: copied there (ditto beside it, then its name), "
              "opened from there", run(ops) == "moved"
              and [x[0] for x in ops.steps] == ["copy", "rename", "relaunch"]
              and ops.steps[0][1] == DMG and ops.steps[2][1] == "/Applications/MicMic.app",
              ops.steps)
        n = note()
        check("and the copy in Applications is told it moved itself",
              n and n.get("to") == "/Applications/MicMic.app" and n.get("from") == DMG, n)
        at = n["at"]
        check("its first screen shows the line, while she is on it",
              ip.moved_note(now=at + 2) and ip.moved_note(now=at + 2 + ip.NOTE_FOR_S - 1))
        check("and never again after that", not ip.moved_note(now=at + 2 + ip.NOTE_FOR_S + 1))
        ops = Rec(existing=None, fail="copy")
        check("a copy that fails: run from the disk image as before, nothing opened",
              run(ops) == "failed" and "relaunch" not in [x[0] for x in ops.steps], ops.steps)
        ops = Rec(existing=None)
        first = run(ops)
        again = ip.relocate_at_launch(ops=Rec(existing=None), src=DMG, now=t0 + 5)
        check("it tries once: a relaunch that comes straight back here runs as is",
              first == "moved" and again == "skipped", (first, again))
        ops = Rec(existing=None, mine="1.2.4")
        ops.existing = "garbage"
        ops.exists = lambda p: str(p) == "/Applications/MicMic.app"
        check("a copy in Applications whose version cannot be read is left alone",
              run(ops) == "failed" and not ops.steps, ops.steps)
        ops = Rec(existing="1.2.4")
        trans = "/private/var/folders/x/T/AppTranslocation/1/d/MicMic.app"
        check("translocated: the copy in Applications opens, no image to eject",
              run(ops, src=trans) == "opened"
              and ops.steps == [("relaunch", "/Applications/MicMic.app", None)], ops.steps)
        ops = Rec(existing="1.2.4")
        check("from Applications (or a checkout) nothing happens at all",
              run(ops, src="/Applications/MicMic.app") == "stay"
              and ip.relocate_at_launch(ops=ops, src=None) == "stay" and not ops.steps,
              ops.steps)
        ops = Rec(existing=None)
        ops.copy = lambda *a: (_ for _ in ()).throw(OSError("disk full"))
        check("an exception anywhere is a failure, never a crash", run(ops) == "failed")
        os.environ["MICMIC_BUNDLE_PATH"] = DMG
        os.environ["MICMIC_FAKE_MOVE"] = json.dumps({"existing": True, "existing_version": "1.2.4"})
        try:
            t0 += 1000
            r = ip.relocate_at_launch(now=t0)
            log = st / "move_log.jsonl"
            steps = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
            check("MICMIC_FAKE_MOVE: the seam the listener uses records the relaunch only",
                  r == "opened" and [x["step"] for x in steps] == ["relaunch"]
                  and steps[0]["dest"] == "/Applications/MicMic.app", (r, steps))
        finally:
            os.environ.pop("MICMIC_BUNDLE_PATH", None)
            os.environ.pop("MICMIC_FAKE_MOVE", None)
        lsrc = (ROOT / "native/listener.py").read_text()
        body = lsrc[lsrc.index("def main() -> None:"):]
        check("the listener relocates before its server starts and before anything is asked",
              "def relocate_from_disk_image()" in lsrc
              and body.index("relocate_from_disk_image()") < body.index("ensure_server()")
              < body.index("app.run()"))
        src = (ROOT / "savta/install_place.py").read_text()
        check("still no rm, unlink, rmtree or shutil.move in the move",
              not re.search(r"\brm\b|unlink|rmtree|os\.remove|shutil\.move", src))
    finally:
        if old_state is None:
            os.environ.pop("MICMIC_STATE_DIR", None)
        else:
            os.environ["MICMIC_STATE_DIR"] = old_state


def test_moved_itself_line(browser):
    """The copy the disk image put in Applications: one small line on the first screen."""
    with Server(env={"MICMIC_BUNDLE_PATH": "/Applications/MicMic.app"}) as srv:
        (srv.state / "moved_itself.json").write_text(json.dumps(
            {"at": time.time(), "from": "/Volumes/MicMic/MicMic.app",
             "to": "/Applications/MicMic.app"}))
        ctx, page = new_page(browser)
        settle(page)
        check("the language screen says MicMic moved itself to Applications",
              page.evaluate("document.querySelector('.ob-step.on').dataset.n") == "1"
              and page.is_visible(".ob-movednote")
              and page.inner_text(".ob-movednote") == "MicMic moved itself to Applications.",
              page.inner_text(".ob-step.on"))
        page.click('.ob-lopt[data-code="en"]')
        page.wait_for_timeout(300)
        page.click('.ob-next[data-go="next"]')
        page.wait_for_function("document.querySelector('.ob-step.on').dataset.n !== '1'"
                               " || document.querySelector('.ob-pcard').dataset.k !== 'language'",
                               timeout=4000)
        check("only on the first screen",
              page.evaluate("document.querySelector('.ob-movednote').hidden"))
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()
    with Server(env={"MICMIC_BUNDLE_PATH": "/Applications/MicMic.app"}):
        ctx, page = new_page(browser)
        settle(page)
        check("an ordinary first run has no such line", not page.is_visible(".ob-movednote"))
        ctx.close()


def test_move_screen(browser):
    """Fix 1: running from the disk image, step 0 is "Move MicMic to Applications".
    Set aside by the owner (2026-10-03): off unless MICMIC_MOVE_SCREEN=1."""
    with Server(env={"MICMIC_BUNDLE_PATH": "/Volumes/MicMic/MicMic.app"}):
        ctx, page = new_page(browser)
        settle(page)
        check("by default, even from the disk image: no move screen, the language first",
              page.evaluate("document.querySelector('.ob-step.on').dataset.n") == "1")
        ctx.close()
    env = {"MICMIC_BUNDLE_PATH": "/Volumes/MicMic/MicMic.app", "MICMIC_MOVE_SCREEN": "1",
           "MICMIC_FAKE_MOVE": json.dumps({"existing": True})}
    with Server(perms={**NEW_MAC}, env=env) as srv:
        ctx, page = new_page(browser)
        settle(page)
        check("from the disk image, the first screen is the move, before the language",
              page.evaluate("document.querySelector('.ob-step.on').dataset.n") == "0"
              and page.inner_text(".ob-step.on .ob-title") == "Move MicMic to Applications",
              page.inner_text(".ob-step.on"))
        check("it says why, and offers Move and reopen, and Not now (no Skip)",
              "disk image" in page.inner_text(".ob-movesub")
              and page.inner_text(".ob-movego") == "Move and reopen"
              and page.inner_text(".ob-ghost") == "Not now"
              and page.evaluate("getComputedStyle(document.querySelector('.ob-skip')).visibility") == "hidden")
        check("nothing was asked before it", page.__asks == [], page.__asks)
        page.click(".ob-movego")
        page.wait_for_function("document.querySelector('.ob-movego').textContent.startsWith('Reopening')",
                               timeout=4000)
        page.wait_for_timeout(900)
        log = srv.state / "move_log.jsonl"
        steps = [json.loads(x)["step"] for x in log.read_text().splitlines()] if log.exists() else []
        check("Move copies, trashes the old copy, takes its place, reopens, quits",
              steps == list(MOVE_STEPS), steps)
        check("while it reopens, the button cannot be pressed again",
              page.is_disabled(".ob-movego"))
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()
    env = {"MICMIC_BUNDLE_PATH": "/private/var/folders/x/T/AppTranslocation/1/d/MicMic.app",
           "MICMIC_MOVE_SCREEN": "1", "MICMIC_FAKE_MOVE": json.dumps({"fail": "copy"})}
    with Server(env=env) as srv:
        ctx, page = new_page(browser)
        settle(page)
        check("a translocated copy: the same screen, its own reason",
              page.evaluate("document.querySelector('.ob-step.on').dataset.n") == "0"
              and "temporary copy" in page.inner_text(".ob-movesub"), page.inner_text(".ob-movesub"))
        page.click(".ob-movego")
        page.wait_for_selector(".ob-movefail:not([hidden])", timeout=4000)
        check("a move that fails says what to do instead, and can be tried again",
              page.inner_text(".ob-movefail").startswith("Couldn't move it")
              and not page.is_disabled(".ob-movego"))
        page.click(".ob-ghost")
        page.wait_for_timeout(500)
        check("Not now goes on to the language", page.evaluate(
            "document.querySelector('.ob-step.on').dataset.n") == "1" and page.is_visible(".ob-lopt"))
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()
    with Server(env={"MICMIC_BUNDLE_PATH": "/Applications/MicMic.app"}):
        ctx, page = new_page(browser)
        settle(page)
        check("from Applications: no move screen, the language first", page.evaluate(
            "document.querySelector('.ob-step.on').dataset.n") == "1")
        ctx.close()


def test_dictation_unit():
    """Fix 2, in-process: other apps' keys, read only."""
    sys.path.insert(0, str(ROOT))
    from savta import dictation_apps as da
    home = fake_home(handy="option_right", superwhisper={"carbonKeyCode": 54, "carbonModifiers": 256,
                                                         "mouseButtonNumbers": []})
    before = {f: (f.read_bytes(), f.stat().st_mtime) for f in home.rglob("*") if f.is_file()}
    os.environ["MICMIC_DICTATION_HOME"] = str(home)
    try:
        found = da.bindings()
        check("Handy's option_right is right Option", da.user_of("right-option", found) == "Handy", found)
        check("Superwhisper's Carbon key 54 is right Command",
              da.user_of("right-command", found) == "Superwhisper", found)
        check("the first free of right Option, Command, Control", da.first_free(found=found) == "right-control")
        check("a combination is compared whatever the order of its modifiers",
              da.norm("shift+option+m") == da.norm("alt+shift+m"))
        after = {f: (f.read_bytes(), f.stat().st_mtime) for f in home.rglob("*") if f.is_file()}
        check("the other apps' files are only read, never written", before == after)
    finally:
        os.environ.pop("MICMIC_DICTATION_HOME", None)
    da._cache["home"] = None
    os.environ.setdefault("MICMIC_STATE_DIR", tempfile.mkdtemp(prefix="ob-unit-"))
    check("a test process (MICMIC_STATE_DIR) reads nobody's real settings",
          os.environ.get("MICMIC_STATE_DIR") and da.bindings() == [])


def test_clash_default():
    """Fix 2: a first run starts on a key no dictation app of hers already uses."""
    with Server(env={"MICMIC_DICTATION_HOME": str(fake_home(handy="option_right"))}) as srv:
        hk = call("GET", "/api/config")[1].get("hotkey")
        check("Handy on right Option: a new MicMic starts on right Command",
              hk == "right-command" and srv.settings().get("hotkey") == "right-command", hk)
    home = fake_home(handy="option_right", superwhisper={"carbonKeyCode": 54, "carbonModifiers": 256})
    with Server(env={"MICMIC_DICTATION_HOME": str(home)}):
        hk = call("GET", "/api/config")[1].get("hotkey")
        check("right Option and right Command both taken: right Control", hk == "right-control", hk)
    with Server(env={"MICMIC_DICTATION_HOME": str(fake_home())}) as srv:
        hk = call("GET", "/api/config")[1].get("hotkey")
        check("nothing else installed: right Option, and nothing saved",
              hk == "right-option" and "hotkey" not in srv.settings(), hk)
    with Server(profile={"setup_complete": True, "step": "done"}, settings={"onboarded": True},
                env={"MICMIC_DICTATION_HOME": str(fake_home(handy="option_right"))}) as srv:
        check("a Mac already set up keeps its key", call("GET", "/api/config")[1].get("hotkey")
              == "right-option" and "hotkey" not in srv.settings())


def test_clash_line(browser):
    """Fix 2: her key is Handy's: Try it says so in one line, with a free key to use."""
    home = fake_home(handy="option_right")
    with Server(settings={"hotkey": "right-option"}, env={"MICMIC_DICTATION_HOME": str(home)}) as srv:
        ctx, page = new_page(browser)
        settle(page)
        to_try_it(page)
        page.wait_for_selector(".ob-clash:not([hidden])", timeout=4000)
        check("Try it says right Option is Handy's, and offers right Command",
              page.inner_text(".ob-clash-t") == "Right ⌥ is used by Handy. Use Right ⌘ for MicMic?"
              and page.inner_text(".ob-clash-use") == "Use Right ⌘", page.inner_text(".ob-clash"))
        check("while the key is Handy's, no hold or tap teaching for it", page.is_hidden(".ob-howto"))
        page.click(".ob-clash-other")
        keys = page.evaluate("[...document.querySelectorAll('.ob-clash-key')].map(b => b.textContent)")
        check("Another key shows the other free ones", keys == ["Right ⌃", "Right ⇧", "Fn twice"], keys)
        page.click(".ob-clash-use")
        hk = None
        for _ in range(30):
            hk = srv.settings().get("hotkey")
            if hk == "right-command":
                break
            page.wait_for_timeout(100)
        check("Use Right ⌘ saves the hotkey setting", hk == "right-command", hk)
        page.wait_for_timeout(1600)                  # two status polls: it stays answered
        check("the line goes, and Try it names the new key", page.is_hidden(".ob-clash")
              and page.inner_text('.ob-step[data-n="2"] .ob-sub') == "Hold ⌘ and say"
              and page.text_content(".ob-kcap") == "⌘",
              page.inner_text('.ob-step[data-n="2"] .ob-sub'))
        check("and teaches it: the ⌘ on the right, hold or tap",
              page.is_visible(".ob-howto") and page.inner_text(".ob-howto").startswith("The ⌘ on the right."),
              page.inner_text(".ob-howto"))
        check("Handy's settings were not touched", json.loads((home /
              "Library/Application Support/com.pais.handy/settings_store.json").read_text())
              ["settings"]["bindings"]["transcribe"]["current_binding"] == "option_right")
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()


def test_try_without_accessibility(browser):
    """Fix 3: with Accessibility off the key cannot work, so Try it leads with the circle."""
    perms = {"microphone": "granted", "speech": "granted", "accessibility": "denied",
             "answer": {"accessibility": "granted"}, "delay": 0.3}
    with Server(perms=perms):
        ctx, page = new_page(browser)
        settle(page)
        to_try_it(page)
        sub = page.inner_text('.ob-step[data-n="2"] .ob-sub')
        check("Accessibility off: Click the circle and say", sub == "Click the circle and say", sub)
        check("and no key is named or drawn on the screen", "⌥" not in page.inner_text(".ob-step.on")
              and not page.query_selector(".ob-step.on .ob-kcap") and page.is_hidden(".ob-howto"),
              page.inner_text(".ob-step.on"))
        check("one line says the shortcut needs Accessibility, with Turn on",
              page.is_visible(".ob-nokey") and page.inner_text(".ob-nokey").startswith(
                  "The keyboard shortcut needs Accessibility.") and page.is_visible(".ob-axon"))
        page.click(".ob-axon")
        page.wait_for_function("document.querySelector('.ob-step[data-n=\"2\"] .ob-sub').textContent"
                               " === 'Hold ⌥ and say'", timeout=6000)
        check("once it is on, the key and how to use it come back, and the line goes",
              page.is_hidden(".ob-nokey") and page.is_visible(".ob-howto")
              and page.text_content(".ob-kcap") == "⌥")
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()


def test_asr_line_follows(browser):
    """Fix 4: one status request that never answers must not freeze the line at 93%."""
    with Server():
        ctx, page = new_page(browser)
        st = {"hang": False, "hung": []}
        down = json.dumps({"lang": "en", "state": "downloading", "done": 93, "total": 100})
        ready = json.dumps({"lang": "en", "state": "ready", "done": 100, "total": 100})

        def asr(route):
            if route.request.method != "GET":
                return route.continue_()
            if not st["hang"]:
                return route.fulfill(status=200, content_type="application/json", body=down)
            if not st["hung"]:
                st["hung"].append(route)          # never answered, like the owner's
                return None
            return route.fulfill(status=200, content_type="application/json", body=ready)
        page.route("**/api/asr", asr)
        settle(page)
        to_try_it(page)
        line = lambda: page.inner_text('.ob-step[data-n="2"] .ob-asr') if page.is_visible(
            '.ob-step[data-n="2"] .ob-asr') else ""
        check("Try it shows the download while it runs", "93%" in line(), line())
        st["hang"] = True
        page.wait_for_timeout(1500)
        check("a status request is now hanging", bool(st["hung"]))
        got = line()
        for _ in range(40):
            got = line()
            if "93%" not in got:
                break
            page.wait_for_timeout(250)
        check("the line follows the model to ready: Better recognition is on",
              got == "Better recognition is on", got)
        check("no page errors", not page.__errors, page.__errors)
        for r in st["hung"]:
            try:
                r.fulfill(status=200, content_type="application/json", body=ready)
            except Exception:  # noqa: BLE001  (the page gave up on it already)
                pass
        ctx.close()


def test_circle_is_a_button(browser):
    """Fix 5: the circle looks like something to press, and pressing it is a real turn."""
    with Server():
        ctx, page = new_page(browser)
        settle(page)
        to_try_it(page)
        label = page.inner_text(".ob-orbt") if page.query_selector(".ob-orbt") else ""
        check("the circle carries its label: Click to talk", label == "Click to talk"
              and page.is_visible(".ob-orbt"), label)
        box = page.evaluate("(() => { const r = document.querySelector('.ob-orbt').getBoundingClientRect();"
                            " return [r.left, r.right, r.bottom]; })()")
        check("the label is inside the window", box[0] >= 0 and box[1] <= 380 and box[2] <= 560, box)
        check("and a ring goes out from it while it waits", page.evaluate(
            "getComputedStyle(document.querySelector('.ob-rip')).animationName") == "obCall")
        check("the pointer says it can be clicked", page.evaluate(
            "getComputedStyle(document.querySelector('.ob-orbt')).cursor") == "pointer")
        page.click(".ob-orbt")                   # the label is part of the button
        check("a click on it hands the press to the listener",
              page.evaluate("window.__msgs") == ["listen"], page.evaluate("window.__msgs"))
        check("and the step shows it is listening", page.inner_text(".ob-pill").lower() == "listening",
              page.inner_text(".ob-pill"))
        code, _ = call("POST", "/api/utterance", {"text": "What time is it?", "speak": False,
                                                  "client": "native"}, timeout=60)
        page.wait_for_selector(".ob-step.ok", timeout=5000)
        check("the turn that follows completes the step", code == 200
              and page.inner_text(".ob-step.ok .ob-title") == "You're set.")
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()




# ---------------------------------------------------------------- the backup talk key
# ⌃⌥ M, a Carbon hot key native/listener.py always registers (no permission). The
# listener reports it on POST /api/backup_hotkey_status; there is no listener here, so
# the tests post what it would.
ISO = str.maketrans("", "", "\u2066\u2069")
REGISTERED = {"combo": "ctrl+opt+m", "registered": True, "status": 0, "error": "", "taken_by": ""}


def test_backup_hotkey_endpoints():
    with Server():
        code, p = call("GET", "/api/permissions")
        b = p.get("backup_hotkey") or {}
        check("backup_hotkey: ⌃⌥ M by default, enabled, unknown until the listener reports",
              b.get("combo") == "ctrl+opt+m" and b.get("enabled") is True
              and b.get("registered") is None and b.get("usable") is False, b)
        check("ready still counts the three permissions only", p["ready"] is True, p)
        code, o = call("POST", "/api/backup_hotkey_status", REGISTERED)
        b = call("GET", "/api/permissions")[1]["backup_hotkey"]
        check("reported registered: usable", code == 200 and b["registered"] is True and b["usable"] is True, b)
        c = call("GET", "/api/config")[1].get("backup_hotkey") or {}
        check("/api/config carries the same state", c.get("combo") == "ctrl+opt+m"
              and c.get("registered") is True and c.get("usable") is True, c)
        call("POST", "/api/backup_hotkey_status", {**REGISTERED, "taken_by": "macos_shortcut_222"})
        b = call("GET", "/api/permissions")[1]["backup_hotkey"]
        check("a macOS shortcut on the same keys: registered but not usable",
              b["registered"] is True and b["usable"] is False and b["taken_by"] == "macos_shortcut_222", b)
        call("POST", "/api/backup_hotkey_status", {**REGISTERED, "registered": False, "status": -9878,
                                                   "error": "taken"})
        b = call("GET", "/api/permissions")[1]["backup_hotkey"]
        check("another app holds it: not registered, error taken, the OSStatus shown",
              b["registered"] is False and b["error"] == "taken" and b["status"] == -9878
              and b["usable"] is False, b)
        code, o = call("POST", "/api/settings", {"backup_hotkey": "ctrl+opt+k"})
        b = call("GET", "/api/permissions")[1]["backup_hotkey"]
        check("Settings changes it; the old report is not this combo's",
              "backup_hotkey" in o.get("saved", []) and b["combo"] == "ctrl+opt+k"
              and b["registered"] is None, (o, b))
        code, o = call("POST", "/api/settings", {"backup_hotkey": "off"})
        b = call("GET", "/api/permissions")[1]["backup_hotkey"]
        check("and turns it off", b["enabled"] is False and b["registered"] is False
              and b["usable"] is False and b["combo"] == "off", b)
        bad = [call("POST", "/api/settings", {"backup_hotkey": v})[1].get("rejected") for v in
               ("space", "m", "command+space", "ctrl+opt+nosuch")]
        check("a bare key, an unknown modifier or key is refused",
              bad == [["backup_hotkey"]] * 4, bad)


def test_try_with_the_backup_key(browser):
    """Accessibility off, ⌃⌥ M registered: Try it teaches that key, and the circle still works."""
    perms = {"microphone": "granted", "speech": "granted", "accessibility": "denied",
             "answer": {"accessibility": "granted"}, "delay": 0.3}
    with Server(perms=perms):
        call("POST", "/api/backup_hotkey_status", REGISTERED)
        ctx, page = new_page(browser)
        settle(page)
        page.click('.ob-next[data-go="next"]')                  # past the language
        page.wait_for_selector('.ob-pcard[data-k="accessibility"]', timeout=4000)
        why = page.inner_text('.ob-step[data-n="1"] .ob-sub').translate(ISO)
        check("the Accessibility step names her key",
              why == "So Right ⌥ works from any app, and so I can press buttons for you.", why)
        to_try_it(page)
        line = page.inner_text(".ob-nokey").translate(ISO)
        check("Try it teaches ⌃⌥ M, from any app",
              page.is_visible(".ob-nokey .ob-bkey") and
              page.inner_text(".ob-nokey .ob-bkey").translate(ISO) == "Press ⌃⌥ M to talk, from any app.",
              line)
        check("plus: Turn on Accessibility any time to use Right ⌥ alone, with Turn on",
              "Turn on Accessibility any time to use Right ⌥ alone." in line and page.is_visible(".ob-axon"),
              line)
        check("and the circle is still there to click", page.is_visible(".ob-orb") and page.is_enabled(".ob-orb"))
        page.click(".ob-orb")
        check("clicking it still opens a turn", "listen" in page.evaluate("window.__msgs"),
              page.evaluate("window.__msgs"))
        page.click(".ob-axon")
        page.wait_for_function("document.querySelector('.ob-step[data-n=\"2\"] .ob-sub').textContent"
                               " === 'Hold ⌥ and say'", timeout=6000)
        check("Accessibility on: the backup line goes, her own key leads", page.is_hidden(".ob-nokey"))
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()
    with Server(perms=perms):
        call("POST", "/api/backup_hotkey_status", {**REGISTERED, "taken_by": "macos_shortcut_222"})
        ctx, page = new_page(browser)
        settle(page)
        to_try_it(page)
        line = page.inner_text(".ob-nokey").translate(ISO)
        check("a macOS shortcut on ⌃⌥ M: it is never promised, the old line stands",
              line.startswith("The keyboard shortcut needs Accessibility.") and "⌃⌥" not in line
              and page.is_hidden(".ob-nokey .ob-bkey"), line)
        ctx.close()


def test_settings_backup_line(browser):
    """Settings > Permissions: the same line with a Turn on button, only while it is off."""
    perms = {"microphone": "granted", "speech": "granted", "accessibility": "denied",
             "answer": {"accessibility": "granted"}, "delay": 0.3}
    with Server(profile={"setup_complete": True, "step": "done", "language": "english"},
                settings={"onboarded": True, "hotkey": "right-command"}, perms=perms):
        call("POST", "/api/backup_hotkey_status", REGISTERED)
        ctx, page = new_page(browser)
        settle(page, "/?panel=1")
        page.evaluate("openSettings()")
        page.wait_for_selector("#hBackup:not([hidden])", timeout=4000)
        text = page.inner_text("#hBackupT").translate(ISO)
        check("Accessibility off: the backup key, and her own key once it is on",
              text == "Press ⌃⌥ M to talk, from any app. Turn on Accessibility any time to use Right ⌘ alone.",
              text)
        check("with one Turn on button", page.inner_text("#backupAx") == "Turn on")
        page.click("#backupAx")
        check("Turn on asks for Accessibility, the same one request", page.__asks == ["accessibility"], page.__asks)
        page.wait_for_selector("#hBackup[hidden]", state="attached", timeout=5000)
        check("Accessibility on: the line goes", page.is_hidden("#hBackup"))
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()


# ---------------------------------------------------------------- the Terms (click-wrap)
def _shots_dir():
    """Screenshots of the Terms line are opt-in: only when MICMIC_SHOTS_DIR names a
    directory. A normal run of this suite writes nothing outside a temp dir."""
    d = os.environ.get("MICMIC_SHOTS_DIR", "").strip()
    return Path(d).expanduser() if d else None


TERMS_LINES = {
    "en": ("By continuing, you agree to the Terms of Use and the Privacy Policy.",
           ["Terms of Use", "Privacy Policy"]),
    "he": ("כשממשיכים, מסכימים לתנאי השימוש ולמדיניות הפרטיות.",
           ["תנאי השימוש", "מדיניות הפרטיות"]),
    "ar": ("بالمتابعة، أنت توافق على شروط الاستخدام وسياسة الخصوصية.",
           ["شروط الاستخدام", "سياسة الخصوصية"]),
    "ru": ("Продолжая, вы соглашаетесь с Условиями использования и Политикой конфиденциальности.",
           ["Условиями использования", "Политикой конфиденциальности"]),
}


def test_legal_unit():
    """The version the Mac records is the one the pages show: one constant, mirrored
    from proxy/proxy/pages.py DOC_VERSIONS, held equal here."""
    saved = os.environ.pop("MICMIC_SHOTS_DIR", None)
    check("no MICMIC_SHOTS_DIR: the suite takes no screenshots", _shots_dir() is None)
    os.environ["MICMIC_SHOTS_DIR"] = "/tmp/x"
    check("MICMIC_SHOTS_DIR set: screenshots go only there", _shots_dir() == Path("/tmp/x"))
    os.environ.pop("MICMIC_SHOTS_DIR")
    if saved is not None:
        os.environ["MICMIC_SHOTS_DIR"] = saved
    # In-process: never the real state directory, even when run on its own.
    os.environ.setdefault("MICMIC_STATE_DIR", tempfile.mkdtemp(prefix="ob-legal-"))
    sys.path.insert(0, str(ROOT))
    from savta import onboarding_api as ob
    src = (ROOT / "proxy/proxy/pages.py").read_text()
    m = re.search(r"DOC_VERSIONS\s*=\s*\{(.*?)\}", src, re.S)
    docs = dict(re.findall(r'"(/\w+)"\s*:\s*"([^"]+)"', m.group(1))) if m else {}
    check("TERMS_VERSION equals the /terms page's version (proxy pages.py DOC_VERSIONS)",
          docs.get("/terms") == ob.TERMS_VERSION, (docs, ob.TERMS_VERSION))
    check("PRIVACY_VERSION equals the /privacy page's version",
          docs.get("/privacy") == ob.PRIVACY_VERSION, (docs, ob.PRIVACY_VERSION))
    check("the two links are the site's own pages, nothing else",
          ob.LEGAL_URLS == {"terms": "https://getmicmic.vercel.app/terms",
                            "privacy": "https://getmicmic.vercel.app/privacy"}, ob.LEGAL_URLS)
    check("a URL from the page is never opened", ob.open_legal("https://evil.example")[0] == 400)
    for k in ("terms_version", "privacy_version", "terms_accepted_at", "terms_accepted_via"):
        from savta import router as _r
        check(f"settings.json keeps {k}", k in _r.SETTABLE)
    html = (WEB / "index.html").read_text()
    for key in ("obTerms", "obTermsLink", "obPrivacyLink", "sTermsNew", "sTermsRead"):
        n = len(re.findall(r"\b" + key + r':"', html))
        check(f"{key} is in all four line tables (he, en, ar, ru)", n == 4, n)
    lines = re.findall(r'\b(?:obTerms|obTermsLink|obPrivacyLink|sTermsNew|sTermsRead):"([^"]*)"', html)
    check("no em dash in the Terms lines", lines and not any("\u2014" in l for l in lines), lines)


def _legal_line(page):
    return page.evaluate("""(() => { const el = document.querySelector('.ob-legal');
      return {text: el.textContent.replace(/\\s+/g, ' ').trim(),
              links: [...el.querySelectorAll('button')].map(b => [b.dataset.doc, b.textContent]),
              on: el.classList.contains('on') && getComputedStyle(el).visibility === 'visible'}; })()""")


def _legal_posts(page):
    """Every POST body the page sends to /api/onboarding/legal, in order. Read from the
    request, not awaited as a response: expect_response hung this suite under load."""
    got = []
    page.on("request", lambda r: got.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and "/api/onboarding/legal" in r.url else None)
    return got


def _wait_posts(page, got, n):
    for _ in range(50):
        if len(got) >= n:
            break
        page.wait_for_timeout(100)
    return got


def test_terms_line(browser):
    """The first setup screen carries the Terms line under Continue, in her language,
    with two links that open the pages in her browser; continuing records it."""
    from savta import onboarding_api as ob
    speech = {"he": "he-IL", "en": "en-US", "ar": "ar-SA", "ru": "ru-RU"}
    for lang in ("en", "he", "ar", "ru"):
        # The page's language is the server's speech language (test_languages_and_motion).
        with Server(settings={"language_hint": speech[lang]}, perms=NEW_MAC):
            ctx, page = new_page(browser, lang=lang)
            settle(page)
            want, names = TERMS_LINES[lang]
            d = _legal_line(page)
            check(f"{lang}: the language screen has the Terms line, visible", d["on"]
                  and d["text"] == want, d)
            check(f"{lang}: both names are links to the two pages",
                  d["links"] == [["terms", names[0]], ["privacy", names[1]]], d["links"])
            if lang in ("he", "ar"):
                check(f"{lang}: right to left", page.evaluate("document.documentElement.dir") == "rtl")
            box = page.evaluate("""(() => { const r = e => e.getBoundingClientRect();
              const l = r(document.querySelector('.ob-legal')),
                    c = r(document.querySelector('.ob-next[data-go="next"]')),
                    o = r(document.querySelector('.ob'));
              return {below: l.top >= c.bottom - 1, inside: l.bottom <= o.bottom + 0.5
                      && l.left >= o.left && l.right <= o.right,
                      scrollW: document.documentElement.scrollWidth, w: innerWidth}; })()""")
            check(f"{lang}: under the Continue button, inside the window, no sideways scroll",
                  box["below"] and box["inside"] and box["scrollW"] <= box["w"], box)
            shots = _shots_dir()
            if shots and lang in ("en", "he"):
                try:
                    shots.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shots / f"clickwrap-{lang}.png"))
                except Exception as e:  # noqa: BLE001
                    print(f"  (screenshot {lang} not saved: {type(e).__name__})")
            check(f"{lang}: no page errors", not page.__errors, page.__errors)
            ctx.close()

    with Server(perms={**NEW_MAC, "delay": 0.3}) as srv:
        ctx, page = new_page(browser)
        settle(page)
        url = page.url
        posts = _legal_posts(page)
        page.click('.ob-legal-link[data-doc="terms"]')
        check("Terms of Use asks the app to open the terms page",
              _wait_posts(page, posts, 1)[:1] == [{"open": "terms"}], posts)
        page.click('.ob-legal-link[data-doc="privacy"]')
        check("Privacy Policy asks for the privacy page",
              _wait_posts(page, posts, 2)[1:2] == [{"open": "privacy"}], posts)
        code, body = call("POST", "/api/onboarding/legal", {"open": "terms"})
        check("and the app opens it in the browser (dry here)",
              code == 200 and body == {"ok": True, "opened": "terms", "dry": True}, body)
        check("the window itself never navigates", page.url == url, page.url)
        check("still on the language screen, nothing recorded yet",
              page.get_attribute(".ob-pcard", "data-k") == "language"
              and "terms_version" not in srv.settings(), srv.settings())
        page.click('.ob-next[data-go="next"]')
        st = {}
        for _ in range(30):
            st = srv.settings()
            if st.get("terms_version"):
                break
            page.wait_for_timeout(100)
        check("Continue records the version she agreed to",
              st.get("terms_version") == ob.TERMS_VERSION
              and st.get("privacy_version") == ob.PRIVACY_VERSION
              and st.get("terms_accepted_via") == "onboarding", st)
        check("with when, as an ISO time",
              bool(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", str(st.get("terms_accepted_at")))), st)
        page.wait_for_timeout(300)
        check("the next screen (microphone) does not show the line",
              page.get_attribute(".ob-pcard", "data-k") == "microphone" and not _legal_line(page)["on"])
        first = st.get("terms_accepted_at")
        call("POST", "/api/onboarding", {"action": "skip"})
        check("finishing later keeps the first agreement's time",
              srv.settings().get("terms_accepted_at") == first, srv.settings())
        code, d = call("GET", "/api/onboarding/legal")
        check("a Mac set up through the onboarding gets no updated-Terms note", d["note"] is False, d)
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()


def test_skip_records_terms(browser):
    """Skip on the first screen is continuing past the line too."""
    from savta import onboarding_api as ob
    with Server() as srv:
        ctx, page = new_page(browser)
        settle(page)
        page.click(".ob-skip")
        st = {}
        for _ in range(30):
            st = srv.settings()
            if st.get("onboarded"):
                break
            page.wait_for_timeout(100)
        check("Skip records the agreement with onboarded",
              st.get("terms_version") == ob.TERMS_VERSION and st.get("onboarded") is True, st)
        ctx.close()


def test_updated_terms_note(browser):
    """A Mac set up before the Terms: no modal, nothing blocks; Settings shows one quiet
    line once, and showing it records the version (continued use counts)."""
    from savta import onboarding_api as ob
    with Server(profile={"setup_complete": True, "step": "done", "language": "english"},
                settings={"onboarded": True}) as srv:
        code, d = call("GET", "/api/onboarding/legal")
        check("an older setup: the note is due", code == 200 and d["note"] is True
              and d["accepted"] is None and d["version"] == ob.TERMS_VERSION, d)
        code, d = call("POST", "/api/onboarding/legal", {"open": "terms"},
                       headers={"Origin": "https://evil.example"})
        check("another site cannot open a tab or record anything", code == 403, (code, d))
        code, d = call("POST", "/api/onboarding/legal", {"open": "/etc/passwd"})
        check("only the two pages can be opened", code == 400, (code, d))
        ctx, page = new_page(browser)
        settle(page)
        check("no onboarding, no modal on launch", not shown(page)
              and not page.evaluate("!!document.querySelector('#settings.on')"))
        page.evaluate("openSettings()")
        page.wait_for_timeout(700)
        note = page.evaluate("""(() => { const n = document.getElementById('termsNote');
          return {hidden: n.hidden, text: n.textContent.replace(/\\s+/g, ' ').trim()}; })()""")
        check("Settings shows the quiet line with its link",
              not note["hidden"] and note["text"] == "We've updated our Terms. Read them", note)
        posts = _legal_posts(page)
        page.click("#termsNoteRead")
        check("its link opens the Terms page",
              {"open": "terms"} in _wait_posts(page, posts, 1), posts)
        st = srv.settings()
        check("showing it recorded the version, via the notice",
              st.get("terms_version") == ob.TERMS_VERSION and st.get("terms_accepted_via") == "notice", st)
        page.evaluate("closeSettings()")
        page.evaluate("openSettings()")
        page.wait_for_timeout(700)
        check("once: the next time Settings opens it is gone",
              page.evaluate("document.getElementById('termsNote').hidden"))
        check("no page errors", not page.__errors, page.__errors)
        ctx.close()
    with Server() as srv:
        code, d = call("GET", "/api/onboarding/legal")
        check("a first run never gets the note (its onboarding carries the line)",
              d["note"] is False, d)


def main():
    test_static()
    test_note_utterance_filters()
    test_request_unit()
    test_contacts_wait_for_a_read_of_her_own()
    test_contacts_upgrade_from_an_older_setup()
    for t in (test_move_unit, test_relocate_unit, test_dictation_unit, test_clash_default, test_backup_hotkey_endpoints,
              test_legal_unit):
        print(f"\n-- {t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(f"{t.__name__} ran to the end", False, repr(e)[:400])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)    # bundled Chromium, never channel="chrome"
        try:
            # The real status calls, read only: never a request on this one.
            with Server(perms="real") as srv:
                for t in (test_endpoints, test_other_modes):
                    print(f"\n-- {t.__name__}")
                    try:
                        t(srv) if t is test_endpoints else t(srv, browser)
                    except Exception as e:  # noqa: BLE001
                        check(f"{t.__name__} ran to the end", False, repr(e)[:400])
            with Server(perms={**NEW_MAC, "delay": 0.3}) as srv:
                print("\n-- test_flow")
                try:
                    test_flow(srv, browser)
                except Exception as e:  # noqa: BLE001
                    check("test_flow ran to the end", False, repr(e)[:400])
            with Server() as srv:
                print("\n-- test_skip_persists")
                try:
                    test_skip_persists(srv, browser)
                except Exception as e:  # noqa: BLE001
                    check("test_skip_persists ran to the end", False, repr(e)[:400])
            print("\n-- test_onboarded_ends_voice_setup")
            try:
                test_onboarded_ends_voice_setup()
            except Exception as e:  # noqa: BLE001
                check("test_onboarded_ends_voice_setup ran to the end", False, repr(e)[:400])
            for t in (test_only_first_run, test_onboarding_language_choice, test_no_key_before_permissions,
                      test_onboarding_downloads_the_english_model, test_settings_asr_row, test_speech_language_default,
                      test_permission_steps, test_settings_permissions, test_languages_and_motion,
                      test_move_screen, test_moved_itself_line, test_clash_line, test_try_without_accessibility,
                      test_asr_line_follows, test_circle_is_a_button,
                      test_try_with_the_backup_key, test_settings_backup_line,
                      test_terms_line, test_skip_records_terms, test_updated_terms_note):
                print(f"\n-- {t.__name__}")
                try:
                    t(browser)
                except Exception as e:  # noqa: BLE001
                    check(f"{t.__name__} ran to the end", False, repr(e)[:400])
        finally:
            browser.close()
    if not LIVE:
        print(f"\n{len(NOCOST)} servers, each ended with 0 Jev calls and $0")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped"
          + ("" if LIVE else " (no model calls; MICMIC_LIVE=1 runs the skipped ones for real)"))
    for f in FAILED:
        print("  FAILED:", f)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
