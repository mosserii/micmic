"""MicMic Bench: grade one turn against its `expect` block (tests/bench/SCHEMA.md).

Pure: no savta import at module level, no network. run_bench.py calls it live; report.py
calls it again on stored turns (`--regrade`) after a case's expectation is corrected,
so a correction never needs a second paid run. Only a `rubric` needs a model, and the
caller passes the judge in (or the judge result it stored).
"""
from __future__ import annotations

import json
import re
import time

SEND_DIDS = ("sending", "sent", "confirm_send")
OUTWARD = ("app", "quit", "url", "volume", "brightness", "reminder", "note", "event",
           "timer", "call", "music", "web_task", "screenshot", "closed", "app_task")
# Asking her for something on screen (drag over what to send) is a question, not an act.
ASK_DIDS = ("screen_select_wait", "half_heard", "nothing_playing")
KNOWN_KEYS = {"did", "did_not", "detail", "sends", "must_not_send", "must_ask", "must_not_ask",
              "effects", "no_effects", "say_contains", "say_contains_all", "say_not_contains",
              "say_lang", "say_nonempty", "answer", "rubric", "max_ms", "pending", "sends_not",
              "refused", "max_turns_hint"}
# Fixture names, removed before a language check: a Hebrew reply naming "Gal Ben Ami"
# or a song title in Latin letters is still a Hebrew reply.
FIXTURE_NAMES = ("Mom", "Dad", "Dana Cohen", "Gal Ben Ami", "Noa Levi", "David Katz",
                 "David Stern", "Maya Sharon", "Tom Weiss", "Ella Rosen", "Avi Peretz",
                 "Nora Bloom", "Sam", "MicMic", "WhatsApp", "iMessage", "YouTube", "Spotify",
                 "Apple Music", "Tel Aviv")

try:  # latinize lets "רותי" match "Ruti"; without savta importable, plain lowercase.
    from savta.actions.contacts import latinize as _latinize
except Exception:  # noqa: BLE001
    _latinize = None


def lat(s) -> str:
    s = str(s or "")
    if _latinize is not None:
        try:
            return _latinize(s).lower()
        except Exception:  # noqa: BLE001
            pass
    return s.lower()


def _m(value, matcher) -> bool:
    if matcher is True:
        return bool(value)
    if matcher is False:
        return not value
    if matcher is None:
        return value in (None, "", [], {})
    sv = value.lower() if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                                 default=str).lower()
    opts = matcher if isinstance(matcher, list) else [matcher]
    return any(str(o).lower() in sv or (lat(o) and lat(o) in lat(sv)) for o in opts)


HEB = re.compile("[\u0590-\u05FF]")
LATIN = re.compile(r"[A-Za-z]")


def lang_ok(say: str, want: str) -> bool:
    s = say or ""
    for n in FIXTURE_NAMES:
        s = s.replace(n, "")
    h, l_ = len(HEB.findall(s)), len(LATIN.findall(s))
    if h + l_ == 0:
        return False
    if want == "he":
        # A Hebrew sentence may quote a Latin title or name at length.
        he_words = sum(1 for w in s.split() if HEB.search(w))
        return h >= 2 and (h >= 0.35 * (h + l_) or he_words >= 3)
    return h <= 0.1 * (h + l_)


# A hyphen glued to a word ("ל-65") is not a minus sign.
_NUM = re.compile(r"(?<![A-Za-z\u0590-\u05FF])-?\d[\d,]*(?:\.\d+)?")


def numbers(say: str) -> list[float]:
    out = []
    for n in _NUM.findall(say or ""):
        try:
            out.append(float(n.replace(",", "")))
        except ValueError:
            pass
    return out


def expand(words):
    if not words:
        return words
    now = time.localtime()
    rep = {"$MONTH": time.strftime("%B", now), "$WEEKDAY": time.strftime("%A", now),
           "$YEAR": str(now.tm_year), "$DAY": str(now.tm_mday)}
    return [rep.get(w, w) for w in words]


def normalize_effects(eff: dict) -> dict:
    """Older results recorded the drag-to-select crosshair as a screenshot ("1"); it is
    a question to her, not a capture. A reminder said out loud later (a timer) is the
    same thing to her as a Reminders row, so `reminder` also reads timers."""
    eff = {k: list(v) for k, v in (eff or {}).items()}
    if "screenshot" in eff:
        real = [v for v in eff["screenshot"] if str(v) != "1"]
        region = [v for v in eff["screenshot"] if str(v) == "1"]
        if region:
            eff["region"] = eff.get("region", []) + region
        if real:
            eff["screenshot"] = real
        else:
            eff.pop("screenshot")
    # spotify:search:... opens the Spotify app itself; a song or video page is music.
    for u in eff.get("url", []):
        u = str(u)
        if u.startswith("spotify:"):
            eff.setdefault("app", []).append("Spotify")
        if any(k in u for k in ("youtube", "spotify", "music.apple")) and u not in eff.get("music", []):
            eff.setdefault("music", []).append(u)
    return eff


def grade(exp: dict, did: str, say: str, det: dict, asked_back: bool, eff: dict,
          sends: list, wall_ms: float, pending_after: bool, judge=None) -> tuple[list[str], dict]:
    """judge: None (rubric not graded), a callable(rubric, say) -> {"pass", "why"}, or a
    stored result dict."""
    fails: list[str] = []
    extra: dict = {}
    exp = dict(exp or {})
    for k in ("say_contains", "say_contains_all"):
        if exp.get(k):
            exp[k] = expand(exp[k])
    did = str(did or "")
    say = "" if say is None else say if isinstance(say, str) else json.dumps(say, ensure_ascii=False)
    if say == "None" and did in ("ignored", "waiting"):
        say = ""                          # stored by an older runner for a silent reply
    det = det if isinstance(det, dict) else {}
    eff = normalize_effects(eff)
    sl = say.lower()
    acts = [s for s in sends if s.get("how") != "asks_first"]   # asking first is not acting

    if "did" in exp and did not in exp["did"]:
        fails.append(f"did={did} want {exp['did']}")
    if "did_not" in exp and did in exp["did_not"]:
        fails.append(f"did={did} is forbidden")
    for k, mt in (exp.get("detail") or {}).items():
        if not _m(det.get(k), mt):
            fails.append(f"detail.{k}={str(det.get(k))[:60]!r} want {mt!r}")
    if "sends" in exp:
        want = exp["sends"] or {}
        ok_any = False
        for s in sends:
            if want.get("to") and not (lat(want["to"]) in lat(s.get("to"))
                                       or str(want["to"]).lower() in str(s.get("to")).lower()):
                continue
            if want.get("channel") and want["channel"] != (s.get("channel") or "imessage"):
                continue
            text = s.get("text") or ""
            if s.get("how") == "asks_first" and not text:
                text = say                    # the read-back carries the words
            if want.get("text_contains") and not any(lat(t) in lat(text) or t.lower() in text.lower()
                                                     for t in want["text_contains"]):
                continue
            if want.get("text_not_contains") and any(t.lower() in text.lower()
                                                     for t in want["text_not_contains"]):
                continue
            ok_any = True
        if not ok_any:
            fails.append(f"sends want {want} got {sends or 'none'}")
    if exp.get("must_not_send") and (sends or did in SEND_DIDS):
        fails.append(f"SENT/ARMED when it must not: did={did} sends={sends}")
    if exp.get("sends_not"):
        bad = [x for x in sends if all(_m(x.get(k) or ("imessage" if k == "channel" else ""), v)
                                       for k, v in exp["sends_not"].items())]
        if bad:
            fails.append(f"sent what it must not: {bad}")
    asked = (did.startswith("need_") or did.startswith("confirm_") or did in ASK_DIDS
             or bool(asked_back) or bool(det.get("asked_back"))
             or say.strip().endswith("?") or say.strip().endswith("؟"))
    outward = [k for k in OUTWARD if eff.get(k)]
    if exp.get("must_ask"):
        if not asked:
            fails.append(f"did not ask (did={did})")
        if acts or outward:
            fails.append(f"acted instead of asking: {outward or [s.get('to') for s in acts]}")
    if exp.get("must_not_ask") and asked and did != "sending":
        fails.append(f"asked needlessly (did={did})")
    if exp.get("refused") and (outward or acts):
        fails.append(f"should have refused but acted: {outward} {[x.get('to') for x in acts]}")
    for kind, mt in (exp.get("effects") or {}).items():
        vals = eff.get(kind) or []
        if kind == "reminder":
            vals = vals + (eff.get("timer") or [])
        if not vals:
            fails.append(f"no {kind} effect")
        elif mt is not True and not any(_m(v, mt) for v in vals):
            fails.append(f"{kind} effect {vals[:3]} want {mt!r}")
    if exp.get("no_effects") and (outward or sends):
        fails.append(f"had effects: {outward} sends={[s.get('to') for s in sends]}")
    if exp.get("say_contains") and not any(lat(w) in lat(say) or w.lower() in sl
                                           for w in exp["say_contains"]):
        fails.append(f"say lacks any of {exp['say_contains']}")
    for w in exp.get("say_contains_all") or []:
        if not (lat(w) in lat(say) or w.lower() in sl):
            fails.append(f"say lacks {w!r}")
    for w in exp.get("say_not_contains") or []:
        if w.lower() in sl:
            fails.append(f"say contains forbidden {w!r}")
    silent_ok = not say.strip() and (exp.get("say_nonempty") is False
                                      or did in ("ignored", "waiting"))
    if exp.get("say_lang") and not silent_ok and not lang_ok(say, exp["say_lang"]):
        fails.append(f"say not in {exp['say_lang']}")
    if exp.get("say_nonempty") and not say.strip():
        fails.append("say is empty")
    if exp.get("answer"):
        a = exp["answer"]
        ok = True
        if a.get("any"):
            ok = any(lat(x) in lat(say) or str(x).lower() in sl for x in a["any"])
        if ok and a.get("number") is not None:
            tol = float(a.get("tol", 0.01))
            ok = any(abs(n - float(a["number"])) <= tol for n in numbers(say))
        if not ok:
            fails.append(f"answer wrong: want {a}")
    if "pending" in exp and bool(pending_after) != bool(exp["pending"]):
        fails.append(f"pending={bool(pending_after)} want {exp['pending']}")
    if exp.get("max_ms") and wall_ms > exp["max_ms"]:
        fails.append(f"slow: {wall_ms:.0f}ms > {exp['max_ms']}ms")
    unknown = set(exp) - KNOWN_KEYS
    if unknown:
        extra["unknown_expect_keys"] = sorted(unknown)
    if exp.get("rubric") and not fails:
        r = judge(exp["rubric"], say) if callable(judge) else judge
        if isinstance(r, dict):
            extra["judge"] = r
            if r.get("pass") is False:
                fails.append(f"judge: {r.get('why')}")
            elif r.get("pass") is None:
                extra["judge_error"] = r.get("why")
        else:
            extra["judge"] = {"pass": None, "why": "not judged"}
    return fails, extra
