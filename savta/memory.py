"""What she likes, learned by watching rather than by asking.

Deliberately not a model call. Every time something she asked for actually works —
a song plays, a message goes to someone, she asks about a town — that is a fact, and
counting facts is free and cannot hallucinate. "Play something I like" then has real
candidates behind it instead of a guess.

Separate from `profile.json`, which is who she is, and from `Memory` in router.py,
which is the current conversation and is thrown away when a new one starts. This is
the part that should survive both.
"""
from __future__ import annotations
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from . import paths as _paths
PATH = _paths.state("memory.json",
                    legacy=Path(__file__).resolve().parents[1] / "memory.json")
_LOCK = threading.Lock()

# How many of each kind to keep. Small on purpose: an assistant that remembers
# everything becomes an assistant that acts on something from a year ago.
KEEP = 12

KINDS = ("music", "watch", "people", "places", "apps", "topics")


def _blank() -> dict:
    return {k: {} for k in KINDS} | {"since": int(time.time())}


def load() -> dict:
    try:
        if PATH.exists():
            d = _blank() | json.loads(PATH.read_text(encoding="utf-8"))
            for k in KINDS:
                if not isinstance(d.get(k), dict):
                    d[k] = {}
            return d
    except Exception:  # noqa: BLE001
        pass
    return _blank()


def _save(d: dict) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=str(PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, str(PATH))
    except Exception:  # noqa: BLE001
        pass


def note(kind: str, what: str) -> None:
    """One more time she wanted this. Called only when the thing actually happened."""
    what = (what or "").strip()
    if kind not in KINDS or not what or len(what) > 80:
        return
    with _LOCK:
        d = load()
        row = d[kind].get(what) or {"n": 0, "last": 0}
        row["n"] += 1
        row["last"] = int(time.time())
        d[kind][what] = row
        # Keep the ones she returns to, not merely the ones she did most recently.
        if len(d[kind]) > KEEP:
            ranked = sorted(d[kind].items(),
                            key=lambda kv: (kv[1]["n"], kv[1]["last"]), reverse=True)
            d[kind] = dict(ranked[:KEEP])
        _save(d)


def favourites(kind: str, limit: int = 5) -> list[str]:
    d = load().get(kind) or {}
    ranked = sorted(d.items(), key=lambda kv: (kv[1]["n"], kv[1]["last"]), reverse=True)
    return [k for k, _ in ranked[:limit]]


def summary() -> dict:
    """The few lines worth putting in front of the model on every turn."""
    out = {}
    for kind in KINDS:
        top = favourites(kind, 4)
        if top:
            out[kind] = top
    return out


def forget_all() -> None:
    """There is no delete path for her data anywhere else in this project, but this is
    hers to clear — it is a record of her habits, and she must be able to erase it."""
    with _LOCK:
        _save(_blank())
