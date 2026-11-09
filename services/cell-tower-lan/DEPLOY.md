# DEPLOY — Cell Tower LAN telemetry demo container

How this app is packaged for live hosting, and what the deploy step
must know. (Local development is covered in `README.md`.)

## Shape

- **One container** runs both processes via `start.sh`:
  1. `backend/server.js` — telemetry service: dashboard, read APIs,
     encrypted ingest (HTTP + queue worker), per-site keys, SQLite store.
  2. `backend/simulator.js` — HTTP telemetry producer for the demo site,
     so the live dashboard always has fresh readings. It posts to the
     server over loopback **with the prefix**
     (`http://127.0.0.1:4173/celltower/lan/telemetry/ingest-async`).
- **Internal port: `4173` (HTTP).** Nothing else needs publishing.
- **Public path:** `http://52.41.84.116:443/celltower/` — the gateway
  forwards requests **with the `/celltower` prefix intact**, and the
  service is configured with `BASE_PATH=/celltower` (the Dockerfile
  default). All routes — dashboard, `/api/*`, and the spec ingest route
  `/lan/telemetry/ingest-async` — answer under the prefix; unprefixed
  paths return 404, and a bare `/celltower` 301-redirects to
  `/celltower/` so the dashboard's relative assets resolve. The
  dashboard derives its API base from its own URL, so it works under
  any prefix (or at root with `BASE_PATH=`).

## Build & run

```bash
docker build -t cell-tower-lan .
docker run -d --name celltower \
  -p 4173:4173 \
  -e CELL_LAN_MASTER_KEY=<64-hex-demo-key> \
  -v celltower-data:/data \
  cell-tower-lan
curl http://localhost:4173/celltower/api/health   # -> status ok
```

Health check for the deploy step: `GET /celltower/api/health` returns
200 with `storage: "sqlite"`, queue stats, and readings count.

## Environment

| Var | Container default | Notes |
|---|---|---|
| `BASE_PATH` | `/celltower` | Mount prefix; must match the gateway path. Set empty to serve at root. |
| `PORT` | `4173` | Internal HTTP port. |
| `DATA_DIR` | `/data` | Holds `telemetry.db` + `keys.json`. Must be container-writable. |
| `CELL_LAN_MASTER_KEY` | *(unset)* | **Set at deploy time — see Keys.** Never commit a real value. |
| `CELL_LAN_UDP_PORT` | `4174` | UDP ingest bind inside the container (not published — see UDP caveat). |
| `CELL_LAN_QUEUE_BASE_MS` | `2000` | Retry backoff base for the ingest queue. |

## Keys — read before deploying

- `CELL_LAN_MASTER_KEY` is the legacy/shared fallback key (AES-256-GCM,
  32 bytes as 64 hex chars). **A fresh demo key must be generated and
  set at deploy time** (`openssl rand -hex 32`). Do not commit it, and
  do not reuse the source-tree test key anywhere real: if the env var
  is unset the code falls back to a clearly-labelled local TEST key so
  the demo runs out of the box — acceptable for a throwaway local run,
  not for the hosted one.
- Day-to-day telemetry uses **per-site keys**: on first boot the
  service provisions one AES-256 key per site into
  `$DATA_DIR/keys.json` and the simulator encrypts with its site's
  active key. Key material is never exposed via the API (fingerprints
  only). Losing `keys.json` while keeping old encrypted data is not an
  issue here — payloads are consumed by the queue within seconds, not
  archived — but a wiped `keys.json` simply re-provisions fresh keys.

## Persistence

- Readings (newest 300/radio), alarms + acknowledgements, and queue
  jobs/DLQ persist in `$DATA_DIR/telemetry.db` (SQLite via Node 24's
  built-in `node:sqlite`; a JSON-file store with the same API is the
  automatic fallback, and `/api/health` reports which is active as
  `storage`).
- **Ephemeral data is acceptable for this demo.** Mount a volume at
  `/data` (as in the run example) if restart continuity is wanted;
  without one, a container replacement resets history, alarm acks, and
  per-site keys — the dashboard repopulates within seconds of the
  simulator's first ticks.

## UDP caveat

The spec defines an encrypted-UDP ingest transport and the server does
bind a UDP listener (4174) in-process. However:

- the **UDP simulator is not run in the container**, and
- the **UDP path is unverified in sandbox/container networking** (in
  the dev sandbox, loopback UDP sends were blocked at OS level —
  `send EPERM` — so the wire hop could never be exercised there, even
  though the datagram handler itself is unit-verified).

**Do not publish UDP 4174** and do not treat UDP ingest as live. All
telemetry in the hosted demo flows over the verified HTTP ingest path.

## Base image note

`node:24-slim` is deliberate. `node:sqlite` doesn't exist in Node 20
and is experimental in Node 22; the build was developed and verified
on Node v24.22.1. Downgrading the base image would silently drop the
service to the JSON-file store.
