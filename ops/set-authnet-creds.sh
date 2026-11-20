#!/usr/bin/env bash
# Deliver the payment-ledger service's Authorize.net credentials
# (API Login ID + Transaction Key) into the host .env (compose
# interpolation source).
#
# Called over SSH by .github/workflows/deploy-payledger.yml ONLY when
# both AUTHNET_* GitHub Actions secrets are configured. The values
# arrive on stdin as two KEY=value lines — never as command-line
# arguments, never printed, never logged. They are upserted into .env
# beside docker-compose.yml (all other entries preserved) with 0600
# permissions, so the next `docker compose up` passes them to the
# payledger service, whose Authorize.net processor
# (services/payment-ledger/app_pkg/authnet.py) reads them from its
# environment. Same pattern as ops/set-zillion-aws-creds.sh.
#
# NOTE: delivering credentials does NOT enable the processor. It
# stays disabled until AUTHNET_ENABLED=1 is also set in the host
# .env — a separate, deliberate step.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

login_id=""
txn_key=""
while IFS='=' read -r k v; do
  case "${k}" in
    AUTHNET_API_LOGIN_ID) login_id="${v}" ;;
    AUTHNET_TRANSACTION_KEY) txn_key="${v}" ;;
  esac
done
# Tolerate incidental trailing whitespace/newlines in the delivered
# values; Authorize.net credential values never contain spaces.
login_id="$(printf '%s' "${login_id}" | tr -d '[:space:]')"
txn_key="$(printf '%s' "${txn_key}" | tr -d '[:space:]')"

if [ -z "${login_id}" ] || [ -z "${txn_key}" ]; then
  echo "ERROR: both AUTHNET_API_LOGIN_ID and AUTHNET_TRANSACTION_KEY are required on stdin" >&2
  exit 1
fi

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^AUTHNET_(API_LOGIN_ID|TRANSACTION_KEY)=' "${ENV_FILE}" > "${tmp}" || true
printf 'AUTHNET_API_LOGIN_ID=%s\n' "${login_id}" >> "${tmp}"
printf 'AUTHNET_TRANSACTION_KEY=%s\n' "${txn_key}" >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "AUTHNET_* credentials upserted into host .env (values not shown; processor still disabled until AUTHNET_ENABLED=1)."
