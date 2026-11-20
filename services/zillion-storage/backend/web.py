#!/usr/bin/env python3
"""Zillion Storage — HTTP layer: the request handler and server
bootstrap. Moved verbatim from backend/server.py; the only new route
is GET {BASE}/assets/fleet.css (the vendored Fleet design system)."""
import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from aws_backend import s3_configured, s3_delete
from backends import backend_health, replicate, verify
from common import _lock, load_manifest, save_manifest, with_retries
from config import BACKENDS, BASE, CHUNK, DATA, NAME_OK, PORT, STARTED
from minio_backend import minio_configured, minio_delete
from s3compat import ceph_backend, gcs_backend

FLEET_CSS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "assets", "fleet.css")

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep logs quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        p = urlparse(self.path).path
        if BASE and p.startswith(BASE):
            p = p[len(BASE):]
        return p or "/"

    def do_GET(self):
        p = self._path()
        if p == "/assets/fleet.css":
            try:
                with open(FLEET_CSS, "rb") as fh:
                    self._send(200, fh.read(), "text/css; charset=utf-8")
            except Exception:
                self._send(404, {"error": "not found"})
            return
        if p in ("/", ""):
            index = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
            try:
                with open(index, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(200, {"service": "zillion-storage"})
            return
        if p == "/health":
            with _lock:
                manifest = load_manifest()
            self._send(200, {
                "status": "ok",
                "service": "zillion-storage",
                "design": "Python Cloud - 1 Zillion TB (design name; real capacity in backends)",
                "backends": backend_health(),
                "objects": len(manifest),
                "bytes_stored": sum(v.get("size", 0) for v in manifest.values()),
                "uptime_seconds": int(time.time() - STARTED),
            })
            return
        if p == "/objects":
            with _lock:
                manifest = load_manifest()
            items = [{"name": k, **{f: v[f] for f in ("size", "sha256", "stored_at") if f in v}}
                     for k, v in sorted(manifest.items())]
            self._send(200, {"objects": items})
            return
        if p.startswith("/objects/"):
            name = unquote(p[len("/objects/"):])
            if not NAME_OK.match(name):
                self._send(400, {"error": "bad object name"})
                return
            for b in BACKENDS:
                path = os.path.join(b["path"], name)
                if os.path.exists(path):
                    size = os.path.getsize(path)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    with open(path, "rb") as fh:
                        while True:
                            block = fh.read(CHUNK)
                            if not block:
                                break
                            self.wfile.write(block)
                    return
            self._send(404, {"error": "object not found"})
            return
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        p = self._path()
        if not p.startswith("/objects/"):
            self._send(404, {"error": "not found"})
            return
        name = unquote(p[len("/objects/"):])
        if not NAME_OK.match(name):
            self._send(400, {"error": "bad object name"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        os.makedirs(DATA, exist_ok=True)
        tmp_path = os.path.join(DATA, ".ingest-" + str(int(time.time() * 1000)))
        h = hashlib.sha256()
        written = 0
        with open(tmp_path, "wb") as fh:
            remaining = length
            while remaining > 0:
                block = self.rfile.read(min(CHUNK, remaining))
                if not block:
                    break
                fh.write(block)
                h.update(block)
                written += len(block)
                remaining -= len(block)
        digest = h.hexdigest()
        results = replicate(tmp_path, name)
        os.unlink(tmp_path)
        checks = verify(name, digest)
        with _lock:
            manifest = load_manifest()
            manifest[name] = {
                "size": written, "sha256": digest,
                "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "backends": sorted(k for k, v in results.items() if v.get("ok")),
            }
            save_manifest(manifest)
        self._send(200, {
            "name": name, "size": written, "sha256": digest,
            "backends": results, "verification": checks,
            "verified": all(v.get("result") == "PASSED" for v in checks.values()),
        })

    do_POST = do_PUT

    def do_DELETE(self):
        p = self._path()
        if p.startswith("/verify/"):
            self._send(405, {"error": "verify is POST"})
            return
        if not p.startswith("/objects/"):
            self._send(404, {"error": "not found"})
            return
        name = unquote(p[len("/objects/"):])
        if not NAME_OK.match(name):
            self._send(400, {"error": "bad object name"})
            return
        removed = []
        for b in BACKENDS:
            path = os.path.join(b["path"], name)
            if os.path.exists(path):
                os.unlink(path)
                removed.append(b["name"])
        delete_errors = {}
        if s3_configured():
            try:
                with_retries(lambda: s3_delete(name), base_wait=2, cap=10)
                removed.append("aws-s3")
            except Exception as ex:  # noqa: BLE001 - reported in the response
                delete_errors["aws-s3"] = str(ex)[:200]
        if minio_configured():
            try:
                with_retries(lambda: minio_delete(name), base_wait=2, cap=10)
                removed.append("minio")
            except Exception as ex:  # noqa: BLE001 - reported in the response
                delete_errors["minio"] = str(ex)[:200]
        if ceph_backend.configured():
            try:
                with_retries(lambda: ceph_backend.delete(name), base_wait=2, cap=10)
                removed.append("ceph")
            except Exception as ex:  # noqa: BLE001 - reported in the response
                delete_errors["ceph"] = str(ex)[:200]
        if gcs_backend.configured():
            try:
                with_retries(lambda: gcs_backend.delete(name), base_wait=2, cap=10)
                removed.append("gcs")
            except Exception as ex:  # noqa: BLE001 - reported in the response
                delete_errors["gcs"] = str(ex)[:200]
        with _lock:
            manifest = load_manifest()
            manifest.pop(name, None)
            save_manifest(manifest)
        body = {"name": name, "removed_from": removed}
        if delete_errors:
            body["delete_errors"] = delete_errors
        self._send(200, body)


def main():
    # POST /verify/<name> is routed here by method override below.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"zillion-storage listening on :{PORT} base={BASE} data={DATA}", flush=True)
    server.serve_forever()


# Route POST /verify/<name> distinctly from PUT-style uploads.
_orig_post = Handler.do_POST


def _post(self):
    p = self._path()
    if p.startswith("/verify/"):
        name = unquote(p[len("/verify/"):])
        if not NAME_OK.match(name):
            self._send(400, {"error": "bad object name"})
            return
        with _lock:
            manifest = load_manifest()
        entry = manifest.get(name)
        if not entry:
            self._send(404, {"error": "object not found"})
            return
        checks = verify(name, entry["sha256"])
        self._send(200, {"name": name, "sha256": entry["sha256"], "verification": checks,
                         "verified": all(v.get("result") == "PASSED" for v in checks.values())})
        return
    _orig_post(self)


Handler.do_POST = _post
