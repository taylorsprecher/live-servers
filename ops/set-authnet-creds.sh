#!/usr/bin/env bash
# Deliver the payment-ledger service's Authorize.net credentials
# (API Login ID + Transaction Key) into the host .env (compose
# interpolation source).
#
# Called over SSH by .github/workflows/deploy-payledger.yml ONLY when
# both AUTHNET_* GitHub Actions secrets are configured. The values
# arrive on stdin as KEY=value lines — never as command-line
# arguments, never printed, never logged. They are upserted into .env
# beside docker-compose.yml (all other entries preserved) with 0600
# permissions, so the next `docker compose up` passes them to the
# payledger service, whose Authorize.net processor
# (services/payment-ledger/app_pkg/authnet.py) reads them from its
# environment. Same pattern as ops/set-zillion-aws-creds.sh.
#
# An OPTIONAL third line, AUTHNET_CLIENT_KEY, may ride along: the
# Public Client Key is a public identifier by design (it ships in
# web pages for Accept.js tokenization), not a secret, but it is
# delivered the same stdin way for consistency. The members service
# reads it from the same host .env (compose passes it through).
# When the line is absent or empty, any AUTHNET_CLIENT_KEY already
# in the host .env is preserved untouched.
#
# NOTE: delivering credentials does NOT enable the processor. It
# stays disabled until AUTHNET_ENABLED=1 is also set in the host
# .env — a separate, deliberate step.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

login_id=""
txn_key=""
client_key=""
while IFS='=' read -r k v; do
  case "${k}" in
    AUTHNET_API_LOGIN_ID) login_id="${v}" ;;
    AUTHNET_TRANSACTION_KEY) txn_key="${v}" ;;
    AUTHNET_CLIENT_KEY) client_key="${v}" ;;
  esac
done
# Tolerate incidental trailing whitespace/newlines in the delivered
# values; Authorize.net credential values never contain spaces.
login_id="$(printf '%s' "${login_id}" | tr -d '[:space:]')"
txn_key="$(printf '%s' "${txn_key}" | tr -d '[:space:]')"
client_key="$(printf '%s' "${client_key}" | tr -d '[:space:]')"

if [ -z "${login_id}" ] || [ -z "${txn_key}" ]; then
  echo "ERROR: both AUTHNET_API_LOGIN_ID and AUTHNET_TRANSACTION_KEY are required on stdin" >&2
  exit 1
fi

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
# The client key line is only replaced when a new value was actually
# delivered; otherwise an existing entry survives the upsert.
if [ -n "${client_key}" ]; then
  strip_pattern='^AUTHNET_(API_LOGIN_ID|TRANSACTION_KEY|CLIENT_KEY)='
else
  strip_pattern='^AUTHNET_(API_LOGIN_ID|TRANSACTION_KEY)='
fi
grep -v -E "${strip_pattern}" "${ENV_FILE}" > "${tmp}" || true
printf 'AUTHNET_API_LOGIN_ID=%s\n' "${login_id}" >> "${tmp}"
printf 'AUTHNET_TRANSACTION_KEY=%s\n' "${txn_key}" >> "${tmp}"
if [ -n "${client_key}" ]; then
  printf 'AUTHNET_CLIENT_KEY=%s\n' "${client_key}" >> "${tmp}"
fi
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "AUTHNET_* credentials upserted into host .env (values not shown; processor still disabled until AUTHNET_ENABLED=1)."
if [ -n "${client_key}" ]; then
  echo "AUTHNET_CLIENT_KEY also upserted into host .env (public Accept.js identifier; value not shown)."
fi
