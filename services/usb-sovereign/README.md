# USB-SOVEREIGN v1 — Local Full-Stack Build (Increments 1–2)

A runnable local build of the USB-SOVEREIGN serial bridge platform, adapted
from the Master Platform Specification in Google Drive's **“Cport to Coort.docx.”**

The platform is a dual-plane design: a **local data plane** doing byte-pure,
non-blocking forwarding between paired serial ports (COM1↔COM2, Sport↔Coort),
and a **cloud telemetry plane** wrapping every stream in v2.0 envelopes
(lowercase hex + SHA-256 + sequence + session ID) for MQTT → AWS IoT Core →
DynamoDB (`source#timestamp` keys), with 30-second batch archival to S3.
A **cellular failover gate** binds the ZILTB channel automatically whenever
the cellular path is silent for **> 5.0 seconds**, and stands it down when
cellular traffic returns.

**Increment 2** adds: the real hardware plane (socat PTY pairs — the
controller runs against actual serial endpoints), the live AWS sink classes
behind a config gate (disabled by default), a local ClickHouse-style
`serial_events` store with the protocol-signature registry, WebSocket push
for the dashboard, and a self-contained demo package (`demo/index.html`).

## Run it

Requires Python 3.12+ — **standard library only, nothing to install**.
The PTY mode additionally needs the system `socat` binary.

```bash
cd ~/workspace/builds/usb-sovereign
python3 backend/main.py                        # simulated plant (default)
USB_SOV_PORTS=pty python3 backend/main.py      # socat PTY hardware plane
```

In PTY mode, socat creates a linked PTY pair per interface under
`/tmp/usb-sovereign-pty/` (`<NAME>.ctl` for the controller, `<NAME>.dev`
for the device plant) — real serial endpoints with raw, echo-free,
byte-pure plumbing; physical hardware later opens in place of the `.ctl`
end with no controller changes.

Then open the monitoring dashboard:

    http://127.0.0.1:8765/

Stop with Ctrl+C — the controller performs the spec's graceful shutdown
(R21–R25): flush the S3 buffer, close ports, disconnect MQTT (socat
children are terminated and PTY links cleaned up).

Useful endpoints:

- `GET /api/status` — JSON snapshot (uplink path, silence timer, per-source
  bytes/frames/throughput, pair totals, sink counters, serial-events
  totals, PTY plant stats in pty mode, recent events)
- `GET /api/events` — recent failover/flush/shutdown events
- `GET /api/serial-events` — serial_events store stats + recent records
  (signature classification included)
- `GET /ws` — WebSocket push of the status snapshot (~2 Hz); the dashboard
  uses it when available and falls back to 1 s polling

Configuration (`backend/config.py`): environment > `USB_SOV_CONFIG` JSON
file (or `backend/config.local.json`) > defaults. Defaults keep everything
local and the AWS sinks **off**. Notable variables: `USB_SOV_PORTS`
(`sim`|`pty`), `USB_SOV_PTY_DIR`, `USB_SOV_STORE` (1/0),
`USB_SOV_S3_FLUSH_SECONDS` (default 30, per spec), `USB_SOV_HOST`,
`USB_SOV_PORT` (default 127.0.0.1:8765).

## What you will see

The device plant feeds all eleven interfaces. The cellular source follows
a repeating **14 s up / 12 s down** pattern, so roughly once every
26 seconds the cellular silence crosses the 5.0 s threshold, the dashboard's
active path flips to **ZILTB** (red), failover traffic flows, and when the
cellular pattern comes back the controller logs “Cellular connection
restored. Standing down ZilTB.” and the path flips back to **CELL**.
Every frame published is also appended to `data/serial_events.jsonl` with
its protocol signature (`41S0` sport_handshake, `41C1` coort_response,
`41L0` lan_ethernet_frame, …) per `data/signature_registry.json`.

## Demo package

`demo/index.html` is a **fully self-contained** static copy of the
dashboard (CSS/JS inlined, zero external references, no backend): an
in-page simulation replays the same failover scenario with the same
counters and event messages. It can be embedded as a section in a static
website as-is.

## Layout

```
backend/
  main.py              entry point — config-driven wiring (plant/sinks/store)
  config.py            env > JSON file > defaults; AWS gate defaults OFF
  controller.py        UnifiedSerialController adapted from the spec
  simulated_ports.py   pyserial-shaped simulated ports + device traffic
  pty_ports.py         socat PTY hardware plane (PtySerialPort + plant)
  sinks.py             TelemetrySink interface + envelope builder + stubs
  aws_sinks.py         spec AwsIoTClient + AwsS3BatchLogger — GATED OFF
  event_store.py       local serial_events JSONL store + signature registry
  server.py            asyncio HTTP + WebSocket server (API + dashboard)
frontend/
  index.html           dashboard page
  style.css            spec design tokens (Slate/White/Blue/Red)
  app.js               WebSocket push renderer (polling fallback)
demo/
  index.html           self-contained static demo (simulation in-page)
data/                  serial_events.jsonl + signature_registry.json (runtime)
logs/                  local S3-batch files land here at runtime
```

## What is stubbed (and where the real thing plugs in)

| Spec component | Here | Real implementation |
|---|---|---|
| Serial hardware | Increment 2: socat PTY pairs (`pty_ports.PtySerialPort`); `sim` mode keeps the scripted plant | Physical `/dev/ttyUSB*` / pyserial objects — the controller only uses `in_waiting` / `read` / `write` / `close` |
| AWS IoT Core MQTT | `MqttIoTStubSink` builds the exact v2.0 envelope locally; `aws_sinks.AwsIoTClient` (spec class) exists but is **gated off** | Enable via config only with endpoint + certs + an MQTT SDK; wiring validates and falls back to the stub with a logged reason |
| Lambda → DynamoDB | `DynamoDbStubSink`, in-memory, `source#timestamp` keys | IoT topic rule / Lambda into a real table |
| S3 batch archival | `S3BatchLoggerStub` writes locally; `aws_sinks.AwsS3BatchLogger` (boto3) exists but is **gated off** | Enable via config with boto3 + credentials |
| Cellular / ZilTB radios | Scripted 14 s up / 12 s down pattern (both planes) | Real CELL/ZILTB serial interfaces |
| ClickHouse analytics | Local JSONL `serial_events` store + JSON signature registry (`event_store.py`) | ClickHouse MergeTree + registry service; anomaly/forecast jobs |
| Dashboard transport | WebSocket push implemented (stdlib RFC 6455); auth hardening still noted as future work | Authenticated WS / the spec's FastAPI broadcaster |

No AWS resources are created or touched, nothing is deployed, and nothing
leaves this machine. See the goal's `files/Build_Brief.md` for the brief.
