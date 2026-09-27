"""Keyless sources of what changed today: headlines, a result, a price.

Code fetches and lists what the source actually says; Jev points at one row; a
sentence is written from that row alone. Nothing here guesses or fills a gap.

Sources, each checked by hand before it was chosen (2026-09-27):
  * Google News RSS, for news and for sports results. Keyless, answers in her own
    language and edition (a Hebrew team name searched in the Hebrew edition finds
    Sport5, ynet and Walla reporting "הפועל ת"א הביסה 67:85 את באר שבע"), and every
    item carries its outlet and publication time. ESPN's public API was tried first
    and dropped: site.api.espn.com answers 403 to anything that is not its own site,
    and the host that does answer (site.web.api.espn.com) stopped listing the Israeli
    Premier League in May 2025, so "the score of Maccabi Tel Aviv" had no answer there.
  * CoinGecko /coins/markets, for cryptocurrency prices. Keyless, one request lists the
    top coins with names, symbols and current prices, so the list is the candidates.
  * Frankfurter (European Central Bank reference rates), for exchange rates. Keyless,
    daily, and names the date the rate is for.

Every fetch is cached for FRESH_S and fails soft: None means the source could not be
reached (say so), [] means it answered with nothing (say that instead)."""
from __future__ import annotations
import email.utils, html, json, threading, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "micmic/0.1"}
FRESH_S = 180.0
TIMEOUT_S = 4.0
# News older than this is not "the latest". Results are reported the same night or the
# morning after; three days covers a weekend.
MAX_AGE_S = 3 * 86400

_CACHE: dict[str, tuple[float, bytes]] = {}
_LOCK = threading.Lock()


def _fetch(url: str, timeout: float) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                  timeout=timeout).read()


def _get(url: str, timeout: float = TIMEOUT_S) -> bytes:
    """The body of url, from the cache when it is fresh. Raises when unreachable; a
    failure is never cached, so the next question tries again."""
    with _LOCK:
        hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < FRESH_S:
        return hit[1]
    raw = _fetch(url, timeout)
    with _LOCK:
        _CACHE[url] = (time.time(), raw)
    return raw


# ---------------------------------------------------------------- headlines
# (hl, gl, ceid) per language. A topic is searched in her own language's edition; the
# general headlines for Hebrew, Arabic and Russian speakers are Israel's, which is where
# MicMic's four-language users live (the Russian and Arabic editions of other
# countries lead with those countries' news).
EDITION = {"hebrew": ("he", "IL", "IL:he"), "arabic": ("ar", "AE", "AE:ar"),
           "russian": ("ru", "RU", "RU:ru"), "english": ("en-US", "US", "US:en")}
HOME_EDITION = {"hebrew": "hebrew", "arabic": "hebrew", "russian": "hebrew",
                "english": "english"}
# Added to a sports search so the newest items are the ones reporting a game, not a
# transfer rumour or a stats page (measured: "tel aviv score" in the English edition
# led with an air-pollution list; with these words it led with the two EuroLeague
# results of the week).
RESULT_WORDS = {
    "english": "(beat OR beats OR win OR wins OR won OR defeat OR lost OR draw OR score)",
    "hebrew": "(ניצחה OR ניצח OR הפסידה OR גברה OR הביסה OR תיקו OR ניצחון OR הפסד)",
    "arabic": "(فاز OR فوز OR خسر OR تعادل OR نتيجة)",
    "russian": "(победил OR победа OR проиграл OR ничья OR счет)",
}


def _news_url(topic: str, lang: str, sport: bool) -> str:
    if not topic.strip():
        hl, gl, ceid = EDITION[HOME_EDITION.get(lang, "english")]
        return "https://news.google.com/rss?" + urllib.parse.urlencode(
            {"hl": hl, "gl": gl, "ceid": ceid})
    hl, gl, ceid = EDITION.get(lang, EDITION["english"])
    q = topic.strip()
    if sport:
        q += " " + RESULT_WORDS.get(lang, RESULT_WORDS["english"])
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q + " when:3d", "hl": hl, "gl": gl, "ceid": ceid})


def parse_rss(raw: bytes, now: float | None = None) -> list[dict]:
    """Google News RSS as rows {"title", "source", "at"}, newest first, deduplicated,
    none older than MAX_AGE_S. The " - Outlet" Google appends to a title is cut off:
    the outlet is its own field."""
    now = time.time() if now is None else now
    rows, seen = [], set()
    for it in ET.fromstring(raw).findall("./channel/item"):
        title = html.unescape((it.findtext("title") or "").strip())
        source = html.unescape((it.findtext("source") or "").strip())
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].rstrip()
        try:
            at = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            continue
        key = title.lower()
        if not title or key in seen or now - at > MAX_AGE_S:
            continue
        seen.add(key)
        rows.append({"title": title, "source": source or "Google News", "at": at})
    rows.sort(key=lambda r: -r["at"])
    return rows


def headlines(topic: str, lang: str, sport: bool = False, n: int = 25) -> list[dict] | None:
    """The newest headlines about topic (or the top stories, with no topic). None when
    Google News could not be reached or sent something that is not a feed."""
    try:
        rows = parse_rss(_get(_news_url(topic, lang, sport)))
        if sport and len(rows) < 3:
            # The result words found almost nothing: the plain search, once.
            rows = parse_rss(_get(_news_url(topic, lang, False))) or rows
    except Exception:  # noqa: BLE001
        return None
    return rows[:n]


# ---------------------------------------------------------------- prices
COINGECKO = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
             "&order=market_cap_desc&per_page=60&page=1")
FRANKFURTER = "https://api.frankfurter.dev/v1/latest?from=USD"
# Currencies offered as candidates, each against the shekel and the dollar.
CURRENCIES = ("USD", "EUR", "GBP", "ILS", "JPY", "CHF", "CAD", "AUD", "CNY", "TRY")


def coins() -> list[dict] | None:
    try:
        d = json.loads(_get(COINGECKO))
        return [{"kind": "coin", "name": str(c["name"]), "symbol": str(c["symbol"]).upper(),
                 "price": float(c["current_price"]),
                 "change_pct": float(c.get("price_change_percentage_24h") or 0.0),
                 "source": "CoinGecko", "at": time.time()}
                for c in d if c.get("current_price") is not None]
    except Exception:  # noqa: BLE001
        return None


def rates() -> list[dict] | None:
    """One unit of each currency in shekels and in dollars."""
    try:
        d = json.loads(_get(FRANKFURTER))
        usd = {**{k: float(v) for k, v in d["rates"].items()}, "USD": 1.0}
        day = str(d.get("date") or "")
    except Exception:  # noqa: BLE001
        return None
    out = []
    for base in CURRENCIES:
        for quote in ("ILS", "USD"):
            if base == quote or base not in usd or quote not in usd:
                continue
            out.append({"kind": "rate", "base": base, "quote": quote,
                        "price": usd[quote] / usd[base], "day": day,
                        "source": "the European Central Bank"})
    return out


def prices() -> list[dict] | None:
    """Coins and exchange rates together, fetched at once. None only when both
    sources are unreachable."""
    box: dict = {}
    ts = [threading.Thread(target=lambda: box.__setitem__("c", coins()), daemon=True),
          threading.Thread(target=lambda: box.__setitem__("r", rates()), daemon=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(TIMEOUT_S + 1)
    c, r = box.get("c"), box.get("r")
    if c is None and r is None:
        return None
    return (r or []) + (c or [])


CURRENCY_NAME = {"USD": "US dollar", "EUR": "euro", "GBP": "British pound",
                 "ILS": "shekel", "JPY": "Japanese yen", "CHF": "Swiss franc",
                 "CAD": "Canadian dollar", "AUD": "Australian dollar",
                 "CNY": "Chinese yuan", "TRY": "Turkish lira"}
# Spoken: "the rate of <this>" in each language, and the unit a rate is given in.
RATE_OF = {
    "hebrew": {"USD": "הדולר", "EUR": "היורו", "GBP": "הלירה שטרלינג", "ILS": "השקל",
               "JPY": "הין היפני", "CHF": "הפרנק השווייצרי", "CAD": "הדולר הקנדי",
               "AUD": "הדולר האוסטרלי", "CNY": "היואן הסיני", "TRY": "הלירה הטורקית"},
    "arabic": {"USD": "الدولار", "EUR": "اليورو", "GBP": "الجنيه الإسترليني",
               "ILS": "الشيكل", "JPY": "الين الياباني", "CHF": "الفرنك السويسري",
               "CAD": "الدولار الكندي", "AUD": "الدولار الأسترالي", "CNY": "اليوان الصيني",
               "TRY": "الليرة التركية"},
    "russian": {"USD": "доллара", "EUR": "евро", "GBP": "фунта стерлингов", "ILS": "шекеля",
                "JPY": "японской иены", "CHF": "швейцарского франка",
                "CAD": "канадского доллара", "AUD": "австралийского доллара",
                "CNY": "китайского юаня", "TRY": "турецкой лиры"},
}
# A rate is never a whole number, and Russian takes the genitive singular after one.
UNIT = {"english": {"ILS": "shekels", "USD": "US dollars"},
        "hebrew": {"ILS": "שקלים", "USD": "דולר"},
        "arabic": {"ILS": "شيكل", "USD": "دولار"},
        "russian": {"ILS": "шекеля", "USD": "доллара"}}


def price_label(row: dict) -> str:
    """How a price row is shown to Jev: the source's own names, nothing else."""
    if row["kind"] == "coin":
        return f"{row['name']} ({row['symbol']}), a cryptocurrency, price in US dollars"
    names = CURRENCY_NAME
    return (f"exchange rate: one {names[row['base']]} ({row['base']}) in "
            f"{names[row['quote']]}s ({row['quote']})")


def amount(x: float) -> str:
    """A price as digits a voice reads well: whole above 100, two places above 1,
    enough significant figures below that."""
    if x >= 100:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.2f}"
    return f"{x:.4g}"
