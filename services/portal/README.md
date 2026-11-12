# Network Portal — 911transedu.net live-status dashboard

Deployed on the audited AWS host as part of the live-servers set, behind
the nginx gateway at **http://52.41.84.116:443/portal/** (host port
18111, loopback only; the gateway is the public entry point). Added
2026-11-12 at Taylor's direction ("Put it live"). No subdomain and no
DNS change: /portal/ is a path route in the shared gateway config, so —
like every shared route — it is also mirrored on the existing HTTPS
subdomains.

- Python 3 stdlib only (`http.server`). No pip installs, no build step.
- One page: `index.html` embeds its CSS and JS. Unlike the other static
  services it DOES make external requests by design: on load (and on
  the Refresh Status button) the page probes https://911transedu.net/
  and the eight live-server subdomains from the visitor's browser
  (`fetch` with `mode: 'no-cors'`) and colours a status dot per card.
- The page is deployed byte-for-byte as pasted. Verbatim source:
  Section 1 of
  `~/workspace/user/files/Other_AI_Upgraded_Portal_Configs_Docs_2026-11-12.md`
  (the "upgraded implementations" another AI produced and Taylor pasted
  on 2026-11-12). Sections 2 (Caddy/Nginx configs) and 3 (member
  documentation) of that paste were NOT applied — the host keeps its
  existing gateway, per-subdomain certs and member site.
- Stateless: no database, no volume, nothing is saved server-side.
- Health: `GET /portal/health` → `{"status": "ok"}`.
- Targeted deploy: `.github/workflows/deploy-portal.yml` rsyncs the
  repo and runs `ops/deploy-portal.sh`, which builds/recreates ONLY
  the portal container, then force-recreates the gateway so it loads
  the current locations-common.conf (the /portal route lives there).
