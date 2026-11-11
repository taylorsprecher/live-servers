#!/usr/bin/env bash
# fix-celltower.sh — repair a wedged Cell Tower LAN demo service.
#
# The failure this addresses (diagnosed Nov 11, 2026): the container
# stays "Up" and the node process stays alive, but the HTTP backend
# answers nothing — not even /celltower/api/health on the loopback
# port. Root cause chain: completed queue jobs were never pruned
# (345k rows / 204 MB SQLite file), queue scans became whole-database
# reads, and a 'database is locked' throw in the queue pump was
# retried at 0 ms forever, starving the event loop. The app code now
# carries the durable guards (store.js: WAL + busy_timeout + indexes
# + pruning; queue.js: save-inside-try + pump error backoff); this
# script is the operational repair for a service already wedged:
#
#   1. report pre-state (container, stats, log tail)
#   2. stop ONLY the celltower compose service
#   3. prune completed jobs + VACUUM via a one-off container on the
#      data volume (backend/prune-jobs.js, mounted in from this tree
#      so it works even with an older image)
#   4. rebuild + restart ONLY the celltower service (compose)
#   5. verify the loopback backend health endpoint, then the gateway
#      path; exit non-zero if either does not come back 200
#
# Idempotent: if the backend already answers 200, it reports healthy
# and exits 0 without touching anything (pass --force to prune and
# restart anyway). Nothing outside the celltower service is touched:
# no gateway, no other services, no audited stack.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SERVICE="celltower"
CONTAINER="live-celltower"
BACKEND_HEALTH="http://127.0.0.1:18107/celltower/api/health"
GATEWAY_HEALTH="http://127.0.0.1:443/celltower/api/health"
KEEP_COMPLETED="${CELL_LAN_KEEP_COMPLETED_JOBS:-1000}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

cd "$COMPOSE_DIR" || { echo "fix-celltower: cannot cd to $COMPOSE_DIR"; exit 1; }

health() { curl -s -o /dev/null -w '%{http_code}' -m 5 "$1" 2>/dev/null || echo 000; }

state() {
  docker ps -a --filter "name=$CONTAINER" \
    --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null
  docker stats --no-stream "$CONTAINER" 2>/dev/null || true
}

echo "=== fix-celltower: pre-state ==="
state
CODE="$(health "$BACKEND_HEALTH")"
echo "backend health ($BACKEND_HEALTH): HTTP $CODE"
if [ "$CODE" = "200" ] && [ "$FORCE" != "1" ]; then
  echo "fix-celltower: service is healthy — nothing to do."
  echo "(pass --force to prune + restart anyway)"
  exit 0
fi

echo "=== fix-celltower: recent logs (tail 40) ==="
docker logs --tail 40 "$CONTAINER" 2>&1 || true

echo "=== fix-celltower: stopping $SERVICE (this service only) ==="
docker compose stop "$SERVICE"

echo "=== fix-celltower: prune completed jobs (keep $KEEP_COMPLETED) + VACUUM ==="
IMG="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)"
VOL="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$CONTAINER" 2>/dev/null)"
if [ -n "$IMG" ] && [ -n "$VOL" ]; then
  docker run --rm \
    -v "$VOL:/data" \
    -v "$SCRIPT_DIR/backend/prune-jobs.js:/app/backend/prune-jobs.js:ro" \
    -e DATA_DIR=/data \
    -e CELL_LAN_KEEP_COMPLETED_JOBS="$KEEP_COMPLETED" \
    --entrypoint node "$IMG" /app/backend/prune-jobs.js \
    || echo "fix-celltower: WARNING — prune step failed; continuing with restart"
else
  echo "fix-celltower: WARNING — image/volume not resolvable (IMG='$IMG' VOL='$VOL'); skipping prune"
fi

echo "=== fix-celltower: starting $SERVICE (compose up --build, this service only) ==="
docker compose up -d --build "$SERVICE"

echo "=== fix-celltower: waiting for backend health ==="
ok=0
i=0
while [ "$i" -lt 45 ]; do
  CODE="$(health "$BACKEND_HEALTH")"
  if [ "$CODE" = "200" ]; then ok=1; echo "backend healthy after ~$((i * 2))s"; break; fi
  i=$((i + 1))
  sleep 2
done
if [ "$ok" != "1" ]; then
  echo "fix-celltower: FAIL — backend health still HTTP $CODE after 90s"
  docker logs --tail 60 "$CONTAINER" 2>&1 || true
  state
  exit 1
fi

GCODE="$(health "$GATEWAY_HEALTH")"
echo "gateway health ($GATEWAY_HEALTH): HTTP $GCODE"
echo "=== fix-celltower: post-state ==="
state
if [ "$GCODE" != "200" ]; then
  echo "fix-celltower: FAIL — backend recovered but gateway path returned HTTP $GCODE"
  exit 1
fi
echo "=== fix-celltower: OK — celltower healthy on backend and gateway ==="
