#!/usr/bin/env python3
"""Stage 0 fleet backup — snapshot live-servers stateful data to S3.

Runs ON the audited host (dispatch via .github/workflows/stage0-fleet-backup.yml,
which scp's this file to the host and executes it; the run log is the record).

What it does, all online (no restarts, no downtime):
  * For each live-servers Docker volume: stream a gzipped tar of the volume
    to a staging dir using a throwaway container from an image already on
    the host (postgres:16-alpine's busybox tar).
  * A logical pg_dump (custom format) of the paydb Postgres, plus a
    globals-only dump, produced with docker exec so DB credentials never
    leave the paydb container.
  * SHA-256 + size for every artifact, written to manifest.json.
  * Upload of every artifact and the manifest to S3 with a stdlib-only
    SigV4 signer (host has no aws CLI / boto3). Credentials are read from
    the host .env ZILLION_AWS_* entries — values are never printed.
  * Verification: HEAD on every uploaded object (size must match) and a
    ListObjectsV2 of the backup prefix, printed as the final table.

Bucket: the only S3 credential path on this host is the Zillion one, so
the default target is zillion-tb-aws-bucket (management account
392349106458) under prefix fleet-backups/<YYYY-MM-DD>/<timestamp>/.
Override with BACKUP_BUCKET / BACKUP_PREFIX / BACKUP_REGION env vars.

Local reuse: keep at ~/workspace/fleet-rebuild/fleet_backup.py; the copy
in the repo (ops/fleet_backup.py) is what the workflow ships to the host.
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

HOST_TREE = "/home/deploy/live-servers"
ENV_PATH = os.path.join(HOST_TREE, ".env")
BUCKET = os.environ.get("BACKUP_BUCKET", "zillion-tb-aws-bucket")
REGION = os.environ.get("BACKUP_REGION", "us-west-2")
PREFIX_ROOT = os.environ.get("BACKUP_PREFIX", "fleet-backups")
TAR_IMAGE = os.environ.get("BACKUP_TAR_IMAGE", "postgres:16-alpine")
PAYDB_CONTAINER = os.environ.get("BACKUP_PAYDB_CONTAINER", "live-paydb")

VOLUME_SUFFIXES = [
    "members-data",
    "payledger-data",
    "zillion-data",
    "minio-data",
    "vpn-data",
    "xsave-data",
    "maclan-data",
    "celltower-data",
    "fileslib-data",
    "syncflow-data",
]


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_env_keys(path, wanted):
    """Read selected keys from a .env file. Values never printed."""
    found = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in wanted:
                found[key] = val.strip().strip('"').strip("'")
    return found


# ---------- SigV4 (stdlib) ----------

def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(method, host, canonical_uri, query, payload_hash, creds, extra=None):
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    headers = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if extra:
        headers.update(extra)
    signed_keys = sorted(headers)
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in signed_keys)
    canonical_request = "\n".join([
        method, canonical_uri, query, canonical_headers,
        ";".join(signed_keys), payload_hash,
    ])
    scope = f"{date_stamp}/{REGION}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k = _sign(("AWS4" + creds["secret"]).encode("utf-8"), date_stamp)
    k = _sign(k, REGION)
    k = _sign(k, "s3")
    k = _sign(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds['id']}/{scope}, "
        f"SignedHeaders={';'.join(signed_keys)}, Signature={signature}"
    )
    return headers


def s3_request(method, key, creds, body_path=None, payload_hash=None, query=""):
    host = f"{BUCKET}.s3.{REGION}.amazonaws.com"
    uri = "/" + key
    if body_path is not None:
        payload_hash = sha256_of(body_path)
    elif payload_hash is None:
        payload_hash = hashlib.sha256(b"").hexdigest()
    headers = sigv4_headers(method, host, uri, query, payload_hash, creds)
    url = f"https://{host}{uri}" + (f"?{query}" if query else "")
    data = None
    if body_path is not None:
        data = open(body_path, "rb")
        headers["Content-Length"] = str(os.path.getsize(body_path))
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        print(f"S3 {method} {key} FAILED: HTTP {e.code}: {e.read()[:300]!r}", flush=True)
        raise


# ---------- backup steps ----------

def discover_volumes():
    out = run(["docker", "volume", "ls", "--format", "{{.Name}}"],
              capture_output=True, text=True).stdout.split()
    chosen = []
    for suffix in VOLUME_SUFFIXES:
        match = [v for v in out if v == suffix or v.endswith("_" + suffix)]
        if match:
            chosen.append((suffix, match[0]))
        else:
            print(f"volume for {suffix}: NOT PRESENT — skipped", flush=True)
    return chosen


def tar_volume(volume, dest_path):
    cmd = ["docker", "run", "--rm", "-v", f"{volume}:/data:ro",
           "--entrypoint", "tar", TAR_IMAGE, "czf", "-", "-C", "/data", "."]
    with open(dest_path, "wb") as f:
        run(cmd, stdout=f)


def pg_dumps(stage_dir, artifacts):
    dump = os.path.join(stage_dir, "paydb.dump")
    cmd = ["docker", "exec", PAYDB_CONTAINER, "sh", "-c",
           'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc']
    with open(dump, "wb") as f:
        run(cmd, stdout=f)
    artifacts.append(("paydb.dump", dump, "pg_dump custom format of paydb"))
    globals_path = os.path.join(stage_dir, "paydb-globals.sql")
    cmd = ["docker", "exec", PAYDB_CONTAINER, "sh", "-c",
           'pg_dumpall -U "$POSTGRES_USER" --globals-only']
    with open(globals_path, "wb") as f:
        run(cmd, stdout=f)
    artifacts.append(("paydb-globals.sql", globals_path, "pg_dumpall globals-only"))


def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = f"{PREFIX_ROOT}/{day}/{ts}"
    stage_dir = f"/home/deploy/fleet-backups/{ts}"
    os.makedirs(stage_dir, exist_ok=True)
    print(f"Stage 0 fleet backup — staging at {stage_dir}", flush=True)
    print(f"Target: s3://{BUCKET}/{prefix}/ (region {REGION})", flush=True)

    creds_env = read_env_keys(ENV_PATH, {"ZILLION_AWS_ACCESS_KEY_ID",
                                         "ZILLION_AWS_SECRET_ACCESS_KEY"})
    if len(creds_env) != 2:
        print("ERROR: ZILLION_AWS_* credentials not found in host .env — aborting",
              flush=True)
        return 1
    creds = {"id": creds_env["ZILLION_AWS_ACCESS_KEY_ID"],
             "secret": creds_env["ZILLION_AWS_SECRET_ACCESS_KEY"]}
    print("Credentials loaded from host .env (values not shown)", flush=True)

    artifacts = []  # (name, path, note)
    for suffix, volume in discover_volumes():
        dest = os.path.join(stage_dir, f"volume-{suffix}.tar.gz")
        print(f"tarring volume {volume} -> {dest}", flush=True)
        tar_volume(volume, dest)
        artifacts.append((f"volume-{suffix}.tar.gz", dest, f"docker volume {volume}"))

    pg_dumps(stage_dir, artifacts)

    manifest = {"created_utc": ts, "bucket": BUCKET, "prefix": prefix,
                "region": REGION, "artifacts": []}
    for name, path, note in artifacts:
        size = os.path.getsize(path)
        digest = sha256_of(path)
        manifest["artifacts"].append(
            {"name": name, "size": size, "sha256": digest, "note": note})
        print(f"artifact {name}: {size} bytes sha256={digest}", flush=True)

    manifest_path = os.path.join(stage_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print("== uploading ==", flush=True)
    for name, path, _note in artifacts:
        key = f"{prefix}/{name}"
        status, _h, _b = s3_request("PUT", key, creds, body_path=path)
        print(f"PUT {key}: HTTP {status}", flush=True)
    status, _h, _b = s3_request("PUT", f"{prefix}/manifest.json", creds,
                                body_path=manifest_path)
    print(f"PUT {prefix}/manifest.json: HTTP {status}", flush=True)

    print("== verifying (HEAD per object) ==", flush=True)
    ok = True
    for entry in manifest["artifacts"]:
        key = f"{prefix}/{entry['name']}"
        status, headers, _b = s3_request("HEAD", key, creds)
        remote_size = int(headers.get("Content-Length", -1))
        match = remote_size == entry["size"]
        ok = ok and match
        print(f"HEAD {key}: HTTP {status} size={remote_size} "
              f"local={entry['size']} {'OK' if match else 'MISMATCH'}", flush=True)

    print("== verifying (ListObjectsV2) ==", flush=True)
    from urllib.parse import quote
    query = "list-type=2&prefix=" + quote(prefix + "/", safe="")
    status, _h, body = s3_request("GET", "", creds, query=query)
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    root = ET.fromstring(body)
    for contents in root.findall("s3:Contents", ns):
        k = contents.find("s3:Key", ns).text
        s = contents.find("s3:Size", ns).text
        print(f"LIST {k} size={s}", flush=True)

    subprocess.run(["rm", "-rf", stage_dir])
    print(f"Staging dir removed. Backup {'VERIFIED' if ok else 'COMPLETED WITH MISMATCHES'}: "
          f"s3://{BUCKET}/{prefix}/", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
