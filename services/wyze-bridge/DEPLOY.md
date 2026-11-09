# DEPLOY — status/dashboard demo tier

`Dockerfile.demo` builds ONE container: the FastAPI status service
(`backend/main.py`) also serving the dashboard (`frontend/`) as static
files. This is the unit hosted publicly at
`http://52.41.84.116:443/wyze/` (gateway forwards the `/wyze` prefix;
requests arrive WITH the prefix, and the app strips it before routing —
see `BASE_PATH` below).

**Scope: status/dashboard tier only, mocked data only.** Camera state is
the service's built-in mock, seeded with known facts about the Sterner
Home cameras and labeled `"source": "mock"` in every response (Family
shows live view failing, error codes -40/-15, exactly as observed). The
actual Docker Wyze Bridge is NOT in this image and stays undeployed — it
needs a Docker host on the home LAN (`docker-compose.yml` remains the
path for that; nothing here changes it).

## Build & run

```sh
docker build -f Dockerfile.demo -t wyze-bridge-status-demo .
docker run -p 8000:8000 wyze-bridge-status-demo
```

- Internal port: **8000** (uvicorn, `backend.main:app`).
- Start command: `uvicorn backend.main:app --host 0.0.0.0 --port 8000`
  (the image's CMD; no other processes).

## Configuration (environment)

| Variable         | Image default    | Meaning                                             |
|------------------|------------------|-----------------------------------------------------|
| `BASE_PATH`      | `/wyze`          | Hosting prefix; set as FastAPI `root_path`, stripped before routing. `-e BASE_PATH=` serves at root. |
| `ALERTS_DB_PATH` | `/tmp/alerts.db` | SQLite alert history (ephemeral in the demo container). |

## Smoke checks (through the gateway prefix)

- `GET /wyze/` → dashboard HTML
- `GET /wyze/health` → `{"status": "ok", "data_source": "mock", ...}`
- `GET /wyze/api/cameras` → 4 cameras, `source: "mock"`, Family `live_view: "failing"`
