"""Keyless sources of current fact, so an answer is grounded rather than recalled."""
from __future__ import annotations
import json, urllib.parse, urllib.request

UA = {"User-Agent": "micmic/0.1"}


def weather(place: str = "", lang: str = "he") -> str:
    """wttr.in: no key, speaks many languages, returns one plain line."""
    loc = urllib.parse.quote(place.strip() or "")
    url = f"https://wttr.in/{loc}?format=%l:+%C+%t+%w&lang={lang[:2]}"
    try:
        req = urllib.request.Request(url, headers=UA)
        return urllib.request.urlopen(req, timeout=4).read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def conditions(place: str = "") -> dict | None:
    """The same wttr.in call as forecast(), but structured, so a spoken line can be
    built locally instead of paying a model several seconds to phrase three numbers."""
    loc = urllib.parse.quote(place.strip() or "")
    try:
        req = urllib.request.Request(f"https://wttr.in/{loc}?format=j1", headers=UA)
        d = json.loads(urllib.request.urlopen(req, timeout=5).read())
        cur = d["current_condition"][0]
        today = d["weather"][0]
        return {
            "where": d["nearest_area"][0]["areaName"][0]["value"],
            "desc": cur["weatherDesc"][0]["value"].strip(),
            "code": int(cur.get("weatherCode") or 0),
            "temp": int(round(float(cur["temp_C"]))),
            "feels": int(round(float(cur["FeelsLikeC"]))),
            "low": int(round(float(today["mintempC"]))),
            "high": int(round(float(today["maxtempC"]))),
            "rain_pct": max((int(h.get("chanceofrain") or 0)
                             for h in today.get("hourly", [])), default=0),
        }
    except Exception:  # noqa: BLE001
        return None


def forecast(place: str = "", days: int = 3) -> str:
    """Today plus the next couple of days, as plain text for the LLM to phrase."""
    loc = urllib.parse.quote(place.strip() or "")
    try:
        req = urllib.request.Request(f"https://wttr.in/{loc}?format=j1", headers=UA)
        d = json.loads(urllib.request.urlopen(req, timeout=5).read())
    except Exception:  # noqa: BLE001
        return ""
    where = ""
    try:
        area = d["nearest_area"][0]
        where = area["areaName"][0]["value"]
    except Exception:  # noqa: BLE001
        pass
    lines = []
    try:
        cur = d["current_condition"][0]
        lines.append(f"Right now in {where}: {cur['weatherDesc'][0]['value']}, "
                     f"{cur['temp_C']}C, feels like {cur['FeelsLikeC']}C.")
    except Exception:  # noqa: BLE001
        pass
    for i, day in enumerate(d.get("weather", [])[:days]):
        label = ("Today", "Tomorrow", "The day after tomorrow")[i] if i < 3 else day.get("date", "")
        try:
            noon = day["hourly"][4]
            lines.append(f"{label} ({day['date']}): {noon['weatherDesc'][0]['value']}, "
                         f"low {day['mintempC']}C, high {day['maxtempC']}C.")
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(lines)


def wiki(term: str, lang: str = "he") -> str:
    """First paragraph of the best-matching article, in her language when it exists."""
    for L in (lang[:2], "en"):
        try:
            s = urllib.parse.quote(term)
            u = (f"https://{L}.wikipedia.org/w/api.php?action=query&list=search"
                 f"&srsearch={s}&format=json&srlimit=1")
            d = json.loads(urllib.request.urlopen(
                urllib.request.Request(u, headers=UA), timeout=4).read())
            hits = d.get("query", {}).get("search", [])
            if not hits:
                continue
            title = urllib.parse.quote(hits[0]["title"].replace(" ", "_"))
            u2 = f"https://{L}.wikipedia.org/api/rest_v1/page/summary/{title}"
            d2 = json.loads(urllib.request.urlopen(
                urllib.request.Request(u2, headers=UA), timeout=4).read())
            if d2.get("extract"):
                return d2["extract"][:700]
        except Exception:  # noqa: BLE001
            continue
    return ""
