"""Everything the proxy is told comes from the environment, read once at startup.

Upstream keys are read from the process environment first. For local development only,
a gitignored proxy/.env or the workspace .env.local next to the checkout may supply
them; only the two key names are ever read from those files, nothing else in them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROXY_ROOT = Path(__file__).resolve().parent.parent      # .../proxy
REPO_ROOT = PROXY_ROOT.parent                            # the checkout

JEV_UPSTREAM = "https://api.typesafe.ai/v1/systemone"
GEMINI_UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/models"


def _env_files() -> list[Path]:
    explicit = os.environ.get("MICMIC_PROXY_ENV_FILE")
    if explicit:
        return [Path(explicit).expanduser()]
    return [PROXY_ROOT / ".env", REPO_ROOT.parent / ".env.local", REPO_ROOT / ".env.local"]


def read_key(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    for p in _env_files():
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith(name + "="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    return None


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} must be an integer")


def _float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} must be a number")


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _opt(name: str) -> str | None:
    """Stripe and the public URL come from the process environment only, never from
    the local key files read_key() looks in."""
    return os.environ.get(name, "").strip() or None


def _public_base_url() -> str | None:
    url = _opt("PUBLIC_BASE_URL")
    if url:
        return url.rstrip("/")
    # Railway sets this to the service's generated domain (no scheme).
    domain = _opt("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{domain}" if domain else None


@dataclass
class Config:
    jev_key: str | None = None
    gemini_key: str | None = None
    host: str = "127.0.0.1"
    port: int = 8810
    db_path: Path = PROXY_ROOT / "data" / "proxy.db"
    jev_upstream: str = JEV_UPSTREAM
    gemini_upstream: str = GEMINI_UPSTREAM
    # Jev is the meter the product is priced on: roughly 2 to 3 calls per spoken request.
    # 300 and 6000 are the advertised 100 and 2,000 requests a day at 3 calls each.
    free_daily_calls: int = 300
    pro_daily_calls: int = 6000
    # Gemini gets its own, smaller cap rather than sharing Jev's. One Gemini call with a
    # screenshot can cost more than a whole Jev request, and a shared counter would let a
    # single chatty screen-description loop burn a user's entire day of voice requests.
    free_daily_gemini: int = 100
    pro_daily_gemini: int = 2000
    # Bodies are what someone said out loud (Jev) or a screenshot (Gemini), so the sizes
    # differ by an order of magnitude. Anything past these is not a MicMic request.
    max_jev_body: int = 256 * 1024
    max_gemini_body: int = 8 * 1024 * 1024
    # Under the app's own 12s, so the app always hears the proxy's answer (a 504) and
    # never gives up first and asks again while this call is still being counted.
    jev_timeout: float = 10.0
    gemini_timeout: float = 40.0
    # Only the models MicMic actually uses. A leaked device token must not be able to
    # point the owner's key at the most expensive model Google sells.
    gemini_models: tuple[str, ...] = ("gemini-3.5-flash-lite",)
    gemini_max_output_tokens: int = 1024
    # Idle keep-alive connections are closed after this, so a client that goes away
    # does not pin a server thread forever.
    idle_timeout: float = 60.0
    # Once the first byte of a request arrives, the whole request (line, headers and
    # body) must be in within this many seconds. idle_timeout alone is per recv, so a
    # client sending one byte a second could hold a thread for ever. None means the
    # same as idle_timeout.
    request_timeout: float | None = None
    # Connections past this are answered 503 and closed at once instead of each
    # getting a thread of their own.
    max_connections: int = 256

    # --- metering policy (the defaults are the behaviour the product launched with)
    # A spoken request is roughly this many Jev calls. Used only to turn the call caps
    # into the "requests a day" the landing page advertises.
    jev_calls_per_request: int = 3
    # "total": the free caps are requests to try, once, never refilled (the owner's
    # decision, 2026-09-25). "day": the old daily allowance.
    free_scope: str = "total"
    # 0: one unit per call, whatever its size. N > 0: a call costs ceil(body / N)
    # units, so a 3 MB prompt is not the same price as a 3 KB one.
    jev_bytes_per_unit: int = 0
    gemini_bytes_per_unit: int = 0
    # A timed-out upstream call keeps its unit by default: the upstream may already
    # have done, and billed, the work. True gives it back like any other failure.
    refund_on_timeout: bool = False

    # --- public service
    environment: str = "development"
    # Behind Railway's edge the socket peer is the edge, not the client. When set, the
    # first X-Forwarded-For hop is taken as the client address.
    trust_proxy_headers: bool = False
    max_devices_per_ip_per_day: int = 5
    # 0 means no ceiling on sign-ups across all addresses.
    max_devices_per_day: int = 0
    public_base_url: str | None = None
    download_url: str | None = None
    pro_price_label: str = "$8/month"
    support_email: str | None = None
    operator_name: str = "BetterFly AI LTD"

    # --- Stripe (all three, or payments answer 503 payments_unavailable)
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_pro: str | None = None

    @property
    def request_deadline(self) -> float:
        return self.request_timeout if self.request_timeout else self.idle_timeout

    @property
    def payments_configured(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_webhook_secret
                    and self.stripe_price_pro and self.public_base_url)

    def daily_requests(self, plan: str) -> int:
        """What the landing page promises: Jev calls a day over calls per request."""
        return self.daily_limit(plan, "jev") // max(1, self.jev_calls_per_request)

    def boot_errors(self) -> list[str]:
        """Reasons to refuse to start. A live Stripe key outside production would let
        real charges flow from a test deploy, so that one is fatal by design."""
        errors = []
        if not self.jev_key:
            errors.append("TYPESAFE_API_KEY is not set")
        if (self.stripe_secret_key or "").startswith(("sk_live_", "rk_live_")) \
                and self.environment != "production":
            errors.append("a live Stripe key is set but ENVIRONMENT is "
                          f"'{self.environment}', not 'production'")
        return errors

    def daily_limit(self, plan: str, kind: str) -> int:
        if kind == "gemini":
            return self.pro_daily_gemini if plan == "pro" else self.free_daily_gemini
        return self.pro_daily_calls if plan == "pro" else self.free_daily_calls

    @classmethod
    def from_env(cls) -> "Config":
        models = tuple(m.strip() for m in os.environ.get(
            "GEMINI_MODELS", "gemini-3.5-flash-lite").split(",") if m.strip())
        return cls(
            jev_key=read_key("TYPESAFE_API_KEY"),
            gemini_key=read_key("GEMINI_API_KEY"),
            host=os.environ.get("MICMIC_PROXY_HOST", "127.0.0.1"),
            # Railway (and most hosts) say where to listen in PORT.
            port=_int("MICMIC_PROXY_PORT", _int("PORT", 8810)),
            db_path=Path(os.environ.get("MICMIC_PROXY_DB",
                                        str(PROXY_ROOT / "data" / "proxy.db"))).expanduser(),
            jev_upstream=os.environ.get("JEV_UPSTREAM_URL", JEV_UPSTREAM),
            gemini_upstream=os.environ.get("GEMINI_UPSTREAM_BASE", GEMINI_UPSTREAM),
            free_daily_calls=_int("FREE_DAILY_CALLS", 300),
            pro_daily_calls=_int("PRO_DAILY_CALLS", 6000),
            free_daily_gemini=_int("FREE_DAILY_GEMINI_CALLS", 100),
            pro_daily_gemini=_int("PRO_DAILY_GEMINI_CALLS", 2000),
            max_jev_body=_int("MAX_JEV_BODY_BYTES", 256 * 1024),
            max_gemini_body=_int("MAX_GEMINI_BODY_BYTES", 8 * 1024 * 1024),
            gemini_models=models,
            gemini_max_output_tokens=_int("GEMINI_MAX_OUTPUT_TOKENS", 1024),
            idle_timeout=_float("IDLE_TIMEOUT_SECONDS", 60.0),
            request_timeout=_float("REQUEST_TIMEOUT_SECONDS", None),
            max_connections=_int("MAX_CONNECTIONS", 256),
            jev_calls_per_request=_int("JEV_CALLS_PER_REQUEST", 3),
            free_scope=("day" if os.environ.get("FREE_LIMIT_SCOPE", "").strip().lower() == "day"
                        else "total"),
            jev_bytes_per_unit=_int("JEV_BYTES_PER_UNIT", 0),
            gemini_bytes_per_unit=_int("GEMINI_BYTES_PER_UNIT", 0),
            refund_on_timeout=_flag("REFUND_ON_UPSTREAM_TIMEOUT"),
            environment=os.environ.get("ENVIRONMENT", "development").strip() or "development",
            trust_proxy_headers=_flag("TRUST_PROXY_HEADERS"),
            max_devices_per_ip_per_day=_int("MAX_DEVICES_PER_IP_PER_DAY", 5),
            max_devices_per_day=_int("MAX_DEVICES_PER_DAY", 0),
            public_base_url=_public_base_url(),
            download_url=_opt("MICMIC_DOWNLOAD_URL"),
            pro_price_label=_opt("PRO_PRICE_LABEL") or "$8/month",
            support_email=_opt("SUPPORT_EMAIL"),
            operator_name=_opt("OPERATOR_NAME") or "BetterFly AI LTD",
            stripe_secret_key=_opt("STRIPE_SECRET_KEY"),
            stripe_webhook_secret=_opt("STRIPE_WEBHOOK_SECRET"),
            stripe_price_pro=_opt("STRIPE_PRICE_PRO"),
        )
