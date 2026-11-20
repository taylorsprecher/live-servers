#!/usr/bin/env python3
"""Zillion Storage — MinIO backend: the same S3 API over the
fleet-internal endpoint, signed with the static root credentials.
Moved verbatim from backend/server.py."""
import hashlib
import http.client
import os
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse

from aws_backend import AwsError, sigv4_headers
from common import sha256_of_file, with_retries
from config import (CHUNK, EMPTY_SHA256, MINIO_ACCESS_KEY, MINIO_BUCKET,
                    MINIO_ENDPOINT, MINIO_REGION, MINIO_SECRET_KEY)

# ======================================================================
# MinIO backend: the same S3 API over the fleet-internal endpoint,
# signed with the static root credentials (stdlib only). MinIO is
# S3-compatible, so the SigV4 machinery above is reused unchanged —
# but addressing is path-style, there is no session token, and the
# AWS-only pieces (STS AssumeRole, SSE-KMS headers) do NOT apply.
# ======================================================================

def minio_configured():
    return bool(MINIO_ENDPOINT and MINIO_ACCESS_KEY and MINIO_SECRET_KEY)


def _minio_url(key=None):
    url = f"{MINIO_ENDPOINT}/{quote(MINIO_BUCKET, safe='-_.~')}"
    if key:
        url += "/" + quote(key, safe='/-_.~')
    return url


def _minio_open(method, url, payload_hash, extra_headers=None, body_len=0):
    """Open one SigV4-signed request against the MinIO endpoint with
    the static root credentials and return the connection after
    headers are sent; the caller streams any body, reads the
    response, and closes."""
    extras = {"X-Amz-Content-Sha256": payload_hash}
    extras.update(extra_headers or {})
    headers = sigv4_headers(
        method=method, url=url, payload_hash=payload_hash,
        service="s3", region=MINIO_REGION,
        access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY,
        extra_headers=extras)
    parsed = urlparse(url)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" \
        else http.client.HTTPConnection
    conn = conn_cls(parsed.netloc, timeout=30)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    conn.putrequest(method, target, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.putheader("Content-Length", str(body_len))
    conn.endheaders()
    return conn


def _minio_error(method, key, status, body_bytes):
    detail = body_bytes.decode("utf-8", "replace")[:300]
    return AwsError(f"MinIO {method} {key} failed HTTP {status}: {detail}",
                    status=status)


_minio_state = {"bucket_ok": False}
_minio_lock = threading.Lock()


def minio_ensure_bucket():
    """Create the object bucket on first use (HEAD, then PUT when it
    is missing) and cache the result for the life of the process. A
    404 from a data call clears the cache, so the next call re-checks
    instead of trusting a bucket that is no longer there."""
    if _minio_state["bucket_ok"]:
        return
    with _minio_lock:
        if _minio_state["bucket_ok"]:
            return
        conn = _minio_open("HEAD", _minio_url(), EMPTY_SHA256)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status == 200:
            _minio_state["bucket_ok"] = True
            return
        if resp.status != 404:
            raise _minio_error("HEAD bucket", MINIO_BUCKET, resp.status, b"")
        conn = _minio_open("PUT", _minio_url(), EMPTY_SHA256, body_len=0)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        # 409 (BucketAlreadyOwnedByYou): a concurrent creator won the
        # race — that is success for our purposes.
        if resp.status not in (200, 409):
            raise _minio_error("PUT bucket", MINIO_BUCKET, resp.status, body)
        _minio_state["bucket_ok"] = True


def minio_put_file(path, name):
    minio_ensure_bucket()
    payload_hash = sha256_of_file(path)
    size = os.path.getsize(path)
    conn = _minio_open("PUT", _minio_url(name), payload_hash, body_len=size)
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            conn.send(block)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if resp.status != 200:
        if resp.status == 404:
            _minio_state["bucket_ok"] = False
        raise _minio_error("PUT", name, resp.status, body)
    return {"ok": True}


def minio_download_hash(name):
    """Download the object and return its SHA-256 (verify phase)."""
    minio_ensure_bucket()
    conn = _minio_open("GET", _minio_url(name), EMPTY_SHA256)
    resp = conn.getresponse()
    if resp.status != 200:
        body = resp.read()
        conn.close()
        raise _minio_error("GET", name, resp.status, body)
    h = hashlib.sha256()
    while True:
        block = resp.read(CHUNK)
        if not block:
            break
        h.update(block)
    conn.close()
    return h.hexdigest()


def minio_delete(name):
    minio_ensure_bucket()
    conn = _minio_open("DELETE", _minio_url(name), EMPTY_SHA256)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if resp.status not in (200, 204):
        raise _minio_error("DELETE", name, resp.status, body)
    return {"ok": True}


def minio_list_stats():
    """One ListObjectsV2 page (MaxKeys=1000): (object_count, bytes)."""
    minio_ensure_bucket()
    url = _minio_url() + "?list-type=2&max-keys=1000"
    conn = _minio_open("GET", url, EMPTY_SHA256)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if resp.status != 200:
        raise _minio_error("LIST", MINIO_BUCKET, resp.status, body)
    count = 0
    total = 0
    for el in ET.fromstring(body).iter():
        if el.tag.split("}")[-1] == "Contents":
            count += 1
            for child in el:
                if child.tag.split("}")[-1] == "Size":
                    total += int(child.text or 0)
    return count, total


_minio_probe = {"at": 0.0, "entry": None}


def minio_health_entry():
    entry = {"name": "minio", "type": "minio", "bucket": MINIO_BUCKET,
             "configured": minio_configured(), "ok": False,
             "endpoint": MINIO_ENDPOINT or None}
    if not minio_configured():
        entry["note"] = ("Fleet-internal S3-compatible backend; activates when the "
                         "endpoint and the host-generated root credentials are provided.")
        return entry
    now = time.time()
    if _minio_probe["entry"] is not None and now - _minio_probe["at"] < 30:
        return dict(_minio_probe["entry"])
    try:
        count, total = with_retries(minio_list_stats, base_wait=1, cap=2)
        entry.update({"ok": True, "objects": count, "bytes_stored": total})
    except Exception as ex:  # noqa: BLE001 - reported, never swallowed
        entry["error"] = str(ex)[:200]
    _minio_probe["at"] = now
    _minio_probe["entry"] = entry
    return dict(entry)

