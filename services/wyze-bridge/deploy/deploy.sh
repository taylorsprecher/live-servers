#!/usr/bin/env bash
# deploy.sh — start the Wyze Bridge stack on the home-LAN Docker host.
#
# RUN THIS ON THE DEPLOYMENT HOST, after deploy/preflight.sh has created
# .env and the secrets/ files. Brings up the default stack and then runs
# deploy/health-check.sh against it.
#
#   deploy/deploy.sh            bridge + status-api + dashboard
#   deploy/deploy.sh --archive  also start the archive tier (s3-sync +
#                               kms-validator); requires ARCHIVE_CONFIGURED
#                               secrets from preflight
#   deploy/deploy.sh --build    rebuild the status-api image first
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

fail() { echo "DEPLOY FAIL: $*" >&2; exit 1; }
info() { echo "==> $*"; }

USE_ARCHIVE=0
REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --archive) USE_ARCHIVE=1 ;;
    --build)   REBUILD=1 ;;
    *) fail "unknown option: $arg (expected --archive and/or --build)" ;;
  esac
done

command -v docker >/dev/null 2>&1 || fail "docker is not installed; run deploy/preflight.sh first."
docker info >/dev/null 2>&1 || fail "the Docker daemon is not running."
[ -f .env ] || fail ".env is missing; run deploy/preflight.sh first."

# The bridge cannot sign in without these two secret files. Check presence
# only — values are never read by this script.
for name in wyze_email wyze_password; do
  [ -s "secrets/${name}" ] || fail "secrets/${name} is missing or empty; run deploy/preflight.sh first."
done

if [ "$USE_ARCHIVE" = "1" ]; then
  for name in aws_access_key_id aws_secret_access_key aws_region s3_bucket; do
    [ -s "secrets/${name}" ] || fail "secrets/${name} is missing or empty; re-run preflight and choose the archive tier."
  done
fi

mkdir -p recordings wyze-config

COMPOSE=(docker compose)
if [ "$USE_ARCHIVE" = "1" ]; then
  COMPOSE+=(--profile archive)
fi

if [ "$REBUILD" = "1" ]; then
  info "Rebuilding the status-api image…"
  "${COMPOSE[@]}" build status-api
fi

if [ "$USE_ARCHIVE" = "1" ]; then
  # Record the running state so the status service's archive view is honest.
  if grep -q '^ARCHIVE_ENABLED=' .env; then
    awk 'BEGIN{FS=OFS="="} $1=="ARCHIVE_ENABLED"{$0="ARCHIVE_ENABLED=true"} {print}' .env > .env.tmp
    mv .env.tmp .env
  else
    printf 'ARCHIVE_ENABLED=true\n' >> .env
  fi
fi

info "Starting the stack…"
"${COMPOSE[@]}" up -d

info "Waiting for the status API to answer…"
STATUS_API_URL="${STATUS_API_URL:-http://localhost:8080}"
for _ in $(seq 1 30); do
  if curl -fsS "${STATUS_API_URL}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

"$(dirname "${BASH_SOURCE[0]}")/health-check.sh"

cat <<EOF

Stack is up (as far as the health check can tell).
  Bridge Web UI : http://localhost:5000/
  Dashboard     : http://localhost:8081/
  Status API    : ${STATUS_API_URL}/api/cameras
  Logs          : docker compose logs -f wyze-bridge
Moment of truth: open the bridge UI, confirm all four cameras are
discovered, and play the Family camera's stream.
EOF
