"""The public web pages: landing, privacy, terms, and the two Stripe return pages.

Rendered once at startup from proxy/web/*.html, with the plan numbers taken from the
running Config so the page never promises a limit the meter does not enforce. Every
page is self-contained: the stylesheet is inlined, there is no script at all (the
landing page's demo is CSS animation), and the only other requests a page makes are
for our own files under /static/ (the icon and the self-hosted fonts).
"""
from __future__ import annotations

import html
from datetime import date
from pathlib import Path

from .config import PROXY_ROOT, Config

WEB_ROOT = PROXY_ROOT / "web"
UPDATED = "3 October 2026"
# Each legal page's version, shown at its top as "Version X. Last updated Y." The iPhone
# app records these same strings when she agrees (terms_version, privacy_version), so a
# material change bumps the date here, in the app's constants and in the proxy's
# *_REQUIRED variables together.
DOC_VERSIONS = {
    "/terms": "2026-10-03",
    "/privacy": "2026-10-03",
}

# path -> template file
PAGES = {
    "/": "index.html",
    "/privacy": "privacy.html",
    "/terms": "terms.html",
    "/support": "support.html",
    "/checkout/success": "checkout-success.html",
    "/checkout/cancel": "checkout-cancel.html",
}
# The home page's first screen (header and hero) is its own file, filled into {{hero}}.
# The owner picked hero A (the bar alone, centred) on 2026-09-25, over the Mac-window
# hero and a Verde-style paper one.
HEROES = {
    "/": ("hero/a.html", "hero-a"),
}
NOINDEX = '\n<meta name="robots" content="noindex">'

# Every file under web/static/ with one of these extensions is served at /static/<its
# path>. The table is built from a directory listing at startup, so a request can only
# ever name a file that was listed: there is no path to resolve and nothing to traverse.
STATIC_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

# No script may run, nothing may be framed, and nothing but our own files is loaded.
HTML_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                "img-src 'self'; font-src 'self'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _fmt(n: int) -> str:
    return f"{n:,}"


def _long_date(iso: str) -> str:
    """"2026-10-03" -> "3 October 2026", the way the pages write a date."""
    d = date.fromisoformat(iso)
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


# Not strftime("%B"): that follows the server's locale, and the pages are English only.
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def static_files(root: Path = WEB_ROOT) -> dict[str, tuple[Path, str]]:
    """{url path: (file, content type)} for everything servable under root/static.
    Hidden files and unknown extensions are left out rather than guessed at."""
    base = root / "static"
    out: dict[str, tuple[Path, str]] = {}
    for f in sorted(base.rglob("*")):
        rel = f.relative_to(base)
        ctype = STATIC_TYPES.get(f.suffix.lower())
        if not f.is_file() or ctype is None or any(p.startswith(".") for p in rel.parts):
            continue
        out["/static/" + rel.as_posix()] = (f, ctype)
    return out


def render(cfg: Config, root: Path = WEB_ROOT) -> dict[str, tuple[bytes, str]]:
    """{path: (body, content type)} for every page and static file."""
    css = (root / "site.css").read_text()
    url = cfg.download_url or ""
    if url.startswith("https://"):
        cta = f'<a class="button" href="{html.escape(url)}">Download for Mac</a>'
    else:
        cta = '<span class="soon">Download coming soon</span>'
    # The same download, compact, in the header; nothing there until there is a link.
    nav_cta = (f'<div class="top-cta"><a class="button" href="{html.escape(url)}">Download</a></div>'
               if url.startswith("https://") else "")
    email = cfg.support_email or ""
    if email:
        e = html.escape(email)
        contact = f'<a href="mailto:{e}">{e}</a>'
        contact_inline = f' at <a href="mailto:{e}">{e}</a>'
    else:
        # Set SUPPORT_EMAIL before launch; without it the pages have no way to reach us.
        contact, contact_inline = "", ""
    values = {
        "css": css,
        "cta": cta,
        "nav_cta": nav_cta,
        "contact": contact,
        "contact_inline": contact_inline,
        "free_requests": _fmt(cfg.daily_requests("free")),
        "pro_requests": _fmt(cfg.daily_requests("pro")),
        "free_voice_chars": _fmt(cfg.free_tts_chars),
        "pro_voice_day_chars": _fmt(cfg.pro_daily_tts_chars),
        "pro_voice_month_chars": _fmt(cfg.pro_monthly_tts_chars),
        "pro_price": html.escape(cfg.pro_price_label),
        "operator": html.escape(cfg.operator_name),
        "updated": UPDATED,
        # Link previews (WhatsApp, iMessage, X, LinkedIn) need an absolute image URL.
        "base_url": html.escape((cfg.public_base_url or "").rstrip("/")),
    }
    out: dict[str, tuple[bytes, str]] = {}
    for path, name in PAGES.items():
        text = (root / name).read_text()
        if path in HEROES:
            hero, variant = HEROES[path]
            # The hero first: it carries {{cta}} and the plan numbers too.
            text = text.replace("{{hero}}", (root / hero).read_text().rstrip("\n"))
            text = text.replace("{{variant}}", variant)
            text = text.replace("{{robots}}", "" if path == "/" else NOINDEX)
        if path in DOC_VERSIONS:
            v = DOC_VERSIONS[path]
            text = text.replace("{{version}}", v).replace(
                "{{updated}}", _long_date(v))
        for k, v in values.items():
            text = text.replace("{{" + k + "}}", v)
        out[path] = (text.encode("utf-8"), "text/html; charset=utf-8")
    for path, (f, ctype) in static_files(root).items():
        out[path] = (f.read_bytes(), ctype)
    return out
