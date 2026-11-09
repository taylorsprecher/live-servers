"""
Wyze Bridge status service.

A small FastAPI service that sits next to Docker Wyze Bridge and answers
these questions for the dashboard:

  * Is the bridge up, and is it signed in to Wyze?
  * What state is each camera in?
  * What does the bridge configuration look like (non-secret view)?
  * Is the archive tier (S3 sync / KMS validation) configured?
  * (Webhook) accept pipeline / motion alert posts, persisted to SQLite
    so the alert history survives restarts.

CAMERA STATE IS MOCKED in this build. The mock below is seeded with what is
actually known about the "Sterner Home" cameras (see MOCK_CAMERAS notes) and
every camera response carries  "source": "mock"  so nothing downstream can
mistake it for live telemetry. On a real deployment, replace `get_cameras()`
with polling of the bridge's own API (http://wyze-bridge:5000/api/...) on
the LAN.

The bridge-config view NEVER reads or returns secret values. It reports
configuration shape (hosts, ports, services, required secret file *names*)
and deployment-state flags that come from plain environment variables
(ARCHIVE_ENABLED / ARCHIVE_CONFIGURED). Whether the secret files actually
exist on the host is checked by deploy/preflight.sh, not by this service.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8080

Environment:
    BRIDGE_HOST          bridge hostname for stream URLs (default wyze-bridge)
    BRIDGE_WEB_PORT      bridge Web UI port      (default 5000)
    BRIDGE_RTSP_PORT     bridge RTSP port        (default 8554)
    BRIDGE_HLS_PORT      bridge HLS/WebRTC port  (default 8888)
    ALERTS_DB_PATH       SQLite file for alert history
                         (default ./alerts.db; compose sets /data/alerts.db)
    ARCHIVE_ENABLED      "true" if the archive profile is running
    ARCHIVE_CONFIGURED   "true" if S3 bucket / IAM / KMS have been created
    BASE_PATH            URL path prefix the service is hosted under
                         (default ""). The demo hosting serves this app at
                         "/wyze" behind a gateway that forwards requests
                         WITH the prefix, e.g. BASE_PATH=/wyze. The prefix
                         is set as FastAPI's root_path and stripped before
                         routing; the dashboard in ../frontend is served as
                         static files by this same app under the prefix.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

STARTED_AT = time.time()

# ---------------------------------------------------------------------------
# Mock camera state — "Sterner Home"
#
# Seeded from observed facts, NOT live data:
#   * Hallway streams fine in the Wyze app; battery full.
#   * Family is NOT offline (motion clips upload, battery full) but its
#     live view fails: "Failed to connect. Error: (code -40)", later -15,
#     stuck at "Step 1 of 3: Starting up a secure connection".
#   * Bedroom / Dinning [Wyze app spelling]: no fault reported.
# Stream URLs use the URL layout Docker Wyze Bridge serves on the LAN
# (http://<bridge-host>:8888/<camera-name>/, rtsp://<bridge-host>:8554/<name>).
# ---------------------------------------------------------------------------
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "wyze-bridge")
BRIDGE_WEB_PORT = int(os.environ.get("BRIDGE_WEB_PORT", "5000"))
BRIDGE_RTSP_PORT = int(os.environ.get("BRIDGE_RTSP_PORT", "8554"))
BRIDGE_HLS_PORT = int(os.environ.get("BRIDGE_HLS_PORT", "8888"))


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Deployment-state flags for the archive tier. These are plain booleans set
# in the host's .env (see deploy/preflight.sh); they carry no secret values.
ARCHIVE_ENABLED = _env_bool("ARCHIVE_ENABLED")
ARCHIVE_CONFIGURED = _env_bool("ARCHIVE_CONFIGURED")

MOCK_CAMERAS = [
    {
        "name": "Hallway",
        "slug": "hallway",
        "model": "Wyze Cam (battery)",
        "online": True,
        "live_view": "ok",
        "live_view_detail": "Streams normally in the Wyze app.",
        "motion_uploads": True,
        "battery_percent": 100,
        "last_event": None,
    },
    {
        "name": "Bedroom",
        "slug": "bedroom",
        "model": "Wyze Cam (battery)",
        "online": True,
        "live_view": "ok",
        "live_view_detail": "No fault reported.",
        "motion_uploads": True,
        "battery_percent": None,
        "last_event": None,
    },
    {
        "name": "Dinning",  # spelling as it appears in the Wyze app
        "slug": "dinning",
        "model": "Wyze Cam (battery)",
        "online": True,
        "live_view": "ok",
        "live_view_detail": "No fault reported.",
        "motion_uploads": True,
        "battery_percent": None,
        "last_event": None,
    },
    {
        "name": "Family",
        "slug": "family",
        "model": "Wyze Cam (battery)",
        "online": True,  # NOT offline — motion clips still upload
        "live_view": "failing",
        "live_view_detail": (
            "Live view fails in the Wyze app: 'Failed to connect. "
            "Error: (code -40)', later -15, at 'Step 1 of 3: Starting up "
            "a secure connection'. This bridge is the fallback path."
        ),
        "motion_uploads": True,
        "battery_percent": 100,
        "last_event": "Motion clips uploaded the same morning live view failed.",
    },
]


def _with_stream_urls(cam: dict) -> dict:
    out = dict(cam)
    out["streams"] = {
        "webrtc_hls": f"http://{BRIDGE_HOST}:{BRIDGE_HLS_PORT}/{cam['slug']}",
        "rtsp": f"rtsp://{BRIDGE_HOST}:{BRIDGE_RTSP_PORT}/{cam['slug']}",
        "bridge_ui": f"http://{BRIDGE_HOST}:{BRIDGE_WEB_PORT}/",
    }
    return out


def get_cameras() -> list[dict]:
    """STUB: returns mock state. Replace with bridge API polling on deploy."""
    return [_with_stream_urls(c) for c in MOCK_CAMERAS]


def _endpoints() -> dict:
    return {
        "web_ui": f"http://{BRIDGE_HOST}:{BRIDGE_WEB_PORT}/",
        "rtsp_base": f"rtsp://{BRIDGE_HOST}:{BRIDGE_RTSP_PORT}/",
        "hls_base": f"http://{BRIDGE_HOST}:{BRIDGE_HLS_PORT}/",
    }


def bridge_status() -> dict:
    """STUB: reports the bridge as not-yet-deployed, which is the truth for
    this local build. On a real host this would probe the bridge container."""
    return {
        "bridge": "docker-wyze-bridge (mrlt8/wyze-bridge)",
        "deployed": False,
        "reachable": False,
        "wyze_signed_in": False,
        "detail": (
            "Not deployed yet: no Docker host on the home LAN. "
            "Status service is running with mocked camera state."
        ),
        "endpoints": _endpoints(),
    }


def archive_status() -> dict:
    """Archive tier state, from deployment flags only — honestly reports
    'not_configured' until a bucket / IAM user / KMS key actually exist and
    the host's .env says so."""
    if not ARCHIVE_CONFIGURED:
        state = "not_configured"
        detail = (
            "Archive tier is NOT CONFIGURED: no S3 bucket, least-privilege "
            "IAM user, or KMS key has been created for this project. The "
            "s3-sync and kms-validator services stay off (compose profile "
            "'archive'), and recordings exist only on the bridge host."
        )
    elif ARCHIVE_ENABLED:
        state = "enabled"
        detail = (
            "Archive tier is configured and the 'archive' compose profile "
            "is running: recordings sync to S3 every 5 minutes and the KMS "
            "validator runs daily."
        )
    else:
        state = "configured_not_enabled"
        detail = (
            "Archive tier is configured (bucket/IAM/KMS exist) but the "
            "'archive' compose profile is not currently running."
        )
    return {
        "profile": "archive",
        "state": state,
        "configured": ARCHIVE_CONFIGURED,
        "enabled": ARCHIVE_ENABLED,
        "services": ["s3-sync", "kms-validator"],
        "sync_interval_seconds": 300,
        "encryption": "SSE (AES256 or aws:kms)",
        "lifecycle": "Glacier Instant Retrieval at 90 days; delete at 365 days",
        "detail": detail,
    }


def bridge_config() -> dict:
    """Non-secret configuration view. Reports the *shape* of the deployment:
    hosts, ports, services, and the names of the secret files each service
    needs. Secret values are never read by this service, so they can never
    leak through this endpoint."""
    return {
        "bridge": {
            "image": "mrlt8/wyze-bridge:latest",
            "host": BRIDGE_HOST,
            "ports": {
                "web_ui": BRIDGE_WEB_PORT,
                "rtsp": BRIDGE_RTSP_PORT,
                "hls_webrtc": BRIDGE_HLS_PORT,
            },
            "endpoints": _endpoints(),
        },
        "services": {
            "default": ["wyze-bridge", "status-api", "dashboard"],
            "archive_profile": ["s3-sync", "kms-validator"],
        },
        "status_service": {
            "version": app.version,
            "alerts_storage": "sqlite",
            "alerts_db_path": ALERTS_DB_PATH,
        },
        "secrets": {
            "note": (
                "Names only. Values live in Docker Secrets files on the "
                "deployment host and are never read or returned here. "
                "deploy/preflight.sh checks their presence on the host."
            ),
            "bridge_files": ["wyze_email", "wyze_password"],
            "bridge_env": ["API_ID", "API_KEY"],
            "archive_files": [
                "aws_access_key_id",
                "aws_secret_access_key",
                "aws_region",
                "s3_bucket",
                "kms_key_arn",
                "slack_webhook_url",
                "discord_webhook_url",
            ],
        },
        "recordings": {
            "local_path": "./recordings (host volume)",
            "archive": archive_status(),
        },
    }


# ---------------------------------------------------------------------------
# Alert history — persisted to SQLite so it survives service restarts.
# Replaces the increment-1 in-memory list.
# ---------------------------------------------------------------------------
ALERTS_DB_PATH = os.environ.get("ALERTS_DB_PATH", "alerts.db")
_db_lock = threading.Lock()
_db = sqlite3.connect(ALERTS_DB_PATH, check_same_thread=False)
_db.execute(
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """
)
_db.commit()


def store_alert(payload) -> dict:
    entry = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    with _db_lock:
        cur = _db.execute(
            "INSERT INTO alerts (received_at, payload) VALUES (?, ?)",
            (entry["received_at"], json.dumps(payload)),
        )
        _db.commit()
        entry = {"id": cur.lastrowid, **entry}
    return entry


def fetch_alerts(limit: int = 50) -> tuple[int, list[dict]]:
    with _db_lock:
        (count,) = _db.execute("SELECT COUNT(*) FROM alerts").fetchone()
        rows = _db.execute(
            "SELECT id, received_at, payload FROM alerts "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    alerts = [
        {"id": r[0], "received_at": r[1], "payload": json.loads(r[2])}
        for r in reversed(rows)  # chronological, oldest first
    ]
    return count, alerts


def alert_count() -> int:
    with _db_lock:
        (count,) = _db.execute("SELECT COUNT(*) FROM alerts").fetchone()
    return count


# Hosting prefix, e.g. "/wyze" for the public demo hosting. Normalized to
# "" (root) or "/something" (leading slash, no trailing slash).
BASE_PATH = ("/" + os.environ.get("BASE_PATH", "").strip("/")).rstrip("/")

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "frontend"
)

app = FastAPI(
    title="Wyze Bridge Status Service",
    description="Bridge health + camera status for the Sterner Home Wyze Bridge dashboard.",
    version="0.2.0",
    root_path=BASE_PATH,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # LAN dashboard only; tighten on real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)


class StripPrefixMiddleware:
    """Strip BASE_PATH from incoming request paths before routing.

    The demo gateway forwards requests WITH the /wyze prefix intact (it
    does not rewrite them away), while the routes below are defined
    without it. root_path alone only affects generated URLs, so the
    prefix is removed here and recorded in scope["root_path"] (which
    FastAPI's root_path above also seeds). No-op when BASE_PATH is "".
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if self.prefix and scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.prefix:
                scope["path"] = "/"
            elif path.startswith(self.prefix + "/"):
                scope["path"] = path[len(self.prefix):]
            scope["root_path"] = self.prefix
        await self.app(scope, receive, send)


if BASE_PATH:
    app.add_middleware(StripPrefixMiddleware, prefix=BASE_PATH)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "wyze-bridge-status",
        "version": app.version,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "data_source": "mock",
        "alerts_stored": alert_count(),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/bridge/status")
def get_bridge_status():
    return {"source": "mock", **bridge_status()}


@app.get("/api/bridge/config")
def get_bridge_config():
    """Non-secret bridge configuration view (see bridge_config())."""
    return {"source": "config", **bridge_config()}


@app.get("/api/archive/status")
def get_archive_status():
    return {"source": "config", **archive_status()}


@app.get("/api/cameras")
def list_cameras():
    cams = get_cameras()
    return {
        "source": "mock",
        "home": "Sterner Home",
        "count": len(cams),
        "cameras": cams,
    }


@app.get("/api/cameras/{slug}")
def get_camera(slug: str):
    for cam in get_cameras():
        if cam["slug"] == slug.lower():
            return {"source": "mock", "camera": cam}
    raise HTTPException(status_code=404, detail=f"No camera named '{slug}'")


@app.post("/api/webhooks/alerts")
async def receive_alert(request: Request):
    """Accepts pipeline/motion alert posts (per the spec doc's
    run-and-notify.sh pattern) and persists them to the SQLite alert
    history. Forwards nowhere in this build; on deployment, forward to
    Slack/Discord webhook URLs from secrets."""
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body()).decode("utf-8", "replace")}
    entry = store_alert(payload)
    return {"accepted": True, "stored": alert_count(), "id": entry["id"],
            "forwarded": False}


@app.get("/api/alerts")
def list_alerts(limit: int = 50):
    count, alerts = fetch_alerts(limit=max(1, min(limit, 500)))
    return {"source": "sqlite", "count": count, "alerts": alerts}


# Dashboard, served by this same app (mounted last so /health and /api/*
# take precedence). With BASE_PATH set, the strip-prefix middleware above
# makes this answer under the prefix too, e.g. GET /wyze/ -> index.html.
# The dashboard's API calls are prefix-relative (see frontend/app.js).
if os.path.isdir(FRONTEND_DIR):
    app.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIR, html=True),
        name="dashboard",
    )
