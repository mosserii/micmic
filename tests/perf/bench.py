#!/usr/bin/env python3
"""Latency bench for one MicMic turn, measured the way she feels it.

    cd <checkout> && .venv/bin/python3 tests/perf/bench.py [--repeats 3] [--label NAME]
    .venv/bin/python3 tests/perf/bench.py --probe          # round-trip costs only
    .venv/bin/python3 tests/perf/bench.py --listener LOG   # release/dispatch/reply gaps

A fixed question set (QUESTIONS below, four languages) goes through router.handle()
in this process, exactly as server.py calls it for the Mac app (client="native",
activation="push", recent built the way the server builds it). Every Jev round trip
and every Gemini round trip is timed where it leaves the process, and attributed to
the function that made it, so a turn breaks down into:

    understand   the one big brain.understand() call
    jev_extra    every other Jev call (pick_span, pick_from, _same_person, ...)
    gemini       every Gemini call (chat, answer, split_steps, zone lookup, screen)
    other        wall time with no network call in flight: code, disk, stubs

Stages can overlap once calls run in parallel, so "other" is the wall time minus the
UNION of the busy intervals, and the three network stages are sums.

NOTHING HERE LEAVES THE MACHINE except the Jev and Gemini questions themselves. The
stub wall is tests/test_micmic.py, imported as a module (its main() never runs): mac.*
side effects are recorders, osascript and `open` are blocked, the address book is a
fixed fixture, YouTube and the weather are fixtures. The screen is stubbed here with a
fixed page of text. speak=False everywhere.

The first repeat is also recorded to tests/perf/replay.json, which is what
tests/perf/perf_check.py replays offline to count round trips for free.
"""
from __future__ import annotations

import os
import sys
import tempfile

# Before savta is imported anywhere: state goes to a fresh scratch directory.
os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-perf-state-")
for _k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL"):
    os.environ.pop(_k, None)

import argparse          # noqa: E402
import importlib.util    # noqa: E402
import json              # noqa: E402
import statistics        # noqa: E402
import subprocess        # noqa: E402
import threading         # noqa: E402
import time              # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
REPLAY = HERE / "replay.json"

# ---------------------------------------------------------------- question set
# Each case is one short conversation; every turn in it is timed. Turn one starts
# from a fresh state, later turns carry the server's `recent` line, like the app.
QUESTIONS: list[tuple[str, list[str]]] = [
    ("clock_en",        ["what time is it"]),
    ("clock_he",        ["מה השעה"]),
    ("clock_ar",        ["كم الساعة هلق"]),
    ("clock_zone_en",   ["what time is it in New York"]),
    ("weather_en",      ["what's the weather like today"]),
    ("weather_he",      ["מה מזג האוויר בתל אביב"]),
    ("weather_ru",      ["какая погода в Москве"]),
    ("joke_en",         ["tell me a joke"]),
    ("joke_ar",         ["احكيلي نكتة"]),
    ("joke_ru",         ["расскажи анекдот"]),
    ("open_app_en",     ["open the calculator"]),
    ("open_app_he",     ["תפתחי את לוח השנה"]),
    ("music_en",        ["play some Umm Kulthum"]),
    ("music_he",        ["תשימי לי שיר של זוהר ארגוב"]),
    ("music_ar",        ["شغليلي أغنية لفيروز"]),
    ("message_en",      ["send Miriam a message that I will be late"]),
    ("message_he",      ["תשלחי הודעה לזוהר שאני בסדר"]),
    ("message_ru",      ["напиши Мириам что я приду позже"]),
    ("screen_en",       ["what's on my screen"]),
    ("screen_he",       ["תסכמי את מה שיש על המסך"]),
    ("knowledge_he",    ["מי היה ראש הממשלה הראשון של ישראל"]),
    ("undo_en",         ["make it louder", "undo"]),
    ("undo_he",         ["תגבירי את הקול", "בטלי"]),
    ("followup_en",     ["what is the capital of France", "and what about Italy"]),
]

SCREEN = {"permissions": {"accessibility": True, "screen_recording": False},
          "frontmost": {"app": "Google Chrome", "pid": 1, "bundle_id": "com.google.Chrome",
                        "window": "Recipe"},
          "selected": "", "focused": {"role": "", "value": "", "secure": False},
          "visible": {"text": "A recipe for shakshuka with tomatoes, eggs and cumin. "
                              "Fry the onion, add the peppers and the tomatoes, simmer for "
                              "ten minutes, then crack in the eggs and cover. Serves four. "
                              "Ready in twenty minutes.", "truncated": False},
          "page": {"url": "https://example.org/shakshuka", "title": "Shakshuka recipe"},
          "has_image": False}

# Eighteen results, the size yt.search really returns, so pick_result carries a
# realistic payload. Titles only; nothing is fetched.
VIDEOS = [{"id": f"v{i}", "title": t, "channel": c, "length": ln, "views": vw}
          for i, (t, c, ln, vw) in enumerate([
              ("Umm Kulthum - Enta Omri (full concert)", "Classics", "1:02:11", "3.1M views"),
              ("Greatest hits compilation", "Music Box", "58:02", "900K views"),
              ("Zohar Argov - HaPerach BeGani", "Argov Official", "4:12", "5.2M views"),
              ("Fairuz - Kifak Inta", "Fairuz", "5:40", "12M views"),
              ("Fairuz morning songs, 1 hour", "Arabic Classics", "1:01:00", "30M views"),
              ("Reaction: first time hearing this", "React Guy", "12:30", "40K views"),
              ("Karaoke version", "Sing Along", "4:10", "80K views"),
              ("Live in Paris 1967", "Archive", "1:30:00", "700K views"),
              ("Top 10 songs", "Lists", "25:00", "200K views"),
              ("Official audio", "Label", "4:01", "8M views"),
              ("Lyrics video", "Lyrics", "4:05", "1.1M views"),
              ("Documentary: the voice of a nation", "DocTV", "52:00", "600K views"),
              ("Cover by a student", "Student", "3:50", "9K views"),
              ("Remix 2024", "DJ", "3:20", "300K views"),
              ("Rare recording", "Collector", "7:45", "90K views"),
              ("Full album", "Label", "45:10", "2M views"),
              ("Interview", "News", "15:00", "150K views"),
              ("Tribute concert", "TV", "1:10:00", "400K views"),
          ])]


def q(xs: list[float], p: float) -> float:
    """Linear-interpolated percentile; p in 0..1."""
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def union_ms(spans: list[tuple[float, float]]) -> float:
    total, end = 0.0, None
    for a, b in sorted(spans):
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total * 1000


def git_head() -> str:
    try:
        rev = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain",
                                "--", "savta", "native"],
                               capture_output=True, text=True).stdout.strip()
        return rev + ("+dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "?"


# ---------------------------------------------------------------- the stub wall
def load_wall():
    spec = importlib.util.spec_from_file_location("test_micmic", ROOT / "tests" / "test_micmic.py")
    tm = importlib.util.module_from_spec(spec)
    sys.modules["test_micmic"] = tm
    spec.loader.exec_module(tm)          # the stubs, never main()
    router, mac = tm.router, tm.mac
    router._screen_context = lambda max_chars=6000: json.loads(json.dumps(SCREEN))
    router._screen_shot = lambda pid=None: None
    tm.yt.search = lambda q_, n=18: [dict(v) for v in VIDEOS]
    # The real inventory is a directory listing and nothing more; it makes the
    # open_app candidate list the size it is on a real Mac.
    mac.installed_apps = _installed_apps
    return tm


def _installed_apps(limit: int = 200) -> list[str]:
    out = []
    for d in ("/Applications", "/System/Applications", "/System/Applications/Utilities"):
        try:
            out += [n[:-4] for n in os.listdir(d) if n.endswith(".app")]
        except OSError:
            pass
    return sorted(set(out))[:limit]


# ---------------------------------------------------------------- instrumentation
class Recorder:
    """Wraps Jev.ask and LLM._post. One list of calls per turn."""

    def __init__(self, record_replay: bool):
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.turn_key = ""
        self.record_replay = record_replay
        self.replay: dict[str, list] = {}

    def install(self, Jev, LLM):
        rec = self
        real_ask, real_post = Jev.ask, LLM._post

        def ask(j, state, questions):
            caller = sys._getframe(1).f_code.co_name
            t0 = time.time()
            ok = True
            try:
                ans = real_ask(j, state, questions)
                return ans
            except Exception:
                ok = False
                ans = None
                raise
            finally:
                t1 = time.time()
                with rec.lock:
                    rec.calls.append({"kind": "jev", "caller": caller, "t0": t0, "t1": t1,
                                      "ms": round((t1 - t0) * 1000, 1), "ok": ok,
                                      "nq": len(questions),
                                      "bytes": len(json.dumps({"state": state, "questions": questions},
                                                              ensure_ascii=False).encode())})
                    if rec.record_replay and ok:
                        key = f"{rec.turn_key}|jev|{caller}|{','.join(sorted(questions))}"
                        rec.replay.setdefault(key, []).append(ans)

        def post(llm, payload, model=None, timeout=None):
            f = sys._getframe(1)
            caller = f.f_code.co_name
            if caller in ("text", "generate_content"):
                caller = f.f_back.f_code.co_name
            t0 = time.time()
            d = real_post(llm, payload, model, timeout)
            t1 = time.time()
            usage = (d or {}).get("usageMetadata", {}) if isinstance(d, dict) else {}
            with rec.lock:
                rec.calls.append({"kind": "gemini", "caller": caller, "t0": t0, "t1": t1,
                                  "ms": round((t1 - t0) * 1000, 1), "ok": d is not None,
                                  "in_tok": usage.get("promptTokenCount", 0),
                                  "out_tok": usage.get("candidatesTokenCount", 0),
                                  "err": None if d is not None else llm.last_error})
                if rec.record_replay and d is not None:
                    key = f"{rec.turn_key}|gemini|{caller}"
                    rec.replay.setdefault(key, []).append(d)
            return d

        Jev.ask = ask
        LLM._post = post

    def take(self) -> list[dict]:
        with self.lock:
            out, self.calls = self.calls, []
        return out


def stage_of(c: dict) -> str:
    if c["kind"] == "gemini":
        return "gemini"
    return "understand" if c["caller"] == "understand" else "jev_extra"


def run_case(tm, j, rec: Recorder, repeat: int, case: str, turns: list[str],
             arm: str = "", before_turn=None) -> list[dict]:
    router = tm.router
    rows = []
    tm.reset_state()
    router.LAST_EMERGENCY = None
    p = tm.prof.load()
    p["last_briefed"] = time.strftime("%Y-%m-%d")      # no morning briefing mid-bench
    tm.prof.save(p)
    recent_log: list[str] = []
    for ti, text in enumerate(turns):
        if before_turn:
            before_turn()                  # while she is still speaking: not timed
        rec.turn_key = f"{case}#{ti}"
        rec.take()
        recent = " | ".join(recent_log[-2:])
        c0 = j.calls
        t0 = time.time()
        err = None
        try:
            res = router.handle(j, text, recent, speak=False, client="native",
                                activation="push")
        except Exception as e:  # noqa: BLE001
            res, err = {"did": "raised"}, repr(e)[:200]
        t1 = time.time()
        calls = rec.take()
        wall = (t1 - t0) * 1000
        busy = union_ms([(c["t0"], c["t1"]) for c in calls])
        st = {"understand": 0.0, "jev_extra": 0.0, "gemini": 0.0}
        for c in calls:
            st[stage_of(c)] += c["ms"]
        rows.append({"case": case, "turn": ti, "text": text, "repeat": repeat, "arm": arm,
                     "did": res.get("did"), "err": err,
                     "wall_ms": round(wall, 1), "other_ms": round(max(0.0, wall - busy), 1),
                     **{k: round(v, 1) for k, v in st.items()},
                     "jev_calls": sum(1 for c in calls if c["kind"] == "jev"),
                     "gemini_calls": sum(1 for c in calls if c["kind"] == "gemini"),
                     "jev_counter": j.calls - c0,
                     "calls": [{k: v for k, v in c.items() if k not in ("t0", "t1")}
                               | {"start_ms": round((c["t0"] - t0) * 1000, 1)}
                               for c in calls]})
        if rec.record_replay:
            rec.replay[f"{rec.turn_key}|did"] = [res.get("did")]
        if res.get("did") not in ("ignored", "waiting"):
            recent_log.append(f"{text} -> {res.get('did')}")
        print(f"  r{repeat}{arm:>2s} {case:16s} t{ti} {wall:7.0f}ms  "
              f"U={st['understand']:6.0f} J={st['jev_extra']:6.0f} "
              f"G={st['gemini']:6.0f} O={max(0.0, wall - busy):6.0f}  "
              f"calls={rows[-1]['jev_calls']}j/{rows[-1]['gemini_calls']}g  "
              f"-> {res.get('did')}" + (f"  !! {err}" if err else ""), flush=True)
    return rows


def run_once(tm, j, rec: Recorder, repeat: int, only: list[str],
             before_turn=None) -> list[dict]:
    rows = []
    for case, turns in QUESTIONS:
        if only and not any(o in case for o in only):
            continue
        rows += run_case(tm, j, rec, repeat, case, turns, before_turn=before_turn)
    return rows


def field_idle(tm, j):
    """--idle: every turn comes after a quiet minute, as in the field. The far end has
    closed the kept-alive connections (simulated by shutting their sockets), then the
    microphone opens: whatever the code under test does on /api/duck runs (refresh(),
    where it exists) before the turn is timed. Works on code with or without a pool."""
    import socket
    llm = tm.router.LLM_CLIENT

    def before():
        conns = [c for c, _ in getattr(j, "_idle", [])] + [getattr(j, "_conn", None),
                                                           getattr(llm, "_conn", None)]
        for conn in conns:
            if conn is not None and conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if hasattr(j, "_idle"):
            j._idle = [(c, time.time() - 120) for c, _ in j._idle]
        for c in (j, llm):
            if hasattr(c, "_used_at"):
                c._used_at = time.time() - 120
        ts = [threading.Thread(target=c.refresh) for c in (j, llm) if hasattr(c, "refresh")]
        [t.start() for t in ts]
        [t.join() for t in ts]
    return before


# ---------------------------------------------------------------- A/B toggles
# A change is measured against the code without it IN THE SAME PROCESS, case by case,
# alternating which arm goes first. Jev's own latency drifts by tens of ms between
# runs minutes apart, which is larger than some of the wins being measured.
class Toggle:
    """set(on) switches the code under test; before_turn(on) runs untimed before each
    turn, which is where a toggle simulates what happens while she is speaking."""
    def __init__(self, tm, j):
        self.tm, self.j = tm, j

    def set(self, on: bool) -> None:
        pass

    def before_turn(self, on: bool) -> None:
        pass


class GeminiKeepAlive(Toggle):
    """A: a new TLS connection for every Gemini call, which is what the urllib path did."""
    def __init__(self, tm, j):
        super().__init__(tm, j)
        from savta.llm import LLM
        self.LLM, self.kept = LLM, LLM._post_kept

    def set(self, on: bool) -> None:
        kept = self.kept

        def fresh(llm, *a, **k):
            with llm._lock:
                if llm._conn is not None:
                    llm._conn.close()
                llm._conn = None
            return kept(llm, *a, **k)
        self.LLM._post_kept = kept if on else fresh


class IdleWarm(Toggle):
    """Every turn arrives after she has been quiet for a while (a minute or more is
    normal), so the far end has closed the kept-alive connections: simulated by
    shutting the sockets. A: nothing more, as before. B: what the microphone opening
    now triggers (server.py _warm_while_she_speaks), done while she is speaking."""
    def before_turn(self, on: bool) -> None:
        import socket
        llm = self.tm.router.LLM_CLIENT
        conns = [c for c, _ in getattr(self.j, "_idle", [])] + [llm._conn]
        for conn in conns:
            if conn is not None and conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.j._idle = [(c, time.time() - 120) for c, _ in self.j._idle]
        llm._used_at = time.time() - 120
        if on:
            ts = [threading.Thread(target=c.refresh) for c in (self.j, llm)]
            [t.start() for t in ts]
            [t.join() for t in ts]


class FoldPlaces(Toggle):
    """A: the place for the time or the weather is its own pick_span round trip.
    B: it rides in understand() (router.FOLDED_SPANS)."""
    def __init__(self, tm, j):
        super().__init__(tm, j)
        self.folded = tm.router.FOLDED_SPANS

    def set(self, on: bool) -> None:
        self.tm.router.FOLDED_SPANS = self.folded if on else ()


class ParallelBody(Toggle):
    """A: a message's words are asked after its recipient check, one after the other.
    B: both at once (router._in_parallel), each on a warm pooled connection."""
    def __init__(self, tm, j):
        super().__init__(tm, j)
        self.real = tm.router._in_parallel

    def set(self, on: bool) -> None:
        def serial(fn, *args):
            value = fn(*args)
            return lambda: value
        self.tm.router._in_parallel = self.real if on else serial


class FoldSubject(Toggle):
    """A: what she wants played is its own pick_span round trip. B: it rides in
    understand() with its sharper name check (router.SPAN_EXISTS)."""
    def __init__(self, tm, j):
        super().__init__(tm, j)
        self.folded = tm.router.FOLDED_SPANS

    def set(self, on: bool) -> None:
        self.tm.router.FOLDED_SPANS = (self.folded if on else
                                       tuple(k for k in self.folded if k != "subject"))


TOGGLES = {"gemini_keepalive": GeminiKeepAlive, "idle_warm": IdleWarm,
           "fold_places": FoldPlaces, "parallel_body": ParallelBody,
           "fold_subject": FoldSubject}


def run_ab(tm, j, rec: Recorder, toggle: Toggle, repeats: int, only: list[str]) -> list[dict]:
    rows = []
    for r in range(repeats):
        for ci, (case, turns) in enumerate(QUESTIONS):
            if only and not any(o in case for o in only):
                continue
            order = (False, True) if (r + ci) % 2 == 0 else (True, False)
            for on in order:
                toggle.set(on)
                rows += run_case(tm, j, rec, r, case, turns, arm="B" if on else "A",
                                 before_turn=lambda on=on: toggle.before_turn(on))
    toggle.set(True)
    return rows


def paired(rows: list[dict], key: str = "wall_ms") -> dict:
    a = {(r["case"], r["turn"], r["repeat"]): r[key] for r in rows if r["arm"] == "A"}
    d = [r[key] - a[(r["case"], r["turn"], r["repeat"])] for r in rows
         if r["arm"] == "B" and (r["case"], r["turn"], r["repeat"]) in a]
    return {"n": len(d), "median": round(q(d, .5)), "p25": round(q(d, .25)),
            "p75": round(q(d, .75))}


def summarize(rows: list[dict]) -> dict:
    out = {}
    for k in ("wall_ms", "understand", "jev_extra", "gemini", "other_ms"):
        xs = [r[k] for r in rows]
        out[k] = {"p50": round(q(xs, .5)), "p95": round(q(xs, .95)), "mean": round(statistics.mean(xs))}
    reps = sorted({r["repeat"] for r in rows})
    per = [round(q([r["wall_ms"] for r in rows if r["repeat"] == k], .5)) for k in reps]
    per95 = [round(q([r["wall_ms"] for r in rows if r["repeat"] == k], .95)) for k in reps]
    out["wall_p50_per_repeat"] = per
    out["wall_p95_per_repeat"] = per95
    out["n_turns"] = len(rows)
    out["jev_calls"] = sum(r["jev_calls"] for r in rows)
    out["gemini_calls"] = sum(r["gemini_calls"] for r in rows)
    # Where the time goes, all turns together.
    tot = sum(r["wall_ms"] for r in rows) or 1
    out["share"] = {k: round(100 * sum(r[k] for r in rows) / tot, 1)
                    for k in ("understand", "jev_extra", "gemini", "other_ms")}
    by_caller: dict[str, list[float]] = {}
    for r in rows:
        for c in r["calls"]:
            by_caller.setdefault(f"{c['kind']}:{c['caller']}", []).append(c["ms"])
    out["by_caller"] = {k: {"n": len(v), "p50": round(q(v, .5)), "p95": round(q(v, .95)),
                            "sum_s": round(sum(v) / 1000, 1)}
                        for k, v in sorted(by_caller.items(), key=lambda kv: -sum(kv[1]))}
    return out


def print_summary(s: dict, label: str) -> None:
    print(f"\n== {label}: {s['n_turns']} turns ==")
    print(f"{'stage':12s} {'p50':>7s} {'p95':>7s} {'mean':>7s}")
    for k in ("wall_ms", "understand", "jev_extra", "gemini", "other_ms"):
        v = s[k]
        print(f"{k:12s} {v['p50']:7d} {v['p95']:7d} {v['mean']:7d}")
    print(f"wall p50 per repeat: {s['wall_p50_per_repeat']}   p95 per repeat: {s['wall_p95_per_repeat']}")
    print(f"share of wall: {s['share']}")
    print("by caller:")
    for k, v in s["by_caller"].items():
        print(f"  {k:32s} n={v['n']:4d} p50={v['p50']:6d} p95={v['p95']:6d} total={v['sum_s']:6.1f}s")


# ---------------------------------------------------------------- probe
def probe(n: int = 5) -> None:
    """What one round trip costs, stripped of everything else."""
    import http.client
    import socket
    import ssl
    from urllib.parse import urlsplit
    from savta.actions import facts as _facts, youtube as _yt
    # Bound now: load_wall() replaces these module attributes with fixtures.
    search, cond, wiki = _yt.search, _facts.conditions, _facts.wiki
    real = {"youtube search": lambda: search("Umm Kulthum"),
            "wttr conditions": lambda: cond("Haifa"),
            "wikipedia summary": lambda: wiki("David Ben-Gurion", "en")}
    tm = load_wall()
    from savta.jev import Jev
    from savta import jev as jmod, account
    j = Jev()
    print(f"jev mode={j.mode} host={j._host}")

    def tiny():
        return j.ask({"utterance": "hello"}, {"x": {"type": "noul", "instructions": "She said hello"}})

    t = time.time(); j.warmup(); print(f"jev connect (DNS+TCP+TLS): {1000*(time.time()-t):.0f}ms")
    xs = []
    for _ in range(n):
        t = time.time(); tiny(); xs.append(1000 * (time.time() - t))
    print(f"jev tiny ask, warm conn: {[round(x) for x in xs]}  p50={q(xs,.5):.0f}")
    xs = []
    for _ in range(n):
        j._conn.close(); j._conn = None
        t = time.time(); tiny(); xs.append(1000 * (time.time() - t))
    print(f"jev tiny ask, NEW conn each: {[round(x) for x in xs]}  p50={q(xs,.5):.0f}")
    contacts = tm.router.get_contacts("hello")
    xs = []
    from savta import brain
    for _ in range(n):
        t = time.time(); brain.understand(j, "what time is it", contacts); xs.append(1000 * (time.time() - t))
    print(f"jev understand (33 questions): {[round(x) for x in xs]}  p50={q(xs,.5):.0f}")
    xs = []
    for _ in range(n):
        t = time.time()
        brain.pick_span(j, "send Miriam a message that I will be late", "Which words are the message?")
        xs.append(1000 * (time.time() - t))
    print(f"jev pick_span (2 questions, ~45 options): {[round(x) for x in xs]}  p50={q(xs,.5):.0f}")
    # Two tiny asks at once, on two clients: does Jev serve them in parallel?
    j2 = Jev(); j2.warmup()
    xs = []
    for _ in range(n):
        t = time.time()
        th = threading.Thread(target=lambda: j2.ask({"utterance": "hi"}, {"x": {"type": "noul", "instructions": "She said hi"}}))
        th.start(); tiny(); th.join()
        xs.append(1000 * (time.time() - t))
    print(f"jev two tiny asks in parallel (2 conns): {[round(x) for x in xs]}  p50={q(xs,.5):.0f}")
    llm = tm.router.LLM_CLIENT
    print(f"gemini mode={llm.mode} available={llm.available}")
    if llm.available:
        xs = []
        for _ in range(n):
            t = time.time(); llm.text("Say OK.", max_tokens=5, temperature=0); xs.append(1000 * (time.time() - t))
        print(f"gemini tiny text: {[round(x) for x in xs]}  p50={q(xs,.5):.0f}")
    url = os.environ.get("MICMIC_PROBE_PROXY", account.DEFAULT_CLOUD_URL)
    u = urlsplit(url)
    xs, ys = [], []
    for _ in range(n):
        t = time.time()
        c = http.client.HTTPSConnection(u.hostname, 443, timeout=10)
        c.connect(); t1 = time.time()
        c.request("GET", "/healthz"); c.getresponse().read(); t2 = time.time()
        c.request("GET", "/healthz"); c.getresponse().read(); t3 = time.time()
        c.close()
        xs.append(1000 * (t1 - t)); ys.append(1000 * (t3 - t2))
    print(f"proxy {u.hostname}: connect {[round(x) for x in xs]} p50={q(xs,.5):.0f}; "
          f"warm GET /healthz {[round(y) for y in ys]} p50={q(ys,.5):.0f}")
    # The public services a turn reads in production. The bench stubs them; this is
    # what they cost for real (read-only GETs, nothing about her in them).
    for name, fn in real.items():
        xs = []
        for _ in range(3):
            t = time.time(); fn(); xs.append(1000 * (time.time() - t))
        print(f"{name}: {[round(x) for x in xs]}  p50={q(xs,.5):.0f}")
    print(f"cost: jev ${j.cost_usd + j2.cost_usd:.5f}")


# ---------------------------------------------------------------- listener log
def listener(path: str) -> None:
    """release -> dispatch and dispatch -> reply, from the Mac app's own log."""
    import datetime as dt
    import re
    stamp = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)  (.*)$")
    timing = re.compile(r"timing: release->dispatch (-?[\d.]+)s dispatch->reply ([\d.]+)s")
    fin = disp = None
    rel, rep = [], []
    exact_rel, exact_rep = [], []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = stamp.match(line)
        if not m:
            continue
        s, msg = m.groups()
        t = dt.datetime.fromisoformat(s)
        mt = timing.match(msg)
        if mt:
            # Written by the listener itself since it logs milliseconds: exact.
            if float(mt.group(1)) >= 0:
                exact_rel.append(float(mt.group(1)))
            exact_rep.append(float(mt.group(2)))
            continue
        if msg.startswith("turn finishing"):
            fin, disp = t, None
        elif msg.startswith("→ ") and "(turn" in msg:
            disp = t
            if fin:
                rel.append((t - fin).total_seconds())
            fin = None
        elif msg.startswith("← ") and disp:
            rep.append((t - disp).total_seconds())
            disp = None
    if exact_rep:
        rel, rep = exact_rel, exact_rep
        print("from the listener's own timing lines (ms resolution)")
    else:
        print("from log timestamps (resolution of the log's clock)")
    for name, xs in (("release -> dispatch", rel), ("dispatch -> reply", rep)):
        if xs:
            print(f"{name:22s} n={len(xs):3d} p50={q(xs,.5):.2f}s p95={q(xs,.95):.2f}s "
                  f"max={max(xs):.2f}s")
        else:
            print(f"{name:22s} no data")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--label", default="run")
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--listener", default="")
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--idle", action="store_true",
                    help="every turn after a quiet minute: connections closed first")
    ap.add_argument("--ab", default="", choices=[""] + sorted(TOGGLES),
                    help="A (without) against B (with) in one process, interleaved")
    a = ap.parse_args()
    if a.listener:
        listener(a.listener)
        return 0
    if a.probe:
        probe()
        return 0

    tm = load_wall()
    from savta.jev import Jev, MODEL
    from savta.llm import LLM
    rec = Recorder(record_replay=not a.no_record and not a.only and not a.ab)
    rec.install(Jev, LLM)
    j = Jev()
    j.warmup()
    llm = tm.router.LLM_CLIENT
    cfg = {"commit": git_head(), "jev_mode": j.mode, "jev_model": MODEL,
           "gemini_mode": llm.mode, "gemini_model": llm.model,
           "host": os.uname().nodename.split(".")[0], "python": sys.version.split()[0],
           "when": time.strftime("%Y-%m-%d %H:%M:%S"), "repeats": a.repeats,
           "cases": len(QUESTIONS), "turns": sum(len(t) for _, t in QUESTIONS),
           "env": {k: os.environ[k] for k in sorted(os.environ) if k.startswith("MICMIC_PERF")}}
    print(json.dumps(cfg))
    if llm.available:
        llm.text("Say OK.", max_tokens=5, temperature=0)   # the app's first turn is warm too
        rec.take()
    all_rows = []
    cost0 = j.cost_usd
    if a.ab:
        cfg["ab"] = a.ab
        all_rows = run_ab(tm, j, rec, TOGGLES[a.ab](tm, j), a.repeats, a.only)
    else:
        cfg["idle"] = a.idle
        for r in range(a.repeats):
            rec.record_replay = rec.record_replay and r == 0 and not a.idle
            all_rows += run_once(tm, j, rec, r, a.only,
                                 before_turn=field_idle(tm, j) if a.idle else None)
    s = summarize(all_rows) if not a.ab else {
        "A": summarize([r for r in all_rows if r["arm"] == "A"]),
        "B": summarize([r for r in all_rows if r["arm"] == "B"]),
        "paired_B_minus_A": {k: paired(all_rows, k) for k in
                             ("wall_ms", "understand", "jev_extra", "gemini", "other_ms")}}
    gem_in = sum(c.get("in_tok", 0) for r in all_rows for c in r["calls"])
    gem_out = sum(c.get("out_tok", 0) for r in all_rows for c in r["calls"])
    # Gemini Flash-Lite list price, an estimate: $0.10 per M in, $0.40 per M out.
    s["cost"] = {"jev_usd": round(j.cost_usd - cost0, 5),
                 "gemini_tokens_in": gem_in, "gemini_tokens_out": gem_out,
                 "gemini_usd_est": round(gem_in * 0.10e-6 + gem_out * 0.40e-6, 5)}
    if a.ab:
        print_summary(s["A"], f"{a.label} A (without)")
        print_summary(s["B"], f"{a.label} B (with)")
        print("\npaired B - A, per turn (ms):")
        for k, v in s["paired_B_minus_A"].items():
            print(f"  {k:12s} n={v['n']:3d} median={v['median']:6d}  IQR {v['p25']} .. {v['p75']}")
    else:
        print_summary(s, a.label)
    print(f"cost: {s['cost']}")
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}_{a.label}.json"
    out.write_text(json.dumps({"config": cfg, "summary": s, "rows": all_rows},
                              ensure_ascii=False, indent=1))
    print(f"wrote {out.relative_to(ROOT)}")
    if rec.replay:
        REPLAY.write_text(json.dumps({"config": cfg, "answers": rec.replay},
                                     ensure_ascii=False, indent=0))
        print(f"wrote {REPLAY.relative_to(ROOT)} ({len(rec.replay)} keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
