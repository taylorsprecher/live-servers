#!/usr/bin/env bash
# Deliver the Zillion Storage service's AWS base credentials into the
# host .env (compose interpolation source).
#
# Called over SSH by .github/workflows/deploy-zillion.yml ONLY when
# both ZILLION_AWS_* GitHub Actions secrets are configured. The values
# arrive on stdin as two KEY=value lines — never as command-line
# arguments, never printed, never logged. They are upserted into .env
# beside docker-compose.yml (all other entries preserved) with 0600
# permissions, so the next `docker compose up` passes them to the
# zillion service, which uses them to STS AssumeRole into
# zillion-control-plane-role for the S3 backend. Same pattern as
# ops/set-proxy-password.sh. See services/zillion-storage/README.md.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

key_id=""
secret_key=""
while IFS='=' read -r k v; do
  case "${k}" in
    ZILLION_AWS_ACCESS_KEY_ID) key_id="${v}" ;;
    ZILLION_AWS_SECRET_ACCESS_KEY) secret_key="${v}" ;;
  esac
done
# Tolerate incidental trailing whitespace/newlines in the delivered
# values; AWS credential values never contain spaces.
key_id="$(printf '%s' "${key_id}" | tr -d '[:space:]')"
secret_key="$(printf '%s' "${secret_key}" | tr -d '[:space:]')"

if [ -z "${key_id}" ] || [ -z "${secret_key}" ]; then
  echo "ERROR: both ZILLION_AWS_ACCESS_KEY_ID and ZILLION_AWS_SECRET_ACCESS_KEY are required on stdin" >&2
  exit 1
fi

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^ZILLION_AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)=' "${ENV_FILE}" > "${tmp}" || true
printf 'ZILLION_AWS_ACCESS_KEY_ID=%s\n' "${key_id}" >> "${tmp}"
printf 'ZILLION_AWS_SECRET_ACCESS_KEY=%s\n' "${secret_key}" >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "ZILLION_AWS_* credentials upserted into host .env (values not shown)."
