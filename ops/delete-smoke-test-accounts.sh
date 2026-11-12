#!/usr/bin/env bash
# Host-side runner for the one-off smoke-test account deletion.
#
# The member site's SQLite DB lives on the members-data volume, visible
# inside the live-members container at /data/members.db. This runner
# feeds ops/delete-smoke-test-accounts.py to the container's own
# python3 over stdin (docker exec -i), so the deletion runs against the
# live DB file with the app's own runtime. The app itself is not
# restarted or redeployed: it holds no in-memory member cache (every
# request queries SQLite fresh), so the committed deletion takes
# effect on the next request.
set -eu
SCRIPT="/home/deploy/delete-smoke-test-accounts.py"

if [ ! -f "${SCRIPT}" ]; then
  echo "Missing ${SCRIPT} — the workflow copies it here first."
  exit 1
fi

docker exec -i live-members python3 - < "${SCRIPT}"
