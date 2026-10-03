# MicMic proxy

The server a shipped MicMic talks to. It holds the Jev, Gemini and ElevenLabs keys, gives each Mac
an anonymous device token, meters calls per token per UTC day, sells MicMic Pro through
Stripe, and serves the public website.

It also serves MicMic for iPhone: MicMic Pro sold through Apple's In-App Purchase
(StoreKit 2 transactions and App Store Server Notifications V2, `proxy/apple.py`), the
terms and AI-data consent record, and in-app account deletion. Design:
`~/jev/micmic-work/appstore/PURCHASES_ACCOUNT.md`.

Standard library HTTP and SQLite; two dependencies: the Stripe SDK (only `billing.py`
imports it) and Apple's `app-store-server-library` (only `apple.py` imports it).

## Routes

| Route | Auth | What it does |
|---|---|---|
| `POST /v1/devices` | none | `{"device_name", "app_version"}` → 201 `{"token", "device_id", "plan": "free"}`. The token is shown once. 5 per client address per UTC day, else 429 `{"error": "too_many_devices"}`. |
| `POST /v1/devices` (iPhone) | none | adds optional `platform` (`mac` default, `ios`) and `consent` (`{terms_version, privacy_version, ai_disclosure_version, providers, accepted_at, locale, method, age_18}`). `ios` without consent, or without `age_18: true`: 400 `consent_required`; a version below `*_REQUIRED`: 409 `consent_outdated`. An `ios` 201 adds `apple_account_token` (the StoreKit `appAccountToken`). A Mac without these fields signs up exactly as before. |
| `GET /v1/account` | Bearer | plan, today's usage and limits, `resets_at`, and the subscription (`null` until Stripe has told us its period). Additive: `platform`, `pro_source` (`stripe`, `apple`, `admin` or null), `apple` (`{original_transaction_id_hash, product_id, status, expires_at, grace_until, auto_renew, environment}` or null), `apple_account_token`, `consent`, `consent_required`. |
| `POST /v1/consent` | Bearer | `{"consent": {...}}` as at sign-up → 200 `{"ok": true}`; 400, 409 `consent_outdated`. |
| `POST /v1/apple/transaction` | Bearer | `{"jws", "source": purchase\|update\|restore\|launch}` (16 KB) → 200 `{"plan": "pro", "source": "apple", "expires_at", "environment", "auto_renew"}`. 400 `bad_request` / `bad_transaction` / `bad_environment` / `unknown_product`, 409 `expired` / `revoked`, 503 `apple_unavailable` / `apple_verify_retry` (`retry_after_s: 30`). Sandbox is accepted in production (App Review buys with sandbox accounts). |
| `POST /v1/apple/notifications` | Apple signature | App Store Server Notifications V2, `{"signedPayload"}` (64 KB) → 200 `{"received": true}` for handled, ignored, stale, duplicate and unknown; 400 `bad_request` / `bad_signature`; 503 `apple_unavailable` (no roots, or Production before `APPLE_APP_ID`), 500 on a storage error (Apple retries). |
| `POST /v1/account/delete` | Bearer | `{"confirm": true}` → 200 `{"deleted": true, "brain": "erased"\|"not_used", "apple_subscription_active", "manage_url"}`. In order: a live Stripe subscription is cancelled (502 `payments_error`), the brain forgets the device (`BRAIN_URL`; 503 `brain_unavailable` with `retry_after_s`, 503 `deletion_unavailable` while the brain answers 501, and for an iPhone device on any deploy (`ENVIRONMENT` other than `development`) while `BRAIN_URL` is unset), then every row about the device is deleted in one transaction. Any failure deletes nothing. 400 `confirm_required`; a second call is 401. |
| `POST /v1/checkout` | Bearer | 200 `{"url"}` to Stripe Checkout for Pro. 403 `use_in_app_purchase` for an `ios` device (guideline 3.1.1), 409 `already_pro`, 503 `payments_unavailable`, 502 `payments_error` if Stripe fails. |
| `POST /v1/portal` | Bearer | 200 `{"url"}` to Stripe's billing portal. 404 `no_subscription`, 503, 502 as above. |
| `POST /v1/stripe/webhook` | Stripe signature | the only place a plan changes. 200 on handled or ignored, 400 `bad_signature`, 503 without a webhook secret, 500 on a storage error (so Stripe retries). |
| `POST /v1/jev`, `POST /v1/gemini/<model>:generateContent` | Bearer | metered forwarding, unchanged. |
| `POST /v1/tts` | Bearer | `{"text", "tag"?, "lead"?, "lang": "en"\|"he"\|"ar"\|"ru", "voice"?, "cache"?}` → 200, 24 kHz mono 16-bit PCM streamed as ElevenLabs makes it (`Transfer-Encoding: chunked`, `X-Audio-Format: pcm_s16le_24000`, `X-Cache: hit\|miss`). 400 `tag_not_allowed` / `bad_language` / `voice_not_allowed` / `text_too_long`, 429 `tts_limit` (`scope` total, month or day, `resets_at`), 503 `tts_disabled` (the kill switch) / `not_configured` (no key on the server: `{"error": "not_configured", "message": "voice not configured"}`, `Retry-After: 3600`) / `upstream_busy`, 504 `upstream_timeout`. See "The voice" below. |
| `GET /v1/usage` | Bearer | the older usage shape, unchanged. |
| `GET /health` (also `/healthz`) | none | liveness, no database work. |
| `GET /ready` | none | 200 when the database answers, else 503. |
| `GET /`, `/privacy`, `/terms`, `/support`, `/checkout/success`, `/checkout/cancel`, `/static/icon.png` | none | the website, from `web/`. `/support` is the App Store Support URL. |

## Environment

Everything is read once at startup. Stripe and URL settings come only from the process
environment; the two upstream keys may also come from a local `.env` file in
development (see `proxy/config.py`), never in the container.

### Required on Railway

| Variable | Example | Where it comes from |
|---|---|---|
| `TYPESAFE_API_KEY` | `tsk-...` | The Jev (Typesafe) dashboard. The server refuses to start without it. |
| `GEMINI_API_KEY` | `AIza...` | Google AI Studio → API keys. Without it Gemini routes answer 503 `not_configured`. |
| `ELEVENLABS_API_KEY` | `sk_...` | ElevenLabs → Developers → API keys, on a paid plan (Free is non-commercial). Needs only Text to Speech access. Without it `/v1/tts` answers 503 `not_configured` and the app speaks with the voice on the Mac. |
| `ENVIRONMENT` | `production` | Set by hand. Must be `production` before a `sk_live_` key is set, or the server refuses to boot (by design). Use `staging` for a test-mode deploy. |
| `STRIPE_SECRET_KEY` | `sk_test_...` | Stripe Dashboard → Developers → API keys (Test mode toggle on for the test key). |
| `STRIPE_WEBHOOK_SECRET` | `whsec_...` | Stripe Dashboard → Developers → Webhooks → your endpoint → Signing secret. Test and live endpoints have different secrets. |
| `STRIPE_PRICE_PRO` | `price_...` | The recurring Price you create for MicMic Pro (below). The client never chooses a price. |
| `SUPPORT_EMAIL` | `help@micmic.app` | Your inbox. Shown on the privacy and terms pages; without it they have no contact address. |

Without all of `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO` and a
public URL, checkout and portal answer 503 `payments_unavailable` and everything else
works. The webhook needs only `STRIPE_WEBHOOK_SECRET`.

### Set by the image or by Railway

| Variable | Default | Notes |
|---|---|---|
| `PORT` | set by Railway | Where to listen. `MICMIC_PROXY_PORT` wins if set; 8810 outside a container. |
| `MICMIC_PROXY_HOST` | `0.0.0.0` in the image | `127.0.0.1` outside it. |
| `MICMIC_PROXY_DB` | `/data/proxy.db` in the image | Mount a Railway volume at `/data`, or every redeploy starts with an empty database. |
| `TRUST_PROXY_HEADERS` | `1` in the image | Count sign-ups against the first `X-Forwarded-For` hop, which Railway's edge sets. Leave `0` anywhere the server is reached directly. |
| `RAILWAY_PUBLIC_DOMAIN` | set by Railway | Used for Stripe's return URLs when `PUBLIC_BASE_URL` is unset. |

### Optional

| Variable | Default | Notes |
|---|---|---|
| `PUBLIC_BASE_URL` | `https://$RAILWAY_PUBLIC_DOMAIN` | Set it once you have a custom domain, e.g. `https://micmic.app`. Stripe sends buyers back to `/checkout/success` and `/checkout/cancel` under it. |
| `MICMIC_DOWNLOAD_URL` | unset | An `https://` link to the signed build. Unset, the landing page says "Download coming soon". |
| `PRO_PRICE_LABEL` | `$8/month` | What the website says Pro costs. Keep it equal to the Stripe price. |
| `OPERATOR_NAME` | `BetterFly AI LTD` | Named on the privacy and terms pages. |
| `FREE_DAILY_CALLS` / `PRO_DAILY_CALLS` | `300` / `6000` | Jev calls a day. |
| `FREE_DAILY_GEMINI_CALLS` / `PRO_DAILY_GEMINI_CALLS` | `100` / `2000` | Gemini calls a day, a separate meter. |
| `JEV_CALLS_PER_REQUEST` | `3` | Turns the Jev caps into the "requests a day" the website shows (300 → 100, 6000 → 2,000). |
| `MAX_DEVICES_PER_IP_PER_DAY` | `5` | Sign-ups per client address per UTC day. |
| `MAX_DEVICES_PER_DAY` | `0` (none) | A ceiling on sign-ups from everyone together, for a day under attack. |
| `IDLE_TIMEOUT_SECONDS` | `60` | Longest wait for a request's first byte, and per read. |
| `REQUEST_TIMEOUT_SECONDS` | same as idle | Once a request starts, all of it (headers and body) must arrive within this. |
| `MAX_CONNECTIONS` | `256` | Connections past this get 503 and are closed at once. |
| `REFUND_ON_UPSTREAM_TIMEOUT` | `0` | `1` gives a unit back when Jev or Gemini times out. Off by default: the upstream may have done, and billed, the work. |
| `JEV_BYTES_PER_UNIT` / `GEMINI_BYTES_PER_UNIT` | `0` | `0`: a call is one unit whatever its size. `N`: a call costs `ceil(bytes / N)` units. |
| `MAX_JEV_BODY_BYTES` / `MAX_GEMINI_BODY_BYTES` | 256 KB / 8 MB | Larger bodies get 413. |
| `GEMINI_MODELS` | `gemini-3.5-flash-lite` | Comma-separated allowlist. |
| `GEMINI_MAX_OUTPUT_TOKENS` | `1024` | Output clamp on every Gemini call. |
| `TTS_ENABLED` | `1` | `0` is the voice's kill switch: every `/v1/tts` answers 503 `tts_disabled` and the app speaks on the Mac for 10 minutes before asking again. |
| `FREE_TTS_CHARS` | `15000` | Spoken characters a free device gets, for life (about 100 requests at the measured p95 of 145 characters a reply). |
| `PRO_MONTHLY_TTS_CHARS` / `PRO_DAILY_TTS_CHARS` | `100000` / `10000` | A Pro device's characters per calendar month (UTC) and per day. Past either, the app speaks on the Mac until it resets. |
| `TTS_MODEL` | `eleven_v4_turbo` | The ElevenLabs model. |
| `TTS_VOICES` | `jessica:cgSgspJ2msm6clMCkdW9` | `name:voice_id,...`, the voices a client may name; the first is the default. |
| `TTS_MAX_CHARS` | `600` | Longest line accepted. |
| `TTS_CACHE_DIR` | `<db folder>/tts-cache` | Where common lines' audio is kept (on the `/data` volume on Railway). |
| `TTS_CACHE_MAX_MB` | `256` | The cache's size; least recently used lines go first. `0` turns it off. |
| `TTS_CACHE_MIN_SEEN` | `2` | A line is stored once this many requests asked for it. |
| `TTS_CACHE_SECRET` | unset | Optional. Mixed into the cache's fingerprints and keys, so a copy of the disk alone cannot even confirm a guessed sentence. Changing it empties the cache in effect. |
| `JEV_UPSTREAM_URL`, `GEMINI_UPSTREAM_BASE`, `ELEVEN_UPSTREAM_URL` | the real services | For tests. |
| `MICMIC_PROXY_ENV_FILE` | unset | Development only: a file to read the two upstream keys from. |

### MicMic for iPhone

| Variable | Default | Notes |
|---|---|---|
| `APPLE_APP_ID` | unset | The app's numeric Apple ID (App Store Connect > App Information). Without it only Sandbox transactions and notifications pass; Production notifications answer 503 so Apple retries. |
| `APPLE_BUNDLE_ID` | `com.betterfly.micmic.ios` | |
| `APPLE_PRODUCT_IDS` | `com.betterfly.micmic.ios.pro.monthly` | Comma list; add the yearly here later. |
| `APPLE_ENVIRONMENTS` | `Production,Sandbox` | `Xcode`/`LocalTesting` (unsigned StoreKit test files) only with `ENVIRONMENT` other than `production`; the boot refuses them in production. |
| `APPLE_ROOT_CERTS_DIR` | `<proxy>/certs` | Apple's public roots (G3, G2, Apple Root CA), committed, copied by the Dockerfile. |
| `APPLE_ONLINE_CHECKS` | `1` | OCSP revocation checks of Apple's certificates. |
| `APPLE_MAX_DEVICES_PER_SUB` | `5` | Devices one subscription unlocks; the least recently linked is unlinked past it. |
| `APPLE_EXPIRY_LEEWAY_SECONDS` | `3600` | Pro ends at expiry (or the end of grace) plus this, even with no notification. |
| `TERMS_VERSION`, `PRIVACY_VERSION`, `AI_DISCLOSURE_VERSION` | `2026-10-03` | The pages' "Last updated" dates. |
| `TERMS_REQUIRED`, `PRIVACY_REQUIRED`, `AI_DISCLOSURE_REQUIRED` | the current version | The oldest a consent may name. Raise only for a material change; a new AI provider always raises `AI_DISCLOSURE_REQUIRED`. |
| `BRAIN_URL` | unset | The iPhone brain (`https://...` or the Railway private `http://...`), used only by account deletion to call `/v1/brain/forget`. Unset: a Mac's deletion skips the brain (`brain: "not_used"`); an iPhone's deletion answers 503 `deletion_unavailable` and deletes nothing, except in local development. Set it before (or together with) the iPhone brain taking any traffic. |

## The voice

`POST /v1/tts` voices MicMic's spoken replies with ElevenLabs Eleven v4 Turbo (voice
Jessica), for the languages whose engine list in the app's `micmic.config.json` starts
with `eleven_turbo` (Hebrew, Arabic and Russian at launch). The app asks only with the
cleaned line, an audio tag chosen by its own code (`warmly`, `calmly`, `softly`, `excited`;
anything else is refused), an optional `lead` (words read before the tag, so a friend's
message is voiced as "Dana: [excited] I got the job!!!") and whether the line may be cached.

- **Streaming.** The audio is relayed chunk by chunk over a kept-alive connection to
  ElevenLabs, so the app hears the first bytes about one network hop after ElevenLabs
  makes them. A stream cut midway ends without its final chunk; the app finishes the line
  with the voice on the Mac.
- **Metering.** Characters, including the tag, are taken before the call and given back if
  ElevenLabs refuses it. A cache hit costs nothing and is not counted.
- **Cache.** A line is stored only when the app allowed it (never a read-back of her
  messages, notes or screen, never a sentence Gemini wrote) and once
  `TTS_CACHE_MIN_SEEN` requests have asked for it. Files are named by an HMAC of the line
  and encrypted with a key stream derived from it, with an authenticating tag; a damaged
  or cut file is a miss. Only the fingerprint of a line seen once is kept, for 30 days.
- **Privacy.** The text is never logged, nor the audio. The log line is
  `tts hit|miss chars=N [stored]` plus the usual access line.

## Stripe setup (test mode first)

Create these in the Stripe Dashboard with **Test mode on**:

1. **Product** "MicMic Pro", with one **recurring Price**: $8.00 USD, monthly. Its id
   (`price_...`) goes in `STRIPE_PRICE_PRO`.
2. **Webhook endpoint**: `https://<your domain>/v1/stripe/webhook`, events:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`

   Its signing secret goes in `STRIPE_WEBHOOK_SECRET`.
3. **Customer portal**: Settings → Billing → Customer portal. Save a configuration (the
   portal link fails until one is saved; test and live are separate). Allow cancelling
   subscriptions, and updating the payment method.

How it behaves: the app calls `/v1/checkout` and opens the URL. Stripe creates the
customer and the subscription; the session carries the device id in
`client_reference_id`, `metadata` and the subscription's `metadata`. The webhook makes
the device Pro on `checkout.session.completed` (paid), keeps it Pro while the
subscription is `active`, `trialing` or `past_due`, and makes it free on anything else
or on `customer.subscription.deleted`. Every event id is recorded in the same
transaction as its effect, so a redelivery does nothing. An event older than the last
one applied to that subscription is ignored.

Local webhook testing: `stripe listen --forward-to localhost:8821/v1/stripe/webhook`
prints a `whsec_` for `STRIPE_WEBHOOK_SECRET`; pay with card `4242 4242 4242 4242`.

## Railway

1. New service from this repository. **Root Directory** `/proxy`, **Config file**
   `/proxy/railway.json` (Railway does not look for it under the root directory by
   itself). It builds `proxy/Dockerfile` and health-checks `/health`.
2. Add a **volume** mounted at `/data`. Keep **one replica**: SQLite on a volume is one
   writer.
3. Generate a domain (or attach yours and set `PUBLIC_BASE_URL`).
4. Set the variables above, then register the webhook endpoint at that domain.

## Sandbox to live flip

From the Stripe recipe, in this order. Nothing in the app changes.

1. In the Stripe Dashboard, flip **Test mode off** to see the live views, and create the
   same Product and monthly Price there (live objects have different ids).
2. **Developers → Webhooks → Add endpoint** (a live registration is separate from the
   test one) pointing at `https://<your domain>/v1/stripe/webhook`, with the five events
   above. Copy its `whsec_...`: a different secret from the test one. Save a live
   Customer portal configuration too.
3. In Railway (production environment), set:
   - `ENVIRONMENT=production` first, or together with the key. The boot guard refuses a
     `sk_live_` key under any other environment name, so the wrong order crash-loops the
     deploy by design.
   - `STRIPE_SECRET_KEY=sk_live_...`
   - `STRIPE_WEBHOOK_SECRET=whsec_...` (the live one from step 2)
   - `STRIPE_PRICE_PRO=price_...` (the live price)
4. Redeploy.
5. Smoke-test with one real subscription before trusting it at volume, and watch the
   Railway logs for the first live webhook (`stripe checkout.session.completed ...: pro`).

## Owner CLI

```
uv run python -m proxy.admin list                 # hashes, device ids, plans, usage; never tokens
uv run python -m proxy.admin plan <hash prefix | dev_id> pro
uv run python -m proxy.admin revoke <hash prefix | dev_id>
uv run python -m proxy.admin mint --plan free     # a token by hand, shown once
uv run python -m proxy.admin delete <hash prefix | dev_id> --yes   # a Mac user's deletion request
```

`delete` cancels the device's live Stripe subscription first (nothing is deleted if that
fails), then deletes every row about the device. Without `--yes` it only says what it
would delete. iPhone users delete from the app, which also erases the brain's notes.

On Railway, run these in the service shell (`--db /data/proxy.db` is the default there).
`plan` changes a device by hand; the next Stripe event about its subscription can change
it again.

## Development

```
cd proxy
uv sync
uv run python -m pytest -q
MICMIC_PROXY_PORT=8821 TYPESAFE_API_KEY=... uv run python -m proxy
```

Tests never reach Jev, Google, Stripe or Apple: upstreams are local fakes, Stripe objects
are built with the SDK's `construct_from`, webhook bodies are signed locally, and every
Apple JWS is signed by a fake Apple PKI (`tests/apple_fakes.py`) and checked by Apple's
real `SignedDataVerifier` with online checks off.
