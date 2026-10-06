"""The public pages and the files they load from /static/."""
import re

from proxy import pages

PAGES = ("/", "/privacy", "/terms", "/checkout/success", "/checkout/cancel")
APPLE_EULA = "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/"
APPLE_REFUNDS = "https://reportaproblem.apple.com"
APPLE_LINKS = {APPLE_EULA, APPLE_REFUNDS}


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
        # The only off-site links: the download, GitHub, and Apple's EULA and refund
        # page, which the terms must point to for the iPhone app.
        offsite = set(re.findall(r'href="(https?://[^"]+)"', html))
        assert offsite <= {"https://example.com/MicMic.dmg",
                           "https://github.com/mosserii/micmic"} | APPLE_LINKS, path


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


def _legal(p, path):
    s, raw, h = _get(p, path)
    html = raw.decode("utf-8")
    assert s == 200 and h["Content-Type"] == "text/html; charset=utf-8", path
    assert "{{" not in html and "<script" not in html.lower(), path
    assert "—" not in html and "–" not in html, path       # no em or en dashes
    assert 'href="/privacy"' in html and 'href="/terms"' in html, path
    assert "noindex" not in html, path                                # App Review reads them
    return html


COMPANY = "registered in Israel under company number 517172011"
ADDRESS = "6 Hashachaf, Tel Aviv-Jaffa 6803452, Israel"


def _section(html: str, anchor: str) -> str:
    """The text of one <h2 id=anchor> section, up to the next <h2."""
    start = html.index(f'<h2 id="{anchor}">')
    end = html.find("<h2", start + 1)
    return html[start:end if end != -1 else len(html)]


def test_terms_page_has_what_the_app_store_needs(make_proxy):
    """The iPhone app links here from its consent box and from App Store Connect, so
    the page must carry Apple's subscription disclosures and the EULA reference."""
    p = make_proxy(support_email="help@example.com", free_daily_calls=300,
                   jev_calls_per_request=3, pro_price_label="$8/month")
    html = _legal(p, "/terms")
    assert "<h1>Terms of use</h1>" in html
    assert "you are asked to tick a box to agree to these terms" in html
    # The iPhone subscription: Apple-managed, auto-renew, how to cancel, refunds.
    assert "In-App Purchase" in html and "auto-renewing monthly subscription" in html
    assert "at least 24 hours before the end of the current period" in html
    assert "Settings, tap your name, then Subscriptions" in html
    assert f'href="{APPLE_EULA}"' in html and f'href="{APPLE_REFUNDS}"' in html
    # The Mac subscription and the plan numbers come from the running config.
    assert "through Stripe" in html and "$8/month" in html
    assert "100 requests to try" in html
    # What the assistant will and will not do.
    assert "MicMic never pays." in html
    assert "nothing is sent and no call is placed until you tap" in html
    assert "can be wrong" in html and "not a doctor, lawyer, financial adviser" in html
    # 18+: the Gemini API terms forbid users under 18; App Store rating is overridden to 18+.
    assert 'id="age"' in html and "at least 18 years old" in html
    assert "Apple ID" not in html
    assert "[OWNER:" not in html                       # jurisdiction decided 2026-10-03
    assert 'href="mailto:help@example.com"' in html


def test_terms_say_it_is_not_an_emergency_service_first(make_proxy):
    """The emergency warning is the first thing on the page after the title, in its
    own box, and has its own section."""
    html = _legal(make_proxy(), "/terms")
    body = html.split("<h1>Terms of use</h1>", 1)[1]
    first = body[body.index("<p class=\"alert\""):]
    assert body.index('<p class="alert"') < body.index("<h2")
    assert first.startswith('<p class="alert" id="not-emergency"><strong>MicMic is not an emergency service.</strong>')
    assert "always call your local emergency number yourself" in first.split("</p>")[0]
    sec = _section(html, "emergency")
    assert "cannot call emergency services for you" in sec
    assert "fail to call or reach anyone" in sec
    assert "call your local emergency number yourself, directly" in sec
    assert ".doc .alert" in html                        # the box is styled, not plain text


def test_terms_changes_need_notice_and_keep_a_version(make_proxy):
    html = _legal(make_proxy(), "/terms")
    sec = _section(html, "changes")
    assert "We may change MicMic and these terms at any time" in sec
    assert "at least 14 days before a material change takes effect" in sec
    assert "by email if we have your email address" in sec
    assert "follow the law, a court or a regulator, or to deal with a security risk" in sec
    assert "If you keep using MicMic after a change takes effect, the changed terms apply" in sec
    assert "stop using MicMic and cancel any subscription" in sec
    assert "Apple tells you about a price change" in sec and "at least 30 days" in sec
    assert '<p class="meta">Version 2026-10-03. Last updated 3 October 2026.' in html


def test_terms_operator_law_and_courts(make_proxy):
    """Owner decisions 2026-10-03: BetterFly AI LTD, Israeli law, Tel Aviv-Jaffa courts,
    consumers keep their home protections, no arbitration or class waiver."""
    html = _legal(make_proxy(), "/terms")
    who = _section(html, "who")
    assert "BetterFly AI LTD" in who and COMPANY in who and ADDRESS in who
    law = _section(html, "law")
    assert "governed by the laws of the State of Israel" in law
    assert "competent courts of Tel Aviv-Jaffa, Israel, have exclusive jurisdiction" in law
    assert "does not take away the protection of the mandatory laws of the country where you live" in law
    assert "do not require arbitration" in law
    assert "within 30 days" in law
    liability = _section(html, "liability")
    assert "the greater of the amount you paid for MicMic in the 12 months" in liability
    assert "US$50" in liability
    assert "death or personal injury caused by our negligence" in liability and "fraud" in liability
    assert "of Israel" in liability and "European Union" in liability
    general = _section(html, "general")
    assert "to its owner personally" in general and "English version controls" in general
    for anchor in ("use", "access", "plans", "refunds", "license", "open-source", "content",
                   "providers", "availability", "warranty", "indemnity", "third-party"):
        _section(html, anchor)                          # each section exists


def test_terms_carry_apples_minimum_eula_terms(make_proxy):
    """Apple's Instructions for Minimum Terms of Developer's End-User License Agreement,
    so the Terms also work if pasted as a custom EULA in App Store Connect."""
    sec = _section(_legal(make_proxy(support_email="help@example.com"), "/terms"), "apple")
    for clause in ("Acknowledgement.", "Scope of license.", "Maintenance and support.",
                   "Warranty.", "Product claims.", "Claims about intellectual property.",
                   "Legal compliance.", "Developer contact.", "Third-party terms.",
                   "Third-party beneficiary."):
        assert f"<strong>{clause}</strong>" in sec, clause
    assert "between you and BetterFly AI LTD only, not Apple" in sec
    assert "Apple will refund the purchase price" in sec
    assert "Apple and its subsidiaries are third-party beneficiaries" in sec
    assert "United States government embargo" in sec
    assert ADDRESS in sec and 'href="mailto:help@example.com"' in sec


def test_terms_open_source_matches_the_mit_license(make_proxy):
    """The Mac app's code is MIT (LICENSE at the repo root): the terms must not take
    back what the license gives, and must not stretch it to the name or the service."""
    license_text = (pages.PROXY_ROOT.parent / "LICENSE").read_text()
    assert license_text.startswith("MIT License") and "BetterFly AI LTD" in license_text
    sec = _section(_legal(make_proxy(), "/terms"), "open-source")
    assert "under the MIT License" in sec
    assert "nothing in these terms limits the rights it gives you" in sec
    assert "does not give you any right to the MicMic name or logo" in sec
    use = _section(_legal(make_proxy(), "/terms"), "use")
    assert "parts of MicMic that are not open source" in use


def test_privacy_page_covers_mac_and_iphone(make_proxy):
    p = make_proxy(support_email="help@example.com")
    html = _legal(p, "/privacy")
    assert "MicMic app for Mac and the MicMic app for iPhone" in html
    for who in ("TypeSafe AI (Jev)", "Google (Gemini API)", "ElevenLabs", "Railway",
                "Apple", "Stripe"):
        assert f"<strong>{who}</strong>" in html, who
    assert "ElevenLabs keeps the text and the audio by default" in html
    assert "paid Gemini API" in html
    assert "Phone numbers, email addresses and street addresses never leave your iPhone" in html
    assert "You must be 18 or older to use it" in html
    assert 'id="delete"' in html and "Delete my account and data" in html
    assert "Use AI services" in html                       # withdrawing AI consent
    assert "never from your location" in html              # guideline 5.1.5
    assert "record that you agreed to our terms" in html
    assert "Apple ID" not in html                          # Apple Account since 2024
    assert "does not cancel a subscription bought through Apple" in html
    assert "California" in html and "data protection authority" in html
    assert "in the United States" in html
    assert 'href="mailto:help@example.com"' in html


def test_privacy_describes_iphone_speech_as_build_5_does_it(make_proxy):
    """Build 5: English through MicMic's own model (Parakeet, from Hugging Face), iOS for the
    other languages and until the model is there; the sound stays in memory for the turn."""
    html = _legal(make_proxy(), "/privacy")
    iphone = _section(html, "iphone")
    assert "MicMic&#39;s own speech model is on your iPhone" in iphone
    assert "In other languages, in English until the model is on your iPhone" in iphone
    assert "iOS sends your voice to Apple&#39;s speech service" in iphone
    assert "kept in your iPhone&#39;s memory only while that request lasts" in iphone
    assert "never saved or sent by MicMic" in iphone
    assert "When you speak to MicMic on iPhone, iOS turns your voice into text." not in html
    proc = _section(html, "processors")
    assert "English speech model (about 464 MB)" in proc and "names add-on (about 103 MB)" in proc
    assert "Hugging Face (huggingface.co)" in proc and "nothing about you" in proc


def test_privacy_names_the_controller_and_every_processor(make_proxy):
    html = _legal(make_proxy(), "/privacy")
    who = _section(html, "controller")
    assert "BetterFly AI LTD" in who and COMPANY in who and ADDRESS in who
    # Article 27 representative: not invented; the page says one is being appointed.
    assert "Article 27" in who and "[OWNER:" not in who
    sec = _section(html, "processors")
    for who in ("Railway", "Vercel", "TypeSafe AI (Jev)", "Google (Gemini API)", "ElevenLabs",
                "Stripe", "Apple", "YouTube and Apple&#39;s iTunes Search"):
        assert f"<strong>{who}</strong>" in sec, who
    assert sec.count("It receives") + sec.count("receive the words") >= 6   # what each gets
    ai = _section(html, "ai")
    assert "We do not train AI models on your data" in ai
    assert "we cannot tell you that today" in ai              # TypeSafe terms not in hand
    assert _section(html, "transfers").count("standard contractual clauses") == 1


def test_privacy_bases_rights_retention_and_changes(make_proxy):
    html = _legal(make_proxy(), "/privacy")
    bases = _section(html, "purposes")
    for art in ("Article 6(1)(b)", "Article 6(1)(a)", "Article 6(1)(f)", "Article 6(1)(c)"):
        assert art in bases, art
    rights = _section(html, "rights")
    for word in ("portability", "object", "restrict", "withdraw your consent",
                 "data protection authority", "Amendment 13", "Privacy Protection Authority",
                 "We do not sell or share personal information"):
        assert word in rights, word
    retention = _section(html, "retention")
    # The real numbers from the code: brain idle 900 s, state TTL 30 days, tts_seen 30 days,
    # diagnostics 30 days (DIAG_RETENTION_DAYS, brain trace sweep).
    assert "15 minutes" in retention and retention.count("30 days") == 3
    assert "stop using MicMic for 30 days" in html                   # the short version too
    changes = _section(html, "changes")
    assert "at least 14 days before it takes effect" in changes
    assert '<p class="meta">Version 2026-10-05. Last updated 5 October 2026.' in html
    assert "18 or older" in _section(html, "children")


def test_legal_pages_without_a_support_email_still_render(make_proxy):
    for path in ("/privacy", "/terms"):
        html = _legal(make_proxy(), path)
        assert "mailto:" not in html, path
        assert "contact us." in html or "contact us first." in html, path


def test_privacy_describes_diagnostics_as_the_app_and_servers_do_them(make_proxy):
    """The on-device turn log, Share diagnostics (opt-in, off in every build; TestFlight asks once),
    the 30 days, the brain's own trace, no audio, and how to stop and delete it."""
    from proxy.config import Config
    html = _legal(make_proxy(), "/privacy")
    sec = _section(html, "diagnostics")
    assert "last 200 requests" in sec and "Recent activity" in sec          # TurnLog.capacity
    assert "It never contains audio" in sec and "never the sound itself" in sec
    assert "only if <strong>Share diagnostics</strong> is on" in sec
    assert "our server also keeps its own record" in sec                    # brain trace.jsonl
    assert "30 days" in sec and Config.__dataclass_fields__["diag_retention_days"].default == 30
    # Consent is the legal basis, so it is never on by default (a pre-ticked box is not
    # consent): the page must not say any build shares from the start.
    assert "off until you turn it on, in every version of MicMic" in sec and "TestFlight" in sec
    assert "asks you once" in sec and "nothing is sent unless you tap Share" in sec
    assert "on from the start" not in html
    assert "Delete my account and data" in sec and "including names" in sec
    assert "never for advertising or to train AI models" in sec
    # Every blanket "we keep nothing" sentence names the exception.
    assert "unless you choose to share diagnostics, we keep none of what you say" in html
    assert "The exception is diagnostics you choose to share." in _section(html, "retention")
    assert "Share diagnostics" in _section(html, "iphone")
    assert "any diagnostics you shared" in _section(html, "delete")
    assert "Share diagnostics" in _section(html, "stored")
    assert "sharing diagnostics when you turn that on" in _section(html, "purposes")
