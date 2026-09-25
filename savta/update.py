"""Is there a newer MicMic? Asked of the public releases repo, at most every six
hours, and only ever a GET of public data: nothing about her goes with it.

The installer lives in the GitHub releases of mosserii/micmic, the open-source repo
(v1.0.0 still asks mosserii/micmic-releases), tagged v<version>. Without this, everyone who downloaded 1.0 would stay on
1.0 forever with no way to hear about a fix."""
from __future__ import annotations

import json
import threading
import time
import urllib.request

from .account import APP_VERSION

LATEST = "https://api.github.com/repos/mosserii/micmic/releases/latest"
PAGE = "https://github.com/mosserii/micmic/releases/latest"
EVERY_S = 6 * 3600

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "latest": None}


def _parse(v: str) -> tuple[int, ...]:
    out = []
    for part in (v or "").lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))[:3]


def check(force: bool = False) -> dict:
    """{"current", "latest", "newer", "url"}; latest is None when it could not ask."""
    with _lock:
        fresh = time.time() - _cache["at"] < EVERY_S
        latest = _cache["latest"]
    if force or not fresh:
        try:
            req = urllib.request.Request(LATEST, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"MicMic/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=6) as r:
                latest = str(json.loads(r.read()).get("tag_name") or "") or None
        except Exception:  # noqa: BLE001  (offline, rate-limited: try again later)
            latest = latest
        with _lock:
            _cache.update(at=time.time(), latest=latest)
    newer = bool(latest) and _parse(latest) > _parse(APP_VERSION)
    return {"current": APP_VERSION, "latest": latest, "newer": newer, "url": PAGE}
