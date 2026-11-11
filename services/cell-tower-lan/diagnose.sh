#!/usr/bin/env bash
# Read-only diagnostic for the Cell Tower LAN demo service (live-celltower).
# Changes NOTHING on the host: it only reports container state, recent logs,
# host memory pressure, and whether the backend health endpoint answers on
# loopback. Full output lands in the dispatching workflow's run log.
set -u

echo "=== HOST ==="
date -u
free -m
echo

echo "=== CONTAINER STATUS (all live-* containers) ==="
docker ps -a --filter 'name=live-' \
  --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
echo

echo "=== live-celltower INSPECT (state) ==="
docker inspect live-celltower \
  --format 'Status={{.State.Status}} Running={{.State.Running}} Paused={{.State.Paused}} Restarting={{.State.Restarting}} OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}} Error={{.State.Error}} StartedAt={{.State.StartedAt}} FinishedAt={{.State.FinishedAt}} RestartCount={{.RestartCount}}' \
  2>&1
echo

echo "=== live-celltower STATS (one shot) ==="
docker stats --no-stream live-celltower 2>&1
echo

echo "=== live-celltower LOGS (last 120 lines) ==="
docker logs --tail 120 live-celltower 2>&1
echo

echo "=== LOOPBACK BACKEND HEALTH (127.0.0.1:18107, 5s timeout) ==="
curl -sS -m 5 -w '\nHTTP %{http_code} in %{time_total}s\n' \
  http://127.0.0.1:18107/celltower/api/health 2>&1 || echo "curl failed rc=$?"
echo

echo "=== LOOPBACK BACKEND ROOT (127.0.0.1:18107/celltower/, 5s timeout) ==="
curl -sS -m 5 -o /dev/null -w 'HTTP %{http_code} in %{time_total}s\n' \
  http://127.0.0.1:18107/celltower/ 2>&1 || echo "curl failed rc=$?"
echo

echo "=== PROCESSES INSIDE CONTAINER (if running) ==="
docker top live-celltower 2>&1 | head -20
echo

echo "=== DATA VOLUME ==="
docker exec live-celltower ls -la /data 2>&1 | head -20 || \
  echo "(docker exec unavailable — container not running?)"
echo

echo "=== HOST KERNEL OOM / DOCKER EVENTS (last 24h, celltower) ==="
docker events --since 24h --until 1s \
  --filter container=live-celltower \
  --format '{{.Time}} {{.Action}} {{.Actor.Attributes.exitCode}}' 2>&1 | tail -30
echo

echo "=== DIAGNOSE DONE ==="
