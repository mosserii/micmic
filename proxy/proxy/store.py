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
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
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
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    received_at INTEGER NOT NULL
);
"""

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
)


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
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------- tokens

    def _insert_token(self, c: sqlite3.Connection, plan: str, label: str,
                      app_version: str) -> tuple[str, str]:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        device_id = new_device_id()
        c.execute(
            "INSERT INTO tokens (token_hash, plan, label, created_at, device_id, app_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (hash_token(token), plan, label, int(time.time()), device_id, app_version))
        return token, device_id

    def mint(self, plan: str, label: str = "") -> str:
        if plan not in PLANS:
            raise ValueError(f"plan must be one of {PLANS}")
        return self._insert_token(self._conn(), plan, label, "")[0]

    def register_device(self, name: str, app_version: str, addr_hash: str, day: str,
                        per_addr: int, per_day: int = 0) -> tuple[str, str] | None:
        """A new free device, unless this address (or everyone, when per_day > 0) has
        had its sign-ups for the day. Returns (token, device_id) or None."""
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
            out = self._insert_token(c, "free", name, app_version)
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
                     "revoked_at")

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

    def plan_for(self, token: str) -> tuple[str, str] | None:
        """(token_hash, plan) for a live token, None for unknown or revoked alike."""
        h = hash_token(token)
        row = self._conn().execute(
            "SELECT plan, revoked_at FROM tokens WHERE token_hash = ?", (h,)).fetchone()
        if row is None or row[1] is not None:
            return None
        return h, row[0]

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

    def list_tokens(self, day: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT t.token_hash, t.plan, t.label, t.created_at, t.revoked_at, t.device_id, "
            "  COALESCE((SELECT n FROM usage u WHERE u.token_hash = t.token_hash "
            "            AND u.day = ? AND u.kind = 'jev'), 0), "
            "  COALESCE((SELECT n FROM usage u WHERE u.token_hash = t.token_hash "
            "            AND u.day = ? AND u.kind = 'gemini'), 0) "
            "FROM tokens t ORDER BY t.created_at", (day, day)).fetchall()
        return [{"token_hash": r[0], "plan": r[1], "label": r[2], "created_at": r[3],
                 "revoked_at": r[4], "device_id": r[5], "jev_today": r[6],
                 "gemini_today": r[7]} for r in rows]

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
