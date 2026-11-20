#!/usr/bin/env bash
# Turn the payment-ledger Authorize.net processor ON by upserting
# AUTHNET_ENABLED=1 into the host .env (compose interpolation source).
#
# Called over SSH by .github/workflows/deploy-payledger.yml ONLY when
# the dispatcher explicitly set the workflow's enable_authnet input
# true — that input is the deliberate on-switch. The value arrives on
# stdin as one KEY=value line, never as a command-line argument. This
# script accepts exactly AUTHNET_ENABLED=1 and refuses anything else,
# so it cannot be used to write arbitrary .env entries. All other
# .env entries are preserved; the file keeps 0600 permissions. Same
# pattern as ops/set-authnet-creds.sh.
#
# Turning the processor on does not by itself make charges succeed:
# the credentials must also be present (ops/set-authnet-creds.sh) and
# Global Payments must have enabled Transaction Processing on the
# merchant account.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

value=""
while IFS='=' read -r k v; do
  case "${k}" in
    AUTHNET_ENABLED) value="$(printf '%s' "${v}" | tr -d '[:space:]')" ;;
  esac
done

if [ "${value}" != "1" ]; then
  echo "ERROR: expected exactly AUTHNET_ENABLED=1 on stdin" >&2
  exit 1
fi

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^AUTHNET_ENABLED=' "${ENV_FILE}" > "${tmp}" || true
printf 'AUTHNET_ENABLED=1\n' >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "AUTHNET_ENABLED=1 upserted into host .env (Authorize.net processor will start enabled on the next payledger up)."
