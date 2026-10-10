"""Her real address book, read from WhatsApp and the macOS Contacts app.

1,038 WhatsApp contacts is far more than Jev's 255-option limit, and sending a
thousand names to an API would be careless with her address book anyway. So code
does a cheap prefilter (transliteration-aware) down to a few dozen candidates and
Jev makes the final semantic pick from the shortlist.
"""
from __future__ import annotations
import os, re, shutil, sqlite3, subprocess, tempfile, threading, time
from pathlib import Path

WA_DIR = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
_CACHE: dict = {"at": 0.0, "rows": []}
CACHE_SECONDS = 600

# Reading WhatsApp's container can block forever: it sits behind a privacy permission
# that has no answer when nobody is at the screen, and its write-ahead log can be very
# large. That used to hang every single request — the server answered /api/health and
# nothing else, so asking the time got no reply at all. Nothing she says may ever wait
# on the address book: it loads in the background, and callers take what is ready.
LOAD_BUDGET = 2.5          # seconds a caller will ever wait for a first load
RETRY_AFTER = 120.0        # ...and how long a failed attempt is believed
_LOADING = threading.Event()
_LOAD_LOCK = threading.Lock()
_LAST_FAIL = [0.0]

# Enough Hebrew to Latin to make "זוהר" and "Zohar" collide. Not a transliteration
# system, just a bucket key so the prefilter can find candidates worth ranking.
HEB = {
    "א": "a", "ב": "b", "ג": "g", "ד": "d", "ה": "h", "ו": "w", "ז": "z", "ח": "h",
    "ט": "t", "י": "y", "כ": "k", "ך": "k", "ל": "l", "מ": "m", "ם": "m", "נ": "n",
    "ן": "n", "ס": "s", "ע": "a", "פ": "p", "ף": "f", "צ": "ts", "ץ": "ts", "ק": "k",
    "ר": "r", "ש": "sh", "ת": "t",
}
ARB = {"ا": "a", "ب": "b", "ت": "t", "ج": "j", "ح": "h", "د": "d", "ر": "r", "ز": "z",
       "س": "s", "ش": "sh", "ع": "a", "ف": "f", "ق": "k", "ك": "k", "ل": "l", "م": "m",
       "ن": "n", "ه": "h", "و": "w", "ي": "y"}


# In Hebrew and Arabic these letters usually carry a vowel rather than a consonant,
# so "זוהר" and "Zohar" only collide once they are gone from both sides.
_SOFT = set("aeiouhwyv")   # v too: Hebrew ו and ב both surface as v


# Digraphs that spell one Hebrew letter in Latin. Folding them first is what lets
# "Rachel" and "רחל" land on the same skeleton.
_DIGRAPH = (("tch", "ts"), ("tz", "ts"), ("ch", "h"), ("kh", "h"),
            ("ph", "f"), ("ck", "k"), ("qu", "k"))


def latinize(s: str) -> str:
    """Loose key: script folded to Latin, repeats collapsed."""
    s = (s or "").lower()
    for a, b in _DIGRAPH:
        s = s.replace(a, b)
    out = []
    for ch in s:
        if ch in HEB:
            out.append(HEB[ch])
        elif ch in ARB:
            out.append(ARB[ch])
        elif ch.isalnum():
            out.append(ch)
    return re.sub(r"(.)\1+", r"\1", "".join(out))


def skeleton(s: str) -> str:
    """Consonant skeleton: what survives in every spelling of the same name."""
    return re.sub(r"(.)\1+", r"\1",
                  "".join(c for c in latinize(s) if c not in _SOFT))


def keys_for(s: str) -> set[str]:
    """Short names legitimately reduce to one consonant, so keep those too."""
    return {k for k in (latinize(s), skeleton(s)) if len(k) >= 1}


COPY_TIMEOUT = 4.0
# A database that could not be copied is not going to become copyable a second later,
# and retrying costs the timeout every time.
_COPY_FAILED: dict = {}
COPY_RETRY_AFTER = 120.0


def _recent_copy_failure(name: str) -> bool:
    return time.time() - _COPY_FAILED.get(name, 0.0) < COPY_RETRY_AFTER


def _note_copy_failure(name: str) -> None:
    _COPY_FAILED[name] = time.time()


# A copy still running from an earlier try, per destination. A cp that is waiting on
# macOS's "access data from other apps" permission cannot be killed until that is
# answered, so a timeout left it behind; every later turn started another, and a
# fresh install piled up stuck copies. One at a time, then.
_INFLIGHT: dict[str, subprocess.Popen] = {}


def _copy_with_timeout(src: Path, dst: Path, timeout: float = COPY_TIMEOUT) -> bool:
    """Copy a file, giving up rather than hanging. True if the copy landed."""
    key = str(dst)
    prev = _INFLIGHT.get(key)
    if prev is not None:
        if prev.poll() is None:
            return False                       # the last one is still stuck: wait for it
        _INFLIGHT.pop(key, None)
    try:
        proc = subprocess.Popen(["/bin/cp", "-f", str(src), key],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return False
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            _INFLIGHT[key] = proc               # stuck in a permission wait: remember it
        return False
    return rc == 0 and dst.exists()


# Every copy lands on one fixed path per database, and `cp -f` rewrites it in place.
# Two readers at once meant one overwrote the -shm file SQLite had memory-mapped in
# the other, and the whole server died of SIGBUS (tests/test_account.py, when the day's
# briefing and a turn both read the message store). Copy and read, one at a time.
_STORE_LOCK = threading.RLock()


def _one_at_a_time(fn):
    def wrapped(*a, **k):
        with _STORE_LOCK:
            return fn(*a, **k)
    wrapped.__name__, wrapped.__doc__ = fn.__name__, fn.__doc__
    return wrapped


@_one_at_a_time
def _read_whatsapp() -> list[dict]:
    # Not even a stat inside WhatsApp's container before she has been asked: a look in
    # there is what raises macOS's "would like to access data from other apps".
    if not may_touch():
        return []
    src = WA_DIR / "ContactsV2.sqlite"
    if not src.exists():
        _STATUS["whatsapp"] = "absent"
        return []
    tmp = Path(tempfile.gettempdir()) / "micmic_wa_contacts.sqlite"
    try:
        # Copy rather than open in place: this database is live, and a reader must not
        # be able to affect WhatsApp. The side files carry any writes not yet folded in.
        #
        # The copy runs in a subprocess with a hard timeout, because reading inside
        # WhatsApp's container is gated by macOS privacy and BLOCKS rather than failing
        # when the permission has not been granted — stat() returns instantly, and then
        # the first read of the contents never comes back. In-process that hung the
        # whole server, so `_read_whatsapp() or _read_macos()` never reached its
        # fallback. A separate process can simply be abandoned.
        #
        # The first read just after she said Continue to MicMic's line waits for her
        # answer to macOS's own question (FIRST_COPY_WAIT), so an Allow lets that same
        # turn go on. Every other read keeps the short timeout.
        if not _copy_with_timeout(src, tmp, FIRST_COPY_WAIT if _PATIENT[0] else COPY_TIMEOUT):
            _STATUS["whatsapp"] = "blocked"
            return []
        for suffix in ("-wal", "-shm"):
            side = src.with_name(src.name + suffix)
            try:
                if side.exists():
                    _copy_with_timeout(side, tmp.with_name(tmp.name + suffix))
            except Exception:  # noqa: BLE001
                pass
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        cur = con.execute(
            "SELECT ZFULLNAME, ZGIVENNAME, ZPHONENUMBER, ZWHATSAPPID "
            "FROM ZWAADDRESSBOOKCONTACT "
            "WHERE ZFULLNAME IS NOT NULL AND ZFULLNAME != ''")
        rows = []
        for full, given, phone, waid in cur.fetchall():
            rows.append({"name": full.strip(), "first": (given or full).strip(),
                         "phone": (phone or "").strip(), "waid": (waid or "").strip(),
                         "source": "whatsapp"})
        con.close()
        _STATUS["whatsapp"] = "ok" if rows else "empty"
        return rows
    except Exception:  # noqa: BLE001
        _STATUS["whatsapp"] = "error"
        return []


def _read_macos() -> list[dict]:
    # The Contacts app through osascript is macOS's Automation question the first time.
    if not may_touch():
        return []
    try:
        p = subprocess.run(["osascript", "-e",
                            'tell application "Contacts" to get name of every person'],
                           capture_output=True, text=True, timeout=25)
        if p.returncode != 0:
            # -1743: Automation refused (or never answered); anything else, the app failed.
            _STATUS["macos"] = "blocked" if "1743" in (p.stderr or "") else "error"
            return []
        if not p.stdout.strip():
            _STATUS["macos"] = "empty"
            return []
        _STATUS["macos"] = "ok"
        return [{"name": n.strip(), "first": n.strip().split(" ")[0], "phone": "",
                 "waid": "", "source": "macos"}
                for n in p.stdout.split(",") if n.strip()]
    except subprocess.TimeoutExpired:
        _STATUS["macos"] = "timeout"
        return []
    except Exception:  # noqa: BLE001
        _STATUS["macos"] = "error"
        return []


# Reading the address book is itself a macOS prompt the first time: WhatsApp's data
# asks for "data from other apps", the Contacts app for Automation. So nothing reads it
# until one read has worked on this Mac (this marker); until then the first read is
# the one her own request about a person makes (router.get_contacts), just in time,
# after MicMic's own line (may_read_first). The listener's contextual names at launch
# and the server's warm-up of recent chats both wait for it. It was the fourth dialog
# of the first-run storm.
def _ok_marker() -> Path:
    from ..paths import state
    return state("contacts_ok")


# macOS asks "access data from other apps" again for each new build of the app (the
# launch-pack on the owner's Mac, macOS 15.6.1), so a read is remembered WITH the
# build that made it: a new build has read nothing yet. Until it has, nothing in the
# background looks inside WhatsApp's container (the launch warm-up, the briefing, the
# chat names all ask read_before), and her next turn about a person shows MicMic's
# line again first. A source checkout has no build identity: the old rule holds.
_IDENT: dict = {}


def code_identity() -> str:
    """This build: "<version>/<hash of the app's own executable>", or "" outside an
    installed app. MICMIC_CODE_IDENTITY stands in for it in tests."""
    fake = os.environ.get("MICMIC_CODE_IDENTITY")
    if fake is not None:
        return fake
    if "id" not in _IDENT:
        ident = ""
        try:
            from ..install_place import bundle_path
            from ..account import APP_VERSION
            b = bundle_path()
            exe = Path(b) / "Contents" / "MacOS" / "MicMic" if b else None
            if exe is not None and exe.is_file():
                import hashlib
                ident = f"{APP_VERSION}/{hashlib.sha256(exe.read_bytes()).hexdigest()[:16]}"
        except Exception:  # noqa: BLE001
            ident = ""
        _IDENT["id"] = ident
    return _IDENT["id"]


def _marker() -> tuple[str, str | None]:
    """(source, identity) of the remembered read; identity None for a marker written
    before builds were told apart (a bare source), ("", None) for none."""
    import json as _json
    try:
        raw = _ok_marker().read_text().strip()
    except OSError:
        return _legacy_ok(), None
    try:
        d = _json.loads(raw)
        if isinstance(d, dict):
            return str(d.get("source") or ""), d.get("identity")
    except ValueError:
        pass
    return raw, None


def read_before(source: str = "") -> bool:
    """Has a read of her contacts (from `source`, when given) worked on this Mac, by
    this very build?"""
    got, ident = _marker()
    if not got:
        return False
    cur = code_identity()
    if cur and ident != cur:
        return False
    return not source or got in (source, "legacy")


def _legacy_ok() -> str:
    """A Mac onboarded before the just-in-time read (no contacts_gate in its settings)
    has been reading her contacts at every launch: its prompts were answered long ago.
    Upgrading must not show MicMic's line and wait on her first request about a person."""
    try:
        from ..router import load_settings
        st = load_settings()
    except Exception:  # noqa: BLE001
        return ""
    if not st.get("onboarded") or st.get("contacts_gate"):
        return ""
    _note_read_ok("legacy", identity="")     # its answers were for another build
    return "legacy"


# MicMic's own line before that first prompt. The listener sets it when it shares
# this process (native/listener.py confirm_contacts): it shows the line and returns
# her answer. Unset (a test, a separate-process source build): no line, and the read
# goes ahead as her request asked.
_explain_first = None
_first_ok: list = [None]            # her answer this run, once she has given one
DECLINE_FOR = 300.0                 # a "Not now" holds for this long


def set_explainer(fn) -> None:
    global _explain_first
    _explain_first = fn


def may_read_first() -> bool:
    """May the first read of her contacts (a macOS prompt) happen now? Asked once:
    a yes stands for the run, a "Not now" for DECLINE_FOR seconds."""
    ans = _first_ok[0]
    if ans is True:
        return True
    if isinstance(ans, float) and time.time() - ans < DECLINE_FOR:
        return False
    if _explain_first is None:
        _first_ok[0] = True
        return True
    try:
        yes = bool(_explain_first())
    except Exception:  # noqa: BLE001
        yes = True
    _first_ok[0] = True if yes else time.time()
    return yes


# THE one gate in front of WhatsApp's container and the Contacts app (owner's fresh
# 1.2.4 install, 2026-10-03): the day's briefing, on a music turn, read the message
# store, and macOS asked "MicMic would like to access data from other apps" before
# MicMic had said a word about it. Every reader in this file asks this first, so no
# caller, present or future, raises that prompt: until a read has worked on this Mac,
# only the read her own request about a person makes after MicMic's line
# (may_read_first said yes) goes ahead, or an emergency (permit), which deliberately
# does not wait for a line.
_permit_until = [0.0]
PERMIT_FOR = 60.0


def may_touch() -> bool:
    """May anything look inside WhatsApp's container or ask the Contacts app now?"""
    return _first_ok[0] is True or _permit_until[0] > time.time() or read_before()


def permit(seconds: float = PERMIT_FOR) -> None:
    """An emergency: whoever she talks to is a better guess than nobody, prompt or not."""
    _permit_until[0] = time.time() + seconds


def _note_read_ok(source: str, identity: str | None = None) -> None:
    import json as _json
    try:
        _ok_marker().write_text(_json.dumps({
            "source": source, "identity": code_identity() if identity is None else identity}))
    except OSError:
        pass


# What the last read of each source found: "" (not tried), "ok", "empty", "absent" (no
# WhatsApp on this Mac), "blocked" (macOS said no, or has not been answered yet),
# "timeout", "error". unreadable() turns this into what she is told.
_STATUS: dict = {"whatsapp": "", "macos": ""}
_ATTEMPTED = [0.0]                  # when a load last started (0: never, in this run)
_PATIENT = [False]                  # the first read after her Continue: wait for macOS
FIRST_COPY_WAIT = 20.0              # ...for her answer to "data from other apps"
FIRST_READ_WAIT = 25.0              # what that turn waits in all, inside its 45 s
RECHECK_GAP = 3.0                   # a turn about a person re-reads at most this often
RECHECK_WAIT = 2.5                  # and waits this long for it


def _load_now(patient: bool = False) -> list[dict]:
    """The slow part, run on whatever thread calls it."""
    _ATTEMPTED[0] = time.time()
    _PATIENT[0] = patient
    try:
        rows = _read_whatsapp() or _read_macos()
    finally:
        _PATIENT[0] = False
    if rows:
        _note_read_ok(rows[0].get("source") or "macos")
    seen, uniq = set(), []
    for r in rows:
        k = r["name"].lower()
        if k and k not in seen:
            seen.add(k)
            r["keys"] = keys_for(r["name"]) | keys_for(r["first"])
            uniq.append(r)
    _CACHE.update(at=time.time(), rows=uniq)
    return uniq


def _refresh_in_background(patient: bool = False) -> None:
    with _LOAD_LOCK:
        if _LOADING.is_set():
            return                      # one loader at a time
        _LOADING.set()

    def work():
        try:
            if not (_load_now(True) if patient else _load_now()):
                # Permission refused, WhatsApp not installed, nothing there. Remember
                # that, so every later request does not pay the waiting budget again
                # for an answer that is not coming.
                _LAST_FAIL[0] = time.time()
        except Exception:  # noqa: BLE001
            _LAST_FAIL[0] = time.time()
        finally:
            _LOADING.clear()

    t = threading.Thread(target=work, daemon=True, name="micmic-contacts")
    t.start()


def all_contacts(force: bool = False, wait: float | None = None) -> list[dict]:
    """Her address book, or as much of it as is ready.

    Never blocks longer than LOAD_BUDGET. If WhatsApp's container is unreadable or
    slow, callers get whatever was cached — possibly nothing — and the request carries
    on. Asking the time must not depend on the address book being available.
    """
    now = time.time()
    fresh = _CACHE["rows"] and now - _CACHE["at"] < CACHE_SECONDS
    if fresh and not force:
        return _CACHE["rows"]
    if not may_touch():
        return _CACHE["rows"]           # not asked yet: nothing is read, nothing failed

    recently_failed = time.time() - _LAST_FAIL[0] < RETRY_AFTER
    if not (recently_failed and not force):
        _refresh_in_background()
    if _CACHE["rows"]:
        return _CACHE["rows"]           # stale is fine; silence is not
    if recently_failed:
        return []                       # known unavailable; do not make her wait again

    # Nothing cached at all: this is the first call since start-up, so give the loader
    # a short moment before giving up on it.
    budget = LOAD_BUDGET if wait is None else wait
    deadline = time.time() + budget
    while time.time() < deadline and not _CACHE["rows"]:
        time.sleep(0.05)
    return _CACHE["rows"]


def _wait_for_load(wait: float) -> list[dict]:
    """Until the book is in, the loader gives up, or `wait` runs out."""
    deadline = time.time() + max(0.0, wait)
    while time.time() < deadline and not _CACHE["rows"] and _LOADING.is_set():
        time.sleep(0.05)
    return _CACHE["rows"]


def never_tried() -> bool:
    """No read of her book has started in this run."""
    return not _ATTEMPTED[0] and not _CACHE["rows"]


def first_read(wait: float = FIRST_READ_WAIT) -> list[dict]:
    """The just-in-time read, right after she said Continue to MicMic's line.

    macOS asks its own question now ("data from other apps" for WhatsApp, Automation
    for the Contacts app), and the read blocks until she answers it. Owner's fresh
    1.2.4 install: the turn waited LOAD_BUDGET (2.5 s), she clicked the Automation
    prompt a second later, and she heard "I don't have Tamar in your contacts" from a
    book nobody had read. This waits for her answer, up to `wait`, so an Allow lets
    this very turn go on and send."""
    if not may_touch():
        return []
    _LAST_FAIL[0] = 0.0
    _refresh_in_background(patient=True)
    return _wait_for_load(wait)


def recheck(wait: float = RECHECK_WAIT) -> list[dict]:
    """A turn about a person after a read that found nothing: allowed since?

    A failed load used to be believed for RETRY_AFTER (120 s), so her answer to "who
    should I send it to?" a moment after switching it on still found nothing. macOS
    remembers her answer to both questions, so a refused read is refused at once and
    never asks again: re-reading is cheap, and done at most every RECHECK_GAP."""
    if _CACHE["rows"] or not may_touch():
        return _CACHE["rows"]
    if not _LOADING.is_set() and time.time() - _ATTEMPTED[0] >= RECHECK_GAP:
        _LAST_FAIL[0] = 0.0
        _refresh_in_background()
    return _wait_for_load(wait)


def unreadable() -> str:
    """Why her book has nobody in it, or "" when it has someone.

    "not_asked": MicMic may not look yet (she has not said Continue, or said Not now);
    "waiting": macOS's own question is still on the screen; "whatsapp": WhatsApp's
    data was refused; "automation": the Contacts app was; "none": read, and empty."""
    if all_contacts(wait=0.0):
        return ""
    if not may_touch():
        return "not_asked"
    if _LOADING.is_set():
        return "waiting"
    wa, mc = _STATUS["whatsapp"], _STATUS["macos"]
    if wa in ("blocked", "error"):
        return "whatsapp"
    if mc in ("blocked", "timeout", "error"):
        return "automation"
    return "none" if (wa or mc) else "not_asked"


def ask_again_next_time() -> None:
    """Her next request about a person shows MicMic's line again, even after a Not now."""
    _first_ok[0] = None


def said_not_now() -> bool:
    """Did she answer MicMic's line with Not now (still holding)? Only then may a reply
    say "since you said not now"."""
    ans = _first_ok[0]
    return isinstance(ans, float) and time.time() - ans < DECLINE_FOR


def _copy_db(name: str) -> Path | None:
    if not may_touch():
        return None                    # the gate above: not even a look inside
    src = WA_DIR / name
    if not src.exists():
        return None
    tmp = Path(tempfile.gettempdir()) / f"micmic_{name}"
    # Same trap as the contacts database: reading inside WhatsApp's container is gated
    # by macOS privacy and BLOCKS rather than failing when the permission is not there.
    # The message store is much larger than the address book, so this one could hang
    # for minutes. Nothing she asks for may ever wait on it.
    if _recent_copy_failure(name):
        return None
    try:
        if not _copy_with_timeout(src, tmp):
            _note_copy_failure(name)
            return None
        # WAL mode: without the siblings, today's messages are invisible.
        for suffix in ("-wal", "-shm"):
            side = src.with_name(src.name + suffix)
            try:
                if side.exists():
                    _copy_with_timeout(side, tmp.with_name(tmp.name + suffix))
            except Exception:  # noqa: BLE001
                pass
        return tmp
    except Exception:  # noqa: BLE001
        _note_copy_failure(name)
        return None


# Who she talks to changes over hours, and learning it means copying WhatsApp's whole
# message store (620 MB on this Mac, 0.7-1.2 s per copy, measured 2026-09-25).
# shortlist() asked on every turn, often twice, before Jev was even called. Now one
# copy serves RECENT_SECONDS; after that the old list is used while a new one loads
# in the background, so no turn waits on the copy except the very first.
RECENT_SECONDS = 300.0
RECENT_ROWS = 40               # read once at this size; smaller asks are a slice of it
_RECENT: dict = {"at": 0.0, "rows": None}
_RECENT_LOADING = threading.Event()
_RECENT_LOCK = threading.Lock()


def _load_recent() -> list[dict]:
    rows = _read_recent(RECENT_ROWS)
    _RECENT.update(at=time.time(), rows=rows)
    return rows


def _refresh_recent_in_background() -> None:
    with _RECENT_LOCK:
        if _RECENT_LOADING.is_set():
            return
        _RECENT_LOADING.set()

    def work():
        try:
            _load_recent()
        except Exception:  # noqa: BLE001
            pass
        finally:
            _RECENT_LOADING.clear()

    threading.Thread(target=work, daemon=True, name="micmic-recent-chats").start()


def recent_chats(limit: int = 30) -> list[dict]:
    """Who she actually talks to, newest first. A thousand contacts, but she has
    perhaps twenty real correspondents, and those are the ones she means."""
    if not may_touch():
        return []                      # and not cached: the first real read fills it
    rows = _RECENT["rows"]
    if rows is None or limit > RECENT_ROWS:
        rows = _load_recent() if limit <= RECENT_ROWS else _read_recent(limit)
    elif time.time() - _RECENT["at"] > RECENT_SECONDS:
        _refresh_recent_in_background()
    return [dict(r) for r in rows[:limit]]


@_one_at_a_time
def _read_recent(limit: int) -> list[dict]:
    chat = _copy_db("ChatStorage.sqlite")
    cont = _copy_db("ContactsV2.sqlite")
    if not chat:
        return []
    try:
        con = sqlite3.connect(f"file:{chat}?mode=ro", uri=True)
        if cont:
            con.execute("ATTACH DATABASE ? AS contacts", (str(cont),))
        rows = con.execute("""
            SELECT cs.ZCONTACTJID,
                   COALESCE(c.ZFULLNAME, cs.ZPARTNERNAME) AS display_name,
                   c.ZPHONENUMBER,
                   cs.ZLASTMESSAGEDATE
            FROM ZWACHATSESSION cs
            LEFT JOIN contacts.ZWAADDRESSBOOKCONTACT c
              ON c.ZWHATSAPPID = cs.ZCONTACTJID
            WHERE cs.ZSESSIONTYPE = 0 AND cs.ZREMOVED = 0
            ORDER BY cs.ZLASTMESSAGEDATE DESC LIMIT ?""", (limit,)).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for jid, name, phone, when in rows:
        if not name:
            continue
        out.append({"name": name.strip(), "first": name.strip().split(" ")[0],
                    "phone": (phone or "").strip() or "+" + str(jid or "").split("@")[0],
                    "waid": jid or "", "source": "whatsapp-recent",
                    "last": (when or 0) + 978307200})
    return out


@_one_at_a_time
def unread_summary(limit: int = 8) -> list[dict]:
    """Her most recent incoming messages, for reading aloud."""
    chat = _copy_db("ChatStorage.sqlite")
    if not chat:
        return []
    try:
        con = sqlite3.connect(f"file:{chat}?mode=ro", uri=True)
        rows = con.execute("""
            SELECT COALESCE(cs.ZPARTNERNAME, m.ZPUSHNAME, m.ZFROMJID),
                   m.ZTEXT, m.ZMESSAGEDATE
            FROM ZWAMESSAGE m
            LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
            WHERE m.ZISFROMME = 0 AND m.ZTEXT IS NOT NULL AND m.ZTEXT != ''
              AND cs.ZSESSIONTYPE = 0
            ORDER BY m.ZMESSAGEDATE DESC LIMIT ?""", (limit,)).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return []
    clean = lambda t: re.sub(r"\s+", " ", (t or "")).strip()[:280]
    return [{"who": clean(w) or "?", "text": clean(t), "at": (d or 0) + 978307200}
            for w, t, d in rows]


WA_APPS = (Path("/Applications/WhatsApp.app"), Path.home() / "Applications/WhatsApp.app")


def messages_access() -> str:
    """Why unread_summary() may have come back empty for a reason other than "no new
    messages", read without touching anything new (PERMISSION_FALLBACKS.md P0-7):
    "not_asked" (MicMic may not look yet: may_touch), "absent" (no WhatsApp app on
    this Mac), "denied" (the message store could not be copied: macOS refused or has
    not been answered, or it is not where WhatsApp keeps it), else "ok". Only a stat
    of the store, and only when may_touch() already allows looking inside."""
    if not may_touch():
        return "not_asked"
    if not any(p.exists() for p in WA_APPS):
        return "absent"
    if _recent_copy_failure("ChatStorage.sqlite"):
        return "denied"
    try:
        if not (WA_DIR / "ChatStorage.sqlite").exists():
            return "denied"
    except OSError:
        return "denied"
    return "ok"


def messages_from(name_fragment: str, limit: int = 5) -> list[dict]:
    """Recent messages from one person."""
    rows = unread_summary(limit=400)
    key = latinize(name_fragment)
    hits = [r for r in rows if key and key in latinize(r["who"])]
    return hits[:limit]


def shortlist(utterance: str, limit: int = 40) -> list[dict]:
    """Cheap prefilter. Anything plausibly named in the sentence, plus nothing else."""
    rows = all_contacts()
    if not rows:
        return []
    words = [w for w in re.split(r"[\s,.:;!?\"'()]+", utterance or "") if len(w) > 1]
    # key -> weight. A word that followed the Hebrew dative "ל" is far more likely to be
    # a name than a bare word, which stops the verb "תשלחי" from matching "Tesla".
    wkeys: dict[str, float] = {}

    def add(keys, weight):
        for k in keys:
            if len(k) >= 1:
                wkeys[k] = max(wkeys.get(k, 0.0), weight)

    for i, w in enumerate(words):
        after_to = i > 0 and words[i - 1].lower() in ("to", "for", "el")
        add(keys_for(w), 1.0 if after_to else 0.6)
        for pref in ("ול", "של", "לה", "ל", "ב", "ה", "ו", "ש", "כ", "מ"):
            if w.startswith(pref) and len(w) > len(pref) + 1:
                # the dative and possessive prefixes are the strong signal
                add(keys_for(w[len(pref):]), 1.0 if pref in ("ל", "ול", "לה", "של") else 0.7)
    if not wkeys:
        return []
    scored = []
    for r in rows:
        best = 0.0
        for k, w in wkeys.items():
            for field in r["keys"]:
                if field == k:
                    best = max(best, w * (1.0 if len(k) >= 2 else 0.5))
                elif field.startswith(k) or k.startswith(field):
                    best = max(best, w * 0.75)
                elif len(k) >= 3 and (k in field or field in k):
                    best = max(best, w * 0.55)
        if best > 0:
            scored.append((best, r))
    recent = {r["name"] for r in recent_chats(40)}
    scored = [(sc + (0.25 if r["name"] in recent else 0.0), r) for sc, r in scored]
    scored.sort(key=lambda t: (-t[0], len(t[1]["name"])))
    picked = [r for _, r in scored[:limit]]
    # always offer her actual correspondents, even if the name did not match well
    if len(picked) < limit:
        have = {r["name"] for r in picked}
        for r in recent_chats(12):
            if r["name"] not in have:
                picked.append(r)
                if len(picked) >= limit:
                    break
    return picked
