# Braintree Subscriptions Service (fresh script, 2026-11-15)

Standalone subscription checkout for 911transedu.net on **Braintree
(by PayPal)**, built from scratch at the owner's direction ("remove
Stripe completely", "Fresh script only", "Install"). It replaces
the Stripe subscriptions service outright — that service's repo
files are deleted and its container is removed by this deploy. It
shares the `members-data` volume only so a confirmed subscription
can unlock course access in `members.db`, matched by the buyer's
account email.

Public address: **https://members.911transedu.net/subs/**

## What it does

- Braintree **Drop-in UI** collects card, **Apple Pay**,
  **Google Pay**, or bank (ACH); the server vaults the returned
  nonce (`customer.create`, card verification on) and starts the
  subscription against the vaulted token + a Control Panel plan.
- Credit **and** debit cards are accepted (the reference design's
  optional debit-only rejection is deliberately not implemented).
- **ACH micro-deposit flow** (two stages) for buyers who verify by
  routing + account number: stage 1 vaults with
  `verification_method: "micro_transfers"` under the NACHA mandate
  shown on the page and stores the pending verification
  server-side; stage 2 confirms the two deposit amounts and only
  then creates the subscription.
- Webhook `POST /subs/webhook` is the only path that grants access:
  - `subscription_charged_successfully` / `subscription_went_active`
    → course access granted in members.db.
  - `subscription_charged_unsuccessfully` → past due.
  - `subscription_canceled` → access revoked (unless another live
    subscription covers the same account + course).

## Credentials and plans (environment only, never in git)

- `BRAINTREE_MERCHANT_ID`, `BRAINTREE_PUBLIC_KEY`,
  `BRAINTREE_PRIVATE_KEY`
- `BRAINTREE_ENVIRONMENT` — `sandbox` or `production`
- `BRAINTREE_PLAN_CCCE`, `BRAINTREE_PLAN_EMSPR` — plan IDs from the
  Braintree Control Panel (plans carry price + interval there)
- `BRAINTREE_GOOGLE_MERCHANT_ID` — optional; needed for Google Pay
  in production

The deploy workflow delivers them from the GitHub Actions secrets
`BT_BRAINTREE_*` into the host `.env` via
`ops/set-braintree-keys.sh` — values travel on SSH stdin only.
Until all required values exist, the service runs unconfigured:
pages render, checkout and webhook refuse politely.

## Braintree Control Panel checklist (owner)

1. Create the Braintree account (PayPal) if none exists; enable
   **ACH Direct Debit**, **Apple Pay** (with domain verification
   for members.911transedu.net), and **Google Pay**.
2. Create two recurring Plans matching the owner's cadence:
   - CCCE — $15.00 every **24 months**
   - EMSPR — $6,000.00 every **12 months**
   Note their plan IDs.
3. Settings → API keys: merchant ID, public key, private key.
4. Settings → Webhooks: destination
   `https://members.911transedu.net/subs/webhook` with the
   subscription kinds (charged successfully / charged
   unsuccessfully / canceled / went active).
5. Add the seven `BT_BRAINTREE_*` repo secrets and re-run the
   "Deploy btsubs service" workflow.
