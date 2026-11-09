# USB-SOVEREIGN — deploy notes (live demo container)

## Image

```sh
docker build -t usb-sovereign .
docker run -d --name usb-sovereign -p 8765:8765 usb-sovereign
```

- Base image `python:3.12-slim`; the app is standard-library only, so the
  build has no dependency step.
- Start command: `python3 backend/main.py` (the image `CMD`).
- Internal port: **8765** (override with env `USB_SOV_PORT`; the bind
  address is `USB_SOV_HOST`, preset to `0.0.0.0` in the image).

## Gateway contract

- The app is mounted under the **`/usb`** prefix (env `BASE_PATH=/usb`,
  preset in the image). The gateway must forward requests **with** the
  prefix — the server strips it before routing and 404s anything outside
  it. Public shape: `http://52.41.84.116:443/usb/` (the `:443` is the
  gateway's side; the container itself speaks plain HTTP on 8765).
- The gateway must proxy the **WebSocket upgrade** for `/usb/ws` — the
  dashboard's live push runs over it (HTTP polling of
  `/usb/api/status` is the built-in fallback if it can't).
- Set `BASE_PATH=` (empty) to host the app at the root instead.

## Runtime behavior

- Plant mode is **sim** (`USB_SOV_PORTS=sim`): the simulated serial plant
  runs the spec's cellular pattern (14 s up / 12 s down), so the 5.0 s
  silence failover to ZilTB engages and stands down every cycle, exactly
  as locally. No socat/PTY plane in the container.
- **Live AWS sinks stay DISABLED.** Nothing sets `USB_SOV_AWS_IOT` /
  `USB_SOV_AWS_S3`; MQTT/DynamoDB/S3 are the local stubs.

## Files the app writes (all safe ephemeral)

| Path in container | What | Persistence |
|---|---|---|
| `/app/data/serial_events.jsonl` | local serial_events store (append-only) | Lost on container recreate — fine for the demo. Mount a volume at `/app/data` only if the event history should survive. |
| `/app/data/signature_registry.json` | protocol-signature registry, rewritten by the store | Same as above. |
| `/app/logs/serial_batch_<ts>.txt` | S3-stub batch flushes (~every 30 s) | Purely local stand-ins for S3 objects; safe to discard. |

No other writes; no volumes are required to run.
