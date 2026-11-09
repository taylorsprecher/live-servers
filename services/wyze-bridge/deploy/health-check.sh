#!/usr/bin/env bash
# health-check.sh — verify the Wyze Bridge stack on the deployment host.
#
# RUN THIS ON THE DEPLOYMENT HOST (or any LAN machine that can reach it —
# set BRIDGE_BASE_HOST). Checks, in order:
#
#   1. Containers: wyze-bridge, status-api, dashboard are up (and the
#      archive containers too, when ARCHIVE_ENABLED=true in .env).
#   2. Bridge Web UI answers on :5000.
#   3. Status API /health, /api/cameras, /api/bridge/config answer, and
#      the camera list contains the four Sterner Home cameras.
#   4. Dashboard answers on :8081.
#
# Exits 0 when every check passes, 1 otherwise. Read-only: it changes
# nothing and never touches the cameras or the Wyze account.
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

HOST="${BRIDGE_BASE_HOST:-localhost}"
FAILURES=0

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1" >&2; FAILURES=$((FAILURES + 1)); }

check_http() {
  # check_http LABEL URL — pass when the URL answers with any 2xx/3xx
  local label="$1" url="$2"
  if curl -fsS -o /dev/null --max-time 10 "$url" 2>/dev/null; then
    pass "${label} (${url})"
  else
    fail "${label} did not answer (${url})"
  fi
}

# --- 1. Containers ---------------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  RUNNING="$(docker compose ps --status running --format '{{.Service}}' 2>/dev/null || true)"
  for svc in wyze-bridge status-api dashboard; do
    if printf '%s\n' "$RUNNING" | grep -qx "$svc"; then
      pass "container running: ${svc}"
    else
      fail "container not running: ${svc}"
    fi
  done
  if grep -q '^ARCHIVE_ENABLED=true' .env 2>/dev/null; then
    for svc in s3-sync kms-validator; do
      if printf '%s\n' "$RUNNING" | grep -qx "$svc"; then
        pass "container running: ${svc}"
      else
        fail "container not running: ${svc} (archive tier is marked enabled)"
      fi
    done
  fi
else
  echo "note: Docker not reachable from here — skipping container checks."
fi

# --- 2. Bridge Web UI --------------------------------------------------------
check_http "bridge Web UI" "http://${HOST}:5000/"

# --- 3. Status API -----------------------------------------------------------
API="http://${HOST}:8080"
check_http "status API health" "${API}/health"

CAMERAS_JSON="$(curl -fsS --max-time 10 "${API}/api/cameras" 2>/dev/null || true)"
if [ -n "$CAMERAS_JSON" ]; then
  for cam in Hallway Bedroom Dinning Family; do
    if printf '%s' "$CAMERAS_JSON" | grep -q "\"${cam}\""; then
      pass "camera listed: ${cam}"
    else
      fail "camera missing from status API response: ${cam}"
    fi
  done
  if printf '%s' "$CAMERAS_JSON" | grep -q '"source": "mock"'; then
    echo "note: status API is still serving MOCK camera state — swap"
    echo "      get_cameras() to poll the bridge API for live data."
  fi
else
  fail "status API /api/cameras returned nothing"
fi

check_http "status API bridge config" "${API}/api/bridge/config"
check_http "status API archive status" "${API}/api/archive/status"

# --- 4. Dashboard --------------------------------------------------------------
check_http "dashboard" "http://${HOST}:8081/"

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "HEALTH CHECK: all checks passed."
  exit 0
else
  echo "HEALTH CHECK: ${FAILURES} check(s) failed." >&2
  exit 1
fi
