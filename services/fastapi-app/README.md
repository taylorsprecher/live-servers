# FastAPI App

A minimal FastAPI service scaffold for the fleet, created
2026-11-18 at Taylor's direction ("Start a new service with it")
around the Dockerfile Taylor uploaded that day.

**The name is a placeholder.** `fastapi-app` is what the scaffold is
called until the service grows a real purpose and a real name; the
rename touches this directory, the compose service, the gateway
route, the Apps Hub tile, and the deploy pair.

## The Dockerfile is Taylor's file, verbatim

`services/fastapi-app/Dockerfile` is a byte-identical copy of the
Dockerfile Taylor uploaded on 2026-11-18 (`Dockerfile.dat`, 385
bytes): `python:3.12-slim`, `COPY requirements.txt` + `pip install`,
`COPY . .`, non-root `appuser`, `EXPOSE 8000`, and
`CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`.
Do not edit it casually — if the container definition needs to
change, that is a deliberate change to Taylor's file, made openly.

Because the Dockerfile copies the whole build context and runs
`app.main:app`, the context is this directory itself:

- `Dockerfile` — Taylor's, verbatim (above).
- `requirements.txt` — pinned `fastapi` and `uvicorn[standard]`
  (versions verified as current stable on PyPI on 2026-11-18).
- `app/__init__.py` — empty package marker.
- `app/main.py` — the application (below).

## What runs

A small FastAPI app with two endpoints, no auth, and no secrets —
there is nothing here worth protecting yet:

- `GET /health` — `{"status": "ok", "service": "fastapi-app"}`;
  the liveness probe for the deploy smoke checks and the Apps Hub
  tile's status dot.
- `GET /` — service name, purpose line, and uptime in seconds.

## Fleet wiring

- Compose service `fastapi-app` (container `live-fastapi-app`,
  build `./services/fastapi-app`, mem 128m, loopback
  `127.0.0.1:18127:8000`, restart unless-stopped).
- Gateway location `/fastapi/` in `gateway/locations-common.conf`.
- Targeted deploy: `ops/deploy-fastapi-app.sh` +
  `.github/workflows/deploy-fastapi-app.yml` (builds only this
  service, smoke-tests health on loopback and through the gateway,
  force-recreates the gateway, runs the standard regressions).
  No secrets are needed by this service and none are delivered.
- Apps Hub tile: "FastAPI App" → `members.911transedu.net/fastapi/`
  (status dot checks `/fastapi/health`).

### Gateway prefix choice: strip, don't forward

Most fleet services route under their own prefix (the gateway
forwards `/zillion/` intact and the service owns `BASE_PATH`). This
app is different: Taylor's Dockerfile runs plain
`uvicorn app.main:app` with no `--root-path`, and the app serves at
the container root (`/health`, `/`). So the gateway follows the
**C-Port precedent** documented in `locations-common.conf` —
`proxy_pass` carries a URI part and nginx strips the prefix:
`/fastapi/health` → `/health`, `/fastapi/` → `/`. The app stays
mount-agnostic: the same routes answer on loopback :18127 and
through the gateway, and no prefix is baked into the code. If a
future endpoint starts generating absolute URLs that must carry the
public prefix, revisit this then (that is when `root_path` earns
its place).
