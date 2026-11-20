#!/usr/bin/env python3
"""Zillion Storage — configuration: every environment variable, name
and default exactly as the original backend/server.py read them
(BASE_PATH, PORT, ZILLION_DATA, all ZILLION_AWS_*, ZILLION_S3_*,
ZILLION_MINIO_*, ZILLION_CEPH_* and ZILLION_GCS_* names), plus the
derived constants the backends share."""
import hashlib
import os
import re
import time

BASE = os.environ.get("BASE_PATH", "/zillion").rstrip("/")
PORT = int(os.environ.get("PORT", "8772"))
DATA = os.environ.get("ZILLION_DATA", "/data")
STARTED = time.time()

BACKENDS = [
    {"name": "primary", "type": "local", "path": os.path.join(DATA, "primary")},
    {"name": "replica", "type": "local", "path": os.path.join(DATA, "replica")},
]

# --- AWS S3 backend configuration (all optional; the backend is ------
# --- configured only when BOTH base credential variables are set) ----
S3_BUCKET = os.environ.get("ZILLION_S3_BUCKET", "zillion-tb-aws-bucket").strip() \
    or "zillion-tb-aws-bucket"
S3_REGION = os.environ.get("ZILLION_S3_REGION", "us-west-2").strip() or "us-west-2"
S3_ROLE_ARN = os.environ.get(
    "ZILLION_S3_ROLE_ARN",
    "arn:aws:iam::392349106458:role/zillion-control-plane-role").strip()
S3_KMS_KEY_ID = os.environ.get(
    "ZILLION_S3_KMS_KEY_ID", "19f314ab-afa4-4746-a2b5-39427104158c").strip()
AWS_ACCESS_KEY_ID = os.environ.get("ZILLION_AWS_ACCESS_KEY_ID", "").strip()
AWS_SECRET_ACCESS_KEY = os.environ.get("ZILLION_AWS_SECRET_ACCESS_KEY", "").strip()
S3_HOST = f"{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHUNK = 1024 * 1024
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]{0,199}$")
MANIFEST_PATH = os.path.join(DATA, "manifest.json")

# --- MinIO configuration (all optional; the backend is configured ---
# --- only when the endpoint AND both credential variables are set) --
MINIO_ENDPOINT = os.environ.get("ZILLION_MINIO_ENDPOINT", "").strip().rstrip("/")
MINIO_ACCESS_KEY = os.environ.get("ZILLION_MINIO_ACCESS_KEY", "").strip()
MINIO_SECRET_KEY = os.environ.get("ZILLION_MINIO_SECRET_KEY", "").strip()
MINIO_BUCKET = os.environ.get("ZILLION_MINIO_BUCKET", "zillion-objects").strip() \
    or "zillion-objects"
# SigV4 credential scope only; MinIO's own default region.
MINIO_REGION = os.environ.get("ZILLION_MINIO_REGION", "us-east-1").strip() or "us-east-1"


CEPH_ENDPOINT = os.environ.get("ZILLION_CEPH_ENDPOINT", "").strip().rstrip("/")
CEPH_ACCESS_KEY = os.environ.get("ZILLION_CEPH_ACCESS_KEY", "").strip()
CEPH_SECRET_KEY = os.environ.get("ZILLION_CEPH_SECRET_KEY", "").strip()
CEPH_BUCKET = os.environ.get("ZILLION_CEPH_BUCKET", "zillion-objects").strip() \
    or "zillion-objects"
CEPH_REGION = os.environ.get("ZILLION_CEPH_REGION", "default").strip() or "default"

GCS_ENDPOINT = os.environ.get(
    "ZILLION_GCS_ENDPOINT", "https://storage.googleapis.com").strip().rstrip("/")
GCS_ACCESS_KEY = os.environ.get("ZILLION_GCS_HMAC_ACCESS_KEY", "").strip()
GCS_SECRET_KEY = os.environ.get("ZILLION_GCS_HMAC_SECRET_KEY", "").strip()
GCS_BUCKET = os.environ.get("ZILLION_GCS_BUCKET", "").strip()
GCS_REGION = os.environ.get("ZILLION_GCS_REGION", "us-east1").strip() or "us-east1"

