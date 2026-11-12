#!/usr/bin/env bash
# Stage B: obtain real Let's Encrypt certs via TLS-ALPN-01 and install
# them into the gateway, one per subdomain (see tls/lib.sh for the
# list). Originally members-only (2026-11-11); extended 2026-11-12 to
# all nine subdomains when Taylor put every live server on its own
# subdomain. Usage:
#   bash tls/issue.sh              # every subdomain that still needs one
#   bash tls/issue.sh xbox maclan  # only these short names
#
# Why TLS-ALPN-01: HTTP-01 needs port 80, which belongs to the audited
# production stack (untouchable), and there are no GoDaddy API keys on
# file for DNS-01. The gateway's stream layer routes ALPN "acme-tls/1"
# on :443 to 127.0.0.1:18444, where each lego run listens (host network).
#
# Per-domain behavior: a domain whose installed cert is already a real
# Let's Encrypt cert with >30 days left is SKIPPED (keeps us well clear
# of Let's Encrypt duplicate-cert rate limits). A domain whose DNS does
# not point at this host yet FAILS its lego run and is reported as
# pending — the others still proceed. The gateway is reloaded once at
# the end if any cert was installed. Exit code is nonzero if any
# requested domain failed.
set -u
cd /home/deploy/live-servers
source tls/lib.sh
mkdir -p tls-state/lego tls-state/certs
chmod 700 tls-state/lego

if [ $# -gt 0 ]; then NAMES="$*"; else NAMES="$(tls_names)"; fi
echo "=== LEGO ISSUE (TLS-ALPN-01 via :443 stream -> 127.0.0.1:18444) ==="
echo "domains: $NAMES"
ls -ld tls-state/lego tls-state/certs
# Ownership: lego's image runs as root, so its state files come out
# root-owned inside the deploy-owned (mode 700) tls-state/lego dir and
# the deploy user cannot even read them afterwards. Normalize with a
# throwaway root container, then run lego AS the deploy user.
docker run --rm --entrypoint chown \
  -v /home/deploy/live-servers/tls-state/lego:/lego-data \
  nginx:alpine -R "$(id -u):$(id -g)" /lego-data
# NOTE: the mount target is /lego-data, not /lego — in the goacme/lego
# image /lego is the binary itself, so a directory cannot mount there.
# Lego v5: all account/challenge flags belong to the 'run' subcommand
# ("get or renew"); there is no separate global flag set or renew cmd.

FAILED=""
INSTALLED=0
for name in $NAMES; do
  fqdn="$(tls_fqdn "$name")"
  dir="$(tls_certdir "$name")"
  mkdir -p "$dir"
  echo "--- $fqdn ---"
  if tls_cert_fresh "$name"; then
    echo "SKIP $fqdn — installed cert is a valid Let's Encrypt cert (>30 days left)"
    continue
  fi
  docker run --rm --network host --user "$(id -u):$(id -g)" \
    -v /home/deploy/live-servers/tls-state/lego:/lego-data \
    goacme/lego:latest \
    run \
    --path /lego-data \
    --email taylorsprecher@gmail.com \
    --accept-tos \
    --tls --tls.address 127.0.0.1:18444 \
    --domains "$fqdn"
  RC=$?
  if [ $RC -ne 0 ]; then
    echo "ISSUE FAILED for $fqdn rc=$RC (DNS not pointing here yet? — reported pending)"
    FAILED="$FAILED $name"
    continue
  fi
  SRC=tls-state/lego/certificates
  if [ ! -f "$SRC/$fqdn.crt" ]; then
    echo "LEGO reported success but $SRC/$fqdn.crt is missing — $fqdn failed"
    FAILED="$FAILED $name"
    continue
  fi
  echo "INSTALL CERT $fqdn -> $dir/"
  cp "$SRC/$fqdn.crt" "$dir/fullchain.pem"
  cp "$SRC/$fqdn.key" "$dir/privkey.pem"
  chmod 600 "$dir/privkey.pem"
  openssl x509 -in "$dir/fullchain.pem" -noout -issuer -subject -dates
  INSTALLED=$((INSTALLED + 1))
done

if [ "$INSTALLED" -gt 0 ]; then
  echo "=== GATEWAY RELOAD (cert swap, no restart) ==="
  docker exec live-gateway nginx -t && docker exec live-gateway nginx -s reload
  echo "reload exit code: $?"
  sleep 2
fi

echo "=== VERIFY (loopback, FULL chain validation, no -k) ==="
for name in $NAMES; do
  fqdn="$(tls_fqdn "$name")"
  if tls_cert_fresh "$name"; then
    curl -s --resolve "$fqdn:443:127.0.0.1" \
      "https://$fqdn$(tls_health_path "$name")" \
      -o /dev/null -w "https $fqdn$(tls_health_path "$name") %{http_code}\n" --max-time 15
  else
    echo "https $fqdn — no valid cert installed (pending)"
  fi
done
echo "--- chain as presented (members) ---"
echo | openssl s_client -connect 127.0.0.1:443 -servername members.911transedu.net 2>/dev/null \
  | grep -E '^(issuer|subject|  [0-9] s:|  [0-9] i:|Verify return code)' | head -12

echo "=== HTTP SERVICE STILL INTACT ==="
curl -s -o /dev/null -w 'http :443 /members/health %{http_code}\n'   --max-time 5 http://127.0.0.1:443/members/health
curl -s -o /dev/null -w 'http :443 /specmatrix/health %{http_code}\n' --max-time 5 http://127.0.0.1:443/specmatrix/health
curl -s -o /dev/null -w 'audited :80 /health %{http_code}\n'          --max-time 5 http://127.0.0.1:80/health

if [ -n "$FAILED" ]; then
  echo "=== ISSUE DONE WITH FAILURES (pending DNS?):$FAILED ==="
  exit 1
fi
echo "=== ISSUE DONE (installed $INSTALLED new cert(s)) ==="
