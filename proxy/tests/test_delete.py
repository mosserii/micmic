"""POST /v1/account/delete and `admin delete` (PURCHASES_ACCOUNT.md 3.3, tests 28 to 35).

The brain is a fake on localhost that, like the real one, authenticates the caller's
token against the proxy's own GET /v1/account before it forgets anything."""
from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import stripe

from apple_fakes import new_otid, tx_payload
from conftest import ios_signup
from proxy import admin
from proxy.store import hash_token
from test_apple import account, apple_proxy, now_s, post_tx
from test_service import STRIPE_CFG, event, post_event, sub_obj

pytestmark = pytest.mark.usefixtures("apple_leak_check")


class FakeBrain:
    def __init__(self):
        self.mode = "ok"
        self.calls: list[dict] = []
        self.proxy_port: int | None = None
        brain = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(n)
                auth = self.headers.get("Authorization")
                seen = None
                if brain.proxy_port:
                    # The real brain checks the token with the proxy first.
                    c = http.client.HTTPConnection("127.0.0.1", brain.proxy_port, timeout=5)
                    c.request("GET", "/v1/account", headers={"Authorization": auth})
                    r = c.getresponse()
                    seen = r.status
                    r.read()
                    c.close()
                brain.calls.append({"path": self.path, "auth": auth, "body": body,
                                    "contract": self.headers.get("X-MicMic-Contract"),
                                    "account_status": seen})
                if brain.mode == "hang":
                    time.sleep(2)
                status, out = {"ok": (200, {"v": 1, "erased": True}),
                               "error": (500, {"error": "internal"}),
                               "off": (501, {"error": "not_enabled"}),
                               "lies": (200, {"v": 1, "erased": False})}.get(
                                   brain.mode, (200, {"v": 1, "erased": True}))
                raw = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def brain():
    b = FakeBrain()
    yield b
    b.close()


def delete(p, token, body=None):
    s, d, _, h = p.request("POST", "/v1/account/delete",
                           {"confirm": True} if body is None else body, token=token)
    return s, d


def rows(p, table: str) -> int:
    return p.store._conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def setup(make_proxy, apple_pki, brain):
    """Two iPhones sharing one Apple subscription, the first also with one of its own."""
    p = apple_proxy(make_proxy, apple_pki, brain_url=brain.url, brain_timeout=1.0)
    brain.proxy_port = p.port
    _, a = ios_signup(p)
    _, b = ios_signup(p)
    shared, own = new_otid(), new_otid()
    for d in (a, b):
        assert post_tx(p, d["token"], apple_pki.sign(tx_payload(now=now_s(p), otid=shared)))[0] == 200
    assert post_tx(p, a["token"], apple_pki.sign(tx_payload(now=now_s(p), otid=own)))[0] == 200
    p.request("POST", "/v1/jev", {"questions": {"a": {}}}, token=a["token"])
    return p, a, b


# 28 + 29 + 33
def test_delete_removes_every_row_of_the_device_and_nothing_else(setup, brain):
    p, a, b = setup
    h = hash_token(a["token"])
    assert p.store.used(h, "jev", "2026-09-24") == 1
    s, out = delete(p, a["token"])
    assert (s, out) == (200, {"deleted": True, "brain": "erased",
                              "apple_subscription_active": True,
                              "manage_url": "https://apps.apple.com/account/subscriptions"})
    assert p.request("GET", "/v1/account", token=a["token"])[0] == 401
    c = p.store._conn()
    assert c.execute("SELECT COUNT(*) FROM tokens WHERE token_hash = ?", (h,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM usage WHERE token_hash = ?", (h,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM apple_links WHERE device_id = ?",
                     (a["device_id"],)).fetchone()[0] == 0
    # Its own subscription row is gone; the shared one survives with B's link.
    assert rows(p, "apple_subscriptions") == 1 and rows(p, "apple_links") == 1
    assert account(p, b["token"])["plan"] == "pro"
    assert c.execute("SELECT n FROM deletions").fetchall() == [(1,)]
    # 29: exactly one forget, with her own token and the contract header, while her
    # row still existed (the brain's own check of the token answered 200).
    assert len(brain.calls) == 1
    call = brain.calls[0]
    assert call["path"] == "/v1/brain/forget" and json.loads(call["body"]) == {"v": 1}
    assert call["auth"] == f"Bearer {a['token']}" and call["contract"] == "1"
    assert call["account_status"] == 200


# 30
@pytest.mark.parametrize("mode,want", [("error", "brain_unavailable"),
                                       ("hang", "brain_unavailable"),
                                       ("lies", "brain_unavailable"),
                                       ("off", "deletion_unavailable")])
def test_brain_failure_deletes_nothing(setup, brain, mode, want):
    p, a, b = setup
    brain.mode = mode
    before = {t: rows(p, t) for t in ("tokens", "usage", "apple_links", "apple_subscriptions")}
    s, out = delete(p, a["token"])
    assert s == 503 and out["error"] == want
    if want == "brain_unavailable":
        assert out["retry_after_s"] == 30
    assert {t: rows(p, t) for t in before} == before
    assert account(p, a["token"])["plan"] == "pro"
    brain.mode = "ok"
    assert delete(p, a["token"])[0] == 200


def test_brain_unreachable_deletes_nothing(make_proxy):
    p = make_proxy(brain_url="http://127.0.0.1:9", brain_timeout=0.5)
    _, d = ios_signup(p)
    assert delete(p, d["token"])[1]["error"] == "brain_unavailable"
    assert p.request("GET", "/v1/account", token=d["token"])[0] == 200


# 31
def test_without_a_brain_url_the_brain_is_not_used(make_proxy):
    p = make_proxy()
    _, d = ios_signup(p)
    s, out = delete(p, d["token"])
    assert s == 200 and out["brain"] == "not_used" and out["apple_subscription_active"] is False


@pytest.mark.parametrize("environment", ["production", "Production", "staging"])
def test_deployed_iphone_without_a_brain_url_refuses_and_deletes_nothing(make_proxy,
                                                                          environment):
    """On a deploy the brain holds an iPhone's notes and preferences. Without BRAIN_URL
    the proxy cannot tell it to forget them, so it says so and deletes nothing, instead
    of answering "deleted" while they stay behind. Only local development (test 31)
    skips the brain."""
    p = make_proxy(environment=environment)
    _, d = ios_signup(p)
    before = {t: rows(p, t) for t in ("tokens", "usage", "apple_links", "apple_subscriptions")}
    assert delete(p, d["token"]) == (503, {"error": "deletion_unavailable"})
    assert {t: rows(p, t) for t in before} == before
    assert p.request("GET", "/v1/account", token=d["token"])[0] == 200
    assert p.store._conn().execute("SELECT COUNT(*) FROM deletions").fetchone()[0] == 0


def test_production_mac_without_a_brain_url_still_deletes(make_proxy):
    """The brain is iPhone-only: a Mac's deletion never needed it."""
    p = make_proxy(environment="production")
    mac = p.mint("free")
    s, out = delete(p, mac)
    assert s == 200 and out["brain"] == "not_used"
    assert p.request("GET", "/v1/account", token=mac)[0] == 401


def test_production_iphone_with_a_brain_url_erases(make_proxy, brain):
    p = make_proxy(environment="production", brain_url=brain.url, brain_timeout=1.0)
    brain.proxy_port = p.port
    _, d = ios_signup(p)
    s, out = delete(p, d["token"])
    assert s == 200 and out["brain"] == "erased" and len(brain.calls) == 1


# 32 + 35
def test_stripe_is_cancelled_first_and_a_failure_deletes_nothing(make_proxy, monkeypatch):
    p = make_proxy(**STRIPE_CFG)
    mac = p.mint("free")
    dev = p.store.account(hash_token(mac))["device_id"]
    assert post_event(p, event("customer.subscription.updated", sub_obj(dev, "active"),
                               created=1))[0] == 200
    order: list[str] = []

    def fails(sub_id, **kw):
        order.append("cancel-failed")
        raise stripe.APIConnectionError("network down")
    monkeypatch.setattr(stripe.Subscription, "cancel", fails)
    assert delete(p, mac) == (502, {"error": "payments_error"})
    assert p.request("GET", "/v1/account", token=mac)[1]["plan"] == "pro"

    def cancel(sub_id, **kw):
        order.append(sub_id)
        assert kw["api_key"] == STRIPE_CFG["stripe_secret_key"]
        assert p.store.account(hash_token(mac)) is not None        # before any deletion
    monkeypatch.setattr(stripe.Subscription, "cancel", cancel)
    s, out = delete(p, mac)
    assert s == 200 and order == ["cancel-failed", "sub_test_1"]
    # 35: Stripe's own event about it arrives after the device is gone.
    body = event("customer.subscription.deleted", sub_obj(dev, "canceled"), created=2)
    assert post_event(p, body)[0] == 200
    assert p.store._conn().execute(
        "SELECT outcome FROM stripe_events ORDER BY received_at DESC, rowid DESC").fetchone()[0] \
        == "unknown_device"


def test_a_subscription_already_ending_is_not_cancelled_again(make_proxy, monkeypatch):
    p = make_proxy(**STRIPE_CFG)
    mac = p.mint("free")
    dev = p.store.account(hash_token(mac))["device_id"]
    post_event(p, event("customer.subscription.updated",
                        sub_obj(dev, "active", cancel_at_period_end=True), created=1))
    monkeypatch.setattr(stripe.Subscription, "cancel",
                        lambda *a, **k: pytest.fail("must not cancel"))
    assert delete(p, mac)[0] == 200


# 33 (expired subscription)
def test_apple_subscription_active_is_false_once_it_has_ended(make_proxy, apple_pki):
    import datetime as dt
    p = apple_proxy(make_proxy, apple_pki)
    _, d = ios_signup(p)
    assert post_tx(p, d["token"], apple_pki.sign(tx_payload(now=now_s(p), expires_in=600)))[0] == 200
    p.clock.now += dt.timedelta(hours=2)
    s, out = delete(p, d["token"])
    assert s == 200 and out["apple_subscription_active"] is False
    assert rows(p, "apple_subscriptions") == 0


# 34
def test_second_call_is_401_and_confirm_is_required(make_proxy):
    p = make_proxy()
    _, d = ios_signup(p)
    for body in ({"confirm": False}, {}, {"confirm": "true"}, {"confirm": 1}):
        assert delete(p, d["token"], body) == (400, {"error": "confirm_required"})
    assert delete(p, d["token"])[0] == 200
    assert delete(p, d["token"]) == (401, {"error": "unauthorized"})
    assert delete(p, None)[0] == 401
    # GET and DELETE are not ways in.
    assert p.request("DELETE", "/v1/account")[0] == 405


def test_notifications_after_deletion_find_no_device(setup, apple_pki):
    from apple_fakes import notification
    p, a, _ = setup
    otid = new_otid()
    assert post_tx(p, a["token"], apple_pki.sign(tx_payload(
        now=now_s(p), otid=otid, app_account_token=a["apple_account_token"])))[0] == 200
    assert delete(p, a["token"])[0] == 200
    n = now_s(p)
    signed = notification(apple_pki, "DID_RENEW", now=n, tx=tx_payload(
        now=n, otid=otid, app_account_token=a["apple_account_token"]))
    assert p.request("POST", "/v1/apple/notifications", {"signedPayload": signed})[0] == 200
    assert p.store._conn().execute(
        "SELECT outcome FROM apple_events").fetchone()[0] == "unknown_subscription"
    assert p.store._conn().execute(
        "SELECT COUNT(*) FROM apple_subscriptions WHERE original_tx_id = ?",
        (otid,)).fetchone()[0] == 0


def test_admin_delete(tmp_path, capsys, monkeypatch):
    from proxy.store import Store
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setenv("MICMIC_PROXY_ENV_FILE", str(tmp_path / "none.env"))
    db = tmp_path / "admin.db"
    store = Store(db)
    tok, dev = store.register_device("Mac", "1.2.4", "addr", "2026-09-24", 5)
    store.consume(hash_token(tok), "jev", "total", 300)
    assert admin.main(["--db", str(db), "delete", dev]) == 1          # without --yes: shows
    assert "would delete" in capsys.readouterr().out
    assert Store(db).plan_for(tok) is not None
    assert admin.main(["--db", str(db), "delete", dev, "--yes"]) == 0
    assert "deleted" in capsys.readouterr().out
    assert Store(db).plan_for(tok) is None and Store(db).used(hash_token(tok), "jev", "total") == 0
    assert admin.main(["--db", str(db), "delete", dev, "--yes"]) == 1
    # admin list shows the new columns.
    tok2, _ = store.register_device("iPhone", "1.0.0", "addr", "2026-09-24", 5, platform="ios")
    assert admin.main(["--db", str(db), "list"]) == 0
    out = capsys.readouterr().out
    assert "pro via" in out and " ios " in out and tok2 not in out


def test_admin_delete_stops_when_stripe_fails(tmp_path, capsys, monkeypatch):
    from proxy.store import Store
    monkeypatch.setenv("MICMIC_PROXY_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", STRIPE_CFG["stripe_secret_key"])
    db = tmp_path / "admin.db"
    store = Store(db)
    tok, dev = store.register_device("Mac", "1.2.4", "addr", "2026-09-24", 5)
    store._conn().execute("UPDATE tokens SET stripe_subscription_id = 'sub_x', "
                          "sub_status = 'active', plan = 'pro' WHERE device_id = ?", (dev,))

    def down(*a, **k):
        raise stripe.APIConnectionError("down")
    monkeypatch.setattr(stripe.Subscription, "cancel", down)
    assert admin.main(["--db", str(db), "delete", dev, "--yes"]) == 1
    assert "nothing deleted" in capsys.readouterr().err
    assert Store(db).plan_for(tok) is not None
