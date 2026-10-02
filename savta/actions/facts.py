"""Keyless sources of current fact, so an answer is grounded rather than recalled."""
from __future__ import annotations
import http.client, json, threading, time, urllib.parse, urllib.request

UA = {"User-Agent": "micmic/0.1"}

# The same town asked about twice in a few minutes ("the weather in Lisbon", then
# "and Lisbon tomorrow?") paid wttr.in's full round trip each time, 0.3 to 3.5 s
# measured. The sky does not change that fast, and a place wttr has never heard of
# will not start existing in ten minutes either.
FRESH_S = 600.0
_COND: dict[str, tuple[float, dict | None]] = {}
_MISS: dict[str, str] = {}
_LOCK = threading.Lock()


def weather(place: str = "", lang: str = "he") -> str:
    """wttr.in: no key, speaks many languages, returns one plain line."""
    loc = urllib.parse.quote(place.strip() or "")
    url = f"https://wttr.in/{loc}?format=%l:+%C+%t+%w&lang={lang[:2]}"
    try:
        req = urllib.request.Request(url, headers=UA)
        return urllib.request.urlopen(req, timeout=4).read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


class _Gone(Exception):
    """wttr answered with an error status; `body` says whether it knows the place."""

    def __init__(self, status: int, body: str):
        super().__init__(status)
        self.status, self.body = status, body


# Kept-alive connections, per host. A fresh one cost a TLS handshake, about 0.2 s of
# every weather answer (measured with curl: time_appconnect 0.18-0.58 s), and a
# Wikipedia lookup 0.55 s fresh against 0.17-0.35 s on a kept one (2026-10-02). A few
# are kept because the readings of one Hebrew town name are fetched in parallel.
_IDLE: dict[str, list] = {}
_IDLE_MAX = 4
# A connection idle longer than this is not trusted: the far end may have dropped it.
_IDLE_S = 50.0


def _kept_get(host: str, path: str, timeout: float) -> tuple[int, bytes]:
    """(status, body) of one GET on a kept-alive connection to host."""
    with _LOCK:
        pool = _IDLE.setdefault(host, [])
        conn = None
        while pool and conn is None:
            c, at = pool.pop()
            if time.time() - at <= _IDLE_S:
                conn = c
            else:
                c.close()
    for attempt in (0, 1):
        reused = conn is not None
        if conn is None:
            conn = http.client.HTTPSConnection(host, 443, timeout=timeout)
        try:
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            conn.request("GET", path, headers={**UA, "Connection": "keep-alive"})
            resp = conn.getresponse()
            raw = resp.read()
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                http.client.CannotSendRequest):
            conn.close()
            conn = None
            if reused and attempt == 0:
                continue          # an idle socket the host had already closed: try fresh
            raise
        except Exception:
            conn.close()
            raise
        _keep(host, conn, resp.will_close)
        return resp.status, raw
    raise ConnectionError(f"{host} unreachable")


def _keep(host: str, conn, will_close: bool = False) -> None:
    with _LOCK:
        pool = _IDLE.setdefault(host, [])
        if len(pool) < _IDLE_MAX and not will_close:
            pool.append((conn, time.time()))
            return
    conn.close()


def warm(host: str) -> None:
    """Open a connection to host now, on its own thread, unless one is already kept, so
    the lookup that follows starts without a handshake. Fails silently."""
    with _LOCK:
        if any(time.time() - at <= _IDLE_S for _, at in _IDLE.get(host, [])):
            return

    def run() -> None:
        try:
            conn = http.client.HTTPSConnection(host, 443, timeout=4)
            conn.connect()
            _keep(host, conn)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=run, daemon=True).start()


def wiki_host(lang: str) -> str:
    return f"{lang[:2]}.wikipedia.org"


def _wttr_get(path: str, timeout: float) -> bytes:
    status, raw = _kept_get("wttr.in", path, timeout)
    if status != 200:
        raise _Gone(status, raw[:200].decode("utf-8", "replace").lower())
    return raw


def _j1(place: str) -> dict | None:
    """wttr.in's full report for a place, cached. Records why it is missing."""
    key = place.strip().lower()
    with _LOCK:
        hit = _COND.get(key)
    if hit and time.time() - hit[0] < FRESH_S:
        return hit[1]
    loc = urllib.parse.quote(place.strip() or "")
    try:
        d = json.loads(_wttr_get(f"/{loc}?format=j1", timeout=5))
        d["current_condition"][0]
    except _Gone as e:
        # wttr answers a place it does not know with an error status (a 500, measured)
        # whose body says "location not found". Any other error is wttr being down.
        unknown = "not found" in e.body or e.status == 404
        with _LOCK:
            _MISS[key] = "not_found" if unknown else "unreachable"
            if unknown:
                _COND[key] = (time.time(), None)
        return None
    except (ValueError, KeyError, IndexError, TypeError):
        # A 200 that is not weather: "Unknown location; please try ~..." and friends.
        with _LOCK:
            _MISS[key] = "not_found"
            _COND[key] = (time.time(), None)
        return None
    except Exception:  # noqa: BLE001
        with _LOCK:
            _MISS[key] = "unreachable"
        return None
    with _LOCK:
        _COND[key] = (time.time(), d)
        _MISS.pop(key, None)
    return d


def miss_reason(place: str = "") -> str:
    """Why conditions(place) came back empty: "not_found" or "unreachable"."""
    with _LOCK:
        return _MISS.get(place.strip().lower(), "unreachable")


# ---------------------------------------------------------------- places
# wttr.in geocodes a name on its own and says little about what it found: "תל אביב"
# came back as "Al Mas`Udiya". Open-Meteo's geocoder (keyless) answers a name in any of
# her languages with ranked real places, country and coordinates, and nothing at all
# for "בליסבון" or "ת ים", which is exactly the test for whether a bound letter was
# part of the name. The weather is then asked for at those coordinates.
_GEO: dict[tuple[str, str], tuple[float, list | None]] = {}
GEO_FRESH_S = 24 * 3600.0


def geocode(name: str, lang: str = "en") -> list[dict] | None:
    """Real places called `name`, most populous first. [] when there is no such
    place, None when the geocoder could not be reached."""
    key = (name.strip().lower(), lang[:2])
    with _LOCK:
        hit = _GEO.get(key)
    if hit and time.time() - hit[0] < GEO_FRESH_S:
        return hit[1]
    if not key[0]:
        return []
    u = ("https://geocoding-api.open-meteo.com/v1/search?"
         + urllib.parse.urlencode({"name": name.strip(), "count": 5,
                                   "language": lang[:2], "format": "json"}))
    try:
        d = json.loads(urllib.request.urlopen(
            urllib.request.Request(u, headers=UA), timeout=3).read())
    except Exception:  # noqa: BLE001
        return None
    out = [{"name": r.get("name", ""), "country": r.get("country", ""),
            "country_code": r.get("country_code", ""), "admin1": r.get("admin1", ""),
            "lat": float(r["latitude"]), "lon": float(r["longitude"]),
            "population": int(r.get("population") or 0)}
           for r in (d.get("results") or []) if "latitude" in r and "longitude" in r]
    with _LOCK:
        _GEO[key] = (time.time(), out)
    return out


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _parse(d: dict) -> dict | None:
    """wttr's j1 report as the few numbers a spoken line needs, today and the next
    two days ("days"), plus where wttr says the report is for."""
    try:
        cur = d["current_condition"][0]
        area = d["nearest_area"][0]
        days = []
        for day in d.get("weather", [])[:3]:
            hourly = day.get("hourly", [])
            noon = hourly[4] if len(hourly) > 4 else (hourly[0] if hourly else {})
            days.append({"date": day.get("date", ""),
                         "code": int(noon.get("weatherCode") or 0),
                         "low": int(round(float(day["mintempC"]))),
                         "high": int(round(float(day["maxtempC"]))),
                         "rain_pct": max((int(h.get("chanceofrain") or 0) for h in hourly),
                                         default=0)})
        today = days[0]
        return {
            "where": area["areaName"][0]["value"],
            "lat": float(area.get("latitude") or 0), "lon": float(area.get("longitude") or 0),
            "desc": cur["weatherDesc"][0]["value"].strip(),
            "code": int(cur.get("weatherCode") or 0),
            "temp": int(round(float(cur["temp_C"]))),
            "feels": int(round(float(cur["FeelsLikeC"]))),
            "low": today["low"], "high": today["high"], "rain_pct": today["rain_pct"],
            "days": days,
        }
    except Exception:  # noqa: BLE001
        return None


def conditions_at(lat: float, lon: float) -> dict | None:
    """The weather at a point, from wttr.in. None when wttr cannot be reached."""
    d = _j1(f"{lat:.3f},{lon:.3f}")
    return _parse(d) if d is not None else None


def conditions(place: str = "") -> dict | None:
    """The same wttr.in call as forecast(), but structured, so a spoken line can be
    built locally instead of paying a model several seconds to phrase three numbers.

    By name, as wttr resolves it. The router asks by coordinates (conditions_at), so
    the place is one it has checked; this stays for callers that only have a name.
    None when there is no answer; miss_reason() says why."""
    d = _j1(place)
    if d is None:
        return None
    out = _parse(d)
    if out is None:
        with _LOCK:
            _MISS[place.strip().lower()] = "not_found"
    return out


def forecast(place: str = "", days: int = 3) -> str:
    """Today plus the next couple of days, as plain text for the LLM to phrase."""
    d = _j1(place)
    if d is None:
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
    """First paragraph of the best-matching article, in her language when it exists.

    One request, not two: the search and the summary used to be separate round trips,
    a few hundred milliseconds of every knowledge answer. On a kept-alive connection
    (see _kept_get): the handshake was most of what was left."""
    for L in (lang[:2], "en"):
        try:
            status, raw = _kept_get(
                wiki_host(L),
                f"/w/api.php?action=query&format=json&generator=search"
                f"&gsrsearch={urllib.parse.quote(term)}&gsrlimit=1"
                f"&prop=extracts&exintro=1&explaintext=1&redirects=1", 4)
            if status != 200:
                continue
            d = json.loads(raw)
            pages = (d.get("query") or {}).get("pages") or {}
            text = next((p.get("extract") or "" for p in pages.values()), "").strip()
            if text:
                return text[:700]
        except Exception:  # noqa: BLE001
            continue
    return ""


_WIKI = wiki


def warm_wiki(lang: str) -> None:
    """warm() the Wikipedia host wiki() will ask first. Skipped when wiki() has been
    replaced (a test's stand-in): nothing real would use the connection."""
    if wiki is _WIKI:
        warm(wiki_host(lang))
