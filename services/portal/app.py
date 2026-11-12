#!/usr/bin/env python3
"""Network Portal — 911transedu.net live-status dashboard (static).

Python 3 stdlib ONLY (http.server). Serves ONE page (index.html: CSS
and JS embedded) under the /portal base path. The page is the
live-status portal Taylor pasted on 2026-11-12 and directed to be put
live (verbatim source: Section 1 of
~/workspace/user/files/Other_AI_Upgraded_Portal_Configs_Docs_2026-11-12.md).
On load it probes 911transedu.net and the eight live-server subdomains
client-side (fetch, no-cors) and shows a green/red reachability dot per
card, with a manual Refresh Status button.

Stateless: nothing is stored; the probes run in the visitor's browser.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_PATH = os.environ.get("BASE_PATH", "/portal")
PORT = int(os.environ.get("PORT", "8000"))
INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = "Portal/1.0"

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
            body = json.dumps({"status": "ok", "service": "portal"}).encode()
            self._send(200, body, "application/json")
        else:
            body = json.dumps({"error": "not found"}).encode()
            self._send(404, body, "application/json")

    do_HEAD = do_GET


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
