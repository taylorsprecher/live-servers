#!/usr/bin/env bash
# Deliver the Stripe subscriptions service's keys into the host .env
# (compose interpolation source).
#
# Called over SSH by .github/workflows/deploy-subs.yml ONLY when all
# three GitHub Actions secrets are configured. The values arrive on
# stdin as KEY=value lines — never as command-line arguments, never
# printed, never logged. They are upserted into .env beside
# docker-compose.yml (all other entries preserved) with 0600
# permissions, so the next `docker compose up` passes them to the
# subs service as STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET /
# STRIPE_PUBLISHABLE_KEY. See services/stripe-subscriptions/README.md.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

sk=""
wh=""
pk=""
while IFS='=' read -r k v; do
  case "${k}" in
    SUBS_STRIPE_SECRET_KEY) sk="${v}" ;;
    SUBS_STRIPE_WEBHOOK_SECRET) wh="${v}" ;;
    SUBS_STRIPE_PUBLISHABLE_KEY) pk="${v}" ;;
  esac
done

if [ -z "${sk}" ] || [ -z "${wh}" ] || [ -z "${pk}" ]; then
  echo "ERROR: SUBS_STRIPE_SECRET_KEY, SUBS_STRIPE_WEBHOOK_SECRET and SUBS_STRIPE_PUBLISHABLE_KEY are all required on stdin" >&2
  exit 1
fi
case "${sk}${wh}${pk}" in
  *[[:space:]]*)
    echo "ERROR: Stripe key values must not contain whitespace" >&2
    exit 1
    ;;
esac

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^(SUBS_STRIPE_SECRET_KEY|SUBS_STRIPE_WEBHOOK_SECRET|SUBS_STRIPE_PUBLISHABLE_KEY)=' "${ENV_FILE}" > "${tmp}" || true
printf 'SUBS_STRIPE_SECRET_KEY=%s\nSUBS_STRIPE_WEBHOOK_SECRET=%s\nSUBS_STRIPE_PUBLISHABLE_KEY=%s\n' "${sk}" "${wh}" "${pk}" >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "subs Stripe keys stored in ${ENV_FILE} (values not shown)"
