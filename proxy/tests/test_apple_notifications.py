"""POST /v1/apple/notifications: App Store Server Notifications V2 (PURCHASES_ACCOUNT.md
2.6, tests 16 to 23 of 7.1). Every payload is signed by the fake Apple PKI and checked by
Apple's real verifier."""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from apple_fakes import (APP_ID, BUNDLE, FakePKI, new_otid, notification, renewal_payload,
                         tamper_signature, tx_payload)
from conftest import ios_signup
from test_apple import account, apple_proxy, now_s, post_tx

pytestmark = pytest.mark.usefixtures("apple_leak_check")


def notify(p, signed: str):
    s, d, _, _ = p.request("POST", "/v1/apple/notifications", {"signedPayload": signed})
    return s, d


def events(p) -> list[tuple]:
    return p.store._conn().execute(
        "SELECT type, subtype, outcome FROM apple_events ORDER BY received_at, rowid").fetchall()


@pytest.fixture
def pro_phone(make_proxy, apple_pki):
    """An iPhone that bought Pro (the app posted the transaction), and its subscription."""
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    n, otid = now_s(p), new_otid()
    tx = tx_payload(now=n, otid=otid, signed_at=n - 100,
                    app_account_token=d["apple_account_token"])
    assert post_tx(p, d["token"], apple_pki.sign(tx))[0] == 200
    return p, d["token"], otid, d["apple_account_token"]


def send(p, pki, ntype, otid, subtype=None, tok=None, renewal=None, signed_at=None, **tx_over):
    n = now_s(p)
    tx = tx_payload(now=n, otid=otid, app_account_token=tok, **tx_over)
    return notify(p, notification(pki, ntype, now=n, tx=tx, subtype=subtype, renewal=renewal,
                                  signed_at=signed_at))


# 16: each row of the table in 2.6
def test_subscribed_and_did_renew_keep_pro_with_the_new_expiry(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    assert send(p, apple_pki, "SUBSCRIBED", otid, "INITIAL_BUY", tok) == (200, {"received": True})
    assert account(p, t)["plan"] == "pro"
    assert send(p, apple_pki, "DID_RENEW", otid, None, tok, expires_in=61 * 86400)[0] == 200
    a = account(p, t)
    assert a["plan"] == "pro" and a["apple"]["expires_at"] == "2026-11-24T12:00:00Z"
    assert send(p, apple_pki, "DID_RENEW", otid, "BILLING_RECOVERY", tok)[0] == 200
    assert [e[2] for e in events(p)] == ["active", "active", "active"]


def test_offer_and_extension_move_the_expiry(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    for ntype in ("OFFER_REDEEMED", "RENEWAL_EXTENDED", "RENEWAL_EXTENSION"):
        days = {"OFFER_REDEEMED": 40, "RENEWAL_EXTENDED": 45, "RENEWAL_EXTENSION": 50}[ntype]
        assert send(p, apple_pki, ntype, otid, None, tok, expires_in=days * 86400)[0] == 200
        a = account(p, t)
        assert a["plan"] == "pro"
        assert a["apple"]["expires_at"] == (dt.datetime(2026, 9, 24, 12, tzinfo=dt.timezone.utc)
                                            + dt.timedelta(days=days)).strftime(
                                                "%Y-%m-%dT%H:%M:%SZ")


def test_auto_renew_off_keeps_pro_until_expiry(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    assert send(p, apple_pki, "DID_CHANGE_RENEWAL_STATUS", otid, "AUTO_RENEW_DISABLED", tok)[0] == 200
    a = account(p, t)
    assert a["plan"] == "pro" and a["apple"]["auto_renew"] is False
    assert send(p, apple_pki, "DID_CHANGE_RENEWAL_STATUS", otid, "AUTO_RENEW_ENABLED", tok)[0] == 200
    assert account(p, t)["apple"]["auto_renew"] is True
    assert [e[2] for e in events(p)] == ["auto_renew_off", "auto_renew_on"]


def test_renewal_preference_changes_nothing_else(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    ren = renewal_payload(now=now_s(p), otid=otid)
    ren["autoRenewProductId"] = "com.betterfly.micmic.ios.pro.yearly"
    assert send(p, apple_pki, "DID_CHANGE_RENEWAL_PREF", otid, "UPGRADE", tok, ren)[0] == 200
    assert account(p, t)["plan"] == "pro"
    row = p.store._conn().execute(
        "SELECT auto_renew_product_id, status FROM apple_subscriptions").fetchone()
    assert row == ("com.betterfly.micmic.ios.pro.yearly", "active")


def test_grace_period_then_grace_period_expired(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    # The period ends in a minute; two hours later (past the leeway) the renewal fails
    # and Apple opens a 16-day grace period, which keeps Pro.
    assert send(p, apple_pki, "DID_RENEW", otid, None, tok, expires_in=60)[0] == 200
    p.clock.now += dt.timedelta(hours=2)
    n = now_s(p)
    ren = renewal_payload(now=n, otid=otid, grace_until=n + 16 * 86400)
    assert send(p, apple_pki, "DID_FAIL_TO_RENEW", otid, "GRACE_PERIOD", tok, ren,
                expires_in=-7140)[0] == 200
    a = account(p, t)
    assert a["plan"] == "pro" and a["apple"]["status"] == "grace"
    assert a["apple"]["grace_until"] == "2026-10-10T14:00:00Z"
    p.clock.now += dt.timedelta(days=10)
    assert account(p, t)["plan"] == "pro"
    assert send(p, apple_pki, "GRACE_PERIOD_EXPIRED", otid, None, tok, expires_in=-60)[0] == 200
    a = account(p, t)
    assert a["plan"] == "free" and a["apple"]["status"] == "expired"
    # Lazy too: with no GRACE_PERIOD_EXPIRED at all, Pro ends at grace_until + leeway.
    assert send(p, apple_pki, "DID_FAIL_TO_RENEW", otid, "GRACE_PERIOD", tok,
                renewal_payload(now=now_s(p), otid=otid, grace_until=now_s(p) + 86400),
                expires_in=-60)[0] == 200
    assert account(p, t)["plan"] == "pro"
    p.clock.now += dt.timedelta(days=1, hours=1, minutes=1)
    assert account(p, t)["plan"] == "free"


def test_billing_retry_without_grace_ends_pro_at_expiry(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    # The current period ends in three hours; its renewal fails, with no grace period.
    assert send(p, apple_pki, "DID_RENEW", otid, None, tok, expires_in=3 * 3600)[0] == 200
    assert send(p, apple_pki, "DID_FAIL_TO_RENEW", otid, None, tok, expires_in=3 * 3600,
                signed_at=now_s(p) + 1)[0] == 200
    a = account(p, t)
    assert a["plan"] == "pro" and a["apple"]["status"] == "billing_retry"
    p.clock.now += dt.timedelta(hours=4, minutes=1)
    assert account(p, t)["plan"] == "free"


def test_expired_ends_pro(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    for sub in ("VOLUNTARY", "BILLING_RETRY", "PRICE_INCREASE", "PRODUCT_NOT_FOR_SALE"):
        assert send(p, apple_pki, "EXPIRED", otid, sub, tok, expires_in=-60)[0] == 200
        assert account(p, t)["plan"] == "free"
        # A newer purchase brings it back (RESUBSCRIBE).
        assert send(p, apple_pki, "SUBSCRIBED", otid, "RESUBSCRIBE", tok)[0] == 200
        assert account(p, t)["plan"] == "pro"


def test_refund_before_expiry_ends_pro_at_once_and_reversal_restores_it(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    n = now_s(p)
    for kind in ("REFUND", "REVOKE"):
        assert send(p, apple_pki, kind, otid, None, tok, revoked_at=n)[0] == 200
        a = account(p, t)
        assert a["plan"] == "free" and a["apple"]["status"] == "revoked"
        assert send(p, apple_pki, "REFUND_REVERSED", otid, None, tok)[0] == 200
        assert account(p, t)["plan"] == "pro"


def test_informational_types_are_ignored(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    for ntype in ("PRICE_INCREASE", "PRICE_CHANGE", "REFUND_DECLINED", "CONSUMPTION_REQUEST",
                  "METADATA_UPDATE", "MIGRATION", "ONE_TIME_CHARGE", "RESCIND_CONSENT"):
        assert send(p, apple_pki, ntype, otid, None, tok)[0] == 200
    assert {e[2] for e in events(p)} == {"ignored"}
    assert account(p, t)["plan"] == "pro"


# 17
def test_duplicate_notification_changes_nothing(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    n = now_s(p)
    signed = notification(apple_pki, "REFUND", now=n, uuid_="dup-1",
                          tx=tx_payload(now=n, otid=otid, revoked_at=n))
    assert notify(p, signed)[0] == 200
    assert send(p, apple_pki, "REFUND_REVERSED", otid, None, tok, signed_at=n + 5)[0] == 200
    assert account(p, t)["plan"] == "pro"
    assert notify(p, signed) == (200, {"received": True})        # Apple's redelivery
    assert account(p, t)["plan"] == "pro"
    assert [e[2] for e in events(p)] == ["revoked", "active"]


# 18
def test_stale_notification_is_recorded_and_changes_nothing_except_refund(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    n = now_s(p)
    assert send(p, apple_pki, "DID_RENEW", otid, None, tok, signed_at=n + 100,
                expires_in=60 * 86400)[0] == 200
    assert send(p, apple_pki, "EXPIRED", otid, "VOLUNTARY", tok, signed_at=n + 50,
                expires_in=-60)[0] == 200
    assert account(p, t)["plan"] == "pro"
    assert events(p)[-1][2] == "stale"
    # REFUND always wins, however late it is signed.
    assert send(p, apple_pki, "REFUND", otid, None, tok, signed_at=n + 10, revoked_at=n)[0] == 200
    assert account(p, t)["plan"] == "free"


# 19
def test_subscribed_before_the_app_posts_finds_the_device_by_app_account_token(
        make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    otid = new_otid()
    assert send(p, apple_pki, "SUBSCRIBED", otid, "INITIAL_BUY",
                d["apple_account_token"])[0] == 200
    a = account(p, d["token"])
    assert a["plan"] == "pro" and a["pro_source"] == "apple"
    # The token in the JWS may be upper case (Swift's UUID.uuidString).
    _, d2 = ios_signup(p)
    otid2 = new_otid()
    assert send(p, apple_pki, "SUBSCRIBED", otid2, "INITIAL_BUY",
                d2["apple_account_token"].upper())[0] == 200
    assert account(p, d2["token"])["plan"] == "pro"


# 20
def test_unknown_subscription_is_recorded_without_a_row(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    ios_signup(p)
    stranger = str(uuid.uuid4())
    from apple_fakes import SECRET_IDS
    SECRET_IDS.append(stranger)
    assert send(p, apple_pki, "SUBSCRIBED", new_otid(), "INITIAL_BUY", stranger)[0] == 200
    assert send(p, apple_pki, "DID_RENEW", new_otid())[0] == 200
    assert [e[2] for e in events(p)] == ["unknown_subscription", "unknown_subscription"]
    c = p.store._conn()
    assert c.execute("SELECT COUNT(*) FROM apple_subscriptions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM apple_links").fetchone()[0] == 0


# 21
def test_storage_error_is_500_and_the_retry_applies_it(pro_phone, apple_pki, monkeypatch):
    p, t, otid, tok = pro_phone
    n = now_s(p)
    signed = notification(apple_pki, "REFUND", now=n, tx=tx_payload(now=n, otid=otid,
                                                                    revoked_at=n))
    real = p.store._write_apple

    def disk_full(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(p.store, "_write_apple", disk_full)
    s, out = notify(p, signed)
    assert (s, out) == (500, {"error": "internal"})
    assert events(p) == [] and account(p, t)["plan"] == "pro"
    monkeypatch.setattr(p.store, "_write_apple", real)
    assert notify(p, signed)[0] == 200
    assert account(p, t)["plan"] == "free" and events(p) == [("REFUND", None, "revoked")]


# 22
def test_test_notification_is_acknowledged_and_logged(make_proxy, apple_pki, log_buffer):
    p = apple_proxy(make_proxy, apple_pki)
    start = log_buffer.tell()
    assert notify(p, notification(apple_pki, "TEST", now=now_s(p), tx=None)) == (
        200, {"received": True})
    assert "apple TEST notification ok" in log_buffer.getvalue()[start:]
    assert events(p) == [("TEST", None, "test")]


# 23
def test_bad_signature_is_400_and_not_configured_is_503(make_proxy, apple_pki, tmp_path):
    p = apple_proxy(make_proxy, apple_pki)
    n = now_s(p)
    good = notification(apple_pki, "TEST", now=n, tx=None)
    foreign = notification(FakePKI("Not Apple"), "TEST", now=n, tx=None)
    for bad in (tamper_signature(good), foreign, "x.y.z", "nope"):
        assert notify(p, bad) == (400, {"error": "bad_signature"})
    for body in ({}, {"signedPayload": 5}, {"signedPayload": ""}):
        s, out, _, _ = p.request("POST", "/v1/apple/notifications", body)
        assert (s, out) == (400, {"error": "bad_request"})
    assert events(p) == []
    off = make_proxy(apple_root_certs_dir=tmp_path / "none")
    assert notify(off, good) == (503, {"error": "apple_unavailable"})


def test_wrong_app_apple_id_or_bundle_is_a_bad_signature(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    n = now_s(p)
    tx = tx_payload(now=n, env="Production")
    assert notify(p, notification(apple_pki, "DID_RENEW", now=n, tx=tx, env="Production",
                                  app_id=APP_ID + 1)) == (400, {"error": "bad_signature"})
    assert notify(p, notification(apple_pki, "TEST", now=n, tx=None,
                                  bundle="com.example.other")) == (400, {"error": "bad_signature"})
    ok = notification(apple_pki, "TEST", now=n, tx=None, env="Production")
    assert notify(p, ok) == (200, {"received": True})


def test_production_notifications_wait_for_the_app_apple_id(make_proxy, apple_pki):
    """Apple retries a 503 for days: nothing is lost while APPLE_APP_ID is being set."""
    p = apple_proxy(make_proxy, apple_pki, apple_app_id=None)
    n = now_s(p)
    signed = notification(apple_pki, "TEST", now=n, tx=None, env="Production")
    assert notify(p, signed) == (503, {"error": "apple_unavailable"})
    assert notify(p, notification(apple_pki, "TEST", now=n, tx=None)) == (200, {"received": True})


def test_other_products_are_not_ours(pro_phone, apple_pki):
    p, t, otid, tok = pro_phone
    assert send(p, apple_pki, "REFUND", otid, None, tok, product="com.example.other",
                revoked_at=now_s(p))[0] == 200
    assert account(p, t)["plan"] == "pro" and events(p)[-1][2] == "unknown_product"


def test_bundle_id_of_the_fake_matches_the_server_default():
    from proxy.config import APPLE_BUNDLE_ID
    assert BUNDLE == APPLE_BUNDLE_ID
