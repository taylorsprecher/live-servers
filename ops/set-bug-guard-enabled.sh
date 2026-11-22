#!/usr/bin/env bash
# Turn the member-site bug guard ON by upserting
# MEMBERS_BUG_GUARD_ENABLED=1 into the host .env (compose
# interpolation source). Same pattern as
# ops/set-authnet-renewal-enabled.sh.
#
# Called over SSH by .github/workflows/deploy-members.yml ONLY
# when the dispatcher explicitly set the workflow's
# enable_bug_guard input true — that input is the deliberate
# on-switch. The value arrives on stdin as one KEY=value line,
# never as a command-line argument. This script accepts exactly
# MEMBERS_BUG_GUARD_ENABLED=1 and refuses anything else, so it
# cannot be used to write arbitrary .env entries. All other .env
# entries are preserved; the file keeps 0600 permissions.
set -eu
cd "$(dirname "$0")/.."
ENV_FILE=".env"

value=""
while IFS='=' read -r k v; do
  case "${k}" in
    MEMBERS_BUG_GUARD_ENABLED) value="$(printf '%s' "${v}" | tr -d '[:space:]')" ;;
  esac
done

if [ "${value}" != "1" ]; then
  echo "ERROR: expected exactly MEMBERS_BUG_GUARD_ENABLED=1 on stdin" >&2
  exit 1
fi

touch "${ENV_FILE}"
tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^MEMBERS_BUG_GUARD_ENABLED=' "${ENV_FILE}" > "${tmp}" || true
printf 'MEMBERS_BUG_GUARD_ENABLED=1\n' >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT
echo "MEMBERS_BUG_GUARD_ENABLED=1 upserted into host .env (the bug guard arms on the next members up)."
