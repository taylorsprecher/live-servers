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
- **Concurrent replication** — every object is copied to all local
  backends in parallel (`ThreadPoolExecutor`), per-backend fault
  isolation.
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
Taylor's AWS management account (us-west-2), created per the design's
Terraform fragments (SSE-KMS with a customer-managed key, all public
access blocked, versioning on, tags project=multi-cloud-fabric /
env=production) plus a scoped IAM role for the Python control plane.
The fleet service reports the S3 backend as configured only when
`ZILLION_S3_BUCKET` is set and the host carries credentials via that
role — until then it is reported honestly as not yet wired.

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
