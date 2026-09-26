"""The public pages and the files they load from /static/."""
import re

from proxy import pages

PAGES = ("/", "/privacy", "/terms", "/checkout/success", "/checkout/cancel")


def _get(p, path):
    s, _, raw, h = p.request("GET", path)
    return s, raw, h


def _refs(html: str) -> set[str]:
    """Every same-origin file a page asks for: src, href and CSS url()."""
    found = set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html))
    found |= set(re.findall(r"url\((/static/[^)]+)\)", html))
    return found


def test_every_file_a_page_loads_is_served_with_its_type(make_proxy):
    p = make_proxy()
    refs: set[str] = set()
    for path in PAGES:
        refs |= _refs(_get(p, path)[1].decode("utf-8"))
    assert "/static/fonts/rubik-latin-wght-normal.woff2" in refs
    assert "/static/icon.svg" in refs and "/static/mic.svg" in refs
    want = {".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png",
            ".webp": "image/webp"}
    for ref in sorted(refs):
        s, raw, h = _get(p, ref)
        assert s == 200 and raw, ref
        ext = ref[ref.rindex("."):]
        assert h["Content-Type"] == want[ext], ref
        assert h["X-Content-Type-Options"] == "nosniff"
    s, raw, _ = _get(p, "/static/fonts/rubik-latin-wght-normal.woff2")
    assert raw[:4] == b"wOF2"


def test_pages_load_nothing_from_anywhere_else(make_proxy):
    p = make_proxy(download_url="https://example.com/MicMic.dmg")
    for path in PAGES:
        s, raw, h = _get(p, path)
        html = raw.decode("utf-8")
        csp = h["Content-Security-Policy"]
        assert "font-src 'self'" in csp and "script-src" not in csp, path
        assert not re.search(r"url\((?!/static/)", html), path
        assert not re.search(r'src="(?!/static/)', html), path
        # The only off-site link on any page is the download itself.
        offsite = set(re.findall(r'href="(https?://[^"]+)"', html))
        assert offsite <= {"https://example.com/MicMic.dmg",
                           "https://github.com/mosserii/micmic"}, path


def test_static_paths_cannot_leave_the_static_folder(make_proxy):
    p = make_proxy()
    for path in ("/static/../proxy/pages.py", "/static/%2e%2e/proxy/pages.py",
                 "/static/fonts/../icon.png", "/static/./icon.png", "/static/",
                 "/static", "/static//icon.png", "/static/fonts/OFL.txt/",
                 "/static/../../proxy/config.py", "/static/icon.PNG"):
        assert _get(p, path)[0] == 404, path


def test_static_table_is_an_allowlist(tmp_path):
    static = tmp_path / "static"
    (static / "fonts").mkdir(parents=True)
    (static / "fonts" / "a.woff2").write_bytes(b"wOF2")
    (static / "b.webp").write_bytes(b"RIFF")
    (static / "notes.md").write_text("not served")
    (static / "run.py").write_text("not served")
    (static / ".secret.png").write_bytes(b"x")
    (static / ".hidden").mkdir()
    (static / ".hidden" / "c.png").write_bytes(b"x")
    got = pages.static_files(tmp_path)
    assert got == {
        "/static/b.webp": (static / "b.webp", "image/webp"),
        "/static/fonts/a.woff2": (static / "fonts" / "a.woff2", "font/woff2"),
    }


def test_landing_numbers_and_links_are_never_hard_coded(make_proxy):
    p = make_proxy(free_daily_calls=90, pro_daily_calls=450, jev_calls_per_request=3,
                   pro_price_label="$11/month")
    html = _get(p, "/")[1].decode("utf-8")
    assert "30 requests to try" in html
    assert "100 requests" not in html and "100 free" not in html
    assert "2,000" not in html and "150 requests a day" in html
    assert "$8" not in html and "$11/month" in html
    assert ".dmg" not in html and "coming soon" in html
    assert "In total, not per day." in html
    assert "Apple silicon (M1 or later), macOS 13 or later" in html


def test_checkout_success_shows_the_pro_allowance(make_proxy):
    html = _get(make_proxy(pro_daily_calls=6000), "/checkout/success")[1].decode("utf-8")
    assert "2,000 requests a day." in html


def test_home_is_hero_a(make_proxy):
    """The owner's pick: the bar alone, centred, one line and one button."""
    p = make_proxy(download_url="https://example.com/MicMic.dmg", free_daily_calls=150)
    s, raw, h = _get(p, "/")
    html = raw.decode("utf-8")
    assert s == 200 and h["Content-Type"] == "text/html; charset=utf-8"
    assert "<script" not in html.lower() and "{{" not in html and "noindex" not in html
    assert "\u2014" not in html and "\u2013" not in html
    assert 'class="home hero-a"' in html and html.count("<h1") == 1
    assert "Talk to your Mac." in html and "top 100 hits" in html and "Undo" in html
    assert 'href="https://example.com/MicMic.dmg">Download for Mac' in html
    assert 'class="rig"' not in html and 'class="picks"' not in html
    assert "50 requests to try" in html


def test_home_without_a_download_link(make_proxy):
    html = _get(make_proxy(), "/")[1].decode("utf-8")
    assert "coming soon" in html and "Download for Mac" not in html


def test_retired_hero_previews_are_gone(make_proxy):
    p = make_proxy()
    for path in ("/hero/a", "/hero/b"):
        assert _get(p, path)[0] == 404, path


def test_site_is_english_only_and_says_what_it_runs_on(make_proxy):
    p = make_proxy()
    for path in PAGES:
        html = _get(p, path)[1].decode("utf-8")
        assert not re.search("[\u0590-\u06ff\u0400-\u04ff]", html), path
        assert 'dir="rtl"' not in html and "rtl" not in html, path
        assert "Intel" not in html and "91 languages" not in html, path
    home = _get(p, "/")[1].decode("utf-8")
    assert home.count("Apple silicon (M1 or later), macOS 13 or later") == 1
    assert "Speaks English, Hebrew, Arabic and Russian." in home
    assert 'id="languages"' not in home


def test_link_previews_have_an_absolute_image(make_proxy):
    """WhatsApp and iMessage read og:image, and it must be an absolute URL under 300 KB."""
    p = make_proxy(public_base_url="https://micmic.example")
    html = _get(p, "/")[1].decode("utf-8")
    assert '<meta property="og:image" content="https://micmic.example/static/og.jpg">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    s, raw, h = _get(p, "/static/og.jpg")
    assert s == 200 and h["Content-Type"] == "image/jpeg" and 20_000 < len(raw) < 300_000


def test_head_answers_like_get_without_a_body(make_proxy):
    """Link-preview crawlers and uptime checks send HEAD first; it used to be 501."""
    p = make_proxy()
    for path, ctype in (("/", "text/html; charset=utf-8"), ("/static/og.jpg", "image/jpeg")):
        s, _, raw, h = p.request("HEAD", path)
        assert s == 200 and h["Content-Type"] == ctype and raw == b"", path
        assert int(h["Content-Length"]) == len(_get(p, path)[1]), path


def test_jev_is_named_as_a_fact_with_the_non_affiliation_line(make_proxy):
    """We may say MicMic uses Jev; we must not look like TypeSafe or imply its
    endorsement. Any page that names Jev carries the trademark line, and no page
    uses TypeSafe's logo."""
    p = make_proxy()
    for path in PAGES:
        html = _get(p, path)[1].decode("utf-8")
        if "Jev" in html:
            assert "Jev is a trademark of TypeSafe AI." in html, path
            assert "not affiliated with or endorsed by TypeSafe" in html, path
        assert not re.search(r"<img[^>]*(typesafe|jev)", html, re.I), path   # no logo of theirs
    home = _get(p, "/")[1].decode("utf-8")
    assert "MicMic understands you with Jev, the decision model by TypeSafe." in home


def test_header_has_github_and_a_download_button(make_proxy):
    p = make_proxy(download_url="https://example.com/MicMic.dmg")
    head = _get(p, "/")[1].decode("utf-8").split("</header>")[0]
    assert 'href="https://github.com/mosserii/micmic"' in head and 'aria-label="MicMic on GitHub"' in head
    assert '<div class="top-cta"><a class="button" href="https://example.com/MicMic.dmg">Download</a></div>' in head
    head = _get(make_proxy(), "/")[1].decode("utf-8").split("</header>")[0]
    assert '<div class="top-cta">' not in head      # no link, no header button
