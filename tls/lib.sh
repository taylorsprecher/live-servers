# Shared subdomain table for the gateway TLS setup (sourced by
# tls/setup.sh, tls/issue.sh, tls/renew.sh, tls/apply-gateway.sh).
#
# members.911transedu.net was the first HTTPS subdomain (2026-11-11) and
# keeps its original cert install location — tls-state/certs/ itself
# (fullchain.pem / privkey.pem at the top level). The eight service
# subdomains added 2026-11-12 install per-name:
# tls-state/certs/<name>/fullchain.pem + privkey.pem. All of it is
# host-only state under tls-state/ (gitignored, rsync-excluded).

# All subdomain short names, members first.
tls_names() {
  echo "members maclan xbox wyze usb aircore fmp celltower specmatrix"
}

# The service subdomains added 2026-11-12 (everything except members).
tls_new_names() {
  echo "maclan xbox wyze usb aircore fmp celltower specmatrix"
}

tls_fqdn() { # $1 = short name
  echo "$1.911transedu.net"
}

tls_certdir() { # $1 = short name; echoes the cert install dir
  if [ "$1" = "members" ]; then
    echo "tls-state/certs"
  else
    echo "tls-state/certs/$1"
  fi
}

# Gateway health path per service (matches the apply-gateway.sh /
# deploy.sh check batteries exactly).
tls_health_path() { # $1 = short name
  case "$1" in
    usb)       echo "/usb/api/status" ;;
    aircore)   echo "/aircore/api/state" ;;
    fmp)       echo "/fmp/api/health" ;;
    celltower) echo "/celltower/api/health" ;;
    *)         echo "/$1/health" ;;
  esac
}

# Succeeds if the cert installed for $1 is a real Let's Encrypt cert
# with more than 30 days of validity left.
tls_cert_fresh() { # $1 = short name
  local dir cert
  dir="$(tls_certdir "$1")"
  cert="$dir/fullchain.pem"
  [ -f "$cert" ] || return 1
  openssl x509 -in "$cert" -noout -issuer 2>/dev/null | grep -q "Let's Encrypt" || return 1
  openssl x509 -in "$cert" -noout -checkend $((30 * 86400)) >/dev/null 2>&1
}
