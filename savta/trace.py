"""A written record of every turn: what she said, what it understood, why it chose
what it chose, and what it actually did.

The reason this exists: when something goes wrong in a voice product there is nothing
to look at afterwards. The words are gone, the screen has moved on, and "it did
something weird" is all anyone can report. So every turn writes one line, and that
line is enough to reconstruct the decision without rerunning anything.

One JSONL line per turn, newest last, capped so it cannot grow without bound.
"""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path

from . import paths as _paths
PATH = _paths.state("trace.jsonl",
                    legacy=Path(__file__).resolve().parents[1] / "trace.jsonl")
MAX_LINES = 4000
_LOCK = threading.Lock()

# The signals worth keeping. Everything brain.py answers is interesting in the moment
# and noise a week later; these are the ones that actually decide where a turn goes.
SIGNALS = ("intent", "intent_confidence", "is_complete", "noise", "emergency",
           "distress", "rejects_last", "is_compound", "needs_knowledge",
           "asking_for_notes", "about_clock", "about_weather", "control_action",
           "control_confidence", "inside_an_app", "money_involved", "sounds_coached",
           "speaker_gender", "setting_emergency_contact",
           # Where a message's person and words came from. Without these the owner's
           # "send her on WhatsApp" asking who could not be told apart in the trace
           # from a name that was not in her book.
           "contact_named", "refers_back", "has_message_content", "amends_message",
           "write_in")


def write(turn: dict) -> None:
    """Append one turn. Never raises — a broken log must not break the assistant."""
    try:
        line = json.dumps(turn, ensure_ascii=False, default=str)
        with _LOCK:
            with PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _trim()
    except Exception:  # noqa: BLE001
        pass


def _trim() -> None:
    try:
        if not PATH.exists():
            return
        lines = PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_LINES:
            PATH.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# Turns whose spoken line IS screen content: what she was reading, summarized or
# translated, or an event title taken off the page. The trace keeps that it happened
# and how long it was, never the words. (The adversarial lane found every read-aloud
# row holding the screen verbatim, and a resumed "send this" storing the selection.)
SCREEN_DIDS = {"read_screen", "described_screen", "summarized_screen",
               "translated_screen", "confirm_calendar", "calendar_added"}
_SCREEN_DETAIL = ("text", "event", "title", "body")


def _private(result: dict) -> tuple[object, object]:
    said, detail = result.get("say"), result.get("detail")
    from_screen = isinstance(detail, dict) and detail.get("from_screen")
    if result.get("did") not in SCREEN_DIDS and not from_screen:
        return said, detail
    if isinstance(said, str):
        said = f"[from the screen, {len(said)} chars]"
    if isinstance(detail, dict):
        detail = {k: (f"[{len(str(v))} chars]" if k in _SCREEN_DETAIL else v)
                  for k, v in detail.items()}
    return said, detail


def record(utterance: str, u: dict | None, result: dict, *, conversation: str = "",
           client: str = "", activation: str = "", took_ms: int = 0,
           chose: str = "") -> None:
    """Build the line from what the router already has in hand."""
    signals = {}
    if u:
        for k in SIGNALS:
            v = u.get(k)
            if isinstance(v, float):
                v = round(v, 2)
            if v is not None:
                signals[k] = v
    said, detail = _private(result)
    write({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "conversation": conversation,
        "heard": utterance,
        "did": result.get("did"),
        "said": said,
        "why": chose or result.get("detail", {}).get("why")
               if isinstance(result.get("detail"), dict) else chose,
        "signals": signals,
        "detail": detail,
        "client": client,
        "activation": activation,
        "ms": took_ms or result.get("ms"),
        "jev_calls": result.get("jev_calls"),
        "cost_usd": result.get("cost_usd"),
        # This turn only: Jev and Gemini round trips and their time on the wire.
        "timing": result.get("timing"),
    })


def read(limit: int = 40, conversation: str = "") -> list[dict]:
    """The most recent turns, newest last."""
    try:
        if not PATH.exists():
            return []
        rows = []
        for line in PATH.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if conversation and r.get("conversation") != conversation:
                continue
            rows.append(r)
        return rows[-limit:]
    except Exception:  # noqa: BLE001
        return []
