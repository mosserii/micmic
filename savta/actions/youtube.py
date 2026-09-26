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


# Uploads that are about a film rather than the film or its real trailer. Jev is told
# the same thing; this only takes the obvious ones off the list before it looks.
# "play the Titanic trailer" played "Annoying Orange: Titanic" on 2 of 3 runs.
_NOT_THE_REAL_THING = re.compile(
    r"\b(parody|parodies|spoof|reaction|reacts?|fan[- ]?made|concept|annoying orange|"
    r"meme|ai[- ]generated|in \d+ (?:seconds|minutes)|but it'?s)\b|פרודיה|пароди|"
    r"محاكاة ساخرة", re.I)
_WHOLE_FILM = re.compile(r"\bfull (?:movie|film)\b|\bmovie full\b|סרט מלא|"
                         r"полный фильм|فيلم كامل|فلم .{0,12}كامل", re.I)


def screen_results(results: list[dict], *, trailer: bool = False) -> list[dict]:
    """Drop what is plainly not what she asked for, keeping at least two to choose
    from. A trailer is short and is never the whole film; a parody is never the thing
    itself. A whole-film upload is dropped only when she asked for the trailer. It used
    to be dropped whenever wants_full_length read low, so a movie request chose among
    clips; the owner (1.0.2): "sometimes I just want the movie"."""
    keep = [r for r in results if not _NOT_THE_REAL_THING.search(r.get("title", ""))]
    if trailer:
        keep = [r for r in keep if not _WHOLE_FILM.search(r.get("title", ""))]
        keep = [r for r in keep if not r.get("length") or 0 < seconds(r["length"]) <= 8 * 60]
    return keep if len(keep) >= 2 else results


# Pieces of a title that are for the search page, not for the ear.
_TAG = re.compile(r"[\(\[][^\)\]]*\b(official|video|audio|lyrics?|hd|4k|1080p|720p|"
                  r"remaster(?:ed)?|visuali[sz]er|clip|mv)\b[^\)\]]*[\)\]]", re.I)
_NOISE = re.compile(r"#\S+|\b(?:hd|4k|1080p|720p|uhd)\b|[\"“”„«»‘’]|"
                    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", re.I)


def spoken_title(title: str, channel: str = "") -> str:
    """A title as it should be read aloud: no channel name tacked on the end, no
    pipes, quotes, hashtags or "(Official Video)". Carmit read out
    'Titanic | "Iceberg, Right Ahead!" | Paramount' pipe marks, quotes and all."""
    t = _TAG.sub(" ", title or "")
    ch = (channel or "").strip().lower()

    def is_channel(seg: str) -> bool:
        low = seg.lower()
        return bool(ch) and (low == ch or low in ch or ch in low)

    segs = [re.sub(r"\s+", " ", _NOISE.sub(" ", seg)).strip(" -–—:,.")
            for seg in re.split(r"\s*[|│｜]\s*", t)]
    # "Titanic - Paramount Movies": a channel name after the last dash goes too.
    if segs and re.search(r"\s[-–—]\s", segs[-1]):
        head, tail = re.split(r"\s[-–—]\s(?=[^-–—]*$)", segs[-1])
        if is_channel(tail.strip()) and head.strip():
            segs[-1] = head.strip()
    # The first piece is the title itself even when it is also the artist's channel.
    parts = [seg for i, seg in enumerate(segs) if seg and not (i and is_channel(seg))]
    out = ", ".join(parts[:2]) or re.sub(r"\s+", " ", _NOISE.sub(" ", title or "")).strip()
    if len(out) > 70:
        cut = out[:70].rsplit(" ", 1)[0]
        out = cut.rstrip(" ,-–—:")
    return out


def seconds(length: str) -> int:
    parts = [int(p) for p in (length or "").split(":") if p.isdigit()]
    total = 0
    for p_ in parts:
        total = total * 60 + p_
    return total


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
