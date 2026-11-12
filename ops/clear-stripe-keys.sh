#!/usr/bin/env bash
# Remove Stripe keys from the host .env (turn card checkout OFF).
#
# Counterpart to ops/set-stripe-keys.sh. Decision (Taylor, 2026-11-12):
# "Let's just keep Cash App" — the Stripe card-checkout go-live is
# closed, so the buy pages must present Cash App only. The site renders
# card / Google Pay / Apple Pay copy only while STRIPE_SECRET_KEY is
# present in the members container's environment, which docker compose
# interpolates from /home/deploy/live-servers/.env.
#
# Deleting the GitHub Actions secrets stops future delivery but does
# not remove keys already written here; this script removes the
# STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET lines (all other entries
# preserved), keeping 0600 permissions. Values are never printed.
# Re-run the members deploy afterwards so the container is recreated
# without the keys. See services/member-site/STRIPE_GO_LIVE.md
# ("Turning card checkout back off").
set -eu
ENV_FILE="/home/deploy/live-servers/.env"

if [ ! -f "${ENV_FILE}" ]; then
  echo "No host .env at ${ENV_FILE} — nothing to clear."
  exit 0
fi

before="$(grep -c -E '^(STRIPE_SECRET_KEY|STRIPE_WEBHOOK_SECRET)=' "${ENV_FILE}" || true)"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^(STRIPE_SECRET_KEY|STRIPE_WEBHOOK_SECRET)=' "${ENV_FILE}" > "${tmp}" || true
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
after="$(grep -c -E '^(STRIPE_SECRET_KEY|STRIPE_WEBHOOK_SECRET)=' "${ENV_FILE}" || true)"
echo "Stripe key lines in host .env: before=${before} after=${after} (values not shown)."
