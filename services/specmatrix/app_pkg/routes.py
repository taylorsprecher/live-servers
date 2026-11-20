"""Spec Matrix — route dispatch. Every route of the original app.py,
verbatim, plus the one allowed addition: GET {BASE}/assets/fleet.css."""
import json
from http.server import BaseHTTPRequestHandler

from .config import BASE_PATH
from .pages import FLEET_CSS, INDEX_HTML


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
        elif path == BASE_PATH + "/assets/fleet.css":
            self._send(200, FLEET_CSS, "text/css; charset=utf-8")
        else:
            body = json.dumps({"error": "not found"}).encode()
            self._send(404, body, "application/json")

    do_HEAD = do_GET
