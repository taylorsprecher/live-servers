#!/usr/bin/env bash
# Export ONE WireGuard peer config from the running live-vpn
# container, encrypted for off-host delivery.
#
# Why: a peer .conf contains the phone's private key. This repo is
# public and its workflow logs are public, so the config must never
# appear in plaintext. This script encrypts on the host:
#   - a random 256-bit file key (hex) encrypts the config with
#     AES-256-CBC (openssl enc, salted, pbkdf2);
#   - the file key is wrapped with the RSA public key in
#     ops/vpn-export-pubkey.pem (OAEP padding);
#   - output = wrapped-key bytes || ciphertext, base64, printed
#     between BEGIN/END markers.
# The matching private key exists only in Jasmine's workspace
# (~/workspace/vpn-keys/) and is never in git, so the printed
# ciphertext is safe in a public log. See services/vpn/README.md.
#
# Usage: export-vpn-peer.sh <peer-name>   (taylor|phone2|phone3|...)
set -eu
PEER="${1:-}"
case "$PEER" in
  ''|*[!a-z0-9_-]*) echo "usage: export-vpn-peer.sh <peer-name> (lowercase, digits, - _)" >&2; exit 2 ;;
esac
PUBKEY="/home/deploy/live-servers/ops/vpn-export-pubkey.pem"
[ -f "$PUBKEY" ] || { echo "FAIL: public key not found at $PUBKEY" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

docker exec live-vpn cat "/config/peer_${PEER}/peer_${PEER}.conf" > "$TMP/peer.conf"
[ -s "$TMP/peer.conf" ] || { echo "FAIL: peer config for '$PEER' is empty or missing" >&2; exit 1; }

openssl rand -hex 32 > "$TMP/filekey.txt"
openssl enc -aes-256-cbc -salt -pbkdf2 \
  -in "$TMP/peer.conf" -out "$TMP/peer.enc" -pass file:"$TMP/filekey.txt"
openssl pkeyutl -encrypt -pubin -inkey "$PUBKEY" \
  -pkeyopt rsa_padding_mode:oaep \
  -in "$TMP/filekey.txt" -out "$TMP/key.enc"

echo "=== VPN PEER EXPORT (peer: ${PEER}) BEGIN ==="
cat "$TMP/key.enc" "$TMP/peer.enc" | base64 -w0
echo ""
echo "=== VPN PEER EXPORT END ==="
