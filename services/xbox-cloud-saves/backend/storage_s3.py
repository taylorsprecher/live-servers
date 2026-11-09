"""
Save storage — boto3 S3 implementation (Increment 2).

This is the production-shaped replacement for storage_stub.py: the
same public interface (put_bytes / get_bytes / list_saves /
delete_save / key_for / validate_save_name, same exceptions, same
metadata model) implemented with boto3 against S3:

    s3://{BUCKET}/users/{xuid}/{save_name}
    s3://{BUCKET}/users/{xuid}/_index.json     <- per-user metadata index

The bucket defaults to "xsave-dev" (Phase 1 of the plan) and can be
overridden with XSAVE_BUCKET. The endpoint can be overridden with
XSAVE_S3_ENDPOINT, which is how the exact same code runs against:

  * LocalStack      (XSAVE_S3_ENDPOINT=http://localhost:4566)
  * moto server     (XSAVE_S3_ENDPOINT=http://localhost:5555)
  * real AWS        (XSAVE_S3_ENDPOINT unset)

Object metadata mirrors the plan's model, extended:
    {"xuid", "name", "saveVersion", "timestamp", "size", "sha256"}
stored both in the per-user index object and as S3 object metadata.

Save-name allowlisting is identical to the stub's (the plan's review
appendix lists raw filenames as a launch blocker; the allowlist is
enforced before any key is built).
"""

import hashlib
import json
import os
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET = os.environ.get("XSAVE_BUCKET", "xsave-dev")
ENDPOINT_URL = os.environ.get("XSAVE_S3_ENDPOINT") or None
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

SAVE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_client = None


class StorageError(Exception):
    pass


class InvalidSaveName(StorageError):
    pass


class SaveNotFound(StorageError):
    pass


# ---------------------------------------------------------------- client

def get_client():
    """Lazily build (and cache) the boto3 S3 client.

    When a custom endpoint is configured (LocalStack / moto) and no
    real credentials are in the environment, fall back to the customary
    dummy "test" credentials those emulators expect.
    """
    global _client
    if _client is None:
        # SigV4 everywhere: pre-signed URLs must be SigV4 (Phase 6),
        # and some emulators otherwise fall back to legacy SigV2 URLs.
        kwargs = {"region_name": REGION, "config": Config(signature_version="s3v4")}
        if ENDPOINT_URL:
            kwargs["endpoint_url"] = ENDPOINT_URL
            if not os.environ.get("AWS_ACCESS_KEY_ID"):
                kwargs["aws_access_key_id"] = "test"
                kwargs["aws_secret_access_key"] = "test"
        _client = boto3.client("s3", **kwargs)
    return _client


def reset_client() -> None:
    """Drop the cached client (tests use this when swapping endpoints
    or entering a fresh moto context)."""
    global _client
    _client = None


def get_bucket() -> str:
    return BUCKET


def ensure_bucket() -> None:
    """Create the saves bucket if it does not exist (dev/emulator aid;
    in real AWS the bucket comes from the SAM template)."""
    client = get_client()
    try:
        client.head_bucket(Bucket=BUCKET)
    except ClientError:
        kwargs = {}
        if REGION != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": REGION}
        client.create_bucket(Bucket=BUCKET, **kwargs)


# ---------------------------------------------------------------- names/keys

def validate_save_name(name: str) -> str:
    if not SAVE_NAME_RE.match(name or "") or ".." in name:
        raise InvalidSaveName(f"invalid save name: {name!r}")
    return name


def key_for(xuid: str, name: str) -> str:
    """Canonical storage key. Paths are ALWAYS derived from the XUID —
    never from a client-supplied user id (Phase 2/3 rule)."""
    return f"users/{xuid}/{name}"


def _index_key(xuid: str) -> str:
    return f"users/{xuid}/_index.json"


# ---------------------------------------------------------------- index

def _load_index(xuid: str) -> dict:
    try:
        resp = get_client().get_object(Bucket=BUCKET, Key=_index_key(xuid))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "NoSuchBucket", "404"):
            return {}
        raise
    try:
        return json.loads(resp["Body"].read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def _save_index(xuid: str, index: dict) -> None:
    get_client().put_object(
        Bucket=BUCKET,
        Key=_index_key(xuid),
        Body=json.dumps(index, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


# ---------------------------------------------------------------- operations

def _record_meta(xuid: str, name: str, size: int, sha256: str) -> dict:
    """Insert/refresh one save's metadata in the per-user index,
    bumping saveVersion (the plan's monotonic-version model)."""
    index = _load_index(xuid)
    previous = index.get(name, {})
    meta = {
        "xuid": xuid,
        "name": name,
        "saveVersion": int(previous.get("saveVersion", 0)) + 1,
        "timestamp": int(time.time()),
        "size": size,
        "sha256": sha256,
    }
    index[name] = meta
    _save_index(xuid, index)
    return meta


def put_bytes(key: str, data: bytes) -> dict:
    xuid, name = key.split("/")[1], key.split("/", 2)[2]
    sha256 = hashlib.sha256(data).hexdigest()
    meta = _record_meta(xuid, name, len(data), sha256)
    get_client().put_object(
        Bucket=BUCKET,
        Key=key,
        Body=data,
        ContentType="application/octet-stream",
        Metadata={
            "xuid": meta["xuid"],
            "saveversion": str(meta["saveVersion"]),
            "sha256": meta["sha256"],
        },
    )
    return meta


def index_uploaded_object(key: str) -> dict | None:
    """Index an object that landed in the bucket WITHOUT going through
    put_bytes — i.e. a client PUT straight to a pre-signed URL, which
    is the normal production flow. Driven by S3 ObjectCreated events
    (see functions.handlers.on_object_created). Returns the recorded
    metadata, or None for keys that are not save objects (including
    the index objects themselves)."""
    parts = key.split("/")
    if len(parts) != 3 or parts[0] != "users" or parts[2] == "_index.json":
        return None
    xuid, name = parts[1], parts[2]
    data = get_bytes(key)
    return _record_meta(xuid, name, len(data), hashlib.sha256(data).hexdigest())


def get_bytes(key: str) -> bytes:
    try:
        resp = get_client().get_object(Bucket=BUCKET, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "NoSuchBucket", "404"):
            raise SaveNotFound(key)
        raise
    return resp["Body"].read()


def list_saves(xuid: str) -> list[dict]:
    index = _load_index(xuid)
    return [index[name] for name in sorted(index)]


def delete_save(xuid: str, name: str) -> dict:
    index = _load_index(xuid)
    if name not in index:
        raise SaveNotFound(name)
    meta = index.pop(name)
    get_client().delete_object(Bucket=BUCKET, Key=key_for(xuid, name))
    _save_index(xuid, index)
    return meta
