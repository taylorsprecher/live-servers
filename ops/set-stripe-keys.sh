#!/usr/bin/env bash
# Deliver Stripe keys into the host .env (compose interpolation source).
#
# Called over SSH by .github/workflows/deploy-members.yml ONLY when both
# GitHub Actions secrets are configured. The two values arrive on stdin
# as KEY=value lines — never as command-line arguments, never printed,
# never logged. They are upserted into .env beside docker-compose.yml
# (all other entries preserved) with 0600 permissions, so the next
# `docker compose up` passes them to the members service. See
# services/member-site/STRIPE_GO_LIVE.md for the full checklist.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

sk=""
wh=""
while IFS='=' read -r k v; do
  case "${k}" in
    STRIPE_SECRET_KEY) sk="${v}" ;;
    STRIPE_WEBHOOK_SECRET) wh="${v}" ;;
  esac
done

if [ -z "${sk}" ] || [ -z "${wh}" ]; then
  echo "ERROR: both STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET are required on stdin" >&2
  exit 1
fi
case "${sk}${wh}" in
  *[[:space:]]*)
    echo "ERROR: Stripe key values must not contain whitespace" >&2
    exit 1
    ;;
esac

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^(STRIPE_SECRET_KEY|STRIPE_WEBHOOK_SECRET)=' "${ENV_FILE}" > "${tmp}" || true
printf 'STRIPE_SECRET_KEY=%s\nSTRIPE_WEBHOOK_SECRET=%s\n' "${sk}" "${wh}" >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "Stripe keys upserted into host .env (values not shown)."
