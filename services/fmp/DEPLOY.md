# FMP-1.0 — deployment notes

The container runs the whole app (static frontend + REST API + SSE +
WebSocket feeds) in one zero-dependency Node process.

## Public route

- Public URL: `http://52.41.84.116:443/fmp/`
- The gateway forwards the `/fmp` prefix **unchanged** — requests arrive
  at the container *with* the prefix (`GET /fmp/api/replay`, …).
- Therefore the container **must** run with `BASE_PATH=/fmp`. The server
  strips the prefix before routing; the frontend derives the same prefix
  from `window.location`, so fetch / EventSource / WebSocket URLs line up.
- Gateway requirements: proxy WebSocket upgrades for
  `/fmp/api/events/ws` (pass `Upgrade` / `Connection` headers through)
  and don't buffer the SSE feed at `/fmp/api/events/stream`.

## Run

```sh
docker build -t fmp .
docker run -d --name fmp \
  -p 8787:8787 \
  -e BASE_PATH=/fmp \
  -v fmp-data:/app/backend/data \
  fmp
```

- Internal port: **8787** (`FMP_PORT` env can override; keep the gateway
  pointed at the same port).
- Start command: `node backend/server.js` (the image CMD).

## Writable data / motion-script saves

The motion-script editor's "Save as new version" writes new JSON objects
into the local store under `backend/data/<bucket>/motion-scripts/`. The
image pre-chowns that tree to the `node` user, so saves work out of the
box. Mount a volume at `/app/backend/data` (as above) if saved scripts
should survive container replacement; without a volume the saves are
ephemeral container-layer data — acceptable for this demo, but they are
lost on rebuild/recreate.

## Smoke checks (through the gateway)

```sh
curl http://52.41.84.116:443/fmp/api/health        # {"status":"ok","basePath":"/fmp",...}
curl http://52.41.84.116:443/fmp/                  # the UI (HTML)
curl 'http://52.41.84.116:443/fmp/api/replay'      # 32 normalized events
curl http://52.41.84.116:443/fmp/api/motion-script # current motion script
```

Root mode (no prefix) still works for local development: run without
`BASE_PATH` and use `http://localhost:8787/`.
