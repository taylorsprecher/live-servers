#!/usr/bin/env bash
# Targeted deploy for Zillion Storage ONLY (services/zillion-storage).
#
# What this installs: the runnable core of Taylor's "Python Cloud —
# 1 Zillion TB" multi-cloud storage design (filed as
# tmp3D_Python_Cloud_1Zillion_TB.docx) — streaming ingest, concurrent
# replication to local backends, retry with backoff, SHA-256
# verification — reachable at https://members.911transedu.net/zillion/.
# Directed 2026-11-18: "Install" / "Apply to AWS S3" / "And websites".
# The compose service and the gateway /zillion/ location are already
# on main; this deploy starts the container and force-recreates the
# gateway to pick the route up.
#
# Why targeted (same reasoning as ops/deploy-payledger.sh): deploy.sh's
# RAM gate protects the host during a FULL build. This script builds
# only zillion and force-recreates the gateway. It does NOT lower or
# edit deploy.sh's gate and does not rebuild any other service.
# Credentials: when the workflow has delivered ZILLION_AWS_* into the
# host .env (ops/set-zillion-aws-creds.sh), compose passes them to
# the service and its S3 backend activates; without them the service
# runs local-only and this deploy still succeeds.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP zillion ==="
docker compose up -d --build zillion

wait_code() {
  local url="$1" want="$2" attempts="${3:-100}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

echo "=== WAIT FOR zillion health (direct loopback :18126) ==="
code="$(wait_code http://127.0.0.1:18126/zillion/health 200)"
echo "CHECK zillion-health direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== STORAGE SMOKE (direct loopback: PUT -> SHA-256 verify -> GET -> DELETE) ==="
# Smoke timeouts are generous: with the S3 backend configured, a PUT
# includes STS AssumeRole plus S3 retries with backoff and can take
# ~30s when AWS is slow (measured 28.4s on 2026-11-18, when the old
# 15s PUT timeout made the first wiring deploy read an empty body).
printf 'zillion-smoke-payload' > /tmp/zillion-smoke.bin
want_hash="$(sha256sum /tmp/zillion-smoke.bin | cut -d' ' -f1)"
put="$(curl -s --max-time 120 -X PUT --data-binary @/tmp/zillion-smoke.bin http://127.0.0.1:18126/zillion/objects/smoke-test.bin || true)"
echo "PUT RESULT ${put}"
echo "${put}" | grep -q "\"sha256\": \"${want_hash}\""
echo "${put}" | grep -q '"verified": true'
got="$(curl -s --max-time 60 http://127.0.0.1:18126/zillion/objects/smoke-test.bin || true)"
[ "$got" = "zillion-smoke-payload" ]
ver="$(curl -s --max-time 120 -X POST http://127.0.0.1:18126/zillion/verify/smoke-test.bin || true)"
echo "VERIFY RESULT ${ver}"
echo "${ver}" | grep -q '"verified": true'
curl -s --max-time 60 -X DELETE http://127.0.0.1:18126/zillion/objects/smoke-test.bin > /dev/null || true
rm -f /tmp/zillion-smoke.bin
echo "CHECK zillion-storage-smoke ok"

echo "=== S3 BACKEND SMOKE (only when configured) ==="
health="$(curl -s --max-time 60 http://127.0.0.1:18126/zillion/health || true)"
echo "HEALTH ${health}"
if echo "${health}" | grep -q '"name": "aws-s3", "type": "s3", "bucket": "[^"]*", "configured": true'; then
  echo "CHECK zillion-s3 configured"
  # The smoke object above must show the S3 backend in the PUT
  # result's backends and in the verification phase.
  echo "${put}" | grep -q '"aws-s3": {"ok": true}'
  echo "${ver}" | grep -q '"aws-s3": {"ok": true, "result": "PASSED"'
  echo "CHECK zillion-s3-smoke ok"
else
  echo "CHECK zillion-s3 not configured — local-only mode, S3 smoke skipped"
fi

echo "=== GATEWAY FORCE-RECREATE (picks up /zillion/ location) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR gateway /zillion/health (public HTTPS) ==="
code="$(wait_code https://members.911transedu.net/zillion/health 200)"
echo "CHECK gateway zillion-health ${code} (want 200)"
[ "$code" = "200" ]

echo "=== REGRESSIONS ==="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/members/health || true)"
echo "CHECK members-health gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/portal/ || true)"
echo "CHECK portal gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/files/ || true)"
echo "CHECK files gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/ledger/health || true)"
echo "CHECK ledger gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/blackberry/health || true)"
echo "CHECK blackberry gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:80/health || true)"
echo "CHECK audited-app ${code} (want 200)"
[ "$code" = "200" ]

echo "=== MEMORY AFTER ==="
free -m
docker compose ps zillion
echo "DEPLOY ZILLION DONE"
