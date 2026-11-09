#!/usr/bin/env bash
# free-443.sh — remove the vestigial "443:443" port mapping from the audited
# stack's nginx container so the live-servers gateway (network_mode: host,
# restart: unless-stopped) can bind host :443 on its next restart attempt.
#
# Approved by Taylor 2026-11-09 ("Free up port 443 and finish it").
#
# Phases:
#   A  Pre-flight (read-only, gated). Any gate failure -> STOP, exit 1,
#      nothing changed.
#   B  Change: back up the audited compose, delete ONLY the nginx "443:443"
#      line, recreate ONLY the nginx service. If audited /health fails
#      afterwards, the backup is restored and nginx recreated again.
#   C  Gateway verification through http://127.0.0.1:443/<prefix>.
set -u

AUD_DIR=/opt/audited-stack
BAK=docker-compose.yml.bak-2026-11-09

code() { local c; c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null); printf '%s' "${c:-000}"; }

cd "$AUD_DIR" || { echo "STOP: cannot cd $AUD_DIR"; exit 1; }

echo "===== PHASE A: PRE-FLIGHT (read-only) ====="
echo "--- whoami / hostname ---"
whoami; hostname
echo "--- nginx service block in $AUD_DIR/docker-compose.yml ---"
sed -n '/^  nginx:/,/^secrets:/p' docker-compose.yml | sed '$d'
echo "--- grep nginx.conf for listen / ssl / 443 ---"
grep -nE 'listen|ssl|443' nginx.conf || echo "(no matches in nginx.conf)"
echo "--- grep docker-compose.yml for 443 ---"
grep -n '443' docker-compose.yml || echo "(no 443 in docker-compose.yml)"

# GATE 1: no genuine TLS/443 listener in the audited nginx config.
if grep -qE 'listen[^;]*443|ssl_certificate' nginx.conf; then
  echo "STOP (GATE 1): audited nginx.conf contains a real 443/TLS listener — NOT proceeding."
  exit 1
fi
echo "GATE 1 OK: no listen-443 / ssl_certificate in audited nginx.conf"

# GATE 2: the "443:443" mapping exists exactly once.
N=$(grep -c '"443:443"' docker-compose.yml)
echo "occurrences of \"443:443\" in docker-compose.yml: $N"
if [ "$N" != "1" ]; then
  echo "STOP (GATE 2): expected exactly 1 occurrence, found $N — NOT proceeding."
  exit 1
fi
echo "GATE 2 OK"

# GATE 3: deploy user can write the compose file and its directory
# (needed for the in-place sed edit and the backup copy).
if [ -w docker-compose.yml ] && [ -w . ]; then
  echo "GATE 3 OK: docker-compose.yml and $AUD_DIR are writable by $(whoami)"
else
  echo "STOP (GATE 3): deploy user cannot write $AUD_DIR/docker-compose.yml — NOT improvising with sudo."
  echo "file-writable=$(test -w docker-compose.yml && echo yes || echo no) dir-writable=$(test -w . && echo yes || echo no)"
  ls -la docker-compose.yml; ls -ld .
  exit 1
fi

# Mirror the audited pipeline's compose invocation (it uses --env-file .env.deploy).
ENVARG=""
if [ -f .env.deploy ]; then
  ENVARG="--env-file .env.deploy"
  echo ".env.deploy present — compose commands will use --env-file .env.deploy"
else
  echo "NOTE: .env.deploy not present — compose commands will run without --env-file"
fi

# GATE 4: audited app healthy BEFORE any change.
H0=$(code http://127.0.0.1:80/health); L0=$(code http://127.0.0.1:80/login)
echo "AUDITED BEFORE: health=$H0 login=$L0"
if [ "$H0" != "200" ]; then
  echo "STOP (GATE 4): audited /health is $H0 before any change — NOT touching production."
  exit 1
fi
echo "GATE 4 OK: audited app healthy before the change"
echo "===== PRE-FLIGHT PASSED ====="

echo "===== PHASE B: REMOVE THE 443 MAPPING (nginx only) ====="
cp docker-compose.yml "$BAK" && echo "backup written: $AUD_DIR/$BAK"

sed -i '/^  nginx:/,/^secrets:/{/"443:443"/d}' docker-compose.yml
echo "--- diff backup -> edited (expected: exactly one removed line) ---"
diff "$BAK" docker-compose.yml || true
REMOVED=$(diff "$BAK" docker-compose.yml | grep -c '^< ' || true)
ADDED=$(diff "$BAK" docker-compose.yml | grep -c '^> ' || true)
echo "diff summary: removed=$REMOVED added=$ADDED"
if [ "$REMOVED" != "1" ] || [ "$ADDED" != "0" ] || grep -q '"443:443"' docker-compose.yml || ! grep -q '"80:80"' docker-compose.yml; then
  echo "STOP: edit did not produce exactly the expected single-line change — restoring backup, no container touched."
  cp "$BAK" docker-compose.yml
  exit 1
fi
echo "EDIT OK: only the \"443:443\" line was removed; \"80:80\" intact"

restore_and_recreate() {
  echo "RESTORING backup compose and recreating nginx..."
  cp "$BAK" docker-compose.yml
  docker compose $ENVARG up -d nginx
  sleep 15
  echo "AUDITED AFTER RESTORE: health=$(code http://127.0.0.1:80/health) login=$(code http://127.0.0.1:80/login)"
}

echo "--- recreating ONLY the nginx service ---"
docker compose $ENVARG up -d nginx
RC=$?
echo "docker compose up -d nginx exit code: $RC"
if [ "$RC" != "0" ]; then
  echo "FAIL: compose up returned $RC"
  restore_and_recreate
  exit 1
fi

sleep 15
H1=$(code http://127.0.0.1:80/health); L1=$(code http://127.0.0.1:80/login)
echo "AUDITED AFTER: health=$H1 login=$L1"
echo "--- audited containers ---"
docker ps --format '{{.Names}} {{.Status}} {{.Ports}}' | grep audited || docker ps
if [ "$H1" != "200" ]; then
  echo "FAIL: audited /health is $H1 after the change — restoring."
  restore_and_recreate
  exit 1
fi
echo "===== PHASE B OK: audited app healthy, nginx no longer publishes 443 ====="

echo "===== PHASE C: GATEWAY VERIFICATION ====="
echo "--- polling for live-gateway to bind :443 (up to 180s) ---"
GW=""
for i in $(seq 1 36); do
  ST=$(docker inspect -f '{{.State.Status}}' live-gateway 2>/dev/null || echo missing)
  C=$(code http://127.0.0.1:443/maclan/health)
  echo "poll $i: live-gateway state=$ST  GET /maclan/health via :443 -> $C"
  if [ "$C" = "200" ]; then GW=up; break; fi
  sleep 5
done
echo "--- live + audited containers ---"
docker ps --format '{{.Names}} {{.Status}} {{.Ports}}' | grep -E 'live|audited' || docker ps
if [ "$GW" != "up" ]; then
  echo "GATEWAY NOT UP after polling — recent live-gateway logs:"
  docker logs --tail 20 live-gateway 2>&1 || true
  exit 1
fi
echo "GATEWAY UP"

check() { echo "CHECK $1 $(code "$2")"; }
check maclan    http://127.0.0.1:443/maclan/health
check xbox      http://127.0.0.1:443/xbox/health
check wyze      http://127.0.0.1:443/wyze/health
check usb       http://127.0.0.1:443/usb/api/status
check aircore   http://127.0.0.1:443/aircore/api/state
check fmp       http://127.0.0.1:443/fmp/api/health
check celltower http://127.0.0.1:443/celltower/api/health
echo "CHECK maclan-ui $(code http://127.0.0.1:443/maclan/)"
echo "CHECK xbox-ui $(code http://127.0.0.1:443/xbox/ui/)"

echo "--- final docker ps ---"
docker ps
echo "--- final free -m ---"
free -m
echo "===== free-443.sh DONE ====="
