"""Record and replay for the two real network calls the test suite makes: Jev
(savta.jev.Jev._ask) and Gemini (savta.llm.LLM._post_once). Both are the lowest
point in their module that actually touches the wire; everything above them
(retries, connection pooling, response parsing) stays real and gets exercised
against whatever `_ask`/`_post_once` hands back, live or replayed.

Modes, from MICMIC_REPLAY (default "replay"):
  off     - today's behaviour: every call is real.
  record  - every call is real, and the (request, response) pair is appended to
            the recording file.
  replay  - nothing goes out. A cache hit returns the recorded response. A MISS
            raises loudly (never a silent pass) unless MICMIC_REPLAY_ALLOW_LIVE=1,
            in which case that one call goes live and is recorded.

Recording file: tests/recordings/test_micmic.jsonl, one JSON object per line:
    {"req": <sha256 hex>, "summary": {...no private text...}, "response": {...}}

Key = sha256 of the endpoint plus a canonical (sorted-key) JSON of the request,
with the wall-clock parts normalised out first: Jev's `state["right_now"]` block
(date/day/time/part_of_day, rebuilt fresh on every call by savta.brain.understand)
and the date/time sentence savta.llm bakes into its Gemini system prompts. Without
this, the exact same test question would hash differently depending on what
second it happened to run, and every recording would go stale the next day.

Re-recording, when a prompt or test question changes:
    MICMIC_REPLAY=record uv run python tests/test_micmic.py
This is a real, billed run (~370 Jev calls, a few Gemini calls; well under $0.10).
To fill in only the handful of keys a small edit invalidated, without re-paying for
everything else:
    MICMIC_REPLAY_ALLOW_LIVE=1 uv run python tests/test_micmic.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

RECORDING = Path(__file__).resolve().parent / "recordings" / "test_micmic.jsonl"
MODE = os.environ.get("MICMIC_REPLAY", "replay").strip().lower()
ALLOW_LIVE = os.environ.get("MICMIC_REPLAY_ALLOW_LIVE", "").strip().lower() in ("1", "true", "yes")

counts = {"replayed": 0, "live": 0}

_lock = threading.Lock()
_store: dict[str, dict] = {}
_loaded = False
_written: set[str] = set()          # keys already appended in this process

_CLOCK_RX = re.compile(
    r"\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{4}-\d{2}-\d{2}"                                    # ISO date
    r"|\b(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)\b"
    r"|\b\d{1,2} (?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December) \d{4}\b")


def _scrub_clock(text: str) -> str:
    return _CLOCK_RX.sub("<CLOCK>", text or "")


# Every j.ask() in savta builds its own state dict, and several ride the wall clock
# in ("time_of_day", "today_is", brain.py's whole "right_now" block, ...) without a
# single shared shape. Rather than chase each call site, any request headed for the
# key is walked recursively: "right_now" (wherever it appears - it also carries a
# non-digit "part_of_day" word the regex above would miss) is blanked structurally,
# and every other string gets the same clock scrub Gemini's system prompt gets. This
# runs only for the cache key; the real request sent live is untouched.
def _normalize(obj):
    if isinstance(obj, dict):
        return {k: ("<CLOCK>" if k == "right_now" else _normalize(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, str):
        return _scrub_clock(obj)
    return obj


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not RECORDING.exists():
        return
    with RECORDING.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            _store[d.get("req") or d["key"]] = d["response"]


def _key(endpoint: str, canon: dict) -> str:
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256((endpoint + "|" + blob).encode()).hexdigest()


def _summarize(endpoint: str, canon: dict) -> dict:
    """What went into the key, without the private text itself: a short hash of the
    canonical body, not the body. Fictional test names only ever appear in the body,
    never here, so this file stays safe to read or commit without a second thought."""
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str)
    return {"endpoint": endpoint, "body_sha256": hashlib.sha256(blob.encode()).hexdigest()[:16],
            "body_len": len(blob)}


def _append(key: str, endpoint: str, canon: dict, response) -> None:
    if key in _written:
        return
    _written.add(key)
    RECORDING.parent.mkdir(parents=True, exist_ok=True)
    with _lock, RECORDING.open("a") as f:
        f.write(json.dumps({"req": key, "summary": _summarize(endpoint, canon),
                            "response": response}, ensure_ascii=False) + "\n")


def _miss(endpoint: str, key: str) -> RuntimeError:
    _load()
    return RuntimeError(
        f"REPLAY MISS: no recording for {endpoint} (key {key[:16]}...). "
        f"{len(_store)} keys are recorded in {RECORDING}.\n"
        f"Re-record with MICMIC_REPLAY=record, or set MICMIC_REPLAY_ALLOW_LIVE=1 to "
        f"call live and fill in just the misses.")


def _install_jev() -> None:
    from savta.jev import Jev

    real_ask = Jev._ask

    def _ask(self, state, questions):
        canon = {"state": _normalize(state), "questions": _normalize(questions)}
        key = _key("jev", canon)
        if MODE != "off" and MODE != "record":
            _load()
            with _lock:
                rec = _store.get(key)
            if rec is not None:
                with self._lock:
                    self.calls += 1
                    self.input_tokens += rec.get("input_tokens", 0)
                    self.last_ms = 0.1
                counts["replayed"] += 1
                return rec["answers"]
            if not ALLOW_LIVE:
                raise _miss("jev", key)
        before_tok = self.input_tokens
        answers = real_ask(self, state, questions)
        if MODE != "off":
            _append(key, "jev", canon, {"answers": answers,
                                        "input_tokens": self.input_tokens - before_tok})
            counts["live"] += 1
        return answers

    Jev._ask = _ask


def _install_llm() -> None:
    from savta.llm import LLM

    real_post = LLM._post_once

    def _post_once(self, payload, model=None, timeout=None):
        m = model or self.model
        endpoint = f"gemini/{m}"
        canon = {"model": m, "payload": _normalize(payload)}
        key = _key(endpoint, canon)
        if MODE != "off" and MODE != "record":
            _load()
            with _lock:
                have = key in _store
                rec = _store.get(key)
            if have:
                self.calls += 1
                self.last_ms = 0.1
                self.last_error = None
                self.quota_exceeded = None
                counts["replayed"] += 1
                return rec
            if not ALLOW_LIVE:
                raise _miss(endpoint, key)
        d = real_post(self, payload, model, timeout)
        if MODE != "off":
            _append(key, endpoint, canon, d)
            counts["live"] += 1
        return d

    LLM._post_once = _post_once


def install() -> None:
    """Patch Jev._ask and LLM._post_once for the rest of this process. Call once,
    before any test runs (and before the shared Jev()/LLM() instances are used)."""
    if MODE not in ("off", "record", "replay"):
        raise SystemExit(f"MICMIC_REPLAY={MODE!r} is not off/record/replay")
    _install_jev()
    _install_llm()


def summary_line() -> str:
    return f"replayed {counts['replayed']}, live {counts['live']} (MICMIC_REPLAY={MODE})"


def skip_latency() -> bool:
    """True when a pure network-latency assertion (Jev/Gemini round trip time) would
    no longer measure anything real and should print SKIP instead of a free pass.
    Real in "off" and "record" mode (both make the actual call); meaningless in
    "replay" mode, where the round trip is a dict lookup."""
    return MODE == "replay"
