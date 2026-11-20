#!/usr/bin/env python3
"""Zillion Storage — backend fan-out: health reporting, concurrent
replication and the SHA-256 verification phase across the local
stores plus aws-s3 / minio / ceph / gcs when configured. Moved
verbatim from backend/server.py."""
import concurrent.futures
import os
import shutil

from aws_backend import (AwsError, s3_configured, s3_delete,
                         s3_download_hash, s3_health_entry, s3_put_file)
from common import sha256_of_file, with_retries
from config import BACKENDS
from minio_backend import (minio_configured, minio_delete,
                           minio_download_hash, minio_health_entry,
                           minio_put_file)
from s3compat import (_verify_s3compat, ceph_backend, ceph_health_entry,
                      gcs_backend, gcs_health_entry)

def backend_health():
    out = []
    for b in BACKENDS:
        entry = {"name": b["name"], "type": b["type"], "configured": True}
        try:
            os.makedirs(b["path"], exist_ok=True)
            usage = shutil.disk_usage(b["path"])
            entry.update({"ok": True, "free_bytes": usage.free, "total_bytes": usage.total})
        except Exception as ex:  # noqa: BLE001
            entry.update({"ok": False, "error": str(ex)[:200]})
        out.append(entry)
    out.append(s3_health_entry())
    out.append(minio_health_entry())
    out.append(ceph_health_entry())
    out.append(gcs_health_entry())
    return out


def replicate(tmp_path, name):
    """Copy the staged object to every backend, concurrently, each
    with retries; returns {backend_name: result dict}. The set is the
    two local stores plus, when configured, aws-s3, minio, ceph and
    gcs. Fault isolation per the design: one backend failing never
    blocks or fails the others — its result carries the error."""
    def put_local(backend):
        dest = os.path.join(backend["path"], name)
        os.makedirs(backend["path"], exist_ok=True)
        staging = dest + ".part"
        shutil.copyfile(tmp_path, staging)
        os.replace(staging, dest)
        return {"ok": True}
    jobs = [(b["name"], lambda b=b: with_retries(lambda: put_local(b)))
            for b in BACKENDS]
    if s3_configured():
        jobs.append(("aws-s3", lambda: with_retries(
            lambda: s3_put_file(tmp_path, name), base_wait=2, cap=10)))
    if minio_configured():
        jobs.append(("minio", lambda: with_retries(
            lambda: minio_put_file(tmp_path, name), base_wait=2, cap=10)))
    if ceph_backend.configured():
        jobs.append(("ceph", lambda: with_retries(
            lambda: ceph_backend.put_file(tmp_path, name), base_wait=2, cap=10)))
    if gcs_backend.configured():
        jobs.append(("gcs", lambda: with_retries(
            lambda: gcs_backend.put_file(tmp_path, name), base_wait=2, cap=10)))
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(job): name_ for name_, job in jobs}
        for fut in concurrent.futures.as_completed(futures):
            bname = futures[fut]
            try:
                results[bname] = fut.result()
            except Exception as ex:  # noqa: BLE001 - fault isolation per the design
                results[bname] = {"ok": False, "error": str(ex)[:200]}
    return results


def verify(name, expected_hash):
    """Re-read and re-hash every backend copy (design Phase 2). The
    aws-s3 and minio copies, when configured, are downloaded and
    hashed the same way; a failure is reported as an ERROR entry with
    the error string, never hidden."""
    out = {}
    for b in BACKENDS:
        path = os.path.join(b["path"], name)
        if not os.path.exists(path):
            out[b["name"]] = {"ok": False, "result": "MISSING"}
            continue
        actual = sha256_of_file(path)
        out[b["name"]] = {"ok": actual == expected_hash,
                          "result": "PASSED" if actual == expected_hash else "FAILED",
                          "sha256": actual}
    if s3_configured():
        try:
            actual = with_retries(lambda: s3_download_hash(name),
                                  base_wait=2, cap=10)
            out["aws-s3"] = {"ok": actual == expected_hash,
                             "result": "PASSED" if actual == expected_hash else "FAILED",
                             "sha256": actual}
        except AwsError as ex:
            if ex.status == 404:
                out["aws-s3"] = {"ok": False, "result": "MISSING"}
            else:
                out["aws-s3"] = {"ok": False, "result": "ERROR",
                                 "error": str(ex)[:200]}
        except Exception as ex:  # noqa: BLE001 - reported, never swallowed
            out["aws-s3"] = {"ok": False, "result": "ERROR",
                             "error": str(ex)[:200]}
    if minio_configured():
        try:
            actual = with_retries(lambda: minio_download_hash(name),
                                  base_wait=2, cap=10)
            out["minio"] = {"ok": actual == expected_hash,
                            "result": "PASSED" if actual == expected_hash else "FAILED",
                            "sha256": actual}
        except AwsError as ex:
            if ex.status == 404:
                out["minio"] = {"ok": False, "result": "MISSING"}
            else:
                out["minio"] = {"ok": False, "result": "ERROR",
                                "error": str(ex)[:200]}
        except Exception as ex:  # noqa: BLE001 - reported, never swallowed
            out["minio"] = {"ok": False, "result": "ERROR",
                            "error": str(ex)[:200]}
    if ceph_backend.configured():
        out["ceph"] = _verify_s3compat(ceph_backend, name, expected_hash)
    if gcs_backend.configured():
        out["gcs"] = _verify_s3compat(gcs_backend, name, expected_hash)
    return out

