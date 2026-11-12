#!/usr/bin/env bash
# Retarget the member site's Cash App payment cashtag in the host .env.
#
# Decision history (Taylor, 2026-11-12): course payments were switched
# to $taylorsprecher (Taylor's Business-badged Cash App account) and
# then moved BACK to $taylorsprecher2026 the same day — "Move course
# payments back to $taylorsprecher2026" — because the Bitcoin features,
# including the 50% Receive-as-Bitcoin auto-convert, exist only on the
# 2026 account. Both accounts are Taylor's; this is a retargeting
# between Taylor's own accounts, not a fix of a stranger's tag.
#
# docker compose interpolates CASHAPP_CASHTAG from
# /home/deploy/live-servers/.env into the members container; the app
# renders that value as the payment target on the buy / home / preview
# pages (app fallback default is the same $taylorsprecher2026). This script
# upserts the single CASHAPP_CASHTAG line (all other entries
# preserved), keeping 0600 permissions. A cashtag is a public payment
# address, so the before/after values are printed; no other env values
# are read or shown. Re-run the members deploy afterwards so the
# container is recreated with the new env.
set -eu
ENV_FILE="/home/deploy/live-servers/.env"
NEW_CASHTAG='$taylorsprecher2026'

if [ ! -f "${ENV_FILE}" ]; then
  echo "No host .env at ${ENV_FILE} — cannot set CASHAPP_CASHTAG." >&2
  exit 1
fi

before="$(grep -E '^CASHAPP_CASHTAG=' "${ENV_FILE}" | head -n 1 | cut -d '=' -f 2- || true)"
if [ -z "${before}" ]; then
  before="(unset — app fallback applied)"
fi

tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
grep -v -E '^CASHAPP_CASHTAG=' "${ENV_FILE}" > "${tmp}" || true
printf 'CASHAPP_CASHTAG=%s\n' "${NEW_CASHTAG}" >> "${tmp}"
chmod 600 "${tmp}"
mv "${tmp}" "${ENV_FILE}"
trap - EXIT

after="$(grep -E '^CASHAPP_CASHTAG=' "${ENV_FILE}" | head -n 1 | cut -d '=' -f 2- || true)"
echo "CASHAPP_CASHTAG in host .env: before=${before} after=${after}"
