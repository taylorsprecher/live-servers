#!/usr/bin/env bash
# Weekly renewal for ALL gateway Let's Encrypt certs — the nine
# subdomains in tls/lib.sh (members + the eight service subdomains
# added 2026-11-12). Installed in the deploy user's crontab by
# tls/setup.sh:
#   17 4 * * 1 cd /home/deploy/live-servers && bash tls/renew.sh >> tls-state/renew.log 2>&1
# Also runnable by hand via the tls.yml workflow (action=renew).
#
# Lego renews only when a cert is within 30 days of expiry; when it
# does, the TLS-ALPN-01 challenge is answered through the gateway's
# stream layer exactly as in tls/issue.sh. A subdomain with no cert in
# lego state yet gets one obtained here too (same 'run' semantics), so
# a subdomain whose DNS propagated after the rollout still lands its
# cert on the next weekly run even if tls/issue.sh is never re-run.
# Renewed/obtained certs are copied into their install dirs
# (tls/lib.sh: members keeps tls-state/certs/, the rest get
# tls-state/certs/<name>/) and the gateway is reloaded once at the end
# if anything changed. Exit code is nonzero only if a domain that HAS
# an installed cert fails its renewal check.
set -u
cd /home/deploy/live-servers
source tls/lib.sh
mkdir -p tls-state/lego tls-state/certs

echo "--- renew run $(date -u '+%Y-%m-%dT%H:%M:%SZ') ---"
# Ownership normalization (see tls/issue.sh): lego state must stay
# deploy-owned or this script cannot read the cert it just renewed.
docker run --rm --entrypoint chown \
  -v /home/deploy/live-servers/tls-state/lego:/lego-data \
  nginx:alpine -R "$(id -u):$(id -g)" /lego-data

CHANGED=0
FAIL=0
for name in $(tls_names); do
  fqdn="$(tls_fqdn "$name")"
  dir="$(tls_certdir "$name")"
  mkdir -p "$dir"
  had_cert=0
  if [ -f "$dir/fullchain.pem" ] && openssl x509 -in "$dir/fullchain.pem" -noout -issuer 2>/dev/null | grep -q "Let's Encrypt"; then
    had_cert=1
  fi
  # Lego v5 folded renewal into 'run' (get or renew): --renew-days 30
  # makes it renew only when <=30 days remain and no-op otherwise.
  docker run --rm --network host --user "$(id -u):$(id -g)" \
    -v /home/deploy/live-servers/tls-state/lego:/lego-data \
    goacme/lego:latest \
    run \
    --path /lego-data \
    --email taylorsprecher@gmail.com \
    --accept-tos \
    --tls --tls.address 127.0.0.1:18444 \
    --domains "$fqdn" \
    --renew-days 30
  RC=$?
  if [ $RC -ne 0 ]; then
    echo "RENEW CHECK FAILED for $fqdn rc=$RC"
    # A never-issued domain failing (DNS still pending) is noted but
    # does not fail the weekly run; an installed cert failing does.
    if [ "$had_cert" -eq 1 ]; then FAIL=1; else echo "($fqdn has no installed cert yet — pending, not a failure)"; fi
    continue
  fi
  SRC="tls-state/lego/certificates/$fqdn.crt"
  if [ -f "$SRC" ] && { [ ! -f "$dir/fullchain.pem" ] || [ "$SRC" -nt "$dir/fullchain.pem" ]; }; then
    cp "$SRC" "$dir/fullchain.pem"
    cp "tls-state/lego/certificates/$fqdn.key" "$dir/privkey.pem"
    chmod 600 "$dir/privkey.pem"
    CHANGED=1
    echo "cert for $fqdn renewed/obtained and installed:"
    openssl x509 -in "$dir/fullchain.pem" -noout -dates
  else
    echo "no renewal due for $fqdn — installed cert unchanged"
  fi
done

if [ "$CHANGED" -eq 1 ]; then
  docker exec live-gateway nginx -t && docker exec live-gateway nginx -s reload
  echo "gateway reloaded with renewed cert(s)"
fi
echo "--- renew run done (changed=$CHANGED fail=$FAIL) ---"
exit $FAIL
