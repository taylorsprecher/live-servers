#!/usr/bin/env bash
# Turn payment-ledger auto-renewal ON by upserting
# AUTHNET_RENEWAL_ENABLED=1 into the host .env (compose
# interpolation source). Same pattern as
# ops/set-authnet-echeck-enabled.sh.
#
# Called over SSH by .github/workflows/deploy-payledger.yml ONLY
# when the dispatcher explicitly set the workflow's enable_renewal
# input true — that input is the deliberate on-switch. The value
# arrives on stdin as one KEY=value line, never as a command-line
# argument. This script accepts exactly AUTHNET_RENEWAL_ENABLED=1
# and refuses anything else, so it cannot be used to write arbitrary
# .env entries. All other .env entries are preserved; the file
# keeps 0600 permissions.
#
# Turning renewal on does not by itself make renewals run:
# AUTHNET_ENABLED and AUTHNET_ECHECK_ENABLED must also be 1 and the
# credentials present and valid (ops/set-authnet-creds.sh). The buy
# pages additionally need the members service redeployed before the
# auto-renew checkbox renders (it reads the same host .env entry).
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

value=""
while IFS='=' read -r k v; do
  case "${k}" in
    AUTHNET_RENEWAL_ENABLED) value="$(printf '%s' "${v}" | tr -d '[:space:]')" ;;
  esac
done

if [ "${value}" != "1" ]; then
  echo "ERROR: expected exactly AUTHNET_RENEWAL_ENABLED=1 on stdin" >&2
  exit 1
fi

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^AUTHNET_RENEWAL_ENABLED=' "${ENV_FILE}" > "${tmp}" || true
printf 'AUTHNET_RENEWAL_ENABLED=1\n' >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "AUTHNET_RENEWAL_ENABLED=1 upserted into host .env (auto-renewal enrollment + scheduler will start enabled on the next payledger up; buy-page auto-renew checkbox also needs the members redeploy)."
