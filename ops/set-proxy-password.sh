#!/usr/bin/env bash
# Deliver the personal proxy's Shadowsocks password into the host
# .env (compose interpolation source).
#
# Called over SSH by .github/workflows/deploy-proxy.yml ONLY when the
# PROXY_SS_PASSWORD GitHub Actions secret is configured. The value
# arrives on stdin as a single PROXY_SS_PASSWORD=value line — never as
# a command-line argument, never printed, never logged. It is upserted
# into .env beside docker-compose.yml (all other entries preserved)
# with 0600 permissions, so the next `docker compose up` passes it to
# the proxy service. See services/proxy/README.md.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

pw=""
while IFS='=' read -r k v; do
  case "${k}" in
    PROXY_SS_PASSWORD) pw="${v}" ;;
  esac
done
# Tolerate incidental trailing whitespace/newlines in the delivered
# value; the password itself is hex and never contains spaces.
pw="$(printf '%s' "${pw}" | tr -d '[:space:]')"

if [ -z "${pw}" ]; then
  echo "ERROR: PROXY_SS_PASSWORD is required on stdin" >&2
  exit 1
fi

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^PROXY_SS_PASSWORD=' "${ENV_FILE}" > "${tmp}" || true
printf 'PROXY_SS_PASSWORD=%s\n' "${pw}" >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "PROXY_SS_PASSWORD upserted into host .env (value not shown)."
