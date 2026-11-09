# Xbox Cloud Save Platform — Local Dev Build (Increments 1–2)

A runnable local scaffold of the Xbox Cloud Save Platform from the
production plan in Drive ("Xbox AWS S3.docx"). It implements the Save
API (list / upload / download / delete) using the production
pre-signed-URL flow, with the cloud pieces simulated locally.

**Nothing here talks to Microsoft or AWS, and nothing is deployed.**

## What's stubbed

| Piece | Stub | Production equivalent |
|---|---|---|
| Xbox XSTS auth | `backend/auth_stub.py` — `/auth/dev-login` issues an unsigned fake token for any gamertag; XUID derived from the gamertag | Microsoft-validated XSTS JWT; XUID from token claims |
| S3 | `backend/storage_stub.py` — files under `backend/storage_data/users/{xuid}/` | S3 buckets `xsave-dev/stage/prod` with versioning + encryption |
| Pre-signed URLs | `backend/presign_stub.py` — HMAC capability tokens at `/transfer/{token}`, 15-min expiry, single object + operation | boto3 SigV4 pre-signed URLs |
| API Gateway + Lambda | One FastAPI app | HTTP API + Lambda handlers (SAM increment next) |

The stubs keep the production contracts: the
`Authorization: XBL3.0 x=<user_hash>;<xsts_token>` header format,
XUID-derived storage paths (`users/{xuid}/…` only), the Phase 5
endpoint shapes (`POST /save` → `{uploadUrl}`, `GET /save` →
`{downloadUrl}`), 400/401/404/500 error semantics, and an allowlisted
save-name namespace (no path traversal).

## Run it

```bash
cd ~/workspace/builds/xbox-cloud-saves
python3 -m venv .venv            # once
.venv/bin/pip install -r backend/requirements.txt

cd backend
../.venv/bin/uvicorn app:app --port 8000
```

Then open the UI: <http://127.0.0.1:8000/ui/>

1. Enter any gamertag → **Sign in (stub)**.
2. Upload: type a save name (e.g. `save_slot1.dat`), pick a file, **Upload**.
3. Download or delete from the list. Uploading the same name again bumps
   its `saveVersion`.

Opening `frontend/index.html` directly from disk also works — the page
falls back to `http://127.0.0.1:8000` for the API.

## API quick reference

```bash
B=http://127.0.0.1:8000

# Stub sign-in -> {"authorization": "XBL3.0 x=<hash>;<token>", "xuid": ...}
curl -s -X POST $B/auth/dev-login -H 'Content-Type: application/json' \
  -d '{"gamertag":"PlayerOne"}'
AUTH='XBL3.0 x=<hash>;<token>'   # paste from the login response

curl -s $B/saves -H "Authorization: $AUTH"                      # list
curl -s -X POST $B/save -H "Authorization: $AUTH" \             # upload URL
  -H 'Content-Type: application/json' -d '{"name":"save_slot1.dat"}'
curl -s -X PUT --data-binary @save_slot1.dat '<uploadUrl>'      # send bytes
curl -s "$B/save?name=save_slot1.dat" -H "Authorization: $AUTH" # download URL
curl -s '<downloadUrl>' -o save_slot1.dat                       # fetch bytes
curl -s -X DELETE $B/saves/save_slot1.dat -H "Authorization: $AUTH"
curl -s $B/health                                               # stub disclosure
```

## Layout

```
backend/
  app.py           FastAPI app: routes, CORS, error mapping (400/401/404/500);
                   backend implementations selected by env (see Increment 2)
  auth_xsts.py     XSTS JWT structure: signature vs local test key,
                   DisplayClaims -> XUID (production-shaped validation)
  auth_stub.py     Increment 1 unsigned fake tokens (XSAVE_AUTH=stub)
  storage_s3.py    boto3 S3 storage, same interface/layout as the stub
  storage_stub.py  local-filesystem S3 stand-in, same key layout (STUB)
  presign_s3.py    boto3 SigV4 pre-signed URLs (15-min expiry)
  presign_stub.py  HMAC pre-signed-URL simulation (STUB)
  functions/
    handlers.py    Lambda handlers for API Gateway (incl. the S3
                   ObjectCreated metadata indexer)
    requirements.txt  (boto3 is provided by the Lambda runtime)
  tests/           pytest suite (53 tests): auth cases, API round-trip
                   + isolation, boto3 storage vs moto, Lambda full loop
  requirements.txt / requirements-dev.txt
frontend/          sign-in placeholder + saves UI (two-step URL flow)
template.yaml      SAM template: bucket, HTTP API, 6 functions,
                   least-privilege object-only policy (NOT deployed)
docker-compose.localstack.yml + localstack/init.sh
                   LocalStack dev stack, auto-provisions xsave-dev
demo/index.html    Self-contained interactive DEMO (browser-only,
                   localStorage) for embedding in a static website
```

## Increment 2 — production-shaped backends

The FastAPI app (and the Lambda handlers, which always use the
production-shaped choices) select implementations by environment:

| Variable | Values | Default |
|---|---|---|
| `XSAVE_AUTH` | `xsts` signed JWT structure, local test key (`XSAVE_XSTS_SIGNING_KEY`) · `stub` Increment 1 unsigned | `xsts` |
| `XSAVE_STORAGE` | `local` filesystem · `s3` boto3 (`XSAVE_BUCKET`, `XSAVE_S3_ENDPOINT` for LocalStack/moto) | `local` |
| `XSAVE_PRESIGN` | `local` HMAC tokens at `/transfer/...` · `s3` SigV4 URLs straight to storage | `local` |

Run the full boto3 path locally (needs the moto server or LocalStack):

```bash
.venv/bin/pip install 'moto[server]' flask flask-cors   # or use Docker + the compose file
.venv/bin/moto_server -p 5555 &
cd backend
XSAVE_STORAGE=s3 XSAVE_PRESIGN=s3 XSAVE_S3_ENDPOINT=http://127.0.0.1:5555 \
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  ../.venv/bin/python -c "import storage_s3; storage_s3.ensure_bucket()"
XSAVE_STORAGE=s3 XSAVE_PRESIGN=s3 XSAVE_S3_ENDPOINT=http://127.0.0.1:5555 \
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  ../.venv/bin/uvicorn app:app --port 8000
```

In `s3` presign mode the byte flow is the production one: the API
issues a SigV4 URL, the client PUTs straight to storage, and the
metadata index is maintained by the ObjectCreated indexer
(`functions/handlers.py: on_object_created`) — in AWS it fires
automatically from the SAM template's S3 event; locally, invoke it
with the object's key to simulate the event.

Tests: `cd backend && ../.venv/bin/python -m pytest` (uses moto
in-process; no AWS, no network).

Still stubbed / not done: real Microsoft XSTS signature validation
(needs the Xbox relying-party registration — a spec launch blocker;
the local test key exercises the identical code path), upload
size/quota/checksum enforcement and write-conflict handling, metrics
and rate limiting, and the console client's retry logic. Nothing is
deployed to AWS.

Environment knobs: `XSAVE_STORAGE_DIR` (storage location),
`XSAVE_DEV_SECRET` (HMAC secret for transfer tokens — dev only).

## Next increments

SAM template + Lambda handlers, LocalStack with a real (emulated) S3
bucket via boto3, real XSTS signature validation, pytest suite
(auth cases + end-to-end), upload size/quota/checksum enforcement.
See `~/workspace/goals/xbox-cloud-save-platform-build/files/Build_Brief.md`.
