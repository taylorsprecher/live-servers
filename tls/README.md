# HTTPS for members.911transedu.net

Added 2026-11-11 at Taylor's direction ("Make it secure"): the member
site takes passwords, so it needed real HTTPS. DNS:
`members.911transedu.net A 52.41.84.116` (GoDaddy).

## How it works

Public port 443 was already taken by the gateway's plain-HTTP service,
and the emailed department links (http://52.41.84.116:443/...) had to
keep working, so the gateway now starts with an nginx **stream** layer
on :443 that sorts connections without altering their bytes:

| Traffic on :443 | Routed to |
| --- | --- |
| TLS, ALPN `acme-tls/1` (Let's Encrypt probes) | `127.0.0.1:18444` (lego, only during issuance/renewal) |
| TLS, SNI `members.911transedu.net` | `127.0.0.1:18443` (HTTPS server) |
| Everything else — all plain HTTP included | `127.0.0.1:10443` (the original HTTP server) |

Both HTTP servers share `gateway/locations-common.conf`. The only
address change anywhere: on the HTTP server, `/members/` **redirects**
(301) to the same path on `https://members.911transedu.net` (Taylor:
members-only; every other service keeps its `http://52.41.84.116:443`
address). The audited app on :80 is a separate stack and untouched.

## Certificate

Let's Encrypt, issued by [lego](https://go-acme.github.io/lego/) via
**TLS-ALPN-01** — HTTP-01 is impossible (port 80 belongs to the audited
stack) and no GoDaddy API keys exist for DNS-01. Account:
taylorsprecher@gmail.com. State lives host-only in `tls-state/`
(rsync-excluded, gitignored):

- `tls-state/lego/` — ACME account + issued cert/key (lego format)
- `tls-state/certs/fullchain.pem`, `privkey.pem` — what nginx loads
  (bind-mounted into the gateway at `/etc/nginx/certs`)

## Renewal

`tls/renew.sh` runs weekly from the deploy user's crontab
(Mon 04:17 UTC, installed by `tls/setup.sh`, log at
`tls-state/renew.log`); lego renews when <30 days remain and the script
reloads the gateway after installing a fresh cert. It can also be run
any time via the **TLS** workflow (`action=renew`).

## Operations (GitHub Actions → "TLS for members.911transedu.net")

- `setup` — stage A: state dirs, self-signed placeholder, config test,
  gateway recreate, check battery, renewal crontab.
- `issue` — stage B: lego issuance, cert install, gateway reload,
  full-chain verification.
- `apply` — gateway-only recreate + verification after config-only
  commits (skips deploy.sh's full rebuild/RAM gate on purpose).
- `renew` — run the renewal check now.

## Rollback

Revert the gateway commits (`gateway/nginx.conf` back to the single
`listen 443` server, drop the extra compose mounts), recreate the
gateway, done — the services never changed. `tls-state/` can be left in
place; nothing references it once the config is reverted.
