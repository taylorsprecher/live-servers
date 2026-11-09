#!/bin/sh
# Container entrypoint — Cell Tower LAN telemetry demo.
#
# Launches the telemetry server and the HTTP simulator (the UDP
# simulator is intentionally NOT run in the container — the UDP
# transport path is unverified in container networking; see DEPLOY.md).
# The simulator posts to the server over loopback WITH the mount prefix
# (http://127.0.0.1:$PORT$BASE_PATH) so ingest lands on the prefixed
# spec route /lan/telemetry/ingest-async, exactly as external traffic
# arrives through the gateway.
set -u
cd "$(dirname "$0")"

PORT="${PORT:-4173}"
BASE_PATH="${BASE_PATH:-}"
export DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

node backend/server.js &
SERVER_PID=$!

# Wait until the API answers (which also guarantees keys.json has been
# provisioned into DATA_DIR) before starting the producer.
i=0
while [ "$i" -lt 50 ]; do
  if node -e "fetch('http://127.0.0.1:${PORT}${BASE_PATH}/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" 2>/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 0.2
done

node backend/simulator.js "http://127.0.0.1:${PORT}${BASE_PATH}" &
SIM_PID=$!

trap 'kill $SERVER_PID $SIM_PID 2>/dev/null' TERM INT
wait $SERVER_PID
kill $SIM_PID 2>/dev/null
