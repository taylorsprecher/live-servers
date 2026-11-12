# HTTPS for the 911transedu.net subdomains

members.911transedu.net was added 2026-11-11 at Taylor's direction
("Make it secure"): the member site takes passwords, so it needed real
HTTPS. On 2026-11-12 Taylor extended the same treatment to every live
server ("All on our own domain and subdomain"): maclan, xbox, wyze,
usb, aircore, fmp, celltower and specmatrix each got their own
subdomain + cert, with the IP paths left exactly as they were. DNS for
every name: `<name>.911transedu.net A 52.41.84.116` (GoDaddy). The
authoritative name/short-name/health-path table lives in `tls/lib.sh`.

## How it works

Public port 443 was already taken by the gateway's plain-HTTP service,
and the emailed department links (http://52.41.84.116:443/...) had to
keep working, so the gateway starts with an nginx **stream** layer on
:443 that sorts connections without altering their bytes:

| Traffic on :443 | Routed to |
| --- | --- |
| TLS, ALPN `acme-tls/1` (Let's Encrypt probes) | `127.0.0.1:18444` (lego, only during issuance/renewal) |
| TLS, SNI one of the nine `*.911transedu.net` names below | `127.0.0.1:18443` (HTTPS servers, one block per name) |
| Everything else — all plain HTTP included | `127.0.0.1:10443` (the original HTTP server) |

All HTTP/HTTPS servers share `gateway/locations-common.conf`, so each
subdomain mirrors the full gateway path layout: e.g.
`https://xbox.911transedu.net/xbox/ui/` serves exactly what
`http://52.41.84.116:443/xbox/ui/` serves. Each service subdomain's
root (`https://xbox.911transedu.net/`) additionally forwards to the
service's own entry path (`/xbox/ui/` for xbox, `/<name>/` for the
rest). The members server also includes its `/members/` proxy block;
`/members/` exists only on the members subdomain.

The only address change anywhere: on the HTTP server, `/members/`
**redirects** (301) to the same path on `https://members.911transedu.net`
(Taylor's direction, 2026-11-11: members-only; every other service
keeps its `http://52.41.84.116:443` address and does NOT redirect).
The audited app on :80 is a separate stack and untouched.

## Certificates

Let's Encrypt, issued by [lego](https://go-acme.github.io/lego/) via
**TLS-ALPN-01** — HTTP-01 is impossible (port 80 belongs to the audited
stack) and no GoDaddy API keys exist for DNS-01. Account:
taylorsprecher@gmail.com. State lives host-only in `tls-state/`
(rsync-excluded, gitignored):

- `tls-state/lego/` — ACME account + issued certs/keys (lego format,
  one `<fqdn>.crt`/`.key` pair per subdomain)
- `tls-state/certs/fullchain.pem`, `privkey.pem` — members' installed
  cert (its original 2026-11-11 location; unchanged)
- `tls-state/certs/<name>/fullchain.pem`, `privkey.pem` — installed
  cert for each service subdomain
- The whole `tls-state/certs/` dir is bind-mounted into the gateway at
  `/etc/nginx/certs`, so the per-name subdirs appear automatically.

Inventory (all Let's Encrypt, ~90-day certs):

| Subdomain | Installed at | First issued |
| --- | --- | --- |
| members.911transedu.net | `tls-state/certs/` | 2026-11-11 (expires 2027-02-09) |
| maclan.911transedu.net | `tls-state/certs/maclan/` | 2026-11-12 |
| xbox.911transedu.net | `tls-state/certs/xbox/` | 2026-11-12 |
| wyze.911transedu.net | `tls-state/certs/wyze/` | 2026-11-12 |
| usb.911transedu.net | `tls-state/certs/usb/` | 2026-11-12 |
| aircore.911transedu.net | `tls-state/certs/aircore/` | 2026-11-12 |
| fmp.911transedu.net | `tls-state/certs/fmp/` | 2026-11-12 |
| celltower.911transedu.net | `tls-state/certs/celltower/` | 2026-11-12 |
| specmatrix.911transedu.net | `tls-state/certs/specmatrix/` | 2026-11-12 |

## Renewal

`tls/renew.sh` runs weekly from the deploy user's crontab (Mon 04:17
UTC, installed by `tls/setup.sh`, log at `tls-state/renew.log`) and
covers **all nine subdomains**: for each it calls `lego run
--renew-days 30` (lego v5 folds renewal into `run`), which renews only
when <30 days remain, copies any renewed cert into its install dir,
and reloads the gateway once at the end. A subdomain that has no cert
yet gets one obtained by the same run, so late DNS propagation heals
itself on the next weekly pass. It can also be run any time via the
**TLS** workflow (`action=renew`).

## Operations (GitHub Actions → "TLS for 911transedu.net subdomains")

- `setup` — stage A: state dirs, self-signed placeholders, config
  test, gateway recreate, check battery, renewal crontab.
- `issue` — stage B: lego issuance for every subdomain that still
  needs a cert (already-valid certs are skipped; a subdomain whose
  DNS hasn't landed yet fails its own run and is reported pending —
  re-run `issue` once its A record resolves), cert install, gateway
  reload, full-chain verification.
- `apply` — gateway-only recreate + verification after config-only
  commits (skips deploy.sh's full rebuild/RAM gate on purpose).
- `renew` — run the renewal check now.

## Rollback

Revert the gateway commits (`gateway/nginx.conf` back to the single
`listen 443` server, drop the extra compose mounts), recreate the
gateway, done — the services never changed. `tls-state/` can be left in
place; nothing references it once the config is reverted. To roll back
only the 2026-11-12 subdomain extension, revert that commit instead:
the stream map and the service-subdomain server blocks go away, and
members HTTPS + the HTTP service carry on exactly as before.
