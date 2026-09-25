"""MicMic Pro through Stripe: hosted Checkout in subscription mode, fulfilled only by
the webhook.

The app never sees a Stripe key. It asks this server for a Checkout URL, opens it in
the browser, and later reads /v1/account. The plan changes only when a signed webhook
says so: the success page proves nothing about payment and the tab can close before
it loads.

Every Stripe object is turned into a plain dict at the boundary (_stripe_to_dict). A
StripeObject has no dict-style .get(), and calling one 500s the endpoint; tests build
their fixtures with the SDK's own construct_from so they would catch that.

Only this module imports stripe. Keys and signatures are never logged.
"""
from __future__ import annotations

import logging
from typing import Any

import stripe

from .config import Config
from .store import Store

log = logging.getLogger("micmic.proxy")

# Seconds of clock skew allowed between Stripe's signature timestamp and ours.
SIGNATURE_TOLERANCE = 300


class PaymentsUnavailable(Exception):
    """Stripe is not configured on this deploy. Answered 503, never 500."""


class StripeFailed(Exception):
    """Stripe answered with an error or could not be reached. Answered 502."""


class BadSignature(Exception):
    pass


def _stripe_to_dict(obj: Any) -> Any:
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def _id(v: Any) -> str | None:
    """A Stripe reference arrives as an id string, or as the object when expanded."""
    if isinstance(v, dict):
        v = v.get("id")
    return v if isinstance(v, str) and v else None


def _require(cfg: Config) -> None:
    if not cfg.payments_configured:
        raise PaymentsUnavailable()


def create_checkout(cfg: Config, device: dict, window: str) -> str:
    """A Checkout Session for MicMic Pro for this device. The price is the server's
    STRIPE_PRICE_PRO, never anything the client sent."""
    _require(cfg)
    device_id = device["device_id"]
    customer = device.get("stripe_customer_id")
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": cfg.stripe_price_pro, "quantity": 1}],
        "client_reference_id": device_id,
        "metadata": {"device_id": device_id},
        # So every customer.subscription.* event names the device too, even one that
        # arrives before checkout.session.completed.
        "subscription_data": {"metadata": {"device_id": device_id}},
        # The literal {CHECKOUT_SESSION_ID} is Stripe's own template token.
        "success_url": f"{cfg.public_base_url}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{cfg.public_base_url}/checkout/cancel",
    }
    if customer:
        # A device that subscribed before keeps one customer, and one billing history.
        params["customer"] = customer
    # A double click, or a retry after a timeout, gets the same session back. The key
    # changes with anything that changes the parameters, since Stripe refuses a reused
    # key with different ones.
    key = f"checkout:{device_id}:{window}:{cfg.stripe_price_pro}:{customer or 'new'}"
    try:
        session = stripe.checkout.Session.create(
            api_key=cfg.stripe_secret_key, idempotency_key=key, **params)
    except stripe.StripeError as e:
        log.warning("stripe checkout create failed: %s", type(e).__name__)
        raise StripeFailed() from e
    session = _stripe_to_dict(session)
    url = session.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise StripeFailed()
    log.info("checkout session created for %s", device_id)
    return url


def create_portal(cfg: Config, device: dict) -> str | None:
    """Stripe's billing portal for this device's customer, or None if it never paid."""
    _require(cfg)
    customer = device.get("stripe_customer_id")
    if not customer:
        return None
    try:
        session = stripe.billing_portal.Session.create(
            api_key=cfg.stripe_secret_key, customer=customer,
            return_url=f"{cfg.public_base_url}/")
    except stripe.StripeError as e:
        log.warning("stripe portal create failed: %s", type(e).__name__)
        raise StripeFailed() from e
    session = _stripe_to_dict(session)
    url = session.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise StripeFailed()
    return url


def verify_event(cfg: Config, payload: bytes, sig_header: str | None) -> dict:
    """Check Stripe's signature over the exact bytes received, then parse. Only the
    webhook secret is needed, so a deploy can accept events before checkout is on."""
    if not cfg.stripe_webhook_secret:
        raise PaymentsUnavailable()
    if not sig_header:
        raise BadSignature()
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, cfg.stripe_webhook_secret, tolerance=SIGNATURE_TOLERANCE)
    except (ValueError, stripe.SignatureVerificationError) as e:
        raise BadSignature() from e
    event = _stripe_to_dict(event)
    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise BadSignature()
    return event


def _period_end(sub: dict) -> int | None:
    # Older API versions put the period on the subscription; since 2025-03-31 it is on
    # each subscription item.
    end = sub.get("current_period_end")
    if isinstance(end, int):
        return end
    items = (sub.get("items") or {}).get("data") or []
    ends = [i.get("current_period_end") for i in items if isinstance(i, dict)]
    ends = [e for e in ends if isinstance(e, int)]
    return max(ends) if ends else None


CHECKOUT_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
SUBSCRIPTION_EVENTS = ("customer.subscription.created", "customer.subscription.updated",
                       "customer.subscription.deleted")


def handle_event(store: Store, event: dict) -> str:
    """Apply one verified event. Idempotent by event id. Returns what happened, for
    the log; anything not about a MicMic subscription is recorded as ignored."""
    event = _stripe_to_dict(event)
    eid, etype = str(event["id"]), str(event["type"])
    obj = ((event.get("data") or {}).get("object")) or {}

    if etype in CHECKOUT_EVENTS:
        if obj.get("mode") != "subscription":
            return store.record_event(eid, etype)
        meta = obj.get("metadata") or {}
        device_id = obj.get("client_reference_id") or meta.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            return store.record_event(eid, etype, "no_device")
        # With a card, payment_status is "paid" at completion. A delayed method
        # completes "unpaid" and sends async_payment_succeeded later.
        paid = (etype == "checkout.session.async_payment_succeeded"
                or obj.get("payment_status") in ("paid", "no_payment_required"))
        return store.checkout_completed(eid, etype, device_id, _id(obj.get("customer")),
                                        _id(obj.get("subscription")), paid)

    if etype in SUBSCRIPTION_EVENTS:
        sub_id = _id(obj.get("id"))
        if not sub_id:
            return store.record_event(eid, etype, "no_subscription")
        meta = obj.get("metadata") or {}
        status = "canceled" if etype == "customer.subscription.deleted" \
            else str(obj.get("status") or "")
        created = event.get("created")
        return store.subscription_changed(
            eid, etype, created if isinstance(created, int) else 0, sub_id,
            _id(obj.get("customer")), meta.get("device_id") or None, status,
            _period_end(obj),
            bool(obj.get("cancel_at_period_end")) or bool(obj.get("cancel_at")))

    return store.record_event(eid, etype)
