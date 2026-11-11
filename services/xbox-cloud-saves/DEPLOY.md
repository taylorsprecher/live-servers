# Deploy — Xbox Cloud Saves demo container

Demo deployment of the Xbox Cloud Save Platform: the FastAPI app in
local mode, served as one container behind a path-prefix gateway.

- **Public URL:** `http://52.41.84.116:443/xbox/` — UI at `/xbox/ui/`,
  health at `/xbox/health`
- **Internal port:** 8000 (uvicorn, `0.0.0.0`)
- **Start command** (baked into the image):
  `uvicorn app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips *`

## Gateway contract

- The gateway must forward requests **with** the `/xbox` prefix
  (do not strip it). The app takes `BASE_PATH` as its ASGI root path,
  so routing matches under the prefix and generated URLs (including
  pre-signed transfer URLs) carry the public prefix.
- The gateway must pass `Host` through, or set `X-Forwarded-Host` /
  `X-Forwarded-Proto` / `X-Forwarded-Port`: the API builds pre-signed
  transfer URLs from the request's base URL, so the URLs handed to the
  browser carry whatever scheme/host the app sees.

## Environment variables

| Variable | Deploy value | Notes |
|---|---|---|
| `BASE_PATH` | `/xbox` | Public path prefix. Empty = serve at root (local dev). |
| `XSAVE_AUTH` | `xsts` | XSTS JWT structure, signature checked against the local test key below. |
| `XSAVE_STORAGE` | `local` | Saves on the container filesystem (see data footprint). |
| `XSAVE_PRESIGN` | `local` | Local HMAC capability-token transfer URLs (15 min expiry). |
| `XSAVE_XSTS_SIGNING_KEY` | **generated at deploy time** | Signs/verifies the demo XSTS JWTs (HS256). **Required:** if unset, the code falls back to a well-known public test string. Generate with `openssl rand -hex 32`. Pass via the orchestrator's env/secret mechanism — never commit it, never bake it into the image. Rotating it invalidates all outstanding demo tokens (users just sign in again). |
| `XSAVE_DEV_SECRET` | **generated at deploy time** | HMAC secret for the local pre-signed transfer tokens. Same handling as above; if unset it falls back to a public dev value. |
| `XSAVE_STORAGE_DIR` | `/data/xsave` | Set by the image; override only to relocate save storage. |
| `XSAVE_XSTS_ISSUER` / `XSAVE_XSTS_AUDIENCE` | *(defaults)* | Optional; defaults are the real XSTS issuer/audience strings. |

## Build & run (reference)

```sh
docker build -t xbox-cloud-saves .
docker run -d --name xbox \
  -p 8000:8000 \
  -e BASE_PATH=/xbox \
  -e XSAVE_AUTH=xsts -e XSAVE_STORAGE=local -e XSAVE_PRESIGN=local \
  -e XSAVE_XSTS_SIGNING_KEY="$(openssl rand -hex 32)" \
  -e XSAVE_DEV_SECRET="$(openssl rand -hex 32)" \
  -v xsave-data:/data/xsave \
  xbox-cloud-saves
```

## Runtime data footprint

All mutable state is under `XSAVE_STORAGE_DIR` (image default
`/data/xsave`), mirroring the S3 key layout:

```
/data/xsave/users/{xuid}/savefiles/{save_name}   save file bytes
/data/xsave/users/{xuid}/_index.json    per-user metadata index
                                        (name, saveVersion, size, sha256, timestamp)
```

Nothing else in the container writes state. Without a volume mounted
at `/data/xsave`, saves live in an anonymous volume (image declares
`VOLUME`) and disappear if that volume is removed — mount a named
volume or bind mount to keep demo saves across redeploys. The image
otherwise runs read-only-friendly as the unprivileged `xsave` user.

## Demo caveats for the deploy step

- This is a **demo**: the "XSTS" tokens are real JWT structure signed
  with the deploy-generated local test key — Microsoft would not
  accept them and real Xbox tokens will not validate here. The UI
  labels this clearly.
- `/auth/dev-login` issues a demo token for **any** gamertag with no
  password. That is by design for the demo; do not widen its exposure
  beyond the demo gateway path.
- Save names are allowlisted (`[A-Za-z0-9._-]`, max 128 chars, no
  `..`); per-user isolation is enforced by deriving all storage paths
  from the token's XUID.
