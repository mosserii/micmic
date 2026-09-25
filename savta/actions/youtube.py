"""Search YouTube without an API key, then let Jev pick the right result."""
from __future__ import annotations
import json, re, subprocess, urllib.error, urllib.parse, urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def search(query: str, n: int = 18) -> list[dict]:
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
    # The consent cookie matters: without it a EU-resolved request can land on the
    # consent interstitial instead of results. (Lesson taken from macbrow.)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "SOCS=CAI; CONSENT=YES+cb"})
    try:
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html) or \
        re.search(r'ytInitialData"\]\s*=\s*(\{.*?\});', html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            v = o.get("videoRenderer")
            if v and v.get("videoId"):
                out.append({
                    "id": v["videoId"],
                    "title": "".join(r.get("text", "") for r in v.get("title", {}).get("runs", [])),
                    "channel": "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", [])),
                    "length": v.get("lengthText", {}).get("simpleText", ""),
                    "views": v.get("shortViewCountText", {}).get("simpleText", ""),
                })
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    seen, uniq = set(), []
    for v in out:
        if v["id"] not in seen and v["title"]:
            seen.add(v["id"]); uniq.append(v)
    return uniq[:n]


def live(query: str, n: int = 12) -> list[dict]:
    """Search restricted to live broadcasts. sp=EgJAAQ%3D%3D is YouTube's live filter."""
    url = ("https://www.youtube.com/results?search_query="
           + urllib.parse.quote(query) + "&sp=EgJAAQ%253D%253D")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "SOCS=CAI; CONSENT=YES+cb"})
    try:
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    # Same two page shapes search() has to handle — without this fallback, live()
    # came back empty on any day YouTube served the alternate shape, even though
    # search() (asking for the same content) parsed fine.
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html) or \
        re.search(r'ytInitialData"\]\s*=\s*(\{.*?\});', html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            v = o.get("videoRenderer")
            if v and v.get("videoId"):
                out.append({"id": v["videoId"],
                            "title": "".join(r.get("text", "") for r in v.get("title", {}).get("runs", [])),
                            "channel": "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", [])),
                            "length": "LIVE",
                            "views": v.get("shortViewCountText", {}).get("simpleText", "")})
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    seen, uniq = set(), []
    for v in out:
        if v["id"] not in seen and v["title"]:
            seen.add(v["id"]); uniq.append(v)
    return uniq[:n]


def minutes(length: str) -> int:
    if not length:
        return 0
    parts = [int(p) for p in length.split(":") if p.isdigit()]
    if len(parts) == 3:
        return parts[0] * 60 + parts[1]
    if len(parts) == 2:
        return parts[0]
    return 0


def watch_url(video_id: str) -> str:
    # autoplay + start at 0; the plain watch URL is the most reliable thing to hand to Chrome
    return f"https://www.youtube.com/watch?v={video_id}&autoplay=1"


def play(video_id: str) -> None:
    subprocess.run(["open", watch_url(video_id)], check=False)
