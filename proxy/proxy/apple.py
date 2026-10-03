"""MicMic Pro on iPhone: Apple's signed transactions and App Store Server Notifications V2.

The only module that imports Apple's `app-store-server-library` (as billing.py is the only
one that imports stripe). Everything Apple signs is checked by the library's
SignedDataVerifier: the x5c chain up to one of Apple's root certificates in proxy/certs,
Apple's certificate OIDs, the ES256 signature, the bundle id, the environment and, in
Production, the app's numeric Apple ID, with online revocation (OCSP) checks on.

The environment claim is read once WITHOUT trusting it, only to choose which verifier to
ask; the chosen verifier then refuses anything whose verified environment differs.

What leaves this module is a plain dict of the few fields the store needs. Nothing here
logs a JWS, a transaction id or an app account token: callers log `short_id()` only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import (SignedDataVerifier,
                                                        VerificationException,
                                                        VerificationStatus)

from .config import APPLE_UNSIGNED_ENVIRONMENTS, Config

log = logging.getLogger("micmic.proxy")

ENVIRONMENTS = {"Production": Environment.PRODUCTION, "Sandbox": Environment.SANDBOX,
                "Xcode": Environment.XCODE, "LocalTesting": Environment.LOCAL_TESTING}
SUBSCRIPTION_TYPE = "Auto-Renewable Subscription"
# Family Sharing is off for MicMic Pro; FAMILY_SHARED is refused until it is turned on.
OWNERSHIP = ("PURCHASED",)
# A JWS is three base64url parts; a StoreKit transaction with its x5c chain is about 5 KB.
MAX_JWS = 15 * 1024


class AppleUnavailable(Exception):
    """This deploy cannot check Apple's signatures (no root certificates), or not for the
    environment asked. Answered 503 so Apple, and the phone, try again later."""


class RetryLater(Exception):
    """Apple's revocation service did not answer. Answered 503 apple_verify_retry."""


class Rejected(Exception):
    """Not a valid MicMic transaction or notification. `code` is the error answered."""

    def __init__(self, code: str, status: str = ""):
        super().__init__(code)
        self.code = code
        self.status = status          # the library's VerificationStatus name, for the log


def short_id(value: str | None) -> str:
    """The first 8 hex digits of sha256(value): enough to match two log lines, never the
    transaction id itself."""
    if not value:
        return "-"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def id_hash(value: str) -> str:
    """The full sha256 of an original transaction id, as /v1/account shows it. The phone
    holds the id and hashes it to compare; the server never hands the id back."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_roots(folder: Path) -> list[bytes]:
    """Apple's root certificates (DER), every *.cer in the folder, sorted by name."""
    try:
        files = sorted(p for p in Path(folder).glob("*.cer") if p.is_file())
    except OSError:
        return []
    return [p.read_bytes() for p in files]


def _peek(jws: str) -> dict:
    """The payload of a JWS, NOT verified. Used only to choose a verifier."""
    if not isinstance(jws, str) or not jws or len(jws) > MAX_JWS * 4:
        raise Rejected("bad_request")
    parts = jws.split(".")
    if len(parts) != 3:
        raise Rejected("bad_transaction", "not_a_jws")
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))
    except (ValueError, UnicodeError):
        raise Rejected("bad_transaction", "unreadable_payload") from None
    if not isinstance(payload, dict):
        raise Rejected("bad_transaction", "unreadable_payload")
    return payload


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _tx_dict(tx) -> dict:
    return {
        "original_tx_id": tx.originalTransactionId,
        "transaction_id": tx.transactionId,
        "product_id": tx.productId,
        "type": tx.rawType,
        "ownership": tx.rawInAppOwnershipType,
        "expires_ms": _int(tx.expiresDate),
        "revocation_ms": _int(tx.revocationDate),
        "signed_ms": _int(tx.signedDate) or 0,
        "app_account_token": (tx.appAccountToken or "").lower() or None,
        "environment": tx.rawEnvironment,
    }


class Apple:
    """One SignedDataVerifier per allowed environment, built once at startup."""

    def __init__(self, cfg: Config, roots: list[bytes] | None = None):
        self.cfg = cfg
        self.roots = load_roots(cfg.apple_root_certs_dir) if roots is None else roots
        self.verifiers: dict[str, SignedDataVerifier] = {}
        for name in cfg.apple_environments:
            env = ENVIRONMENTS.get(name)
            if env is None:
                continue
            if name in APPLE_UNSIGNED_ENVIRONMENTS and cfg.environment == "production":
                continue                      # the boot guard refuses this too
            if env == Environment.PRODUCTION and cfg.apple_app_id is None:
                continue                      # the library requires the app's Apple ID
            self.verifiers[name] = SignedDataVerifier(
                self.roots, cfg.apple_online_checks, env, cfg.apple_bundle_id,
                cfg.apple_app_id)

    @property
    def configured(self) -> bool:
        return bool(self.cfg.apple_bundle_id and self.roots and self.verifiers)

    @property
    def production_on(self) -> bool:
        return "Production" in self.verifiers

    def _verifier(self, env) -> SignedDataVerifier:
        if not isinstance(env, str) or env not in ENVIRONMENTS:
            raise Rejected("bad_environment", "unknown_environment")
        if env not in self.cfg.apple_environments:
            raise Rejected("bad_environment", "environment_not_allowed")
        v = self.verifiers.get(env)
        if v is None:
            # Allowed but not built: Production without APPLE_APP_ID.
            raise Rejected("bad_environment", "environment_not_configured")
        return v

    @staticmethod
    def _call(fn, arg, code: str):
        try:
            return fn(arg)
        except VerificationException as e:
            if e.status == VerificationStatus.RETRYABLE_VERIFICATION_FAILURE:
                raise RetryLater() from None
            raise Rejected(code, e.status.name) from None
        except Exception as e:  # noqa: BLE001  (a payload the models cannot read)
            raise Rejected(code, type(e).__name__) from None

    # ------------------------------------------------------------ transactions

    def verify_transaction(self, jws: str) -> dict:
        """A verified transaction for one of our products, as a plain dict. Raises
        Rejected (bad_transaction, bad_environment, unknown_product) or RetryLater."""
        if not self.configured:
            raise AppleUnavailable()
        if not isinstance(jws, str) or not jws or len(jws) > MAX_JWS:
            raise Rejected("bad_request")
        verifier = self._verifier(_peek(jws).get("environment"))
        tx = self._call(verifier.verify_and_decode_signed_transaction, jws, "bad_transaction")
        d = _tx_dict(tx)
        if not d["original_tx_id"]:
            raise Rejected("bad_transaction", "no_original_transaction_id")
        if d["product_id"] not in self.cfg.apple_product_ids or d["type"] != SUBSCRIPTION_TYPE:
            raise Rejected("unknown_product")
        if d["ownership"] not in OWNERSHIP:
            raise Rejected("bad_transaction", "ownership")
        if d["expires_ms"] is None:
            raise Rejected("bad_transaction", "no_expiry")
        return d

    # ------------------------------------------------------------ notifications

    def verify_notification(self, signed_payload: str) -> dict:
        """A verified V2 notification with its transaction and renewal info, decoded.
        Raises Rejected (bad_signature), RetryLater or AppleUnavailable."""
        if not self.configured:
            raise AppleUnavailable()
        if not isinstance(signed_payload, str) or not signed_payload:
            raise Rejected("bad_request")
        try:
            peek = _peek(signed_payload)
        except Rejected as e:
            raise Rejected("bad_signature", e.status) from None
        inner = peek.get("data") or peek.get("summary") or peek.get("appData") or {}
        env = inner.get("environment") if isinstance(inner, dict) else None
        if env is None and isinstance(peek.get("externalPurchaseToken"), dict):
            env = "Production"
        if env == "Production" and "Production" in self.cfg.apple_environments \
                and not self.production_on:
            # Apple keeps retrying for days: answered 503 until APPLE_APP_ID is set.
            raise AppleUnavailable()
        try:
            verifier = self._verifier(env)
        except Rejected as e:
            raise Rejected("bad_signature", e.status) from None
        n = self._call(verifier.verify_and_decode_notification, signed_payload, "bad_signature")
        out = {"notification_uuid": n.notificationUUID, "type": n.rawNotificationType,
               "subtype": n.rawSubtype, "signed_ms": _int(n.signedDate) or 0,
               "environment": env, "tx": None, "renewal": None}
        if not out["notification_uuid"] or not out["type"]:
            raise Rejected("bad_signature", "incomplete_notification")
        data = n.data
        if data is not None and data.signedTransactionInfo:
            tx = self._call(verifier.verify_and_decode_signed_transaction,
                            data.signedTransactionInfo, "bad_signature")
            out["tx"] = _tx_dict(tx)
        if data is not None and data.signedRenewalInfo:
            r = self._call(verifier.verify_and_decode_renewal_info,
                           data.signedRenewalInfo, "bad_signature")
            out["renewal"] = {
                "original_tx_id": r.originalTransactionId,
                "auto_renew_product_id": r.autoRenewProductId,
                "auto_renew": r.rawAutoRenewStatus,
                "grace_expires_ms": _int(r.gracePeriodExpiresDate),
            }
        return out
