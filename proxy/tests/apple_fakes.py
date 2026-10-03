"""A fake Apple PKI and fake App Store signed data, so the real verifier in apple.py
(Apple's own SignedDataVerifier) checks real ES256 signatures and real certificate chains
without reaching Apple. Nothing here talks to the network.

The chain mirrors Apple's: a root CA, an intermediate carrying Apple's WWDR OID
1.2.840.113635.100.6.2.1, and a leaf carrying the App Store receipt-signing OID
1.2.840.113635.100.6.11.1. A second, unrelated root signs the "foreign" cases.

Every JWS and every id made here is remembered in SIGNED and SECRET_IDS, so a test can
prove none of them ever reached the proxy's log.
"""
from __future__ import annotations

import base64
import datetime
import json
import uuid
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

BUNDLE = "com.betterfly.micmic.ios"
PRODUCT = "com.betterfly.micmic.ios.pro.monthly"
APP_ID = 6700000001
SUB_TYPE = "Auto-Renewable Subscription"
WWDR_OID = "1.2.840.113635.100.6.2.1"
LEAF_OID = "1.2.840.113635.100.6.11.1"

SIGNED: list[str] = []
SECRET_IDS: list[str] = []


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fake Apple For Tests")])


def _cert(subject, issuer, pub, signer, ca: bool, path_len, extra_oid: str | None,
          issuer_pub=None) -> x509.Certificate:
    start = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
         .public_key(pub).serial_number(x509.random_serial_number())
         .not_valid_before(start).not_valid_after(start + datetime.timedelta(days=365 * 20))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=path_len), critical=True)
         .add_extension(x509.KeyUsage(
             digital_signature=not ca, content_commitment=False, key_encipherment=False,
             data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
             encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(pub), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_pub or pub),
                        critical=False))
    if extra_oid:
        b = b.add_extension(x509.UnrecognizedExtension(x509.ObjectIdentifier(extra_oid),
                                                       b"\x05\x00"), critical=False)
    return b.sign(signer, hashes.SHA256())


class FakePKI:
    def __init__(self, label: str = "Fake Apple Root CA"):
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        self.int_key = ec.generate_private_key(ec.SECP256R1())
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        root_name, int_name = _name(label), _name(label + " WWDR")
        self.root = _cert(root_name, root_name, self.root_key.public_key(), self.root_key,
                          True, None, None)
        self.intermediate = _cert(int_name, root_name, self.int_key.public_key(),
                                  self.root_key, True, 0, WWDR_OID,
                                  self.root_key.public_key())
        self.leaf = _cert(_name(label + " StoreKit"), int_name, self.leaf_key.public_key(),
                          self.int_key, False, None, LEAF_OID, self.int_key.public_key())

    def root_der(self) -> bytes:
        return self.root.public_bytes(serialization.Encoding.DER)

    def write_root(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "FakeAppleRoot.cer").write_bytes(self.root_der())
        return folder

    def sign(self, payload: dict, x5c: bool = True) -> str:
        chain = [base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode()
                 for c in (self.leaf, self.intermediate, self.root)]
        headers = {"x5c": chain} if x5c else {}
        pem = self.leaf_key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption())
        token = jwt.encode(payload, pem, algorithm="ES256", headers=headers)
        SIGNED.append(token)
        return token


def ms(seconds: int | float) -> int:
    return int(seconds * 1000)


def new_otid() -> str:
    v = str(2000000000000000 + uuid.uuid4().int % 999999999999)
    SECRET_IDS.append(v)
    return v


def tx_payload(*, now: int, otid: str | None = None, env: str = "Sandbox",
               product: str = PRODUCT, bundle: str = BUNDLE, expires_in: int = 30 * 86400,
               signed_at: int | None = None, revoked_at: int | None = None,
               tx_type: str = SUB_TYPE, ownership: str = "PURCHASED",
               app_account_token: str | None = None) -> dict:
    otid = otid or new_otid()
    tid = str(int(otid) + 1 + uuid.uuid4().int % 1000)
    SECRET_IDS.append(tid)
    if app_account_token:
        SECRET_IDS.append(app_account_token)
    p = {"transactionId": tid, "originalTransactionId": otid, "webOrderLineItemId": tid,
         "bundleId": bundle, "productId": product, "subscriptionGroupIdentifier": "21500000",
         "purchaseDate": ms(now - 60), "originalPurchaseDate": ms(now - 60),
         "expiresDate": ms(now + expires_in), "quantity": 1, "type": tx_type,
         "inAppOwnershipType": ownership, "signedDate": ms(signed_at if signed_at else now),
         "environment": env, "transactionReason": "PURCHASE", "storefront": "USA",
         "storefrontId": "143441", "price": 7990, "currency": "USD"}
    if app_account_token:
        p["appAccountToken"] = app_account_token
    if revoked_at is not None:
        p["revocationDate"] = ms(revoked_at)
        p["revocationReason"] = 0
    return p


def renewal_payload(*, now: int, otid: str, env: str = "Sandbox", auto_renew: int = 1,
                    grace_until: int | None = None) -> dict:
    p = {"originalTransactionId": otid, "autoRenewProductId": PRODUCT, "productId": PRODUCT,
         "autoRenewStatus": auto_renew, "signedDate": ms(now), "environment": env,
         "recentSubscriptionStartDate": ms(now - 60), "renewalDate": ms(now + 30 * 86400)}
    if grace_until is not None:
        p["gracePeriodExpiresDate"] = ms(grace_until)
        p["isInBillingRetryPeriod"] = True
    return p


def notification(pki: FakePKI, ntype: str, *, now: int, tx: dict | None,
                 subtype: str | None = None, renewal: dict | None = None,
                 env: str = "Sandbox", signed_at: int | None = None,
                 uuid_: str | None = None, bundle: str = BUNDLE,
                 app_id: int | None = APP_ID) -> str:
    data = {"environment": env, "bundleId": bundle, "bundleVersion": "1",
            "appAppleId": app_id, "status": 1}
    if tx is not None:
        data["signedTransactionInfo"] = pki.sign(tx)
    if renewal is not None:
        data["signedRenewalInfo"] = pki.sign(renewal)
    p = {"notificationType": ntype, "notificationUUID": uuid_ or str(uuid.uuid4()),
         "data": data, "version": "2.0", "signedDate": ms(signed_at if signed_at else now)}
    if subtype:
        p["subtype"] = subtype
    return pki.sign(p)


def tamper_payload(jws: str, **changes) -> str:
    """The same signature over a payload someone edited."""
    h, p, s = jws.split(".")
    body = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    body.update(changes)
    p2 = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    return f"{h}.{p2}.{s}"


def tamper_signature(jws: str) -> str:
    h, p, s = jws.split(".")
    raw = bytearray(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
    raw[5] ^= 0xFF
    return f"{h}.{p}.{base64.urlsafe_b64encode(bytes(raw)).rstrip(b'=').decode()}"
