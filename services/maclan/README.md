# MacLAN — local build (Increment 2 — Phase 1 complete)

MacLAN is Taylor's LAN/Bluetooth network-management app, specced in the
Drive document **MacLAN1ZilTB.docx** (10-phase roadmap). This repo is the
runnable Phase 1 build: **React + TypeScript front end + the local FastAPI
back end** (the "1ZilTBCloud" shape from Phase 4, run locally with SQLite).

```
maclan/
├── frontend/   React + TypeScript + Vite — macOS Network Settings-style UI
├── backend/    FastAPI — devices, network status/config, telemetry (SQLite)
└── demo/       Self-contained static demo (single index.html, no backend)
```

## Run it

Backend (terminal 1):

```bash
cd backend
python3 -m venv .venv          # first time only
.venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/uvicorn app.main:app --port 8000
```

Frontend (terminal 2):

```bash
cd frontend
npm install   # first time only
npm run dev
```

Open **http://localhost:5173** (use `localhost`, not `127.0.0.1` — Vite
binds the IPv6 loopback by default). The Vite dev server proxies `/api`
and `/health` to the backend on :8000. A production bundle can be built
with `npm run build` (output in `frontend/dist/`).

## What's in this build

**Frontend (Phase 1, TypeScript)**
- Sidebar navigation modeled on the spec's prototype: Overview, TCP/IP,
  Devices, Ping Tester, Bandwidth Meter, Telemetry.
- Overview: status cards (primary interface, IP, router, TCP/IP mode,
  cable state, devices) and the interface table (USB Ethernet, Bluetooth
  PAN, Wi-Fi, Thunderbolt Bridge) — Ethernet/Bluetooth rows are derived
  live from the simulated cable and radio state.
- **TCP/IP panel:** DHCP / manual configuration with validation, DHCP
  lease renewal, an effective-addressing readout, and a **cable
  plug/unplug simulation** that drives the whole app's network state.
- Devices: LAN & Bluetooth device table seeded from the spec (1ZilTB
  Phone Connection, AirPods Max, Keychron K2, MX Master 3S + router /
  Apple TV / host), with pair → connect → disconnect / unpair actions,
  plus **Bluetooth radio controls** (power, discoverability, local name).
  Turning the radio off drops connected devices to "paired" and blocks
  pairing, like real macOS.
- Ping Tester: terminal-style simulated ICMP output.
- **Bandwidth Meter:** live canvas graph streaming 1 sample/sec from the
  backend's telemetry SSE endpoint (seeded from persisted history).
- Telemetry: live view of events in the backend, plus a test-event sender;
  the UI posts a heartbeat on load.
- TypeScript throughout (`npm run typecheck` runs `tsc --noEmit`, strict).

**Backend**
- `GET /health`
- `GET /api/network/status` — interfaces + summary (config-aware)
- `GET /api/network/config`, `PUT /api/network/config` — TCP/IP mode +
  manual addressing (persisted in a `network_config` table)
- `POST /api/network/cable` — plug/unplug the simulated Ethernet cable
- `POST /api/network/dhcp/renew` — simulated DHCP lease renewal
- `PUT /api/bluetooth` — radio power / discoverability / name
- `GET /api/devices`, `POST /api/devices/{id}/{pair|connect|disconnect|unpair}`
- `POST /api/telemetry`, `GET /api/telemetry` (optional `?metric=` filter)
- `GET /api/telemetry/stream` — SSE bandwidth samples (1/sec, persisted)
- `POST /api/diagnostics/ping` — simulated ping
- SQLite storage in `backend/maclan.db` (created and seeded on first run;
  delete the file to reset).

**Demo (`demo/index.html`)**
- One static file, no backend and no network calls: the Phase 1 UI with
  the same simulated state machine in the browser (devices, TCP/IP +
  cable, Bluetooth radio, ping, animated bandwidth graph). Open it
  directly or drop it into any static site as a section.

## What is stubbed / simulated

- **All device and network state is simulated.** No real Bluetooth, Wi-Fi,
  or Ethernet hardware is read or controlled; device actions, cable state,
  DHCP leases, and ping results are synthetic state in SQLite. Bandwidth
  samples come from a random-walk simulator on the backend.
- **SQLite stands in for PostgreSQL** (spec Phase 4). Table shapes mirror
  the planned schema so the swap is mechanical.
- **No auth yet** — spec Phase 4/5 adds accounts, JWT, and encryption
  (Secure Enclave / AES-GCM are native-phase items).
- **Phase 2 diagnostics not started** — HCI console, packet sniffer, and
  RF analyzer come after this Phase 1 completion, per the spec sequence.

## Roadmap (from the spec)

Phase 2 Bluetooth diagnostics (HCI console, packet sniffer, RF analyzer,
collision detector) · Phase 3 native SwiftUI app + MacLANBluetoothKit ·
Phase 4 1ZilTBCloud (FastAPI + PostgreSQL + auth) · Phase 5 security &
encryption · Phase 6 AWS telemetry (Cognito/Lambda/DynamoDB/Kinesis) ·
Phase 7 AI anomaly detection · Phase 8 spatial audio / LE Audio ·
Phase 9 Logic Pro Mode · Phase 10 production/demo layer.
