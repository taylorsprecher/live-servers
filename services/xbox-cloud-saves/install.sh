#!/usr/bin/env bash
# Automatic installer for the Xbox Cloud Save demo service.
#
# Installs the backend design from Taylor's "Xbox AWS S3 1" document
# (presigned-URL save API: POST /saves/{saveId}/upload-url and
# GET /saves/{saveId}/download-url, XSTS-validated XUID-scoped keys
# users/{xuid}/savefiles/{saveId}, 15-minute signed URLs) into the
# EXISTING demo environment on this host. Demo data only — this script
# never touches real AWS, the gateway/TLS config, the audited
# production app, or any other service.
#
# What it does, in order:
#   1. Ensures the two deploy secrets exist in the repo-root .env
#      (XSAVE_XSTS_SIGNING_KEY, XSAVE_DEV_SECRET). Generates them with
#      openssl only when missing — never overwrites, never prints
#      values, never commits them (.env is excluded from rsync/git).
#   2. Builds and (re)creates ONLY the xbox container
#      (docker compose up -d --build xbox), the same targeted pattern
#      as ops/deploy-members.sh. It does NOT run the full deploy.sh
#      and does not lower deploy.sh's RAM gate.
#   3. Waits for the service health endpoint on loopback.
#   4. Self-verifies end to end through the gateway with
#      verify_roundtrip.py: demo login -> upload URL -> PUT a demo
#      save -> download URL -> GET it back byte-identical -> cleanup.
#   5. Prints read-only spot checks of neighbouring services.
#
# Idempotent: safe to re-run at any time. Exits non-zero with a clear
# report if the xbox health check or the round-trip fails.
#
# Run on the host from anywhere:
#   bash /home/deploy/live-servers/services/xbox-cloud-saves/install.sh
# Environment overrides:
#   XSAVE_VERIFY_BASE  base URL for the round-trip (default
#                      http://127.0.0.1:443/xbox — the gateway path)
set -eu

SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SERVICE_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

echo "=== XBOX CLOUD SAVE — AUTOMATIC INSTALL (demo environment) ==="
echo "repo: ${REPO_ROOT}"

echo "=== MEMORY BEFORE ==="
free -m || true

echo "=== ENSURE DEPLOY SECRETS (.env, values never printed) ==="
touch .env
for var in XSAVE_XSTS_SIGNING_KEY XSAVE_DEV_SECRET; do
  if grep -q "^${var}=.\+" .env; then
    echo "SECRET ${var} present — kept"
  else
    # Remove a possibly-empty stub line, then append a fresh secret.
    sed -i "/^${var}=$/d" .env
    printf '%s=%s\n' "${var}" "$(openssl rand -hex 32)" >> .env
    echo "SECRET ${var} generated"
  fi
done

echo "=== BUILD + UP xbox only ==="
docker compose up -d --build xbox

echo "=== WAIT FOR xbox health (loopback) ==="
code="000"
for i in $(seq 1 25); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18102/xbox/health)" || code="000"
  [ "${code}" = "200" ] && break
  sleep 4
done
echo "CHECK xbox direct ${code}"
if [ "${code}" != "200" ]; then
  echo "INSTALL FAILED: xbox health endpoint did not come up (last code ${code})"
  docker compose ps xbox || true
  docker compose logs --tail 50 xbox || true
  exit 1
fi

echo "=== SELF-VERIFY: presigned-URL save round-trip via gateway ==="
VERIFY_BASE="${XSAVE_VERIFY_BASE:-http://127.0.0.1:443/xbox}"
if ! python3 "${SERVICE_DIR}/verify_roundtrip.py" --base "${VERIFY_BASE}"; then
  echo "INSTALL FAILED: end-to-end save round-trip verification failed"
  exit 1
fi

echo "=== GATEWAY SPOT CHECKS (read-only) ==="
for pair in \
  "xbox http://127.0.0.1:443/xbox/health" \
  "members https://members.911transedu.net/members/" \
  "maclan http://127.0.0.1:443/maclan/health" \
  "specmatrix http://127.0.0.1:443/specmatrix/health" \
  "celltower http://127.0.0.1:443/celltower/api/health" ; do
  name="${pair%% *}"; url="${pair#* }"
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "${url}")" || c="000"
  echo "CHECK ${name} ${c}"
done

echo "=== AUDITED APP (separate production stack — must be untouched) ==="
APPCODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)" || APPCODE="000"
echo "CHECK audited-app ${APPCODE}"

echo "=== MEMORY AFTER ==="
free -m || true
docker compose ps xbox
echo "=== XBOX INSTALL DONE ==="
