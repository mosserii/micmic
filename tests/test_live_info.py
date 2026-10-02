#!/usr/bin/env python3
"""Live info, offline: scores, stock prices, the weather over a range of days, and the
Hebrew stammer read as half a sentence (MicMic Bench v1, themes 3i and 3k).

    cd <checkout> && uv run python tests/test_live_info.py

The older live-answer checks (headlines, coins, rates) are in tests/test_micmic.py,
which replays recorded Jev answers; these are new and scripted. Nothing leaves the
machine: the stub wall is tests/test_micmic.py, loaded the way tests/perf/bench.py
loads it (its main() never runs); live sources and the weather are fixtures; Jev and
Gemini are scripted. Nothing reaches 127.0.0.1:8799.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["MICMIC_STATE_DIR"] = tempfile.mkdtemp(prefix="micmic-live-state-")
for _k in ("MICMIC_ALLOW_SEND", "MICMIC_ALLOW_CALL"):
    os.environ.pop(_k, None)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "perf"))
import bench                                   # noqa: E402

tm = bench.load_wall()
router, mac = tm.router, tm.mac
from savta.actions import live                 # noqa: E402
from savta import brain                        # noqa: E402

PASSED = 0
FAILED: list[tuple[str, str]] = []


def check(name: str, ok, detail: str = "") -> bool:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  pass  {name}")
    else:
        FAILED.append((name, str(detail)))
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return bool(ok)


# ---------------------------------------------------------------- fakes
class FakeLLM:
    available = True

    def __init__(self):
        self.calls, self.busy_ms, self.last_ms, self.last_error = 0, 0.0, 0.0, None
        self.reported: list[dict] = []

    def report(self, question, items, language="english", gender=""):
        self.calls += 1
        self.reported.append({"question": question, "items": items})
        return " ".join(f"{i['source']} reports: {i['title']}." for i in items)

    def answer(self, *a, **k):
        self.calls += 1
        return "[answer]"

    def chat(self, *a, **k):
        self.calls += 1
        return "[chat]"

    def split_steps(self, *a, **k):
        return None

    def generate_content(self, *a, **k):
        self.calls += 1
        return {"candidates": [{"content": {"parts": [{"text": "[model]"}]}}]}


QUIET = ("not_applicable", "unspecified", "none", "nobody", "__none__", "any", "today",
         "unrevealed", "unchanged", "other", "not_live")


class ScriptedJev:
    mode = "replay"

    def __init__(self, say: dict, picks=None):
        self.calls, self.cost_usd, self.busy_ms = 0, 0.0, 0.0
        self.asked: list[dict] = []
        self.states: list[dict] = []
        self.say = say
        self.picks = picks or (lambda instr, opts: "__none__")

    def ask(self, state, qs):
        self.calls += 1
        self.asked.append(qs)
        self.states.append(state)
        out = {}
        for k, q in qs.items():
            t = q.get("type")
            if t == "choice":
                keys = list(q["criteria"])
                sel = next((c for c in QUIET if c in keys), keys[0])
                out[k] = {"choice": sel, "confidence": 0.9,
                          "probabilities": {c: (0.9 if c == sel else 0.1 / max(1, len(keys) - 1))
                                            for c in keys}}
            elif t == "score":
                out[k] = {"score": 0.0}
            else:
                out[k] = {"noul": 0.95 if k == "is_complete" else 0.0}
        if "pick" in qs:
            sel = self.picks(qs["pick"]["instructions"], qs["pick"]["criteria"])
            out["pick"] = {"choice": sel, "confidence": 0.9, "probabilities": {sel: 0.9}}
            out["any_good"] = {"noul": 0.9 if sel != "__none__" else 0.1}
        for k, v in self.say.items():
            if k in qs:
                out[k] = (v if isinstance(v, dict)
                          else {"choice": v, "confidence": 0.9, "probabilities": {v: 0.9}}
                          if isinstance(v, str) else {"noul": v})
        return out

    def warmup(self, *a, **k):
        pass


def _rss(items: list[tuple[str, str, float]]) -> bytes:
    now = time.time()
    body = "".join(
        f"<item><title>{t} - {s}</title><source>{s}</source>"
        f"<pubDate>{email.utils.formatdate(now - h * 3600)}</pubDate></item>"
        for t, s, h in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


URLS: list[str] = []
FEEDS: dict[str, bytes] = {}


def fake_fetch(url: str, timeout: float) -> bytes:
    URLS.append(url)
    for part, body in FEEDS.items():
        if part in url:
            return body
    raise OSError(f"no fixture for {url}")


live._fetch = fake_fetch
LLM = FakeLLM()


def fresh():
    tm.reset_state()
    URLS.clear()
    FEEDS.clear()
    live._CACHE.clear()
    router.LLM_CLIENT = LLM
    LLM.reported.clear()


def ask(utterance: str, say: dict, picks=None, lang: str | None = None):
    j = ScriptedJev({"intent": "look_up", "needs_knowledge": 0.9, **say,
                     **({"language": lang} if lang else {})}, picks)
    return router.handle(j, utterance, speak=False, client="native", activation="push"), j


def by_word(word: str):
    """A pick that points at the first option holding `word`, else none."""
    def fn(instr, opts):
        return next((k for k, v in opts.items() if k != "__none__" and word.lower() in v.lower()),
                    "__none__")
    return fn


# ================================================================== scores
def t_scores():
    """Bench v1 hard-live-002: "whats the score of the last lakers game" -> "I did not
    find a fresh result for lakers" (3/3); live-012 Real Madrid 1/3. No headline in the
    result search reports a game: the news about the team is read instead."""
    print("\nt_scores")
    fresh()
    FEEDS["%28beat"] = _rss([("Lakers preseason: what to watch", "ESPN", 30),
                             ("NBA power rankings week 1", "CBS", 20)])
    FEEDS["q=lakers+when"] = _rss([("LeBron James sits out Lakers practice with sore foot",
                                    "ESPN", 6),
                                   ("Lakers sign guard to two-way deal", "Lakers.com", 28)])
    sport = {"live_kind": "sport_result", "span_term": "lakers", "span_term_exists": 0.95}

    def picks(instr, opts):
        if "result of the most recent game" in instr:
            return "__none__"                   # no headline reports a game
        return by_word("LeBron")(instr, opts)
    r, j = ask("whats the score of the last lakers game", sport, picks)
    check("no result found: the latest news about the team is answered instead",
          r["did"] == "answered" and "LeBron" in (r["say"] or ""), f"{r['did']} {r['say']}")
    check("  and it says there was no result first",
          (r["say"] or "").lower().startswith("i did not find a result of a recent game"),
          r["say"])
    check("  matched to the team: the team's own news search, without the result words",
          any("q=lakers+when" in u for u in URLS), URLS)
    check("  one headline, one sentence from it", len(LLM.reported) == 1
          and len(LLM.reported[0]["items"]) == 1, LLM.reported)
    check("  the detail says it fell back to the news", r["detail"].get("fallback") == "news",
          r["detail"])

    fresh()
    FEEDS["%28beat"] = _rss([("Lakers preseason: what to watch", "ESPN", 30)])
    FEEDS["q=lakers+when"] = _rss([("Lakers preseason: what to watch", "ESPN", 30)])
    r, j = ask("whats the score of the last lakers game", sport, lambda i, o: "__none__")
    check("nothing about the team either: told so, nothing made up",
          r["did"] == "live_not_found" and not LLM.reported, f"{r['did']} {r['say']}")

    fresh()
    FEEDS["%28beat"] = _rss([("Real Madrid beat Villarreal 2-1 at the Bernabeu", "AS", 10)])
    r, j = ask("did real madrid win last night",
               {"live_kind": "sport_result", "span_term": "real madrid",
                "span_term_exists": 0.95}, by_word("Villarreal"))
    check("a result found is answered as before, without the news fallback",
          r["did"] == "answered" and "2-1" in r["say"] and "fallback" not in r["detail"]
          and j.calls == 2, f"{r['did']} {r['say']} calls={j.calls}")


# ================================================================== stocks
def _chart(symbol: str, name: str, closes: list[float], currency: str = "USD") -> bytes:
    now = int(time.time())
    ts = [now - (len(closes) - 1 - i) * 86400 for i in range(len(closes))]
    return json.dumps({"chart": {"result": [{
        "meta": {"currency": currency, "symbol": symbol, "longName": name, "shortName": name,
                 "regularMarketPrice": closes[-1], "regularMarketTime": now,
                 "exchangeTimezoneName": "America/New_York"},
        "timestamp": ts, "indicators": {"quote": [{"close": closes}]}}]}}).encode()


def _search(rows: list[tuple[str, str, str]]) -> bytes:
    return json.dumps({"quotes": [{"symbol": s, "shortname": n, "longname": n, "quoteType": t,
                                   "exchange": "NMS"} for s, n, t in rows]}).encode()


def t_stocks():
    """Bench v1 hard-live-003: "hows apple stock doing today, and how does that compare
    to a year ago" -> "I could not find a current price for that". Coins and exchange
    rates only; a stock now has a keyless source (Yahoo Finance's public chart)."""
    print("\nt_stocks")
    fresh()
    FEEDS["finance/search"] = _search([("AAPL", "Apple Inc.", "EQUITY"),
                                       ("SAAPL=F", "Apple Inc Stock Futures", "FUTURE")])
    closes = [250.0] + [300.0] * 250 + [333.0, 330.0]
    FEEDS["chart/AAPL"] = _chart("AAPL", "Apple Inc.", closes)
    price = {"live_kind": "price", "span_term": "apple stock", "span_term_exists": 0.95}
    r, j = ask("hows apple stock doing today", price)
    say = r["say"] or ""
    check("a stock price is answered from the live source", r["did"] == "answered"
          and "Apple" in say and "330" in say and "Yahoo Finance" in say, f"{r['did']} {say}")
    check("  with today's change", "down 0.9%" in say, say)
    check("  no pick among coins and rates: one Jev call (understanding)", j.calls == 1, j.calls)
    check("  never a coin or rate fetch", not any("coingecko" in u or "frankfurter" in u
                                                  for u in URLS), URLS)
    r, j = ask("how does apple stock compare to a year ago", price)
    say = r["say"] or ""
    check("a year ago: the price then and the change since", "250" in say and "32%" in say,
          say)
    check("no em dash in any stock line",
          not any("—" in v for t in router._STOCK_LINES.values() for v in t.values()))
    for lang, said in (("hebrew", "מה עם מניית אפל היום"), ("arabic", "كيف سهم أبل اليوم"),
                       ("russian", "как сегодня акции apple")):
        r, _ = ask(said, {**price, "language": lang})
        check(f"{lang}: the stock line is in {lang}",
              r["did"] == "answered" and r["lang"] == lang and "Apple" in (r["say"] or ""),
              r["say"])

    fresh()
    FEEDS["finance/search"] = _search([])
    r, j = ask("how is zorblax stock doing", {"live_kind": "price", "span_term": "zorblax stock",
                                              "span_term_exists": 0.95})
    check("an unknown company: said honestly, nothing invented",
          r["did"] == "live_not_found" and "zorblax" in (r["say"] or "").lower(),
          f"{r['did']} {r['say']}")

    fresh()
    FEEDS.update({"coingecko": json.dumps([{"name": "Bitcoin", "symbol": "btc",
                                            "current_price": 61000.0}]).encode(),
                  "frankfurter": json.dumps({"date": "2026-10-01", "rates": {"ILS": 3.7}}).encode()})
    r, j = ask("how much is bitcoin right now", {"live_kind": "price", "span_term": "bitcoin",
                                                 "span_term_exists": 0.95}, by_word("Bitcoin"))
    check("a coin is still the coin path, with its pick",
          r["did"] == "answered" and "Bitcoin" in r["say"] and j.calls == 2
          and not any("yahoo" in u for u in URLS), f"{r['did']} {r['say']} {URLS}")
    check("the stock words pre-check", router._about_stock("how is apple stock doing")
          and router._about_stock("מה עם מניית אפל") and router._about_stock("как акции apple")
          and router._about_stock("كيف سهم أبل") and not router._about_stock("how much is bitcoin"))


# ================================================================== weather range
def _days(start: dt.date) -> list[dict]:
    return [{"date": (start + dt.timedelta(days=i)).isoformat(), "code": 113,
             "low": 20 + i, "high": 28 + i, "rain_pct": 0 if i != 2 else 80} for i in range(3)]


def t_weather_range():
    """Bench v1 live-016: "is it gonna be hot this weekend in tel aviv" -> "I only have
    the forecast for today, tomorrow and the day after", on a Friday (Saturday and
    Sunday were both in the forecast). The days are worked out in code."""
    print("\nt_weather_range")
    today = dt.date.today()
    cond = {"where": "Tel Aviv", "desc": "Sunny", "code": 113, "temp": 27, "feels": 27,
            "low": 20, "high": 28, "rain_pct": 0, "days": _days(today)}
    real = router._weather_lookup
    router._weather_lookup = lambda j, named, home, utterance="": (json.loads(json.dumps(cond)),
                                                                 "Tel Aviv", [named], "")
    try:
        in2 = today + dt.timedelta(days=2)
        name2 = in2.strftime("%A")
        fresh()
        r, j = ask(f"what's the weather on {name2} in tel aviv",
                   {"about_weather": 0.95, "weather_day": "later",
                    "span_weather_place": "tel aviv", "span_weather_place_exists": 0.95})
        check("a weekday in the forecast is answered for that day",
              r["did"] == "answered" and name2 in (r["say"] or "") and "22" in r["say"]
              and "30" in r["say"], f"{r['did']} {r['say']}")
        check("  rain that day: take an umbrella", "umbrella" in r["say"].lower(), r["say"])

        far = today + dt.timedelta(days=5)
        fresh()
        r, j = ask(f"what's the weather on {far.strftime('%A')}",
                   {"about_weather": 0.95, "weather_day": "later"})
        check("a weekday past the forecast still says so",
              r["did"] == "weather_too_far", f"{r['did']} {r['say']}")

        check("weekend days, English: Saturday and Sunday",
              router._asked_days("this weekend", "english", dt.date(2026, 10, 2))
              == [dt.date(2026, 10, 3), dt.date(2026, 10, 4)])
        check("weekend days, Hebrew: Friday and Saturday",
              router._asked_days("מה מזג האוויר בסוף השבוע", "hebrew", dt.date(2026, 10, 1))
              == [dt.date(2026, 10, 2), dt.date(2026, 10, 3)])
        check("on the weekend itself, only what is left of it",
              router._asked_days("this weekend", "english", dt.date(2026, 10, 4))
              == [dt.date(2026, 10, 4)])
        check("a named day in each language",
              router._asked_days("on saturday", "english", dt.date(2026, 10, 2)) == [dt.date(2026, 10, 3)]
              and router._asked_days("מה מזג האוויר בשבת", "hebrew", dt.date(2026, 10, 2)) == [dt.date(2026, 10, 3)]
              and router._asked_days("ما الطقس يوم السبت", "arabic", dt.date(2026, 10, 2)) == [dt.date(2026, 10, 3)]
              and router._asked_days("какая погода в субботу", "russian", dt.date(2026, 10, 2)) == [dt.date(2026, 10, 3)])
        check("no day words: nothing", router._asked_days("what's the weather", "english",
                                                          today) == [])

        # The bench case, with the weekend inside the forecast whatever today is.
        sat = next(today + dt.timedelta(days=i) for i in range(7)
                   if (today + dt.timedelta(days=i)).weekday() == 5)
        cond["days"] = _days(sat - dt.timedelta(days=1))
        real_today = router._today
        router._today = lambda: sat - dt.timedelta(days=1)
        try:
            fresh()
            r, j = ask("is it gonna be hot this weekend in tel aviv",
                       {"about_weather": 0.95, "weather_day": "later",
                        "span_weather_place": "tel aviv", "span_weather_place_exists": 0.95})
            say = r["say"] or ""
            check("this weekend: Saturday and Sunday, from the forecast",
                  r["did"] == "answered" and "Saturday" in say and "Sunday" in say
                  and "Tel Aviv" in say, f"{r['did']} {say}")
            check("  no extra Jev call (code worked out the days)", j.calls == 1, j.calls)
            fresh()
            r, _ = ask("מה מזג האוויר בשבת בתל אביב",
                       {"about_weather": 0.95, "weather_day": "later", "language": "hebrew",
                        "span_weather_place": "תל אביב", "span_weather_place_exists": 0.95},
                       lang="hebrew")
            check("he: Saturday in Hebrew", r["did"] == "answered" and "בשבת" in (r["say"] or ""),
                  f"{r['did']} {r['say']}")
        finally:
            router._today = real_today

        # "do i need an umbrella": a yes or a no.
        cond["days"] = _days(today)
        fresh()
        r, _ = ask("do i need an umbrella today", {"about_weather": 0.95})
        check("umbrella, dry day: says no umbrella is needed",
              r["did"] == "answered" and "no need for an umbrella" in (r["say"] or "").lower(),
              r["say"])
        fresh()
        r, _ = ask("what's the weather", {"about_weather": 0.95})
        check("no umbrella asked: the line as it was", "umbrella" not in (r["say"] or "").lower(),
              r["say"])
    finally:
        router._weather_lookup = real


# ================================================================== Hebrew stammer
def t_stammer():
    """Bench v1 msg-019: "אממ תשלחי לדנה ש... שאני בפקק" -> "I only caught part of that"
    (3/3). A repeated or cut-off start is tidied in code before understand()."""
    print("\nt_stammer")
    st = brain.steady
    for said, want in (
            ("אממ תשלחי לדנה ש... שאני בפקק", "תשלחי לדנה שאני בפקק"),
            ("תש... תשלחי לדנה שאני בדרך", "תשלחי לדנה שאני בדרך"),
            ("תש תשלחי לדנה שאני בדרך", "תשלחי לדנה שאני בדרך"),
            ("תשלחי תשלחי לדנה שאני בדרך", "תשלחי לדנה שאני בדרך"),
            ("um text dana that im la- late", "text dana that im late"),
            ("uh, call call dana", "call dana"),
            ("אה... מה השעה", "מה השעה"),
            ("שש... שלחי לגל", "שלחי לגל")):
        check(f"steady({said!r})", st(said) == want, st(said))
    for same in ("text dana that that is fine", "what's the weather", "מה השעה",
                 "play bad bunny", "ל לא", "שלחי לדנה ש 5", "I I", "tell her ok ok", "cough cough", "bye bye", "no no",
                 "come come here", "לא לא"):
        check(f"left alone: {same!r}", st(same) == same, st(same))

    fresh()
    seen: list[str] = []
    real = router.understand

    def spy(j, utterance, *a, **k):
        seen.append(utterance)
        return real(j, utterance, *a, **k)
    router.understand = spy
    try:
        j = ScriptedJev({"intent": "chitchat"})
        router.handle(j, "אממ תשלחי לדנה ש... שאני בפקק", speak=False, client="native",
                      activation="push")
        check("understand() is asked about the tidied sentence",
              seen and seen[0] == "תשלחי לדנה שאני בפקק", seen)
        seen.clear()
        router.handle(ScriptedJev({"intent": "chitchat"}), "what's the weather", speak=False,
                      client="native", activation="push")
        check("a sentence with no stammer goes out as it was", seen == ["what's the weather"],
              seen)
    finally:
        router.understand = real


def t_remaining():
    """Bench v1 at 12405eb. hard-multi-006: "who scored the most points" right after
    the last Lakers game was asked "which team?". hard-live-003: the share price and
    the year comparison were split in two, and the price was said twice."""
    print("\nt_remaining")
    fresh()
    FEEDS["%28beat"] = _rss([("Lakers beat Suns 112-104, LeBron scores 31", "ESPN", 10)])
    FEEDS["q=lakers+when"] = _rss([("Lakers beat Suns 112-104, LeBron scores 31", "ESPN", 10)])
    sport = {"live_kind": "sport_result", "span_term": "lakers", "span_term_exists": 0.95}
    r, _ = ask("whats the score of the last lakers game", sport, by_word("Suns"))
    check("the Lakers game is answered", r["did"] == "answered", f"{r['did']} {r['say']}")
    URLS.clear()
    r, _ = ask("who scored the most points", {"live_kind": "sport_result"}, by_word("Suns"))
    check("the follow-up is about the same team, never 'which team?'",
          r["did"] == "answered" and r["detail"].get("topic") == "lakers",
          f"{r['did']} {r['say']} {r['detail']}")
    fresh()
    r, _ = ask("who scored the most points", {"live_kind": "sport_result"}, by_word("Suns"))
    check("with no team asked about before, it still asks which", r["did"] == "need_team",
          r["did"])

    fresh()
    FEEDS["finance/search"] = _search([("AAPL", "Apple Inc.", "EQUITY")])
    FEEDS["chart/AAPL"] = _chart("AAPL", "Apple Inc.", [250.0] + [300.0] * 250 + [333.0, 330.0])
    asked: list = []
    real_split = LLM.split_steps
    LLM.split_steps = lambda u, *a, **k: (asked.append(u), [
        "hows apple stock doing today", "how does apple stock compare to a year ago"])[1]
    try:
        utt = "hows apple stock doing today, and how does that compare to a year ago"
        r, _ = ask(utt, {"live_kind": "price", "span_term": "apple stock",
                         "span_term_exists": 0.95, "is_compound": 0.9})
    finally:
        LLM.split_steps = real_split
    say = r["say"] or ""
    check("price and year in one answer, never split", r["did"] == "answered" and not asked
          and say.count("330") == 1 and "250" in say, f"{r['did']} {asked} {say}")
    check("a line another step already says in full is said once",
          router._once(["Apple is at 330.", "Apple is at 330. A year ago it was 250."],
                       "english") == ["Apple is at 330. A year ago it was 250."])


def main():
    for t in (t_scores, t_stocks, t_weather_range, t_stammer, t_remaining):
        try:
            t()
        except Exception:  # noqa: BLE001
            import traceback
            FAILED.append((t.__name__, traceback.format_exc()))
            print(traceback.format_exc())
    real_osa = [c for c in tm.LAUNCHED if c and c[0] in ("osascript", "screencapture")]
    check("0 osascript or screencapture escapes", not tm.OSA and not real_osa,
          f"{len(tm.OSA)} + {len(real_osa)}")
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for n, d in FAILED:
        print(f"  FAILED: {n}  {str(d)[:300]}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
