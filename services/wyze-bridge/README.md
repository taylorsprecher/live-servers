# Wyze Bridge — Sterner Home

A fallback live-view path for the Wyze cameras, built from the
"WYZE APP.docx" spec (Docker Wyze Bridge → local streams → optional S3
archive). The immediate motivation: the **Family** camera's live view fails
in the Wyze app (errors -40 / -15) while the camera itself is online and
still uploading motion clips. The bridge re-serves the streams on the LAN,
bypassing the Wyze app's live-view path.

**Current state: local build only.** Camera data in the status service is
mocked. Nothing is deployed, and no real credentials exist in this folder.

## Layout

```
docker-compose.yml   bridge + status-api + dashboard (+ archive profile)
.env.example         placeholder env template (API_ID / API_KEY, archive flags)
secrets/             Docker Secrets files — *.example placeholders only
backend/             FastAPI status service (bridge health, cameras, alerts)
frontend/            dashboard page (nginx; proxies /api/ to status-api)
scripts/             sync.sh, kms-validate.sh, run-and-notify.sh (archive tier)
deploy/              preflight.sh / deploy.sh / health-check.sh (run on the host)
demo/                self-contained static demo of the dashboard (mock data)
recordings/          local recordings volume (created on the host)
wyze-config/         bridge config volume (created on the host)
```

## Run the status service + dashboard logic locally (no Docker)

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

Then:

- `GET http://localhost:8080/health`
- `GET http://localhost:8080/api/bridge/status`
- `GET http://localhost:8080/api/bridge/config` — non-secret config view
  (hosts, ports, services, required secret *names*; never values)
- `GET http://localhost:8080/api/archive/status` — archive tier state
  (honestly reports `not_configured` until a bucket/IAM/KMS exist)
- `GET http://localhost:8080/api/cameras` — the four Sterner Home cameras
- `POST /api/webhooks/alerts` / `GET /api/alerts` — alert history,
  persisted to SQLite (`ALERTS_DB_PATH`; compose uses a named volume)
  so it survives restarts
- Open `frontend/index.html` in a browser (it calls the API on
  `localhost:8080` via the nginx proxy in the compose stack; for a raw
  file open, set `window.WYZE_API_BASE = "http://localhost:8080"` before
  `app.js` loads, or serve the folder with any static server behind the
  same proxy rule).

A fully static, self-contained **demo** of the dashboard (mock data,
clearly labeled) lives in `demo/index.html` — it can be dropped into a
static website as-is or lifted in as a page section.

## Run the full stack (needs a Docker host)

The guided path, on the host:

```bash
deploy/preflight.sh             # checks Docker/disk/ports, prompts for
                                # API_ID/API_KEY + secrets, pulls images
deploy/deploy.sh                # starts bridge + status-api + dashboard
deploy/deploy.sh --archive      # ... and the archive tier too
deploy/health-check.sh          # verifies containers + endpoints
```

The manual equivalent:

```bash
cp .env.example .env            # fill in API_ID / API_KEY on the host
# create real secret files from the examples, on the host only:
for f in secrets/*.example; do cp "$f" "${f%.example}"; done
# edit each secrets/* file with the real value, then:
chmod 600 secrets/* && rm secrets/*.example  # optional tidy-up

mkdir -p recordings wyze-config
docker compose up -d            # bridge + status-api + dashboard
docker compose --profile archive up -d   # + s3-sync + kms-validator
```

- Bridge Web UI: `http://<host>:5000/`
- Dashboard: `http://<host>:8081/`
- Status API: `http://<host>:8080/api/cameras`
- RTSP per camera: `rtsp://<host>:8554/<camera-name>`
- HLS/WebRTC: `http://<host>:8888/<camera-name>`

## Deploying on the home network later — requirements

1. **A Docker host on the home LAN, on 24/7.** Mac mini, MacBook Air,
   Raspberry Pi, etc. This is the gating item: the bridge must be inside
   the LAN with the cameras; it cannot run from a cloud VM (and Wyze
   streams would break the point of the fallback anyway).
2. **Wyze credentials, host-side only:** account email + password in
   `secrets/wyze_email` / `secrets/wyze_password`, plus an **API Key ID +
   API key** from the Wyze developer portal in `.env` (required by
   docker-wyze-bridge for current accounts). If the account has 2FA,
   expect a one-time code at first bridge sign-in.
3. **After first `docker compose up`:** open the bridge UI, confirm all
   four cameras are discovered, and test the Family camera's HLS/RTSP
   stream — that's the moment of truth for the fallback path.
4. **Swap the mock for live data:** point the status service's
   `get_cameras()` at the bridge's API (`http://wyze-bridge:5000/api/...`)
   instead of `MOCK_CAMERAS`.
5. **Archive tier (optional):** create a *private* S3 bucket + a
   least-privilege IAM user; put its keys in `secrets/`; optionally create
   a KMS key for `aws:kms` encryption; apply the doc's lifecycle rule
   (Glacier Instant Retrieval at 90 days, expire at 365 days); then start
   the `archive` profile.
6. **Battery note:** these are battery cameras. Use the bridge for
   on-demand viewing; continuous recording will drain them quickly.

## Safety rules baked in

- No real credentials in this repo — placeholders only; real values live
  in `secrets/` files on the deployment host with `chmod 600`.
- Containers run with `no-new-privileges`; recordings mount read-only
  into the sync container.
- The status service labels all mock data with `"source": "mock"`.
