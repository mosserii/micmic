"""Everything the proxy is told comes from the environment, read once at startup.

Upstream keys are read from the process environment first. For local development only,
a gitignored proxy/.env or the workspace .env.local next to the checkout may supply
them; only the three key names are ever read from those files, nothing else in them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROXY_ROOT = Path(__file__).resolve().parent.parent      # .../proxy
REPO_ROOT = PROXY_ROOT.parent                            # the checkout

JEV_UPSTREAM = "https://api.typesafe.ai/v1/systemone"
GEMINI_UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/models"
ELEVEN_UPSTREAM = "https://api.elevenlabs.io"
# MicMic's voice: Jessica, a premade ElevenLabs voice ("Playful, Bright, Warm"), chosen in
# micmic-work/voice/ELEVENLABS_V4.md. The client names a voice; only these ids go upstream.
TTS_VOICES = {"jessica": "cgSgspJ2msm6clMCkdW9"}

# --- MicMic for iPhone (apple.py, PURCHASES_ACCOUNT.md section 5.3)
APPLE_BUNDLE_ID = "com.betterfly.micmic.ios"
APPLE_PRODUCT_IDS = ("com.betterfly.micmic.ios.pro.monthly",)
# Signed by Apple: accepted on every deploy. Sandbox stays on in production because App
# Review and TestFlight buy with sandbox accounts against the production build.
APPLE_SIGNED_ENVIRONMENTS = ("Production", "Sandbox")
# Not signed by Apple (an Xcode StoreKit file): local development only, never production.
APPLE_UNSIGNED_ENVIRONMENTS = ("Xcode", "LocalTesting")
# The dates the legal pages show as "Last updated". The phone compiles in the versions it
# showed; a consent below the *_REQUIRED one is refused with 409 consent_outdated.
LEGAL_VERSION = "2026-10-03"
# Privacy moved on alone: iPhone build 5 turns English speech into text with MicMic's own
# model, downloaded once from Hugging Face (2026-10-05). Terms and AI disclosure did not change.
PRIVACY_LEGAL_VERSION = "2026-10-05"
CONSENT_PROVIDERS = ("typesafe", "google_gemini", "elevenlabs")


def _voices(raw: str) -> dict[str, str]:
    """TTS_VOICES="name:voice_id,name:voice_id". The first one is the default."""
    out: dict[str, str] = {}
    for part in raw.split(","):
        name, _, vid = part.partition(":")
        if name.strip() and vid.strip():
            out[name.strip().lower()] = vid.strip()
    return out or dict(TTS_VOICES)


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


def _is_date(v: str) -> bool:
    return len(v) == 10 and v[4] == "-" and v[7] == "-" and (v[:4] + v[5:7] + v[8:]).isdigit()


def _app_id() -> int | None:
    raw = os.environ.get("APPLE_APP_ID", "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise SystemExit("APPLE_APP_ID must be the app's numeric Apple ID")
    return int(raw)


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _opt(name: str) -> str | None:
    """Stripe and the public URL come from the process environment only, never from
    the local key files read_key() looks in."""
    return os.environ.get(name, "").strip() or None


def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


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
    # --- the spoken voice (POST /v1/tts, tts.py). ElevenLabs Eleven v4 Turbo, streamed
    # as 24 kHz 16-bit PCM. The key lives here and nowhere else.
    eleven_key: str | None = None
    eleven_upstream: str = ELEVEN_UPSTREAM
    # The kill switch: TTS_ENABLED=0 answers every /v1/tts with 503 tts_disabled, and
    # the app speaks with the voice on the Mac at once.
    tts_enabled: bool = True
    tts_model: str = "eleven_v4_turbo"
    tts_voices: dict = None  # type: ignore[assignment]   # set in __post_init__
    tts_max_chars: int = 600
    # Characters, metered per device (see README "The voice"). Free: one lifetime pool
    # sized to the 100 free requests. Pro: a monthly pool and a daily ceiling; past
    # either, the app speaks with the voice on the Mac until it resets.
    free_tts_chars: int = 15000
    pro_monthly_tts_chars: int = 100000
    pro_daily_tts_chars: int = 10000
    # No audio from ElevenLabs within this many seconds is an error (the app has long
    # since fallen back to the local voice); the whole render within tts_timeout.
    tts_first_byte_timeout: float = 4.0
    tts_timeout: float = 20.0
    # The shared cache of rendered lines (tts.py). Size 0 turns it off.
    tts_cache_dir: Path | None = None
    tts_cache_max_bytes: int = 256 * 1024 * 1024
    # A line is stored only once this many requests asked for it: a one-off private
    # sentence is never written to disk, "Alright." is after its second time.
    tts_cache_min_seen: int = 2
    # Mixed into the cache keys and the stored audio's key stream. Optional: without
    # it the cache still works, and a stolen disk can only confirm a sentence someone
    # already knows word for word.
    tts_cache_secret: str = ""

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
    # A step-by-step guide (client header X-MicMic-Session: guide:<id>) costs its first
    # call; the rest of it is covered up to these many calls of each kind, for
    # guide_ttl seconds, for at most so many guides per bucket (lifetime on free, a day
    # on Pro). Past any of them, calls are metered as usual. tests/test_guide_session.py
    guide_ttl: int = 1800
    guide_free_jev: int = 60
    guide_free_gemini: int = 60
    free_guide_sessions: int = 20
    pro_guide_sessions: int = 100
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

    # --- Apple In-App Purchase (apple.py). Without root certificates every Apple route
    # answers 503 apple_unavailable. Production transactions also need the app's numeric
    # Apple ID (App Store Connect > App Information); until it is set only Sandbox passes.
    apple_bundle_id: str = APPLE_BUNDLE_ID
    apple_app_id: int | None = None
    apple_product_ids: tuple[str, ...] = APPLE_PRODUCT_IDS
    apple_environments: tuple[str, ...] = APPLE_SIGNED_ENVIRONMENTS
    apple_root_certs_dir: Path = PROXY_ROOT / "certs"
    # OCSP revocation checks of Apple's certificates, online. Tests turn them off.
    apple_online_checks: bool = True
    # One subscription unlocks at most this many devices; the least recently linked
    # one is unlinked past it (it can restore again).
    apple_max_devices_per_sub: int = 5
    apple_expiry_leeway: int = 3600

    # --- terms and AI-data consent (POST /v1/devices, POST /v1/consent)
    terms_version: str = LEGAL_VERSION
    privacy_version: str = PRIVACY_LEGAL_VERSION
    ai_disclosure_version: str = LEGAL_VERSION
    # None means the same as the current version.
    terms_required: str | None = None
    privacy_required: str | None = None
    ai_disclosure_required: str | None = None
    consent_providers: tuple[str, ...] = CONSENT_PROVIDERS

    # --- the iPhone brain, called only by account deletion (POST {brain_url}/v1/brain/forget)
    brain_url: str | None = None
    brain_timeout: float = 10.0

    # --- diagnostics (POST /v1/diagnostics): the turn log a phone sends only while its
    # owner has Share diagnostics on. Text and timings, never audio. Kept 30 days.
    # DIAGNOSTICS_ENABLED=0 is the kill switch: every upload answers 503.
    diagnostics_enabled: bool = True
    diag_max_body: int = 64 * 1024
    diag_max_items: int = 50                 # turns in one upload
    diag_max_item_bytes: int = 16 * 1024     # one turn, as JSON
    diag_daily_items: int = 600              # turns a device may send per UTC day
    diag_per_minute: int = 6                 # uploads a device may make per minute
    diag_retention_days: int = 30
    diag_max_rows_per_device: int = 2000     # the oldest go first past this

    def __post_init__(self):
        if self.tts_voices is None:
            self.tts_voices = dict(TTS_VOICES)
        if self.tts_cache_dir is None:
            self.tts_cache_dir = Path(self.db_path).parent / "tts-cache"
        self.terms_required = self.terms_required or self.terms_version
        self.privacy_required = self.privacy_required or self.privacy_version
        self.ai_disclosure_required = self.ai_disclosure_required or self.ai_disclosure_version

    def consent_required(self) -> dict:
        """The oldest versions a consent may name, as /v1/account reports them."""
        return {"terms_version": self.terms_required,
                "privacy_version": self.privacy_required,
                "ai_disclosure_version": self.ai_disclosure_required}

    @property
    def tts_configured(self) -> bool:
        return bool(self.eleven_key)

    def tts_limits(self, plan: str) -> list[tuple[str, int]]:
        """(scope, characters) caps that all apply to one device."""
        if plan == "pro":
            return [("month", self.pro_monthly_tts_chars), ("day", self.pro_daily_tts_chars)]
        return [("total", self.free_tts_chars)]

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
        known = APPLE_SIGNED_ENVIRONMENTS + APPLE_UNSIGNED_ENVIRONMENTS
        for env in self.apple_environments:
            if env not in known:
                errors.append(f"APPLE_ENVIRONMENTS names '{env}', not one of {', '.join(known)}")
            elif env in APPLE_UNSIGNED_ENVIRONMENTS and self.environment == "production":
                # Xcode StoreKit transactions are not signed by Apple: anyone could make one.
                errors.append(f"APPLE_ENVIRONMENTS allows '{env}' (unsigned) with "
                              "ENVIRONMENT=production")
        for name in ("terms_version", "privacy_version", "ai_disclosure_version",
                     "terms_required", "privacy_required", "ai_disclosure_required"):
            v = getattr(self, name) or ""
            if not _is_date(v):
                errors.append(f"{name.upper()} must be a date like 2026-10-03")
        if self.brain_url and not self.brain_url.startswith(("https://", "http://")):
            errors.append("BRAIN_URL must start with https:// or http://")
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
            guide_ttl=_int("GUIDE_TTL_S", 1800),
            guide_free_jev=_int("GUIDE_FREE_JEV_CALLS", 60),
            guide_free_gemini=_int("GUIDE_FREE_GEMINI_CALLS", 60),
            free_guide_sessions=_int("FREE_GUIDE_SESSIONS", 20),
            pro_guide_sessions=_int("PRO_GUIDE_SESSIONS", 100),
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
            eleven_key=read_key("ELEVENLABS_API_KEY"),
            eleven_upstream=os.environ.get("ELEVEN_UPSTREAM_URL", ELEVEN_UPSTREAM),
            tts_enabled=_flag("TTS_ENABLED", True),
            tts_model=os.environ.get("TTS_MODEL", "").strip() or "eleven_v4_turbo",
            tts_voices=_voices(os.environ.get("TTS_VOICES", "")),
            tts_max_chars=_int("TTS_MAX_CHARS", 600),
            free_tts_chars=_int("FREE_TTS_CHARS", 15000),
            pro_monthly_tts_chars=_int("PRO_MONTHLY_TTS_CHARS", 100000),
            pro_daily_tts_chars=_int("PRO_DAILY_TTS_CHARS", 10000),
            tts_cache_dir=(Path(os.environ["TTS_CACHE_DIR"]).expanduser()
                           if os.environ.get("TTS_CACHE_DIR", "").strip() else None),
            tts_cache_max_bytes=_int("TTS_CACHE_MAX_MB", 256) * 1024 * 1024,
            tts_cache_min_seen=_int("TTS_CACHE_MIN_SEEN", 2),
            tts_cache_secret=_opt("TTS_CACHE_SECRET") or "",
            apple_bundle_id=_opt("APPLE_BUNDLE_ID") or APPLE_BUNDLE_ID,
            apple_app_id=_app_id(),
            apple_product_ids=_list("APPLE_PRODUCT_IDS", APPLE_PRODUCT_IDS),
            apple_environments=_list("APPLE_ENVIRONMENTS", APPLE_SIGNED_ENVIRONMENTS),
            apple_root_certs_dir=(Path(os.environ["APPLE_ROOT_CERTS_DIR"]).expanduser()
                                  if os.environ.get("APPLE_ROOT_CERTS_DIR", "").strip()
                                  else PROXY_ROOT / "certs"),
            apple_online_checks=_flag("APPLE_ONLINE_CHECKS", True),
            apple_max_devices_per_sub=_int("APPLE_MAX_DEVICES_PER_SUB", 5),
            apple_expiry_leeway=_int("APPLE_EXPIRY_LEEWAY_SECONDS", 3600),
            terms_version=_opt("TERMS_VERSION") or LEGAL_VERSION,
            privacy_version=_opt("PRIVACY_VERSION") or PRIVACY_LEGAL_VERSION,
            ai_disclosure_version=_opt("AI_DISCLOSURE_VERSION") or LEGAL_VERSION,
            terms_required=_opt("TERMS_REQUIRED"),
            privacy_required=_opt("PRIVACY_REQUIRED"),
            ai_disclosure_required=_opt("AI_DISCLOSURE_REQUIRED"),
            brain_url=(_opt("BRAIN_URL") or "").rstrip("/") or None,
            diagnostics_enabled=_flag("DIAGNOSTICS_ENABLED", True),
            diag_daily_items=_int("DIAG_DAILY_ITEMS", 600),
            diag_per_minute=_int("DIAG_PER_MINUTE", 6),
            diag_retention_days=max(1, _int("DIAG_RETENTION_DAYS", 30)),
            diag_max_rows_per_device=_int("DIAG_MAX_ROWS_PER_DEVICE", 2000),
        )
