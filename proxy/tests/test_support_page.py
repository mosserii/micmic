"""The /support page, the App Store Support URL (APPSTORE_PLAN.md D11, brief B4)."""
from __future__ import annotations

import re


def _get(p, path):
    s, _, raw, h = p.request("GET", path)
    return s, raw.decode("utf-8"), h


def test_support_page_answers_like_the_other_pages(make_proxy):
    p = make_proxy(support_email="help@micmic.example")
    s, html, h = _get(p, "/support")
    assert s == 200 and h["Content-Type"] == "text/html; charset=utf-8"
    assert h["Content-Security-Policy"].startswith("default-src 'none'")
    assert h["X-Frame-Options"] == "DENY"
    assert "<script" not in html.lower() and "{{" not in html
    assert html.count("<h1") == 1 and "<title>MicMic support</title>" in html
    # The same page frame as /terms.
    terms = _get(p, "/terms")[1]
    assert html.split("<main")[0].split("<title>")[0] == terms.split("<main")[0].split("<title>")[0]
    assert 'class="doc"' in html and "Made by BetterFly AI LTD." in html
    s, _, _, h2 = p.request("HEAD", "/support")
    assert s == 200 and int(h2["Content-Length"]) == len(html.encode("utf-8"))


def test_support_page_says_how_to_reach_a_person_delete_and_cancel(make_proxy):
    p = make_proxy(support_email="help@micmic.example")
    html = _get(p, "/support")[1]
    assert 'href="mailto:help@micmic.example"' in html
    assert 'href="/terms"' in html and 'href="/privacy"' in html
    # Delete, on iPhone (the exact in-app label) and on Mac.
    assert "Delete my account and data" in html and "Settings, then Account" in html
    assert "On Mac:</strong> write to us" in html
    assert "does not cancel a subscription bought through Apple" in html
    # Cancel an Apple subscription, and restore it.
    assert "Subscriptions, then MicMic Pro, then Cancel Subscription" in html
    assert 'href="https://apps.apple.com/account/subscriptions"' in html
    assert "24 hours before the renewal date" in html
    assert "Restore purchases" in html and "reportaproblem.apple.com" in html
    assert "18 or over" in html


def test_support_page_rules(make_proxy):
    """English only, no dashes a reader sees, no Siri, no price, nothing loaded from
    elsewhere, and the only links off the site are Apple's own pages."""
    p = make_proxy(support_email="help@micmic.example", pro_price_label="$8/month")
    html = _get(p, "/support")[1]
    text = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    assert "—" not in text and "–" not in text
    assert "siri" not in text.lower()
    assert "$" not in text and "€" not in text and "7.99" not in text
    assert "Apple ID" not in text                    # Apple calls it Apple Account now
    assert not re.search("[֐-ۿЀ-ӿ]", text)
    assert not re.search(r'src="(?!/static/)', html)
    offsite = set(re.findall(r'href="(https?://[^"]+)"', html))
    assert offsite == {"https://apps.apple.com/account/subscriptions",
                       "https://reportaproblem.apple.com"}


def test_support_page_without_an_email_still_renders(make_proxy):
    html = _get(make_proxy(support_email=None), "/support")[1]
    assert "Write to us." in html and "mailto:" not in html
