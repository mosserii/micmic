"""The public service around the meter: sign-up, account, Stripe, pages, health.

Nothing here reaches Stripe. Session creation is replaced by a function returning the
SDK's own objects (stripe.checkout.Session.construct_from), never a dict, and webhook
payloads are signed locally the way Stripe signs them, then verified by the real
stripe.Webhook.construct_event.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
import stripe

from conftest import FAKE_STRIPE_KEY, FAKE_WEBHOOK_SECRET, jev_body
from proxy import app as appmod
from proxy import billing
from proxy.config import Config
from proxy.store import Store, hash_token

WHSEC = FAKE_WEBHOOK_SECRET
SK = FAKE_STRIPE_KEY
PRICE = "price_test_pro_monthly"
BASE = "https://micmic.example"
STRIPE_CFG = dict(stripe_secret_key=SK, stripe_webhook_secret=WHSEC,
                  stripe_price_pro=PRICE, public_base_url=BASE)
DAY = "2026-09-24"


def sign(payload: bytes, secret: str = WHSEC, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def event(etype: str, obj: dict, eid: str | None = None, created: int = 1_790_000_000) -> bytes:
    """A webhook body as Stripe sends it, built through the SDK's own Event type."""
    ev = stripe.Event.construct_from(
        {"id": eid or f"evt_{etype}_{obj.get('id')}_{created}", "object": "event",
         "type": etype, "created": created, "livemode": False,
         "api_version": "2026-08-26.dahlia", "data": {"object": obj}}, SK)
    return json.dumps(ev.to_dict()).encode()


def post_event(p, body: bytes, sig: str | None = "auto"):
    headers = {"Content-Type": "application/json"}
    if sig == "auto":
        sig = sign(body)
    if sig is not None:
        headers["Stripe-Signature"] = sig
    return p.request("POST", "/v1/stripe/webhook", body, headers=headers)


def session_obj(device_id: str, **over) -> dict:
    d = {"id": "cs_test_1", "object": "checkout.session", "mode": "subscription",
         "client_reference_id": device_id, "metadata": {"device_id": device_id},
         "customer": "cus_test_1", "subscription": "sub_test_1",
         "payment_status": "paid", "status": "complete"}
    d.update(over)
    return d


def sub_obj(device_id: str | None, status: str = "active", **over) -> dict:
    d = {"id": "sub_test_1", "object": "subscription", "customer": "cus_test_1",
         "status": status, "cancel_at_period_end": False, "cancel_at": None,
         "metadata": {"device_id": device_id} if device_id else {},
         # Current API versions carry the period on the items.
         "items": {"object": "list", "data": [
             {"id": "si_1", "object": "subscription_item", "current_period_end": 1_792_000_000,
              "price": {"id": PRICE, "object": "price"}}]}}
    d.update(over)
    return d


def signup(p, name="Zohar's MacBook", version="1.0.0", headers=None):
    s, d, raw, h = p.request("POST", "/v1/devices",
                             {"device_name": name, "app_version": version}, headers=headers)
    if s == 201:
        p.tokens.append(d["token"])
    return s, d, raw, h


# ------------------------------------------------------------------ sign-up


def test_signup_gives_a_working_free_token_shown_once(proxy):
    s, d, _, _ = signup(proxy)
    assert s == 201
    assert set(d) == {"token", "device_id", "plan"} and d["plan"] == "free"
    assert d["token"].startswith("mmp_") and d["device_id"].startswith("dev_")
    assert proxy.request("POST", "/v1/jev", jev_body(), token=d["token"])[0] == 200
    s, a, _, _ = proxy.request("GET", "/v1/account", token=d["token"])
    assert s == 200 and a["device_id"] == d["device_id"]
    # Only the hash is stored; the label is what the app called the device.
    raw = b"".join(f.read_bytes() for f in proxy.cfg.db_path.parent.iterdir()
                   if f.name.startswith("proxy.db"))
    assert d["token"].encode() not in raw
    row = proxy.store.account(hash_token(d["token"]))
    assert row["label"] == "Zohar's MacBook" and row["app_version"] == "1.0.0"


def test_five_devices_per_address_per_utc_day(proxy):
    for _ in range(5):
        assert signup(proxy)[0] == 201
    s, d, _, h = signup(proxy)
    assert (s, d) == (429, {"error": "too_many_devices"})
    assert h["Retry-After"] == str(12 * 3600)
    # Without TRUST_PROXY_HEADERS a client cannot pick its own address.
    assert signup(proxy, headers={"X-Forwarded-For": "203.0.113.9"})[0] == 429
    proxy.clock.now = proxy.clock.now.replace(day=25, hour=0, minute=0, second=1)
    assert signup(proxy)[0] == 201


def test_forwarded_address_is_used_only_when_trusted(make_proxy):
    p = make_proxy(trust_proxy_headers=True, max_devices_per_ip_per_day=1)
    # The edge appends the real client after whatever the client sent, then maybe an
    # internal hop. 8.8.4.4 and 1.1.1.1 stand in for public client addresses (the
    # TEST-NET ranges are not "global" to ipaddress).
    xff = lambda ip: {"X-Forwarded-For": f"{ip}, 10.0.0.1"}  # noqa: E731
    assert signup(p, headers=xff("8.8.4.4"))[0] == 201
    assert signup(p, headers=xff("8.8.4.4"))[0] == 429
    assert signup(p, headers=xff("1.1.1.1"))[0] == 201
    # A forged first hop changes nothing: the real address the edge added still counts.
    forged = {"X-Forwarded-For": "9.9.9.9, 8.8.4.4, 10.0.0.1"}
    assert signup(p, headers=forged)[0] == 429
    # X-Real-IP, when the platform sets it, wins.
    assert signup(p, headers={"X-Real-IP": "1.0.0.1", "X-Forwarded-For": "8.8.4.4"})[0] == 201
    # Not an address: counted against the socket peer, like no header at all.
    assert signup(p, headers={"X-Forwarded-For": "nonsense"})[0] == 201
    assert signup(p)[0] == 429


def test_global_signup_ceiling(make_proxy):
    p = make_proxy(trust_proxy_headers=True, max_devices_per_day=2)
    assert signup(p, headers={"X-Forwarded-For": "203.0.113.1"})[0] == 201
    assert signup(p, headers={"X-Forwarded-For": "203.0.113.2"})[0] == 201
    assert signup(p, headers={"X-Forwarded-For": "203.0.113.3"})[1] == {
        "error": "too_many_devices"}


def test_signup_rejects_bad_bodies_and_counts_nothing(proxy):
    for body in ({}, {"device_name": "Mac"}, {"app_version": "1"},
                 {"device_name": 5, "app_version": "1"}, {"device_name": "  ", "app_version": "1"},
                 {"device_name": "Mac", "app_version": ["1"]}, [1], b"{nope"):
        assert proxy.request("POST", "/v1/devices", body)[0] == 400, body
    s, d, _, _ = proxy.request("POST", "/v1/devices",
                               {"device_name": "x" * 5000, "app_version": "1"})
    assert s == 413
    assert proxy.request("GET", "/v1/devices")[0] == 404
    # None of those used up the address's five.
    for _ in range(5):
        assert signup(proxy)[0] == 201


def test_signup_trims_control_characters_and_length(proxy):
    s, d, _, _ = signup(proxy, name="Mac\x1b[31m\nBook" + "y" * 300, version="2.0\x00")
    assert s == 201
    row = proxy.store.account(hash_token(d["token"]))
    assert row["label"].startswith("Mac[31mBook") and len(row["label"]) == 100
    assert row["app_version"] == "2.0"


# ------------------------------------------------------------------ account


def test_account_shape_for_a_free_device(proxy):
    _, d, _, _ = signup(proxy)
    t = d["token"]
    proxy.request("POST", "/v1/jev", jev_body(), token=t)
    s, a, _, _ = proxy.request("GET", "/v1/account", token=t)
    assert s == 200
    assert a == {"device_id": d["device_id"], "plan": "free",
                 "usage": {"jev": {"used": 1, "limit": 5}, "gemini": {"used": 0, "limit": 3},
                           "requests": {"used": 1, "limit": 1}},
                 "resets_at": "2026-09-25T00:00:00Z", "scope": "day", "subscription": None}


def test_account_unauthorized(proxy):
    t = proxy.mint()
    proxy.store.revoke(hash_token(t))
    for tok in (None, "mmp_nope", t):
        s, d, _, _ = proxy.request("GET", "/v1/account", token=tok)
        assert (s, d) == (401, {"error": "unauthorized"})


def test_minted_tokens_have_device_ids_too(proxy):
    t = proxy.mint("pro")
    s, a, _, _ = proxy.request("GET", "/v1/account", token=t)
    assert s == 200 and a["device_id"].startswith("dev_") and a["plan"] == "pro"
    assert a["usage"]["jev"]["limit"] == 50


def test_old_database_is_migrated_in_place(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE tokens (token_hash TEXT PRIMARY KEY, plan TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, revoked_at INTEGER);
        CREATE TABLE usage (token_hash TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
            n INTEGER NOT NULL, PRIMARY KEY (token_hash, day, kind));
    """)
    c.execute("INSERT INTO tokens VALUES (?, 'pro', 'old laptop', 1, NULL)",
              (hash_token("mmp_old"),))
    c.execute("INSERT INTO usage VALUES (?, ?, 'jev', 7)", (hash_token("mmp_old"), DAY))
    c.commit()
    c.close()
    s = Store(db)
    row = s.account(hash_token("mmp_old"))
    assert row["device_id"].startswith("dev_") and row["plan"] == "pro"
    assert s.used(hash_token("mmp_old"), "jev", DAY) == 7
    assert Store(db).account(hash_token("mmp_old"))["device_id"] == row["device_id"]


# ------------------------------------------------------------------ checkout + portal


@pytest.fixture
def fake_stripe(monkeypatch):
    """Session creation replaced by the SDK's own object types. Records every call."""
    calls: list[dict] = []

    def checkout_create(**kw):
        calls.append({"kind": "checkout", **kw})
        return stripe.checkout.Session.construct_from(
            {"id": "cs_test_1", "object": "checkout.session",
             "url": "https://checkout.stripe.com/c/pay/cs_test_1", "mode": "subscription",
             "metadata": kw.get("metadata", {})}, SK)

    def portal_create(**kw):
        calls.append({"kind": "portal", **kw})
        return stripe.billing_portal.Session.construct_from(
            {"id": "bps_1", "object": "billing_portal.session",
             "url": "https://billing.stripe.com/p/session/test_1"}, SK)

    monkeypatch.setattr(stripe.checkout.Session, "create", checkout_create)
    monkeypatch.setattr(stripe.billing_portal.Session, "create", portal_create)
    return calls


def test_checkout_and_portal_are_503_when_stripe_is_not_configured(proxy, fake_stripe):
    t = proxy.mint()
    for path in ("/v1/checkout", "/v1/portal"):
        s, d, _, _ = proxy.request("POST", path, token=t)
        assert (s, d) == (503, {"error": "payments_unavailable"}), path
    # Each of the four settings is required.
    for missing in STRIPE_CFG:
        for k, v in STRIPE_CFG.items():
            setattr(proxy.cfg, k, None if k == missing else v)
        assert proxy.request("POST", "/v1/checkout", token=t)[0] == 503, missing
    assert fake_stripe == []


def test_checkout_creates_a_subscription_session_at_the_server_price(make_proxy, fake_stripe):
    p = make_proxy(**STRIPE_CFG)
    _, d, _, _ = signup(p)
    s, out, _, _ = p.request("POST", "/v1/checkout",
                             {"price": "price_attacker_cheap", "quantity": 99}, token=d["token"])
    assert (s, out) == (200, {"url": "https://checkout.stripe.com/c/pay/cs_test_1"})
    (call,) = fake_stripe
    assert call["api_key"] == SK
    assert call["mode"] == "subscription"
    assert call["line_items"] == [{"price": PRICE, "quantity": 1}]
    assert call["client_reference_id"] == d["device_id"]
    assert call["metadata"] == {"device_id": d["device_id"]}
    assert call["subscription_data"] == {"metadata": {"device_id": d["device_id"]}}
    assert call["success_url"] == f"{BASE}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
    assert call["cancel_url"] == f"{BASE}/checkout/cancel"
    assert call["idempotency_key"] == f"checkout:{d['device_id']}:{DAY}:{PRICE}:new"
    assert "customer" not in call
    # The plan does not move until a webhook says so.
    assert p.request("GET", "/v1/account", token=d["token"])[1]["plan"] == "free"


def test_checkout_is_409_for_pro_and_401_without_a_token(make_proxy, fake_stripe):
    p = make_proxy(**STRIPE_CFG)
    assert p.request("POST", "/v1/checkout", token=p.mint("pro"))[1] == {"error": "already_pro"}
    assert p.request("POST", "/v1/checkout")[1] == {"error": "unauthorized"}
    assert p.request("POST", "/v1/portal", token="mmp_x")[0] == 401
    assert fake_stripe == []


def test_stripe_failure_is_502_not_500(make_proxy, monkeypatch):
    p = make_proxy(**STRIPE_CFG)

    def boom(**kw):
        raise stripe.APIConnectionError("network down")
    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    s, d, _, _ = p.request("POST", "/v1/checkout", token=p.mint())
    assert (s, d) == (502, {"error": "payments_error"})


def test_portal_needs_a_customer(make_proxy, fake_stripe):
    p = make_proxy(**STRIPE_CFG)
    _, d, _, _ = signup(p)
    s, out, _, _ = p.request("POST", "/v1/portal", token=d["token"])
    assert (s, out) == (404, {"error": "no_subscription"})
    assert post_event(p, event("checkout.session.completed",
                               session_obj(d["device_id"])))[0] == 200
    s, out, _, _ = p.request("POST", "/v1/portal", token=d["token"])
    assert (s, out) == (200, {"url": "https://billing.stripe.com/p/session/test_1"})
    assert fake_stripe[-1]["customer"] == "cus_test_1"
    assert fake_stripe[-1]["return_url"] == f"{BASE}/"


# ------------------------------------------------------------------ webhook


def test_webhook_503_without_a_secret_and_400_on_any_bad_signature(proxy, make_proxy):
    body = event("checkout.session.completed", session_obj("dev_x"))
    s, d, _, _ = post_event(proxy, body)
    assert (s, d) == (503, {"error": "payments_unavailable"})
    p = make_proxy(stripe_webhook_secret=WHSEC)
    for sig in (None, "", "t=1,v1=00", sign(body, "whsec_someone_else"),
                sign(body, ts=int(time.time()) - 3600)):
        s, d, _, _ = post_event(p, body, sig)
        assert (s, d) == (400, {"error": "bad_signature"}), sig
    # The signature covers the exact bytes: re-serialising breaks it.
    tampered = json.dumps(json.loads(body), indent=1).encode()
    assert post_event(p, tampered, sign(body))[0] == 400
    assert post_event(p, b"not json at all")[0] == 400


def test_checkout_completed_makes_the_device_pro_once(make_proxy):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    body = event("checkout.session.completed", session_obj(d["device_id"]), eid="evt_1")
    s, out, _, _ = post_event(p, body)
    assert (s, out) == (200, {"received": True})
    a = p.request("GET", "/v1/account", token=d["token"])[1]
    assert a["plan"] == "pro" and a["usage"]["jev"]["limit"] == 50
    row = p.store.device(d["device_id"])
    assert row["stripe_customer_id"] == "cus_test_1"
    assert row["stripe_subscription_id"] == "sub_test_1"
    # The owner moves it back by hand; a redelivery of the same event changes nothing.
    p.store.set_plan(hash_token(d["token"]), "free")
    assert post_event(p, body)[0] == 200
    assert p.store.device(d["device_id"])["plan"] == "free"


def test_subscription_lifecycle(make_proxy):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    dev, t = d["device_id"], d["token"]
    plan = lambda: p.request("GET", "/v1/account", token=t)[1]  # noqa: E731
    post_event(p, event("checkout.session.completed", session_obj(dev), created=100))
    post_event(p, event("customer.subscription.updated", sub_obj(dev), created=101))
    a = plan()
    assert a["plan"] == "pro"
    assert a["subscription"] == {"status": "active", "current_period_end": "2026-10-14T17:46:40Z",
                                 "cancel_at_period_end": False}
    # Card fails: Stripe retries, and she keeps Pro meanwhile.
    post_event(p, event("customer.subscription.updated", sub_obj(dev, "past_due"), created=102))
    assert plan()["plan"] == "pro" and plan()["subscription"]["status"] == "past_due"
    # She cancels at period end: still Pro, and the app can say until when.
    post_event(p, event("customer.subscription.updated",
                        sub_obj(dev, "active", cancel_at_period_end=True), created=103))
    assert plan()["plan"] == "pro" and plan()["subscription"]["cancel_at_period_end"] is True
    # An older event delivered late does not undo a newer one.
    post_event(p, event("customer.subscription.updated", sub_obj(dev, "unpaid"), created=90))
    assert plan()["plan"] == "pro"
    post_event(p, event("customer.subscription.deleted", sub_obj(dev, "canceled"), created=110))
    a = plan()
    assert a["plan"] == "free" and a["subscription"]["status"] == "canceled"


@pytest.mark.parametrize("status,want", [("active", "pro"), ("trialing", "pro"),
                                         ("past_due", "pro"), ("unpaid", "free"),
                                         ("canceled", "free"), ("incomplete", "free"),
                                         ("incomplete_expired", "free"), ("paused", "free")])
def test_subscription_status_to_plan(make_proxy, status, want):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    post_event(p, event("customer.subscription.updated", sub_obj(d["device_id"], status)))
    assert p.store.device(d["device_id"])["plan"] == want


def test_subscription_event_before_checkout_event_finds_the_device(make_proxy):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    dev = d["device_id"]
    post_event(p, event("customer.subscription.created", sub_obj(dev, "incomplete"), created=99))
    assert p.store.device(dev)["plan"] == "free"
    post_event(p, event("customer.subscription.updated", sub_obj(dev), created=100))
    assert p.store.device(dev)["plan"] == "pro"
    post_event(p, event("checkout.session.completed", session_obj(dev), created=100))
    row = p.store.device(dev)
    assert row["plan"] == "pro" and row["sub_status"] == "active"
    assert row["sub_period_end"] == 1_792_000_000


def test_late_incomplete_event_does_not_undo_a_paid_checkout(make_proxy):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    dev = d["device_id"]
    post_event(p, event("checkout.session.completed", session_obj(dev), created=100))
    post_event(p, event("customer.subscription.created", sub_obj(dev, "incomplete"), created=100))
    assert p.store.device(dev)["plan"] == "pro"


def test_checkout_after_the_subscription_already_ended_does_not_grant(make_proxy):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    dev = d["device_id"]
    post_event(p, event("customer.subscription.deleted", sub_obj(dev, "canceled"), created=200))
    post_event(p, event("checkout.session.completed", session_obj(dev), created=100))
    assert p.store.device(dev)["plan"] == "free"


def test_delayed_payment_is_pro_only_when_it_succeeds(make_proxy):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    dev = d["device_id"]
    post_event(p, event("checkout.session.completed",
                        session_obj(dev, payment_status="unpaid"), eid="evt_a"))
    assert p.store.device(dev)["plan"] == "free"
    post_event(p, event("checkout.session.async_payment_succeeded",
                        session_obj(dev, payment_status="paid"), eid="evt_b"))
    assert p.store.device(dev)["plan"] == "pro"


def test_old_subscription_ending_does_not_demote_a_new_one(make_proxy):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    dev = d["device_id"]
    post_event(p, event("checkout.session.completed", session_obj(dev, subscription="sub_new")))
    post_event(p, event("customer.subscription.deleted",
                        sub_obj(dev, "canceled", id="sub_old"), created=500))
    assert p.store.device(dev)["plan"] == "pro"


def test_events_that_are_not_ours_are_acknowledged_and_ignored(make_proxy):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    for body in (event("invoice.paid", {"id": "in_1", "object": "invoice"}),
                 event("checkout.session.completed",
                       session_obj(d["device_id"], mode="payment"), eid="evt_pay"),
                 event("checkout.session.completed", session_obj("dev_nobody"), eid="evt_nb"),
                 event("checkout.session.completed",
                       session_obj(None, metadata={}), eid="evt_nometa"),
                 event("customer.subscription.updated", sub_obj(None, id="sub_elsewhere"))):
        s, out, _, _ = post_event(p, body)
        assert (s, out) == (200, {"received": True})
    assert p.store.device(d["device_id"])["plan"] == "free"


def test_handle_event_accepts_a_real_stripe_object():
    """The .get() trap: a StripeObject has no dict .get(). handle_event must convert."""
    class Rec:
        def __init__(self):
            self.calls = []

        def checkout_completed(self, *a):
            self.calls.append(a)
            return "pro"
    ev = stripe.Event.construct_from(
        {"id": "evt_obj", "type": "checkout.session.completed", "created": 1,
         "data": {"object": session_obj("dev_abc")}}, SK)
    with pytest.raises(AttributeError):
        ev.get("id")                         # the SDK object really lacks .get()
    rec = Rec()
    assert billing.handle_event(rec, ev) == "pro"
    assert rec.calls == [("evt_obj", "checkout.session.completed", "dev_abc", "cus_test_1",
                          "sub_test_1", True)]


def test_webhook_store_failure_is_500_so_stripe_retries(make_proxy, monkeypatch):
    p = make_proxy(stripe_webhook_secret=WHSEC)
    _, d, _, _ = signup(p)
    body = event("checkout.session.completed", session_obj(d["device_id"]), eid="evt_r")

    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(p.store, "checkout_completed", broken)
    assert post_event(p, body)[0] == 500
    monkeypatch.undo()
    assert post_event(p, body)[0] == 200             # the retry is applied
    assert p.store.device(d["device_id"])["plan"] == "pro"


# ------------------------------------------------------------------ pages + health


def test_health_and_ready(proxy, monkeypatch):
    assert proxy.request("GET", "/health")[:2] == (200, {"ok": True})
    assert proxy.request("GET", "/healthz")[:2] == (200, {"ok": True})
    assert proxy.request("GET", "/ready")[:2] == (200, {"ok": True})

    def down():
        raise OSError("disk gone")
    monkeypatch.setattr(proxy.store, "ping", down)
    assert proxy.request("GET", "/ready")[:2] == (503, {"ok": False})
    assert proxy.request("GET", "/health")[0] == 200            # liveness needs no DB


def _page(p, path):
    s, _, raw, h = p.request("GET", path)
    return s, raw.decode("utf-8"), h


def test_landing_page_numbers_come_from_config(make_proxy):
    p = make_proxy(free_daily_calls=300, pro_daily_calls=6000)
    s, html, h = _page(p, "/")
    assert s == 200 and h["Content-Type"] == "text/html; charset=utf-8"
    assert "Talk to your Mac." in html
    assert "100 requests to try" in html and "2,000 requests a day" in html
    assert "$8/month" in html
    for ex in ("What's on my screen?", "Send this to Matan", "Play something happy"):
        assert ex in html
    for lang in ("English", "Hebrew", "Arabic", "Russian"):
        assert lang in html
    assert "Apple silicon (M1 or later), macOS 13 or later" in html
    assert "coming soon" in html and "Download for Mac" not in html
    p2 = make_proxy(free_daily_calls=90, jev_calls_per_request=3, pro_price_label="$9/month",
                    download_url="https://example.com/MicMic.dmg")
    html = _page(p2, "/")[1]
    assert "30 requests to try" in html and "$9/month" in html
    assert 'href="https://example.com/MicMic.dmg">Download for Mac' in html


def test_pages_are_self_contained_and_locked_down(make_proxy):
    p = make_proxy(support_email="help@micmic.example")
    for path in ("/", "/privacy", "/terms", "/checkout/success", "/checkout/cancel"):
        s, html, h = _page(p, path)
        assert s == 200, path
        assert "<script" not in html.lower() and "{{" not in html, path
        assert "—" not in html and "–" not in html, f"dash in {path}"
        assert "http://" not in html and "src=\"https://" not in html, path
        assert "script-src" not in h["Content-Security-Policy"]
        assert "default-src 'none'" in h["Content-Security-Policy"]
        assert h["X-Content-Type-Options"] == "nosniff"
    for path in ("/privacy", "/terms"):
        html = _page(p, path)[1]
        assert "First draft, for review by a lawyer" in html
        assert "mailto:help@micmic.example" in html
    ok = _page(p, "/checkout/success")[1]
    assert "You're on MicMic Pro." in ok and "You can close this tab and go back to MicMic." in ok
    assert "Nothing was charged." in _page(p, "/checkout/cancel")[1]
    s, _, raw, h = p.request("GET", "/static/icon.png")
    assert s == 200 and h["Content-Type"] == "image/png" and raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_checkout_success_page_ignores_its_query(proxy):
    s, html, _ = _page(proxy, "/checkout/success?session_id=cs_test_<script>")
    assert s == 200 and "<script" not in html


# ------------------------------------------------------------------ config + boot


def _env(monkeypatch, tmp_path, **env):
    for k in ("TYPESAFE_API_KEY", "GEMINI_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
              "STRIPE_PRICE_PRO", "PUBLIC_BASE_URL", "RAILWAY_PUBLIC_DOMAIN", "ENVIRONMENT",
              "PORT", "MICMIC_PROXY_PORT", "MICMIC_PROXY_DB", "TRUST_PROXY_HEADERS",
              "MICMIC_DOWNLOAD_URL", "REFUND_ON_UPSTREAM_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    # Never read a real key file from the checkout's surroundings.
    monkeypatch.setenv("MICMIC_PROXY_ENV_FILE", str(tmp_path / "no-such.env"))
    monkeypatch.setenv("MICMIC_PROXY_DB", str(tmp_path / "boot.db"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_from_env_reads_the_service_settings(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, TYPESAFE_API_KEY="fake", PORT="8823",
         STRIPE_SECRET_KEY=SK, STRIPE_WEBHOOK_SECRET=WHSEC, STRIPE_PRICE_PRO=PRICE,
         RAILWAY_PUBLIC_DOMAIN="micmic-proxy.up.railway.app", TRUST_PROXY_HEADERS="1",
         REFUND_ON_UPSTREAM_TIMEOUT="1")
    cfg = Config.from_env()
    assert cfg.port == 8823 and cfg.trust_proxy_headers and cfg.refund_on_timeout
    assert cfg.public_base_url == "https://micmic-proxy.up.railway.app"
    assert cfg.payments_configured and cfg.boot_errors() == []
    assert cfg.daily_requests("free") == 100 and cfg.daily_requests("pro") == 2000
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://micmic.app/")
    assert Config.from_env().public_base_url == "https://micmic.app"


@pytest.mark.parametrize("env,ok", [("development", False), ("staging", False),
                                    ("", False), ("production", True)])
def test_live_stripe_key_refused_outside_production(monkeypatch, tmp_path, env, ok):
    _env(monkeypatch, tmp_path, TYPESAFE_API_KEY="fake",
         STRIPE_SECRET_KEY="sk_live_" + "0" * 24, ENVIRONMENT=env)
    errors = Config.from_env().boot_errors()
    assert (errors == []) is ok, errors
    if not ok:
        # main() refuses before it binds a port or opens the database.
        assert appmod.main() == 2
        assert not (tmp_path / "boot.db").exists()


def test_test_key_boots_anywhere(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, TYPESAFE_API_KEY="fake", STRIPE_SECRET_KEY=SK)
    assert Config.from_env().boot_errors() == []
    _env(monkeypatch, tmp_path)
    assert Config.from_env().boot_errors() == ["TYPESAFE_API_KEY is not set"]


def test_admin_finds_a_device_by_its_id(tmp_path, capsys):
    from proxy import admin
    db = tmp_path / "adm.db"
    store = Store(db)
    tok, dev = store.register_device("Mac", "1.0", "addr", DAY, 5)
    assert admin.main(["--db", str(db), "list"]) == 0
    out = capsys.readouterr()[0]
    assert dev in out and tok not in out
    assert admin.main(["--db", str(db), "plan", dev, "pro"]) == 0
    assert Store(db).plan_for(tok)[1] == "pro"
    assert admin.main(["--db", str(db), "revoke", "dev_nope"]) == 1
    assert admin.main(["--db", str(db), "revoke", dev]) == 0
    assert Store(db).plan_for(tok) is None
