#!/usr/bin/env bash
# Deliver the payment-ledger SecureStore vault's Fernet key
# (PAYLEDGER_SECURESTORE_KEY) into the host .env (compose
# interpolation source).
#
# Called over SSH by .github/workflows/deploy-payledger.yml ONLY when
# the PAYLEDGER_SECURESTORE_KEY GitHub Actions secret is configured.
# The value arrives on stdin as one KEY=value line — never as a
# command-line argument, never printed, never logged. It is upserted
# into .env beside docker-compose.yml (all other entries preserved)
# with 0600 permissions, so the next `docker compose up` passes it to
# the payledger service, whose receipt vault
# (services/payment-ledger/app_pkg/vault.py) reads it from its
# environment. Same pattern as ops/set-authnet-creds.sh.
#
# NOTE: delivering the key does NOT enable the vault. It stays dark
# until PAYLEDGER_SECURESTORE_ENABLED=1 is also set in the host .env
# — a separate, deliberate step (ops/set-securestore-enabled.sh).
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

vault_key=""
while IFS='=' read -r k v; do
  case "${k}" in
    PAYLEDGER_SECURESTORE_KEY) vault_key="${v}" ;;
  esac
done
# Tolerate incidental trailing whitespace/newlines in the delivered
# value; a Fernet key never contains spaces.
vault_key="$(printf '%s' "${vault_key}" | tr -d '[:space:]')"

if [ -z "${vault_key}" ]; then
  echo "ERROR: PAYLEDGER_SECURESTORE_KEY is required on stdin" >&2
  exit 1
fi

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^PAYLEDGER_SECURESTORE_KEY=' "${ENV_FILE}" > "${tmp}" || true
printf 'PAYLEDGER_SECURESTORE_KEY=%s\n' "${vault_key}" >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "PAYLEDGER_SECURESTORE_KEY upserted into host .env (value not shown; vault still dark until PAYLEDGER_SECURESTORE_ENABLED=1)."
