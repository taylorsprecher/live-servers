#!/usr/bin/env python3
"""Spec Matrix — cross-brand device & camera spec analyzer (static).

Python 3 stdlib ONLY (http.server). Serves ONE self-contained page
(index.html: dataset, CSS and JS all embedded — no external requests,
no build step) under the /specmatrix base path. The page is a faithful
port of the React component Taylor pasted on 2026-11-11
(~/workspace/user/files/SpecMatrix_App_Pasted.jsx): 15-device dataset,
Side-by-Side matrix, Catalog, Score Radar analytics and Deep Scanner
views, search/brand/category/price filters, pin-to-compare (max 4),
diff highlighting, device modal, and copy-comparison-report.

Stateless: nothing is stored; compare pins live in the page only.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_PATH = os.environ.get("BASE_PATH", "/specmatrix")
PORT = int(os.environ.get("PORT", "8000"))
INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = "SpecMatrix/1.0"

    def log_message(self, fmt, *args):  # keep container logs quiet
        pass

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in (BASE_PATH, BASE_PATH + "/"):
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == BASE_PATH + "/health":
            body = json.dumps({"status": "ok", "service": "specmatrix"}).encode()
            self._send(200, body, "application/json")
        else:
            body = json.dumps({"error": "not found"}).encode()
            self._send(404, body, "application/json")

    do_HEAD = do_GET


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
