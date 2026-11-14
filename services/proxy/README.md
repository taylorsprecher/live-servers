# proxy — Taylor's personal Shadowsocks proxy

Added 2026-11-13 at Taylor's direction ("Build the proxy on my
server") so the Outlink app on Taylor's phone has a proxy endpoint of
Taylor's own to connect to. One user, one password, TCP only.

## What runs

- Container `live-proxy`, built from the official
  `ghcr.io/shadowsocks/ssserver-rust` image plus the official
  `v2ray-plugin` binary (v1.5.0, pinned) — upstream's own
  `docker/Dockerfile.v2ray` recipe.
- Shadowsocks method `chacha20-ietf-poly1305`, transport
  WebSocket-over-TLS via v2ray-plugin (server mode), path `/proxy`,
  `mux=0` on both ends (v1.5.0's server expects mux framing unless
  told otherwise — verified by test, not assumed).
- Published on the host at `127.0.0.1:18114` only, like every other
  service. Memory limit 64 MB; the pair idles at a few tens of MB.

## Public route — and why it is not proxy.911transedu.net

```
Outlink ──TLS──▶ 52.41.84.116:443 (gateway stream mux, SNI)
              ──▶ specmatrix.911transedu.net HTTPS vhost (127.0.0.1:18443)
              ──▶ location = /proxy (WebSocket upgrade)
              ──▶ 127.0.0.1:18114 ──▶ v2ray-plugin ──▶ ssserver
```

A dedicated `proxy.911transedu.net` subdomain was the first choice
and is **not currently possible from this pipeline**: the name does
not exist in DNS (NXDOMAIN, verified 2026-11-13), the zone lives at
GoDaddy and is edited by hand, and no GoDaddy API credentials exist
for the deploy pipeline (the same reason cert issuance uses
TLS-ALPN-01 — see tls/README.md). Without the A record there is no
Let's Encrypt cert and no resolvable name for clients.

So the WebSocket path rides the existing `specmatrix` demo vhost,
which already has a real subdomain, a real cert, and a stream-mux
SNI route — the `/proxy` location exists ONLY in that one server
block in `gateway/nginx.conf`, nowhere else. Everything else about
the gateway is byte-identical to before. If Taylor later adds the
`proxy.911transedu.net A 52.41.84.116` record at GoDaddy, moving the
endpoint to its own name is a small follow-up: add the name to the
tls/ table, issue its cert, clone the specmatrix vhost block with
the same `/proxy` location, and regenerate the client link with the
new host (the password and container do not change).

The audited production app and every other service are untouched:
the only shared file edited is `gateway/nginx.conf` (one added
location in one vhost), and the gateway is force-recreated by the
targeted deploy exactly as deploy.sh itself does.

## The password

- Generated once as 32 random bytes (hex). Stored ONLY as the
  GitHub Actions secret `PROXY_SS_PASSWORD` and in the host `.env`
  (0600), delivered by `.github/workflows/deploy-proxy.yml` piping it
  over SSH stdin into `ops/set-proxy-password.sh` — the same pattern
  as the members Stripe keys. Never in git, never in a log.
- The container receives it as the `PROXY_SS_PASSWORD` env var;
  `entrypoint.sh` renders the ssserver config from it at start and
  refuses to start if it is unset.
- The client share link (SIP002 `ss://` with the v2ray-plugin
  parameters) is generated outside this repo and handed to Taylor
  directly; it is never committed.
- **Rotation:** set a new `PROXY_SS_PASSWORD` secret, re-run the
  Deploy-proxy workflow, regenerate the client link. The old
  password stops working the moment the container is recreated.

## Operations

- Deploy: GitHub Actions → **Deploy proxy service only**
  (`ops/deploy-proxy.sh` — targeted; does not touch deploy.sh's
  RAM gate). It checks: WebSocket 101 direct on :18114 and through
  the gateway under the specmatrix SNI, the gateway regression
  battery, the audited app, and the container's memory footprint.
- Probe: GitHub Actions → **Verify proxy service on live endpoints**
  (public, read-only: 101 on upgrade, 400 on plain GET, the other
  sites still 200). A runner cannot do the full client test — it
  has no password by design.
- Full end-to-end (done at deploy time from the operator's
  machine): a stock Shadowsocks client (shadowsocks-rust `sslocal`
  and a sing-box outbound, the core Outlink is built on) with
  `plugin=v2ray-plugin;tls;host=specmatrix.911transedu.net;path=/proxy;mux=0`
  fetching an IP-echo page through the tunnel; the echo must return
  the host's own address, 52.41.84.116.
