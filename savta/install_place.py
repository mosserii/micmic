"""Running from the disk image: offer to move MicMic to Applications first.

The owner's fresh install (1.2.1, 2026-09-30) ran MicMic straight from the DMG window:
/Volumes/MicMic/MicMic.app. The microphone and speech were granted, but Accessibility
never turned on (an app on a read-only disk image is a poor Accessibility entry), and
the whole app would vanish the moment the image was ejected. The same goes for an app
macOS has translocated (a quarantined app run from where it was downloaded, served
from a random read-only path under .../AppTranslocation/).

So before any other onboarding step the window shows MicMic's own "Move MicMic to
Applications" screen. "Move and reopen":

  1. copies the bundle into /Applications with `ditto --noqtn` (~/Applications when
     /Applications cannot be written), beside any MicMic.app already there and without
     the quarantine flag, so the copy is not translocated again (what LetsMove does);
  2. only then is a MicMic.app already there moved to the Trash (NSFileManager's
     trash, the Finder's Move to Trash), never removed, and the copy takes its name;
  3. a small detached shell waits for this process to exit, opens the new copy with
     `open`, and tries to eject the disk image;
  4. this instance quits (the listener registers how, set_quitter()).

The owner set that screen aside (MICMIC_MOVE_SCREEN, 2026-10-03), then opened MicMic from
the disk image window anyway, with a copy already dragged into /Applications. So a launch
from a disk image or a translocated path now fixes itself, silently, before anything else
(relocate_at_launch(), called by native/listener.py before the server starts and before
any permission is asked, so every macOS grant belongs to the copy in Applications):

  * /Applications/MicMic.app is there, the same version or newer: that copy is opened
    with `open`, this one quits, and the image is ejected. No window, no line.
  * none there, or an older one: the same copy as above (the older one to the Trash),
    then the copy is opened and this one quits. The copy's first screen says one small
    line, "MicMic moved itself to Applications." (moved_note()).
  * anything fails, or it already tried a moment ago (a relaunch that would come back
    to this same place): it runs where it is, as before.

Tests fake all of it: MICMIC_BUNDLE_PATH stands in for where the app runs from, and
MICMIC_FAKE_MOVE=1 records each step in the state dir instead of doing it. A faked
location always means a faked move, so only the release bundle ever moves for real.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import threading
import time
from pathlib import Path

from .paths import state_dir

DEST_DIRS = (Path("/Applications"), Path.home() / "Applications")
APP_NAME = "MicMic.app"

_quitter = None
_lock = threading.Lock()
_moving = False


def set_quitter(fn) -> None:
    """The listener's way to quit this app on its main thread."""
    global _quitter
    _quitter = fn


def bundle_path() -> str | None:
    """The .app this code runs from, or None (a source checkout)."""
    fake = os.environ.get("MICMIC_BUNDLE_PATH")
    if fake:
        return fake
    here = str(Path(__file__).resolve())
    if ".app/Contents/" not in here:
        return None
    return here.split("/Contents/", 1)[0]


def _read_only(path: str) -> bool:
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except (OSError, AttributeError):
        return False


def kind(path: str | None, read_only=_read_only) -> str | None:
    """Why this copy should move: "disk_image", "translocated", or None (it is fine)."""
    if not path:
        return None
    p = str(path)
    if "/AppTranslocation/" in p:
        return "translocated"
    if any(p.startswith(str(d) + "/") for d in DEST_DIRS):
        return None
    if p.startswith("/Volumes/"):
        return "disk_image"
    if read_only(p):
        return "disk_image"
    return None


def status() -> dict:
    """For GET /api/onboarding: {"needed": bool, "kind": ..., "from": path}."""
    p = bundle_path()
    k = kind(p)
    return {"needed": bool(k), "kind": k, "from": p if k else None}


def _volume(path: str) -> str | None:
    """/Volumes/MicMic/MicMic.app -> /Volumes/MicMic: the image to eject."""
    parts = Path(path).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return str(Path(*parts[:3]))
    return None


class RealOps:
    """What a move does on a real Mac. Every step is a copy, a move to the Trash, or
    launching a process: nothing here removes a file."""

    def writable(self, d: Path) -> bool:
        return d.is_dir() and os.access(d, os.W_OK)

    def exists(self, p: Path) -> bool:
        return p.exists()

    def rename(self, a: Path, b: Path) -> bool:
        os.rename(a, b)                  # same folder, and b is already in the Trash
        return b.exists()

    def trash(self, p: Path) -> bool:
        import Foundation
        url = Foundation.NSURL.fileURLWithPath_(str(p))
        ok, _res, err = Foundation.NSFileManager.defaultManager() \
            .trashItemAtURL_resultingItemURL_error_(url, None, None)
        return bool(ok) and err is None

    def version(self, app: Path) -> str | None:
        """CFBundleShortVersionString of an .app, or None when it cannot be read."""
        try:
            with open(Path(app) / "Contents/Info.plist", "rb") as f:
                v = plistlib.load(f).get("CFBundleShortVersionString")
            return str(v) if v else None
        except Exception:  # noqa: BLE001
            return None

    def copy(self, src: str, dest: Path) -> bool:
        r = subprocess.run(["/usr/bin/ditto", "--noqtn", src, str(dest)],
                           capture_output=True, timeout=180)
        return r.returncode == 0 and (dest / "Contents/Info.plist").exists()

    def relaunch(self, dest: Path, pid: int, volume: str | None) -> None:
        # Waits for this process to be gone (a new copy starting while this one still
        # holds the port would attach to a server that is about to quit), then opens
        # the new one, then tries to eject the image. Detached: it outlives us.
        script = ('while /bin/kill -0 "$1" 2>/dev/null; do /bin/sleep 0.2; done; '
                  '/usr/bin/open "$2"; '
                  'if [ -n "$3" ]; then /bin/sleep 1; /usr/bin/hdiutil detach -quiet "$3" '
                  '|| /usr/bin/hdiutil detach -quiet -force "$3"; fi; true')
        subprocess.Popen(["/bin/sh", "-c", script, "micmic-move", str(pid), str(dest),
                          volume or ""], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    def quit(self) -> None:
        if _quitter is not None:
            _quitter()


class FakeOps(RealOps):
    """MICMIC_FAKE_MOVE=1: every step is written to <state>/move_log.jsonl instead.
    MICMIC_FAKE_MOVE may also be JSON: {"existing": true} (a MicMic.app already in
    Applications), {"fail": "copy"} (that step fails)."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.log = state_dir() / "move_log.jsonl"

    def _note(self, step, **kw):
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"step": step, **kw}) + "\n")
        return self.cfg.get("fail") != step

    def writable(self, d: Path) -> bool:
        return str(d) == "/Applications"

    def exists(self, p: Path) -> bool:
        return (p.name == APP_NAME and str(p.parent) == "/Applications"
                and bool(self.cfg.get("existing")))

    def version(self, app: Path) -> str | None:
        """{"version": "1.2.4"} is this copy's, {"existing_version": ...} the one in
        Applications (null: unreadable)."""
        inside = any(str(app).startswith(str(d) + "/") for d in DEST_DIRS)
        return self.cfg.get("existing_version" if inside else "version", "1.2.4")

    def rename(self, a: Path, b: Path) -> bool:
        return self._note("rename", src=str(a), dest=str(b))

    def trash(self, p: Path) -> bool:
        return self._note("trash", path=str(p))

    def copy(self, src: str, dest: Path) -> bool:
        return self._note("copy", src=src, dest=str(dest))

    def relaunch(self, dest: Path, pid: int, volume: str | None) -> None:
        self._note("relaunch", dest=str(dest), volume=volume)

    def quit(self) -> None:
        self._note("quit")


def _ops():
    raw = os.environ.get("MICMIC_FAKE_MOVE")
    # A faked location is always a faked move: a test that set one and forgot the
    # other must never reach the real /Applications.
    if raw or os.environ.get("MICMIC_BUNDLE_PATH"):
        try:
            cfg = json.loads(raw or "{}")
        except ValueError:
            cfg = {}
        return FakeOps(cfg if isinstance(cfg, dict) else {})
    return RealOps()


def _copy_into_place(ops, src: str) -> tuple[Path | None, str]:
    """Copy `src` into the first writable Applications folder as MicMic.app: (its path,
    "") or (None, why not). Copied beside it first: if the copy fails, the MicMic already
    there is untouched. Only a good copy sends the old one to the Trash and takes its
    place. Shared by the move screen and the silent move at launch."""
    dest_dir = next((d for d in DEST_DIRS if ops.writable(d)), None)
    if dest_dir is None:
        return None, "no_writable_applications"
    dest = dest_dir / APP_NAME
    staging = dest_dir / ".MicMic-moving.app"
    if ops.exists(staging):
        ops.trash(staging)
    if not ops.copy(src, staging):
        if ops.exists(staging):
            ops.trash(staging)
        return None, "could_not_copy"
    if ops.exists(dest) and not ops.trash(dest):
        ops.trash(staging)
        return None, "could_not_trash_old"
    if not ops.rename(staging, dest):
        return None, "could_not_copy"
    return dest, ""


def move(ops=None, src: str | None = None) -> tuple[int, dict]:
    """POST /api/install/move. Copies, relaunches from the copy, quits this one."""
    global _moving
    ops = ops or _ops()
    src = src or bundle_path()
    why = kind(src)
    if not why:
        return 200, {"ok": False, "error": "not_needed"}
    with _lock:
        if _moving:
            return 200, {"ok": False, "error": "already_moving"}
        _moving = True
    ok = False
    try:
        dest, err = _copy_into_place(ops, src)
        if dest is None:
            return 200, {"ok": False, "error": err}
        ops.relaunch(dest, os.getpid(), _volume(src) if why == "disk_image" else None)
        # A moment for this reply to reach the page before the app goes.
        t = threading.Timer(0.6, ops.quit)
        t.daemon = True
        t.start()
        ok = True
        return 200, {"ok": True, "to": str(dest)}
    except Exception as e:  # noqa: BLE001
        return 200, {"ok": False, "error": "failed", "detail": type(e).__name__}
    finally:
        if not ok:                      # a failed move can be tried again; a done one not
            with _lock:
                _moving = False


# ---------------------------------------------------------------- the silent move at launch

MOVED_NOTE = "moved_itself.json"      # in the state folder, which both copies share
ATTEMPT = "relocate_attempt.json"
# A relaunch that lands back here (macOS translocating the copy in Applications too)
# must not bounce between the two forever: one try in this window, then run as is.
RETRY_AFTER_S = 120.0
NOTE_FOR_S = 90.0                     # the line stays on the first screen this long


def _version_tuple(v: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except (TypeError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def relocate_at_launch(ops=None, src: str | None = None, now: float | None = None) -> str:
    """At launch, before the server and before any permission: get MicMic running from
    Applications without a screen. What it did:

      "stay"      nothing to do (already in Applications, or a source checkout)
      "opened"    an Applications copy of the same version or newer was opened; quit now
      "moved"     this copy was put in Applications and opened from there; quit now
      "failed"    it could not; run from here as before
      "skipped"   it tried a moment ago and came back here; run from here

    The caller quits on "opened" and "moved" (the relaunch waits for this process to
    end, then opens the copy and ejects the image)."""
    ops = ops or _ops()
    src = src or bundle_path()
    why = kind(src)
    if not why:
        return "stay"
    now = time.time() if now is None else now
    try:
        st = state_dir()
        last = _read_json(st / ATTEMPT)
        if now - float(last.get("at", 0)) < RETRY_AFTER_S:
            return "skipped"
        _write_json(st / ATTEMPT, {"at": now, "from": src})
        volume = _volume(src) if why == "disk_image" else None
        mine = _version_tuple(ops.version(Path(src)))
        there = next((d / APP_NAME for d in DEST_DIRS if ops.exists(d / APP_NAME)), None)
        if there is not None:
            theirs = _version_tuple(ops.version(there))
            if theirs is None or mine is None:
                return "failed"           # cannot tell which is newer: touch nothing
            if theirs >= mine:
                ops.relaunch(there, os.getpid(), volume)
                return "opened"
        dest, _err = _copy_into_place(ops, src)
        if dest is None:
            return "failed"
        _write_json(st / MOVED_NOTE, {"at": now, "from": src, "to": str(dest)})
        ops.relaunch(dest, os.getpid(), volume)
        return "moved"
    except Exception:  # noqa: BLE001
        return "failed"


def moved_note(now: float | None = None) -> bool:
    """True on the first screens of the copy that MicMic just moved into Applications:
    "MicMic moved itself to Applications." From the first time it is asked, for
    NOTE_FOR_S (the page asks again as it goes), never after."""
    now = time.time() if now is None else now
    path = state_dir() / MOVED_NOTE
    d = _read_json(path)
    if not d or now - float(d.get("at", 0) or 0) > 24 * 3600:
        return False
    seen = d.get("seen_at")
    if seen is None:
        try:
            _write_json(path, {**d, "seen_at": now})
        except OSError:
            pass
        return True
    return now - float(seen) < NOTE_FOR_S
