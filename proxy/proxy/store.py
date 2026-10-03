"""Device tokens and daily usage, in one SQLite file.

A token is never stored, only its SHA-256. The token is 256 bits of randomness, so the
hash cannot be reversed and a stolen database does not hand anyone a working token.
It also makes lookup a plain equality on the hash, which is why no constant-time
comparison is needed: an attacker cannot steer which hash bytes their guess produces.

Metering is a check-and-increment inside one BEGIN IMMEDIATE transaction. IMMEDIATE
takes SQLite's write lock before the read, so two requests racing for the last unit
of a cap are serialised by the database itself, across threads and across processes.
Device sign-up and Stripe fulfilment use the same pattern: the per-address sign-up
count and the processed-event record are checked and written in the transaction that
does the work, so a race or a webhook redelivery can never do it twice.

Each token row is also a device: a public `device_id` (safe to show, to put in a
Stripe session, to log) and, once it has paid, its Stripe customer and subscription.

MicMic Pro on iPhone is an Apple subscription (apple.py): `apple_subscriptions` holds one
row per original transaction id and `apple_links` ties it to at most a few devices. Apple
never writes `tokens.plan` and Stripe never touches the Apple rows; plan_for() answers
"pro" when either says so, and an Apple subscription ends by itself at its expiry date
(plus grace and a leeway) even if every notification about it were lost.

Account deletion (delete_account) is the one place that removes a device: it is the
user's own request from the app's Settings, required by App Store guideline 5.1.1(v),
and it removes only what this server keeps about that device.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

PLANS = ("free", "pro")
TOKEN_PREFIX = "mmp_"
DEVICE_PREFIX = "dev_"
# Subscription states that keep Pro. past_due keeps it while Stripe retries the card.
PRO_STATUSES = ("active", "trialing", "past_due")
# States a subscription never comes back from.
TERMINAL_STATUSES = ("canceled", "incomplete_expired", "unpaid")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    plan       TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS usage (
    token_hash TEXT NOT NULL,
    day        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    n          INTEGER NOT NULL,
    PRIMARY KEY (token_hash, day, kind)
);
CREATE TABLE IF NOT EXISTS signups (
    addr_hash TEXT NOT NULL,
    day       TEXT NOT NULL,
    n         INTEGER NOT NULL,
    PRIMARY KEY (addr_hash, day)
);
CREATE TABLE IF NOT EXISTS tts_seen (
    key_id  TEXT PRIMARY KEY,
    n       INTEGER NOT NULL,
    last_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    received_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS apple_subscriptions (
    original_tx_id         TEXT PRIMARY KEY,
    environment            TEXT NOT NULL,
    product_id             TEXT NOT NULL,
    auto_renew_product_id  TEXT,
    status                 TEXT NOT NULL,
    expires_at             INTEGER,
    grace_until            INTEGER,
    revoked_at             INTEGER,
    auto_renew             INTEGER NOT NULL DEFAULT 1,
    app_account_token      TEXT,
    last_signed_at         INTEGER NOT NULL DEFAULT 0,
    updated_at             INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS apple_links (
    original_tx_id TEXT NOT NULL,
    device_id      TEXT NOT NULL,
    linked_at      INTEGER NOT NULL,
    seq            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (original_tx_id, device_id)
);
CREATE INDEX IF NOT EXISTS apple_links_device ON apple_links (device_id);
CREATE TABLE IF NOT EXISTS apple_events (
    notification_uuid TEXT PRIMARY KEY,
    type              TEXT NOT NULL,
    subtype           TEXT,
    outcome           TEXT NOT NULL,
    received_at       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deletions (
    day TEXT PRIMARY KEY,
    n   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS device_links (
    child_device_id  TEXT PRIMARY KEY,
    parent_device_id TEXT NOT NULL,
    created_at       INTEGER NOT NULL
);
"""

# Apple subscription states (apple_subscriptions.status).
APPLE_ENDED = ("expired", "revoked")
# A subscription with this status, not revoked, gives Pro until
# MAX(expires_at, grace_until) + leeway. The two ended states never do.
_APPLE_LIVE = (
    "s.revoked_at IS NULL AND s.status NOT IN ('expired', 'revoked') "
    "AND MAX(COALESCE(s.expires_at, 0), COALESCE(s.grace_until, 0)) + ? > ?")

# Columns added to tokens after the first release. Existing databases get them by
# ALTER TABLE at startup; a new one gets them the same way, so there is one path.
_TOKEN_COLUMNS = (
    ("device_id", "TEXT"),
    ("app_version", "TEXT NOT NULL DEFAULT ''"),
    ("stripe_customer_id", "TEXT"),
    ("stripe_subscription_id", "TEXT"),
    ("sub_status", "TEXT"),
    ("sub_period_end", "INTEGER"),
    ("sub_cancel_at_period_end", "INTEGER NOT NULL DEFAULT 0"),
    # Stripe's `created` of the last event applied to the subscription columns, so a
    # late, older event cannot overwrite a newer state.
    ("sub_event_at", "INTEGER NOT NULL DEFAULT 0"),
    # MicMic for iPhone (PURCHASES_ACCOUNT.md 5.2). "mac" for every row made before.
    ("platform", "TEXT NOT NULL DEFAULT 'mac'"),
    # A uuid4 the phone passes to StoreKit as appAccountToken, so a server notification
    # that arrives before the app reports the purchase still finds the device.
    ("apple_account_token", "TEXT"),
    # The terms and AI-data consent: the versions accepted, the providers named, the
    # phone's own time, the server's time (authoritative), locale and method.
    ("consent_terms", "TEXT"),
    ("consent_privacy", "TEXT"),
    ("consent_ai", "TEXT"),
    ("consent_providers", "TEXT"),
    ("consent_client_at", "TEXT"),
    ("consent_at", "INTEGER"),
    ("consent_locale", "TEXT"),
    ("consent_method", "TEXT"),
    ("consent_age18", "INTEGER"),
)

CONSENT_COLUMNS = ("consent_terms", "consent_privacy", "consent_ai", "consent_providers",
                   "consent_client_at", "consent_at", "consent_locale", "consent_method",
                   "consent_age18")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_device_id() -> str:
    return DEVICE_PREFIX + secrets.token_hex(8)


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        c = self._conn()
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
        self._migrate(c)
        try:
            # The file holds who used what and when; nobody else on the box needs it.
            self.path.chmod(0o600)
        except OSError:
            pass

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            # isolation_level=None: we issue BEGIN ourselves, so the module never opens
            # an implicit DEFERRED transaction behind our back.
            c = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def _migrate(self, c: sqlite3.Connection) -> None:
        # IMMEDIATE so two processes starting at once do not both try the same ALTER.
        c.execute("BEGIN IMMEDIATE")
        try:
            have = {r[1] for r in c.execute("PRAGMA table_info(tokens)")}
            for name, decl in _TOKEN_COLUMNS:
                if name not in have:
                    c.execute(f"ALTER TABLE tokens ADD COLUMN {name} {decl}")
            # Tokens minted before devices existed get an id of their own.
            for (h,) in c.execute(
                    "SELECT token_hash FROM tokens WHERE device_id IS NULL").fetchall():
                c.execute("UPDATE tokens SET device_id = ? WHERE token_hash = ?",
                          (new_device_id(), h))
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS tokens_device ON tokens (device_id)")
            c.execute("CREATE INDEX IF NOT EXISTS tokens_sub ON tokens (stripe_subscription_id)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS tokens_apple_token "
                      "ON tokens (apple_account_token)")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------- tokens

    def _insert_token(self, c: sqlite3.Connection, plan: str, label: str,
                      app_version: str, platform: str = "mac",
                      consent: dict | None = None) -> tuple[str, str]:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        device_id = new_device_id()
        consent = consent or {}
        cols = ["token_hash", "plan", "label", "created_at", "device_id", "app_version",
                "platform", "apple_account_token"] + [k for k in CONSENT_COLUMNS if k in consent]
        vals = [hash_token(token), plan, label, int(time.time()), device_id, app_version,
                platform, str(uuid.uuid4())] + [consent[k] for k in CONSENT_COLUMNS if k in consent]
        c.execute(f"INSERT INTO tokens ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                  vals)
        return token, device_id

    def mint(self, plan: str, label: str = "") -> str:
        if plan not in PLANS:
            raise ValueError(f"plan must be one of {PLANS}")
        return self._insert_token(self._conn(), plan, label, "")[0]

    def register_device(self, name: str, app_version: str, addr_hash: str, day: str,
                        per_addr: int, per_day: int = 0, platform: str = "mac",
                        consent: dict | None = None) -> tuple[str, str] | None:
        """A new free device, unless this address (or everyone, when per_day > 0) has
        had its sign-ups for the day. Returns (token, device_id) or None. `consent` maps
        CONSENT_COLUMNS to values already checked by the caller."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT n FROM signups WHERE addr_hash = ? AND day = ?",
                            (addr_hash, day)).fetchone()
            if row and row[0] >= per_addr:
                c.execute("ROLLBACK")
                return None
            if per_day > 0:
                total = c.execute("SELECT COALESCE(SUM(n), 0) FROM signups WHERE day = ?",
                                  (day,)).fetchone()[0]
                if total >= per_day:
                    c.execute("ROLLBACK")
                    return None
            # Yesterday's counts are no longer needed; the table stays one day big.
            c.execute("DELETE FROM signups WHERE day < ?", (day,))
            c.execute(
                "INSERT INTO signups (addr_hash, day, n) VALUES (?, ?, 1) "
                "ON CONFLICT (addr_hash, day) DO UPDATE SET n = n + 1", (addr_hash, day))
            out = self._insert_token(c, "free", name, app_version, platform, consent)
            c.execute("COMMIT")
            return out
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    _ACCOUNT_COLS = ("token_hash", "plan", "label", "device_id", "app_version",
                     "stripe_customer_id", "stripe_subscription_id", "sub_status",
                     "sub_period_end", "sub_cancel_at_period_end", "sub_event_at",
                     "revoked_at", "platform", "apple_account_token") + CONSENT_COLUMNS

    def _row(self, where: str, arg: str, c: sqlite3.Connection | None = None) -> dict | None:
        c = c or self._conn()
        row = c.execute(f"SELECT {', '.join(self._ACCOUNT_COLS)} FROM tokens WHERE {where} = ?",
                        (arg,)).fetchone()
        return dict(zip(self._ACCOUNT_COLS, row)) if row else None

    def account(self, token_hash: str) -> dict | None:
        return self._row("token_hash", token_hash)

    def device(self, device_id: str) -> dict | None:
        return self._row("device_id", device_id)

    def ping(self) -> None:
        self._conn().execute("SELECT 1 FROM tokens LIMIT 1").fetchall()

    def plan_for(self, token: str, now: int | None = None,
                 leeway: int = 3600) -> tuple[str, str] | None:
        """(token_hash, effective plan) for a live token, None for unknown or revoked
        alike. The plan is "pro" when tokens.plan says so (Stripe, the owner's admin
        command) or when a live Apple subscription is linked to the device."""
        h = hash_token(token)
        now = int(time.time()) if now is None else now
        row = self._conn().execute(
            "SELECT t.plan, t.revoked_at, EXISTS (SELECT 1 FROM apple_links l "
            "  JOIN apple_subscriptions s ON s.original_tx_id = l.original_tx_id "
            f"  WHERE l.device_id = t.device_id AND {_APPLE_LIVE}) "
            "FROM tokens t WHERE t.token_hash = ?", (leeway, now, h)).fetchone()
        if row is None or row[1] is not None:
            return None
        return h, ("pro" if row[0] == "pro" or row[2] else row[0])

    def resolve_prefix(self, prefix: str) -> list[str]:
        prefix = prefix.lower()
        rows = self._conn().execute(
            "SELECT token_hash FROM tokens WHERE substr(token_hash, 1, ?) = ?",
            (len(prefix), prefix)).fetchall()
        return [r[0] for r in rows]

    def revoke(self, token_hash: str) -> bool:
        cur = self._conn().execute(
            "UPDATE tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (int(time.time()), token_hash))
        return cur.rowcount == 1

    def set_plan(self, token_hash: str, plan: str) -> bool:
        if plan not in PLANS:
            raise ValueError(f"plan must be one of {PLANS}")
        cur = self._conn().execute(
            "UPDATE tokens SET plan = ? WHERE token_hash = ?", (plan, token_hash))
        return cur.rowcount == 1

    def list_tokens(self, day: str, now: int | None = None, leeway: int = 3600) -> list[dict]:
        now = int(time.time()) if now is None else now
        rows = self._conn().execute(
            "SELECT t.token_hash, t.plan, t.label, t.created_at, t.revoked_at, t.device_id, "
            "  COALESCE((SELECT n FROM usage u WHERE u.token_hash = t.token_hash "
            "            AND u.day = ? AND u.kind = 'jev'), 0), "
            "  COALESCE((SELECT n FROM usage u WHERE u.token_hash = t.token_hash "
            "            AND u.day = ? AND u.kind = 'gemini'), 0), "
            "  t.platform, t.stripe_subscription_id, t.sub_status "
            "FROM tokens t ORDER BY t.created_at", (day, day)).fetchall()
        out = []
        for r in rows:
            apple = self.apple_for_device(r[5], now, leeway)
            out.append({"token_hash": r[0], "plan": r[1], "label": r[2], "created_at": r[3],
                        "revoked_at": r[4], "device_id": r[5], "jev_today": r[6],
                        "gemini_today": r[7], "platform": r[8],
                        "pro_source": _pro_source(r[1], r[9], r[10], apple),
                        "apple_env": apple["environment"] if apple else None})
        return out

    # ------------------------------------------------------------- metering

    def consume(self, token_hash: str, kind: str, day: str, limit: int,
                units: int = 1) -> tuple[bool, int]:
        """Take `units` if that many are left today. Returns (granted, used_after)."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT n FROM usage WHERE token_hash = ? AND day = ? AND kind = ?",
                            (token_hash, day, kind)).fetchone()
            used = row[0] if row else 0
            if used + units > limit:
                c.execute("ROLLBACK")
                return False, used
            c.execute(
                "INSERT INTO usage (token_hash, day, kind, n) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (token_hash, day, kind) DO UPDATE SET n = n + excluded.n",
                (token_hash, day, kind, units))
            c.execute("COMMIT")
            return True, used + units
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def consume_all(self, token_hash: str, kind: str, caps: list[tuple[str, int]],
                    units: int) -> tuple[bool, str | None]:
        """Take `units` from every (bucket, limit) in caps, or from none of them, in one
        transaction. Returns (granted, the first bucket that was full)."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            for bucket, limit in caps:
                row = c.execute("SELECT n FROM usage WHERE token_hash = ? AND day = ? AND kind = ?",
                                (token_hash, bucket, kind)).fetchone()
                if (row[0] if row else 0) + units > limit:
                    c.execute("ROLLBACK")
                    return False, bucket
            for bucket, _ in caps:
                c.execute(
                    "INSERT INTO usage (token_hash, day, kind, n) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (token_hash, day, kind) DO UPDATE SET n = n + excluded.n",
                    (token_hash, bucket, kind, units))
            c.execute("COMMIT")
            return True, None
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def seen(self, key_id: str, now: int, forget_after: int = 30 * 86400) -> int:
        """Count one more request for a cache key and return the count. Only the key's
        hash is kept, never the words; a key nobody asked for again within
        forget_after seconds is forgotten (pruned here, a few rows at a time)."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM tts_seen WHERE rowid IN (SELECT rowid FROM tts_seen "
                      "WHERE last_at < ? LIMIT 50)", (now - forget_after,))
            c.execute("INSERT INTO tts_seen (key_id, n, last_at) VALUES (?, 1, ?) "
                      "ON CONFLICT (key_id) DO UPDATE SET n = n + 1, last_at = excluded.last_at",
                      (key_id, now))
            n = c.execute("SELECT n FROM tts_seen WHERE key_id = ?", (key_id,)).fetchone()[0]
            c.execute("COMMIT")
            return n
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def refund(self, token_hash: str, kind: str, day: str, units: int = 1) -> None:
        """Give units back when the upstream refused the call, so a Jev outage does
        not eat a user's day. Only ever decreases, so it can never push anyone over."""
        self._conn().execute(
            "UPDATE usage SET n = MAX(0, n - ?) WHERE token_hash = ? AND day = ? AND kind = ?",
            (units, token_hash, day, kind))

    def used(self, token_hash: str, kind: str, day: str) -> int:
        row = self._conn().execute(
            "SELECT n FROM usage WHERE token_hash = ? AND day = ? AND kind = ?",
            (token_hash, day, kind)).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------- stripe

    def _event(self, event_id: str, etype: str, apply) -> str:
        """Run apply(conn) -> outcome once per Stripe event id. A redelivery returns
        "duplicate" and changes nothing. If apply raises, nothing is recorded, so
        Stripe's retry gets a second chance."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            if c.execute("SELECT 1 FROM stripe_events WHERE event_id = ?",
                         (event_id,)).fetchone():
                c.execute("ROLLBACK")
                return "duplicate"
            outcome = apply(c)
            c.execute("INSERT INTO stripe_events (event_id, type, outcome, received_at) "
                      "VALUES (?, ?, ?, ?)", (event_id, etype, outcome, int(time.time())))
            c.execute("COMMIT")
            return outcome
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def record_event(self, event_id: str, etype: str, outcome: str = "ignored") -> str:
        return self._event(event_id, etype, lambda c: outcome)

    def checkout_completed(self, event_id: str, etype: str, device_id: str,
                           customer_id: str | None, subscription_id: str | None,
                           paid: bool) -> str:
        def apply(c: sqlite3.Connection) -> str:
            d = self._row("device_id", device_id, c)
            if d is None:
                return "unknown_device"
            same = subscription_id and d["stripe_subscription_id"] == subscription_id
            if same and d["sub_status"] in TERMINAL_STATUSES:
                # The subscription already ended (events arrived out of order).
                c.execute("UPDATE tokens SET stripe_customer_id = COALESCE(stripe_customer_id, ?)"
                          " WHERE device_id = ?", (customer_id, device_id))
                return "already_ended"
            status = d["sub_status"] if same and d["sub_status"] else (
                "active" if paid else "incomplete")
            c.execute(
                "UPDATE tokens SET stripe_customer_id = ?, stripe_subscription_id = ?, "
                "  sub_status = ?, sub_period_end = ?, sub_cancel_at_period_end = ?, "
                "  sub_event_at = ?, plan = CASE WHEN ? THEN 'pro' ELSE plan END "
                "WHERE device_id = ?",
                (customer_id or d["stripe_customer_id"], subscription_id, status,
                 d["sub_period_end"] if same else None,
                 d["sub_cancel_at_period_end"] if same else 0,
                 # Ordering is kept among subscription events only: a checkout event
                 # says nothing about the period, so it must not make a later
                 # customer.subscription.updated look stale.
                 d["sub_event_at"] if same else 0, 1 if paid else 0, device_id))
            return "pro" if paid else "awaiting_payment"
        return self._event(event_id, etype, apply)

    def subscription_changed(self, event_id: str, etype: str, created: int,
                             subscription_id: str, customer_id: str | None,
                             device_id: str | None, status: str, period_end: int | None,
                             cancel_at_period_end: bool) -> str:
        def apply(c: sqlite3.Connection) -> str:
            d = self._row("stripe_subscription_id", subscription_id, c)
            if d is None and device_id:
                d = self._row("device_id", device_id, c)
            if d is None:
                return "unknown_device"
            cur_sub, cur_status = d["stripe_subscription_id"], d["sub_status"]
            if cur_sub and cur_sub != subscription_id and cur_status in PRO_STATUSES:
                # An event about some other subscription of this device (an old,
                # replaced one) must not demote the one that is paying now.
                return "other_subscription"
            if cur_sub == subscription_id:
                if created < d["sub_event_at"]:
                    return "stale"
                if status == "incomplete" and cur_status in PRO_STATUSES:
                    return "stale"
            plan = "pro" if status in PRO_STATUSES else "free"
            c.execute(
                "UPDATE tokens SET stripe_subscription_id = ?, "
                "  stripe_customer_id = COALESCE(?, stripe_customer_id), sub_status = ?, "
                "  sub_period_end = COALESCE(?, sub_period_end), "
                "  sub_cancel_at_period_end = ?, sub_event_at = ?, plan = ? "
                "WHERE device_id = ?",
                (subscription_id, customer_id, status, period_end,
                 1 if cancel_at_period_end else 0, created, plan, d["device_id"]))
            return plan
        return self._event(event_id, etype, apply)

    # ------------------------------------------------------------- iPhone: account token

    def apple_account_token(self, token_hash: str) -> str | None:
        """The device's StoreKit appAccountToken, minted once (rows made before it
        existed get one the first time it is asked for)."""
        c = self._conn()
        row = c.execute("SELECT apple_account_token FROM tokens WHERE token_hash = ?",
                        (token_hash,)).fetchone()
        if row is None:
            return None
        if row[0]:
            return row[0]
        c.execute("UPDATE tokens SET apple_account_token = ? "
                  "WHERE token_hash = ? AND apple_account_token IS NULL",
                  (str(uuid.uuid4()), token_hash))
        row = c.execute("SELECT apple_account_token FROM tokens WHERE token_hash = ?",
                        (token_hash,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------- iPhone: consent

    def set_consent(self, token_hash: str, consent: dict) -> bool:
        """Replace the device's consent record. `consent` maps CONSENT_COLUMNS to values
        already checked by the caller."""
        keys = [k for k in CONSENT_COLUMNS if k in consent]
        cur = self._conn().execute(
            f"UPDATE tokens SET {', '.join(k + ' = ?' for k in keys)} WHERE token_hash = ?",
            [consent[k] for k in keys] + [token_hash])
        return cur.rowcount == 1

    # ------------------------------------------------------------- iPhone: Apple

    _APPLE_COLS = ("original_tx_id", "environment", "product_id", "auto_renew_product_id",
                   "status", "expires_at", "grace_until", "revoked_at", "auto_renew",
                   "app_account_token", "last_signed_at", "updated_at")

    def _apple_row(self, c: sqlite3.Connection, otid: str) -> dict | None:
        row = c.execute(f"SELECT {', '.join(self._APPLE_COLS)} FROM apple_subscriptions "
                        "WHERE original_tx_id = ?", (otid,)).fetchone()
        return dict(zip(self._APPLE_COLS, row)) if row else None

    @staticmethod
    def _live(s: dict, now: int, leeway: int) -> bool:
        return (s["revoked_at"] is None and s["status"] not in APPLE_ENDED
                and max(s["expires_at"] or 0, s["grace_until"] or 0) + leeway > now)

    def apple_for_device(self, device_id: str, now: int, leeway: int = 3600) -> dict | None:
        """The Apple subscription that matters for this device: a live one first, then
        the one that ends last. `status` is what is true now (a lapsed row reads
        "expired" even before Apple says so); `live` is whether it gives Pro."""
        rows = self._conn().execute(
            f"SELECT {', '.join('s.' + k for k in self._APPLE_COLS)} FROM apple_links l "
            "JOIN apple_subscriptions s ON s.original_tx_id = l.original_tx_id "
            "WHERE l.device_id = ?", (device_id,)).fetchall()
        subs = [dict(zip(self._APPLE_COLS, r)) for r in rows]
        if not subs:
            return None
        for s in subs:
            s["live"] = self._live(s, now, leeway)
            if s["revoked_at"] is not None:
                s["status"] = "revoked"
            elif not s["live"] and s["status"] not in APPLE_ENDED:
                s["status"] = "expired"
        subs.sort(key=lambda s: (s["live"], s["expires_at"] or 0), reverse=True)
        return subs[0]

    def _link(self, c: sqlite3.Connection, otid: str, device_id: str, now: int,
              max_devices: int) -> None:
        """Link a device to a subscription. Past max_devices, the least recently linked
        other device is unlinked: "all of your devices" keeps working, and one copied
        transaction cannot unlock a crowd."""
        seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM apple_links").fetchone()[0]
        c.execute("INSERT INTO apple_links (original_tx_id, device_id, linked_at, seq) "
                  "VALUES (?, ?, ?, ?) ON CONFLICT (original_tx_id, device_id) DO UPDATE "
                  "SET linked_at = excluded.linked_at, seq = excluded.seq",
                  (otid, device_id, now, seq))
        n = c.execute("SELECT COUNT(*) FROM apple_links WHERE original_tx_id = ?",
                      (otid,)).fetchone()[0]
        if n > max(1, max_devices):
            c.execute("DELETE FROM apple_links WHERE rowid IN (SELECT rowid FROM apple_links "
                      "WHERE original_tx_id = ? AND device_id != ? ORDER BY seq LIMIT ?)",
                      (otid, device_id, n - max(1, max_devices)))

    def _write_apple(self, c: sqlite3.Connection, s: dict, now: int) -> None:
        s = dict(s, updated_at=now)
        cols = self._APPLE_COLS
        c.execute(f"INSERT INTO apple_subscriptions ({', '.join(cols)}) VALUES "
                  f"({', '.join('?' * len(cols))}) ON CONFLICT (original_tx_id) DO UPDATE SET "
                  + ", ".join(f"{k} = excluded.{k}" for k in cols[1:]),
                  [s[k] for k in cols])

    def _tx(self, work):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            out = work(c)
            c.execute("COMMIT")
            return out
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def apple_apply_transaction(self, device_id: str, tx: dict, now: int,
                                max_devices: int, leeway: int) -> dict:
        """A verified transaction the phone posted. Returns the subscription as it stands
        afterwards, with `outcome` "pro", "expired" or "revoked". A transaction signed
        before the stored state never overwrites it; an ended subscription the server
        never saw is not stored at all, and a device is linked only to a live one."""
        otid = tx["original_tx_id"]
        exp = tx["expires_ms"] // 1000
        revoked = tx["revocation_ms"] // 1000 if tx["revocation_ms"] else None

        def work(c: sqlite3.Connection) -> dict:
            row = self._apple_row(c, otid)
            if row is None:
                if revoked is not None:
                    return {"outcome": "revoked", "expires_at": exp, "auto_renew": True}
                if exp + leeway <= now:
                    return {"outcome": "expired", "expires_at": exp, "auto_renew": True}
                row = {"original_tx_id": otid, "auto_renew_product_id": None,
                       "status": "active", "expires_at": exp, "grace_until": None,
                       "revoked_at": None, "auto_renew": 1, "last_signed_at": 0}
                fresh = True
            else:
                fresh = tx["signed_ms"] >= row["last_signed_at"]
            if fresh:
                renewed = exp > (row["expires_at"] or 0)
                row.update(environment=tx["environment"], product_id=tx["product_id"],
                           app_account_token=tx["app_account_token"] or row.get(
                               "app_account_token"),
                           last_signed_at=max(tx["signed_ms"], row["last_signed_at"]))
                if revoked is not None:
                    row.update(status="revoked", revoked_at=revoked)
                else:
                    row["revoked_at"] = None
                    if renewed:
                        row.update(status="active", grace_until=None, expires_at=exp)
                    elif row["status"] in APPLE_ENDED and exp + leeway > now:
                        row["status"] = "active"
                self._write_apple(c, row, now)
            elif (revoked is None and row["revoked_at"] is None
                  and exp > (row["expires_at"] or 0)):
                # Signed before the stored state, but proof of a later paid period (a
                # renewal whose DID_RENEW was lost behind a newer notification). A later
                # expiry is never "older state"; only a refund could undo it.
                row.update(status="active", grace_until=None, expires_at=exp)
                self._write_apple(c, row, now)
            live = self._live(row, now, leeway)
            if live:
                self._link(c, otid, device_id, now, max_devices)
            outcome = "pro" if live else (
                "revoked" if row["revoked_at"] is not None or row["status"] == "revoked"
                else "expired")
            return {"outcome": outcome, "expires_at": max(row["expires_at"] or 0,
                                                          row["grace_until"] or 0),
                    "auto_renew": bool(row["auto_renew"])}
        return self._tx(work)

    # Notification types (App Store Server Notifications V2) that change nothing here.
    APPLE_IGNORED = ("PRICE_INCREASE", "PRICE_CHANGE", "REFUND_DECLINED", "CONSUMPTION_REQUEST",
                     "METADATA_UPDATE", "MIGRATION", "ONE_TIME_CHARGE",
                     "EXTERNAL_PURCHASE_TOKEN", "RESCIND_CONSENT")

    def apple_notification(self, n: dict, now: int, product_ids: tuple[str, ...],
                           max_devices: int, leeway: int) -> str:
        """Apply one verified notification, once per notificationUUID (the stripe_events
        pattern: recorded in the same transaction as its effect; if applying raises,
        nothing is recorded and Apple's retry gets another chance)."""
        def apply(c: sqlite3.Connection) -> str:
            t, sub, tx, ren = n["type"], n["subtype"], n["tx"], n["renewal"] or {}
            if t == "TEST":
                return "test"
            if t in self.APPLE_IGNORED or not tx or not tx.get("original_tx_id"):
                return "ignored"
            if tx["product_id"] not in product_ids:
                return "unknown_product"
            otid = tx["original_tx_id"]
            row = self._apple_row(c, otid)
            device = None
            if row is None:
                tok = tx.get("app_account_token")
                if tok:
                    r = c.execute("SELECT device_id FROM tokens WHERE apple_account_token = ? "
                                  "AND revoked_at IS NULL", (tok,)).fetchone()
                    device = r[0] if r else None
                if device is None:
                    # No living device to tie it to: nothing is stored about it.
                    return "unknown_subscription"
                row = {"original_tx_id": otid, "auto_renew_product_id": None,
                       "status": "active", "expires_at": None, "grace_until": None,
                       "revoked_at": None, "auto_renew": 1, "last_signed_at": 0,
                       "app_account_token": tok}
            elif n["signed_ms"] < row["last_signed_at"] and t not in ("REFUND", "REVOKE"):
                return "stale"
            exp = tx["expires_ms"] // 1000 if tx.get("expires_ms") else None
            row.update(environment=tx["environment"] or n["environment"],
                       product_id=tx["product_id"],
                       last_signed_at=max(n["signed_ms"], row["last_signed_at"]))
            if row["expires_at"] is None:
                row["expires_at"] = exp
            outcome = None
            if t in ("SUBSCRIBED", "DID_RENEW"):
                row.update(status="active", expires_at=exp, grace_until=None, revoked_at=None)
            elif t in ("OFFER_REDEEMED", "RENEWAL_EXTENDED", "RENEWAL_EXTENSION"):
                if exp:
                    row["expires_at"] = exp
                if row["revoked_at"] is None:
                    row["status"] = "active"
            elif t == "DID_CHANGE_RENEWAL_STATUS":
                on = (sub == "AUTO_RENEW_ENABLED") if sub else ren.get("auto_renew") == 1
                row["auto_renew"] = 1 if on else 0
                outcome = "auto_renew_on" if on else "auto_renew_off"
            elif t == "DID_CHANGE_RENEWAL_PREF":
                row["auto_renew_product_id"] = ren.get("auto_renew_product_id")
                outcome = "renewal_pref"
            elif t == "DID_FAIL_TO_RENEW":
                if sub == "GRACE_PERIOD":
                    g = ren.get("grace_expires_ms")
                    row.update(status="grace", grace_until=g // 1000 if g else None)
                else:
                    row.update(status="billing_retry", grace_until=None)
            elif t in ("GRACE_PERIOD_EXPIRED", "EXPIRED"):
                row.update(status="expired", grace_until=None)
            elif t in ("REFUND", "REVOKE"):
                r = tx.get("revocation_ms")
                row.update(status="revoked", revoked_at=r // 1000 if r else now)
            elif t == "REFUND_REVERSED":
                row["revoked_at"] = None
                row["status"] = ("active" if (row["expires_at"] or 0) + leeway > now
                                 else "expired")
            else:
                return "ignored"
            if exp and exp > (row["expires_at"] or 0) and t not in ("REFUND", "REVOKE"):
                # Every transaction Apple signs carries the latest period's end.
                row["expires_at"] = exp
            self._write_apple(c, row, now)
            if device is not None:
                self._link(c, otid, device, now, max_devices)
            return outcome or row["status"]

        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            if c.execute("SELECT 1 FROM apple_events WHERE notification_uuid = ?",
                         (n["notification_uuid"],)).fetchone():
                c.execute("ROLLBACK")
                return "duplicate"
            outcome = apply(c)
            c.execute("INSERT INTO apple_events (notification_uuid, type, subtype, outcome, "
                      "received_at) VALUES (?, ?, ?, ?, ?)",
                      (n["notification_uuid"], n["type"], n["subtype"], outcome, now))
            c.execute("COMMIT")
            return outcome
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    # ------------------------------------------------------------- account deletion

    def delete_account(self, token_hash: str, now: int, leeway: int = 3600) -> dict | None:
        """Delete everything this server keeps about one device, in one transaction: its
        Apple links (and each subscription row no other device is linked to), its
        Mac-iPhone links, its usage counts and its row. Only an anonymous count of
        deletions per day is kept. None when the device does not exist."""
        day = time.strftime("%Y-%m-%d", time.gmtime(now))

        def work(c: sqlite3.Connection) -> dict | None:
            r = c.execute("SELECT device_id FROM tokens WHERE token_hash = ?",
                          (token_hash,)).fetchone()
            if r is None:
                return None
            dev = r[0]
            active = c.execute(
                "SELECT EXISTS (SELECT 1 FROM apple_links l JOIN apple_subscriptions s "
                f"ON s.original_tx_id = l.original_tx_id WHERE l.device_id = ? AND {_APPLE_LIVE})",
                (dev, leeway, now)).fetchone()[0]
            otids = [x[0] for x in c.execute(
                "SELECT original_tx_id FROM apple_links WHERE device_id = ?", (dev,))]
            c.execute("DELETE FROM apple_links WHERE device_id = ?", (dev,))
            for otid in otids:
                c.execute("DELETE FROM apple_subscriptions WHERE original_tx_id = ? AND NOT "
                          "EXISTS (SELECT 1 FROM apple_links WHERE original_tx_id = ?)",
                          (otid, otid))
            c.execute("DELETE FROM device_links WHERE child_device_id = ? OR parent_device_id = ?",
                      (dev, dev))
            c.execute("DELETE FROM usage WHERE token_hash = ?", (token_hash,))
            c.execute("DELETE FROM tokens WHERE token_hash = ?", (token_hash,))
            c.execute("INSERT INTO deletions (day, n) VALUES (?, 1) "
                      "ON CONFLICT (day) DO UPDATE SET n = n + 1", (day,))
            return {"device_id": dev, "apple_subscription_active": bool(active)}
        return self._tx(work)


def _pro_source(plan: str, stripe_sub: str | None, sub_status: str | None,
                apple: dict | None) -> str | None:
    """Where a device's Pro comes from: "stripe", "admin" (the owner's `admin plan`),
    "apple", or None when it is on the free plan."""
    if plan == "pro":
        return "stripe" if stripe_sub and sub_status in PRO_STATUSES else "admin"
    if apple and apple["live"]:
        return "apple"
    return None


pro_source = _pro_source
