# Stripe Subscriptions Service (fresh script, 2026-11-15)

Standalone subscription checkout for 911transedu.net, built from
scratch at the owner's direction ("Fresh script only"). It does not
reuse or modify the member site's payment code; it is its own
container, its own SQLite database (`/data/subs.db`), and its own
deploy (`ops/deploy-subs.sh`). It shares the `members-data` volume
only so a confirmed subscription can unlock course access in
`members.db`, matched by the buyer's account email.

Public address: **https://members.911transedu.net/subs/**

## What it does

- Stripe **Subscriptions API** + **Payment Element** (Stripe.js).
- `POST /subs/create-subscription` creates a Customer and a
  Subscription with `payment_behavior="default_incomplete"` and
  `save_default_payment_method="on_subscription"`; the browser
  confirms the first invoice's PaymentIntent (or SetupIntent) with
  the Payment Element.
- Payment methods requested: **card** (credit and debit) and
  **us_bank_account** (ACH direct debit, Financial Connections
  verification, 3-D Secure automatic on cards).
- **Apple Pay / Google Pay stay OFF** (owner direction, 2026-11-15):
  they ride on `card` only when the Stripe account enables them, so
  keep them disabled in Stripe → Settings → Payment methods, with
  **Cards** and **US bank account** enabled.
- Prices are inline `price_data` (no Dashboard Price objects):
  - CCCE course — $15 every **two years**, per provider.
  - EMSPR protocols — $6,000 every **year**.
- Webhook `POST /subs/webhook` is the only path that grants access:
  - `invoice.paid` → subscription active, course access granted in
    members.db for the account with the subscription's email.
  - `invoice.payment_failed` → marked past due (Stripe retries;
    access is revoked only on deletion).
  - `customer.subscription.deleted` → canceled; access revoked
    unless another live subscription covers the same account+course.
  ACH takes 1–4 business days to clear, so `invoice.paid` is the
  unlock moment — never the browser's return page.

## Keys

Environment only, never in git, never logged:

- `STRIPE_SECRET_KEY` (sk_live_…)
- `STRIPE_WEBHOOK_SECRET` (whsec_…)
- `STRIPE_PUBLISHABLE_KEY` (pk_live_…)

The deploy workflow delivers them from the GitHub Actions secrets
`SUBS_STRIPE_SECRET_KEY`, `SUBS_STRIPE_WEBHOOK_SECRET`, and
`SUBS_STRIPE_PUBLISHABLE_KEY` into the host `.env` (as
`SUBS_STRIPE_*`, which compose passes to this service) via
`ops/set-subs-stripe-keys.sh` — values travel on SSH stdin only.
Until all three exist, the service runs unconfigured: pages render,
checkout and webhook refuse politely.

## Stripe Dashboard checklist (owner)

1. Settings → Payment methods: enable **Card** and **US bank
   account**; disable **Apple Pay** and **Google Pay**.
2. Developers → Webhooks: add endpoint
   `https://members.911transedu.net/subs/webhook` with events
   `invoice.paid`, `invoice.payment_failed`,
   `customer.subscription.deleted`. Its signing secret is the
   `whsec_…` value.
3. Add the three repo secrets above and re-run the
   "Deploy subs service" workflow.
