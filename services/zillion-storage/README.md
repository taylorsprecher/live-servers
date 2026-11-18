# Zillion Storage

The runnable core of Taylor's **Python Cloud — 1 Zillion TB** design
document (`tmp3D_Python_Cloud_1Zillion_TB.docx`, filed 2026-11-03;
installed 2026-11-18 at Taylor's direction: "Install" / "Apply to
AWS S3" / "And websites").

## What runs

A single stdlib-Python service implementing the design's pipeline:

- **Streaming ingest** — request bodies are written to disk in 1 MB
  chunks while the SHA-256 is computed; payloads never load whole
  into RAM.
- **Concurrent replication** — every object is copied to all
  backends in parallel (`ThreadPoolExecutor`), per-backend fault
  isolation. The backend set is the two local stores plus, when AWS
  credentials are configured, the S3 backend described below.
- **Retry with exponential backoff** — 3 attempts per backend write
  (local profile 0.5s→1s→2s; the document's network profile for cloud
  backends is 2–10s).
- **SHA-256 verification phase** — after upload and on demand
  (`POST /zillion/verify/<name>`), every backend copy is re-read,
  re-hashed, and compared bit-for-bit with the ingest hash.

Endpoints (under `BASE_PATH=/zillion`):

- `GET /zillion/health` — status, backends with real measured
  capacity, object count, bytes stored.
- `GET /zillion/objects` — manifest listing.
- `PUT /zillion/objects/<name>` — store (body = raw bytes).
- `GET /zillion/objects/<name>` — download.
- `POST /zillion/verify/<name>` — re-verify all copies.
- `DELETE /zillion/objects/<name>` — remove from all backends.

## Backends — real vs design

Real today: `primary` and `replica` local stores on the `zillion-data`
Docker volume.

Applied to AWS S3 (2026-11-18): bucket **zillion-tb-aws-bucket** in
Taylor's AWS management account (392349106458, us-west-2), created
per the design's Terraform fragments (SSE-KMS with the
customer-managed key alias `zillion-tb-key`, all public access
blocked, versioning on, tags project=multi-cloud-fabric /
env=production), plus the IAM role **zillion-control-plane-role**
whose only policy allows ListBucket + Get/Put/Delete on this bucket.

### How the fleet service reaches the bucket (wired 2026-11-18)

The service host sits in a *different* AWS account, so it cannot use
an instance role. Instead:

1. **Base credentials** — an IAM user's access key pair is held as
   the GitHub secrets `ZILLION_AWS_ACCESS_KEY_ID` /
   `ZILLION_AWS_SECRET_ACCESS_KEY`. The deploy workflow pipes them
   over SSH stdin into `ops/set-zillion-aws-creds.sh`, which upserts
   them into the host `.env` (0600, values never in argv or logs —
   the same pattern as the proxy password). Compose passes them to
   the container as env vars of the same names.
2. **AssumeRole** — on first S3 use, the service SigV4-signs an STS
   AssumeRole call (stdlib only; no boto3) for
   `arn:aws:iam::392349106458:role/zillion-control-plane-role`
   (session name `zillion-storage`, region us-west-2) and caches the
   temporary credentials until 60 s before they expire.
3. **S3 operations** — PUT (with `aws:kms` server-side encryption
   headers naming the `zillion-tb-key` key), GET, DELETE, and the
   verification phase (download + SHA-256) are all SigV4-signed with
   the temporary credentials, token header included. The S3 backend
   joins the replication set beside `primary` and `replica` with the
   same fault isolation: an AWS failure lands in that backend's
   result as an error string and never takes the local backends down.
4. **Health** — `GET /zillion/health` reports `aws-s3` with
   `configured: true` and `ok` from a real ListObjectsV2 probe
   (object count and bytes included) only when both credential
   variables are present; otherwise `configured: false`, exactly as
   before. Optional overrides: `ZILLION_S3_BUCKET`,
   `ZILLION_S3_REGION`, `ZILLION_S3_ROLE_ARN`, `ZILLION_S3_KMS_KEY_ID`.

**Not live yet:** the wiring above is code-complete and locally
tested, but it activates only when the two GitHub secrets exist and
a deploy runs — the credentials themselves are created on a separate
track. Until then the service runs local-only and reports the S3
backend as not configured. The deploy smoke checks assert the S3
backend appears in the PUT result and verification only when it is
configured, and pass on local backends alone when it is not.

Design-only until their clusters/credentials exist: MinIO, Ceph, GCS.
The health endpoint lists them as not configured; nothing pretends
otherwise. "1 Zillion TB" is the design's name — real capacity is
always reported as measured.

## Fleet wiring

- Compose service `zillion` (container `live-zillion`, mem 128m,
  loopback `127.0.0.1:18126:8772`, volume `zillion-data:/data`).
- Gateway location `/zillion/` in `gateway/locations-common.conf`
  (with `client_max_body_size 64m` for uploads).
- Targeted deploy: `ops/deploy-zillion.sh` +
  `.github/workflows/deploy-zillion.yml` (builds only this service,
  smoke-tests an object round trip with SHA-256 verification,
  force-recreates the gateway, runs the standard regressions).
- Apps Hub tile: "Zillion Storage" → `members.911transedu.net/zillion/`.
