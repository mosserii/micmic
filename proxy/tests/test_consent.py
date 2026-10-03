"""Terms and AI-data consent at sign-up (PURCHASES_ACCOUNT.md 4.3, tests 24 to 27)."""
from __future__ import annotations

import pytest

from conftest import CONSENT, ios_signup
from proxy.store import hash_token
from test_service import signup


def consent(**over) -> dict:
    c = dict(CONSENT)
    c.update(over)
    return c


# 24
def test_iphone_needs_consent_and_it_is_stored_with_the_server_time(proxy):
    s, d, _, _ = proxy.request("POST", "/v1/devices", {"device_name": "iPhone",
                                                       "app_version": "1.0.0",
                                                       "platform": "ios"})
    assert (s, d) == (400, {"error": "consent_required"})
    for bad_age in (False, None):
        c = consent(age_18=bad_age)
        if bad_age is None:
            del c["age_18"]
        assert ios_signup(proxy, c) == (400, {"error": "consent_required"})
    s, d = ios_signup(proxy)
    assert s == 201 and set(d) == {"token", "device_id", "plan", "apple_account_token"}
    row = proxy.store.account(hash_token(d["token"]))
    assert row["platform"] == "ios"
    assert row["consent_terms"] == row["consent_privacy"] == row["consent_ai"] == "2026-10-03"
    assert row["consent_providers"] == "typesafe,google_gemini,elevenlabs"
    assert row["consent_client_at"] == "2026-09-24T15:00:00+03:00"
    assert row["consent_locale"] == "he-IL" and row["consent_method"] == "checkbox"
    assert row["consent_age18"] == 1
    # The authoritative time is the server's clock, not what the phone said.
    assert row["consent_at"] == int(proxy.clock.now.timestamp())
    # None of the refusals used up the address's sign-ups.
    for _ in range(4):
        assert ios_signup(proxy)[0] == 201
    assert ios_signup(proxy)[1] == {"error": "too_many_devices"}


# 25
def test_mac_signup_without_consent_is_unchanged(proxy):
    s, d, _, _ = signup(proxy)
    assert s == 201 and set(d) == {"token", "device_id", "plan"}
    row = proxy.store.account(hash_token(d["token"]))
    assert row["platform"] == "mac" and row["consent_at"] is None
    # A Mac may send the record too; age is optional there.
    c = consent()
    del c["age_18"]
    s, d, _, _ = proxy.request("POST", "/v1/devices", {"device_name": "Mac", "app_version": "1.3",
                                                       "platform": "mac", "consent": c})
    proxy.tokens.append(d["token"])
    assert s == 201 and proxy.store.account(hash_token(d["token"]))["consent_age18"] is None


# 26
@pytest.mark.parametrize("over", [
    {"terms_version": "3 October 2026"}, {"privacy_version": 20261003},
    {"ai_disclosure_version": None}, {"providers": ["typesafe", "openai"]},
    {"providers": []}, {"providers": "typesafe"}, {"accepted_at": "yesterday"},
    {"accepted_at": "2026-10-04T09:12:33"}, {"accepted_at": "2026-10-04T09:12:33+03:00" + "0" * 40},
    {"locale": "x" * 17}, {"locale": "he\nIL"}, {"method": "voice"}, {"age_18": "yes"},
])
def test_bad_consent_is_400(proxy, over):
    assert ios_signup(proxy, consent(**over))[0] == 400


def test_bad_platform_and_non_object_consent(proxy):
    assert ios_signup(proxy, platform="android")[0] == 400
    s, _, _, _ = proxy.request("POST", "/v1/devices", {"device_name": "iPhone",
                                                       "app_version": "1", "platform": "ios",
                                                       "consent": "yes"})
    assert s == 400


def test_outdated_consent_is_409_with_the_current_versions(make_proxy):
    p = make_proxy(ai_disclosure_required="2026-11-01")
    s, d = ios_signup(p)
    assert s == 409 and d == {"error": "consent_outdated", "consent_required": {
        "terms_version": "2026-10-03", "privacy_version": "2026-10-03",
        "ai_disclosure_version": "2026-11-01"}}
    assert ios_signup(p, consent(ai_disclosure_version="2026-11-01"))[0] == 201


# 27
def test_consent_route_updates_and_account_reports_it(make_proxy):
    p = make_proxy()
    _, d = ios_signup(p)
    t = d["token"]
    a = p.request("GET", "/v1/account", token=t)[1]
    assert a["consent"] == {"terms_version": "2026-10-03", "privacy_version": "2026-10-03",
                            "ai_disclosure_version": "2026-10-03",
                            "accepted_at": "2026-09-24T12:00:00Z"}
    assert a["consent_required"] == {"terms_version": "2026-10-03",
                                     "privacy_version": "2026-10-03",
                                     "ai_disclosure_version": "2026-10-03"}
    # The owner raises the AI disclosure (a new provider): the phone shows the screen
    # again and sends the new record.
    p.cfg.ai_disclosure_version = p.cfg.ai_disclosure_required = "2026-12-01"
    assert p.request("GET", "/v1/account", token=t)[1]["consent_required"][
        "ai_disclosure_version"] == "2026-12-01"
    s, out, _, _ = p.request("POST", "/v1/consent", {"consent": CONSENT}, token=t)
    assert s == 409 and out["error"] == "consent_outdated"
    import datetime as dt
    p.clock.now += dt.timedelta(days=1)
    s, out, _, _ = p.request("POST", "/v1/consent",
                             {"consent": consent(ai_disclosure_version="2026-12-01")}, token=t)
    assert (s, out) == (200, {"ok": True})
    a = p.request("GET", "/v1/account", token=t)[1]
    assert a["consent"]["ai_disclosure_version"] == "2026-12-01"
    assert a["consent"]["accepted_at"] == "2026-09-25T12:00:00Z"
    # Without a token, or with a broken record: nothing changes.
    assert p.request("POST", "/v1/consent", {"consent": CONSENT})[0] == 401
    assert p.request("POST", "/v1/consent", {"consent": {}}, token=t)[0] == 400
    assert p.request("POST", "/v1/consent", {}, token=t)[0] == 400
    s, _, _, _ = p.request("POST", "/v1/consent", {"consent": consent(age_18=False)}, token=t)
    assert s == 400


def test_versions_must_be_dates_at_boot():
    from proxy.config import Config
    assert any("TERMS_REQUIRED" in e for e in
               Config(jev_key="k", terms_required="soon").boot_errors())
    assert Config(jev_key="k").boot_errors() == []
