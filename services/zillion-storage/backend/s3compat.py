#!/usr/bin/env python3
"""Zillion Storage — Ceph and GCS backends on the one generic
S3-compatible client. Moved verbatim from backend/server.py."""
import hashlib
import http.client
import os
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse

from aws_backend import AwsError, sigv4_headers
from common import sha256_of_file, with_retries
from config import (CEPH_ACCESS_KEY, CEPH_BUCKET, CEPH_ENDPOINT,
                    CEPH_REGION, CEPH_SECRET_KEY, CHUNK, EMPTY_SHA256,
                    GCS_ACCESS_KEY, GCS_BUCKET, GCS_ENDPOINT, GCS_REGION,
                    GCS_SECRET_KEY)

# ======================================================================
# Ceph and GCS backends: both speak the S3 API, so both ride the one
# generic S3-compatible client below (stdlib only, SigV4-signed,
# path-style — the MinIO pattern, parameterized). Written 2026-11-18
# at Taylor's direction so the code is ready ahead of the
# infrastructure: no Ceph cluster and no Google Cloud project exist
# yet, so both report honestly as not configured until their
# environment variables arrive with the real systems.
#
# Ceph: the endpoint is the cluster's RADOS Gateway (RGW); the keys
# are an RGW user's S3 keys (radosgw-admin user create); the SigV4
# region is the RGW zonegroup's region name — "default" on a stock
# single-realm cluster.
#
# GCS: the endpoint is Google Cloud Storage's S3-compatible XML API
# (storage.googleapis.com); the keys are HMAC keys for a service
# account (Cloud Console -> Cloud Storage -> Settings ->
# Interoperability) — deliberately NOT the google-cloud-storage SDK
# and NOT OAuth, so this file stays stdlib-only under Taylor's
# Python-with-S3 standard. The SigV4 region must be the bucket's
# location (e.g. "us-east1"). ZILLION_GCS_ENDPOINT may override the
# endpoint (regional endpoints, or a stand-in endpoint in tests).
# ======================================================================

class S3CompatBackend:
    """One S3-compatible backend (Ceph RGW or the GCS XML API): the
    MinIO operations, parameterized — static credentials, path-style
    addressing, no session token, no AWS-only headers. Each instance
    carries its own bucket cache and health-probe cache."""

    def __init__(self, name, label, endpoint, access_key, secret_key,
                 bucket, region, unconfigured_note):
        self.name = name
        self.label = label
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        self.unconfigured_note = unconfigured_note
        self._bucket_ok = False
        self._lock = threading.Lock()
        self._probe = {"at": 0.0, "entry": None}

    def configured(self):
        return bool(self.endpoint and self.access_key
                    and self.secret_key and self.bucket)

    def _url(self, key=None):
        url = f"{self.endpoint}/{quote(self.bucket, safe='-_.~')}"
        if key:
            url += "/" + quote(key, safe='/-_.~')
        return url

    def _open(self, method, url, payload_hash, body_len=0):
        headers = sigv4_headers(
            method=method, url=url, payload_hash=payload_hash,
            service="s3", region=self.region,
            access_key=self.access_key, secret_key=self.secret_key,
            extra_headers={"X-Amz-Content-Sha256": payload_hash})
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

    def _error(self, method, key, status, body_bytes):
        detail = body_bytes.decode("utf-8", "replace")[:300]
        return AwsError(
            f"{self.label} {method} {key} failed HTTP {status}: {detail}",
            status=status)

    def ensure_bucket(self):
        """Create the object bucket on first use (HEAD, then PUT when
        missing) and cache the result for the life of the process. A
        404 from a data call clears the cache so the next call
        re-checks instead of trusting a bucket that is gone."""
        if self._bucket_ok:
            return
        with self._lock:
            if self._bucket_ok:
                return
            conn = self._open("HEAD", self._url(), EMPTY_SHA256)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                self._bucket_ok = True
                return
            if resp.status != 404:
                raise self._error("HEAD bucket", self.bucket, resp.status, b"")
            conn = self._open("PUT", self._url(), EMPTY_SHA256, body_len=0)
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            # 409: a concurrent creator won the race — success here.
            if resp.status not in (200, 409):
                raise self._error("PUT bucket", self.bucket, resp.status, body)
            self._bucket_ok = True

    def put_file(self, path, name):
        self.ensure_bucket()
        payload_hash = sha256_of_file(path)
        size = os.path.getsize(path)
        conn = self._open("PUT", self._url(name), payload_hash, body_len=size)
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
                self._bucket_ok = False
            raise self._error("PUT", name, resp.status, body)
        return {"ok": True}

    def download_hash(self, name):
        """Download the object and return its SHA-256 (verify phase)."""
        self.ensure_bucket()
        conn = self._open("GET", self._url(name), EMPTY_SHA256)
        resp = conn.getresponse()
        if resp.status != 200:
            body = resp.read()
            conn.close()
            raise self._error("GET", name, resp.status, body)
        h = hashlib.sha256()
        while True:
            block = resp.read(CHUNK)
            if not block:
                break
            h.update(block)
        conn.close()
        return h.hexdigest()

    def delete(self, name):
        self.ensure_bucket()
        conn = self._open("DELETE", self._url(name), EMPTY_SHA256)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status not in (200, 204):
            raise self._error("DELETE", name, resp.status, body)
        return {"ok": True}

    def list_stats(self):
        """One ListObjectsV2 page (MaxKeys=1000): (object_count, bytes)."""
        self.ensure_bucket()
        url = self._url() + "?list-type=2&max-keys=1000"
        conn = self._open("GET", url, EMPTY_SHA256)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status != 200:
            raise self._error("LIST", self.bucket, resp.status, body)
        count = 0
        total = 0
        for el in ET.fromstring(body).iter():
            if el.tag.split("}")[-1] == "Contents":
                count += 1
                for child in el:
                    if child.tag.split("}")[-1] == "Size":
                        total += int(child.text or 0)
        return count, total

    def health_entry(self):
        entry = {"name": self.name, "type": self.name,
                 "bucket": self.bucket or None,
                 "configured": self.configured(), "ok": False,
                 "endpoint": self.endpoint or None}
        if not self.configured():
            entry["note"] = self.unconfigured_note
            return entry
        now = time.time()
        if self._probe["entry"] is not None and now - self._probe["at"] < 30:
            return dict(self._probe["entry"])
        try:
            count, total = with_retries(self.list_stats, base_wait=1, cap=2)
            entry.update({"ok": True, "objects": count, "bytes_stored": total})
        except Exception as ex:  # noqa: BLE001 - reported, never swallowed
            entry["error"] = str(ex)[:200]
        self._probe["at"] = now
        self._probe["entry"] = entry
        return dict(entry)


ceph_backend = S3CompatBackend(
    "ceph", "Ceph", CEPH_ENDPOINT, CEPH_ACCESS_KEY, CEPH_SECRET_KEY,
    CEPH_BUCKET, CEPH_REGION,
    "Adapter ready (Ceph RADOS Gateway, S3-compatible); no Ceph cluster "
    "exists yet — activates when the RGW endpoint and its S3 keys are provided.")
gcs_backend = S3CompatBackend(
    "gcs", "GCS", GCS_ENDPOINT, GCS_ACCESS_KEY, GCS_SECRET_KEY,
    GCS_BUCKET, GCS_REGION,
    "Adapter ready (Google Cloud Storage XML API, HMAC keys); no Google "
    "Cloud project exists yet — activates when the bucket and HMAC keys "
    "are provided.")


def ceph_health_entry():
    return ceph_backend.health_entry()


def gcs_health_entry():
    return gcs_backend.health_entry()


def _verify_s3compat(backend, name, expected_hash):
    """Verify-phase entry for one S3-compatible backend: download and
    re-hash, with the same PASSED / FAILED / MISSING / ERROR reporting
    as the aws-s3 and minio blocks — errors surface, never hide."""
    try:
        actual = with_retries(lambda: backend.download_hash(name),
                              base_wait=2, cap=10)
        return {"ok": actual == expected_hash,
                "result": "PASSED" if actual == expected_hash else "FAILED",
                "sha256": actual}
    except AwsError as ex:
        if ex.status == 404:
            return {"ok": False, "result": "MISSING"}
        return {"ok": False, "result": "ERROR", "error": str(ex)[:200]}
    except Exception as ex:  # noqa: BLE001 - reported, never swallowed
        return {"ok": False, "result": "ERROR", "error": str(ex)[:200]}

