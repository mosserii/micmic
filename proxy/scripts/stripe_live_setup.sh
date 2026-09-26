#!/usr/bin/env bash
# Creates MicMic Pro in LIVE mode on the Stripe account the CLI is logged into:
# the product, the $8/month price, the webhook and the customer portal. Run it
# yourself; it prints ids only, never a secret. The webhook's signing secret is
# revealed later in Dashboard > Developers > Webhooks, when you paste the live keys.
#
#   proxy/scripts/stripe_live_setup.sh
#
# Safe to re-run: it stops if a live "MicMic Pro" product already exists.
set -euo pipefail

want_account="acct_1UJXwGCSG4UinxEd"   # MicMic
cloud="https://cloud-production-f42c.up.railway.app"
site="https://getmicmic.vercel.app"

json() { python3 -c 'import sys,json; s=sys.stdin.read(); d=json.loads(s[s.find("{"):]); print(eval(sys.argv[1]))' "$1"; }

acct=$(stripe get /v1/account --live | json 'd["id"]')
[ "$acct" = "$want_account" ] || { echo "The CLI is on $acct, not MicMic ($want_account). Run: stripe login"; exit 1; }

existing=$(stripe products list --live --limit 100 | json 'len([p for p in d["data"] if p["name"]=="MicMic Pro" and p["active"]])')
[ "$existing" = "0" ] || { echo "A live MicMic Pro product already exists. Nothing created."; exit 1; }

product=$(stripe products create --live --name "MicMic Pro" \
  --description "2,000 requests a day. Cancel any time." -d "metadata[app]=micmic" \
  -d "images[]=$site/static/icon.png" \
  --tax-code txcd_10105003 | json 'd["id"]')
# txcd_10105003: AI as a service, cloud based and downloaded, personal use. Managed
# Payments (Stripe as merchant of record, on by default) refuses a product without one.
echo "product  $product"

price=$(stripe prices create --live --product "$product" --unit-amount 800 --currency usd \
  -d "recurring[interval]=month" --nickname "MicMic Pro monthly" \
  -d "lookup_key=micmic_pro_monthly" -d "tax_behavior=inclusive" | json 'd["id"]')
# Tax inclusive: everyone pays exactly $8 and the tax comes out of it (the owner's call,
# 2026-09-25; EU consumer prices must include VAT anyway).
stripe products update --live "$product" --default-price "$price" >/dev/null
echo "price    $price   <- STRIPE_PRICE_PRO"

webhook=$(stripe webhook_endpoints create --live --url "$cloud/v1/stripe/webhook" \
  --description "MicMic cloud (Railway)" \
  -d "enabled_events[]=checkout.session.completed" \
  -d "enabled_events[]=checkout.session.async_payment_succeeded" \
  -d "enabled_events[]=customer.subscription.created" \
  -d "enabled_events[]=customer.subscription.updated" \
  -d "enabled_events[]=customer.subscription.deleted" | json 'd["id"]')
echo "webhook  $webhook   <- its signing secret is STRIPE_WEBHOOK_SECRET"

portal=$(stripe billing_portal configurations create --live \
  -d "business_profile[headline]=Manage your MicMic Pro subscription" \
  -d "features[subscription_cancel][enabled]=true" \
  -d "features[subscription_cancel][mode]=at_period_end" \
  -d "features[payment_method_update][enabled]=true" \
  -d "features[invoice_history][enabled]=true" \
  -d "features[customer_update][enabled]=false" \
  -d "default_return_url=$site/" | json 'd["id"]')
echo "portal   $portal"
echo "done. Next: activate the account, create the restricted key, paste the three values into Railway."
