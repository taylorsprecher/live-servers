# Stripe go-live — card, Google Pay, Apple Pay on the member site

Decision (Taylor, 2026-11-12): *"Add them via Stripe."* Google Pay and
Apple Pay are added through Stripe Checkout, alongside the existing
Cash App path — Cash App remains the no-fee option; nothing about the
Cash App flow changes when Stripe goes live.

The site side is already prepared (automatic payment methods in
Checkout, HTTPS return URLs, signature-verified webhook, wallet copy
that renders only while the key exists). What remains is connecting
Taylor's Stripe account and delivering two keys. **No key is ever
committed to git, pasted into chat, or printed in a log.**

## The two GitHub Actions secrets (exact names)

Repository `taylorsprecher/live-servers` → Settings → Secrets and
variables → Actions:

| Secret name | Value | Where it comes from |
|---|---|---|
| `STRIPE_SECRET_KEY` | `sk_live_…` | Stripe Dashboard → Developers → API keys → Secret key |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` | Stripe Dashboard → Developers → Webhooks → the endpoint below → Signing secret |

## Checklist

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

Deleting the GitHub secrets stops *future* delivery but does not
remove keys already written to the host. To fully disable card
checkout: remove the `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET`
lines from `/home/deploy/live-servers/.env` on the host and re-run
the members deploy — the site returns to Cash App only.

## Notes

- Stripe payouts, refunds, and buyer card disputes live in the Stripe
  Dashboard; the site only records completed checkouts and grants
  access.
- Test mode works the same way: a `sk_test_…` key plus a test-mode
  webhook endpoint and its `whsec_…` can be delivered through the
  same two secrets to rehearse before going live.
