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

Tests fake all of it: MICMIC_BUNDLE_PATH stands in for where the app runs from, and
MICMIC_FAKE_MOVE=1 records each step in the state dir instead of doing it. A faked
location always means a faked move, so only the release bundle ever moves for real.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
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
        return p.name == APP_NAME and bool(self.cfg.get("existing"))

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
        dest_dir = next((d for d in DEST_DIRS if ops.writable(d)), None)
        if dest_dir is None:
            return 200, {"ok": False, "error": "no_writable_applications"}
        dest = dest_dir / APP_NAME
        # Copied beside it first: if the copy fails, the MicMic already there is
        # untouched. Only a good copy sends the old one to the Trash and takes its place.
        staging = dest_dir / ".MicMic-moving.app"
        if ops.exists(staging):
            ops.trash(staging)
        if not ops.copy(src, staging):
            if ops.exists(staging):
                ops.trash(staging)
            return 200, {"ok": False, "error": "could_not_copy"}
        if ops.exists(dest) and not ops.trash(dest):
            ops.trash(staging)
            return 200, {"ok": False, "error": "could_not_trash_old"}
        if not ops.rename(staging, dest):
            return 200, {"ok": False, "error": "could_not_copy"}
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
