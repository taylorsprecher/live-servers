"""FastAPI App — a minimal fleet service scaffold.

Scaffolded 2026-11-18 from the Dockerfile Taylor uploaded that day
(python:3.12-slim, ``uvicorn app.main:app`` on port 8000). The
service name ``fastapi-app`` is a placeholder: this app exists so the
container, the fleet wiring (compose service, gateway route, Apps
Hub tile, targeted deploy), and the smoke checks are all real and
verified, ready for real endpoints to be added here.

The app serves at the container root (``/health``, ``/``). The
gateway strips the public ``/fastapi`` prefix before proxying (the
C-Port precedent in gateway/locations-common.conf), so no base-path
or root-path handling lives in this code.

No authentication and no secrets: there is nothing here worth
protecting yet. Add both in the same change that adds the first
endpoint that needs them.
"""

import time

from fastapi import FastAPI

STARTED = time.time()

app = FastAPI(title="FastAPI App")


@app.get("/health")
def health() -> dict:
    """Liveness probe used by the deploy smoke checks and the
    Apps Hub tile's status dot."""
    return {"status": "ok", "service": "fastapi-app"}


@app.get("/")
def index() -> dict:
    """Service identity: name, purpose, and uptime in seconds."""
    return {
        "service": "fastapi-app",
        "purpose": (
            "FastAPI service scaffolded 2026-11-18 from Taylor's "
            "uploaded Dockerfile; ready to grow endpoints"
        ),
        "uptime_seconds": int(time.time() - STARTED),
    }
