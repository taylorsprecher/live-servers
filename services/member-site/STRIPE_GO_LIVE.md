# Stripe go-live — card, Google Pay, Apple Pay on the member site

> **STATUS — CLOSED (2026-11-12).** Taylor's decision: *"Let's just
> keep Cash App."* Card checkout is **not** going live. The GitHub
> Actions secrets were deleted and the Stripe lines were cleared
> from the host `.env`; the member site presents Cash App as its
> only payment method. This document is kept as the historical
> record of the Stripe setup that was prepared and briefly
> attempted — nothing in it is a pending step.

Decision history (Taylor, 2026-11-12): first *"Add them via Stripe"*
— Google Pay and Apple Pay were to be added through Stripe Checkout,
alongside the existing Cash App path, with Cash App remaining the
no-fee option. Later the same day Taylor superseded it: *"Let's just
keep Cash App."* That decision stands.

The site side remains as prepared (automatic payment methods in
Checkout, HTTPS return URLs, signature-verified webhook, wallet copy
that renders only while a key exists), but no key is configured, so
none of it is offered to buyers. **No key is ever committed to git,
pasted into chat, or printed in a log.**

## The two GitHub Actions secrets (exact names)

*Historical: both secrets were created on 2026-11-12 and deleted the
same day when the go-live was closed.*

Repository `taylorsprecher/live-servers` → Settings → Secrets and
variables → Actions:

| Secret name | Value | Where it comes from |
|---|---|---|
| `STRIPE_SECRET_KEY` | `sk_live_…` | Stripe Dashboard → Developers → API keys → Secret key |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` | Stripe Dashboard → Developers → Webhooks → the endpoint below → Signing secret |

## Checklist

*(Historical record of the steps prepared and performed on
2026-11-12 — not a pending plan.)*

1. **Create / activate the Stripe account** (Taylor's hands — needs
   the LLC's details and the payout bank account). Business:
   Taylor's Transgender Patient Awareness, LLC.
2. **Enable the wallets**: Stripe Dashboard → Settings → Payments →
   payment methods — turn on **Google Pay** and **Apple Pay** (cards
   are on by default). The app deliberately does *not* restrict
   `payment_method_types`, so Checkout shows whatever the account has
   enabled and the buyer's device supports. No Apple Pay domain
   verification is needed on our host: checkout is hosted by Stripe
   at `checkout.stripe.com`.
3. **Register the webhook**: Stripe Dashboard → Developers →
   Webhooks → Add endpoint:
   - URL: `https://members.911transedu.net/members/stripe/webhook`
   - Event: `checkout.session.completed`
   - Copy the endpoint's **signing secret** (`whsec_…`) for step 4.
4. **Add the two GitHub Actions secrets** named exactly as in the
   table above.
5. **Re-run the members deploy** (Actions → "Deploy members service
   only" → Run workflow). The workflow pipes the two secrets over SSH
   into the host `.env` via `ops/set-stripe-keys.sh` (values are never
   echoed or logged) and recreates the members container with them.
   From that deploy on, the buy pages and the member desk present
   paying by **card, Google Pay or Apple Pay** via Stripe Checkout,
   and a completed payment unlocks the buyer's access automatically
   through the webhook — no roster step.
6. **Verify with a real purchase**: buy the $15 CCCE course with a
   second account, confirm access unlocks on its own and the payment
   appears in the owner desk's "Recent card payments (Stripe)" log,
   then refund it in the Stripe Dashboard.

## Turning card checkout back off

**This was done on 2026-11-12.** The GitHub secrets were deleted and
the Stripe lines were removed from the host `.env`; the site runs
Cash App only. The mechanics, for the record: deleting the GitHub
secrets stops *future* delivery but does not remove keys already
written to the host — fully disabling card checkout required
removing the `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` lines
from `/home/deploy/live-servers/.env` on the host and re-running
the members deploy, which returned the site to Cash App only.

## Notes

- Stripe payouts, refunds, and buyer card disputes live in the Stripe
  Dashboard; the site only records completed checkouts and grants
  access.
- Test mode works the same way: a `sk_test_…` key plus a test-mode
  webhook endpoint and its `whsec_…` could be delivered through the
  same two secrets to rehearse in test mode.
