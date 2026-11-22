#!/usr/bin/env bash
# Arming step for the VPN DNS bug filter (Taylor, 2026-11-22),
# run on the host by the deploy-vpn workflow when its
# enable_dns_filter input is true. Reads ONE line from stdin:
#   VPN_DNS_FILTER_ENABLED=1
# and writes it into the host .env (creating the file if
# needed, replacing any existing line for that exact key, and
# leaving every other line byte-identical).
#
# Why stdin: the value never appears in the workflow YAML, the
# git history, or a process command line. The value is parsed
# by exact key prefix, never by splitting on '=' — a value
# ending in '=' (base64 padding) must survive intact (lesson
# from the SecureStore key delivery, 2026-11-21).
#
# The value itself is never echoed. The script prints only the
# key name and the resulting state.
set -eu
cd /home/deploy/live-servers

KEY="VPN_DNS_FILTER_ENABLED"
ENV_FILE=".env"

line=""
IFS= read -r line || true
case "$line" in
  "${KEY}="*) ;;
  *) echo "FAIL: expected one stdin line starting with ${KEY}=" >&2; exit 1 ;;
esac
value="${line#${KEY}=}"
if [ -z "$value" ]; then
  echo "FAIL: empty value for ${KEY}" >&2; exit 1
fi

touch "$ENV_FILE"
tmp="$(mktemp)"
grep -v "^${KEY}=" "$ENV_FILE" > "$tmp" || true
printf '%s=%s\n' "$KEY" "$value" >> "$tmp"
mv "$tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "SET ${KEY} (value not shown; ${#value} chars)"
