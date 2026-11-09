# AirCore — Deploy Notes

Demo container for the AirCore VM + AirShield + AirBridge + AirUI stack.
One stdlib-Python server serves the API and the static frontend; there are
no dependencies to install (the Ed25519 implementation is in-tree).

## Build & run

```sh
docker build -t aircore ~/workspace/builds/aircore
docker run -d --name aircore -p 8765:8765 aircore
```

## Hosting contract (what the deploy step must know)

- **Internal port: 8765.** Start command: `python3 backend/server.py`
  (working directory `/app`; the Dockerfile's CMD already does this).
- **Public path: `/aircore/`** on Taylor's AWS host — a gateway forwards
  `http://52.41.84.116:443/aircore/` to this container **with the prefix
  intact**. The image therefore defaults to `BASE_PATH=/aircore`; the
  server only answers under that prefix and the frontend derives its API
  base from the page URL, so both sides agree. If the gateway ever strips
  the prefix instead, set `BASE_PATH=` (empty) on the container.
- Env knobs: `BASE_PATH` (default `/aircore` in the image, `""` in bare
  local runs), `HOST` (image: `0.0.0.0`), `PORT` (image: `8765`).
- Endpoints behind the prefix: `GET /aircore/` (AirUI), `GET
  /aircore/api/state`, `GET /aircore/api/log`, `POST /aircore/api/send`.
- Local smoke test without Docker:
  `BASE_PATH=/aircore python3 backend/server.py` then browse
  `http://127.0.0.1:8765/aircore/`.

## Developer seed key (demo identity)

AirShield signs/verifies with a demo Ed25519 identity that
`backend/keys.py` generates on first use. The local checkout's key is
**excluded from the image** (`.dockerignore`), so a fresh container
generates its own identity at first start into `/app/backend/keys/`
inside the container filesystem.

- **Ephemeral by default — acceptable for this demo.** The identity is
  lost when the container is removed/recreated; the demo stays
  self-consistent because AirShield signs and trusts the same identity.
- Consequence: Airlets packaged elsewhere (e.g. the local
  `airlets/MyAirlet`, signed by the local checkout's key) will **not**
  verify against a container's fresh identity.
- To persist an identity across recreations, mount a volume:
  `docker run -v aircore-keys:/app/backend/keys ...`
