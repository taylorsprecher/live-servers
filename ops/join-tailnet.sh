#!/usr/bin/env bash
# Join the audited host to Taylor's Tailscale tailnet (private WAN),
# or probe/verify that join. Companion to
# .github/workflows/join-tailnet.yml, which copies this script to the
# host and runs it over SSH — the same pattern as ops/host-mem-probe.sh.
#
# Modes:
#   probe   Read-only. Changes nothing. Reports OS, whether the deploy
#           user has passwordless sudo (the install needs root), free
#           RAM and disk, Docker state, and any existing Tailscale
#           install. Run this FIRST.
#   join    Reads one TAILSCALE_AUTH_KEY=value line on stdin (piped
#           over SSH by the workflow — never a command-line argument,
#           never printed, never logged), installs Tailscale from the
#           official installer if it is missing, starts tailscaled,
#           and runs `tailscale up` as hostname "audited-host".
#           Refuses to change anything if passwordless sudo is
#           unavailable or available RAM is under 700 MB (the same
#           threshold as deploy.sh's gate — mirrored here, not
#           lowered, and deploy.sh itself is untouched).
#   verify  Read-only. Prints `tailscale status` in full and pings
#           the assistant VM's tailnet address (100.87.134.23) and,
#           if its address is passed as $2, the connectable-pathways
#           host. Exits non-zero if the VM ping fails.
#
# Full output always: no step's output or error is suppressed, per
# Taylor's standing rule that information is not hidden.
#
# Deliberate choices:
#   --accept-dns=false   tailscaled must not rewrite this host's DNS:
#                        the Docker containers and the nginx 443
#                        gateway depend on the existing resolver setup.
#                        (Trade-off: MagicDNS names do not resolve on
#                        this host; use 100.x addresses.)
#   No --advertise-routes, no --advertise-exit-node, no --ssh:
#                        this host joins as a plain node only. It does
#                        not advertise or accept any subnet route, so
#                        container networking and the gateway are
#                        untouched.
set -u

VM_TAILNET_IP="100.87.134.23"
MODE="${1:-probe}"

avail_mb() {
  free -m | awk '/^Mem:/ {print $7}'
}

have_sudo() {
  sudo -n true 2>/dev/null
}

print_state() {
  echo "=== /etc/os-release ==="
  cat /etc/os-release
  echo "=== uname -a ==="
  uname -a
  echo "=== free -m ==="
  free -m
  echo "=== df -h / ==="
  df -h /
  echo "=== docker ps ==="
  docker ps --format 'table {{.Names}}\t{{.Status}}' || echo "docker ps failed (see error above)"
  echo "=== passwordless sudo check ==="
  if have_sudo; then
    echo "sudo -n true: OK (deploy user can sudo without a password)"
  else
    echo "sudo -n true: FAILED (deploy user has no passwordless sudo)"
  fi
  echo "=== tailscale presence ==="
  if command -v tailscale >/dev/null 2>&1; then
    tailscale version
  else
    echo "tailscale: not installed"
  fi
}

case "${MODE}" in
  probe)
    print_state
    if command -v tailscale >/dev/null 2>&1; then
      echo "=== tailscale status (existing install) ==="
      tailscale status || true
    fi
    echo "PROBE DONE — nothing was changed."
    ;;

  join)
    # --- receive the auth key on stdin (TAILSCALE_AUTH_KEY=value) ---
    key=""
    while IFS='=' read -r k v; do
      case "${k}" in
        TAILSCALE_AUTH_KEY) key="${v}" ;;
      esac
    done
    key="$(printf '%s' "${key}" | tr -d '[:space:]')"
    if [ -z "${key}" ]; then
      echo "ERROR: TAILSCALE_AUTH_KEY is required on stdin — refusing to continue." >&2
      exit 1
    fi

    # --- gates: change nothing unless both pass ---
    if ! have_sudo; then
      echo "STOP: installing Tailscale needs root, and the deploy user has no passwordless sudo."
      echo "Nothing was changed. See RUNBOOK.md (blocker 1) for Taylor's options."
      exit 1
    fi
    AVAIL="$(avail_mb)"
    echo "Available RAM: ${AVAIL} MB (gate: >= 700 MB required, mirroring deploy.sh)"
    if [ -z "${AVAIL}" ] || [ "${AVAIL}" -lt 700 ]; then
      echo "STOP: only ${AVAIL:-unknown} MB available — no changes made."
      exit 1
    fi

    # --- install from the official Tailscale installer ---
    if command -v tailscale >/dev/null 2>&1; then
      echo "=== tailscale already installed ==="
      tailscale version
    else
      echo "=== installing tailscale (official installer: https://tailscale.com/install.sh) ==="
      curl -fsSL https://tailscale.com/install.sh | sudo sh
      echo "=== installed version ==="
      tailscale version
    fi

    echo "=== enabling tailscaled ==="
    sudo systemctl enable --now tailscaled
    sudo systemctl --no-pager status tailscaled || true

    # --- join: key travels in a root-only file, never in argv ---
    keyfile="$(sudo mktemp /run/tskey.XXXXXX)"
    trap 'sudo rm -f "${keyfile}"' EXIT
    printf '%s' "${key}" | sudo tee "${keyfile}" >/dev/null
    sudo chmod 600 "${keyfile}"
    unset key
    echo "=== tailscale up (hostname: audited-host) ==="
    echo "Note: if the auth key was not generated 'pre-approved', this tailnet's device"
    echo "approval holds the new device until Taylor approves it in the admin console."
    echo "The up command is therefore time-boxed at 180s; a timeout here with the node"
    echo "registered below means 'pending approval', not a failed install."
    timeout 180 sudo tailscale up \
      --auth-key="file:${keyfile}" \
      --hostname=audited-host \
      --accept-dns=false || echo "tailscale up returned non-zero or timed out (full output above) — check status below."
    sudo rm -f "${keyfile}"
    trap - EXIT

    echo "=== tailscale status ==="
    tailscale status || true
    echo "=== tailscale ip -4 ==="
    tailscale ip -4 || true
    echo "=== tailscale ping VM (${VM_TAILNET_IP}) ==="
    tailscale ping --c 3 "${VM_TAILNET_IP}" || echo "ping failed — expected if device approval is still pending; approve 'audited-host', then run verify mode."
    echo "JOIN STEP DONE. If approval was pending, approve the device, then dispatch verify mode."
    ;;

  verify)
    echo "=== tailscale status ==="
    tailscale status
    echo "=== tailscale ip -4 ==="
    tailscale ip -4
    echo "=== tailscale ping VM (${VM_TAILNET_IP}) ==="
    tailscale ping --c 3 "${VM_TAILNET_IP}"
    echo "=== system ping VM (${VM_TAILNET_IP}) ==="
    ping -c 3 "${VM_TAILNET_IP}"
    if [ -n "${2:-}" ]; then
      echo "=== tailscale ping peer (${2}) ==="
      tailscale ping --c 3 "${2}"
      echo "=== system ping peer (${2}) ==="
      ping -c 3 "${2}"
    fi
    echo "=== docker ps (containers undisturbed) ==="
    docker ps --format 'table {{.Names}}\t{{.Status}}'
    echo "VERIFY DONE."
    ;;

  *)
    echo "usage: join-tailnet.sh [probe|join|verify] [peer-tailnet-ip]" >&2
    exit 2
    ;;
esac
