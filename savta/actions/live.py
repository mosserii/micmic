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
  * Yahoo Finance's public search and chart endpoints, for stock prices and indices
    (2026-10-02). Keyless: the search names the listing (Apple -> AAPL, "teva" -> TEVA,
    "s&p 500" -> ^GSPC), the chart gives the price, the day's closes and a year of them.
    Stooq, the other keyless source, now answers its CSV address with a JavaScript
    check instead of data. Unofficial: if Yahoo stops answering, she hears that the
    price could not be reached, never a guess.

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
    except Exception:  # noqa: BLE001
        return None
    if sport and len(rows) < 3:
        # The result words found almost nothing: the plain search, once. Its failure
        # keeps what the first search found.
        try:
            rows = parse_rss(_get(_news_url(topic, lang, False))) or rows
        except Exception:  # noqa: BLE001
            pass
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


# ---------------------------------------------------------------- stocks
YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search?"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
# What a spoken question about a share price can mean: a company's shares, a fund, an
# index ("how is the S&P doing"). Futures, options and currencies are left out.
STOCK_TYPES = ("EQUITY", "ETF", "INDEX", "MUTUALFUND")
# A few prices are quoted in hundredths: Tel Aviv in agorot (ILA), London in pence (GBp).
_MINOR = {"ILA": ("ILS", 100.0), "GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
          "ZAc": ("ZAR", 100.0)}


def stock_search(name: str) -> list[dict] | None:
    """Listings for a company or index name, Yahoo's best first: [{"symbol", "name",
    "type"}]. None when Yahoo could not be reached."""
    url = YAHOO_SEARCH + urllib.parse.urlencode(
        {"q": name, "quotesCount": 6, "newsCount": 0, "listsCount": 0})
    try:
        d = json.loads(_get(url))
    except Exception:  # noqa: BLE001
        return None
    return [{"symbol": str(q["symbol"]), "name": str(q.get("longname") or q.get("shortname")
                                                     or q["symbol"]).strip(),
             "type": str(q.get("quoteType") or "")}
            for q in d.get("quotes") or [] if q.get("symbol")
            and str(q.get("quoteType") or "") in STOCK_TYPES]


def stock_quote(symbol: str) -> dict | None:
    """The price now, the previous close and the close a year ago, from one chart of
    daily closes: {"symbol", "name", "price", "prev", "year_ago", "currency", "index",
    "at"}. None when Yahoo could not be reached or sent no price."""
    url = YAHOO_CHART + urllib.parse.quote(symbol) + "?" + urllib.parse.urlencode(
        {"range": "1y", "interval": "1d"})
    try:
        r = json.loads(_get(url))["chart"]["result"][0]
        meta = r["meta"]
        price = float(meta["regularMarketPrice"])
    except Exception:  # noqa: BLE001
        return None
    ts = r.get("timestamp") or []
    closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    days = [(t, float(c)) for t, c in zip(ts, closes) if c is not None]
    at = float(meta.get("regularMarketTime") or (days[-1][0] if days else time.time()))
    prev = None
    if days:
        # The last close is today's own when it is the same day as the price.
        same_day = time.gmtime(days[-1][0])[:3] == time.gmtime(at)[:3]
        prev = days[-2][1] if same_day and len(days) > 1 else (None if same_day else days[-1][1])
    year_ago = days[0][1] if len(days) > 200 else None
    currency = str(meta.get("currency") or "USD")
    if currency in _MINOR:
        currency, div = _MINOR[currency]
        price, prev = price / div, (prev / div if prev is not None else None)
        year_ago = year_ago / div if year_ago is not None else None
    return {"symbol": str(meta.get("symbol") or symbol),
            "name": str(meta.get("shortName") or meta.get("longName") or symbol).strip(),
            "price": price, "prev": prev, "year_ago": year_ago, "currency": currency,
            "index": str(meta.get("instrumentType") or "") == "INDEX", "at": at,
            "source": "Yahoo Finance"}


def pct(a: float, b: float) -> float:
    """The change from b to a, in percent."""
    return (a - b) / b * 100.0 if b else 0.0


def pct_words(x: float) -> str:
    """A percentage as a voice reads it well: one decimal under ten, whole above."""
    x = abs(x)
    return f"{x:.1f}".rstrip("0").rstrip(".") if x < 10 else f"{x:.0f}"
