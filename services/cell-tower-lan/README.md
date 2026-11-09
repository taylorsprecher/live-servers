# Cell Tower LAN Platform — increments 1 + 2

A runnable, local-only build of the Cell Tower LAN platform from Taylor's
Drive specs: an encrypted telemetry backend plus a live OAM-style
dashboard. **All RAN hardware is simulated — there are no real radios,
switches, or network interfaces involved.**

Increment 2 (2026-11-09) added: durable persistence, a real job queue
with retries and dead-lettering, encrypted-UDP ingest, per-site keys
with a generate/rotate/revoke lifecycle, alarm acknowledgement, and
dashboard depth (topology view, VLAN matrix, pipeline panel) — plus a
self-contained demo package under `demo/`.

## What's here

```
backend/
  server.js        Telemetry service (zero-dependency Node): site registry,
                   encrypted HTTP + UDP ingest, queue worker, read-back
                   APIs, key/queue admin, serves the frontend
  crypto.js        AES-256-GCM per the "Encrypt Cell Tower LAN" spec
  keys.js          Per-site keys + lifecycle (keys.json), decrypt key ring
  queue.js         Durable in-process queue with BullMQ semantics
                   (attempts, exponential backoff, dead-letter)
  store.js         Persistence: SQLite via node:sqlite, JSON-file fallback
  simulator.js     Demo-site telemetry producer (HTTP ingest, site key)
  simulator-udp.js Same producer over encrypted UDP datagrams
  readings.js      Shared simulated-reading generator + fault injection
  registry.json    Sites / sectors / radios as data
frontend/
  index.html       Dashboard (in the spirit of the Drive HTML prototype)
  app.js           Polling, metric cards, canvas charts, topology view,
                   VLAN matrix, alarm panel with ack workflow, pipeline
  style.css        Dark SITE-LAN OAM theme
demo/
  index.html       Fully self-contained demo (see demo/README.md)
```

## Run it

Requires Node 18+ (24 recommended for the built-in `node:sqlite`). No
`npm install` — no dependencies.

```bash
cd backend
node server.js          # telemetry service on http://localhost:4173,
                        # UDP ingest on udp://localhost:4174
node simulator.js       # second terminal: feeds the demo site over HTTP
node simulator-udp.js   # alternative: feeds it over encrypted UDP
```

Then open **http://localhost:4173** for the dashboard.

Optional environment:

- `PORT` — backend HTTP port (default `4173`).
- `CELL_LAN_UDP_PORT` — UDP ingest port (default `4174`).
- `DATA_DIR` — where `telemetry.db`, `keys.json` (and any fallback
  store) live (default `backend/data/`). Delete it to reset the demo.
- `CELL_LAN_MASTER_KEY` — 64-char hex (32-byte) AES-256 key used as the
  legacy shared fallback in the decrypt ring. If unset, a
  clearly-labelled local **test key** built into `crypto.js` is used.
  Producers normally encrypt with their site's own key (below).
- `CELL_LAN_QUEUE_BASE_MS` — retry backoff base in ms (default `2000`,
  per spec: retries after 2 s, 4 s). Shrink it for tests/demos.

## How it works

1. A simulator generates one telemetry reading per radio for the demo
   site **SITE-TX-9042 (Pine Ridge Macro)** every 2 s: RSRP, RSRQ, SINR,
   DL/UL throughput, connected UEs, TX power, temperature, VSWR.
2. Each reading is encrypted with the spec's scheme — AES-256-GCM,
   12-byte random nonce, AAD `"cell-tower-lan"`, wire format
   `Base64( IV || ciphertext || tag )` — under the **site's own active
   key** (provisioned into `keys.json` on first boot) and sent either to
   `POST /lan/telemetry/ingest-async` as `{ encryptedPayload, timestamp }`
   or as a raw encrypted UDP datagram.
3. Ingest persists a `process-telemetry` job on the
   `cell-tower-telemetry` queue and returns **202 immediately**. The
   worker then decrypts (trying the whole key ring: active + retired
   site keys, legacy shared key last), validates against the registry,
   stores the reading, and derives alarms (weak signal, low SINR,
   overtemp, VSWR, throughput collapse). Failures retry up to 3 attempts
   with exponential backoff and then land in the **dead-letter queue**
   (`GET /api/queue/dlq`) — a tampered payload is rejected there and
   never reaches the store.
4. Readings, alarms, and jobs **persist in SQLite** under `DATA_DIR`
   and survive restarts (history is bounded to the newest 300 readings
   per radio, 200 alarms). Mid-flight jobs are recovered on boot.
5. The dashboard reads back **decrypted** data via:
   - `GET /api/sites`, `GET /api/sites/:id`
   - `GET /api/telemetry/latest?site_id=`
   - `GET /api/telemetry/history?radio_id=&limit=`
   - `GET /api/alarms?site_id=&status=` (`status` = ACTIVE | ACKNOWLEDGED)
   - `POST /api/alarms/:id/ack` `{ by, note }` — acknowledge workflow
   - `GET /api/keys` — key metadata (ids, statuses, fingerprints; never
     key material)
   - `POST /api/keys/:siteId/generate | /rotate | /revoke` — lifecycle;
     rotation keeps the retired key decrypt-capable (grace) until revoke
   - `GET /api/queue/stats`, `GET /api/queue/dlq`
   - `GET /api/health`

The HTTP simulator injects faults so alarms are visible: radio
`RRU-TX9042-B-NR` degrades ~20 s of every 50 s (RSRP/SINR alarms), and
`RRU-TX9042-C-LTE` periodically overheats past 65 °C.

## Verified

Increment 2, 2026-11-09 (test instance on a scratch `DATA_DIR`):
simulator → encrypted ingest → queue → SQLite → read-back; **restart
persistence** (readings, queue records, DLQ contents, key states, and an
alarm acknowledgement all survived a server restart); **tampered
payload rejected** — 3 attempts, dead-lettered with the GCM auth error,
never stored; **UDP round-trip** for all three datagram forms (raw
packed bytes, Base64 text, JSON envelope) through the server's datagram
handler — note: this sandbox blocks real UDP sends (`send EPERM` even
on loopback), so the handler + full downstream pipeline were exercised
in-process and the socket bind confirmed from the server log; on a
normal host `simulator-udp.js` exercises the wire path directly;
**key lifecycle** — rotate kept old-key payloads decrypting (grace),
revoke moved them to the DLQ; **alarm ack** persisted across restart;
dashboard serves topology, VLAN matrix, and pipeline panels; demo
package smoke-tested headlessly (renders sites, topology, matrix,
pipeline, live values). Increment 1's round-trip and tamper checks were
verified 2026-11-09 as recorded in the Build Brief.

## What is stubbed

- **All radios, sectors, and the packet core are simulated data.** No
  Open5GS core (designed in the brief, deliberately not built), no
  gNodeB, no switch, no PTP/SyncE — the site LAN design from the spec
  docs is represented as registry data and dashboard views only.
- **The queue is in-process.** It reproduces BullMQ's semantics
  (attempts, backoff, DLQ, durability) on top of the local store, but
  there is no Redis and no multi-process worker scaling.
- **Key management is a local file.** `keys.json` stands in for an
  enrollment/KMS flow; simulators on the same host read it to encrypt,
  as a provisioned edge exporter would use its enrolled key. Key
  material is never exposed via the API (fingerprints only).
- The second registered site (OR-0117) receives no simulator feed by
  default, to show the dashboard's no-data state (it does have its own
  provisioned key, used by the lifecycle tests).
- No authentication on the read or admin APIs; local demo only. Do not
  expose this service beyond localhost as-is.
