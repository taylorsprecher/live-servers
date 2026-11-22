#!/usr/bin/env bash
# Turn the payment-ledger SecureStore receipt vault ON by upserting
# PAYLEDGER_SECURESTORE_ENABLED=1 into the host .env (compose
# interpolation source).
#
# Called over SSH by .github/workflows/deploy-payledger.yml ONLY when
# the dispatcher explicitly set the workflow's enable_securestore
# input true — that input is the deliberate on-switch. The value
# arrives on stdin as one KEY=value line, never as a command-line
# argument. This script accepts exactly
# PAYLEDGER_SECURESTORE_ENABLED=1 and refuses anything else, so it
# cannot be used to write arbitrary .env entries. All other .env
# entries are preserved; the file keeps 0600 permissions. Same
# pattern as ops/set-authnet-enabled.sh.
#
# Turning the switch on does not by itself make the vault record:
# the Fernet key must also be present (ops/set-securestore-key.sh)
# and valid, or the vault reports disabled in /ledger/health.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

value=""
while IFS='=' read -r k v; do
  case "${k}" in
    PAYLEDGER_SECURESTORE_ENABLED) value="$(printf '%s' "${v}" | tr -d '[:space:]')" ;;
  esac
done

if [ "${value}" != "1" ]; then
  echo "ERROR: expected exactly PAYLEDGER_SECURESTORE_ENABLED=1 on stdin" >&2
  exit 1
fi

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^PAYLEDGER_SECURESTORE_ENABLED=' "${ENV_FILE}" > "${tmp}" || true
printf 'PAYLEDGER_SECURESTORE_ENABLED=1\n' >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "PAYLEDGER_SECURESTORE_ENABLED=1 upserted into host .env (SecureStore vault will start enabled on the next payledger up, once its key is present and valid)."
