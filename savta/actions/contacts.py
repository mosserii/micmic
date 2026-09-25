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


def _read_whatsapp() -> list[dict]:
    src = WA_DIR / "ContactsV2.sqlite"
    if not src.exists():
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
        if not _copy_with_timeout(src, tmp):
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
        return rows
    except Exception:  # noqa: BLE001
        return []


def _read_macos() -> list[dict]:
    try:
        p = subprocess.run(["osascript", "-e",
                            'tell application "Contacts" to get name of every person'],
                           capture_output=True, text=True, timeout=25)
        if p.returncode != 0 or not p.stdout.strip():
            return []
        return [{"name": n.strip(), "first": n.strip().split(" ")[0], "phone": "",
                 "waid": "", "source": "macos"}
                for n in p.stdout.split(",") if n.strip()]
    except Exception:  # noqa: BLE001
        return []


def _load_now() -> list[dict]:
    """The slow part, run on whatever thread calls it."""
    rows = _read_whatsapp() or _read_macos()
    seen, uniq = set(), []
    for r in rows:
        k = r["name"].lower()
        if k and k not in seen:
            seen.add(k)
            r["keys"] = keys_for(r["name"]) | keys_for(r["first"])
            uniq.append(r)
    _CACHE.update(at=time.time(), rows=uniq)
    return uniq


def _refresh_in_background() -> None:
    with _LOAD_LOCK:
        if _LOADING.is_set():
            return                      # one loader at a time
        _LOADING.set()

    def work():
        try:
            if not _load_now():
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


def _copy_db(name: str) -> Path | None:
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
    rows = _RECENT["rows"]
    if rows is None or limit > RECENT_ROWS:
        rows = _load_recent() if limit <= RECENT_ROWS else _read_recent(limit)
    elif time.time() - _RECENT["at"] > RECENT_SECONDS:
        _refresh_recent_in_background()
    return [dict(r) for r in rows[:limit]]


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
