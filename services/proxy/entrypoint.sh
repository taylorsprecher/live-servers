#!/bin/sh
# Renders the shadowsocks-rust config from the environment and starts
# the server. The password arrives ONLY via the PROXY_SS_PASSWORD env
# var (docker compose interpolates it from the host .env — see
# ops/set-proxy-password.sh and .github/workflows/deploy-proxy.yml);
# it is never baked into the image and never committed to git. The
# password is hex, so it is safe to interpolate into JSON verbatim.
#
# Transport notes (verified against v2ray-plugin v1.5.0):
# - WebSocket path is /proxy; the gateway exposes exactly that path.
# - mux=0 is REQUIRED on the server: v1.5.0's server defaults to
#   expecting mux framing and drops unmuxed clients otherwise.
# - TCP only: plugins carry no UDP, and nothing here needs it.
set -eu

if [ -z "${PROXY_SS_PASSWORD:-}" ]; then
  echo "proxy: PROXY_SS_PASSWORD is not set — refusing to start" >&2
  exit 1
fi

umask 077
cat > /tmp/shadowsocks-rust-config.json <<EOF
{
  "servers": [
    {
      "server": "0.0.0.0",
      "server_port": 8388,
      "password": "${PROXY_SS_PASSWORD}",
      "method": "chacha20-ietf-poly1305",
      "plugin": "v2ray-plugin",
      "plugin_opts": "server;path=/proxy;mux=0;loglevel=none",
      "mode": "tcp_only"
    }
  ],
  "mode": "tcp_only"
}
EOF

exec ssserver --log-without-time -c /tmp/shadowsocks-rust-config.json
