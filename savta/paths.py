"""Where MicMic's writable state lives.

In a checkout it is the project root, right next to the code, which is what the test
suite and everyday development expect and what every earlier version did.

Inside a signed .app it cannot be. codesign seals Contents/ at signing time, so a
write in there either fails outright or invalidates the seal, and once the seal is
broken Gatekeeper stops trusting the app on the next launch. The failure is nasty
because it is delayed: the app works until the first time it learns something. So a
bundled MicMic keeps its state in Application Support instead, and copies across
anything it finds from the old layout the first time it looks.

Set MICMIC_STATE_DIR to override both, which is how a test gets a scratch directory.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_PKG = Path(__file__).resolve().parent          # .../savta/savta
PROJECT = _PKG.parent                           # .../savta


def is_bundled() -> bool:
    """True when this package is running from inside an .app bundle."""
    return ".app/Contents/" in str(_PKG)


def state_dir() -> Path:
    override = os.environ.get("MICMIC_STATE_DIR")
    if override:
        d = Path(override).expanduser()
    elif is_bundled():
        d = Path.home() / "Library/Application Support/MicMic"
    else:
        d = PROJECT
    d.mkdir(parents=True, exist_ok=True)
    return d


def state(name: str, legacy: Path | None = None) -> Path:
    """Path to one piece of writable state, migrating an older copy in once.

    The legacy file is copied, never moved: if someone downgrades, or runs the
    checkout again after trying the bundle, their history is still where it was.
    """
    p = state_dir() / name
    if legacy is not None and legacy != p and legacy.exists() and not p.exists():
        try:
            shutil.copy2(legacy, p)
        except OSError:
            pass                                 # a missing migration is not fatal
    return p
