"""Whether MicMic opens itself when the Mac logs in.

SMAppService is a PyObjC/ServiceManagement call, which only native/listener.py can
make (that file talks to nothing but this server's HTTP API and knows nothing about
this package — see its module docstring). So the setting is split in two pieces of
state, both kept here, neither ever crossing into savta/router.py's settings.json /
SETTABLE gate (that file is owned elsewhere; this one is intentionally separate):

  desired  -- what the Settings switch last asked for. Set by POST /api/settings.
  status   -- what SMAppService actually reports, as of the listener's last poll
              (every SETTINGS_POLL, native/listener.py refresh_login_item()),
              posted back over POST /api/login_item_status.

GET /api/config reports enabled(): the OS's own word once the listener has checked
in, so a login item she turned off herself in System Settings > General > Login
Items is reflected next time Settings opens, not just what was last requested.
"""
from __future__ import annotations

import json

from . import paths

PATH = paths.state("login_item.json")

# The strings native/listener.py's LOGIN_STATUS maps SMAppService's status to, plus
# "dev_checkout" (a bare `python3 listener.py`, which has no identity to register)
# and "unknown" (nobody has checked in yet).
STATUSES = ("enabled", "not_registered", "requires_approval", "not_found",
            "not_supported", "dev_checkout", "unknown")


def _read() -> dict:
    try:
        d = json.loads(PATH.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write(d: dict) -> None:
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PATH)


def desired() -> bool:
    """What Settings last asked for. Off until she turns it on."""
    return bool(_read().get("desired", False))


def set_desired(value: bool) -> None:
    d = _read()
    d["desired"] = bool(value)
    _write(d)


def status() -> str:
    """What SMAppService actually reported, as of the listener's last poll."""
    s = _read().get("status")
    return s if s in STATUSES else "unknown"


def set_status(value: str) -> None:
    d = _read()
    d["status"] = value if value in STATUSES else "unknown"
    _write(d)


def enabled() -> bool:
    """The switch's real position: the OS's own word when the listener has checked
    in, otherwise the last thing Settings asked for (a fresh install nobody has
    polled yet, or a dev checkout that never will)."""
    s = status()
    if s in ("enabled", "not_registered", "requires_approval", "not_found"):
        return s == "enabled"
    return desired()
