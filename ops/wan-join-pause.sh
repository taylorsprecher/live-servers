#!/usr/bin/env bash
# Pause celltower and cport, join the audited host to Taylor's
# Tailscale tailnet, then restart the paused containers.
# Taylor approved this sequence on 2026-11-14 after the read-only
# probe showed 578-598 MB available RAM, below the join's 700 MB
# safety gate: live-celltower holds ~114 MB and live-cport ~67 MB.
# Both are paused unconditionally, at Taylor's direction.
#
# Companion to .github/workflows/wan-join-pause.yml, which copies
# this script and ops/join-tailnet.sh to the host and pipes the
# TAILSCALE_AUTH_KEY=value line in over SSH stdin — never a command
# line argument, never printed, never logged.
#
# The containers are restarted on EVERY exit path (an EXIT trap
# covers failures and interruptions), and again explicitly at the
# end. `docker start` on a container that was never stopped prints
# a visible "already running" error and changes nothing.
#
# The join itself is delegated to join-tailnet.sh mode "join",
# which enforces its own gates (passwordless sudo, >= 700 MB
# available after the pause) and refuses to change anything if
# they fail. This script changes nothing before the key check.
set -u

# --- receive the auth key on stdin (TAILSCALE_AUTH_KEY=value) ---
key=""
while IFS='=' read -r k v; do
  case "${k}" in
    TAILSCALE_AUTH_KEY) key="${v}" ;;
  esac
done
key="$(printf '%s' "${key}" | tr -d '[:space:]')"
if [ -z "${key}" ]; then
  echo "ERROR: TAILSCALE_AUTH_KEY is required on stdin — nothing was stopped or changed." >&2
  exit 1
fi

restart_containers() {
  echo "=== restarting containers ==="
  docker start live-celltower || true
  docker start live-cport || true
}
trap restart_containers EXIT

avail_mb() {
  free -m | awk '/^Mem:/ {print $7}'
}

echo "=== memory before pause ==="
free -m

echo "=== stopping live-celltower ==="
docker stop live-celltower
sleep 2
echo "=== memory after stopping live-celltower ==="
free -m

echo "=== stopping live-cport (Taylor directed both paused) ==="
docker stop live-cport
sleep 2
echo "=== memory after stopping live-cport ==="
free -m
echo "Available RAM now: $(avail_mb) MB (join gate: >= 700 MB)"

echo "=== running the join (join-tailnet.sh join) ==="
printf 'TAILSCALE_AUTH_KEY=%s\n' "${key}" | bash /home/deploy/join-tailnet.sh join
JOIN_RC=$?
unset key
echo "join-tailnet.sh join exit code: ${JOIN_RC}"

restart_containers
trap - EXIT

echo "=== docker ps (final state) ==="
docker ps --format 'table {{.Names}}\t{{.Status}}'
echo "=== tailscale status (final state) ==="
tailscale status || true
echo "PAUSE-JOIN DONE (join exit code ${JOIN_RC}). If device approval is pending, approve 'audited-host' in the Tailscale admin console, then dispatch join-tailnet verify mode."
exit "${JOIN_RC}"
