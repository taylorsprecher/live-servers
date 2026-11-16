#!/usr/bin/env bash
# Deliver the Braintree subscriptions service's credentials and
# plan IDs into the host .env (compose interpolation source).
#
# Called over SSH by .github/workflows/deploy-btsubs.yml ONLY when
# all required GitHub Actions secrets are configured. The values
# arrive on stdin as KEY=value lines — never as command-line
# arguments, never printed, never logged. They are upserted into
# .env beside docker-compose.yml (all other entries preserved)
# with 0600 permissions, so the next `docker compose up` passes
# them to the btsubs service. See
# services/braintree-subscriptions/README.md.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

mid=""; pub=""; priv=""; envname=""; pccce=""; pemspr=""; gmid=""
while IFS='=' read -r k v; do
  case "${k}" in
    BT_BRAINTREE_MERCHANT_ID) mid="${v}" ;;
    BT_BRAINTREE_PUBLIC_KEY) pub="${v}" ;;
    BT_BRAINTREE_PRIVATE_KEY) priv="${v}" ;;
    BT_BRAINTREE_ENVIRONMENT) envname="${v}" ;;
    BT_BRAINTREE_PLAN_CCCE) pccce="${v}" ;;
    BT_BRAINTREE_PLAN_EMSPR) pemspr="${v}" ;;
    BT_BRAINTREE_GOOGLE_MERCHANT_ID) gmid="${v}" ;;
  esac
done

if [ -z "${mid}" ] || [ -z "${pub}" ] || [ -z "${priv}" ] || [ -z "${pccce}" ] || [ -z "${pemspr}" ]; then
  echo "ERROR: BT_BRAINTREE_MERCHANT_ID, BT_BRAINTREE_PUBLIC_KEY, BT_BRAINTREE_PRIVATE_KEY, BT_BRAINTREE_PLAN_CCCE and BT_BRAINTREE_PLAN_EMSPR are all required on stdin" >&2
  exit 1
fi
[ -z "${envname}" ] && envname="sandbox"
case "${mid}${pub}${priv}${pccce}${pemspr}${gmid}" in
  *[[:space:]]*)
    echo "ERROR: Braintree values must not contain whitespace" >&2
    exit 1
    ;;
esac

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^(BT_BRAINTREE_MERCHANT_ID|BT_BRAINTREE_PUBLIC_KEY|BT_BRAINTREE_PRIVATE_KEY|BT_BRAINTREE_ENVIRONMENT|BT_BRAINTREE_PLAN_CCCE|BT_BRAINTREE_PLAN_EMSPR|BT_BRAINTREE_GOOGLE_MERCHANT_ID)=' "${ENV_FILE}" > "${tmp}" || true
{
  printf 'BT_BRAINTREE_MERCHANT_ID=%s\n' "${mid}"
  printf 'BT_BRAINTREE_PUBLIC_KEY=%s\n' "${pub}"
  printf 'BT_BRAINTREE_PRIVATE_KEY=%s\n' "${priv}"
  printf 'BT_BRAINTREE_ENVIRONMENT=%s\n' "${envname}"
  printf 'BT_BRAINTREE_PLAN_CCCE=%s\n' "${pccce}"
  printf 'BT_BRAINTREE_PLAN_EMSPR=%s\n' "${pemspr}"
  printf 'BT_BRAINTREE_GOOGLE_MERCHANT_ID=%s\n' "${gmid}"
} >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "Braintree credentials stored in ${ENV_FILE} (values not shown)"
