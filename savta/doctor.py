"""What works on this Mac right now, and what needs one click to fix.

Several capabilities depend on macOS permissions that can only be granted by a person
clicking a dialog. A script cannot grant them and cannot make the dialog appear from a
background process, so the honest thing is to check each one and say plainly what is
missing rather than let a feature fail silently at the moment she needs it.

    python3 -m savta.doctor
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GREEN, RED, DIM, WARN, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[33m", "\033[0m"


def _osa(script: str, timeout: float = 6.0) -> tuple[bool, str]:
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "timed out waiting for permission"
    except Exception as e:  # noqa: BLE001
        return False, repr(e)[:90]


def check_key(name: str, env: str) -> dict:
    val = os.environ.get(env, "")
    if not val:
        for p in (ROOT.parent / ".env.local", ROOT / ".env.local"):
            if p.exists():
                for line in p.read_text().splitlines():
                    if line.startswith(f"{env}="):
                        val = line.split("=", 1)[1].strip()
    return {"name": name, "ok": bool(val), "detail": "found" if val else f"{env} is not set",
            "fix": f"put {env}=... in {ROOT.parent / '.env.local'}", "required": env == "TYPESAFE_API_KEY"}


def check_jev() -> dict:
    """Jev is reachable either with her own key or through the MicMic account (the
    proxy). Only having neither is a failure."""
    from .jev import mode
    m = mode()
    detail = {"own_key": "her own key", "proxy": "MicMic account"}.get(m, "not set up")
    return {"name": "Jev access", "ok": m != "none", "detail": detail,
            "fix": f"sign in to MicMic, or put TYPESAFE_API_KEY=... in {ROOT.parent / '.env.local'}",
            "required": True}


def check_app(label: str, app: str, probe: str, fix: str, required: bool = False) -> dict:
    ok, msg = _osa(probe)
    return {"name": label, "ok": ok, "detail": "working" if ok else msg[:90],
            "fix": fix, "required": required}


def check_file(label: str, path: Path, fix: str, required: bool = False) -> dict:
    """Exists AND can be read — which are different questions on macOS.

    Opening a file inside another app's container BLOCKS when the privacy permission
    has not been granted; it does not raise. Doing that in-process hung this checker
    on the very check meant to diagnose it, so the read happens in a process that can
    simply be abandoned.
    """
    if not path.exists():
        return {"name": label, "ok": False, "detail": "not on this Mac",
                "fix": fix, "required": required}
    try:
        r = subprocess.run(["/usr/bin/head", "-c", "1", str(path)],
                           capture_output=True, timeout=4)
        ok = r.returncode == 0
        detail = "readable" if ok else "there, but reading it is blocked"
    except subprocess.TimeoutExpired:
        ok, detail = False, "there, but reading it hangs — permission not granted"
    except Exception:  # noqa: BLE001
        ok, detail = False, "there, but could not be read"
    return {"name": label, "ok": ok, "detail": detail,
            "fix": fix if not ok else "nothing to grant", "required": required}


def run() -> list[dict]:
    wa = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
    checks = [
        check_jev(),
        check_key("Gemini API key (answers, multi-step)", "GEMINI_API_KEY"),
        check_contacts_readable(),
        check_inside_apps(),
        check_file("WhatsApp messages", wa / "ChatStorage.sqlite",
                   "System Settings > Privacy & Security > Full Disk Access > add "
                   "MicMic (and Terminal, if you start it from a terminal)"),
        check_app("Play video and music", "Chrome",
                  'return "ok"', "nothing to grant", required=True),
        check_app("Send a message (Messages)", "Messages",
                  'tell application "Messages" to return (count of services) as string',
                  "System Settings > Privacy & Security > Automation > allow Terminal to control Messages"),
        check_app("Make notes", "Notes",
                  'tell application "Notes" to return (count of folders) as string',
                  "System Settings > Privacy & Security > Automation > allow Terminal to control Notes"),
        check_app("Set reminders", "Reminders",
                  'tell application "Reminders" to return (count of lists) as string',
                  "System Settings > Privacy & Security > Automation > allow Terminal to control Reminders"),
        check_app("Read the calendar", "Calendar",
                  'tell application "Calendar" to return (count of calendars) as string',
                  "System Settings > Privacy & Security > Automation > allow Terminal to control Calendar"),
        check_app("Volume control", "System Events",
                  "output volume of (get volume settings)", "nothing to grant"),
        {"name": "Open apps", **(lambda r: {"ok": r, "detail": "working" if r else "open -a failed",
                                            "fix": "", "required": False})(
            subprocess.run(["open", "-Ra", "Calculator"], capture_output=True,
                           timeout=8).returncode == 0)},
    ]
    # browser agent
    venv_py = ROOT / ".venv" / "bin" / "python3"
    have_pw = False
    for py in (sys.executable, str(venv_py) if venv_py.exists() else None):
        if not py:
            continue
        try:
            if subprocess.run([py, "-c", "import playwright"], capture_output=True,
                              timeout=20).returncode == 0:
                have_pw = True
                break
        except Exception:  # noqa: BLE001
            pass
    checks.append({"name": "Book and buy on websites", "ok": have_pw,
                   "detail": "playwright installed" if have_pw else "playwright not installed",
                   "fix": f"{venv_py} -m pip install playwright", "required": False})
    return checks


def check_inside_apps():
    """Can MicMic work inside other applications right now?

    Three different things stop this and they look identical from the inside — a
    locked screen, an application with nothing open, and a missing permission — so
    each is named rather than guessed at. On a healthy machine this reads real
    controls out of a real window, which is the only proof that actually counts.
    """
    from .actions import apps, mac
    name = "Work inside other apps"
    ok, why = apps.available()
    if not ok:
        return {"name": name, "ok": False, "detail": "not available", "fix": why,
                "required": False}
    if mac.screen_locked():
        return {"name": name, "ok": False, "detail": "the screen is locked",
                "fix": "unlock the Mac and run this again — nothing inside an "
                       "application can be read while it is locked",
                "required": False}
    for candidate in ("Finder", "Notes", "Calculator", "Safari", "Google Chrome"):
        snap = apps.snapshot(candidate)
        if not snap.get("error") and snap["elements"]:
            labels = [e["label"] for e in snap["elements"] if e["label"]][:3]
            return {"name": name, "ok": True,
                    "detail": f"read {len(snap['elements'])} controls in {candidate} "
                              f"({', '.join(labels)})",
                    "fix": "nothing to grant", "required": False}
    return {"name": name, "ok": False,
            "detail": "no application would show its controls",
            "fix": "System Settings > Privacy & Security > Accessibility > add "
                   "whatever runs MicMic, then open a window in any app and retry",
            "required": False}


def check_contacts_readable():
    """Her address book exists AND can actually be read.

    Checking the file is there is not enough: macOS lets stat() through while the
    first read of the contents blocks on a privacy permission that nobody is present
    to grant. That used to hang every request, so this reports the difference between
    "no WhatsApp" and "WhatsApp is there but this process may not read it".
    """
    import time
    from .actions import contacts as book
    wa = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
    name = "WhatsApp contacts"
    if not (wa / "ContactsV2.sqlite").exists():
        return {"name": name, "ok": False, "detail": "not on this Mac",
                "fix": "open WhatsApp on this Mac once and sign in", "required": False}
    t = time.time()
    rows = book.all_contacts(force=True, wait=6.0)
    took = time.time() - t
    if rows:
        return {"name": name, "ok": True,
                "detail": f"{len(rows)} names, read in {took:.1f}s",
                "fix": "nothing to grant", "required": False}
    return {"name": name, "ok": False,
            "detail": "the file is there, but reading it is blocked",
            "fix": "System Settings > Privacy & Security > Full Disk Access > add "
                   "MicMic (and Terminal, if you start it from a terminal). Without "
                   "this MicMic still works, it just cannot call anyone by name.",
            "required": False}


def main() -> int:
    print(f"\n  MicMic setup check   {time.strftime('%Y-%m-%d %H:%M')}\n")
    rows = run()
    missing_required, missing_optional = [], []
    for r in rows:
        mark = f"{GREEN}works{OFF}" if r["ok"] else (
            f"{RED}missing{OFF}" if r["required"] else f"{WARN}not yet{OFF}")
        print(f"  {mark:<18} {r['name']}")
        if not r["ok"]:
            print(f"  {DIM}{'':<9} {r['detail']}{OFF}")
            if r["fix"] and r["fix"] != "nothing to grant":
                print(f"  {DIM}{'':<9} fix: {r['fix']}{OFF}")
            (missing_required if r["required"] else missing_optional).append(r["name"])
    print()
    if missing_required:
        print(f"  {RED}MicMic cannot run until these are fixed: "
              f"{', '.join(missing_required)}{OFF}")
    elif missing_optional:
        print(f"  MicMic will run. These features stay off until granted: "
              f"{', '.join(missing_optional)}")
        print(f"  {DIM}Most are one click in System Settings > Privacy & Security > "
              f"Automation. The dialog appears the first time you try the feature "
              f"from a normal Terminal window.{OFF}")
    else:
        print(f"  {GREEN}Everything is working.{OFF}")
    print()
    return 1 if missing_required else 0


if __name__ == "__main__":
    sys.exit(main())
