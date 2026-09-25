# MicMic proxy

The server a shipped MicMic talks to. It holds the Jev and Gemini keys, gives each Mac
an anonymous device token, meters calls per token per UTC day, sells MicMic Pro through
Stripe, and serves the public website.

Standard library HTTP and SQLite; the only dependency is the Stripe SDK.

## Routes

| Route | Auth | What it does |
|---|---|---|
| `POST /v1/devices` | none | `{"device_name", "app_version"}` → 201 `{"token", "device_id", "plan": "free"}`. The token is shown once. 5 per client address per UTC day, else 429 `{"error": "too_many_devices"}`. |
| `GET /v1/account` | Bearer | plan, today's usage and limits, `resets_at`, and the subscription (`null` until Stripe has told us its period). |
| `POST /v1/checkout` | Bearer | 200 `{"url"}` to Stripe Checkout for Pro. 409 `already_pro`, 503 `payments_unavailable`, 502 `payments_error` if Stripe fails. |
| `POST /v1/portal` | Bearer | 200 `{"url"}` to Stripe's billing portal. 404 `no_subscription`, 503, 502 as above. |
| `POST /v1/stripe/webhook` | Stripe signature | the only place a plan changes. 200 on handled or ignored, 400 `bad_signature`, 503 without a webhook secret, 500 on a storage error (so Stripe retries). |
| `POST /v1/jev`, `POST /v1/gemini/<model>:generateContent` | Bearer | metered forwarding, unchanged. |
| `GET /v1/usage` | Bearer | the older usage shape, unchanged. |
| `GET /health` (also `/healthz`) | none | liveness, no database work. |
| `GET /ready` | none | 200 when the database answers, else 503. |
| `GET /`, `/privacy`, `/terms`, `/checkout/success`, `/checkout/cancel`, `/static/icon.png` | none | the website, from `web/`. |

## Environment

Everything is read once at startup. Stripe and URL settings come only from the process
environment; the two upstream keys may also come from a local `.env` file in
development (see `proxy/config.py`), never in the container.

### Required on Railway

| Variable | Example | Where it comes from |
|---|---|---|
| `TYPESAFE_API_KEY` | `tsk-...` | The Jev (Typesafe) dashboard. The server refuses to start without it. |
| `GEMINI_API_KEY` | `AIza...` | Google AI Studio → API keys. Without it Gemini routes answer 503 `not_configured`. |
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
| `JEV_UPSTREAM_URL`, `GEMINI_UPSTREAM_BASE` | the real services | For tests. |
| `MICMIC_PROXY_ENV_FILE` | unset | Development only: a file to read the two upstream keys from. |

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
```

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

Tests never reach Jev, Google or Stripe: upstreams are local fakes, Stripe objects are
built with the SDK's `construct_from`, and webhook bodies are signed locally.
