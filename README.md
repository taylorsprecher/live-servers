# live-servers

Seven demo servers deployed on Taylor's AWS host (52.41.84.116) behind a
single nginx gateway on port 443, routed by path prefix:

| Prefix       | Service              | Loopback port |
|--------------|----------------------|---------------|
| /maclan/     | MacLAN               | 127.0.0.1:18101 |
| /xbox/       | Xbox Cloud Saves     | 127.0.0.1:18102 |
| /wyze/       | Wyze Bridge status   | 127.0.0.1:18103 |
| /usb/        | USB-SOVEREIGN        | 127.0.0.1:18104 |
| /aircore/    | AirCore              | 127.0.0.1:18105 |
| /fmp/        | FMP-1.0              | 127.0.0.1:18106 |
| /celltower/  | Cell Tower LAN       | 127.0.0.1:18107 |

Deploy is manual-dispatch only: the GitHub Actions workflow rsyncs this
repo to `/opt/live-servers` on the host and runs `deploy.sh`, which
enforces a RAM gate (>= 700 MB available) before building, generates the
host `.env` secrets on first run (never committed), brings the compose
project up, and verifies every service through the gateway plus the
untouched audited production app on port 80.

Demo secrets (Xbox XSTS signing key / dev secret, Cell Tower master key)
are generated on the host at deploy time and live only in
`/opt/live-servers/.env` (chmod 600).
