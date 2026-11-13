#!/usr/bin/env bash
# Targeted deploy for the Files Library (fileslib) + the portal card.
#
# Stages the library's file bytes into the fileslib-data volume from
# /home/deploy/fileslib-data.tar (delivered by the deploy-files-library
# workflow over SSH — the bytes are never in git), then builds and
# recreates ONLY fileslib and portal, and force-recreates the gateway
# so it loads the /files route from locations-common.conf.
#
# Like the other targeted ops scripts, this does NOT use deploy.sh and
# does not touch its RAM gate; both image builds are trivial COPYs onto
# the already-pulled python:3.12-slim base. The archive is verified
# against FILESLIB_SHA256 before anything is staged, and deleted after
# staging. The tar contains ONLY non-held files, named by download id;
# held files' bytes are never in it (asserted again below by count).
set -eu
cd /home/deploy/live-servers

TAR=/home/deploy/fileslib-data.tar
EXPECTED_FILES=747

echo "=== MEMORY BEFORE ==="
free -m

echo "=== VERIFY ARCHIVE ==="
[ -f "${TAR}" ] || { echo "MISSING ${TAR}"; exit 1; }
[ -n "${FILESLIB_SHA256:-}" ] || { echo "FILESLIB_SHA256 not set"; exit 1; }
echo "${FILESLIB_SHA256}  ${TAR}" | sha256sum -c -

echo "=== STAGE BYTES INTO fileslib-data VOLUME ==="
docker volume create live-servers_fileslib-data >/dev/null
docker run --rm \
  -v live-servers_fileslib-data:/data \
  -v /home/deploy:/stage:ro \
  nginx:alpine sh -c '
    find /data -maxdepth 1 -name "f*" -delete
    tar -xf /stage/fileslib-data.tar -C /data
    count="$(ls /data | wc -l)"
    echo "STAGED FILES: ${count}"
    [ "${count}" = "747" ] || { echo "STAGED COUNT MISMATCH"; exit 1; }
  '
rm -f "${TAR}"
echo "archive removed after staging"

echo "=== BUILD + UP fileslib and portal only ==="
docker compose up -d --build fileslib portal

echo "=== GATEWAY FORCE-RECREATE (pick up current locations-common.conf) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR fileslib health (loopback) ==="
code="000"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18113/files/health)" || code="000"
  [ "${code}" = "200" ] && break
  sleep 4
done
echo "CHECK fileslib direct ${code}"
[ "${code}" = "200" ]

echo "=== GATEWAY CHECKS (through nginx on :443) ==="
for pair in \
  "files-page http://127.0.0.1:443/files/" \
  "files-health http://127.0.0.1:443/files/health" \
  "members http://127.0.0.1:443/members/health" \
  "portal http://127.0.0.1:443/portal/" \
  "syncflow http://127.0.0.1:443/syncflow/health" ; do
  name="${pair%% *}"; url="${pair#* }"
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${url}")" || c="000"
  echo "CHECK ${name} ${c}"
done

echo "=== SAMPLE DOWNLOAD (f0001, want 200 and 351767 bytes) ==="
curl -s -o /dev/null -w 'CHECK sample-download %{http_code} %{size_download}\n' --max-time 30 http://127.0.0.1:443/files/dl/f0001

echo "=== AUDITED APP (separate production stack — must be untouched) ==="
APPCODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)" || APPCODE="000"
echo "CHECK audited-app ${APPCODE}"

echo "=== MEMORY AFTER ==="
free -m
docker compose ps fileslib portal gateway
echo "=== DEPLOY-FILESLIB DONE ==="
