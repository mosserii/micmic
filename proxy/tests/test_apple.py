"""POST /v1/apple/transaction: the iPhone proves MicMic Pro with a StoreKit 2 signed
transaction (PURCHASES_ACCOUNT.md 2.5, 2.7, tests 1 to 15 of 7.1).

Apple's real SignedDataVerifier checks every JWS here, against a fake Apple root
(apple_fakes.py) with online revocation checks off. Nothing reaches Apple."""
from __future__ import annotations

import pytest
from appstoreserverlibrary.signed_data_verifier import (VerificationException,
                                                        VerificationStatus)

from apple_fakes import (APP_ID, PRODUCT, FakePKI, new_otid, tamper_payload,
                         tamper_signature, tx_payload)
from conftest import ios_signup, jev_body
from proxy.config import Config
from proxy.store import hash_token

pytestmark = pytest.mark.usefixtures("apple_leak_check")


def apple_proxy(make_proxy, pki, **over):
    cfg = dict(apple_root_certs_dir=pki.roots_dir, apple_online_checks=False,
               apple_app_id=APP_ID)
    cfg.update(over)
    return make_proxy(**cfg)


def now_s(p) -> int:
    return int(p.clock.now.timestamp())


def post_tx(p, token, jws, source="purchase"):
    s, d, _, h = p.request("POST", "/v1/apple/transaction", {"jws": jws, "source": source},
                           token=token)
    return s, d


def account(p, token):
    return p.request("GET", "/v1/account", token=token)[1]


# 1
def test_valid_production_transaction_makes_the_device_pro(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    t = d["token"]
    otid = new_otid()
    s, out = post_tx(p, t, apple_pki.sign(tx_payload(now=now_s(p), otid=otid, env="Production")))
    assert s == 200, out
    assert out == {"plan": "pro", "source": "apple", "expires_at": "2026-10-24T12:00:00Z",
                   "environment": "Production", "auto_renew": True}
    a = account(p, t)
    assert a["plan"] == "pro" and a["pro_source"] == "apple" and a["platform"] == "ios"
    assert a["apple"]["product_id"] == PRODUCT and a["apple"]["status"] == "active"
    assert a["apple"]["environment"] == "Production"
    assert a["apple"]["expires_at"] == "2026-10-24T12:00:00Z"
    assert len(a["apple"]["original_transaction_id_hash"]) == 64
    assert otid not in str(a)
    # The Pro allowance (the test proxy's pro_daily_calls) now applies.
    assert a["usage"]["jev"]["limit"] == 50


# 2
def test_sandbox_accepted_by_default_and_refused_when_not_listed(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    assert post_tx(p, d["token"], apple_pki.sign(tx_payload(now=now_s(p))))[0] == 200
    p2 = apple_proxy(make_proxy, apple_pki, apple_environments=("Production",))
    _, d2 = ios_signup(p2)
    s, out = post_tx(p2, d2["token"], apple_pki.sign(tx_payload(now=now_s(p2))))
    assert (s, out) == (400, {"error": "bad_environment"})
    assert account(p2, d2["token"])["plan"] == "free"


# 3
def test_xcode_transactions_only_outside_production(make_proxy, apple_pki, monkeypatch, tmp_path):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    jws = apple_pki.sign(tx_payload(now=now_s(p), env="Xcode"))
    assert post_tx(p, d["token"], jws) == (400, {"error": "bad_environment"})
    staging = apple_proxy(make_proxy, apple_pki, environment="staging",
                          apple_environments=("Production", "Sandbox", "Xcode"))
    _, d2 = ios_signup(staging)
    assert post_tx(staging, d2["token"], apple_pki.sign(
        tx_payload(now=now_s(staging), env="Xcode")))[0] == 200
    # Production with the flag never boots: those transactions are not signed by Apple.
    cfg = Config(jev_key="k", environment="production",
                 apple_environments=("Production", "Sandbox", "Xcode"))
    assert any("unsigned" in e for e in cfg.boot_errors())
    cfg = Config(jev_key="k", environment="staging",
                 apple_environments=("Production", "Sandbox", "Xcode"))
    assert cfg.boot_errors() == []
    assert any("APPLE_ENVIRONMENTS" in e for e in
               Config(jev_key="k", apple_environments=("Prod",)).boot_errors())


# 4
def test_production_refused_until_the_app_apple_id_is_set(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki, apple_app_id=None)
    assert p.srv.apple.configured and not p.srv.apple.production_on
    _, d = ios_signup(p)
    s, out = post_tx(p, d["token"], apple_pki.sign(tx_payload(now=now_s(p), env="Production")))
    assert (s, out) == (400, {"error": "bad_environment"})
    assert post_tx(p, d["token"], apple_pki.sign(tx_payload(now=now_s(p))))[0] == 200


# 5
def test_wrong_bundle_id_is_a_bad_transaction(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    for env in ("Sandbox", "Production"):
        jws = apple_pki.sign(tx_payload(now=now_s(p), env=env, bundle="com.example.other"))
        assert post_tx(p, d["token"], jws) == (400, {"error": "bad_transaction"})
    assert account(p, d["token"])["plan"] == "free"
    # The app's Apple ID is carried by notifications, not transactions: see
    # test_apple_notifications.py::test_wrong_app_apple_id_is_a_bad_signature.


# 6
def test_unknown_product_and_non_subscription(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    for over in ({"product": "com.betterfly.micmic.ios.coins"},
                 {"tx_type": "Non-Consumable"}, {"tx_type": "Consumable"}):
        jws = apple_pki.sign(tx_payload(now=now_s(p), **over))
        assert post_tx(p, d["token"], jws) == (400, {"error": "unknown_product"}), over
    jws = apple_pki.sign(tx_payload(now=now_s(p), ownership="FAMILY_SHARED"))
    assert post_tx(p, d["token"], jws) == (400, {"error": "bad_transaction"})


# 7
def test_expired_and_revoked_are_409_and_change_nothing(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    t, n = d["token"], now_s(p)
    jws = apple_pki.sign(tx_payload(now=n, expires_in=-2 * 3600))
    assert post_tx(p, t, jws) == (409, {"error": "expired"})
    jws = apple_pki.sign(tx_payload(now=n, revoked_at=n - 60))
    assert post_tx(p, t, jws) == (409, {"error": "revoked"})
    a = account(p, t)
    assert a["plan"] == "free" and a["apple"] is None
    # Nothing is stored about a subscription the server never saw alive.
    assert p.store._conn().execute("SELECT COUNT(*) FROM apple_subscriptions").fetchone()[0] == 0
    # A known subscription that is then revoked is recorded and ends Pro at once.
    otid = new_otid()
    assert post_tx(p, t, apple_pki.sign(tx_payload(now=n, otid=otid, signed_at=n - 10)))[0] == 200
    assert post_tx(p, t, apple_pki.sign(tx_payload(now=n, otid=otid, revoked_at=n)))[0] == 409
    a = account(p, t)
    assert a["plan"] == "free" and a["apple"]["status"] == "revoked"


# 8
def test_forged_and_broken_signatures_are_refused(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    n = now_s(p)
    good = apple_pki.sign(tx_payload(now=n))
    foreign = FakePKI("Not Apple Root CA")
    cases = [tamper_payload(good, expiresDate=(n + 10 * 365 * 86400) * 1000),
             tamper_signature(good),
             foreign.sign(tx_payload(now=n)),
             apple_pki.sign(tx_payload(now=n), x5c=False),
             "not.a.jws", "abc"]
    for jws in cases:
        s, out = post_tx(p, d["token"], jws)
        assert s == 400 and out["error"] in ("bad_transaction", "bad_request"), out
    for body in ({}, {"jws": 5}, {"jws": good, "source": "gift"}):
        assert p.request("POST", "/v1/apple/transaction", body, token=d["token"])[0] == 400
    assert account(p, d["token"])["plan"] == "free"
    # Oversized bodies are refused before any parsing.
    s, _, _, _ = p.request("POST", "/v1/apple/transaction",
                           {"jws": "x" * 20000}, token=d["token"])
    assert s == 413


# 9
def test_retryable_verification_failure_is_503(make_proxy, apple_pki, monkeypatch):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)

    def ocsp_down(_jws):
        raise VerificationException(VerificationStatus.RETRYABLE_VERIFICATION_FAILURE)
    monkeypatch.setattr(p.srv.apple.verifiers["Sandbox"],
                        "verify_and_decode_signed_transaction", ocsp_down)
    s, out, _, h = p.request("POST", "/v1/apple/transaction",
                             {"jws": apple_pki.sign(tx_payload(now=now_s(p))),
                              "source": "update"}, token=d["token"])
    assert (s, out) == (503, {"error": "apple_verify_retry", "retry_after_s": 30})
    assert h["Retry-After"] == "30"


# 10
def test_one_subscription_unlocks_at_most_five_devices(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki, max_devices_per_ip_per_day=10)
    otid = new_otid()
    tokens = []
    for _ in range(6):
        _, d = ios_signup(p)
        tokens.append(d["token"])
        jws = apple_pki.sign(tx_payload(now=now_s(p), otid=otid))
        assert post_tx(p, d["token"], jws, "restore")[0] == 200
    plans = [account(p, t)["plan"] for t in tokens]
    assert plans == ["free", "pro", "pro", "pro", "pro", "pro"]
    # The first one restores again: it is Pro, and now the second is the one unlinked.
    assert post_tx(p, tokens[0], apple_pki.sign(tx_payload(now=now_s(p), otid=otid)))[0] == 200
    assert [account(p, t)["plan"] for t in tokens] == ["pro", "free", "pro", "pro", "pro", "pro"]


# 11
def test_an_older_transaction_never_overwrites_a_newer_state(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    n, otid = now_s(p), new_otid()
    new = apple_pki.sign(tx_payload(now=n, otid=otid, expires_in=60 * 86400, signed_at=n))
    old = apple_pki.sign(tx_payload(now=n, otid=otid, expires_in=30 * 86400, signed_at=n - 3600))
    assert post_tx(p, d["token"], new)[1]["expires_at"] == "2026-11-23T12:00:00Z"
    s, out = post_tx(p, d["token"], old, "launch")
    assert s == 200 and out["expires_at"] == "2026-11-23T12:00:00Z"
    assert account(p, d["token"])["apple"]["expires_at"] == "2026-11-23T12:00:00Z"


def test_a_late_renewal_still_extends_pro(make_proxy, apple_pki):
    """The DID_RENEW notification was lost; a newer notification (auto-renew turned
    off) arrived first. The renewal the phone posts later, signed before that
    notification, still proves the new period."""
    from apple_fakes import notification
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    n, otid = now_s(p), new_otid()
    assert post_tx(p, d["token"], apple_pki.sign(tx_payload(
        now=n, otid=otid, expires_in=86400, signed_at=n - 100)))[0] == 200
    signed = notification(apple_pki, "DID_CHANGE_RENEWAL_STATUS", now=n, subtype=
                          "AUTO_RENEW_DISABLED", signed_at=n + 50,
                          tx=tx_payload(now=n, otid=otid, expires_in=86400))
    assert p.request("POST", "/v1/apple/notifications", {"signedPayload": signed})[0] == 200
    renewal = apple_pki.sign(tx_payload(now=n, otid=otid, expires_in=31 * 86400, signed_at=n))
    s, out = post_tx(p, d["token"], renewal, "update")
    assert s == 200 and out["expires_at"] == "2026-10-25T12:00:00Z"
    # A refund cannot be undone that way.
    signed = notification(apple_pki, "REFUND", now=n, signed_at=n + 60,
                          tx=tx_payload(now=n, otid=otid, revoked_at=n + 60))
    assert p.request("POST", "/v1/apple/notifications", {"signedPayload": signed})[0] == 200
    later = apple_pki.sign(tx_payload(now=n, otid=otid, expires_in=62 * 86400, signed_at=n + 1))
    assert post_tx(p, d["token"], later, "update") == (409, {"error": "revoked"})
    assert account(p, d["token"])["plan"] == "free"


# 12
def test_pro_ends_by_itself_at_expiry_plus_leeway(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    t = d["token"]
    assert post_tx(p, t, apple_pki.sign(tx_payload(now=now_s(p), expires_in=86400)))[0] == 200
    assert account(p, t)["plan"] == "pro"
    import datetime as dt
    p.clock.now += dt.timedelta(days=1, minutes=59)          # inside the one-hour leeway
    assert account(p, t)["plan"] == "pro"
    p.clock.now += dt.timedelta(minutes=2)
    a = account(p, t)
    assert a["plan"] == "free" and a["pro_source"] is None and a["apple"]["status"] == "expired"
    # The meter follows: free limits again.
    assert a["usage"]["jev"]["limit"] == 5


# 13
def test_stripe_and_apple_never_demote_each_other(make_proxy, apple_pki):
    import datetime as dt
    from test_service import STRIPE_CFG, event, post_event, sub_obj
    p = apple_proxy(make_proxy, apple_pki, **STRIPE_CFG)
    # A Mac paid with Stripe; an Apple subscription that it is somehow linked to expires.
    mac = p.mint("free")
    mac_dev = p.store.account(hash_token(mac))["device_id"]
    body = event("customer.subscription.updated", sub_obj(mac_dev, "active"), created=1)
    assert post_event(p, body)[0] == 200
    assert account(p, mac)["pro_source"] == "stripe"
    n = now_s(p)
    assert post_tx(p, mac, apple_pki.sign(tx_payload(now=n, expires_in=60)))[0] == 200
    p.clock.now += dt.timedelta(hours=3)
    assert account(p, mac)["plan"] == "pro"
    # An Apple-Pro iPhone; Stripe deletes some other subscription: still Pro.
    _, d = ios_signup(p)
    assert post_tx(p, d["token"], apple_pki.sign(tx_payload(now=now_s(p))))[0] == 200
    body = event("customer.subscription.deleted", sub_obj(None, "canceled", id="sub_other"),
                 created=2)
    assert post_event(p, body)[0] == 200
    assert account(p, d["token"])["plan"] == "pro"
    assert account(p, d["token"])["pro_source"] == "apple"


# 14
def test_checkout_refused_on_iphone_and_already_pro_with_apple(make_proxy, apple_pki):
    from test_service import STRIPE_CFG
    p = apple_proxy(make_proxy, apple_pki, **STRIPE_CFG)
    _, d = ios_signup(p)
    s, out, _, _ = p.request("POST", "/v1/checkout", token=d["token"])
    assert (s, out) == (403, {"error": "use_in_app_purchase"})
    mac = p.mint("free")
    assert post_tx(p, mac, apple_pki.sign(tx_payload(now=now_s(p))))[0] == 200
    s, out, _, _ = p.request("POST", "/v1/checkout", token=mac)
    assert (s, out) == (409, {"error": "already_pro"})


# 15
def test_logs_never_hold_the_jws_the_transaction_id_or_the_token(make_proxy, apple_pki,
                                                                    log_buffer):
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    start = log_buffer.tell()
    otid = new_otid()
    jws = apple_pki.sign(tx_payload(now=now_s(p), otid=otid))
    assert post_tx(p, d["token"], jws)[0] == 200
    post_tx(p, d["token"], tamper_signature(jws))
    post_tx(p, d["token"], apple_pki.sign(tx_payload(now=now_s(p), otid=otid,
                                                     product="x.y.z")))
    text = log_buffer.getvalue()[start:]
    assert "apple tx env=Sandbox source=purchase sub=" in text
    assert jws[:40] not in text and jws.split(".")[1][:32] not in text
    assert otid not in text and d["token"] not in text
    assert d["apple_account_token"] not in text
    # The line names the subscription by an 8-hex prefix of its hash, nothing longer.
    line = next(x for x in text.splitlines() if "apple tx env=" in x)
    assert len(line.split("sub=")[1].split(":")[0]) == 8


def test_apple_routes_answer_503_when_apple_is_not_configured(make_proxy, tmp_path):
    p = make_proxy(apple_root_certs_dir=tmp_path / "none")
    t = p.mint()
    s, out, _, _ = p.request("POST", "/v1/apple/transaction", {"jws": "a.b.c"}, token=t)
    assert (s, out) == (503, {"error": "apple_unavailable"})
    s, out, _, _ = p.request("POST", "/v1/apple/notifications", {})
    assert (s, out) == (503, {"error": "apple_unavailable"})
    assert p.request("POST", "/v1/apple/transaction", {"jws": "a.b.c"})[0] == 401


def test_committed_roots_are_apples(tmp_path):
    """proxy/certs holds exactly Apple's three public roots, by SHA-256 fingerprint
    (checked against apple.com/certificateauthority and macOS's system roots)."""
    import hashlib

    from proxy.apple import load_roots
    from proxy.config import PROXY_ROOT
    want = {"63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179",   # G3
            "c2b9b042dd57830e7d117dac55ac8ae19407d38e41d88f3215bc3a890444a050",   # G2
            "b0b1730ecbc7ff4505142c49f1295e6eda6bcaed7e2c68c5be91b5a11001f024"}   # Apple Root CA
    assert {hashlib.sha256(r).hexdigest() for r in load_roots(PROXY_ROOT / "certs")} == want
    # The default deploy builds a Sandbox verifier from them, and Production once the
    # app's Apple ID is set.
    from proxy.apple import Apple
    assert Apple(Config(jev_key="k")).configured
    assert not Apple(Config(jev_key="k")).production_on
    assert Apple(Config(jev_key="k", apple_app_id=APP_ID)).production_on


def test_free_meter_is_untouched_by_a_failed_proof(make_proxy, apple_pki):
    p = apple_proxy(make_proxy, apple_pki, free_scope="total")
    _, d = ios_signup(p)
    post_tx(p, d["token"], "a.b.c")
    assert p.request("POST", "/v1/jev", jev_body(), token=d["token"])[0] == 200
    assert account(p, d["token"])["plan"] == "free"
