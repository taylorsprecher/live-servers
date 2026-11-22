# VPN DNS filter — bug blocking at the VPN server

Built 2026-11-22 at Taylor's direction ("Add blocking at my
VPN server"), following Taylor's rule for the member-site bug
guard: only allow real information to be seen.

## What it does

The WireGuard peers' DNS already terminates inside the `vpn`
container's network namespace (`PEERDNS=auto` points the
phones at the server, 10.8.0.1). This sidecar
(`network_mode: service:vpn`) REDIRECTs port-53 traffic from
the tunnel interface to a small Python DNS filter:

- A query for a blocklisted domain — advertising, tracking,
  telemetry; matched by suffix so subdomains count — is
  answered locally with 0.0.0.0 / :: and written to the
  report on the volume (`/data/dns_filter_report.log`,
  one line per blocked name per phone per minute at most).
- Every other query is forwarded untouched to the upstream
  resolvers (1.1.1.1, fallback 9.9.9.9) and the real answer
  relayed. Real information is never altered.
- Because the filter sits in the tunnel, it covers **every
  app on a connected phone at once** while the VPN is on —
  the phone-side equivalent of the member site's bug guard.

## Safety shape

- **Fail-closed arming:** the filter runs only when
  `VPN_DNS_FILTER_ENABLED=1` is in the host `.env`
  (`ops/set-vpn-dns-filter-enabled.sh`, set by the deploy-vpn
  workflow's `enable_dns_filter` input). Otherwise the
  container idles dark: no listeners, no rules.
- **Fail-open running:** a supervisor health-checks the
  filter every 10 s; after 3 failures it removes the
  redirect and phones fall back to the vpn container's own
  DNS, exactly as before the filter existed. Shutdown
  removes the redirect first.
- **No phone changes:** peer configs are untouched; Eva's
  and Jasper's phones get the same filtering when connected,
  as Taylor was told before approving.

## Stated limits

Plain DNS only. Apps carrying their own encrypted DNS
(DNS-over-HTTPS) bypass any port-53 filter; this one does
not try to intercept encrypted traffic. The seed list
(~45 domains in `vpn_dns_filter.py`) covers ads/trackers/
telemetry; add domains one per line in
`/data/dns_blocklist_extra.txt` on the vpn-dns-data volume
(read at container start).

## Verify

The deploy runs the filter's `--selftest` inside the
namespace: a tracker domain must sinkhole when the filter is
queried directly, both REDIRECT rules must be installed
(their packet counters are the live proof once a phone is
connected — a query generated inside the namespace never
crosses the wg0 ingress path, so it cannot test the redirect
end-to-end), and a real domain must resolve to a real
answer. The selftest's own direct queries appear in the
report as blocks from 127.0.0.1; phone blocks show the
phone's 10.8.0.x address.
