# MacLAN — deployment notes

Single container: the FastAPI backend serves the API **and** the built
React frontend, so UI and API share one origin and one path prefix.

## Frontend build choice: prebuilt dist (no Node in the image)

The Dockerfile does **not** build the frontend. It copies a prebuilt
`frontend/dist` into the image, keeping the image small
(`python:3.12-slim`, no Node toolchain). Build the bundle first, with
the base path it will be served under:

```bash
cd frontend
npm install            # first time only
VITE_BASE_PATH=/maclan/ npm run build
cd ..
docker build -t maclan .
```

`VITE_BASE_PATH` (default `/`) becomes Vite's `base`: asset URLs in
`dist/index.html` and `import.meta.env.BASE_URL` in the code both follow
it, and every frontend API call is prefixed from that value. Rebuild the
bundle if the prefix ever changes.

## Run

```bash
docker run -p 8000:8000 maclan
# internal port 8000, start command:
#   uvicorn app.main:app --host 0.0.0.0 --port 8000   (WORKDIR /app/backend)
```

## Gateway contract

The deployment gateway forwards `http://<host>/maclan/*` to the
container **with the `/maclan` prefix still in the request path**. The
app is built for exactly that:

- `BASE_PATH=/maclan` (baked into the image as an ENV; code default is
  `""`). FastAPI receives it as `root_path`: routing matches routes
  without the prefix, generated URLs include it.
- Requests that must answer:
  - `GET /maclan/` → the UI (`index.html` from the static mount)
  - `GET /maclan/health` → `{"status":"ok", ..., "base_path":"/maclan"}`
  - `GET /maclan/api/network/status`, `/maclan/api/devices`, … → API
  - `GET /maclan/assets/*` → the built JS/CSS
- To run the same image at a domain root instead, start it with
  `-e BASE_PATH=` **and** a frontend bundle built without
  `VITE_BASE_PATH`.

## Environment variables

| Var             | Default (image) | Meaning                                             |
|-----------------|-----------------|-----------------------------------------------------|
| `BASE_PATH`     | `/maclan`       | Public prefix; `""` for root hosting.               |
| `MACLAN_DB_PATH`| `/app/backend/maclan.db` | SQLite file location (override to put it on a volume). |
| `FRONTEND_DIST` | `/app/frontend/dist` | Where the built frontend is served from.        |

## Runtime state

- The app creates and seeds **`maclan.db`** (SQLite) next to the backend
  on first start — devices, interfaces, telemetry, network config.
  State changes through the UI persist there. Mount a volume over it
  (or set `MACLAN_DB_PATH` into a mounted directory) to keep state
  across container replacement; delete the file to reset to seed data.
- No other files are written at runtime. No network egress is needed.
- All device/network state remains *simulated* (see README) — the
  container touches no real LAN/Bluetooth hardware.

## Local verification without Docker

```bash
cd frontend && VITE_BASE_PATH=/maclan/ npm run build && cd ..
cd backend
BASE_PATH=/maclan .venv/bin/uvicorn app.main:app --port 8000
# then: curl localhost:8000/maclan/ , /maclan/health ,
#       /maclan/api/network/status , /maclan/assets/<bundle>.js
```
