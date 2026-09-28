"""macOS actions. Anything that leaves the machine is gated behind an explicit opt-in."""
from __future__ import annotations
import os, subprocess, threading, time
from pathlib import Path

# Sending a message is outward facing and cannot be undone. It stays off until the
# owner of the machine turns it on deliberately.
SEND_FOR_REAL = os.environ.get("MICMIC_ALLOW_SEND", "").lower() in ("1", "true", "yes")

# Placing a call is louder and less forgiving than a message: it rings a real phone,
# possibly at night. It is gated separately and stays off unless explicitly enabled,
# even when messages are live.
CALL_FOR_REAL = os.environ.get("MICMIC_ALLOW_CALL", "").lower() in ("1", "true", "yes")


# Short on purpose. If automation permission for an app has not been granted, the
# AppleScript hangs rather than erroring, and she would sit in silence waiting for a
# timeout. Better to fail in two seconds and say so.
def _osa(script: str, timeout: float = 4.0) -> tuple[bool, str]:
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:200]


def contacts(limit: int = 60) -> list[str]:
    """Names from the Contacts app. Falls back to an empty list if access is denied,
    which is normal until the user grants permission."""
    ok, out = _osa('tell application "Contacts" to get name of every person', timeout=8)
    if not ok or not out:
        return []
    names = [n.strip() for n in out.split(",") if n.strip()]
    seen, uniq = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower()); uniq.append(n)
    return uniq[:limit]


def send_message(name: str, text: str, attachment: str | None = None) -> tuple[bool, str]:
    """A text, a file, or both, to `name` over iMessage. `attachment` is a local path
    (a screenshot, so far); Messages accepts it directly as `POSIX file`, no separate
    upload step. When both are given the file goes first, exactly as it would if she
    had dragged it into the conversation and then typed a line under it."""
    if not SEND_FOR_REAL:
        what = f"{text!r}" if text else "a file"
        if attachment and text:
            what = f"{attachment} and {text!r}"
        elif attachment:
            what = attachment
        return True, f"[dry run] would send {what} to {name}"
    esc_n = name.replace("\\", "\\\\").replace('"', '\\"')
    lines = ['tell application "Messages"',
             '  set svc to 1st service whose service type = iMessage',
             f'  set bud to buddy "{esc_n}" of svc']
    if attachment:
        esc_p = attachment.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  send (POSIX file "{esc_p}") to bud')
    if text:
        esc_t = text.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  send "{esc_t}" to bud')
    lines.append('end tell')
    return _osa("\n".join(lines))


def copy_file_to_clipboard(path: str) -> bool:
    """Put a file's own bytes on the clipboard - not its path as text - so a paste
    into Messages, WhatsApp or Mail drops the picture itself. Never gated behind
    SEND_FOR_REAL: nothing leaves the machine, the clipboard is local."""
    esc = path.replace("\\", "\\\\").replace('"', '\\"')
    ok, _ = _osa(f'set the clipboard to (read (POSIX file "{esc}") as «class PNGf»)',
                 timeout=6.0)
    return ok


def facetime(name: str, number: str = "", video: bool = True) -> tuple[bool, str]:
    """Actually place the call. Opening FaceTime and leaving her to dial is no use to
    someone who cannot read a screen, so this dials the number directly."""
    target = (number or name or "").strip()
    digits = "".join(c for c in target if c.isdigit() or c == "+")
    if not digits:
        # No number to dial. Open FaceTime rather than pretending the call happened.
        ok, msg = _osa('tell application "FaceTime" to activate')
        return False, f"no number for {name!r}; opened FaceTime instead ({msg})"
    if not (SEND_FOR_REAL and CALL_FOR_REAL):
        return True, (f"[dry run] would {'video' if video else 'audio'} call {name} "
                      f"on {digits} (set MICMIC_ALLOW_CALL=1 to place real calls)")
    import urllib.parse
    scheme = "facetime" if video else "facetime-audio"
    subprocess.run(["open", f"{scheme}://{urllib.parse.quote(digits)}"], check=False)
    return True, f"dialled {digits}"


def screen_locked() -> bool:
    """Is the Mac locked right now?

    Worth knowing for two reasons. Nothing visual can be done while it is — no video
    plays, no window opens, and no other application will show its controls, which
    from the inside looks exactly like a missing permission. And she can still be
    heard while it is locked, so she has to be told why the thing she asked for did
    not appear rather than being left guessing.
    """
    try:
        import Quartz
        d = Quartz.CGSessionCopyCurrentDictionary()
        return bool(d and d.get("CGSSessionScreenIsLocked"))
    except Exception:  # noqa: BLE001
        return False        # cannot tell; assume not, and fail the normal way


def end_call() -> tuple[bool, str]:
    """Hang up. A false alarm has to be retractable, or she learns to stay quiet when
    something is actually wrong."""
    return _osa('tell application "FaceTime" to quit')


def volume(delta: int) -> tuple[bool, str]:
    """Nudge the output volume and report where it landed, so the screen can show
    the real level rather than a guess that drifts away from the hardware."""
    ok, cur = _osa("output volume of (get volume settings)")
    try:
        new = max(0, min(100, int(cur) + delta))
    except ValueError:
        new = 50
    done, _ = _osa(f"set volume output volume {new}")
    return done, str(new)


# ---------------------------------------------------------------- ducking
# A song MicMic started plays out of the speakers straight back into the microphone.
# Chrome's recogniser only finalises a sentence on silence, and a song is never
# silent, so it showed her words as they came in, never finished them, and the listen
# window expired with the request dropped. The embedded player used to pause itself
# when the mic opened; a video in a browser tab cannot be paused from here without a
# play/pause TOGGLE, which would start music if nothing was playing. Lowering the
# volume is unambiguous and always reversible.
_DUCK_LOCK = threading.Lock()
_ducked_from: int | None = None
_duck_timer: threading.Timer | None = None
DUCK_TO = 12
# If the page vanishes mid-listen it never says "unduck", and she is left with a Mac
# that has gone quiet for no reason. So the duck undoes itself regardless.
DUCK_MAX_S = 75.0             # longer than the longest listen window Settings allows (60s)


def duck() -> bool:
    """Lower the output while she is speaking, remembering where it was."""
    global _ducked_from, _duck_timer
    with _DUCK_LOCK:
        if _ducked_from is None:
            ok, cur = _osa("output volume of (get volume settings)")
            try:
                cur_i = int(cur)
            except ValueError:
                return False
            if not ok or cur_i <= DUCK_TO:
                return True              # already quiet: nothing to lower, nothing to restore
            _ducked_from = cur_i
            _osa(f"set volume output volume {DUCK_TO}")
        if _duck_timer is not None:
            _duck_timer.cancel()
        _duck_timer = threading.Timer(DUCK_MAX_S, unduck)
        _duck_timer.daemon = True
        _duck_timer.start()
        return True


def unduck() -> bool:
    """Put the volume back where it was before duck(). Safe to call any number of times."""
    global _ducked_from, _duck_timer
    with _DUCK_LOCK:
        if _duck_timer is not None:
            _duck_timer.cancel()
            _duck_timer = None
        if _ducked_from is None:
            return True
        level, _ducked_from = _ducked_from, None
    ok, _ = _osa(f"set volume output volume {level}")
    return ok


def get_volume() -> int | None:
    """The output level right now, 0-100, so an Undo can put back exactly this."""
    ok, cur = _osa("output volume of (get volume settings)")
    try:
        return int(cur) if ok else None
    except ValueError:
        return None


def set_volume(level: int) -> bool:
    ok, _ = _osa(f"set volume output volume {max(0, min(100, int(level)))}")
    return ok


def close_tab_with(fragment: str) -> bool:
    """Close only the Chrome tab whose address contains this fragment (a video id).

    Undo for "play this" must never be close_front_window(): that presses Cmd-W in
    whatever app is in front when she presses Undo, which by then may be a document
    in another app entirely. Finding the tab by its address can only close the thing
    MicMic opened; if it is already gone, nothing else is touched."""
    frag = fragment.replace('"', "")
    if not frag:
        return False
    ok, out = _osa(
        'tell application "Google Chrome"\n'
        '  set n to 0\n'
        '  repeat with w in windows\n'
        '    repeat with t in (tabs of w whose URL contains "' + frag + '")\n'
        '      close t\n'
        '      set n to n + 1\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return n\n'
        'end tell', timeout=6.0)
    return ok and out.strip().isdigit() and int(out.strip()) > 0


def brightness(delta: int) -> tuple[bool, str]:
    key = 144 if delta > 0 else 145  # F1/F2 brightness keys
    return _osa(f'tell application "System Events" to key code {key}')


_GENDER_MARK = __import__("re").compile(r"«([^«»|]*)\|([^«»|]*)»")


# The `say` process currently reading something out. Without holding on to it there is
# no way to stop a long answer, so "enough, stop" had nothing to act on: she had to sit
# through twenty seconds of speech before the machine would listen again.
_SPEAKING: dict = {}
_SPEECH_LOCK = threading.Lock()


def speaking() -> bool:
    """Is a sentence being read out right now?"""
    with _SPEECH_LOCK:
        proc = _SPEAKING.get("proc")
    return bool(proc and proc.poll() is None)


def stop_speaking() -> bool:
    """Cut the current sentence off mid-word. True if something was actually stopped."""
    with _SPEECH_LOCK:
        proc = _SPEAKING.get("proc")
        _SPEAKING["proc"] = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def say(text: str, language: str = "english") -> None:
    # Last line of defence. A gendered marker that reaches the speech synthesiser is
    # read out as punctuation, so nothing may leave here still carrying one.
    if "«" in text:
        from ..profile import load as _load
        keep = 2 if _load().get("gender") == "masculine" else 1
        text = _GENDER_MARK.sub(lambda m: m.group(keep), text)
    voice = {"hebrew": "Carmit", "arabic": "Majed", "russian": "Milena"}.get(language)
    args = ["say"]
    if voice:
        args += ["-v", voice]
    args += ["-r", "170", text]
    # Starting a new sentence always stops the old one. Two voices talking over each
    # other is worse than either of them, and it happens whenever an answer arrives
    # while a previous one is still being read out.
    stop_speaking()
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        with _SPEECH_LOCK:
            _SPEAKING["proc"] = proc
            _SPEAKING["at"] = time.time()
            _SPEAKING["text"] = text[:120]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- inventory
# Everything below enumerates real things on THIS machine so Jev can select one.
# Jev cannot invent an app name or a file path; it can only point at a row here.

def installed_apps(limit: int = 200) -> list[str]:
    import os
    out = []
    for d in ("/Applications", "/System/Applications",
              "/System/Applications/Utilities", os.path.expanduser("~/Applications")):
        try:
            for n in os.listdir(d):
                if n.endswith(".app"):
                    out.append(n[:-4])
        except OSError:
            pass
    return sorted(set(out))[:limit]


KIND_QUERY = {
    "photo":    'kMDItemContentTypeTree == "public.image"',
    "pdf":      'kMDItemContentTypeTree == "com.adobe.pdf"',
    "document": 'kMDItemContentTypeTree == "public.content"',
    "video":    'kMDItemContentTypeTree == "public.movie"',
    "any":      "",
}

# Spotlight's "public.content" includes source code, so a broad document search on a
# developer's machine returns .py files and never her actual letters. She will never
# want a source file, so filter to things a person opens and reads.
KIND_EXT = {
    "photo":    {"jpg", "jpeg", "png", "heic", "heif", "gif", "tiff", "tif", "webp", "bmp"},
    "pdf":      {"pdf"},
    "video":    {"mp4", "mov", "m4v", "avi", "mkv", "mpg", "mpeg", "webm"},
    "document": {"pdf", "doc", "docx", "pages", "rtf", "odt", "txt",
                 "key", "ppt", "pptx", "numbers", "xls", "xlsx", "csv", "epub"},
}
KIND_EXT["any"] = KIND_EXT["document"] | KIND_EXT["photo"] | KIND_EXT["video"]

_NOISE = ("/node_modules/", "/.git/", "/site-packages/", "/Library/", "/.Trash/",
          "/.venv/", "/dist/", "/build/", "/__pycache__/", "/.cache/")


def find_files(words: str = "", kind: str = "any", limit: int = 40,
               recent_days: int | None = None) -> list[dict]:
    """Spotlight search. Returns real paths only; Jev picks one of them."""
    import os, subprocess, threading, time, time
    # Ask Spotlight for the exact extensions we want. Its content-type tree is not
    # reliable here ("public.content" misses PDFs and sweeps in source code), and a
    # generic query buries real documents under cache files.
    exts = KIND_EXT.get(kind, KIND_EXT["any"])
    clauses = ["(" + " || ".join(f'kMDItemFSName == "*.{e}"c' for e in sorted(exts)) + ")"]
    if words.strip():
        # Match ANY of her words, not the exact phrase. "security guide" must find
        # "macOS_Security_Complete_Guide.pdf", which an exact-substring match never will.
        toks = [w for w in words.replace('"', " ").split() if len(w) > 2][:6]
        if toks:
            clauses.append("(" + " || ".join(
                f'kMDItemDisplayName == "*{w}*"cd' for w in toks) + ")")
    if recent_days:
        clauses.append(f"kMDItemContentModificationDate >= $time.today(-{recent_days})")
    q = " && ".join(clauses)
    home = os.path.expanduser("~")
    try:
        out = subprocess.run(["mdfind", "-onlyin", home, q],
                             capture_output=True, text=True, timeout=12).stdout
    except Exception:  # noqa: BLE001
        return []
    allowed = KIND_EXT.get(kind, KIND_EXT["any"])
    rows = []
    for path in out.splitlines()[:limit * 20]:
        if not path or any(n in path for n in _NOISE):
            continue
        ext = os.path.splitext(os.path.basename(path))[1].lstrip(".").lower()
        if ext not in allowed:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        rows.append({"path": path, "name": os.path.basename(path),
                     "folder": os.path.basename(os.path.dirname(path)),
                     "mtime": st.st_mtime,
                     "when": time.strftime("%d %b %Y", time.localtime(st.st_mtime)),
                     "mb": round(st.st_size / 1e6, 1)})
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: -r["mtime"])
    return rows


def open_path(path: str) -> tuple[bool, str]:
    import subprocess
    try:
        subprocess.run(["open", path], check=False, timeout=10)
        return True, path
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:150]


# ---------------------------------------------------------------- whatsapp
def whatsapp(name_or_number: str, text: str) -> tuple[bool, str]:
    """Open WhatsApp with the message composed. Sending is gated like Messages."""
    import urllib.parse
    if not SEND_FOR_REAL:
        return True, f"[dry run] would WhatsApp {name_or_number}: {text}"
    digits = "".join(c for c in name_or_number if c.isdigit() or c == "+")
    if digits.lstrip("+"):
        url = f"whatsapp://send?phone={urllib.parse.quote(digits)}&text={urllib.parse.quote(text)}"
    else:
        url = f"whatsapp://send?text={urllib.parse.quote(text)}"
    import subprocess
    subprocess.run(["open", url], check=False)
    # WhatsApp composes the message but does not send it; press return once it is frontmost.
    import time as _t
    _t.sleep(2.2)
    return _osa('tell application "System Events" to tell process "WhatsApp" to keystroke return')


def whatsapp_open_chat(name_or_number: str) -> tuple[bool, str]:
    """Open a chat and leave it there, with nothing typed and nothing sent.

    WhatsApp desktop has no attach-a-file hook this project can drive the way Messages'
    `send (POSIX file ...)` does, so a screenshot cannot actually go out on this channel
    from here. What can honestly be done is put the picture on the clipboard (already
    done by the caller before this runs) and get her to the right conversation, so a
    single paste and Enter is all that is left for her - never claimed as sent, because
    it was not."""
    if not SEND_FOR_REAL:
        return True, f"[dry run] would open WhatsApp chat with {name_or_number}"
    import urllib.parse
    digits = "".join(c for c in name_or_number if c.isdigit() or c == "+")
    url = (f"whatsapp://send?phone={urllib.parse.quote(digits)}" if digits.lstrip("+")
           else "whatsapp://send")
    subprocess.run(["open", url], check=False)
    return True, "opened chat"


# ---------------------------------------------------------------- notes etc
NOTES_FALLBACK = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "notes.txt")


def _keep_locally(text: str) -> tuple[bool, str]:
    """If Notes is not reachable, the words still must not be lost. She said something
    she wanted kept; dropping it is worse than any permission dialog."""
    try:
        with open(NOTES_FALLBACK, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M')}  {text}\n")
        return True, f"kept in {os.path.basename(NOTES_FALLBACK)}"
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:90]


def read_local_notes(limit: int = 5) -> list[str]:
    """Newest first, and never the same note twice: hearing "buy eggs, buy eggs" read
    back makes it sound broken."""
    try:
        lines = [l.strip() for l in open(NOTES_FALLBACK, encoding="utf-8") if l.strip()]
    except Exception:  # noqa: BLE001
        return []
    seen, out = set(), []
    for line in reversed(lines):
        body = line.split("  ", 1)[-1].strip().lower()
        if body and body not in seen:
            seen.add(body)
            out.append(line)
        if len(out) >= limit:
            break
    return out


def make_note(text: str, title: str = "") -> tuple[bool, str]:
    body = (title + "<br>" + text) if title else text
    esc = body.replace("\\", "\\\\").replace('"', '\\"')
    ok, msg = _osa(f'tell application "Notes" to make new note at folder "Notes" '
                   f'of account "iCloud" with properties {{body:"{esc}"}}')
    if ok:
        return True, "saved in Notes"
    ok2, msg2 = _keep_locally(text)
    return ok2, (f"Notes is not available ({msg[:40]}), {msg2}" if ok2 else msg)


def add_reminder(text: str) -> tuple[bool, str]:
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    ok, msg = _osa(f'tell application "Reminders" to make new reminder '
                   f'with properties {{name:"{esc}"}}')
    if ok:
        return True, "saved in Reminders"
    ok2, msg2 = _keep_locally(text)
    return ok2, (f"Reminders is not available, {msg2}" if ok2 else msg)


def music(action: str, query: str = "") -> tuple[bool, str]:
    table = {"play": "play", "pause": "pause", "next": "next track",
             "previous": "previous track", "stop": "stop"}
    cmd = table.get(action)
    if cmd:
        return _osa(f'tell application "Music" to {cmd}')
    return False, "unknown music action"


def battery() -> str:
    import subprocess
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=8).stdout
        import re as _re
        m = _re.search(r"(\d+)%", out)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def open_url(url: str) -> None:
    """Open a link where the user is actually looking.

    A bare `open` uses the DEFAULT browser, which on this machine is Safari while
    everything else about MicMic happens in Chrome: the page itself, and the browser
    agent, which is launched with channel="chrome". A song landing in a second browser
    the user was not using reads as nothing having happened at all. Chrome when it is
    installed, whatever they have set otherwise.
    """
    if Path("/Applications/Google Chrome.app").exists():
        # A cold `open -a "Google Chrome" <url>` launches Chrome and drops the link:
        # measured, it came up on chrome://newtab/ with the URL gone, which looks
        # exactly like nothing happened. Warm, the same command is reliable. So make
        # it warm first, then hand the link over.
        if not _running("Google Chrome"):
            subprocess.run(["open", "-a", "Google Chrome"], check=False)
            for _ in range(24):
                time.sleep(0.25)
                if _running("Google Chrome"):
                    break
            time.sleep(0.7)          # the first window still has to exist
        if subprocess.run(["open", "-a", "Google Chrome", url],
                          check=False).returncode == 0:
            return
    subprocess.run(["open", url], check=False)


def _running(app: str) -> bool:
    return subprocess.run(["pgrep", "-x", app], capture_output=True).returncode == 0


def open_app(app: str) -> tuple[bool, str]:
    """`open -a` needs no automation permission and returns in about 100ms, where the
    AppleScript equivalent needs a grant per app and can hang without one."""
    try:
        p = subprocess.run(["open", "-a", app], capture_output=True, text=True, timeout=8)
        if p.returncode == 0:
            return True, app
        return False, (p.stderr or "could not open").strip()[:120]
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:120]


# ---------------------------------------------------------------- quitting apps
def _clean(name: str) -> str:
    """Strip invisible format characters. WhatsApp reports its own name as
    '\u200eWhatsApp' — a left-to-right mark in front — so matching on the raw string
    would never find it, and a candidate list shown to Jev would carry the junk."""
    import unicodedata
    return "".join(ch for ch in (name or "") if unicodedata.category(ch) != "Cf").strip()


def running_apps() -> list[dict]:
    """The programs open right now that a person would call an app: the ones with a
    Dock icon. One row per name, with every process id behind it — Chrome alone runs
    as three. Finder is left out: it cannot really be quit, it only relaunches.

    Read from `lsappinfo`, NOT NSWorkspace. NSWorkspace keeps its list current only
    through notifications delivered on a main run loop, which the server's request
    thread does not have. Measured: Calculator's process was gone within 0.25s of
    quitting and NSWorkspace still listed it two seconds later — and an app launched
    after the server started would never have appeared at all.
    """
    try:
        out = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True,
                             timeout=4).stdout
    except Exception:  # noqa: BLE001
        return []
    import re as _re
    rows: dict[str, list[int]] = {}
    name = None
    for line in out.splitlines():
        m = _re.match(r'\s*\d+\) "(.*)" ASN:', line)
        if m:
            name = _clean(m.group(1))
            continue
        m = _re.search(r'\bpid = (\d+)\b.*\btype="Foreground"', line)
        if m and name and name != "Finder":
            rows.setdefault(name, []).append(int(m.group(1)))
        if "pid =" in line:
            name = None
    return [{"name": n, "pids": pids} for n, pids in rows.items()]


def is_running(pids: list[int]) -> bool:
    """Asked of the process itself, so it cannot be stale."""
    for pid in pids:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            continue
        except PermissionError:
            return True              # exists, just not ours to signal
    return False


def quit_app(pids: list[int]) -> tuple[bool, str]:
    """Quit it the way Cmd-Q does. The app is ASKED, not killed, so anything unsaved
    gets its own "save changes?" prompt, exactly as if she had pressed it herself. By
    process id rather than by name, and with no Automation permission needed — which
    `tell application "X" to quit` would require, once per app."""
    try:
        from AppKit import NSRunningApplication
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:120]
    asked = 0
    for pid in pids:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None and app.terminate():
            asked += 1
    return asked > 0, f"asked {asked} of {len(pids)} to quit"


def close_front_window() -> tuple[bool, str]:
    return _osa('tell application "System Events" to keystroke "w" using command down')


# ---------------------------------------------------------------- timers
_TIMERS: list[dict] = []


def set_timer(minutes: float, text: str, language: str = "english") -> dict:
    """A spoken reminder, in process. Needs no permission from anybody.

    Deliberately not a macOS reminder: those need an automation grant, and half the
    time what she wants is 'tell me in ten minutes', which is a sentence said out
    loud, not a row in a database."""
    when = time.time() + minutes * 60

    def fire():
        lead = {"hebrew": "תזכורת.", "arabic": "تذكير.", "russian": "Напоминание.",
                "english": "Reminder."}.get(language, "Reminder.")
        say(f"{lead} {text}", language)
        _TIMERS[:] = [t for t in _TIMERS if t["at"] != when]

    t = threading.Timer(minutes * 60, fire)
    t.daemon = True
    t.start()
    # "id": what a follow-up ("make it 10 minutes instead", "cancel it") points at,
    # so it moves or stops this reminder and never another one she has running.
    row = {"at": when, "text": text, "minutes": minutes, "timer": t, "id": f"{when:.6f}"}
    _TIMERS.append(row)
    return row


def timer_row(timer_id: str) -> dict | None:
    """The reminder with this id while it has not gone off yet, else None."""
    now = time.time()
    return next((t for t in _TIMERS if t.get("id") == timer_id and t["at"] > now), None)


def cancel_timer(timer_id: str) -> bool:
    """Stop this one pending reminder, and only it. Nothing is deleted anywhere: the
    reminder was never written outside this process."""
    row = timer_row(timer_id)
    if row is None:
        return False
    try:
        row["timer"].cancel()
    except Exception:  # noqa: BLE001
        return False
    _TIMERS[:] = [t for t in _TIMERS if t is not row]
    return True


def pending_timers() -> list[dict]:
    now = time.time()
    return [{"in_minutes": round((t["at"] - now) / 60, 1), "text": t["text"]}
            for t in _TIMERS if t["at"] > now]


def add_event(title: str, date: str, start: str, end: str = "",
              location: str = "", all_day: bool = False) -> tuple[bool, str]:
    """Add one event to her calendar. date is YYYY-MM-DD, start/end are HH:MM (24h).
    `all_day` ignores start/end and adds it as an all-day event instead - for a
    date on the screen with no time anywhere near it ("Pyramids of Giza (November
    28, 2026)"), rather than refusing the whole thing for want of a clock reading.

    The date is built field by field, never from a string: AppleScript parses date
    strings in the Mac's own locale, so "2026-10-01 10:00" means something different,
    or nothing, on a Hebrew or Russian system. Day is set to 1 first so that moving
    from the 31st into a shorter month cannot roll over into the month after.
    Written into the first calendar she can write to: the first one in the list is
    often a read-only subscription such as holidays, which silently refuses.

    On success the text is the calendar's name and the new event's uid, one per line:
    what a follow-up ("move it to Friday") needs to change this event and no other."""
    import re as _re
    m = _re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date or "")
    if not m:
        return False, "bad date"
    y, mo, d = (int(x) for x in m.groups())
    esc = _esc

    def when(var, h, mi):
        return _as_date(var, y, mo, d, h, mi)
    if all_day:
        script = (when("s", 0, 0) +
                  'tell application "Calendar"\n'
                  '  set cal to first calendar whose writable is true\n'
                  f'  set ev to make new event at end of events of cal with properties '
                  f'{{summary:"{esc(title)}", start date:s, allday event:true, '
                  f'location:"{esc(location)}"}}\n'
                  '  return (name of cal) & linefeed & (uid of ev)\n'
                  'end tell')
        return _osa(script, timeout=10.0)
    t = _re.fullmatch(r"(\d{1,2}):(\d{2})", start or "")
    if not t:
        return False, "bad time"
    sh, sm = (int(x) for x in t.groups())
    e = _re.fullmatch(r"(\d{1,2}):(\d{2})", end or "")
    eh, em = (int(x) for x in e.groups()) if e else (sh + 1, sm)
    script = (when("s", sh, sm) + when("e", eh, em) +
              'tell application "Calendar"\n'
              '  set cal to first calendar whose writable is true\n'
              f'  set ev to make new event at end of events of cal with properties '
              f'{{summary:"{esc(title)}", start date:s, end date:e, location:"{esc(location)}"}}\n'
              '  return (name of cal) & linefeed & (uid of ev)\n'
              'end tell')
    ok, out = _osa(script, timeout=10.0)
    return ok, out


def _esc(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace('"', '\\"')[:200]


def _as_date(var: str, y: int, mo: int, d: int, h: int, mi: int) -> str:
    """An AppleScript date built field by field (see add_event for why)."""
    return (f"set {var} to current date\n"
            f"set day of {var} to 1\n"
            f"set year of {var} to {y}\n"
            f"set month of {var} to {mo}\n"
            f"set day of {var} to {d}\n"
            f"set hours of {var} to {min(h, 23)}\n"
            f"set minutes of {var} to {mi}\n"
            f"set seconds of {var} to 0\n")


def change_event(uid: str, calendar: str, date: str, start: str = "", end: str = "",
                 all_day: bool = False) -> tuple[bool, str]:
    """Move or resize the one event MicMic created, found by its uid in the calendar it
    was written to. Returns (True, "ok"), (False, "missing") when it is not there any
    more (she deleted or moved it herself), or (False, why). Never looks at, and so can
    never change, any other event: an event without a uid on record is refused here."""
    import re as _re
    m = _re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date or "")
    if not (uid and calendar and m):
        return False, "missing" if not uid else "bad date"
    y, mo, d = (int(x) for x in m.groups())
    head = ('tell application "Calendar"\n'
            f'  set evs to (every event of (first calendar whose name is "{_esc(calendar)}") '
            f'whose uid is "{_esc(uid)}")\n'
            '  if (count of evs) is 0 then return "missing"\n'
            '  set ev to item 1 of evs\n')
    if all_day:
        script = (_as_date("s", y, mo, d, 0, 0) + head +
                  '  set allday event of ev to true\n'
                  '  set start date of ev to s\n'
                  '  return "ok"\nend tell')
    else:
        t = _re.fullmatch(r"(\d{1,2}):(\d{2})", start or "")
        e = _re.fullmatch(r"(\d{1,2}):(\d{2})", end or "")
        if not (t and e):
            return False, "bad time"
        (sh, sm), (eh, em) = ((int(x) for x in t.groups()), (int(x) for x in e.groups()))
        # Moved later, the end goes first, so the event never ends before it starts.
        script = (_as_date("s", y, mo, d, sh, sm) + _as_date("e", y, mo, d, eh, em) + head +
                  '  set allday event of ev to false\n'
                  '  if s > (end date of ev) then\n'
                  '    set end date of ev to e\n'
                  '    set start date of ev to s\n'
                  '  else\n'
                  '    set start date of ev to s\n'
                  '    set end date of ev to e\n'
                  '  end if\n'
                  '  return "ok"\nend tell')
    ok, out = _osa(script, timeout=10.0)
    if ok and out.strip() == "missing":
        return False, "missing"
    return ok and out.strip() == "ok", out


# ---------------------------------------------------------------- a video in a tab
# What MicMic put on in the browser (YouTube, Netflix and the rest) is driven in its own
# tab and nowhere else: the tab is found by the address MicMic opened (a video id, the
# service's host). If it is gone, nothing is pressed anywhere. Exact control through the
# page's own video element when Chrome allows JavaScript from Apple Events (off by
# default); otherwise the site's own keyboard shortcuts, pressed with that tab in front.
_VIDEO_JS = {
    "pause": "if(v.paused)return 'already';v.pause();return 'done';",
    "resume": "if(!v.paused)return 'already';v.play();return 'done';",
    "back_10": "v.currentTime=Math.max(0,v.currentTime-10);return 'done';",
    "forward_10": "v.currentTime=v.currentTime+10;return 'done';",
    "restart": "v.currentTime=0;v.play();return 'done';",
}
# System Events: a character, or (key code, modifiers).
_TAB_KEYS = {
    "youtube": {"pause": "k", "resume": "k", "back_10": "j", "forward_10": "l",
                "full_screen": "f", "exit_full_screen": (53, ""), "restart": "0",
                "next": ("n", "shift"), "next_episode": ("n", "shift"),
                "previous": ("p", "shift")},
    "other": {"pause": (49, ""), "resume": (49, ""), "back_10": (123, ""),
              "forward_10": (124, ""), "full_screen": "f", "exit_full_screen": (53, "")},
}


def _press(key) -> str:
    if isinstance(key, str):
        return f'tell application "System Events" to keystroke "{key}"'
    k, mods = key
    using = f" using {mods} down" if mods else ""
    if isinstance(k, str):
        return f'tell application "System Events" to keystroke "{k}"{using}'
    return f'tell application "System Events" to key code {k}{using}'


def tab_player(fragment: str, action: str, site: str = "other") -> str:
    """Do `action` to the video in the Chrome tab whose address holds `fragment`.
    Returns "done", "already" (it was paused already, and so on), "keys" (the site's
    shortcut was pressed in that tab), "missing" (no such tab: nothing was touched),
    "unsupported" (that site has no way to do it), or "failed"."""
    frag = (fragment or "").replace('"', "")
    keys = _TAB_KEYS.get(site, _TAB_KEYS["other"])
    js = _VIDEO_JS.get(action)
    key = keys.get(action)
    if not frag or (js is None and key is None):
        return "unsupported" if frag else "missing"
    run_js = ""
    if js:
        body = ("(function(){var v=document.querySelector('video');"
                "if(!v)return 'novideo';" + js + "})()")
        run_js = ('        try\n'
                  f'          set r to (execute t javascript "{body}")\n'
                  '        end try\n')
    press = ('        if r is "keys" or r is "novideo" then\n'
             + ('          set active tab index of w to i\n'
                '          set index of w to 1\n'
                '          activate\n'
                '          delay 0.3\n'
                f'          {_press(key)}\n'
                if key is not None else '          return "unsupported"\n')
             + '          return "keys"\n'
               '        end if\n')
    script = ('if application "Google Chrome" is not running then return "missing"\n'
              'tell application "Google Chrome"\n'
              '  repeat with w in windows\n'
              '    set i to 0\n'
              '    repeat with t in tabs of w\n'
              '      set i to i + 1\n'
              f'      if URL of t contains "{frag}" then\n'
              '        set r to "keys"\n'
              + run_js + press +
              '        return r\n'
              '      end if\n'
              '    end repeat\n'
              '  end repeat\n'
              '  return "missing"\n'
              'end tell')
    ok, out = _osa(script, timeout=6.0)
    out = (out or "").strip()
    if not ok:
        return "failed"
    return out if out in ("done", "already", "keys", "missing", "unsupported") else "failed"


def cancel_last_timer() -> bool:
    """Undo for "remind me in five minutes": only the reminder just set, never the
    others she already had running."""
    while _TIMERS:
        t = _TIMERS.pop()
        if t["at"] > time.time():
            try:
                t["timer"].cancel()
                return True
            except Exception:  # noqa: BLE001
                return False
    return False


def cancel_timers() -> int:
    n = 0
    for t in _TIMERS:
        try:
            t["timer"].cancel(); n += 1
        except Exception:  # noqa: BLE001
            pass
    _TIMERS.clear()
    return n
