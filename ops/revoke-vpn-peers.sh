#!/usr/bin/env bash
# Revoke WireGuard VPN peers — keep ONLY the peers named on the
# command line (default: taylor) and remove every other peer from
# the live-vpn container's vpn-data volume. See
# services/vpn/README.md ("Revocation (2026-11-18)").
#
# Why this exists: on 2026-11-18 Taylor directed that the
# connection to Mom's phone (peer phone2) be taken out, that no
# other fleet phones be allowed access, and that the blocking be
# automated. phone3–phone5 were reserved peers whose configs were
# never distributed, but their keys still existed in the volume and
# would have connected if a config ever leaked. End state after a
# run with the default keep-list: peer `taylor` (10.8.0.2) is the
# only peer that exists.
#
# What it does, per removed peer:
#   - deletes /config/peer_<name>/ from the volume (via docker
#     exec, the same access pattern as ops/export-vpn-peer.sh);
#   - removes that peer's [Peer] block from
#     /config/wg_confs/wg0.conf, identified by the peer's public
#     key (read from the peer dir's publickey file) or, when the
#     peer dir is already gone, by its tunnel AllowedIPs from the
#     known fleet map below.
# Then it restarts the vpn service the way ops/deploy-vpn.sh
# brings it up (docker compose stop + up -d, then the same
# wait-for-wg0 loop) and prints masked `wg show` proof.
#
# IMPORTANT — PEERS in docker-compose.yml must match the keep-list
# or removed peers return on recreate: the linuxserver/wireguard
# image regenerates any peer named in the PEERS environment
# variable whose config directory is missing. The compose change
# (PEERS: taylor) rides along in the same rsync as this script;
# if you run this script by hand with a different keep-list, edit
# PEERS to the same list first. The post-restart check below fails
# loudly if a removed peer reappears, which is how a PEERS
# mismatch shows up.
#
# Log hygiene: this repo and its workflow logs are public. The
# script never prints a private key, a preshared key, or any peer
# .conf contents (wg0.conf itself contains the server's private
# key and is only ever held in a mktemp dir on the host, removed
# on exit). Every IPv4 address in the printed proof is masked as
# <ip masked>, per Taylor's 2026-11-18 direction (same rule as
# ops/verify-vpn.sh); peers are identified by name and public key.
#
# Usage: revoke-vpn-peers.sh [keep-peer ...]   (default: taylor)
# Idempotent: when only the keep-list peers remain, it changes
# nothing, skips the restart, prints the current peers, exits 0.
set -eu
cd /home/deploy/live-servers

KEEP=("$@")
if [ "${#KEEP[@]}" -eq 0 ]; then
  KEEP=(taylor)
fi
for name in "${KEEP[@]}"; do
  case "$name" in
    ''|*[!a-z0-9_-]*) echo "usage: revoke-vpn-peers.sh [keep-peer ...] (lowercase, digits, - _)" >&2; exit 2 ;;
  esac
done
KEEP_STR=" ${KEEP[*]} "

in_keep() {
  case "$KEEP_STR" in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

# Known fleet tunnel addresses (fallback block identification when
# a peer's config directory is already gone). Used only to MATCH
# [Peer] blocks inside wg0.conf — never printed.
fallback_ip() {
  case "$1" in
    taylor) echo "10.8.0.2/32" ;;
    phone2) echo "10.8.0.3/32" ;;
    phone3) echo "10.8.0.4/32" ;;
    phone4) echo "10.8.0.5/32" ;;
    phone5) echo "10.8.0.6/32" ;;
    *) echo "" ;;
  esac
}
FLEET="taylor phone2 phone3 phone4 phone5"

mask() {
  sed -E 's/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/<ip masked>/g'
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "=== VPN PEER REVOCATION ==="
echo "KEEP: ${KEEP[*]}"

echo "=== SANITY: vpn container up ==="
if ! docker exec live-vpn wg show wg0 >/dev/null 2>&1; then
  echo "FAIL: live-vpn is not running or wg0 is down — nothing changed" >&2
  docker compose ps vpn || true
  exit 1
fi

echo "=== ENUMERATE PEERS (config dirs in the volume) ==="
PRESENT="$(docker exec live-vpn ls /config | sed -n 's/^peer_//p' || true)"
echo "present: ${PRESENT:-none}"

# Candidates = peers with a config dir, plus every known fleet
# name (a [Peer] block can outlive its config dir).
REMOVE=()
for name in $PRESENT $FLEET; do
  in_keep "$name" && continue
  already=0
  for r in "${REMOVE[@]:-}"; do [ "$r" = "$name" ] && already=1; done
  [ "$already" = "1" ] || REMOVE+=("$name")
done

if [ "${#REMOVE[@]}" -eq 0 ]; then
  echo "=== NOTHING TO REVOKE — only the keep-list peers remain ==="
else
  echo "REVOKE: ${REMOVE[*]}"

  echo "=== COLLECT PUBLIC KEYS OF PEERS TO REMOVE ==="
  REMOVE_KEYS=""
  REMOVE_IPS=""
  for name in "${REMOVE[@]}"; do
    pub=""
    pubfile="$(docker exec live-vpn ls "/config/peer_${name}" 2>/dev/null | grep '^publickey' | head -n 1 || true)"
    if [ -n "$pubfile" ]; then
      pub="$(docker exec live-vpn cat "/config/peer_${name}/${pubfile}" | tr -d '[:space:]' || true)"
    fi
    if [ -n "$pub" ]; then
      REMOVE_KEYS="${REMOVE_KEYS} ${pub}"
      echo "${name}: identified by public key ${pub}"
    else
      echo "${name}: no public key file — block will be matched by its tunnel address"
    fi
    ip="$(fallback_ip "$name")"
    if [ -n "$ip" ]; then
      REMOVE_IPS="${REMOVE_IPS} ${ip}"
    elif [ -z "$pub" ]; then
      echo "WARN: ${name} is not in the known fleet map and has no public key file — its [Peer] block, if any, cannot be identified; its config dir is still deleted" >&2
    fi
  done

  echo "=== REMOVE [Peer] BLOCKS FROM wg0.conf ==="
  docker exec live-vpn cat /config/wg_confs/wg0.conf > "$TMP/wg0.orig"
  awk -v keys="$REMOVE_KEYS" -v ips="$REMOVE_IPS" '
    BEGIN { nk = split(keys, K, " "); ni = split(ips, I, " ") }
    function is_target(   j) {
      for (j = 1; j <= nk; j++) if (K[j] != "" && curkey == K[j]) return 1
      for (j = 1; j <= ni; j++) if (I[j] != "" && curip == I[j]) return 1
      return 0
    }
    function flush() {
      if (buf == "") return
      if (section == "peer" && is_target()) {
        if (curkey != "") printf "removed [Peer] block (public key %s)\n", curkey > "/dev/stderr"
        else printf "removed [Peer] block (matched by tunnel address)\n" > "/dev/stderr"
      } else {
        printf "%s", buf
      }
      buf = ""; curkey = ""; curip = ""
    }
    /^\[Peer\]/ { flush(); section = "peer"; buf = $0 "\n"; next }
    /^\[/       { flush(); section = "other"; buf = $0 "\n"; next }
    {
      buf = buf $0 "\n"
      if ($1 == "PublicKey") curkey = $3
      if ($1 == "AllowedIPs") curip = $3
    }
    END { flush() }
  ' "$TMP/wg0.orig" > "$TMP/wg0.new"

  # Safety checks before anything is written back: every removed
  # public key must be gone from the new file, and every kept peer
  # that had a block in the old file must still have it.
  for key in $REMOVE_KEYS; do
    if grep -Fq "$key" "$TMP/wg0.new"; then
      echo "FAIL: a removed peer's public key is still in the edited wg0.conf — nothing written back" >&2
      exit 1
    fi
  done
  for name in "${KEEP[@]}"; do
    pubfile="$(docker exec live-vpn ls "/config/peer_${name}" 2>/dev/null | grep '^publickey' | head -n 1 || true)"
    [ -n "$pubfile" ] || continue
    pub="$(docker exec live-vpn cat "/config/peer_${name}/${pubfile}" | tr -d '[:space:]' || true)"
    [ -n "$pub" ] || continue
    if grep -Fq "$pub" "$TMP/wg0.orig" && ! grep -Fq "$pub" "$TMP/wg0.new"; then
      echo "FAIL: kept peer ${name} would lose its [Peer] block — nothing written back" >&2
      exit 1
    fi
  done

  docker exec -i live-vpn sh -c 'cat > /config/wg_confs/wg0.conf.new' < "$TMP/wg0.new"
  docker exec live-vpn mv /config/wg_confs/wg0.conf.new /config/wg_confs/wg0.conf
  echo "wg0.conf updated"

  echo "=== DELETE PEER CONFIG DIRS ==="
  for name in "${REMOVE[@]}"; do
    docker exec live-vpn rm -rf "/config/peer_${name}"
    echo "deleted peer_${name}"
  done

  echo "=== RESTART vpn (deploy-vpn.sh pattern: stop, up -d, wait for wg0) ==="
  # stop + up -d (rather than a plain restart) so a container
  # created with a stale PEERS list is recreated from the current
  # docker-compose.yml.
  docker compose stop vpn
  docker compose up -d vpn
  ok=0
  i=0
  while [ "$i" -lt 60 ]; do
    if docker exec live-vpn wg show wg0 >/dev/null 2>&1; then
      ok=1; echo "wg0 up after ~$((i * 5))s"; break
    fi
    i=$((i + 1)); sleep 5
  done
  if [ "$ok" != "1" ]; then
    echo "FAIL: wg0 did not come up within ~300s"
    docker compose ps vpn || true
    echo "--- live-vpn log tail (key material and QR art filtered out) ---"
    docker logs --tail 60 live-vpn 2>&1 | grep -v -e '█' -e 'PrivateKey' -e 'PresharedKey' || true
    exit 1
  fi
fi

echo "=== WG SHOW AFTER (IP addresses masked) ==="
WG_OUT="$(docker exec live-vpn wg show)"
printf '%s\n' "$WG_OUT" | mask

echo "=== VERIFY REMOVED PEERS ARE GONE ==="
fail=0
for key in ${REMOVE_KEYS:-}; do
  if printf '%s\n' "$WG_OUT" | grep -Fq "$key"; then
    echo "FAIL: a removed peer (public key ${key}) is still in wg show — check that PEERS in docker-compose.yml matches the keep-list" >&2
    fail=1
  fi
done
if [ "${#REMOVE[@]:-0}" -gt 0 ]; then
  PRESENT_AFTER="$(docker exec live-vpn ls /config | sed -n 's/^peer_//p' | tr '\n' ' ' || true)"
  for name in "${REMOVE[@]}"; do
    case " $PRESENT_AFTER " in
      *" $name "*)
        echo "FAIL: config dir peer_${name} reappeared after restart — the image regenerated it, so PEERS in docker-compose.yml does not match the keep-list" >&2
        fail=1
        ;;
    esac
  done
fi
[ "$fail" = "0" ] || exit 1
echo "no removed peer present in wg show or in the volume"

REMAINING=""
for name in $(docker exec live-vpn ls /config | sed -n 's/^peer_//p' || true); do
  REMAINING="${REMAINING} ${name}"
done
echo "REMAINING PEERS:${REMAINING:- none}"
echo "=== REVOKE-VPN-PEERS DONE ==="
