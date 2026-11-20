#!/usr/bin/env bash
# Run the payment ledger's Authorize.net credential check FROM the host,
# using the ledger admin token that lives only in the host .env.
#
# Why host-side: PAYLEDGER_ADMIN_TOKEN was generated into the host .env
# by ops/deploy-payledger.sh (2026-11-15) and has never left the host —
# nobody holds a copy. This script keeps it that way: it reads the
# token from .env into a shell variable, presents it in the
# X-Admin-Token header to the ledger's loopback endpoint, and prints
# ONLY the check result. The token value is never printed, never put in
# argv (curl reads the header from a stdin-fed config), and never
# leaves the host.
#
# If the token is somehow absent from .env, the script generates one
# with the same method as ops/deploy-payledger.sh, appends it (0600),
# and recreates the payledger container so the app picks it up.
#
# The check itself (POST /ledger/processor/authnet/check ->
# authnet.authenticate_check) asks Authorize.net to authenticate the
# merchant credentials. It moves no money and writes nothing to the
# ledger. Its JSON answer says whether the API Login ID + Transaction
# Key pair is valid; it contains no credential values.
set -eu
cd /home/deploy/live-servers

ENV_FILE=".env"
touch "${ENV_FILE}"

if ! grep -q '^PAYLEDGER_ADMIN_TOKEN=' "${ENV_FILE}"; then
  val="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "PAYLEDGER_ADMIN_TOKEN=${val}" >> "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  echo "generated PAYLEDGER_ADMIN_TOKEN (stored in host .env only)"
  echo "=== recreate payledger so it picks up the token ==="
  docker compose up -d payledger
  for i in $(seq 1 40); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18123/ledger/health || true)"
    [ "${code}" = "200" ] && break
    sleep 5
  done
  echo "payledger health after recreate: ${code} (want 200)"
  [ "${code}" = "200" ]
else
  echo "PAYLEDGER_ADMIN_TOKEN already present in host .env"
fi

token="$(grep '^PAYLEDGER_ADMIN_TOKEN=' "${ENV_FILE}" | head -1 | cut -d= -f2-)"
echo "token length: ${#token} characters (value never printed)"

echo "=== POST /ledger/processor/authnet/check (loopback, admin header via stdin config) ==="
{ printf 'header = "X-Admin-Token: %s"\n' "${token}"; } | \
  curl -sS -K - -X POST http://127.0.0.1:18123/ledger/processor/authnet/check \
    -H 'Content-Type: application/json' -d '{}' \
    -w '\nHTTP %{http_code}\n'
