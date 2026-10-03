#!/usr/bin/env python3
"""Screenshots without the happy path (lane L6, PERMISSION_FALLBACKS.md P1-11, P1-12).

    uv run python tests/test_screenshot_fallbacks.py

Only savta/actions/screen.py is exercised, never the router: where screenshots are
saved (~/Pictures/MicMic, never a folder macOS guards), the clipboard copy, the
Screen Recording state the router reads ("granted" | "not_asked" | "denied"), the
Screenshot app fallback and the Control-Shift-Command-4 clipboard path.

Zero live calls of any kind: no model, no osascript, no `open`, no screencapture, no
real clipboard. subprocess, the clipboard and the permission check are all stubbed,
and every folder lives under a scratch directory.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="micmic-test-l6-")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
os.environ["MICMIC_STATE_DIR"] = os.path.join(_TMP, "state")
os.environ.pop("MICMIC_SHOTS_DIR", None)
os.environ.pop("MICMIC_FAKE_SCREEN_RECORDING", None)
os.environ.pop("MICMIC_FAKE_PERMISSIONS", None)
os.environ.pop("MICMIC_FAKE_PASTE_ACCESS", None)

from savta.actions import mac      # noqa: E402
from savta.actions import screen   # noqa: E402

# ---------------------------------------------------------------- walls
LAUNCHED: list[list] = []


class _R:
    def __init__(self, rc=0):
        self.returncode, self.stdout, self.stderr = rc, "", ""


def _blocked_run(cmd, *a, **kw):
    LAUNCHED.append(list(cmd) if isinstance(cmd, (list, tuple)) else [cmd])
    return _R(0)


def _blocked_popen(cmd, *a, **kw):
    raise AssertionError(f"unexpected Popen {cmd!r}")


screen.subprocess.run = _blocked_run
screen.subprocess.Popen = _blocked_popen
mac.subprocess.run = _blocked_run
OSA: list[str] = []
mac._osa = lambda script, timeout=15.0: (OSA.append(script), (False, "[test] blocked"))[1]

CLIP: list[str] = []
CLIP_OK = {"v": True}
mac.copy_file_to_clipboard = lambda p: (CLIP.append(p), CLIP_OK["v"])[1]


class FakePasteboard:
    """Stands in for NSPasteboard.generalPasteboard(): the real one is hers."""

    def __init__(self):
        self.count = 10
        self.items: dict[str, bytes] = {}
        self.cleared = 0

    def changeCount(self):
        return self.count

    def types(self):
        return list(self.items)

    def dataForType_(self, t):
        return self.items.get(t)

    def clearContents(self):
        self.cleared += 1
        self.items = {}
        self.count += 1
        return self.count

    def setData_forType_(self, data, t):
        self.items[t] = bytes(data)
        return True


PB = FakePasteboard()
screen._pasteboard = lambda: PB

GRANTED = {"v": False}
PREFLIGHT_CALLS = {"n": 0}


def _fake_preflight():
    """The REAL reading (CGPreflightScreenCaptureAccess) is replaced, not the
    fake-aware _screen_recording_granted above it, so every test here also runs the
    seam the router's tests rely on."""
    PREFLIGHT_CALLS["n"] += 1
    return GRANTED["v"]


screen._preflight_granted = _fake_preflight

REAL_DEFAULT = screen.SHOTS_DIR
# The scratch "home" folders this suite points screen.py at.
SHOTS = os.path.join(_TMP, "Pictures", "MicMic")
screen.SHOTS_DIR = SHOTS

# ---------------------------------------------------------------- harness
PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail="") -> bool:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, str(detail)[:300]))
        print(f"  FAIL  {name}\n          {str(detail)[:300]}")
    return bool(ok)


def fake_capture(cmd, *a, **kw):
    """screencapture writes its file; this does the same so the size check is real."""
    LAUNCHED.append(list(cmd))
    if cmd and cmd[0] == "screencapture":
        with open(cmd[-1], "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
    return _R(0)


# ---------------------------------------------------------------- 1. where it is saved
def t_folder():
    print("1. screenshots go to ~/Pictures/MicMic, never a folder macOS guards")
    check("the default folder is ~/Pictures/MicMic, said as 'Pictures/MicMic'",
          REAL_DEFAULT == os.path.expanduser("~/Pictures/MicMic")
          and screen.SHOTS_LABEL == "Pictures/MicMic", REAL_DEFAULT)
    check("the real default (~/Pictures/MicMic) is not a guarded folder",
          not screen.in_protected_folder(REAL_DEFAULT), REAL_DEFAULT)
    for p in ("~/Desktop", "~/Desktop/MicMic screenshot.png", "~/Documents/x.png",
              "~/Downloads/x.png", "~/Library/Mobile Documents/com~apple~CloudDocs/x.png"):
        check(f"{p} counts as guarded", screen.in_protected_folder(p), p)
    check("~/Pictures itself is not guarded", not screen.in_protected_folder("~/Pictures/x.png"))
    check("a folder merely named like one is not guarded",
          not screen.in_protected_folder("~/Pictures/Desktop stuff/x.png"))

    check("the folder does not exist before the first screenshot", not os.path.exists(SHOTS))
    d = screen.shots_dir()
    check("shots_dir() creates it on first use", d == SHOTS and os.path.isdir(SHOTS), d)
    check("calling it again is harmless", screen.shots_dir() == SHOTS)

    desk = os.path.expanduser("~/Desktop/should-not-be-used")
    os.environ["MICMIC_SHOTS_DIR"] = desk
    try:
        d = screen.shots_dir()
        check("an override pointing into the Desktop is ignored, not obeyed",
              d == SHOTS and not os.path.exists(desk), d)
    finally:
        os.environ.pop("MICMIC_SHOTS_DIR", None)

    other = os.path.join(_TMP, "override")
    os.environ["MICMIC_SHOTS_DIR"] = other
    try:
        check("MICMIC_SHOTS_DIR (a test seam) is used when it is safe",
              screen.shots_dir() == other and os.path.isdir(other))
    finally:
        os.environ.pop("MICMIC_SHOTS_DIR", None)

    blocker = os.path.join(_TMP, "a-file")
    open(blocker, "w").close()
    screen.SHOTS_DIR = os.path.join(blocker, "MicMic")     # cannot be made: a file is in the way
    try:
        d = screen.shots_dir()
        check("when ~/Pictures/MicMic cannot be made, MicMic's own Application Support "
              "folder is used (never the Desktop)",
              d == os.path.join(os.environ["MICMIC_STATE_DIR"], "Screenshots")
              and os.path.isdir(d) and not screen.in_protected_folder(d), d)
    finally:
        screen.SHOTS_DIR = SHOTS


# ---------------------------------------------------------------- 2. take_screenshot
def t_take():
    print("2. take_screenshot(): saved in Pictures/MicMic, clipboard copy kept, honest")
    GRANTED["v"] = False
    LAUNCHED.clear()
    res = screen.take_screenshot()
    check("without the grant: the same honest shape as before",
          res == {"ok": False, "why": "screen_recording"}, res)
    check("without the grant nothing is launched (no screencapture of the wallpaper)",
          not LAUNCHED, LAUNCHED)

    GRANTED["v"] = True
    screen.subprocess.run = fake_capture
    CLIP.clear()
    LAUNCHED.clear()
    CLIP_OK["v"] = True
    try:
        res = screen.take_screenshot()
        path = res.get("path", "")
        check("a granted capture is ok", res.get("ok") is True, res)
        check("it is saved in Pictures/MicMic as 'MicMic screenshot <time>.png'",
              os.path.dirname(path) == SHOTS
              and os.path.basename(path).startswith("MicMic screenshot ")
              and path.endswith(".png"), path)
        check("never under the Desktop or another guarded folder",
              "Desktop" not in path and not screen.in_protected_folder(path), path)
        check("screencapture was pointed at that same file",
              LAUNCHED and LAUNCHED[-1][0] == "screencapture" and LAUNCHED[-1][-1] == path,
              LAUNCHED)
        check("the result names the folder to say out loud",
              res.get("folder") == "Pictures/MicMic", res)
        check("the picture went on the clipboard, read back from the same safe file",
              CLIP == [path] and res.get("clipboard") is True, (CLIP, res))
        check("the file read back for the clipboard is not in a guarded folder",
              all(not screen.in_protected_folder(p) for p in CLIP), CLIP)

        # The clipboard copy fails both ways: the screenshot still exists, so ok stays
        # True, but the result must not claim the clipboard holds it.
        CLIP_OK["v"] = False
        screen._pasteboard = lambda: None
        res = screen.take_screenshot()
        check("a failed clipboard copy is reported, not claimed",
              res.get("ok") is True and res.get("clipboard") is False, res)

        # mac's osascript copy fails, the in-process clipboard works.
        screen._pasteboard = lambda: PB
        PB.items = {}
        res = screen.take_screenshot()
        check("when the osascript copy fails, the clipboard is filled directly (no osascript)",
              res.get("clipboard") is True and PB.items.get("public.png", b"").startswith(b"\x89PNG"),
              (res, list(PB.items)))
        CLIP_OK["v"] = True

        screen._front_window_id = lambda pid: None
        res = screen.take_screenshot(pid=42)
        check("a window that cannot be found is still 'no_window'",
              res == {"ok": False, "why": "no_window"}, res)
        screen._front_window_id = lambda pid: 7
        res = screen.take_screenshot(pid=42)
        check("a window capture saves to Pictures/MicMic too",
              res.get("ok") and os.path.dirname(res["path"]) == SHOTS
              and LAUNCHED[-1][:5] == ["screencapture", "-x", "-o", "-l", "7"], (res, LAUNCHED[-1]))

        def empty(cmd, *a, **kw):
            open(cmd[-1], "wb").close()
            return _R(0)
        screen.subprocess.run = empty
        res = screen.take_screenshot()
        check("an empty file is still a failed capture",
              res == {"ok": False, "why": "capture_failed"}, res)

        def boom(cmd, *a, **kw):
            raise OSError("no screencapture")
        screen.subprocess.run = boom
        res = screen.take_screenshot()
        check("a screencapture that cannot run is a failed capture, never a raise",
              res == {"ok": False, "why": "capture_failed"}, res)
    finally:
        screen.subprocess.run = _blocked_run
        screen._pasteboard = lambda: PB
        GRANTED["v"] = False


# ---------------------------------------------------------------- 3. capture_region
def t_region():
    print("3. capture_region(): the dragged part goes to Pictures/MicMic as well")

    class _P:
        def __init__(self, cmd, write=True):
            self.cmd = cmd
            if write:
                with open(cmd[-1], "wb") as fh:
                    fh.write(b"\x89PNG\r\n\x1a\n")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    GRANTED["v"] = True
    seen: list = []
    screen.subprocess.Popen = lambda cmd, *a, **kw: (seen.append(cmd), _P(cmd))[1]
    CLIP.clear()
    try:
        res = screen.capture_region()
        path = res.get("path", "")
        check("a finished drag is ok, saved as 'MicMic selection <time>.png' in Pictures/MicMic",
              res.get("ok") and os.path.dirname(path) == SHOTS
              and os.path.basename(path).startswith("MicMic selection "), res)
        check("screencapture -i was pointed at that file",
              seen and seen[-1][:3] == ["screencapture", "-i", "-x"] and seen[-1][-1] == path, seen)
        check("it is on the clipboard and says so", CLIP == [path] and res.get("clipboard") is True,
              (CLIP, res))
        first = path
        screen.subprocess.Popen = lambda cmd, *a, **kw: (seen.append(cmd), _P(cmd))[1]
        res = screen.capture_region()
        check("two shots in the same second get two files, never one overwritten",
              res.get("ok") and res["path"] != first and os.path.exists(first), (first, res))
        screen.subprocess.Popen = lambda cmd, *a, **kw: _P(cmd, write=False)
        res = screen.capture_region()
        check("Esc (no file) is still 'cancelled'", res == {"ok": False, "why": "cancelled"}, res)
        GRANTED["v"] = False
        res = screen.capture_region()
        check("without the grant: 'screen_recording', nothing started",
              res == {"ok": False, "why": "screen_recording"}, res)
    finally:
        screen.subprocess.Popen = _blocked_popen
        GRANTED["v"] = False


# ---------------------------------------------------------------- 4. Screen Recording state
def t_state():
    print("4. screen_recording_state(): granted | not_asked | denied, never a prompt")
    marker = Path(os.environ["MICMIC_STATE_DIR"]) / screen.SCREEN_ASKED_MARKER
    check("no marker yet", not marker.exists())
    GRANTED["v"] = False
    check("never asked and not granted -> 'not_asked'",
          screen.screen_recording_state() == "not_asked", screen.screen_recording_state())
    check("screen_recording_asked() is False before any ask", screen.screen_recording_asked() is False)
    check("note_screen_recording_asked() keeps the note", screen.note_screen_recording_asked() is True
          and marker.exists())
    check("asked and still not granted -> 'denied'",
          screen.screen_recording_state() == "denied", screen.screen_recording_state())
    GRANTED["v"] = True
    check("granted wins over the note -> 'granted'",
          screen.screen_recording_state() == "granted", screen.screen_recording_state())
    GRANTED["v"] = False

    for v in screen.SCREEN_STATES:
        os.environ["MICMIC_FAKE_SCREEN_RECORDING"] = v
        check(f"MICMIC_FAKE_SCREEN_RECORDING={v} is reported as is",
              screen.screen_recording_state() == v)
    # The real reading would say "denied" here (the note exists, the grant does not),
    # so "not_asked" below proves the fake never fell through to it.
    GRANTED["v"] = True
    PREFLIGHT_CALLS["n"] = 0
    os.environ["MICMIC_FAKE_SCREEN_RECORDING"] = "maybe"
    check("a junk fake value reads as 'not_asked', never the real reading",
          screen.screen_recording_state() == "not_asked" and PREFLIGHT_CALLS["n"] == 0,
          (screen.screen_recording_state(), PREFLIGHT_CALLS))
    os.environ.pop("MICMIC_FAKE_SCREEN_RECORDING", None)
    os.environ["MICMIC_FAKE_PERMISSIONS"] = '{"microphone": "granted", "screen_recording": "denied"}'
    check("the shared MICMIC_FAKE_PERMISSIONS table can fake it too",
          screen.screen_recording_state() == "denied")
    os.environ["MICMIC_FAKE_PERMISSIONS"] = '{"microphone": "granted"}'
    check("a fake table without a screen_recording key defaults to 'not_asked' "
          "(as onboarding_api does), never the real preflight or the real note",
          screen.screen_recording_state() == "not_asked" and PREFLIGHT_CALLS["n"] == 0,
          (screen.screen_recording_state(), PREFLIGHT_CALLS))
    os.environ["MICMIC_FAKE_PERMISSIONS"] = "{not json"
    check("a broken fake table never raises and reads as 'not_asked'",
          screen.screen_recording_state() == "not_asked" and PREFLIGHT_CALLS["n"] == 0)
    os.environ["MICMIC_FAKE_PERMISSIONS"] = '["screen_recording"]'
    check("a fake table that is not an object reads as 'not_asked'",
          screen.screen_recording_state() == "not_asked")
    os.environ.pop("MICMIC_FAKE_PERMISSIONS", None)
    GRANTED["v"] = False

    src = Path(screen.__file__).read_text()
    check("screen.py never calls CGRequestScreenCaptureAccess (L4 owns the ask)",
          not re.search(r"Quartz\.CGRequestScreenCaptureAccess|=\s*CGRequestScreenCaptureAccess",
                        src))
    p = screen.permissions()
    check("permissions() keeps exactly its two documented keys",
          set(p) == {"accessibility", "screen_recording"}, p)


# ---------------------------------------------------------------- 4b. the fake steers capture
def t_fake_steers_capture():
    print("4b. the fake steers take_screenshot, capture_region and permissions() too")
    screen.subprocess.run = fake_capture

    class _P:
        def __init__(self, cmd):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass
    started: list = []
    screen.subprocess.Popen = lambda cmd, *a, **kw: (started.append(cmd), _P(cmd))[1]
    try:
        # A dev Mac whose terminal holds Screen Recording: the real reading says yes.
        GRANTED["v"] = True
        for env, val in (("MICMIC_FAKE_SCREEN_RECORDING", "not_asked"),
                         ("MICMIC_FAKE_SCREEN_RECORDING", "denied"),
                         ("MICMIC_FAKE_PERMISSIONS", '{"screen_recording": "not_asked"}'),
                         ("MICMIC_FAKE_PERMISSIONS", '{"microphone": "granted"}')):
            os.environ[env] = val
            LAUNCHED.clear()
            started.clear()
            PREFLIGHT_CALLS["n"] = 0
            try:
                state = screen.screen_recording_state()
                shot = screen.take_screenshot()
                region = screen.capture_region()
                perms = screen.permissions()
                check(f"{env}={val} on a Mac that really is granted: state {state!r}, "
                      "take_screenshot and capture_region both refuse, nothing launched",
                      state in ("not_asked", "denied")
                      and shot == {"ok": False, "why": "screen_recording"}
                      and region == {"ok": False, "why": "screen_recording"}
                      and not LAUNCHED and not started, (state, shot, region, LAUNCHED, started))
                check(f"{env}={val}: permissions() says screen_recording False, "
                      "and the real preflight was never read",
                      perms.get("screen_recording") is False and PREFLIGHT_CALLS["n"] == 0,
                      (perms, PREFLIGHT_CALLS))
            finally:
                os.environ.pop(env, None)

        # CI: the real reading says no. The fake says granted: capture goes ahead, so
        # "granted" never comes paired with why=screen_recording.
        GRANTED["v"] = False
        for env, val in (("MICMIC_FAKE_SCREEN_RECORDING", "granted"),
                         ("MICMIC_FAKE_PERMISSIONS", '{"screen_recording": "granted"}')):
            os.environ[env] = val
            PREFLIGHT_CALLS["n"] = 0
            try:
                shot = screen.take_screenshot()
                region = screen.capture_region()
                check(f"{env}={val} on a Mac that is not granted: state 'granted' and the "
                      "capture really runs (no granted + why=screen_recording pair)",
                      screen.screen_recording_state() == "granted"
                      and shot.get("ok") is True and region.get("ok") is True
                      and screen.permissions().get("screen_recording") is True
                      and PREFLIGHT_CALLS["n"] == 0, (shot, region, PREFLIGHT_CALLS))
            finally:
                os.environ.pop(env, None)

        PREFLIGHT_CALLS["n"] = 0
        GRANTED["v"] = True
        check("with no fake set at all, the real preflight is what decides",
              screen._screen_recording_granted() is True and PREFLIGHT_CALLS["n"] == 1)
    finally:
        screen.subprocess.run = _blocked_run
        screen.subprocess.Popen = _blocked_popen
        GRANTED["v"] = False


# ---------------------------------------------------------------- 5. Screenshot app
def t_screenshot_app():
    print("5. open_screenshot_app(): macOS's own tool, honest about whether it opened")
    LAUNCHED.clear()
    ok, why = screen.open_screenshot_app()
    check("opened -> (True, 'opened')", (ok, why) == (True, "opened"), (ok, why))
    check("it is `open -a Screenshot`, nothing else",
          LAUNCHED == [["open", "-a", "Screenshot"]], LAUNCHED)
    screen.subprocess.run = lambda cmd, *a, **kw: _R(1)
    check("a failed open -> (False, 'open_failed')",
          screen.open_screenshot_app() == (False, "open_failed"))

    def boom(cmd, *a, **kw):
        raise OSError("gone")
    screen.subprocess.run = boom
    check("an open that raises -> (False, 'open_failed'), no raise",
          screen.open_screenshot_app() == (False, "open_failed"))
    screen.subprocess.run = _blocked_run
    check("the keys to say are Control-Shift-Command-4 (no em dash, plain words)",
          screen.CLIPBOARD_KEYS == "Control-Shift-Command-4" and "—" not in screen.CLIPBOARD_KEYS)


# ---------------------------------------------------------------- 6. clipboard path
def t_clipboard():
    print("6. Control-Shift-Command-4 then paste: watching the clipboard")
    PB.items, PB.count = {"public.utf8-plain-text": b"old"}, 50
    since = screen.clipboard_count()
    check("clipboard_count() reads the counter", since == 50, since)
    check("text on the clipboard is not an image", screen.clipboard_has_image() is False)

    ok, why = screen.wait_for_clipboard_image(since, timeout=0.05, poll=0.01)
    check("nothing new -> (False, 'timeout')", (ok, why) == (False, "timeout"), (ok, why))

    # She presses the keys: macOS puts a picture there and bumps the counter.
    calls = {"n": 0}
    real_count = screen.clipboard_count

    def counting():
        calls["n"] += 1
        if calls["n"] == 3:
            PB.items = {"public.png": b"\x89PNG\r\n\x1a\nshot", "public.tiff": b"II*"}
            PB.count += 1
        return real_count()
    screen.clipboard_count = counting
    try:
        ok, why = screen.wait_for_clipboard_image(since, timeout=2.0, poll=0.01)
    finally:
        screen.clipboard_count = real_count
    check("a new picture -> (True, 'on_clipboard')", (ok, why) == (True, "on_clipboard"), (ok, why))
    check("a picture that was already there before is not 'new'",
          screen.wait_for_clipboard_image(screen.clipboard_count(), timeout=0.03, poll=0.01)
          == (False, "timeout"))

    since = screen.clipboard_count()
    PB.items, PB.count = {"public.utf8-plain-text": b"hello"}, PB.count + 1
    check("the clipboard changed to text -> (False, 'not_an_image')",
          screen.wait_for_clipboard_image(since, timeout=0.5, poll=0.01) == (False, "not_an_image"))

    since = screen.clipboard_count()
    check("cancel stops the wait -> (False, 'cancelled')",
          screen.wait_for_clipboard_image(since, timeout=5, poll=0.01, cancel=lambda: True)
          == (False, "cancelled"))
    screen._pasteboard = lambda: None
    check("no clipboard at all -> count -1 and (False, 'no_clipboard')",
          screen.clipboard_count() == -1
          and screen.wait_for_clipboard_image(-1, timeout=0.05) == (False, "no_clipboard"))
    check("save_clipboard_image without a clipboard -> (False, 'no_image')",
          screen.save_clipboard_image() == (False, "no_image"))
    screen._pasteboard = lambda: PB

    PB.items = {"public.png": b"\x89PNG\r\n\x1a\nselection-bytes"}
    ok, path = screen.save_clipboard_image()
    check("save_clipboard_image keeps the picture in Pictures/MicMic",
          ok and os.path.dirname(path) == SHOTS
          and os.path.basename(path).startswith("MicMic selection ")
          and Path(path).read_bytes() == b"\x89PNG\r\n\x1a\nselection-bytes", (ok, path))
    check("and not in a guarded folder", not screen.in_protected_folder(path), path)
    PB.items = {"public.utf8-plain-text": b"hi"}
    check("no picture on the clipboard -> (False, 'no_image')",
          screen.save_clipboard_image() == (False, "no_image"))
    PB.items = {"public.png": b""}
    check("an empty picture -> (False, 'no_image')",
          screen.save_clipboard_image() == (False, "no_image"))


# ---------------------------------------------------------------- 6b. paste privacy
class _BehaviorPasteboard(FakePasteboard):
    """A clipboard on newer macOS: reports accessBehavior, and counts every data read
    (the read that can put up macOS's own 'MicMic would like to paste' alert)."""

    def __init__(self, behavior):
        super().__init__()
        self.behavior = behavior
        self.reads = 0

    def accessBehavior(self):
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior

    def dataForType_(self, t):
        self.reads += 1
        return super().dataForType_(t)


def t_paste_access():
    print("6b. paste_access() and save_clipboard_image(): no unannounced paste alert")
    png = b"\x89PNG\r\n\x1a\npasted"
    check("a clipboard without accessBehavior (macOS before pasteboard privacy) is 'free'",
          screen.paste_access() == "free")
    for v, want in ((2, "free"), (1, "ask"), (0, "ask"), (3, "denied"),
                    (RuntimeError("x"), "ask")):
        pb = _BehaviorPasteboard(v)
        screen._pasteboard = lambda pb=pb: pb
        try:
            check(f"accessBehavior {v!r} -> {want!r}", screen.paste_access() == want,
                  screen.paste_access())
        finally:
            screen._pasteboard = lambda: PB
    screen._pasteboard = lambda: None
    check("no clipboard at all -> 'denied'", screen.paste_access() == "denied")
    screen._pasteboard = lambda: PB

    for behavior in (1, 0):
        pb = _BehaviorPasteboard(behavior)
        pb.items = {"public.png": png}
        screen._pasteboard = lambda pb=pb: pb
        try:
            before = sorted(os.listdir(SHOTS)) if os.path.isdir(SHOTS) else []
            res = screen.save_clipboard_image()
            check(f"behavior {behavior} ('ask'): save_clipboard_image() refuses with "
                  "'would_ask' and never reads the data",
                  res == (False, "would_ask") and pb.reads == 0, (res, pb.reads))
            check("and no file was written",
                  (sorted(os.listdir(SHOTS)) if os.path.isdir(SHOTS) else []) == before)
            ok, path = screen.save_clipboard_image(allow_ask=True)
            check("after MicMic's own line, allow_ask=True reads and saves it",
                  ok and pb.reads >= 1 and Path(path).read_bytes() == png, (ok, path))
        finally:
            screen._pasteboard = lambda: PB

    pb = _BehaviorPasteboard(3)
    pb.items = {"public.png": png}
    screen._pasteboard = lambda: pb
    try:
        check("Never (3) -> (False, 'paste_denied'), nothing read, even with allow_ask",
              screen.save_clipboard_image(allow_ask=True) == (False, "paste_denied")
              and pb.reads == 0, pb.reads)
    finally:
        screen._pasteboard = lambda: PB

    pb = _BehaviorPasteboard(2)
    pb.items = {"public.png": png}
    screen._pasteboard = lambda: pb
    try:
        ok, path = screen.save_clipboard_image()
        check("Always Allow (2) -> read straight away", ok and pb.reads >= 1, (ok, path))
        PB_READS_BEFORE = pb.reads
        screen.clipboard_count()
        screen.clipboard_has_image()
        check("watching the counter and the types never reads the data",
              pb.reads == PB_READS_BEFORE, pb.reads)
    finally:
        screen._pasteboard = lambda: PB

    for v, want in (("free", "free"), ("ask", "ask"), ("denied", "denied"), ("junk", "ask")):
        os.environ["MICMIC_FAKE_PASTE_ACCESS"] = v
        try:
            check(f"MICMIC_FAKE_PASTE_ACCESS={v} -> {want!r}", screen.paste_access() == want)
        finally:
            os.environ.pop("MICMIC_FAKE_PASTE_ACCESS", None)
    os.environ["MICMIC_FAKE_PASTE_ACCESS"] = "ask"
    PB.items = {"public.png": png}
    try:
        check("the fake steers save_clipboard_image too",
              screen.save_clipboard_image() == (False, "would_ask"))
    finally:
        os.environ.pop("MICMIC_FAKE_PASTE_ACCESS", None)


# ---------------------------------------------------------------- 7. project rules
DESTRUCTIVE = ["os.remove", "os.unlink", ".unlink(", "rmtree", "rm -rf",
               "DELETE FROM", "DROP TABLE", "shutil.move"]


def t_rules():
    print("7. project rules")
    src = Path(screen.__file__).read_text()
    hits = [b for b in DESTRUCTIVE if b in src]
    check("no delete path in screen.py (test 9)", not hits, hits)
    check("nothing in screen.py saves to the Desktop any more",
          "os.path.join(DESKTOP" not in src)
    check("no osascript ran in this suite", not OSA, OSA)
    check("nothing escaped the subprocess wall except the stubbed open/screencapture",
          all(c and c[0] in ("open", "screencapture") for c in LAUNCHED), LAUNCHED)
    check("no real folder in her home was created by this suite",
          not os.path.exists(os.path.expanduser("~/Desktop/should-not-be-used")))


if __name__ == "__main__":
    for t in (t_folder, t_take, t_region, t_state, t_fake_steers_capture, t_screenshot_app,
              t_clipboard, t_paste_access, t_rules):
        t()
    print("-" * 78)
    print(f"{PASSED} passed, {len(FAILED)} failed")
    print("live calls: 0 (no model, osascript, open, screencapture or clipboard reached)")
    sys.exit(1 if FAILED else 0)
